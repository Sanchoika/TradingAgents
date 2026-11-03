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
4. The setup has not been invalidated.
5. The quote is under 10 s old.

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
  - the mid or the entry zone is outside 100-120;
  - the price is outside the zone or already beyond the stop;
  - the stop is under 4 spreads away;
  - it is within 15 min of the daily close;
  - it falls inside the EIA blackout;
  - the measured delays are too long;
  - the cooldown is running.
- **Operating range 100-120.** When the mid leaves the range, one message says so and new
  suggestions pause. Monitoring, logging and open paper signals continue. The range is never
  changed by the code.

## 5. Paper tracking (frozen with the rules version)

Every alert is written to the journal **before** it is sent, with:
- the rules version and config hash;
- the quote and its timestamps;
- the entry zone, expiry, stop and targets.

The journal records every outcome: delivered or failed, SKIP, EXPIRED, missed, fill and exit.
Losing and missed signals are never dropped.

The hypothetical fill rules are:
- **Earliest fill:** 20 s after Telegram accepted the alert, and only while the alert is
  still valid.
- **Fill price:** the executable quote at that moment (ask to buy, bid to sell) plus 1 spread
  of slippage. It is never the best price in the zone.
- **Stop:** fills 1 spread worse than the stop. A gap through the stop fills at the bar open
  minus 1 spread.
- **2R target:** fills at the target.
- **Time exit:** at the bid (long) or ask (short).
- **Costs:** financing at 45%/yr on either side, and the CAD conversion gap (winners x 0.99,
  losers x 1.01).
- **After a restart:** an open paper trade replays the 1-minute bid/ask candles it missed,
  and the stop is assumed to hit first.

Results are reported in four separate parts:
1. **Operational:** delivery, freshness, suppressions.
2. **Directional:** the move at +60 and +240 min.
3. **Hypothetical paper trading:** in R, after costs.
4. **Manual results:** only what you record with `manual`.

A hypothetical fill is never treated as an order you placed. No edge is claimed from a
handful of alerts or from a trade count alone.

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

The monitor runs on **hermes-server**, because Brent trades nearly 24 hours a day and the
laptop sleeps. It runs in its own tmux window, the same way as the old tbot. The OANDA
practice token is typed at a hidden prompt when the monitor starts. It is never saved or
printed. If `OANDA_API_KEY` is already in the environment, the monitor uses it.

```bash
ssh -t hermes-server
cd ~/TradingAgents && cp brent.example.toml brent.toml   # once; set site_dir if wanted
. .venv/bin/activate
python -m tradingagents.brent --config brent.toml check          # read-only facts
python -m tradingagents.brent --config brent.toml feasibility --send-test
tmux new -s brent
python -m tradingagents.brent --config brent.toml run            # type the token; Ctrl+B then D
```

- **Status:** `python -m tradingagents.brent --config brent.toml status` (offline, reads saved state).
- **Stop:** `python -m tradingagents.brent --config brent.toml stop`, or Ctrl+C in the tmux window.
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

## 10. News + chart decision layer (phase 1: checks only)

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
| Yahoo Finance | `BZ=F`, `CL=F` | none |
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
- **fresh check:** at least 2 feeds were polled successfully in the last 10 min, and the
  analysis succeeded.

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
