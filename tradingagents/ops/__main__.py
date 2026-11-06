"""python -m tradingagents.ops {backup,restore-check,nightly,deploy} (Brent server operations)."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from . import backup, deploy, nightly, notices


def _sources(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        name, _, path = p.partition("=")
        if not name or not path:
            raise SystemExit(f"--source wants NAME=PATH, got {p!r}")
        out[name] = path
    return out


def _notify(brent_config: str | None, text: str) -> bool:
    if not brent_config:
        print(text)
        return True
    from tradingagents.brent.__main__ import _sender
    from tradingagents.brent.config import load_config

    ok, _ = _sender(load_config(brent_config))(text)
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tradingagents.ops")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup", help="archive journals/configs with a hash manifest; prune old ones")
    b.add_argument("--dest", required=True)
    b.add_argument("--source", action="append", default=[], help="NAME=PATH (repeat)")
    b.add_argument("--keep-days", type=int, default=14)
    rc = sub.add_parser("restore-check", help="restore an archive into a scratch folder and verify hashes")
    rc.add_argument("archive", nargs="?", help="default: the newest in --dest")
    rc.add_argument("--dest")
    n = sub.add_parser("nightly", help="disk, backup age, PC snapshot, file sizes; notices on change")
    n.add_argument("--backup-dir", required=True)
    n.add_argument("--snapshot")
    n.add_argument("--state", required=True, help="notice state file")
    n.add_argument("--brent-config", help="send notices through Brent's notify command")
    n.add_argument("--watch-file", action="append", default=[], help="NAME=PATH size watch")
    d = sub.add_parser("deploy", help="deploy a pinned merged commit with tests, read-back, rollback")
    d.add_argument("commit")
    d.add_argument("--checkout", action="append", required=True)
    d.add_argument("--python", required=True, help="venv python")
    d.add_argument("--service", action="append")
    d.add_argument("--backup-cmd", default="")
    d.add_argument("--health-cmd", default="")
    a = ap.parse_args(argv)

    if a.cmd == "backup":
        out = backup.create(_sources(a.source), a.dest, keep_days=a.keep_days)
        print(out)
        return 0
    if a.cmd == "restore-check":
        arch = a.archive or (sorted(Path(a.dest).glob(f"{backup.PREFIX}*.tar.gz")) or [None])[-1]
        if not arch:
            print("no archive found", file=sys.stderr)
            return 2
        scratch = tempfile.mkdtemp(prefix="restore-check-")
        try:
            res = backup.verify(backup.restore(arch, scratch))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        print(json.dumps({"archive": str(arch), **res}))
        return 0 if res["ok"] else 1
    if a.cmd == "nightly":
        res = nightly.check(a.backup_dir, a.snapshot, big_files=_sources(a.watch_file))
        print(json.dumps(res))
        msgs = notices.step(a.state, res["problems"], datetime.now(UTC), confirm=1)
        if msgs:
            _notify(a.brent_config, "[OPS NIGHTLY] " + "\n".join(msgs))
        return 1 if res["problems"] else 0
    if a.cmd == "deploy":
        plan = deploy.Plan(commit=a.commit, checkouts=a.checkout, venv_python=a.python,
                           services=tuple(a.service or deploy.DEFAULT_SERVICES),
                           backup_cmd=a.backup_cmd, health_cmd=a.health_cmd)
        try:
            res = deploy.deploy(plan)
        except deploy.DeployError as exc:
            print(f"refused before any change: {exc}", file=sys.stderr)
            return 2
        print("\n".join(res["log"]))
        return 0 if res["ok"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
