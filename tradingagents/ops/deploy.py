"""One-step deploy of a pinned, already-merged commit to the Brent server, with rollback.

Steps: preflight (the commit is a full SHA reachable from origin/main; checkouts clean; not in
the protected morning window) -> backup (journals and configs) -> tests of that exact commit in a
scratch worktree -> switch every checkout -> reinstall only if pyproject.toml changed -> restart
the named Brent services -> read back (services active, the running code reports the commit,
health OK within the wait). Any failure after the switch puts every checkout back on the previous
commit and restarts the same services. It never touches stock-test units: their enabled/active
state is recorded before and compared after, and a change aborts with rollback.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
PROTECTED = ("07:50", "08:40")       # around the 08:00 ET stock run (protects its model use)
DEFAULT_SERVICES = ("brent-news", "brent-monitor", "brent-control")
STOCK_UNITS = "ta-demo* ta-watchdog* tradingagents-demo*"   # the server's stock-test units (kept off)
STOCK_UNITS_ARGS = " ".join(f"'{u}'" for u in STOCK_UNITS.split())


class DeployError(RuntimeError):
    pass


@dataclass
class Plan:
    commit: str
    checkouts: list[str]
    venv_python: str
    services: tuple[str, ...] = DEFAULT_SERVICES
    test_args: tuple[str, ...] = ("tests/test_brent_alerts.py", "tests/test_brent_news.py",
                                  "tests/test_brent_info.py", "tests/test_control.py")
    backup_cmd: str = ""
    health_cmd: str = ""
    health_wait_s: int = 90
    log: list[str] = field(default_factory=list)


def _run(cmd: list[str] | str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, **kw)
    if check and r.returncode:
        raise DeployError(f"{cmd if isinstance(cmd, str) else ' '.join(cmd)} -> {r.returncode}: "
                          f"{(r.stderr or r.stdout)[-400:]}")
    return r


def in_protected(now: datetime) -> bool:
    t = now.astimezone(NY)
    return t.weekday() < 5 and PROTECTED[0] <= t.strftime("%H:%M") < PROTECTED[1]


def deploy(plan: Plan, run: Callable = _run, now: Callable[[], datetime] = lambda: datetime.now(UTC),
           sleep: Callable[[float], None] = time.sleep) -> dict:
    say = plan.log.append
    if not re.fullmatch(r"[0-9a-f]{40}", plan.commit):
        raise DeployError("give the full 40-character commit SHA (a moving branch name is refused)")
    if in_protected(now()):
        raise DeployError(f"refused: inside the protected morning window {PROTECTED[0]}-{PROTECTED[1]} ET")
    prev = {}
    for co in plan.checkouts:
        run(["git", "-C", co, "fetch", "-q", "origin", "main"])
        if run(["git", "-C", co, "merge-base", "--is-ancestor", plan.commit, "origin/main"],
               check=False).returncode:
            raise DeployError(f"{plan.commit[:12]} is not on origin/main in {co}")
        if run(["git", "-C", co, "status", "--porcelain", "--untracked-files=no"]).stdout.strip():
            raise DeployError(f"{co} has local changes; refusing to overwrite them")
        prev[co] = run(["git", "-C", co, "rev-parse", "HEAD"]).stdout.strip()
    say(f"previous: {', '.join(f'{k}={v[:12]}' for k, v in prev.items())}")
    stock_before = run(f"systemctl list-unit-files {STOCK_UNITS_ARGS} --no-legend", check=False).stdout
    if plan.backup_cmd:
        run(plan.backup_cmd)
        say("backup done")
    main = plan.checkouts[0]
    scratch = f"/tmp/deploy-{plan.commit[:12]}"
    run(["git", "-C", main, "worktree", "add", "--detach", "-f", scratch, plan.commit])
    try:
        r = run([plan.venv_python, "-m", "pytest", "-q", "-x", *plan.test_args], check=False, cwd=scratch)
        if r.returncode:
            raise DeployError(f"tests failed at {plan.commit[:12]}: {r.stdout[-400:]}")
        say("tests passed in scratch worktree")
    finally:
        run(["git", "-C", main, "worktree", "remove", "--force", scratch], check=False)

    def switch(to: dict[str, str]):
        for co, sha in to.items():
            changed = run(["git", "-C", co, "diff", "--name-only", "HEAD", sha]).stdout
            run(["git", "-C", co, "checkout", "-q", "--detach", sha])
            if co == main and "pyproject.toml" in changed.split():
                run([plan.venv_python, "-m", "pip", "install", "-q", "-e", co], check=False)
        run(["systemctl", "restart", *plan.services])

    try:
        switch(dict.fromkeys(plan.checkouts, plan.commit))
        say(f"switched to {plan.commit[:12]} and restarted {' '.join(plan.services)}")
        deadline = now() + timedelta(seconds=plan.health_wait_s)
        while True:
            inactive = [s for s in plan.services
                        if run(["systemctl", "is-active", s], check=False).stdout.strip() != "active"]
            heads = {co: run(["git", "-C", co, "rev-parse", "HEAD"]).stdout.strip() for co in plan.checkouts}
            health = run(plan.health_cmd, check=False) if plan.health_cmd else None
            ok = not inactive and all(h == plan.commit for h in heads.values()) and \
                (health is None or health.returncode == 0)
            if ok or now() >= deadline:
                break
            sleep(10)
        if run(f"systemctl list-unit-files {STOCK_UNITS_ARGS} --no-legend", check=False).stdout != stock_before:
            raise DeployError("stock-test units changed during deploy")
        if not ok:
            raise DeployError(f"read-back failed: inactive={inactive} heads={heads} "
                              f"health={(health.stdout if health else '')[-200:]}")
        say("read-back OK: services active, checkouts on the commit, health OK")
        return {"ok": True, "commit": plan.commit, "previous": prev, "log": plan.log}
    except DeployError as exc:
        say(f"FAILED: {exc}; rolling back")
        switch(prev)
        say("rolled back to the previous commits")
        return {"ok": False, "commit": plan.commit, "previous": prev, "error": str(exc), "log": plan.log}
