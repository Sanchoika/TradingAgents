"""Earnings report times from Yahoo, for the demo runner's earnings mode."""

from __future__ import annotations

import pandas as pd
import yfinance as yf

from tradingagents.dataflows.symbols import normalize_symbol
from tradingagents.dataflows.vendors.yahoo.common import yf_retry


def earnings_times(symbol: str, limit: int = 8) -> list[pd.Timestamp]:
    """Recent and upcoming earnings report times (UTC), newest first; [] if none."""
    canonical = normalize_symbol(symbol)
    frame = yf_retry(lambda: yf.Ticker(canonical).get_earnings_dates(limit=limit))
    if frame is None or len(frame) == 0:
        return []
    stamps = pd.DatetimeIndex(frame.index)
    stamps = stamps.tz_localize("UTC") if stamps.tz is None else stamps.tz_convert("UTC")
    return sorted(stamps, reverse=True)
