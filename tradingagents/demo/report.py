"""Score closed demo trades after costs, against a random-direction baseline."""

from __future__ import annotations

import random
import statistics


def _units(t: dict) -> float:
    return float(t.get("units", t.get("initialUnits", 0)) or 0)


def trade_pnl(trades: list[dict]) -> list[float]:
    """Realized P&L per closed trade, account currency, spread and financing included."""
    return [float(t.get("realizedPL", 0)) + float(t.get("financing", 0)) for t in trades]


def gross_moves(trades: list[dict], spread_cost: dict[str, float]) -> list[float]:
    """P&L before spread: what the same trade earned at mid prices (approximate).

    ``spread_cost`` maps trade id -> half-spread cost paid on entry (from the
    journal); the exit is assumed to cost the same.
    """
    out = []
    for t, pnl in zip(trades, trade_pnl(trades), strict=False):
        out.append(pnl - float(t.get("financing", 0)) + 2 * spread_cost.get(str(t["id"]), 0.0))
    return out


def summarize(trades: list[dict], spread_cost: dict[str, float], n_random: int = 5000,
              seed: int = 7) -> dict:
    pnl = trade_pnl(trades)
    n = len(pnl)
    if n == 0:
        return {"trades": 0, "note": "no closed trades yet"}
    total = sum(pnl)
    rng = random.Random(seed)

    # Bootstrap 95% CI of mean P&L per trade.
    means = sorted(statistics.fmean(rng.choices(pnl, k=n)) for _ in range(n_random))
    ci = (means[int(0.025 * n_random)], means[int(0.975 * n_random) - 1])

    # Random baseline: same trades, same timing, coin-flip direction, same costs.
    gross = gross_moves(trades, spread_cost)
    costs = [g - p for g, p in zip(gross, pnl, strict=False)]
    beat = 0
    for _ in range(n_random):
        rand_total = sum(g * rng.choice((1, -1)) - c for g, c in zip(gross, costs, strict=False))
        beat += rand_total >= total
    by_side = {}
    for name, sign in (("long", 1), ("short", -1)):
        side = [p for t, p in zip(trades, pnl, strict=False) if _units(t) * sign > 0]
        if side:
            by_side[name] = {"trades": len(side), "mean_pnl": round(sum(side) / len(side), 2),
                             "win_rate": round(sum(p > 0 for p in side) / len(side), 3)}
    return {
        "trades": n,
        "by_side": by_side,
        "total_pnl": round(total, 2),
        "mean_pnl": round(total / n, 2),
        "win_rate": round(sum(p > 0 for p in pnl) / n, 3),
        "mean_pnl_ci95": [round(ci[0], 2), round(ci[1], 2)],
        "p_random_as_good": round(beat / n_random, 3),
        "enough_data": n >= 150,
    }


_TIERS = ("Buy", "Overweight", "Hold", "Underweight", "Sell")
_SIGN = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}


def rating_decisions(events: list[dict]) -> list[dict]:
    """Every real (not dry-run) rating in the journal, traded or not, Holds included."""
    out, dry = [], False
    for e in events:
        if e["event"] == "run_start":
            dry = bool(e.get("dry_run"))
        elif e["event"] == "decision" and not dry and e.get("rating") in _TIERS:
            out.append({"ticker": e.get("ticker") or e["instrument"], "trade_date": e["trade_date"],
                        "rating": e["rating"]})
    return out


def score_ratings(decisions: list[dict], bars, hold_days: int = 5, today: str | None = None) -> dict:
    """How each rating did over the next ``hold_days``, whether or not it was traded.

    Move = next daily open after the rating date to the open ``hold_days + 1`` bars
    later, the same window a paper trade holds. It uses mid prices, so it
    measures the call, not the costs. Ratings whose window has not finished
    are counted as pending.
    """
    from datetime import date

    import pandas as pd

    today = today or date.today().isoformat()
    tiers = {r: [] for r in _TIERS}
    pending = errors = 0
    for d in decisions:
        try:
            df = bars(d["ticker"], today).dropna(subset=["Open"]).reset_index(drop=True)
            dates = pd.to_datetime(df["Date"]).dt.date.astype(str)
            later = df.index[dates > d["trade_date"]]
            if len(later) <= hold_days + 1:
                pending += 1
                continue
            i = later[0]
            tiers[d["rating"]].append(100.0 * (float(df.Open[i + hold_days + 1]) / float(df.Open[i]) - 1))
        except Exception:
            errors += 1
    by_rating = {r: {"n": len(v), "mean_move_pct": round(statistics.fmean(v), 2)}
                 for r, v in tiers.items() if v}
    signed = [m * _SIGN[r] for r, v in tiers.items() for m in v if _SIGN[r]]
    result = {"scored": sum(len(v) for v in tiers.values()), "pending": pending, "by_rating": by_rating}
    if errors:
        result["price_errors"] = errors
    if signed:
        result["directional"] = {"n": len(signed), "hit_rate": round(sum(s > 0 for s in signed) / len(signed), 3),
                                 "mean_signed_move_pct": round(statistics.fmean(signed), 2)}
    return result
