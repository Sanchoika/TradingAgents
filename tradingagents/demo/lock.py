"""Single-writer lock for a demo run: one process may change the account and journal at a time.

Uses an operating-system file lock (msvcrt on Windows, fcntl elsewhere). The OS drops the lock
when its owner dies, so a crashed run never blocks the next one and no process-liveness probe
(``os.kill(pid, 0)`` is not a probe on Windows) is needed.
"""

from __future__ import annotations

import os
from pathlib import Path


class RunLocked(RuntimeError):
    pass


def _try_lock(fh) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


class RunLock:
    """Exclusive lock on ``path``. A second holder gets RunLocked; a dead owner's lock is gone."""

    def __init__(self, path: str | Path, stale_after: float = 0):  # stale_after kept for callers
        self.path = Path(path)
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")  # noqa: SIM115  (held open for the lock's lifetime)
        if not _try_lock(fh):
            fh.close()
            raise RunLocked(f"another run holds {self.path}; not starting a second writer")
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return self

    def __exit__(self, *exc):
        if self._fh:
            _unlock(self._fh)
            self._fh.close()
            self._fh = None
