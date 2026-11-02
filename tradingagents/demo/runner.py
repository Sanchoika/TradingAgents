"""One pass of the demo runner: rate instruments, move the demo positions to match."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from . import plan
from .config import DemoConfig
from .journal import Journal

# (ticker, trade_date) -> (rating, decision text)
Analyzer = Callable[[str, str], tuple[str, str]]


def tradingagents_analyzer(cfg: DemoConfig) -> Analyzer:
    """The real thing: one full TradingAgents run per call (paid LLM calls)."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = {**DEFAULT_CONFIG, **cfg.llm}
    graph = TradingAgentsGraph(debug=False, config=config)

    def analyze(ticker: str, trade_date: str) -> tuple[str, str]:
        final_state, rating = graph.propagate(ticker, trade_date)
        try:  # keep the full multi-agent report for later review; never fail the run over it
            folder = Path(cfg.journal_dir) / "reports" / f"{trade_date}_{ticker}"
            analyze.reports[ticker] = str(graph.save_reports(final_state, ticker, save_path=folder))
        except Exception:
            logging.getLogger(__name__).warning("could not save the report for %s", ticker, exc_info=True)
        return rating, final_state.get("final_trade_decision", "")

    analyze.reports = {}
    return analyze


def fixed_analyzer(rating: str) -> Analyzer:
    """No LLM: always returns ``rating``. For free plumbing checks."""
    return lambda ticker, trade_date: (rating, f"fixed rating {rating} (no LLM run)")


# ticker -> earnings report times (UTC timestamps)
EarningsTimes = Callable[[str], list]


def edgar_earnings(ticker: str) -> list:
    from tradingagents.dataflows.vendors.sec_earnings import earnings_release_times

    return earnings_release_times(ticker)


def _edgar_check(ticker: str, yahoo_ts: pd.Timestamp, edgar) -> str:
    """Whether SEC EDGAR has an earnings 8-K within 36 hours of Yahoo's report time."""
    try:
        times = edgar(ticker)
    except Exception:
        return "EDGAR unavailable"
    if not times:
        return "not an EDGAR filer"
    near = any(abs(pd.Timestamp(t) - yahoo_ts) <= pd.Timedelta(hours=36) for t in times)
    return "confirmed by EDGAR" if near else "NOT confirmed by EDGAR"


def yahoo_earnings(ticker: str) -> list:
    from tradingagents.dataflows.vendors.yahoo.earnings import earnings_times

    return earnings_times(ticker)


def run_once(broker, analyze: Analyzer, cfg: DemoConfig, journal: Journal,
             trade_date: str, dry_run: bool = False, earnings: EarningsTimes | None = None,
             now: datetime | None = None, edgar: EarningsTimes | None = None) -> list[dict]:
    """One pass of the configured mode. Returns the journal rows it wrote."""
    rows = []
    if hasattr(broker, "sync") and not dry_run:  # paper account: settle queued fills and stops
        rows += [journal.write(e.pop("event"), **e) for e in broker.sync()]
    nav = broker.nav()
    state = journal.state()
    bench = _benchmark_price(broker, cfg)
    last_run = state.get("last_run_date")
    # Weekday runs the schedule should have made between the last run and this one.
    missed = max(int(np.busday_count(last_run, trade_date)) - 1, 0) if last_run else 0
    if not dry_run:
        state.setdefault("start_nav", nav)
        if bench is not None:
            state.setdefault("benchmark_start", bench)
        state["last_run_date"] = trade_date
        journal.save_state(state)
    start_nav = state.get("start_nav", nav)
    drawdown = 100.0 * (start_nav - nav) / start_nav if start_nav else 0.0
    halted = drawdown >= cfg.max_drawdown_pct
    bench_start = state.get("benchmark_start")
    bench_pct = round(100.0 * (bench / bench_start - 1), 2) if bench and bench_start else None
    rows.append(journal.write("run_start", trade_date=trade_date, nav=nav, start_nav=start_nav,
                              drawdown_pct=round(drawdown, 2), halted=halted, dry_run=dry_run,
                              mode=cfg.mode, missed_runs=missed, benchmark=cfg.benchmark,
                              llm=_llm_label(cfg),
                              benchmark_pct=bench_pct))
    if cfg.mode == "earnings":
        rows += _earnings_pass(broker, analyze, cfg, journal, trade_date, nav, dry_run, halted,
                               earnings or yahoo_earnings, now or datetime.now(UTC),
                               edgar or edgar_earnings)
        return rows

    for instrument, ticker in cfg.instruments.items():
        try:
            rows.extend(_one(broker, analyze, cfg, journal, instrument, ticker,
                             trade_date, nav, dry_run, halted))
        except Exception as exc:  # one instrument failing must not stop the others
            rows.append(journal.write("error", instrument=instrument, ticker=ticker,
                                      error=f"{type(exc).__name__}: {exc}"))
    return rows


def _llm_label(cfg) -> str:
    """Which models made this run's ratings, so a model change is visible in the journal."""
    from tradingagents.default_config import DEFAULT_CONFIG

    c = {**DEFAULT_CONFIG, **cfg.llm}
    return f"{c['llm_provider']}:{c['quick_think_llm']}/{c['deep_think_llm']}"


def _benchmark_price(broker, cfg):
    """Benchmark (default SPY) mid price, for comparing the account with buy-and-hold."""
    if not cfg.benchmark:
        return None
    try:
        return broker.quote(cfg.benchmark).mid
    except Exception:  # an OANDA account may not quote the ETF; the run goes on without it
        return None


def _one(broker, analyze, cfg, journal, instrument, ticker, trade_date, nav, dry_run, halted):
    rows = []
    rating, decision = analyze(ticker, trade_date)
    target = plan.target_direction(rating, cfg.mild_size)
    trades = broker.our_open_trades(instrument)
    current = plan.current_direction(trades)
    steps = plan.actions(current, target)
    if halted and "open" in steps:
        steps.remove("open")
        if not steps:
            steps = ["halted"]
    rows.append(journal.write("decision", instrument=instrument, ticker=ticker,
                              trade_date=trade_date, rating=rating, target=target,
                              current=current, actions=steps, decision=decision[:2000]))

    if "close" in steps:
        for t in trades:
            if dry_run:
                rows.append(journal.write("close_dry_run", instrument=instrument, trade_id=t["id"]))
                continue
            fill = broker.close_trade(t["id"])
            rows.append(journal.write("close", instrument=instrument, trade_id=t["id"],
                                      price=fill.get("price"), pl=fill.get("pl"),
                                      half_spread_cost=fill.get("halfSpreadCost")))

    if "open" in steps:
        rows.append(_open(broker, cfg, journal, instrument, rating, target, nav, dry_run))
    return rows


def _open(broker, cfg, journal, instrument, rating, direction, nav, dry_run):
    if cfg.max_open_positions and len(broker.our_open_trades()) >= cfg.max_open_positions:
        return journal.write("skip", instrument=instrument, rating=rating,
                             reason=f"{cfg.max_open_positions} positions already open")
    spec = broker.instrument(instrument)
    quote = broker.quote(instrument)
    if not quote.tradeable:
        return journal.write("skip", instrument=instrument, reason="market closed / not tradeable")
    a = plan.atr(broker.daily_candles(instrument, count=cfg.atr_period + 6), cfg.atr_period)
    if quote.spread > cfg.max_spread_atr * a:
        return journal.write("skip", instrument=instrument, reason="spread too wide",
                             spread=quote.spread, atr=a)
    distance = cfg.atr_mult * a
    risk_pct = cfg.risk_pct * plan.size_factor(rating, cfg.mild_size)
    units = plan.position_units(nav, risk_pct, distance, quote.quote_to_home,
                                spec.units_precision, cfg.max_units.get(instrument))
    if units < spec.min_units:
        return journal.write("skip", instrument=instrument, reason="size below minimum",
                             units=units, min_units=spec.min_units)
    entry = quote.ask if direction > 0 else quote.bid
    stop = plan.stop_price(direction, entry, distance, spec.display_precision)
    signed = units * direction
    info = {"instrument": instrument, "rating": rating, "units": signed, "quote_entry": entry, "stop": stop,
                "atr": round(a, 6), "spread": round(quote.spread, 6),
                "risk_amount": round(nav * risk_pct / 100, 2)}
    if dry_run:
        return journal.write("open_dry_run", **info)
    fill = broker.market_order(instrument, signed, stop, comment=f"{rating}")
    opened = fill.get("tradeOpened") or {}
    return journal.write("open", **info, trade_id=opened.get("tradeID"), price=fill.get("price"),
                         half_spread_cost=fill.get("halfSpreadCost"))


def _session(ts: pd.Timestamp) -> str:
    """Whether a report came out before the US open, during the session, or after the close."""
    et = ts.tz_localize("UTC").tz_convert("America/New_York") if ts.tzinfo is None \
        else ts.tz_convert("America/New_York")
    minutes = et.hour * 60 + et.minute
    return "before open" if minutes < 570 else ("after close" if minutes >= 960 else "during session")


def _earnings_pass(broker, analyze, cfg, journal, trade_date, nav, dry_run, halted, earnings, now, edgar):
    """Close trades held hold_days, then rate the universe stocks that just reported."""
    rows = []
    for t in broker.our_open_trades():
        if t.get("status") == "open" and np.busday_count(t["opened"], trade_date) >= cfg.hold_days:
            if dry_run:
                rows.append(journal.write("close_dry_run", instrument=t["instrument"], trade_id=t["id"]))
            else:
                broker.close_trade(t["id"])
                rows.append(journal.write("close", instrument=t["instrument"], trade_id=t["id"],
                                          reason=f"held {cfg.hold_days} trading days"))

    state = journal.state()
    since = pd.Timestamp(state["last_earnings_scan"]) if "last_earnings_scan" in state \
        else pd.Timestamp(now - timedelta(days=1))
    now_ts = pd.Timestamp(now)
    held = {t["instrument"] for t in broker.our_open_trades()}
    reported, report_times, edgar_check = [], {}, {}
    for ticker in cfg.universe:
        try:
            hits = [pd.Timestamp(ts) for ts in earnings(ticker) if since < pd.Timestamp(ts) <= now_ts]
            if hits:
                reported.append(ticker)
                report_times[ticker] = f"{hits[0].isoformat()} ({_session(hits[0])})"
                edgar_check[ticker] = _edgar_check(ticker, hits[0], edgar)
        except Exception as exc:
            rows.append(journal.write("error", instrument=ticker, error=f"earnings lookup: {exc}"))
    # Stocks that failed or were over the limit last time get another chance first,
    # for up to cfg.retry_days trading days after they reported.
    retry = {t: d for t, d in state.get("retry", {}).items()
             if np.busday_count(d, trade_date) <= cfg.retry_days}
    first_seen = {**dict.fromkeys(reported, trade_date), **retry}
    todo = [t for t in dict.fromkeys([*retry, *reported]) if t not in held]
    now_list, skipped = todo[:cfg.max_analyses_per_run], todo[cfg.max_analyses_per_run:]
    rows.append(journal.write("earnings_scan", since=since.isoformat(), reported=reported,
                              retrying=list(retry), analysing=now_list, skipped_over_limit=skipped,
                              report_times=report_times, edgar_check=edgar_check))

    failed = []
    for ticker in now_list:
        try:
            rating, decision = analyze(ticker, trade_date)
            target = plan.target_direction(rating, cfg.mild_size)
            steps = ["open"] if target and not halted else (["halted"] if target else ["skip"])
            rows.append(journal.write("decision", instrument=ticker, ticker=ticker, trade_date=trade_date,
                                      rating=rating, target=target, current=0, actions=steps,
                                      decision=decision[:4000],
                                      report=getattr(analyze, "reports", {}).get(ticker)))
            if steps == ["open"]:
                rows.append(_open(broker, cfg, journal, ticker, rating, target, nav, dry_run))
        except Exception as exc:
            failed.append(ticker)
            rows.append(journal.write("error", instrument=ticker, ticker=ticker,
                                      error=f"{type(exc).__name__}: {exc}"))
    if not dry_run:
        state["last_earnings_scan"] = now_ts.isoformat()
        state["retry"] = {t: first_seen[t] for t in [*failed, *skipped]}
        journal.save_state(state)
    return rows
