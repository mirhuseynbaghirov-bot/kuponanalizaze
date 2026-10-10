"""
proqnoz.py — /proqnoz (günün oyunları üzrə analiz) modulu.

Bazara/botun əsas axınına toxunmur:
  * Botun analizi  -> football-data.org (liqa üzrə gündə 1 sorğu, Redis-də keşlənir)
                      + Poisson modeli + hər komandanın SON 5 oyunu
  * Bazarın seçimi -> Odds API-dan artıq bazada olan keflər (m.p3, m.legs)
v2 dəyişikliklər:
  * Keş boşdursa liqanın oyunlarını özü çəkir (fd_fetch) — baza statistikası alınmasa da işləyir
  * m.info yoxdursa statistikanı özü hesablayır
  * "Qərarsız oyun" olanda ən yüksək ehtimalı və səbəbi də göstərir
"""
import asyncio
import logging
import math
import sys
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
_fail = {}               # liqa kodu -> son uğursuz çəkmə vaxtı (10 dəq. təkrar etmə)
_err = {}                # liqa kodu -> son xəta mətni (mesajda göstərilir)
_lock = __import__("threading").Lock()


def init(**kw):
    """main.py-dən çağırılır: lazımi funksiya və dəyişənləri ötürür."""
    _c.update(kw)
    try:
        _install_fd_fallback()
    except Exception:
        log.exception("proqnoz | football-data ehtiyat yaması qurulmadı (bot normal işləyir)")


def _any_rows(matches, team, limit=6):
    """Komandanın son oyunları (ev+səfər fərqi qoymadan): (vurduğu, buraxdığı), yeni → köhnə."""
    rows = []
    ns = _c["name_score"]
    for mt in sorted(matches, key=lambda x: x.get("utcDate", "") or "", reverse=True):
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
        if len(rows) >= limit:
            break
    return rows


def _install_fd_fallback():
    """
    football-data.org statistikası: komandanın ayrıca 4 EVDƏ və 4 SƏFƏRDƏ oyunu yoxdursa (mövsümün
    əvvəli) əvvəl 0/N çıxırdı. Bu yama əsas build_fd_info uğursuz olanda komandanın son oyunlarını
    (harada oynamasından asılı olmayaraq) götürür. bot.py-a toxunmaq lazım deyil.
    """
    mod = sys.modules.get("__main__")
    if mod is None or not hasattr(mod, "build_fd_info") or getattr(mod, "_fd_fallback_on", False):
        log.info("proqnoz | football-data ehtiyat yaması tətbiq olunmadı")
        return
    orig = mod.build_fd_info
    min_games = _c["min_games"]

    def patched(m, matches, avg_h, avg_a):
        res = orig(m, matches, avg_h, avg_a)
        if res:
            return res
        h_rows = _any_rows(matches, m.home)
        a_rows = _any_rows(matches, m.away)
        if len(h_rows) < min_games or len(a_rows) < min_games:
            return None
        h_sc = sum(g for g, _ in h_rows) / len(h_rows)
        h_co = sum(c for _, c in h_rows) / len(h_rows)
        a_sc = sum(g for g, _ in a_rows) / len(a_rows)
        a_co = sum(c for _, c in a_rows) / len(a_rows)
        league = (avg_h + avg_a) / 2 or 1.0
        exp_home = max(0.2, (h_sc / league) * (a_co / league) * avg_h)
        exp_away = max(0.2, (a_sc / league) * (h_co / league) * avg_a)
        ph, pd, pa = mod._fd_match_probs(exp_home, exp_away)

        def form_str(rows):
            s = "".join("W" if g > c else ("D" if g == c else "L") for g, c in rows[:5])
            return s[::-1]
        return {"ok": True, "pct": [ph, pd, pa], "form": [form_str(h_rows), form_str(a_rows)],
                "g": [h_sc, h_co, a_sc, a_co], "h2h": [0, 0, 0, 0], "cor": None, "crd": None}

    mod.build_fd_info = patched
    mod._fd_fallback_on = True
    log.info("proqnoz | football-data ehtiyat yaması aktivdir (ev/səfər oyunu azdırsa son oyunlara baxır)")


# ---------------------------------------------------------------- yardımçılar
def _pc(x):
    return round(x * 100)


def _sn(name):
    return name if len(name) <= 16 else name[:15] + "…"


def _league_matches(code, cache):
    """Liqanın bitmiş oyunları. Redis keşi boşdursa football-data-dan özü çəkir (10 dəq. fasilə ilə)."""
    if not code:
        return []
    if code not in cache:
        day = datetime.now(_c["TZ"]).date().isoformat()
        rows = _c["kvj_get"](f"fdorg:matches:{code}:{day}") or []
        fetch = _c.get("fd_fetch")
        if not rows and not fetch:
            _err[code] = "main.py-da fd_fetch ötürülməyib"
        if not rows and fetch and time.time() - _fail.get(code, 0) > 600:
            try:
                rows = fetch(code) or []
                log.info("proqnoz | %s liqası football-data-dan çəkildi: %d oyun", code, len(rows))
            except Exception as e:
                log.exception("proqnoz | %s liqası çəkilmədi", code)
                _err[code] = f"{type(e).__name__}: {str(e)[:60]}"
                rows = []
            if not rows:
                _fail[code] = time.time()
                _err.setdefault(code, "football-data boş cavab verdi")
        if rows:
            _err.pop(code, None)
        cache[code] = rows
    return cache[code]


def _team5(code, team, cache):
    """Komandanın SON 5 oyunu (ev+qonaq), (vurduğu, buraxdığı), yeni → köhnə. Az oyun varsa None."""
    key = ("t5", code, team)
    if key in cache:
        return cache[key]
    rows = _any_rows(_league_matches(code, cache), team, 5)
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


def _probs(eh, ea, mx=7):
    """Gözlənən qollardan (Poisson) 1X2 ehtimalı."""
    ph = [math.exp(-eh) * eh ** i / math.factorial(i) for i in range(mx + 1)]
    pa = [math.exp(-ea) * ea ** i / math.factorial(i) for i in range(mx + 1)]
    h = d = a = 0.0
    for i in range(mx + 1):
        for j in range(mx + 1):
            p = ph[i] * pa[j]
            if i > j:
                h += p
            elif i == j:
                d += p
            else:
                a += p
    t = h + d + a
    return (h / t, d / t, a / t) if t > 0 else (1 / 3, 1 / 3, 1 / 3)


def _own_info(m, code, cache):
    """Bazada m.info yoxdursa, liqa datasından özümüz 1X2 ehtimalı hesablayırıq (son 6 oyun, ev/səfər fərqsiz)."""
    lm = _league_matches(code, cache)
    h_rows = _any_rows(lm, m.home)
    a_rows = _any_rows(lm, m.away)
    mg = _c["min_games"]
    if len(h_rows) < mg or len(a_rows) < mg:
        return None
    hs = as_ = n = 0
    for mt in lm:
        sc = (mt.get("score") or {}).get("fullTime") or {}
        hg, ag = sc.get("home"), sc.get("away")
        if hg is None or ag is None:
            continue
        hs += hg
        as_ += ag
        n += 1
    avg_h, avg_a = (hs / n, as_ / n) if n >= 10 else (1.5, 1.2)
    league = (avg_h + avg_a) / 2 or 1.0
    h_sc = sum(g for g, _ in h_rows) / len(h_rows)
    h_co = sum(c for _, c in h_rows) / len(h_rows)
    a_sc = sum(g for g, _ in a_rows) / len(a_rows)
    a_co = sum(c for _, c in a_rows) / len(a_rows)
    exp_home = max(0.2, (h_sc / league) * (a_co / league) * avg_h)
    exp_away = max(0.2, (a_sc / league) * (h_co / league) * avg_a)
    return {"pct": list(_probs(exp_home, exp_away))}


def _why_no_stats(m, code, cache):
    """Statistikanın niyə olmadığını qısa izah edir (logsuz diaqnostika)."""
    if not code:
        return "bu liqa football-data planında yoxdur"
    lm = _league_matches(code, cache)
    if not lm:
        return f"liqa datası alınmadı ({_err.get(code, 'səbəb məlum deyil')})"
    nh = len(_any_rows(lm, m.home, 5))
    na = len(_any_rows(lm, m.away, 5))
    return f"komanda adı tapılmadı və ya az oyun var (ev {nh}, qonaq {na}, liqada {len(lm)} oyun)"


def _leg(m, kind, line=None):
    c = [l for l in m.legs if l.kind == kind and (line is None or l.line == line)]
    return max(c, key=lambda l: l.prob) if c else None


# ---------------------------------------------------------------- qərarsız mətni
_WHY = {"form": " (forma əks tərəfi göstərir)",
        "market": " (bazar əks tərəfi üstün tutur)",
        "close": ""}


def _lean_team(ph, pd, pa):
    if pd > max(ph, pa):
        return "draw"
    return "home" if ph >= pa else "away"


def _lean_line(m, ph, pd, pa, why=""):
    """Qərarsız oyunda da ən yüksək ehtimalı göstər: '38% ilə 2-ci komanda, amma qərarsız oyundur'."""
    t = _lean_team(ph, pd, pa)
    if t == "draw":
        top = f"Heç-heçə ehtimalı ən yüksəkdir (~{_pc(pd)}%)"
    elif t == "home":
        top = f"Ən yüksək ehtimal: {m.home} (~{_pc(ph)}%)"
    else:
        top = f"Ən yüksək ehtimal: {m.away} (~{_pc(pa)}%)"
    return f"   🤔 {top}, amma qərarsız oyundur{why}"


# ---------------------------------------------------------------- botun analizi
def _bot_verdict(m, h5, a5, info):
    """Qalib yalnız model + son 5 oyun forması + bazar bir-birinə ZİD DÜŞMƏSƏ verilir. Əks halda side=None (qərarsız)."""
    if not (info and info.get("pct") and h5 and a5):
        return None
    ph, pd, pa = info["pct"]
    fd = _ppg(h5) - _ppg(a5)
    side, reason = None, "close"
    if ph - pa >= 0.10 and ph >= 0.40:
        side = "home"
    elif pa - ph >= 0.10 and pa >= 0.40:
        side = "away"
    if (side == "home" and fd <= -0.3) or (side == "away" and fd >= 0.3):
        side, reason = None, "form"                   # forma modelə ziddir
    if side and m.p3:
        mh, _d, ma = m.p3
        if (side == "home" and ma - mh > 0.05) or (side == "away" and mh - ma > 0.05):
            side, reason = None, "market"             # bazar əks tərəfi üstün tutur
    return {"side": side, "ph": ph, "pd": pd, "pa": pa, "reason": reason}


def _goal_parts(h5, a5, m):
    """2.5 və 1.5 Üst/Alt — yalnız kifayət qədər əminlik olanda."""
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

    info = None
    if _c["verified"](m) and (m.info or {}).get("pct"):
        info = m.info
    elif code and h5 and a5:
        info = _own_info(m, code, cache)

    out = [f"{i}. 🕒 {m.start:%H:%M} · {m.home} - {m.away}", f"🏟 {m.league}", LINE,
           "🤖 Botun analizi — Yekun rəy"]

    # --- bot bloku
    v = _bot_verdict(m, h5, a5, info)
    bot_side = None
    bot_lean = None
    if v is None:
        out.append(f"   ℹ️ Statistika yoxdur: {_why_no_stats(m, code, cache)}")
    else:
        bot_side = v["side"]
        bot_lean = _lean_team(v["ph"], v["pd"], v["pa"])
        if bot_side == "home":
            out.append(f"   ✅ {m.home} qalib gələcək kimi görünür (~{_pc(v['ph'])}%)")
        elif bot_side == "away":
            out.append(f"   ✅ {m.away} qalib gələcək kimi görünür (~{_pc(v['pa'])}%)")
        else:
            out.append(_lean_line(m, v["ph"], v["pd"], v["pa"], _WHY[v["reason"]]))
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
    mk_lean = None
    if m.p3:
        h, d, a = m.p3
        mk_lean = _lean_team(h, d, a)
        if mk_side == "home":
            out.append(f"   ✅ {m.home} (~{_pc(mk_p)}%)")
        elif mk_side == "away":
            out.append(f"   ✅ {m.away} (~{_pc(mk_p)}%)")
        else:
            out.append(_lean_line(m, h, d, a))
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
            if bot_lean and bot_lean == mk_lean and bot_lean != "draw":
                team = m.home if bot_lean == "home" else m.away
                out.append(f"🟡 İkisi də {team}-ə meyillidir, amma əmin deyil — risk yüksəkdir")
            else:
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
