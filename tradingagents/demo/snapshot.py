"""Read-only snapshot of the paper test for the dashboard.

Reads the saved journal, paper account and state files and returns plain data.
It never settles orders, runs analyses, writes account files or places trades:
the paper account file is parsed as JSON, not opened through ``PaperBroker``.

Two optional extras use the network read-only, at build time only (never when
the page is opened): last-close prices for open positions (``prices=True``)
and the Windows Task Scheduler entries for the demo (``schedule=True``).
Every value says when it was recorded; anything not recorded is ``None``.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

SCHEMA = 1
# Fields copied from the config. Commands, paths and anything secret are left out on purpose.
CONFIG_FIELDS = ("broker", "mode", "hold_days", "max_analyses_per_run", "retry_days", "risk_pct",
                 "mild_size", "atr_period", "atr_mult", "max_open_positions", "max_net_positions",
                 "max_drawdown_pct", "paper_start_cash", "paper_spread_bps", "paper_borrow_bps_per_year",
                 "benchmark")
DECISION_TEXT_LIMIT = 4000
TASK_NAMES = ("TradingAgentsDemo", "TradingAgentsWatchdog")


def _read_json(path: Path):
    if not path.exists():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as exc:
        return None, f"unreadable: {type(exc).__name__}"


def _read_journal(path: Path):
    if not path.exists():
        return [], "missing", 0
    rows, bad = [], 0
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        bad += 1
    except OSError as exc:
        return [], f"unreadable: {type(exc).__name__}", 0
    return rows, None, bad


def _planned_exit(filled_on: str | None, hold_days: int) -> str | None:
    """First run date on which the hold rule asks for the exit (weekdays only, holidays not counted)."""
    if not filled_on:
        return None
    return str(np.busday_offset(filled_on, hold_days, roll="forward"))


def _report_file(path: str | None) -> Path | None:
    """The saved full report for a decision: complete_report.html next to the .md if present."""
    if not path:
        return None
    p = Path(path)
    html = p.with_suffix(".html")
    for cand in (html, p):
        if cand.exists() and cand.is_file():
            return cand
    return None


def _scheduled_tasks() -> dict:
    """Read-only query of the demo's Windows scheduled tasks; empty off Windows."""
    out = {}
    if not sys.platform.startswith("win"):
        return out
    for name in TASK_NAMES:
        try:
            res = subprocess.run(["schtasks", "/Query", "/TN", name, "/FO", "LIST", "/V"],
                                 capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if res.returncode != 0:
            out[name] = {"found": False}
            continue
        info = {"found": True}
        for line in res.stdout.splitlines():
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if key in ("Next Run Time", "Status", "Last Run Time", "Last Result", "Scheduled Task State"):
                info[key.lower().replace(" ", "_")] = value
        out[name] = info
    return out


def build_snapshot(cfg, *, prices: bool = True, schedule: bool = True, bars=None,
                   now: datetime | None = None) -> dict:
    """Everything the dashboard shows, from saved files only (plus the optional read-only extras)."""
    now = (now or datetime.now(UTC)).astimezone()
    jdir = Path(cfg.journal_dir)
    events, journal_err, bad_lines = _read_journal(jdir / "journal.jsonl")
    paper, paper_err = _read_json(jdir / "paper_account.json") if cfg.broker == "paper" else (None, "not a paper account")
    state, state_err = _read_json(jdir / "state.json")
    sources = {
        "journal": {"status": journal_err or "ok", "rows": len(events), "bad_lines": bad_lines},
        "paper_account": {"status": paper_err or "ok"},
        "state": {"status": state_err or "ok"},
    }
    for name, f in (("journal", "journal.jsonl"), ("paper_account", "paper_account.json"), ("state", "state.json")):
        p = jdir / f
        if p.exists():
            sources[name]["modified"] = datetime.fromtimestamp(p.stat().st_mtime, UTC).astimezone().isoformat(
                timespec="seconds")

    from tradingagents.default_config import DEFAULT_CONFIG

    llm = {**DEFAULT_CONFIG, **getattr(cfg, "llm", {})}
    config = {k: getattr(cfg, k) for k in CONFIG_FIELDS if hasattr(cfg, k)}
    config["universe_size"] = len(getattr(cfg, "universe", []) or [])
    config["llm"] = f"{llm.get('llm_provider')}: {llm.get('quick_think_llm')} (quick) / {llm.get('deep_think_llm')} (deep)"

    # -- walk the journal once: runs, decisions and what happened to each ---------
    runs, decisions, activity = [], [], []
    current, dry = None, False
    trade_link = {}  # trade id -> decision index
    for e in events:
        ev, ts = e.get("event"), e.get("ts")
        if ev == "run_start":
            dry = bool(e.get("dry_run"))
            current = {"trade_date": e.get("trade_date"), "started": ts, "dry_run": dry,
                       "nav": e.get("nav"), "start_nav": e.get("start_nav"), "drawdown_pct": e.get("drawdown_pct"),
                       "halted": e.get("halted"), "missed_runs": e.get("missed_runs", 0),
                       "benchmark": e.get("benchmark"), "benchmark_pct": e.get("benchmark_pct"),
                       "llm": e.get("llm"), "mode": e.get("mode"), "last_event": ts,
                       "counts": {"decisions": 0, "orders": 0, "skips": 0, "errors": 0, "closes": 0, "fills": 0}}
            runs.append(current)
        elif current is not None:
            current["last_event"] = ts
        item = {"ts": ts, "event": ev, "dry_run": dry, "run": current["trade_date"] if current else None}
        for k in ("instrument", "ticker", "rating", "reason", "error", "trade_id", "price", "pl", "units",
                  "realizedPL", "close_price", "since", "reported", "analysing", "skipped_over_limit",
                  "retrying", "edgar_check", "report_times", "trade_date", "actions", "stop"):
            if k in e:
                item[k] = e[k]
        activity.append(item)
        if current is None:
            continue
        c = current["counts"]
        if ev == "decision":
            c["decisions"] += 1
            if not dry:
                rep = _report_file(e.get("report"))
                decisions.append({
                    "ts": ts, "symbol": e.get("ticker") or e.get("instrument"), "trade_date": e.get("trade_date"),
                    "rating": e.get("rating"), "target": e.get("target"), "actions": e.get("actions", []),
                    "rationale": (e.get("decision") or "")[:DECISION_TEXT_LIMIT], "model": current.get("llm"),
                    "report_src": str(rep) if rep else None, "report_recorded": bool(e.get("report")),
                    "status": None, "status_reason": None, "trade_id": None,
                    "report_time": None, "edgar_check": None})
        elif ev in ("open", "open_dry_run"):
            c["orders"] += 1
            if ev == "open" and not dry and decisions and decisions[-1]["symbol"] == e.get("instrument"):
                decisions[-1]["status"], decisions[-1]["trade_id"] = "order placed", e.get("trade_id")
                if e.get("trade_id"):
                    trade_link[str(e["trade_id"])] = len(decisions) - 1
        elif ev == "skip":
            c["skips"] += 1
            if not dry and decisions and decisions[-1]["symbol"] == e.get("instrument") and not decisions[-1]["status"]:
                decisions[-1]["status"], decisions[-1]["status_reason"] = "skipped", e.get("reason")
        elif ev == "error":
            c["errors"] += 1
            sym = e.get("ticker") or e.get("instrument")
            if not dry and sym and not str(e.get("error", "")).startswith("earnings lookup"):
                decisions.append({"ts": ts, "symbol": sym, "trade_date": current["trade_date"], "rating": None,
                                  "target": None, "actions": [], "rationale": "", "model": current.get("llm"),
                                  "report_src": None, "report_recorded": False, "status": "failed",
                                  "status_reason": e.get("error"), "trade_id": None,
                                  "report_time": None, "edgar_check": None})
        elif ev in ("close", "paper_close"):
            c["closes"] += 1
        elif ev == "paper_fill":
            c["fills"] += 1
        elif ev == "earnings_scan" and not dry:
            for sym in e.get("skipped_over_limit", []) or []:
                decisions.append({"ts": ts, "symbol": sym, "trade_date": current["trade_date"], "rating": None,
                                  "target": None, "actions": [], "rationale": "", "model": current.get("llm"),
                                  "report_src": None, "report_recorded": False, "status": "not analysed",
                                  "status_reason": f"over the {getattr(cfg, 'max_analyses_per_run', '?')}-per-run "
                                                   "limit; retried on later runs",
                                  "trade_id": None, "report_time": (e.get("report_times") or {}).get(sym),
                                  "edgar_check": (e.get("edgar_check") or {}).get(sym)})
            current["scan"] = {"reported": e.get("reported", []), "analysing": e.get("analysing", []),
                               "skipped_over_limit": e.get("skipped_over_limit", []),
                               "retrying": e.get("retrying", [])}
            current["_times"] = e.get("report_times") or {}
            current["_edgar"] = e.get("edgar_check") or {}
    # earnings timing + EDGAR check onto each decision of the same run
    by_run = {r["trade_date"]: r for r in runs if not r["dry_run"]}
    for d in decisions:
        r = by_run.get(d["trade_date"]) or {}
        d["report_time"] = d["report_time"] or (r.get("_times") or {}).get(d["symbol"])
        d["edgar_check"] = d["edgar_check"] or (r.get("_edgar") or {}).get(d["symbol"])
        if d["status"] is None and d["rating"] is not None:
            acts = d["actions"]
            if d["rating"] == "REVIEW":
                d["status"], d["status_reason"] = "no action", "rating unreadable (REVIEW)"
            elif "halted" in acts:
                d["status"], d["status_reason"] = "halted", "drawdown limit reached; no new trades"
            elif d["target"] in (0, None):
                d["status"], d["status_reason"] = "no trade", "Hold rating"
            elif "open" in acts:
                d["status"], d["status_reason"] = "no order recorded", None
            else:
                d["status"] = ", ".join(acts) or "no action"
    for r in runs:
        r.pop("_times", None)
        r.pop("_edgar", None)

    # -- account --------------------------------------------------------------
    account = None
    if paper is not None:
        start_cash = float(getattr(cfg, "paper_start_cash", 100_000.0))
        closed = paper.get("closed", [])
        realized = sum(float(t.get("realizedPL", 0)) for t in closed)
        financing = sum(float(t.get("financing", 0)) for t in closed)
        spread = sum(float(t.get("spread_cost", 0)) for t in closed)
        real_runs = [r for r in runs if not r["dry_run"]]
        last = real_runs[-1] if real_runs else None
        unrealized_at_run = None
        if last and last.get("nav") is not None and last.get("started"):
            cash_then = start_cash + sum(float(t.get("realizedPL", 0)) + float(t.get("financing", 0))
                                         for t in closed if (t.get("recorded") or "") <= last["started"])
            unrealized_at_run = float(last["nav"]) - cash_then
        positions = []
        hold = int(getattr(cfg, "hold_days", 5))
        for t in paper.get("open", []):
            link = trade_link.get(str(t.get("id")))
            positions.append({
                "id": str(t.get("id")), "symbol": t.get("instrument"),
                "direction": "long" if float(t.get("units", 0)) > 0 else "short",
                "units": abs(float(t.get("units", 0))), "status": t.get("status"),
                "entry": t.get("price"), "stop": t.get("stop"), "stop_distance": t.get("stop_distance"),
                "ordered_on": t.get("ordered_on"), "filled_on": t.get("filled_on"),
                "exit_requested_on": t.get("close_ordered_on"),
                "planned_exit_request": _planned_exit(t.get("filled_on"), hold) if getattr(cfg, "mode", "") == "earnings" else None,
                "rating": t.get("comment"), "decision_index": link, "mark": None})
        account = {"cash": paper.get("cash"), "start_cash": start_cash, "realized_pl": realized,
                   "financing": financing, "spread_cost": spread, "closed": closed, "positions": positions,
                   "nav_last_run": last.get("nav") if last else None,
                   "nav_last_run_time": last.get("started") if last else None,
                   "unrealized_last_run": unrealized_at_run}

    # -- optional read-only extras (network, build time only) -----------------
    extras = {"prices": None, "ratings_score": None, "vs_sector_fund": None, "score": None}
    if account is not None:
        from .report import rating_decisions, score_ratings, summarize

        closed = account["closed"]
        spread_half = {str(t["id"]): float(t.get("spread_cost", 0)) / 2 for t in closed}
        extras["score"] = summarize(closed, spread_half, n_random=2000) if closed else {"trades": 0}
        if prices:
            if bars is None:
                from .paper import yahoo_bars as bars
            today = now.date().isoformat()
            marks_ok = 0
            for p in account["positions"]:
                try:
                    df = bars(p["symbol"], today).dropna(subset=["Close"])
                    close, day = float(df["Close"].iloc[-1]), str(df["Date"].iloc[-1])[:10]
                    p["mark"] = {"close": close, "date": day}
                    if p["status"] != "pending" and p.get("entry") is not None:
                        sign = 1 if p["direction"] == "long" else -1
                        p["mark"]["unrealized"] = sign * p["units"] * (close - float(p["entry"]))
                    marks_ok += 1
                except Exception as exc:  # shown as "unavailable", never as zero
                    p["mark"] = {"error": f"{type(exc).__name__}"}
            extras["prices"] = {"fetched": now.isoformat(timespec="seconds"), "ok": marks_ok,
                                "of": len(account["positions"])}
            try:
                extras["ratings_score"] = score_ratings(rating_decisions(events), bars,
                                                        int(getattr(cfg, "hold_days", 5)), today=today)
            except Exception as exc:
                extras["ratings_score"] = {"error": type(exc).__name__}
            if closed:
                from .sectors import excess_vs_sector

                try:
                    extras["vs_sector_fund"] = excess_vs_sector(closed, bars)
                except Exception as exc:
                    extras["vs_sector_fund"] = {"error": type(exc).__name__}

    return {
        "schema": SCHEMA, "built_at": now.isoformat(timespec="seconds"), "built_tz": now.tzname(),
        "built_on_host": socket.gethostname(), "sources": sources, "config": config,
        "runs": runs, "decisions": decisions, "activity": activity[-400:], "activity_total": len(activity),
        "account": account, "extras": extras, "state": {k: state[k] for k in ("last_run_date", "last_earnings_scan",
                                                                               "start_nav", "benchmark_start")
                                                        if isinstance(state, dict) and k in state},
        "retry_queue": (state or {}).get("retry", {}) if isinstance(state, dict) else {},
        "schedule": {"checked": now.isoformat(timespec="seconds"), "tasks": _scheduled_tasks()} if schedule else None,
        "notifications": {"recorded": False,
                          "note": "Telegram send results are not written to the journal; see demo.log on the PC."},
        "completion": {"recorded": False,
                       "note": "The runner records when a run starts, not when it finishes."},
    }
