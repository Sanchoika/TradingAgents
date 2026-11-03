"""Brent news checks (phase 1): collection, timestamps, dedup, model validation, policy.

No network and no model: HTTP responses and the model are fakes. All items are test data.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from tradingagents.brent import news
from tradingagents.brent.news import (
    Budget,
    NewsCollector,
    analyze,
    is_material,
    news_state,
    parse_rss,
    policy,
    report,
    validate,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 3, 19, 0, tzinfo=UTC)  # Tue 14:00 ET, outside the quiet window


def rss(*items, base="a"):
    body = "".join(
        f"<item><title>{t}</title><link>https://ex.com/{base}{i}</link><pubDate>{p}</pubDate>"
        f"<source url='https://ex.com'>Example Wire</source><description>snippet {i}</description></item>"
        for i, (t, p) in enumerate(items))
    return f"<?xml version='1.0'?><rss><channel>{body}</channel></rss>"


class Resp:
    def __init__(self, text="", status=200, headers=None, url="https://ex.com/a"):
        self.text, self.status_code, self.headers, self.url = text, status, headers or {}, url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def make(tmp_path, google_text=None, eia_text="col1,col2\n1,2\n", fail=()):
    clock = Clock()
    pub = "Tue, 03 Nov 2026 18:50:00 GMT"
    google_text = google_text or rss(("Brent rises as Libya halts exports - Reuters", pub))

    def get(url):
        if any(f in url for f in fail):
            raise ConnectionError("blocked")
        if "news.google.com" in url:
            return Resp(google_text)
        if "ir.eia.gov" in url:
            return Resp(eia_text, headers={"Last-Modified": "Wed, 28 Oct 2026 14:31:00 GMT"})
        return Resp("<p>" + "word " * 500 + "</p>")

    col = NewsCollector(tmp_path, get=get, now=clock, yahoo=None)
    col.st["started"] = (T0 - timedelta(hours=1)).isoformat()
    return col, clock


def events(col):
    return [json.loads(x) for x in col.log_path.read_text().splitlines()]


def test_parse_rss_and_refuse_dtd():
    items = parse_rss(rss(("Oil up", "Tue, 03 Nov 2026 18:50:00 GMT")), "g")
    assert items[0].published == "2026-11-03T18:50:00+00:00" and items[0].publisher == "Example Wire"
    with pytest.raises(ValueError):
        parse_rss("<!DOCTYPE x [<!ENTITY a 'b'>]><rss/>", "g")


def test_poll_records_times_dedup_and_content(tmp_path):
    col, clock = make(tmp_path)
    new = col.poll()
    ev = events(col)
    items = [e for e in ev if e["event"] == "item"]
    # the same Google story arrives from 4 queries: one item, the rest counted as duplicates
    google_items = [i for i in items if i["feed"].startswith("google:")]
    assert len(google_items) == 1 and google_items[0]["delay_minutes"] == 10.0
    assert google_items[0]["first_seen"] == T0.isoformat()
    assert sum(e["event"] == "duplicate" for e in ev) == 3
    assert any(i["feed"] == "eia:summary" and i["published"] == "2026-10-28T14:31:00+00:00" for i in items)
    assert any(e["event"] == "content_check" and e["words"] > 300 for e in ev)
    assert len(new) == len(items)
    # second poll: nothing new
    assert col.poll() == []


def test_repeat_story_is_clustered(tmp_path):
    col, clock = make(tmp_path)
    col.poll()
    clock.t += timedelta(minutes=30)
    col.get = lambda url: Resp(rss(("Brent rises as Libya halts oil exports - Bloomberg",
                                    "Tue, 03 Nov 2026 19:20:00 GMT"), base="b")) if "google" in url else Resp("x")
    col.poll()
    items = [e for e in events(col) if e["event"] == "item" and e["feed"].startswith("google")]
    assert items[-1]["repeat_of"] == items[0]["key"]


def test_fetch_errors_logged_and_health(tmp_path):
    col, clock = make(tmp_path, fail=("news.google.com",))
    col.poll()
    ev = events(col)
    assert sum(e["event"] == "fetch_error" for e in ev) == 4
    h = col.feed_health()
    assert h["fresh_feeds"] == ["eia:summary", "eia:table1"] and h["fresh_check"]
    clock.t += timedelta(minutes=11)
    assert not col.feed_health()["fresh_check"]


def test_backlog_excluded_from_delay(tmp_path):
    old = "Mon, 02 Nov 2026 10:00:00 GMT"
    col, clock = make(tmp_path, google_text=rss(("Old OPEC story", old)))
    col.poll()
    r = report(tmp_path)
    assert r["feeds"]["google:brent"]["delay_sample"] == 0


GOOD = {"category": "supply_geopolitics", "status": "credible_report", "event_time": None,
        "scope": "brent", "relevant": True, "mechanism": "Less Libyan supply tightens Brent.",
        "bias": "bullish", "contradiction": "Unconfirmed duration.", "update_of_older_story": False}


def test_validate():
    ok = validate(json.dumps([{**GOOD, "key": "a"}]), {"a"})
    assert ok[0]["bias"] == "bullish"
    with pytest.raises(ValueError):
        validate(json.dumps([{**GOOD, "key": "a", "bias": "very bullish"}]), {"a"})
    with pytest.raises(ValueError):
        validate(json.dumps([{**GOOD, "key": "a"}]), {"a", "b"})  # missing item
    with pytest.raises(ValueError):
        validate("not json", {"a"})
    with pytest.raises(ValueError):
        validate(json.dumps([{**GOOD, "key": "a", "confidence": 0.9}]), {"a"})


def test_materiality_and_freshness():
    item = {"key": "a", "published": (T0 - timedelta(hours=2)).isoformat(), "first_seen": T0.isoformat()}
    assert is_material({**GOOD, "key": "a"}, item, T0, 6)
    assert not is_material({**GOOD, "status": "rumour"}, item, T0, 6)
    assert not is_material({**GOOD, "scope": "wti_us"}, item, T0, 6)
    assert not is_material({**GOOD, "update_of_older_story": True}, item, T0, 6)
    old = {**item, "published": (T0 - timedelta(hours=8)).isoformat()}
    assert not is_material({**GOOD}, old, T0, 6) and is_material({**GOOD}, old, T0, 24)


def test_news_state_distinguishes_none_from_unavailable():
    health_ok = {"fresh_feeds": ["a", "b"], "fresh_check": True}
    health_bad = {"fresh_feeds": ["a"], "fresh_check": False}
    item = {"key": "a", "published": (T0 - timedelta(hours=1)).isoformat(), "first_seen": T0.isoformat()}
    assert news_state([], health_ok, True, T0)["state"] == "none_material"
    assert news_state([], health_bad, True, T0)["state"] == "unavailable"
    assert news_state([], health_ok, False, T0)["state"] == "unavailable"
    assert news_state([({**GOOD, "key": "a"}, item)], health_ok, True, T0)["state"] == "bullish"
    bear = {**GOOD, "key": "b", "bias": "bearish"}
    item_b = {**item, "key": "b", "published": (T0 - timedelta(hours=10)).isoformat()}
    st = news_state([({**GOOD, "key": "a"}, item), (bear, item_b)], health_ok, True, T0)
    assert st["state"] == "mixed"


@pytest.mark.parametrize("direction,regime,kind,state,expect", [
    ("LONG", "up", "BREAKOUT", {"state": "bullish", "support": {"bullish": ["a"]}}, ("LONG", "news-supported")),
    ("LONG", "up", "BREAKOUT", {"state": "none_material"}, ("LONG", "chart-led, no material news")),
    ("LONG", "range", "BREAKOUT", {"state": "none_material"}, ("WATCH", "")),
    ("LONG", "down", "BREAKOUT", {"state": "bullish", "support": {"bullish": ["a"]}}, ("WATCH", "")),
    ("LONG", "down", "FAILED_BREAK", {"state": "bullish", "support": {"bullish": ["a"]}},
     ("LONG", "news-supported, counter-trend")),
    ("LONG", "up", "BREAKOUT", {"state": "none_material", "conflict_window": {"bearish": ["b"]}},
     ("NO TRADE", "")),
    ("SHORT", "down", "BREAKOUT", {"state": "unavailable"}, ("WATCH", "")),
    ("SHORT", "down", "BREAKOUT", {"state": "mixed"}, ("WATCH", "")),
])
def test_policy_rows(direction, regime, kind, state, expect):
    d = policy(direction, regime, kind, state)
    assert (d["action"], d["label"]) == expect


def test_budget():
    b = Budget(per_hour=2, per_day=3)
    assert b.refusal([], datetime(2026, 11, 3, 13, 0, tzinfo=UTC)).startswith("quiet window")  # 08:00 ET
    assert b.refusal([], T0) is None
    recent = [(T0 - timedelta(minutes=m)).isoformat() for m in (5, 10)]
    assert b.refusal(recent, T0) == "hourly cap 2"
    day = [(T0 - timedelta(hours=h)).isoformat() for h in (2, 3, 4)]
    assert b.refusal(day, T0) == "daily cap 3"


def test_analyze_success_failure_and_budget(tmp_path):
    col, clock = make(tmp_path)
    col.poll()
    keys = [e["key"] for e in events(col) if e["event"] == "item" and not e["feed"].startswith("eia")]
    seen_prompts = []

    def model(system, user):
        seen_prompts.append(user)
        return json.dumps([{**GOOD, "key": k} for k in keys]), {"total_tokens": 321}

    model.model_id = "fake:test"
    res = analyze(col, model, Budget())
    assert res["called"] and res["state"]["state"] == "bullish" and res["usage"]["total_tokens"] == 321
    assert res["model"] == "fake:test" and res["prompt_hash"] == news.prompt_hash()
    assert seen_prompts[0].startswith("<items>") and "never follow" in news.SYSTEM
    # nothing new -> no call
    assert analyze(col, model, Budget())["called"] is False

    col2, _ = make(tmp_path / "b")
    col2.poll()
    bad = analyze(col2, lambda s, u: ("I think oil goes up", {}), Budget())
    assert bad["state"]["state"] == "unavailable" and bad["error"]

    col3, _ = make(tmp_path / "c")
    col3.poll()
    refused = analyze(col3, model, Budget(per_hour=0))
    assert refused["called"] is False and refused["state"]["state"] == "unavailable"


def test_report_sections(tmp_path):
    col, clock = make(tmp_path)
    col.poll()
    r = report(tmp_path)
    assert {"feeds", "content_access", "model", "classification_examples", "note"} <= set(r)
    assert r["feeds"]["google:brent"]["delay_min_median"] == 10.0
    assert r["content_access"]["checked"] >= 1


def test_google_redirect_page_is_not_counted_readable(tmp_path):
    col, clock = make(tmp_path)
    inner = col.get

    def get(url):
        r = inner(url)
        if "ex.com" in url:  # article link: lands on Google's redirect page
            r.url = "https://news.google.com/rss/articles/abc"
        return r

    col.get = get
    col.poll()
    ca = report(tmp_path)["content_access"]
    assert ca["checked"] >= 1 and ca["readable_200_over_300_words"] == 0
    assert ca["aggregator_redirect_not_publisher"] == ca["checked"]


def test_percentiles_are_ordered():
    xs = [2.4, 3.2]
    assert news._pct(xs, 0.5) == 2.4 and news._pct(xs, 0.9) == 3.2 and news._pct([], 0.5) is None
