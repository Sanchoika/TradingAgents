"""Event ledger for Brent news (shadow, derived from the append-only news log).

Items are linked into an event by the collector's cluster id only; words are never re-matched
here, so two distinct stories are not merged because they sound alike. Each linked item gets an
explicit relationship, assigned by code from recorded fields:

  original       first item of the event (by first receipt)
  repetition     later item from the same publisher, or one the model marked as a repeat/update
                 with the same bias
  corroboration  later item from a different publisher, status official/credible_report
  conflicting    later item whose classified bias is opposite to the original's
                 (an explicit disagreement inside one event, not a verified denial)

Denial, supersession and resolution are NOT inferred from wording: they need an explicit
relationship with evidence, so they are left to a human or a later versioned policy. Observed
price moves after first receipt are descriptive only and never claimed as causes.
Nothing here controls ENTRY decisions. Version it, then compare it prospectively.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

EVENTS_VERSION = "brent-events-shadow-1"
RESPONSE_MINUTES = (15, 60, 240)


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s)


def build_events(rows: list[dict], price_at: Callable[[datetime], float | None] | None = None,
                 now: datetime | None = None) -> list[dict]:
    items = {r["key"]: r for r in rows if r.get("event") == "item"}
    cls: dict[str, dict] = {}
    for r in rows:
        if r.get("event") == "analysis":
            for c in r.get("parsed") or []:
                cls[c["key"]] = c
    groups: dict[str, list[dict]] = {}
    for it in items.values():
        groups.setdefault(it.get("cluster") or it["key"], []).append(it)
    out = []
    for cluster, members in groups.items():
        members.sort(key=lambda m: m["first_seen"])
        first = members[0]
        fc = cls.get(first["key"])
        links = []
        for m in members:
            c = cls.get(m["key"])
            if m is first:
                rel = "original"
            elif c is None or fc is None:
                rel = "repetition" if m.get("publisher") == first.get("publisher") else "unclassified"
            elif c["bias"] != "unclear" and fc["bias"] != "unclear" and c["bias"] != fc["bias"]:
                rel = "conflicting"
            elif m.get("publisher") != first.get("publisher") and c["status"] in (
                    "official", "credible_report"):
                rel = "corroboration"
            else:
                rel = "repetition"
            links.append({"key": m["key"], "relation": rel, "publisher": m.get("publisher"),
                          "title": m["title"], "published": m.get("published"),
                          "first_seen": m["first_seen"], "classified": c is not None})
        ev = {"version": EVENTS_VERSION, "event_id": cluster, "first_receipt": first["first_seen"],
              "original_published": first.get("published"), "items": len(members),
              "publishers": sorted({m.get("publisher") or "?" for m in members}),
              "links": links, "classification": fc,
              "relations": {r: sum(link["relation"] == r for link in links)
                            for r in ("original", "repetition", "corroboration", "conflicting",
                                      "unclassified")},
              "unresolved": ["denial/supersession/resolution need an explicit evidenced link"],
              "price_response": None}
        if price_at is not None and fc and fc.get("relevant"):
            t0 = _t(first["first_seen"])
            base = price_at(t0)
            resp = {}
            for mins in RESPONSE_MINUTES:
                t1 = t0 + timedelta(minutes=mins)
                if base is None or (now and t1 > now):
                    resp[f"{mins}m"] = None
                    continue
                p = price_at(t1)
                resp[f"{mins}m"] = None if p is None else round(p - base, 3)
            ev["price_response"] = {"basis": "price change after first receipt; descriptive, "
                                             "not causal", "base": base, "moves": resp}
        out.append(ev)
    out.sort(key=lambda e: e["first_receipt"])
    return out
