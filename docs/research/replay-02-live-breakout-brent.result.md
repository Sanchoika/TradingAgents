# Replay: replay-02-live-breakout-brent

Preregistered 2026-11-13T00:09:40.364484+00:00 (hash 3703ed52599acd49). Hypothesis: The live Brent setups (BREAKOUT and FAILED_BREAK of yesterday's high/low, brent-exp-4 rules as in the bot's code defaults with the live spread/quote-age/EIA blackout settings) show a positive net R per trade on 2024-01-01..2026-10-31 after paper-v2 costs, in both halves, and beat a coin-flip direction at the same minutes. In-sample history (the setups were designed and tuned on it): a pass only supports continuing the forward count; a fail is a warning for the research track, not a stop of the live count.

**A filter, not proof.** A pass means only: worth forward-testing in the lab or research track.

**History:** IN-SAMPLE: Brent 2013-2026 was used for prereg-21/22/23, the brent-exp tuning and seen before families v1; any replay before 2026-11-11 is in-sample.

**Result:** 708 trades, average -0.156 R after costs, likely [-0.228, -0.084], worst losing run -115.84 R; halves -0.223 / -0.089 R; vs a coin-flip direction at the same minutes -0.035 R (likely [-0.101, 0.032]).

**Filter:** FAILS (rule {'min_trades': 200, 'ci_low_above': 0, 'mean_at_least': 0.1}, both halves positive, beats the coin-flip direction).

Caveats: the likely ranges assume independent trades; trades that overlap in time make them too narrow. The coin-flip comparison has no financing or currency cost (the setups' R does), so it leans against the setups.

| Setup | Trades | Avg R | Likely range | Verdict |
|---|---|---|---|---|
| BREAKOUT | 496 | -0.156 | [-0.233, -0.079] | kill |
| FAILED_BREAK | 212 | -0.157 | [-0.317, 0.003] | watch |

Run once on the PC, 2026-11-13 00:10 UTC, code commit c82bccf (clean tree), data sha256 in replay-runs.jsonl. Scope note: the replay is chart-only (no news filter), so it compares with the live research track (chart-only signals, which NORTH practice follows), not with real alerts, which also need news support.
