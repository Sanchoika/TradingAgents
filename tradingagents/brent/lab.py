"""Strategy lab v1 (2026-11-12, Irakli list item 2): frozen variants of existing setups, paper only.

A lab setup is a JSON rule file written by NORTH's weekly lab job into ``lab_dir/<INSTRUMENT>/``.
It names one existing setup kind on this market and changes only how its trade is taken:
direction, New York hours, stop size, target in R and holding time. When the base setup issues
its research signal, each matching lab rule gets a silent paper twin (arm "lab", ids LB-...):
never a card, a real alert or a practice order, never a change to the live rules or fingerprint.
Each rule's hash covers the instrument, base and params: an edited file no longer matches and is
skipped (a changed idea is a new idea with its own count).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .families import FAMILIES

LAB_VERSION = "lab-v1"
BASES = ("BREAKOUT", "FAILED_BREAK") + FAMILIES
BOUNDS = {"stop_mult": (0.5, 2.0), "target_r": (1.0, 4.0), "hold_hours": (1.0, 24.0)}
MAX_PER_MARKET = 3


def rule_hash(instrument: str, base: str, params: dict) -> str:
    body = json.dumps({"instrument": instrument, "base": base, "params": params}, sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()[:12]


def validate(rule: dict, instrument: str) -> dict:
    """The rule as frozen, or ValueError. Only the fields below exist; anything else is refused."""
    if not isinstance(rule, dict) or rule.get("instrument") != instrument:
        raise ValueError("wrong instrument")
    base, p = rule.get("base"), rule.get("params")
    if base not in BASES or not isinstance(p, dict):
        raise ValueError("unknown base setup")
    if set(p) != {"direction", "hours_et", *BOUNDS}:
        raise ValueError("params must be exactly direction, hours_et, stop_mult, target_r, hold_hours")
    if p["direction"] not in ("both", "LONG", "SHORT"):
        raise ValueError("bad direction")
    h = p["hours_et"]
    if not (isinstance(h, list) and len(h) == 2 and all(isinstance(x, int) and not isinstance(x, bool) for x in h)
            and 0 <= h[0] < h[1] <= 24):
        raise ValueError("bad hours_et")
    for k, (lo, hi) in BOUNDS.items():
        v = p[k]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
            raise ValueError(f"bad {k}")
    if rule.get("hash") != rule_hash(instrument, base, p):
        raise ValueError("hash does not match (edited after freezing)")
    return {"id": rule["hash"], "base": base, "params": p}


def load(lab_dir: str, instrument: str) -> tuple[list[dict], list[str]]:
    """Active rules for this market (at most MAX_PER_MARKET, oldest file names first) and problems.
    A file named OFF in lab_dir switches the lab off."""
    root = Path(lab_dir) if lab_dir else None
    if root is None or not root.is_dir() or (root / "OFF").exists():
        return [], []
    rules, bad = [], []
    folder = root / instrument
    for f in sorted(folder.glob("*.json"))[:20] if folder.is_dir() else []:
        try:
            rules.append(validate(json.loads(f.read_text(encoding="utf-8")), instrument))
        except (OSError, ValueError) as exc:
            bad.append(f"{f.name}: {exc}")
    return rules[:MAX_PER_MARKET], bad


def matches(rule: dict, kind: str, direction: str, ny_hour: int) -> bool:
    p = rule["params"]
    return rule["base"] == kind and p["direction"] in ("both", direction) \
        and p["hours_et"][0] <= ny_hour < p["hours_et"][1]
