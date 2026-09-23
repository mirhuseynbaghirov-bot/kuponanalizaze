import os
import time
import asyncio
import logging
import threading
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("kuponbot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ODDS_API_KEY = os.environ["ODDS_API_KEY"]
ADMIN_ID = os.environ.get("ADMIN_ID")  # ixtiyari: sənin Telegram ID-n, xəbərdarlıq üçün
TZ = ZoneInfo("Asia/Baku")

# Popular leagues (The Odds API sport keys)
LEAGUES = [
    "soccer_uefa_champs_league",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
]

# Each tier: odds band for a single pick, and target total odds range
TIERS = {
    "safe": {"name": "🛡 Ehtiyatlı kupon", "pick_lo": 1.15, "pick_hi": 1.60, "lo": 2.0, "hi": 5.0},
    "normal": {"name": "⚖️ Normal kupon", "pick_lo": 1.40, "pick_hi": 2.20, "lo": 5.0, "hi": 10.0},
    "risky": {"name": "🔥 Riskli kupon", "pick_lo": 1.70, "pick_hi": 3.00, "lo": 10.0, "hi": 20.0},
}

# ---------- Keş: hər liqa gündə bir dəfə çəkilir (kredit qənaəti) ----------
RETRY_AFTER_FAIL = 10 * 60  # uğursuz liqanı 10 dəqiqədən sonra yenidən yoxla
_cache = {}  # liqa -> {"day", "t", "ok", "picks"}
_lock = threading.Lock()

# ---------- Sağlamlıq yoxlaması (watchdog) ----------
BEAT_EVERY = 30
DEAD_AFTER = 180  # saniyə: bu qədər heartbeat yoxdursa proses yenidən başladılır
_beat = {"t": time.time()}
_last_admin_alert = {"t": 0.0}


def fair_probs(odds_by_name):
    """Remove bookmaker margin: normalise implied probabilities."""
    inv = {k: 1 / v for k, v in odds_by_name.items()}
    total = sum(inv.values())
    return {k: v / total for k, v in inv.items()}


def avg(values):
    return sum(values) / len(values)


def fetch_league(league, today):
    """Bir liqanın bu günkü matç variantlarını qaytarır: (picks, ok)."""
    picks = []
    try:
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{league}/odds/",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "eu",
                "markets": "h2h,totals",
                "oddsFormat": "decimal",
            },
            timeout=20,
        )
        log.info(
            "%s | status=%s | qalan kredit=%s",
            league, r.status_code, r.headers.get("x-requests-remaining"),
        )
        if r.status_code != 200:
            log.warning("%s | API xətası: %s", league, r.text[:200])
            return picks, False

        for ev in r.json():
            start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).astimezone(TZ)
            if start.date() != today:
                continue
            home, away = ev["home_team"], ev["away_team"]
            h2h = defaultdict(list)
            totals = defaultdict(list)
            for bk in ev["bookmakers"]:
                for m in bk["markets"]:
                    if m["key"] == "h2h":
                        for o in m["outcomes"]:
                            h2h[o["name"]].append(o["price"])
                    elif m["key"] == "totals":
                        for o in m["outcomes"]:
                            if o.get("point") == 2.5:
                                totals[o["name"]].append(o["price"])
            title = f"{home} - {away}"
            when = start.strftime("%H:%M")
            if len(h2h) == 3:
                odds = {k: avg(v) for k, v in h2h.items()}
                probs = fair_probs(odds)
                labels = {home: f"{home} qalib", away: f"{away} qalib", "Draw": "Heç-heçə"}
                for k in odds:
                    picks.append(dict(match=title, time=when, pick=labels.get(k, k),
                                      odds=odds[k], prob=probs[k]))
            if len(totals) == 2:
                odds = {k: avg(v) for k, v in totals.items()}
                probs = fair_probs(odds)
                labels = {"Over": "2.5 Üst", "Under": "2.5 Alt"}
                for k in odds:
                    picks.append(dict(match=title, time=when, pick=labels.get(k, k),
                                      odds=odds[k], prob=probs[k]))
        return picks, True
    except Exception:
        log.exception("%s | liqa çəkilərkən xəta", league)
        return [], False


def get_picks():
    """Bütün liqaların variantları + hamısı uğurlu olub-olmadığı."""
    today = datetime.now(TZ).date()
    all_picks, all_ok = [], True
    with _lock:
        for league in LEAGUES:
            c = _cache.get(league)
            fresh = (
                c
                and c["day"] == today
                and (c["ok"] or time.time() - c["t"] < RETRY_AFTER_FAIL)
            )
            if not fresh:
                picks, ok = fetch_league(league, today)
                c = {"day": today, "t": time.time(), "ok": ok, "picks": picks}
                _cache[league] = c
            all_picks += c["picks"]
            all_ok = all_ok and c["ok"]
    return all_picks, all_ok


def build_coupon(picks, tier):
    t = TIERS[tier]
    cands = [p for p in picks if t["pick_lo"] <= p["odds"] <= t["pick_hi"]]
    cands.sort(key=lambda p: p["prob"], reverse=True)  # most likely first
    chosen, used, total = [], set(), 1.0
    for p in cands:
        if p["match"] in used:
            continue
        if total * p["odds"] > t["hi"]:
            continue
        chosen.append(p)
        used.add(p["match"])
        total *= p["odds"]
        if total >= t["lo"]:
            break
    return chosen, total


def format_coupon(tier, chosen, total):
    t = TIERS[tier]
    if not chosen:
        return f"{t['name']}\n\nBu gün üçün uyğun matç tapılmadı."
    win_prob = 1.0
    lines = [f"{t['name']} (ümumi kef ≈ {total:.2f})\n"]
    for i, p in enumerate(chosen, 1):
        win_prob *= p["prob"]
        lines.append(f"{i}. {p['match']} ({p['time']})\n   ➜ {p['pick']} | kef {p['odds']:.2f}")
    lines.append(f"\nÜmumi uduş ehtimalı (bazar əsasında): ~{win_prob * 100:.0f}%")
    if total < t["lo"]:
        lines.append("⚠️ Bu gün kifayət qədər matç olmadığı üçün hədəf kefə çatmadı.")
    lines.append("\n⚠️ Zəmanət yoxdur. Yalnız itirə biləcəyin məbləği qoy.")
    return "\n".join(lines)


async def alert_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    """Admin-ə saatda ən çox 1 xəbərdarlıq göndərir."""
    if not ADMIN_ID or time.time() - _last_admin_alert["t"] < 3600:
        return
    _last_admin_alert["t"] = time.time()
    try:
        await context.bot.send_message(int(ADMIN_ID), text)
    except Exception:
        log.exception("Admin-ə mesaj göndərilə bilmədi")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Salam! Günün kuponunu almaq üçün /gununoyunlari yaz.")


async def gununoyunlari(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔥 Riskli kupon (10-20)", callback_data="risky")],
        [InlineKeyboardButton("⚖️ Normal kupon (5-10)", callback_data="normal")],
        [InlineKeyboardButton("🛡 Ehtiyatlı kupon (1-5)", callback_data="safe")],
    ])
    await update.message.reply_text("Hansı kuponu istəyirsən?", reply_markup=kb)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tier = q.data
    if tier not in TIERS:
        return
    await q.message.reply_text("Matçlar yoxlanılır...")
    try:
        picks, ok = await asyncio.to_thread(get_picks)
    except Exception:
        log.exception("get_picks xətası")
        await q.message.reply_text("⚠️ Xəta baş verdi. Bir az sonra yenidən yoxla.")
        return
    if not picks and not ok:
        await alert_admin(context, "⚠️ Kupon botu: matç məlumatı alınmır (Odds API limiti bitmiş ola bilər). Render Logs-a bax.")
        await q.message.reply_text("⚠️ Matç məlumatı hazırda alına bilmir. Bir az sonra yenidən yoxla.")
        return
    chosen, total = build_coupon(picks, tier)
    await q.message.reply_text(format_coupon(tier, chosen, total))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Handler xətası", exc_info=context.error)


# ---------- Heartbeat + watchdog ----------
async def heartbeat_loop(app: Application):
    """Bot event loop-u canlıdırsa və polling işləyirsə, hər 30 san. nəbz yazır."""
    while True:
        up = app.updater
        if up is None or getattr(up, "running", True):
            _beat["t"] = time.time()
        await asyncio.sleep(BEAT_EVERY)


async def post_init(app: Application):
    app.bot_data["hb"] = asyncio.create_task(heartbeat_loop(app))


def watchdog():
    """Nəbz kəsilibsə prosesi öldürür; Render onu avtomatik yenidən başladır."""
    while True:
        time.sleep(BEAT_EVERY)
        if time.time() - _beat["t"] > DEAD_AFTER:
            log.error("Heartbeat yoxdur, bot donub. Proses yenidən başladılır.")
            os._exit(1)


class Ping(BaseHTTPRequestHandler):
    def _status(self):
        return 200 if time.time() - _beat["t"] < DEAD_AFTER else 500

    def do_GET(self):
        code = self._status()
        self.send_response(code)
        self.end_headers()
        self.wfile.write(b"ok" if code == 200 else b"bot down")

    def do_HEAD(self):
        self.send_response(self._status())
        self.end_headers()

    def log_message(self, *args):
        pass


def keep_alive():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


if __name__ == "__main__":
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("gununoyunlari", gununoyunlari))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    # drop_pending_updates: yenidən başlayanda köhnə yığılmış mesajlara cavab yağdırmasın
    app.run_polling(drop_pending_updates=True)
