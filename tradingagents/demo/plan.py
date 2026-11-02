"""Pure decision logic: rating -> target position, stop distance and size.

No I/O here, so every rule is unit-tested without a broker or an LLM.
"""

from __future__ import annotations

import math

# Rating -> direction. Overweight/Underweight trade like Buy/Sell; the size is
# set by the risk budget, not by how strong the wording was.
DIRECTION = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}


MILD = ("Overweight", "Underweight")


def target_direction(rating: str | None, mild_size: float = 1.0) -> int | None:
    """+1 long, -1 short, 0 flat; None for REVIEW or anything unreadable (no action).

    With ``mild_size`` 0, Overweight and Underweight mean flat rather than a trade.
    """
    name = (rating or "").strip().capitalize()
    if name in MILD and mild_size == 0:
        return 0
    return DIRECTION.get(name)


def size_factor(rating: str | None, mild_size: float = 1.0) -> float:
    """Share of the full risk budget a rating trades: Buy/Sell 1, Overweight/Underweight mild_size."""
    return mild_size if (rating or "").strip().capitalize() in MILD else 1.0


def current_direction(trades: list[dict]) -> int:
    units = sum(float(t["currentUnits"]) for t in trades)
    return (units > 0) - (units < 0)


def atr(candles: list[dict], period: int = 14) -> float:
    """Wilder-free simple average true range over the last ``period`` candles."""
    if len(candles) < period + 1:
        raise ValueError(f"need {period + 1} daily candles for ATR({period}), got {len(candles)}")
    trs = []
    for prev, cur in zip(candles[-period - 1:-1], candles[-period:], strict=False):
        trs.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"])))
    return sum(trs) / period


def position_units(nav: float, risk_pct: float, stop_distance: float, quote_to_home: float,
                   units_precision: int = 0, max_units: float | None = None) -> float:
    """Units whose loss at the stop is about ``risk_pct``% of NAV (rounded down)."""
    if stop_distance <= 0 or quote_to_home <= 0:
        raise ValueError("stop distance and conversion factor must be positive")
    raw = nav * risk_pct / 100.0 / (stop_distance * quote_to_home)
    if max_units is not None:
        raw = min(raw, max_units)
    step = 10 ** -units_precision
    return math.floor(raw / step) * step if units_precision else float(math.floor(raw))


def stop_price(direction: int, entry: float, distance: float, precision: int) -> str:
    price = entry - distance if direction > 0 else entry + distance
    return f"{price:.{precision}f}"


def actions(current: int, target: int | None) -> list[str]:
    """What to do to move from the current to the target direction."""
    if target is None:
        return ["skip"]
    if current == target:
        return ["keep"]
    steps = ["close"] if current != 0 else []
    if target != 0:
        steps.append("open")
    return steps
