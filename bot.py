"""
KUPON BOT v3 — futbol kupon köməkçisi (Azərbaycan məntiqinə uyğun, çoxdilli).

Mühit dəyişənləri (Render → Environment):
  BOT_TOKEN                 (məcburi) BotFather tokeni
  ODDS_API_KEY              (məcburi) the-odds-api.com açarı (kefləri verir)
  API_FOOTBALL_KEY          (tövsiyə) api-football.com açarı (REAL komanda statistikası).
                            Qoyulmasa bot yalnız bazar kefinə əsaslanır.
                            DİQQƏT: pulsuz plan cari mövsümü vermir → Pro plan lazımdır.
  ADMIN_ID                  (tövsiyə) Telegram ID-n; birdən çox olsa vergüllə: 123,456
  UPSTASH_REDIS_REST_URL    (tövsiyə) daimi statistika + kredit qorunması üçün
  UPSTASH_REDIS_REST_TOKEN  (tövsiyə) yuxarıdakı ilə birlikdə
  USER_DAILY_LIMIT          (ixtiyari) hər istifadəçiyə gündə neçə kupon krediti, default 3 (0 = limitsiz)
  ADMIN_EXEMPT              (ixtiyari) 1 = adminlərə limit tətbiq olunmur (default), 0 = olunur
  DAILY_CREDIT_BUDGET       (ixtiyari) Odds API əsas baza üçün gündə maksimum kredit, default 10
  EXTRA_CREDIT_BUDGET       (ixtiyari) Odds API korner/kart kefləri üçün gündə maksimum kredit, default 6
  FOOTBALL_DAILY_BUDGET     (ixtiyari) API-Football gündə maksimum sorğu, default 400
  STATS_MAX_MATCHES         (ixtiyari) statistika çəkiləcək maksimum oyun sayı, default 30
  DEFAULT_LANG              (ixtiyari) az / en, default az
  CONTACT                   (ixtiyari) məxfilik mətnində göstərilən əlaqə (@username)

Əsas fikirlər:
  * Mərc növləri: 1, 2, 1X, X2, 12 (təkcə "X" yoxdur) + 2.5 qol Üst/Alt
    + (mövcud olduqda) korner və kart Üst/Alt.
  * STATİSTİKA REJİMİ (API_FOOTBALL_KEY varsa): oyun yalnız real statistika ilə təsdiqlənibsə
    kupona düşür. Hər pick statistika ilə "üst-üstə düşməlidir" (model, forma, gözlənən qol/
    korner/kart). Analiz qeydlərindəki rəqəmlər birbaşa API-Football məlumatından hesablanır.
  * KREDİT SİSTEMİ: hər istifadəçiyə gündə USER_DAILY_LIMIT kupon (Bakı vaxtı 00:00-da yenilənir).
    "Başqa variant" da kredit sayılır. İstifadəçi kuponları API krediti XƏRCLƏMİR
    (məlumat gündə bir dəfə çəkilib keşlənir).
  * Odds API kreditinə qənaət: liqalar və oyunlar PULSUZ endpoint-lərlə tapılır,
    kredit yalnız bu gün oyunu olan liqalara xərclənir, gündəlik büdcə var.
  * Admin xəbərdarlıqları: kredit azalanda, kvota bitəndə, API-Football planı uyğun olmayanda.
  * Watchdog: bot donsa proses özü yenidən başlayır.
  * Mühit dəyişənlərindəki artıq boşluq / yeni sətir / dırnaq avtomatik təmizlənir.
"""
import asyncio
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

FOOTBALL_KEY = env("API_FOOTBALL_KEY")
STATS_MODE = bool(FOOTBALL_KEY)

ADMIN_IDS = {int(x) for x in re.findall(r"\d+", env("ADMIN_ID"))}
ADMIN_EXEMPT = env("ADMIN_EXEMPT", "1") != "0"
DEFAULT_LANG = env("DEFAULT_LANG", "az")
USER_DAILY_LIMIT = int(env("USER_DAILY_LIMIT", "3") or 3)
DAILY_CREDIT_BUDGET = int(env("DAILY_CREDIT_BUDGET", "10") or 10)
EXTRA_CREDIT_BUDGET = int(env("EXTRA_CREDIT_BUDGET", "6") or 6)
FOOTBALL_DAILY_BUDGET = int(env("FOOTBALL_DAILY_BUDGET", "400") or 400)
STATS_MAX_MATCHES = int(env("STATS_MAX_MATCHES", "30") or 30)
CONTACT = env("CONTACT")
TZ = ZoneInfo("Asia/Baku")

ODDS_BASE = "https://api.the-odds-api.com/v4"
FOOTBALL_BASE = "https://v3.football.api-sports.io"
CREDIT_RESERVE = 5          # Odds API kreditinin bu qədəri həmişə ehtiyatda qalır
LOW_CREDIT_WARN = 60        # Odds API krediti bundan aşağı düşəndə admin xəbərdar olunur
RETRY_AFTER_FAIL = 10 * 60  # uğursuz yükləməni 10 dəq. sonra yenidən yoxla
STATS_RETRY = 15 * 60       # statistika uğursuz olubsa 15 dəq. sonra yenidən yoxla
MAX_FETCH_TRIES = 3         # natamam baza ən çox bu qədər yenidən yüklənir (kredit büdcəsi daxilində)
COOLDOWN = 2.0              # eyni istifadəçinin düymə basma fasiləsi (saniyə)
MIN_MINUTES_BEFORE_KICKOFF = 15  # başlamağa 15 dəq. qalmış oyunları kupona salma

# Korner / kart (Odds API: alternate_totals_corners / alternate_totals_cards, oyun-oyun sorğu)
EXTRA_MARKETS = "alternate_totals_corners,alternate_totals_cards"
EXTRA_LINES = {"corners": (7.5, 13.5), "cards": (2.5, 6.5)}   # məntiqli xətt aralığı
EXTRA_KINDS = {"corners_over", "corners_under", "cards_over", "cards_under"}
MAX_EXTRAS = 2              # bir kuponda ən çox korner/kart pick sayı
MAX_NOTES = 3               # bir kuponda ən çox analiz qeydi sayı

# Statistika təsdiqi hədləri (pick statistika ilə üst-üstə düşməlidir)
PROFILE_GAMES = 6           # komandanın son neçə oyunu (korner/kart üçün)
MIN_PROFILE_GAMES = 4       # ən azı bu qədər oyunun statistikası olmalıdır
GATE_WIN_MIN = 0.45         # 1 / 2 pick: modelin bu tərəfə ehtimalı ≥ 45%
GATE_WIN_GAP = 0.15         # ... və rəqibdən ən azı 15 punkt çox
GATE_DC_MIN = 0.70          # 1X / X2 / 12: modelin ikiqat şans ehtimalı ≥ 70%
GATE_FORM_LOSSES = 4        # son 5 oyunda ≥4 məğlubiyyəti olan komandaya qalib/məğlub-olmaz verilmir
GATE_GOALS_OVER = 2.7       # 2.5 Üst: gözlənən qol ≥ 2.7
GATE_GOALS_UNDER = 2.3      # 2.5 Alt: gözlənən qol ≤ 2.3
GATE_CORNER_MARGIN = 0.8    # korner: gözlənən dəyər xəttdən ən azı 0.8 fərqli
GATE_CARD_MARGIN = 0.6      # kart: gözlənən dəyər xəttdən ən azı 0.6 fərqli
FINISHED = {"FT", "AET", "PEN"}

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
#  extra_w         : {əlavə/aşağı liqadan neçə oyun icazəlidir: çəki}
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
            "🌐 Dil: /lang · 🔒 Məxfilik: /privacy\n\n"
            "⚠️ 18+ · Mərc risklidir, zəmanət yoxdur. Yalnız itirə biləcəyin məbləği qoy."
        ),
        "welcome_limit": "\n\n🎟 Hər gün {limit} kupon krediti verilir (Bakı vaxtı ilə 00:00-da yenilənir).",
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
        "extra_note": "ℹ️ Korner/kart xətləri və kefləri mərc şirkətlərində fərqli ola bilər. Mərcdən əvvəl yoxla.",
        "stats_note": "ℹ️ Statistika API-Football məlumatlarına əsaslanır (son oyunlar, mövsüm ortalaması). Zəmanət deyil.",
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
        # yalnız bazar əsaslı qeydlər (statistika rejimi söndürülü olanda)
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
        "a_stats_off": "📈 Statistika söndürülüb (API_FOOTBALL_KEY yoxdur) — kuponlar yalnız bazar kefinə əsaslanır.",
        "a_limit": "🎟 İstifadəçi limiti: gündə {n} kupon",
        "a_partial": "⚠️ Baza natamamdır (cəhd {t}/{m}) — bot özü yenidən yoxlayacaq.",
        "a_credits": "💳 Bu gün Odds API xərci: əsas {o}/{ob} · korner/kart {x}/{xb}",
        "a_snap_none": "⚽ Oyun bazası hələ yüklənməyib.",
        "a_none": "hələ yoxdur",
    },
    "en": {
        "welcome": (
            "👋 Hi! I'm a football coupon assistant.\n\n"
            "I build safe, normal and risky coupons from market odds and team statistics.\n\n"
            "🎯 Get a coupon: /coupon\n"
            "🌐 Language: /lang · 🔒 Privacy: /privacy\n\n"
            "⚠️ 18+ · Betting is risky, nothing is guaranteed. Only stake what you can afford to lose."
        ),
        "welcome_limit": "\n\n🎟 You get {limit} coupon credits per day (resets at 00:00 Baku time).",
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
        "extra_note": "ℹ️ Corner/card lines and odds may differ between bookmakers. Check before betting.",
        "stats_note": "ℹ️ Stats come from API-Football data (recent matches, season averages). Not a guarantee.",
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
        "a_stats_off": "📈 Stats disabled (no API_FOOTBALL_KEY) — coupons rely on market odds only.",
        "a_limit": "🎟 User limit: {n} coupons per day",
        "a_partial": "⚠️ Match data is incomplete (attempt {t}/{m}) — the bot will retry by itself.",
        "a_credits": "💳 Odds API spent today: main {o}/{ob} · corners/cards {x}/{xb}",
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
    info: object = None  # API-Football statistikası (dict) və ya None


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
    fb_calls: int = 0          # bu gün API-Football sorğu sayı
    fb_left: object = None     # API-Football qalan gündəlik sorğu
    fb_err: str = ""           # "plan" / "quota" / "auth" / "budget" və ya boş
    partial: bool = False      # bəzi sorğular uğursuz olub / oyun tapılmayıb → bir az sonra yenidən yoxlanacaq
    tries: int = 0             # bazanın neçə dəfə yüklənməsi cəhdi

    def to_json(self):
        return json.dumps(dict(
            day=self.day, spent=self.spent, remaining=self.remaining,
            leagues=self.leagues, ok=self.ok, extras=self.extras,
            stats_done=self.stats_done, fb_calls=self.fb_calls,
            fb_left=self.fb_left, fb_err=self.fb_err, partial=self.partial, tries=self.tries,
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
                        d.get("partial", False), d.get("tries", 0))


def _avg(values):
    return sum(values) / len(values)


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def discover_leagues():
    """Aktiv futbol liqaları + qalan kredit. /sports PULSUZDUR (kredit çəkmir)."""
    r = _get("/sports")
    r.raise_for_status()
    rem = None
    try:
        rem = int(float(r.headers.get("x-requests-remaining")))
    except (TypeError, ValueError):
        pass
    leagues = [(s["key"], s.get("title", s["key"])) for s in r.json()
               if s.get("key", "").startswith("soccer_")
               and not s.get("has_outrights") and s.get("active", True)]
    return leagues, rem


def league_events(key, t_from, t_to):
    """Liqanın pəncərədəki oyunları. /events PULSUZDUR. Xəta olsa None."""
    try:
        r = _get(f"/sports/{key}/events", commenceTimeFrom=_iso(t_from), commenceTimeTo=_iso(t_to))
        return r.json() if r.status_code == 200 else None
    except Exception:
        log.warning("%s | events xətası", key)
        return None


def fetch_day(prev=None):
    """
    Günün oyun bazasını qurur (prev: bu günün əvvəlki natamam bazası — artıq alınmış liqalar təkrar alınmır).
    Addımlar:
      1) aktiv liqaları tap (pulsuz)   2) hansında oyun var — yoxla (pulsuz)
      3) yalnız oyunu olan liqalar üçün kef çək (kredit), gündəlik büdcə daxilində.
    """
    now = datetime.now(TZ)
    t_to = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)   # bu günün sonu (Bakı)
    leagues, remaining = discover_leagues()
    with ThreadPoolExecutor(max_workers=8) as ex:
        found = list(ex.map(lambda kt: (kt, league_events(kt[0], now, t_to)), leagues))
    ev_failed = sum(1 for (k, _t), evs in found if evs is None and k in TOP_LEAGUES)
    active = [(k, t, evs) for (k, t), evs in found if evs]
    # Əvvəl məşhur liqalar, sonra oyun sayı çox olan əlavə liqalar
    active.sort(key=lambda x: (0, TOP_LEAGUES.index(x[0])) if x[0] in TOP_LEAGUES else (1, -len(x[2])))

    have = {m.league_key for m in prev.matches} if prev else set()
    matches = list(prev.matches) if prev else []
    spent, quota_out, fails = 0, False, 0
    budget = max(0, DAILY_CREDIT_BUDGET - credits_used("odds"))   # restart/təkrar cəhdlərdən asılı olmayaraq gündəlik tavan
    for key, title, _evs in active:
        if key in have:
            continue
        top = key in TOP_LEAGUES
        cost = 2 if top else 1
        if spent + cost > budget:
            continue
        if remaining is not None and remaining - cost < CREDIT_RESERVE:
            break
        try:
            r = _get(f"/sports/{key}/odds", regions="eu", oddsFormat="decimal",
                     markets="h2h,totals" if top else "h2h",
                     commenceTimeFrom=_iso(now), commenceTimeTo=_iso(t_to))
        except Exception:
            log.warning("%s | odds sorğusu uğursuz", key)
            fails += 1
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
            fails += 1
            continue
        spent += cost
        credits_add("odds", cost)
        try:
            matches += parse_league_odds(r.json(), key, title, top, now, t_to)
        except Exception:
            log.exception("%s | parse xətası", key)
    ok = bool(matches) or not quota_out
    partial = (not matches) or fails > 0 or ev_failed > 0
    tries = (prev.tries if prev else 0) + 1
    log.info("Baza: %d liqa aktiv, %d oyun, %d kredit xərcləndi, qalan=%s, uğursuz sorğu=%d, natamam=%s (cəhd %d)",
             len(active), len(matches), spent, remaining, fails + ev_failed, partial, tries)
    return Snapshot(now.date().isoformat(), matches, (prev.spent if prev else 0) + spent, remaining,
                    len(active), ok, extras=prev.extras if prev else False,
                    partial=partial, tries=tries)


def enrich_extras(snap):
    """
    Seçilmiş məşhur liqa oyunları üçün korner/kart kefləri (oyun-oyun sorğu, KREDİTLİDİR).
    Gündə yalnız bir dəfə icra olunur; büdcə EXTRA_CREDIT_BUDGET ilə məhdudlaşır.
    Bütün bookmaker-lər bu marketləri vermir — tapılmayan oyunlar sadəcə keçilir.
    """
    if snap.extras:
        return
    snap.extras = True   # nəticədən asılı olmayaraq təkrar kredit xərclənməsin
    budget = max(0, EXTRA_CREDIT_BUDGET - credits_used("extra"))
    if budget < 2:
        return
    now = datetime.now(TZ)
    pool = sorted((m for m in snap.matches
                   if m.top and m.start.date() == now.date() and m.start > now + timedelta(minutes=60)
                   and not any(l.kind in EXTRA_KINDS for l in m.legs)),
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
        rem = r.headers.get("x-requests-remaining")
        if rem is not None:
            try:
                remaining = int(float(rem))
            except ValueError:
                pass
        last = r.headers.get("x-requests-last")
        try:
            cost = int(float(last)) if last is not None else 2
        except ValueError:
            cost = 2
        log.info("extra | %s - %s | status=%s | xərc=%s | qalan=%s",
                 m.home, m.away, r.status_code, last, rem)
        if r.status_code == 401:
            break
        if r.status_code != 200:
            continue
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


# ============================== API-FOOTBALL (REAL STATİSTİKA) ==============================
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
            if "free plan" in text or "plan" in text and "season" in text:
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


def team_profile(api, team_id, season, day):
    """
    Komandanın son PROFILE_GAMES bitmiş oyununda orta korner/kart (qazandığı/buraxdığı).
    Oyun statistikası Redis-də uzun müddət keşlənir (bitmiş oyun dəyişmir).
    """
    key = f"tp:{team_id}:{day}"
    cached = _kvj_get(key)
    if cached is not None:
        return cached if cached.get("n") else None
    fx = api.get("/fixtures", team=team_id, last=8, season=season)
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
    season = (fx.get("league") or {}).get("season")
    hid = fx["teams"]["home"]["id"]
    aid = fx["teams"]["away"]["id"]
    info = parse_prediction(api.get("/predictions", fixture=fid), hid, aid)
    if not info:
        return None
    try:
        ph = team_profile(api, hid, season, day)
        pa = team_profile(api, aid, season, day)
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
    Oyunlara API-Football statistikası əlavə edir (gündə bir dəfə; nəticə Redis-də saxlanır).
    Eyniləşdirilə bilməyən və ya statistikası tapılmayan oyunlar info=None qalır və
    statistika rejimində kupona düşmür.
    """
    if not STATS_MODE:
        snap.stats_done = True
        return
    api = FootballAPI(FOOTBALL_KEY, FOOTBALL_DAILY_BUDGET)
    day = datetime.now(TZ).date().isoformat()
    cand = sorted((m for m in snap.matches
                   if m.start.date() == datetime.now(TZ).date() and not (m.info and m.info.get("ok"))),
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
    total_ok = sum(1 for m in snap.matches if m.info and m.info.get("ok"))
    snap.fb_calls += api.calls
    snap.fb_left = api.remaining
    snap.fb_err = api.stopped or ""
    snap.stats_done = (total_ok > 0 or api.stopped in ("plan", "auth")
                       or (api.failed == 0 and api.stopped is None))
    log.info("Statistika: %d/%d oyun təsdiqləndi (bu dəfə +%d, eyniləşməyən %d), %d sorğu, qalan=%s, xəta=%s",
             total_ok, len(snap.matches), verified, unmatched, api.calls, api.remaining, api.stopped)


_snap = {"day": None, "t": 0.0, "st": 0.0, "data": None, "ok": False}
_snap_lock = threading.Lock()


def _finish_snapshot(today, data):
    """Odds API korner/kart + API-Football statistika mərhələləri, sonra Redis-ə yazır."""
    try:
        enrich_extras(data)
    except Exception:
        log.exception("enrich_extras xətası")
        data.extras = True
    if not data.stats_done:
        try:
            enrich_stats(data)
        except Exception:
            log.exception("enrich_stats xətası")
    try:
        kv.set(f"snap:{today}", data.to_json(), ex=3 * 86400)
    except Exception:
        log.exception("Redis snapshot yazılmadı")


def _needs_refetch(s):
    """Uğursuz və ya natamam baza 10 dəq. sonra (ən çox MAX_FETCH_TRIES dəfə) yenidən yüklənir."""
    d = s["data"]
    age = time.time() - s["t"]
    if not s["ok"]:
        return age >= RETRY_AFTER_FAIL
    if d.partial and d.tries < MAX_FETCH_TRIES:
        return age >= RETRY_AFTER_FAIL
    return False


def get_snapshot():
    """Günlük keş. Redis varsa restartdan sonra kredit xərcləmədən oradan oxuyur."""
    today = datetime.now(TZ).date().isoformat()
    with _snap_lock:
        s = _snap
        if s["day"] == today and s["data"] is not None and not _needs_refetch(s):
            d = s["data"]
            if s["ok"] and STATS_MODE and not d.stats_done and time.time() - s["st"] > STATS_RETRY:
                s["st"] = time.time()          # statistika yarımçıq qalıbsa, arabir təkrar cəhd
                _finish_snapshot(today, d)
            return s["data"], s["ok"]
        if s["day"] != today:
            s["data"] = None
            try:
                raw = kv.get(f"snap:{today}")
                if raw:
                    data = Snapshot.from_json(raw)
                    if not data.extras or (STATS_MODE and not data.stats_done):
                        s["st"] = time.time()
                        _finish_snapshot(today, data)
                    s.update(day=today, t=time.time(), data=data, ok=True)
                    log.info("Baza Redis-dən yükləndi")
                    return s["data"], True
            except Exception:
                log.exception("Redis snapshot oxunmadı")
        prev = s["data"] if s["day"] == today else None
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
            s["ok"] = False  # köhnə (eyni günün) məlumatı varsa saxlanır
        return s["data"], s["ok"]


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


def stat_gate(m, leg):
    """
    Pick real statistika ilə üst-üstə düşürmü? Statistika rejimi söndürülübsə həmişə True.
    Statistikası olmayan oyun və ya kifayət qədər məlumat yoxdursa False (yəni pick verilmir).
    """
    if not STATS_MODE:
        return True
    info = m.info
    if not info or not info.get("ok"):
        return False
    k = leg.kind
    pct = info.get("pct")
    form = info.get("form") or ["", ""]
    if k in ("home", "away", "dc1x", "dcx2", "dc12"):
        if not pct:
            return False
        h, d, a = pct
        bad_home = form[0].count("L") >= GATE_FORM_LOSSES
        bad_away = form[1].count("L") >= GATE_FORM_LOSSES
        if k == "home":
            return h >= GATE_WIN_MIN and h - a >= GATE_WIN_GAP and not bad_home
        if k == "away":
            return a >= GATE_WIN_MIN and a - h >= GATE_WIN_GAP and not bad_away
        if k == "dc1x":
            return h + d >= GATE_DC_MIN and not bad_home
        if k == "dcx2":
            return a + d >= GATE_DC_MIN and not bad_away
        return h + a >= GATE_DC_MIN
    if k in ("over", "under"):
        e = expect_goals(info)
        if e is None:
            return False
        tot = e[0] + e[1]
        return tot >= GATE_GOALS_OVER if k == "over" else tot <= GATE_GOALS_UNDER
    if k in EXTRA_KINDS:
        key = "cor" if k.startswith("corners") else "crd"
        e = expect_pair(info, key)
        if e is None or leg.line is None:
            return False
        tot = e[0] + e[1]
        margin = GATE_CORNER_MARGIN if key == "cor" else GATE_CARD_MARGIN
        return tot >= leg.line + margin if k.endswith("over") else tot <= leg.line - margin
    return False


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
            # əsas marketlərə üstünlük: təxmini kef +0.10, korner/kart +0.04 cərimə
            leg = min(opts, key=lambda l: abs(math.log(l.odds) - math.log(target))
                      + (0.10 if l.approx else (0.04 if l.kind in EXTRA_KINDS else 0.0))
                      + rng.uniform(0, 0.06))
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
    Statistika rejimində yalnız statistikası təsdiqlənmiş oyunlar və statistika ilə
    üst-üstə düşən pick-lər nəzərə alınır.
    Eyni gün + eyni növ + eyni variant həmişə eyni kupon verir; 'Başqa variant' yenisini.
    """
    cfg = TIERS[tier]
    now = now or datetime.now(TZ)
    rng = random.Random(f"{now.date().isoformat()}|{tier}|{variant}")
    pool = [m for m in matches
            if m.start.date() == now.date()                                   # yalnız bu gün (Bakı vaxtı)
            and m.start > now + timedelta(minutes=MIN_MINUTES_BEFORE_KICKOFF)]
    cands = {}
    for m in pool:
        if STATS_MODE and not (m.info and m.info.get("ok")):
            continue                                                  # statistikası təsdiqlənməyib
        opts = [l for l in m.legs
                if l.kind != "draw"                                   # təkcə X yoxdur
                and (cfg["extras"] or l.kind not in EXTRA_KINDS)      # ehtiyatlıda korner/kart yoxdur
                and cfg["pick_lo"] <= l.odds <= cfg["pick_hi"]
                and stat_gate(m, l)]                                  # statistika ilə təsdiq
        if opts:
            cands[m.id] = (m, opts)
    ids = list(cands)
    log.info("kupon %s | bazada %d oyun, vaxtı uyğun %d, namizəd %d (minimum %d)",
             tier, len(matches), len(pool), len(ids), cfg["min_legs"])
    if len(ids) < cfg["min_legs"]:      # növün vəd etdiyi oyun sayından az oyunla kupon verilmir
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


def count_upcoming(matches, now=None):
    """Bu gün hələ başlamamış (15 dəq. qalmış çıxmaqla) oyun sayı."""
    now = now or datetime.now(TZ)
    return sum(1 for m in matches
               if m.start.date() == now.date() and m.start > now + timedelta(minutes=MIN_MINUTES_BEFORE_KICKOFF))


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
    Analiz qeydi. Statistika varsa: rəqəmlər birbaşa API-Football məlumatından hesablanır.
    Statistika yoxdursa (rejim söndürülüb): yalnız bazar ehtimalı, ehtimal həddindən yuxarı olanda.
    """
    k = leg.kind
    info = m.info if (m.info and m.info.get("ok")) else None
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
    if STATS_MODE and any(i in notes and c.picks[i][0].info for i in range(len(c.picks))):
        lines.append(L["stats_note"])
    lines.append("")
    lines.append(L["disclaimer"])
    if left is not None and limit:
        lines.append("")
        lines.append(L["left"].format(left=left, limit=limit))
        if left == 0:
            lines.append(L["left_last"])
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
            out.append(L["a_partial"].format(t=snap.tries, m=MAX_FETCH_TRIES))
        if STATS_MODE:
            v = sum(1 for m in snap.matches if m.info and m.info.get("ok"))
            err = f" · ⚠️ {snap.fb_err}" if snap.fb_err else ""
            fl = snap.fb_left if snap.fb_left is not None else "?"
            out.append(L["a_stats"].format(v=v, m=len(snap.matches), c=snap.fb_calls, r=fl, err=err))
    else:
        out.append(L["a_snap_none"])
    if not STATS_MODE:
        out.append(L["a_stats_off"])
    out.append(L["a_limit"].format(n=USER_DAILY_LIMIT))
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
    text = L["welcome"]
    if USER_DAILY_LIMIT > 0:
        text += L["welcome_limit"].format(limit=USER_DAILY_LIMIT)
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
        snap, ok = await asyncio.to_thread(get_snapshot)
        if snap is None:
            await alert_admin(context, "⚠️ Kupon botu: oyun məlumatı alınmır. Odds API limitini/Logs-u yoxla.")
            await status.edit_text(L["data_error"])
            return
        warns = []                                   # adminə xəbərdarlıqlar
        if snap.remaining is not None and snap.remaining < LOW_CREDIT_WARN:
            warns.append(f"Odds API krediti azalıb: {snap.remaining} qalıb")
        if snap.fb_err == "plan":
            warns.append("API-Football planı cari mövsümü vermir → Pro plana keç")
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
                    txt = L["no_matches_stats"] if STATS_MODE else L["no_matches"]
                else:
                    txt = L["no_tier"].format(name=L["name_" + tier])
                await status.edit_text(txt)
            return
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
    log.info("Statistika rejimi: %s | istifadəçi limiti: %s/gün | Odds büdcə: %s+%s/gün",
             "AÇIQ (API-Football)" if STATS_MODE else "SÖNDÜRÜLÜB (yalnız bazar kefi)",
             USER_DAILY_LIMIT or "limitsiz", DAILY_CREDIT_BUDGET, EXTRA_CREDIT_BUDGET)
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
    app.add_handler(CommandHandler("privacy", cmd_privacy))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    # drop_pending_updates: restartda köhnə yığılmış mesajlara cavab yağdırmasın
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
