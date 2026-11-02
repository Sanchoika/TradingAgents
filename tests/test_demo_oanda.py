"""OANDA practice demo runner: plan rules, runner flow, client payloads, report.

No network and no LLM: the broker and the HTTP session are fakes.
"""

import pytest

from tradingagents.demo import plan
from tradingagents.demo.config import DemoConfig, load_config
from tradingagents.demo.journal import Journal
from tradingagents.demo.oanda import (
    PRACTICE_URL,
    InstrumentSpec,
    OandaError,
    OandaPractice,
    Quote,
    credentials_from_env,
)
from tradingagents.demo.report import summarize
from tradingagents.demo.runner import fixed_analyzer, run_once

pytestmark = pytest.mark.unit

CANDLES = [{"o": 100, "h": 101, "l": 99, "c": 100} for _ in range(20)]  # ATR = 2


class FakeBroker:
    def __init__(self, nav=100_000.0, bid=99.9, ask=100.1, tradeable=True):
        self._nav = nav
        self.q = Quote(bid=bid, ask=ask, tradeable=tradeable, quote_to_home=1.0)
        self.trades = []
        self.orders = []
        self.closed = []
        self._next = 1

    def nav(self):
        return self._nav

    def instrument(self, name):
        return InstrumentSpec(name, display_precision=1, units_precision=0, min_units=1)

    def quote(self, name):
        return self.q

    def daily_candles(self, name, count=40):
        return CANDLES

    def our_open_trades(self, name=None):
        return [t for t in self.trades if name is None or t["instrument"] == name]

    def market_order(self, name, units, stop_price, comment):
        self.orders.append((name, units, stop_price))
        tid = str(self._next)
        self._next += 1
        self.trades.append({"id": tid, "instrument": name, "currentUnits": str(units)})
        return {"price": "100.1", "tradeOpened": {"tradeID": tid}, "halfSpreadCost": "1.0"}

    def close_trade(self, trade_id):
        self.closed.append(trade_id)
        self.trades = [t for t in self.trades if t["id"] != trade_id]
        return {"price": "100", "pl": "-5", "halfSpreadCost": "1.0"}


def _cfg(tmp_path, **kw):
    return DemoConfig(instruments={"NAS100_USD": "QQQ"}, journal_dir=str(tmp_path), **kw)


def _run(broker, rating, tmp_path, **kw):
    cfg = _cfg(tmp_path)
    return run_once(broker, fixed_analyzer(rating), cfg, Journal(cfg.journal_dir), "2026-11-02", **kw)


# -- plan -------------------------------------------------------------------

@pytest.mark.parametrize("rating,expected", [
    ("Buy", 1), ("Overweight", 1), ("Hold", 0), ("Underweight", -1), ("Sell", -1),
    ("REVIEW", None), ("", None), (None, None),
])
def test_target_direction(rating, expected):
    assert plan.target_direction(rating) == expected


@pytest.mark.parametrize("current,target,expected", [
    (0, 1, ["open"]), (1, 1, ["keep"]), (1, -1, ["close", "open"]), (1, 0, ["close"]),
    (0, 0, ["keep"]), (-1, None, ["skip"]),
])
def test_actions(current, target, expected):
    assert plan.actions(current, target) == expected


def test_atr_and_sizing():
    assert plan.atr(CANDLES, 14) == pytest.approx(2.0)
    with pytest.raises(ValueError):
        plan.atr(CANDLES[:5], 14)
    # 0.5% of 100k = 500 at risk, stop 4 points away, 1 USD per point -> 125 units.
    assert plan.position_units(100_000, 0.5, 4.0, 1.0) == 125
    assert plan.position_units(100_000, 0.5, 4.0, 1.0, max_units=10) == 10
    assert plan.position_units(1000, 0.5, 3.0, 1.0, units_precision=1) == pytest.approx(1.6)
    assert plan.stop_price(1, 100.1, 4.0, 1) == "96.1"
    assert plan.stop_price(-1, 99.9, 4.0, 1) == "103.9"


# -- runner -----------------------------------------------------------------

def test_buy_opens_long_with_stop_then_keeps(tmp_path):
    b = FakeBroker()
    _run(b, "Buy", tmp_path)
    assert b.orders == [("NAS100_USD", 125.0, "96.1")]
    _run(b, "Overweight", tmp_path)
    assert len(b.orders) == 1 and not b.closed


def test_reversal_closes_then_opens_short(tmp_path):
    b = FakeBroker()
    _run(b, "Buy", tmp_path)
    _run(b, "Sell", tmp_path)
    assert b.closed == ["1"]
    assert b.orders[-1] == ("NAS100_USD", -125.0, "103.9")


def test_hold_closes_and_review_does_nothing(tmp_path):
    b = FakeBroker()
    _run(b, "Buy", tmp_path)
    _run(b, "REVIEW", tmp_path)
    assert not b.closed
    _run(b, "Hold", tmp_path)
    assert b.closed == ["1"] and b.trades == []


def test_dry_run_sends_nothing(tmp_path):
    b = FakeBroker()
    rows = _run(b, "Buy", tmp_path, dry_run=True)
    assert b.orders == []
    assert any(r["event"] == "open_dry_run" for r in rows)
    assert "start_nav" not in Journal(tmp_path).state()


def test_drawdown_halt_blocks_new_trades_but_still_closes(tmp_path):
    b = FakeBroker()
    _run(b, "Buy", tmp_path)
    b._nav = 89_000  # 11% below the 100k start
    rows = _run(b, "Sell", tmp_path)
    assert b.closed == ["1"] and len(b.orders) == 1
    assert rows[0]["halted"] is True


def test_wide_spread_or_closed_market_skips(tmp_path):
    b = FakeBroker(bid=99.0, ask=101.0)  # spread 2 > 0.1 * ATR
    _run(b, "Buy", tmp_path)
    assert b.orders == []
    b = FakeBroker(tradeable=False)
    _run(b, "Buy", tmp_path / "x")
    assert b.orders == []


def test_analyzer_error_is_journaled_and_others_continue(tmp_path):
    b = FakeBroker()
    cfg = DemoConfig(instruments={"NAS100_USD": "QQQ", "SPX500_USD": "SPY"}, journal_dir=str(tmp_path))

    def analyze(ticker, date):
        if ticker == "QQQ":
            raise RuntimeError("LLM down")
        return "Buy", "text"

    rows = run_once(b, analyze, cfg, Journal(tmp_path), "2026-11-02")
    assert [r["instrument"] for r in rows if r["event"] == "error"] == ["NAS100_USD"]
    assert [o[0] for o in b.orders] == ["SPX500_USD"]
    events = [e["event"] for e in Journal(tmp_path).events()]
    assert events.count("error") == 1 and "open" in events


# -- client -----------------------------------------------------------------

class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.headers, self.calls, self.responses = {}, [], list(responses)

    def request(self, method, url, timeout=None, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


def test_client_uses_practice_url_and_tags_orders():
    s = FakeSession([FakeResp(201, {"orderFillTransaction": {"price": "1", "tradeOpened": {"tradeID": "9"}}})])
    c = OandaPractice("tok", "101-001", session=s)
    fill = c.market_order("NAS100_USD", -3, "20000.0", comment="Sell")
    method, url, kw = s.calls[0]
    assert url == f"{PRACTICE_URL}/accounts/101-001/orders" and "fxpractice" in url
    order = kw["json"]["order"]
    assert order["units"] == "-3" and order["stopLossOnFill"]["price"] == "20000.0"
    assert order["tradeClientExtensions"]["tag"] == "tradingagents"
    assert fill["tradeOpened"]["tradeID"] == "9"
    assert s.headers["Authorization"] == "Bearer tok"


def test_client_raises_on_cancel_and_http_error():
    s = FakeSession([FakeResp(201, {"orderCancelTransaction": {"reason": "MARKET_HALTED"}}),
                     FakeResp(401, {"errorMessage": "Insufficient authorization"})])
    c = OandaPractice("tok", "a", session=s)
    with pytest.raises(OandaError, match="MARKET_HALTED"):
        c.market_order("NAS100_USD", 1, "1.0", "Buy")
    with pytest.raises(OandaError, match="401"):
        c.nav()


def test_client_only_sees_its_own_trades_and_converts_quote():
    trades = {"trades": [
        {"id": "1", "instrument": "NAS100_USD", "currentUnits": "2", "clientExtensions": {"tag": "tradingagents"}},
        {"id": "2", "instrument": "NAS100_USD", "currentUnits": "5"},
    ]}
    price = {"prices": [{"bids": [{"price": "99"}], "asks": [{"price": "101"}], "tradeable": True}],
             "homeConversions": [{"currency": "USD", "accountGain": "1", "positionValue": "1"}]}
    c = OandaPractice("tok", "a", session=FakeSession([FakeResp(200, trades), FakeResp(200, price)]))
    assert [t["id"] for t in c.our_open_trades("NAS100_USD")] == ["1"]
    q = c.quote("NAS100_USD")
    assert (q.mid, q.spread, q.quote_to_home) == (100.0, 2.0, 1.0)


def test_credentials_must_be_present():
    with pytest.raises(OandaError, match="OANDA_API_KEY"):
        credentials_from_env({"OANDA_ACCOUNT_ID": "x"})
    assert credentials_from_env({"OANDA_API_KEY": " t ", "OANDA_ACCOUNT_ID": "a"}) == ("t", "a")


# -- config and report ------------------------------------------------------

def test_config_rejects_unknown_keys_and_big_risk(tmp_path):
    p = tmp_path / "d.toml"
    p.write_text("risk_pc = 1\n")
    with pytest.raises(ValueError, match="risk_pc"):
        load_config(p)
    with pytest.raises(ValueError):
        DemoConfig(risk_pct=5)


def test_example_config_loads():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "demo.example.toml")
    assert cfg.broker == "paper" and cfg.instruments["BTC-USD"] == "BTC-USD"
    assert cfg.llm["llm_provider"] == "google"
    earn = load_config(root / "demo.earnings.example.toml")
    assert earn.mode == "earnings" and len(earn.universe) == 50
    watch = load_config(root / "demo.watchlist.example.toml")
    assert watch.mode == "watchlist" and "~" not in watch.journal_dir
    assert watch.journal_dir != earn.journal_dir and watch.llm["llm_provider"] == "chatgpt"


def test_report_baseline_and_ci():
    assert summarize([], {})["trades"] == 0
    # Every trade won 10 after costs: a coin-flip direction rarely does as well.
    trades = [{"id": str(i), "realizedPL": "10", "financing": "0"} for i in range(30)]
    r = summarize(trades, {str(i): 1.0 for i in range(30)})
    assert r["total_pnl"] == 300 and r["win_rate"] == 1.0
    assert r["mean_pnl_ci95"] == [10.0, 10.0]
    assert r["p_random_as_good"] < 0.01 and r["enough_data"] is False


@pytest.mark.parametrize("mild,expected_units", [(0.5, 62.0), (1.0, 125.0), (0.0, None)])
def test_mild_ratings_trade_a_share_of_the_risk(tmp_path, mild, expected_units):
    b = FakeBroker()
    cfg = DemoConfig(instruments={"NAS100_USD": "QQQ"}, journal_dir=str(tmp_path), mild_size=mild)
    run_once(b, fixed_analyzer("Underweight"), cfg, Journal(tmp_path), "2026-11-02")
    if expected_units is None:
        assert b.orders == []
    else:
        assert b.orders == [("NAS100_USD", -expected_units, "103.9")]
    # Buy/Sell always use the full budget.
    assert plan.size_factor("Sell", mild) == 1.0
