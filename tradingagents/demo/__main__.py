"""python -m tradingagents.demo {check,run,report}: paper or OANDA-practice forward test."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from .config import load_config
from .journal import Journal


def _broker(cfg):
    if cfg.broker == "paper":
        from pathlib import Path

        from .paper import PaperBroker

        return PaperBroker(Path(cfg.journal_dir) / "paper_account.json", cfg.paper_start_cash,
                           cfg.paper_spread_bps)
    from .oanda import OandaPractice, credentials_from_env

    token, account = credentials_from_env()
    return OandaPractice(token, account)


def cmd_check(args, cfg) -> int:
    """Free: connect, show the account and each instrument's price/ATR. No orders, no LLM."""
    from . import plan

    broker = _broker(cfg)
    s = broker.summary()
    print(f"{cfg.broker} account {broker.account_id}: NAV {s['NAV']} {s['currency']}, "
          f"open trades {s.get('openTradeCount')}")
    ok = True
    for inst, ticker in cfg.instruments.items():
        try:
            q = broker.quote(inst)
            a = plan.atr(broker.daily_candles(inst, cfg.atr_period + 6), cfg.atr_period)
            print(f"  {inst:<12} (analysed as {ticker}): bid {q.bid} ask {q.ask} "
                  f"spread {q.spread:.4g} ATR {a:.4g} tradeable={q.tradeable}")
        except Exception as exc:  # report every instrument, then fail
            ok = False
            print(f"  {inst:<12} ERROR: {exc}")
    _check_llm_key(cfg)
    return 0 if ok else 1


def _check_llm_key(cfg) -> None:
    import os

    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.llm_clients.api_key_env import get_api_key_env

    provider = cfg.llm.get("llm_provider", DEFAULT_CONFIG["llm_provider"])
    env = get_api_key_env(provider)
    if env is None:
        print(f"LLM provider {provider}: no API key needed")
    else:
        print(f"LLM provider {provider}: {env} is {'set' if os.environ.get(env) else 'MISSING'}")


def cmd_run(args, cfg) -> int:
    from .runner import fixed_analyzer, run_once, tradingagents_analyzer

    try:
        analyze = fixed_analyzer(args.fixed_rating) if args.fixed_rating else tradingagents_analyzer(cfg)
        rows = run_once(_broker(cfg), analyze, cfg, Journal(cfg.journal_dir),
                        trade_date=args.date or date.today().isoformat(), dry_run=args.dry_run)
    except Exception as exc:  # a crashed run must not be silent
        if cfg.notify_command and not args.dry_run:
            from .notes import failure_text, notify

            try:
                notify(cfg.notify_command, failure_text(exc))
            except Exception as notify_exc:
                print(f"notify failed: {notify_exc}", file=sys.stderr)
        raise
    for r in rows:
        r = {k: v for k, v in r.items() if k != "decision"}
        print(json.dumps(r, default=str))
    if not args.dry_run:
        _publish(rows, cfg, trade_date=args.date or date.today().isoformat())
    return 1 if any(r["event"] == "error" for r in rows) else 0


def _dashboard(cfg):
    from pathlib import Path

    from .dashboard import write_dashboard

    paper = Path(cfg.journal_dir) / "paper_account.json" if cfg.broker == "paper" else None
    return write_dashboard(Journal(cfg.journal_dir), paper, Path(cfg.journal_dir) / "dashboard.html")


def cmd_dashboard(args, cfg) -> int:
    path = _dashboard(cfg)
    print(f"dashboard: {path}")
    if args.open:
        import webbrowser

        webbrowser.open(path.resolve().as_uri())
    return 0


def _publish(rows, cfg, trade_date) -> None:
    """Note, dashboard and notification; a failure here is reported but never fails the run."""
    from .notes import notify, run_summary, write_run_note

    try:
        print(f"dashboard: {_dashboard(cfg)}")
    except Exception as exc:
        print(f"dashboard not written: {exc}", file=sys.stderr)

    if cfg.notes_dir:
        try:
            print(f"note: {write_run_note(rows, cfg.notes_dir, trade_date)}")
        except OSError as exc:
            print(f"note not written: {exc}", file=sys.stderr)
    if cfg.notify_command:
        try:
            code = notify(cfg.notify_command, run_summary(rows))
            if code:
                print(f"notify command exited with {code}", file=sys.stderr)
        except Exception as exc:
            print(f"notify failed: {exc}", file=sys.stderr)


def cmd_report(args, cfg) -> int:
    from .report import summarize

    journal = Journal(cfg.journal_dir)
    spread = {str(e["trade_id"]): float(e.get("half_spread_cost") or 0)
              for e in journal.events() if e["event"] == "open" and e.get("trade_id")}
    broker = _broker(cfg)
    if hasattr(broker, "sync"):
        broker.sync()
    closed = broker.our_closed_trades()
    # The paper account records the spread it charged per trade; spread is its half per side.
    spread.update({str(t["id"]): t["spread_cost"] / 2 for t in closed if "spread_cost" in t})
    result = summarize(closed, spread)
    result["open_trades"] = len(broker.our_open_trades())
    print(json.dumps(result, indent=2))
    if cfg.notes_dir:
        from .notes import write_scorecard

        print(f"scorecard: {write_scorecard(result, cfg.notes_dir)}")
    if result.get("trades") and not result["enough_data"]:
        print("Fewer than 150 closed trades: too early to judge, keep it running.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m tradingagents.demo", description=__doc__)
    p.add_argument("--config", help="TOML settings file (see demo.example.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="test the account, prices and keys (free)")
    r = sub.add_parser("run", help="rate each instrument and adjust demo positions")
    r.add_argument("--dry-run", action="store_true", help="decide and log, but send no orders")
    r.add_argument("--date", help="analysis date YYYY-MM-DD (default: today)")
    r.add_argument("--fixed-rating", choices=["Buy", "Overweight", "Hold", "Underweight", "Sell"],
                   help="skip the LLM and use this rating (free plumbing test)")
    sub.add_parser("report", help="score closed demo trades vs a random baseline")
    d = sub.add_parser("dashboard", help="write dashboard.html from the journal (offline)")
    d.add_argument("--open", action="store_true", help="open it in the browser")
    args = p.parse_args(argv)
    from .oanda import OandaError

    try:
        cfg = load_config(args.config)
        return {"check": cmd_check, "run": cmd_run, "report": cmd_report,
                "dashboard": cmd_dashboard}[args.cmd](args, cfg)
    except (OandaError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
