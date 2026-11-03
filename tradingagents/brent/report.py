"""Results from the Brent journal, kept in four separate parts, plus the status page.

1. Operational reliability (delivery, freshness, suppressions).
2. Directional usefulness (signed move at +60/+240 min in daily-ATR units).
3. Hypothetical paper trading after modelled costs (in R).
4. Manual results: only what you record yourself with `manual`.
"""

from __future__ import annotations

import html
import json
import random
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median

from .engine import p90


def _ci(xs: list[float], n: int = 2000, seed: int = 7) -> tuple[float, float] | None:
    if len(xs) < 10:
        return None
    rng = random.Random(seed)
    means = sorted(mean(rng.choices(xs, k=len(xs))) for _ in range(n))
    return round(means[int(0.025 * n)], 3), round(means[int(0.975 * n)], 3)


def summarize(events: list[dict]) -> dict:
    sent = [e for e in events if e["event"] == "sent"]
    alerts = [e for e in sent if e["kind"] == "alert"]
    signals = {e["id"]: e for e in events if e["event"] == "signal"}
    updates = Counter(e["status"] for e in events if e["event"] == "update")
    supp = Counter(re.sub(r"[-+]?\d[\d.]*", "#", r).split(" (")[0]
                   for e in events if e["event"] == "suppressed" for r in e["reasons"])
    send_s = [e["seconds"] for e in sent if e["ok"]]
    op = {
        "signals_issued": len(signals),
        "alerts_delivered": sum(1 for e in alerts if e["ok"]),
        "alerts_failed": sum(1 for e in alerts if not e["ok"]),
        "invalidated": updates.get("invalidated", 0),
        "expired": updates.get("expired", 0),
        "send_seconds_median": round(median(send_s), 2) if send_s else None,
        "send_seconds_p90": p90(send_s),
        "setups_armed": sum(1 for e in events if e["event"] == "watch"),
        "suppressed_reasons": dict(supp.most_common(10)),
        "ops_notices": sum(1 for e in events if e["event"] == "ops"),
        "monitor_starts": sum(1 for e in events if e["event"] == "start"),
    }
    marks: dict[str, list[float]] = {}
    for e in events:
        if e["event"] == "mark":
            marks.setdefault(str(e["minutes"]), []).append(e["move_atr"])
    direction = {f"+{m}min": {"n": len(v), "mean_move_atr": round(mean(v), 3),
                              "share_in_alert_direction": round(sum(x > 0 for x in v) / len(v), 2)}
                 for m, v in sorted(marks.items(), key=lambda kv: int(kv[0]))}
    def paper_stats(ids: set) -> dict:
        exits = [e for e in events if e["event"] == "paper_exit" and e["id"] in ids]
        rs = [e["net_r"] for e in exits]
        by_kind: dict[str, list[float]] = {}
        by_label: dict[str, list[float]] = {}
        for e in exits:
            sg = signals.get(e["id"], {})
            s = sg.get("setup", {})
            by_kind.setdefault(f"{s.get('kind', '?')} {s.get('direction', '?')}", []).append(e["net_r"])
            lab = (sg.get("label") or "n/a") + ("" if sg.get("telegram") or sg.get("arm") != "combined"
                                                else " (logged, not sent)")
            by_label.setdefault(lab, []).append(e["net_r"])
        return {
            "signals": len(ids),
            "fills": sum(1 for e in events if e["event"] == "paper_fill" and e["id"] in ids),
            "missed": sum(1 for e in events if e["event"] == "paper_missed" and e["id"] in ids),
            "closed": len(rs),
            "net_r_total": round(sum(rs), 2) if rs else 0.0,
            "net_r_mean": round(mean(rs), 3) if rs else None,
            "net_r_mean_ci95": _ci(rs),
            "win_rate": round(sum(r > 0 for r in rs) / len(rs), 2) if rs else None,
            "exit_reasons": dict(Counter(e["reason"] for e in exits)),
            "by_setup": {k: {"n": len(v), "mean_r": round(mean(v), 3)} for k, v in by_kind.items()},
            "by_label": {k: {"n": len(v), "mean_r": round(mean(v), 3), "ci95": _ci(v)}
                         for k, v in by_label.items()},
        }

    combined = {i for i, sg in signals.items() if sg.get("arm", "combined") == "combined"}
    shadow = {i for i, sg in signals.items() if sg.get("arm") == "chart-only"}
    paper = paper_stats(combined)
    cands = [e for e in events if e["event"] == "candidate"]
    paper["candidates_by_policy_row"] = dict(Counter(f"{e['action']}: {e['row']}" for e in cands))
    chart_only = paper_stats(shadow)
    manual = [e for e in events if e["event"] == "manual"]
    op["signals_issued"] = len(combined)
    op["chart_only_signals"] = len(shadow)
    return {"operational": op, "directional": direction, "paper_hypothetical": paper,
            "chart_only_comparison": chart_only,
            "manual_confirmed": {"records": len(manual),
                                 "entered": sum(1 for e in manual if e.get("entered")),
                                 "results": [{k: e.get(k) for k in ("id", "entered", "price", "units",
                                                                    "result", "note")}
                                             for e in manual[-20:]]},
            "note": ("No edge claim: a few good alerts prove nothing. Judge only when the paper "
                     "mean's confidence interval sits clearly above 0 after costs, across both "
                     "setup families, over months.")}


def status_lines(state: dict, locked: bool, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC)
    out = [f"monitor running: {'yes' if locked else 'NO'}"]
    if state.get("last_tick"):
        age = (now - datetime.fromisoformat(state["last_tick"])).total_seconds()
        out.append(f"last tick: {state['last_tick']} ({age:.0f}s ago)")
    q = state.get("last_quote")
    if q:
        out.append(f"last quote: bid {q['bid']} ask {q['ask']} at {q['time']} "
                   f"tradeable={q['tradeable']}")
    c = state.get("ctx")
    if c:
        out.append(f"levels: yesterday high {c['pdh']:.2f}, low {c['pdl']:.2f}, ATR {c['atr']:.2f} "
                   f"(day {c['day']})")
    if state.get("range_inside") is not None:
        out.append(f"inside operating range: {state['range_inside']}")
    armed = [s for s in state.get("setups", {}).values() if s["status"] == "armed"]
    out.append(f"armed setups (WATCH): {len(armed)}")
    for s in armed:
        out.append(f"  {s['kind']} {s['direction']} zone {s['zone_lo']}-{s['zone_hi']} stop "
                   f"{s['stop']} until {s['expires_at']}")
    for sig in state.get("signals", {}).values():
        out.append(f"  signal {sig['id']} {sig['setup']['direction']} status {sig['status']} "
                   f"paper {sig['paper']['state']}")
    m = state.get("metrics", {})
    def fmt(k):
        v = p90(m.get(k, []))
        return "n/a" if v is None else f"{v:.2f}s"

    out.append(f"p90 delays: quote age {fmt('quote_age_s')}, Telegram send {fmt('send_s')}, "
               f"monitor tick {fmt('tick_s')}")
    acc = state.get("account", {})
    if acc:
        out.append(f"account: {acc.get('currency')} hedging={acc.get('hedging')} "
                   f"open Brent trades={acc.get('trades')} {acc.get('error') or ''}")
    return out


def _table(d: dict) -> str:
    rows = "".join(f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(str(v))}</td></tr>"
                   for k, v in d.items())
    return f"<table>{rows}</table>"


def write_page(state: dict, events: list[dict], locked: bool, path: Path) -> Path:
    """Read-only status and results page (no scripts, no controls)."""
    res = summarize(events)
    recent = [e for e in events if e["event"] == "signal"][-15:]
    exits = {e["id"]: e for e in events if e["event"] == "paper_exit"}
    upd = {}
    for e in events:
        if e["event"] == "update":
            upd[e["id"]] = e["status"]
    rows = "".join(
        f"<tr><td>{html.escape(s['id'])}</td><td>{s['setup']['kind']} {s['setup']['direction']}</td>"
        f"<td>{s['setup']['zone_lo']}-{s['setup']['zone_hi']}</td><td>{s['setup']['stop']}</td>"
        f"<td>{html.escape(upd.get(s['id'], 'valid/sent'))}</td>"
        f"<td>{exits[s['id']]['net_r'] if s['id'] in exits else ''}</td></tr>" for s in reversed(recent))
    body = (
        "<h1>Brent alerts (paper / advisory)</h1>"
        f"<p class=note>Updated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC. Alerts only; no orders are "
        "placed. Hypothetical results are not trades you made.</p>"
        "<h2>Status</h2><pre>" + html.escape("\n".join(status_lines(state, locked))) + "</pre>"
        "<h2>Recent signals</h2><table><tr><th>id</th><th>setup</th><th>zone</th><th>stop</th>"
        f"<th>status</th><th>paper net R</th></tr>{rows}</table>"
        "<h2>1. Operational</h2>" + _table(res["operational"]) +
        "<h2>2. Directional (+60/+240 min, ATR units)</h2>" + _table(res["directional"] or {"n": 0}) +
        "<h2>3. Paper, hypothetical, after costs</h2>" + _table(res["paper_hypothetical"]) +
        "<h2>4. Manual (your records)</h2>" + _table(res["manual_confirmed"]) +
        f"<p class=note>{html.escape(res['note'])}</p>")
    page = (
        "<!doctype html><html><head><meta charset=utf-8><meta name=viewport "
        "content='width=device-width,initial-scale=1'><title>Brent alerts</title><style>"
        ":root{--bg:#fff;--fg:#1a1a1a;--mute:#666;--line:#ddd}"
        "@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#e8e8e8;--mute:#999;--line:#333}}"
        "body{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;margin:0 auto;"
        "max-width:900px;padding:16px}table{border-collapse:collapse;width:100%;margin:8px 0}"
        "th,td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left;vertical-align:top}"
        "pre{white-space:pre-wrap;font-size:13px}.note{color:var(--mute);font-size:13px}"
        f"</style></head><body>{body}</body></html>")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(page, encoding="utf-8")
    tmp.replace(path)
    return path


def write_scorecard(result: dict, notes_dir: str | Path) -> Path:
    folder = Path(notes_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "Brent Alerts Scorecard.md"
    parts = [f"# Brent alerts: scorecard\n\nUpdated {datetime.now():%Y-%m-%d %H:%M}. Paper/advisory "
             "only; no orders placed by the module.\n"]
    for name, sec in result.items():
        if isinstance(sec, dict):
            parts.append(f"## {name}\n\n| measure | value |\n|---|---|\n" +
                         "\n".join(f"| {k} | {v} |" for k, v in sec.items()) + "\n")
    parts.append(result["note"] + "\n")
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def _svg_chart(sig: dict, h1: list[dict], now: datetime) -> str:
    """H1 candles with the alert's levels. Numbers come from the candles and the signal."""
    bars = h1[-30:]
    s, p = sig["setup"], sig["plan"]
    ch = (sig.get("assessment") or {}).get("chart", {})
    lines = [("entry zone low", s["zone_lo"], "#3b6fd8"), ("entry zone high", s["zone_hi"], "#3b6fd8"),
             ("suggested stop", s["stop"], "#c0392b"), ("target 1R", p["t1"], "#2e8b57"),
             ("target 2R", p["t2"], "#2e8b57"), (f"prior-day {s['level_name']}", s["level"], "#888")]
    nl = ch.get("next_level")
    if nl:
        lines.append((nl["name"], nl["price"], "#888"))
    prices = [v for _, v, _ in lines] + [b["h"] for b in bars] + [b["l"] for b in bars]
    if not prices:
        return "<p>No candles available.</p>"
    lo, hi = min(prices), max(prices)
    pad = (hi - lo) * 0.05 or 0.5
    lo, hi = lo - pad, hi + pad
    W, H, left, right, y0, y1 = 760, 380, 50, 560, 30, 340

    def Y(v):
        return y1 - (v - lo) / (hi - lo) * (y1 - y0)

    step = (right - left) / max(len(bars), 1)
    out = [f"<svg viewBox='0 0 {W} {H}' role='img' aria-label='H1 candles and alert levels' "
           "style='width:100%;height:auto;font:11px system-ui'>",
           f"<rect x='{left}' y='{Y(s['zone_hi']):.1f}' width='{right - left}' "
           f"height='{max(Y(s['zone_lo']) - Y(s['zone_hi']), 1):.1f}' fill='#3b6fd8' fill-opacity='0.15'/>"]
    for name, v, col in lines:
        out.append(f"<line x1='{left}' x2='{right}' y1='{Y(v):.1f}' y2='{Y(v):.1f}' stroke='{col}' "
                   "stroke-dasharray='4 3'/>")
        out.append(f"<text x='{right + 6}' y='{Y(v) + 4:.1f}' fill='currentColor'>"
                   f"{html.escape(name)} {v:.2f}</text>")
    for i, b in enumerate(bars):
        x = left + step * (i + 0.5)
        forming = not b.get("complete", True)
        top, bot = Y(max(b["o"], b["c"])), Y(min(b["o"], b["c"]))
        dash = " stroke-dasharray='2 2'" if forming else ""
        out.append(f"<line x1='{x:.1f}' x2='{x:.1f}' y1='{Y(b['h']):.1f}' y2='{Y(b['l']):.1f}' "
                   f"stroke='currentColor'{dash}/>")
        fill = "currentColor" if b["c"] >= b["o"] else "none"
        out.append(f"<rect x='{x - step * 0.3:.1f}' y='{top:.1f}' width='{step * 0.6:.1f}' "
                   f"height='{max(bot - top, 1):.1f}' fill='{fill}' fill-opacity='0.6' "
                   f"stroke='currentColor'{dash}><title>{b['time']:%Y-%m-%d %H:%M} UTC O {b['o']:.2f} "
                   f"H {b['h']:.2f} L {b['l']:.2f} C {b['c']:.2f}{' (forming)' if forming else ''}"
                   "</title></rect>")
    if bars:
        out.append(f"<text x='{left}' y='{H - 18}' fill='currentColor'>{bars[0]['time']:%m-%d %H:%M} UTC</text>")
        out.append(f"<text x='{right}' y='{H - 18}' text-anchor='end' fill='currentColor'>"
                   f"{bars[-1]['time']:%m-%d %H:%M} UTC</text>")
    out.append(f"<text x='{left}' y='{H - 4}' fill='currentColor'>OANDA BCO_USD H1 mid candles, chart "
               f"made {now:%Y-%m-%d %H:%M:%S} UTC. Dashed candle = forming (not used).</text></svg>")
    return "".join(out)


def write_signal_page(sig: dict, h1: list[dict], path: Path, now: datetime) -> Path:
    a = sig.get("assessment") or {}
    items = a.get("news_items") or []
    rows = "".join(
        f"<tr><td>{html.escape(i.get('bias', ''))}</td><td>{html.escape(i.get('status', ''))}</td>"
        f"<td>{html.escape(i.get('scope', ''))}</td><td><a href='{html.escape(i.get('link') or '#')}'>"
        f"{html.escape(i.get('title', ''))}</a><br><small>{html.escape(i.get('mechanism', ''))}</small></td>"
        f"<td>{html.escape(str(i.get('published')))}</td><td>{html.escape(str(i.get('event_time')))}</td>"
        f"<td>{html.escape(i.get('first_seen', ''))}</td></tr>" for i in items)
    body = (
        f"<h1>{html.escape(sig['id'])}: {sig['setup']['direction']} BCO_USD "
        f"({html.escape(sig.get('label', ''))})</h1>"
        "<p class=note>Paper / advisory. No order was placed. Hypothetical tracking only.</p>"
        f"<p><b>News:</b> {html.escape(sig.get('news_line', ''))}</p>"
        f"<p><b>Chart:</b> {html.escape(sig.get('chart_line', ''))}</p>"
        f"<p><b>Main risk:</b> {html.escape(sig.get('contradiction', ''))}</p>"
        f"<p><b>Scenarios:</b> {html.escape(sig.get('why', ''))}</p>"
        f"<p><b>Policy row:</b> {html.escape(sig.get('policy_row', ''))}; news state "
        f"{html.escape(str((a.get('news') or {}).get('state')))}</p>"
        + _svg_chart(sig, h1, now) +
        "<h2>Material news used (times in UTC)</h2><table><tr><th>bias</th><th>status</th>"
        "<th>scope</th><th>item</th><th>published</th><th>event time</th><th>first seen</th></tr>"
        f"{rows or '<tr><td colspan=7>none material (see news state)</td></tr>'}</table>"
        "<h2>Decision record</h2><pre>" + html.escape(json.dumps(
            {k: sig.get(k) for k in ("quote", "plan", "decided_at", "expires_at", "assessment")},
            indent=1, default=str)) + "</pre>")
    page = ("<!doctype html><html><head><meta charset=utf-8><meta name=viewport "
            "content='width=device-width,initial-scale=1'><title>Brent signal</title><style>"
            ":root{--bg:#fff;--fg:#1a1a1a;--mute:#666;--line:#ddd}"
            "@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#e8e8e8;--mute:#999;--line:#333}}"
            "body{background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif;"
            "margin:0 auto;max-width:900px;padding:16px}a{color:inherit}"
            "table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid var(--line);"
            "padding:4px;text-align:left;vertical-align:top;font-size:13px}"
            "pre{white-space:pre-wrap;font-size:12px}.note{color:var(--mute)}"
            f"</style></head><body>{body}</body></html>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")
    return path
