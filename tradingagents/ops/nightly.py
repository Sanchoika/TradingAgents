"""Nightly checks that the one-minute health check does not cover: disk, backup age, the PC's
stock snapshot (age and last run status) and journal growth. Returns problems; the caller sends
notices only on state changes (see notices.py)."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from . import backup


def check(backup_dir: str | Path, snapshot: str | Path | None, disk_path: str | Path = "/",
          now: datetime | None = None, max_backup_hours: float = 36, max_snapshot_hours: float = 30,
          min_free_pct: float = 10.0, big_files: dict[str, str | Path] | None = None,
          max_file_mb: float = 500) -> dict:
    now = now or datetime.now(UTC)
    problems, facts = [], {}
    du = shutil.disk_usage(disk_path)
    free = 100.0 * du.free / du.total
    facts["disk_free_pct"] = round(free, 1)
    if free < min_free_pct:
        problems.append(f"disk {free:.0f}% free (under {min_free_pct:g}%)")
    age = backup.newest_age_hours(backup_dir, now)
    facts["backup_age_h"] = age
    if age is None or age > max_backup_hours:
        problems.append(f"backup stale: newest is {'missing' if age is None else f'{age:.0f} h old'}")
    if snapshot:
        p = Path(snapshot)
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            built = datetime.fromisoformat(d["built_at"])
            facts["snapshot_age_h"] = round((now - built).total_seconds() / 3600, 1)
            if now.weekday() < 5 and facts["snapshot_age_h"] > max_snapshot_hours:
                problems.append(f"PC stock snapshot stale: {facts['snapshot_age_h']:.0f} h old")
            runs = [r for r in d.get("runs") or [] if not r.get("dry_run")]
            end = (runs[-1].get("end") or {}) if runs else {}
            facts["pc_last_run"] = {"date": runs[-1]["trade_date"] if runs else None, "status": end.get("status")}
            if end.get("status") in ("partial", "failed"):
                problems.append(f"PC stock run {runs[-1]['trade_date']} {end['status']}")
        except (OSError, ValueError, KeyError) as exc:
            problems.append(f"PC stock snapshot unreadable: {type(exc).__name__}")
    for name, f in (big_files or {}).items():
        f = Path(f).expanduser()
        if f.exists() and f.stat().st_size > max_file_mb * 1e6:
            problems.append(f"{name} is {f.stat().st_size / 1e6:.0f} MB (over {max_file_mb:g} MB)")
    return {"problems": problems, "facts": facts}
