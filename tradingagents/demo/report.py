"""Score closed demo trades after costs, against a random-direction baseline."""

from __future__ import annotations

import random
import statistics


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
    return {
        "trades": n,
        "total_pnl": round(total, 2),
        "mean_pnl": round(total / n, 2),
        "win_rate": round(sum(p > 0 for p in pnl) / n, 3),
        "mean_pnl_ci95": [round(ci[0], 2), round(ci[1], 2)],
        "p_random_as_good": round(beat / n_random, 3),
        "enough_data": n >= 150,
    }
