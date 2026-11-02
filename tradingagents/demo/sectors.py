"""Compare each closed trade with its sector fund, so a rising market is not mistaken for skill."""

from __future__ import annotations

import pandas as pd

# The 100 stocks in demo.earnings.example.toml, mapped to the SPDR sector fund that holds them.
SECTOR_ETF = {
    **dict.fromkeys(["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "AMD", "CRM", "ADBE", "INTC", "QCOM", "CSCO",
                     "IBM", "PLTR"], "XLK"),
    **dict.fromkeys(["AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "UBER", "SHOP"], "XLY"),
    **dict.fromkeys(["GOOGL", "META", "NFLX", "DIS"], "XLC"),
    **dict.fromkeys(["JPM", "BAC", "GS", "MS", "V", "MA", "PYPL", "AXP", "BLK", "C"], "XLF"),
    **dict.fromkeys(["UNH", "LLY", "JNJ", "PFE", "MRK", "ABBV", "TMO"], "XLV"),
    **dict.fromkeys(["XOM", "CVX", "COP"], "XLE"),
    **dict.fromkeys(["WMT", "COST", "KO", "PEP"], "XLP"),
    **dict.fromkeys(["BA", "CAT", "GE", "HON", "UNP", "RTX", "LMT", "DE", "UPS"], "XLI"),
    # second 50 (added 2026-11-02, before the first trade)
    **dict.fromkeys(["MU", "TXN", "AMAT", "LRCX", "NOW", "INTU", "PANW", "ANET", "ADI", "KLAC"], "XLK"),
    **dict.fromkeys(["T", "VZ", "TMUS", "CMCSA"], "XLC"),
    **dict.fromkeys(["WFC", "SCHW", "SPGI", "CB", "PGR", "COF"], "XLF"),
    **dict.fromkeys(["ABT", "AMGN", "ISRG", "GILD", "BMY", "CVS", "MDT", "VRTX"], "XLV"),
    **dict.fromkeys(["LOW", "BKNG", "TJX", "CMG", "ABNB"], "XLY"),
    **dict.fromkeys(["PG", "PM", "MDLZ", "TGT"], "XLP"),
    "SLB": "XLE", "LIN": "XLB",
    **dict.fromkeys(["NEE", "DUK", "SO"], "XLU"),
}


def _open_on_or_after(df: pd.DataFrame, day: str) -> float | None:
    dates = pd.to_datetime(df["Date"]).dt.date.astype(str)
    later = df[dates >= day]
    return float(later.iloc[0]["Open"]) if not later.empty else None


def excess_vs_sector(closed: list[dict], bars) -> dict:
    """Mean excess return per trade (percent) over the sector fund, in the trade's own direction.

    ``bars(symbol, as_of_date)`` returns daily bars with Date and Open. Trades with no sector fund
    or no prices are left out and counted.
    """
    excess, skipped = [], 0
    cache: dict[str, pd.DataFrame] = {}
    for t in closed:
        etf = SECTOR_ETF.get(t["instrument"])
        side = 1 if t["units"] > 0 else -1
        try:
            if etf is None:
                raise KeyError(t["instrument"])
            df = cache.setdefault(etf, bars(etf, t["closed"]))
            a, b = _open_on_or_after(df, t["opened"]), _open_on_or_after(df, t["closed"])
            if a is None or b is None:
                raise ValueError("no sector prices")
            stock = side * (t["close_price"] / t["price"] - 1)
            excess.append(100 * (stock - side * (b / a - 1)))
        except Exception:
            skipped += 1
    n = len(excess)
    return {"trades": n, "skipped": skipped,
            "mean_excess_pct": round(sum(excess) / n, 2) if n else None,
            "share_beating_sector": round(sum(e > 0 for e in excess) / n, 2) if n else None}
