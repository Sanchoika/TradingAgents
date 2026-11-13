# Replay: replay-01-families-v1-brent

Preregistered 2026-11-12T23:44:53.679359+00:00 (hash b0bc87be1331dd9f). Hypothesis: The five research families v1 (EIA_REACTION, ORB_LONDON, ORB_NY, RANGE_REVERSAL, TREND_PULLBACK), as frozen on 2026-11-11 and run by the live bot, show a positive net R per trade on Brent 2024-01-01..2026-10-31 after paper-v2 costs, in both halves, and beat random entries at the same minutes. In-sample history: a pass only justifies continuing the forward count; a fail is a warning, not a stop.

**A filter, not proof.** A pass means only: worth forward-testing in the lab or research track.

**History:** IN-SAMPLE: Brent 2013-2026 was used for prereg-21/22/23, the brent-exp tuning and seen before families v1; any replay before 2026-11-11 is in-sample.

**Result:** 1286 trades, average -0.267 R after costs, likely [-0.335, -0.2], worst losing run -360.0 R; halves -0.378 / -0.156 R; vs random entries -0.076 R (likely [-0.136, -0.016]).

**Filter:** FAILS (rule {'min_trades': 200, 'ci_low_above': 0, 'mean_at_least': 0.1}, both halves positive, beats random).

| Setup | Trades | Avg R | Likely range |
|---|---|---|---|
| EIA_REACTION | 29 | -0.4 | [-0.774, -0.025] |
| ORB_LONDON | 436 | -0.32 | [-0.451, -0.19] |
| ORB_NY | 526 | -0.29 | [-0.394, -0.185] |
| RANGE_REVERSAL | 57 | -0.399 | [-0.671, -0.126] |
| TREND_PULLBACK | 238 | -0.072 | [-0.198, 0.054] |

Run once on the PC, 2026-11-12, with code at commit 9d0b5c2 (before the run log and provenance fields existed), data `C:/bbt/trading-bot/brent/data/BCO_USD_M1.csv`. Review notes: the comparison is a coin-flip direction at the same minutes, not random minutes; the likely ranges assume independent trades, so they are too narrow when trades overlap; the prereg "written" time was typed by hand (registered 23:44Z).
