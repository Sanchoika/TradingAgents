# Multi-market desk v1: design (2026-11-12, before any code or results)

**Goal:** run the bot's existing setups on **gold (XAU_USD), WTI (WTICO_USD), EUR/USD (EUR_USD) and NAS100 (NAS100_USD)** next to Brent. It is research only: graded RESEARCH CALL cards and paper tracking, never real alerts. NORTH practice orders do **not** follow them; that is a later switch, OFF, decided by Irakli.

## Risk walls (written first)

1. **The Brent instance is untouched:** its code path, config, fingerprint and count.
2. **Each market is a separate instance** of the same code: its own config, journal and state, and its own go/no-go count per setup family. One market failing cannot stop another (separate processes).
3. **`research_only = true` for every new market.**
   - The real-alert ("combined") arm is never issued: the news check is oil-specific, so it cannot judge gold or NAS100.
   - Research cards and paper tracking work as for Brent's research track.
4. **Read-only broker access only,** the same token and account as Brent (practice). No orders from the bot, ever.
5. **Card limits:** at most 3 research cards per market per New York day (`research_call_cap = 3`), plus Brent's own 8.
6. **Shared risk book (design only, for when practice is switched on later):**
   - total open risk across all markets ≤ 1.5% (as now);
   - correlated markets count as one: Brent + WTI are one group; gold and EUR/USD are linked through the US dollar;
   - at most one trade per group and direction;
   - when more setups fire than room allows, take the earliest.

   No limit is raised. Nothing of this is active until Irakli switches practice on for a market.
7. **No paid data and no LLM calls** for the new markets.

## Code changes (bot repo; default off, so Brent is unchanged)

- **`research_only` (config, default false):** when true, the combined arm is skipped; research cards still go out (cards no longer require `alerts_enabled` in research-only mode).
- **`price_decimals` (config, default 3):** setup rounding and card prices use it (EUR/USD 5, NAS100 1, XAU 2, WTI 3).
- **`display_name` (config, default "BRENT"):** used in card titles ("RESEARCH CALL · LONG GOLD").
- **`eia_family` (config, default true):** the EIA_REACTION family runs only when true (oil only).
- **Not RULE_FIELDS:** the new fields are added to the config but kept out of `RULE_FIELDS` where they do not change Brent's signal rules. The Brent config hash stays the same.

## Per-market settings (server, one TOML each)

| Market | display | max_spread | decimals | range | EIA family | blackouts |
|---|---|---|---|---|---|---|
| XAU_USD | GOLD | 0.60 | 2 | 1000–10000 | no | none |
| WTICO_USD | WTI | 0.08 | 3 | 30–200 | yes | Wed 10:15–10:45 (+ the same shifted dates) |
| EUR_USD | EURUSD | 0.00030 | 5 | 0.8–1.6 | no | none |
| NAS100_USD | NAS100 | 3.0 | 1 | 5000–50000 | no | none |

- **Spread limits:** about 1.5× the typical spreads NORTH recorded (gold 0.42, EUR/USD 0.0002, NAS100 2.3).
- **Other rule fields** (break/zone/stop in ATR units, arm hours, hold hours) are the same as Brent's, because they scale with each market's own ATR.
- **Honest note:** these setups were designed on Brent. Whether they transfer is exactly what the research count measures.

## Running

- **Service:** a systemd template unit `brent-market@<name>.service`, running `python -m tradingagents.brent --config /root/ta-brent/markets/<name>.toml run`.
- **State:** journals in `~/.tradingagents/markets/<name>`.
- **Health:** each instance's own `health` command.
- **Deploy:** the existing pinned tool restarts the named services.
- **NORTH:** the scorecard and phone page add a per-market section (read-only).

## Verdicts

The same go/no-go rule as Brent, **per market and per family**: checkpoint at 30 trades, decision at 50.

## As built (2026-11-12)

- **Files:** `deploy/markets/{gold,wti,eurusd,nas100}.toml` and `deploy/systemd/brent-market@.service` (the configs ship with the code checkout; the unit is copied once to `/etc/systemd/system/`).
- **No news for these markets:** a research-only instance runs no news collector and makes no model call. Its cards say "chart only (no news check for this market)", grade B. Its health check ignores the news collector.
- **No WATCH cards, no INFO lane, no real alerts.** Research cards only, at most 3 per market per New York day.
- **Paper costs:** WTI keeps Brent's financing (45%/yr). Gold, EUR/USD and NAS100 use 8%/yr, an estimate, not OANDA's exact rates.
- **NORTH does not follow them:** NORTH reads only the Brent journal for paper and practice. The market journals reach NORTH only as a read-only scorecard section ("Research desk · GOLD …", NORTH 5.9).
- **Not built in v1:** a separate health timer per market (systemd restarts a crashed instance); per-market dashboard pages; the shared risk book code (design above; nothing to enforce while practice is off for these markets).
