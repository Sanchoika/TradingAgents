"""Readable publisher evidence for news items, with an honest status for every outcome.

Statuses (never blurred): ``full_text`` (publisher page fetched, article paragraphs extracted),
``snippet_only`` (no article link, or the page had no extractable article), ``redirect_unresolved``
(the link is a Google News redirect we could not resolve to a publisher), ``paywall`` (the page
signals a paywall: nothing extracted, no attempt to get around it) and ``failed`` (network or HTTP
error). A redirect page is never counted as an article. Only public pages are fetched with a plain
GET. Page text is untrusted data: it is length-capped and handed to the model as quoted data.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlparse

EVIDENCE_VERSION = "evidence-v1"
MAX_EXCERPT = 1500
MIN_ARTICLE_WORDS = 120
PAYWALL = re.compile(r"subscribe to (continue|read)|sign in to read|to continue reading|paywall|"
                     r"subscription required|already a subscriber", re.I)
_BLOCK = re.compile(r"<(script|style|nav|header|footer|aside|form|noscript|svg)\b.*?</\1>",
                    re.I | re.S)
_PARA = re.compile(r"<p\b[^>]*>(.*?)</p>", re.I | re.S)
_TAG = re.compile(r"<[^>]+>")
_URL = re.compile(rb"https?://[\x21-\x7e]+")


def is_google(host: str) -> bool:
    return host.endswith("news.google.com") or host.endswith("consent.google.com") \
        or host.endswith("google.com")


def decode_google_news_url(link: str) -> str | None:
    """Older Google News article tokens embed the publisher URL in base64; newer ones do not."""
    m = re.search(r"/(?:rss/)?articles/([A-Za-z0-9_\-]+)", link)
    if not m:
        return None
    token = m.group(1)
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except Exception:
        return None
    hit = _URL.search(raw)
    if not hit:
        return None
    url = hit.group(0).decode("ascii", "ignore")
    url = re.split(r"[\x00-\x20\"'<>]", url)[0]
    return url if urlparse(url).netloc and not is_google(urlparse(url).netloc) else None


def extract_text(html: str) -> str:
    """Article paragraphs only: scripts, navigation and boilerplate are dropped."""
    body = _BLOCK.sub(" ", html)
    paras = []
    for p in _PARA.findall(body):
        t = re.sub(r"\s+", " ", _TAG.sub(" ", p)).strip()
        if len(t) >= 60:
            paras.append(t)
    return "\n".join(paras)


def fetch_evidence(link: str, get: Callable, now: datetime, snippet: str = "") -> dict:
    out = {"version": EVIDENCE_VERSION, "checked_at": now.isoformat(), "link_host": "",
           "status": "snippet_only", "publisher_url": None, "http_status": None, "words": 0,
           "excerpt": "", "how_resolved": ""}
    if not link:
        return out
    out["link_host"] = urlparse(link).netloc
    url = link
    if is_google(out["link_host"]):
        dec = decode_google_news_url(link)
        if dec:
            url, out["how_resolved"] = dec, "token"
    try:
        r = get(url)
        final = getattr(r, "url", url) or url
        host = urlparse(final).netloc
        out["http_status"] = r.status_code
        if is_google(host):
            out["status"] = "redirect_unresolved"
            return out
        out["publisher_url"] = final
        if not out["how_resolved"]:
            out["how_resolved"] = "redirect" if final != link else "direct"
        if r.status_code != 200:
            out["status"] = "failed"
            return out
        page = r.text[:2_000_000]
        text = extract_text(page)
        words = len(re.findall(r"[A-Za-z]{3,}", text))
        out["words"] = words
        if PAYWALL.search(_TAG.sub(" ", page[:200_000])) and words < 3 * MIN_ARTICLE_WORDS:
            out["status"] = "paywall"
        elif words >= MIN_ARTICLE_WORDS:
            out["status"], out["excerpt"] = "full_text", text[:MAX_EXCERPT]
        else:
            out["status"] = "snippet_only"
    except Exception as exc:
        out["status"] = "failed"
        out["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return out
