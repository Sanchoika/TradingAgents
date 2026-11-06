# Runbook: moving the stock test from the PC to the server (#11)

**Status: dry-checked only. Do not run any step marked CHANGE until Irakli writes the exact
words "cut over".** Ordinary deploys, backups and nightly checks never touch the stock test.

The stock test runs on the PC today (`TradingAgentsDemo` 08:00 ET, `TradingAgentsWatchdog`
09:30 ET). The server has `ta-demo.timer` and `ta-watchdog.timer` installed and **disabled**.
The 2026-11-05 cutover was rolled back the same day. Its scripts are kept in `C:\bbt\cutover-11\`.
The server's copy of the journal from that attempt is parked at
`/root/.tradingagents/demo.rolled-back-20261105`.

## When

Run this on a weekend, or on a weekday after 10:00 ET once that day's PC run has written
`run_end`. Never run it between 07:50 and 08:40 ET (`python -m tradingagents.ops deploy`
refuses that window too).

## Dry check (safe, read-only; run it any time)

1. On the PC, open the dashboard (or `data.json`). Check that the last run has an end record,
   and note the open and pending counts.
2. On the server, run `systemctl is-enabled ta-demo.timer ta-watchdog.timer`. Both must say
   `disabled`.
3. On the server, run `python -m tradingagents.ops nightly --backup-dir /root/backups/brent
   --snapshot /root/ta-dashboard/data.json --state /tmp/dry-nightly.json`. Use a scratch state
   file so no notice is sent. It must report no problems.
4. On the server, check that the newest backup is under 26 hours old:
   `ls -t /root/backups/brent | head -1`.
5. Compare the PC journal line count with the parked server copy. Expect the PC to be ahead,
   because the server copy is from 2026-11-05.
6. Check that the server can sign in to the model the same way the PC does. Irakli does that
   sign-in himself. Never copy tokens through chat.

Record the results in the thread. The dry check changes nothing.

## Cutover (CHANGE, only after "cut over")

1. On the PC, disable `TradingAgentsDemo` and `TradingAgentsWatchdog`. Do not delete them.
2. Copy the PC journal, state and paper account to the server folder
   `/root/.tradingagents/demo`. Then compare SHA-256 checksums for each file. If any checksum
   differs, stop.
3. On the server, run `systemctl enable --now ta-demo.timer ta-watchdog.timer`.
4. Read back the result:
   - `systemctl list-timers ta-*` shows the next 08:00 ET run;
   - `python -m tradingagents.demo report` on the server shows the same open count as the
     PC did in dry check step 1;
   - the dashboard snapshot is now built on the server.
5. After the first server run, check that it wrote `run_end` with `status=success` and
   that no order was duplicated.

## Rollback

1. On the server, run `systemctl disable --now ta-demo.timer ta-watchdog.timer`.
2. Move the server copy aside: `/root/.tradingagents/demo.rolled-back-<date>`. Never delete it.
3. Copy back to the PC any journal rows the server wrote, then check the checksums.
4. On the PC, re-enable `TradingAgentsDemo` and `TradingAgentsWatchdog`.
5. Read back the result: the PC tasks are Ready, and the next run time is 08:00 ET.

Positions, frozen rules (model, universe, hold days, risk, stops, caps, spread) and Irakli's
manual OANDA trades are never changed by any step here.
