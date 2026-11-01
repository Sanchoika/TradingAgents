"""python -m tradingagents.demo {check,run,report}  (OANDA practice account only)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from .config import load_config
from .journal import Journal


def _broker():
    from .oanda import OandaPractice, credentials_from_env

    token, account = credentials_from_env()
    return OandaPractice(token, account)


def cmd_check(args, cfg) -> int:
    """Free: connect, show the account and each instrument's price/ATR. No orders, no LLM."""
    from . import plan
    from .oanda import OandaError

    broker = _broker()
    s = broker.summary()
    print(f"OANDA practice account {broker.account_id}: NAV {s['NAV']} {s['currency']}, "
          f"open trades {s.get('openTradeCount')}")
    ok = True
    for inst, ticker in cfg.instruments.items():
        try:
            q = broker.quote(inst)
            a = plan.atr(broker.daily_candles(inst, cfg.atr_period + 6), cfg.atr_period)
            print(f"  {inst:<12} (analysed as {ticker}): bid {q.bid} ask {q.ask} "
                  f"spread {q.spread:.4g} ATR {a:.4g} tradeable={q.tradeable}")
        except (OandaError, ValueError) as exc:
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

    analyze = fixed_analyzer(args.fixed_rating) if args.fixed_rating else tradingagents_analyzer(cfg)
    rows = run_once(_broker(), analyze, cfg, Journal(cfg.journal_dir),
                    trade_date=args.date or date.today().isoformat(), dry_run=args.dry_run)
    for r in rows:
        r = {k: v for k, v in r.items() if k != "decision"}
        print(json.dumps(r, default=str))
    return 1 if any(r["event"] == "error" for r in rows) else 0


def cmd_report(args, cfg) -> int:
    from .report import summarize

    journal = Journal(cfg.journal_dir)
    spread = {str(e["trade_id"]): float(e.get("half_spread_cost") or 0)
              for e in journal.events() if e["event"] == "open" and e.get("trade_id")}
    broker = _broker()
    result = summarize(broker.our_closed_trades(), spread)
    result["open_trades"] = len(broker.our_open_trades())
    print(json.dumps(result, indent=2))
    if result.get("trades") and not result["enough_data"]:
        print("Fewer than 150 closed trades: too early to judge, keep it running.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m tradingagents.demo", description=__doc__)
    p.add_argument("--config", help="TOML settings file (see demo.example.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="test the OANDA connection and keys (free)")
    r = sub.add_parser("run", help="rate each instrument and adjust demo positions")
    r.add_argument("--dry-run", action="store_true", help="decide and log, but send no orders")
    r.add_argument("--date", help="analysis date YYYY-MM-DD (default: today)")
    r.add_argument("--fixed-rating", choices=["Buy", "Overweight", "Hold", "Underweight", "Sell"],
                   help="skip the LLM and use this rating (free plumbing test)")
    sub.add_parser("report", help="score closed demo trades vs a random baseline")
    args = p.parse_args(argv)
    from .oanda import OandaError

    try:
        cfg = load_config(args.config)
        return {"check": cmd_check, "run": cmd_run, "report": cmd_report}[args.cmd](args, cfg)
    except (OandaError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
