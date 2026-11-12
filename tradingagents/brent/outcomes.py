"""Measurement only (2026-11-12, Irakli): what price did after a research card that never filled,
and a random-entry baseline for each closed paper trade. Never changes a signal, a rule or a count.

Both read 1-minute bid/ask candles (the same data catch_up uses). Pure functions; the engine
fetches the candles and writes the result as its own journal event.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

LONG = "LONG"


def first_hit(bars: list[dict], direction: str, stop: float, target: float,
              start: datetime, until: datetime) -> str:
    """Which level the exit-side price reached first between start and until:
    "target", "stop" or "neither". A candle touching both counts as stop (the paper rule)."""
    long_ = direction == LONG
    for b in sorted(bars, key=lambda b: b["time"]):
        if b["time"] < start or b["time"] >= until or not b.get("complete", True):
            continue
        lo, hi = (b["bid_l"], b["bid_h"]) if long_ else (b["ask_l"], b["ask_h"])
        if (lo <= stop) if long_ else (hi >= stop):
            return "stop"
        if (hi >= target) if long_ else (lo <= target):
            return "target"
    return "neither"


def _one(bars: list[dict], i: int, d: int, risk: float, hold_end: datetime) -> float | None:
    """Paper-v2-like trade from bar i's open: entry at the executable side + 1 spread, stop and
    2R target at the given risk, exit at the time limit. Returns R (no financing), None if no data."""
    b = bars[i]
    spread = b["ask_o"] - b["bid_o"]
    entry = (b["ask_o"] + spread) if d == 1 else (b["bid_o"] - spread)
    stop, target = entry - d * risk, entry + d * 2 * risk
    last = None
    for x in bars[i:]:
        if x["time"] >= hold_end:
            break
        lo, hi = (x["bid_l"], x["bid_h"]) if d == 1 else (x["ask_l"], x["ask_h"])
        sp = x["ask_c"] - x["bid_c"]
        if (lo <= stop) if d == 1 else (hi >= stop):
            return ((stop - d * sp) - entry) * d / risk
        if (hi >= target) if d == 1 else (lo <= target):
            return 2.0
        last = x
    if last is None:
        return None
    exit_ = last["bid_c"] if d == 1 else last["ask_c"]
    return (exit_ - entry) * d / risk


def baseline(bars: list[dict], risk: float, start: datetime, end: datetime, hold_hours: float,
             seed: str, n: int = 50) -> dict | None:
    """Random entries (random minute in [start, end), random direction), each with the real
    trade's risk, the 2R target and the hold time. Seeded by the signal id: reproducible."""
    bars = sorted((b for b in bars if b.get("complete", True)), key=lambda b: b["time"])
    idx = [i for i, b in enumerate(bars) if start <= b["time"] < end]
    if not idx or risk <= 0:
        return None
    rng = random.Random(seed)
    rs = []
    for _ in range(n):
        i = rng.choice(idx)
        r = _one(bars, i, rng.choice((1, -1)), risk, bars[i]["time"] + timedelta(hours=hold_hours))
        if r is not None:
            rs.append(r)
    if not rs:
        return None
    return {"mean_r": round(sum(rs) / len(rs), 3), "n": len(rs)}
