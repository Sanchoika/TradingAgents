"""python -m tradingagents.brent {check,feasibility,run,status,stop,report,dashboard,manual}

Alerts only. No command here can place, change or close an order.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import RULES_VERSION, load_config
from .store import AlreadyRunning, Store


def _client(cfg):
    from .client import ReadOnlyOanda, token_from_env_or_prompt

    return ReadOnlyOanda(token_from_env_or_prompt())


def _sender(cfg):
    """Telegram through the existing notify command (e.g. hermes); console when unset."""
    import subprocess

    def send(text: str) -> tuple[bool, float]:
        t0 = time.monotonic()
        if not cfg.notify_command:
            print(f"\n{text}\n", flush=True)
            return True, time.monotonic() - t0
        # Own session: the notify tool can't signal (e.g. Ctrl+C) the monitor's process group.
        r = subprocess.run(cfg.notify_command, shell=True, input=text, text=True, timeout=60,
                           capture_output=True, start_new_session=(os.name != "nt"))
        if r.returncode:
            print(f"notify exit {r.returncode}: {(r.stderr or r.stdout)[-300:]}", file=sys.stderr)
        return r.returncode == 0, time.monotonic() - t0

    return send


def cmd_check(args, cfg) -> int:
    """Read-only: instrument details, account currency and hedging, quote, open Brent trades."""
    c = _client(cfg)
    s = c.summary()
    i = c.instrument(cfg.instrument)
    q = c.quote(cfg.instrument)
    now = datetime.now(UTC)
    out = {
        "account": {"id_suffix": str(c.account_id)[-4:], "currency": s.get("currency"),
                    "hedgingEnabled": s.get("hedgingEnabled"), "NAV": s.get("NAV")},
        "instrument": {k: i.get(k) for k in ("name", "type", "displayName", "pipLocation",
                                             "displayPrecision", "tradeUnitsPrecision",
                                             "minimumTradeSize", "maximumOrderUnits",
                                             "marginRate", "guaranteedStopLossOrderMode",
                                             "financing")},
        "quote": {"bid": q.bid, "ask": q.ask, "spread": round(q.spread, 4), "time": q.time.isoformat(),
                  "age_seconds": round(q.age(now), 2), "rtt_seconds": round(q.rtt, 3),
                  "tradeable": q.tradeable, "usd_to_account_ccy": q.quote_to_home},
        "open_trades": c.open_trades(cfg.instrument),
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_feasibility(args, cfg) -> int:
    """Measure quote age, request time and (with --send-test) Telegram send time."""
    from .engine import p90

    c = _client(cfg)
    ages, rtts = [], []
    for _ in range(args.samples):
        q = c.quote(cfg.instrument)
        ages.append(q.age(datetime.now(UTC)))
        rtts.append(q.rtt)
        time.sleep(1)
    send_s = None
    if args.send_test:
        ok, send_s = _sender(cfg)(f"[BRENT TEST] feasibility check {datetime.now(UTC):%H:%M:%S} UTC. "
                                  "Ignore: measures Telegram delivery. No order, no signal.")
        if not ok:
            print("test message FAILED to send")
    qa, rt = p90(ages), p90(rtts)
    budget = cfg.validity_seconds - (qa or 0) - (send_s or 0)
    print(json.dumps({"quote_age_p90_s": qa, "request_p90_s": rt, "max_quote_age_s": max(ages),
                      "telegram_send_s": send_s, "validity_s": cfg.validity_seconds,
                      "usable_after_delays_s": round(budget, 1),
                      "min_usable_s": cfg.min_usable_seconds}, indent=2))
    if qa is not None and qa > cfg.max_quote_age_seconds:
        print("NOT FEASIBLE: quotes are older than max_quote_age_seconds.")
        return 1
    if budget < cfg.min_usable_seconds:
        print("NOT FEASIBLE at this window: delays leave too little time. Consider a longer "
              "validity window (a measurement-based choice) or quote-only alerts.")
        return 1
    print("Feasible at this window, by these measurements. Phone display time after Telegram "
          "accepts the message is not measured here: compare the alert's 'Sent' time with when "
          "your phone shows it.")
    return 0


def cmd_run(args, cfg) -> int:
    from .engine import Monitor
    from .report import write_page

    store = Store(cfg.journal_dir)
    try:
        store.acquire()
    except AlreadyRunning as exc:
        print(f"error: {exc}. Only one Brent monitor may run.", file=sys.stderr)
        return 3
    stopping = {"flag": False}

    def _stop(signum, _frame):
        stopping["flag"] = True
        stopping["why"] = f"signal {signum}"

    signal.signal(signal.SIGTERM, _stop)
    try:
        mon = Monitor(cfg, _client(cfg), store, _sender(cfg))
        print(f"Brent monitor {RULES_VERSION} cfg {cfg.config_hash()} journal {store.dir}. "
              "Alerts only, no orders. Ctrl+C to stop.", flush=True)
        mon.startup()
        last_page = [0.0]

        def on_tick():
            if time.monotonic() - last_page[0] < 300:
                return
            last_page[0] = time.monotonic()
            try:
                _pages(cfg, store, mon.st, True, write_page)
            except OSError as exc:
                print(f"status page not written: {exc}", file=sys.stderr)

        mon.run(stop=lambda: stopping["flag"], on_tick=on_tick)
    except KeyboardInterrupt:
        stopping["why"] = "KeyboardInterrupt (Ctrl+C or SIGINT)"
    except Exception as exc:
        stopping["why"] = f"{type(exc).__name__}: {exc}"[:300]
        raise
    finally:
        why = stopping.get("why", "stop requested")
        store.write("stop", reason=why)
        store.release()
        print(f"Brent monitor stopped: {why}", flush=True)
    return 0


def _pages(cfg, store, state, locked, write_page):
    paths = [write_page(state, store.events(), locked, Path(cfg.journal_dir) / "brent.html")]
    if cfg.site_dir:
        paths.append(write_page(state, store.events(), locked, Path(cfg.site_dir) / "index.html"))
    return paths


def cmd_status(args, cfg) -> int:
    from .report import status_lines

    store = Store(cfg.journal_dir)
    print("\n".join(status_lines(store.state(), store.is_locked())))
    return 0


def cmd_stop(args, cfg) -> int:
    store = Store(cfg.journal_dir)
    if not store.is_locked():
        print("no Brent monitor is running")
        return 0
    pid = int(store.lock_path.read_text().strip() or "0")
    if not pid:
        print("monitor is running but its PID is unknown; stop it in its window (Ctrl+C)")
        return 1
    os.kill(pid, signal.SIGTERM)
    print(f"sent stop to Brent monitor (pid {pid})")
    return 0


def cmd_report(args, cfg) -> int:
    from .report import summarize, write_scorecard

    result = summarize(Store(cfg.journal_dir).events())
    print(json.dumps(result, indent=2))
    if cfg.notes_dir:
        print(f"scorecard: {write_scorecard(result, cfg.notes_dir)}")
    return 0


def cmd_dashboard(args, cfg) -> int:
    from .report import write_page

    store = Store(cfg.journal_dir)
    for p in _pages(cfg, store, store.state(), store.is_locked(), write_page):
        print(f"page: {p}")
    return 0


def cmd_manual(args, cfg) -> int:
    """Record what YOU did with a signal. The only source of 'actual' results."""
    store = Store(cfg.journal_dir)
    if not any(e["event"] == "signal" and e["id"] == args.id for e in store.events()):
        print(f"error: no signal {args.id} in the journal", file=sys.stderr)
        return 2
    store.write("manual", id=args.id, entered=args.entered, price=args.price, units=args.units,
                result=args.result, note=args.note, source="your input")
    print(f"recorded manual note for {args.id}")
    return 0


def cmd_news_check(args, cfg) -> int:
    """Phase 1: collect and timestamp news, optionally classify with the bounded model.
    Logs only; sends no Telegram message and changes no decision."""
    from .news import Budget, NewsCollector, analyze, default_model

    lock = Store(Path(cfg.journal_dir) / "newscheck")
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    col = NewsCollector(cfg.journal_dir)
    model = default_model(cfg.llm_provider, cfg.llm_model) if args.analyze else None
    budget = Budget(cfg.llm_per_hour, cfg.llm_per_day, tuple(cfg.llm_quiet_et))
    end = time.monotonic() + args.minutes * 60
    try:
        while True:
            new = col.poll()
            line = f"{datetime.now(UTC):%H:%M:%S} new items {len(new)}; {col.feed_health()}"
            if model and new:
                res = analyze(col, model, budget)
                line += f"; analysis: {res.get('state', {}).get('state', res.get('note'))}"
                if res.get("refused") or res.get("error"):
                    line += f" ({res.get('refused') or res.get('error')})"
            print(line, flush=True)
            if args.once or time.monotonic() >= end:
                break
            time.sleep(cfg.news_poll_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()
    return 0


def cmd_news_report(args, cfg) -> int:
    from .news import report

    print(json.dumps(report(cfg.journal_dir), indent=2, default=str))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m tradingagents.brent", description=__doc__)
    p.add_argument("--config", help="TOML settings file (see brent.example.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="read-only: instrument, account currency/hedging, quote, trades")
    f = sub.add_parser("feasibility", help="measure quote age and Telegram send time")
    f.add_argument("--samples", type=int, default=30)
    f.add_argument("--send-test", action="store_true", help="send one [BRENT TEST] message")
    sub.add_parser("run", help="start the monitor (foreground; one instance only)")
    sub.add_parser("status", help="show the monitor's saved state (offline)")
    sub.add_parser("stop", help="stop a running monitor")
    sub.add_parser("report", help="results in four separate parts")
    sub.add_parser("dashboard", help="write the read-only status page")
    m = sub.add_parser("manual", help="record what you did with a signal")
    m.add_argument("id")
    g = m.add_mutually_exclusive_group(required=True)
    g.add_argument("--entered", action="store_true", dest="entered")
    g.add_argument("--skipped", action="store_false", dest="entered")
    m.add_argument("--price", type=float)
    m.add_argument("--units", type=float)
    m.add_argument("--result", type=float, help="realised P&L in account currency, if closed")
    m.add_argument("--note", default="")
    n = sub.add_parser("news-check", help="phase 1: collect news (+ --analyze); logs only")
    n.add_argument("--once", action="store_true")
    n.add_argument("--minutes", type=float, default=60.0)
    n.add_argument("--analyze", action="store_true", help="classify new items (bounded model)")
    sub.add_parser("news-report", help="feed delays, gaps, duplicates, content access, model use")
    args = p.parse_args(argv)
    from .client import OandaReadError

    try:
        cfg = load_config(args.config)
        return {"check": cmd_check, "feasibility": cmd_feasibility, "run": cmd_run,
                "status": cmd_status, "stop": cmd_stop, "report": cmd_report,
                "dashboard": cmd_dashboard, "manual": cmd_manual,
                "news-check": cmd_news_check, "news-report": cmd_news_report}[args.cmd](args, cfg)
    except (OandaReadError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
