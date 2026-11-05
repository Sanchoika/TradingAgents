# Quiet-window replacement test (brief item 3)

The weekday 07:30-10:30 ET pause on Brent model calls stays on. It protects the morning stock run,
which uses the same ChatGPT plan. It is replaced only if all three steps below pass.

## Candidate rule

Inside the window, Brent may call the model only for urgent items (the code-defined `URGENT_WORDS`),
at most 2 calls per rolling hour. Everything else waits for 10:30 as today.

## Steps

1. **Shadow (live from 2026-11-05).** Every quiet-window refusal logs `quiet_shadow` in news.jsonl:
   whether the candidate rule would have called, and which urgent items it would have served (each
   item once). `brent status` shows the 24h counts. No extra calls are made.
   After 10 weekday mornings, report: urgent items held back by the window, how long they waited,
   and how many calls the candidate rule would have made.
2. **Concurrent load test, on a weekend (no stock run).** Measure the morning run's model load from
   the same 10 days of demo.log. Then run that load on the PC sign-in alone, and again with Brent
   calling at the candidate rate on the server sign-in. Pass: zero provider errors or rate limits,
   and the stock-sized workload's latency within 20% of the solo run. Run once; it uses plan calls.
3. **Decision.** Only if 1 shows the window delays material news and 2 passes: propose the switch
   to Irakli (decision card), with an automatic fallback to the full window if a morning stock run
   ever reports a provider error.

If any step fails or is inconclusive, the window stays and the result is recorded here.

## Results

(none yet)
