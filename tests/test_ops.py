"""Server operations: failure notices, backups and restore, nightly checks, pinned deploy."""

import json
import tarfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tradingagents.ops import backup, deploy, nightly, notices

pytestmark = pytest.mark.unit
T = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)        # Friday 15:00 New York


def test_notices_quiet_until_one_hour_then_one_message_and_no_recovered(tmp_path):
    st = tmp_path / "n.json"
    p = ["news collector not running"]
    sent = []
    for m in range(0, 60):
        sent += notices.step(st, p, T + timedelta(minutes=m))
    assert sent == []                                                   # first hour: log only
    sent = notices.step(st, p, T + timedelta(minutes=60))
    assert len(sent) == 1 and sent[0].startswith("DOWN 1.0 h (process): news collector not running")
    for m in range(61, 60 * 30):
        sent += notices.step(st, p, T + timedelta(minutes=m))
    assert len(sent) == 1                                               # no reminders
    assert notices.step(st, [], T + timedelta(hours=31)) == []          # no "recovered" message
    h = notices.load(st)["history"][-1]
    assert h["escalated"] and h["confirmed"] and h["minutes"] > 60 * 29
    # a fresh outage of the same problem later is noticed again (once)
    again = []
    for m in range(0, 61):
        again += notices.step(st, p, T + timedelta(hours=32, minutes=m))
    assert len(again) == 1


def test_notices_same_problem_twice_in_a_day_sends_once(tmp_path):
    st = tmp_path / "n.json"
    p = ["quotes failing"]
    sent = []
    for start in (0, 180):       # two 2-hour outages, 3 hours apart
        for m in range(0, 120):
            sent += notices.step(st, p, T + timedelta(minutes=start + m))
        notices.step(st, [], T + timedelta(minutes=start + 120))
    assert len(sent) == 1


def test_notices_numbers_in_text_are_one_problem(tmp_path):
    st = tmp_path / "n.json"
    sent = []
    for m in range(0, 61):
        sent += notices.step(st, [f"news analysis_failed: {m % 9 + 2} recent items not analysed; "
                                  f"last model call failed: APIError {m}"], T + timedelta(minutes=m))
    assert len(sent) == 1 and "(model)" in sent[0]
    assert len(notices.load(st)["open"]) == 1


def test_notices_blips_and_summary(tmp_path):
    st = tmp_path / "n.json"
    notices.step(st, ["no new quote for 120s"], T)
    notices.step(st, [], T + timedelta(minutes=1))                      # a blip
    for m in range(2, 12):
        notices.step(st, ["chart data refresh failing"], T + timedelta(minutes=m))
    notices.step(st, [], T + timedelta(minutes=12))
    notices.step(st, ["price monitor not running"], T + timedelta(minutes=13))
    s = notices.summary(st, T + timedelta(minutes=14))
    assert s["blips"] == 1 and s["resolved"][0]["reason"] == "chart data refresh failing"
    assert s["resolved"][0]["minutes"] == 10 and s["open"][0]["reason"] == "price monitor not running"
    text = "\n".join(notices.summary_lines(s))
    assert "recovered" in text and "STILL OPEN" in text
    assert notices.summary_lines(notices.summary(tmp_path / "none.json", T)) == [
        "problems in the last 24 h, handled automatically: none."]


def test_notices_reads_old_state_format(tmp_path):
    st = tmp_path / "n.json"
    st.write_text(json.dumps({"open": {"quotes failing": {"first": T.isoformat(), "seen": 5,
                                                          "notified": T.isoformat(), "reminders": 1}}}))
    assert notices.step(st, [], T + timedelta(minutes=5)) == []
    assert notices.load(st)["history"][0]["reason"] == "quotes failing"


class FakeSystemctl:
    def __init__(self, states):
        self.states, self.calls = dict(states), []

    def __call__(self, *args):
        self.calls.append(args)
        out = self.states.get(args[1], "inactive") if args[0] == "is-active" else ""
        return SimpleNamespace(stdout=out + "\n", stderr="", returncode=0)


def test_heal_restarts_failed_or_stuck_after_five_minutes_with_cooldown(tmp_path):
    from tradingagents.ops import heal

    st = tmp_path / "n.json"
    probs = ["price monitor not running", "news collector alive but not polling for 15 min"]
    sc = FakeSystemctl({"brent-monitor": "failed", "brent-news": "active"})
    for m in range(0, 5):
        notices.step(st, probs, T + timedelta(minutes=m))
        assert heal.heal(st, T + timedelta(minutes=m), run=sc) == []     # under 5 min: wait
    notices.step(st, probs, T + timedelta(minutes=5))
    done = heal.heal(st, T + timedelta(minutes=5), run=sc)
    assert len(done) == 2
    assert ("reset-failed", "brent-monitor") in sc.calls and ("start", "brent-monitor") in sc.calls
    assert ("restart", "brent-news") in sc.calls
    n = len(sc.calls)
    notices.step(st, probs, T + timedelta(minutes=10))
    assert heal.heal(st, T + timedelta(minutes=10), run=sc) == [] and len(sc.calls) == n  # cooldown
    assert len(heal.heal(st, T + timedelta(minutes=21), run=sc)) == 2
    assert len(notices.load(st)["heals"]) == 4


def test_heal_leaves_a_deliberately_stopped_service_alone(tmp_path):
    from tradingagents.ops import heal

    st = tmp_path / "n.json"
    sc = FakeSystemctl({"brent-monitor": "inactive"})
    for m in range(0, 10):
        notices.step(st, ["price monitor not running", "quotes failing"], T + timedelta(minutes=m))
    assert heal.heal(st, T + timedelta(minutes=9), run=sc) == []
    assert all(c[0] == "is-active" for c in sc.calls)


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
