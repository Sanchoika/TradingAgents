"""Demo-runner settings, read from an optional TOML file."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# OANDA instrument -> ticker TradingAgents analyses. Index CFDs are analysed
# through the ETF that tracks them, because the agents' data tools need a stock
# or ETF ticker.
DEFAULT_INSTRUMENTS = {"NAS100_USD": "QQQ", "SPX500_USD": "SPY"}


@dataclass
class DemoConfig:
    instruments: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_INSTRUMENTS))
    risk_pct: float = 0.5           # % of NAV lost if the stop is hit
    atr_period: int = 14
    atr_mult: float = 2.0           # stop distance = atr_mult * ATR(daily)
    max_spread_atr: float = 0.1     # skip the entry if spread > this fraction of ATR
    max_drawdown_pct: float = 10.0  # stop opening trades once NAV falls this far from the start
    max_units: dict[str, float] = field(default_factory=dict)
    journal_dir: str = str(Path.home() / ".tradingagents" / "demo")
    # Overrides applied on top of TradingAgents' DEFAULT_CONFIG (llm_provider, models, ...).
    llm: dict = field(default_factory=dict)

    def __post_init__(self):
        if not 0 < self.risk_pct <= 2:
            raise ValueError("risk_pct must be in (0, 2]; this is a demo test, keep risk small")
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
