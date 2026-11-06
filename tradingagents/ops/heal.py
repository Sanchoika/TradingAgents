"""Self-healing for the Brent services, run from the one-minute health check.

systemd already restarts a crashed service (Restart=on-failure), but it gives up after too many
crashes in a row (the unit is then "failed"), and it cannot see a process that is alive but
stuck. When the health check has seen one of those for ``after`` (5 min), this resets and
restarts that one service, at most once per ``cooldown`` (15 min) per service. A service that
was stopped on purpose (``inactive``) is left alone. Every action is logged in the state file.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from . import notices

AFTER = timedelta(minutes=5)
COOLDOWN = timedelta(minutes=15)
HISTORY_MAX = 200
# problem text (start) -> the systemd unit that fixes it
UNITS = {
    "price monitor not running": "brent-monitor",
    "price monitor has not ticked": "brent-monitor",
    "news collector not running": "brent-news",
    "news collector alive but not polling": "brent-news",
}


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=60)


def heal(state_path: str | Path, now: datetime, run=_systemctl, after: timedelta = AFTER,
         cooldown: timedelta = COOLDOWN) -> list[str]:
    """Restart services whose problem has lasted ``after``; return what was done (for the log)."""
    p = Path(state_path)
    st = notices.load(p)
    if not st:
        return []
    last = st.setdefault("heal_last", {})
    hist = st.setdefault("heals", [])
    done = []
    wanted: dict[str, str] = {}
    for rec in st.get("open", {}).values():
        reason = rec.get("reason", "")
        unit = next((u for start, u in UNITS.items() if reason.startswith(start)), None)
        if unit and now - datetime.fromisoformat(rec["first"]) >= after:
            wanted.setdefault(unit, reason)
    for unit, reason in wanted.items():
        if last.get(unit) and now - datetime.fromisoformat(last[unit]) < cooldown:
            continue
        state = run("is-active", unit).stdout.strip()
        if state == "failed":
            run("reset-failed", unit)
            action = "start"
        elif state == "active":
            action = "restart"
        else:   # inactive = stopped on purpose; activating/deactivating = already changing
            continue
        r = run(action, unit)
        last[unit] = now.isoformat()
        entry = {"at": now.isoformat(), "unit": unit, "was": state, "action": action,
                 "ok": r.returncode == 0, "reason": reason}
        hist.append(entry)
        done.append(f"self-heal: {action} {unit} (was {state}) for '{reason}': "
                    f"{'ok' if entry['ok'] else 'failed ' + (r.stderr or '')[-200:]}")
    del hist[:-HISTORY_MAX]
    if done:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st), encoding="utf-8")
        tmp.replace(p)
    return done
