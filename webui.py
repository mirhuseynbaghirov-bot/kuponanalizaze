"""
webui.py — ProqnozAze saytı. Telegram botunun yanında, eyni serverdə işləyir.
Yeni API sorğusu yaratmır: proqnoz.get_cards() botun artıq topladığı datanı (5 dəq. keş) göstərir.
"""
import json
import logging
import time

log = logging.getLogger("webui")

_hits = {}          # ip -> (pəncərə başlanğıcı, say)
RATE = 90           # 1 dəqiqədə bir IP-dən maks. sorğu (yalnız /api)


def _limited(ip):
    now = time.time()
    t0, n = _hits.get(ip, (now, 0))
    if now - t0 > 60:
        t0, n = now, 0
    _hits[ip] = (t0, n + 1)
    if len(_hits) > 5000:
        _hits.clear()
    return n + 1 > RATE


def handle(path, ip="?"):
    """(status, content_type, bytes) qaytarır. None = sayt bu ünvanı tanımır."""
    path = path.split("?", 1)[0]
    if path in ("/", "/index.html"):
        return 200, "text/html; charset=utf-8", PAGE.encode("utf-8")
    if path == "/api/proqnoz":
        if _limited(ip):
            return 429, "application/json", b'{"error":"cox sorgu"}'
        try:
            import proqnoz
            cards = proqnoz.get_cards()
        except Exception:
            log.exception("webui | kartlar alınmadı")
            return 500, "application/json", b'{"error":"xeta"}'
        body = {"ready": cards is not None, "cards": cards or [], "t": int(time.time())}
        return 200, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False).encode("utf-8")
    if path == "/robots.txt":
        return 200, "text/plain", b"User-agent: *\nAllow: /\n"
    return None


PAGE = r"""<!doctype html>
<html lang="az">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ProqnozAze — Günün futbol proqnozları</title>
<meta name="description" content="ProqnozAze: günün futbol oyunları üçün statistika və bazar əsaslı proqnozlar. Bu qumar tövsiyəsi deyil.">
<meta name="theme-color" content="#0b0b0d">
<style>
:root{
  --bg:#0b0b0d; --bg2:#141418; --card:#17171c; --line:#2a2a31;
  --red:#e11d2e; --red2:#ff3b4a; --txt:#f4f4f5; --mut:#9a9aa5;
  --ok:#2ecc71; --warn:#f5b301; --stop:#ff4d5a;
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{background:var(--bg);color:var(--txt);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  background-image:radial-gradient(900px 400px at 85% -10%,rgba(225,29,46,.18),transparent 60%)}
.wrap{max-width:980px;margin:0 auto;padding:0 16px}
header{padding:22px 0 14px;display:flex;align-items:center;justify-content:space-between;gap:12px}
.logo{font-size:28px;font-weight:900;letter-spacing:-.5px}
.logo b{color:var(--red)}
.logo small{display:block;font-size:12px;font-weight:500;color:var(--mut);letter-spacing:0;margin-top:2px}
.age{border:2px solid var(--red);color:var(--red2);font-weight:800;border-radius:999px;padding:4px 10px;font-size:13px}
.disc{background:linear-gradient(90deg,#2a0c10,#1a1013);border:1px solid #4a1a20;border-left:4px solid var(--red);
  border-radius:10px;padding:10px 12px;color:#ffd7da;font-size:13px;margin-bottom:14px}
.disc b{color:#fff}
.hero{padding:6px 0 14px}
.hero h1{font-size:22px;line-height:1.2}
.hero p{color:var(--mut);margin-top:4px;font-size:14px}
.filters{position:sticky;top:0;z-index:5;background:rgba(11,11,13,.92);backdrop-filter:blur(8px);
  padding:10px 0;border-bottom:1px solid var(--line);margin-bottom:14px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.row+.row{margin-top:8px}
input[type=search],select{background:var(--bg2);border:1px solid var(--line);color:var(--txt);border-radius:10px;
  padding:9px 12px;font-size:14px;outline:none;min-width:0}
input[type=search]{flex:1 1 180px}
input:focus,select:focus{border-color:var(--red)}
.chips{display:flex;gap:6px;overflow-x:auto;padding-bottom:2px;scrollbar-width:none}
.chips::-webkit-scrollbar{display:none}
.chip{flex:0 0 auto;border:1px solid var(--line);background:var(--bg2);color:var(--txt);border-radius:999px;
  padding:6px 12px;font-size:13px;cursor:pointer;user-select:none}
.chip.on{background:var(--red);border-color:var(--red);color:#fff;font-weight:700}
.toggle{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--mut);cursor:pointer;user-select:none}
.toggle input{accent-color:var(--red);width:18px;height:18px}
.count{color:var(--mut);font-size:13px;margin:0 0 10px}
.grid{display:grid;grid-template-columns:1fr;gap:12px}
@media(min-width:760px){.grid{grid-template-columns:1fr 1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;overflow:hidden}
.card.agree{border-color:#1f6b3f}
.card.conflict,.card.undecided{border-color:#3a3a42}
.ch{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;background:var(--bg2);
  font-size:12.5px;color:var(--mut)}
.time{color:#fff;font-weight:800;background:var(--red);border-radius:6px;padding:2px 8px;font-size:13px}
.teams{padding:12px 14px 6px;font-size:18px;font-weight:800;line-height:1.25}
.teams span{color:var(--mut);font-weight:500;font-size:14px;padding:0 4px}
.sec{padding:8px 14px}
.sec h4{font-size:12px;letter-spacing:.6px;text-transform:uppercase;color:var(--mut);margin-bottom:5px;font-weight:700}
.sec p{font-size:14px}
.bar{display:flex;height:8px;border-radius:99px;overflow:hidden;margin:7px 0 3px;background:#25252b}
.bar i{display:block;height:100%}
.bar i:nth-child(1){background:var(--red)}
.bar i:nth-child(2){background:#6b6b75}
.bar i:nth-child(3){background:#f4f4f5}
.lg{display:flex;justify-content:space-between;font-size:12px;color:var(--mut)}
.form{display:flex;gap:4px;align-items:center;margin-top:6px;font-size:12px;color:var(--mut)}
.f{display:inline-block;width:20px;height:20px;border-radius:5px;text-align:center;line-height:20px;
  font-weight:800;font-size:11px;color:#111}
.f.W{background:var(--ok)}.f.D{background:var(--warn)}.f.L{background:var(--stop);color:#fff}
.sep{height:1px;background:var(--line);margin:4px 14px}
.final{margin:8px 12px 12px;border-radius:12px;padding:10px 12px;background:#101014;border:1px solid var(--red)}
.final h4{color:var(--red2);font-size:13px;letter-spacing:.8px;margin-bottom:6px}
.fi{display:flex;gap:8px;font-size:14px;padding:3px 0}
.fi.ok{color:#c9f7da}.fi.part{color:#ffe9a6}.fi.warn{color:#ffd48a}.fi.stop{color:#ffb3b9}
.badge{display:inline-block;font-size:12px;font-weight:700;border-radius:6px;padding:2px 8px;margin-left:6px}
.b-agree{background:#123a25;color:#7ff0ad}.b-lean{background:#3a3010;color:#ffd866}
.b-conflict,.b-undecided,.b-nodata{background:#2a2a30;color:#c8c8d0}
.note{padding:0 14px 12px;color:#74747e;font-size:11.5px}
.empty{text-align:center;color:var(--mut);padding:50px 10px}
.spin{width:30px;height:30px;border:3px solid #2a2a31;border-top-color:var(--red);border-radius:50%;
  margin:0 auto 12px;animation:r 1s linear infinite}
@keyframes r{to{transform:rotate(360deg)}}
footer{margin:30px 0 40px;padding-top:18px;border-top:1px solid var(--line);color:var(--mut);font-size:12.5px}
footer p{margin-bottom:8px}
footer b{color:#ffd7da}
.hide{display:none!important}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">Proqnoz<b>Aze</b><small>Günün futbol proqnozları · Bakı vaxtı ilə</small></div>
    <div class="age">18+</div>
  </header>

  <div class="disc">⚠️ <b>Bu bir qumar tövsiyəsi deyildir.</b> Qumar oynamamaq sizin üçün daha uyğundur.
    Bu qumar reklamı deyil — yalnız statistika əsaslı proqnozdur və heç bir nəticəyə zəmanət vermir.</div>

  <div class="hero">
    <h1>Bu günün oyunları — bot və bazar nə deyir?</h1>
    <p>Hər oyun üçün iki baxış: botun statistika analizi və bazarın kefləri. Sonda <b style="color:#fff">yekun qərar</b>.</p>
  </div>

  <div class="filters">
    <div class="row">
      <input type="search" id="q" placeholder="Komanda axtar…" autocomplete="off">
      <select id="sort">
        <option value="time">Saata görə</option>
        <option value="conf">Ən əmin olanlar əvvəl</option>
      </select>
    </div>
    <div class="row"><div class="chips" id="leagues"></div></div>
    <div class="row">
      <label class="toggle"><input type="checkbox" id="onlyAgree"> Yalnız bot və bazar razı olanlar</label>
      <label class="toggle"><input type="checkbox" id="hideBad"> Qərarsızları gizlət</label>
      <select id="when">
        <option value="all">Bütün saatlar</option>
        <option value="day">Gündüz (10–18)</option>
        <option value="eve">Axşam (18–22)</option>
        <option value="night">Gecə (22+)</option>
      </select>
    </div>
  </div>

  <div class="count" id="count"></div>
  <div class="grid" id="grid"></div>
  <div class="empty" id="state"><div class="spin"></div>Yüklənir…</div>

  <footer>
    <p><b>Məsuliyyət xəbərdarlığı:</b> Bu bir qumar tövsiyəsi deyildir. Qumar oynamamaq sizin üçün daha uyğundur.
       Bu qumar reklamı deyil, yalnız proqnozdur.</p>
    <p>Proqnozlar statistik model və bazar kefləri əsasında avtomatik hesablanır, səhv ola bilər. Nəticəyə zəmanət yoxdur.
       Qərarsız oyunu oynama. Maliyyə itkisi risk daşıyır. Yalnız 18 yaşdan yuxarı olanlar üçündür.</p>
    <p>Qumar asılılığı problem olarsa, ixtisaslı mütəxəssisə müraciət edin.</p>
    <p>© ProqnozAze</p>
  </footer>
</div>

<script>
const $ = s => document.querySelector(s);
let ALL = [], LEAGUE = "", timer = null;

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const BADGE = {
  agree: ["b-agree", "Bot və bazar razıdır"],
  lean: ["b-lean", "Meyil var, əmin deyil"],
  conflict: ["b-conflict", "Fikir ayrılığı"],
  undecided: ["b-undecided", "Qərarsız"],
  nodata: ["b-nodata", "Bazar məlumatı"]
};
const ICON = {ok: "✅", part: "🔸", warn: "⚠️", stop: "⛔"};

function bar(p){
  if(!p) return "";
  return `<div class="bar"><i style="width:${p[0]}%"></i><i style="width:${p[1]}%"></i><i style="width:${p[2]}%"></i></div>
  <div class="lg"><span>1 · ${p[0]}%</span><span>X · ${p[1]}%</span><span>2 · ${p[2]}%</span></div>`;
}
function form(f, h, a){
  if(!f) return "";
  const one = s => [...s].map(c => `<span class="f ${c}">${c}</span>`).join("");
  return `<div class="form" style="flex-wrap:wrap">Son 5: <span>${esc(h)}</span> ${one(f[0])} <span style="margin-left:6px">${esc(a)}</span> ${one(f[1])}</div>`;
}

function card(c){
  const [bc, bt] = BADGE[c.agree] || BADGE.nodata;
  let bot = `<div class="sec"><h4>🤖 Botun analizi</h4><p>${esc(c.bot.text)}</p>${bar(c.bot.pct)}`;
  if(c.bot.form) bot += form(c.bot.form, c.home, c.away);
  if(c.bot.goals && c.bot.goals.length) bot += `<p style="margin-top:6px;color:var(--mut);font-size:13px">⚽ ${esc(c.bot.goals.join(" · "))} (gözlənən ~${c.bot.exp_goals})</p>`;
  bot += `</div>`;
  let mk = "";
  if(c.market){
    mk = `<div class="sep"></div><div class="sec"><h4>👥 İstifadəçilərin seçdikləri (bazar)</h4>
      <p>${esc(c.market.text)}</p>${bar(c.market.pct)}</div>`;
  }
  const fin = c.final.map(f => `<div class="fi ${f.level}"><span>${ICON[f.level] || "🔸"}</span><span>${esc(f.text)}</span></div>`).join("");
  return `<article class="card ${c.agree}">
    <div class="ch"><span>🏟 ${esc(c.league)}</span><span class="time">${esc(c.time)}</span></div>
    <div class="teams">${esc(c.home)}<span>vs</span>${esc(c.away)}</div>
    <div class="sec" style="padding-top:2px"><span class="badge ${bc}" style="margin-left:0">${bt}</span></div>
    ${bot}${mk}
    <div class="final"><h4>🏁 YEKUN QƏRAR</h4>${fin}</div>
    <div class="note">Bu proqnozdur, qumar tövsiyəsi deyil. Qərarsız oyunu oynama. 18+</div>
  </article>`;
}

function render(){
  const q = $("#q").value.trim().toLowerCase();
  const onlyAgree = $("#onlyAgree").checked, hideBad = $("#hideBad").checked;
  const when = $("#when").value, sort = $("#sort").value;
  let list = ALL.filter(c => {
    if(LEAGUE && c.league !== LEAGUE) return false;
    if(q && !(c.home + " " + c.away).toLowerCase().includes(q)) return false;
    if(onlyAgree && c.agree !== "agree") return false;
    if(hideBad && (c.agree === "undecided" || c.agree === "nodata" || c.agree === "conflict")) return false;
    const h = parseInt(c.time.slice(0, 2), 10);
    if(when === "day" && !(h >= 10 && h < 18)) return false;
    if(when === "eve" && !(h >= 18 && h < 22)) return false;
    if(when === "night" && !(h >= 22 || h < 6)) return false;
    return true;
  });
  if(sort === "conf") list.sort((a, b) => b.score - a.score || a.ts.localeCompare(b.ts));
  $("#grid").innerHTML = list.map(card).join("");
  $("#count").textContent = list.length ? `${list.length} oyun göstərilir` : "";
  const st = $("#state");
  if(list.length){ st.classList.add("hide"); }
  else { st.classList.remove("hide"); st.innerHTML = ALL.length ? "Bu filtrlərə uyğun oyun tapılmadı." : "Bu gün analiz ediləcək oyun qalmayıb."; }
}

function buildLeagues(){
  const names = [...new Set(ALL.map(c => c.league))].sort();
  const box = $("#leagues");
  box.innerHTML = [`<span class="chip ${LEAGUE ? "" : "on"}" data-l="">Hamısı</span>`]
    .concat(names.map(n => `<span class="chip ${LEAGUE === n ? "on" : ""}" data-l="${esc(n)}">${esc(n)}</span>`)).join("");
  box.querySelectorAll(".chip").forEach(el => el.onclick = () => { LEAGUE = el.dataset.l; buildLeagues(); render(); });
}

async function load(){
  try{
    const r = await fetch("/api/proqnoz", {cache: "no-store"});
    if(r.status === 429){ return; }
    const d = await r.json();
    if(!d.ready){
      $("#state").classList.remove("hide");
      $("#state").innerHTML = '<div class="spin"></div>Baza hazırlanır, 1–2 dəqiqə sonra avtomatik yenilənəcək…';
      clearTimeout(timer); timer = setTimeout(load, 20000); return;
    }
    ALL = d.cards; buildLeagues(); render();
  }catch(e){
    $("#state").classList.remove("hide");
    $("#state").textContent = "Bağlantı xətası. Bir az sonra yenidən cəhd edin.";
  }
  clearTimeout(timer); timer = setTimeout(load, 5 * 60 * 1000);
}
["q", "onlyAgree", "hideBad", "when", "sort"].forEach(id => $("#" + id).addEventListener(id === "q" ? "input" : "change", render));
load();
</script>
</body>
</html>
"""
