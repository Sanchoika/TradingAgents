"""Failure notices that report state changes, not the same error every minute.

A problem is notified once it has persisted ``confirm`` consecutive checks, then reminded at
bounded intervals while unresolved (default after 1 h, 6 h and 24 h, then never again), and a
recovery notice goes out when it clears. State lives in a small JSON file next to the journal.
Problems are grouped into four kinds so the message says what is wrong:
process (a service is not running), data (inputs stale), model (classification failing) and
entry (new entries blocked for another reason).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

REMINDERS = (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))


def kind_of(reason: str) -> str:
    r = reason.lower()
    if "not running" in r:
        return "process"
    if "analysis" in r or "model" in r:
        return "model"
    if any(w in r for w in ("quote", "tick", "poll", "feeds", "chart data", "stale", "snapshot")):
        return "data"
    return "entry"


def step(path: str | Path, problems: list[str], now: datetime, confirm: int = 3) -> list[str]:
    """Update the notice state with this check's problems; return the messages to send."""
    p = Path(path)
    st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    open_ = st.setdefault("open", {})       # reason -> {first, seen, notified, reminders}
    out = []
    current = set(problems)
    for reason in problems:
        rec = open_.setdefault(reason, {"first": now.isoformat(), "seen": 0, "notified": None,
                                        "reminders": 0})
        rec["seen"] += 1
        if rec["notified"] is None and rec["seen"] >= confirm:
            rec["notified"] = now.isoformat()
            out.append(f"PROBLEM ({kind_of(reason)}): {reason} (since {rec['first'][11:16]} UTC)")
        elif rec["notified"] and rec["reminders"] < len(REMINDERS):
            since = now - datetime.fromisoformat(rec["notified"])
            if since >= REMINDERS[rec["reminders"]]:
                rec["reminders"] += 1
                hours = (now - datetime.fromisoformat(rec["first"])).total_seconds() / 3600
                out.append(f"STILL UNRESOLVED ({kind_of(reason)}) after {hours:.1f} h: {reason}"
                           + (" (last reminder)" if rec["reminders"] == len(REMINDERS) else ""))
    for reason in [r for r in open_ if r not in current]:
        rec = open_.pop(reason)
        if rec["notified"]:
            mins = (now - datetime.fromisoformat(rec["first"])).total_seconds() / 60
            out.append(f"RECOVERED ({kind_of(reason)}): {reason} (lasted {mins:.0f} min)")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    tmp.replace(p)
    return out
