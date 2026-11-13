"""Research families v1 (frozen 2026-11-11, docs/brent-research-families-v1.md).

New Brent setup families measured forward on paper as research calls only. They never become
real alerts and never change the existing family (yesterday's high/low breaks). Unproven:
earlier backtests of an EIA pullback (prereg-21) and of opening-range breaks failed.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import chart
from .rules import LONG, SHORT, Setup, ny, previous_day

FAMILIES = ("EIA_REACTION", "ORB_LONDON", "ORB_NY", "RANGE_REVERSAL", "TREND_PULLBACK")
FAMILIES_VERSION = "families-v1"
SESSIONS = (("ORB_LONDON", 3), ("ORB_NY", 9))   # opening range: the first 30 min after HH:00 ET
FAMILIES_V11 = "families-v1.1"   # 2026-11-13 (Irakli): a desk with its own ORB_NY start (NAS100: 09:30 ET stock open)


def version(orb_ny_start: str = "09:00") -> str:
    return FAMILIES_VERSION if orb_ny_start == "09:00" else FAMILIES_V11
# EIA weekly release shifts (holiday weeks), from the EIA schedule page (the same source NORTH 5.0 reads),
# keyed by the standard release Wednesday -> actual release (New York time). EIA had published shifts only
# through 2026-11-12 when this was written; an unknown week uses Wednesday 10:30 ET. Update when EIA
# publishes more (a rule change: families-v1 -> v1.1, that family's count restarts).
EIA_SHIFTS = {
    "2024-12-31": "2025-01-02T11:00", "2025-01-22": "2025-01-23T12:00", "2025-02-19": "2025-02-20T12:00",
    "2025-05-28": "2025-05-29T12:00", "2025-09-03": "2025-09-04T12:00", "2025-10-15": "2025-10-16T12:00",
    "2025-11-12": "2025-11-13T12:00", "2025-12-24": "2025-12-29T17:00", "2026-01-21": "2026-01-22T12:00",
    "2026-02-18": "2026-02-19T12:00", "2026-05-27": "2026-05-28T12:00", "2026-09-09": "2026-09-10T12:00",
    "2026-10-14": "2026-10-15T12:00", "2026-11-11": "2026-11-12T12:00",
}


def eia_release_candle(t: datetime) -> bool:
    """True if the 15-min candle starting at New York time t is the EIA release candle."""
    stamp = t.strftime("%Y-%m-%dT%H:%M")
    if stamp in EIA_SHIFTS.values():
        return True
    wed = (t - timedelta(days=(t.weekday() - 2) % 7)).date().isoformat()   # this week's Wednesday
    return t.weekday() == 2 and (t.hour, t.minute) == (10, 30) and wed not in EIA_SHIFTS


def detect(bar: dict, close_t: datetime, bars: list[dict], a: float, daily: list[dict] | None,
           arm_hours: float, decimals: int = 3, eia: bool = True, orb_ny_start: str = "09:00") -> list[Setup]:
    """Family setups armed by one completed 15-minute candle (mid prices). Pure function."""
    if a <= 0:
        return []
    out: list[Setup] = []
    t = ny(bar["time"])
    day = t.date().isoformat()
    z = 0.10 * a
    trig = {k: round(bar[k], 5) for k in ("o", "h", "l", "c")}
    expires = (close_t + timedelta(hours=arm_hours)).isoformat()

    def mk(family, direction, lo, hi, stop, invalid, level_name, level, key_extra=""):
        return Setup(key=f"{day}-{family}{key_extra}-{direction}", kind=family, direction=direction,
                     level_name=level_name, level=round(level, decimals), zone_lo=round(lo, decimals),
                     zone_hi=round(hi, decimals), stop=round(stop, decimals), invalid_close=round(invalid, decimals),
                     atr=a, armed_at=close_t.isoformat(), expires_at=expires, trigger=trig)

    rng, mid = bar["h"] - bar["l"], (bar["h"] + bar["l"]) / 2
    # EIA_REACTION: the release candle (Wednesday 10:30 ET, or the shifted time in a holiday week).
    if eia and eia_release_candle(t) and rng >= 0.25 * a:
        pos = (bar["c"] - bar["l"]) / rng
        if pos >= 0.7:
            out.append(mk("EIA_REACTION", LONG, bar["c"] - z, bar["c"], bar["l"] - 0.05 * a, mid, "EIA", mid))
        elif pos <= 0.3:
            out.append(mk("EIA_REACTION", SHORT, bar["c"], bar["c"] + z, bar["h"] + 0.05 * a, mid, "EIA", mid))
    # ORB_LONDON / ORB_NY: first close beyond the 30-minute opening range, within 2 h after it.
    ny_h, ny_m = (int(x) for x in orb_ny_start.split(":"))
    for name, hour in SESSIONS:
        hh, mm = (ny_h, ny_m) if name == "ORB_NY" else (hour, 0)
        start = t.replace(hour=hh, minute=mm, second=0, microsecond=0)
        end = start + timedelta(minutes=30)
        if not (end <= t < end + timedelta(hours=2)):
            continue
        orb = [b for b in bars if start <= ny(b["time"]) < end]
        if len(orb) < 2:
            continue
        orh, orl = max(b["h"] for b in orb), min(b["l"] for b in orb)
        if not (0.10 * a <= orh - orl <= 0.60 * a):
            continue
        omid = (orh + orl) / 2
        if bar["c"] > orh + 0.05 * a:
            out.append(mk(name, LONG, orh, orh + z, omid, orh, "ORH", orh))
        elif bar["c"] < orl - 0.05 * a:
            out.append(mk(name, SHORT, orl - z, orl, omid, orl, "ORL", orl))
    if not daily:
        return out
    reg = chart.regime(daily, a)
    # RANGE_REVERSAL: rejection at the 20-day edge while the regime is "range".
    if reg["regime"] == "range":
        hi20, lo20 = reg["hi20"], reg["lo20"]
        if bar["h"] >= hi20 - 0.10 * a and bar["c"] <= bar["h"] - 0.15 * a and bar["c"] < bar["o"]:
            out.append(mk("RANGE_REVERSAL", SHORT, bar["c"], bar["c"] + z,
                          max(bar["h"], hi20) + 0.10 * a, hi20, "H20", hi20))
        if bar["l"] <= lo20 + 0.10 * a and bar["c"] >= bar["l"] + 0.15 * a and bar["c"] > bar["o"]:
            out.append(mk("RANGE_REVERSAL", LONG, bar["c"] - z, bar["c"],
                          min(bar["l"], lo20) - 0.10 * a, lo20, "L20", lo20))
    # TREND_PULLBACK: a pullback to yesterday's midpoint in an up/down regime.
    if reg["regime"] in ("up", "down"):
        pd = previous_day(daily)
        m = (pd["h"] + pd["l"]) / 2
        if reg["regime"] == "up" and bar["l"] <= m <= bar["c"]:
            out.append(mk("TREND_PULLBACK", LONG, m, m + z, m - 0.30 * a, m - 0.10 * a, "PDMID", m))
        if reg["regime"] == "down" and bar["h"] >= m >= bar["c"]:
            out.append(mk("TREND_PULLBACK", SHORT, m - z, m, m + 0.30 * a, m + 0.10 * a, "PDMID", m))
    return out


def grade(nv: dict, direction: str) -> tuple[str, str]:
    """News-only grade for a family call (no model call): A supports, C conflicts, else B."""
    want, against = ("bullish", "bearish") if direction == LONG else ("bearish", "bullish")
    if nv.get("state") == "mixed" or (nv.get("conflict_window") or {}).get(against):
        return "C", "news conflicts"
    if (nv.get("support") or {}).get(want):
        return "A", "news supports the direction"
    return "B", "chart only, no conflicting news"
