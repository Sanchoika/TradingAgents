"""Journal backups: a compressed archive with a hash manifest, retention, and a restore check.

``create`` archives the given folders (journals, state, configs) into
``dest/brent-backup-YYYYmmddTHHMMSSZ.tar.gz`` with ``MANIFEST.json`` (path, size, sha256 per file)
inside. ``restore`` extracts into a scratch folder (refusing absolute paths, '..' and links) and
``verify`` re-hashes every file against the manifest. Backups older than ``keep_days`` are deleted
(the newest is always kept). A backup on the same server does not survive losing the server: copy
the newest archive off the machine too (the PC/vault), and watch ``newest_age_hours``.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

PREFIX = "brent-backup-"


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create(sources: dict[str, str | Path], dest: str | Path, now: datetime | None = None,
           keep_days: int = 14, skip_suffixes: tuple[str, ...] = (".lock", ".tmp")) -> Path:
    """``sources`` maps an archive name to a file or folder. Missing sources are listed, not fatal."""
    now = now or datetime.now(UTC)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"{PREFIX}{now:%Y%m%dT%H%M%SZ}.tar.gz"
    manifest = {"created": now.isoformat(timespec="seconds"), "files": [], "missing": []}
    tmp = out.with_suffix(".partial")
    with tarfile.open(tmp, "w:gz") as tf:
        for name, src in sources.items():
            src = Path(src).expanduser()
            if not src.exists():
                manifest["missing"].append(name)
                continue
            files = [src] if src.is_file() else sorted(x for x in src.rglob("*") if x.is_file())
            for f in files:
                if f.suffix in skip_suffixes or f.is_symlink():
                    continue
                arc = f"{name}/{f.relative_to(src)}" if src.is_dir() else name
                tf.add(f, arcname=arc, recursive=False)
                manifest["files"].append({"path": arc, "size": f.stat().st_size, "sha256": _sha(f)})
        data = json.dumps(manifest, indent=1).encode()
        info = tarfile.TarInfo("MANIFEST.json")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    tmp.replace(out)
    prune(dest, now, keep_days)
    return out


def prune(dest: str | Path, now: datetime, keep_days: int) -> list[Path]:
    arch = sorted(Path(dest).glob(f"{PREFIX}*.tar.gz"))
    gone = []
    for a in arch[:-1]:
        if now - _when(a) > timedelta(days=keep_days):
            a.unlink()
            gone.append(a)
    return gone


def _when(a: Path) -> datetime:
    return datetime.strptime(a.name[len(PREFIX):-len(".tar.gz")], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def newest_age_hours(dest: str | Path, now: datetime | None = None) -> float | None:
    arch = sorted(Path(dest).glob(f"{PREFIX}*.tar.gz"))
    if not arch:
        return None
    return round(((now or datetime.now(UTC)) - _when(arch[-1])).total_seconds() / 3600, 1)


def restore(archive: str | Path, into: str | Path) -> Path:
    into = Path(into)
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tf:
        for m in tf.getmembers():
            p = Path(m.name)
            if p.is_absolute() or ".." in p.parts or m.issym() or m.islnk() or m.isdev():
                raise ValueError(f"refused archive member: {m.name!r}")
        tf.extractall(into, filter="data")
    return into


def verify(restored: str | Path) -> dict:
    restored = Path(restored)
    man = json.loads((restored / "MANIFEST.json").read_text(encoding="utf-8"))
    bad = [f["path"] for f in man["files"]
           if not (restored / f["path"]).exists() or _sha(restored / f["path"]) != f["sha256"]]
    return {"files": len(man["files"]), "bad": bad, "missing_sources": man["missing"], "ok": not bad}
