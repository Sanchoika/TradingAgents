"""One daily pass: rate each instrument, move the demo position to match."""

from __future__ import annotations

from collections.abc import Callable

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
        return rating, final_state.get("final_trade_decision", "")

    return analyze


def fixed_analyzer(rating: str) -> Analyzer:
    """No LLM: always returns ``rating``. For free plumbing checks."""
    return lambda ticker, trade_date: (rating, f"fixed rating {rating} (no LLM run)")


def run_once(broker, analyze: Analyzer, cfg: DemoConfig, journal: Journal,
             trade_date: str, dry_run: bool = False) -> list[dict]:
    """Rate every configured instrument and align the demo position. Returns journal rows."""
    rows = []
    nav = broker.nav()
    state = journal.state()
    if "start_nav" not in state and not dry_run:
        state["start_nav"] = nav
        journal.save_state(state)
    start_nav = state.get("start_nav", nav)
    drawdown = 100.0 * (start_nav - nav) / start_nav if start_nav else 0.0
    halted = drawdown >= cfg.max_drawdown_pct
    rows.append(journal.write("run_start", trade_date=trade_date, nav=nav, start_nav=start_nav,
                              drawdown_pct=round(drawdown, 2), halted=halted, dry_run=dry_run))

    for instrument, ticker in cfg.instruments.items():
        try:
            rows.extend(_one(broker, analyze, cfg, journal, instrument, ticker,
                             trade_date, nav, dry_run, halted))
        except Exception as exc:  # one instrument failing must not stop the others
            rows.append(journal.write("error", instrument=instrument, ticker=ticker,
                                      error=f"{type(exc).__name__}: {exc}"))
    return rows


def _one(broker, analyze, cfg, journal, instrument, ticker, trade_date, nav, dry_run, halted):
    rows = []
    rating, decision = analyze(ticker, trade_date)
    target = plan.target_direction(rating)
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
    spec = broker.instrument(instrument)
    quote = broker.quote(instrument)
    if not quote.tradeable:
        return journal.write("skip", instrument=instrument, reason="market closed / not tradeable")
    a = plan.atr(broker.daily_candles(instrument, count=cfg.atr_period + 6), cfg.atr_period)
    if quote.spread > cfg.max_spread_atr * a:
        return journal.write("skip", instrument=instrument, reason="spread too wide",
                             spread=quote.spread, atr=a)
    distance = cfg.atr_mult * a
    units = plan.position_units(nav, cfg.risk_pct, distance, quote.quote_to_home,
                                spec.units_precision, cfg.max_units.get(instrument))
    if units < spec.min_units:
        return journal.write("skip", instrument=instrument, reason="size below minimum",
                             units=units, min_units=spec.min_units)
    entry = quote.ask if direction > 0 else quote.bid
    stop = plan.stop_price(direction, entry, distance, spec.display_precision)
    signed = units * direction
    info = {"instrument": instrument, "rating": rating, "units": signed, "quote_entry": entry, "stop": stop,
                "atr": round(a, 6), "spread": round(quote.spread, 6),
                "risk_amount": round(nav * cfg.risk_pct / 100, 2)}
    if dry_run:
        return journal.write("open_dry_run", **info)
    fill = broker.market_order(instrument, signed, stop, comment=f"{rating}")
    opened = fill.get("tradeOpened") or {}
    return journal.write("open", **info, trade_id=opened.get("tradeID"), price=fill.get("price"),
                         half_spread_cost=fill.get("halfSpreadCost"))
