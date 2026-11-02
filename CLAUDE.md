# Notes for Claude working on this fork

This is Irakli's fork of TradingAgents. The `tradingagents/demo/` package runs a
paper forward test ("test #22", earnings mode) on his Windows PC every weekday.

Standing rules:
- Paper money only. Never add real-money trading or point the OANDA client at a live URL.
- Never read, print or ask for API keys, tokens or `.env`; Irakli enters them himself.
- The test's rules are frozen from 2026-11-02 (model, universe, hold days, risk, stops,
  caps, spread). A change restarts the 150-trade count: propose it, do not make it.
  Measurement-only changes (reports, dashboard, alerts) are fine.
- No paid LLM runs without Irakli's OK; tests use fixed ratings and fake prices.
- Work on a branch and open a PR into this fork's `main`; Irakli approves merges.
  Never push to TauricResearch/TradingAgents.

Dev: `uv pip install -e .` (the PC venv has no pip), `python -m pytest tests -q`,
`ruff check`. Demo guide: `docs/DEMO.md`; audit: `docs/DEMO-AUDIT.md`.
