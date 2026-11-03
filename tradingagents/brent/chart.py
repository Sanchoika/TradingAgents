"""Chart assessment, computed in code from completed OANDA candles (no model involved).

Timeframes: daily (context: regime, 20-day range), H1 (structure: swings, higher/lower highs
and lows), M15 (entry timing: the brent-v1 setups in rules.py). Every number shown in an
alert comes from here or from rules.py.
"""

from __future__ import annotations

from .rules import LONG

REGIME_SLOPE_ATR = 0.10   # |20-day regression slope| in ATR/day that counts as a trend


def regime(daily: list[dict], a: float, n: int = 20) -> dict:
    bars = [b for b in daily if b.get("complete", True)][-n:]
    if len(bars) < n or a <= 0:
        return {"regime": "unknown", "why": f"need {n} completed daily candles"}
    ys = [b["c"] for b in bars]
    xm, ym = (n - 1) / 2, sum(ys) / n
    slope = sum((i - xm) * (y - ym) for i, y in enumerate(ys)) / sum((i - xm) ** 2 for i in range(n))
    s = slope / a
    reg = "up" if s >= REGIME_SLOPE_ATR else "down" if s <= -REGIME_SLOPE_ATR else "range"
    hi, lo = max(b["h"] for b in bars), min(b["l"] for b in bars)
    pos = (bars[-1]["c"] - lo) / (hi - lo) if hi > lo else 0.5
    return {"regime": reg, "slope_atr_per_day": round(s, 3), "hi20": hi, "lo20": lo,
            "pos_in_20d_range": round(pos, 2)}


def swings(h1: list[dict], k: int = 2) -> dict:
    """Fractal swing highs/lows (k completed bars each side), oldest first."""
    bars = [b for b in h1 if b.get("complete", True)]
    highs, lows = [], []
    for i in range(k, len(bars) - k):
        win = bars[i - k:i + k + 1]
        if bars[i]["h"] == max(b["h"] for b in win):
            highs.append({"time": bars[i]["time"], "price": bars[i]["h"]})
        if bars[i]["l"] == min(b["l"] for b in win):
            lows.append({"time": bars[i]["time"], "price": bars[i]["l"]})
    if len(highs) >= 2 and len(lows) >= 2:
        hh, hl = highs[-1]["price"] > highs[-2]["price"], lows[-1]["price"] > lows[-2]["price"]
        lh, ll = highs[-1]["price"] < highs[-2]["price"], lows[-1]["price"] < lows[-2]["price"]
        structure = ("higher highs and lows" if hh and hl else
                     "lower highs and lows" if lh and ll else "mixed")
    else:
        structure = "not enough swings"
    return {"highs": highs[-6:], "lows": lows[-6:], "structure": structure}


def assess(setup: dict, daily: list[dict], h1: list[dict], entry: float, spread: float) -> dict:
    """Chart record for one candidate: regime, structure, levels and room in R."""
    a = setup["atr"]
    reg = regime(daily, a)
    sw = swings(h1)
    long_ = setup["direction"] == LONG
    risk = max(abs(entry - setup["stop"]), 1e-9)
    levels = [{"name": "H1 swing high", "price": s["price"]} for s in sw["highs"]]
    levels += [{"name": "H1 swing low", "price": s["price"]} for s in sw["lows"]]
    if "hi20" in reg:
        levels += [{"name": "20-day high", "price": reg["hi20"]}, {"name": "20-day low", "price": reg["lo20"]}]
    levels.append({"name": "prior-day " + ("high" if setup["level_name"] == "PDH" else "low"),
                   "price": setup["level"]})
    # Opposing levels: above entry for a long, below for a short (beyond 0.05 ATR).
    opp = sorted((lv for lv in levels if (lv["price"] - entry) * (1 if long_ else -1) > 0.05 * a),
                 key=lambda lv: abs(lv["price"] - entry))
    nxt = opp[0] if opp else None
    room = round(abs(nxt["price"] - entry) / risk, 2) if nxt else None
    supports = sorted({round(lv["price"], 2) for lv in levels if lv["price"] < entry}, reverse=True)[:3]
    resist = sorted({round(lv["price"], 2) for lv in levels if lv["price"] > entry})[:3]
    with_trend = reg["regime"] == ("up" if long_ else "down")
    return {"regime": reg["regime"], "slope_atr_per_day": reg.get("slope_atr_per_day"),
            "pos_in_20d_range": reg.get("pos_in_20d_range"), "h1_structure": sw["structure"],
            "supports": supports, "resistances": resist, "next_level": nxt, "room_r": room,
            "spread_share_of_risk": round(spread / risk, 3), "with_trend": with_trend,
            "atr": a}


def sentence(ch: dict, setup: dict) -> str:
    kind = {"BREAKOUT": "breakout and retest", "FAILED_BREAK": "failed break"}[setup["kind"]]
    side = "high" if setup["level_name"] == "PDH" else "low"
    room = (f"{ch['room_r']:.1f}R to the {ch['next_level']['name']} {ch['next_level']['price']:.2f}"
            if ch["next_level"] else "no listed level in the way")
    slope = f" ({ch['slope_atr_per_day']:+.2f} ATR/day)" if ch.get("slope_atr_per_day") is not None else ""
    return (f"Daily regime {ch['regime']}{slope}; H1 {ch['h1_structure']}; 15-min {kind} at "
            f"yesterday's {side} {setup['level']:.2f}; {room}.")
