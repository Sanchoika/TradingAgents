"""Forward test of TradingAgents ratings on a paper or OANDA *practice* account.

TradingAgents itself only produces a 5-tier rating per ticker and date. This
package turns that rating into a small, stop-protected demo order, journals
every decision, and reports results against a random-direction baseline.

It runs on a built-in paper account or an OANDA practice account, never a live one.
See docs/DEMO.md for setup.
"""
