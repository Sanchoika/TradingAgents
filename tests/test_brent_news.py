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
    # EIA files polled fine, but they are official data, not a news check (audit finding 3)
    # one direct publisher feed alone is not enough general-news coverage
    assert h["fresh_feeds"] == ["direct:oilprice", "eia:summary", "eia:table1"] and not h["fresh_check"]
    assert h["general_news_fresh"] == 1


def test_general_news_coverage_needs_two_news_feeds(tmp_path):
    col, clock = make(tmp_path)
    col.poll()
    assert col.feed_health()["fresh_check"] and col.feed_health()["general_news_fresh"] == 5
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
    # one out-of-range value (seen live: category "opinion") no longer fails the whole batch
    two = validate(json.dumps([{**GOOD, "key": "a", "category": "opinion"}, {**GOOD, "key": "b"}]), {"a", "b"})
    bad, good = two
    assert bad["invalid_output"].startswith("category='opinion'") and not bad["relevant"]
    assert good["bias"] == "bullish" and "invalid_output" not in good
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


def test_budget_defaults_and_rolling_boundaries():
    from datetime import UTC, datetime, timedelta

    from tradingagents.brent.config import BrentConfig

    cfg = BrentConfig()
    assert (cfg.llm_per_hour, cfg.llm_per_day) == (12, 250)
    b = Budget(cfg.llm_per_hour, cfg.llm_per_day)
    now = datetime(2026, 11, 7, 16, 0, tzinfo=UTC)  # Saturday: outside the weekday quiet window
    stamp = lambda mins: (now - timedelta(minutes=mins)).isoformat()  # noqa: E731
    assert b.refusal([stamp(m) for m in range(1, 12)], now) is None                    # 11 in the hour
    assert "hourly cap 12" in b.refusal([stamp(m) for m in range(1, 13)], now)         # 12th used
    assert b.refusal([stamp(60 + m) for m in range(12)], now) is None                  # all older than 1 h
    day = [stamp(60 + 5 * m) for m in range(249)]
    assert b.refusal(day, now) is None                                                 # 249 in 24 h
    assert "daily cap 250" in b.refusal([*day, stamp(60 + 5 * 249)], now)              # 250 used
    assert b.refusal([*day[:-1], stamp(24 * 60 + 1)], now) is None                     # one aged out


def test_quiet_window_still_blocks_weekday_mornings():
    from datetime import UTC, datetime

    b = Budget()
    wed_8am_et = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)
    assert "quiet window" in b.refusal([], wed_8am_et)


def test_urgent_reserve_batching_and_call_started_counting(tmp_path):
    from datetime import UTC, datetime, timedelta

    from tradingagents.brent import news

    b = Budget(per_hour=4, per_day=250, reserve_urgent=2)
    now = datetime(2026, 11, 7, 16, 0, tzinfo=UTC)   # Saturday
    two = [(now - timedelta(minutes=m)).isoformat() for m in (5, 10)]
    assert "kept for urgent" in b.refusal(two, now)             # 2 used = non-urgent cap
    assert b.refusal(two, now, urgent=True) is None              # urgent may use the reserve
    four = two + [(now - timedelta(minutes=m)).isoformat() for m in (15, 20)]
    assert b.refusal(four, now, urgent=True) == "hourly cap 4"

    assert news.is_urgent({"title": "OPEC+ agrees surprise output cut"})
    assert not news.is_urgent({"title": "Oil edges higher in thin trade"})
    fresh = {"first_seen": (now - timedelta(seconds=20)).isoformat(), "title": "Oil edges higher"}
    old = {**fresh, "first_seen": (now - timedelta(seconds=200)).isoformat()}
    assert news.should_wait([fresh], now) is True               # small, quiet, fresh: wait a little
    assert news.should_wait([old], now) is False                # waited long enough
    assert news.should_wait([{**fresh, "title": "Hormuz tanker attack"}], now) is False
    assert news.should_wait([fresh, fresh, fresh], now) is False

    ev = [{"event": "call_started", "call_id": "a", "ts": "2026-11-07T15:50:00+00:00"},
          {"event": "analysis", "called": True, "call_id": "a", "ts": "2026-11-07T15:50:03+00:00"},
          {"event": "analysis", "called": True, "ts": "2026-11-07T15:40:00+00:00"}]   # old record
    assert news.count_calls(ev) == ["2026-11-07T15:40:00+00:00", "2026-11-07T15:50:00+00:00"]


def test_analyze_writes_call_started_and_can_batch_wait(tmp_path):
    col, clock = make(tmp_path)
    col.poll()
    keys = [e["key"] for e in events(col) if e["event"] == "item" and not e["feed"].startswith("eia")]
    model = lambda s, u: (json.dumps([{**GOOD, "key": k} for k in keys]), {})  # noqa: E731
    waited = analyze(col, model, Budget(), batch_wait_s=90)
    assert waited["called"] is False and "batching" in waited["note"]    # one fresh quiet item
    assert not [e for e in events(col) if e["event"] == "call_started"]
    clock.t += timedelta(seconds=120)
    res = analyze(col, model, Budget(), batch_wait_s=90)
    assert res["called"]
    started = [e for e in events(col) if e["event"] == "call_started"]
    assert len(started) == 1 and res["call_id"] == started[0]["call_id"]


def test_source_backoff_is_per_source_and_recovers(tmp_path):
    from datetime import UTC, datetime, timedelta

    from tradingagents.brent import news

    assert [news.backoff_seconds(n) for n in (1, 2, 3, 4, 9)] == [0, 0, 60, 120, 900]
    clock = [datetime(2026, 11, 5, 12, tzinfo=UTC)]
    calls = {"a": 0, "b": 0}
    c = news.NewsCollector(tmp_path, get=lambda u: None, now=lambda: clock[0], yahoo=None)
    bad = {"on": True}

    def a():
        calls["a"] += 1
        if bad["on"]:
            raise OSError("x")
        return []

    def b():
        calls["b"] += 1
        return []
    c.sources = lambda: [("a", a), ("b", b)]
    for _ in range(4):
        c.poll()
        clock[0] += timedelta(seconds=30)
    assert calls["b"] == 4 and calls["a"] == 3     # 4th pass skipped: backing off
    bad["on"] = False
    clock[0] += timedelta(minutes=5)
    c.poll()
    assert c.st["feeds"]["a"]["streak"] == 0 and c.st["feeds"]["a"]["next_try"] is None


def test_read_since_matches_full_read_across_chunk_boundaries(tmp_path):
    import json
    from datetime import UTC, datetime, timedelta

    from tradingagents.brent.news import read_since

    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    p = tmp_path / "news.jsonl"
    rows = [{"ts": (t0 + timedelta(minutes=i)).isoformat(), "event": "x", "i": i, "pad": "é" * 50}
            for i in range(500)]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    since = t0 + timedelta(minutes=123)
    for chunk in (64, 1000, 1 << 20):
        got = read_since(p, since, chunk=chunk)
        assert [e["i"] for e in got] == list(range(123, 500))
    assert read_since(tmp_path / "none.jsonl", since) == []


def test_rotate_archives_old_events_and_keeps_recent(tmp_path):
    import gzip
    import json
    from datetime import UTC, datetime, timedelta

    from tradingagents.brent import news

    now = datetime(2026, 11, 5, 12, tzinfo=UTC)
    c = news.NewsCollector(tmp_path, get=lambda u: None, now=lambda: now, yahoo=None)
    old1 = (now - timedelta(days=40)).isoformat()
    old2 = (now - timedelta(days=20)).isoformat()
    new = (now - timedelta(days=2)).isoformat()
    c.log_path.write_text("\n".join(json.dumps(r) for r in (
        {"ts": old1, "event": "x", "n": 1}, {"ts": old2, "event": "x", "n": 2},
        {"ts": new, "event": "x", "n": 3})) + "\nnot json at all\n", encoding="utf-8")
    res = c.rotate()
    assert res == {"moved": 2, "kept": 2}                       # recent event + the unparseable line
    live = [x for x in c.log_path.read_text().splitlines() if x]
    assert any('"n": 3' in x for x in live) and "not json at all" in live
    names = {p.name for p in (tmp_path / "news-archive").glob("*.gz")}
    assert names == {"2026-09.jsonl.gz", "2026-10.jsonl.gz"}
    with gzip.open(tmp_path / "news-archive" / "2026-09.jsonl.gz", "rt") as fh:
        assert '"n": 1' in fh.read()
    assert c.rotate()["moved"] == 0                             # idempotent
    assert c.maybe_rotate() is not None and c.maybe_rotate() is None   # once a day


class HTTP403(Exception):
    def __init__(self):
        super().__init__("403 Client Error: Forbidden")
        self.response = Resp(status=403)


def test_eia_403_before_release_is_waiting_not_failure(tmp_path):
    col, clock = make(tmp_path)

    def get(url):
        if "ir.eia.gov" in url:
            raise HTTP403()
        return Resp(rss(("Brent steady", "Wed, 04 Nov 2026 14:50:00 GMT")))

    col.get = get
    clock.t = datetime(2026, 11, 4, 15, 0, tzinfo=UTC)   # Wed 10:00 ET, before the release
    col.poll()
    ev = events(col)
    assert sum(e["event"] == "eia_not_released" for e in ev) == 2
    assert not any(e["event"] == "fetch_error" and e["feed"].startswith("eia:") for e in ev)
    assert col.st["feeds"]["eia:summary"].get("streak", 0) == 0
    clock.t = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)   # Thu is in the window too (holiday-week releases)
    col.poll()
    clock.t = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)   # Fri: a 403 is a real failure
    col.poll()
    assert col.st["feeds"]["eia:summary"]["streak"] == 1
    assert sum(e["event"] == "fetch_error" and e["feed"] == "eia:summary" for e in events(col)) == 1


def test_yahoo_feed_is_off_by_default(tmp_path):
    feeds = [f for f, _ in NewsCollector(tmp_path, get=lambda u: None).sources()]
    assert "yahoo" not in feeds and "google:brent" in feeds
    on = [f for f, _ in NewsCollector(tmp_path, get=lambda u: None, yahoo=news.fetch_yahoo).sources()]
    assert "yahoo" in on


def test_quiet_window_shadow_logs_reserved_capacity_but_never_calls(tmp_path):
    wed_8am = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)
    col, clock = make(tmp_path, google_text=rss(("OPEC+ agrees output cut - Reuters", "Wed, 04 Nov 2026 12:50:00 GMT")))
    clock.t = wed_8am
    col.poll()

    def model(s, u):
        raise AssertionError("no model call inside the quiet window")

    first = analyze(col, model, Budget())
    assert first["refused"].startswith("quiet window")
    assert first["quiet_shadow"]["would_call"] and first["quiet_shadow"]["urgent_waiting"] == 1
    clock.t += timedelta(minutes=5)
    again = analyze(col, model, Budget())   # same urgent item: the shadow serves it only once
    assert again["quiet_shadow"]["would_call"] is False and again["quiet_shadow"]["urgent_waiting"] == 0
    use = news.budget_usage(tmp_path, clock.t, 6, 250, ("07:30", "10:30"))
    assert use["quiet_shadow_24h"]["would_call"] == 1 and use["quiet_shadow_24h"]["urgent_items"] == 1
    assert not [e for e in events(col) if e["event"] == "call_started"]


def test_denial_of_a_similar_story_is_material_not_a_repeat(tmp_path):
    # audit finding 1: headline similarity is event membership, not a repeat veto
    col, clock = make(tmp_path, google_text=rss(("Saudi Arabia plans to raise oil output - Reuters",
                                                 "Tue, 03 Nov 2026 18:50:00 GMT")))
    col.poll()
    clock.t += timedelta(hours=3)
    col.get = lambda url: Resp(rss(("Saudi Arabia denies plans to raise oil output - Reuters",
                                    "Tue, 03 Nov 2026 21:50:00 GMT"), base="d")) \
        if "google" in url else Resp("x")
    col.poll()
    items = [e for e in events(col) if e["event"] == "item" and e["feed"].startswith("google")]
    first, denial = items[0], items[-1]
    assert denial["repeat_of"] == first["key"]          # same event cluster
    seen = []

    def model(system, user):
        seen.append(user)
        keys = [i["key"] for i in json.loads(user.split("\n")[1])]
        return json.dumps([{**GOOD, "key": k, "bias": "bearish", "update_of_older_story": True,
                            "relation": "denial"} for k in keys]), {}

    res = analyze(col, model, Budget())
    assert "Saudi Arabia plans to raise oil output" in seen[0]   # the model sees the earlier headline
    c = next(c for c in res["parsed"] if c["key"] == denial["key"])
    assert news.is_material(c, denial, clock.t, 6) and news.changes_event(c, denial)
    # an identical re-post (relation "repeat") is still not material
    assert not news.is_material({**c, "relation": "repeat"}, denial, clock.t, 6)
    # old classifications without "relation" keep the old rule
    legacy = {k: v for k, v in c.items() if k != "relation"}
    assert not news.is_material(legacy, denial, clock.t, 6)


def test_relation_value_is_checked():
    assert validate(json.dumps([{**GOOD, "key": "a", "relation": "correction"}]), {"a"})
    assert validate(json.dumps([{**GOOD, "key": "a", "relation": "debunked"}]), {"a"})[0]["invalid_output"]


def test_batch_ranks_whole_backlog_and_reads_summary():
    # audit finding 3: urgency is judged on headline AND summary, before the batch is cut
    def item(n, title="Oil traders watch the dollar", summary=""):
        return {"key": f"k{n}", "title": title, "summary": summary,
                "first_seen": (T0 + timedelta(minutes=n)).isoformat()}

    routine = [item(n) for n in range(25)]
    urgent = item(30, title="Markets update", summary="A drone struck a Saudi refinery overnight")
    assert news.is_urgent(urgent)
    batch = news.select_batch(routine + [urgent], 20)
    keys = [i["key"] for i in batch]
    assert "k30" in keys and len(batch) == 20
    assert keys == sorted(keys, key=lambda k: int(k[1:]))        # oldest first within the batch
    # a flood of urgent items cannot starve routine news: the oldest routine items keep a share
    flood = [item(100 + n, title="Tanker attacked") for n in range(30)]
    batch = news.select_batch(routine + flood, 20)
    assert sum(not news.is_urgent(i) for i in batch) == 5 and batch[0]["key"] == "k0"


def test_evidence_summary_separates_what_was_read(tmp_path):
    from tradingagents.brent.news import evidence_summary

    ev = [{"event": "item", "key": "a", "feed": "google:brent", "link": "https://news.google.com/x"},
          {"event": "item", "key": "b", "feed": "google:brent", "link": "https://reuters.com/y"},
          {"event": "item", "key": "c", "feed": "eia:summary", "link": "https://ir.eia.gov/z"},
          {"event": "item", "key": "d", "feed": "google:opec", "link": "https://news.google.com/w"},
          {"event": "evidence", "key": "a", "status": "redirect_unresolved", "link_host": "news.google.com"},
          {"event": "evidence", "key": "b", "status": "full_text", "link_host": "reuters.com"}]
    s = evidence_summary(ev)
    assert (s["full_text"], s["official_structured"], s["redirect_unresolved"], s["not_attempted"]) == (1, 1, 1, 1)
    assert s["not_read_by_host"] == {"news.google.com": 1}


def test_direct_publisher_feed_keeps_article_links_and_skips_old_items():
    from tradingagents.brent.news import fetch_direct

    body = rss(("Oil jumps on supply fears", "Tue, 03 Nov 2026 18:50:00 GMT"),
               ("Old story", "Fri, 30 Oct 2026 10:00:00 GMT"))
    items = fetch_direct(lambda url: Resp(body), "direct:oilprice", "https://oilprice.com/rss/main",
                         "OilPrice.com", now=T0)
    assert [i.title for i in items] == ["Oil jumps on supply fears"]
    assert items[0].link.startswith("https://ex.com/") and items[0].feed == "direct:oilprice"
