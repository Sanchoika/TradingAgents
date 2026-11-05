"""Receiving side of the dashboard sync: never let an older snapshot overwrite a newer one.

    tar -C site -cf - . | ssh server "python -m tradingagents.demo.syncguard receive /root/ta-dashboard"

Reads a tar stream from stdin into a temp folder, compares ``built_at`` in its data.json with the one
already in DEST, and swaps the folders only if the incoming one is not older. The previous copy is kept
as ``DEST.prev`` (one generation) and every decision is appended to ``DEST.sync-log.jsonl``. Paths in
the archive are checked: absolute paths, '..' and links are refused. Exit 0 = accepted, 3 = stale
(refused on purpose, not an error), 2 = bad input.
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def _built_at(folder: Path) -> str | None:
    try:
        return json.loads((folder / "data.json").read_text(encoding="utf-8")).get("built_at")
    except (OSError, ValueError):
        return None


def _safe_extract(data: bytes, into: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        for m in tf.getmembers():
            p = Path(m.name)
            if p.is_absolute() or ".." in p.parts or m.issym() or m.islnk() or m.isdev():
                raise ValueError(f"refused archive member: {m.name!r}")
        tf.extractall(into, filter="data")


def receive(dest: str | Path, data: bytes, now: datetime | None = None) -> tuple[int, dict]:
    dest = Path(dest)
    now = now or datetime.now(UTC)
    log = dest.with_name(dest.name + ".sync-log.jsonl")
    log.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="syncguard-", dir=dest.parent))
    rec = {"ts": now.isoformat(timespec="seconds")}
    try:
        try:
            _safe_extract(data, tmp)
        except (ValueError, tarfile.TarError, OSError) as exc:
            rec.update(decision="bad_input", error=str(exc)[:200])
            return 2, rec
        new, old = _built_at(tmp), _built_at(dest)
        rec.update(incoming=new, existing=old)
        if not new:
            rec["decision"] = "bad_input"
            rec["error"] = "no built_at in data.json"
            return 2, rec
        if old and new < old:
            rec["decision"] = "refused_stale"
            return 3, rec
        prev = dest.with_name(dest.name + ".prev")
        if dest.exists():
            if prev.exists():
                shutil.rmtree(prev)
            dest.rename(prev)
        tmp.rename(dest)
        rec["decision"] = "accepted"
        return 0, rec
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")


def main(argv=None) -> int:
    argv = list(argv or sys.argv[1:])
    if len(argv) != 2 or argv[0] != "receive":
        print("usage: syncguard receive DEST   (tar stream on stdin)", file=sys.stderr)
        return 2
    code, rec = receive(argv[1], sys.stdin.buffer.read())
    print(rec["decision"], rec.get("error", ""))
    return code


if __name__ == "__main__":
    sys.exit(main())
