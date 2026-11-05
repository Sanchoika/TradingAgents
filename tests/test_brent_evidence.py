import base64
from datetime import UTC, datetime
from types import SimpleNamespace

from tradingagents.brent import evidence as ev

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
PARA = ("Oil prices rose on Thursday after inventories fell more than analysts expected, "
        "according to the agency report published this morning. ")


def page(n=12, extra=""):
    return "<html><script>var x=1</script>" + extra + "".join(f"<p>{PARA}</p>" for _ in range(n)) + "</html>"


def resp(url, text="", code=200):
    return SimpleNamespace(url=url, text=text, status_code=code)


def test_full_text_and_excerpt_cap():
    r = ev.fetch_evidence("https://pub.example/a", lambda u: resp(u, page()), NOW)
    assert r["status"] == "full_text" and len(r["excerpt"]) <= ev.MAX_EXCERPT
    assert "var x" not in r["excerpt"] and r["how_resolved"] == "direct"


def test_snippet_only_cases():
    assert ev.fetch_evidence("", lambda u: None, NOW)["status"] == "snippet_only"
    r = ev.fetch_evidence("https://pub.example/a", lambda u: resp(u, page(1)), NOW)
    assert r["status"] == "snippet_only" and r["excerpt"] == ""


def test_paywall_not_bypassed():
    r = ev.fetch_evidence("https://pub.example/a",
                          lambda u: resp(u, page(1, "<div>Subscribe to continue</div>")), NOW)
    assert r["status"] == "paywall" and r["excerpt"] == ""


def test_failed_status_and_exception():
    assert ev.fetch_evidence("https://pub.example/a", lambda u: resp(u, "", 503), NOW)["status"] == "failed"

    def boom(u):
        raise OSError("down")
    assert ev.fetch_evidence("https://pub.example/a", boom, NOW)["status"] == "failed"


def test_google_redirect_unresolved_and_token():
    link = "https://news.google.com/rss/articles/CBMiAAAA"
    r = ev.fetch_evidence(link, lambda u: resp("https://consent.google.com/x", "x"), NOW)
    assert r["status"] == "redirect_unresolved" and r["excerpt"] == ""
    tok = base64.urlsafe_b64encode(b"\x08\x13\x22\x20https://pub.example/story\xd2\x01").decode().rstrip("=")
    link = f"https://news.google.com/rss/articles/{tok}"
    assert ev.decode_google_news_url(link) == "https://pub.example/story"
    r = ev.fetch_evidence(link, lambda u: resp(u, page()), NOW)
    assert r["status"] == "full_text" and r["how_resolved"] == "token"
