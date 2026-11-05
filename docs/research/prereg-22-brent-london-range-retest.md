# prereg-22 (FROZEN before any result): Brent London-range breakout and retest

Status: rules written 2026-11-05 before the module `tradingagents/brent/research/candidates.py` was run on any
price. Hash of this file and of the module are recorded in `docs/research/freeze-2026-11-05.sha256` in the same
commit, before the run. Item 12 candidate 2 of the 2026-11-05 upgrade brief. Shadow/research only: it never sends
an ENTRY alert, and no result promotes it automatically.

## Ledger check (what already failed, and what differs)
- Row 17 (ORB on NAS100, FAIL 3/6) and row 20 (XAU_USD continuation after the 08:30 NY range, FAIL 6/6) enter on
  the first close outside an opening range. This candidate does NOT: it waits for a retest of the broken level and
  a hold, which is a different entry and a different (lower) trade frequency.
- It is a different instrument (BCO_USD) and a different session (London morning, the home liquidity of Brent).
- It is NOT the failed EIA-reaction branch (row 21): no event, no release time, no news condition. Days with an
  EIA release are not excluded and not selected.
- Prior evidence: weak. Opening-range breakouts showed a real but too-thin edge on NAS100 and none on gold. The
  only reason to run it is that the retest and the instrument are new. A fail is the expected outcome and is kept.

## The one session specification (chosen now, not searched)
Range window 03:00-03:59 New York time (DST aware, the New York clock; London runs one hour earlier relative to
New York for about two weeks a year and this is accepted, not corrected). One window. No other window will be tried.

## Exact rule
- Data: OANDA BCO_USD M1 bid/ask, complete candles. Mid = (bid+ask)/2.
- Qualifying day: Monday-Friday, all 60 bars 03:00..03:59 present, and bars present through 08:25.
- Range: H = highest mid high, L = lowest mid low of 03:00..03:59. W = H - L. Skip the day if W < 4 x the median
  spread of the range window (a range that small is noise).
- Break: the first M1 bar from 04:00 to 06:59 whose mid close is above H (long) or below L (short). Only the first
  break of the day counts; a break on the other side later is ignored.
- Retest: within 60 minutes after the break bar, the first bar whose mid low <= H (long; mid high >= L for short)
  AND whose mid close is above H (below L for short): touched the level and closed back on the break side.
  No retest within 60 minutes, or a bar closing back inside the range first: no trade.
- Entry: at the open of the next M1 bar after the retest bar. Not after 07:30.
- Stop: the range midpoint (H+L)/2. No target.
- Skip if R (cost-level-1 entry to stop) < 4 x the entry bar's spread, or entry spread > 3 x the range-window
  median spread.
- Exit: the stop, else the open of the first bar at or after 08:25 New York (before the 08:30 data window).
- One trade per day, no overnight holding.

## Costs (identical to prereg-21)
Level k: entry pays k x (half spread + one spread); a stop pays k x one more spread, a gap through the stop fills
at the bar open with that extra; a time exit pays k x half spread; financing k x 45%/yr of price for minutes held;
currency conversion x0.99 on wins and x1.01 on losses (CAD account). k=1 base, k=2 doubled, k=0 gross.

## Periods
Development 2013-01..2020-12: code check only, nothing tuned. Holdout 2021-01-01..download date, evaluated ONCE.
The same holdout was used by row 21 for a different hypothesis; this is stated, and the forward shadow below is the
real test. Variants attempted for this candidate: 1 (this one). Family size for the correction: 2 candidates.

## Pass needs all six (holdout, base costs unless stated; alpha 0.05/2 for the family)
1. Mean daily R > 0 and the block-bootstrap CI (20-day blocks, 97.5%) above 0.
2. Profit factor >= 1.15.
3. Mean trade R > 0 at doubled costs.
4. >= 2 of 3 chronological thirds positive and total R without the best 5 trades > 0.
5. Mean trade R above the 95th percentile of 1,000 random-direction runs (same entry bar, same stop distance, same
   exit) and above always-long.
6. >= 200 trades and maximum drawdown of daily R <= 15R.
Fewer than 200 trades or criteria that cannot be computed: INCONCLUSIVE.

## Reported, never selected
Gross mean R; exit mix; win rate; long share; median spread in R; by-year results; development mean; skip counts.

## Forward comparison (required before any alert use)
From the day the shadow recorder starts, each qualifying day's would-be trade is logged with its own ID, costs and
outcome, separately from ENTRY alerts. A candidate may be considered for alerts only after a pass here AND at least
60 forward shadow trades whose mean R is positive, decided by Irakli. No promotion to meet an alert count.

## Limits stated in advance
OANDA CFD quotes, not ICE futures; M1 hides spikes inside a minute; real slippage may exceed one spread; the
winter/summer London offset; financing rate history unknown (45%/yr assumed).

## Result (run once on the PC, 2026-11-05, frozen code and data unchanged)

Verdict: **FAIL, 0 of 6 criteria.** Holdout 557 trades: mean -0.328R, win rate 25.9%, 51% long; exits
367 stop / 182 time / 8 gap; median spread 0.099R. Gross (no costs): -0.020R — essentially no edge
before costs, and costs alone turn it clearly negative. Stress (2x costs): -0.551R. Development
(2013-2020, code check only): 571 trades, -0.522R. Not promoted. Kept here as a negative result.
Data: BCO_USD M1 (sha256 68f853b5e8d1...), same file used for prereg-21. Output:
C:\bbt\brent-candidates-2026-11-05\candidates-results.json on the PC.
