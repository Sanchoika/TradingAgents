"""The paper test's dashboard: a static, read-only site built from a snapshot.

``build_snapshot`` (snapshot.py) reads the saved files; this module turns the
snapshot into a small site folder:

    site/index.html            the page (data embedded, works offline)
    site/data.json             the same data, re-read by the page when served
    site/reports/*.html        copies of the full analysis reports
    site/manifest.webmanifest  phone home-screen support (when served over HTTPS)
    site/sw.js, icon.svg

plus ``dashboard.html`` next to the journal (same page, for double-clicking).
Opening or refreshing the page only reads these files: it cannot settle
orders, run analyses or touch the paper account.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

TARGET_TRADES = 150


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:80]


def publishable(snapshot: dict, report_base: str = "reports/") -> dict:
    """The snapshot as shipped to the browser: local file paths replaced by site links."""
    data = json.loads(json.dumps(snapshot, default=str))
    for d in data.get("decisions", []):
        src = d.pop("report_src", None)
        d["report_href"] = (report_base + _safe_name(f"{d.get('trade_date')}_{d.get('symbol')}") + Path(src).suffix
                            if src else None)
    return data


def _json_for_script(data: dict) -> str:
    # Safe inside <script type="application/json">: no "</" can end the tag early.
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/").replace(
        "<!--", "<\\!--")


def render(snapshot: dict, report_base: str = "reports/") -> str:
    data = publishable(snapshot, report_base)
    return _PAGE.replace("__DATA__", _json_for_script(data)).replace("__TARGET__", str(TARGET_TRADES))


_ICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" '
         'fill="#11161f"/><path d="M12 44 L26 30 L36 38 L52 20" fill="none" stroke="#7aa2ff" stroke-width="6" '
         'stroke-linecap="round" stroke-linejoin="round"/></svg>')
_MANIFEST = {"name": "TradingAgents paper test", "short_name": "TA paper", "start_url": "./", "display": "standalone",
             "background_color": "#0d1117", "theme_color": "#0d1117",
             "icons": [{"src": "icon.svg", "sizes": "any", "type": "image/svg+xml"}]}
# Network first, cache as fallback: an offline phone still opens the last copy, and the
# page itself always shows when that copy was built and whether it is stale.
_SW = """const C='ta-dash-v1';
self.addEventListener('install',e=>{self.skipWaiting();e.waitUntil(caches.open(C).then(c=>c.addAll(['./','index.html','data.json','icon.svg'])).catch(()=>{}))});
self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));
self.addEventListener('fetch',e=>{if(e.request.method!=='GET')return;
e.respondWith(fetch(e.request).then(r=>{const k=r.clone();caches.open(C).then(c=>c.put(e.request,k));return r}).catch(()=>caches.match(e.request,{ignoreSearch:true})))});
"""


def write_site(snapshot: dict, out_dir: str | Path, legacy_html: str | Path | None = None) -> Path:
    """Write the site folder (and optionally the single-file copy). Returns index.html."""
    out = Path(out_dir)
    (out / "reports").mkdir(parents=True, exist_ok=True)
    for d in snapshot.get("decisions", []):
        src = d.get("report_src")
        if src and Path(src).exists():
            dst = out / "reports" / (_safe_name(f"{d.get('trade_date')}_{d.get('symbol')}") + Path(src).suffix)
            if not dst.exists() or dst.stat().st_mtime < Path(src).stat().st_mtime:
                shutil.copyfile(src, dst)
    index = out / "index.html"
    tmp = out / "index.html.tmp"
    tmp.write_text(render(snapshot, "reports/"), encoding="utf-8")
    (out / "data.json.tmp").write_text(json.dumps(publishable(snapshot, "reports/"), default=str), encoding="utf-8")
    (out / "data.json.tmp").replace(out / "data.json")
    tmp.replace(index)
    (out / "manifest.webmanifest").write_text(json.dumps(_MANIFEST), encoding="utf-8")
    (out / "icon.svg").write_text(_ICON, encoding="utf-8")
    (out / "sw.js").write_text(_SW, encoding="utf-8")
    if legacy_html:
        legacy = Path(legacy_html)
        rel = Path(out).resolve().relative_to(legacy.parent.resolve()).as_posix() if \
            Path(out).resolve().is_relative_to(legacy.parent.resolve()) else Path(out).resolve().as_uri()
        legacy.write_text(render(snapshot, f"{rel}/reports/"), encoding="utf-8")
    return index


def write_dashboard(cfg, prices: bool = True, schedule: bool = True) -> Path:
    """Build the snapshot from saved files and write the site. Returns dashboard.html."""
    from .snapshot import build_snapshot

    jdir = Path(cfg.journal_dir)
    snap = build_snapshot(cfg, prices=prices, schedule=schedule)
    write_site(snap, jdir / "site", jdir / "dashboard.html")
    return jdir / "dashboard.html"


_PAGE = r"""<!doctype html>
<html lang="en" data-theme="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<meta name="theme-color" content="#0d1117">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="TA paper">
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" href="icon.svg" type="image/svg+xml">
<title>TradingAgents paper test</title>
<style>
:root{--bg:#0d1117;--panel:#141a23;--panel2:#1a212c;--ink:#e6e9ef;--muted:#9aa4b2;--faint:#6b7685;--line:#232c39;
--accent:#7aa2ff;--pos:#4cc38a;--neg:#ff7b72;--warn:#e3b341;--chip:#202835;--bench:#8b95a5;
--shadow:0 1px 0 rgba(255,255,255,.03) inset}
:root[data-theme="light"]{--bg:#f5f6f8;--panel:#fff;--panel2:#f8f9fb;--ink:#151a21;--muted:#5b6574;--faint:#7c8695;
--line:#e3e6eb;--accent:#2f5bd3;--pos:#11804a;--neg:#c4302b;--warn:#9a6700;--chip:#eef1f5;--bench:#7c8695;--shadow:none}
*{box-sizing:border-box}html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:15px/1.5 Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
-webkit-text-size-adjust:100%;padding-bottom:env(safe-area-inset-bottom)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.wrap{max-width:1160px;margin:0 auto;padding:0 16px}
header.top{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--bg) 88%,transparent);
backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.bar1{display:flex;align-items:center;gap:12px;padding:12px 0 8px;flex-wrap:wrap}
.brand{font-weight:650;font-size:16px;letter-spacing:-.01em}
.paper{font-weight:700;font-size:11px;letter-spacing:.08em;padding:3px 8px;border-radius:6px;
background:var(--warn);color:#111}
.fresh{color:var(--muted);font-size:13px}
#theme{margin-left:auto}
.iconbtn{background:none;border:1px solid var(--line);color:var(--muted);border-radius:8px;padding:4px 8px;cursor:pointer;font:inherit;font-size:13px}
nav.tabs{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none}
nav.tabs a{padding:8px 12px;color:var(--muted);border-bottom:2px solid transparent;white-space:nowrap;font-size:14px}
nav.tabs a.on{color:var(--ink);border-color:var(--accent)}nav.tabs a:hover{text-decoration:none;color:var(--ink)}
main{padding:20px 0 48px}
.banner{border:1px solid var(--line);border-left:4px solid var(--warn);background:var(--panel);padding:10px 14px;
border-radius:10px;margin-bottom:14px;font-size:14px}
.banner.err{border-left-color:var(--neg)}.banner.info{border-left-color:var(--accent)}
h2{font-size:13px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin:0 0 10px;font-weight:600}
h3{font-size:15px;margin:0 0 8px}
.grid{display:grid;gap:12px}.g4{grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.g2{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;box-shadow:var(--shadow);min-width:0}
.kpi .l{color:var(--muted);font-size:13px;display:flex;align-items:center;gap:6px}
.kpi .v{font-size:24px;font-weight:650;margin:2px 0;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.kpi .s{color:var(--faint);font-size:12px}
.na{color:var(--faint);font-weight:500;font-size:15px}
.pos{color:var(--pos)}.neg{color:var(--neg)}.warn{color:var(--warn)}.muted{color:var(--muted)}.faint{color:var(--faint)}
.status{font-size:16px;line-height:1.55;margin:0}
.section{margin-top:14px}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:14px;font-variant-numeric:tabular-nums}
th{text-align:left;color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em;
padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap;vertical-align:top}
tr:last-child td{border-bottom:0}td.wrap{white-space:normal;min-width:180px}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;background:var(--chip);color:var(--ink);white-space:nowrap}
.r-Buy,.r-Overweight{background:color-mix(in srgb,var(--pos) 22%,transparent);color:var(--pos)}
.r-Sell,.r-Underweight{background:color-mix(in srgb,var(--neg) 22%,transparent);color:var(--neg)}
.r-Hold{background:var(--chip);color:var(--muted)}.r-REVIEW,.st-failed{background:color-mix(in srgb,var(--warn) 22%,transparent);color:var(--warn)}
.long{color:var(--pos)}.short{color:var(--neg)}
.empty{padding:28px 12px;text-align:center;color:var(--muted)}
.empty b{display:block;color:var(--ink);font-size:15px;margin-bottom:4px}
.tip{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;
border:1px solid var(--faint);color:var(--faint);font-size:10px;cursor:help;background:none;padding:0;line-height:1;font-family:inherit}
.pop{position:absolute;z-index:20;max-width:280px;background:var(--panel2);border:1px solid var(--line);color:var(--ink);
padding:10px 12px;border-radius:10px;font-size:13px;line-height:1.45;box-shadow:0 8px 24px rgba(0,0,0,.35)}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.filters input,.filters select{background:var(--panel2);color:var(--ink);border:1px solid var(--line);border-radius:8px;
padding:7px 10px;font:inherit;font-size:14px;min-width:0}
.filters input[type=search]{flex:1 1 160px}
.dec{border-top:1px solid var(--line);padding:12px 0}.dec:first-child{border-top:0}
.dec summary{cursor:pointer;list-style:none;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.dec summary::-webkit-details-marker{display:none}
.dec .meta{color:var(--muted);font-size:13px}
.dec .body{margin-top:10px}
pre{white-space:pre-wrap;word-break:break-word;font:13px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;
background:var(--panel2);border:1px solid var(--line);padding:12px;border-radius:8px;max-height:420px;overflow:auto;margin:8px 0 0}
dl.kv{display:grid;grid-template-columns:minmax(120px,max-content) 1fr;gap:6px 14px;margin:0;font-size:14px}
dl.kv dt{color:var(--muted)}dl.kv dd{margin:0;min-width:0;overflow-wrap:anywhere}
svg.chart{width:100%;height:auto;display:block}.gl{stroke:var(--line)}.ax{fill:var(--faint);font-size:11px}
.ln{fill:none;stroke-width:2.2;stroke-linejoin:round;stroke-linecap:round}.ln.a{stroke:var(--accent)}
.ln.b{stroke:var(--bench);stroke-dasharray:5 4}.ln.d{stroke:var(--neg)}.zero{stroke:var(--faint);stroke-dasharray:2 4}
.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:13px;margin-top:6px}
.key{display:inline-block;width:14px;height:3px;border-radius:2px;margin-right:6px;vertical-align:middle}
.feed{font-size:14px}.feed .row{display:grid;grid-template-columns:150px 1fr;gap:10px;padding:8px 0;border-top:1px solid var(--line)}
.feed .row:first-child{border-top:0}.feed .ev{font-weight:600}
.badge{font-size:11px;padding:1px 6px;border-radius:5px;background:var(--chip);color:var(--muted);margin-left:6px}
.progress{height:6px;background:var(--chip);border-radius:3px;overflow:hidden;margin-top:8px}.progress div{height:100%;background:var(--accent)}
footer{color:var(--faint);font-size:12px;padding:12px 0 32px;border-top:1px solid var(--line)}
.sr{position:absolute;left:-9999px}
.sm{display:none}.mlist{display:none}.mcard{border-top:1px solid var(--line);padding:12px 0}.mcard:first-child{border-top:0}
.mcard .top{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.mcard dl{margin-top:8px;font-size:13.5px}.kpi .v.small{font-size:17px;line-height:1.35}
footer{margin-top:8px}
@media (max-width:640px){.kpi .v{font-size:20px}.feed .row{grid-template-columns:1fr;gap:2px}.fresh{width:100%;order:5}
.g4{grid-template-columns:repeat(2,minmax(0,1fr))}.lg{display:none}.sm{display:inline}nav.tabs a{padding:8px 7px;font-size:13.5px}.mlist{display:block}.desk{display:none}.kpi .s{font-size:11.5px}
.card{padding:14px}main{padding-top:14px}}
@media print{header.top{position:static}nav.tabs{display:none}}
</style></head>
<body>
<header class="top"><div class="wrap">
 <div class="bar1"><span class="brand">TradingAgents</span><span class="paper" title="Simulated money only; no real orders">PAPER MODE</span>
 <button class="iconbtn" id="theme" type="button" aria-label="Switch light or dark theme">Theme</button>
 <span class="fresh" id="fresh">Loading…</span></div>
 <nav class="tabs" id="tabs" aria-label="Sections">
  <a href="#overview">Overview</a><a href="#positions">Positions</a><a href="#decisions"><span class="lg">AI </span>Decisions</a>
  <a href="#performance"><span class="lg">Performance</span><span class="sm">Results</span></a><a href="#activity"><span class="lg">Activity &amp; status</span><span class="sm">Status</span></a></nav>
</div></header>
<main class="wrap" id="main" aria-live="polite"><div class="empty"><b>Loading dashboard…</b>Reading the saved snapshot.</div></main>
<footer class="wrap" id="foot"></footer>
<noscript><div class="wrap banner err">This dashboard needs JavaScript to draw its pages.</div></noscript>
<script id="snapshot" type="application/json">__DATA__</script>
<script>
(function(){
"use strict";
const TARGET=__TARGET__, ET="America/New_York";
let D=null, SOURCE="embedded", LOADERR=null;
const $=s=>document.querySelector(s);
const esc=v=>String(v==null?"":v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const NA=(why)=>`<span class="na" title="${esc(why||"Not recorded")}">${esc(why||"not recorded")}</span>`;
const num=(x,d=0)=>x==null||isNaN(x)?null:Number(x).toLocaleString("en-US",{minimumFractionDigits:d,maximumFractionDigits:d});
const money=(x,sign)=>{if(x==null||isNaN(x))return null;const r=Math.round(x)||0;
 return (sign&&r>0?"+":"")+(r<0?"−":"")+"$"+Math.abs(r).toLocaleString("en-US")};
const cls=x=>x>0?"pos":x<0?"neg":"";
const fmtF=new Intl.DateTimeFormat("en-US",{timeZone:ET,year:"numeric",month:"short",day:"numeric",hour:"numeric",minute:"2-digit"});
const fmtD=new Intl.DateTimeFormat("en-US",{timeZone:ET,weekday:"short",month:"short",day:"numeric"});
function t(iso){if(!iso)return NA();const d=new Date(iso);if(isNaN(d))return esc(iso);
 return `<span title="${esc(d.toLocaleString())} (your time)">${esc(fmtF.format(d))} ET</span>`}
function ago(iso){if(!iso)return "";const s=(Date.now()-new Date(iso))/1000;if(isNaN(s))return "";
 if(s<90)return "just now";if(s<5400)return Math.round(s/60)+" min ago";if(s<172800)return Math.round(s/3600)+" h ago";return Math.round(s/86400)+" days ago"}
const day=s=>s?esc(fmtD.format(new Date(s+"T12:00:00Z"))):NA();
function tip(text){return `<button class="tip" type="button" data-tip="${esc(text)}" aria-label="${esc(text)}">i</button>`}
function empty(title,body){return `<div class="empty"><b>${esc(title)}</b>${body||""}</div>`}
function table(head,rows,emptyHtml){if(!rows.length)return emptyHtml;
 return `<div class="tablewrap"><table><thead><tr>${head.map(h=>`<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`}
function etParts(d){const p={};new Intl.DateTimeFormat("en-US",{timeZone:ET,hourCycle:"h23",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",weekday:"short"})
 .formatToParts(d).forEach(x=>p[x.type]=x.value);return p}
// Most recent weekday 08:00 ET that should have produced a run (plus 2 h for long runs). Holidays not known.
function expectedRunDate(now){const d=new Date(now);for(let i=0;i<8;i++){const p=etParts(d);const wd=p.weekday;
 const mins=+p.hour*60+ +p.minute;const ymd=`${p.year}-${p.month}-${p.day}`;
 if(wd!=="Sat"&&wd!=="Sun"&&(i>0||mins>=600))return ymd;d.setUTCDate(d.getUTCDate()-1);d.setUTCHours(18)}return null}
const realRuns=()=>(D.runs||[]).filter(r=>!r.dry_run);

// ---------- data loading (embedded first; fresher data.json when served) ----------
function parseEmbedded(){try{return JSON.parse(document.getElementById("snapshot").textContent)}catch(e){LOADERR="The embedded data could not be read ("+e.message+").";return null}}
async function load(){D=parseEmbedded();
 if(location.protocol.startsWith("http")){try{const ctl=new AbortController();setTimeout(()=>ctl.abort(),5000);
  const r=await fetch("data.json?ts="+Date.now(),{cache:"no-store",signal:ctl.signal});
  if(r.ok){const j=await r.json();if(j&&j.schema===1&&(!D||j.built_at>=D.built_at)){D=j;SOURCE="live file"}}
  else LOADERR=`Could not refresh data.json (HTTP ${r.status}); showing the copy built into the page.`}
  catch(e){LOADERR="Could not refresh the data (offline?); showing the copy built into the page."}}
 if("serviceWorker" in navigator&&location.protocol==="https:"){navigator.serviceWorker.register("sw.js").catch(()=>{})}
 if(!D){$("#main").innerHTML=`<div class="banner err"><b>No data.</b> ${esc(LOADERR||"The dashboard has no snapshot.")} Rebuild it with <code>python -m tradingagents.demo --config demo.toml dashboard</code>.</div>`;$("#fresh").textContent="No data";return}
 route()}

// ---------- shared status ----------
function staleInfo(){const runs=realRuns();const last=runs[runs.length-1];const exp=expectedRunDate(Date.now());
 const built=new Date(D.built_at);const ageH=(Date.now()-built)/3.6e6;const out=[];
 if(!last)out.push(["warn","No real run is recorded yet."]);
 else if(exp&&last.trade_date<exp)out.push(["warn",`No run recorded for ${fmtD.format(new Date(exp+"T12:00:00Z"))} (expected weekdays 08:00 ET; holidays not known). Last run: ${fmtD.format(new Date(last.trade_date+"T12:00:00Z"))}.`]);
 if(ageH>26)out.push(["warn",`This snapshot was built ${ago(D.built_at)}. It is rebuilt after each run, so it may be out of date.`]);
 return out}
function statusSentence(){const runs=realRuns();const last=runs[runs.length-1];const A=D.account;const n=(A&&A.closed||[]).length;
 const open=(A&&A.positions||[]);const s=[];
 if(!last)return "The test is set up but has no recorded run yet. Nothing has been traded.";
 s.push(`The last run started ${fmtF.format(new Date(last.started))} ET for ${fmtD.format(new Date(last.trade_date+"T12:00:00Z"))}; completion is not recorded.`);
 const c=last.counts||{};const scan=last.scan;
 if(scan)s.push(scan.reported.length?`${scan.reported.length} watched compan${scan.reported.length>1?"ies":"y"} had reported earnings; ${c.decisions||0} analysed.`:"No watched company had reported earnings, so nothing was analysed.");
 if(c.errors)s.push(`${c.errors} error${c.errors>1?"s":""} recorded in that run.`);
 if(last.halted)s.push("New trades are halted by the drawdown limit.");
 s.push(open.length?`${open.length} position${open.length>1?"s":""} open or pending.`:"No open positions.");
 s.push(n?`${n} of about ${TARGET} closed trades needed before judging; results so far are provisional.`:`No closed trades yet; judging needs about ${TARGET}.`);
 return s.join(" ")}

// ---------- pages ----------
function overview(){const A=D.account,runs=realRuns(),last=runs[runs.length-1],cfg=D.config||{};
 const pos=A?A.positions:[];const pend=pos.filter(p=>p.status==="pending"),openP=pos.filter(p=>p.status==="open"),exiting=pos.filter(p=>p.status==="closing");
 const marks=pos.filter(p=>p.status!=="pending");const markSum=marks.length&&marks.every(p=>p.mark&&p.mark.unrealized!=null)?marks.reduce((a,p)=>a+p.mark.unrealized,0):null;
 const lastAct=(D.activity||[]).slice(-1)[0];
 const k=(l,v,s,tp,vc)=>`<div class="card kpi"><div class="l">${esc(l)} ${tp?tip(tp):""}</div><div class="v ${vc||""}">${v}</div><div class="s">${s||""}</div></div>`;
 const eq=A&&A.nav_last_run!=null?`<span>${money(A.nav_last_run)}</span>`:NA(A?"no run yet":"no paper account file");
 const n=A?A.closed.length:0;
 return `${banners()}
 <div class="card"><h2>Status</h2><p class="status">${esc(statusSentence())}</p></div>
 <div class="grid g4 section">
 ${k("Equity",eq,A&&A.nav_last_run_time?"as of the last run start, "+t(A.nav_last_run_time):"","Account value (cash plus open positions at the last close) recorded when the last run started. Prices are daily closes from Yahoo.")}
 ${k("Cash",A&&A.cash!=null?money(A.cash):NA(),A?"paper account file, "+(D.sources.paper_account.modified?t(D.sources.paper_account.modified):"time unknown"):"","Cash in the paper account file. It changes only when a trade closes.")}
 ${k("Realized P&L",A?`<span class="${cls(A.realized_pl+A.financing)}">${money(A.realized_pl+A.financing,true)}</span>`:NA(),A?`${n} closed trade${n===1?"":"s"}; includes spread and short borrow`:"","Sum of closed trades' profit or loss after the simulated spread and short-borrow fee.")}
 ${k("Unrealized P&L",A&&A.unrealized_last_run!=null?`<span class="${cls(A.unrealized_last_run)}">${money(A.unrealized_last_run,true)}</span>`:NA(A?"no run yet":null),
   markSum!=null?`at last close (${esc(marks[0].mark.date)}): <span class="${cls(markSum)}">${money(markSum,true)}</span>`:(A&&A.unrealized_last_run!=null?"at the last run start":""),"Open positions' gain or loss before exit costs. First figure: recorded at the last run. Second: re-priced when this page was built, if prices were available.")}
 ${k("Drawdown",last&&last.drawdown_pct!=null?`<span class="${last.drawdown_pct>0?"neg":""}">${num(Math.max(0,last.drawdown_pct),2)}%</span>`:NA(),last&&last.drawdown_pct<0?`equity ${num(-last.drawdown_pct,2)}% above the start · limit ${esc(cfg.max_drawdown_pct)}%`:`stops new trades at ${esc(cfg.max_drawdown_pct??"?")}%`,"The runner's own measure: how far equity is below the STARTING $"+num(cfg.paper_start_cash||100000)+" (not below the peak), recorded at each run start. New trades stop at the limit.")}
 ${k("Positions",`${openP.length} <span class="na">open</span>`,`${pend.length} pending entr${pend.length===1?"y":"ies"} · ${exiting.length} pending exit${exiting.length===1?"":"s"}`,"Open = filled. Pending entry = order waiting for the next daily open. Pending exit = exit requested, fills at the next daily open.")}
 ${k("Closed trades",`${n}<span class="na"> / ${TARGET}</span>`,`<div class="progress"><div style="width:${Math.min(100,100*n/TARGET)}%"></div></div>`,"The test is judged after about "+TARGET+" closed trades. Before that every result is provisional.")}
 ${k("Last activity",lastAct?t(lastAct.ts):NA(),`journal file changed ${D.sources.journal.modified?t(D.sources.journal.modified):"—"}`,"Newest journal row, and when the journal file last changed.","small")}
 </div>
 <div class="grid g2 section"><div class="card"><h2>Test settings ${tip("Frozen for the whole test; changing them restarts the trade count.")}</h2>${settingsList(cfg)}</div>
 <div class="card"><h2>Latest decisions</h2>${latestDecisions(5)}</div></div>`}
function settingsList(c){const rows=[["Mode",c.mode==="earnings"?`Earnings: ${c.universe_size} stocks, hold ${c.hold_days} trading days`:esc(c.mode)],
 ["Models",esc(c.llm)],["Risk per trade",`${esc(c.risk_pct)}% of equity (${esc((c.mild_size||0)*100)}% of that for Overweight/Underweight)`],
 ["Stop",`${esc(c.atr_mult)} × ATR(${esc(c.atr_period)})`],["Caps",`${esc(c.max_open_positions||"no")} open, net ${esc(c.max_net_positions||"no")}, ${esc(c.max_analyses_per_run)} analyses/run`],
 ["Costs",`${esc(c.paper_spread_bps)} bps spread, ${esc((c.paper_borrow_bps_per_year||0)/100)}%/yr short borrow`],["Benchmark",esc(c.benchmark||"none")]];
 return `<dl class="kv">${rows.map(r=>`<dt>${r[0]}</dt><dd>${r[1]}</dd>`).join("")}</dl>`}
function latestDecisions(n){const ds=(D.decisions||[]).slice(-n).reverse();
 if(!ds.length)return empty("No analyses yet","The agents analyse a stock only after it reports earnings.");
 return ds.map(d=>`<div class="dec"><div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"><b>${esc(d.symbol)}</b>${ratingChip(d)}<span class="meta">${t(d.ts)} · ${esc(d.status||"")}</span>
 <a href="#decisions/${encodeURIComponent(d.symbol)}" style="margin-left:auto;font-size:13px">open</a></div></div>`).join("")}
const ratingChip=d=>d.rating?`<span class="chip r-${esc(d.rating)}">${esc(d.rating)}</span>`:`<span class="chip">no rating</span>`;
const statusChip=d=>`<span class="chip ${d.status==="failed"?"st-failed":""}">${esc(d.status||"")}</span>`;
function banners(){const b=[];if(LOADERR)b.push(`<div class="banner err">${esc(LOADERR)}</div>`);
 staleInfo().forEach(([k,m])=>b.push(`<div class="banner">${esc(m)}</div>`));
 const S=D.sources||{};Object.entries(S).forEach(([k,v])=>{if(v.status!=="ok")b.push(`<div class="banner err">Data source <b>${esc(k.replace("_"," "))}</b>: ${esc(v.status)}.</div>`);
  if(v.bad_lines)b.push(`<div class="banner err">${v.bad_lines} journal line(s) could not be read and are not shown.</div>`)});
 return b.join("")}

function positions(){const A=D.account;if(!A)return banners()+`<div class="card">${empty("No paper account file","The dashboard reads paper_account.json in the journal folder. It is created by the first run.")}</div>`;
 const P=D.extras&&D.extras.prices;const dec=D.decisions||[];
 const statusTxt=p=>p.status==="pending"?`<span class="chip">Pending entry</span><div class="faint" style="font-size:12px">ordered ${day(p.ordered_on)}; fills at next open</div>`:
  p.status==="closing"?`<span class="chip warn">Exit requested</span><div class="faint" style="font-size:12px">requested ${day(p.exit_requested_on)}; fills at next open</div>`:`<span class="chip">Open</span>`;
 const rows=A.positions.map(p=>{const m=p.mark;const link=p.decision_index!=null&&dec[p.decision_index]?`<a href="#decisions/${encodeURIComponent(p.symbol)}">analysis</a>`:`<span class="faint">not linked</span>`;
  const mk=!m?NA(P?"not priced":"pricing off"):m.error?NA("price unavailable"):`${num(m.close,2)} <span class="faint">(${esc(m.date)} close)</span>`;
  const ur=m&&m.unrealized!=null?`<span class="${cls(m.unrealized)}">${money(m.unrealized,true)}</span>`:NA(p.status==="pending"?"not filled":"—");
  return `<tr><td><b>${esc(p.symbol)}</b><div class="faint" style="font-size:12px">#${esc(p.id)} ${p.rating?esc(p.rating):""}</div></td>
  <td class="${p.direction}">${p.direction==="long"?"Long":"Short"}</td><td>${num(p.units,2)}</td>
  <td>${p.entry!=null?num(p.entry,2):NA("not filled")}${p.filled_on?`<div class="faint" style="font-size:12px">${day(p.filled_on)}</div>`:""}</td>
  <td>${p.stop!=null?num(p.stop,2):NA("set at fill")}</td><td>${statusTxt(p)}</td><td>${mk}</td><td>${ur}</td>
  <td>${p.status==="closing"?"exit requested":p.planned_exit_request?`run of ${day(p.planned_exit_request)}`:NA(p.status==="pending"?"set at fill":null)}</td><td>${link}</td></tr>`});
 const mpos=A.positions.map(p=>{const m=p.mark;return `<div class="mcard"><div class="top"><b>${esc(p.symbol)}</b><span class="${p.direction}">${p.direction==="long"?"Long":"Short"}</span>${statusTxt(p)}</div>
  <dl class="kv"><dt>Units</dt><dd>${num(p.units,2)}</dd><dt>Entry</dt><dd>${p.entry!=null?num(p.entry,2)+" ("+day(p.filled_on)+")":NA("not filled")}</dd><dt>Stop</dt><dd>${p.stop!=null?num(p.stop,2):NA("set at fill")}</dd>
  <dt>Last close</dt><dd>${!m?NA("not priced"):m.error?NA("price unavailable"):num(m.close,2)+" ("+esc(m.date)+")"}</dd><dt>Unrealized</dt><dd>${m&&m.unrealized!=null?`<span class="${cls(m.unrealized)}">${money(m.unrealized,true)}</span>`:NA("—")}</dd>
  <dt>Planned exit</dt><dd>${p.status==="closing"?"exit requested":p.planned_exit_request?"run of "+day(p.planned_exit_request):NA()}</dd><dt>Analysis</dt><dd>${p.decision_index!=null&&dec[p.decision_index]?`<a href="#decisions/${encodeURIComponent(p.symbol)}">open</a>`:NA("not linked")}</dd></dl></div>`}).join("");
 const cl=A.closed.slice().reverse().map(c=>{const pl=(+c.realizedPL||0)+(+c.financing||0);
  return `<tr><td><b>${esc(c.instrument)}</b><div class="faint" style="font-size:12px">#${esc(c.id)}</div></td><td class="${c.units>0?"long":"short"}">${c.units>0?"Long":"Short"}</td>
  <td>${num(Math.abs(c.units),2)}</td><td>${num(c.price,2)}<div class="faint" style="font-size:12px">${day(c.opened)}</div></td>
  <td>${num(c.close_price,2)}<div class="faint" style="font-size:12px">${day(c.closed)} (filled)</div></td><td>${esc(c.reason==="rating"?"hold period / rating":c.reason)}</td>
  <td>${money(c.spread_cost)}${+c.financing?` + ${money(-c.financing)} borrow`:""}</td><td class="${cls(pl)}">${money(pl,true)}</td></tr>`});
 const mcl=A.closed.slice().reverse().map(c=>{const pl=(+c.realizedPL||0)+(+c.financing||0);return `<div class="mcard"><div class="top"><b>${esc(c.instrument)}</b><span class="${c.units>0?"long":"short"}">${c.units>0?"Long":"Short"}</span><span class="${cls(pl)}" style="margin-left:auto;font-weight:600">${money(pl,true)}</span></div>
  <dl class="kv"><dt>Entry</dt><dd>${num(c.price,2)} (${day(c.opened)})</dd><dt>Exit (filled)</dt><dd>${num(c.close_price,2)} (${day(c.closed)})</dd><dt>Reason</dt><dd>${esc(c.reason==="rating"?"hold period / rating":c.reason)}</dd><dt>Costs</dt><dd>${money(c.spread_cost)}${+c.financing?` + ${money(-c.financing)} borrow`:""}</dd></dl></div>`}).join("");
 return `${banners()}<div class="card"><h2>Open and pending ${tip("Prices for 'Last close' are fetched when the page is built, never when it is opened.")}</h2>
 ${P?`<p class="faint" style="margin:-4px 0 10px;font-size:13px">Re-priced ${t(P.fetched)} (${P.ok} of ${P.of}).</p>`:""}
 ${table(["Symbol","Side","Units","Entry","Stop","Status","Last close","Unrealized","Planned exit "+tip("Earnings mode places the exit order on the run before the open that ends session "+(D.config.hold_days||5)+" after the fill (NYSE holidays skipped); the order fills at that open."),"Analysis"],rows,empty("No open or pending positions","New positions appear after an analysis gives a Buy, Overweight, Underweight or Sell.")).replace('<div class="tablewrap">','<div class="tablewrap desk">')}${mpos?`<div class="mlist">${mpos}</div>`:""}</div>
 <div class="card section"><h2>Closed trades ${tip("Exit price and date are the recorded fill, after the exit was requested.")}</h2>
 ${table(["Symbol","Side","Units","Entry","Exit","Exit reason","Costs","P&L"],cl,empty("No closed trades yet","Trades close after the holding period or at their stop.")).replace('<div class="tablewrap">','<div class="tablewrap desk">')}${mcl?`<div class="mlist">${mcl}</div>`:""}</div>`}

let F={q:"",rating:"",status:"",from:"",to:""};
function decisions(sym){if(sym)F.q=decodeURIComponent(sym);const ds=(D.decisions||[]);
 const ratings=[...new Set(ds.map(d=>d.rating||"(none)"))],stats=[...new Set(ds.map(d=>d.status||""))].filter(Boolean);
 const opt=(arr,v)=>arr.map(x=>`<option ${x===v?"selected":""}>${esc(x)}</option>`).join("");
 return `${banners()}<div class="card"><h2>AI decisions</h2>
 <div class="filters"><input type="search" id="fq" placeholder="Symbol" value="${esc(F.q)}" aria-label="Filter by symbol">
 <select id="fr" aria-label="Rating"><option value="">All ratings</option>${opt(ratings,F.rating)}</select>
 <select id="fs" aria-label="Status"><option value="">All statuses</option>${opt(stats,F.status)}</select>
 <input type="date" id="ff" value="${esc(F.from)}" aria-label="From date"><input type="date" id="ft" value="${esc(F.to)}" aria-label="To date"></div>
 <div id="declist"></div></div>`}
function drawDecisions(){const el=$("#declist");if(!el)return;const q=F.q.trim().toUpperCase();
 const ds=(D.decisions||[]).filter(d=>(!q||String(d.symbol).toUpperCase().includes(q))&&(!F.rating||(d.rating||"(none)")===F.rating)&&(!F.status||d.status===F.status)
  &&(!F.from||(d.trade_date||"")>=F.from)&&(!F.to||(d.trade_date||"")<=F.to)).reverse();
 if(!(D.decisions||[]).length){el.innerHTML=empty("No analyses recorded yet","Each weekday the runner checks which watched companies reported earnings and analyses up to "+esc(D.config.max_analyses_per_run)+" of them. Holds, skips and failures will be listed here too.");return}
 if(!ds.length){el.innerHTML=empty("Nothing matches these filters","");return}
 el.innerHTML=`<p class="faint" style="font-size:13px;margin:0 0 6px">${ds.length} of ${D.decisions.length} shown, newest first.</p>`+ds.map(d=>`<details class="dec"><summary><b>${esc(d.symbol)}</b>${ratingChip(d)}
 <span class="meta">${t(d.ts)}</span>${statusChip(d)}</summary><div class="body"><dl class="kv">
 <dt>Analysis date</dt><dd>${day(d.trade_date)}</dd><dt>Outcome</dt><dd>${esc(d.status||"—")}${d.status_reason?` — ${esc(d.status_reason)}`:""}${d.trade_id?` (trade #${esc(d.trade_id)})`:""}</dd>
 <dt>Model</dt><dd>${d.model?esc(d.model):NA()}</dd><dt>Earnings report</dt><dd>${d.report_time?esc(d.report_time):NA()}${d.edgar_check?` · ${esc(d.edgar_check)}`:""}</dd>
 <dt>Full report</dt><dd>${d.report_href?`<a href="${esc(d.report_href)}" target="_blank" rel="noopener">open saved report</a>`:NA(d.report_recorded?"saved on the runner PC only":"not saved")}</dd></dl>
 ${d.rationale?`<pre>${esc(d.rationale)}</pre>`:`<p class="faint">No rationale recorded.</p>`}</div></details>`).join("")}

function chart(series,opts){const pts=series.filter(s=>s.length>=1);const all=pts.flat();if(!all.length||Math.max(...pts.map(s=>s.length))<2)return null;
 const W=window.innerWidth<640?380:720,H=window.innerWidth<640?200:230,P={l:50,r:12,t:12,b:28};const xs=[...new Set(all.map(p=>p[0]))].sort();const xi=x=>xs.indexOf(x);
 let lo=Math.min(...all.map(p=>p[1]),opts.zero?0:Infinity),hi=Math.max(...all.map(p=>p[1]),opts.zero?0:-Infinity);if(hi-lo<opts.minSpan){const m=(hi+lo)/2;lo=m-opts.minSpan/2;hi=m+opts.minSpan/2}
 const X=i=>P.l+(xs.length<2?0:i*(W-P.l-P.r)/(xs.length-1)),Y=v=>H-P.b-(v-lo)*(H-P.t-P.b)/(hi-lo);
 const grid=[lo,(lo+hi)/2,hi].map(v=>`<line class="gl" x1="${P.l}" x2="${W-P.r}" y1="${Y(v)}" y2="${Y(v)}"/><text class="ax" x="${P.l-6}" y="${Y(v)+4}" text-anchor="end">${opts.fmt(v)}</text>`).join("");
 const zero=opts.zero&&lo<0&&hi>0?`<line class="zero" x1="${P.l}" x2="${W-P.r}" y1="${Y(0)}" y2="${Y(0)}"/>`:"";
 const lines=pts.map((s,i)=>`<path class="ln ${opts.cls[i]}" d="${s.map((p,j)=>(j?"L":"M")+X(xi(p[0])).toFixed(1)+","+Y(p[1]).toFixed(1)).join(" ")}"/>`).join("");
 const lab=`<text class="ax" x="${P.l}" y="${H-8}">${esc(xs[0])}</text><text class="ax" x="${W-P.r}" y="${H-8}" text-anchor="end">${esc(xs[xs.length-1])}</text>`;
 return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.label)}">${grid}${zero}${lines}${lab}</svg>`}
function performance(){const A=D.account,runs=realRuns(),E=D.extras||{},S=E.score||{trades:0};const cfg=D.config||{};
 const eqS=runs.filter(r=>r.nav!=null).map(r=>[r.trade_date,100*(r.nav/(r.start_nav||cfg.paper_start_cash||100000)-1)]);
 const bS=runs.filter(r=>r.benchmark_pct!=null).map(r=>[r.trade_date,r.benchmark_pct]);
 const ddS=runs.filter(r=>r.drawdown_pct!=null).map(r=>[r.trade_date,-Math.max(0,r.drawdown_pct)]);
 const eqC=chart([bS,eqS],{cls:["b","a"],zero:true,minSpan:1,fmt:v=>(v>0?"+":"")+v.toFixed(1)+"%",label:"Account and benchmark, percent since start"});
 const ddC=chart([ddS],{cls:["d"],zero:true,minSpan:1,fmt:v=>v.toFixed(1)+"%",label:"Recorded drawdown from start, percent"});
 const n=S.trades||0;const range=A&&A.closed.length?`${A.closed[0].opened} to ${A.closed[A.closed.length-1].closed}`:null;
 const prov=`<span class="chip warn" title="Fewer than ${TARGET} closed trades: not evidence of an edge.">provisional</span>`;
 const k=(l,v,tp)=>`<dt>${esc(l)} ${tp?tip(tp):""}</dt><dd>${v}</dd>`;
 const score=n?`<dl class="kv">${k("Closed trades",`${n} of ${TARGET} ${n<TARGET?prov:""}`)}${k("Date range",esc(range||"—"))}
 ${k("Total P&L",`<span class="${cls(S.total_pnl)}">${money(S.total_pnl,true)}</span>`,"After spread and short borrow.")}${k("Mean per trade",`${money(S.mean_pnl,true)} (95% CI ${money(S.mean_pnl_ci95[0],true)} to ${money(S.mean_pnl_ci95[1],true)})`,"Bootstrap 95% interval. It treats trades as independent; trades in one earnings week move together, so the real interval is wider.")}
 ${k("Win rate",num(100*S.win_rate,0)+"%")}${k("Random as good",num(100*S.p_random_as_good,0)+"%","Share of 2,000 coin-flip versions of the same trades (same timing and costs, random direction) that did at least as well. An edge needs this below about 5% AND the interval above zero, after about "+TARGET+" trades.")}
 ${Object.entries(S.by_side||{}).map(([s,v])=>k(s==="long"?"Longs":"Shorts",`${v.trades} trades, ${num(100*v.win_rate,0)}% win, ${money(v.mean_pnl,true)} avg`)).join("")}</dl>`
 :empty("No closed trades yet","Results, costs and the comparison with random trades appear here after the first trade closes.");
 const costs=A&&A.closed.length?`<dl class="kv">${k("Spread paid",money(A.spread_cost),"Simulated "+(cfg.paper_spread_bps)+" bps round trip, charged on entry and exit.")}${k("Short borrow",money(-A.financing),"Charged on shorts at "+((cfg.paper_borrow_bps_per_year||0)/100)+"% per year for the days held.")}</dl>`:"";
 const VSL={trades:["Trades compared",0],skipped:["Skipped (no fund or prices)",0],mean_excess_pct:["Mean extra return per trade",2,"%"],share_beating_sector:["Share beating the fund",0,"%",100]};
 const vs=E.vs_sector_fund;const vsH=vs&&!vs.error&&vs.trades?`<dl class="kv">${Object.entries(vs).map(([a,b])=>{const L=VSL[a]||[a.replace(/_/g," "),2];return k(L[0],typeof b==="number"?num(b*(L[3]||1),L[1])+(L[2]||""):esc(JSON.stringify(b)))}).join("")}</dl>`:(vs&&vs.error?NA("comparison failed: "+vs.error):NA("needs closed trades"));
 const R=E.ratings_score;let rH;
 if(!R)rH=NA("not computed (prices off)");else if(R.error)rH=NA("failed: "+R.error);else if(!R.scored)rH=`<p class="faint">No rating has finished its ${esc(cfg.hold_days)}-day window yet${R.pending?` (${R.pending} pending)`:""}.</p>`;
 else rH=`${table(["Rating","Ratings","Mean move"],Object.entries(R.by_rating).map(([r,v])=>`<tr><td><span class="chip r-${esc(r)}">${esc(r)}</span></td><td>${v.n}</td><td class="${cls(v.mean_move_pct)}">${num(v.mean_move_pct,2)}%</td></tr>`),"")}
  ${R.directional?`<p style="margin:10px 0 0">Direction right ${num(100*R.directional.hit_rate,0)}% of ${R.directional.n} directional calls ${prov}${R.pending?` · ${R.pending} still in their window`:""}</p>`:""}`;
 const lastB=bS.length?bS[bS.length-1][1]:null,lastA=eqS.length?eqS[eqS.length-1][1]:null;
 return `${banners()}<div class="banner info">Everything below is ${n<TARGET?"<b>provisional</b>: fewer than "+TARGET+" closed trades is not evidence of profitability.":"based on "+n+" closed trades."}</div>
 <div class="grid g2"><div class="card"><h2>Equity vs ${esc(cfg.benchmark||"benchmark")} ${tip("Account: equity at each run start relative to the start. Benchmark: buy-and-hold of "+(cfg.benchmark||"SPY")+" over the same period, recorded by the runner.")}</h2>
 ${eqC||empty("Not enough runs to draw","The chart needs at least two recorded runs.")}${eqC?`<div class="legend"><span><i class="key" style="background:var(--accent)"></i>Paper account ${lastA!=null?num(lastA,2)+"%":""}</span><span><i class="key" style="background:var(--bench)"></i>${esc(cfg.benchmark)} ${lastB!=null?num(lastB,2)+"%":""}</span></div>`:""}
 <p class="faint" style="font-size:12px;margin:8px 0 0">${runs.length} recorded run${runs.length===1?"":"s"}${runs.length?`, ${esc(runs[0].trade_date)} to ${esc(runs[runs.length-1].trade_date)}`:""}; benchmark recorded on ${bS.length} of them.</p></div>
 <div class="card"><h2>Drawdown ${tip("The runner's definition: fall below the STARTING equity (not the peak), recorded at each run start. New trades stop at "+cfg.max_drawdown_pct+"%.")}</h2>${ddC||empty("Not enough runs to draw","")}<p class="faint" style="font-size:12px;margin:8px 0 0">Shown below zero; 0 while equity is at or above the start. Limit: ${esc(cfg.max_drawdown_pct)}%.</p></div></div>
 <div class="grid g2 section"><div class="card"><h2>Closed-trade results</h2>${score}</div>
 <div class="card"><h2>Every rating vs the next ${esc(cfg.hold_days)} days ${tip("Each rating, traded or not, compared with the stock's move from the next open over the same window a trade holds, at mid prices (no costs). Computed when the page is built.")}</h2>${rH}</div></div>
 <div class="grid g2 section"><div class="card"><h2>Costs</h2>${costs||NA("no closed trades")}</div><div class="card"><h2>Vs sector funds ${tip("Mean extra return per trade over the stock's SPDR sector fund, in the trade's direction.")}</h2>${vsH}</div></div>`}

let showDry=false;
function activity(){const S=D.sources||{},sch=D.schedule;const acts=(D.activity||[]).filter(a=>showDry||!a.dry_run).slice().reverse();
 const label={run_start:"Run started",earnings_scan:"Earnings scan",decision:"Analysis",open:"Order placed",open_dry_run:"Order (dry run)",skip:"Skipped",
  error:"Error",close:"Exit requested",close_dry_run:"Exit (dry run)",paper_fill:"Entry filled",paper_close:"Exit filled"};
 const detail=a=>{switch(a.event){case "run_start":return `for ${day(a.trade_date)}`;
  case "earnings_scan":return `${(a.reported||[]).length} reported${(a.reported||[]).length?": "+esc(a.reported.join(", ")):""}; analysing ${(a.analysing||[]).length}${(a.skipped_over_limit||[]).length?`; over limit: ${esc(a.skipped_over_limit.join(", "))}`:""}`;
  case "decision":return `${esc(a.ticker||a.instrument)} ${a.rating?`<span class="chip r-${esc(a.rating)}">${esc(a.rating)}</span>`:""}`;
  case "error":return `<span class="neg">${esc(a.instrument||"")} ${esc(a.error)}</span>`;case "skip":return `${esc(a.instrument)}: ${esc(a.reason)}`;
  case "paper_fill":return `trade #${esc(a.trade_id)} at ${num(a.price,2)}`;case "paper_close":return `${esc(a.instrument)} #${esc(a.trade_id||"")} at ${num(a.close_price,2)}, ${money(a.realizedPL,true)} (${esc(a.reason)})`;
  default:return esc([a.instrument,a.reason,a.trade_id?"#"+a.trade_id:""].filter(Boolean).join(" "))}};
 const feed=acts.length?`<div class="feed">${acts.map(a=>`<div class="row"><div class="faint">${t(a.ts)}</div><div><span class="ev ${a.event==="error"?"neg":""}">${esc(label[a.event]||a.event)}</span>${a.dry_run?'<span class="badge">dry run</span>':""} ${detail(a)}</div></div>`).join("")}</div>`
  :empty("No activity recorded","The journal is empty.");
 const src=Object.entries(S).map(([k,v])=>`<dt>${esc(k.replace("_"," "))}</dt><dd>${v.status==="ok"?"read OK":`<span class="neg">${esc(v.status)}</span>`}${v.rows!=null?` · ${v.rows} rows`:""}${v.modified?` · changed ${t(v.modified)}`:""}</dd>`).join("");
 const tasks=sch&&sch.tasks&&Object.keys(sch.tasks).length?Object.entries(sch.tasks).map(([n,v])=>`<dt>${esc(n)}</dt><dd>${v.found?`${esc(v.status||v.scheduled_task_state||"")} · next run ${esc(v.next_run_time||"?")} (PC local time) · last result ${esc(v.last_result||"?")}`:"<span class='neg'>not found</span>"}</dd>`).join("")
  :`<dt>Schedule</dt><dd>${NA("not verified (Task Scheduler not readable where this page was built)")}</dd>`;
 const retry=Object.keys(D.retry_queue||{});
 const last=realRuns().slice(-1)[0];
 return `${banners()}<div class="grid g2"><div class="card"><h2>System status ${tip("Facts the dashboard can check. It never calls the system healthy just because a run started.")}</h2><dl class="kv">
 <dt>Last run</dt><dd>${last?`started ${t(last.started)}; <b>completion not recorded</b>; last row from that run ${t(last.last_event)}`:NA("none yet")}</dd>
 <dt>Errors in last run</dt><dd>${last?(last.counts.errors?`<span class="neg">${last.counts.errors}</span>`:"0 recorded"):"—"}</dd>
 <dt>Missed weekdays</dt><dd>${last?(last.missed_runs?`<span class="warn">${last.missed_runs} before the last run</span>`:"none recorded"):"—"}</dd>
 <dt>Retry queue</dt><dd>${retry.length?esc(retry.join(", ")):"empty"}</dd>
 <dt>Notifications</dt><dd>${NA(D.notifications.note)}</dd>
 <dt>Page built</dt><dd>${t(D.built_at)} on ${esc(D.built_on_host)} (${esc(SOURCE)})</dd>${tasks}${sch?`<dt>Schedule checked</dt><dd>${t(sch.checked)}</dd>`:""}</dl></div>
 <div class="card"><h2>Data sources</h2><dl class="kv">${src}</dl></div></div>
 <div class="card section"><h2>Journal ${tip("Newest first. Read only: this page cannot write to the journal.")}</h2>
 <label class="faint" style="font-size:13px;display:inline-flex;gap:6px;align-items:center;margin-bottom:8px"><input type="checkbox" id="dry" ${showDry?"checked":""}> show dry runs (tests)</label>
 ${D.activity_total>(D.activity||[]).length?`<p class="faint" style="font-size:13px">Showing the latest ${(D.activity||[]).length} of ${D.activity_total} rows.</p>`:""}${feed}</div>`}

// ---------- router ----------
function route(){const h=(location.hash||"#overview").slice(1).split("/");const page=h[0]||"overview";
 document.querySelectorAll("#tabs a").forEach(a=>a.classList.toggle("on",a.getAttribute("href")==="#"+page));
 const m=$("#main");try{m.innerHTML=page==="positions"?positions():page==="decisions"?decisions(h[1]):page==="performance"?performance():page==="activity"?activity():overview()}
 catch(e){m.innerHTML=`<div class="banner err"><b>This page could not be drawn.</b> ${esc(e.message)}. The saved data may be in an unexpected shape.</div>`}
 if(page==="decisions"){drawDecisions();[["#fq","q","input"],["#fr","rating","change"],["#fs","status","change"],["#ff","from","change"],["#ft","to","change"]].forEach(([s,k,ev])=>{const el=$(s);el&&el.addEventListener(ev,()=>{F[k]=el.value;drawDecisions()})})}
 if(page==="activity"){const c=$("#dry");c&&c.addEventListener("change",()=>{showDry=c.checked;route()})}
 const st=staleInfo();$("#fresh").innerHTML=`Data built ${t(D.built_at)} · ${esc(ago(D.built_at))}${st.length?' · <span class="warn">stale?</span>':""}`;
 $("#foot").innerHTML=`Paper money only — no real orders. Times are New York (ET); hover for your local time. Read-only page: opening or refreshing it changes nothing. Data: ${esc(SOURCE)}.`;
 window.scrollTo(0,0)}
window.addEventListener("hashchange",route);
// tooltips that also work by tap
let pop=null;document.addEventListener("click",e=>{const b=e.target.closest(".tip");if(pop){pop.remove();pop=null}
 if(!b)return;e.preventDefault();pop=document.createElement("div");pop.className="pop";pop.textContent=b.dataset.tip;document.body.appendChild(pop);
 const r=b.getBoundingClientRect();const w=Math.min(280,window.innerWidth-24);pop.style.maxWidth=w+"px";
 pop.style.left=Math.max(12,Math.min(window.scrollX+r.left-w/2,window.scrollX+window.innerWidth-w-12))+"px";pop.style.top=(window.scrollY+r.bottom+8)+"px"});
// theme: dark by default, remembered per browser
const root=document.documentElement;try{const s=localStorage.getItem("ta-theme");if(s)root.dataset.theme=s}catch(e){}
$("#theme").addEventListener("click",()=>{root.dataset.theme=root.dataset.theme==="dark"?"light":"dark";try{localStorage.setItem("ta-theme",root.dataset.theme)}catch(e){}
 document.querySelector('meta[name=theme-color]').content=root.dataset.theme==="dark"?"#0d1117":"#f5f6f8"});
load();
})();
</script></body></html>"""
