# Demo runner audit (2026-11-02)

**Status 2026-11-02:** items 1, 4-10 and 12's config are built and merged (PRs 4-6; 6 adds the
EDGAR date check and sector comparison). Open: 2 and 3 and 12 need Irakli to run a scheduler
command or enter a Windows password; 11 (hermes-server) and Telegram status/pause are not built.


Audit of the paper forward test as configured on the PC: earnings mode, paper
account, ChatGPT plan, weekdays 08:00 New York, Telegram digest, Obsidian notes.
Ordered by how much each item threatens a trustworthy result.

## Fix before or soon after the first trades

1. **No cap on open positions (risk).** Up to 10 new trades a day, each held 5
   trading days, can mean ~50 open trades at 0.5% (or 0.25%) risk each: 15-25% of
   the account riding on one market move, mostly in the same direction during
   earnings season. The 10% drawdown halt only stops new trades. Proposal:
   `max_open_positions` (e.g. 15) and a cap on net long/short exposure. This is a
   rule change, so add it before the first trades or not at all.
2. **Runs depend on the PC being on and logged in.** The task is "interactive
   only". Options: let Task Scheduler run whether logged on or not, or move the job
   to hermes-server (always on). A skipped day is only reported by the next run.
3. **No watchdog for a silent day.** A missing Telegram digest is the only signal.
   A small Hermes cron check ("no journal entry today by 09:00 ET -> alert") closes
   this without letting Hermes trade or change settings.
4. **ChatGPT sign-in can lapse.** Refresh tokens rotate; if the sign-in ends, every
   analysis fails. The crash/error digest will show it, but the fix (sign in again)
   is manual. Worth a line in the digest: "ChatGPT sign-in expired, run
   `tradingagents login chatgpt`".

## Make the result easier to trust

5. **Log the model per decision.** Done in this change: each run records
   `provider:quick/deep`. Gemini and ChatGPT already disagreed on NVDA, so a silent
   model change would split the test in two.
6. **Earnings timing.** Yahoo's earnings times can be missing or shifted, and
   before-open vs after-close reports are treated alike. Log which it was, and
   check a sample of reporters against a second source.
7. **Paper fills are generous for shorts and gaps.** No borrow fees, no hard-to-
   borrow refusals, fills exactly at the open plus 5 bps. Raise `paper_spread_bps`
   or add a short-borrow cost so results are not flattered.
8. **Report by rating and by side.** The dashboard now shows results by rating;
   the report should also split long vs short and compare with a sector ETF, so a
   bull market does not pass for skill.

## Nice to have

9. Weekly scorecard to Telegram (from `report`), not only daily digests.
10. Store each full TradingAgents report path in the journal for later review.
11. Run the job on hermes-server with its own ChatGPT sign-in, keeping the PC free.
12. Watchlist mode on ETFs/crypto alongside earnings mode, in a separate journal,
    as a second, independent test.

Not recommended mid-test: changing the trading rules, the model, the universe or
the holding period. Each restarts the count toward 150 trades.
