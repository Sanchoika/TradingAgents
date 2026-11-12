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


def news_moves(bars: list[dict], first_seen: datetime, atr: float, after=(15, 60, 240)) -> dict | None:
    """Mid-price move from the minute a headline was first seen to +15/+60/+240 minutes, in daily-ATR
    units (positive = up). None when the candles do not cover the start or the ATR is unknown."""
    if not atr or atr <= 0:
        return None
    mid = {b["time"].replace(second=0, microsecond=0): (b["bid_c"] + b["ask_c"]) / 2
           for b in bars if b.get("complete", True)}
    t0 = first_seen.replace(second=0, microsecond=0)
    start = mid.get(t0 - timedelta(minutes=1)) or mid.get(t0)
    if start is None:
        return None
    out = {}
    for m in after:
        later = [mid[t] for t in sorted(mid) if t0 <= t <= t0 + timedelta(minutes=m)]
        out[f"m{m}"] = round((later[-1] - start) / atr, 3) if later else None
    return out


VARIANTS = ("plain", "scale_1r", "breakeven", "news_flip", "broker_tp")


def manage(bars: list[dict], direction: str, entry: float, stop: float, risk: float, t1: float, t2: float,
           entry_time: datetime, deadline: datetime, flip_at: datetime | None = None,
           tp_hours: float = 24.0) -> dict | None:
    """Position management in shadow (list item 4): the same trade replayed on 1-minute bid/ask candles
    under each rule, in R (no financing). Paper-v2 conventions: exit-side prices, a stop fills one
    spread worse (or at a gapped open), a candle touching both stop and target counts as the stop.
      plain      stop, 2R target, time limit (the paper rule, recomputed here so all compare alike)
      scale_1r   half off at 1R, the rest as plain
      breakeven  stop moves to entry once 1R is reached
      news_flip  as plain, but out at the first minute after a material headline against the trade
      broker_tp  the 2R take-profit and the stop rest at the broker; no time exit (up to tp_hours)
    None when the candles do not cover the entry."""
    d = 1 if direction == LONG else -1
    bars = sorted((b for b in bars if b.get("complete", True) and b["time"] >= entry_time.replace(second=0, microsecond=0)),
                  key=lambda b: b["time"])
    if risk <= 0 or not bars or bars[0]["time"] > entry_time + timedelta(minutes=2):
        return None
    R = lambda px: (px - entry) * d / risk   # noqa: E731

    def run(variant):
        end = entry_time + timedelta(hours=tp_hours) if variant == "broker_tp" else deadline
        st, size, got, last = stop, 1.0, 0.0, None
        for b in bars:
            if b["time"] >= end:
                break
            lo, hi = (b["bid_l"], b["bid_h"]) if d == 1 else (b["ask_l"], b["ask_h"])
            opn = b["bid_o"] if d == 1 else b["ask_o"]
            sp = b["ask_c"] - b["bid_c"]
            if variant == "news_flip" and flip_at is not None and b["time"] >= flip_at:
                return got + size * R(opn)
            worst, best = (lo, hi) if d == 1 else (hi, lo)
            if (worst - st) * d <= 0:
                px = opn - d * sp if (opn - st) * d <= 0 else st - d * sp
                return got + size * R(px)
            if (best - t1) * d >= 0:
                if variant == "scale_1r" and size == 1.0:
                    got, size = 0.5 * R(t1), 0.5
                if variant == "breakeven":
                    st = entry
            if (best - t2) * d >= 0:
                return got + size * R(t2)
            last = b
        if last is None:
            return None
        return got + size * R(last["bid_c"] if d == 1 else last["ask_c"])

    out = {v: run(v) for v in VARIANTS}
    return None if out["plain"] is None else {k: (round(v, 3) if v is not None else None) for k, v in out.items()}


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
