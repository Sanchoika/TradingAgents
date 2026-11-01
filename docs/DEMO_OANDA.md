# Running TradingAgents on an OANDA demo account

`python -m tradingagents.demo` turns TradingAgents' daily rating into small,
stop-protected trades on an **OANDA practice account**, logs every decision, and
scores the results after costs against a coin-flip baseline. It only talks to
`api-fxpractice.oanda.com`; there is no setting that points it at a live account.

This is a forward test, not proof of an edge. The rules below are fixed before
the first trade. Judge it only after about 150 closed trades.

## What it does each run

For each instrument in the config (default: NAS100_USD analysed as QQQ, SPX500_USD as SPY):

1. Runs the full TradingAgents analysis for today and reads the final rating.
2. Maps it to a target: Buy/Overweight = long, Hold = flat, Underweight/Sell = short.
   REVIEW (unreadable decision) = do nothing.
3. Same direction as the open trade: keep it. Different: close it, then open the new side.
4. New trades: stop at 2 x daily ATR(14); size so that hitting the stop loses 0.5% of
   the account. Skipped if the market is closed or the spread is above 10% of ATR.
5. If the account falls 10% below its starting balance, no new trades are opened
   (closes still happen).

Only trades tagged `tradingagents` are touched; anything you open by hand is left alone.
Everything is written to `~/.tradingagents/demo/journal.jsonl`.

## Setup (Windows PC, in C:\bbt\TradingAgents)

```powershell
git fetch origin; git checkout main; git pull
.venv\Scripts\activate
pip install -e .
copy demo.example.toml demo.toml
```

Open `.env` in Notepad and add these lines yourself (never paste keys into a chat):

```
OANDA_API_KEY=...        # practice account token from OANDA's demo hub
OANDA_ACCOUNT_ID=...     # e.g. 101-004-1234567-001
DEEPSEEK_API_KEY=...     # or the key for whichever provider demo.toml names
```

## Free checks first (no LLM cost, no real decisions)

```powershell
python -m tradingagents.demo --config demo.toml check
python -m tradingagents.demo --config demo.toml run --fixed-rating Buy --dry-run
```

`check` shows the balance, prices, spreads and ATR, and whether the LLM key is set.
The dry run goes through the whole order path with a fixed rating and sends nothing.

## First real run (paid LLM calls)

```powershell
python -m tradingagents.demo --config demo.toml run --dry-run   # real analysis, no orders
python -m tradingagents.demo --config demo.toml run             # real analysis + demo orders
```

**Cost:** one TradingAgents run is roughly 20 to 40 LLM calls. With a cheap model
(the example uses DeepSeek Flash) expect a few cents up to about $0.50 per
instrument per run; check your provider's usage page after the first dry run to get
the real number. Two instruments on every trading day is about 42 runs a month.

## Run it every weekday

Windows Task Scheduler, weekdays at 16:00 (Georgia time; 07:00 New York in winter, before
the US open):

```powershell
schtasks /Create /TN TradingAgentsDemo /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 16:00 ^
  /TR "cmd /c cd /d C:\bbt\TradingAgents && .venv\Scripts\python -m tradingagents.demo --config demo.toml run >> demo.log 2>&1"
```

Or on a Linux server with cron: `0 12 * * 1-5 cd ~/TradingAgents && .venv/bin/python -m tradingagents.demo --config demo.toml run >> demo.log 2>&1` (UTC).

## Reading the results

```powershell
python -m tradingagents.demo --config demo.toml report
```

Shows closed trades, total and mean P&L after spread and financing, win rate, a 95%
confidence interval for P&L per trade, and `p_random_as_good`: how often the same
trades with coin-flip directions did at least as well. An edge needs the confidence
interval above zero **and** a small `p_random_as_good` after ~150 trades.

Known limits: LLM output varies run to run; the agents' data comes from Yahoo for the
ETF, not OANDA's CFD; the baseline's spread cost is estimated from the entry fill.
