"""
BOTPLUS (v5 əlavəsi) — kupon botu üçün ayrıca modul.

1) 🖼  Kuponu ŞƏKİL kimi paylaş  — Pillow ilə birbaşa Render CPU-sunda çəkilir (xarici API/limit yoxdur)
2) ⭐  Sevimli komanda bildirişi — komanda/ölkə seç, oynadığı gün bot yazır: "KUPON YOXLA!"

Limitlərdən qorunma:
  * Şəkil: yalnız lokal CPU. Eyni anda max 2 render (semaphore), 45 san. timeout, nəticə keşlənir.
  * Bildiriş: yeni API sorğusu YOXDUR — botun onsuz da yüklədiyi bugünkü bazadan (peek_snapshot) oxuyur.
  * Redis: sevimli komandalar RAM-da; saatda ~1 oxuma + hər bildirişə 1 yazma.
  * Telegram: saniyədə ≤20 mesaj, RetryAfter gözlənilir, botu bloklayan istifadəçi avtomatik silinir.
  * Fon dövrü ölməzdir: hər xəta tutulur, 5 dəqiqədən bir təkrar işləyir.
"""
import asyncio
import io
import json
import logging
import os
import random
import re
import string
import time
import unicodedata
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta

from PIL import Image, ImageDraw, ImageFont
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

log = logging.getLogger("kuponbot.plus")

HERE = os.path.dirname(os.path.abspath(__file__))
IMG_SCALE = max(1, min(3, int(os.environ.get("IMG_SCALE", "2") or 2)))  # 1 = sürətli, 2 = hamar kənarlar
FAV_SEND_HOUR = int(os.environ.get("FAV_SEND_HOUR", "10") or 10)        # Bakı vaxtı ilə bildiriş saatı
FAV_TICK = 300                                                          # bildiriş dövrü (saniyə)
D = {}                                                                  # main-dən ötürülən asılılıqlar


def init(**deps):
    """main() içində bir dəfə çağırılır: botplus.init(kv=kv, TZ=TZ, T=T, ...)"""
    D.update(deps)


# ============================== MƏTNLƏR ==============================
PT = {
    "az": {
        "share_btn": "🖼 Şəkil kimi paylaş",
        "expired": "⌛ Bu kuponun şəkli artıq köhnəlib. Yeni kupon al və yenidən cəhd et.",
        "img_err": "⚠️ Şəkil hazırlana bilmədi. Bir az sonra yenidən cəhd et.",
        "img_caption": "🎯 Kupon hazırdır! Dostlarınla paylaş 👇",
        "games": "oyun",
        "total": "ÜMUMİ KEF",
        "tagline": "Futbol kupon köməkçisi",
        "team_none": (
            "⭐ Sevimli komandanı seç!\n\n"
            "Komanda (və ya ölkə) oynayan gün sənə xəbər verəcəm.\n"
            "Adını yaz (məs: Bavariya, Real Madrid, Azərbaycan) və ya aşağıdan seç 👇"
        ),
        "team_cur": "⭐ Sevimli komandan: {team}\n\nO oynayan gün sənə xəbər verəcəm. 🔔",
        "team_ask": "✍️ Komanda və ya ölkə adını yaz (məs: Bavariya, Barselona, Türkiyə).",
        "team_found": "Hansını nəzərdə tutursan?",
        "team_nf": "😕 Tapa bilmədim. Başqa cür yaz (ingiliscə də ola bilər) və ya /komandam ilə aşağıdakı siyahıdan seç.",
        "team_set": "✅ Sevimli komandan: {team}\nO oynayan gün sənə yazacağam! 🔔\nDəyişmək üçün: /komandam",
        "team_clr": "🗑 Sevimli komanda silindi. Bildirişlər söndürüldü.",
        "team_exp": "⌛ Seçim köhnəlib. /komandam yaz və yenidən cəhd et.",
        "b_change": "✏️ Dəyiş",
        "b_clear": "🗑 Sil",
        "b_type": "✍️ Adı yaz",
        "b_coupon": "🎯 Kupon al",
        "b_change2": "✏️ Komandanı dəyiş",
        "b_mute": "🔕 Söndür",
        "notify": (
            "⚽ Bu gün {team} oynayır!\n\n"
            "🆚 {home} - {away}\n"
            "🕒 {time} (Bakı) · {league}\n\n"
            "🎯 KUPON YOXLA!"
        ),
    },
    "en": {
        "share_btn": "🖼 Share as image",
        "expired": "⌛ This coupon image has expired. Get a new coupon and try again.",
        "img_err": "⚠️ Couldn't create the image. Please try again later.",
        "img_caption": "🎯 Your coupon is ready! Share it with friends 👇",
        "games": "games",
        "total": "TOTAL ODDS",
        "tagline": "Football coupon assistant",
        "team_none": (
            "⭐ Pick your favorite team!\n\n"
            "I'll message you on match day (works for national teams too).\n"
            "Type a name (e.g. Bayern, Real Madrid, Azerbaijan) or pick below 👇"
        ),
        "team_cur": "⭐ Your favorite team: {team}\n\nI'll notify you on match day. 🔔",
        "team_ask": "✍️ Type a team or country name (e.g. Bayern, Barcelona, Turkey).",
        "team_found": "Which one do you mean?",
        "team_nf": "😕 Couldn't find it. Try another spelling or use /myteam and pick from the list.",
        "team_set": "✅ Favorite team: {team}\nI'll message you on match day! 🔔\nTo change: /myteam",
        "team_clr": "🗑 Favorite team removed. Notifications are off.",
        "team_exp": "⌛ Selection expired. Send /myteam and try again.",
        "b_change": "✏️ Change",
        "b_clear": "🗑 Remove",
        "b_type": "✍️ Type a name",
        "b_coupon": "🎯 Get coupon",
        "b_change2": "✏️ Change team",
        "b_mute": "🔕 Mute",
        "notify": (
            "⚽ {team} play today!\n\n"
            "🆚 {home} - {away}\n"
            "🕒 {time} (Baku) · {league}\n\n"
            "🎯 CHECK A COUPON!"
        ),
    },
}


def P(lang):
    return PT.get(lang) or PT["az"]


def share_label(lang):
    return P(lang)["share_btn"]


# ============================== ŞƏKİL (PILLOW) ==============================
_EMOJI_RE = re.compile("[\U0001F000-\U0001FFFF\u2600-\u27BF\uFE0F\u2B00-\u2BFF\u200d]")
_font_cache = {}
_FONT_DIRS = [os.path.join(HERE, "fonts"), HERE, "/usr/share/fonts/truetype/dejavu",
              "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF", "/usr/local/share/fonts"]


def _noemoji(s):
    return _EMOJI_RE.sub("", s or "").strip()


def _font(size, bold=False):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    font = None
    for d in _FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            try:
                font = ImageFont.truetype(p, size)
                break
            except Exception:
                pass
    if font is None:      # şrift faylı tapılmasa da bot çökməsin (ə hərfi düzgün çıxmaya bilər)
        try:
            font = ImageFont.load_default(size=size)
        except Exception:
            font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _tw(draw, text, font):
    return draw.textlength(text, font=font)


def _fit(draw, text, font, maxw):
    """Uzun mətni sığdırana qədər kəs və '…' əlavə et."""
    if _tw(draw, text, font) <= maxw:
        return text
    while text and _tw(draw, text + "…", font) > maxw:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(draw, text, font, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if _tw(draw, t, font) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


TIER_COLORS = {"safe": (46, 204, 113), "normal": (64, 156, 255), "risky": (255, 107, 53)}
GOLD = (255, 205, 60)


def _logo(base, x, y, size, s):
    """Loqo: fayl varsa logo.png, yoxsa çəkilmiş futbol topu nişanı."""
    p = os.path.join(HERE, "logo.png")
    if os.path.exists(p):
        try:
            lg = Image.open(p).convert("RGBA").resize((int(size * s), int(size * s)), Image.LANCZOS)
            base.alpha_composite(lg, (int(x * s), int(y * s)))
            return
        except Exception:
            pass
    d = ImageDraw.Draw(base)
    S = lambda v: int(v * s)
    d.ellipse([S(x), S(y), S(x + size), S(y + size)], fill=(46, 204, 113))
    d.ellipse([S(x + 6), S(y + 6), S(x + size - 6), S(y + size - 6)], fill=(12, 24, 44))
    cx, cy, r = x + size / 2, y + size / 2, size * 0.30
    d.ellipse([S(cx - r), S(cy - r), S(cx + r), S(cy + r)], fill=(255, 255, 255))
    import math
    pent = [(S(cx + r * 0.42 * math.cos(math.radians(a - 90))),
             S(cy + r * 0.42 * math.sin(math.radians(a - 90)))) for a in range(0, 360, 72)]
    d.polygon(pent, fill=(12, 24, 44))
    for a in range(0, 360, 72):
        ex, ey = cx + r * 0.72 * math.cos(math.radians(a - 90)), cy + r * 0.72 * math.sin(math.radians(a - 90))
        d.ellipse([S(ex - r * 0.16), S(ey - r * 0.16), S(ex + r * 0.16), S(ey + r * 0.16)], fill=(12, 24, 44))


def render_coupon_image(data):
    """Kupon məlumatından (dict) JPEG bayt qaytarır. Xarici heç nə çağırmır."""
    s = IMG_SCALE
    W = 1080
    picks = data["picks"]
    n = len(picks)
    ROW_H, GAP = 134, 14
    top_h = 440
    foot_h = 270
    H = top_h + n * (ROW_H + GAP) + foot_h
    S = lambda v: int(v * s)
    L = P(data.get("lang", "az"))
    Lm = D["T"](data.get("lang", "az"))
    color = TIER_COLORS.get(data["tier"], (46, 204, 113))

    # --- fon: qradiyent + meydança xətləri ---
    base = Image.new("RGBA", (S(W), S(H)))
    g = ImageDraw.Draw(base)
    c1, c2 = (9, 16, 36), (5, 46, 38)
    for yy in range(S(H)):
        t = yy / max(1, S(H) - 1)
        g.line([(0, yy), (S(W), yy)], fill=tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)) + (255,))
    ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
    o = ImageDraw.Draw(ov)
    o.ellipse([S(W / 2 - 330), S(H - 330), S(W / 2 + 330), S(H + 330)], outline=(255, 255, 255, 16), width=S(5))
    o.line([(0, S(H - 0)), (S(W), S(H - 0))], fill=(255, 255, 255, 16), width=S(5))
    o.ellipse([S(-200), S(-200), S(300), S(300)], fill=color + (26,))
    # başlıq kartı
    o.rounded_rectangle([S(40), S(200), S(W - 40), S(400)], radius=S(34), fill=(255, 255, 255, 22),
                        outline=color + (120,), width=S(3))
    # oyun kartları
    y = top_h
    for _ in picks:
        o.rounded_rectangle([S(40), S(y), S(W - 40), S(y + ROW_H)], radius=S(26), fill=(255, 255, 255, 16))
        y += ROW_H + GAP
    # yekun kartı
    o.rounded_rectangle([S(40), S(y + 6), S(W - 40), S(y + 86)], radius=S(26), fill=(255, 255, 255, 12))
    base = Image.alpha_composite(base, ov)
    d = ImageDraw.Draw(base)

    # --- başlıq ---
    _logo(base, 50, 46, 120, s)
    d.text((S(196), S(52)), "KUPON BOT", font=_font(S(58), True), fill=(255, 255, 255))
    d.text((S(198), S(124)), L["tagline"], font=_font(S(26)), fill=(150, 170, 195))
    date_txt = data.get("date", "")
    dw = _tw(d, date_txt, _font(S(28), True))
    d.rounded_rectangle([S(W - 60) - dw - S(36), S(60), S(W - 40), S(112)], radius=S(26), fill=color)
    d.text((S(W - 40) - S(18) - dw, S(66)), date_txt, font=_font(S(28), True), fill=(8, 16, 30))

    # --- ümumi kef kartı ---
    tier_name = _noemoji(Lm["name_" + data["tier"]])
    d.text((S(76), S(226)), tier_name, font=_font(S(46), True), fill=color)
    d.text((S(78), S(292)), f"{n} {L['games']}", font=_font(S(32)), fill=(200, 212, 228))
    d.text((S(78), S(338)), _fit(d, _noemoji(Lm["tz_note"]), _font(S(22)), S(420)),
           font=_font(S(22)), fill=(130, 150, 175))
    tot = f"{data['total']:.2f}"
    tf = _font(S(104), True)
    tww = _tw(d, tot, tf)
    d.text((S(W - 76) - tww, S(214)), tot, font=tf, fill=GOLD)
    lab = L["total"]
    lf = _font(S(24), True)
    d.text((S(W - 76) - _tw(d, lab, lf), S(352)), lab, font=lf, fill=(190, 200, 215))

    # --- oyunlar ---
    y = top_h
    for i, p in enumerate(picks, 1):
        cy = y + ROW_H / 2
        d.ellipse([S(64), S(cy - 26), S(116), S(cy + 26)], fill=color)
        nf = _font(S(28), True)
        d.text((S(90) - _tw(d, str(i), nf) / 2, S(cy) - S(17)), str(i), font=nf, fill=(8, 16, 30))
        tx = 140
        teams = _fit(d, f"{p['home']} - {p['away']}", _font(S(31), True), S(700))
        d.text((S(tx), S(y + 16)), teams, font=_font(S(31), True), fill=(255, 255, 255))
        d.text((S(tx), S(y + 58)), _fit(d, _noemoji(p["pick"]), _font(S(26), True), S(700)),
               font=_font(S(26), True), fill=color)
        meta = f"{p['when']}  ·  {p['league']}"
        d.text((S(tx), S(y + 98)), _fit(d, meta, _font(S(21)), S(700)), font=_font(S(21)), fill=(135, 155, 180))
        od = ("≈" if p.get("approx") else "") + f"{p['odds']:.2f}"
        of = _font(S(42), True)
        d.text((S(W - 70) - _tw(d, od, of), S(cy) - S(26)), od, font=of, fill=GOLD)
        y += ROW_H + GAP

    # --- yekun ---
    prob = _noemoji(Lm["prob"].format(
        p=f"{data['prob'] * 100:.1f}" if data["prob"] < 0.1 else f"{data['prob'] * 100:.0f}"))
    d.text((S(70), S(y + 30)), _fit(d, prob, _font(S(28), True), S(W - 140)),
           font=_font(S(28), True), fill=(230, 238, 248))

    # --- footer ---
    fy = y + 124
    d.line([(S(60), S(fy)), (S(W - 60), S(fy))], fill=(70, 95, 105), width=S(2))
    disc = _noemoji(Lm["disclaimer"])
    ff = _font(S(21))
    for j, ln in enumerate(_wrap(d, disc, ff, S(W - 120))[:2]):
        d.text((S(60), S(fy + 22 + j * 30)), ln, font=ff, fill=(140, 158, 182))
    bu = D["bot_info"].get("username")
    owner = f"{D['OWNER_NAME']} ({D['OWNER_HANDLE']})"
    left = f"@{bu}" if bu else "KUPON BOT"
    bf = _font(S(30), True)
    d.text((S(60), S(fy + 88)), left, font=bf, fill=color)
    of2 = _font(S(21))
    d.text((S(W - 60) - _tw(d, owner, of2), S(fy + 98)), owner, font=of2, fill=(150, 170, 195))

    img = base.convert("RGB")
    if s != 1:
        img = img.resize((W, H), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, optimize=True)
    return buf.getvalue()


# ---- kuponun saxlanması (düymə basılanda şəkil çəkmək üçün) ----
_mem = OrderedDict()          # token -> data
_img_cache = OrderedDict()    # token -> jpeg bytes
_last_share = {}
_sem = {"s": None}


def _remember(od, key, val, cap):
    od[key] = val
    od.move_to_end(key)
    while len(od) > cap:
        od.popitem(last=False)


def save_share(coupon, lang):
    """Kupon verilən anda çağırılır. Qısa token qaytarır (callback_data: s:<token>). Xəta olsa None."""
    tz = D["TZ"]
    today = datetime.now(tz).date()
    L = D["T"](lang)
    picks = []
    for m, leg in coupon.picks:
        picks.append(dict(
            home=m.home, away=m.away,
            when=m.start.strftime("%H:%M") if m.start.date() == today else m.start.strftime("%d.%m %H:%M"),
            league=m.league, pick=D["pick_text"](L, m, leg),
            odds=round(leg.odds, 2), approx=bool(leg.approx)))
    data = dict(tier=coupon.tier, lang=lang, total=round(coupon.total, 2), prob=coupon.prob,
                picks=picks, date=today.strftime("%d.%m.%Y"))
    token = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    _remember(_mem, token, data, 300)
    try:
        D["kv"].set(f"shr:{token}", json.dumps(data), ex=86400)
    except Exception:
        log.warning("share kv yazılmadı (RAM-da qalır)")
    return token


def _load_share(token):
    if token in _mem:
        return _mem[token]
    try:
        raw = D["kv"].get(f"shr:{token}")
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _send_share(q, context, token):
    uid = q.from_user.id
    now = time.time()
    if now - _last_share.get(uid, 0) < 5:     # spam qoruması
        return
    _last_share[uid] = now
    lang = await _get_lang(uid)
    L = P(lang)
    data = await asyncio.to_thread(_load_share, token)
    if not data:
        await q.message.reply_text(L["expired"])
        return
    try:
        await context.bot.send_chat_action(q.message.chat_id, "upload_photo")
        jpg = _img_cache.get(token)
        if jpg is None:
            if _sem["s"] is None:
                _sem["s"] = asyncio.Semaphore(2)      # eyni anda ən çox 2 render (CPU qorunur)
            async with _sem["s"]:
                jpg = await asyncio.wait_for(asyncio.to_thread(render_coupon_image, data), 45)
            _remember(_img_cache, token, jpg, 20)
        bio = io.BytesIO(jpg)
        bio.name = "kupon.jpg"
        bu = D["bot_info"].get("username")
        cap = L["img_caption"] + (f"\n🤖 @{bu}" if bu else "")
        await context.bot.send_photo(q.message.chat_id, bio, caption=cap)
    except Exception:
        log.exception("şəkil göndərilmədi")
        try:
            await q.message.reply_text(L["img_err"])
        except Exception:
            pass


# ============================== SEVİMLİ KOMANDA ==============================
# (Odds API-dakı ingiliscə ad, Azərbaycanca göstərilən ad)
POPULAR = [
    ("Bayern Munich", "Bavariya"), ("Real Madrid", "Real Madrid"), ("Barcelona", "Barselona"),
    ("Manchester City", "Mançester Siti"), ("Manchester United", "Mançester Yunayted"),
    ("Liverpool", "Liverpul"), ("Arsenal", "Arsenal"), ("Chelsea", "Çelsi"),
    ("Tottenham Hotspur", "Tottenhem"), ("Paris Saint Germain", "PSJ"), ("Juventus", "Yuventus"),
    ("AC Milan", "Milan"), ("Inter Milan", "İnter"), ("Napoli", "Napoli"),
    ("Borussia Dortmund", "Borussiya Dortmund"), ("Atletico Madrid", "Atletiko Madrid"),
    ("Benfica", "Benfika"), ("Porto", "Porto"), ("Ajax", "Ayaks"),
    ("Galatasaray", "Qalatasaray"), ("Fenerbahce", "Fənərbağça"), ("Besiktas", "Beşiktaş"),
    ("Qarabag", "Qarabağ"), ("Azerbaijan", "Azərbaycan"), ("Turkey", "Türkiyə"),
    ("Germany", "Almaniya"), ("Spain", "İspaniya"), ("France", "Fransa"), ("England", "İngiltərə"),
    ("Portugal", "Portuqaliya"), ("Italy", "İtaliya"), ("Netherlands", "Niderland"),
]
QUICK = [0, 1, 2, 3, 5, 22, 23, 24]      # POPULAR-dakı sürətli seçim düymələri
_XTRA_ALIASES = {"bayern": "Bayern Munich", "munhen": "Bayern Munich", "real": "Real Madrid",
                 "barca": "Barcelona", "siti": "Manchester City", "psg": "Paris Saint Germain",
                 "psj": "Paris Saint Germain", "yuve": "Juventus", "inter": "Inter Milan"}
_SYN = {"turkiye": "turkey", "czechia": "czech republic", "holland": "netherlands"}
_TR = str.maketrans({"ə": "e", "Ə": "e", "ı": "i", "İ": "i", "I": "i"})


def _norm(s):
    s = unicodedata.normalize("NFKD", (s or "").translate(_TR))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = " ".join(re.findall(r"[a-z0-9]+", s))
    return _SYN.get(s, s)


_ALIAS, _DISPLAY, _ready = {}, {}, {"v": False}
_known = set()
_favs = {}                              # str(uid) -> ingiliscə komanda adı
_sent = {"day": None, "map": {}}        # bu gün bildiriş göndərilənlər: uid -> komanda
_st = {"favs_t": 0.0}
_awaiting, _cands = {}, {}


def _prepare():
    if _ready["v"]:
        return
    for en, az in POPULAR:
        _ALIAS[_norm(az)] = en
        _ALIAS[_norm(en)] = en
        _DISPLAY[en] = az
    for az, en in D["AZ_EN_TEAM_ALIASES"].items():
        _ALIAS.setdefault(_norm(az), en.title())
        _DISPLAY.setdefault(en.title(), az.title())
    for k, v in _XTRA_ALIASES.items():
        _ALIAS[_norm(k)] = v
    _ready["v"] = True


def _disp(team, lang):
    _prepare()
    return _DISPLAY.get(team, team) if lang == "az" else team


def _pool():
    _prepare()
    seen, out = set(), []
    for c in [en for en, _ in POPULAR] + [en.title() for en in D["AZ_EN_TEAM_ALIASES"].values()] + sorted(_known):
        n = _norm(c)
        if n not in seen:
            seen.add(n)
            out.append(c)
    return out


def find_teams(query):
    """Yazılan mətnə görə ən çox 5 komanda/ölkə. Azərbaycanca və ingiliscə işləyir."""
    _prepare()
    qn = _norm(query)
    if len(qn) < 2:
        return []
    scored = {}
    if qn in _ALIAS:
        scored[_ALIAS[qn]] = 1.0
    if len(qn) >= 3:
        for k, v in _ALIAS.items():
            if qn in k:
                scored[v] = max(scored.get(v, 0), 0.9)
    ns = D["name_score"]
    for c in _pool():
        cn = _norm(c)
        if qn == cn:
            sc = 1.0
        elif len(qn) >= 3 and (qn in cn or cn in qn):
            sc = 0.92
        else:
            sc = ns(qn, c) * 0.95
        if sc >= 0.72:
            scored[c] = max(scored.get(c, 0), sc)
    return [t for t, _ in sorted(scored.items(), key=lambda x: -x[1])[:5]]


def _same(team, name):
    a, b = _norm(team), _norm(name)
    return a == b or D["name_score"](a, b) >= 0.85


async def _get_lang(uid):
    try:
        lang = await asyncio.to_thread(D["get_lang"], uid)
    except Exception:
        lang = None
    return lang or D["DEFAULT_LANG"] if (lang or D["DEFAULT_LANG"]) in PT else "az"


async def _set_team(uid, team):
    _favs[str(uid)] = team
    try:
        await asyncio.to_thread(D["kv"].hset, "favteam", str(uid), team)
    except Exception:
        log.exception("favteam yazılmadı")


def _quick_kb(lang, extra_rows=()):
    rows = []
    pair = []
    for i in QUICK:
        en, az = POPULAR[i]
        pair.append(InlineKeyboardButton(az if lang == "az" else en, callback_data=f"fav:q:{i}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows += list(extra_rows)
    return InlineKeyboardMarkup(rows)


async def _team_menu(send, uid):
    lang = await _get_lang(uid)
    L = P(lang)
    cur = _favs.get(str(uid))
    if cur:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(L["b_change"], callback_data="fav:ask"),
                                    InlineKeyboardButton(L["b_clear"], callback_data="fav:clear")]])
        await send(L["team_cur"].format(team=_disp(cur, lang)), reply_markup=kb)
    else:
        _awaiting[uid] = time.time()
        await send(L["team_none"], reply_markup=_quick_kb(lang))


async def cmd_team(update, context):
    uid = update.effective_user.id
    await _team_menu(update.message.reply_text, uid)


async def on_text(update, context):
    """Yalnız komanda adı gözlənildikdə işləyir; digər mətnlərə toxunmur."""
    if not update.message or not update.message.text:
        return
    uid = update.effective_user.id
    t0 = _awaiting.get(uid)
    if not t0 or time.time() - t0 > 600:
        return
    lang = await _get_lang(uid)
    L = P(lang)
    res = find_teams(update.message.text)
    if not res:
        await update.message.reply_text(L["team_nf"])
        return
    _awaiting.pop(uid, None)
    _cands[uid] = res
    rows = [[InlineKeyboardButton(_disp(t, lang), callback_data=f"fav:pick:{i}")] for i, t in enumerate(res)]
    await update.message.reply_text(L["team_found"], reply_markup=InlineKeyboardMarkup(rows))


async def on_callback(update, context):
    """s:<token> (şəkil paylaş) və fav:* (sevimli komanda) düymələri."""
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id
    if data.startswith("s:"):
        await _send_share(q, context, data[2:])
        return
    lang = await _get_lang(uid)
    L = P(lang)
    parts = data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    if act == "menu":
        await _team_menu(q.message.reply_text, uid)
    elif act == "ask":
        _awaiting[uid] = time.time()
        await q.message.reply_text(L["team_ask"], reply_markup=_quick_kb(lang))
    elif act == "clear":
        _favs.pop(str(uid), None)
        try:
            await asyncio.to_thread(D["kv"].hset, "favteam", str(uid), "")
        except Exception:
            log.exception("favteam silinmədi")
        await q.message.reply_text(L["team_clr"])
    elif act in ("q", "pick") and len(parts) == 3 and parts[2].isdigit():
        i = int(parts[2])
        if act == "q":
            team = POPULAR[i][0] if i < len(POPULAR) else None
        else:
            c = _cands.get(uid) or []
            team = c[i] if i < len(c) else None
        if not team:
            await q.message.reply_text(L["team_exp"])
            return
        _awaiting.pop(uid, None)
        await _set_team(uid, team)
        await q.message.reply_text(L["team_set"].format(team=_disp(team, lang)))


# ---- fon dövrü: bildiriş göndərmə ----
async def _load_state(day):
    t = time.time()
    if t - _st["favs_t"] > 3600:
        raw = await asyncio.to_thread(D["kv"].hgetall, "favteam")
        _favs.clear()
        _favs.update({k: v for k, v in raw.items() if v})
        _st["favs_t"] = t
    if _sent["day"] != day:
        raw = await asyncio.to_thread(D["kv"].hgetall, f"favsent:{day}")
        _sent["day"], _sent["map"] = day, dict(raw)


async def _learn(snap):
    """Bazadakı komanda adlarını axtarış siyahısına əlavə edir (yalnız yeni ad çıxanda 1 yazma)."""
    if not _known:
        try:
            raw = await asyncio.to_thread(D["kv"].get, "teams:known")
            if raw:
                _known.update(json.loads(raw))
        except Exception:
            pass
    new = {m.home for m in snap.matches} | {m.away for m in snap.matches}
    new -= _known
    if new and len(_known) < 3000:
        _known.update(new)
        try:
            await asyncio.to_thread(D["kv"].set, "teams:known", json.dumps(sorted(_known)))
        except Exception:
            log.warning("teams:known yazılmadı")


async def _notify(bot, uid, team, m, day):
    lang = await _get_lang(uid)
    L = P(lang)
    text = L["notify"].format(team=_disp(team, lang), home=m.home, away=m.away,
                              time=m.start.strftime("%H:%M"), league=m.league)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(L["b_coupon"], callback_data="m")],
        [InlineKeyboardButton(L["b_change2"], callback_data="fav:menu"),
         InlineKeyboardButton(L["b_mute"], callback_data="fav:clear")],
    ])
    for _attempt in (1, 2):
        try:
            await bot.send_message(int(uid), text, reply_markup=kb)
            break
        except RetryAfter as e:
            ra = e.retry_after
            ra = ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)
            await asyncio.sleep(min(ra, 60) + 1)
        except Forbidden:                    # istifadəçi botu bloklayıb → bir də yazma
            _favs.pop(uid, None)
            await asyncio.to_thread(D["kv"].hset, "favteam", uid, "")
            return
        except BadRequest as e:
            if "chat not found" in str(e).lower():
                _favs.pop(uid, None)
                await asyncio.to_thread(D["kv"].hset, "favteam", uid, "")
            else:
                log.warning("bildiriş BadRequest: %s", e)
            return
        except TelegramError as e:          # müvəqqəti xəta: işarələmirik, növbəti dövrdə təkrar
            log.warning("bildiriş xətası (%s): %s", uid, e)
            return
    else:
        return
    _sent["map"][uid] = team
    try:
        await asyncio.to_thread(D["kv"].hset, f"favsent:{day}", uid, team)
    except Exception:
        log.warning("favsent yazılmadı")


async def _tick(bot):
    snap, _ok, _tried = D["peek_snapshot"]()       # yeni sorğu YOXDUR, kredit xərclənmir
    if snap is None:
        return
    tz = D["TZ"]
    now = datetime.now(tz)
    day = now.date().isoformat()
    await _load_state(day)
    await _learn(snap)
    if not _favs:
        return
    today = sorted((m for m in snap.matches
                    if m.start.date() == now.date() and m.start > now + timedelta(minutes=10)),
                   key=lambda m: m.start)
    if not today:
        return
    by_team = defaultdict(list)
    for uid, team in list(_favs.items()):
        if team and _sent["map"].get(uid) != team:
            by_team[team].append(uid)
    opening = now.replace(hour=FAV_SEND_HOUR, minute=0, second=0, microsecond=0)
    for team, uids in by_team.items():
        m = next((x for x in today if _same(team, x.home) or _same(team, x.away)), None)
        if not m:
            continue
        if now < min(opening, m.start - timedelta(hours=3)):
            continue                                # hələ tezdir
        log.info("Sevimli komanda bildirişi: %s (%d nəfər)", team, len(uids))
        for uid in uids:
            await _notify(bot, uid, team, m, day)
            await asyncio.sleep(0.06)               # Telegram limiti (~20 mesaj/san.) qorunur


async def fav_loop(app):
    """Ölməz dövr: hər xəta tutulur, 5 dəqiqədən bir təkrar işləyir."""
    await asyncio.sleep(60)
    while True:
        try:
            await _tick(app.bot)
        except Exception:
            log.exception("fav_loop xətası")
        await asyncio.sleep(FAV_TICK)
