"""Feedback on sent Brent messages, and the weekly review built from the journals.

Feedback is append-only (``feedback.jsonl`` in the Brent folder): a later answer revises an
earlier one but the history stays. No answer is "no answer", never negative feedback. No
feedback value can place, change or cancel anything; it is a record only.

The weekly review is descriptive. INFO messages carry no directional prediction, so they are
graded for timeliness, novelty and usefulness (your feedback), not for price direction. ENTRY
results come from the existing cost-aware paper model and are kept apart from delivery and from
what you reported doing. Big moves without a prior message are listed as retrospective context
only: no "missed opportunity" universe was defined in advance, so they are not scored as misses.
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

VALUES = {"useful": "Useful", "not_useful": "Not useful", "too_late": "Too late"}
ENTRY_VALUES = {"took_it": "Took it", "skipped": "Skipped"}
FEEDBACK_VERSION = "brent-feedback-1"


def _path(folder: str | Path) -> Path:
    return Path(folder) / "feedback.jsonl"


def sent_messages(events: list[dict], since: datetime) -> list[dict]:
    """Messages that were actually accepted for delivery since ``since``: INFO, WATCH, ENTRY."""
    out, seen = [], set()
    for e in events:
        ts = e.get("ts")
        if not ts or datetime.fromisoformat(ts) < since:
            continue
        if e["event"] == "info" and e.get("sent") and e.get("accepted"):
            mid, kind, text = e["id"], f"INFO {e.get('kind')}", e.get("text", "")
        elif e["event"] == "watch_notice" and e.get("accepted"):
            mid, kind, text = e["id"], "WATCH", e.get("blocker", "")
        elif e["event"] == "signal" and e.get("delivered") and e.get("arm", "combined") == "combined":
            mid, kind, text = e["id"], "ENTRY", (e.get("setup") or {}).get("direction", "")
        else:
            continue
        if mid in seen:
            continue
        seen.add(mid)
        out.append({"id": mid, "kind": kind, "ts": ts, "summary": summary_of(text)})
    return out


def summary_of(text: str) -> str:
    """Short plain summary of a sent text: card style (title, first rows) or the old style (line 2)."""
    from .messages import DIV

    if DIV in text:
        card = html.unescape(re.sub(r"<[^>]+>", "", text.split("<blockquote")[0]))
        lines = [ln.strip() for ln in card.splitlines() if ln.strip() and ln.strip() != DIV]
        return " · ".join(lines[:3])[:160]
    return text.splitlines()[1][:160] if "\n" in text else text[:160]


def record(folder: str | Path, user: str, message_id: str, kind: str, value: str, now: datetime) -> dict:
    allowed = {**VALUES, **(ENTRY_VALUES if kind == "ENTRY" else {})}
    if value not in allowed:
        raise ValueError(f"feedback value {value!r} not allowed for {kind}")
    row = {"ts": now.isoformat(timespec="seconds"), "user": user, "id": message_id, "kind": kind,
           "value": value, "version": FEEDBACK_VERSION}
    p = _path(folder)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def history(folder: str | Path) -> list[dict]:
    p = _path(folder)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def latest(folder: str | Path) -> dict[str, dict]:
    """Latest answer per (message, answer group). Usefulness and Took/Skipped are separate."""
    out: dict[str, dict] = {}
    for r in history(folder):
        group = "action" if r["value"] in ENTRY_VALUES else "rating"
        out[f"{r['id']}|{group}"] = r
    return out


def weekly(events: list[dict], fb: dict[str, dict], now: datetime, days: int = 7) -> dict:
    since = now - timedelta(days=days)
    ev = [e for e in events if e.get("ts") and datetime.fromisoformat(e["ts"]) >= since]
    infos = [e for e in ev if e["event"] == "info"]
    held = Counter(e.get("reason", "?").split(" (")[0] for e in ev if e["event"] == "info_held")
    sent = sent_messages(events, since)
    ratings = {k.split("|")[0]: v["value"] for k, v in fb.items() if k.endswith("|rating")}
    actions = {k.split("|")[0]: v["value"] for k, v in fb.items() if k.endswith("|action")}
    by_kind: dict[str, Counter] = {}
    for m in sent:
        c = by_kind.setdefault(m["kind"], Counter())
        c[ratings.get(m["id"], "no answer")] += 1
    days_active = len({e["ts"][:10] for e in infos if e.get("sent")})
    entries = [e for e in ev if e["event"] == "signal" and e.get("arm", "combined") == "combined"]
    closed = [e for e in ev if e["event"] == "paper_exit"]
    moves = [e for e in infos if e.get("kind") == "move"]
    heads = [e for e in infos if e.get("kind") == "headline"]
    context = []
    for m in moves:
        t = datetime.fromisoformat(m["ts"])
        before = [h for h in heads if timedelta(0) <= t - datetime.fromisoformat(h["ts"]) <= timedelta(hours=2)]
        if not before:
            context.append({"at": m["ts"], "change": m.get("change")})
    return {
        "window": f"{since.date()} to {now.date()}", "version": FEEDBACK_VERSION,
        "info": {"sent": sum(1 for e in infos if e.get("sent") and e.get("accepted")),
                 "send_failed": sum(1 for e in infos if e.get("sent") and not e.get("accepted")),
                 "shadow_only": sum(1 for e in infos if e.get("shadow")),
                 "by_kind": dict(Counter(e.get("kind") for e in infos)),
                 "held_by_reason": dict(held), "days_with_info": days_active},
        "trade_lane": {"watch_accepted": sum(1 for m in sent if m["kind"] == "WATCH"),
                       "entry_decisions": len(entries),
                       "entry_delivered": sum(1 for e in entries if e.get("delivered")),
                       "paper_closed": len(closed),
                       "paper_net_r_after_costs": [e["net_r"] for e in closed if e.get("net_r") is not None],
                       "you_reported": dict(Counter(actions.values()))},
        "feedback": {k: dict(v) for k, v in by_kind.items()},
        "directional_scoring": "none: INFO messages record no prospective directional hypothesis",
        "retrospective_context": {"fast_moves_without_news_info_in_prior_2h": context,
                                  "note": "context only; not scored as misses (no eligible "
                                          "opportunity set was defined in advance)"},
    }


def weekly_text(w: dict, preview: bool = False) -> str:
    i, t = w["info"], w["trade_lane"]
    head = "[BRENT WEEKLY]" + (" PREVIEW (not the scheduled review)" if preview else "")
    lines = [f"{head} {w['window']}",
             f"INFO sent {i['sent']} ({', '.join(f'{k} {v}' for k, v in sorted(i['by_kind'].items())) or 'none'}); "
             f"failed sends {i['send_failed']}; shadow only {i['shadow_only']}; days with INFO {i['days_with_info']}.",
             "Held back: " + (", ".join(f"{k} {v}" for k, v in i["held_by_reason"].items()) or "none") + ".",
             f"Trade lane: WATCH {t['watch_accepted']}, ENTRY decided {t['entry_decisions']} "
             f"(delivered {t['entry_delivered']}), paper closed {t['paper_closed']}"
             + (f", net R after costs {t['paper_net_r_after_costs']}" if t["paper_net_r_after_costs"] else "") + ".",
             "Your feedback: " + ("; ".join(f"{k}: " + ", ".join(f"{a} {b}" for a, b in v.items())
                                            for k, v in w["feedback"].items()) or "no messages to rate") + ".",
             "Direction: not scored (INFO makes no directional call). Small samples; no claim of skill.",
             f"Fast moves with no news INFO in the 2 h before: "
             f"{len(w['retrospective_context']['fast_moves_without_news_info_in_prior_2h'])} (context, not misses)."]
    return "\n".join(lines)
