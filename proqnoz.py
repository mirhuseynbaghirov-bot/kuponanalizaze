"""
proqnoz.py — /proqnoz (günün oyunları üzrə analiz) modulu.

Heç bir yeni API sorğusu atmır (limitə toxunmur):
  * Botun analizi  -> football-data.org-dan artıq Redis-də keşlənmiş son oyunlar (fdorg:matches:...)
                      + mövcud Poisson modeli (m.info) + hər komandanın SON 5 oyunu
  * Bazarın seçimi -> Odds API-dan artıq bazada olan keflər (m.p3, m.legs)
Əmin olmayanda "Qərarsız oyun" deyir.
"""
import asyncio
import logging
import math
import time
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

log = logging.getLogger("proqnoz")

PAGE_SIZE = 4            # bir səhifədə 4 oyun
MAX_MATCHES = 40         # cəmi 10 səhifə
MIN_MINUTES = 15         # başlamağa 15 dəq. qalmış oyunlar göstərilmir
CACHE_TTL = 5 * 60
COOLDOWN = 1.5
LINE = "━━━━━━━━━━━━━━"

_c = {}
_cache = {"t": 0.0, "pages": None}
_last_click = {}
_lock = __import__("threading").Lock()


def init(**kw):
    """main.py-dən çağırılır: lazımi funksiya və dəyişənləri ötürür."""
    _c.update(kw)


# ---------------------------------------------------------------- yardımçılar
def _pc(x):
    return round(x * 100)


def _sn(name):
    return name if len(name) <= 16 else name[:15] + "…"


def _league_matches(code, cache):
    if not code:
        return []
    if code not in cache:
        day = datetime.now(_c["TZ"]).date().isoformat()
        cache[code] = _c["kvj_get"](f"fdorg:matches:{code}:{day}") or []
    return cache[code]


def _team5(code, team, cache):
    """Komandanın SON 5 oyunu (ev+qonaq), (vurduğu, buraxdığı), yeni → köhnə. Az oyun varsa None."""
    key = ("t5", code, team)
    if key in cache:
        return cache[key]
    rows = []
    ns = _c["name_score"]
    for mt in sorted(_league_matches(code, cache), key=lambda x: x.get("utcDate", ""), reverse=True):
        sc = (mt.get("score") or {}).get("fullTime") or {}
        hg, ag = sc.get("home"), sc.get("away")
        if hg is None or ag is None:
            continue
        hn = (mt.get("homeTeam") or {}).get("name") or ""
        an = (mt.get("awayTeam") or {}).get("name") or ""
        if ns(team, hn) >= 0.85:
            rows.append((hg, ag))
        elif ns(team, an) >= 0.85:
            rows.append((ag, hg))
        if len(rows) >= 5:
            break
    res = rows if len(rows) >= _c["min_games"] else None
    cache[key] = res
    return res


def _ppg(rows):
    return sum(3 if g > c else (1 if g == c else 0) for g, c in rows) / len(rows)


def _form(rows):
    return "".join("W" if g > c else ("D" if g == c else "L") for g, c in reversed(rows))


def _p_over(lam, line):
    k = int(line)
    return 1 - sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(k + 1))


def _leg(m, kind, line=None):
    c = [l for l in m.legs if l.kind == kind and (line is None or l.line == line)]
    return max(c, key=lambda l: l.prob) if c else None


# ---------------------------------------------------------------- botun analizi
def _bot_verdict(m, h5, a5):
    """Qalib yalnız model + son 5 oyun forması + bazar bir-birinə ZİD DÜŞMƏSƏ verilir. Əks halda None (qərarsız)."""
    if not (_c["verified"](m) and m.info.get("pct") and h5 and a5):
        return None
    ph, pd, pa = m.info["pct"]
    fd = _ppg(h5) - _ppg(a5)
    side = None
    if ph - pa >= 0.12 and ph >= 0.42:
        side = "home"
    elif pa - ph >= 0.12 and pa >= 0.42:
        side = "away"
    if (side == "home" and fd <= -0.3) or (side == "away" and fd >= 0.3):
        side = None                                   # forma modelə ziddir
    if side and m.p3:
        mh, _d, ma = m.p3
        if (side == "home" and ma - mh > 0.05) or (side == "away" and mh - ma > 0.05):
            side = None                               # bazar əks tərəfi üstün tutur
    return {"side": side, "ph": ph, "pd": pd, "pa": pa}


def _goal_parts(h5, a5, m):
    """2.5 və 1.5 Üst/Alt — yalnız kifayət qədər əminlik olanda."""
    if not (h5 and a5):
        return []
    n_h, n_a = len(h5), len(a5)
    eh = (sum(g for g, _ in h5) / n_h + sum(c for _, c in a5) / n_a) / 2
    ea = (sum(g for g, _ in a5) / n_a + sum(c for _, c in h5) / n_h) / 2
    lam = eh + ea
    out = []
    p25 = _p_over(lam, 2.5)
    mk_o, mk_u = _leg(m, "over", 2.5), _leg(m, "under", 2.5)
    if p25 >= 0.60 and not (mk_u and mk_u.prob >= 0.55):
        out.append(f"2.5 Üst (~{_pc(p25)}%)")
    elif p25 <= 0.40 and not (mk_o and mk_o.prob >= 0.55):
        out.append(f"2.5 Alt (~{_pc(1 - p25)}%)")
    p15 = _p_over(lam, 1.5)
    if p15 >= 0.75:
        out.append(f"1.5 Üst (~{_pc(p15)}%)")
    elif p15 <= 0.40:
        out.append(f"1.5 Alt (~{_pc(1 - p15)}%)")
    return out, lam


def _extra_pick(m, group, bot_only):
    """Korner/kart: yalnız bazada ARTIQ olan xətlər (əlavə sorğu yoxdur)."""
    kinds = {f"{group}_over", f"{group}_under"}
    key = "cor" if group == "corners" else "crd"
    has_stats = bool(m.info and m.info.get(key))
    if bot_only and not has_stats:
        return None
    cands = []
    for l in m.legs:
        if l.kind in kinds and l.prob >= 0.60 and l.line is not None:
            if has_stats and not _c["stat_gate"](m, l, "relaxed"):
                continue
            cands.append(l)
    if not cands:
        return None
    l = max(cands, key=lambda x: x.prob)
    side = "Üst" if l.kind.endswith("over") else "Alt"
    name = "Korner" if group == "corners" else "Kart"
    return f"{name} {l.line:g} {side} (~{_pc(l.prob)}%)"


# ---------------------------------------------------------------- bazarın seçimi
def _market_side(m):
    if not m.p3:
        return None, None
    h, d, a = m.p3
    if h - a >= 0.10 and h >= 0.45:
        return "home", h
    if a - h >= 0.10 and a >= 0.45:
        return "away", a
    return None, None


def _fmt_match(i, m, cache):
    code = _c["FD_ORG_COMPETITIONS"].get(m.league_key)
    h5 = _team5(code, m.home, cache) if code else None
    a5 = _team5(code, m.away, cache) if code else None
    H, A = _sn(m.home), _sn(m.away)

    out = [f"{i}. 🕒 {m.start:%H:%M} · {m.home} - {m.away}", f"🏟 {m.league}", LINE,
           "🤖 Botun analizi — Yekun rəy"]

    # --- bot bloku
    v = _bot_verdict(m, h5, a5)
    bot_side = None
    if v is None:
        out.append("   ℹ️ Bu oyun üçün statistika yoxdur (liqa əhatə olunmur və ya az oyun var)")
    else:
        bot_side = v["side"]
        if bot_side == "home":
            out.append(f"   ✅ {m.home} qalib gələcək kimi görünür (~{_pc(v['ph'])}%)")
        elif bot_side == "away":
            out.append(f"   ✅ {m.away} qalib gələcək kimi görünür (~{_pc(v['pa'])}%)")
        else:
            out.append("   🤝 Qərarsız oyun")
        out.append(f"   🎯 Model 1/X/2: {_pc(v['ph'])}/{_pc(v['pd'])}/{_pc(v['pa'])}%")
    if h5 and a5:
        out.append(f"   📈 Son 5: {H} {_form(h5)} · {A} {_form(a5)}")
        parts, lam = _goal_parts(h5, a5, m)
        if parts:
            out.append(f"   ⚽ Qol: {' · '.join(parts)} (gözlənən ~{lam:.1f})")
        elif v is not None:
            out.append(f"   ⚽ Qol: qərarsız (gözlənən ~{lam:.1f})")
    for grp in ("corners", "cards"):
        t = _extra_pick(m, grp, bot_only=True)
        if t:
            out.append(f"   {'🚩' if grp == 'corners' else '🟨'} {t}")

    # --- bazar bloku
    out += [LINE, "👥 İstifadəçilərin seçdikləri — Yekun rəy"]
    mk_side, mk_p = _market_side(m)
    if m.p3:
        h, d, a = m.p3
        if mk_side == "home":
            out.append(f"   ✅ {m.home} (~{_pc(mk_p)}%)")
        elif mk_side == "away":
            out.append(f"   ✅ {m.away} (~{_pc(mk_p)}%)")
        else:
            out.append("   🤝 Qərarsız oyun")
        out.append(f"   📊 1/X/2: {_pc(h)}/{_pc(d)}/{_pc(a)}%")
    else:
        out.append("   ℹ️ Bazar məlumatı yoxdur")
    gl = []
    for kind, word in (("over", "Üst"), ("under", "Alt")):
        l = _leg(m, kind, 2.5)
        if l and l.prob >= 0.55:
            gl.append(f"2.5 {word} (~{_pc(l.prob)}%)")
    if gl:
        out.append(f"   ⚽ Qol: {' · '.join(gl)}")
    for grp in ("corners", "cards"):
        t = _extra_pick(m, grp, bot_only=False)
        if t:
            out.append(f"   {'🚩' if grp == 'corners' else '🟨'} {t}")

    # --- uyğunluq
    if v is not None:
        out.append(LINE)
        if bot_side and bot_side == mk_side:
            out.append("🟢 Bot və bazar eyni fikirdədir")
        elif bot_side is None and mk_side is None:
            out.append("🟡 İkisi də qərarsızdır — keç")
        else:
            out.append("🟡 Fikir ayrılığı var — risk yüksəkdir")
    return "\n".join(out)


# ---------------------------------------------------------------- səhifələr
def build_pages():
    snap, ok, _a = _c["peek_snapshot"]()
    if snap is None:
        return None
    now = datetime.now(_c["TZ"])
    ms = [m for m in snap.matches
          if m.start.date() == now.date() and m.start > now + timedelta(minutes=MIN_MINUTES)]
    ms.sort(key=lambda m: (not _c["verified"](m), m.start))
    ms = ms[:MAX_MATCHES]
    if not ms:
        return []
    cache = {}
    blocks = []
    for i, m in enumerate(ms, 1):
        try:
            blocks.append(_fmt_match(i, m, cache))
        except Exception:
            log.exception("proqnoz | %s - %s", m.home, m.away)
    chunks = [blocks[i:i + PAGE_SIZE] for i in range(0, len(blocks), PAGE_SIZE)]
    total = len(chunks)
    pages = []
    for p, ch in enumerate(chunks, 1):
        head = f"📊 Günün oyunları — Analiz ({p}/{total})\n🕒 Bakı vaxtı ilədir\n\n"
        foot = ("\n\n⚠️ Bu proqnozdur, zəmanət deyil. Qərarsız oyunu oynama. 18+\n"
                "👥 blok = Odds API bazar kefləri əsasında")
        text = head + "\n\n".join(ch) + foot
        pages.append(text[:4090])
    return pages


def get_pages():
    with _lock:
        if _cache["pages"] is not None and time.time() - _cache["t"] < CACHE_TTL:
            return _cache["pages"]
        pages = build_pages()
        if pages is not None:
            _cache.update(t=time.time(), pages=pages)
        return pages


def _kb(page, total):
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("⬅️", callback_data=f"pq:{page - 1}"))
    row.append(InlineKeyboardButton(f"{page + 1}/{total}", callback_data="pq:noop"))
    if page < total - 1:
        row.append(InlineKeyboardButton("➡️", callback_data=f"pq:{page + 1}"))
    return InlineKeyboardMarkup([row])


async def _send_first(message, user_id):
    now = time.time()
    if now - _last_click.get(user_id, 0) < COOLDOWN:
        return
    _last_click[user_id] = now
    pages = await asyncio.to_thread(get_pages)
    if pages is None:
        await message.reply_text("⏳ Baza hələ hazırlanır, 1-2 dəqiqə sonra yenidən yaz.")
    elif not pages:
        await message.reply_text("😕 Bu gün analiz ediləcək oyun qalmayıb.")
    else:
        await message.reply_text(pages[0], reply_markup=_kb(0, len(pages)))


async def cmd_proqnoz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_first(update.message, update.effective_user.id)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    arg = (q.data or "")[3:]
    if arg == "noop":
        return
    if arg == "new":                                  # menyudakı düymə
        await _send_first(q.message, q.from_user.id)
        return
    try:
        page = int(arg)
    except ValueError:
        return
    pages = await asyncio.to_thread(get_pages)
    if not pages:
        return
    page = max(0, min(page, len(pages) - 1))
    try:
        await q.edit_message_text(pages[page], reply_markup=_kb(page, len(pages)))
    except Exception:
        log.debug("proqnoz | səhifə dəyişmədi")
