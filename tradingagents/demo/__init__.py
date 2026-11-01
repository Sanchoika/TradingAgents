"""Forward test of TradingAgents ratings on an OANDA *practice* account.

TradingAgents itself only produces a 5-tier rating per ticker and date. This
package turns that rating into a small, stop-protected demo order, journals
every decision, and reports results against a random-direction baseline.

It talks only to OANDA's practice endpoint; there is no switch to a live account.
See docs/DEMO_OANDA.md for setup.
"""
