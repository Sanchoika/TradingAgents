"""Regression tests for the 2026-11-03 audit fixes (beyond the supplied probes).

No network, no model, no orders: fake client, clock, sender and news. All data is test data.
"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.test_brent_alerts import NO_NEWS, T0, NewsFake, Sender, alerts, make
from tests.test_brent_news import GOOD, make as make_news
from tradingagents.brent import news
from tradingagents.brent.client import Quote
from tradingagents.brent.config import BrentConfig
from tradingagents.brent.engine import Monitor
from tradingagents.brent.messages import ENTRY_ONLY
from tradingagents.brent.report import status_lines, summarize
from tradingagents.brent.rules import Setup
from tradingagents.brent.store import Store

pytestmark = pytest.mark.unit


def iso(t):
    return t.isoformat()


def open_paper(mon, direction, entry, stop, t2, t1=None, now=T0):
    """Put one open paper position straight into the monitor's state."""
    d = 1 if direction == "LONG" else -1
    s = Setup(key="k", kind="BREAKOUT", direction=direction, level_name="PDH", level=entry,
              zone_lo=entry - 0.1, zone_hi=entry + 0.1, stop=stop, invalid_close=entry,
              atr=2.0, armed_at=iso(now), expires_at=iso(now + timedelta(hours=4)))
    sig = {"id": "T-1", "arm": "combined", "setup": s.to_dict(), "status": "expired",
           "plan": {"t1": t1 if t1 is not None else entry + d * abs(entry - stop), "t2": t2},
           "delivered": True, "telegram": True, "decided_at": iso(now), "sent_at": iso(now),
           "expires_at": iso(now + timedelta(seconds=60)), "marks": {}, "alert_mid": entry,
           "paper": {"state": "open", "entry": entry, "risk": abs(entry - stop),
                     "deadline": iso(now + timedelta(hours=8)), "mfe": 0.0, "mae": 0.0, "t1": False,
                     "last_checked": iso(now), "entry_time": iso(now), "rules": "paper-v2"}}
    mon.st["signals"]["T-1"] = sig
    return sig, s


def quote(clock, bid, ask, age=0.5, tradeable=True):
    now = clock()
    return Quote(bid=bid, ask=ask, time=now - timedelta(seconds=age), fetched_at=now,
                 tradeable=tradeable, quote_to_home=1.4, rtt=0.1)


# ---------------------------------------------------------------- paper exits
def test_short_target_needs_ask_at_target(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    sig, s = open_paper(mon, "SHORT", entry=110.0, stop=110.5, t2=109.0)
    clock.advance(3)
    mon.paper_step(sig, s, quote(clock, 108.98, 109.01), clock())   # bid below, ask above target
    assert sig["paper"]["state"] == "open"
    clock.advance(3)
    mon.paper_step(sig, s, quote(clock, 108.97, 109.00), clock())
    assert sig["paper"]["state"] == "closed" and sig["paper"]["exit"] == 109.0


def test_short_stop_gap_fills_at_ask_plus_slippage(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    sig, s = open_paper(mon, "SHORT", entry=110.0, stop=110.5, t2=109.0)
    clock.advance(3)
    mon.paper_step(sig, s, quote(clock, 111.97, 112.00), clock())
    pp = sig["paper"]
    assert pp["exit_reason"] == "stop" and pp["exit"] == pytest.approx(112.03)
    assert pp["exit_basis"] == "live price through the stop"


def test_long_stop_touch_without_gap_still_stop_minus_spread_on_replay(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    sig, s = open_paper(mon, "LONG", entry=110.0, stop=109.5, t2=111.0)
    clock.advance(600)
    base = T0
    client.m1 = [{"time": base + timedelta(minutes=i), "complete": True,
                  "bid_o": 110.0, "bid_h": 111.2 if i == 4 else 110.1,  # both levels in bar 4
                  "bid_l": 109.4 if i == 4 else 109.9, "bid_c": 110.0,
                  "ask_o": 110.03, "ask_h": 111.23, "ask_l": 109.43, "ask_c": 110.03} for i in range(8)]
    assert mon.catch_up(sig, clock())
    pp = sig["paper"]
    assert pp["exit_reason"] == "stop" and pp["exit"] == pytest.approx(109.47)  # stop first


def test_replay_candle_opening_through_stop_uses_open(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    sig, s = open_paper(mon, "SHORT", entry=110.0, stop=110.5, t2=109.0)
    clock.advance(300)
    client.m1 = [{"time": T0 + timedelta(minutes=2), "complete": True,
                  "bid_o": 110.9, "bid_h": 111.0, "bid_l": 110.8, "bid_c": 110.9,
                  "ask_o": 110.93, "ask_h": 111.03, "ask_l": 110.83, "ask_c": 110.93}]
    mon.catch_up(sig, clock())
    assert sig["paper"]["exit"] == pytest.approx(110.93 + 0.03)


def test_failed_catch_up_keeps_reconciled_time_and_skips_quote(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    sig, s = open_paper(mon, "LONG", entry=110.0, stop=109.5, t2=111.0)
    before = sig["paper"]["last_checked"]
    clock.advance(300)

    def boom(*a):
        raise RuntimeError("candles down")

    client.candles = boom
    mon.paper_step(sig, s, quote(clock, 111.5, 111.53), clock())  # live price above target
    assert sig["paper"]["state"] == "open" and sig["paper"]["last_checked"] == before
    assert any(e["event"] == "catch_up_error" for e in store.events())


def test_missing_replay_minutes_are_labelled_as_a_gap(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    sig, s = open_paper(mon, "LONG", entry=110.0, stop=109.5, t2=111.0)
    clock.advance(600)
    client.m1 = []  # no data at all for 10 minutes of an open market
    assert mon.catch_up(sig, clock())
    assert sig["paper"]["data_gap"] is True
    assert any(e["event"] == "catch_up_gap" for e in store.events())


@pytest.mark.parametrize("age,tradeable", [(30, True), (0.5, False)])
def test_stale_or_untradeable_quote_never_exits(tmp_path, age, tradeable):
    mon, client, sender, clock, store = make(tmp_path)
    sig, s = open_paper(mon, "LONG", entry=110.0, stop=109.5, t2=111.0)
    clock.advance(3)
    mon.paper_step(sig, s, quote(clock, 109.0, 109.03, age=age, tradeable=tradeable), clock())
    assert sig["paper"]["state"] == "open"


def test_paper_fill_records_quote_and_rules(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    clock.advance(25)
    mon.tick()
    fill = [e for e in store.events() if e["event"] == "paper_fill"][0]
    assert fill["paper_rules"] == "paper-v2" and fill["quote_age_s"] <= 10
    assert {"bid", "ask", "quote_time", "beyond_zone"} <= set(fill)


# ----------------------------------------------------------- entry validity
def test_conflict_inside_window_sends_skip_without_implying_position_change(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    bear = {"key": "b1", "title": "x", "bias": "bearish", "window": "conflict", "first_seen": iso(T0)}
    mon.news_view = NewsFake({**NO_NEWS, "conflict_window": {"bearish": ["b1"]}, "items": [bear]})
    clock.advance(5)
    mon.tick()
    sig = mon.st["signals"]["BR-20261103-001"]
    assert sig["status"] == "invalidated" and "no longer approves" in sig["invalid_reason"]
    skip = [t for t in sender.texts if "SKIP" in t][0]
    assert ENTRY_ONLY in skip
    clock.advance(25)
    mon.tick()
    assert mon.st["signals"]["BR-20261103-001"]["paper"]["state"] == "missed"


def test_expiry_text_says_entry_only(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    client.bid, client.ask = 110.60, 110.63   # out of zone? no: keep valid until expiry
    client.bid, client.ask = 110.17, 110.20
    for _ in range(14):
        clock.advance(5)
        mon.tick()
    exp = [t for t in sender.texts if "EXPIRED" in t]
    assert exp and ENTRY_ONLY in exp[0]


def test_chart_data_stale_without_error_blocks_new_entries(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    client.bid, client.ask = 110.47, 110.50   # above the zone: armed, not alerted
    mon.tick()
    clock.advance(20 * 60)                    # no newer 15-min candle arrives
    client.bid, client.ask = 110.17, 110.20
    mon.tick()
    assert alerts(sender) == []
    sup = [e for e in store.events() if e["event"] == "suppressed"]
    assert any("chart data stale" in r for e in sup for r in e["reasons"])


def test_shadow_signals_are_simulated_not_delivered(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    sh = mon.st["signals"]["SH-20261103-001"]
    assert sh["delivered"] is False and sh["simulated"] is True
    assert sh["notice"] == "not sent: chart-only comparison arm"
    br = mon.st["signals"]["BR-20261103-001"]
    assert br["delivered"] is True and "phone display not measured" in br["notice"]


def test_signal_and_start_carry_experiment_manifest(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.startup(samples=1, sleep=lambda s: None)
    mon.tick()
    sig = [e for e in store.events() if e["event"] == "signal"][0]
    m = sig["experiment"]
    assert m["experiment"] == "brent-exp-2" and m["news_policy"] == "news-policy-v2"
    assert m["model"] == "chatgpt:gpt-6-luna" and m["prompt_hash"] == news.prompt_hash()
    assert m["code_commit"] and m["experiment_hash"]
    start = [e for e in store.events() if e["event"] == "start"][0]
    assert start["manifest"]["experiment"] == "brent-exp-2"
    assert "Experimental entry alerts are ENABLED" in sender.texts[0]


def test_failed_alert_send_gets_no_paper_fill(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon._send = Sender(clock, ok=False)
    mon.tick()
    clock.advance(25)
    mon.tick()
    ev = store.events()
    assert any(e["event"] == "paper_missed" and e["id"] == "BR-20261103-001" for e in ev)
    assert not any(e["event"] == "paper_fill" and e["id"] == "BR-20261103-001" for e in ev)


# ------------------------------------------------------------------- reports
def test_report_separates_groups_and_old_paper_records():
    events = [
        {"event": "signal", "id": "BR-1", "arm": "combined", "label": "chart-led, no material news",
         "telegram": True, "setup": {"kind": "BREAKOUT", "direction": "LONG"}},
        {"event": "sent", "kind": "alert", "id": "BR-1", "ok": True, "seconds": 2.0},
        {"event": "signal", "id": "BR-2", "arm": "combined", "label": "news-supported", "telegram": True,
         "setup": {"kind": "BREAKOUT", "direction": "LONG"}, "experiment": {"experiment": "brent-exp-2"},
         "paper_rules": "paper-v2"},
        {"event": "sent", "kind": "alert", "id": "BR-2", "ok": True, "seconds": 2.0},
        {"event": "signal", "id": "SH-1", "arm": "chart-only", "telegram": False,
         "setup": {"kind": "BREAKOUT", "direction": "LONG"}},
        {"event": "paper_exit", "id": "BR-1", "net_r": 1.5, "reason": "target 2R"},
        {"event": "paper_exit", "id": "BR-2", "net_r": -1.1, "reason": "stop", "paper_rules": "paper-v2"},
        {"event": "paper_exit", "id": "SH-1", "net_r": 0.2, "reason": "time"},
        {"event": "mark", "id": "BR-2", "minutes": 60, "move_atr": 0.1},
        {"event": "mark", "id": "SH-1", "minutes": 60, "move_atr": -0.2},
    ]
    r = summarize(events)
    groups = r["paper_hypothetical"]["by_group"]
    old = "pre-manifest | combined chart-led | sent (notify command accepted) | paper-v1"
    assert groups[old]["caution"].startswith("paper-v1") and groups[old]["net_r_total"] == 1.5
    assert "brent-exp-2 | combined news-supported | sent (notify command accepted) | paper-v2" in groups
    assert "BR-2" not in json.dumps(r["chart_only_comparison"]["by_group"])
    assert set(r["directional_by_group"]) == {
        "brent-exp-2 | combined news-supported | sent (notify command accepted)",
        "pre-manifest | chart-only (shadow, not sent)"}


def test_status_degraded_when_news_collector_down(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    news_down = {"running": False, "status": "collector_stale", "state": "unavailable",
                 "why": "no news poll", "last_poll_attempt": None, "general_news_fresh": 0,
                 "basis": "headline_and_snippet"}
    lines = status_lines(mon.st, True, now=clock(), news=news_down)
    assert lines[0].startswith("overall: DEGRADED") and "news collector not running" in lines[0]
    ok = {**news_down, "running": True, "status": "checked", "state": "none_material", "why": ""}
    assert status_lines(mon.st, True, now=clock(), news=ok)[0] == "overall: OK"


def test_monitor_notices_stopped_news_collector(tmp_path):
    clock_t = [T0]
    cfg = BrentConfig(journal_dir=str(tmp_path / "brent"), alerts_enabled=True)
    from tests.test_brent_alerts import Clock, FakeClient

    clock = Clock()
    sender = Sender(clock)
    mon = Monitor(cfg, FakeClient(clock), Store(cfg.journal_dir), sender, now=clock)  # real news view
    mon.tick()
    assert any("news collector is not running" in t for t in sender.texts)
    assert mon.st["news_process"]["running"] is False
    assert alerts(sender) == []   # no news -> WATCH
    del clock_t


# ---------------------------------------------------------------------- news
def test_news_view_statuses(tmp_path):
    col, clock = make_news(tmp_path)
    col.poll()
    v = news.news_view(tmp_path, clock())
    assert v["state"] == "unavailable" and v["status"] == "pending_analysis"
    keys = [i["key"] for i in news.pending_items(col)]

    def bad(system, user):
        return "not json", {}

    news.analyze(col, bad, news.Budget())
    v = news.news_view(tmp_path, clock())
    assert v["status"] == "analysis_failed" and "last model call failed" in v["why"]

    def good(system, user):
        return json.dumps([{**GOOD, "key": k, "bias": "unclear", "relevant": False} for k in keys]), {}

    news.analyze(col, good, news.Budget())
    v = news.news_view(tmp_path, clock())
    assert v["status"] == "checked" and v["state"] == "none_material"
    assert v["basis"] == "headline_and_snippet" and "not interpreted" in v["official_data_note"]
    clock.t += timedelta(minutes=11)
    assert news.news_view(tmp_path, clock())["status"] == "collector_stale"


def test_analyze_works_through_backlog_oldest_first(tmp_path):
    col, clock = make_news(tmp_path)
    for i in range(3):
        col._register(news.Item(feed="google:brent", title=f"Story number {i} about crude {i * 7}",
                                link=f"https://ex.com/s{i}", published=None))
        clock.t += timedelta(minutes=1)
    seen = []

    def model(system, user):
        rows = json.loads(user.split("\n")[1])
        seen.append([r["key"] for r in rows])
        return json.dumps([{**GOOD, "key": r["key"]} for r in rows]), {}

    first = news.analyze(col, model, news.Budget(), max_items=2)
    assert first["backlog"] == 3 and len(first["item_keys"]) == 2
    second = news.analyze(col, model, news.Budget(), max_items=2)
    assert len(second["item_keys"]) == 1 and not set(seen[0]) & set(seen[1])
    assert news.pending_items(col) == []


def test_eia_new_version_is_new_item_but_not_news_coverage(tmp_path):
    col, clock = make_news(tmp_path, fail=("news.google.com",))
    col.poll()
    col.get = lambda url: SimpleNamespace(text="v2,changed\n", status_code=200, url=url,
                                          headers={}, raise_for_status=lambda: None)
    clock.t += timedelta(minutes=5)
    new = col.poll()
    assert any(i.feed.startswith("eia:") for i in new)
    assert col.feed_health()["fresh_check"] is False


def test_news_check_backs_off_after_failures_and_runs_continuously_by_default(tmp_path, monkeypatch):
    from tradingagents.brent import __main__ as cli

    polls = {"n": 0}

    class Collector:
        def poll(self):
            polls["n"] += 1
            if polls["n"] >= 4:
                raise KeyboardInterrupt  # stop the endless loop
            return []

        def feed_health(self):
            return {"fresh_check": True}

    monkeypatch.setattr(news, "NewsCollector", lambda *a: Collector())
    monkeypatch.setattr(news, "default_model", lambda *a: object())
    calls = []
    monkeypatch.setattr(news, "analyze", lambda *a: calls.append(1) or {"error": "boom", "called": True})
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    args = cli.argparse.Namespace(analyze=True, once=False, minutes=0.0)
    assert cli.cmd_news_check(args, BrentConfig(journal_dir=str(tmp_path))) == 0
    assert polls["n"] == 4      # did not stop on its own: minutes=0 means run until stopped
    assert len(calls) == 1      # after a failure, the retry waits (backoff), not every poll


def test_news_check_refuses_second_collector(tmp_path):
    from tradingagents.brent import __main__ as cli

    lock = Store(tmp_path / "newscheck")
    lock.acquire()
    try:
        args = cli.argparse.Namespace(analyze=False, once=True, minutes=0.0)
        assert cli.cmd_news_check(args, BrentConfig(journal_dir=str(tmp_path))) == 3
    finally:
        lock.release()


def test_news_check_parser_defaults_to_continuous():
    import contextlib
    import io

    from tradingagents.brent import __main__ as cli

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
        cli.main(["news-check", "--help"])
    assert "0 = run until stopped" in " ".join(buf.getvalue().split())


def test_no_orders_anywhere():
    """Alerts-only boundary: the client has no write methods."""
    from tradingagents.brent.client import ReadOnlyOanda

    for name in dir(ReadOnlyOanda):
        assert not name.startswith(("post", "put", "patch", "delete", "order", "close"))
    assert datetime.now(UTC)  # keep import used


def test_alert_switch_reloads_without_restart(tmp_path):
    cfgfile = tmp_path / "brent.toml"
    cfgfile.write_text("alerts_enabled = false\n")
    mon, client, sender, clock, store = make(tmp_path, alerts_enabled=False)
    mon.cfg_path = str(cfgfile)
    client.bid, client.ask = 110.47, 110.50   # armed, outside the zone: nothing to send yet
    mon.tick()
    assert sender.texts == []
    cfgfile.write_text("alerts_enabled = true\n")
    clock.advance(61)
    client.m15 = [{**client.m15[0], "time": client.m15[0]["time"]}]  # same bar; still fresh
    mon.tick()
    assert mon.cfg.alerts_enabled is True
    assert any(t.startswith("[BRENT STATUS] Experimental Brent entry alerts are ENABLED") for t in sender.texts)
    assert any(e["event"] == "alerts_switch" and e["enabled"] for e in store.events())


def test_token_prompt_strips_bracketed_paste_and_control_chars(monkeypatch):
    from tradingagents.brent import client

    monkeypatch.setattr(client.getpass, "getpass", lambda prompt: "\x1b[200~abc-123\x1b[201~\r\n")
    assert client.token_from_env_or_prompt(env={}) == "abc-123"
    monkeypatch.setattr(client.getpass, "getpass", lambda prompt: "[200~xyz[201~ ")
    assert client.token_from_env_or_prompt(env={}) == "xyz"


def test_client_connect_retries_timeouts_but_not_bad_tokens(monkeypatch):
    import requests

    from tradingagents.brent import __main__ as cli, client

    monkeypatch.setattr(client, "token_from_env_or_prompt", lambda: "tok")
    calls = []

    class Flaky:
        def __init__(self, token):
            calls.append(token)
            if len(calls) < 3:
                raise requests.ReadTimeout("read timed out")

    monkeypatch.setattr(client, "ReadOnlyOanda", Flaky)
    assert isinstance(cli._client(BrentConfig(), sleep=lambda s: None), Flaky) and len(calls) == 3

    def bad(token):
        raise client.OandaReadError("GET /accounts -> HTTP 400: bad")

    monkeypatch.setattr(client, "ReadOnlyOanda", bad)
    with pytest.raises(client.OandaReadError):
        cli._client(BrentConfig(), sleep=lambda s: None)
