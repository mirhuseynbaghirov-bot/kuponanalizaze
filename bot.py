"""
KUPON BOT — futbol kupon köməkçisi (Azərbaycan məntiqinə uyğun, çoxdilli).

Mühit dəyişənləri (Render → Environment):
  BOT_TOKEN                 (məcburi) BotFather tokeni
  ODDS_API_KEY              (məcburi) the-odds-api.com açarı
  ADMIN_ID                  (tövsiyə) Telegram ID-n; birdən çox olsa vergüllə: 123,456
  UPSTASH_REDIS_REST_URL    (tövsiyə) daimi statistika + kredit qorunması üçün
  UPSTASH_REDIS_REST_TOKEN  (tövsiyə) yuxarıdakı ilə birlikdə
  DAILY_CREDIT_BUDGET       (ixtiyari) gündə maksimum Odds API krediti, default 16
  DEFAULT_LANG              (ixtiyari) az / en, default az
  CONTACT                   (ixtiyari) məxfilik mətnində göstərilən əlaqə (@username)

Əsas fikirlər:
  * Kupon ağıllı qurulur: oyun sayı növə görə dəyişir (ehtiyatlı 3-4, normal 5-6,
    riskli 7-9), ümumi kef hədəf aralığına salınır, aşağı liqalar yalnız riskli
    və normal kuponlarda, məhdud sayda görünür.
  * Odds API kreditinə qənaət: liqalar və oyunlar PULSUZ endpoint-lərlə tapılır,
    kredit yalnız bu gün oyunu olan liqalara xərclənir, gündəlik büdcə var.
  * Statistika (/admin): kim, neçə dəfə, hansı mənbədən (reklam linki) gəlib.
  * Watchdog: bot donsa proses özü yenidən başlayır.
  * Mühit dəyişənlərindəki artıq boşluq / yeni sətir / dırnaq avtomatik təmizlənir.
"""
import asyncio
import json
import logging
import math
import os
import random
import re
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import requests
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("kuponbot")


# ============================== KONFİQURASİYA ==============================
def env(name, default=""):
    """Mühit dəyişənini oxuyur; başdakı/sondakı boşluq, \\n və dırnaqları təmizləyir."""
    return os.environ.get(name, default).strip().strip("\"'").strip()


BOT_TOKEN = env("BOT_TOKEN")
ODDS_API_KEY = env("ODDS_API_KEY")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN təyin edilməyib (Render → Environment)")
if not ODDS_API_KEY:
    raise RuntimeError("ODDS_API_KEY təyin edilməyib (Render → Environment)")

ADMIN_IDS = {int(x) for x in re.findall(r"\d+", env("ADMIN_ID"))}
DEFAULT_LANG = env("DEFAULT_LANG", "az")
DAILY_CREDIT_BUDGET = int(env("DAILY_CREDIT_BUDGET", "16") or 16)
CONTACT = env("CONTACT")
TZ = ZoneInfo("Asia/Baku")

ODDS_BASE = "https://api.the-odds-api.com/v4"
CREDIT_RESERVE = 5          # kreditin bu qədəri həmişə ehtiyatda qalır
RETRY_AFTER_FAIL = 10 * 60  # uğursuz yükləməni 10 dəq. sonra yenidən yoxla
COOLDOWN = 2.0              # eyni istifadəçinin düymə basma fasiləsi (saniyə)
MIN_MINUTES_BEFORE_KICKOFF = 15  # başlamağa 15 dəq. qalmış oyunları kupona salma

# Məşhur liqalar: hər biri üçün 2 market (h2h + totals) = 2 kredit.
# Siyahıda olan liqa Odds API-da yoxdursa, sadəcə ignor olunur.
TOP_LEAGUES = [
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_turkey_super_league",
    "soccer_netherlands_eredivisie",
    "soccer_portugal_primeira_liga",
]

# Kupon növləri.
#  pick_lo/pick_hi : tək oyunun kef aralığı
#  lo/hi           : ÜMUMİ kef aralığı
#  legs            : {oyun sayı: çəki} — sayı təsadüfi seçilir (həmişə eyni olmasın)
#  min_legs        : oyun azdırsa düşə biləcəyi minimum
#  extra_w         : {əlavə/aşağı liqadan neçə oyun icazəlidir: çəki} — hər kupon üçün
#                    təsadüfi seçilir, yəni bəzən 0 olur, həmişə Ekvador/Argentina çıxmır
#  league_cap      : eyni liqadan maksimum oyun
TIERS = {
    "safe": dict(icon="🛡", pick_lo=1.15, pick_hi=1.60, lo=2.0, hi=5.0,
                 legs={3: 5, 4: 5}, min_legs=3, extra_w={0: 1}, league_cap=2),
    "normal": dict(icon="⚖️", pick_lo=1.20, pick_hi=1.90, lo=5.0, hi=10.0,
                   legs={5: 5, 6: 5}, min_legs=4, extra_w={0: 6, 1: 4}, league_cap=2),
    "risky": dict(icon="🔥", pick_lo=1.25, pick_hi=2.10, lo=10.0, hi=22.0,
                  legs={7: 2, 8: 4, 9: 4}, min_legs=6, extra_w={0: 4, 1: 3, 2: 2, 3: 1},
                  league_cap=3),
}
MAX_PER_KIND = {"dc12": 2, "over": 3, "under": 3}  # kuponda eyni tip çox təkrarlanmasın

# ============================== MƏTNLƏR (i18n) ==============================
# Yeni dil əlavə etmək üçün TXT["ru"] = {...} kimi eyni açarlarla lüğət əlavə et
# və LANG_BUTTONS, COMMANDS-a da yaz.
TXT = {
    "az": {
        "welcome": (
            "👋 Salam! Mən futbol kupon köməkçisiyəm.\n\n"
            "Oyunların bazar kefləri əsasında ehtiyatlı, normal və riskli kupon hazırlayıram.\n\n"
            "🎯 Kupon üçün: /gununoyunlari\n"
            "🌐 Dil: /lang · 🔒 Məxfilik: /privacy\n\n"
            "⚠️ 18+ · Mərc risklidir, zəmanət yoxdur. Yalnız itirə biləcəyin məbləği qoy."
        ),
        "menu_title": "Hansı kuponu istəyirsən?",
        "btn_coupon": "🎯 Kupon al",
        "btn_safe": "🛡 Ehtiyatlı · 3-4 oyun",
        "btn_normal": "⚖️ Normal · 5-6 oyun",
        "btn_risky": "🔥 Riskli · çox oyunlu",
        "btn_again": "🔄 Başqa variant",
        "btn_menu": "📋 Menyu",
        "checking": "⏳ Oyunlar yoxlanılır...",
        "name_safe": "Ehtiyatlı kupon",
        "name_normal": "Normal kupon",
        "name_risky": "Riskli kupon",
        "head": "{icon} {name}\n{n} oyun · ümumi kef ≈ {total}",
        "tz_note": "🕒 Vaxtlar Bakı vaxtı ilədir.",
        "odds_word": "kef",
        "prob": "📈 Bazar əsasında uduş ehtimalı: ~{p}%",
        "short_note": "ℹ️ Bu gün kifayət qədər uyğun oyun olmadığı üçün hədəf kefə/oyun sayına tam çatmadı.",
        "approx_note": "≈ təxmini kefdir (1X, X2, 12 üçün hesablanır). Mərc etməzdən əvvəl mərc şirkətində yoxla.",
        "disclaimer": "⚠️ 18+ · Zəmanət yoxdur. Kefləri mərc şirkətində yoxla. Yalnız itirə biləcəyin məbləği qoy.",
        "no_matches": "😕 Bu gün üçün uyğun oyun tapılmadı. Bir az sonra yenidən yoxla.",
        "data_error": "⚠️ Oyun məlumatı hazırda alına bilmir. Bir az sonra yenidən yoxla.",
        "gen_error": "⚠️ Xəta baş verdi. Bir az sonra yenidən yoxla.",
        "lang_prompt": "🌐 Dili seç / Choose language:",
        "lang_set": "✅ Dil: Azərbaycanca",
        "privacy": (
            "🔒 Məxfilik\n\nBot statistika üçün yalnız bunları saxlayır: Telegram ID, istifadəçi adı, "
            "seçdiyin dil və kupon sorğularının sayı. Mesajlarının məzmunu saxlanmır."
        ),
        "privacy_contact": "\nMəlumatın silinməsi üçün: {contact}",
        "your_id": "Sənin Telegram ID-n: {id}",
        "p_win": "{team} qalib",
        "p_1x": "{team} məğlub olmaz (1X)",
        "p_x2": "{team} məğlub olmaz (X2)",
        "p_draw": "Heç-heçə",
        "p_dc12": "Heç-heçə olmaz (12)",
        "p_over": "2.5 Üst",
        "p_under": "2.5 Alt",
        # admin
        "a_title": "📊 Statistika — {day}",
        "a_today": "👥 İstifadəçi: {u} · 🎫 Kupon: {c}",
        "a_tiers": "🛡 {s} · ⚖️ {n} · 🔥 {r}",
        "a_new": "🆕 Yeni: {n}",
        "a_total": "👤 Ümumi istifadəçi: {n}",
        "a_days": "📅 Son {d} gün",
        "a_row": "{day} — {u} user · {c} kupon · {n} yeni",
        "a_top": "🏆 Bu gün ən aktiv",
        "a_mem_ok": "💾 Yaddaş: Redis (daimi)",
        "a_mem_tmp": "⚠️ Yaddaş müvəqqətidir (restartda sıfırlanır). UPSTASH_* əlavə et.",
        "a_snap": "⚽ Bazada: {m} oyun · {l} liqa · xərclənən kredit {s} · qalan {r}",
        "a_snap_none": "⚽ Oyun bazası hələ yüklənməyib.",
        "a_none": "hələ yoxdur",
    },
    "en": {
        "welcome": (
            "👋 Hi! I'm a football coupon assistant.\n\n"
            "I build safe, normal and risky coupons from market odds.\n\n"
            "🎯 Get a coupon: /coupon\n"
            "🌐 Language: /lang · 🔒 Privacy: /privacy\n\n"
            "⚠️ 18+ · Betting is risky, nothing is guaranteed. Only stake what you can afford to lose."
        ),
        "menu_title": "Which coupon do you want?",
        "btn_coupon": "🎯 Get coupon",
        "btn_safe": "🛡 Safe · 3-4 games",
        "btn_normal": "⚖️ Normal · 5-6 games",
        "btn_risky": "🔥 Risky · many games",
        "btn_again": "🔄 Another variant",
        "btn_menu": "📋 Menu",
        "checking": "⏳ Checking matches...",
        "name_safe": "Safe coupon",
        "name_normal": "Normal coupon",
        "name_risky": "Risky coupon",
        "head": "{icon} {name}\n{n} games · total odds ≈ {total}",
        "tz_note": "🕒 Times are Baku time (UTC+4).",
        "odds_word": "odds",
        "prob": "📈 Market-implied win chance: ~{p}%",
        "short_note": "ℹ️ Not enough suitable games today to fully reach the target odds/number of games.",
        "approx_note": "≈ means estimated odds (calculated for 1X, X2, 12). Check with your bookmaker before betting.",
        "disclaimer": "⚠️ 18+ · No guarantees. Check the odds with your bookmaker. Only stake what you can afford to lose.",
        "no_matches": "😕 No suitable matches found for today. Try again a bit later.",
        "data_error": "⚠️ Match data is unavailable right now. Please try again later.",
        "gen_error": "⚠️ Something went wrong. Please try again later.",
        "lang_prompt": "🌐 Dili seç / Choose language:",
        "lang_set": "✅ Language: English",
        "privacy": (
            "🔒 Privacy\n\nFor statistics the bot stores only: your Telegram ID, username, chosen "
            "language and the number of coupon requests. The content of your messages is not stored."
        ),
        "privacy_contact": "\nTo delete your data contact: {contact}",
        "your_id": "Your Telegram ID: {id}",
        "p_win": "{team} to win",
        "p_1x": "{team} win or draw (1X)",
        "p_x2": "{team} win or draw (X2)",
        "p_draw": "Draw",
        "p_dc12": "No draw (12)",
        "p_over": "Over 2.5",
        "p_under": "Under 2.5",
        "a_title": "📊 Stats — {day}",
        "a_today": "👥 Users: {u} · 🎫 Coupons: {c}",
        "a_tiers": "🛡 {s} · ⚖️ {n} · 🔥 {r}",
        "a_new": "🆕 New: {n}",
        "a_total": "👤 Total users: {n}",
        "a_days": "📅 Last {d} days",
        "a_row": "{day} — {u} users · {c} coupons · {n} new",
        "a_top": "🏆 Most active today",
        "a_mem_ok": "💾 Storage: Redis (persistent)",
        "a_mem_tmp": "⚠️ Storage is temporary (resets on restart). Add UPSTASH_*.",
        "a_snap": "⚽ In database: {m} matches · {l} leagues · credits spent {s} · remaining {r}",
        "a_snap_none": "⚽ Match database not loaded yet.",
        "a_none": "none yet",
    },
}
LANG_BUTTONS = [("az", "🇦🇿 Azərbaycanca"), ("en", "🇬🇧 English")]
COMMANDS = {
    "az": [("start", "Başla"), ("gununoyunlari", "Günün kuponu"),
           ("lang", "Dil / Language"), ("privacy", "Məxfilik")],
    "en": [("start", "Start"), ("coupon", "Today's coupon"),
           ("lang", "Language"), ("privacy", "Privacy")],
}


def T(lang):
    return TXT.get(lang) or TXT[DEFAULT_LANG if DEFAULT_LANG in TXT else "az"]


# ============================== YADDAŞ (KV) ==============================
class MemKV:
    """Müvəqqəti yaddaş (proses yenidən başlayanda silinir). Redis olmayanda istifadə olunur."""
    persistent = False

    def __init__(self):
        self._h = defaultdict(dict)
        self._s = defaultdict(set)
        self._k = {}
        self._lock = threading.Lock()

    def hincrby(self, key, field, n=1):
        with self._lock:
            v = int(self._h[key].get(str(field), 0)) + n
            self._h[key][str(field)] = str(v)
            return v

    def hget(self, key, field):
        with self._lock:
            return self._h[key].get(str(field))

    def hset(self, key, field, value):
        with self._lock:
            self._h[key][str(field)] = str(value)

    def hgetall(self, key):
        with self._lock:
            return dict(self._h.get(key, {}))

    def hlen(self, key):
        with self._lock:
            return len(self._h.get(key, {}))

    def sadd(self, key, member):
        with self._lock:
            before = len(self._s[key])
            self._s[key].add(str(member))
            return 1 if len(self._s[key]) > before else 0

    def scard(self, key):
        with self._lock:
            return len(self._s.get(key, ()))

    def get(self, key):
        with self._lock:
            return self._k.get(key)

    def set(self, key, value, ex=None):
        with self._lock:
            self._k[key] = value


class RedisREST:
    """Upstash Redis (REST). Əlavə paket lazım deyil — yalnız requests."""
    persistent = True

    def __init__(self, url, token):
        self.url = url.strip().rstrip("/")
        self.headers = {"Authorization": f"Bearer {token.strip()}"}

    def _cmd(self, *args):
        r = requests.post(self.url, headers=self.headers,
                          json=[str(a) for a in args], timeout=8)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(data["error"])
        return data.get("result")

    def hincrby(self, key, field, n=1):
        return int(self._cmd("HINCRBY", key, field, n))

    def hget(self, key, field):
        return self._cmd("HGET", key, field)

    def hset(self, key, field, value):
        self._cmd("HSET", key, field, value)

    def hgetall(self, key):
        flat = self._cmd("HGETALL", key) or []
        return dict(zip(flat[::2], flat[1::2]))

    def hlen(self, key):
        return int(self._cmd("HLEN", key) or 0)

    def sadd(self, key, member):
        return int(self._cmd("SADD", key, member))

    def scard(self, key):
        return int(self._cmd("SCARD", key) or 0)

    def get(self, key):
        return self._cmd("GET", key)

    def set(self, key, value, ex=None):
        if ex:
            self._cmd("SET", key, value, "EX", ex)
        else:
            self._cmd("SET", key, value)


def make_kv():
    url = env("UPSTASH_REDIS_REST_URL")
    token = env("UPSTASH_REDIS_REST_TOKEN")
    if url and token:
        if not url.startswith("https://"):
            log.error("UPSTASH_REDIS_REST_URL 'https://' ilə başlamalıdır (redis:// yox!)")
        log.info("Yaddaş: Upstash Redis")
        return RedisREST(url, token)
    log.warning("Yaddaş: MÜVƏQQƏTİ (UPSTASH_* təyin edilməyib) — restartda statistika silinir")
    return MemKV()


kv = make_kv()


def clean_source(s):
    """/start instagram → 'instagram'. Yalnız hərf, rəqəm, _ və -."""
    s = re.sub(r"[^a-z0-9_-]", "", (s or "").lower())[:20]
    return s or "direct"


class Stats:
    """İstifadəçi və kupon statistikası. Xətalar çağıran tərəfdə (safe_call) tutulur."""

    def __init__(self, store):
        self.kv = store
        self._known = set()
        self._lang = {}

    @staticmethod
    def _day():
        return datetime.now(TZ).date().isoformat()

    def touch_user(self, uid, name, source=None):
        """İstifadəçini qeyd edir. Yeni idisə True qaytarır (mənbə də saxlanır)."""
        if (uid, name) in self._known:
            return False
        is_new = self.kv.sadd("users:all", uid) == 1
        self.kv.hset(f"u:{uid}", "name", name)
        if is_new:
            day, src = self._day(), clean_source(source)
            self.kv.hset(f"u:{uid}", "first", day)
            self.kv.hset(f"u:{uid}", "src", src)
            self.kv.hincrby(f"d:{day}:tot", "new", 1)
            self.kv.hincrby(f"d:{day}:src", src, 1)
        self._known.add((uid, name))  # yalnız uğurlu yazışdan sonra
        return is_new

    def log_coupon(self, uid, tier):
        day = self._day()
        self.kv.hincrby(f"d:{day}", uid, 1)
        self.kv.hincrby(f"d:{day}:tot", "all", 1)
        self.kv.hincrby(f"d:{day}:tot", tier, 1)

    def record(self, uid, name, tier):
        self.touch_user(uid, name)
        self.log_coupon(uid, tier)

    def get_lang(self, uid):
        if uid in self._lang:
            return self._lang[uid]
        lang = self.kv.hget(f"u:{uid}", "lang")
        if lang in TXT:
            self._lang[uid] = lang
            return lang
        return None

    def set_lang(self, uid, lang):
        self._lang[uid] = lang
        self.kv.hset(f"u:{uid}", "lang", lang)

    def report(self, ndays=7):
        today = datetime.now(TZ).date()
        rows = []
        for i in range(ndays):
            d = (today - timedelta(days=i)).isoformat()
            tot = self.kv.hgetall(f"d:{d}:tot")
            rows.append(dict(
                day=d, users=self.kv.hlen(f"d:{d}"),
                coupons=int(tot.get("all", 0)), safe=int(tot.get("safe", 0)),
                normal=int(tot.get("normal", 0)), risky=int(tot.get("risky", 0)),
                new=int(tot.get("new", 0)),
            ))
        d0 = today.isoformat()
        src = {k: int(v) for k, v in self.kv.hgetall(f"d:{d0}:src").items()}
        counts = {k: int(v) for k, v in self.kv.hgetall(f"d:{d0}").items()}
        top = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
        top = [(self.kv.hget(f"u:{uid}", "name") or uid, c) for uid, c in top]
        return dict(rows=rows, src=src, top=top,
                    total_users=self.kv.scard("users:all"),
                    persistent=self.kv.persistent)


stats = Stats(kv)


# ============================== OYUN MƏLUMATI ==============================
@dataclass
class Leg:
    kind: str          # home/away/draw/dc1x/dcx2/dc12/over/under
    odds: float
    prob: float        # marjası çıxarılmış bazar ehtimalı
    approx: bool = False


@dataclass
class Match:
    id: str
    home: str
    away: str
    start: datetime
    league_key: str
    league: str
    top: bool
    legs: list


@dataclass
class Snapshot:
    day: str
    matches: list
    spent: int
    remaining: object
    leagues: int
    ok: bool

    def to_json(self):
        return json.dumps(dict(
            day=self.day, spent=self.spent, remaining=self.remaining,
            leagues=self.leagues, ok=self.ok,
            matches=[dict(id=m.id, home=m.home, away=m.away, start=m.start.isoformat(),
                          league_key=m.league_key, league=m.league, top=m.top,
                          legs=[[l.kind, l.odds, l.prob, l.approx] for l in m.legs])
                     for m in self.matches],
        ))

    @staticmethod
    def from_json(raw):
        d = json.loads(raw)
        matches = [Match(id=m["id"], home=m["home"], away=m["away"],
                         start=datetime.fromisoformat(m["start"]).astimezone(TZ),
                         league_key=m["league_key"], league=m["league"], top=m["top"],
                         legs=[Leg(*l) for l in m["legs"]])
                   for m in d["matches"]]
        return Snapshot(d["day"], matches, d["spent"], d["remaining"], d["leagues"], d["ok"])


def _avg(values):
    return sum(values) / len(values)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_legs(o_home, o_draw, o_away, over=None, under=None):
    """h2h kefindən: 1/X/2, təxmini ikiqat şans (1X, X2, 12) və 2.5 Üst/Alt variantları."""
    inv = {"home": 1 / o_home, "draw": 1 / o_draw, "away": 1 / o_away}
    s = sum(inv.values())
    p = {k: v / s for k, v in inv.items()}
    odds = {"home": o_home, "draw": o_draw, "away": o_away}
    legs = [Leg(k, odds[k], p[k]) for k in ("home", "draw", "away")]
    # İkiqat şans: real kef mərc şirkətində fərqli ola bilər, ona görə approx=True
    for kind, (a, b) in {"dc1x": ("home", "draw"), "dcx2": ("away", "draw"),
                         "dc12": ("home", "away")}.items():
        legs.append(Leg(kind, 1 / (inv[a] + inv[b]), p[a] + p[b], approx=True))
    if over and under:
        io, iu = 1 / over, 1 / under
        legs.append(Leg("over", over, io / (io + iu)))
        legs.append(Leg("under", under, iu / (io + iu)))
    return legs


def parse_league_odds(data, key, title, top, now):
    """Bir liqanın Odds API cavabından hələ başlamamış oyunları çıxarır."""
    out = []
    for ev in data:
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).astimezone(TZ)
        if start <= now:
            continue
        home, away = ev["home_team"], ev["away_team"]
        h2h, totals = defaultdict(list), defaultdict(list)
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk["key"] == "h2h":
                    for o in mk["outcomes"]:
                        h2h[o["name"]].append(o["price"])
                elif mk["key"] == "totals":
                    for o in mk["outcomes"]:
                        if o.get("point") == 2.5:
                            totals[o["name"]].append(o["price"])
        if not (home in h2h and away in h2h and "Draw" in h2h):
            continue
        o_h, o_d, o_a = _avg(h2h[home]), _avg(h2h["Draw"]), _avg(h2h[away])
        if min(o_h, o_d, o_a) < 1.01:
            continue
        over = _avg(totals["Over"]) if totals.get("Over") else None
        under = _avg(totals["Under"]) if totals.get("Under") else None
        out.append(Match(ev["id"], home, away, start, key, title, top,
                         make_legs(o_h, o_d, o_a, over, under)))
    return out


def _get(path, **params):
    return requests.get(f"{ODDS_BASE}{path}", params={"apiKey": ODDS_API_KEY, **params}, timeout=20)


def discover_leagues():
    """Aktiv futbol liqaları. /sports PULSUZDUR (kredit çəkmir)."""
    r = _get("/sports")
    r.raise_for_status()
    return [(s["key"], s.get("title", s["key"])) for s in r.json()
            if s.get("key", "").startswith("soccer_")
            and not s.get("has_outrights") and s.get("active", True)]


def league_events(key, t_from, t_to):
    """Liqanın pəncərədəki oyunları. /events PULSUZDUR. Xəta olsa None."""
    try:
        r = _get(f"/sports/{key}/events", commenceTimeFrom=_iso(t_from), commenceTimeTo=_iso(t_to))
        return r.json() if r.status_code == 200 else None
    except Exception:
        log.warning("%s | events xətası", key)
        return None


def fetch_day():
    """
    Günün oyun bazasını qurur. Addımlar:
      1) aktiv liqaları tap (pulsuz)   2) hansında oyun var — yoxla (pulsuz)
      3) yalnız oyunu olan liqalar üçün kef çək (kredit), gündəlik büdcə daxilində.
    """
    now = datetime.now(TZ)
    t_to = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=30)
    leagues = discover_leagues()
    with ThreadPoolExecutor(max_workers=8) as ex:
        found = list(ex.map(lambda kt: (kt, league_events(kt[0], now, t_to)), leagues))
    active = [(k, t, evs) for (k, t), evs in found if evs]
    # Əvvəl məşhur liqalar, sonra oyun sayı çox olan əlavə liqalar
    active.sort(key=lambda x: (0, TOP_LEAGUES.index(x[0])) if x[0] in TOP_LEAGUES else (1, -len(x[2])))

    matches, spent, remaining, quota_out = [], 0, None, False
    for key, title, _evs in active:
        top = key in TOP_LEAGUES
        cost = 2 if top else 1
        if spent + cost > DAILY_CREDIT_BUDGET:
            continue
        if remaining is not None and remaining - cost < CREDIT_RESERVE:
            break
        try:
            r = _get(f"/sports/{key}/odds", regions="eu", oddsFormat="decimal",
                     markets="h2h,totals" if top else "h2h")
        except Exception:
            log.warning("%s | odds sorğusu uğursuz", key)
            continue
        rem = r.headers.get("x-requests-remaining")
        if rem is not None:
            try:
                remaining = int(float(rem))
            except ValueError:
                pass
        log.info("%s | status=%s | qalan kredit=%s", key, r.status_code, rem)
        if r.status_code == 401:      # kvota bitib
            quota_out = True
            break
        if r.status_code != 200:
            continue
        spent += cost
        try:
            matches += parse_league_odds(r.json(), key, title, top, now)
        except Exception:
            log.exception("%s | parse xətası", key)
    ok = bool(matches) or not quota_out
    log.info("Baza: %d liqa aktiv, %d oyun, %d kredit xərcləndi, qalan=%s",
             len(active), len(matches), spent, remaining)
    return Snapshot(now.date().isoformat(), matches, spent, remaining, len(active), ok)


_snap = {"day": None, "t": 0.0, "data": None, "ok": False}
_snap_lock = threading.Lock()


def get_snapshot():
    """Günlük keş. Redis varsa restartdan sonra kredit xərcləmədən oradan oxuyur."""
    today = datetime.now(TZ).date().isoformat()
    with _snap_lock:
        s = _snap
        if s["day"] == today and s["data"] is not None and (
                s["ok"] or time.time() - s["t"] < RETRY_AFTER_FAIL):
            return s["data"], s["ok"]
        if s["day"] != today:
            s["data"] = None
            try:
                raw = kv.get(f"snap:{today}")
                if raw:
                    s.update(day=today, t=time.time(), data=Snapshot.from_json(raw), ok=True)
                    log.info("Baza Redis-dən yükləndi (kredit xərclənmədi)")
                    return s["data"], True
            except Exception:
                log.exception("Redis snapshot oxunmadı")
        try:
            data = fetch_day()
        except Exception:
            log.exception("fetch_day xətası")
            data = None
        s["day"], s["t"] = today, time.time()
        if data is not None:
            s["data"], s["ok"] = data, data.ok
            if data.ok:
                try:
                    kv.set(f"snap:{today}", data.to_json(), ex=3 * 86400)
                except Exception:
                    log.exception("Redis snapshot yazılmadı")
        else:
            s["ok"] = False  # köhnə (eyni günün) məlumatı varsa saxlanır
        return s["data"], s["ok"]


# ============================== KUPON ALQORİTMİ ==============================
@dataclass
class Coupon:
    tier: str
    picks: list        # [(Match, Leg), ...] vaxta görə sıralı
    total: float
    prob: float
    on_target: bool


def _search(rng, cands, ids, n, cfg, cap, t0, level, tries=400):
    """
    n oyunluq kombinasiya axtarır. Ümumi kef [lo, hi] daxilində olmalıdır və hədəf t0-a
    yaxın olan, ehtimalı yüksək, mümkünsə real (təxmini olmayan) kefli variant seçilir.
    level: 0 — bütün limitlər; 1 — liqa limiti yox, tip limiti var; 2 — yalnız kef aralığı.
    """
    lo, hi = cfg["lo"], cfg["hi"]
    target = t0 ** (1.0 / n)                      # hər oyun üçün hədəf kef
    weight = {i: (3.0 if cands[i][0].top else 1.0) for i in ids}
    best = closest = None
    for _ in range(tries):
        # çəkili təsadüfi seçim (məşhur liqalara üstünlük)
        chosen = sorted(ids, key=lambda i: rng.random() ** (1.0 / weight[i]), reverse=True)[:n]
        if sum(1 for i in chosen if not cands[i][0].top) > cap:
            continue
        if level == 0:
            per_league = Counter(cands[i][0].league_key for i in chosen)
            if max(per_league.values()) > cfg["league_cap"]:
                continue
        picks, kinds, bad = [], Counter(), False
        for i in chosen:
            m, opts = cands[i]
            if level <= 1:   # eyni tip limiti dolubsa, o tipi bu oyun üçün kənarlaşdır
                opts = [l for l in opts if kinds[l.kind] < MAX_PER_KIND.get(l.kind, 99)]
                if not opts:  # bu oyun üçün uyğun variant qalmadı → kombinasiya keçərsiz
                    bad = True
                    break
            leg = min(opts, key=lambda l: abs(math.log(l.odds) - math.log(target))
                      + (0.10 if l.approx else 0.0) + rng.uniform(0, 0.06))
            kinds[leg.kind] += 1
            picks.append((m, leg))
        if bad:
            continue
        total = math.prod(l.odds for _, l in picks)
        prob = math.prod(l.prob for _, l in picks)
        if lo <= total <= hi:
            n_approx = sum(1 for _, l in picks if l.approx)
            score = math.log(prob) - 4.0 * abs(math.log(total) - math.log(t0)) - 0.08 * n_approx
            if best is None or score > best[3]:
                best = (picks, total, prob, score)
        else:
            dist = abs(math.log(total) - math.log(min(max(total, lo), hi)))
            if closest is None or dist < closest[3]:
                closest = (picks, total, prob, dist)
    return best, closest


def build_coupon(matches, tier, variant=0, now=None):
    """
    Azərbaycan məntiqi: oyun sayı sabit deyil.
      ehtiyatlı — 3-4 oyun, normal — 5-6, riskli — 7-9 (oyun azdırsa azalır).
    Eyni gün + eyni növ + eyni variant həmişə eyni kupon verir; 'Başqa variant' yenisini.
    """
    cfg = TIERS[tier]
    now = now or datetime.now(TZ)
    rng = random.Random(f"{now.date().isoformat()}|{tier}|{variant}")
    pool = [m for m in matches if m.start > now + timedelta(minutes=MIN_MINUTES_BEFORE_KICKOFF)]
    cands = {}
    for m in pool:
        opts = [l for l in m.legs if cfg["pick_lo"] <= l.odds <= cfg["pick_hi"]]
        if opts:
            cands[m.id] = (m, opts)
    ids = list(cands)
    if len(ids) < 2:
        return None
    n_top = sum(1 for i in ids if cands[i][0].top)
    choices = {n: w for n, w in cfg["legs"].items() if n <= len(ids)} or {len(ids): 1}
    n = rng.choices(list(choices), weights=list(choices.values()))[0]

    # bu kupon üçün hədəf ümumi kef və neçə əlavə-liqa oyununa icazə (təsadüfi, bəzən 0)
    t0 = math.exp(rng.uniform(math.log(cfg["lo"] * 1.05), math.log(cfg["hi"] * 0.95)))
    extra_pref = rng.choices(list(cfg["extra_w"]), weights=list(cfg["extra_w"].values()))[0]

    result = first_closest = None
    for nn in [n] + list(range(n - 1, max(cfg["min_legs"], 2) - 1, -1)):
        cap = max(extra_pref, nn - n_top)   # məşhur liqa oyunu azdırsa əlavələr icazəli olur
        best = closest = None
        for level in (0, 1, 2):             # limitləri addım-addım yumşalt
            b, c2 = _search(rng, cands, ids, nn, cfg, cap, t0, level)
            if b:
                best = b
                break
            if c2 and (closest is None or c2[3] < closest[3]):
                closest = c2
        if best:
            result = best
            break
        if first_closest is None:
            first_closest = closest
    on_target = result is not None
    if result is None:
        if first_closest is None:
            return None
        result = first_closest[:3]
    picks, total, prob = result[:3]
    picks = sorted(picks, key=lambda x: x[0].start)
    return Coupon(tier, picks, total, prob, on_target)


def pick_text(L, m, leg):
    k = leg.kind
    if k == "home":
        return L["p_win"].format(team=m.home)
    if k == "away":
        return L["p_win"].format(team=m.away)
    if k == "dc1x":
        return L["p_1x"].format(team=m.home)
    if k == "dcx2":
        return L["p_x2"].format(team=m.away)
    return L["p_" + k]  # draw, dc12, over, under


def format_coupon(lang, c, now=None):
    L, cfg = T(lang), TIERS[c.tier]
    today = (now or datetime.now(TZ)).date()
    lines = [L["head"].format(icon=cfg["icon"], name=L["name_" + c.tier],
                              n=len(c.picks), total=f"{c.total:.2f}"), L["tz_note"], ""]
    for i, (m, leg) in enumerate(c.picks, 1):
        when = m.start.strftime("%H:%M") if m.start.date() == today else m.start.strftime("%d.%m %H:%M")
        approx = "≈" if leg.approx else ""
        lines.append(f"{i}. {m.home} - {m.away}\n   🕒 {when} · {m.league}\n"
                     f"   ➜ {pick_text(L, m, leg)} | {L['odds_word']} {approx}{leg.odds:.2f}")
    lines.append("")
    lines.append(L["prob"].format(p=f"{c.prob * 100:.1f}" if c.prob < 0.1 else f"{c.prob * 100:.0f}"))
    if not c.on_target:
        lines.append(L["short_note"])
    if any(l.approx for _, l in c.picks):
        lines.append(L["approx_note"])
    lines.append("")
    lines.append(L["disclaimer"])
    return "\n".join(lines)


def format_admin(lang, rep, snap):
    L = T(lang)
    rows = rep["rows"]
    t = rows[0]
    day = datetime.fromisoformat(t["day"]).strftime("%d.%m.%Y")
    out = [L["a_title"].format(day=day),
           L["a_today"].format(u=t["users"], c=t["coupons"]),
           L["a_tiers"].format(s=t["safe"], n=t["normal"], r=t["risky"])]
    new_line = L["a_new"].format(n=t["new"])
    if rep["src"]:
        parts = " · ".join(f"{k} {v}" for k, v in sorted(rep["src"].items(), key=lambda x: -x[1]))
        new_line += f" → {parts}"
    out += [new_line, L["a_total"].format(n=rep["total_users"]), "",
            L["a_days"].format(d=len(rows))]
    for r in rows:
        d = datetime.fromisoformat(r["day"]).strftime("%d.%m")
        out.append(L["a_row"].format(day=d, u=r["users"], c=r["coupons"], n=r["new"]))
    out += ["", L["a_top"]]
    if rep["top"]:
        out += [f"{i}. {name} — {c}" for i, (name, c) in enumerate(rep["top"], 1)]
    else:
        out.append(L["a_none"])
    out.append("")
    if snap is not None:
        rem = snap.remaining if snap.remaining is not None else "?"
        out.append(L["a_snap"].format(m=len(snap.matches), l=snap.leagues, s=snap.spent, r=rem))
    else:
        out.append(L["a_snap_none"])
    out.append(L["a_mem_ok"] if rep["persistent"] else L["a_mem_tmp"])
    return "\n".join(out)


# ============================== TELEGRAM ==============================
_last_click = {}
_last_admin_alert = {"t": 0.0}


def display_name(user):
    return f"@{user.username}" if user.username else (user.first_name or str(user.id))[:30]


async def safe_call(func, *args):
    """Statistika/yaddaş xətası heç vaxt istifadəçiyə təsir etməsin."""
    try:
        return await asyncio.to_thread(func, *args)
    except Exception:
        log.exception("%s xətası", getattr(func, "__name__", "call"))
        return None


async def user_lang(user):
    return await safe_call(stats.get_lang, user.id) or (DEFAULT_LANG if DEFAULT_LANG in TXT else "az")


async def alert_admin(context, text):
    """Adminlərə saatda ən çox 1 xəbərdarlıq."""
    if not ADMIN_IDS or time.time() - _last_admin_alert["t"] < 3600:
        return
    _last_admin_alert["t"] = time.time()
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(aid, text)
        except Exception:
            log.exception("Admin-ə mesaj göndərilə bilmədi")


def menu_keyboard(lang):
    L = T(lang)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(L["btn_safe"], callback_data="c:safe:0")],
        [InlineKeyboardButton(L["btn_normal"], callback_data="c:normal:0")],
        [InlineKeyboardButton(L["btn_risky"], callback_data="c:risky:0")],
    ])


def lang_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"l:{code}")
                                  for code, label in LANG_BUTTONS]])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # /start instagram → reklam mənbəyi (t.me/BOT?start=instagram)
    source = context.args[0] if context.args else None
    await safe_call(stats.touch_user, user.id, display_name(user), source)
    lang = await user_lang(user)
    L = T(lang)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(L["btn_coupon"], callback_data="m")],
        [InlineKeyboardButton(label, callback_data=f"l:{code}") for code, label in LANG_BUTTONS],
    ])
    await update.message.reply_text(L["welcome"], reply_markup=kb)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = await user_lang(update.effective_user)
    await update.message.reply_text(T(lang)["menu_title"], reply_markup=menu_keyboard(lang))


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(T("az")["lang_prompt"], reply_markup=lang_keyboard())


async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    L = T(await user_lang(update.effective_user))
    text = L["privacy"] + (L["privacy_contact"].format(contact=CONTACT) if CONTACT else "")
    await update.message.reply_text(text)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    L = T(await user_lang(update.effective_user))
    await update.message.reply_text(L["your_id"].format(id=update.effective_user.id))


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return  # adminlərdən başqasına cavab verilmir
    ndays = 7
    if context.args and context.args[0].isdigit():
        ndays = max(1, min(int(context.args[0]), 30))
    lang = await user_lang(user)
    rep = await safe_call(stats.report, ndays)
    if rep is None:
        await update.message.reply_text("⚠️ Statistika oxunmadı. Render Logs-a bax.")
        return
    await update.message.reply_text(format_admin(lang, rep, _snap["data"]))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user, data = q.from_user, q.data or ""

    if data == "m":                                   # menyu
        lang = await user_lang(user)
        await q.message.reply_text(T(lang)["menu_title"], reply_markup=menu_keyboard(lang))
        return

    if data.startswith("l:"):                         # dil seçimi
        lang = data[2:]
        if lang in TXT:
            await safe_call(stats.touch_user, user.id, display_name(user))
            await safe_call(stats.set_lang, user.id, lang)
            await q.message.reply_text(T(lang)["lang_set"], reply_markup=menu_keyboard(lang))
        return

    if not data.startswith("c:"):
        return
    try:
        _, tier, variant = data.split(":")
        variant = int(variant)
    except ValueError:
        return
    if tier not in TIERS:
        return

    now_t = time.time()                               # spam qoruması
    if now_t - _last_click.get(user.id, 0) < COOLDOWN:
        return
    _last_click[user.id] = now_t

    lang = await user_lang(user)
    L = T(lang)
    status = await q.message.reply_text(L["checking"])
    try:
        snap, ok = await asyncio.to_thread(get_snapshot)
        if snap is None:
            await alert_admin(context, "⚠️ Kupon botu: oyun məlumatı alınmır. Odds API limitini/Logs-u yoxla.")
            await status.edit_text(L["data_error"])
            return
        coupon = await asyncio.to_thread(build_coupon, snap.matches, tier, variant)
        if coupon is None:
            if not ok:
                await alert_admin(context, "⚠️ Kupon botu: Odds API kvotası bitmiş ola bilər.")
            await status.edit_text(L["data_error"] if not ok else L["no_matches"])
            return
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(L["btn_again"], callback_data=f"c:{tier}:{variant + 1}"),
            InlineKeyboardButton(L["btn_menu"], callback_data="m"),
        ]])
        await status.edit_text(format_coupon(lang, coupon), reply_markup=kb)
    except Exception:
        log.exception("Kupon xətası")
        try:
            await status.edit_text(L["gen_error"])
        except Exception:
            pass
        return
    await safe_call(stats.record, user.id, display_name(user), tier)  # statistika


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Handler xətası", exc_info=context.error)


# ============================== HEARTBEAT + WATCHDOG ==============================
BEAT_EVERY = 30
DEAD_AFTER = 180
_beat = {"t": time.time()}


async def heartbeat_loop(app: Application):
    """Event loop canlıdırsa və polling işləyirsə, hər 30 san. nəbz yazır."""
    while True:
        up = app.updater
        if up is None or getattr(up, "running", True):
            _beat["t"] = time.time()
        await asyncio.sleep(BEAT_EVERY)


def warm_up():
    try:
        get_snapshot()
    except Exception:
        log.exception("Warm-up xətası")


async def post_init(app: Application):
    app.bot_data["hb"] = asyncio.create_task(heartbeat_loop(app))
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS["az"]])
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS["en"]], language_code="en")
    except Exception:
        log.exception("Komanda menyusu qurulmadı")
    asyncio.create_task(asyncio.to_thread(warm_up))  # ilk istifadəçi gözləməsin


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
    port = int(env("PORT", "10000") or 10000)
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


def main():
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    app = (Application.builder().token(BOT_TOKEN)
           .post_init(post_init)
           .concurrent_updates(True)   # bir istifadəçinin gözləməsi digərini dondurmasın
           .build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["gununoyunlari", "coupon"], cmd_menu))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    # drop_pending_updates: restartda köhnə yığılmış mesajlara cavab yağdırmasın
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
