"""
KUPON BOT v4 — futbol kupon köməkçisi (Azərbaycan məntiqinə uyğun, çoxdilli).
Hazırlayan: Mirhüseyn Bağırov (@Baghirov21)

Mühit dəyişənləri (Render → Environment):
  BOT_TOKEN                 (məcburi) BotFather tokeni
  ODDS_API_KEY               (məcburi) the-odds-api.com açarı (kefləri verir)
  API_FOOTBALL_KEY          (tövsiyə) api-football.com açarı (REAL komanda statistikası).
                            Qoyulmasa bot yalnız bazar kefinə əsaslanır.
                            DİQQƏT: pulsuz plan cari mövsümü vermir. Belə olsa bot AVTOMATİK
                            yalnız bazar kefinə keçir (kupon verməyi dayandırmır).
  ADMIN_ID                  (tövsiyə) Telegram ID-n; birdən çox olsa vergüllə: 123,456
  UPSTASH_REDIS_REST_URL    (tövsiyə) daimi statistika + kredit qorunması üçün (https://...)
  UPSTASH_REDIS_REST_TOKEN  (tövsiyə) yuxarıdakı ilə birlikdə
  USER_DAILY_LIMIT          (ixtiyari) hər istifadəçiyə gündə neçə kupon krediti, default 3 (0 = limitsiz)
  ADMIN_EXEMPT              (ixtiyari) 1 = adminlərə limit tətbiq olunmur (default), 0 = olunur
  DAILY_CREDIT_BUDGET       (ixtiyari) Odds API əsas baza üçün gündə maksimum kredit, default 10
  EXTRA_CREDIT_BUDGET       (ixtiyari) Odds API korner/kart kefləri üçün gündə maksimum kredit, default 6
  EXTRA_LEAGUE_RESERVE      (ixtiyari) əsas büdcədən neçə krediti aşağı/əlavə liqalara ayırmaq, default 3
  FOOTBALL_DAILY_BUDGET     (ixtiyari) API-Football gündə maksimum sorğu, default 400
  STATS_MAX_MATCHES         (ixtiyari) statistika çəkiləcək maksimum oyun sayı, default 30
  DEFAULT_LANG              (ixtiyari) az / en, default az
  CONTACT                   (ixtiyari) məxfilik mətnində göstərilən əlaqə (@username)
  OWNER_NAME / OWNER_HANDLE (ixtiyari) qurucu adı və @istifadəçi adı (default: Mirhüseyn Bağırov / @Baghirov21)
  PROMO_TEXT                (ixtiyari) /about-dakı reklam cümləsi (boşdursa standart mətn)
  SHOW_OWNER_IN_COUPON      (ixtiyari) 1 = kuponun sonunda qurucu sətri göstərilir (default), 0 = gizlət
  BOT_USERNAME              (ixtiyari) avtomatik tapılır; tapılmasa əl ilə yaz (@-siz)
  FOOTBALL_DATA_ORG_KEY     (tövsiyə) football-data.org PULSUZ token-i (12 əsas liqada statistika)
  GEMINI_API_KEY            (tövsiyə) Google AI Studio-dan pulsuz açar — /kuponumabax (kupon şəkli oxuma) üçün

v4-də nə düzəldilib:
  * Statistika işləmirsə (pulsuz plan, xəta, az oyun) bot avtomatik bazar rejiminə keçir və kuponda bunu yazır.
  * Baza fonda hazırlanır (hər 10 dəq. yoxlanır). İstifadəçi heç vaxt uzun gözləmir; hazırlanırsa "hazırlanır"
    mesajı görür və kredit xərclənmir.
  * Statistika şərtləri az oyun qalanda avtomatik yumşalır (riskli kupon boş qalmasın).
  * Korner/kart kreditləri yalnız statistikası təsdiqlənmiş oyunlara xərclənir.
  * Aşağı/əlavə liqalar üçün büdcədə ehtiyat ayrılır (həftə sonu da görünsünlər).
  * Odds API real xərci (x-requests-last) sayılır; API-Football gündəlik büdcəsi kumulyativdir.
  * Parametr uyğunsuzluğuna davamlılıq (422 olarsa parametrsiz təkrar).
  * /about (qurucu + reklam + paylaşma linki), kuponun sonunda qurucu sətri.
  * PULSUZ football-data.org inteqrasiyası: 1X2 üçün öz Poisson modelimizlə statistika (12 əsas liqa).
  * DÜZƏLİŞ: pick seçimi yenidən EHTİMALA görə aparılır (kef hədəfə yaxınlıq ikinci dərəcəli meyardır) ki,
    eyni oyun üçün "Başqa variant" bir-birinə zidd pick (bir dəfə 1X, bir dəfə X2) verməsin.
  * YENİ: /kuponumabax — istifadəçi öz kuponunun şəklini atır, Gemini vision ilə oxunur, hər oyuna
    reaksiya verilir və sonda faizsiz ümumi rəy verilir.
"""
import asyncio
import base64
import difflib
import json
import logging
import math
import os
import random
import re
import threading
import time
import unicodedata
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
    MessageHandler,
    filters,
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


def env_int(name, default):
    try:
        return int(env(name, str(default)) or default)
    except ValueError:
        log.error("%s rəqəm olmalıdır, default %s istifadə olunur", name, default)
        return default


BOT_TOKEN = env("BOT_TOKEN")
ODDS_API_KEY = env("ODDS_API_KEY")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN təyin edilməyib (Render → Environment)")
if not ODDS_API_KEY:
    raise RuntimeError("ODDS_API_KEY təyin edilməyib (Render → Environment)")

FOOTBALL_KEY = env("API_FOOTBALL_KEY")
STATS_MODE = bool(FOOTBALL_KEY)

ADMIN_IDS = {int(x) for x in re.findall(r"\d+", env("ADMIN_ID"))}
ADMIN_EXEMPT = env("ADMIN_EXEMPT", "1") != "0"
DEFAULT_LANG = env("DEFAULT_LANG", "az")
USER_DAILY_LIMIT = env_int("USER_DAILY_LIMIT", 3)
DAILY_CREDIT_BUDGET = env_int("DAILY_CREDIT_BUDGET", 10)
EXTRA_CREDIT_BUDGET = env_int("EXTRA_CREDIT_BUDGET", 6)
EXTRA_LEAGUE_RESERVE = env_int("EXTRA_LEAGUE_RESERVE", 3)
FOOTBALL_DAILY_BUDGET = env_int("FOOTBALL_DAILY_BUDGET", 400)
STATS_MAX_MATCHES = env_int("STATS_MAX_MATCHES", 30)
CONTACT = env("CONTACT")
OWNER_NAME = env("OWNER_NAME", "Mirhüseyn Bağırov")
OWNER_HANDLE = env("OWNER_HANDLE", "@Baghirov21")
PROMO_TEXT = env("PROMO_TEXT")
SHOW_OWNER_IN_COUPON = env("SHOW_OWNER_IN_COUPON", "1") != "0"

# --- /kuponumabax (kupon şəkli oxuma) ---
GEMINI_API_KEY = env("GEMINI_API_KEY")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash")
COUPON_READ_ENABLED = bool(GEMINI_API_KEY)
COUPON_WAIT_SECONDS = 10 * 60

TZ = ZoneInfo("Asia/Baku")

ODDS_BASE = "https://api.the-odds-api.com/v4"
FOOTBALL_BASE = "https://v3.football.api-sports.io"
CREDIT_RESERVE = 5          # Odds API kreditinin bu qədəri həmişə ehtiyatda qalır
LOW_CREDIT_WARN = 60        # Odds API krediti bundan aşağı düşəndə admin xəbərdar olunur
RETRY_AFTER_FAIL = 10 * 60  # uğursuz yükləməni 10 dəq. sonra yenidən yoxla
STATS_RETRY = 15 * 60       # statistika uğursuz olubsa 15 dəq. sonra yenidən yoxla
REFRESH_EVERY = 10 * 60     # fon dövrü baza vəziyyətini bu intervalla yoxlayır
MAX_FETCH_TRIES = 6         # natamam baza ən çox bu qədər yenidən yüklənir (10/20/30/30/30 dəq. fasilə)
THIN_SNAPSHOT = 6           # bazada bundan az oyun varsa "natamam" sayılır və yenidən yoxlanır (oyun-yoxlaması PULSUZDUR)
COOLDOWN = 2.0              # eyni istifadəçinin düymə basma fasiləsi (saniyə)
MIN_MINUTES_BEFORE_KICKOFF = 15  # başlamağa 15 dəq. qalmış oyunları kupona salma

# Korner / kart (Odds API: alternate_totals_corners / alternate_totals_cards, oyun-oyun sorğu)
EXTRA_MARKETS = "alternate_totals_corners,alternate_totals_cards"
EXTRA_LINES = {"corners": (7.5, 13.5), "cards": (2.5, 6.5)}   # məntiqli xətt aralığı
EXTRA_KINDS = {"corners_over", "corners_under", "cards_over", "cards_under"}
MAX_EXTRAS = 2              # bir kuponda ən çox korner/kart pick sayı
MAX_NOTES = 3               # bir kuponda ən çox analiz qeydi sayı

# Statistika
PROFILE_GAMES = 6           # komandanın son neçə oyunu (korner/kart üçün)
MIN_PROFILE_GAMES = 4       # ən azı bu qədər oyunun statistikası olmalıdır
MIN_VERIFIED = 8            # statistika rejiminin işləməsi üçün bu qədər təsdiqlənmiş oyun lazımdır
FINISHED = {"FT", "AET", "PEN"}

# Statistika təsdiqi hədləri: pick statistika ilə "üst-üstə düşməlidir".
# "strict" — əsas rejim; "relaxed" — namizəd oyun az qalanda avtomatik istifadə olunur.
GATES = {
    "strict": dict(win_min=0.45, win_gap=0.15, dc_min=0.70, form_losses=4,
                   goals_over=2.7, goals_under=2.3, corner_margin=0.8, card_margin=0.6),
    "relaxed": dict(win_min=0.40, win_gap=0.10, dc_min=0.65, form_losses=5,
                    goals_over=2.55, goals_under=2.45, corner_margin=0.5, card_margin=0.4),
}

# Məşhur liqalar: hər biri üçün 2 market (h2h + totals) = 2 kredit.
# Siyahıda olan liqa Odds API-da yoxdursa, sadəcə ignor olunur.
# QEYD: Millətlər Liqası (Nations League) və dövlət yığmaları arası turnirlər ən yuxarıda —
# beynəlxalq fasilə günlərində bunlar Argentina/Braziliya kimi klub liqalarından ÖNCƏLİKLİDİR.
TOP_LEAGUES = [
    "soccer_uefa_nations_league",
    "soccer_fifa_world_cup_qualification",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league",
    "soccer_uefa_europa_conference_league",
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
#  min_legs        : bundan az oyunla kupon verilmir
#  extra_w         : {əlavə/aşağı liqadan neçə oyun icazəlidir: çəki} — YALNIZ məşhur liqalarda
#                    kifayət qədər oyun olmayanda (fallback) istifadə olunur, süni "müxtəliflik" üçün yox
#  league_cap      : eyni liqadan maksimum oyun
#  extras          : korner/kart pick-lərinə icazə (ehtiyatlı kuponda YOX)
TIERS = {
    "safe": dict(icon="🛡", pick_lo=1.15, pick_hi=1.60, lo=2.0, hi=5.0,
                 legs={3: 5, 4: 5}, min_legs=3, extra_w={0: 1}, league_cap=2, extras=False),
    "normal": dict(icon="⚖️", pick_lo=1.20, pick_hi=1.90, lo=5.0, hi=10.0,
                   legs={5: 5, 6: 5}, min_legs=4, extra_w={0: 6, 1: 4}, league_cap=2, extras=True),
    "risky": dict(icon="🔥", pick_lo=1.25, pick_hi=2.10, lo=10.0, hi=22.0,
                  legs={7: 2, 8: 4, 9: 4}, min_legs=6, extra_w={0: 4, 1: 3, 2: 2, 3: 1},
                  league_cap=3, extras=True),
}
MAX_PER_KIND = {"dc12": 2, "over": 3, "under": 3,
                "corners_over": 1, "corners_under": 1,
                "cards_over": 1, "cards_under": 1}  # kuponda eyni tip çox təkrarlanmasın

# ============================== MƏTNLƏR (i18n) ==============================
TXT = {
    "az": {
        "welcome": (
            "👋 Salam! Mən futbol kupon köməkçisiyəm.\n\n"
            "Oyunların bazar kefləri və komanda statistikası əsasında ehtiyatlı, normal və riskli kupon hazırlayıram.\n\n"
            "🎯 Kupon üçün: /gununoyunlari\n"
            "🌐 Dil: /lang · 🔒 Məxfilik: /privacy · ℹ️ Haqqında: /about\n\n"
            "⚠️ 18+ · Mərc risklidir, zəmanət yoxdur. Yalnız itirə biləcəyin məbləği qoy."
        ),
        "welcome_limit": "\n\n🎟 Hər gün {limit} kupon krediti verilir (Bakı vaxtı ilə 00:00-da yenilənir).",
        "made_by": "👨‍💻 Hazırlayan: {name} ({handle})",
        "about": "👨‍💻 Bot haqqında\n\nBu botu {name} ({handle}) hazırlayıb.",
        "promo": "🚀 Sənə də Telegram bot, sayt və ya SMM xidməti lazımdırsa, yaz: {handle}",
        "share": "📣 Dostlarına göndər: {link}",
        "menu_title": "Hansı kuponu istəyirsən?",
        "btn_coupon": "🎯 Kupon al",
        "btn_safe": "🛡 Ehtiyatlı · 3-4 oyun",
        "btn_normal": "⚖️ Normal · 5-6 oyun",
        "btn_risky": "🔥 Riskli · çox oyunlu",
        "btn_again": "🔄 Başqa variant",
        "btn_menu": "📋 Menyu",
        "checking": "⏳ Oyunlar yoxlanılır...",
        "preparing": "⏳ Bugünkü oyunlar və statistika hazırlanır. 1-2 dəqiqə sonra yenidən yoxla. Krediti xərclənmədi.",
        "name_safe": "Ehtiyatlı kupon",
        "name_normal": "Normal kupon",
        "name_risky": "Riskli kupon",
        "head": "{icon} {name}\n{n} oyun · ümumi kef ≈ {total}",
        "tz_note": "🕒 Vaxtlar Bakı vaxtı ilədir.",
        "odds_word": "kef",
        "prob": "📈 Bazar əsasında uduş ehtimalı: ~{p}%",
        "short_note": "ℹ️ Bu gün kifayət qədər uyğun oyun olmadığı üçün hədəf kefə/oyun sayına tam çatmadı.",
        "approx_note": "≈ təxmini kefdir (1X, X2, 12 üçün hesablanır). Mərc etməzdən əvvəl mərc şirkətində yoxla.",
        "extra_note": "ℹ️ Korner/kart xətləri və kefləri mərc şirkətlərində fərqli ola bilər. Mərcdən əvvəl yoxla.",
        "stats_note": "ℹ️ Statistika API-Football məlumatlarına əsaslanır (son oyunlar, mövsüm ortalaması). Zəmanət deyil.",
        "fb_note": "ℹ️ Statistika müvəqqəti əlçatmazdır, kupon yalnız bazar kefinə əsaslanır.",
        "disclaimer": "⚠️ 18+ · Zəmanət yoxdur. Kefləri mərc şirkətində yoxla. Yalnız itirə biləcəyin məbləği qoy.",
        "left": "🎟 Bu gün qalan kupon krediti: {left}/{limit}",
        "left_last": "⚠️ Bu, bu gün üçün sonuncu kupon krediti idi. Yeni kreditlər sabah verilir.",
        "limit_reached": (
            "🚫 Bu gün {limit}/{limit} kupon kreditiniz bitib.\n\n"
            "Yeni kreditlər sabah, Bakı vaxtı ilə 00:00-dan sonra verilir. 🙌"
        ),
        "no_matches": "😕 Bu gün üçün uyğun oyun qalmayıb (çoxu artıq başlayıb). Yeni günün oyunları Bakı vaxtı ilə 00:00-dan sonra yüklənir.",
        "no_matches_stats": "😕 Bu gün üçün statistikası təsdiqlənmiş uyğun oyun tapılmadı. Bir az sonra yenidən yoxla.",
        "no_tier": "😕 Bu gün «{name}» üçün kifayət qədər uyğun oyun yoxdur. Başqa növü seç və ya bir az sonra yenidən yoxla.",
        "data_error": "⚠️ Oyun məlumatı hazırda alına bilmir. Bir az sonra yenidən yoxla.",
        "quota_out": "⚠️ Bu günün oyun məlumatı limiti bitib. Sabah yenidən yoxla.",
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
        "p_corners_over": "Korner {line} Üst",
        "p_corners_under": "Korner {line} Alt",
        "p_cards_over": "Kart {line} Üst",
        "p_cards_under": "Kart {line} Alt",
        # statistikaya əsaslanan analiz qeydləri (API-Football)
        "s_form": "📊 Forma (son 5): {h} {fh} · {a} {fa}",
        "s_model": "🎯 Model 1/X/2: {ph}/{pd}/{pa}%",
        "s_cor": "📊 Gözlənən korner (son {n} oyun): {h} ~{eh} · {a} ~{ea} · cəmi ~{e}",
        "s_crd": "📊 Gözlənən kart (son {n} oyun): {h} ~{eh} · {a} ~{ea} · cəmi ~{e}",
        "s_goals": "📊 Gözlənən qol (mövsüm ortalaması): {h} ~{eh} · {a} ~{ea} · cəmi ~{e}",
        # yalnız bazar əsaslı qeydlər (statistika yoxdursa)
        "n_1x2": "📊 Bazar: 1 → {h}% · X → {d}% · 2 → {a}%",
        "n_corners_over": "💡 Bazar bu oyunda {line}-dən çox korner gözləyir (~{p}%)",
        "n_corners_under": "💡 Bazar bu oyunda {line}-dən az korner gözləyir (~{p}%)",
        "n_cards_over": "💡 Bazar bu oyunda {line}-dən çox kart gözləyir (~{p}%)",
        "n_cards_under": "💡 Bazar bu oyunda {line}-dən az kart gözləyir (~{p}%)",
        "n_over": "💡 Bazar {line}-dən çox qol gözləyir (~{p}%)",
        "n_under": "💡 Bazar {line}-dən az qol gözləyir (~{p}%)",
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
        "a_snap": "⚽ Bazada: {m} oyun · {l} liqa · Odds API xərc {s} · qalan {r}",
        "a_snap_x": "🎯 Korner/kart xətti olan oyun: {x}",
        "a_stats": "📈 Statistika: {v}/{m} oyun təsdiqlənib · API-Football sorğu {c} · qalan {r}{err}",
        "a_stats_fb": "⚠️ Statistika hazırda istifadə olunmur (təsdiqlənmiş oyun azdır) — kuponlar yalnız bazar kefinə əsaslanır.",
        "a_stats_off": "📈 Statistika söndürülüb (API_FOOTBALL_KEY yoxdur) — kuponlar yalnız bazar kefinə əsaslanır.",
        "a_limit": "🎟 İstifadəçi limiti: gündə {n} kupon",
        "a_partial": "⚠️ Baza natamamdır (cəhd {t}/{m}, uğursuz sorğu {f}) — bot özü yenidən yoxlayacaq. Dərhal yeniləmək üçün: /yenile",
        "r_wait": "⏳ Baza yenilənir...",
        "r_done": "🔄 Yeniləndi: {m} oyun · {l} liqada oyun var · uğursuz sorğu {f} · Odds API qalan {r}\n\n{lg}",
        "a_credits": "💳 Bu gün Odds API xərci: əsas {o}/{ob} · korner/kart {x}/{xb}",
        "a_snap_none": "⚽ Oyun bazası hələ yüklənməyib.",
        "a_none": "hələ yoxdur",
        # nəticə izləmə (settlement)
        "stat_title": "📊 Son {n} gün — tutma faizi (şəffaflıq üçün)",
        "stat_row": "{icon} {name}: {pct}% ({settled} kupon)",
        "stat_empty": "📊 Statistika hələ toplanır — kifayət qədər tamamlanmış kupon yoxdur. Bir neçə gün sonra yenidən yoxla.",
        "stat_footer": "\n⚠️ Keçmiş nəticələr gələcək uğuru zəmanət etmir. 18+ · Məsuliyyətlə oyna.",
        "my_title": "🎫 Son {n} gün kupon tarixçən: {total} kupon",
        "my_row": "✅ {w} tutub · ❌ {l} tutmayıb · ⏳ {p} gözlənilir",
        "my_empty": "Hələ kupon tarixçən yoxdur — /gununoyunlari ilə ilk kuponunu al.",
        "s_win": "tutub",
        "s_loss": "tutmayıb",
        "s_pending": "gözlənilir",
    },
    "en": {
        "welcome": (
            "👋 Hi! I'm a football coupon assistant.\n\n"
            "I build safe, normal and risky coupons from market odds and team statistics.\n\n"
            "🎯 Get a coupon: /coupon\n"
            "🌐 Language: /lang · 🔒 Privacy: /privacy · ℹ️ About: /about\n\n"
            "⚠️ 18+ · Betting is risky, nothing is guaranteed. Only stake what you can afford to lose."
        ),
        "welcome_limit": "\n\n🎟 You get {limit} coupon credits per day (resets at 00:00 Baku time).",
        "made_by": "👨‍💻 Made by {name} ({handle})",
        "about": "👨‍💻 About the bot\n\nThis bot was built by {name} ({handle}).",
        "promo": "🚀 Need a Telegram bot, a website or SMM services? Message {handle}",
        "share": "📣 Share with friends: {link}",
        "menu_title": "Which coupon do you want?",
        "btn_coupon": "🎯 Get coupon",
        "btn_safe": "🛡 Safe · 3-4 games",
        "btn_normal": "⚖️ Normal · 5-6 games",
        "btn_risky": "🔥 Risky · many games",
        "btn_again": "🔄 Another variant",
        "btn_menu": "📋 Menu",
        "checking": "⏳ Checking matches...",
        "preparing": "⏳ Today's matches and stats are being prepared. Try again in 1-2 minutes. No credit was used.",
        "name_safe": "Safe coupon",
        "name_normal": "Normal coupon",
        "name_risky": "Risky coupon",
        "head": "{icon} {name}\n{n} games · total odds ≈ {total}",
        "tz_note": "🕒 Times are Baku time (UTC+4).",
        "odds_word": "odds",
        "prob": "📈 Market-implied win chance: ~{p}%",
        "short_note": "ℹ️ Not enough suitable games today to fully reach the target odds/number of games.",
        "approx_note": "≈ means estimated odds (calculated for 1X, X2, 12). Check with your bookmaker before betting.",
        "extra_note": "ℹ️ Corner/card lines and odds may differ between bookmakers. Check before betting.",
        "stats_note": "ℹ️ Stats come from API-Football data (recent matches, season averages). Not a guarantee.",
        "fb_note": "ℹ️ Stats are temporarily unavailable, this coupon relies on market odds only.",
        "disclaimer": "⚠️ 18+ · No guarantees. Check the odds with your bookmaker. Only stake what you can afford to lose.",
        "left": "🎟 Coupon credits left today: {left}/{limit}",
        "left_last": "⚠️ That was your last coupon credit for today. New credits are given tomorrow.",
        "limit_reached": (
            "🚫 You have used all {limit}/{limit} coupon credits for today.\n\n"
            "New credits are given tomorrow after 00:00 Baku time. 🙌"
        ),
        "no_matches": "😕 No suitable matches are left for today (most have already started). Tomorrow's games load after 00:00 Baku time.",
        "no_matches_stats": "😕 No suitable matches with verified stats found for today. Try again a bit later.",
        "no_tier": "😕 Not enough suitable matches today for the \"{name}\". Pick another type or try again a bit later.",
        "data_error": "⚠️ Match data is unavailable right now. Please try again later.",
        "quota_out": "⚠️ Today's match data limit has been reached. Please try again tomorrow.",
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
        "p_corners_over": "Corners Over {line}",
        "p_corners_under": "Corners Under {line}",
        "p_cards_over": "Cards Over {line}",
        "p_cards_under": "Cards Under {line}",
        "s_form": "📊 Form (last 5): {h} {fh} · {a} {fa}",
        "s_model": "🎯 Model 1/X/2: {ph}/{pd}/{pa}%",
        "s_cor": "📊 Expected corners (last {n}): {h} ~{eh} · {a} ~{ea} · total ~{e}",
        "s_crd": "📊 Expected cards (last {n}): {h} ~{eh} · {a} ~{ea} · total ~{e}",
        "s_goals": "📊 Expected goals (season avg): {h} ~{eh} · {a} ~{ea} · total ~{e}",
        "n_1x2": "📊 Market: 1 → {h}% · X → {d}% · 2 → {a}%",
        "n_corners_over": "💡 The market expects more than {line} corners here (~{p}%)",
        "n_corners_under": "💡 The market expects fewer than {line} corners here (~{p}%)",
        "n_cards_over": "💡 The market expects more than {line} cards here (~{p}%)",
        "n_cards_under": "💡 The market expects fewer than {line} cards here (~{p}%)",
        "n_over": "💡 The market expects more than {line} goals (~{p}%)",
        "n_under": "💡 The market expects fewer than {line} goals (~{p}%)",
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
        "a_snap": "⚽ In database: {m} matches · {l} leagues · Odds API spent {s} · remaining {r}",
        "a_snap_x": "🎯 Matches with corner/card lines: {x}",
        "a_stats": "📈 Stats: {v}/{m} matches verified · API-Football calls {c} · remaining {r}{err}",
        "a_stats_fb": "⚠️ Stats are not being used right now (too few verified matches) — coupons rely on market odds only.",
        "a_stats_off": "📈 Stats disabled (no API_FOOTBALL_KEY) — coupons rely on market odds only.",
        "a_limit": "🎟 User limit: {n} coupons per day",
        "a_partial": "⚠️ Match data is incomplete (attempt {t}/{m}, failed requests {f}) — the bot will retry by itself. Refresh now: /yenile",
        "r_wait": "⏳ Refreshing match data...",
        "r_done": "🔄 Refreshed: {m} matches · {l} leagues with games · failed requests {f} · Odds API remaining {r}\n\n{lg}",
        "a_credits": "💳 Odds API spent today: main {o}/{ob} · corners/cards {x}/{xb}",
        "a_snap_none": "⚽ Match database not loaded yet.",
        "a_none": "none yet",
        "stat_title": "📊 Last {n} days — hit rate (for transparency)",
        "stat_row": "{icon} {name}: {pct}% ({settled} coupons)",
        "stat_empty": "📊 Statistics are still being collected — not enough settled coupons yet. Check back in a few days.",
        "stat_footer": "\n⚠️ Past results don't guarantee future results. 18+ · Bet responsibly.",
        "my_title": "🎫 Your coupon history, last {n} days: {total} coupons",
        "my_row": "✅ {w} hit · ❌ {l} missed · ⏳ {p} pending",
        "my_empty": "You don't have any coupon history yet — get your first with /coupon.",
        "s_win": "hit",
        "s_loss": "missed",
        "s_pending": "pending",
    },
}
LANG_BUTTONS = [("az", "🇦🇿 Azərbaycanca"), ("en", "🇬🇧 English")]
COMMANDS = {
    "az": [("start", "Başla"), ("gununoyunlari", "Günün kuponu"), ("kuponumabax", "Öz kuponunu yoxla"),
           ("kuponlarim", "Kupon tarixçəm"),
           ("statistika", "Tutma statistikası"), ("lang", "Dil / Language"),
           ("about", "Bot haqqında"), ("privacy", "Məxfilik")],
    "en": [("start", "Start"), ("coupon", "Today's coupon"), ("kuponumabax", "Check my coupon"),
           ("mycoupons", "My coupon history"),
           ("statistika", "Hit-rate stats"), ("lang", "Language"),
           ("about", "About the bot"), ("privacy", "Privacy")],
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


def _kvj_get(key):
    """JSON dəyəri oxu; xəta və ya boşdursa None."""
    try:
        raw = kv.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _kvj_set(key, obj, ex=None):
    try:
        kv.set(key, json.dumps(obj), ex=ex)
    except Exception:
        log.warning("kv yazılmadı: %s", key)


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

    def used_today(self, uid):
        """İstifadəçinin bu gün (Bakı vaxtı) aldığı kupon sayı = istifadə etdiyi kredit."""
        return int(self.kv.hget(f"d:{self._day()}", uid) or 0)

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
    kind: str          # home/away/draw/dc1x/dcx2/dc12/over/under/corners_*/cards_*
    odds: float
    prob: float        # marjası çıxarılmış bazar ehtimalı
    approx: bool = False
    line: object = None  # korner/kart/qol xətti (məs. 9.5)


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
    p3: object = None    # (ev, heç-heçə, qonaq) bazar ehtimalları
    info: object = None  # statistika (dict) və ya None


@dataclass
class Snapshot:
    day: str
    matches: list
    spent: int
    remaining: object
    leagues: int
    ok: bool
    extras: bool = False       # Odds API korner/kart mərhələsi icra olunub
    stats_done: bool = False   # API-Football statistika mərhələsi tamamlanıb
    fb_calls: int = 0          # bu gün API-Football sorğu sayı (kumulyativ)
    fb_left: object = None     # API-Football qalan gündəlik sorğu
    fb_err: str = ""           # "plan" / "quota" / "auth" / "budget" və ya boş
    partial: bool = False      # bəzi sorğular uğursuz olub / oyun tapılmayıb → bir az sonra yenidən yoxlanacaq
    tries: int = 0             # bazanın neçə dəfə yüklənməsi cəhdi
    fails: int = 0             # son yükləmədə uğursuz Odds API sorğularının sayı

    def to_json(self):
        return json.dumps(dict(
            day=self.day, spent=self.spent, remaining=self.remaining,
            leagues=self.leagues, ok=self.ok, extras=self.extras,
            stats_done=self.stats_done, fb_calls=self.fb_calls,
            fb_left=self.fb_left, fb_err=self.fb_err, partial=self.partial, tries=self.tries, fails=self.fails,
            matches=[dict(id=m.id, home=m.home, away=m.away, start=m.start.isoformat(),
                          league_key=m.league_key, league=m.league, top=m.top,
                          p3=list(m.p3) if m.p3 else None, info=m.info,
                          legs=[[l.kind, l.odds, l.prob, l.approx, l.line] for l in m.legs])
                     for m in self.matches],
        ))

    @staticmethod
    def from_json(raw):
        d = json.loads(raw)
        matches = [Match(id=m["id"], home=m["home"], away=m["away"],
                         start=datetime.fromisoformat(m["start"]).astimezone(TZ),
                         league_key=m["league_key"], league=m["league"], top=m["top"],
                         legs=[Leg(*l) for l in m["legs"]],
                         p3=tuple(m["p3"]) if m.get("p3") else None,
                         info=m.get("info"))
                   for m in d["matches"]]
        return Snapshot(d["day"], matches, d["spent"], d["remaining"], d["leagues"], d["ok"],
                        d.get("extras", False), d.get("stats_done", False),
                        d.get("fb_calls", 0), d.get("fb_left"), d.get("fb_err", ""),
                        d.get("partial", False), d.get("tries", 0), d.get("fails", 0))


def _avg(values):
    return sum(values) / len(values)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _verified(m):
    return bool(m.info and m.info.get("ok"))


def stats_usable(matches, now=None):
    """
    Statistika rejimi real işləyirmi? Yalnız bu günün oyunlarında kifayət qədər təsdiqlənmiş
    statistika varsa (≥8 və ya oyunların yarısı) istifadə olunur. Əks halda bot bazar rejiminə düşür.
    """
    if not STATS_MODE and not FD_ORG_MODE:
        return False
    now = now or datetime.now(TZ)
    today = [m for m in matches if m.start.date() == now.date()]
    v = sum(1 for m in today if _verified(m))
    return v >= MIN_VERIFIED or (v >= 3 and v * 2 >= len(today))


def make_legs(o_home, o_draw, o_away, over=None, under=None):
    """
    h2h kefindən: 1 / 2, təxmini ikiqat şans (1X, X2, 12) və 2.5 Üst/Alt variantları.
    Təkcə 'X' (heç-heçə) pick kimi verilmir.
    """
    inv = {"home": 1 / o_home, "draw": 1 / o_draw, "away": 1 / o_away}
    s = sum(inv.values())
    p = {k: v / s for k, v in inv.items()}
    odds = {"home": o_home, "away": o_away}
    legs = [Leg(k, odds[k], p[k]) for k in ("home", "away")]
    # İkiqat şans: real kef mərc şirkətində fərqli ola bilər, ona görə approx=True
    for kind, (a, b) in {"dc1x": ("home", "draw"), "dcx2": ("away", "draw"),
                         "dc12": ("home", "away")}.items():
        legs.append(Leg(kind, 1 / (inv[a] + inv[b]), p[a] + p[b], approx=True))
    if over and under:
        io, iu = 1 / over, 1 / under
        legs.append(Leg("over", over, io / (io + iu), False, 2.5))
        legs.append(Leg("under", under, iu / (io + iu), False, 2.5))
    return legs


def parse_league_odds(data, key, title, top, now, t_end=None):
    """Bir liqanın Odds API cavabından hələ başlamamış və (t_end verilibsə) həmin vaxtdan əvvəl başlayan oyunları çıxarır."""
    out = []
    for ev in data:
        start = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00")).astimezone(TZ)
        if start <= now or (t_end is not None and start >= t_end):
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
        inv = [1 / o_h, 1 / o_d, 1 / o_a]
        s = sum(inv)
        p3 = tuple(x / s for x in inv)
        out.append(Match(ev["id"], home, away, start, key, title, top,
                         make_legs(o_h, o_d, o_a, over, under), p3))
    return out


def parse_extra_markets(ev):
    """Bir oyunun korner/kart Üst-Alt xətlərini Leg siyahısına çevirir (bazar ehtimalı, marja çıxarılmış)."""
    by = defaultdict(lambda: defaultdict(lambda: {"Over": [], "Under": []}))
    for bk in ev.get("bookmakers", []):
        for mk in bk.get("markets", []):
            key = mk.get("key", "")
            if key == "alternate_totals_corners":
                what = "corners"
            elif key == "alternate_totals_cards":
                what = "cards"
            else:
                continue
            for o in mk.get("outcomes", []):
                pt, side, price = o.get("point"), o.get("name"), o.get("price")
                if pt is None or price is None or side not in ("Over", "Under"):
                    continue
                by[what][float(pt)][side].append(float(price))
    legs = []
    for what, pts in by.items():
        lo, hi = EXTRA_LINES[what]
        for pt, sides in pts.items():
            if not (lo <= pt <= hi) or abs(pt % 1 - 0.5) > 1e-9:   # yalnız x.5 xətləri
                continue
            if not sides["Over"] or not sides["Under"]:
                continue
            o_over, o_under = _avg(sides["Over"]), _avg(sides["Under"])
            if min(o_over, o_under) < 1.01:
                continue
            io, iu = 1 / o_over, 1 / o_under
            legs.append(Leg(f"{what}_over", o_over, io / (io + iu), False, pt))
            legs.append(Leg(f"{what}_under", o_under, iu / (io + iu), False, pt))
    return legs


def credits_used(kind):
    """Bu gün (Bakı) Odds API-da xərclənən kredit. kind: "odds" (əsas) və ya "extra" (korner/kart)."""
    try:
        return int(kv.hget(f"cr:{datetime.now(TZ).date().isoformat()}", kind) or 0)
    except Exception:
        return 0


def credits_add(kind, n):
    try:
        kv.hincrby(f"cr:{datetime.now(TZ).date().isoformat()}", kind, n)
    except Exception:
        log.warning("kredit sayğacı yazılmadı")


def _get(path, **params):
    return requests.get(f"{ODDS_BASE}{path}", params={"apiKey": ODDS_API_KEY, **params}, timeout=20)


def _hdr_int(resp, name):
    try:
        return int(float(resp.headers.get(name)))
    except (TypeError, ValueError):
        return None


def discover_leagues():
    """Aktiv futbol liqaları + qalan kredit. /sports PULSUZDUR (kredit çəkmir)."""
    r = _get("/sports")
    r.raise_for_status()
    leagues = [(s["key"], s.get("title", s["key"])) for s in r.json()
               if s.get("key", "").startswith("soccer_")
               and not s.get("has_outrights") and s.get("active", True)]
    return leagues, _hdr_int(r, "x-requests-remaining")


def league_events(key, t_from, t_to):
    """
    Liqanın pəncərədəki oyunları. /events PULSUZDUR.
    Müvəqqəti xətada (429/5xx/şəbəkə) 3 dəfəyə qədər təkrar cəhd edir. Hələ də alınmırsa None (= xəta).
    404/422 "bu liqada oyun yoxdur" deməkdir → [].
    """
    for attempt in range(3):
        try:
            r = _get(f"/sports/{key}/events", commenceTimeFrom=_iso(t_from), commenceTimeTo=_iso(t_to))
            if r.status_code == 200:
                return r.json()
            if r.status_code in (404, 422):
                return []
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            log.warning("%s | events status=%s", key, r.status_code)
            return None
        except Exception:
            time.sleep(1.0)
    log.warning("%s | events xətası (3 cəhddən sonra)", key)
    return None


def fetch_day(prev=None):
    """
    Günün oyun bazasını qurur (prev: bu günün əvvəlki natamam bazası — artıq alınmış liqalar təkrar alınmır).
    Addımlar:
      1) aktiv liqaları tap (pulsuz)   2) hansında oyun var — yoxla (pulsuz)
      3) yalnız oyunu olan liqalar üçün kef çək (kredit), gündəlik büdcə daxilində.
         Büdcədən EXTRA_LEAGUE_RESERVE qədəri aşağı/əlavə liqalar üçün ayrılır.
    """
    now = datetime.now(TZ)
    t_to = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)   # bu günün sonu (Bakı)
    leagues, remaining = discover_leagues()
    with ThreadPoolExecutor(max_workers=4) as ex:      # az paralel: Odds API sürət limitinə (429) düşməmək üçün
        found = list(ex.map(lambda kt: (kt, league_events(kt[0], now, t_to)), leagues))
    ev_failed = sum(1 for _kt, evs in found if evs is None)
    active = [(k, t, evs) for (k, t), evs in found if evs]
    log.info("Odds API: %d futbol liqası, %d-də bu gün oyun var, %d liqada events sorğusu uğursuz",
             len(leagues), len(active), ev_failed)
    # Əvvəl məşhur liqalar, sonra oyun sayı çox olan əlavə liqalar
    active.sort(key=lambda x: (0, TOP_LEAGUES.index(x[0])) if x[0] in TOP_LEAGUES else (1, -len(x[2])))

    have = {m.league_key for m in prev.matches} if prev else set()
    matches = list(prev.matches) if prev else []
    n_before = len(matches)
    st = {"spent": 0, "remaining": remaining, "fails": 0, "quota_out": False}
    budget = max(0, DAILY_CREDIT_BUDGET - credits_used("odds"))   # restart/təkrar cəhdlərdən asılı olmayaraq gündəlik tavan

    def pull(key, title, top):
        """Bir liqanın kefini çəkir. Kvota bitibsə False qaytarır (dövr dayanmalıdır)."""
        path = f"/sports/{key}/odds"
        base = dict(regions="eu", oddsFormat="decimal", markets="h2h,totals" if top else "h2h")
        try:
            r = _get(path, **base, commenceTimeFrom=_iso(now), commenceTimeTo=_iso(t_to))
            if r.status_code == 422:            # vaxt parametrləri qəbul edilmədisə, onlarsız təkrar
                r = _get(path, **base)
        except Exception:
            log.warning("%s | odds sorğusu uğursuz", key)
            st["fails"] += 1
            return True
        rem = _hdr_int(r, "x-requests-remaining")
        if rem is not None:
            st["remaining"] = rem
        log.info("%s | status=%s | qalan kredit=%s", key, r.status_code, rem)
        if r.status_code == 401:               # kvota bitib
            st["quota_out"] = True
            return False
        if r.status_code != 200:
            st["fails"] += 1
            return True
        cost = _hdr_int(r, "x-requests-last")   # real xərc (oyun qaytarılmayıbsa 0)
        if cost is None:
            cost = 2 if top else 1
        st["spent"] += cost
        credits_add("odds", cost)
        try:
            matches.extend(parse_league_odds(r.json(), key, title, top, now, t_to))
        except Exception:
            log.exception("%s | parse xətası", key)
        return True

    pending = [(k, t) for k, t, _e in active if k not in have]
    has_extra = any(k not in TOP_LEAGUES for k, _t in pending)
    top_budget = budget - EXTRA_LEAGUE_RESERVE if has_extra else budget
    top_budget = max(top_budget, budget // 2)
    skipped, stop = [], False
    for key, title in pending:                          # 1-ci keçid: məşhurlar üçün məhdud, əlavələr üçün tam büdcə
        top = key in TOP_LEAGUES
        cost = 2 if top else 1
        if st["spent"] + cost > (top_budget if top else budget):
            skipped.append((key, title, top))
            continue
        if st["remaining"] is not None and st["remaining"] - cost < CREDIT_RESERVE:
            stop = True
            break
        if not pull(key, title, top):
            stop = True
            break
    if not stop:                                        # 2-ci keçid: qalan kredit atlanmış liqalara
        for key, title, top in skipped:
            cost = 2 if top else 1
            if st["spent"] + cost > budget:
                continue
            if st["remaining"] is not None and st["remaining"] - cost < CREDIT_RESERVE:
                break
            if not pull(key, title, top):
                break

    fails = st["fails"] + ev_failed
    ok = bool(matches) or not st["quota_out"]
    partial = (not matches) or fails > 0 or len(matches) < THIN_SNAPSHOT
    tries = (prev.tries if prev else 0) + 1
    log.info("Baza: %d liqa aktiv, %d oyun, %d kredit xərcləndi, qalan=%s, uğursuz sorğu=%d, natamam=%s (cəhd %d)",
             len(active), len(matches), st["spent"], st["remaining"], fails, partial, tries)
    snap = Snapshot(now.date().isoformat(), matches, (prev.spent if prev else 0) + st["spent"], st["remaining"],
                    len(active), ok, partial=partial, tries=tries, fails=fails)
    if prev:                                            # əvvəlki mərhələlərin nəticəsini itirmə
        snap.extras = prev.extras
        snap.fb_calls, snap.fb_left, snap.fb_err = prev.fb_calls, prev.fb_left, prev.fb_err
        snap.stats_done = prev.stats_done and len(matches) == n_before   # yeni oyun gəlibsə statistika yenidən işləsin
    return snap


def enrich_extras(snap):
    """
    Korner/kart kefləri (oyun-oyun sorğu, KREDİTLİDİR). Gündə yalnız bir dəfə icra olunur.
    Statistika işləyirsə, yalnız statistikası təsdiqlənmiş oyunlara xərclənir (digərləri
    gate-dən keçə bilməz). Bütün bookmaker-lər bu marketləri vermir — tapılmayan oyunlar keçilir.
    """
    if snap.extras:
        return
    snap.extras = True   # nəticədən asılı olmayaraq təkrar kredit xərclənməsin
    budget = max(0, EXTRA_CREDIT_BUDGET - credits_used("extra"))
    if budget < 2:
        return
    now = datetime.now(TZ)
    need_stats = stats_usable(snap.matches, now)
    pool = sorted((m for m in snap.matches
                   if m.top and m.start.date() == now.date() and m.start > now + timedelta(minutes=60)
                   and not any(l.kind in EXTRA_KINDS for l in m.legs)
                   and (not need_stats or _verified(m))),
                  key=lambda m: m.start)
    spent, added, remaining = 0, 0, snap.remaining
    for m in pool:
        if spent + 2 > budget:
            break
        if remaining is not None and remaining - 2 < CREDIT_RESERVE:
            break
        try:
            r = _get(f"/sports/{m.league_key}/events/{m.id}/odds", regions="eu",
                     oddsFormat="decimal", markets=EXTRA_MARKETS)
        except Exception:
            log.warning("%s | extra sorğusu uğursuz", m.id)
            continue
        rem = _hdr_int(r, "x-requests-remaining")
        if rem is not None:
            remaining = rem
        cost = _hdr_int(r, "x-requests-last")
        log.info("extra | %s - %s | status=%s | xərc=%s | qalan=%s", m.home, m.away, r.status_code, cost, rem)
        if r.status_code == 401:
            break
        if r.status_code != 200:
            continue
        cost = 2 if cost is None else cost
        spent += cost
        credits_add("extra", cost)
        try:
            legs = parse_extra_markets(r.json())
        except Exception:
            log.exception("extra parse xətası")
            continue
        if legs:
            m.legs += legs
            added += 1
    snap.spent += spent
    snap.remaining = remaining
    log.info("Extra: %d oyunda korner/kart xətti tapıldı, %d kredit xərcləndi", added, spent)


# ============================== API-FOOTBALL (REAL STATİSTİKA, PULLU) ==============================
class FootballError(Exception):
    pass


class FootballAPI:
    """
    API-Football v3 klienti. Gündəlik sorğu büdcəsi var; kvota/plan/açar xətası olanda dayanır.
    stopped: None | "quota" | "plan" | "auth" | "budget"
    """

    def __init__(self, key, budget):
        self.key, self.budget = key, budget
        self.calls = 0
        self.failed = 0
        self.remaining = None
        self.stopped = None

    def get(self, path, **params):
        for attempt in (1, 2):
            if self.stopped:
                raise FootballError(self.stopped)
            if self.calls >= self.budget:
                self.stopped = "budget"
                raise FootballError("budget")
            time.sleep(0.2)    # dəqiqədə sorğu limitinə hörmət
            self.calls += 1
            try:
                r = requests.get(FOOTBALL_BASE + path, headers={"x-apisports-key": self.key},
                                 params=params, timeout=25)
                rem = r.headers.get("x-ratelimit-requests-remaining")
                if rem is not None:
                    try:
                        self.remaining = int(float(rem))
                    except ValueError:
                        pass
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                self.failed += 1
                raise FootballError(str(e)) from e
            errs = data.get("errors")
            if not errs:
                return data.get("response") or []
            text = (errs if isinstance(errs, str) else json.dumps(errs)).lower()
            log.warning("API-Football | %s | %s", path, text[:200])
            if ("per minute" in text or "too many" in text) and attempt == 1:
                time.sleep(10)          # dəqiqəlik limit — bir dəfə təkrar cəhd
                continue
            if "free plan" in text or ("plan" in text and ("season" in text or "date" in text)):
                self.stopped = "plan"
            elif "token" in text or "application key" in text or "api key" in text:
                self.stopped = "auth"
            elif "request" in text and ("limit" in text or "reached" in text):
                self.stopped = "quota"
            if self.stopped:
                raise FootballError(self.stopped)
            self.failed += 1
            raise FootballError(text[:200])
        raise FootballError("retry")


_STOP_TOKENS = {"fc", "cf", "afc", "sc", "cd", "ud", "sv", "fk", "sk", "bsc", "rc", "the", "de", "of", "club"}
_ALIASES = {"utd": "united", "man": "manchester", "st": "saint"}
# tam ad ləqəbləri (fərqli mənbələrdə tamamilə fərqli yazılan komandalar)
_NAME_ALIASES = {
    "wolves": "wolverhampton wanderers", "spurs": "tottenham hotspur", "tottenham": "tottenham hotspur",
    "psg": "paris saint germain", "nottm forest": "nottingham forest", "inter": "inter milan",
    "atletico": "atletico madrid", "sporting": "sporting cp", "sporting lisbon": "sporting cp",
}


def _tokens(name):
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    words = re.findall(r"[a-z0-9]+", s)
    words = _NAME_ALIASES.get(" ".join(words), " ".join(words)).split()
    toks = [_ALIASES.get(t, t) for t in words]
    keep = [t for t in toks if t not in _STOP_TOKENS]
    return keep or toks


def name_score(a, b):
    """İki komanda adının oxşarlığı (0..1). Fərqli yazılışlara (Man Utd / Manchester United) davamlıdır."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    whole = difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    tok = _avg([max(difflib.SequenceMatcher(None, s, t).ratio() for t in big) for s in small])
    return max(whole, tok)


def find_fixture(m, fixtures, used):
    """Odds API oyununu API-Football oyunu ilə eyniləşdirir: vaxt (±45 dəq.) + hər iki komanda adı."""
    best, best_sc = None, 0.0
    ts = m.start.timestamp()
    for f in fixtures:
        fx = f.get("fixture") or {}
        fid = fx.get("id")
        if fid is None or fid in used:
            continue
        if (fx.get("status") or {}).get("short") not in ("NS", "TBD"):
            continue
        if abs((fx.get("timestamp") or 0) - ts) > 45 * 60:
            continue
        teams = f.get("teams") or {}
        sh = name_score(m.home, (teams.get("home") or {}).get("name", ""))
        sa = name_score(m.away, (teams.get("away") or {}).get("name", ""))
        if min(sh, sa) < 0.85 or sh + sa < 1.8:      # səhv oyunu eyniləşdirməkdənsə, buraxmaq yaxşıdır
            continue
        if sh + sa > best_sc:
            best, best_sc = f, sh + sa
    return best


def _flt(x):
    try:
        return float(str(x).replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def parse_prediction(resp, hid, aid):
    """/predictions cavabından model ehtimalı, forma, orta qol və H2H çıxarır. Çatışmazlıq varsa None."""
    if not resp:
        return None
    r = resp[0]
    pc = (r.get("predictions") or {}).get("percent") or {}
    p = [_flt(pc.get("home")), _flt(pc.get("draw")), _flt(pc.get("away"))]
    if None in p or sum(p) <= 0:
        return None
    tot = sum(p)
    pct = [x / tot for x in p]
    teams = r.get("teams") or {}

    def side(key, venue):
        lg = (teams.get(key) or {}).get("league") or {}
        form = "".join(ch for ch in (lg.get("form") or "") if ch in "WDL")[-5:]
        goals = lg.get("goals") or {}

        def avg(kind):
            d = (goals.get(kind) or {}).get("average") or {}
            v = _flt(d.get(venue))
            return v if v is not None else _flt(d.get("total"))
        return form, avg("for"), avg("against")

    fh, hf, ha = side("home", "home")
    fa, af, aa = side("away", "away")
    g = [hf, ha, af, aa]
    if None in g:
        return None
    hw = dr = aw = n = 0
    for f in (r.get("h2h") or []):
        gl = f.get("goals") or {}
        gh, ga = gl.get("home"), gl.get("away")
        tid = ((f.get("teams") or {}).get("home") or {}).get("id")
        if gh is None or ga is None:
            continue
        if tid == hid:
            mine, opp = gh, ga
        elif tid == aid:
            mine, opp = ga, gh
        else:
            continue
        n += 1
        if mine > opp:
            hw += 1
        elif mine == opp:
            dr += 1
        else:
            aw += 1
        if n >= 5:
            break
    return {"ok": True, "pct": pct, "form": [fh, fa], "g": g, "h2h": [hw, dr, aw, n],
            "cor": None, "crd": None}


def parse_fixture_stats(item, fid=None):
    """Bitmiş oyunun statistikasından korner və kart sayları. Statistika yoxdursa None."""
    teams = item.get("teams") or {}
    hid = (teams.get("home") or {}).get("id")
    aid = (teams.get("away") or {}).get("id")
    vals = {}
    for blk in item.get("statistics") or []:
        tid = (blk.get("team") or {}).get("id")
        vals[tid] = {s.get("type"): s.get("value") for s in blk.get("statistics") or []}
    if hid not in vals or aid not in vals:
        return None
    hc, ac = vals[hid].get("Corner Kicks"), vals[aid].get("Corner Kicks")
    if hc is None or ac is None:
        return None

    def cards(d):
        return int(_flt(d.get("Yellow Cards")) or 0) + int(_flt(d.get("Red Cards")) or 0)
    return {"id": fid if fid is not None else (item.get("fixture") or {}).get("id"),
            "h": hid, "a": aid, "hc": int(_flt(hc) or 0), "ac": int(_flt(ac) or 0),
            "hk": cards(vals[hid]), "ak": cards(vals[aid])}


def team_profile(api, team_id, day):
    """
    Komandanın son PROFILE_GAMES bitmiş oyununda orta korner/kart (qazandığı/buraxdığı).
    Sorğu: /fixtures?team=ID&last=8 (mövsüm parametri lazım deyil). Oyun statistikası Redis-də
    uzun müddət keşlənir (bitmiş oyun dəyişmir).
    """
    key = f"tp:{team_id}:{day}"
    cached = _kvj_get(key)
    if cached is not None:
        return cached if cached.get("n") else None
    fx = api.get("/fixtures", team=team_id, last=8)
    fin = [f for f in fx if ((f.get("fixture") or {}).get("status") or {}).get("short") in FINISHED]
    fin.sort(key=lambda f: (f.get("fixture") or {}).get("timestamp") or 0, reverse=True)
    fin = fin[:PROFILE_GAMES]
    rows, need = [], []
    for f in fin:
        c = _kvj_get(f"fs:{f['fixture']['id']}")
        if c is not None:
            rows.append(c)
        else:
            need.append(f)
    if need:
        got = {}
        try:
            det = api.get("/fixtures", ids="-".join(str(f["fixture"]["id"]) for f in need))
        except FootballError:
            if api.stopped:
                raise
            det = []
        for it in det:
            s = parse_fixture_stats(it)
            if s and s.get("id") is not None:
                got[s["id"]] = s
        for f in need:
            fid = f["fixture"]["id"]
            s = got.get(fid)
            if s is None and not api.stopped:      # ehtiyat yol: oyun-oyun statistika
                try:
                    resp = api.get("/fixtures/statistics", fixture=fid)
                    s = parse_fixture_stats({"teams": f.get("teams"), "statistics": resp}, fid)
                except FootballError:
                    s = None
            if s:
                _kvj_set(f"fs:{fid}", s, ex=60 * 86400)
                rows.append(s)
            elif not api.stopped:
                _kvj_set(f"fs:{fid}", {"none": True}, ex=7 * 86400)   # statistikası olmayan oyunu təkrar çəkmə
    n = cf = ca = kf = ka = 0
    for s in rows:
        if s.get("none"):
            continue
        if s.get("h") == team_id:
            a, b, c, d = s["hc"], s["ac"], s["hk"], s["ak"]
        elif s.get("a") == team_id:
            a, b, c, d = s["ac"], s["hc"], s["ak"], s["hk"]
        else:
            continue
        n += 1
        cf, ca, kf, ka = cf + a, ca + b, kf + c, ka + d
    prof = {"n": n, "cf": cf / n, "ca": ca / n, "kf": kf / n, "ka": ka / n} if n else {"n": 0}
    _kvj_set(key, prof, ex=20 * 3600)
    return prof if n else None


def build_info(api, fx, day):
    """Bir oyun üçün tam statistika paketi: model + forma + orta qol + H2H + korner/kart profili."""
    fid = fx["fixture"]["id"]
    hid = fx["teams"]["home"]["id"]
    aid = fx["teams"]["away"]["id"]
    info = parse_prediction(api.get("/predictions", fixture=fid), hid, aid)
    if not info:
        return None
    try:
        ph = team_profile(api, hid, day)
        pa = team_profile(api, aid, day)
    except FootballError:
        if api.stopped:
            raise
        ph = pa = None
    if ph and pa:
        info["cor"] = [ph["cf"], ph["ca"], pa["cf"], pa["ca"], ph["n"], pa["n"]]
        info["crd"] = [ph["kf"], ph["ka"], pa["kf"], pa["ka"], ph["n"], pa["n"]]
    return info


def enrich_stats(snap):
    """
    Oyunlara API-Football statistikası əlavə edir (nəticə Redis-də saxlanır). Gündəlik sorğu büdcəsi
    kumulyativdir (snap.fb_calls). Eyniləşdirilə bilməyən və ya statistikası tapılmayan oyunlar
    info=None qalır; kifayət qədər təsdiqlənmiş oyun yoxdursa bot bazar rejiminə keçir.
    """
    if not STATS_MODE:
        snap.stats_done = True
        return
    budget = max(0, FOOTBALL_DAILY_BUDGET - snap.fb_calls)
    if budget < 5:
        snap.fb_err = snap.fb_err or "budget"
        snap.stats_done = True
        return
    api = FootballAPI(FOOTBALL_KEY, budget)
    day = datetime.now(TZ).date().isoformat()
    cand = sorted((m for m in snap.matches
                   if m.start.date() == datetime.now(TZ).date() and not _verified(m)),
                  key=lambda m: (not m.top, m.start))[:STATS_MAX_MATCHES]
    dates = sorted({m.start.date().isoformat() for m in cand})
    fixtures = []
    for d in dates:
        try:
            fixtures += api.get("/fixtures", date=d, timezone="Asia/Baku")
        except FootballError:
            break
    used, verified, unmatched = set(), 0, 0
    for m in cand:
        if api.stopped:
            break
        fx = find_fixture(m, fixtures, used)
        if not fx:
            unmatched += 1
            continue
        try:
            info = build_info(api, fx, day)
        except FootballError:
            continue
        if info:
            m.info = info
            used.add(fx["fixture"]["id"])
            verified += 1
    total_ok = sum(1 for m in snap.matches if _verified(m))
    snap.fb_calls += api.calls
    snap.fb_left = api.remaining if api.remaining is not None else snap.fb_left
    snap.fb_err = api.stopped or ""
    snap.stats_done = (total_ok > 0 or api.stopped in ("plan", "auth")
                       or (api.failed == 0 and api.stopped is None))
    log.info("Statistika: %d/%d oyun təsdiqləndi (bu dəfə +%d, eyniləşməyən %d), %d sorğu, qalan=%s, xəta=%s",
             total_ok, len(snap.matches), verified, unmatched, api.calls, api.remaining, api.stopped)


# ============================== FOOTBALL-DATA.ORG (PULSUZ STATİSTİKA) ==============================
# Bu bölmə API-Football-ı əvəz edir: pulsuz plan, 12 əsas liqa, "hazır proqnoz" yoxdur —
# özümüz Poisson (hücum/müdafiə gücü) modeli ilə 1X2 ehtimalını hesablayırıq.
# Korner/kart bu mənbədən gəlmir (bunlar hələ də Odds API bazarından, enrich_extras() vasitəsilə gəlir).
FOOTBALL_ORG_KEY = env("FOOTBALL_DATA_ORG_KEY", "34670f28fa5543728e78c6f683e19009")
FD_ORG_MODE = bool(FOOTBALL_ORG_KEY)
FD_ORG_BASE = "https://api.football-data.org/v4"
FD_ORG_RATE_LIMIT = 10          # sorğu/dəqiqə (pulsuz plan) — avtomatik gözləyərək bu limitə hörmət edilir
FD_ORG_MIN_GAMES = 4            # komandanın statistikası üçün minimum tapılan (ev/qonaq) oyun
FD_ORG_LOOKBACK_DAYS = 75       # liqa formasını hesablamaq üçün geriyə neçə gün baxılsın
FD_ORG_MAX_MATCHES = STATS_MAX_MATCHES

# Odds API liqa açarı → football-data.org competition kodu (YALNIZ pulsuz planda olan liqalar).
# Diqqət: Türkiyə Süper Liqi və Europa League pulsuz planda YOXDUR — bunlar üçün bot avtomatik
# yalnız bazar kefinə (Odds API) əsaslanmağa davam edəcək.
FD_ORG_COMPETITIONS = {
    "soccer_uefa_champs_league": "CL",
    "soccer_epl": "PL",
    "soccer_spain_la_liga": "PD",
    "soccer_italy_serie_a": "SA",
    "soccer_germany_bundesliga": "BL1",
    "soccer_france_ligue_one": "FL1",
    "soccer_netherlands_eredivisie": "DED",
    "soccer_portugal_primeira_liga": "PPL",
}

# /footballorg admin əmri üçün son vəziyyət (proses yaddaşında saxlanır, restartda sıfırlanır)
_fd_state = {
    "last_run": None,    # datetime (Bakı) və ya None (hələ heç çağırılmayıb)
    "ok": False,
    "error": "",         # "" | "auth" | "quota" | "network" | başqa mətn
    "verified": 0,       # son işə düşmədə təsdiqlənən oyun
    "checked": 0,        # son işə düşmədə yoxlanılan oyun
    "competitions": 0,   # son işə düşmədə yoxlanılan liqa sayı
}


class FootballOrgError(Exception):
    pass


class FootballOrgAPI:
    """
    football-data.org v4 klienti. Pulsuz plan: 10 sorğu/dəqiqə — burda avtomatik "throttle"
    (lazım olanda gözləyir) ilə həll olunur, əl ilə idarə etməyə ehtiyac yoxdur.
    """

    def __init__(self, key):
        self.key = key
        self.calls = 0
        self._times = []
        self.stopped = ""   # "" | "auth" | "quota" | "network"

    def _throttle(self):
        now = time.time()
        self._times = [t for t in self._times if now - t < 60]
        if len(self._times) >= FD_ORG_RATE_LIMIT:
            wait = 60 - (now - self._times[0]) + 0.5
            if wait > 0:
                time.sleep(wait)
        self._times.append(time.time())

    def get(self, path, **params):
        if self.stopped:
            raise FootballOrgError(self.stopped)
        self._throttle()
        self.calls += 1
        try:
            r = requests.get(FD_ORG_BASE + path, headers={"X-Auth-Token": self.key},
                             params=params, timeout=20)
        except Exception as e:
            self.stopped = "network"
            raise FootballOrgError(str(e)) from e
        if r.status_code == 429:              # dəqiqəlik limit — bir dəfə gözləyib təkrar cəhd
            time.sleep(15)
            try:
                r = requests.get(FD_ORG_BASE + path, headers={"X-Auth-Token": self.key},
                                 params=params, timeout=20)
            except Exception as e:
                self.stopped = "network"
                raise FootballOrgError(str(e)) from e
        if r.status_code in (401, 403):
            self.stopped = "auth"
            raise FootballOrgError("auth")
        if r.status_code == 429:
            self.stopped = "quota"
            raise FootballOrgError("quota")
        if r.status_code != 200:
            raise FootballOrgError(f"http {r.status_code}")
        return r.json()


def _fd_league_matches(api, comp_code, day):
    """Liqanın son ~75 günlük bitmiş matçları. Gündə 1 dəfə çəkilir, Redis-də keşlənir."""
    key = f"fdorg:matches:{comp_code}:{day}"
    cached = _kvj_get(key)
    if cached is not None:
        return cached
    date_from = (datetime.now(TZ).date() - timedelta(days=FD_ORG_LOOKBACK_DAYS)).isoformat()
    date_to = datetime.now(TZ).date().isoformat()
    data = api.get(f"/competitions/{comp_code}/matches", status="FINISHED",
                   dateFrom=date_from, dateTo=date_to)
    matches = data.get("matches", [])
    _kvj_set(key, matches, ex=12 * 3600)
    return matches


def _fd_team_appearances(matches, team_name, side, limit=PROFILE_GAMES):
    """Komandanın (ad üzrə uyğunlaşdırılmış) son `limit` evdə/çöldə oyunundakı (vurduğu, buraxdığı) qol cütləri."""
    rows = []
    for mt in sorted(matches, key=lambda x: x.get("utcDate", ""), reverse=True):
        score = (mt.get("score") or {}).get("fullTime") or {}
        hg, ag = score.get("home"), score.get("away")
        if hg is None or ag is None:
            continue
        home_name = (mt.get("homeTeam") or {}).get("name") or ""
        away_name = (mt.get("awayTeam") or {}).get("name") or ""
        if side == "home" and name_score(team_name, home_name) >= 0.85:
            rows.append((hg, ag))
        elif side == "away" and name_score(team_name, away_name) >= 0.85:
            rows.append((ag, hg))
        if len(rows) >= limit:
            break
    return rows


def _fd_league_averages(matches):
    """Liqanın ev/qonaq orta qol ortalaması (Poisson modeli üçün baza xətt)."""
    hs = as_ = n = 0
    for mt in matches:
        score = (mt.get("score") or {}).get("fullTime") or {}
        hg, ag = score.get("home"), score.get("away")
        if hg is None or ag is None:
            continue
        hs += hg
        as_ += ag
        n += 1
    if n < 10:
        return 1.5, 1.2   # kifayət qədər data yoxdursa Avropa liqalarının tipik ortalaması
    return hs / n, as_ / n


def _poisson(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def _fd_match_probs(exp_home, exp_away, max_goals=6):
    """Gözlənilən qollardan (Poisson) 1X2 ehtimalını hesablayır."""
    ph = [_poisson(i, exp_home) for i in range(max_goals + 1)]
    pa = [_poisson(i, exp_away) for i in range(max_goals + 1)]
    p_home = p_draw = p_away = 0.0
    for i in range(max_goals + 1):
        for j in range(max_goals + 1):
            p = ph[i] * pa[j]
            if i > j:
                p_home += p
            elif i == j:
                p_draw += p
            else:
                p_away += p
    tot = p_home + p_draw + p_away
    if tot <= 0:
        return 1 / 3, 1 / 3, 1 / 3
    return p_home / tot, p_draw / tot, p_away / tot


def build_fd_info(m, matches, avg_h, avg_a):
    """
    Bir Odds API matçı üçün football-data.org xammalından statistika paketi qurur.
    Format API-Football-un parse_prediction() nəticəsi ilə eynidir ki, stat_gate()/note_text()
    heç bir dəyişiklik olmadan işləsin. cor/crd=None (bu mənbədə korner/kart yoxdur).
    """
    h_home = _fd_team_appearances(matches, m.home, "home")
    a_away = _fd_team_appearances(matches, m.away, "away")
    if len(h_home) < FD_ORG_MIN_GAMES or len(a_away) < FD_ORG_MIN_GAMES:
        return None
    h_scored, h_conceded = _avg([g for g, _ in h_home]), _avg([c for _, c in h_home])
    a_scored, a_conceded = _avg([g for g, _ in a_away]), _avg([c for _, c in a_away])
    attack_h = h_scored / avg_h if avg_h else 1.0
    def_h = h_conceded / avg_a if avg_a else 1.0
    attack_a = a_scored / avg_a if avg_a else 1.0
    def_a = a_conceded / avg_h if avg_h else 1.0
    exp_home = attack_h * def_a * avg_h
    exp_away = attack_a * def_h * avg_a
    ph, pd, pa = _fd_match_probs(exp_home, exp_away)

    def form_str(rows):
        s = "".join("W" if g > c else ("D" if g == c else "L") for g, c in rows[:5])
        return s[::-1]   # ən köhnədən ən yeniyə doğru (s_form/note_text bu sıra ilə göstərir)

    return {"ok": True, "pct": [ph, pd, pa], "form": [form_str(h_home), form_str(a_away)],
            "g": [h_scored, h_conceded, a_scored, a_conceded], "h2h": [0, 0, 0, 0],
            "cor": None, "crd": None}


def enrich_stats_fdorg(snap):
    """
    football-data.org əsasında pulsuz statistika. Yalnız FD_ORG_COMPETITIONS-dəki liqalarda və
    hələ təsdiqlənməmiş oyunlarda işləyir. Nəticə _fd_state-ə yazılır ki, /footballorg admin
    əmri ilə vəziyyət yoxlana bilsin.
    """
    if not FD_ORG_MODE:
        return
    day = datetime.now(TZ).date().isoformat()
    now = datetime.now(TZ)
    api = FootballOrgAPI(FOOTBALL_ORG_KEY)
    cand = [m for m in snap.matches
            if m.start.date() == now.date() and not _verified(m)
            and m.league_key in FD_ORG_COMPETITIONS][:FD_ORG_MAX_MATCHES]
    comps = sorted({m.league_key for m in cand})
    verified = checked = 0
    error = ""
    for league_key in comps:
        code = FD_ORG_COMPETITIONS[league_key]
        try:
            league_matches = _fd_league_matches(api, code, day)
        except FootballOrgError as e:
            error = str(e) or "network"
            if api.stopped:
                break
            continue
        avg_h, avg_a = _fd_league_averages(league_matches)
        for m in (x for x in cand if x.league_key == league_key):
            checked += 1
            info = build_fd_info(m, league_matches, avg_h, avg_a)
            if info:
                m.info = info
                verified += 1
    _fd_state.update(last_run=now, ok=(verified > 0 or (error == "" and checked == 0)),
                     error=error, verified=verified, checked=checked, competitions=len(comps))
    log.info("football-data.org: %d/%d oyun təsdiqləndi, %d liqa yoxlanıldı, %d sorğu, xəta=%s",
             verified, checked, len(comps), api.calls, error or "-")


# ============================== BAZA İDARƏSİ (KEŞ + FON) ==============================
_snap = {"day": None, "t": 0.0, "st": 0.0, "data": None, "ok": False}
_snap_lock = threading.Lock()
_state = {"building": False, "warming": False}


class _Building:
    """Ağır iş (yükləmə/statistika) gedərkən bayraq qaldırır: istifadəçilər gözləmir, 'hazırlanır' görür."""

    def __enter__(self):
        _state["building"] = True

    def __exit__(self, *exc):
        _state["building"] = False


def _finish_snapshot(today, data):
    """Statistika mərhələsi (əvvəl football-data.org — pulsuz, sonra API-Football əgər açardırsa),
    sonra korner/kart mərhələsi, sonra Redis-ə yazır."""
    if FD_ORG_MODE:
        try:
            enrich_stats_fdorg(data)
        except Exception:
            log.exception("enrich_stats_fdorg xətası")
    if STATS_MODE and not data.stats_done:
        try:
            enrich_stats(data)
        except Exception:
            log.exception("enrich_stats xətası")
    try:
        enrich_extras(data)
    except Exception:
        log.exception("enrich_extras xətası")
        data.extras = True
    try:
        kv.set(f"snap:{today}", data.to_json(), ex=3 * 86400)
    except Exception:
        log.exception("Redis snapshot yazılmadı")


def force_refresh():
    """Admin: bu günün bazasını dərhal yenidən yüklə. Artıq alınmış liqalar təkrar alınmır, gündəlik kredit tavanı qüvvədədir."""
    today = datetime.now(TZ).date().isoformat()
    with _snap_lock, _Building():
        s = _snap
        prev = s["data"] if s["day"] == today else None
        data = fetch_day(prev)
        data.tries = 1
        s.update(day=today, t=time.time(), st=time.time(), data=data, ok=data.ok)
        if data.ok:
            _finish_snapshot(today, data)
        return data


def _needs_refetch(s):
    """Uğursuz və ya natamam baza fasilə ilə (10/20/30 dəq.) yenidən yüklənir (ən çox MAX_FETCH_TRIES dəfə)."""
    d = s["data"]
    age = time.time() - s["t"]
    if not s["ok"]:
        return age >= RETRY_AFTER_FAIL
    if d.partial and d.tries < MAX_FETCH_TRIES:
        return age >= RETRY_AFTER_FAIL * min(max(d.tries, 1), 3)
    return False


def get_snapshot():
    """
    Günlük baza. Yalnız FON dövrü (refresh_loop/warm_up) və admin çağırır — istifadəçilər gözləmir.
    Redis varsa restartdan sonra kredit xərcləmədən oradan oxuyur.
    """
    today = datetime.now(TZ).date().isoformat()
    with _snap_lock:
        s = _snap
        if s["day"] == today and s["data"] is not None and not _needs_refetch(s):
            d = s["data"]
            if s["ok"] and STATS_MODE and not d.stats_done and time.time() - s["st"] > STATS_RETRY:
                s["st"] = time.time()          # statistika yarımçıq qalıbsa, arabir təkrar cəhd
                with _Building():
                    _finish_snapshot(today, d)
            return s["data"], s["ok"]
        if s["day"] != today:
            s["data"] = None
            try:
                raw = kv.get(f"snap:{today}")
                if raw:
                    data = Snapshot.from_json(raw)
                    if len(data.matches) < THIN_SNAPSHOT and data.tries < MAX_FETCH_TRIES:
                        data.partial = True            # nazik/köhnə baza → aşağıda dərhal yenidən yoxlanır
                    s.update(day=today, t=time.time(), data=data, ok=True)
                    if not data.extras or (STATS_MODE and not data.stats_done):
                        s["st"] = time.time()
                        with _Building():
                            _finish_snapshot(today, data)
                    log.info("Baza Redis-dən yükləndi (%d oyun, natamam=%s)", len(data.matches), data.partial)
                    if not data.partial or data.tries >= MAX_FETCH_TRIES:
                        return s["data"], True
            except Exception:
                log.exception("Redis snapshot oxunmadı")
        prev = s["data"] if s["day"] == today else None
        with _Building():
            try:
                data = fetch_day(prev)
            except Exception:
                log.exception("fetch_day xətası")
                data = None
            s["day"], s["t"] = today, time.time()
            if data is not None:
                s["data"], s["ok"] = data, data.ok
                if data.ok:
                    s["st"] = time.time()
                    _finish_snapshot(today, data)
            else:
                # yeniləmə alınmadı: eyni günün köhnə bazası varsa ondan istifadə olunur, 10 dəq. sonra yenidən cəhd
                s["ok"] = prev.ok if prev is not None else False
        return s["data"], s["ok"]


def peek_snapshot():
    """
    İstifadəçi sorğuları üçün: heç vaxt gözləmir, heç nə yükləmir. Qaytarır (data, ok, cəhd_olunub).
    """
    s = _snap
    today = datetime.now(TZ).date().isoformat()
    if s["day"] == today:
        return s["data"], s["ok"], True
    return None, False, False


def warm_up():
    try:
        get_snapshot()
    except Exception:
        log.exception("Warm-up xətası")
    finally:
        _state["warming"] = False


# ============================== STATİSTİKA TƏSDİQİ ==============================
def expect_pair(info, key):
    """Gözlənən (ev, qonaq) korner/kart: (ev_qazandığı + qonaq_buraxdığı)/2 və əksinə. Kifayət qədər oyun yoxdursa None."""
    v = info.get(key)
    if not v or len(v) != 6:
        return None
    hf, ha, af, aa, nh, na = v
    if min(nh, na) < MIN_PROFILE_GAMES:
        return None
    return (hf + aa) / 2, (af + ha) / 2


def expect_goals(info):
    g = info.get("g")
    if not g or len(g) != 4:
        return None
    hf, ha, af, aa = g
    return (hf + aa) / 2, (af + ha) / 2


def stat_gate(m, leg, mode="strict"):
    """
    Pick real statistika ilə üst-üstə düşürmü? Statistika rejimi söndürülübsə həmişə True.
    Statistikası olmayan oyun və ya kifayət qədər məlumat yoxdursa False (yəni pick verilmir).
    mode: "strict" və ya "relaxed" (bax GATES).
    """
    if not STATS_MODE and not FD_ORG_MODE:
        return True
    info = m.info
    if not info or not info.get("ok"):
        return False
    g = GATES[mode]
    k = leg.kind
    pct = info.get("pct")
    form = info.get("form") or ["", ""]
    if k in ("home", "away", "dc1x", "dcx2", "dc12"):
        if not pct:
            return False
        h, d, a = pct
        bad_home = form[0].count("L") >= g["form_losses"]
        bad_away = form[1].count("L") >= g["form_losses"]
        if k == "home":
            return h >= g["win_min"] and h - a >= g["win_gap"] and not bad_home
        if k == "away":
            return a >= g["win_min"] and a - h >= g["win_gap"] and not bad_away
        if k == "dc1x":
            return h + d >= g["dc_min"] and not bad_home
        if k == "dcx2":
            return a + d >= g["dc_min"] and not bad_away
        return h + a >= g["dc_min"]
    if k in ("over", "under"):
        e = expect_goals(info)
        if e is None:
            return False
        tot = e[0] + e[1]
        return tot >= g["goals_over"] if k == "over" else tot <= g["goals_under"]
    if k in EXTRA_KINDS:
        key = "cor" if k.startswith("corners") else "crd"
        e = expect_pair(info, key)
        if e is None:
            # Bu mənbədə (məs. football-data.org) korner/kart statistikası yoxdur — statistika
            # yoxluğuna görə pick-i rədd etmə, Odds API bazar kefinə etibar et.
            return True
        if leg.line is None:
            return False
        tot = e[0] + e[1]
        margin = g["corner_margin"] if key == "cor" else g["card_margin"]
        return tot >= leg.line + margin if k.endswith("over") else tot <= leg.line - margin
    return False


# ============================== KUPONUMABAX (İSTİFADƏÇİNİN ÖZ KUPONUNUN ŞƏKLİ) ==============================
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

COUPON_PROMPT = """Bu bir mərc kuponu skrinşotudur (misli.az "Kupon Detalı" formatına bənzər).
Şəkildəki HƏR sətri oxu. Hər oyun üçün bunları çıxar:
- home: ev komandasının adı (şəkildə yazıldığı kimi, Azərbaycan dilində)
- away: qonaq komandasının adı
- pick: seçilmiş variantın tam yazısı, MƏSƏLƏN "Cüt Şans: 12", "Cüt Şans: 1X", "Cüt Şans: X2",
  "Oyun Nəticəsi 1X2: 1", "Oyun Nəticəsi 1X2: 2", "Oyun Nəticəsi 1X2: X", "Üst 2.5", "Alt 2.5",
  "Korner Üst 9.5", "Kart Alt 4.5" və bənzərləri — şəkildə necə yazılıbsa elə köçür
- odds: həmin sətrin kefi (əmsalı), rəqəm olaraq (məs. 1.29)

Əgər bu, tanınan formatda (idman mərc kuponu, oyun siyahısı + keflər) bir şəkil DEYİLSƏ,
boş massiv [] qaytar.

Cavabı YALNIZ bu JSON formatında ver, başqa HEÇ NƏ yazma (izah, başlıq, ```json işarəsi yox):
[{"home": "...", "away": "...", "pick": "...", "odds": 1.29}, ...]
"""


def call_gemini_vision(image_bytes, mime_type="image/jpeg"):
    """Şəkli Gemini-yə göndərir, pick siyahısını (list[dict]) və ya None (xəta/tanınmadı) qaytarır."""
    if not COUPON_READ_ENABLED:
        return None
    b64 = base64.b64encode(image_bytes).decode()
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": mime_type, "data": b64}},
                {"text": COUPON_PROMPT},
            ]
        }],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
        "safetySettings": [
            {"category": c, "threshold": "BLOCK_NONE"} for c in [
                "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT",
            ]
        ],
    }
    try:
        r = requests.post(GEMINI_URL.format(model=GEMINI_MODEL),
                          params={"key": GEMINI_API_KEY}, json=body, timeout=40)
        r.raise_for_status()
        data = r.json()
    except Exception:
        log.exception("Gemini sorğusu uğursuz")
        return None
    cands = data.get("candidates") or []
    if not cands:
        log.warning("Gemini cavab vermədi: %s", json.dumps(data.get("promptFeedback", {}))[:300])
        return None
    cand = cands[0]
    if cand.get("finishReason") not in (None, "STOP"):
        log.warning("Gemini finishReason=%s (şəkil blok oluna bilər)", cand.get("finishReason"))
        return None
    try:
        text = cand["content"]["parts"][0]["text"]
        picks = json.loads(text)
        return picks if isinstance(picks, list) else None
    except Exception:
        log.exception("Gemini cavabı parse olunmadı: %s", str(cand)[:300])
        return None



# Azərbaycan dilində yazılan komanda/ölkə adlarının Odds API-dakı İngilis adına uyğunlaşdırılması.
# Yalnız ən çox rast gəlinən Avropa millilərini əhatə edir — lazım olduqca əlavə et.
AZ_EN_TEAM_ALIASES = {
    "sloveniya": "slovenia", "şotlandiya": "scotland", "bolqarıstan": "bulgaria",
    "lüksemburq": "luxembourg", "şimali makedoniya": "north macedonia", "isveçrə": "switzerland",
    "çexiya": "czech republic", "xorvatiya": "croatia", "albaniya": "albania", "belarus": "belarus",
    "almaniya": "germany", "fransa": "france", "ispaniya": "spain", "italiya": "italy",
    "portuqaliya": "portugal", "niderland": "netherlands", "hollandiya": "netherlands",
    "belçika": "belgium", "ingiltərə": "england", "türkiyə": "turkey", "rusiya": "russia",
    "ukrayna": "ukraine", "polşa": "poland", "avstriya": "austria", "serbiya": "serbia",
    "yunanıstan": "greece", "rumıniya": "romania", "macarıstan": "hungary", "isveç": "sweden",
    "norveç": "norway", "danimarka": "denmark", "finlandiya": "finland", "islandiya": "iceland",
    "irlandiya": "ireland", "şimali irlandiya": "northern ireland", "uels": "wales",
    "bosniya": "bosnia", "çernoqoriya": "montenegro", "moldova": "moldova", "gürcüstan": "georgia",
    "ermənistan": "armenia", "azərbaycan": "azerbaijan", "qazaxıstan": "kazakhstan",
    "kipr": "cyprus", "malta": "malta", "estoniya": "estonia", "latviya": "latvia",
    "litva": "lithuania", "slovakiya": "slovakia", "san-marino": "san marino", "andorra": "andorra",
    "liyxtenşteyn": "liechtenstein", "monako": "monaco", "kosovo": "kosovo", "izrail": "israel",
    "farer adaları": "faroe islands",
}


def _norm_team_name(name):
    return AZ_EN_TEAM_ALIASES.get((name or "").strip().lower(), name)


def find_match_by_names(matches, home, away):
    """Skrindəki komanda adlarını bugünkü baza ilə uyğunlaşdırır (name_score-dan istifadə edir)."""
    h, a = _norm_team_name(home), _norm_team_name(away)
    best, best_score = None, 0.0
    for m in matches:
        sh = name_score(h, m.home)
        sa = name_score(a, m.away)
        if min(sh, sa) < 0.5:
            continue
        score = sh + sa
        if score > best_score:
            best, best_score = m, score
    return best if best_score >= 1.1 else None


_PICK_LINE_RE = re.compile(r"(\d+(?:[.,]\d+)?)")


def parse_pick_label(pick_text):
    """Skrindəki pick yazısını (məs. 'Cüt Şans: 12') bizim daxili kind-ə çevirir.
    Qaytarır: (kind, line) və ya (None, None) tanınmasa."""
    if not pick_text:
        return None, None
    text = pick_text.strip()
    low = text.lower()
    prefix, _, val = text.rpartition(":")
    prefix_l = (prefix or text).lower()
    val = val.strip().upper() if prefix else text.strip().upper()

    if "cüt" in prefix_l or "çüt" in prefix_l or "şans" in prefix_l:
        return {"12": "dc12", "1X": "dc1x", "X2": "dcx2"}.get(val), None
    if "1x2" in prefix_l or "nəticə" in prefix_l or "netice" in prefix_l:
        return {"1": "home", "2": "away"}.get(val), None   # "X" (heç-heçə) dəstəklənmir
    m = _PICK_LINE_RE.search(text)
    line = float(m.group(1).replace(",", ".")) if m else None
    if "korner" in low or "corner" in low:
        return ("corners_over" if "üst" in low or "ust" in low or "over" in low else "corners_under"), line
    if "kart" in low or "card" in low:
        return ("cards_over" if "üst" in low or "ust" in low or "over" in low else "cards_under"), line
    if "üst" in low or "ust" in low or "over" in low:
        return "over", line
    if "alt" in low or "under" in low:
        return "under", line
    return None, None


def find_leg(m, kind, line=None):
    if not m or not kind:
        return None
    cands = [l for l in m.legs if l.kind == kind]
    if not cands:
        return None
    if line is not None:
        cands.sort(key=lambda l: abs((l.line if l.line is not None else 0) - line))
    return cands[0]


def react_to_pick(m, leg, use_stats):
    """(status, mətn) qaytarır. status: 'good' | 'mid' | 'risky'."""
    if use_stats and _verified(m):
        if stat_gate(m, leg, "strict"):
            return "good", "✅ Bu oyun statistikaya görə böyük ehtimalla gələr."
        if stat_gate(m, leg, "relaxed"):
            return "mid", "🤔 Bu oyun bir qədər risklidir, amma mümkündür."
        return "risky", "⚠️ Bu oyun statistikaya görə riskli görünür."
    if leg.prob >= 0.65:
        return "good", "✅ Bazar kefinə görə bu oyun böyük ehtimalla gələr."
    if leg.prob >= 0.50:
        return "mid", "🤔 Bazar kefinə görə bu oyun orta risklidir."
    return "risky", "⚠️ Bazar kefinə görə bu oyun riskli görünür."


def analyze_coupon_picks(snap, picks):
    """Gemini-dən gələn pick siyahısını analiz edib mətn sətirləri + ümumi rəy qaytarır."""
    use_stats = stats_usable(snap.matches)
    lines, statuses = [], []
    for i, p in enumerate(picks, 1):
        home, away = p.get("home", "?"), p.get("away", "?")
        head = f"{i}. {home} - {away}"
        m = find_match_by_names(snap.matches, home, away)
        kind, line = parse_pick_label(p.get("pick", ""))
        leg = find_leg(m, kind, line) if (m and kind) else None
        if not m or not leg:
            lines.append(f"{head}\n   ❓ Bu oyun/pick üçün məlumatımız yoxdur.")
            statuses.append("no_data")
            continue
        status, text = react_to_pick(m, leg, use_stats)
        lines.append(f"{head}\n   {text}")
        statuses.append(status)

    counted = Counter(s for s in statuses if s != "no_data")
    if not counted:
        verdict = "🤷 Kuponundakı oyunlar barədə kifayət qədər məlumatımız olmadı — dəqiq rəy verə bilmirik."
    elif counted["risky"] > counted["good"]:
        verdict = "🔴 Bu kupon ümumilikdə riskli görünür."
    elif counted["risky"] == 0 and counted["mid"] == 0:
        verdict = "🟢 Bu kupon böyük ehtimalla gələcək kimi görünür."
    else:
        verdict = "🟡 Bu kupon qarışıqdır — bəzi seçimlər yaxşı, bəziləri riskli."
    return "\n\n".join(lines) + "\n\n" + verdict


# ============================== NƏTİCƏ İZLƏMƏ (SETTLEMENT) ==============================
# Hər verilən kupon Redis-də saxlanılır, sonra real nəticələrlə (Odds API-nin PULSUZ
# /scores endpoint-i + korner/kart üçün API-Football) yoxlanılır. Bu, həm admin panelində
# ("bu kuponlardan hansı tutub") istifadə olunur, həm də istifadəçilərə şəffaflıq üçün
# (/statistika, /kuponlarim) göstərilir.
SETTLE_LOOP_EVERY = 30 * 60     # nəticə yoxlama dövrü
SETTLE_BACK_DAYS = 3            # son neçə günün kuponları yoxlanılsın (gecikmiş nəticələr üçün)
SETTLE_FOOTBALL_BUDGET = 60     # korner/kart nəticəsi üçün gündə maksimum API-Football sorğu


def _coupon_id():
    return f"{int(time.time() * 1000)}{random.randint(100, 999)}"


def record_coupon_for_settlement(coupon, uid):
    """Verilən kuponu (tier + hər pick-in event id/kind/line) Redis-ə yazır ki, sonra
    real nəticə ilə yoxlanıla bilsin. Xəta olsa belə istifadəçiyə təsir etməməlidir."""
    day = datetime.now(TZ).date().isoformat()
    cid = _coupon_id()
    payload = {
        "uid": uid,
        "tier": coupon.tier,
        "total": coupon.total,
        "picks": [
            {"event_id": m.id, "sport": m.league_key, "home": m.home, "away": m.away,
             "start": m.start.isoformat(), "kind": leg.kind, "line": leg.line, "odds": leg.odds}
            for m, leg in coupon.picks
        ],
    }
    try:
        kv.hset(f"cp:{day}", cid, json.dumps(payload))
    except Exception:
        log.warning("kupon settlement üçün yazılmadı")


def fetch_scores(sport_key, days_from=3):
    """Odds API-nin PULSUZ /scores endpoint-i: bitmiş oyunların hesabı. Kredit çəkmir."""
    try:
        r = _get(f"/sports/{sport_key}/scores", daysFrom=days_from)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        log.warning("%s | scores sorğusu uğursuz", sport_key)
        return None


def _parse_score_entry(ev):
    if not ev.get("completed"):
        return None
    sm = {s.get("name"): s.get("score") for s in (ev.get("scores") or [])}
    hs, as_ = sm.get(ev.get("home_team")), sm.get(ev.get("away_team"))
    try:
        return int(hs), int(as_)
    except (TypeError, ValueError):
        return None


def _settle_h2h_goal_leg(kind, hs, as_):
    """1X2/ikiqat şans/2.5 üst-alt pick-i real hesaba görə tutub-tutmadığını qaytarır."""
    if kind == "home":
        return hs > as_
    if kind == "away":
        return as_ > hs
    if kind == "dc1x":
        return hs >= as_
    if kind == "dcx2":
        return as_ >= hs
    if kind == "dc12":
        return hs != as_
    if kind == "over":
        return (hs + as_) > 2.5
    if kind == "under":
        return (hs + as_) < 2.5
    return None


def find_finished_fixture(home, away, start_ts, fixtures, used):
    """find_fixture-in bitmiş oyunlar üçün variantı (status FINISHED, daha geniş vaxt pəncərəsi)."""
    best, best_sc = None, 0.0
    for f in fixtures:
        fx = f.get("fixture") or {}
        fid = fx.get("id")
        if fid is None or fid in used:
            continue
        if (fx.get("status") or {}).get("short") not in FINISHED:
            continue
        if abs((fx.get("timestamp") or 0) - start_ts) > 4 * 3600:
            continue
        teams = f.get("teams") or {}
        sh = name_score(home, (teams.get("home") or {}).get("name", ""))
        sa = name_score(away, (teams.get("away") or {}).get("name", ""))
        if min(sh, sa) < 0.85 or sh + sa < 1.8:
            continue
        if sh + sa > best_sc:
            best, best_sc = f, sh + sa
    return best


def _settle_extra_leg(api, pick, fixtures_cache, used_cache):
    """Korner/kart pick-i API-Football vasitəsilə yoxlayır (yalnız STATS_MODE aktivdirsə)."""
    try:
        start_dt = datetime.fromisoformat(pick["start"]).astimezone(TZ)
    except ValueError:
        return None
    day_str = start_dt.date().isoformat()
    if day_str not in fixtures_cache:
        try:
            fixtures_cache[day_str] = api.get("/fixtures", date=day_str, timezone="Asia/Baku")
        except FootballError:
            fixtures_cache[day_str] = []
    used = used_cache.setdefault(day_str, set())
    fx = find_finished_fixture(pick["home"], pick["away"], start_dt.timestamp(),
                               fixtures_cache[day_str], used)
    if not fx:
        return None
    try:
        resp = api.get("/fixtures/statistics", fixture=fx["fixture"]["id"])
    except FootballError:
        return None
    s = parse_fixture_stats({"teams": fx.get("teams"), "statistics": resp}, fx["fixture"]["id"])
    if not s:
        return None
    used.add(fx["fixture"]["id"])
    total = (s["hc"] + s["ac"]) if pick["kind"].startswith("corners") else (s["hk"] + s["ak"])
    line = pick.get("line")
    if line is None:
        return None
    return total > line if pick["kind"].endswith("over") else total < line


def settle_day(day):
    """
    Bir günün bütün saxlanılmış kuponlarını real nəticələrlə yoxlayır. Kupon "win" sayılır
    yalnız BÜTÜN pick-lər tutubsa (akkumulyator məntiqi). Naməlum qalan pick varsa (nəticə
    mənbəyi tapılmayıb) kupon "unknown" sayılır və faizə qatılmır — səhv statistika verməkdənsə
    bilinməyəni bilinməyən kimi saxlamaq daha doğrudur.
    """
    try:
        raw = kv.hgetall(f"cp:{day}")
        already = set(kv.hgetall(f"cpres:{day}").keys())
    except Exception:
        log.exception("settle_day: oxuma xətası")
        return
    if not raw:
        return
    pending = {}
    for cid, v in raw.items():
        if cid in already:
            continue
        try:
            pending[cid] = json.loads(v)
        except Exception:
            continue
    if not pending:
        return

    sports = {p["sport"] for c in pending.values() for p in c["picks"]}
    score_map = {}
    for sp in sports:
        data = fetch_scores(sp, days_from=min(SETTLE_BACK_DAYS + 1, 3))
        if not data:
            continue
        for ev in data:
            sc = _parse_score_entry(ev)
            if sc:
                score_map[ev["id"]] = sc

    api = FootballAPI(FOOTBALL_KEY, SETTLE_FOOTBALL_BUDGET) if STATS_MODE else None
    fixtures_cache, used_cache = {}, {}

    settled_n = 0
    for cid, c in pending.items():
        results = []
        for p in c["picks"]:
            if p["kind"] in EXTRA_KINDS:
                if api is None or api.stopped:
                    results.append(None)
                    continue
                try:
                    results.append(_settle_extra_leg(api, p, fixtures_cache, used_cache))
                except Exception:
                    results.append(None)
            else:
                sc = score_map.get(p["event_id"])
                results.append(_settle_h2h_goal_leg(p["kind"], *sc) if sc else None)
        if any(r is None for r in results):
            status = "unknown"
        elif all(results):
            status = "win"
        else:
            status = "loss"
        try:
            kv.hset(f"cpres:{day}", cid, status)
            kv.hincrby(f"tierres:{day}:{c['tier']}", status, 1)
            settled_n += 1
        except Exception:
            log.warning("settlement yazılmadı: %s", cid)
    if settled_n:
        log.info("settle_day %s: %d/%d kupon yoxlanıldı", day, settled_n, len(pending))


def settlement_report(ndays=30):
    """Tier üzrə son ndays günün win/loss/unknown cəmini qaytarır."""
    today = datetime.now(TZ).date()
    per_tier = {t: {"win": 0, "loss": 0, "unknown": 0} for t in TIERS}
    for i in range(ndays):
        day = (today - timedelta(days=i)).isoformat()
        for tier in TIERS:
            try:
                h = kv.hgetall(f"tierres:{day}:{tier}")
            except Exception:
                h = {}
            for k in ("win", "loss", "unknown"):
                per_tier[tier][k] += int(h.get(k, 0))
    return per_tier


def my_coupon_history(uid, ndays=14):
    """İstifadəçinin son ndays gündəki kuponları: (day, tier, status)."""
    today = datetime.now(TZ).date()
    rows = []
    for i in range(ndays):
        day = (today - timedelta(days=i)).isoformat()
        try:
            cp = kv.hgetall(f"cp:{day}")
            res = kv.hgetall(f"cpres:{day}")
        except Exception:
            continue
        for cid, raw in cp.items():
            try:
                c = json.loads(raw)
            except Exception:
                continue
            if c.get("uid") != uid:
                continue
            rows.append((day, c["tier"], res.get(cid, "gözlənilir")))
    return rows


async def settle_loop():
    """Fonda: hər SETTLE_LOOP_EVERY dövründə son bir neçə günün kuponlarını yoxlayır."""
    await asyncio.sleep(45)
    while True:
        try:
            now = datetime.now(TZ)
            for back in range(1, SETTLE_BACK_DAYS + 1):
                day = (now.date() - timedelta(days=back)).isoformat()
                await asyncio.to_thread(settle_day, day)
        except Exception:
            log.exception("settle_loop xətası")
        await asyncio.sleep(SETTLE_LOOP_EVERY)


# ============================== KUPON ALQORİTMİ ==============================
@dataclass
class Coupon:
    tier: str
    picks: list        # [(Match, Leg), ...] vaxta görə sıralı
    total: float
    prob: float
    on_target: bool
    stats_used: bool = False   # kupon statistika ilə təsdiqlənib (False → yalnız bazar kefi)


def _search(rng, cands, ids, n, cfg, cap, t0, level, tries=400):
    """
    n oyunluq kombinasiya axtarır. Ümumi kef [lo, hi] daxilində olmalıdır və hədəf t0-a
    yaxın olan, ehtimalı yüksək, mümkünsə real (təxmini olmayan) kefli variant seçilir.
    level: 0 — bütün limitlər; 1 — liqa limiti yox, tip limiti var; 2 — yalnız kef aralığı.
    Korner/kart limiti (MAX_EXTRAS) bütün səviyyələrdə qüvvədədir.
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
            n_extra = sum(kinds[k] for k in EXTRA_KINDS)
            if n_extra >= MAX_EXTRAS:
                opts = [l for l in opts if l.kind not in EXTRA_KINDS]
            if level <= 1:   # eyni tip limiti dolubsa, o tipi bu oyun üçün kənarlaşdır
                opts = [l for l in opts if kinds[l.kind] < MAX_PER_KIND.get(l.kind, 99)]
            if not opts:     # bu oyun üçün uyğun variant qalmadı → kombinasiya keçərsiz
                bad = True
                break
            # ən çox EHTİMALLI variant seçilir (məsələn 1X vs X2-dən daha yəqin olan), kef hədəfə
            # yaxınlıq yalnız ikinci dərəcəli meyardır — beləcə eyni oyun üçün "Başqa variant"
            # bir-birinə zidd (bir dəfə 1X, bir dəfə X2) tövsiyə vermir
            leg = max(opts, key=lambda l: l.prob
                      - 0.15 * abs(math.log(l.odds) - math.log(target))
                      - (0.05 if l.approx else (0.02 if l.kind in EXTRA_KINDS else 0.0))
                      + rng.uniform(0, 0.01))
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


def _candidates(pool, cfg, use_stats, mode):
    """Hər oyun üçün kupona düşə biləcək pick-lər (kef aralığı, tip, statistika təsdiqi)."""
    cands = {}
    for m in pool:
        if use_stats and not _verified(m):
            continue                                                  # statistikası təsdiqlənməyib
        opts = [l for l in m.legs
                if l.kind != "draw"                                   # təkcə X yoxdur
                and (cfg["extras"] or l.kind not in EXTRA_KINDS)      # ehtiyatlıda korner/kart yoxdur
                and cfg["pick_lo"] <= l.odds <= cfg["pick_hi"]
                and (not use_stats or stat_gate(m, l, mode))]         # statistika ilə təsdiq
        if opts:
            cands[m.id] = (m, opts)
    return cands


def build_coupon(matches, tier, variant=0, now=None, use_stats=None):
    """
    Azərbaycan məntiqi: oyun sayı sabit deyil.
      ehtiyatlı — 3-4 oyun, normal — 5-6, riskli — 7-9 (oyun azdırsa azalır).
    Statistika işləyirsə: yalnız təsdiqlənmiş oyunlar və statistika ilə üst-üstə düşən pick-lər;
    namizəd az qalanda şərtlər avtomatik yumşalır. Statistika işləmirsə bazar rejimi.
    Eyni gün + eyni növ + eyni variant həmişə eyni kupon verir; 'Başqa variant' yenisini.
    """
    cfg = TIERS[tier]
    now = now or datetime.now(TZ)
    if use_stats is None:
        use_stats = stats_usable(matches, now)
    rng = random.Random(f"{now.date().isoformat()}|{tier}|{variant}")
    pool = [m for m in matches
            if m.start.date() == now.date()                                   # yalnız bu gün (Bakı vaxtı)
            and m.start > now + timedelta(minutes=MIN_MINUTES_BEFORE_KICKOFF)]
    cands = _candidates(pool, cfg, use_stats, "strict")
    relaxed = False
    if use_stats and len(cands) < cfg["min_legs"]:
        cands = _candidates(pool, cfg, use_stats, "relaxed")            # şərtləri yumşalt
        relaxed = True
    ids = list(cands)
    log.info("kupon %s | bazada %d oyun, vaxtı uyğun %d, namizəd %d (minimum %d) | statistika=%s%s",
             tier, len(matches), len(pool), len(ids), cfg["min_legs"], use_stats,
             " (yumşaldılmış)" if relaxed else "")
    if len(ids) < cfg["min_legs"]:      # növün vəd etdiyi oyun sayından az oyunla kupon verilmir
        return None
    n_top = sum(1 for i in ids if cands[i][0].top)
    choices = {n: w for n, w in cfg["legs"].items() if n <= len(ids)} or {len(ids): 1}
    n = rng.choices(list(choices), weights=list(choices.values()))[0]

    # bu kupon üçün hədəf ümumi kef
    t0 = math.exp(rng.uniform(math.log(cfg["lo"] * 1.05), math.log(cfg["hi"] * 0.95)))
    # Aşağı/əlavə liqadan neçə oyuna icazə: YALNIZ FALLBACK. Məşhur liqalarda (TOP_LEAGUES)
    # kifayət qədər oyun varsa əlavə liqaya ehtiyac yoxdur (extra_pref=0); çatışmazsa
    # aşağıdakı "cap = max(extra_pref, nn - n_top)" bunu avtomatik açır.
    extra_pref = 0

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
    return Coupon(tier, picks, total, prob, on_target, use_stats)


def count_upcoming(matches, now=None):
    """Bu gün hələ başlamamış (15 dəq. qalmış çıxmaqla) oyun sayı."""
    now = now or datetime.now(TZ)
    return sum(1 for m in matches
               if m.start.date() == now.date() and m.start > now + timedelta(minutes=MIN_MINUTES_BEFORE_KICKOFF))


_bot_info = {"username": env("BOT_USERNAME").lstrip("@")}


def bot_link():
    u = _bot_info["username"]
    return f"https://t.me/{u}?start=share" if u else ""


def owner_footer(L):
    """Kuponun sonundakı qısa qurucu sətri (SHOW_OWNER_IN_COUPON=0 ilə söndürülür)."""
    if not SHOW_OWNER_IN_COUPON:
        return None
    bits = []
    if _bot_info["username"]:
        bits.append(f"🤖 @{_bot_info['username']}")
    bits.append(L["made_by"].format(name=OWNER_NAME, handle=OWNER_HANDLE))
    return " · ".join(bits)


def _line(leg, default=""):
    return f"{leg.line:g}" if leg.line is not None else default


def _sn(name):
    """Komanda adını qısalt (mobil ekran üçün)."""
    return name if len(name) <= 14 else name[:13] + "…"


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
    return L["p_" + k].format(line=_line(leg))  # dc12, over, under, corners_*, cards_*


def note_text(L, m, leg):
    """
    Analiz qeydi. Statistika varsa: rəqəmlər birbaşa mənbə (API-Football/football-data.org)
    məlumatından hesablanır. Statistika yoxdursa: yalnız bazar ehtimalı, ehtimal həddindən yuxarı olanda.
    """
    k = leg.kind
    info = m.info if _verified(m) else None
    h, a = _sn(m.home), _sn(m.away)
    if info:
        if k in EXTRA_KINDS:
            key = "cor" if k.startswith("corners") else "crd"
            e = expect_pair(info, key)
            if e is None:
                return None
            n = min(info[key][4], info[key][5])
            return L["s_" + key].format(n=n, h=h, a=a, eh=f"{e[0]:.1f}", ea=f"{e[1]:.1f}",
                                        e=f"{e[0] + e[1]:.1f}")
        if k in ("over", "under"):
            e = expect_goals(info)
            if e is None:
                return None
            return L["s_goals"].format(h=h, a=a, eh=f"{e[0]:.1f}", ea=f"{e[1]:.1f}",
                                       e=f"{e[0] + e[1]:.1f}")
        if k in ("home", "away", "dc1x", "dcx2", "dc12"):
            parts = []
            form = info.get("form") or []
            if len(form) == 2 and form[0] and form[1]:
                parts.append(L["s_form"].format(h=h, a=a, fh=form[0], fa=form[1]))
            pct = info.get("pct")
            if pct:
                ph, pd, pa = (round(x * 100) for x in pct)
                s = L["s_model"].format(ph=ph, pd=pd, pa=pa)
                h2h = info.get("h2h")
                if h2h and len(h2h) == 4 and h2h[3] >= 3:
                    s += f" · H2H {h2h[0]}-{h2h[1]}-{h2h[2]}"
                parts.append(s)
            return "\n   ".join(parts) or None
        return None
    # statistika yoxdur → yalnız bazar əsaslı qeyd
    if k in EXTRA_KINDS or k in ("over", "under"):
        if leg.prob >= 0.54:
            return L["n_" + k].format(line=_line(leg, "2.5"), p=round(leg.prob * 100))
        return None
    if k in ("home", "away", "dc1x", "dcx2", "dc12") and m.p3 and leg.prob >= 0.60:
        ph, pd, pa = (round(x * 100) for x in m.p3)
        return L["n_1x2"].format(h=ph, d=pd, a=pa)
    return None


def format_coupon(lang, c, now=None, left=None, limit=None):
    L, cfg = T(lang), TIERS[c.tier]
    today = (now or datetime.now(TZ)).date()

    # analiz qeydləri: əvvəl korner/kart, sonra qol, sonra qalib/ikiqat şans; cəmi MAX_NOTES
    cand = []
    for i, (m, leg) in enumerate(c.picks):
        t = note_text(L, m, leg)
        if t:
            grp = 0 if leg.kind in EXTRA_KINDS else (1 if leg.kind in ("over", "under") else 2)
            cand.append((grp, -leg.prob, i, t))
    notes = {i: t for _, _, i, t in sorted(cand)[:MAX_NOTES]}

    lines = [L["head"].format(icon=cfg["icon"], name=L["name_" + c.tier],
                              n=len(c.picks), total=f"{c.total:.2f}"), L["tz_note"], ""]
    for i, (m, leg) in enumerate(c.picks):
        when = m.start.strftime("%H:%M") if m.start.date() == today else m.start.strftime("%d.%m %H:%M")
        approx = "≈" if leg.approx else ""
        block = (f"{i + 1}. {m.home} - {m.away}\n   🕒 {when} · {m.league}\n"
                 f"   ➜ {pick_text(L, m, leg)} | {L['odds_word']} {approx}{leg.odds:.2f}")
        if i in notes:
            block += f"\n   {notes[i]}"
        lines.append(block)
    lines.append("")
    lines.append(L["prob"].format(p=f"{c.prob * 100:.1f}" if c.prob < 0.1 else f"{c.prob * 100:.0f}"))
    if not c.on_target:
        lines.append(L["short_note"])
    if any(l.approx for _, l in c.picks):
        lines.append(L["approx_note"])
    if any(l.kind in EXTRA_KINDS for _, l in c.picks):
        lines.append(L["extra_note"])
    if c.stats_used and any(i in notes and _verified(c.picks[i][0]) for i in range(len(c.picks))):
        lines.append(L["stats_note"])
    if (STATS_MODE or FD_ORG_MODE) and not c.stats_used:
        lines.append(L["fb_note"])
    lines.append("")
    lines.append(L["disclaimer"])
    if left is not None and limit:
        lines.append("")
        lines.append(L["left"].format(left=left, limit=limit))
        if left == 0:
            lines.append(L["left_last"])
    foot = owner_footer(L)
    if foot:
        lines.append("")
        lines.append(foot)
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
        n_x = sum(1 for m in snap.matches if any(l.kind in EXTRA_KINDS for l in m.legs))
        out.append(L["a_snap_x"].format(x=n_x))
        out.append(L["a_credits"].format(o=credits_used("odds"), ob=DAILY_CREDIT_BUDGET,
                                         x=credits_used("extra"), xb=EXTRA_CREDIT_BUDGET))
        if snap.partial:
            out.append(L["a_partial"].format(t=snap.tries, m=MAX_FETCH_TRIES, f=snap.fails))
        if STATS_MODE:
            v = sum(1 for m in snap.matches if _verified(m))
            err = f" · ⚠️ {snap.fb_err}" if snap.fb_err else ""
            fl = snap.fb_left if snap.fb_left is not None else "?"
            out.append(L["a_stats"].format(v=v, m=len(snap.matches), c=snap.fb_calls, r=fl, err=err))
            if not stats_usable(snap.matches):
                out.append(L["a_stats_fb"])
        if FD_ORG_MODE:
            fs = _fd_state
            when = fs["last_run"].strftime("%H:%M") if fs["last_run"] else "—"
            status = "✅" if (fs["ok"] and not fs["error"]) else "⚠️"
            out.append(f"{status} football-data.org: {fs['verified']}/{fs['checked']} oyun ({fs['competitions']} liqa) "
                       f"· son yoxlama {when}" + (f" · xəta: {fs['error']}" if fs["error"] else "")
                       + " · ətraflı: /footballorg")
    else:
        out.append(L["a_snap_none"])
    if not STATS_MODE and not FD_ORG_MODE:
        out.append(L["a_stats_off"])
    out.append(L["a_limit"].format(n=USER_DAILY_LIMIT))
    out.append(L["a_mem_ok"] if rep["persistent"] else L["a_mem_tmp"])
    return "\n".join(out)


# ============================== TELEGRAM ==============================
_last_click = {}
_last_admin_alert = {"t": 0.0}
_awaiting_coupon_photo = {}   # uid -> vaxt (timestamp) — /kuponumabax-dan sonra şəkil gözlənilir


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
    text = L["welcome"]
    if USER_DAILY_LIMIT > 0:
        text += L["welcome_limit"].format(limit=USER_DAILY_LIMIT)
    text += "\n\n" + L["made_by"].format(name=OWNER_NAME, handle=OWNER_HANDLE)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(L["btn_coupon"], callback_data="m")],
        [InlineKeyboardButton(label, callback_data=f"l:{code}") for code, label in LANG_BUTTONS],
    ])
    await update.message.reply_text(text, reply_markup=kb)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = await user_lang(update.effective_user)
    await update.message.reply_text(T(lang)["menu_title"], reply_markup=menu_keyboard(lang))


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(T("az")["lang_prompt"], reply_markup=lang_keyboard())


async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Qurucu haqqında + reklam + paylaşma linki (start=share mənbə kimi izlənir)."""
    L = T(await user_lang(update.effective_user))
    if not _bot_info["username"] and getattr(context.bot, "username", None):
        _bot_info["username"] = context.bot.username
    parts = [L["about"].format(name=OWNER_NAME, handle=OWNER_HANDLE),
             PROMO_TEXT or L["promo"].format(handle=OWNER_HANDLE)]
    link = bot_link()
    if link:
        parts.append(L["share"].format(link=link))
    await update.message.reply_text("\n\n".join(parts))


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


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    L = T(await user_lang(user))
    msg = await update.message.reply_text(L["r_wait"])
    try:
        d = await asyncio.to_thread(force_refresh)
    except Exception:
        log.exception("/yenile xətası")
        await msg.edit_text(L["gen_error"])
        return
    cnt = Counter(m.league for m in d.matches)
    lg = ", ".join(f"{k} ({v})" for k, v in cnt.most_common(12)) or "—"
    rem = d.remaining if d.remaining is not None else "?"
    await msg.edit_text(L["r_done"].format(m=len(d.matches), l=d.leagues, f=d.fails, r=rem, lg=lg))


async def cmd_footballorg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: football-data.org statistika mənbəyinin cari vəziyyətini göstərir."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    if not FD_ORG_MODE:
        await update.message.reply_text(
            "⚠️ football-data.org söndürülüb — FOOTBALL_DATA_ORG_KEY təyin edilməyib (Render → Environment)."
        )
        return
    s = _fd_state
    if s["last_run"] is None:
        await update.message.reply_text(
            "⏳ football-data.org hələ heç çağırılmayıb — baza hələ fonda hazırlanır. "
            "Bir neçə dəqiqə sonra yenidən /footballorg yaz, ya da /yenile ilə dərhal yenilə."
        )
        return
    when = s["last_run"].strftime("%H:%M")
    comps = ", ".join(sorted(FD_ORG_COMPETITIONS)) or "—"
    if s["ok"] and not s["error"]:
        if s["checked"] == 0:
            text = (
                "✅ football-data.org əlaqəsi qaydasındadır, amma bu gün pulsuz plandakı "
                "liqalarda (Champions League, Premier League, La Liga, Bundesliga, Serie A, "
                "Ligue 1, Eredivisie, Primeira Liga) hələ yoxlanılacaq oyun yoxdur.\n\n"
                f"🕒 Son yoxlama: {when}"
            )
        else:
            text = (
                "✅ football-data.org vasitəsilə statistika uğurla çəkilməyə davam edir...\n\n"
                f"🕒 Son yoxlama: {when}\n"
                f"⚽ Yoxlanılan liqa sayı: {s['competitions']}\n"
                f"📊 Statistikası təsdiqlənən oyun: {s['verified']}/{s['checked']}"
            )
    else:
        err_map = {
            "auth": "token səhvdir — FOOTBALL_DATA_ORG_KEY-i yoxla",
            "quota": "sorğu limiti bitib (10/dəqiqə pulsuz plan) — bir az sonra özü düzələcək",
            "network": "şəbəkə xətası — bir az sonra özü yenidən cəhd edəcək",
        }
        text = (
            "⚠️ football-data.org-dan statistika hazırda alınmır.\n\n"
            f"🕒 Son cəhd: {when}\n"
            f"❌ Səbəb: {err_map.get(s['error'], s['error'] or 'naməlum xəta')}\n"
            f"📊 Bu cəhddə təsdiqlənən: {s['verified']}/{s['checked']}"
        )
    text += f"\n\n📋 İzlənən liqalar: {comps}"
    await update.message.reply_text(text)


# ---- /islemek: 3 məlumat mənbəyinin (Odds API, API-Football, football-data.org) canlı sağlamlıq yoxlaması ----
def _check_odds_api():
    """Odds API: /sports PULSUZDUR, ona görə burda kredit xərclənmir."""
    try:
        leagues, remaining = discover_leagues()
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        if code == 401:
            return dict(ok=False, msg="açar səhvdir (401) — ODDS_API_KEY-i yoxla")
        return dict(ok=False, msg=f"HTTP xətası ({code})")
    except Exception as e:
        return dict(ok=False, msg=f"sorğu uğursuz: {str(e)[:150]}")
    if not leagues:
        return dict(ok=False, msg="açar işləyir, amma heç bir futbol liqası tapılmadı")
    warn = remaining is not None and remaining < LOW_CREDIT_WARN
    msg = f"{len(leagues)} liqa aktiv · qalan kredit: {remaining if remaining is not None else '?'}"
    if warn:
        msg += " ⚠️ kredit azalıb"
    return dict(ok=True, msg=msg, warn=warn)


def _check_football_api():
    """API-Football: /status endpoint-i abunəlik məlumatını göstərir (əsas statistika mənbəyi)."""
    if not STATS_MODE:
        return dict(ok=None, msg="API_FOOTBALL_KEY təyin edilməyib — söndürülüb")
    try:
        api = FootballAPI(FOOTBALL_KEY, budget=3)
        resp = api.get("/status")
    except FootballError as e:
        reason_map = {"plan": "pulsuz plan cari mövsümü vermir (bot bazar rejiminə keçib)",
                      "auth": "açar səhvdir", "quota": "gündəlik sorğu limiti bitib",
                      "budget": "daxili büdcə bitib"}
        return dict(ok=False, msg=reason_map.get(str(e), str(e)[:150]))
    except Exception as e:
        return dict(ok=False, msg=f"sorğu uğursuz: {str(e)[:150]}")
    sub = (resp[0].get("subscription") or {}) if resp else {}
    plan, active = sub.get("plan"), sub.get("active")
    msg = "açar işləkdir"
    if plan:
        msg += f" · plan: {plan}"
    if active is False:
        msg += " ⚠️ abunəlik aktiv deyil"
    return dict(ok=True, msg=msg)


def _check_football_org():
    """football-data.org: /competitions endpoint-i pulsuz plan üçün də açıqdır (ehtiyat statistika mənbəyi)."""
    if not FD_ORG_MODE:
        return dict(ok=None, msg="FOOTBALL_DATA_ORG_KEY təyin edilməyib — söndürülüb")
    try:
        api = FootballOrgAPI(FOOTBALL_ORG_KEY)
        data = api.get("/competitions")
    except FootballOrgError as e:
        reason_map = {"auth": "açar səhvdir",
                      "quota": "dəqiqəlik limit bitib (bir az sonra özü düzələcək)",
                      "network": "şəbəkə xətası"}
        return dict(ok=False, msg=reason_map.get(str(e), str(e)[:150]))
    except Exception as e:
        return dict(ok=False, msg=f"sorğu uğursuz: {str(e)[:150]}")
    n = len(data.get("competitions") or [])
    return dict(ok=True, msg=f"açar işləkdir · {n} liqa əlçatandır")


def check_all_sources():
    return dict(odds=_check_odds_api(), football=_check_football_api(), footballorg=_check_football_org())


async def cmd_islemek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /islemek — Odds API, API-Football və football-data.org-un CANLI sağlamlığını yoxlayır."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    msg = await update.message.reply_text("🔍 3 mənbə yoxlanılır...")
    try:
        r = await asyncio.to_thread(check_all_sources)
    except Exception:
        log.exception("/islemek xətası")
        await msg.edit_text("⚠️ Yoxlama zamanı xəta baş verdi. Render Logs-a bax.")
        return

    def line(name, res):
        if res["ok"] is True:
            icon = "⚠️" if res.get("warn") else "✅"
        elif res["ok"] is False:
            icon = "❌"
        else:
            icon = "➖"
        return f"{icon} {name}: {res['msg']}"

    lines = [
        "🔍 Mənbə vəziyyəti",
        "",
        line("Odds API (bazar çoxluğu)", r["odds"]),
        line("API-Football (əsas statistika)", r["football"]),
        line("football-data.org (ehtiyat statistika)", r["footballorg"]),
    ]
    if r["odds"]["ok"] is False:
        lines.append("\n🚨 Odds API işləmirsə bot ümumiyyətlə kupon verə bilməz — bunu ilk növbədə düzəlt.")
    elif r["football"]["ok"] is False and r["footballorg"]["ok"] is False:
        lines.append("\n⚠️ Hər iki statistika mənbəyi xətalıdır — kuponlar müvəqqəti yalnız bazar kefinə əsaslanacaq.")
    await msg.edit_text("\n".join(lines))


async def cmd_statistika(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hamıya açıq: son 30 günün tier üzrə tutma faizi (şəffaflıq/etibar üçün, şəxsi məlumat yoxdur)."""
    lang = await user_lang(update.effective_user)
    L = T(lang)
    ndays = 30
    rep = await safe_call(settlement_report, ndays)
    if not rep or not any(v["win"] + v["loss"] for v in rep.values()):
        await update.message.reply_text(L["stat_empty"])
        return
    lines = [L["stat_title"].format(n=ndays)]
    for tier, cfg in TIERS.items():
        w, l = rep[tier]["win"], rep[tier]["loss"]
        settled = w + l
        if settled == 0:
            continue
        pct = round(100 * w / settled)
        lines.append(L["stat_row"].format(icon=cfg["icon"], name=L["name_" + tier],
                                          pct=pct, settled=settled))
    lines.append(L["stat_footer"])
    await update.message.reply_text("\n".join(lines))


async def cmd_mycoupons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """İstifadəçinin öz kupon tarixçəsi + şəxsi tutma faizi (son 14 gün)."""
    user = update.effective_user
    lang = await user_lang(user)
    L = T(lang)
    ndays = 14
    rows = await safe_call(my_coupon_history, user.id, ndays)
    if not rows:
        await update.message.reply_text(L["my_empty"])
        return
    w = sum(1 for _, _, s in rows if s == "win")
    l = sum(1 for _, _, s in rows if s == "loss")
    p = sum(1 for _, _, s in rows if s not in ("win", "loss"))
    lines = [L["my_title"].format(n=ndays, total=len(rows)),
             L["my_row"].format(w=w, l=l, p=p)]
    for day, tier, status in sorted(rows, reverse=True)[:10]:
        mark = {"win": "✅", "loss": "❌"}.get(status, "⏳")
        cfg = TIERS[tier]
        lines.append(f"{mark} {day} · {cfg['icon']} {L['name_' + tier]}")
    await update.message.reply_text("\n".join(lines))


async def cmd_netice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: detallı nəticə statistikası (tier üzrə tutma faizi, sınmış/naməlum sayı)."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    ndays = 30
    if context.args and context.args[0].isdigit():
        ndays = max(1, min(int(context.args[0]), 90))
    rep = await safe_call(settlement_report, ndays)
    if rep is None:
        await update.message.reply_text("⚠️ Nəticə statistikası oxunmadı. Render Logs-a bax.")
        return
    lines = [f"📊 Son {ndays} gün — kupon nəticələri (admin)"]
    tot_w = tot_l = tot_u = 0
    for tier, cfg in TIERS.items():
        w, l, u = rep[tier]["win"], rep[tier]["loss"], rep[tier]["unknown"]
        settled = w + l
        pct = f"{100 * w / settled:.0f}%" if settled else "—"
        lines.append(f"{cfg['icon']} {tier}: {w}✅ / {l}❌ (tutma {pct}) · naməlum {u}")
        tot_w += w
        tot_l += l
        tot_u += u
    settled = tot_w + tot_l
    pct = f"{100 * tot_w / settled:.0f}%" if settled else "—"
    lines.append(f"\nÜmumi: {tot_w}✅ / {tot_l}❌ (tutma {pct}) · naməlum {tot_u}")
    lines.append("\nℹ️ 'naməlum' = nəticə mənbəyində (Odds API scores / API-Football) uyğun "
                "oyun və ya market tapılmadı, kuponu heç bir tərəfə hesablamadıq.")
    if not STATS_MODE:
        lines.append("ℹ️ API_FOOTBALL_KEY yoxdur — korner/kart pick-ləri həmişə 'naməlum' qalacaq.")
    await update.message.reply_text("\n".join(lines))


async def cmd_kuponumabax(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """İstifadəçi öz kuponunun şəklini atır, bot analiz edir."""
    L = T(await user_lang(update.effective_user))
    if not COUPON_READ_ENABLED:
        await update.message.reply_text("⚠️ Bu funksiya hazırda deaktivdir.")
        return
    _awaiting_coupon_photo[update.effective_user.id] = time.time()
    await update.message.reply_text("📸 Kuponunun şəklini at, baxım.")


async def on_coupon_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/kuponumabax-dan sonra atılan şəkli oxuyur. Başqa vaxt gələn şəkillərə toxunmur."""
    uid = update.effective_user.id
    started = _awaiting_coupon_photo.get(uid)
    if not started or time.time() - started > COUPON_WAIT_SECONDS:
        return
    _awaiting_coupon_photo.pop(uid, None)

    status = await update.message.reply_text("🔍 Kuponu oxuyuram...")
    try:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        img_bytes = bytes(await tg_file.download_as_bytearray())

        picks = await asyncio.to_thread(call_gemini_vision, img_bytes)
        if not picks:
            await status.edit_text("😕 Şəkli oxuya bilmədim. Aydın, tam kupon skrini at (kupon detalı səhifəsi).")
            return

        snap, ok, attempted = peek_snapshot()
        if snap is None:
            await status.edit_text("⏳ Bugünkü oyun bazası hələ hazır deyil, bir az sonra yenidən cəhd et.")
            return

        result = await asyncio.to_thread(analyze_coupon_picks, snap, picks)
        await status.edit_text(result)
    except Exception:
        log.exception("/kuponumabax xətası")
        try:
            await status.edit_text("⚠️ Xəta baş verdi. Bir az sonra yenidən cəhd et.")
        except Exception:
            pass


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

    # ---- gündəlik kupon krediti (hər kupon, o cümlədən "Başqa variant", 1 kredit) ----
    limited = USER_DAILY_LIMIT > 0 and not (ADMIN_EXEMPT and user.id in ADMIN_IDS)
    used = 0
    if limited:
        used = await safe_call(stats.used_today, user.id) or 0
        if used >= USER_DAILY_LIMIT:
            await q.message.reply_text(L["limit_reached"].format(limit=USER_DAILY_LIMIT))
            return

    status = await q.message.reply_text(L["checking"])
    try:
        # İstifadəçi heç vaxt gözləmir: baza fonda hazırlanır, burada yalnız hazır olanı oxuyuruq.
        snap, ok, attempted = peek_snapshot()
        if snap is None:
            if attempted and not _state["building"]:      # yükləmə cəhd olunub, alınmayıb
                await alert_admin(context, "⚠️ Kupon botu: oyun məlumatı alınmır. Odds API limitini/Logs-u yoxla.")
                await status.edit_text(L["data_error"])
            else:                                          # hələ hazırlanır / yeni gün başlayıb
                if not _state["building"] and not _state["warming"]:
                    _state["warming"] = True
                    asyncio.create_task(asyncio.to_thread(warm_up))
                await status.edit_text(L["preparing"])
            return
        if STATS_MODE and not snap.stats_done and _state["building"]:
            await status.edit_text(L["preparing"])        # statistika hazırlanır (kredit xərclənmir)
            return
        warns = []                                   # adminə xəbərdarlıqlar
        if snap.remaining is not None and snap.remaining < LOW_CREDIT_WARN:
            warns.append(f"Odds API krediti azalıb: {snap.remaining} qalıb")
        if snap.fb_err == "plan":
            warns.append("API-Football planı cari mövsümü vermir → Pro plana keç (bot bazar rejimində işləyir)")
        elif snap.fb_err == "quota":
            warns.append("API-Football gündəlik limiti bitib")
        elif snap.fb_err == "auth":
            warns.append("API_FOOTBALL_KEY səhvdir")
        if warns:
            await alert_admin(context, "⚠️ Kupon botu: " + " | ".join(warns))
        coupon = await asyncio.to_thread(build_coupon, snap.matches, tier, variant)
        if coupon is None:
            if not ok:
                await alert_admin(context, "⚠️ Kupon botu: Odds API kvotası bitmiş ola bilər.")
                await status.edit_text(L["quota_out"])
            else:
                upcoming = count_upcoming(snap.matches)
                if upcoming < 2:
                    txt = L["no_matches_stats"] if (STATS_MODE or FD_ORG_MODE) else L["no_matches"]
                else:
                    txt = L["no_tier"].format(name=L["name_" + tier])
                await status.edit_text(txt)
            return
        await safe_call(record_coupon_for_settlement, coupon, user.id)   # sonrakı nəticə yoxlaması üçün saxla
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(L["btn_again"], callback_data=f"c:{tier}:{variant + 1}"),
            InlineKeyboardButton(L["btn_menu"], callback_data="m"),
        ]])
        left = max(0, USER_DAILY_LIMIT - used - 1) if limited else None
        await status.edit_text(format_coupon(lang, coupon, left=left, limit=USER_DAILY_LIMIT),
                               reply_markup=kb)
    except Exception:
        log.exception("Kupon xətası")
        try:
            await status.edit_text(L["gen_error"])
        except Exception:
            pass
        return
    await safe_call(stats.record, user.id, display_name(user), tier)  # statistika + kredit sayğacı


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Handler xətası", exc_info=context.error)


# ============================== HEARTBEAT + WATCHDOG + FON DÖVRÜ ==============================
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


async def refresh_loop(app: Application):
    """
    Bazanı fonda hazır saxlayır: bot açılanda, hər yeni gün (00:00-dan sonra) və natamam/yarımçıq
    bazanı avtomatik təkrar yoxlayır. get_snapshot() işi olmayanda ani qayıdır.
    """
    await asyncio.sleep(2)
    while True:
        try:
            await asyncio.to_thread(warm_up)
        except Exception:
            log.exception("refresh_loop xətası")
        await asyncio.sleep(REFRESH_EVERY)


async def post_init(app: Application):
    app.bot_data["hb"] = asyncio.create_task(heartbeat_loop(app))
    if not _bot_info["username"] and getattr(app.bot, "username", None):
        _bot_info["username"] = app.bot.username
    log.info("Bot: @%s", _bot_info["username"] or "?")
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS["az"]])
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS["en"]], language_code="en")
    except Exception:
        log.exception("Komanda menyusu qurulmadı")
    app.bot_data["refresh"] = asyncio.create_task(refresh_loop(app))
    app.bot_data["settle"] = asyncio.create_task(settle_loop())


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
    port = env_int("PORT", 10000)
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


def main():
    log.info("Statistika mənbələri: API-Football=%s | football-data.org=%s | kupon şəkli oxuma=%s | "
             "istifadəçi limiti: %s/gün | Odds büdcə: %s+%s/gün | qurucu: %s %s",
             "AÇIQ" if STATS_MODE else "SÖNDÜRÜLÜB", "AÇIQ (pulsuz)" if FD_ORG_MODE else "SÖNDÜRÜLÜB",
             "AÇIQ" if COUPON_READ_ENABLED else "SÖNDÜRÜLÜB",
             USER_DAILY_LIMIT or "limitsiz", DAILY_CREDIT_BUDGET, EXTRA_CREDIT_BUDGET,
             OWNER_NAME, OWNER_HANDLE)
    if (DAILY_CREDIT_BUDGET + EXTRA_CREDIT_BUDGET) * 31 > 500:
        log.warning("Odds API büdcəsi (%d/gün) pulsuz planın aylıq 500 kreditini keçə bilər!",
                    DAILY_CREDIT_BUDGET + EXTRA_CREDIT_BUDGET)
    threading.Thread(target=keep_alive, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    app = (Application.builder().token(BOT_TOKEN)
           .post_init(post_init)
           .concurrent_updates(True)   # bir istifadəçinin gözləməsi digərini dondurmasın
           .build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["gununoyunlari", "coupon"], cmd_menu))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("about", cmd_about))
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler(["yenile", "refresh"], cmd_refresh))
    app.add_handler(CommandHandler("footballorg", cmd_footballorg))
    app.add_handler(CommandHandler("islemek", cmd_islemek))
    app.add_handler(CommandHandler(["statistika", "stats"], cmd_statistika))
    app.add_handler(CommandHandler(["kuponlarim", "mycoupons"], cmd_mycoupons))
    app.add_handler(CommandHandler("netice", cmd_netice))
    app.add_handler(CommandHandler("kuponumabax", cmd_kuponumabax))
    app.add_handler(MessageHandler(filters.PHOTO, on_coupon_photo))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    # drop_pending_updates: restartda köhnə yığılmış mesajlara cavab yağdırmasın
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
