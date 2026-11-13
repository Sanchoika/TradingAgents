# Brent research families v1: frozen rules (2026-11-11)

Written and frozen on **2026-11-11, before looking at any results**. Nothing here was tuned on past data. Changing a rule creates a new version and restarts that family's count.

**These families are unproven.** Earlier backtests of an EIA pullback idea (prereg-21) and of opening-range-break ideas **failed**. These are research calls only, measured forward on paper.

## Common to every family

- **Instrument and data:** BCO_USD. The bot's own completed 15-minute mid candles, daily ATR (`a`, the same `atr_period` as the existing family), and daily candles.
- **Output:** a research signal (`arm = "family"`, ids `RF-YYYYMMDD-NNN`), paper-tracked with the same paper rules as the existing family (paper-v2: fill rules, the 2R target, stop and time limit). **Never a real alert.** **NORTH practice orders do not follow these** (NORTH's research track takes only `arm = "chart-only"`).
- **Phone:** one graded RESEARCH CALL card naming the family.
  - **Grade (news only, no model call):** A = news supports the direction; C = news conflicts (against the direction, or mixed); B = otherwise.
  - **Daily cap:** `research_call_cap` (default 8) research cards per New York day, counting the existing family's research calls too.
  - **No double cards:** families that fire in the same minute in the same direction share **one** card naming all of them. No card if any research call or real alert went out in the same direction in the last 15 minutes. Paper tracking still happens.
- **Shared entry rules:** each setup is armed at the close of its trigger candle and stays valid **2 hours** (`family_arm_hours`). It enters when the executable price is inside the zone and the existing entry checks pass (spread, quote age, tradeable, not near the 17:00 ET close, blackouts). A 15-minute close beyond `invalid_close` against the trade ends it.
- **Zone width:** `z = 0.10 × a`. One setup per family, direction and New York day (or per session for ORB).
- **Separate counts:** the existing family (yesterday's high/low breaks, brent-exp-4 / news-policy-v3 / paper-v2) is **unchanged** and keeps its count. Each new family's count is its own and uses the same go/no-go rule (checkpoint at 30, decision at 50).

## EIA_REACTION

- **Trigger:** the EIA weekly release candle: the 15-minute candle starting **10:30 ET on a Wednesday**, or, in a **holiday week**, the candle starting at the **shifted release time**.
  - The shifts come from EIA's schedule page, the same source NORTH 5.0 reads. They are kept in `families.EIA_SHIFTS` (all published 2025–2026 shifts, e.g. Veterans Day 2026: Wed 11 Nov → **Thu 12 Nov 12:00 ET**).
  - In a shifted week the standard Wednesday 10:30 candle does **not** trigger.
  - When this was written, EIA had published shifts only through 2026-11-12. A week with an unknown shift uses Wednesday 10:30.
  - Adding later shifts is a rule change: v1.1, and this family's count restarts.
  - *(Amended 2026-11-11, still before any results, at the coordinator's review: the first draft used only Wednesday 10:30.)*
- **Cannot fire:** candle range < 0.25 × a, or close in the middle 40% of the candle.
- **LONG:** close in the top 30%.
  - zone [close − z, close];
  - stop = candle low − 0.05 × a;
  - invalid_close = candle midpoint.
- **SHORT:** mirror image.

## ORB_LONDON and ORB_NY (opening-range breaks)

- **Opening range:** the two 15-minute candles from **03:00–03:30 ET** (London) and **09:00–09:30 ET** (New York).
- **Cannot fire:**
  - range width < 0.10 × a or > 0.60 × a;
  - more than 2 hours after the range ended;
  - a second time in the same session and direction.
- **LONG:** the first 15-minute close above ORH + 0.05 × a.
  - zone [ORH, ORH + z];
  - stop = range midpoint;
  - invalid_close = ORH.
- **SHORT:** mirror image at ORL.

## RANGE_REVERSAL

- **Only when** the bot's regime check (20-day close slope, `chart.regime`) says **range**. Uses the 20-day high H20 and low L20 of completed daily candles.
- **SHORT:** a 15-minute candle with high ≥ H20 − 0.10 × a, close ≤ high − 0.15 × a, and close < open.
  - zone [close, close + z];
  - stop = max(high, H20) + 0.10 × a;
  - invalid_close = H20.
- **LONG:** mirror image at L20.
- **Cannot fire:** regime up, down or unknown; once per edge per day.

## TREND_PULLBACK

- **Level:** M = yesterday's midpoint, (PDH + PDL) / 2.
- **Regime up, LONG:** a 15-minute candle with low ≤ M and close ≥ M.
  - zone [M, M + z];
  - stop = M − 0.30 × a;
  - invalid_close = M − 0.10 × a.
- **Regime down, SHORT:** mirror image.
- **Cannot fire:** regime range or unknown; once per direction per day.

## Dropped

- **1-hour variants:** no cheap path. They would need separate hourly-candle handling, and they overlap the 15-minute families. Not built in v1.

## Switches (brent.toml)

- `research_families = true`
- `research_call_cap = 8`
- `family_arm_hours = 2`

None of these is in `RULE_FIELDS`, so the existing family's config fingerprint does not change.

## Amendment: families v1.1 for the NAS100 desk (2026-11-13, Irakli)

- **Change:** on the NAS100 desk, the ORB_NY opening range starts at the **09:30 ET stock open** (09:30–10:00), not at 09:00, which is pre-market for stocks. Setting: `orb_ny_start = "09:30"` in deploy/markets/nas100.toml.
- **Labels:** that desk's family signals are labelled `families-v1.1`, so its count restarts. Every other rule is unchanged.
- **Unchanged:** Brent and the other desks keep `families-v1` (09:00). `orb_ny_start` is not a rule field, so Brent's fingerprint (6f42ab) is unchanged.
- **Why:** found in the final audit (item 8); approved by Irakli.

