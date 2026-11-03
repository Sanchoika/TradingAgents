"""Results from the Brent journal, kept in four separate parts, plus the status page.

1. Operational reliability (delivery, freshness, suppressions).
2. Directional usefulness (signed move at +60/+240 min in daily-ATR units).
3. Hypothetical paper trading after modelled costs (in R).
4. Manual results: only what you record yourself with `manual`.
"""

from __future__ import annotations

import html
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
    exits = [e for e in events if e["event"] == "paper_exit"]
    rs = [e["net_r"] for e in exits]
    by_kind: dict[str, list[float]] = {}
    for e in exits:
        s = signals.get(e["id"], {}).get("setup", {})
        by_kind.setdefault(f"{s.get('kind', '?')} {s.get('direction', '?')}", []).append(e["net_r"])
    paper = {
        "fills": sum(1 for e in events if e["event"] == "paper_fill"),
        "missed": sum(1 for e in events if e["event"] == "paper_missed"),
        "closed": len(rs),
        "net_r_total": round(sum(rs), 2) if rs else 0.0,
        "net_r_mean": round(mean(rs), 3) if rs else None,
        "net_r_mean_ci95": _ci(rs),
        "win_rate": round(sum(r > 0 for r in rs) / len(rs), 2) if rs else None,
        "exit_reasons": dict(Counter(e["reason"] for e in exits)),
        "by_setup": {k: {"n": len(v), "mean_r": round(mean(v), 3)} for k, v in by_kind.items()},
    }
    manual = [e for e in events if e["event"] == "manual"]
    return {"operational": op, "directional": direction, "paper_hypothetical": paper,
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
