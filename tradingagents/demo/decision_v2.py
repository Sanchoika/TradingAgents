"""Decision format v2 (shadow only): does the written report support the trade the runner makes?

The frozen experiment maps every Buy/Overweight to a new long and every Sell/Underweight to a
new short held ``hold_days``. Some reports say something else (trim an existing holding, buy only
on a pullback, a 6-12 month view). This module reads the SAME report text with fixed rules (no LLM,
no cost) and records what an explicit LONG / SHORT / ABSTAIN decision would have been, with the
reasons. It never places or changes an order; it writes a separate ``decision_v2`` journal row
so the two can be compared later. Promotion of v2 needs an explicit, evidence-based decision.
"""

from __future__ import annotations

import re

VERSION = "decision-v2-rules-1"

_UNITS = {"day": 1, "week": 7, "month": 30, "quarter": 90, "year": 365}
_HORIZON = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:[-–—]|to)?\s*(\d+(?:\.\d+)?)?\s*[- ]?(day|week|month|quarter|year)s?\b", re.I)
_TRIM = re.compile(r"\b(trim|reduce|lighten|scale back|sell down|take profits?)\b[^.\n]{0,80}"
                   r"\b(existing|current|holding|position|allocation|exposure|stake)\b", re.I)
_NO_MECH = re.compile(r"\b(not|avoid|against|no)\b[^.\n]{0,40}\b(mechanical|forced|wholesale|full)\s+"
                      r"(sell|selling|exit|liquidat)", re.I)
_COND = re.compile(r"\b(only if|if and when|once|wait for|waiting for|on a pullback|on a dip|on weakness|"
                   r"upon confirmation|after confirmation|provided that|contingent on)\b", re.I)
_ACT = re.compile(r"\b(buy|enter|initiate|add|accumulate|short|sell|establish)\b", re.I)
_UNVERIFIED = re.compile(r"\b(unverified|could not (?:be )?verif\w*|not (?:been )?(?:confirmed|verified)|"
                         r"no (?:earnings )?(?:data|release|report)|unable to (?:verify|confirm)|"
                         r"unavailable|missing)\b", re.I)
_EARN = re.compile(r"earnings|results|guidance|quarter", re.I)


def horizon_days(text: str) -> int | None:
    """Longest explicit horizon (days) the report states for its thesis, or None."""
    best = None
    for m in _HORIZON.finditer(text):
        hi = float(m.group(2) or m.group(1))
        days = int(hi * _UNITS[m.group(3).lower()])
        best = days if best is None else max(best, days)
    return best


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def interpret(rating: str, direction: int | None, text: str, hold_days: int = 5) -> dict:
    """v2 decision for one report. ``direction`` is the runner's v1 target (+1/-1/0/None)."""
    flags, notes = [], []
    low = text or ""
    if direction in (None, 0):
        return {"version": VERSION, "decision": "ABSTAIN", "v1_direction": direction or 0,
                "flags": ["no_trade_rating"], "notes": ["rating maps to no trade"],
                "horizon_days": horizon_days(low), "agrees_with_v1": True}

    if direction < 0 and (_TRIM.search(low) or _NO_MECH.search(low)):
        flags.append("trim_existing_only")
        notes.append("report advises trimming/not mechanically selling an existing holding; "
                     "that is not a new short")
    if direction > 0 and _NO_MECH.search(low):
        notes.append("report cautions against mechanical trading")
    h = horizon_days(low)
    if h is not None and h > 3 * hold_days:
        flags.append("horizon_mismatch")
        notes.append(f"thesis horizon ~{h} days vs a {hold_days}-session holding period")
    for s in _sentences(low):
        if _COND.search(s) and _ACT.search(s):
            flags.append("conditional_entry")
            notes.append(f"entry is conditional: \"{s.strip()[:120]}\"")
            break
    for s in _sentences(low):
        if _EARN.search(s) and _UNVERIFIED.search(s):
            flags.append("earnings_unverified")
            notes.append(f"earnings evidence flagged unverified: \"{s.strip()[:120]}\"")
            break

    decision = "ABSTAIN" if flags else ("LONG" if direction > 0 else "SHORT")
    return {"version": VERSION, "decision": decision, "v1_direction": direction,
            "flags": sorted(set(flags)), "notes": notes, "horizon_days": h,
            "intended_holding_days": hold_days,
            "agrees_with_v1": (decision == "LONG" and direction > 0) or (decision == "SHORT" and direction < 0)}


def compare(events: list[dict]) -> dict:
    """Shadow comparison over the journal: how often v2 would have abstained, and why."""
    rows = [e for e in events if e["event"] == "decision_v2"]
    flags: dict[str, int] = {}
    for r in rows:
        for f in r.get("flags") or []:
            flags[f] = flags.get(f, 0) + 1
    return {"version": VERSION, "decisions": len(rows),
            "v2_abstained_where_v1_traded": sum(1 for r in rows if not r["agrees_with_v1"]),
            "flags": flags,
            "note": "shadow only: v2 places no orders; its ledger is separate from the frozen test"}
