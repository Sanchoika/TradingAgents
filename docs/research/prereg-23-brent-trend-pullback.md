# prereg-23 (FROZEN before any result): Brent trend pullback with a higher-timeframe trend

Status: rules written 2026-11-05 before `tradingagents/brent/research/candidates.py` was run on any price. Hashes in
`docs/research/freeze-2026-11-05.sha256`, taken in the same commit before the run. Item 12 candidate 1. Research /
shadow only; no automatic promotion.

## Ledger check (what already failed, and what differs)
- Row 19 (Grimes pullback on NAS100: 5-minute Keltner thrust, pullback to EMA20, fixed target at the thrust extreme)
  FAILED 6/6 with gross -0.000R, i.e. no edge before costs. This candidate is the same family, so the honest
  position is: the prior is LOW.
- What materially differs: (1) the trend is defined on 1-hour bars, not by a Keltner thrust; (2) the entry needs a
  confirmation bar after the touch, not a stop order; (3) no fixed target, time exit; (4) a different instrument
  with a different microstructure (BCO_USD, oil trends in persistent supply/demand regimes) and different sessions.
  If this is judged not different enough, the candidate is stopped; it is run once because the cost is one
  evaluation and the failure will be kept in the ledger.
- Not the failed EIA branch (row 21): no event condition.

## Exact rule (timeframes: M1 aggregated to M5 and H1 on the New York clock, completed bars only)
- Data: OANDA BCO_USD M1 bid/ask, mid = (bid+ask)/2.
- Trend (evaluated at the close of each completed H1 bar and held until the next): long trend if the last completed
  H1 mid close > H1 EMA(50) AND EMA(50) now > EMA(50) six H1 bars earlier; short trend is the mirror; otherwise no
  trend and no trade.
- Extension: within the last 12 completed M5 bars the M5 mid close was at least 1.0 x ATR(20, M5) above M5 EMA(20)
  (long; below for short).
- Pullback + confirmation: an M5 bar whose mid low <= M5 EMA(20) (long; mid high >= for short) is the touch bar. The
  next completed M5 bar must close above the touch bar's high (long; below its low for short): that is the
  confirmation bar.
- Entry: at the open of the M1 bar after the confirmation bar completes. Window 03:00-12:00 NY (entry bar start).
- Stop: the touch bar's extreme (low for long, high for short) minus/plus one spread at the touch bar.
- Skip if R (level-1 entry to stop) < 4 x entry spread, or entry spread > 3 x the median M1 spread of the previous
  60 minutes.
- Exit: the stop, else the open of the first bar at or after min(entry + 120 min, 12:30 NY). No target.
- At most 2 trades per day; none while one is open; no overnight holding.

## Costs, periods, pass criteria, reporting, forward comparison, limits
Identical to prereg-22 (same cost model as prereg-21; development 2013-2020 code check only; holdout 2021-01..
download date, once; six criteria at alpha 0.05/2 with criterion 6 at >=200 trades; INCONCLUSIVE if not computable;
forward shadow >= 60 trades before any alert consideration). Variants attempted: 1. Family size: 2.

## Result (run once on the PC, 2026-11-05, frozen code and data unchanged)

Verdict: **FAIL, 0 of 6 criteria.** Holdout 2,838 trades: mean -0.365R, win rate 26.7%, 56% long;
exits 1,860 stop / 929 time / 49 gap; median spread 0.12R. Gross (no costs): -0.013R — about
break-even before costs, same as the earlier NAS100 pullback (row 19), confirming the low prior
stated when this was frozen. Stress (2x costs): -0.597R. Development: 3,899 trades, -0.447R. Not
promoted. Kept here as a negative result. Same data file as prereg-22/prereg-21.
