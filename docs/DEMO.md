# Forward-testing TradingAgents for free

`python -m tradingagents.demo` turns TradingAgents' ratings into small, stop-protected
paper trades, logs every decision, and scores the results after costs against a
coin-flip baseline. Two accounts are supported:

- **paper** (default, free, no signup): a built-in paper account on Yahoo's daily
  prices. Works for any Yahoo symbol: US stocks, ETFs, crypto (BTC-USD), forex
  (EURUSD=X). Orders fill at the **next day's open** plus spread, so a rating never
  trades on a price it already saw. Stops are checked on each day's high/low.
- **oanda**: an OANDA **practice** account (`api-fxpractice.oanda.com` only; there is
  no setting for a live account).

Two modes:

- **watchlist**: rate every instrument on each run (weekly is best, see below).
- **earnings**: every weekday, rate only the stocks in `universe` that reported
  earnings since the last run, trade at the next open, exit after `hold_days`
  (default 5). This is where TradingAgents' news and fundamentals reading has the
  most to work with. Example: `demo.earnings.example.toml`.

This is a forward test, not proof of an edge. Fix the rules before the first trade
and judge only after about 150 closed trades.

## What a watchlist run does

For each instrument in the config:

1. Runs the full TradingAgents analysis for today and reads the final rating.
2. Maps it to a target: Buy/Overweight = long, Hold = flat, Underweight/Sell = short.
   REVIEW (unreadable decision) = do nothing.
3. Same direction as the open trade: keep it. Different: close it, then open the new side.
4. New trades: stop at 2 x daily ATR(14); size so that hitting the stop loses 0.5% of
   the account. Skipped if the market is closed or the spread is above 10% of ATR.
5. If the account falls 10% below its starting balance, no new trades are opened
   (closes still happen).

On OANDA only trades tagged `tradingagents` are touched. Everything is written to
`~/.tradingagents/demo/journal.jsonl`; the paper account lives in `paper_account.json` beside it.

## Setup (Windows PC, in C:\bbt\TradingAgents)

```powershell
git fetch origin; git checkout main; git pull
.venv\Scripts\activate
pip install -e .
copy demo.example.toml demo.toml
```

For the free setup, get a free key at Google AI Studio (aistudio.google.com) and add it
to `.env` yourself in Notepad (never paste keys into a chat):

```
GOOGLE_API_KEY=...
# only if you use broker = "oanda":
OANDA_API_KEY=...        # practice account token
OANDA_ACCOUNT_ID=...     # e.g. 101-004-1234567-001
```

The free Gemini tier limits requests per minute and per day (see AI Studio's rate-limit
page); one full analysis is roughly 20-40 requests, so expect a handful of analyses a
day. `max_analyses_per_run` keeps earnings mode inside that. Fully offline alternative:
Ollama with a local model (`llm_provider = "ollama"`), if your PC can run one.

### ChatGPT plan instead of an API key

With ChatGPT Plus or Pro, sign in once with `tradingagents login chatgpt` (it opens
your browser) and set `llm_provider = "chatgpt"` with OpenAI model names in `[llm]`
(see the comment in `demo.earnings.example.toml`). No key is stored in `.env`; runs
count toward the plan's usage limits.

### Notes and alerts

- `notes_dir`: each real run appends to `<date> TradingAgents.md` there (summary plus
  every analysis in full), and `report` rewrites `TradingAgents Scorecard.md`. Point it
  inside an Obsidian vault.
- `notify_command`: run after each real run with the short summary on stdin, for
  example a command that forwards it to Telegram through Hermes. Its failure never
  fails the run.
- A stock that fails to analyse, or is over `max_analyses_per_run`, is retried on the
  next runs for `retry_days` trading days.

## Checks first (no LLM calls)

```powershell
python -m tradingagents.demo --config demo.toml check
python -m tradingagents.demo --config demo.toml run --fixed-rating Buy --dry-run
```

`check` shows the balance, prices, spreads and ATR, and whether the LLM key is set.
The paper account's first `run` creates it with 100,000 USD.
The dry run goes through the whole order path with a fixed rating and sends nothing.

## First real run

```powershell
python -m tradingagents.demo --config demo.toml run --dry-run   # real analysis, no orders
python -m tradingagents.demo --config demo.toml run             # real analysis + demo orders
```

**Cost:** free with the Gemini free tier or Ollama. With a paid provider, one run is
roughly 20 to 40 LLM calls, a few cents to about $0.50 with a cheap model.

## Why weekly

TradingAgents rates a 5-trading-day holding period, and a simulation of these rules on
2019-2026 prices (scripts in the project files, `tradingagents/sim-2026-11-01/`) found
weekly runs beat daily ones whenever the ratings had any skill: fewer trades, less
spread and financing, and a fifth of the LLM bill. With weekly runs, a trade stopped
out mid-week stays flat until the next run. The same simulation showed no setting
turns random ratings into a profit; settings only change how much a real edge is worth.

## Schedule it

Watchlist mode once a week, earnings mode every weekday (use `/D MON,TUE,WED,THU,FRI`).
Windows Task Scheduler, Mondays at 16:00 (Georgia time; 07:00 New York in winter, before
the US open):

```powershell
schtasks /Create /TN TradingAgentsDemo /SC WEEKLY /D MON /ST 16:00 ^
  /TR "cmd /c cd /d C:\bbt\TradingAgents && .venv\Scripts\python -m tradingagents.demo --config demo.toml run >> demo.log 2>&1"
```

Or on a Linux server with cron: `0 12 * * 1 cd ~/TradingAgents && .venv/bin/python -m tradingagents.demo --config demo.toml run >> demo.log 2>&1` (UTC).

## Reading the results

```powershell
python -m tradingagents.demo --config demo.toml report
```

Shows closed trades, total and mean P&L after spread and financing, win rate, a 95%
confidence interval for P&L per trade, and `p_random_as_good`: how often the same
trades with coin-flip directions did at least as well. An edge needs the confidence
interval above zero **and** a small `p_random_as_good` after ~150 trades.

Known limits: LLM output varies run to run; Yahoo's free data can be late or missing;
the paper account charges no financing or borrow fees and counts P&L in each symbol's
own currency; on OANDA the agents read the tracking ETF, not the CFD.
