"""Pure signal rules: levels, setups, entry checks, costs. No I/O, so every rule is testable.

Two setup families around yesterday's high (PDH) and low (PDL), each in both directions:

- BREAKOUT: a completed 15-minute candle closes beyond the level by break_atr x ATR.
  Entry only on a return to the level (zone of zone_atr x ATR on the broken side).
  Stop stop_atr x ATR back through the level. Setup invalid if a 15-min candle closes
  back through the level.
- FAILED_BREAK: a 15-minute candle trades beyond the level by break_atr x ATR but closes
  back inside. Entry on the inside of the level, against the attempted break. Stop just
  beyond the candle's extreme. Setup invalid if a 15-min candle closes beyond the level.

The two families contradict each other on purpose: forward tracking measures which, if
either, is useful. Neither is a validated strategy; neither was fitted to past data.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import BrentConfig, parse_blackout

NY = ZoneInfo("America/New_York")
LONG, SHORT = "LONG", "SHORT"


def atr(daily: list[dict], period: int) -> float:
    """Average true range of the last ``period`` completed daily candles (mid prices)."""
    bars = [b for b in daily if b.get("complete", True)]
    if len(bars) < period + 1:
        raise ValueError(f"need {period + 1} completed daily candles, have {len(bars)}")
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"]))
           for p, b in zip(bars[-period - 1:-1], bars[-period:], strict=True)]
    return sum(trs) / period


def previous_day(daily: list[dict]) -> dict:
    bars = [b for b in daily if b.get("complete", True)]
    if not bars:
        raise ValueError("no completed daily candle")
    return bars[-1]


@dataclass
class Setup:
    key: str                 # dedup key: one setup per family, side and level per day
    kind: str                # BREAKOUT | FAILED_BREAK
    direction: str           # LONG | SHORT
    level_name: str          # PDH | PDL
    level: float
    zone_lo: float
    zone_hi: float
    stop: float
    invalid_close: float     # a 15-min close beyond this (against the trade) invalidates
    atr: float
    armed_at: str            # ISO time of the trigger candle's close
    expires_at: str
    trigger: dict = field(default_factory=dict)
    status: str = "armed"    # armed | alerted | invalidated | expired | suppressed
    news: list = field(default_factory=list)
    news_error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def detect(bar: dict, bar_close_time: datetime, pdh: float, pdl: float, a: float,
           day_label: str, cfg: BrentConfig) -> list[Setup]:
    """Setups armed by one completed 15-minute candle (mid prices)."""
    out = []
    brk, zone = cfg.break_atr * a, cfg.zone_atr * a
    expires = (bar_close_time + timedelta(hours=cfg.arm_hours)).isoformat()
    armed = bar_close_time.isoformat()
    trig = {k: round(bar[k], 5) for k in ("o", "h", "l", "c")}

    def mk(kind, direction, name, level, lo, hi, stop, invalid):
        return Setup(key=f"{day_label}-{kind}-{direction}-{name}", kind=kind, direction=direction,
                     level_name=name, level=level, zone_lo=round(lo, cfg.price_decimals), zone_hi=round(hi, cfg.price_decimals),
                     stop=round(stop, cfg.price_decimals), invalid_close=level, atr=a, armed_at=armed,
                     expires_at=expires, trigger=trig)

    if bar["c"] > pdh + brk:
        out.append(mk("BREAKOUT", LONG, "PDH", pdh, pdh, pdh + zone, pdh - cfg.stop_atr * a, pdh))
    elif bar["h"] > pdh + brk and bar["c"] < pdh:
        out.append(mk("FAILED_BREAK", SHORT, "PDH", pdh, pdh - zone, pdh,
                      bar["h"] + cfg.fail_stop_atr * a, pdh))
    if bar["c"] < pdl - brk:
        out.append(mk("BREAKOUT", SHORT, "PDL", pdl, pdl - zone, pdl, pdl + cfg.stop_atr * a, pdl))
    elif bar["l"] < pdl - brk and bar["c"] > pdl:
        out.append(mk("FAILED_BREAK", LONG, "PDL", pdl, pdl, pdl + zone,
                      bar["l"] - cfg.fail_stop_atr * a, pdl))
    return out


def close_invalidates(s: Setup, bar: dict) -> str | None:
    """Reason if a completed 15-min candle's close breaks the setup, else None."""
    # Every LONG setup sits above its level, every SHORT setup below it.
    c = bar["c"]
    if s.direction == LONG and c < s.invalid_close:
        return f"15-min candle closed at {c:.2f}, below {s.invalid_close:.2f}"
    if s.direction == SHORT and c > s.invalid_close:
        return f"15-min candle closed at {c:.2f}, above {s.invalid_close:.2f}"
    return None


def executable(direction: str, bid: float, ask: float) -> float:
    return ask if direction == LONG else bid


def in_zone(s: Setup, bid: float, ask: float) -> bool:
    px = executable(s.direction, bid, ask)
    return s.zone_lo <= px <= s.zone_hi


def plan(s: Setup, entry: float, spread: float, cfg: BrentConfig) -> dict:
    """Stop distance, conditional targets and reward-to-risk after modelled costs.

    Costs per barrel: entry slippage 1 spread (the spread itself is in the executable
    price), stop slippage 1 spread, financing over the full holding period, and the CAD
    conversion gap (winners x (1 - fx), losers x (1 + fx)).
    """
    d = 1 if s.direction == LONG else -1
    risk = abs(entry - s.stop)
    fin = entry * cfg.financing_pct_year / 100 * cfg.max_hold_hours / (365 * 24)
    fx = cfg.fx_loss_pct / 100
    loss = (risk + 2 * spread + fin) * (1 + fx)
    out = {"risk": round(risk, 4), "financing": round(fin, 4), "costs_note":
           f"1 spread slippage on entry and stop, financing {cfg.financing_pct_year:g}%/yr for "
           f"{cfg.max_hold_hours:g}h, CAD conversion ±{cfg.fx_loss_pct:g}%"}
    for n in (1, 2):
        target = entry + d * n * risk
        gain = (n * risk - spread - fin) * (1 - fx)
        out[f"t{n}"] = round(target, cfg.price_decimals)
        out[f"rr{n}"] = round(gain / loss, 2) if loss > 0 else 0.0
    return out


def ny(dt: datetime) -> datetime:
    return dt.astimezone(NY)


def minutes_to_close(now: datetime) -> float:
    """Minutes until the next daily (17:00 ET) close. Friday's is the weekly close."""
    t = ny(now)
    close = t.replace(hour=17, minute=0, second=0, microsecond=0)
    if t >= close:
        close += timedelta(days=1)
    return (close - t).total_seconds() / 60


def market_closed_by_clock(now: datetime) -> bool:
    """Brent CFD hours: Sun 18:00 to Fri 17:00 ET with a 17:00-18:00 daily break."""
    t = ny(now)
    wd, hm = t.weekday(), t.hour * 60 + t.minute
    if wd == 5 or (wd == 4 and hm >= 17 * 60) or (wd == 6 and hm < 18 * 60):
        return True
    return 17 * 60 <= hm < 18 * 60


def weekly_close(now: datetime) -> datetime:
    """Next Friday 17:00 ET."""
    t = ny(now)
    days = (4 - t.weekday()) % 7
    close = (t + timedelta(days=days)).replace(hour=17, minute=0, second=0, microsecond=0)
    if close <= t:
        close += timedelta(days=7)
    return close


def in_blackout(now: datetime, blackouts: list[str]) -> str | None:
    t = ny(now)
    hm = t.strftime("%H:%M")
    for b in blackouts:
        day, start, end = parse_blackout(b)
        same_day = (len(day) == 1 and int(day) == t.weekday()) or day == t.date().isoformat()
        if same_day and start <= hm < end:
            return b
    return None


def entry_blockers(s: Setup, bid: float, ask: float, quote_time: datetime, tradeable: bool,
                   now: datetime, cfg: BrentConfig) -> list[str]:
    """Every reason an alert for this setup must not be sent now (empty list = actionable)."""
    reasons = []
    if not tradeable or market_closed_by_clock(now):
        reasons.append("market closed")
    age = (now - quote_time).total_seconds()
    if age > cfg.max_quote_age_seconds:
        reasons.append(f"quote is {age:.0f}s old (limit {cfg.max_quote_age_seconds:g}s)")
    spread = ask - bid
    if spread > cfg.max_spread + 1e-9:
        reasons.append(f"spread {spread:.3f} above {cfg.max_spread:g}")
    mid = (bid + ask) / 2
    if not (cfg.range_low <= mid <= cfg.range_high) or s.zone_lo < cfg.range_low \
            or s.zone_hi > cfg.range_high:
        reasons.append(f"outside operating range {cfg.range_low:g}-{cfg.range_high:g}")
    if not in_zone(s, bid, ask):
        px = executable(s.direction, bid, ask)
        reasons.append(f"{'ask' if s.direction == LONG else 'bid'} {px:.3f} outside entry zone "
                       f"{s.zone_lo:.2f}-{s.zone_hi:.2f}")
    if now >= datetime.fromisoformat(s.expires_at):
        reasons.append("setup older than its arm window")
    px = executable(s.direction, bid, ask)
    if (s.direction == LONG and px <= s.stop) or (s.direction == SHORT and px >= s.stop):
        reasons.append("price already beyond the stop")
    elif abs(px - s.stop) < cfg.min_r_spreads * max(spread, 1e-9):
        reasons.append(f"stop distance under {cfg.min_r_spreads:g} spreads")
    if minutes_to_close(now) < cfg.close_buffer_minutes:
        reasons.append("too close to the daily close")
    b = in_blackout(now, cfg.blackouts)
    if b:
        reasons.append(f"scheduled-event blackout ({b} ET)")
    return reasons
