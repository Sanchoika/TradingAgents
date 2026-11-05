from datetime import datetime

from tradingagents.brent.events import build_events


def item(key, cluster, pub, t, title="x"):
    return {"event": "item", "key": key, "cluster": cluster, "publisher": pub, "first_seen": t,
            "title": title, "published": t}


def cl(key, bias="bullish", status="credible_report", relevant=True):
    return {"key": key, "bias": bias, "status": status, "relevant": relevant}


def rows():
    return [
        item("a", "A", "Reuters", "2026-11-05T10:00:00+00:00"),
        item("b", "A", "Reuters", "2026-11-05T10:05:00+00:00"),
        item("c", "A", "Bloomberg", "2026-11-05T10:10:00+00:00"),
        item("d", "A", "FT", "2026-11-05T10:20:00+00:00"),
        item("e", "B", "Reuters", "2026-11-05T10:01:00+00:00", "unrelated opposing"),
        {"event": "analysis", "parsed": [cl("a"), cl("b"), cl("c"), cl("d", "bearish"),
                                         cl("e", "bearish")]},
    ]


def test_relations_and_separate_events():
    ev = {e["event_id"]: e for e in build_events(rows())}
    assert set(ev) == {"A", "B"}                      # similar-sounding but distinct: not merged
    rel = {link["key"]: link["relation"] for link in ev["A"]["links"]}
    assert rel == {"a": "original", "b": "repetition", "c": "corroboration", "d": "conflicting"}
    assert ev["B"]["relations"]["original"] == 1
    assert ev["A"]["unresolved"]                      # no denial is ever inferred


def test_price_response_descriptive_and_future_blank():
    now = datetime.fromisoformat("2026-11-05T10:30:00+00:00")
    px = lambda t: 100.0 + (t - datetime.fromisoformat("2026-11-05T10:00:00+00:00")).total_seconds() / 3600  # noqa: E731
    ev = build_events(rows(), price_at=px, now=now)[0]
    mv = ev["price_response"]["moves"]
    assert mv["15m"] == 0.25 and mv["60m"] is None and "not causal" in ev["price_response"]["basis"]


def test_unclassified_items_are_labelled():
    r = [item("a", "A", "X", "2026-11-05T10:00:00+00:00"), item("b", "A", "Y", "2026-11-05T10:01:00+00:00")]
    assert build_events(r)[0]["relations"]["unclassified"] == 1
