"""Brent journal (append-only JSONL), state (atomic JSON) and a single-runner lock.

Everything lives in its own folder (default ~/.tradingagents/brent), apart from the
morning demo's journal.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


class Store:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.journal_path = self.dir / "signals.jsonl"
        self.state_path = self.dir / "state.json"
        self.lock_path = self.dir / "monitor.lock"
        self._lock_fh = None

    def write(self, event: str, **fields) -> dict:
        row = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "event": event, **fields}
        with self.journal_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        return row

    def events(self) -> list[dict]:
        if not self.journal_path.exists():
            return []
        with self.journal_path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def state(self) -> dict:
        if not self.state_path.exists():
            return {}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, self.state_path)

    # -- one monitor at a time ---------------------------------------------
    def acquire(self) -> None:
        fh = open(self.lock_path, "a+")  # noqa: SIM115 - held for the life of the monitor
        try:
            _lock(fh)
        except OSError as exc:
            fh.close()
            raise AlreadyRunning(f"another Brent monitor holds {self.lock_path}") from exc
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._lock_fh = fh

    def release(self) -> None:
        if self._lock_fh:
            _unlock(self._lock_fh)
            self._lock_fh.close()
            self._lock_fh = None

    def is_locked(self) -> bool:
        """True if some monitor process holds the lock right now."""
        if self._lock_fh:
            return True
        if not self.lock_path.exists():
            return False
        with open(self.lock_path, "a+") as fh:
            try:
                _lock(fh)
            except OSError:
                return True
            _unlock(fh)
            return False


if os.name == "nt":  # pragma: no cover - exercised on the Windows PC
    import msvcrt

    def _lock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
