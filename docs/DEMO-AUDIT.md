# Demo runner audit (2026-11-02)

**Status 2026-11-05 (end of day):** items 1-10 are built, merged and deployed on the PC. #2 (runs
whether logged on or not, wakes the PC) and #3 (watchdog 09:30) are done. #12 was dropped. Telegram
status/pause became the phone control page (docs/CONTROL.md, installed). Hold length is now exactly 5 NYSE
sessions (Irakli's decision, PR #22); the 150-trade count restarts 2026-11-05 with nothing lost.
#11 is done (cut over 2026-11-05 ~05:10 UTC on Irakli's word): the run and watchdog now execute on
hermes-server as `ta-demo.timer` (Mon-Fri 08:00 America/New_York) and `ta-watchdog.timer` (09:30), from
`/root/TradingAgents` with the journal in `/root/.tradingagents/demo`. The data was copied with the PC frozen
and checksums matched; `check` was identical on both machines. The PC tasks `TradingAgentsDemo` and
`TradingAgentsWatchdog` are disabled, never to run alongside the server. Run notes go to
`/root/ta-dashboard/notes/trading-demo`; the PC task `TradingAgentsPullNotes` (weekdays 09:00 ET) copies them into
`C:\Brain\outputs\trading-demo`. Rollback: disable the two server timers, copy the journal folder back, re-enable
the PC tasks. A missed day stays missed (`Persistent=false`), as before. The first server run is 2026-11-05 08:00 ET.
Nothing on the audit list is open. Follow-up: the control page's "Retry a failed stock run" is still greyed out
(it needs a code change now that the runner is hosted here).


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

## Stage B correctness pass (2026-11-05)

Fixed, each with a test that failed before the fix (`tests/test_demo_paper.py`, bottom):

- Exit-queued ("closing") positions now count toward the position and net-exposure caps and the
  "already held" check, and keep their stop until the exit bar. A stop hit before the exit bar wins;
  the exit bar itself fills at its open.
- `report` no longer calls `sync()`; only `run` settles fills and stops (and journals them once).
- Sector comparison fetches each fund once, as of the latest close that needs it (it used to reuse
  the first, possibly shorter, history).
- Runs write `run_end`; the missing-run check needs it, so a crash after `run_start` is flagged.
  Journals from before this change are trusted for earlier dates.
- A failed earnings lookup keeps its old window (`lookup_retry` in state) after the scan watermark moves.
- `run` takes a single-writer lock (`run.lock`, stale-owner takeover); the paper account file is
  written atomically (temp file + replace).
- Report: added a day-clustered bootstrap CI (`mean_pnl_ci95_by_day`) and labelled the random
  baseline as a rough control, since it flips direction on strategy-dependent exits. 150 trades is a
  trade-count milestone, not proof.

**Rule change, decided by Irakli 2026-11-05 (hold length).** Before: the exit was ordered when
`busday_count(fill day, run day) >= 5` and filled at the next open, so trades were held about 6
sessions, and NYSE holidays were ignored. Now (`demo/sessions.py`): a trade filled at the open of
session D with hold_days=5 exits at the open of session D+5 (five full sessions held); the order is
placed on the run before that open, holidays come from a fixed NYSE list for 2026-2028 (extend it
yearly). `score_ratings` uses the same window. The 150-trade count restarts from 2026-11-05; at the
time of the change all 12 orders were still pending and none had closed, so no result was lost.
