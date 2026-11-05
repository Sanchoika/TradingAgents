"""Single-writer lock for a demo run: one process may change the account and journal at a time."""

from __future__ import annotations

import os
import time
from pathlib import Path


class RunLocked(RuntimeError):
    pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # exists but not ours (or Windows quirks): assume alive
        return True
    return True


class RunLock:
    """Exclusive lock file holding the owner's pid. A dead owner or an old file is taken over."""

    def __init__(self, path: str | Path, stale_after: float = 6 * 3600):
        self.path, self.stale_after = Path(path), stale_after

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if not self._stale():
                    raise RunLocked(f"another run holds {self.path}; not starting a second writer") from None
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w") as fh:
                fh.write(str(os.getpid()))
            return self
        raise RunLocked(f"could not take {self.path}")

    def _stale(self) -> bool:
        try:
            old = time.time() - self.path.stat().st_mtime > self.stale_after
            pid = int(self.path.read_text().strip() or 0)
        except (OSError, ValueError):
            return True
        return old or not _alive(pid)

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)
