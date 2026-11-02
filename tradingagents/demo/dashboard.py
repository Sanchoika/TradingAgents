"""One self-contained HTML page showing how the forward test is going.

Built only from local files (the journal and the paper account), so it opens
offline and needs no server: double-click ``dashboard.html``. Charts are inline
SVG; colours follow the system's light or dark mode.
"""

from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .report import summarize

TARGET_TRADES = 150
RATING_ORDER = ["Buy", "Overweight", "Hold", "Underweight", "Sell", "REVIEW"]


def _e(value) -> str:
    return html.escape(str(value))


def _money(x: float, sign: bool = False) -> str:
    x = round(x) or 0  # no "-0"
    return f"{x:+,}" if sign else f"{x:,}"


def collect(events: list[dict], paper_state: dict | None) -> dict:
    """Everything the page shows, as plain data (tested on its own)."""
    runs, decisions, equity = [], [], {}
    rating_of_trade, dry = {}, False
    current = None
    for e in events:
        ev = e["event"]
        if ev == "run_start":
            dry = bool(e.get("dry_run"))
            if dry:
                current = None
                continue
            current = {"date": e["trade_date"], "nav": e["nav"], "missed": e.get("missed_runs", 0),
                       "reported": 0, "decisions": 0, "orders": 0, "errors": 0}
            runs.append(current)
            equity[e["trade_date"]] = (e["nav"], e.get("start_nav") or e["nav"], e.get("benchmark_pct"))
        elif dry or current is None:
            continue
        elif ev == "earnings_scan":
            current["reported"] = len(e.get("reported", []))
        elif ev == "decision":
            current["decisions"] += 1
            decisions.append(e)
        elif ev == "open":
            current["orders"] += 1
            if e.get("trade_id"):
                rating_of_trade[str(e["trade_id"])] = e.get("rating")
        elif ev == "error":
            current["errors"] += 1

    paper_state = paper_state or {}
    closed = paper_state.get("closed", [])
    open_ = paper_state.get("open", [])
    spread = {str(t["id"]): t.get("spread_cost", 0) / 2 for t in closed}
    score = summarize(closed, spread, n_random=2000) if closed else {"trades": 0}

    by_rating = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t in closed:
        r = rating_of_trade.get(str(t["id"]), "?")
        b = by_rating[r]
        b["n"] += 1
        b["wins"] += t["realizedPL"] > 0
        b["pnl"] += t["realizedPL"]

    curve = []
    for d in sorted(equity):
        nav, start, bench = equity[d]
        curve.append({"date": d, "acct": 100.0 * (nav / start - 1) if start else 0.0, "bench": bench})
    last = runs[-1] if runs else None
    return {
        "runs": runs, "curve": curve, "score": score, "closed": closed, "open": open_,
        "rating_counts": Counter(d.get("rating") for d in decisions),
        "by_rating": dict(by_rating), "decisions": decisions[-6:][::-1], "last": last,
        "rating_of_trade": rating_of_trade,
    }


def _chart(curve: list[dict]) -> str:
    if len(curve) < 2:
        return '<p class="muted">The chart appears after the second run.</p>'
    w, h, pad = 720, 240, 52
    vals = [c["acct"] for c in curve] + [c["bench"] for c in curve if c["bench"] is not None] + [0.0]
    lo, hi = min(vals), max(vals)
    if hi - lo < 1:
        lo, hi = lo - 0.5, hi + 0.5
    x = lambda i: pad + i * (w - 2 * pad) / (len(curve) - 1)  # noqa: E731
    y = lambda v: h - pad - (v - lo) * (h - 2 * pad) / (hi - lo)  # noqa: E731

    def path(key):
        pts = [(x(i), y(c[key])) for i, c in enumerate(curve) if c[key] is not None]
        return " ".join(f"{'M' if j == 0 else 'L'}{px:.1f},{py:.1f}" for j, (px, py) in enumerate(pts))

    grid = "".join(
        f'<line x1="{pad}" x2="{w - pad}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>'
        f'<text x="{pad - 6}" y="{y(v) + 4:.1f}" class="axis" text-anchor="end">{v:+.1f}%</text>'
        for v in (lo, (lo + hi) / 2, hi))
    zero = f'<line x1="{pad}" x2="{w - pad}" y1="{y(0):.1f}" y2="{y(0):.1f}" class="zero"/>'
    labels = (f'<text x="{pad}" y="{h - 10}" class="axis">{_e(curve[0]["date"])}</text>'
              f'<text x="{w - pad}" y="{h - 10}" class="axis" text-anchor="end">{_e(curve[-1]["date"])}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Account vs SPY, percent since start">'
            f'{grid}{zero}<path d="{path("bench")}" class="line bench"/>'
            f'<path d="{path("acct")}" class="line acct"/>{labels}</svg>'
            '<div class="legend"><span class="key acct"></span>Paper account'
            '<span class="key bench"></span>SPY buy-and-hold</div>')


def _rating_bars(counts: Counter) -> str:
    total = sum(counts.values())
    if not total:
        return '<p class="muted">No analyses yet.</p>'
    rows = []
    for r in RATING_ORDER + sorted(set(counts) - set(RATING_ORDER), key=str):
        n = counts.get(r, 0)
        if not n:
            continue
        rows.append(f'<div class="bar"><span>{_e(r)}</span><div class="track"><div class="fill r-{_e(str(r).lower())}" '
                    f'style="width:{100 * n / total:.0f}%"></div></div><b>{n}</b></div>')
    return "".join(rows)


def render(data: dict, title: str = "TradingAgents paper test") -> str:
    s, last = data["score"], data["last"]
    curve = data["curve"]
    acct = curve[-1]["acct"] if curve else 0.0
    bench = curve[-1]["bench"] if curve and curve[-1]["bench"] is not None else None
    n_closed = s.get("trades", 0)
    progress = min(100, 100 * n_closed / TARGET_TRADES)
    p_rand = s.get("p_random_as_good")
    if n_closed < 30:
        verdict, vclass = "Too early to judge", "neutral"
    elif p_rand is not None and p_rand < 0.05 and s["mean_pnl_ci95"][0] > 0:
        verdict, vclass = "Beating random so far", "good"
    else:
        verdict, vclass = "No edge shown yet", "neutral"
    status = "No runs yet"
    if last:
        status = f"Last run {last['date']}"
        status += f" · {last['errors']} error(s)" if last["errors"] else " · OK"
        if last["missed"]:
            status += f" · {last['missed']} missed day(s)"

    def kpi(label, value, sub="", cls=""):
        return (f'<div class="card kpi"><div class="label">{_e(label)}</div>'
                f'<div class="value {cls}">{value}</div><div class="sub">{sub}</div></div>')

    cls = lambda v: "pos" if v > 0 else ("neg" if v < 0 else "")  # noqa: E731
    kpis = "".join([
        kpi("Account", f"{acct:+.2f}%", f"NAV {_money(last['nav']) if last else '100,000'}", cls(acct)),
        kpi("SPY", f"{bench:+.2f}%" if bench is not None else "–", "buy-and-hold, same period"),
        kpi("Closed trades", f"{n_closed}<small> / {TARGET_TRADES}</small>",
            f'<div class="progress"><div style="width:{progress:.0f}%"></div></div>'),
        kpi("Win rate", f"{100 * s['win_rate']:.0f}%" if n_closed else "–",
            f"P&amp;L {_money(s['total_pnl'], True)}" if n_closed else "after costs"),
        kpi("Random as good", f"{100 * p_rand:.0f}%" if p_rand is not None else "–",
            "share of coin-flip versions that did as well"),
        kpi("Verdict", verdict, "needs CI above 0 and random &lt; 5%", vclass),
    ])

    def table(head, rows, empty):
        if not rows:
            return f'<p class="muted">{empty}</p>'
        th = "".join(f"<th>{h}</th>" for h in head)
        return f'<div class="scroll"><table><thead><tr>{th}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'

    open_rows = []
    for t in data["open"]:
        side = "Long" if t["units"] > 0 else "Short"
        open_rows.append(
            f'<tr><td><b>{_e(t["instrument"])}</b></td><td><span class="pill {side.lower()}">{side}</span></td>'
            f'<td>{abs(t["units"]):,.2f}</td><td>{_e(round(t["price"], 2)) if t.get("price") else "next open"}</td>'
            f'<td>{_e(round(t["stop"], 2)) if t.get("stop") else "–"}</td>'
            f'<td>{_e(t.get("filled_on") or t.get("ordered_on"))}</td><td>{_e(t["status"])}</td></tr>')
    closed_rows = []
    for t in data["closed"][-20:][::-1]:
        pl = t["realizedPL"]
        closed_rows.append(
            f'<tr><td><b>{_e(t["instrument"])}</b></td><td>{_e(data["rating_of_trade"].get(str(t["id"]), "?"))}</td>'
            f'<td>{_e(t["opened"])}</td><td>{_e(t["closed"])}</td><td>{_e(t["reason"])}</td>'
            f'<td class="{cls(pl)}">{_money(pl, True)}</td></tr>')
    rating_rows = [
        f'<tr><td>{_e(r)}</td><td>{b["n"]}</td><td>{100 * b["wins"] / b["n"]:.0f}%</td>'
        f'<td class="{cls(b["pnl"])}">{_money(b["pnl"], True)}</td></tr>'
        for r, b in sorted(data["by_rating"].items(), key=lambda kv: str(kv[0]))]
    run_rows = [
        f'<tr><td>{_e(r["date"])}</td><td>{r["reported"]}</td><td>{r["decisions"]}</td><td>{r["orders"]}</td>'
        f'<td class="{"neg" if r["errors"] else ""}">{r["errors"]}</td>'
        f'<td class="{"neg" if r["missed"] else ""}">{r["missed"]}</td></tr>'
        for r in data["runs"][-10:][::-1]]
    decisions = "".join(
        f'<details><summary><b>{_e(d["instrument"])}</b> <span class="pill r-{_e(str(d.get("rating")).lower())}">'
        f'{_e(d.get("rating"))}</span> <span class="muted">{_e(d.get("trade_date", ""))} · '
        f'{_e(", ".join(d.get("actions", [])))}</span></summary><pre>{_e(d.get("decision", ""))}</pre></details>'
        for d in data["decisions"]) or '<p class="muted">No analyses yet.</p>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--muted:#6b7380;--line:#e4e7ec;--acct:#3b5bdb;--bench:#9aa3af;
--pos:#1a7f4b;--neg:#c92a2a;--accent:#3b5bdb;--pill:#eef1f6}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1115;--card:#171a21;--ink:#e8eaee;--muted:#9099a6;--line:#262b35;
--acct:#7c9cff;--bench:#6b7380;--pos:#4ade80;--neg:#f87171;--accent:#7c9cff;--pill:#222733}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:1080px;margin:0 auto;padding:28px 16px 48px}}
header{{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:baseline;gap:8px;margin-bottom:20px}}
h1{{font-size:22px;margin:0;letter-spacing:-.01em}}h2{{font-size:15px;margin:0 0 12px;color:var(--muted);
font-weight:600;text-transform:uppercase;letter-spacing:.04em}}
.status{{color:var(--muted);font-size:14px}}
.grid6{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}}
.kpi .label{{color:var(--muted);font-size:13px}}.kpi .value{{font-size:24px;font-weight:650;margin:2px 0}}
.kpi .value small{{font-size:14px;color:var(--muted);font-weight:500}}.kpi .sub{{color:var(--muted);font-size:12px}}
.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}.good{{color:var(--pos);font-size:18px!important}}
.neutral{{font-size:18px!important}}
.progress{{height:6px;background:var(--pill);border-radius:3px;margin-top:6px;overflow:hidden}}
.progress div{{height:100%;background:var(--accent)}}
.two{{display:grid;grid-template-columns:2fr 1fr;gap:12px;margin-bottom:12px}}
@media (max-width:760px){{.two{{grid-template-columns:1fr}}}}
svg{{width:100%;height:auto}}.grid{{stroke:var(--line)}}.zero{{stroke:var(--muted);stroke-dasharray:3 4}}
.axis{{fill:var(--muted);font-size:11px}}.line{{fill:none;stroke-width:2.5;stroke-linejoin:round}}
.line.acct{{stroke:var(--acct)}}.line.bench{{stroke:var(--bench);stroke-dasharray:6 4}}
.legend{{display:flex;gap:16px;color:var(--muted);font-size:13px;align-items:center;margin-top:6px}}
.key{{display:inline-block;width:14px;height:3px;margin-right:6px;vertical-align:middle}}
.key.acct{{background:var(--acct)}}.key.bench{{background:var(--bench)}}
.bar{{display:grid;grid-template-columns:96px 1fr 32px;align-items:center;gap:8px;margin:6px 0;font-size:14px}}
.track{{background:var(--pill);border-radius:4px;height:10px;overflow:hidden}}.fill{{height:100%;background:var(--accent)}}
.r-buy,.r-overweight{{background:var(--pos)}}.r-sell,.r-underweight{{background:var(--neg)}}.r-hold{{background:var(--bench)}}
.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;font-size:14px}}
th{{text-align:left;color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}}
th,td{{padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}}tr:last-child td{{border-bottom:0}}
.pill{{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;background:var(--pill);color:var(--ink)}}
.pill.long{{color:var(--pos)}}.pill.short{{color:var(--neg)}}
span.pill.r-buy,span.pill.r-overweight{{background:var(--pos);color:#fff}}
span.pill.r-sell,span.pill.r-underweight{{background:var(--neg);color:#fff}}span.pill.r-hold{{background:var(--bench);color:#fff}}
details{{border-top:1px solid var(--line);padding:10px 0}}details:first-child{{border-top:0}}
summary{{cursor:pointer}}pre{{white-space:pre-wrap;font:13px/1.5 ui-monospace,Consolas,monospace;color:var(--ink);
background:var(--pill);padding:12px;border-radius:8px;max-height:360px;overflow:auto}}
.muted{{color:var(--muted)}}.section{{margin-bottom:12px}}footer{{color:var(--muted);font-size:12px;margin-top:20px}}
</style></head><body><div class="wrap">
<header><h1>{_e(title)}</h1><div class="status">{_e(status)}</div></header>
<div class="grid6">{kpis}</div>
<div class="two"><div class="card"><h2>Account vs SPY</h2>{_chart(curve)}</div>
<div class="card"><h2>Ratings given</h2>{_rating_bars(data["rating_counts"])}</div></div>
<div class="card section"><h2>Open positions</h2>{table(["Stock", "Side", "Units", "Entry", "Stop", "Since", "Status"], open_rows, "No open positions.")}</div>
<div class="two"><div class="card"><h2>Closed trades</h2>{table(["Stock", "Rating", "Opened", "Closed", "Exit", "P&amp;L"], closed_rows, "No closed trades yet.")}</div>
<div class="card"><h2>By rating</h2>{table(["Rating", "Trades", "Win", "P&amp;L"], rating_rows, "Fills in as trades close.")}</div></div>
<div class="two"><div class="card"><h2>Latest analyses</h2>{decisions}</div>
<div class="card"><h2>Recent runs</h2>{table(["Date", "Reported", "Rated", "Orders", "Errors", "Missed"], run_rows, "No runs yet.")}</div></div>
<footer>Paper money only. Generated {datetime.now():%Y-%m-%d %H:%M} from the local journal. Judge after about {TARGET_TRADES} closed trades.</footer>
</div></body></html>"""


def write_dashboard(journal, paper_path: str | Path | None, out_path: str | Path) -> Path:
    state = None
    if paper_path and Path(paper_path).exists():
        state = json.loads(Path(paper_path).read_text(encoding="utf-8"))
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(collect(journal.events(), state)), encoding="utf-8")
    return out
