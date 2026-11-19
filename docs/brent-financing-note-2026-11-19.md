# Known cost-model issue: oil financing (recorded 2026-11-19)

**Status: recorded only. No rule, config or code was changed.** The frozen paper rules
(including `financing_pct_year = 45` in `tradingagents/brent/config.py`) stay as they are,
so no trade count restarts. Found by a read-only three-model check (Claude, Codex,
Antigravity); the full write-up is kept on Irakli's PC (`Brain/outputs/orca/oil-financing/`).

## What the code assumes

Every Brent and WTI paper path charges a flat 45 percent a year, as a cost on both long and
short trades (a deliberate choice in prereg-21, step 2), and charges it by the minute of the
holding period:

- `tradingagents/brent/engine.py` (paper P/L, around line 1257)
- `tradingagents/brent/replay.py` (replay, around line 219, full planned hold)
- `tradingagents/brent/rules.py` (research candidate costs, around line 126)

NORTH's pretend account applies no financing at all.

## What OANDA does

- In OANDA's financing file a positive rate means the account is paid, a negative rate means it
  pays. Two practice trades agree: the Brent short (trade 11) was charged 5.15 CAD, the long
  (trade 18) was paid 0.05 CAD.
- On the snapshot dated 2025-10-25, Brent longs were paid about 39.7 percent a year and shorts
  charged about 44.7 percent a year (WTI: +20.7 percent long).
- Financing is charged only at the 17:00 New York rollover, not by the minute.
- That is one snapshot. Oil financing moves with the futures market, and the tiny credit on
  trade 18 suggests the rate was much lower by mid-November.

## Size of the error (Brent, 8 hour hold, stop 0.25 to 0.40 of a day's average range)

| Trade | Paper vs reality |
| --- | --- |
| Closed before 17:00, either side | paper looks worse by 0.04 to 0.06R |
| Long held over 17:00 | paper looks worse by 0.14 to 0.23R |
| Short held over 17:00 | paper looks better by 0.08 to 0.12R |

WTI shows the same pattern at about 60 percent of the size.

## What is affected

The Brent bot and WTI desk paper results (and so NORTH's weekly scorecard), replay-01 and
replay-02, and the ledger rows for prereg-21, 22 and 23. No published fail turns into a pass.
Not affected: T-01 to T-03 and NORTH's practice-account P/L (it uses the broker's real figures).
The gold, NAS100, EURUSD and USDCAD desks use 8 percent: cautious on average, but too cheap for
trades held over a rollover.

## Smallest fix (not applied)

One helper that charges financing per side, with separate long and short rates, only at each
17:00 New York crossing, used in the three places above and in NORTH's pretend account.
It changes frozen paper rules, so it needs a new rules version and a trade-count restart:
Irakli's decision. Until then, read oil results in the scorecard with the error table above in mind.
