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


def test_missed_runs_and_benchmark_in_summary(tmp_path):
    from tradingagents.demo.notes import run_summary

    p = Prices()
    b = _broker(tmp_path, p)
    cfg = DemoConfig(instruments=["AAA"], journal_dir=str(tmp_path))
    j = Journal(tmp_path)
    run_once(b, fixed_analyzer("Hold"), cfg, j, "2026-10-12")     # Monday
    p.add(110, 111, 109, 110)                                      # SPY (same fake prices) +10%
    rows = run_once(b, fixed_analyzer("Hold"), cfg, j, "2026-10-15")  # Thursday: Tue, Wed missed
    start = rows[0] if rows[0]["event"] == "run_start" else next(r for r in rows if r["event"] == "run_start")
    assert start["missed_runs"] == 2 and start["benchmark_pct"] == pytest.approx(10.0)
    text = run_summary(rows)
    assert "SPY +10.0%" in text and "2 scheduled weekday run(s) missed" in text


def test_crashed_run_sends_failure_alert(tmp_path, monkeypatch):
    from tradingagents.demo import __main__ as cli

    sent = []
    monkeypatch.setattr("tradingagents.demo.notes.notify", lambda cmd, text: sent.append(text) or 0)

    def boom(cfg):
        raise RuntimeError("Yahoo down")

    monkeypatch.setattr(cli, "_broker", boom)
    cfgfile = tmp_path / "d.toml"
    cfgfile.write_text(f'notify_command = "x"\njournal_dir = "{tmp_path.as_posix()}"\n')
    with pytest.raises(RuntimeError):
        cli.main(["--config", str(cfgfile), "run", "--fixed-rating", "Hold"])
    assert sent == ["TradingAgents demo run FAILED: RuntimeError: Yahoo down"]


def test_position_cap_skips_new_trades(tmp_path):
    p = Prices()
    b = _broker(tmp_path, p)
    cfg = DemoConfig(instruments=["AAA", "BBB", "CCC"], max_open_positions=2, journal_dir=str(tmp_path))
    rows = run_once(b, fixed_analyzer("Buy"), cfg, Journal(tmp_path), str(p.last))
    assert len(b.our_open_trades()) == 2
    assert any(r["event"] == "skip" and "2 positions already open" in r["reason"] for r in rows)


def test_short_pays_borrow_fee_longs_do_not(tmp_path):
    p = Prices()
    b = PaperBroker(tmp_path / "a.json", 100_000, spread_bps=0, bars=p, today=lambda: p.last,
                    borrow_bps_per_year=3650)          # 36.5%/yr = 0.1% per day, easy to check
    b.market_order("AAA", -10, "104.0", "Sell")
    b.market_order("BBB", 10, "96.0", "Buy")
    p.add(100, 101, 99, 100)
    b.sync()
    for _ in range(9):
        p.add(100, 101, 99, 100)
    b.sync()
    for t in b.our_open_trades():
        b.close_trade(t["id"])
    p.add(100, 101, 99, 100)
    recs = {r["instrument"]: r for r in b.sync()}
    days = (date.fromisoformat(recs["AAA"]["closed"]) - date.fromisoformat(recs["AAA"]["opened"])).days
    assert recs["AAA"]["financing"] == pytest.approx(-(10 * 100 * 0.001 * days), abs=0.01)
    assert recs["BBB"]["financing"] == 0
    assert b.nav() == pytest.approx(100_000 + recs["AAA"]["financing"], abs=0.01)


def test_report_splits_long_and_short():
    from tradingagents.demo.report import summarize

    trades = ([{"id": str(i), "units": 5, "realizedPL": 10, "financing": 0} for i in range(4)]
              + [{"id": str(i + 9), "units": -5, "realizedPL": -4, "financing": -1} for i in range(2)])
    r = summarize(trades, {}, n_random=200)
    assert r["by_side"]["long"] == {"trades": 4, "mean_pnl": 10.0, "win_rate": 1.0}
    assert r["by_side"]["short"] == {"trades": 2, "mean_pnl": -5.0, "win_rate": 0.0}


def test_earnings_session_hint_report_path_and_weekly_text(tmp_path):
    from tradingagents.demo.notes import SIGN_IN_HINT, failure_text, run_summary, weekly_text
    from tradingagents.demo.runner import _session

    assert _session(pd.Timestamp("2026-10-13 11:00", tz="UTC")) == "before open"      # 07:00 ET
    assert _session(pd.Timestamp("2026-10-13 20:30", tz="UTC")) == "after close"      # 16:30 ET
    assert _session(pd.Timestamp("2026-10-13 15:00", tz="UTC")) == "during session"   # 11:00 ET

    p = Prices()
    cfg = DemoConfig(mode="earnings", universe=["A"], journal_dir=str(tmp_path))
    analyze = lambda t, d: ("Hold", "x")  # noqa: E731
    analyze.reports = {"A": "C:/reports/2026-10-13_A"}
    rows = run_once(_broker(tmp_path, p), analyze, cfg, Journal(tmp_path), "2026-10-13",
                    earnings=lambda t: [pd.Timestamp("2026-10-13 20:30", tz="UTC")],
                    now=datetime(2026, 10, 13, 21, tzinfo=UTC))
    scan = next(r for r in rows if r["event"] == "earnings_scan")
    assert scan["report_times"]["A"].endswith("(after close)")
    assert next(r for r in rows if r["event"] == "decision")["report"] == "C:/reports/2026-10-13_A"

    err = [{"event": "run_start", "trade_date": "2026-10-13", "nav": 1}, {"event": "error", "instrument": "A",
           "error": "ChatGPTAuthError: sign-in ended"}]
    assert SIGN_IN_HINT in run_summary(err)
    assert SIGN_IN_HINT in failure_text(RuntimeError("login chatgpt first"))
    assert "no closed trades" in weekly_text({"trades": 0})
    text = weekly_text({"trades": 10, "total_pnl": 120.0, "win_rate": 0.6, "mean_pnl": 12.0,
                        "mean_pnl_ci95": [-3.0, 27.0], "p_random_as_good": 0.2,
                        "by_side": {"long": {"trades": 6, "mean_pnl": 20.0, "win_rate": 0.7}}})
    assert "10/150" in text and "random as good 20%" in text and "long 6" in text


def test_watchdog_finds_missing_weekday_runs():
    from tradingagents.demo.notes import missing_run

    ran = [{"event": "run_start", "trade_date": "2026-11-02", "dry_run": False}]
    dry = [{"event": "run_start", "trade_date": "2026-11-03", "dry_run": True}]
    tue_9am = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)         # 09:30 ET Tuesday
    tue_8am = datetime(2026, 11, 3, 13, 0, tzinfo=UTC)          # 08:00 ET, inside the grace hour
    sun = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)
    assert missing_run(ran, tue_9am) == "2026-11-03"            # Tuesday's run never happened
    assert missing_run(ran, tue_8am) is None                    # still waiting on Tuesday: Monday is done
    assert missing_run(ran + dry, tue_9am) == "2026-11-03"      # a dry run does not count
    assert missing_run(ran + [{"event": "run_start", "trade_date": "2026-11-03"}], tue_9am) is None
    assert missing_run(ran, sun) == "2026-11-06"                # weekend: the last weekday is Friday
    assert missing_run([], tue_9am) == "2026-11-03"


def test_edgar_second_source_and_summary_flag(tmp_path):
    from tradingagents.demo.notes import run_summary

    p = Prices()
    cfg = DemoConfig(mode="earnings", universe=["A", "B", "C"], journal_dir=str(tmp_path))
    yahoo = pd.Timestamp("2026-10-13 20:30", tz="UTC")
    edgar = {"A": [pd.Timestamp("2026-10-13 20:35", tz="UTC")],        # same evening: confirmed
             "B": [pd.Timestamp("2026-07-20 20:00", tz="UTC")],        # months apart: not confirmed
             "C": []}                                                  # not a US filer
    rows = run_once(_broker(tmp_path, p), lambda t, d: ("Hold", ""), cfg, Journal(tmp_path), "2026-10-13",
                    earnings=lambda t: [yahoo], now=datetime(2026, 10, 13, 21, tzinfo=UTC), edgar=edgar.get)
    scan = next(r for r in rows if r["event"] == "earnings_scan")
    assert scan["edgar_check"] == {"A": "confirmed by EDGAR", "B": "NOT confirmed by EDGAR",
                                   "C": "not an EDGAR filer"}
    assert "CHECK: earnings date not confirmed by SEC EDGAR for B" in run_summary(rows)


def test_edgar_release_times_reads_item_202_8ks(monkeypatch):
    from tradingagents.dataflows.vendors import sec_earnings

    monkeypatch.setattr(sec_earnings, "cik_for", lambda t: "0000000001" if t == "AAA" else None)
    monkeypatch.setattr(sec_earnings, "_fetch_json", lambda url: {"filings": {"recent": {
        "form": ["8-K", "10-Q", "8-K", "8-K"],
        "items": ["2.02,9.01", "", "5.02", "2.02"],
        "acceptanceDateTime": ["2026-10-20T20:05:00.000Z", "2026-10-21T10:00:00.000Z",
                               "2026-10-22T10:00:00.000Z", "2026-07-21T20:10:00.000Z"]}}})
    times = sec_earnings.earnings_release_times("AAA")
    assert [t.isoformat()[:16] for t in times] == ["2026-10-20T20:05", "2026-07-21T20:10"]
    assert sec_earnings.earnings_release_times("ZZZ") == []


def test_excess_return_over_sector_fund_in_trade_direction():
    from tradingagents.demo.sectors import excess_vs_sector

    def bars(sym, as_of):
        days = pd.bdate_range("2026-10-01", periods=10)
        opens = [100.0 + i for i in range(10)]                       # sector fund rises 1 a day
        return pd.DataFrame({"Date": days, "Open": opens})

    long_win = {"instrument": "NVDA", "units": 5, "price": 100.0, "close_price": 110.0,
                "opened": "2026-10-02", "closed": "2026-10-08"}      # stock +10%, fund +~5.9%
    short_loss = {"instrument": "NVDA", "units": -5, "price": 100.0, "close_price": 110.0,
                  "opened": "2026-10-02", "closed": "2026-10-08"}    # short loses 10%, fund fell for the short
    unknown = {"instrument": "ZZZZ", "units": 1, "price": 1.0, "close_price": 1.0,
               "opened": "2026-10-02", "closed": "2026-10-08"}
    r = excess_vs_sector([long_win, short_loss, unknown], bars)
    assert r["trades"] == 2 and r["skipped"] == 1
    fund = 100 * (107 / 101 - 1)
    assert r["mean_excess_pct"] == pytest.approx(((10 - fund) + (-10 + fund)) / 2, abs=0.01)
    assert r["share_beating_sector"] == 0.5


def test_net_exposure_cap_and_no_short_list(tmp_path):
    p = Prices()
    b = _broker(tmp_path, p)
    cfg = DemoConfig(instruments=["A", "B", "C", "D"], max_net_positions=2, no_short=["Z"],
                     journal_dir=str(tmp_path))
    rows = run_once(b, fixed_analyzer("Buy"), cfg, Journal(tmp_path), str(p.last))
    assert len(b.our_open_trades()) == 2                      # third and fourth long would make net +3
    assert sum("net exposure already +2" in r.get("reason", "") for r in rows) == 2
    short_cfg = DemoConfig(instruments=["Z", "A"], no_short=["Z"], journal_dir=str(tmp_path / "s"))
    b2 = _broker(tmp_path / "s", p)
    rows = run_once(b2, fixed_analyzer("Sell"), short_cfg, Journal(tmp_path / "s"), str(p.last))
    assert [t["instrument"] for t in b2.our_open_trades()] == ["A"]
    assert any(r.get("reason") == "on the no-short list" for r in rows)
    # An opposite-side trade is still allowed when the book is already net long.
    cfg2 = DemoConfig(instruments=["A", "B"], max_net_positions=2, journal_dir=str(tmp_path / "m"))
    b3 = _broker(tmp_path / "m", p)
    run_once(b3, fixed_analyzer("Buy"), cfg2, Journal(tmp_path / "m"), str(p.last))
    run_once(b3, fixed_analyzer("Sell"), DemoConfig(instruments=["C"], max_net_positions=2,
             journal_dir=str(tmp_path / "m")), Journal(tmp_path / "m"), str(p.last))
    assert len(b3.our_open_trades()) == 3


def test_score_ratings_counts_every_rating_and_skips_dry_runs():
    import pandas as pd

    from tradingagents.demo.report import rating_decisions, score_ratings

    events = [
        {"event": "run_start", "dry_run": True},
        {"event": "decision", "instrument": "AAA", "ticker": "AAA", "trade_date": "2026-01-02", "rating": "Sell"},
        {"event": "run_start", "dry_run": False},
        {"event": "decision", "instrument": "AAA", "ticker": "AAA", "trade_date": "2026-01-02", "rating": "Buy"},
        {"event": "decision", "instrument": "BBB", "ticker": "BBB", "trade_date": "2026-01-02", "rating": "Hold"},
        {"event": "decision", "instrument": "CCC", "ticker": "CCC", "trade_date": "2026-01-02", "rating": "REVIEW"},
        {"event": "decision", "instrument": "DDD", "ticker": "DDD", "trade_date": "2026-01-12", "rating": "Sell"},
    ]
    decisions = rating_decisions(events)
    assert [d["rating"] for d in decisions] == ["Buy", "Hold", "Sell"]

    days = pd.bdate_range("2026-01-02", periods=10)
    def bars(symbol, as_of):  # opens rise 1 per day from 100
        return pd.DataFrame({"Date": days, "Open": [100.0 + i for i in range(10)]})

    out = score_ratings(decisions, bars, hold_days=5, today="2026-01-20")
    # entry = open on 2026-01-05 (101), exit 5 bars later (106)
    assert out["by_rating"]["Buy"] == {"n": 1, "mean_move_pct": round(500 / 101, 2)}
    assert out["by_rating"]["Hold"]["n"] == 1
    assert out["pending"] == 1  # DDD's window has not finished
    assert out["directional"]["hit_rate"] == 1.0


# ---- stage B correctness fixes -------------------------------------------------

def test_exit_queued_trade_stays_exposed_and_stop_protected(tmp_path):
    p = Prices()
    b = _broker(tmp_path, p)
    tid = b.market_order("AAA", 10, "96.0", "Buy")["tradeOpened"]["tradeID"]
    p.add(100, 101, 99, 100)
    b.sync()                                          # filled
    b.close_trade(tid)                                # exit queued for the next open
    assert b.our_open_trades() == [] and len(b.exposure_trades()) == 1
    p.add(99, 99.5, 90.0, 95)                         # next bar crashes through the stop after an open above it
    ev = b.sync()
    # the exit fills at that bar's open, which is before the crash: a rating exit, not a stop
    assert ev[0]["reason"] == "rating" and b.exposure_trades() == []


def test_stop_hit_before_the_exit_bar_wins(tmp_path):
    p = Prices()
    b = _broker(tmp_path, p)
    tid = b.market_order("AAA", 10, "96.0", "Buy")["tradeOpened"]["tradeID"]
    p.add(100, 101, 99, 100)
    b.sync()
    p.add(99, 99.5, 90.0, 95)                         # stop hit; sync not run yet
    b.close_trade(tid)                                # exit queued for a day after the stop
    p.add(94, 95, 93, 94)
    ev = b.sync()
    assert len(ev) == 1 and ev[0]["reason"] == "stop"


def test_caps_count_exit_queued_positions(tmp_path):
    p = Prices()
    b = _broker(tmp_path, p)
    cfg = DemoConfig(mode="earnings", universe=["AAA", "BBB"], max_open_positions=1, journal_dir=str(tmp_path))
    j = Journal(tmp_path)
    tid = b.market_order("AAA", 10, "96.0", "Buy")["tradeOpened"]["tradeID"]
    p.add(100, 101, 99, 100)
    b.sync()
    b.close_trade(tid)
    from tradingagents.demo.runner import _open
    row = _open(b, cfg, j, "BBB", "Buy", 1, 100_000, False)
    assert row["event"] == "skip" and "already open" in row["reason"]


def test_report_does_not_settle_the_account(tmp_path, monkeypatch):
    from tradingagents.demo import __main__ as m

    called = []
    monkeypatch.setattr(PaperBroker, "sync", lambda self: called.append(1) or [])
    cfg = DemoConfig(journal_dir=str(tmp_path))
    monkeypatch.setattr(m, "_broker", lambda c: PaperBroker(tmp_path / "a.json", bars=Prices(), today=lambda: date(2026, 10, 1)))
    m.cmd_report(None, cfg)
    assert called == []


def test_sector_comparison_fetches_the_latest_needed_history():
    from tradingagents.demo.sectors import excess_vs_sector

    seen = []

    def bars(sym, as_of):
        seen.append(as_of)
        n = 5 if as_of < "2026-10-20" else 30
        return pd.DataFrame({"Date": pd.bdate_range("2026-10-01", periods=n), "Open": [100.0 + i for i in range(n)]})

    closed = [{"instrument": "AAPL", "units": 1, "price": 100, "close_price": 101,
               "opened": "2026-10-02", "closed": "2026-10-05"},
              {"instrument": "MSFT", "units": 1, "price": 100, "close_price": 101,
               "opened": "2026-10-12", "closed": "2026-10-23"}]
    out = excess_vs_sector(closed, bars)
    assert out["trades"] == 2 and out["skipped"] == 0 and seen == ["2026-10-23"]


def test_crash_after_run_start_is_not_a_completed_run(tmp_path):
    from tradingagents.demo.notes import missing_run

    now = datetime(2026, 11, 3, 14, tzinfo=UTC)
    started = [{"event": "run_start", "trade_date": "2026-11-02", "dry_run": False},
               {"event": "run_end", "trade_date": "2026-10-30", "dry_run": False}]
    assert missing_run(started, now) == "2026-11-03"
    done = started + [{"event": "run_end", "trade_date": "2026-11-03", "dry_run": False}]
    assert missing_run(done, now) is None


def test_run_once_writes_run_end_and_a_failed_lookup_is_retried(tmp_path):
    p = Prices()
    b = _broker(tmp_path, p)
    cfg = DemoConfig(mode="earnings", universe=["AAA"], journal_dir=str(tmp_path))
    j = Journal(tmp_path)
    now = datetime(2026, 11, 3, 13, tzinfo=UTC)
    calls = {"n": 0}

    def flaky(t):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("rate limited")
        return [pd.Timestamp("2026-11-02T21:00:00Z")]

    rows = run_once(b, fixed_analyzer("Buy"), cfg, j, "2026-11-03", earnings=flaky, now=now,
                    edgar=lambda t: [])
    assert rows[-1]["event"] == "run_end"
    assert "AAA" in j.state()["lookup_retry"]
    # next day the watermark has moved past the report, but the failed symbol keeps its old window
    rows = run_once(b, fixed_analyzer("Buy"), cfg, j, "2026-11-04", earnings=flaky,
                    now=datetime(2026, 11, 4, 13, tzinfo=UTC), edgar=lambda t: [])
    scan = next(r for r in rows if r["event"] == "earnings_scan")
    assert scan["reported"] == ["AAA"]


def test_run_lock_is_single_writer(tmp_path):
    from tradingagents.demo.lock import RunLock, RunLocked

    with RunLock(tmp_path / "run.lock"), pytest.raises(RunLocked):
        RunLock(tmp_path / "run.lock").__enter__()
    with RunLock(tmp_path / "run.lock"):   # released
        pass
    # a leftover file from a crashed owner (no live lock) does not block
    (tmp_path / "run.lock").write_text("999999999")
    with RunLock(tmp_path / "run.lock"):
        pass


def test_summary_reports_day_clustered_ci():
    from tradingagents.demo.report import summarize

    trades = [{"id": str(i), "units": 1, "realizedPL": 10.0 * (i % 3 - 1), "financing": 0,
               "opened": f"2026-11-0{1 + i % 4}"} for i in range(12)]
    r = summarize(trades, {}, n_random=200)
    assert r["entry_days"] == 4 and len(r["mean_pnl_ci95_by_day"]) == 2 and "note" in r


# ---- decision v2 (shadow) ------------------------------------------------------

def test_v2_trim_existing_holding_is_not_a_new_short():
    from tradingagents.demo.decision_v2 import interpret

    txt = ("Rating: Underweight. We recommend trimming the existing allocation over the coming months; "
           "this is not a call for mechanical selling at a low existing position.")
    r = interpret("Underweight", -1, txt)
    assert r["decision"] == "ABSTAIN" and "trim_existing_only" in r["flags"] and not r["agrees_with_v1"]


def test_v2_conditional_entry_and_horizon_mismatch_and_missing_earnings():
    from tradingagents.demo.decision_v2 import horizon_days, interpret

    assert horizon_days("a 6-12 month thesis") == 360 and horizon_days("hold for 5 days") == 5
    cond = interpret("Overweight", 1, "Buy only if the stock pulls back to 150 after the print.")
    assert cond["decision"] == "ABSTAIN" and "conditional_entry" in cond["flags"]
    long_h = interpret("Buy", 1, "Initiate a position; our thesis plays out over 6-12 months.")
    assert long_h["decision"] == "ABSTAIN" and "horizon_mismatch" in long_h["flags"]
    miss = interpret("Buy", 1, "The earnings release could not be verified from available sources.")
    assert miss["decision"] == "ABSTAIN" and "earnings_unverified" in miss["flags"]


def test_v2_clean_report_matches_v1_and_hold_is_abstain():
    from tradingagents.demo.decision_v2 import interpret

    ok = interpret("Buy", 1, "Strong beat and raised guidance. Buy for the next 5 trading days.")
    assert ok["decision"] == "LONG" and ok["agrees_with_v1"] and ok["flags"] == []
    sh = interpret("Sell", -1, "Guidance cut and weak margins; short for the coming week.")
    assert sh["decision"] == "SHORT" and sh["agrees_with_v1"]
    hold = interpret("Hold", 0, "Nothing to do.")
    assert hold["decision"] == "ABSTAIN" and hold["flags"] == ["no_trade_rating"] and hold["agrees_with_v1"]


def test_v2_is_journalled_without_changing_orders(tmp_path):
    p = Prices()
    b = _broker(tmp_path, p)
    cfg = DemoConfig(mode="earnings", universe=["AAA"], journal_dir=str(tmp_path))
    j = Journal(tmp_path)
    analyzer = lambda t, d: ("Underweight", "Trim the existing holding over 6 months.")  # noqa: E731
    rows = run_once(b, analyzer, cfg, j, "2026-11-03",
                    earnings=lambda t: [pd.Timestamp("2026-11-02T21:00:00Z")],
                    now=datetime(2026, 11, 3, 13, tzinfo=UTC), edgar=lambda t: [])
    v2 = [r for r in rows if r["event"] == "decision_v2"]
    assert len(v2) == 1 and v2[0]["decision"] == "ABSTAIN"
    assert any(r["event"] == "open" for r in rows)                  # the frozen test still trades
    from tradingagents.demo.decision_v2 import compare
    assert compare(j.events())["v2_abstained_where_v1_traded"] == 1


def test_decision_v2_crash_does_not_skip_order(tmp_path, monkeypatch):
    from tradingagents.demo import decision_v2
    monkeypatch.setattr(decision_v2, "interpret", lambda *a, **k: 1 / 0)
    p = Prices()
    b = _broker(tmp_path, p)
    cfg = DemoConfig(mode="earnings", universe=["AAA"], journal_dir=str(tmp_path))
    rows = run_once(b, lambda t, d: ("Buy", "Strong beat; long for the week."), cfg, Journal(tmp_path),
                    "2026-11-03", earnings=lambda t: [pd.Timestamp("2026-11-02T21:00:00Z")],
                    now=datetime(2026, 11, 3, 13, tzinfo=UTC), edgar=lambda t: [])
    assert any(r["event"] == "decision_v2_error" for r in rows)
    assert any(r["event"] == "open" for r in rows)


def test_hold_is_exactly_five_sessions_and_counts_holidays():
    from tradingagents.demo import sessions
    # filled at Tue 2026-11-03 open: sessions 3,4,5,6,9; the exit order is due on the Mon 11-09 run
    # and fills at the Tue 11-10 open, i.e. five full sessions held
    assert not sessions.exit_due("2026-11-03", "2026-11-06", 5)
    assert sessions.exit_due("2026-11-03", "2026-11-09", 5)
    assert sessions.planned_exit_run("2026-11-03", 5) == "2026-11-09"
    # Thanksgiving 2026-11-26 is not a session: fill Mon 11-23 -> sessions 23,24,25,27,30
    assert sessions.planned_exit_run("2026-11-23", 5) == "2026-11-30"
    assert not sessions.exit_due("2026-11-23", "2026-11-27", 5)
    assert sessions.exit_due("2026-11-23", "2026-11-30", 5)
    assert sessions.covered("2026-11-03") and not sessions.covered("2032-01-02")
