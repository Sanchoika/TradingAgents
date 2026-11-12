"""News brain v1: measured only (headline -> Brent move), never changes a grade. Fakes only."""
import json
from datetime import UTC, datetime, timedelta

from tests.test_brent_alerts import make
from tradingagents.brent import outcomes

T = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)


def bar(i, mid):
    return {"time": T + timedelta(minutes=i), "complete": True, "bid_c": mid - 0.01, "ask_c": mid + 0.01}


def test_news_moves_in_atr_units_from_the_minute_before_first_seen():
    bars = [bar(i, 100 + i * 0.01) for i in range(-2, 250)]
    mv = outcomes.news_moves(bars, T + timedelta(seconds=30), 2.0)
    assert mv == {"m15": round(0.16 / 2, 3), "m60": round(0.61 / 2, 3), "m240": round(2.41 / 2, 3)}
    assert outcomes.news_moves(bars, T, 0) is None
    assert outcomes.news_moves(bars[300:], T, 2.0) is None


def test_each_classified_headline_is_scored_once_after_four_hours_and_grades_are_untouched(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    texts = list(sender.texts)
    now = clock()
    seen = now - timedelta(hours=4, minutes=5)
    folder = tmp_path / "brent"
    rows = [{"event": "item", "key": "k1", "feed": "google:oil", "title": "OPEC cuts", "publisher": "Reuters",
             "first_seen": seen.isoformat(), "link": "x"},
            {"event": "evidence", "key": "k1", "status": "full_text"},
            {"event": "analysis", "parsed": [{"key": "k1", "category": "opec_plus", "status": "credible_report",
             "event_time": None, "scope": "brent", "relevant": True, "mechanism": "supply", "bias": "bullish",
             "update_of_older_story": False, "relation": "new"}]}]
    (folder / "news.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    (folder / "news_state.json").write_text("{}")
    base = seen.replace(second=0, microsecond=0)
    client.candles = lambda inst, g, n: [{"time": base + timedelta(minutes=i), "complete": True,
                                          "bid_c": 100 + i * 0.002, "ask_c": 100.02 + i * 0.002} for i in range(-3, 300)]
    mon.st["news_outcomes_at"] = None
    mon.guard_news_outcomes(now)
    mon.st["news_outcomes_at"] = None
    mon.guard_news_outcomes(now)
    ev = [e for e in store.events() if e["event"] == "news_outcome"]
    assert len(ev) == 1 and ev[0]["publisher"] == "Reuters" and ev[0]["evidence"] == "full_text"
    assert ev[0]["category"] == "opec_plus" and ev[0]["signed_m60"] > 0 and ev[0]["moves"]["m240"] > ev[0]["moves"]["m60"]
    assert sender.texts == texts                     # nothing sent


def test_research_only_desks_do_no_news_measurement(tmp_path):
    from tests.test_brent_markets import market
    mon, sender, store, clock = market(tmp_path)
    mon.tick()
    assert "news_outcomes_at" not in mon.st
