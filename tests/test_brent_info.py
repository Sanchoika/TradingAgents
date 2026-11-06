"""Brent information lane: content, labels, caps, dedup, updates, isolation from trades."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tradingagents.brent import info
from tradingagents.brent.config import BrentConfig
from tradingagents.brent.info import InfoLane

pytestmark = pytest.mark.unit

# Thursday 2026-11-05, times in UTC (ET = UTC-5)
T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)          # 07:00 ET
TRADE_WORDS = ("ENTRY", "stop ", "target", "BUY", "SELL")


class Store:
    def __init__(self):
        self.rows = []

    def write(self, event, **kw):
        self.rows.append({"event": event, **kw})

    def of(self, event):
        return [r for r in self.rows if r["event"] == event]


def quote(mid, spread=0.04):
    return SimpleNamespace(mid=mid, bid=mid - spread / 2, ask=mid + spread / 2, tradeable=True)


CTX = {"pdh": 85.0, "pdl": 83.0, "atr": 2.0}
DAILY = [{"time": T0 - timedelta(days=25 - i), "o": 80.0 + i * 0.2, "h": 81.0 + i * 0.2,
          "l": 79.0 + i * 0.2, "c": 80.5 + i * 0.2, "complete": True} for i in range(25)]


def lane(tmp_path, **kw):
    kw.setdefault("info_alerts", True)
    cfg = BrentConfig(journal_dir=str(tmp_path), **kw)
    store, sent, st = Store(), [], {}
    ln = InfoLane(cfg, store, st, lambda text, ref, t: sent.append(text) or True)
    return ln, store, sent


def write_news(tmp_path, items, parsed):
    rows = [{"ts": T0.isoformat(), "event": "item", "feed": "google:brent", **i} for i in items]
    rows.append({"ts": T0.isoformat(), "event": "analysis", "called": True, "parsed": parsed})
    (tmp_path / "news.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (tmp_path / "news_state.json").write_text(json.dumps({"feeds": {}, "last_poll_at": T0.isoformat()}))


CLS = {"category": "opec_plus", "status": "credible_report", "event_time": None, "scope": "brent",
       "relevant": True, "mechanism": "Longer cuts keep supply tight.", "bias": "bullish",
       "contradiction": "", "update_of_older_story": False, "relation": "new"}


def item(key, title, at, cluster=None, repeat_of=None):
    return {"key": key, "title": title, "publisher": "Reuters", "link": f"https://ex.com/{key}",
            "published": at.isoformat(), "first_seen": at.isoformat(), "cluster": cluster or key,
            "repeat_of": repeat_of, "summary": ""}


def test_brief_at_seven_once_with_labels_and_no_trade_words(tmp_path):
    ln, store, sent = lane(tmp_path)
    write_news(tmp_path, [], [])
    ln.step(quote(84.10), CTX, DAILY, str(tmp_path), T0 + timedelta(minutes=1))
    ln.step(quote(84.12), CTX, DAILY, str(tmp_path), T0 + timedelta(minutes=2))
    assert len(sent) == 1
    b = sent[0]
    assert b.startswith("[BRENT INFO] BI-20261105-001 | 07:00 ET BRIEF | INFORMATION ONLY")
    assert "Brent mid 84.10" in b and "Next EIA weekly report" in b and "20-day range" in b
    assert not any(w in b for w in TRADE_WORDS)
    assert store.of("info")[0]["kind"] == "brief"


def test_shadow_mode_logs_but_sends_nothing(tmp_path):
    ln, store, sent = lane(tmp_path, info_alerts=False)
    write_news(tmp_path, [], [])
    ln.step(quote(84.10), CTX, DAILY, str(tmp_path), T0 + timedelta(minutes=1))
    assert sent == [] and store.of("info")[0]["shadow"] is True


def test_a_late_brief_is_skipped_not_sent_stale(tmp_path):
    ln, store, sent = lane(tmp_path)
    write_news(tmp_path, [], [])
    ln.step(quote(84.10), CTX, DAILY, str(tmp_path), T0 + timedelta(minutes=50))
    assert sent == [] and store.of("info_held")[0]["reason"] == "too late to be useful"


def test_headline_once_per_event_then_linked_denial_update(tmp_path):
    t1 = T0 + timedelta(hours=3)                       # 10:00 ET
    ln, store, sent = lane(tmp_path, info_briefs_et=[])
    for m in range(0, 70, 1):                          # price history for the reaction
        ln.record_mid(84.0 + m * 0.001, t1 - timedelta(minutes=60 - m))
    a = item("a", "OPEC+ weighs longer cuts", t1)
    dup = item("b", "OPEC+ weighs longer output cuts", t1 + timedelta(minutes=5), cluster="a", repeat_of="a")
    write_news(tmp_path, [a, dup], [{**CLS, "key": "a"}, {**CLS, "key": "b", "relation": "repeat"}])
    ln.step(quote(84.30), CTX, DAILY, str(tmp_path), t1 + timedelta(minutes=10))
    news_msgs = [s for s in sent if "| NEWS |" in s]
    assert len(news_msgs) == 1 and "OPEC+ weighs longer cuts" in news_msgs[0]
    assert "Price since first seen" in news_msgs[0] and "not proof" in news_msgs[0]
    assert "headline and snippet only" in news_msgs[0]
    assert not any(w in news_msgs[0] for w in TRADE_WORDS)
    # a denial in the same event: one linked UPDATE, not a fresh headline; never repeated
    den = item("c", "Saudi denies OPEC+ is weighing longer cuts", t1 + timedelta(minutes=30), cluster="a",
               repeat_of="a")
    write_news(tmp_path, [a, dup, den], [{**CLS, "key": "a"}, {**CLS, "key": "b", "relation": "repeat"},
                                         {**CLS, "key": "c", "relation": "denial", "status": "official",
                                          "bias": "bearish", "update_of_older_story": True}])
    for k in range(3):
        ln.step(quote(84.00), CTX, DAILY, str(tmp_path), t1 + timedelta(minutes=40 + k))
    upd = [s for s in sent if "UPDATE to BI-" in s]
    assert len(upd) == 1 and "DENIAL" in upd[0] and "does not by itself prove" in upd[0]
    assert len([s for s in sent if "| NEWS |" in s]) == 1


def test_routine_cap_and_spacing_but_updates_still_go_out(tmp_path):
    ln, store, sent = lane(tmp_path, info_briefs_et=[])
    now = T0 + timedelta(hours=4)
    assert ln.allowed("routine", now) is None
    ln.emit("level", "routine", "[BRENT INFO] {ID} | LEVEL", now, {})
    assert "45 min" in ln.allowed("routine", now + timedelta(minutes=10))
    ln.emit("level", "routine", "[BRENT INFO] {ID} | LEVEL", now + timedelta(minutes=50), {})
    assert "routine cap 2/day" in ln.allowed("routine", now + timedelta(hours=2))
    assert ln.allowed("update", now + timedelta(hours=2)) is None        # corrections not blocked
    for k in range(3):
        ln.emit("move", "urgent", "[BRENT INFO] {ID} | FAST MOVE", now + timedelta(minutes=k), {})
    assert ln.allowed("urgent", now) == "urgent ceiling reached"
    # a new New York day resets the counters
    assert ln.allowed("routine", now + timedelta(days=1)) is None


def test_fast_move_is_atr_normalised_and_not_repeated(tmp_path):
    ln, store, sent = lane(tmp_path, info_briefs_et=[])
    now = T0 + timedelta(hours=5)
    for m in range(61):
        ln.record_mid(84.0 + (1.2 if m > 50 else 0.0), now - timedelta(minutes=60 - m))
    write_news(tmp_path, [], [])
    ln.step(quote(85.2), CTX, DAILY, str(tmp_path), now)
    ln.step(quote(85.3), CTX, DAILY, str(tmp_path), now + timedelta(minutes=5))
    moves = [s for s in sent if "FAST MOVE UP" in s]
    assert len(moves) == 1 and "+0.60 daily ATR" in moves[0] and "Cause not established" in moves[0]


def test_small_move_is_not_a_fast_move(tmp_path):
    ln, store, sent = lane(tmp_path, info_briefs_et=[])
    now = T0 + timedelta(hours=5)
    for m in range(61):
        ln.record_mid(84.0 + 0.01 * m, now - timedelta(minutes=60 - m))
    write_news(tmp_path, [], [])
    ln.step(quote(84.6), CTX, DAILY, str(tmp_path), now)
    assert not [s for s in sent if "FAST MOVE" in s]


def test_level_needs_fifteen_minutes_beyond_it(tmp_path):
    ln, store, sent = lane(tmp_path, info_briefs_et=[])
    now = T0 + timedelta(hours=6)
    write_news(tmp_path, [], [])
    for m in range(16):
        ln.record_mid(85.3, now - timedelta(minutes=15 - m))
    ln.step(quote(85.3), {**CTX, "pdh": 85.0}, DAILY[:5], str(tmp_path), now)
    lv = [s for s in sent if "| LEVEL |" in s]
    assert len(lv) == 1 and "held above yesterday's high 85.00" in lv[0]
    ln.step(quote(85.3), {**CTX, "pdh": 85.0}, DAILY[:5], str(tmp_path), now + timedelta(hours=1))
    assert len([s for s in sent if "| LEVEL |" in s]) == 1


def test_outside_the_trade_range_is_labelled(tmp_path):
    ln, store, sent = lane(tmp_path)
    write_news(tmp_path, [], [])
    ln.step(quote(78.5), {**CTX, "pdh": 80.0, "pdl": 78.0}, DAILY, str(tmp_path), T0 + timedelta(minutes=1))
    assert "outside the 80-115 range, where no trade alerts are made" in sent[0]


def test_eia_countdown_is_flagged_in_holiday_weeks():
    at, shifted = info.next_eia(datetime(2026, 11, 2, 15, tzinfo=UTC))
    assert at.isoformat().startswith("2026-11-04T10:30") and not shifted
    at, shifted = info.next_eia(datetime(2026, 9, 8, 15, tzinfo=UTC))      # Labor Day week
    assert shifted
    assert "2026-11-26" in info.us_federal_holidays(2026)                  # Thanksgiving


def test_weekends_and_brent_holidays_are_quiet(tmp_path):
    ln, store, sent = lane(tmp_path)
    write_news(tmp_path, [], [])
    sat = datetime(2026, 11, 7, 12, 1, tzinfo=UTC)
    xmas = datetime(2026, 12, 25, 12, 1, tzinfo=UTC)
    ln.step(quote(84.0), CTX, DAILY, str(tmp_path), sat)
    ln.step(quote(84.0), CTX, DAILY, str(tmp_path), xmas)
    assert sent == []


def test_info_lane_never_creates_trades_inside_the_monitor(tmp_path):
    from tests.test_brent_alerts import arm_setup, make

    # same market path with the information lane on and off: identical trade-side records
    def run(folder, on):
        mon, client, sender, clock, store = make(folder, info_alerts=on, info_briefs_et=["09:01"])
        arm_setup(mon, client, clock)
        for _ in range(10):
            clock.advance(30)
            mon.tick()
        trade = [t for t in sender.texts if not t.startswith("[BRENT INFO]")]
        return mon, sender, store, trade

    mon_on, sender_on, store_on, trade_on = run(tmp_path / "on", True)
    _, _, _, trade_off = run(tmp_path / "off", False)
    infos = [t for t in sender_on.texts if t.startswith("[BRENT INFO]")]
    assert len(infos) == 1 and "09:01 ET BRIEF" in infos[0]       # the lane did send
    assert trade_on == trade_off                                    # and changed nothing else
    assert not any(str(e.get("id", "")).startswith("BI-") for e in store_on.events()
                   if e["event"] in ("signal", "paper_fill", "watch", "candidate"))


def test_second_opinion_is_bounded_logged_and_timestamped(tmp_path):
    from tradingagents.brent import news, secondopinion
    from tradingagents.brent.store import Store

    store = Store(tmp_path)
    store.write("signal", id="BS-1", setup={"direction": "LONG"}, plan={}, quote={})
    (tmp_path / "news.jsonl").write_text("")
    t = datetime(2026, 11, 5, 19, tzinfo=UTC)                       # 14:00 ET, outside the quiet window

    def model(system, user):
        assert "<record>" in user and "do not trade" in system
        return '{"verdict": "disagree", "reason": "Room to the next level is small."}', {}

    row = secondopinion.review(store, "BS-1", model, news.Budget(12, 250), tmp_path, now=t)
    assert row["verdict"] == "disagree" and row["prospective"] is True
    assert "call_started" in (tmp_path / "news.jsonl").read_text()     # counted in the shared budget
    quiet = secondopinion.review(store, "BS-1", model, news.Budget(12, 250), tmp_path,
                                 now=datetime(2026, 11, 5, 13, tzinfo=UTC))   # 08:00 ET
    assert quiet["refused"].startswith("quiet window") and quiet["verdict"] is None
    assert len([e for e in store.events() if e["event"] == "second_opinion"]) == 2


def test_info_test_retries_after_a_failed_send_but_never_after_an_accepted_one(tmp_path, monkeypatch):
    from tradingagents.brent import __main__ as cli
    from tradingagents.brent.config import BrentConfig

    cfg = BrentConfig(journal_dir=str(tmp_path))
    results = iter([(False, 0.1), (True, 0.1)])
    sent = []

    def sender(c):
        def send(text):
            sent.append(text)
            return next(results)
        return send

    monkeypatch.setattr(cli, "_sender", sender)
    assert cli.cmd_info_test(None, cfg) == 1          # notify failed: nothing reached the chat
    assert cli.cmd_info_test(None, cfg) == 0          # so one retry is allowed
    assert cli.cmd_info_test(None, cfg) == 3 and len(sent) == 2   # accepted once: never again
