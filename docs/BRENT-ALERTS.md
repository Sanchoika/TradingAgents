# Brent alerts (alerts only, forward paper tracking)

`python -m tradingagents.brent` watches OANDA's Brent CFD (BCO_USD). When a pre-defined setup
qualifies on a fresh bid/ask quote, it sends a short Telegram alert. You decide whether to
place an order by hand. **It never places, changes or closes an order.** Every alert is also
tracked as a hypothetical paper trade, so the module can later be judged on its record.

It is separate from the TradingAgents morning test (#22). It has its own config
(`brent.toml`), journal folder (`~/.tradingagents/brent`), process, schedule, signal IDs
(`BR-YYYYMMDD-NNN`) and report. It shares only the Telegram route (`hermes send`) and the
private dashboard host. It makes no LLM calls, so it cannot use up the ChatGPT plan the
morning bot depends on.

## 1. Data and its limits

| Source | Used for | Limits |
|---|---|---|
| OANDA v20 pricing (practice host, read-only client) | Live bid/ask, quote timestamp, `tradeable` flag, USD to account currency | Practice prices. Polled every 3 s, so moves inside 3 s are not seen. Quote time is OANDA's; age = local clock minus quote time, so clock drift on the host counts as age. |
| OANDA candles (bid/ask, M15, daily at 17:00 New York) | Yesterday's high/low, daily ATR(14), 15-min setup candles, paper catch-up after a restart (M1) | Candles are context. An alert is never sent from candle prices alone. |
| OANDA account (summary, open trades) | Currency, NAV, `hedgingEnabled`, open BCO_USD trades | Read every 5 min. Shown as a conflict note only. |
| Yahoo Finance headlines for `BZ=F` (Brent) and `CL=F` (WTI) | Context in the alert | Not real time. It is a sample of articles, not a complete feed. Fetched when a setup arms, not at send time, so it adds no delay. Each item shows its publication time and when the monitor received it, and is tagged **Brent**, **WTI** or **oil (general)**. Headlines never trigger or block a signal and are not read as a direction call. |
| EIA weekly report time (Wed 10:30 ET) | Blackout, 10:15 to 10:45 ET | Holiday-shifted release dates must be added by hand (`blackouts`). |

The instrument facts on file are from the 2026-10-31 account check (`trading-bot/brent/STEP2-GATE.md`):
- BCO_USD "Brent Crude Oil" CFD, quoted in USD per barrel, on a CAD practice account.
- Whole units, 1 unit = 1 barrel, minimum 1 unit.
- Margin 13.9%. No guaranteed stops.
- Spread: median about $0.04 in 2026; about $0.06-0.07 from 17:00 to 23:00 ET, p90 $0.13 at 17:00.
- Financing about 40-45%/yr, charged 7 days a week.

Run `check` to confirm these on the live account; it is read-only. Trading hours are Sunday
18:00 to Friday 17:00 New York time, with a daily break from 17:00 to 18:00. The monitor
trusts OANDA's `tradeable` flag and also applies these hours.

## 2. Signal conditions (rules `brent-v1`)

There are two setup families around **yesterday's high (PDH) and low (PDL)**. Each is checked
on completed 15-minute candles, using mid prices. ATR is the daily ATR(14).

| Setup | Arms when (15-min candle) | Entry zone (executable price) | Broker stop | Setup invalid when |
|---|---|---|---|---|
| BREAKOUT LONG | closes above PDH + 0.10 ATR | ask in PDH .. PDH + 0.15 ATR | PDH - 0.25 ATR | a 15-min close below PDH |
| BREAKOUT SHORT | closes below PDL - 0.10 ATR | bid in PDL - 0.15 ATR .. PDL | PDL + 0.25 ATR | a 15-min close above PDL |
| FAILED_BREAK SHORT | trades above PDH + 0.10 ATR, closes back below PDH | bid in PDH - 0.15 ATR .. PDH | candle high + 0.05 ATR | a 15-min close above PDH |
| FAILED_BREAK LONG | trades below PDL - 0.10 ATR, closes back above PDL | ask in PDL .. PDL + 0.15 ATR | candle low - 0.05 ATR | a 15-min close below PDL |

Why these inputs:
- **Yesterday's high/low** are levels every Brent trader sees. They are not your fixed 118/107.
  Neither 118 nor 107 means anything to the rules unless a setup forms there.
- **ATR** scales the levels to the current volatility.
- **Spread** is part of the cost, and wide spreads mean thin liquidity.
- **News** is shown so you can judge it. No input was added only to look sophisticated.

The two families contradict each other on purpose. Forward tracking will show which one, if
either, is useful.

Neither family is a validated strategy. They were not fitted or backtested, so nothing was
tuned to past data. They are **not** the failed prereg-21 EIA pullback rule (ledger row 21),
which stays failed.

The outcomes are:
- **WATCH**: a setup armed. It is logged and shown on the status page, with no Telegram message.
- **LONG** or **SHORT**: an actionable alert.
- **NO TRADE**: the setup was suppressed. The reason is logged.

There is no daily quota. A setup that has not alerted within 4 hours is dropped. Each setup
can alert at most once.

**Horizons.** There are three separate time spans:
- **Entry validity:** 60 s at most.
- **Holding period (paper):** up to 8 h, ending by Friday 16:45 ET at the latest.
- **Forecast check:** the signed mid move at +60 and +240 min, measured in ATR units, for
  every delivered alert, including missed ones.

**Targets.** 1R and 2R are conditional levels. The paper trade exits at the stop, at 2R or at
the time limit. Whether price touched 1R is recorded too.

## 3. Entry validity (time for you to place the order)

The clock starts at the **timestamp of the final quote** checked just before sending. It does
not start when you read the message. The alert is valid only while **all** of these hold:
1. It has not expired.
2. The executable price (ask to buy, bid to sell) is inside the entry zone.
3. The spread is at most `max_spread` (0.08).
4. The setup has not been invalidated (a 15-min close beyond the invalidation level).
5. The quote is under 10 s old and the market is tradeable.
6. For a combined (BR-) alert: the news + chart policy still approves the same direction.
   The news state is re-read from the news log on every poll (no model call inside the
   window). If news becomes unavailable, stale, unanalysed or contradictory, or the chart
   no longer has room, the alert gets a SKIP and no paper fill can happen.

New entry suggestions also need fresh chart data: the latest completed 15-min candle must
have been processed successfully within 18 min, and the last candle refresh must not have
failed. A failed refresh blocks new entries (logged, plus one ops notice per 30 min).
Ages and the usable-time check are recomputed after the slow network and news work, just
before the decision.

SKIP and EXPIRED cancel the **entry suggestion only**. Each says so: no broker order or
position was changed, and if you already entered, it does not close your trade.

Immediately before sending, the monitor takes a fresh quote and re-runs every check. It
suppresses the alert if the quote fails any of them, or if less than 30 s would be left
after the expected Telegram delay (the p90 of measured send times). While the alert is live,
each poll re-checks it:
- If a condition fails, a **SKIP** update goes out with the same ID.
- At the end of the window, an **EXPIRED** line goes out.
- If Telegram took so long that less than 20 s is left, a SKIP goes out at once.

Each update says "Do not chase". A missed window needs a new setup and a new signal.
Telegram is not instant, so always check the broker quote before you submit.

The 60 s window is a starting value. `feasibility` measures quote age, request time and
Telegram send time. The monitor also keeps measuring them, and it blocks alerts if the
measured p90 delays leave too little time. Change the window only from those measurements,
never from results.

## 4. Alert discipline

- Each setup alerts at most once (deduplicated by day, family, side and level).
- No new alert within 15 min of the last one, or while another alert is live.
- Messages go out only for an alert, a SKIP or EXPIRED update, an operating-range change,
  or an operational problem. Operational notices repeat at most every 30 min.
- An alert is suppressed in any of these cases. The reason is logged as NO TRADE:
  - the market is closed or not tradeable;
  - the quote is stale or missing;
  - the spread is above the limit;
  - the mid or the entry zone is outside 80-115;
  - the price is outside the zone or already beyond the stop;
  - the stop is under 4 spreads away;
  - it is within 15 min of the daily close;
  - it falls inside the EIA blackout;
  - the measured delays are too long;
  - the cooldown is running.
- **Operating range 80-115.** When the mid leaves the range, one message says so and new
  suggestions pause. Monitoring, logging and open paper signals continue. The range is never
  changed by the code.

## 5. Paper tracking (frozen with the rules version)

Every alert is written to the journal **before** it is sent, with:
- the rules version and config hash;
- the quote and its timestamps;
- the entry zone, expiry, stop and targets.

The journal records every outcome: delivered or failed, SKIP, EXPIRED, missed, fill and exit.
Losing and missed signals are never dropped.

The hypothetical fill rules are `paper-v2` (from 2026-11-03; earlier records are
`paper-v1`, kept unchanged, labelled "may be affected" and never pooled with v2):
- **Earliest fill:** 20 s after the notify command accepted the alert (for a signal that was
  not sent: 20 s after the decision), only while the alert is still valid (zone, spread,
  fresh tradeable quote and, for BR-, the news + chart policy). Undelivered, expired and
  invalidated signals never get a fill.
- **Fill price:** the executable quote at that moment (ask to buy, bid to sell) plus 1 spread
  of adverse slippage. It is never the best price in the zone. The slippage can put the fill
  up to one spread beyond the zone edge; that is deliberate and recorded as `beyond_zone`.
  The quote used (bid, ask, time, age) is recorded with each fill.
- **Exit side:** a long exits at the bid, a short at the ask. A live bid/ask pair is never
  treated as a candle's low/high.
- **Stop:** live quote at or through the stop: that executable price minus 1 spread (a poll
  can miss the moment the stop was crossed, so the stop price itself is not assumed). Replay
  candle that opens through the stop: the open minus 1 spread. Otherwise the stop minus
  1 spread. If one candle touches both stop and target, the stop counts first.
- **2R target:** fills at the target once the exit-side price reaches it.
- **Time exit:** at the bid (long) or ask (short).
- **Setup invalidation after entry:** none. A 15-min close beyond the invalidation level
  cancels an open entry suggestion, but an entered paper trade exits only at the stop, the
  2R target or the time limit (the alert says the same).
- **Stale or non-tradeable quotes** never trigger a paper exit.
- **Costs:** financing at 45%/yr on either side, and the CAD conversion gap (winners x 0.99,
  losers x 1.01).
- **After a restart or gap:** an open paper trade replays the completed 1-minute bid/ask
  candles it missed. If that request fails, the last reconciled time is not advanced and
  nothing is concluded from live prices until the replay succeeds. Missing minutes while the
  market should be open are logged and the trade is labelled `data_gap`.

Results are reported in four separate parts:
1. **Operational:** delivery, freshness, suppressions.
2. **Directional:** the move at +60 and +240 min.
3. **Hypothetical paper trading:** in R, after costs.
4. **Manual results:** only what you record with `manual`.

Reports keep groups apart by experiment version, arm (combined vs chart-only), label
(chart-led vs news-supported), sent vs not sent, and paper-rules version, for directional
marks and paper P&L alike. "Sent" means the notify command accepted it; when your phone
showed it is not measured. A signal that was not sent is never called delivered.

A hypothetical fill is never treated as an order you placed. No fixed trade count and no
simple confidence interval is proof of an edge: signals overlap in time and share news and
market exposure, so resampled intervals are descriptive only.

## 6. Sizing and positions

Sizing is **not shown**, because no risk % has been approved for this module. The alert
shows the risk per barrel to the stop, in USD and in account currency. To show a size, set
`risk_pct` (for example 0.10). The size then comes from:
- NAV and account currency;
- OANDA's USD-to-account conversion;
- the stop distance plus slippage.

Position notional is shown separately and is not the risk.

Each alert lists the open BCO_USD trades on the account and the account's `hedgingEnabled`
setting. When hedging is off, an opposite-direction order reduces or closes your existing
position instead of opening a new one. Check this before any future execution work; this
module does not do execution.

## 7. Example alert (EXAMPLE, test data, not a real signal)

```
[BRENT PAPER/ADVISORY] BR-20261103-001 | NO ORDER PLACED
LONG BCO_USD (Brent CFD, OANDA practice prices)
Entry: BUY only if ask is 110.00-110.30
Valid until 09:00:59 ET (14:00:59 UTC), ONLY while the executable ask stays within 110.00-110.30 and spread is below 0.08. Otherwise skip. Check the broker quote before you submit.
Quote 08:59:59 ET (13:59:59 UTC): bid 110.170 / ask 110.200 / spread 0.030
Stop-loss order (broker): 109.50
Setup invalid if a 15-min candle closes below 110.00 (a reason to skip or exit, not a broker order)
Targets (conditional): 1R 110.90 | 2R 111.60
Holding period: up to 8h; forecast judged at +4h
Reward/risk after costs: 0.76 at 1R, 1.61 at 2R (1 spread slippage on entry and stop, financing 45%/yr for 8h, CAD conversion ±1%)
Size: not shown: no approved risk % for this module (set risk_pct). Risk per unit (1 barrel) to the stop is about 0.70 USD (~0.98 CAD) before costs.

---
Why: 15-min candle ending 09:00:00 ET (O 109.90 H 110.50 L 109.80 C 110.50) vs yesterday's high 110.00; break threshold 0.20 = 0.1 x daily ATR 2.00. It closed beyond the level and price is back at it. Scenario A: the break holds and price extends up. Scenario B: a 15-min close back through 110.00 means the break failed. Untested rule, forward paper tracking only.
Reasons to skip: oil news in the last 60 min: fast moves possible; you already hold a Brent position (see position check)
Position check: open BCO_USD: SHORT 1,500 (trade 287 at 116.20). Account hedging is OFF: this order would reduce or close that position instead of opening a separate trade
News (context only; not a direction call, may already be priced in):
- [Brent] Brent edges higher as traders weigh supply outlook (Example Wire; published 08:20:00 ET, received 09:00:00 ET) https://example.com/brent-story
Decided 09:00:00 ET (14:00:00 UTC). Rules brent-v1 cfg c1484be01af2. Paper tracking only.

[BRENT PAPER/ADVISORY] BR-20261103-001 SKIP: no longer valid at 09:00:14 ET: ask 110.430 left the entry zone 110.00-110.30. Do not chase. A new evaluation and a new signal are needed.
```

## 8. Running it

Two processes run on **hermes-server** (Brent trades nearly 24 hours a day and the laptop
sleeps), each in its own tmux window and each holding a lock so a second copy refuses to
start (exit code 3):
- `brentnews`: the news collector (`news-check --analyze`). It runs **until stopped**;
  `--minutes N` or `--once` are only for bounded tests. It retries pending classifications
  after a quiet window, budget refusal or failed call (backoff up to 30 min after failures),
  and sends one `[BRENT OPS]` notice per 30 min if the price monitor stops or stalls.
- `brent`: the price monitor (`run`). It watches the news collector's heartbeat and coverage
  and sends one `[BRENT OPS]` notice per 30 min if the collector stops, goes stale or loses
  coverage; entry alerts then wait (WATCH).

The OANDA practice token is typed at a hidden prompt when the monitor starts. It is never
saved or printed. If `OANDA_API_KEY` is already in the environment, the monitor uses it.

```bash
ssh -t hermes-server
bash ~/ta-brent/scripts/brent_activate.sh   # clean restart of both; writes brent.toml if missing
tmux attach -t brent                        # type the token; Ctrl+B then D
```

`alerts_enabled` in `brent.toml` is re-read every 60 s, so alerts can be switched on or off
(shadow mode) **without a restart**. The switch is logged and announced with one
`[BRENT STATUS]` message. Every other setting needs a restart.

- **Stop:** `... stop` (monitor) or `... stop --news` (collector): a clean SIGTERM.
- **Status:** `python -m tradingagents.brent --config brent.toml status` (offline). The first
  line is `overall: OK` only when the monitor ticks, chart data refreshes, the news collector
  runs and news coverage is fresh; otherwise `overall: DEGRADED:` with the reasons.
- **Results:** `... report`. Record what you actually did with
  `... manual BR-20261103-001 --entered --price 110.22 --units 5` or `... manual <id> --skipped`.
- **Status page:** `~/.tradingagents/brent/brent.html`, plus `<site_dir>/index.html` when
  `site_dir` is set. It is rewritten every 5 min. It is read-only, with no controls.
- **Files:**
  - `signals.jsonl`: every event, append-only.
  - `state.json`: armed setups, live signals, paper trades and metrics.
  - `monitor.lock`: allows one monitor at a time. A second `run` exits with code 3.

On startup the monitor sends one `[BRENT TEST]` message, which also measures Telegram send
time. It then recovers any windows and paper trades left from before a restart.

## 9. Known limitations

- Practice prices can differ from what your own trading app shows. Always check the quote in
  your app.
- With 3 s polling, a quote can move into and out of the zone between polls without being
  seen.
- Telegram send time is measured until `hermes send` returns. The time until your phone shows
  the message is not measured. Compare the alert's "Decided" time with when you see it.
- Yahoo headlines can be minutes to hours late, and they are a sample.
- The holiday-shifted EIA dates and other scheduled events, such as OPEC meetings, must be
  added to `blackouts` by hand.
- The token must be typed again after a server restart (by design: unattended start would
  mean storing the token, which is your decision).
- The rules are untested hypotheses. Months of forward data are needed before any judgment.
- News coverage currently depends on one aggregator (Google News searches); Yahoo returned
  nothing for BZ=F/CL=F and is off by default. opec.org blocks automated access (Cloudflare 403,
  checked 2026-11-05), so OPEC news also comes through Google. If Google fails, coverage is
  missing and alerts wait (WATCH).
- EIA answers 403 on the WPSR files before the release (Wed, and Thu for holiday weeks,
  08:00-12:30 ET). Those are logged as `eia_not_released`, not as fetch errors, and do not
  start a backoff.
- The model sees headlines and RSS snippets only, never publisher article text. Article
  "content checks" only test whether a link opens (Google links land on a redirect page).
- EIA files are logged by version but not interpreted (no reliable consensus expectations).
- If the server reboots, both processes stop and nothing sends a notice (each watches the
  other, not the machine). The status page and `status` show it.
- The OANDA client only has GET methods and uses the practice host; that does not make the
  token itself read-only at the broker.

## 10. News + chart decision layer

On 2026-11-03 Irakli asked that decisions combine news analysis with chart analysis.
The design is in the Claude Doc "Brent assistant: revised decision design". Phase 1
collects and classifies news **without sending alerts**, so feed timeliness and model
behaviour can be measured first.

```bash
python -m tradingagents.brent --config brent.toml news-check --minutes 240 --analyze   # tmux window
python -m tradingagents.brent --config brent.toml news-report
```

### Feeds

| Feed | Source | Key needed |
|---|---|---|
| Google News RSS | Queries `Brent crude`, `OPEC+ oil output`, `oil supply disruption`, `site:opec.org` | none |
| Yahoo Finance (off by default since 2026-11-05: no news for these tickers in 1,073 server polls) | `BZ=F`, `CL=F` | none |
| EIA WPSR release files | `ir.eia.gov/wpsr/summary.txt`, `table1.csv` | none |

The EIA API v2 needs a free key, but these release files do not. Every feed is treated as
unverified for timeliness until the report shows measured delays.

### Logged per item

Everything goes to `news.jsonl`:
- publish time, first-seen time and delay;
- whether the item was published before the collector started (excluded from delay stats);
- duplicates and repeat clusters;
- fetch errors;
- content checks: status, host, word count, paywall hint. Article text is never stored.

### Logged per model call

- model id and prompt hash;
- input keys, raw output and the validated result;
- latency and token usage;
- the resulting news state.

### Definitions (fixed before alerts)

- **with-trend:** the daily regime matches the direction. Up means a 20-day regression slope
  of at least +0.1 ATR/day; down means at most -0.1 ATR/day. A range regime is never
  with-trend.
- **relevant:** concerns crude supply, demand, OPEC+ policy, inventories, or geopolitics
  that affects oil production or shipping.
- **material:** all of these hold:
  - the item is relevant;
  - its status is official or credible_report (not rumour or opinion);
  - its scope is brent or broad_oil (wti_us is context only);
  - a mechanism is stated;
  - it is not a repeat or update of an older story.
- **fresh, for support:** published, or first seen if there is no publish time, within 6h.
  That is inside the 8h holding period. Older supportive news never authorises a new trade.
- **fresh, for conflict:** within 24h. Opposing material news blocks for longer.
- **fresh check (coverage by source function):** at least 2 **general news** feeds (Google
  News searches) polled successfully in the last 10 min, and every recent item has
  been analysed. EIA files are official data: they are logged by content version (a new
  release or revision at the same URL is a new item) but never count as a news check and
  are not interpreted.
- **news status** (kept apart in the log, the status page and each decision): `checked`
  (sources checked; the state says what was found, e.g. `none_material`),
  `collector_missing`, `collector_stale` (no poll attempt in 10 min), `missing_coverage`,
  `pending_analysis` (e.g. quiet window or budget), `analysis_failed`. Anything but
  `checked` means WATCH.

### Policy

The policy is implemented in `news.policy()` and covered by tests:

| Situation | Outcome |
|---|---|
| News unavailable | WATCH |
| Material conflict within 24h | NO TRADE |
| Mixed news | WATCH |
| Support within 6h, with-trend | Alert, labelled **news-supported** |
| Support within 6h, counter-trend, FAILED_BREAK setup with its own invalidation | Alert, labelled **news-supported, counter-trend** |
| Support within 6h, any other counter-trend setup | WATCH |
| No material news on a fresh check, with-trend | Alert, labelled **chart-led, no material news** |
| No material news on a fresh check, otherwise | WATCH |

### Model

- `llm_provider = "chatgpt"`, `llm_model = "gpt-6-luna"`: the existing repo integration on
  the ChatGPT plan sign-in.
- Ceilings of 6 calls an hour and 40 a day.
- No calls from 07:30 to 10:30 ET on weekdays, while the morning bot runs.
- One call only when new items arrive.
- Any failure, invalid JSON or refused budget gives news state `unavailable`, and therefore
  WATCH.

## 11. Combined decision in the monitor (alerts-only, experimental)

The monitor reads the news state from `news.jsonl` (written by the `news-check` process)
on every candidate. It never calls the model itself, and no model call happens after the
60-second clock starts. Both processes must run:

```bash
tmux new -s brentnews   # python -m tradingagents.brent --config brent.toml news-check --analyze
tmux new -s brent       # python -m tradingagents.brent --config brent.toml run   (type the token)
# or, on hermes-server, all in one: bash ~/ta-brent/scripts/brent_activate.sh
```

For each M15 setup that passes the chart blockers (range, spread, quote age, blackouts,
market hours, cooldown):

1. **Chart-only comparison arm.** It records one `SH-…` signal and paper-tracks it with the
   same fill rules. It never sends to Telegram.
2. **Candidate record.** Chart assessment (`chart.assess`) plus news view plus policy row are
   logged as a `candidate` event, whether approved or rejected.
3. **Combined arm.** If the policy action equals the setup direction, a final fresh quote is
   taken and the assessment is recomputed. If the action or label changed, it is suppressed
   ("assessment changed at final check"). Otherwise a `BR-…` signal is issued.
   - If `alerts_enabled = true`, it is sent to Telegram.
   - If `alerts_enabled = false`, it is logged and paper-tracked only.

Extra abstentions:
- **No room:** less than 1R to the next opposing level gives NO TRADE.
- **Unsettled:** a material item less than 15 min old, with price moved more than 0.5 ATR,
  gives NO TRADE. If that move can't be measured, the result is WATCH.

Each `BR-…` alert links to `<report_base_url>/signals/<id>.html`. That page shows an SVG
chart with the zone, stop, targets and levels, the news table and the full decision record.

`report` keeps the arms separate:
- combined paper results;
- candidates by policy row;
- the chart-only comparison.

Telegram acceptance time is what gets measured. When your phone showed the message is not
measured.

## 12. Experiment identity and the 2026-11-03 audit

Every signal and every monitor start carries a manifest: experiment version
(`brent-exp-2`), setup rules (`brent-v1`, unchanged), chart policy (`chart-v1`), news policy
(`news-policy-v2`), paper rules (`paper-v2`), code commit, model id, prompt hash, config hash
and the news/model settings. Reports group by experiment, so earlier records (no manifest)
never pool with new ones. Strategy parameters, the 80-115 range, risk settings and model
budgets did not change.

The independent audit of e10d2f3 (2026-11-02) found ten issues. Each was reproduced first
(the seven supplied probes all failed on e10d2f3) and is now covered by tests:

| # | Finding | Status |
|---|---|---|
| 1 | News not rechecked inside the entry window | Confirmed, fixed (SKIP + no fill) |
| 2 | Failed chart refresh still allowed entries | Confirmed, fixed (fresh processed candle required) |
| 3 | Two EIA files counted as news coverage | Confirmed, fixed (coverage by source function) |
| 4 | Paper targets used the wrong quote side | Confirmed, fixed (bid for longs, ask for shorts) |
| 5 | Live stop gaps filled at the stop | Confirmed, fixed (adverse executable price) |
| 6 | Updated EIA file at the same URL ignored | Confirmed, fixed (content version in identity) |
| 7 | Pending analysis retried only on new items | Confirmed, fixed (retry + backoff, oldest first) |
| 8 | News collector stopped after 72 h | Confirmed (server ran `--minutes 4320`), fixed (continuous) |
| 9 | Experiment identity incomplete | Confirmed, fixed (manifest) |
| 10 | Directional marks pooled across arms | Confirmed, fixed (grouped; shadow never "delivered") |
| - | Windows sender test asserted Linux behaviour | Confirmed, test corrected |

## Notification types (exp-3)

| Type | Label | Meaning |
|---|---|---|
| WATCH | `[BRENT WATCH] BW-… NOT AN ENTRY` | A defined setup is developing. Shows the level, possible zone, current blocker, news and chart. Sent once per material state (price not in zone / news-policy blocker / other checks), never on every poll. Creates no signal and no paper fill. Only for new setups inside the operating range. |
| ENTRY | `[BRENT PAPER/ADVISORY] BR-… ENTRY` | All current checks passed on a fresh quote. Unchanged 60-second validity from the quote time. Says `follows WATCH BW-…` when a WATCH came first. |
| UPDATE / CANCEL | `… SKIP` / `… EXPIRED` / `[BRENT WATCH] BW-… CANCELLED` | A material change or invalidation, linked to the same ID with the reason. A WATCH whose setup became an ENTRY is linked, not cancelled. |

`watch_alerts = false` turns WATCH messages off. WATCH sends are rate-limited per setup
(`watch_min_gap_seconds`, default 300). Outcome tracking stays separate: only ENTRY alerts and
chart-only shadow signals are paper-tracked; WATCH events are journalled as `watch_notice` /
`watch_end`.
