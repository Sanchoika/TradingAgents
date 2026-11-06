"""Quiet problem handling: problems are logged, not sent, unless they outlast auto-recovery.

Each check passes its current problems. A problem is tracked under a stable key (numbers in the
text are ignored, so "3 items not analysed" and "4 items not analysed" are one problem). Every
problem that opens and clears is kept in a bounded history that the dashboard, the nightly check
and the weekly review read. A Telegram message goes out only when one problem has lasted longer
than ``escalate_after`` (default 1 hour: retries and automatic restarts did not fix it), once per
problem, at most once per ``cooldown`` (24 h) for the same key, and with no reminder or
"recovered" message. Problems are grouped into kinds so the message says what is wrong:
process (a service is not running), data (inputs stale), model (classification failing) and
entry (new entries blocked for another reason).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

ESCALATE_AFTER = timedelta(hours=1)
COOLDOWN = timedelta(hours=24)
HISTORY_MAX = 500


def kind_of(reason: str) -> str:
    r = reason.lower()
    if "not running" in r:
        return "process"
    if "analysis" in r or "model" in r or "analysed" in r:
        return "model"
    if any(w in r for w in ("quote", "tick", "poll", "feeds", "chart data", "stale", "snapshot")):
        return "data"
    return "entry"


def key_of(reason: str) -> str:
    """The stable identity of a problem: its text with numbers and the model's error detail removed."""
    r = reason.split("; last model call failed")[0]
    return re.sub(r"\d+(?:\.\d+)?", "#", r).strip()


def load(path: str | Path) -> dict:
    p = Path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def _minutes(a: str, b: datetime) -> float:
    return round((b - datetime.fromisoformat(a)).total_seconds() / 60, 1)


def step(path: str | Path, problems: list[str], now: datetime, confirm: int = 3,
         escalate_after: timedelta = ESCALATE_AFTER, cooldown: timedelta = COOLDOWN) -> list[str]:
    """Update the problem state with this check's problems; return the (rare) messages to send.

    ``confirm`` is how many consecutive checks a problem needs before it counts (shorter blips
    are still logged in the history, marked unconfirmed)."""
    p = Path(path)
    st = load(p)
    open_ = st.setdefault("open", {})        # key -> {reason, first, last, seen, escalated}
    hist = st.setdefault("history", [])
    last_esc = st.setdefault("last_escalated", {})
    out = []
    current = {}
    for reason in problems:
        current.setdefault(key_of(reason), reason)
    for key, reason in current.items():
        rec = open_.get(key)
        if rec is None or "reason" not in rec:   # older state files used the raw text as key
            rec = open_[key] = {"reason": reason, "first": now.isoformat(), "seen": 0,
                                "escalated": None}
        rec["reason"], rec["last"] = reason, now.isoformat()
        rec["seen"] += 1
        long_enough = now - datetime.fromisoformat(rec["first"]) >= escalate_after
        recent = last_esc.get(key) and now - datetime.fromisoformat(last_esc[key]) < cooldown
        if rec["escalated"] is None and rec["seen"] >= confirm and long_enough and not recent:
            rec["escalated"] = last_esc[key] = now.isoformat()
            hours = (now - datetime.fromisoformat(rec["first"])).total_seconds() / 3600
            out.append(f"DOWN {hours:.1f} h ({kind_of(reason)}): {reason}. Automatic retries "
                       "and restarts have not fixed it. No more messages about this; see the "
                       "dashboard.")
    for key in [k for k in open_ if k not in current]:
        rec = open_.pop(key)
        hist.append({"key": key, "reason": rec.get("reason", key), "kind": kind_of(rec.get("reason", key)),
                     "first": rec["first"], "cleared": now.isoformat(),
                     "minutes": _minutes(rec["first"], now), "checks": rec.get("seen", 0),
                     "confirmed": rec.get("seen", 0) >= confirm,
                     "escalated": bool(rec.get("escalated") or rec.get("notified"))})
    del hist[:-HISTORY_MAX]
    for k in [k for k, v in last_esc.items() if now - datetime.fromisoformat(v) >= cooldown]:
        del last_esc[k]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st), encoding="utf-8")
    tmp.replace(p)
    return out


def summary(path: str | Path, now: datetime, hours: float = 24) -> dict:
    """Problems handled in the last ``hours`` (cleared, confirmed) plus those still open."""
    st = load(path)
    since = now - timedelta(hours=hours)
    done = [h for h in st.get("history", []) if h.get("confirmed")
            and datetime.fromisoformat(h["cleared"]) >= since]
    blips = sum(1 for h in st.get("history", []) if not h.get("confirmed")
                and datetime.fromisoformat(h["cleared"]) >= since)
    still = [{"reason": r.get("reason", k), "kind": kind_of(r.get("reason", k)),
              "since": r["first"], "minutes": _minutes(r["first"], now),
              "escalated": bool(r.get("escalated"))} for k, r in st.get("open", {}).items()]
    by_key: dict[str, dict] = {}
    for h in done:
        b = by_key.setdefault(h["key"], {"reason": h["reason"], "kind": h["kind"], "times": 0,
                                         "minutes": 0.0, "longest": 0.0})
        b["times"] += 1
        b["minutes"] = round(b["minutes"] + h["minutes"], 1)
        b["longest"] = max(b["longest"], h["minutes"])
    return {"hours": hours, "resolved": sorted(by_key.values(), key=lambda b: -b["minutes"]),
            "blips": blips, "open": still,
            "escalated": sum(1 for h in done if h.get("escalated")) + sum(1 for s in still if s["escalated"])}


def summary_lines(s: dict, label: str = "") -> list[str]:
    """Short text for the nightly/weekly reports and the dashboard."""
    head = f"{label}problems in the last {s['hours']:g} h, handled automatically"
    if not s["resolved"] and not s["open"]:
        return [f"{head}: none" + (f" ({s['blips']} short blips)" if s["blips"] else "") + "."]
    lines = [f"{head}:"]
    for b in s["resolved"]:
        lines.append(f"- {b['reason']} ({b['kind']}): {b['times']}x, {b['minutes']:g} min total, "
                     f"longest {b['longest']:g} min; recovered")
    for o in s["open"]:
        lines.append(f"- STILL OPEN {o['reason']} ({o['kind']}) for {o['minutes']:g} min"
                     + (" (Telegram notice sent)" if o["escalated"] else ""))
    if s["blips"]:
        lines.append(f"- {s['blips']} short blips (under 3 checks)")
    return lines
