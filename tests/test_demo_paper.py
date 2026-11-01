"""Paper account and earnings mode of the demo runner. No network, no LLM."""

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from tradingagents.demo.config import DemoConfig
from tradingagents.demo.journal import Journal
from tradingagents.demo.paper import PaperBroker
from tradingagents.demo.runner import fixed_analyzer, run_once

pytestmark = pytest.mark.unit


class Prices:
    """Daily bars a test can extend; flat at 100 with a 2-point range."""

    def __init__(self, days=30):
        self.rows = [{"Date": d, "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0}
                     for d in pd.bdate_range("2026-09-01", periods=days)]

    def add(self, o, h, low, c):
        nxt = self.rows[-1]["Date"] + pd.offsets.BDay()
        self.rows.append({"Date": nxt, "Open": o, "High": h, "Low": low, "Close": c})

    def __call__(self, symbol, as_of):
        return pd.DataFrame(self.rows)

    @property
    def last(self):
        return self.rows[-1]["Date"].date()


def _broker(tmp_path, prices):
    return PaperBroker(tmp_path / "acct.json", 100_000, spread_bps=10, bars=prices,
                       today=lambda: prices.last)


def test_order_fills_at_next_open_with_spread_then_stop_hits(tmp_path):
    p = Prices()
    b = _broker(tmp_path, p)
    b.market_order("AAA", 10, "96.0", "Buy")       # stop 4 below the 100 reference
    assert b.sync() == []                           # no new bar yet: still pending
    assert b.our_open_trades()[0]["status"] == "pending"
    p.add(102, 103, 101, 102)
    fill = b.sync()[0]
    assert fill["price"] == pytest.approx(102 * 1.0005)  # open + half of 10 bps
    p.add(99, 99.5, 97.0, 98)                       # stop at ~98.05 is hit intraday
    closed = b.sync()[0]
    assert closed["reason"] == "stop"
    assert closed["realizedPL"] < 0 and b.our_open_trades() == []
    assert b.nav() == pytest.approx(100_000 + closed["realizedPL"])


def test_gap_through_stop_fills_at_open_and_state_persists(tmp_path):
    p = Prices()
    b = _broker(tmp_path, p)
    b.market_order("AAA", -5, "104.0", "Sell")
    p.add(100, 100.5, 99.5, 100)
    b.sync()
    p.add(110, 111, 109, 110)                       # gaps above the short's stop
    rec = b.sync()[0]
    assert rec["close_price"] == pytest.approx(110 * 1.0005)
    again = _broker(tmp_path, p)
    assert again.our_closed_trades()[0]["id"] == rec["id"]


def test_close_is_queued_and_pending_close_cancels(tmp_path):
    p = Prices()
    b = _broker(tmp_path, p)
    tid = b.market_order("AAA", 10, "90.0", "Buy")["tradeOpened"]["tradeID"]
    b.close_trade(tid)                               # still pending: just cancelled
    assert b.our_open_trades() == [] and b.our_closed_trades() == []
    tid = b.market_order("AAA", 10, "90.0", "Buy")["tradeOpened"]["tradeID"]
    p.add(100, 101, 99, 100)
    b.sync()
    b.close_trade(tid)
    assert b.our_open_trades() == []                 # hidden once closing
    p.add(105, 106, 104, 105)
    rec = b.sync()[0]
    assert rec["reason"] == "rating" and rec["realizedPL"] > 0


def test_watchlist_run_on_paper_account(tmp_path):
    p = Prices()
    b = _broker(tmp_path, p)
    cfg = DemoConfig(instruments=["AAA"], journal_dir=str(tmp_path))
    run_once(b, fixed_analyzer("Buy"), cfg, Journal(tmp_path), str(p.last))
    assert b.our_open_trades("AAA")[0]["status"] == "pending"
    p.add(100, 101, 99, 100)
    run_once(b, fixed_analyzer("Buy"), cfg, Journal(tmp_path), str(p.last))
    t = b.our_open_trades("AAA")
    assert len(t) == 1 and t[0]["status"] == "open"


def test_earnings_mode_rates_only_reporters_and_exits_after_hold_days(tmp_path):
    p = Prices()
    b = _broker(tmp_path, p)
    cfg = DemoConfig(broker="paper", mode="earnings", universe=["AAA", "BBB", "CCC"],
                     hold_days=2, journal_dir=str(tmp_path))
    now = datetime(2026, 10, 13, 11, tzinfo=UTC)
    times = {"AAA": [pd.Timestamp("2026-10-12 20:05", tz="UTC")],     # reported last night
             "BBB": [pd.Timestamp("2026-07-20 20:05", tz="UTC")],     # months ago
             "CCC": [pd.Timestamp("2026-10-20 20:05", tz="UTC")]}     # not yet
    seen = []

    def analyze(ticker, d):
        seen.append(ticker)
        return "Sell", "text"

    j = Journal(tmp_path)
    run_once(b, analyze, cfg, j, str(p.last), earnings=times.get, now=now)
    assert seen == ["AAA"]
    assert float(b.our_open_trades("AAA")[0]["currentUnits"]) < 0
    # The next scan starts where this one ended, so AAA is not rated twice.
    run_once(b, analyze, cfg, j, str(p.last), earnings=times.get, now=now)
    assert seen == ["AAA"]
    p.add(100, 101, 99, 100)
    b.sync()
    for _ in range(3):
        p.add(100, 101, 99, 100)
    run_once(b, analyze, cfg, j, str(p.last), earnings=times.get, now=now)
    assert b.our_open_trades() == []                 # close queued after 2 trading days


def test_earnings_mode_caps_analyses_per_run(tmp_path):
    p = Prices()
    cfg = DemoConfig(mode="earnings", universe=list("ABCDEFG"), max_analyses_per_run=3,
                     journal_dir=str(tmp_path))
    now = datetime(2026, 10, 13, 11, tzinfo=UTC)
    seen = []
    rows = run_once(_broker(tmp_path, p), lambda t, d: (seen.append(t), ("Hold", ""))[1], cfg,
                    Journal(tmp_path), str(p.last),
                    earnings=lambda t: [pd.Timestamp("2026-10-13 10:00", tz="UTC")], now=now)
    assert seen == ["A", "B", "C"]
    scan = next(r for r in rows if r["event"] == "earnings_scan")
    assert scan["skipped_over_limit"] == ["D", "E", "F", "G"]


def test_config_rules():
    assert DemoConfig().instruments == {"QQQ": "QQQ", "SPY": "SPY"}
    assert DemoConfig(broker="oanda").instruments["NAS100_USD"] == "QQQ"
    with pytest.raises(ValueError):
        DemoConfig(mode="earnings")                  # no universe
    with pytest.raises(ValueError):
        DemoConfig(mode="earnings", broker="oanda", universe=["AAPL"])
    assert date  # imported for readability of fixtures


def test_failed_and_skipped_reporters_are_retried_then_dropped(tmp_path):
    p = Prices()
    cfg = DemoConfig(mode="earnings", universe=["A", "B", "C"], max_analyses_per_run=1,
                     retry_days=1, journal_dir=str(tmp_path))
    j, b = Journal(tmp_path), _broker(tmp_path, p)
    now = datetime(2026, 10, 13, 11, tzinfo=UTC)
    seen = []

    def analyze(t, d):
        seen.append(t)
        if t == "A" and seen.count("A") == 1:
            raise RuntimeError("429 rate limit")
        return "Hold", ""

    times = {"A": [pd.Timestamp("2026-10-13 10:00", tz="UTC")],
             "B": [pd.Timestamp("2026-10-13 10:00", tz="UTC")], "C": []}
    run_once(b, analyze, cfg, j, "2026-10-13", earnings=times.get, now=now)
    assert seen == ["A"] and j.state()["retry"] == {"A": "2026-10-13", "B": "2026-10-13"}
    later = datetime(2026, 10, 14, 11, tzinfo=UTC)
    run_once(b, analyze, cfg, j, "2026-10-14", earnings=times.get, now=later)
    assert seen == ["A", "A"] and j.state()["retry"] == {"B": "2026-10-13"}
    run_once(b, analyze, cfg, j, "2026-10-15", earnings=times.get, now=later)
    assert seen == ["A", "A"]                     # B expired after retry_days
    assert j.state()["retry"] == {}


def test_summary_note_scorecard_and_notify(tmp_path):
    from tradingagents.demo.notes import notify, run_summary, write_run_note, write_scorecard

    rows = [{"event": "run_start", "trade_date": "2026-11-02", "nav": 100500.0, "drawdown_pct": -0.5},
            {"event": "earnings_scan", "reported": ["NVDA"], "skipped_over_limit": []},
            {"event": "decision", "instrument": "NVDA", "rating": "Overweight", "actions": ["open"],
             "decision": "Rating: Overweight\nBecause."},
            {"event": "open", "instrument": "NVDA", "units": 38.2, "stop": "191.61"},
            {"event": "error", "instrument": "AMD", "error": "429"}]
    text = run_summary(rows)
    assert "NAV 100,500 (+0.5% since start)" in text and "NVDA: Overweight -> open" in text
    assert "ERROR AMD" in text
    note = write_run_note(rows, tmp_path / "vault", "2026-11-02")
    write_run_note(rows, tmp_path / "vault", "2026-11-02")
    body = note.read_text(encoding="utf-8")
    assert body.count("---\ndate:") == 1 and body.count("## Run at") == 2 and "Because." in body
    card = write_scorecard({"trades": 3, "enough_data": False}, tmp_path / "vault")
    assert "too early" in card.read_text(encoding="utf-8")
    import sys
    out = tmp_path / "msg.txt"
    cmd = f'"{sys.executable}" -c "import sys; open(r\'{out}\', \'w\').write(sys.stdin.read())"'
    assert notify(cmd, "hello") == 0 and out.read_text() == "hello"
