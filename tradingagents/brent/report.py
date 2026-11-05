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


PAPER_V1_NOTE = ("paper-v1 records (before 2026-11-03 audit fixes) may be affected: live exits "
                 "used the ask for long targets / bid for short targets, and live stop gaps "
                 "filled at the stop. Kept unchanged; never pooled with paper-v2.")

ARM_DIFFERENCES = (
    "The arms are not matched, so their difference is NOT a measure of news's contribution. "
    "Chart-only (SH-): issued at the first qualifying quote, no news check, no cooldown or "
    "one-at-a-time limit, never sent; its paper clock starts at the decision. Combined (BR-): "
    "needs the news + chart policy, cooldown and one open alert at a time, a final re-check, "
    "and its paper clock starts when the notify command accepted the alert (adds the "
    "notification delay).")


def group_of(sg: dict, accepted: set | None = None) -> str:
    """Reporting group: arm, then (combined) label and whether it was actually sent."""
    if sg.get("arm", "combined") != "combined":
        return "chart-only (shadow, not sent)"
    label = sg.get("label") or "unlabelled"
    base = "news-supported" if label.startswith("news-supported") else (
        "chart-led" if label.startswith("chart-led") else label)
    if "counter-trend" in label:
        base += ", counter-trend"
    ok = sg.get("delivered") if accepted is None else sg.get("id") in accepted
    if sg.get("telegram") and ok:
        sent = "sent (notify command accepted)"
    elif sg.get("telegram"):
        sent = "send failed"
    else:
        sent = "logged, not sent"
    return f"combined {base} | {sent}"


def progression(events: list[dict]) -> dict:
    """Setups detected -> entry zone reached -> evidence ready -> checks passed -> notified.

    Counts are unique setups (keys), not polls. A setup's primary blocker is the last reason it
    was held back; all reasons are listed. Research-only chart-only signals are not counted as
    notifications, and WATCH messages are reported apart.
    """
    detected = {e["key"] for e in events if e["event"] == "watch" and e.get("key")}
    suppressed: dict[str, list[str]] = {}
    for e in events:
        if e["event"] == "suppressed" and e.get("key"):
            suppressed.setdefault(e["key"], []).extend(e["reasons"])
    cand = {e["key"]: e for e in events if e["event"] == "candidate" and e.get("key")}
    zone = (set(suppressed) | set(cand)) & detected
    evidence = set(cand) & detected
    combined = {e["setup"]["key"]: e for e in events if e["event"] == "signal"
                and e.get("arm", "combined") == "combined" and (e.get("setup") or {}).get("key")}
    passed = set(combined) & detected
    ok_ids = {e["id"] for e in events if e["event"] == "sent" and e["kind"] == "alert" and e["ok"]}
    notified = {k for k, sg in combined.items() if sg["id"] in ok_ids and k in detected}
    blockers: Counter = Counter()
    for k in detected - notified:
        if k in cand and cand[k]["action"] != cand[k]["direction"]:
            blockers[f"news/chart policy: {cand[k]['row']}"] += 1
        elif k in suppressed:
            blockers[re.sub(r"[-+]?\d[\d.]*", "#", suppressed[k][-1]).split(" (")[0]] += 1
        elif k not in zone:
            blockers["entry zone never reached (no setup lost)"] += 1
        else:
            blockers["other"] += 1
    watches = {e["id"] for e in events if e["event"] == "watch_notice"}
    return {"setups_detected": len(detected), "entry_zone_reached": len(zone),
            "evidence_ready": len(evidence), "entry_checks_passed": len(passed),
            "notification_accepted": len(notified),
            "primary_blocker_of_setups_not_notified": dict(blockers.most_common()),
            "watch_messages_unique_watches": len(watches),
            "watch_messages_sent": sum(1 for e in events if e["event"] == "watch_notice"),
            "watch_cancelled": sum(1 for e in events if e["event"] == "watch_end"
                                   and e.get("outcome") in ("invalidated", "expired")),
            "watch_to_entry": sum(1 for e in events if e["event"] == "watch_end"
                                  and e.get("outcome") == "entry"),
            "denominators": "unique setups; 'notification_accepted' = notify command accepted the "
                            "ENTRY; phone display is not measured; chart-only shadow signals excluded"}


def summarize(events: list[dict]) -> dict:
    sent = [e for e in events if e["event"] == "sent"]
    alerts = [e for e in sent if e["kind"] == "alert"]
    signals = {e["id"]: e for e in events if e["event"] == "signal"}
    updates = Counter(e["status"] for e in events if e["event"] == "update")
    supp = Counter(re.sub(r"[-+]?\d[\d.]*", "#", r).split(" (")[0]
                   for e in events if e["event"] == "suppressed" for r in e["reasons"])
    send_s = [e["seconds"] for e in sent if e["ok"]]
    accepted = {e["id"] for e in alerts if e["ok"]}
    combined = {i for i, sg in signals.items() if sg.get("arm", "combined") == "combined"}
    shadow = {i for i, sg in signals.items() if sg.get("arm") == "chart-only"}
    op = {
        "signals_issued": len(combined),
        "chart_only_signals": len(shadow),
        "alerts_accepted_by_notify_command": sum(1 for e in alerts if e["ok"]),
        "alerts_failed": sum(1 for e in alerts if not e["ok"]),
        "phone_display_time": "not measured (only notify-command acceptance is)",
        "invalidated": updates.get("invalidated", 0),
        "expired": updates.get("expired", 0),
        "eligibility_lost_in_window": sum(1 for e in events if e["event"] == "eligibility_lost"),
        "send_seconds_median": round(median(send_s), 2) if send_s else None,
        "send_seconds_p90": p90(send_s),
        "setups_armed": sum(1 for e in events if e["event"] == "watch"),
        "suppressed_reasons": dict(supp.most_common(10)),
        "ops_notices": sum(1 for e in events if e["event"] == "ops"),
        "monitor_starts": sum(1 for e in events if e["event"] == "start"),
        "experiments_seen": sorted({(sg.get("experiment") or {}).get("experiment", "brent-v1 (pre-manifest)")
                                    for sg in signals.values()}),
    }

    def gkey(i: str) -> str:
        sg = signals.get(i, {})
        exp = (sg.get("experiment") or {}).get("experiment", "pre-manifest")
        return f"{exp} | {group_of(sg, accepted)}"

    marks: dict[str, dict[str, list[float]]] = {}
    for e in events:
        if e["event"] == "mark" and e["id"] in signals:
            marks.setdefault(gkey(e["id"]), {}).setdefault(str(e["minutes"]), []).append(e["move_atr"])
    direction = {g: {f"+{m}min": {"n": len(v), "mean_move_atr": round(mean(v), 3),
                                  "share_in_alert_direction": round(sum(x > 0 for x in v) / len(v), 2)}
                     for m, v in sorted(ms.items(), key=lambda kv: int(kv[0]))}
                 for g, ms in sorted(marks.items())}

    def paper_stats(ids: set) -> dict:
        exits = [e for e in events if e["event"] == "paper_exit" and e["id"] in ids]
        out = {"signals": len(ids),
               "fills": sum(1 for e in events if e["event"] == "paper_fill" and e["id"] in ids),
               "missed": sum(1 for e in events if e["event"] == "paper_missed" and e["id"] in ids)}
        groups: dict[str, list[dict]] = {}
        for e in exits:
            ver = e.get("paper_rules") or signals.get(e["id"], {}).get("paper_rules") or "paper-v1"
            groups.setdefault(f"{gkey(e['id'])} | {ver}", []).append(e)
        out["by_group"] = {}
        for g, es in sorted(groups.items()):
            rs = [e["net_r"] for e in es]
            by_kind: dict[str, list[float]] = {}
            for e in es:
                st = signals.get(e["id"], {}).get("setup", {})
                by_kind.setdefault(f"{st.get('kind', '?')} {st.get('direction', '?')}", []).append(e["net_r"])
            out["by_group"][g] = {
                "closed": len(rs), "net_r_total": round(sum(rs), 2), "net_r_mean": round(mean(rs), 3),
                "net_r_mean_bootstrap_ci95_descriptive": _ci(rs),
                "win_rate": round(sum(r > 0 for r in rs) / len(rs), 2),
                "exit_reasons": dict(Counter(e["reason"] for e in es)),
                "with_data_gap": sum(1 for e in es if e.get("data_gap")),
                "by_setup": {k: {"n": len(v), "mean_r": round(mean(v), 3)} for k, v in by_kind.items()},
                **({"caution": PAPER_V1_NOTE} if g.endswith("paper-v1") else {}),
            }
        return out

    paper = paper_stats(combined)
    cands = [e for e in events if e["event"] == "candidate"]
    paper["candidates_by_policy_row"] = dict(Counter(f"{e['action']}: {e['row']}" for e in cands))
    chart_only = paper_stats(shadow)
    chart_only["differences_from_combined"] = ARM_DIFFERENCES
    manual = [e for e in events if e["event"] == "manual"]
    return {"progression": progression(events), "operational": op, "directional_by_group": direction, "paper_hypothetical": paper,
            "chart_only_comparison": chart_only,
            "manual_confirmed": {"records": len(manual),
                                 "entered": sum(1 for e in manual if e.get("entered")),
                                 "results": [{k: e.get(k) for k in ("id", "entered", "price", "units",
                                                                    "result", "note")}
                                             for e in manual[-20:]]},
            "note": ("No edge claim. No fixed trade count and no simple confidence interval is "
                     "proof of profitability: signals overlap in time and share news and market "
                     "exposure, so resampled intervals are descriptive only. Groups are kept "
                     "apart by experiment, arm, label, sent/not sent and paper-rules version.")}


def news_health(journal_dir: str | Path, now: datetime | None = None, cfg=None) -> dict:
    """The news collector as a separate process: lock held, and what its log says now."""
    from .news import news_view
    from .store import Store

    now = now or datetime.now(UTC)
    running = Store(Path(journal_dir) / "newscheck").is_locked()
    try:
        v = news_view(journal_dir, now)
    except Exception as exc:  # a broken log must show as a problem, not crash status
        v = {"state": "unavailable", "status": "error", "why": str(exc)[:200]}
    budget = None
    if cfg is not None:
        from .news import budget_usage

        try:
            budget = budget_usage(journal_dir, now, cfg.llm_per_hour, cfg.llm_per_day,
                                  tuple(cfg.llm_quiet_et))
        except Exception as exc:
            budget = {"error": str(exc)[:200]}
    return {"budget": budget, "running": running, "status": v.get("status"), "state": v.get("state"),
            "why": v.get("why", ""), "last_poll_attempt": v.get("last_poll_attempt"),
            "general_news_fresh": (v.get("health") or {}).get("general_news_fresh"),
            "basis": v.get("basis")}


def overall_health(state: dict, locked: bool, news: dict | None, now: datetime) -> list[str]:
    """Reasons the assistant is NOT fully healthy (empty = healthy)."""
    bad = []
    if not locked:
        bad.append("price monitor not running")
    elif not state.get("last_tick") or \
            (now - datetime.fromisoformat(state["last_tick"])).total_seconds() > 30:
        bad.append("price monitor has not ticked in 30s")
    if state.get("context_error"):
        bad.append("chart data refresh failing")
    from .rules import market_closed_by_clock

    if locked and not market_closed_by_clock(now):
        lq = state.get("last_quote")
        if state.get("ops", {}).get("quote_error_count", 0) >= 5:
            bad.append("quotes failing")
        elif lq and (now - datetime.fromisoformat(lq["fetched_at"])).total_seconds() > 120:
            bad.append("no new quote for 120s")
    if news is None:
        bad.append("news collector not checked")
    else:
        if not news["running"]:
            bad.append("news collector not running")
        lp = news.get("last_poll_attempt")
        if news["running"] and lp:
            try:
                if (now - datetime.fromisoformat(lp)).total_seconds() > 15 * 60:
                    bad.append("news collector alive but not polling for 15 min")
            except ValueError:
                pass
        if news["status"] not in ("checked", "pending_analysis"):
            bad.append(f"news {news['status']}: {news['why']}")
    return bad


def status_lines(state: dict, locked: bool, now: datetime | None = None,
                 news: dict | None = None) -> list[str]:
    now = now or datetime.now(UTC)
    bad = overall_health(state, locked, news, now)
    out = ["overall: OK" if not bad else "overall: DEGRADED: " + "; ".join(bad),
           f"monitor running: {'yes' if locked else 'NO'}"]
    if news is not None:
        out.append(f"news collector running: {'yes' if news['running'] else 'NO'}; news "
                   f"{news['status']} ({news['state']}); general news feeds fresh "
                   f"{news['general_news_fresh']}; last poll {news['last_poll_attempt']}; "
                   f"analysis basis {news['basis']}" + (f"; {news['why']}" if news["why"] else ""))
    eff = state.get("effective")
    if eff:
        out.append(f"version: {eff['experiment']} code {eff['code_commit']}; operating range "
                   f"{eff['range'][0]:g}-{eff['range'][1]:g} ({eff['range_convention']}); entry validity "
                   f"{eff['validity_seconds']}s; entry alerts {'ON' if eff['alerts_enabled'] else 'OFF'}, "
                   f"WATCH {'ON' if eff['watch_alerts'] else 'OFF'}; phone display: {eff['phone_display']}")
    b = (news or {}).get("budget")
    if b and "error" not in b:
        out.append(f"model budget (shared, rolling): {b['hour']}/{b['per_hour']} last hour, "
                   f"{b['day']}/{b['per_day']} last 24h; quiet window {b['quiet_window_et']}; "
                   f"refusals 24h {b['refusals_24h'] or 'none'}; tokens 24h {b['tokens_24h']}; "
                   f"median latency {b['latency_median_s']}s. {b['note']}")
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


def write_page(state: dict, events: list[dict], locked: bool, path: Path,
               news: dict | None = None) -> Path:
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
        "<h2>Status</h2><pre>" + html.escape("\n".join(status_lines(state, locked, news=news))) + "</pre>"
        "<h2>Recent signals</h2><table><tr><th>id</th><th>setup</th><th>zone</th><th>stop</th>"
        f"<th>status</th><th>paper net R</th></tr>{rows}</table>"
        "<h2>1. Operational</h2>" + _table(res["operational"]) +
        "<h2>2. Directional (+60/+240 min, ATR units), by group</h2>"
        + _table(res["directional_by_group"] or {"n": 0}) +
        "<h2>3. Paper, hypothetical, after costs (combined arm)</h2>" + _table(res["paper_hypothetical"]) +
        "<h3>Chart-only comparison (not sent)</h3>" + _table(res["chart_only_comparison"]) +
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
