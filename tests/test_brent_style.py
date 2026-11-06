"""Brent Telegram card style (6A): valid Telegram HTML, everything escaped, Brent always named."""

from datetime import UTC, datetime
from html.parser import HTMLParser

from tradingagents.brent import messages
from tradingagents.brent.__main__ import sample_text

ALLOWED = {"b", "i", "code", "a", "blockquote"}


class Check(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.stack, self.bad = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED:
            self.bad.append(tag)
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.bad.append("/" + tag)


def valid(text: str) -> bool:
    c = Check()
    c.feed(text)
    c.close()
    return not c.bad and not c.stack


NOW = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)


def test_every_notice_is_valid_html_and_names_brent():
    texts = [
        messages.invalidated_text("BR-1", NOW, "spread 0.12 > 0.08 & ask < zone"),
        messages.expired_text("BR-1", NOW),
        messages.range_text(121.0, 100, 120, False),
        messages.range_text(110.0, 100, 120, True),
        messages.ops_text("quote <stale> & feed down"),
        messages.start_text("brent-v1", "abc", "account <x>", "exp", True),
        messages.switch_text(False, {}),
        messages.watch_end_text("BW-1", NOW, "close < 110 & falling"),
        sample_text(),
    ]
    for t in texts:
        assert valid(t), t
        assert "BRENT" in t.split("\n")[0]
    assert "&lt;stale&gt; &amp; feed" in texts[4]


def test_card_escapes_values_and_hides_detail_in_a_collapsed_quote():
    t = messages.card("🟢", "LONG BRENT", [("Entry", "1<2")], "a & b", "foot",
                      ["detail <x>"], url="https://h.ts.net/a?b=1&c=2")
    assert "<code>1&lt;2</code>" in t and "<i>a &amp; b</i>" in t
    assert '<a href="https://h.ts.net/a?b=1&amp;c=2">report</a>' in t
    assert t.endswith("<blockquote expandable>detail &lt;x&gt;</blockquote>") and valid(t)
    assert "<a " not in messages.card("x", "BRENT", url="signals/BR-1.html in the Brent folder")


def test_sample_is_labelled_as_a_sample():
    t = sample_text()
    assert t.startswith("🟢 <b>SAMPLE · LONG BRENT</b>")
    assert "not a signal" in t and "no order" in t and "Valid until" in t


def test_feedback_page_summary_reads_the_card_not_the_divider():
    from tradingagents.brent.feedback import summary_of

    t = messages.ops_text("news feed silent 12 min & more")
    assert summary_of(t) == "🛠 BRENT PROBLEM · news feed silent 12 min & more"
    assert summary_of("[BRENT INFO] X | BRIEF\nBrent mid 84.10") == "Brent mid 84.10"
