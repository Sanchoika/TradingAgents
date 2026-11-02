"""Demo-runner settings, read from an optional TOML file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# OANDA instrument -> ticker TradingAgents analyses. Index CFDs are analysed
# through the ETF that tracks them, because the agents' data tools need a stock
# or ETF ticker.
DEFAULT_INSTRUMENTS = {"NAS100_USD": "QQQ", "SPX500_USD": "SPY"}
# The paper account trades the Yahoo symbol itself.
DEFAULT_PAPER_INSTRUMENTS = {"QQQ": "QQQ", "SPY": "SPY"}


@dataclass
class DemoConfig:
    # "paper": built-in paper account on free Yahoo prices; "oanda": OANDA practice account.
    broker: str = "paper"
    paper_start_cash: float = 100_000.0
    paper_spread_bps: float = 5.0   # round-trip spread charged by the paper account, in basis points
    # "watchlist": rate every instrument each run. "earnings": rate only universe
    # stocks that reported earnings since the last run, hold hold_days trading days.
    mode: str = "watchlist"
    universe: list[str] = field(default_factory=list)
    hold_days: int = 5
    max_analyses_per_run: int = 5   # keeps free-tier LLM limits in reach
    retry_days: int = 2             # retry a failed or skipped reporter for this many trading days
    # Optional: folder (e.g. inside an Obsidian vault) that gets a Markdown note per run.
    notes_dir: str = ""
    benchmark: str = "SPY"          # buy-and-hold comparison in summaries ("" to turn off)
    # Optional: shell command run after each real run; the run summary arrives on its stdin.
    notify_command: str = ""
    # traded symbol -> ticker TradingAgents analyses; a plain list means the same symbol for both.
    instruments: dict[str, str] | list[str] = field(default_factory=dict)
    risk_pct: float = 0.5           # % of NAV lost if the stop is hit
    # Share of risk_pct an Overweight/Underweight trades (Buy/Sell trade all of it); 0 = no trade.
    mild_size: float = 0.5
    atr_period: int = 14
    atr_mult: float = 2.0           # stop distance = atr_mult * ATR(daily)
    max_spread_atr: float = 0.1     # skip the entry if spread > this fraction of ATR
    max_open_positions: int = 0     # skip new trades while this many are open (0 = no limit)
    max_drawdown_pct: float = 10.0  # stop opening trades once NAV falls this far from the start
    max_units: dict[str, float] = field(default_factory=dict)
    journal_dir: str = str(Path.home() / ".tradingagents" / "demo")
    # Overrides applied on top of TradingAgents' DEFAULT_CONFIG (llm_provider, models, ...).
    llm: dict = field(default_factory=dict)

    def __post_init__(self):
        if not 0 < self.risk_pct <= 2:
            raise ValueError("risk_pct must be in (0, 2]; this is a demo test, keep risk small")
        if isinstance(self.instruments, list):
            self.instruments = {t: t for t in self.instruments}
        if not self.instruments:
            self.instruments = dict(DEFAULT_PAPER_INSTRUMENTS if self.broker == "paper" else DEFAULT_INSTRUMENTS)
        if self.broker not in ("paper", "oanda"):
            raise ValueError('broker must be "paper" or "oanda"')
        if self.mode not in ("watchlist", "earnings"):
            raise ValueError('mode must be "watchlist" or "earnings"')
        if self.mode == "earnings" and (self.broker != "paper" or not self.universe):
            raise ValueError("earnings mode needs broker = \"paper\" and a universe of stock tickers")
        if not 0 <= self.mild_size <= 1:
            raise ValueError("mild_size must be between 0 and 1")
        if self.atr_mult <= 0 or self.atr_period < 2:
            raise ValueError("atr_mult must be > 0 and atr_period >= 2")


def load_config(path: str | Path | None) -> DemoConfig:
    if path is None:
        return DemoConfig()
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    known = DemoConfig.__dataclass_fields__
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ValueError(f"unknown settings in {path}: {', '.join(unknown)}")
    return DemoConfig(**data)
