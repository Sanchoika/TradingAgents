"""Brent alerts-only module: rules, validity, alert discipline, paper tracking, restart.

No network, no LLM, no orders: the OANDA client, clock and Telegram sender are fakes.
All messages produced here are test data.
"""

from datetime import UTC, datetime, timedelta

import pytest

from tradingagents.brent import rules
from tradingagents.brent.client import Quote, ReadOnlyOanda, parse_time
from tradingagents.brent.config import BrentConfig, load_config
from tradingagents.brent.engine import Monitor
from tradingagents.brent.report import status_lines, summarize, write_page
from tradingagents.brent.store import AlreadyRunning, Store

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 3, 14, 0, tzinfo=UTC)  # Tuesday 09:00 New York


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


def daily(n=30, step=0.3, last_close=109.0):
    """Rising closes (daily regime 'up'), each candle 2.0 high-low; last high 110, low 108."""
    start = T0 - timedelta(days=n)
    out = []
    for i in range(n):
        c = last_close - step * (n - 1 - i)
        out.append({"time": start + timedelta(days=i), "complete": True, "o": c, "h": c + 1,
                    "l": c - 1, "c": c, "bid_o": c, "ask_o": c, "bid_c": c, "ask_c": c})
    return out


def m15(close, high=None, low=None, end=T0):
    start = end - timedelta(minutes=15)
    high = high if high is not None else max(close, 109.9)
    low = low if low is not None else min(close, 109.8)
    return [{"time": start, "complete": True, "o": 109.9, "h": high, "l": low, "c": close}]


class FakeClient:
    """Scripted read-only OANDA client."""

    def __init__(self, clock):
        self.clock = clock
        self.bid, self.ask, self.age, self.tradeable = 110.17, 110.20, 0.5, True
        self.daily = daily()
        self.m15 = m15(110.5)
        self.m1 = []
        self.h1 = []
        self.trades = []
        self.hedging = False
        self.fail_quotes = False

    def quote(self, name):
        if self.fail_quotes:
            raise RuntimeError("network down")
        now = self.clock()
        return Quote(bid=self.bid, ask=self.ask, time=now - timedelta(seconds=self.age),
                     fetched_at=now, tradeable=self.tradeable, quote_to_home=1.40, rtt=0.1)

    def candles(self, name, gran, count):
        return {"D": self.daily, "M15": self.m15, "M1": self.m1, "H1": self.h1}[gran]

    def summary(self):
        return {"currency": "CAD", "NAV": "100000", "hedgingEnabled": self.hedging}

    def open_trades(self, name):
        return self.trades


class Sender:
    def __init__(self, clock, seconds=2.0, ok=True):
        self.clock, self.seconds, self.ok, self.texts = clock, seconds, ok, []

    def __call__(self, text):
        self.clock.advance(self.seconds)
        self.texts.append(text)
        return self.ok, self.seconds


NO_NEWS = {"state": "none_material", "status": "checked",
           "health": {"fresh_feeds": ["google:brent", "google:opec"], "general_news_fresh": 2,
                      "fresh_check": True},
           "last_poll": "2026-11-03T13:58:00+00:00", "items": [], "support": {}, "conflict_window": {}}


class NewsFake:
    def __init__(self, view=None):
        self.view = dict(view or NO_NEWS)

    def __call__(self, now):
        return self.view


def make(tmp_path, news_view=None, **cfg_kw):
    clock = Clock()
    cfg_kw.setdefault("alerts_enabled", True)
    cfg = BrentConfig(journal_dir=str(tmp_path / "brent"), **cfg_kw)
    client, sender = FakeClient(clock), Sender(clock)
    store = Store(cfg.journal_dir)
    mon = Monitor(cfg, client, store, sender, now=clock, news_view=news_view or NewsFake())
    return mon, client, sender, clock, store


def alerts(sender):
    return [t for t in sender.texts if "NO ORDER PLACED" in t]


# ---------------------------------------------------------------------- rules
def test_atr_and_levels():
    d = daily()
    assert rules.atr(d, 14) == pytest.approx(2.0)
    assert rules.previous_day(d)["h"] == 110.0


def test_detect_breakout_and_failed_break():
    cfg = BrentConfig()
    bo = rules.detect({"o": 109.9, "h": 110.6, "l": 109.8, "c": 110.5}, T0, 110, 108, 2.0, "d", cfg)
    assert [(s.kind, s.direction) for s in bo] == [("BREAKOUT", "LONG")]
    s = bo[0]
    assert (s.zone_lo, s.zone_hi, s.stop) == (110.0, 110.3, 109.5)
    fb = rules.detect({"o": 109.9, "h": 110.5, "l": 109.7, "c": 109.9}, T0, 110, 108, 2.0, "d", cfg)
    assert [(s.kind, s.direction) for s in fb] == [("FAILED_BREAK", "SHORT")]
    assert fb[0].stop == pytest.approx(110.6)
    assert rules.detect({"o": 109, "h": 109.5, "l": 108.5, "c": 109}, T0, 110, 108, 2.0, "d", cfg) == []


def test_no_fixed_level_bias():
    """118 is not automatically a sell nor 107 a buy: nothing arms without a setup."""
    cfg = BrentConfig()
    bar = {"o": 118, "h": 118.05, "l": 117.95, "c": 118}
    assert rules.detect(bar, T0, 119, 106, 2.0, "d", cfg) == []


def test_blockers_cover_each_condition():
    cfg = BrentConfig()
    s = rules.detect({"o": 109.9, "h": 110.6, "l": 109.8, "c": 110.5}, T0, 110, 108, 2.0, "d", cfg)[0]
    ok = rules.entry_blockers(s, 110.17, 110.20, T0, True, T0, cfg)
    assert ok == []
    assert any("old" in r for r in rules.entry_blockers(s, 110.17, 110.2, T0 - timedelta(seconds=30),
                                                         True, T0, cfg))
    assert any("spread" in r for r in rules.entry_blockers(s, 110.05, 110.2, T0, True, T0, cfg))
    assert any("entry zone" in r for r in rules.entry_blockers(s, 110.5, 110.53, T0, True, T0, cfg))
    assert any("closed" in r for r in rules.entry_blockers(s, 110.17, 110.2, T0, False, T0, cfg))
    sat = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
    assert any("closed" in r for r in rules.entry_blockers(s, 110.17, 110.2, sat, True, sat, cfg))
    wed = datetime(2026, 11, 4, 15, 20, tzinfo=UTC)  # 10:20 ET Wednesday: EIA blackout
    assert any("blackout" in r for r in rules.entry_blockers(s, 110.17, 110.2, wed, True, wed, cfg))
    narrow = BrentConfig(range_low=100, range_high=110.1)
    assert any("operating range" in r for r in rules.entry_blockers(s, 110.17, 110.2, T0, True, T0, narrow))


def test_plan_costs_lower_reward_to_risk():
    cfg = BrentConfig()
    s = rules.detect({"o": 109.9, "h": 110.6, "l": 109.8, "c": 110.5}, T0, 110, 108, 2.0, "d", cfg)[0]
    p = rules.plan(s, 110.2, 0.03, cfg)
    assert p["t1"] == pytest.approx(110.9) and p["t2"] == pytest.approx(111.6)
    assert 0 < p["rr1"] < 1 and 1 < p["rr2"] < 2


def test_market_hours():
    assert rules.market_closed_by_clock(datetime(2026, 11, 3, 22, 30, tzinfo=UTC))  # 17:30 ET break
    assert not rules.market_closed_by_clock(T0)
    assert rules.market_closed_by_clock(datetime(2026, 11, 8, 22, 0, tzinfo=UTC))  # Sun 17:00 ET
    assert not rules.market_closed_by_clock(datetime(2026, 11, 8, 23, 30, tzinfo=UTC))


def test_config_validation_and_hash(tmp_path):
    with pytest.raises(ValueError):
        BrentConfig(range_low=120, range_high=100)
    with pytest.raises(ValueError):
        BrentConfig(blackouts=["Wed 10:00"])
    assert BrentConfig().config_hash() != BrentConfig(max_spread=0.1).config_hash()
    assert BrentConfig().config_hash() == BrentConfig(notify_command="x").config_hash()
    p = tmp_path / "b.toml"
    p.write_text("nope = 1\n")
    with pytest.raises(ValueError, match="unknown"):
        load_config(p)


# ------------------------------------------------------------- alert flow
def test_alert_content_and_paper_fill(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    a = alerts(sender)
    assert len(a) == 1
    text = a[0]
    for must in ("BR-20261103-001", "PAPER/ADVISORY", "NO ORDER PLACED", "LONG BCO_USD",
                 "Valid until", "ET (", "UTC)", "ONLY while the executable ask",
                 "Suggested stop 110.00".replace("110.00", "109.50"), "(no order placed)",
                 "cancels this entry suggestion", "paper record exits only at the stop", "1R", "2R", "hold up to 8h", "chart-led, no material news",
                 "News (headlines/snippets only): Fresh check of 2 general news searches", "no news is not a direction signal",
                 "Chart: Daily regime up", "Main risk / reason to skip", "Report and chart:",
                 "Reward/risk after costs", "Other reasons to skip", "Position check",
                 "Policy row: no-news", "Size: not shown"):
        assert must in text, must
    sig = mon.st["signals"]["BR-20261103-001"]
    # validity clock starts at the final quote's timestamp, not at delivery
    assert datetime.fromisoformat(sig["expires_at"]) == T0 - timedelta(seconds=0.5) + timedelta(seconds=60)
    assert any(e["event"] == "signal" and e["id"] == sig["id"] for e in store.events())
    clock.advance(25)
    mon.tick()
    pp = mon.st["signals"][sig["id"]]["paper"]
    assert pp["state"] == "open"
    assert pp["entry"] == pytest.approx(110.20 + 0.03)  # ask + 1 spread, not the zone's best price


def test_price_leaving_zone_sends_skip_with_same_id(tmp_path):
    mon, client, sender, clock, _ = make(tmp_path)
    mon.tick()
    client.bid, client.ask = 110.40, 110.43
    clock.advance(3)
    mon.tick()
    skip = [t for t in sender.texts if "SKIP" in t]
    assert len(skip) == 1 and "BR-20261103-001" in skip[0] and "Do not chase" in skip[0]
    sig = mon.st["signals"]["BR-20261103-001"]
    assert sig["status"] == "invalidated"
    assert sig["paper"]["state"] == "missed"
    clock.advance(3)
    mon.tick()  # no repeat
    assert len([t for t in sender.texts if "SKIP" in t]) == 1


def test_spread_widening_invalidates(tmp_path):
    mon, client, sender, clock, _ = make(tmp_path)
    mon.tick()
    client.bid, client.ask = 110.05, 110.20
    clock.advance(3)
    mon.tick()
    assert "spread widened" in [t for t in sender.texts if "SKIP" in t][0]


def test_stale_quote_suppresses_alert(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    client.age = 30
    mon.tick()
    assert alerts(sender) == []
    sup = [e for e in store.events() if e["event"] == "suppressed"]
    assert sup and any("old" in r for r in sup[0]["reasons"])


def test_quote_errors_suppress_and_raise_ops_notice_once(tmp_path):
    mon, client, sender, clock, _ = make(tmp_path)
    client.fail_quotes = True
    for _ in range(25):
        mon.tick()
        clock.advance(3)
    ops = [t for t in sender.texts if t.startswith("[BRENT OPS]")]
    assert len(ops) == 1 and alerts(sender) == []


def test_expiry_and_new_signal_needed(tmp_path):
    mon, client, sender, clock, _ = make(tmp_path)
    mon.tick()
    clock.advance(61)
    mon.tick()
    assert any("EXPIRED" in t and "BR-20261103-001" in t for t in sender.texts)
    # The same setup never alerts again; a missed window needs a fresh setup.
    clock.advance(20 * 60)
    mon.tick()
    assert len(alerts(sender)) == 1


def test_slow_delivery_invalidates_immediately(tmp_path):
    mon, client, sender, clock, _ = make(tmp_path)
    sender.seconds = 50
    mon.tick()
    assert len(alerts(sender)) == 1
    assert "Telegram delivery took 50s" in [t for t in sender.texts if "SKIP" in t][0]


def test_measured_slow_delivery_blocks_alerts(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.st["metrics"]["send_s"] = [40.0] * 10
    mon.tick()
    assert alerts(sender) == []
    assert any("send p90" in r for e in store.events() if e["event"] == "suppressed"
               for r in e["reasons"])


def test_too_little_usable_time_suppresses(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    client.age = 9.5  # still fresh (<10s)
    mon.st["metrics"]["send_s"] = [25.0] * 10  # p90 within budget, but 60 - 9.5 - 25 < 30s usable
    mon.tick()
    assert alerts(sender) == []
    assert any("usable" in r for e in store.events() if e["event"] == "suppressed" for r in e["reasons"])


def test_duplicate_setup_and_cooldown(tmp_path):
    mon, client, sender, clock, _ = make(tmp_path, cooldown_minutes=30)
    mon.tick()
    clock.advance(61)
    mon.tick()  # expired
    # A new 15-min candle re-triggers the same breakout: same key, deduplicated.
    clock.advance(15 * 60)
    client.m15 = m15(110.6, end=clock())
    mon.tick()
    assert len(alerts(sender)) == 1
    # A different setup inside the cooldown is suppressed.
    clock.advance(60)
    client.m15 = m15(107.5, high=108.1, low=107.4, end=T0 + timedelta(minutes=30))
    client.bid, client.ask = 107.85, 107.88
    mon.st["last_bar_time"] = (T0 - timedelta(minutes=15)).isoformat()
    mon.tick()
    assert len(alerts(sender)) == 1


def test_market_closed_no_alert(tmp_path):
    mon, client, sender, clock, _ = make(tmp_path)
    client.tradeable = False
    mon.tick()
    assert alerts(sender) == []


def test_outside_range_pauses_and_explains(tmp_path):
    mon, client, sender, clock, _ = make(tmp_path, range_low=100, range_high=110.1)
    mon.tick()
    assert alerts(sender) == []
    assert any("above your 100-110.1 operating range" in t for t in sender.texts)
    mon.tick()
    assert sum("operating range" in t for t in sender.texts) == 1


def test_position_conflict_note(tmp_path):
    mon, client, sender, clock, _ = make(tmp_path)
    client.trades = [{"id": "287", "units": -1500.0, "price": 116.2}]
    mon.refresh_account(force=True)
    mon.tick()
    text = alerts(sender)[0]
    assert "SHORT 1,500 (trade 287" in text and "hedging is OFF" in text


def test_sizing_uses_risk_policy_only_when_set(tmp_path):
    mon, client, sender, clock, _ = make(tmp_path, risk_pct=0.1)
    mon.refresh_account(force=True)
    mon.tick()
    text = alerts(sender)[0]
    assert "units would lose ~0.1% of NAV" in text and "notional is not the risk" in text


def test_paper_stop_exit_net_of_costs(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    clock.advance(25)
    mon.tick()
    client.bid, client.ask = 109.45, 109.48
    clock.advance(5)
    mon.tick()
    ex = [e for e in store.events() if e["event"] == "paper_exit"][0]
    assert ex["reason"] == "stop" and ex["net_r"] < -1  # slippage, financing and FX make it worse


def test_restart_recovery(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    clock.advance(25)
    mon.tick()
    sig_id = "BR-20261103-001"
    assert mon.st["signals"][sig_id]["paper"]["state"] == "open"
    # Monitor dies; price hits the stop while it is down.
    clock.advance(600)
    base = clock() - timedelta(minutes=8)
    client.m1 = [{"time": base + timedelta(minutes=i), "complete": True,
                  "bid_o": 110.0, "bid_h": 110.1, "bid_l": 109.4 if i == 3 else 109.9, "bid_c": 110.0,
                  "ask_o": 110.03, "ask_h": 110.13, "ask_l": 109.43, "ask_c": 110.03} for i in range(6)]
    mon2 = Monitor(mon.cfg, client, Store(mon.cfg.journal_dir), sender, now=clock)
    mon2.recover(clock())
    pp = mon2.st["signals"][sig_id]["paper"]
    assert pp["state"] == "closed" and pp["exit_reason"] == "stop"


def test_restart_expires_window_that_passed(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    mon.save()
    clock.advance(300)
    mon2 = Monitor(mon.cfg, client, Store(mon.cfg.journal_dir), sender, now=clock)
    mon2.recover(clock())
    sig = mon2.st["signals"]["BR-20261103-001"]
    assert sig["status"] == "expired" and sig["paper"]["state"] == "missed"


def test_single_runner_lock(tmp_path):
    a, b = Store(tmp_path / "x"), Store(tmp_path / "x")
    a.acquire()
    with pytest.raises(AlreadyRunning):
        b.acquire()
    assert b.is_locked()
    a.release()
    assert not b.is_locked()


def test_separate_from_morning_demo(tmp_path):
    """Own folder, own files, no LLM, no demo journal writes; demo can run alongside."""
    from tradingagents.demo.journal import Journal

    demo = Journal(tmp_path / "demo")
    demo.write("run_start", trade_date="2026-11-03")
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    demo.write("run_end")
    assert [e["event"] for e in demo.events()] == ["run_start", "run_end"]
    assert store.dir != demo.dir and store.journal_path.name == "signals.jsonl"
    assert all(t.startswith("[BRENT") for t in sender.texts)
    assert BrentConfig().journal_dir.endswith("brent")


def test_client_is_read_only():
    calls = []

    class Resp:
        status_code = 200

        def __init__(self, body):
            self._b = body

        def json(self):
            return self._b

    class Session:
        headers = {}

        def get(self, url, params=None, timeout=None):
            calls.append(url)
            assert url.startswith("https://api-fxpractice.oanda.com/v3")
            return Resp({"prices": [{"bids": [{"price": "110.1"}], "asks": [{"price": "110.13"}],
                                     "time": "2026-11-03T14:00:00.123456789Z", "tradeable": True,
                                     "quoteHomeConversionFactors": {"positiveUnits": "1.38",
                                                                    "negativeUnits": "1.41"}}]})

    c = ReadOnlyOanda("t", account_id="101-x", session=Session())
    q = c.quote("BCO_USD")
    assert q.spread == pytest.approx(0.03) and q.quote_to_home == 1.41
    assert q.time == datetime(2026, 11, 3, 14, 0, 0, 123456, tzinfo=UTC)
    public = {n for n in dir(ReadOnlyOanda) if not n.startswith("__")}
    assert not public & {"market_order", "close_trade", "post", "put", "patch", "delete", "order"}
    assert parse_time("2026-11-03T14:00:00Z") == datetime(2026, 11, 3, 14, tzinfo=UTC)


SUPPORT_ITEM = {"key": "k1", "title": "Libya halts crude exports from two ports", "publisher": "Example Wire",
                "link": "https://example.com/libya", "published": "2026-11-03T12:30:00+00:00",
                "first_seen": "2026-11-03T12:34:00+00:00", "feed": "google:brent",
                "category": "supply_geopolitics", "status": "credible_report", "scope": "brent",
                "bias": "bullish", "mechanism": "Less Libyan supply tightens Brent.",
                "contradiction": "Outage length unknown.", "event_time": None, "window": "support"}


def test_news_supported_alert(tmp_path):
    view = {**NO_NEWS, "state": "bullish", "support": {"bullish": ["k1"]}, "items": [SUPPORT_ITEM]}
    mon, client, sender, clock, store = make(tmp_path, news_view=NewsFake(view))
    mon.tick()
    text = alerts(sender)[0]
    assert "| news-supported" in text and "Libya halts crude exports" in text
    assert "credible_report, brent; published 07:30:00 ET" in text and "first seen 07:34:00 ET" in text
    assert "Main risk / reason to skip: Outage length unknown." in text
    page = tmp_path / "brent" / "signals" / "BR-20261103-001.html"
    assert page.exists() and "<svg" in page.read_text() and "forming" in page.read_text()


def test_conflicting_news_blocks_but_chart_only_arm_records(tmp_path):
    bear = {**SUPPORT_ITEM, "bias": "bearish", "window": "conflict"}
    view = {**NO_NEWS, "state": "none_material", "conflict_window": {"bearish": ["k1"]}, "items": [bear]}
    mon, client, sender, clock, store = make(tmp_path, news_view=NewsFake(view))
    mon.tick()
    assert alerts(sender) == []
    ev = store.events()
    cand = [e for e in ev if e["event"] == "candidate"][0]
    assert cand["action"] == "NO TRADE" and cand["row"] == "conflict"
    shadow = [e for e in ev if e["event"] == "signal"]
    assert [e["id"] for e in shadow] == ["SH-20261103-001"] and shadow[0]["arm"] == "chart-only"
    clock.advance(25)
    mon.tick()  # the shadow trade is paper-tracked, never messaged
    assert any(e["event"] == "paper_fill" and e["id"] == "SH-20261103-001" for e in store.events())
    # Only one WATCH (the blocker is the conflicting news); the shadow trade is never messaged.
    assert len(sender.texts) == 1 and sender.texts[0].startswith("[BRENT WATCH]")
    assert "NOT AN ENTRY" in sender.texts[0] and "conflict" in sender.texts[0]


def test_news_unavailable_or_failing_means_watch(tmp_path):
    mon, client, sender, clock, store = make(
        tmp_path, news_view=NewsFake({"state": "unavailable", "why": "3 recent items not analysed yet"}))
    mon.tick()
    assert alerts(sender) == []

    def boom(_):
        raise RuntimeError("log unreadable")

    mon2, _, sender2, _, store2 = make(tmp_path / "b", news_view=boom)
    mon2.tick()
    assert alerts(sender2) == []
    cand = [e for e in store2.events() if e["event"] == "candidate"][0]
    assert cand["action"] == "WATCH" and cand["row"] == "evidence missing"


def test_range_regime_needs_news_and_counter_trend_needs_failed_break(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    client.daily = daily(step=0.0)  # flat: range regime
    mon.tick()
    assert alerts(sender) == []
    view = {**NO_NEWS, "state": "bullish", "support": {"bullish": ["k1"]}, "items": [SUPPORT_ITEM]}
    mon2, client2, sender2, _, store2 = make(tmp_path / "b", news_view=NewsFake(view))
    client2.daily = daily(step=-0.3, last_close=109.0)  # down regime, LONG breakout = counter-trend
    mon2.tick()
    assert alerts(sender2) == []
    cand = [e for e in store2.events() if e["event"] == "candidate"][0]
    assert cand["row"] == "counter-trend without a counter-trend setup"


def test_alerts_disabled_logs_decision_without_sending(tmp_path):
    mon, client, sender, clock, store = make(tmp_path, alerts_enabled=False)
    mon.tick()
    assert sender.texts == []
    sig = [e for e in store.events() if e["event"] == "signal" and e["arm"] == "combined"][0]
    assert sig["telegram"] is False and sig["label"] == "chart-led, no material news"


def test_assessment_change_at_final_check_suppresses(tmp_path):
    class Flip:
        def __init__(self):
            self.n = 0

        def __call__(self, now):
            self.n += 1
            return NO_NEWS if self.n == 1 else {"state": "unavailable", "why": "new item arrived"}

    mon, client, sender, clock, store = make(tmp_path, news_view=Flip())
    mon.tick()
    assert alerts(sender) == []
    assert any("assessment changed" in r for e in store.events() if e["event"] == "suppressed"
               for r in e["reasons"])


def test_unsettled_fresh_news_abstains(tmp_path):
    fresh = {**SUPPORT_ITEM, "first_seen": (T0 - timedelta(minutes=5)).isoformat()}
    view = {**NO_NEWS, "state": "bullish", "support": {"bullish": ["k1"]}, "items": [fresh]}
    mon, client, sender, clock, store = make(tmp_path, news_view=NewsFake(view))
    mon.tick()  # no price history at first receipt: cannot measure -> abstain
    assert alerts(sender) == []
    cand = [e for e in store.events() if e["event"] == "candidate"][0]
    assert cand["row"].startswith("unsettled")


def test_no_room_to_next_level_blocks(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    base = T0 - timedelta(hours=10)
    # an H1 swing high just above the entry (110.40, 0.2 above ask 110.20; stop risk 0.70)
    client.h1 = [{"time": base + timedelta(hours=i), "complete": True, "o": 110, "c": 110,
                  "h": 110.4 if i == 5 else 110.1, "l": 109.6} for i in range(10)]
    mon.tick()
    assert alerts(sender) == []
    cand = [e for e in store.events() if e["event"] == "candidate"][0]
    assert cand["row"].startswith("no room")


def test_report_and_page(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    clock.advance(25)
    mon.tick()
    client.bid, client.ask = 111.65, 111.68  # 2R target
    clock.advance(5)
    mon.tick()
    res = summarize(store.events())
    assert res["operational"]["signals_issued"] == 1
    groups = res["paper_hypothetical"]["by_group"]
    (g, stats), = groups.items()
    assert g == "brent-exp-3 | combined chart-led | sent (notify command accepted) | paper-v2"
    assert stats["closed"] == 1 and stats["net_r_mean"] > 1
    assert res["manual_confirmed"]["records"] == 0
    assert "NOT a measure of news's contribution" in res["chart_only_comparison"]["differences_from_combined"]
    assert "proof" in res["note"]
    page = write_page(mon.st, store.events(), False, tmp_path / "p" / "index.html")
    assert "Brent alerts" in page.read_text() and "<script" not in page.read_text()
    assert any("armed setups" in line for line in status_lines(mon.st, False))


def test_cmd_run_builds_the_monitor(tmp_path, monkeypatch):
    """The real `run` entry point wires the Monitor (regression: stale keyword crashed it)."""
    from tradingagents.brent import __main__ as cli

    cfg = BrentConfig(journal_dir=str(tmp_path))
    monkeypatch.setattr(cli, "_client", lambda c: FakeClient(lambda: datetime.now(UTC)))
    monkeypatch.setattr(cli, "_sender", lambda c: (lambda text: (True, 0.1)))
    ran = []
    monkeypatch.setattr(Monitor, "startup", lambda self: None)
    monkeypatch.setattr(Monitor, "run", lambda self, stop, on_tick: ran.append(True))
    assert cli.cmd_run(None, cfg) == 0 and ran == [True]


def test_cmd_run_records_why_it_stopped(tmp_path, monkeypatch):
    from tradingagents.brent import __main__ as cli

    cfg = BrentConfig(journal_dir=str(tmp_path))
    monkeypatch.setattr(cli, "_client", lambda c: FakeClient(lambda: datetime.now(UTC)))
    monkeypatch.setattr(cli, "_sender", lambda c: (lambda text: (True, 0.1)))
    monkeypatch.setattr(Monitor, "startup", lambda self: (_ for _ in ()).throw(KeyboardInterrupt))
    assert cli.cmd_run(None, cfg) == 0
    stop = [e for e in Store(str(tmp_path)).events() if e["event"] == "stop"][-1]
    assert stop["reason"].startswith("KeyboardInterrupt")


def test_sender_runs_notify_in_own_session(tmp_path, monkeypatch):
    import subprocess

    from tradingagents.brent import __main__ as cli

    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, _ = cli._sender(BrentConfig(journal_dir=str(tmp_path), notify_command="true"))("hi")
    # POSIX: own session so the notify tool can't signal the monitor; Windows has no sessions
    import os
    assert ok and seen["input"] == "hi" and seen["start_new_session"] is (os.name != "nt")


def test_range_80_115_inclusive_boundaries_use_mid_and_zone():
    cfg = BrentConfig()
    assert (cfg.range_low, cfg.range_high) == (80.0, 115.0)
    assert cfg.config_hash() != BrentConfig(range_low=100.0, range_high=120.0).config_hash()

    def blockers(level, bid, ask):
        s = rules.detect({"o": level - 0.1, "h": level + 0.6, "l": level - 0.2, "c": level + 0.5},
                         T0, level, level - 2, 2.0, "d", cfg)[0]
        return [r for r in rules.entry_blockers(s, bid, ask, T0, True, T0, cfg) if "operating range" in r]

    # A long breakout above a yesterday-high of 90 has its zone near 90: well inside.
    assert blockers(90.0, 90.17, 90.20) == []
    # Mid just below 80 or above 115 is outside, whatever the zone.
    assert blockers(79.5, 79.5, 79.54)
    assert blockers(115.5, 115.5, 115.54)
    # Exactly 80.0 and 115.0 mid are inside the mid gate; the zone must fit too.
    narrow_lo = BrentConfig(range_low=80.0, range_high=115.0)
    assert narrow_lo.range_low <= (79.98 + 80.02) / 2 <= narrow_lo.range_high
    assert not (narrow_lo.range_low <= (79.96 + 80.0) / 2 <= narrow_lo.range_high)
    assert not (narrow_lo.range_low <= (115.0 + 115.04) / 2 <= narrow_lo.range_high)


# ----------------------------------------------------------- WATCH lifecycle
def watches(sender):
    return [t for t in sender.texts if t.startswith("[BRENT WATCH]")]


def arm_setup(mon, client, clock):
    """Arm a LONG breakout at 110 (zone 110.00-110.30) with price still outside the zone."""
    client.bid, client.ask = 111.50, 111.53
    client.m15 = m15(110.5, high=110.6, low=109.8, end=clock.t)
    mon.tick()


def test_one_watch_per_state_then_entry_links_to_it(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    arm_setup(mon, client, clock)
    assert len(watches(sender)) == 1 and alerts(sender) == []
    w = watches(sender)[0]
    assert "BW-20261103-001" in w and "NOT AN ENTRY" in w and "price not in the entry zone" in w
    assert "Next confirmation" in w and "News (headlines/snippets only)" in w and "Chart:" in w
    assert mon.st["signals"] == {} or all(x.get("arm") != "combined" for x in mon.st["signals"].values())
    for _ in range(5):               # same state on later polls: no repeat
        clock.advance(10)
        mon.tick()
    assert len(watches(sender)) == 1
    client.bid, client.ask = 110.17, 110.20      # enters the zone with all checks passing
    clock.advance(10)
    mon.tick()
    assert len(alerts(sender)) == 1 and "follows WATCH BW-20261103-001" in alerts(sender)[0]
    assert "| ENTRY |" in alerts(sender)[0]
    clock.advance(10)
    mon.tick()
    assert not [t for t in sender.texts if "CANCELLED" in t]       # linked, not cancelled
    ev = store.events()
    assert any(e["event"] == "watch_end" and e["outcome"] == "entry" for e in ev)
    assert not any(e["event"] == "paper_fill" and e["id"].startswith("BW") for e in ev)


def test_watch_is_cancelled_when_the_setup_breaks(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    arm_setup(mon, client, clock)
    assert len(watches(sender)) == 1
    clock.advance(1900)
    client.m15 = m15(109.6, high=110.0, low=109.5, end=clock.t)   # closes back through the level
    mon.tick()
    cancel = [t for t in sender.texts if "CANCELLED" in t]
    assert len(cancel) == 1 and "BW-20261103-001" in cancel[0] and "no order was placed" in cancel[0]
    clock.advance(10)
    mon.tick()
    assert len([t for t in sender.texts if "CANCELLED" in t]) == 1


def test_watch_not_sent_outside_range_or_with_alerts_off(tmp_path):
    mon, client, sender, clock, _ = make(tmp_path, range_low=80, range_high=105)
    arm_setup(mon, client, clock)
    assert watches(sender) == []
    mon2, client2, sender2, clock2, _ = make(tmp_path / "b", alerts_enabled=False)
    arm_setup(mon2, client2, clock2)
    assert sender2.texts == []


def test_news_blocker_change_sends_one_watch_update(tmp_path):
    bear = {**SUPPORT_ITEM, "bias": "bearish", "window": "conflict"}
    conflict = {**NO_NEWS, "conflict_window": {"bearish": ["k1"]}, "items": [bear]}
    nf = NewsFake(conflict)
    mon, client, sender, clock, _ = make(tmp_path, news_view=nf)
    client.bid, client.ask = 110.17, 110.20
    client.m15 = m15(110.5, high=110.6, low=109.8, end=clock.t)
    mon.tick()
    assert len(watches(sender)) == 1 and "conflict" in watches(sender)[0]
    nf.view = dict(NO_NEWS)                       # conflict resolves: ready -> ENTRY, no 2nd WATCH
    clock.advance(400)
    mon.tick()
    assert len(watches(sender)) == 1 and len(alerts(sender)) == 1


def test_progression_counts_unique_setups_and_primary_blockers(tmp_path):
    from tradingagents.brent.report import progression

    mon, client, sender, clock, store = make(tmp_path)
    arm_setup(mon, client, clock)                       # detected, zone never reached
    for _ in range(4):
        clock.advance(10)
        mon.tick()
    p = progression(store.events())
    assert p["setups_detected"] == 1 and p["entry_zone_reached"] == 0
    assert p["notification_accepted"] == 0 and p["watch_messages_sent"] == 1
    assert p["primary_blocker_of_setups_not_notified"] == {"entry zone never reached (no setup lost)": 1}
    client.bid, client.ask = 110.17, 110.20
    clock.advance(10)
    mon.tick()
    p = progression(store.events())
    assert (p["entry_zone_reached"], p["evidence_ready"], p["entry_checks_passed"],
            p["notification_accepted"], p["watch_to_entry"]) == (1, 1, 1, 1, 1)
    assert "phone display is not measured" in p["denominators"]


def test_status_shows_effective_settings_and_budget(tmp_path):
    from tradingagents.brent.report import news_health, status_lines

    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    nh = news_health(mon.cfg.journal_dir, clock.t, cfg=mon.cfg)
    txt = "\n".join(status_lines(store.state(), True, clock.t, news=nh))
    assert "operating range 80-115" in txt and "mid-price gate" in txt
    assert "model budget (shared, rolling): 0/12 last hour, 0/250 last 24h" in txt
    assert "phone display: not measured" in txt


def test_health_command_and_progress_checks(tmp_path, monkeypatch, capsys):
    from tradingagents.brent import __main__ as m
    from tradingagents.brent.report import overall_health

    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    st = store.state()
    healthy_news = {"running": True, "status": "checked", "why": "",
                    "last_poll_attempt": clock.t.isoformat()}
    assert overall_health(st, True, healthy_news, clock.t) == []
    later = clock.t + timedelta(seconds=200)            # process alive, but nothing progressing
    st2 = {**st, "last_tick": later.isoformat()}
    assert "no new quote for 120s" in overall_health(st2, True, healthy_news, later)
    stale_news = {**healthy_news, "last_poll_attempt": (later - timedelta(minutes=20)).isoformat()}
    assert any("not polling" in b for b in overall_health(st2, True, stale_news, later))

    pings = []
    monkeypatch.setattr(m, "ping_url", lambda url, timeout=10.0: pings.append(url) or True)
    ping_file = tmp_path / "hb"
    ping_file.write_text("https://hc.example/abc\n")
    monkeypatch.setattr(m, "load_config", lambda p: mon.cfg)
    monkeypatch.setattr("tradingagents.brent.report.news_health", lambda *a, **k: healthy_news)
    monkeypatch.setattr(m.Store, "is_locked", lambda self: False)   # monitor "not running"
    assert m.main(["health", "--ping-file", str(ping_file)]) == 1
    assert pings == ["https://hc.example/abc/fail"]
    assert "hc.example" not in capsys.readouterr().out               # URL never printed
