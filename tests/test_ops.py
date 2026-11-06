"""Server operations: failure notices, backups and restore, nightly checks, pinned deploy."""

import json
import tarfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tradingagents.ops import backup, deploy, nightly, notices

pytestmark = pytest.mark.unit
T = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)        # Friday 15:00 New York


def test_notices_confirm_remind_boundedly_and_recover(tmp_path):
    st = tmp_path / "n.json"
    p = ["news collector not running"]
    assert notices.step(st, p, T) == []
    assert notices.step(st, p, T + timedelta(minutes=1)) == []
    first = notices.step(st, p, T + timedelta(minutes=2))
    assert first == ["PROBLEM (process): news collector not running (since 20:00 UTC)"]
    assert notices.step(st, p, T + timedelta(minutes=3)) == []           # not every minute
    sent = []
    for m in range(4, 60 * 50, 1):
        sent += notices.step(st, p, T + timedelta(minutes=m))
    assert len(sent) == 3 and sent[-1].endswith("(last reminder)")      # 1 h, 6 h, 24 h, then quiet
    rec = notices.step(st, [], T + timedelta(hours=51))
    assert rec[0].startswith("RECOVERED (process)")
    # a blip shorter than the confirmation never notifies, and never "recovers"
    assert notices.step(st, ["no new quote for 120s"], T) == []
    assert notices.step(st, [], T + timedelta(minutes=1)) == []


def test_backup_restore_verify_and_tamper(tmp_path):
    src = tmp_path / "brent"
    src.mkdir()
    (src / "signals.jsonl").write_text('{"a": 1}\n')
    (src / "monitor.lock").write_text("x")
    cfg = tmp_path / "brent.toml"
    cfg.write_text("alerts_enabled = true\n")
    arch = backup.create({"brent": src, "brent.toml": cfg, "gone": tmp_path / "nope"}, tmp_path / "bk", now=T)
    res = backup.verify(backup.restore(arch, tmp_path / "r1"))
    assert res["ok"] and res["files"] == 2 and res["missing_sources"] == ["gone"]
    assert not (tmp_path / "r1" / "brent" / "monitor.lock").exists()
    (tmp_path / "r1" / "brent" / "signals.jsonl").write_text("changed")
    assert backup.verify(tmp_path / "r1")["bad"] == ["brent/signals.jsonl"]
    assert backup.newest_age_hours(tmp_path / "bk", T + timedelta(hours=5)) == 5.0


def test_backup_prunes_old_but_keeps_newest_and_refuses_bad_archives(tmp_path):
    src = tmp_path / "f.txt"
    src.write_text("x")
    for d in (40, 20, 1):
        backup.create({"f": src}, tmp_path / "bk", now=T - timedelta(days=d), keep_days=14)
    left = sorted(p.name for p in (tmp_path / "bk").glob("*.tar.gz"))
    assert len(left) == 1                                     # 40 and 20 days old pruned at the last run
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tf:
        tf.add(src, arcname="../escape.txt")
    with pytest.raises(ValueError):
        backup.restore(evil, tmp_path / "r")


def test_nightly_flags_stale_backup_snapshot_and_partial_run(tmp_path):
    snap = tmp_path / "data.json"
    snap.write_text(json.dumps({"built_at": (T - timedelta(hours=40)).isoformat(),
                                "runs": [{"trade_date": "2026-11-05", "dry_run": False,
                                          "end": {"status": "partial"}}]}))
    res = nightly.check(tmp_path / "none", snap, disk_path=tmp_path, now=T)
    joined = " | ".join(res["problems"])
    assert "backup stale: newest is missing" in joined
    assert "PC stock snapshot stale: 40 h old" in joined and "PC stock run 2026-11-05 partial" in joined


class FakeSys:
    """Records commands; simulates git, systemctl and pytest."""

    def __init__(self, tests_ok=True, active=True, stock_change=False, health_ok=True):
        self.cmds, self.heads = [], {"/a": "1" * 40, "/b": "1" * 40}
        self.tests_ok, self.active, self.stock_change, self.health_ok = tests_ok, active, stock_change, health_ok
        self.stock_calls = 0

    def __call__(self, cmd, check=True, **kw):
        c = cmd if isinstance(cmd, str) else " ".join(cmd)
        self.cmds.append(c)
        out, rc = "", 0
        if "list-unit-files" in c:
            self.stock_calls += 1
            out = "ta-demo.timer disabled\nta-watchdog.timer disabled\n" + ("changed\n" if self.stock_change and self.stock_calls > 1 else "")
        elif " checkout -q --detach " in c:
            co, sha = cmd[2], cmd[-1]
            self.heads[co] = sha
        elif "rev-parse HEAD" in c:
            out = self.heads[cmd[2]]
        elif "pytest" in c:
            rc = 0 if self.tests_ok else 1
        elif "is-active" in c:
            out = "active" if self.active else "failed"
        elif c.startswith("health"):
            rc = 0 if self.health_ok else 1
        r = SimpleNamespace(returncode=rc, stdout=out, stderr="")
        if check and rc:
            raise deploy.DeployError(c)
        return r


def plan(**kw):
    return deploy.Plan(commit="2" * 40, checkouts=["/a", "/b"], venv_python="py", health_cmd="health",
                       health_wait_s=0, **kw)


def test_deploy_refuses_moving_refs_and_the_morning_window():
    with pytest.raises(deploy.DeployError, match="full 40-character"):
        deploy.deploy(deploy.Plan(commit="main", checkouts=["/a"], venv_python="py"), run=FakeSys())
    morning = datetime(2026, 11, 6, 13, 0, tzinfo=UTC)        # 08:00 New York
    with pytest.raises(deploy.DeployError, match="protected morning window"):
        deploy.deploy(plan(), run=FakeSys(), now=lambda: morning)


def test_deploy_success_switches_all_checkouts_and_reads_back():
    fs = FakeSys()
    res = deploy.deploy(plan(), run=fs, now=lambda: T, sleep=lambda s: None)
    assert res["ok"] and fs.heads == {"/a": "2" * 40, "/b": "2" * 40}
    assert any("systemctl restart brent-news brent-monitor brent-control" in c for c in fs.cmds)
    assert not any("ta-demo" in c and ("enable" in c or "start" in c) for c in fs.cmds)
    assert any("'ta-demo*'" in c and "'ta-watchdog*'" in c for c in fs.cmds if "list-unit-files" in c)


def test_failed_tests_change_nothing():
    fs = FakeSys(tests_ok=False)
    res = None
    with pytest.raises(deploy.DeployError, match="tests failed"):
        res = deploy.deploy(plan(), run=fs, now=lambda: T)
    assert res is None and fs.heads == {"/a": "1" * 40, "/b": "1" * 40}
    assert not any("systemctl restart" in c for c in fs.cmds)


@pytest.mark.parametrize("fs", [FakeSys(active=False), FakeSys(health_ok=False), FakeSys(stock_change=True)])
def test_failed_read_back_rolls_back(fs):
    res = deploy.deploy(plan(), run=fs, now=lambda: T, sleep=lambda s: None)
    assert not res["ok"] and fs.heads == {"/a": "1" * 40, "/b": "1" * 40}
    assert "rolled back" in res["log"][-1]


def test_refusal_names_a_missing_pytest():
    class NoPytest(FakeSys):
        def __call__(self, cmd, check=True, **kw):
            if "pytest" in (cmd if isinstance(cmd, str) else " ".join(cmd)):
                r = SimpleNamespace(returncode=1, stdout="", stderr="/x/python: No module named pytest")
                if check:
                    raise deploy.DeployError("x")
                return r
            return super().__call__(cmd, check, **kw)

    with pytest.raises(deploy.DeployError, match="pytest is not installed"):
        deploy.deploy(plan(), run=NoPytest(), now=lambda: T)
