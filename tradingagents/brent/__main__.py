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


def _client(cfg, tries: int = 6, sleep=time.sleep):
    """Ask for the token once, then connect, retrying network errors and HTTP 5xx with
    backoff (a single timeout must not end the monitor). A 4xx (bad token) fails at once."""
    import requests

    from .client import OandaReadError, ReadOnlyOanda, token_from_env_or_prompt

    token = token_from_env_or_prompt()
    for i in range(tries):
        try:
            return ReadOnlyOanda(token)
        except (requests.RequestException, OandaReadError) as exc:
            transient = isinstance(exc, requests.RequestException) or " -> HTTP 5" in str(exc)
            if not transient or i == tries - 1:
                raise
            wait = min(60, 5 * 2 ** i)
            print(f"OANDA not reachable ({type(exc).__name__}); retrying in {wait}s", flush=True)
            sleep(wait)
    raise RuntimeError("unreachable")


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
        mon = Monitor(cfg, _client(cfg), store, _sender(cfg), cfg_path=getattr(args, "config", None))
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
    from .report import news_health

    nh = news_health(cfg.journal_dir, cfg=cfg)
    paths = [write_page(state, store.events(), locked, Path(cfg.journal_dir) / "brent.html", news=nh)]
    if cfg.site_dir:
        paths.append(write_page(state, store.events(), locked, Path(cfg.site_dir) / "index.html",
                                news=nh))
    return paths


def cmd_status(args, cfg) -> int:
    from .report import news_health, status_lines

    store = Store(cfg.journal_dir)
    print("\n".join(status_lines(store.state(), store.is_locked(), news=news_health(cfg.journal_dir, cfg=cfg))))
    return 0


def cmd_stop(args, cfg) -> int:
    """Clean stop (SIGTERM) of the price monitor, or of the news collector with --news."""
    name = "news collector" if getattr(args, "news", False) else "monitor"
    store = Store(Path(cfg.journal_dir) / "newscheck") if name != "monitor" else Store(cfg.journal_dir)
    if not store.is_locked():
        print(f"no Brent {name} is running")
        return 0
    pid = int(store.lock_path.read_text().strip() or "0")
    if not pid:
        print(f"{name} is running but its PID is unknown; stop it in its window (Ctrl+C)")
        return 1
    os.kill(pid, signal.SIGTERM)
    print(f"sent stop to Brent {name} (pid {pid})")
    return 0


def cmd_report(args, cfg) -> int:
    from .report import summarize, write_scorecard

    result = summarize(Store(cfg.journal_dir).events())
    print(json.dumps(result, indent=2))
    if cfg.notes_dir:
        print(f"scorecard: {write_scorecard(result, cfg.notes_dir)}")
    return 0


def cmd_replay(args, cfg) -> int:
    from .report import replay_by_version

    print(json.dumps(replay_by_version(Store(cfg.journal_dir).events()), indent=2, default=str))
    return 0


def ping_url(url: str, timeout: float = 10.0) -> bool:
    """GET an external heartbeat URL (healthchecks-style). The URL is a secret: never printed."""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (operator-set https URL)
            return 200 <= resp.status < 300
    except Exception:
        return False


def cmd_health(args, cfg) -> int:
    """Exit 0 when the monitor ticks and the news collector progresses, else 1 (one line each reason).

    With --ping-file, also pings the external heartbeat URL stored in that file (root-only): the
    URL on success, URL + /fail on a problem, so an outside service sees both a crashed process
    and a dead host (no ping at all)."""
    from .report import news_health, overall_health

    store = Store(cfg.journal_dir)
    nh = news_health(cfg.journal_dir, cfg=cfg)
    bad = overall_health(store.state(), store.is_locked(), nh, datetime.now(UTC))
    print("OK" if not bad else "DEGRADED: " + "; ".join(bad))
    if args.notify:
        from tradingagents.ops import notices

        msgs = notices.step(Path(cfg.journal_dir) / "health_notices.json", bad, datetime.now(UTC))
        if msgs:
            from .messages import ops_text

            text = ops_text("\n".join(msgs))
            ok, _ = _sender(cfg)(text)
            store.write("ops_notice_sent", accepted=ok, text=text)
    if args.ping_file:
        url = Path(args.ping_file).read_text(encoding="utf-8").strip()
        if url:
            sent = ping_url(url if not bad else url.rstrip("/") + "/fail")
            if not sent:
                print("heartbeat ping failed (network or service)", file=sys.stderr)
    return 1 if bad else 0


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
    """News collector: poll feeds and classify pending items with the bounded model.

    Runs until stopped (production) unless --once or --minutes N (bounded test). Pending
    items are retried on later polls even when nothing new arrives (after a quiet window,
    budget refusal or failed call), with exponential backoff after failures. It also watches
    the price monitor's heartbeat and sends one [BRENT OPS] notice per 30 min if it stopped.
    """
    from . import news

    lock = Store(Path(cfg.journal_dir) / "newscheck")
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        print(f"error: {exc}. Only one Brent news collector may run.", file=sys.stderr)
        return 3
    def _term(*_):
        raise KeyboardInterrupt

    try:
        old_term = signal.signal(signal.SIGTERM, _term)
    except ValueError:  # not in the main thread (tests)
        old_term = None
    col = news.NewsCollector(cfg.journal_dir)
    model = news.default_model(cfg.llm_provider, cfg.llm_model) if args.analyze else None
    budget = news.Budget(cfg.llm_per_hour, cfg.llm_per_day, tuple(cfg.llm_quiet_et),
                         reserve_urgent=cfg.llm_reserve_urgent)
    end = time.monotonic() + args.minutes * 60 if args.minutes else None
    retry, fails, next_try = True, 0, 0.0   # True at start: pick up items left pending
    watch = _MonitorWatch(cfg)
    try:
        while True:
            new = col.poll()
            line = f"{datetime.now(UTC):%H:%M:%S} new items {len(new)}; {col.feed_health()}"
            if model and (new or retry) and time.monotonic() >= next_try:
                res = news.analyze(col, model, budget, batch_wait_s=news.BATCH_WAIT_S)
                line += f"; analysis: {(res.get('state') or {}).get('state', res.get('note'))}"
                if res.get("refused") or res.get("error"):
                    line += f" ({res.get('refused') or res.get('error')})"
                left = (res.get("backlog") or 0) - len(res.get("item_keys") or [])
                retry = bool(res.get("refused") or res.get("error") or left > 0
                             or "batching" in (res.get("note") or ""))
                if res.get("error"):
                    fails += 1
                    delay = min(1800.0, cfg.news_poll_seconds * 2 ** fails)
                    next_try = time.monotonic() + delay
                    line += f"; retry in {delay / 60:.0f} min"
                elif res.get("called"):
                    fails, next_try = 0, 0.0
            watch.check()
            print(line, flush=True)
            if args.once or (end is not None and time.monotonic() >= end):
                break
            time.sleep(cfg.news_poll_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        lock.release()
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)
    return 0


class _MonitorWatch:
    """The news collector's view of the price monitor: lock held and a recent tick."""

    STALE_S = 120
    REPEAT_S = 1800

    def __init__(self, cfg):
        self.cfg, self.last_sent = cfg, 0.0

    def problem(self) -> str:
        store = Store(self.cfg.journal_dir)
        if not store.is_locked():
            return "the Brent price monitor is not running"
        tick = (store.state() or {}).get("last_tick")
        if tick:
            age = (datetime.now(UTC) - datetime.fromisoformat(tick)).total_seconds()
            if age > self.STALE_S:
                return f"the Brent price monitor's last tick was {age:.0f}s ago (stuck?)"
        return ""

    def check(self):
        if not self.cfg.notify_command:
            return
        try:
            why = self.problem()
            if why and time.monotonic() - self.last_sent > self.REPEAT_S:
                self.last_sent = time.monotonic()
                from .messages import ops_text

                _sender(self.cfg)(ops_text(f"{why}. No entry alerts while it is down. "
                                           "Restart: bash ~/ta-brent/scripts/brent_activate.sh"))
        except Exception as exc:  # the watch must never stop news collection
            print(f"monitor watch failed: {exc}", file=sys.stderr)


def cmd_news_report(args, cfg) -> int:
    from .news import report

    print(json.dumps(report(cfg.journal_dir), indent=2, default=str))
    return 0


def cmd_weekly(args, cfg) -> int:
    """Weekly review from the journals. --send delivers it (scheduled Sunday run)."""
    from datetime import UTC, datetime

    from . import feedback

    store = Store(cfg.journal_dir)
    now = datetime.now(UTC)
    w = feedback.weekly(store.events(), feedback.latest(cfg.journal_dir), now)
    text = feedback.weekly_text(w, preview=not args.send)
    if args.json:
        print(json.dumps(w, indent=2, default=str))
    print(text)
    if args.send:
        ok, secs = _sender(cfg)(text)
        store.write("weekly_sent", accepted=ok, window=w["window"], seconds=round(secs, 2))
        return 0 if ok else 1
    return 0


def cmd_info_test(args, cfg) -> int:
    """ONE clearly labelled end-to-end INFO test to the configured chat.

    Refuses once a test was accepted by the notify command; a failed attempt (for example the
    notify command not found) sent nothing, so it may be retried."""
    from datetime import UTC, datetime

    store = Store(cfg.journal_dir)
    if any(e["event"] == "info_test" and e.get("accepted") for e in store.events()):
        print("an INFO test was already sent; not sending another", file=sys.stderr)
        return 3
    now = datetime.now(UTC)
    text = ("[BRENT INFO] TEST | END-TO-END CHECK | INFORMATION ONLY | no trade suggested\n"
            "This is a one-time test of the new Brent information messages (briefs, news, levels, "
            "fast moves). Nothing is traded and no order is placed.\n"
            f"Sent {now:%Y-%m-%d %H:%M} UTC. Phone display is not measured unless you confirm it.")
    ok, secs = _sender(cfg)(text)
    store.write("info_test", accepted=ok, seconds=round(secs, 2), text=text)
    print(f"accepted by the notify command: {ok}")
    return 0 if ok else 1


SAMPLE_SIG = {
    "id": "BR-SAMPLE", "label": "chart-led, no material news", "instrument": "BCO_USD",
    "setup": {"direction": "LONG", "zone_lo": 107.80, "zone_hi": 108.05, "stop": 107.20,
              "invalid_close": 107.75},
    "quote": {"time": "2026-11-06T15:41:30+00:00", "bid": 107.93, "ask": 107.96, "spread": 0.03},
    "plan": {"t1": 108.65, "t2": 109.50, "rr1": 0.76, "rr2": 1.61,
             "costs_note": "example costs"},
    "expires_at": "2026-11-06T15:42:30+00:00", "decided_at": "2026-11-06T15:41:30+00:00",
    "news_line": "EXAMPLE: no material Brent news in 6h; no news is not a direction signal.",
    "chart_line": "EXAMPLE: broke yesterday's high 107.75 and retested it.",
    "contradiction": "EXAMPLE: EIA report next week can move price fast.",
    "sizing": "not shown", "skip_reasons": [], "position_note": "EXAMPLE only",
    "policy_row": "no-news", "rules": "example", "config_hash": "example",
    "report_url": "",
}


def sample_text() -> str:
    """The ENTRY card filled with example numbers, labelled SAMPLE in the title and footer."""
    from .messages import alert_text

    text = alert_text(SAMPLE_SIG, {"max_spread": 0.08, "max_hold_hours": 8})
    head, rest = text.split("\n", 1)
    rest = rest.replace("paper · NO ORDER PLACED · BR-SAMPLE",
                        "SAMPLE of the new style · example numbers · not a signal · no order")
    return head.replace("<b>LONG BRENT</b>", "<b>SAMPLE · LONG BRENT</b>") + "\n" + rest


def cmd_style_sample(args, cfg) -> int:
    """Print the sample ENTRY card; with --send, deliver ONE clearly labelled sample."""
    text = sample_text()
    if not args.send:
        print(text)
        return 0
    ok, secs = _sender(cfg)(text)
    Store(cfg.journal_dir).write("style_sample", accepted=ok, seconds=round(secs, 2), text=text)
    print(f"accepted by the notify command: {ok}")
    return 0 if ok else 1


def cmd_second_opinion(args, cfg) -> int:
    """One bounded second-opinion call on a recorded ENTRY (logged only, never sent)."""
    from . import news, secondopinion

    budget = news.Budget(cfg.llm_per_hour, cfg.llm_per_day, tuple(cfg.llm_quiet_et))
    model = news.default_model(cfg.llm_provider, cfg.llm_model)
    row = secondopinion.review(Store(cfg.journal_dir), args.id, model, budget, cfg.journal_dir)
    print(json.dumps(row, indent=2))
    return 0 if row.get("verdict") else 1


def cmd_news_events(args, cfg) -> int:
    from pathlib import Path

    from .events import build_events

    path = Path(cfg.journal_dir) / "news.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []
    evs = build_events(rows)
    print(json.dumps(evs[-args.last:], indent=2, default=str))
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
    st = sub.add_parser("stop", help="stop the running monitor (or --news: the news collector)")
    st.add_argument("--news", action="store_true", help="stop the news collector instead")
    h = sub.add_parser("health", help="exit 0 if healthy; optionally ping an external heartbeat")
    h.add_argument("--notify", action="store_true",
                   help="send a notice when a problem persists 3 checks, bounded reminders, and recovery")
    h.add_argument("--ping-file", help="file holding the heartbeat URL (never printed)")
    sub.add_parser("report", help="results in four separate parts")
    sub.add_parser("replay", help="same progression over each experiment version (read-only)")
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
    wk = sub.add_parser("weekly", help="weekly review (preview by default; --send delivers it)")
    wk.add_argument("--send", action="store_true")
    wk.add_argument("--json", action="store_true")
    ss = sub.add_parser("style-sample", help="show (or --send ONE) labelled sample of the alert style")
    ss.add_argument("--send", action="store_true")
    sub.add_parser("info-test", help="send ONE labelled end-to-end INFO test message")
    so = sub.add_parser("second-opinion", help="one bounded model review of an ENTRY (logged only)")
    so.add_argument("id")
    n = sub.add_parser("news-check", help="news collector (+ --analyze): runs until stopped")
    n.add_argument("--once", action="store_true", help="one poll, then exit (test)")
    n.add_argument("--minutes", type=float, default=0.0,
                   help="stop after this many minutes (bounded test run); 0 = run until stopped")
    n.add_argument("--analyze", action="store_true", help="classify pending items (bounded model)")
    sub.add_parser("news-report", help="feed delays, gaps, duplicates, content access, model use")
    ne = sub.add_parser("news-events", help="shadow event ledger: linked articles per event (read-only)")
    ne.add_argument("--last", type=int, default=10)
    args = p.parse_args(argv)
    from .client import OandaReadError

    try:
        cfg = load_config(args.config)
        return {"check": cmd_check, "feasibility": cmd_feasibility, "run": cmd_run,
                "status": cmd_status, "health": cmd_health, "replay": cmd_replay, "stop": cmd_stop, "report": cmd_report,
                "dashboard": cmd_dashboard, "manual": cmd_manual,
                "news-check": cmd_news_check, "news-report": cmd_news_report,
                "news-events": cmd_news_events, "weekly": cmd_weekly,
                "info-test": cmd_info_test, "style-sample": cmd_style_sample, "second-opinion": cmd_second_opinion}[args.cmd](args, cfg)
    except (OandaReadError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
