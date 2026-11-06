"""Phone control page: identity, one-time nonces, allowed actions only, audit, read-back."""

import json
import re
from types import SimpleNamespace

import pytest

from tradingagents.control.app import ControlApp, ControlConfig, read_alerts_enabled

pytestmark = pytest.mark.unit

ME = {"tailscale-user-login": "irakli@example.com", "host": "box.ts.net"}


def make(tmp_path, now=lambda: 1000.0, run=None, **kw):
    toml = tmp_path / "brent.toml"
    toml.write_text('instrument = "BCO_USD"\nalerts_enabled = true\nrange_low = 80.0\n')
    cfg = ControlConfig(allowed_users=["irakli@example.com"], brent_toml=str(toml),
                        brent_journal=str(tmp_path / "j"), audit_path=str(tmp_path / "audit.jsonl"), **kw)
    return ControlApp(cfg, now=now, run=run or (lambda *a, **k: SimpleNamespace(returncode=0))), toml


def nonce_for(app, action, headers=ME):
    code, _, page = app.handle("GET", "/", headers)
    assert code == 200
    m = re.search(rf"name=action value={action}><input type=hidden name=nonce value=([\w-]+)>", page)
    return m.group(1)


def audit(tmp_path):
    p = tmp_path / "audit.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_unknown_or_missing_identity_is_refused_and_audited(tmp_path):
    app, _ = make(tmp_path)
    assert app.handle("GET", "/", {})[0] == 403
    assert app.handle("GET", "/", {"Tailscale-User-Login": "other@example.com"})[0] == 403
    assert app.handle("POST", "/act", {"Tailscale-User-Login": "x"}, "action=brent_pause")[0] == 403
    assert [a["result"] for a in audit(tmp_path)] == ["refused: not an allowed identity"] * 3


def test_viewing_changes_nothing(tmp_path):
    app, toml = make(tmp_path)
    before = toml.read_text()
    for _ in range(3):
        assert app.handle("GET", "/", ME)[0] == 200
    assert toml.read_text() == before and audit(tmp_path) == []


def test_pause_then_resume_reads_back_state_and_is_audited(tmp_path):
    app, toml = make(tmp_path)
    n = nonce_for(app, "brent_pause")
    code, _, page = app.handle("POST", "/act", ME, f"action=brent_pause&nonce={n}")
    assert code == 200 and "now false" in page and "State read back" in page
    assert read_alerts_enabled(toml) is False and "range_low = 80.0" in toml.read_text()
    n = nonce_for(app, "brent_resume")
    app.handle("POST", "/act", ME, f"action=brent_resume&nonce={n}")
    assert read_alerts_enabled(toml) is True
    rows = audit(tmp_path)
    assert [(r["action"], r["result"]) for r in rows] == [("brent_pause", "done"), ("brent_resume", "done")]


def test_replayed_forged_and_wrong_action_nonces_do_nothing(tmp_path):
    app, toml = make(tmp_path)
    n = nonce_for(app, "brent_pause")
    assert app.handle("POST", "/act", ME, f"action=brent_pause&nonce={n}")[0] == 200
    toml.write_text(toml.read_text().replace("false", "true"))
    code, _, _ = app.handle("POST", "/act", ME, f"action=brent_pause&nonce={n}")      # replay
    assert code == 409 and read_alerts_enabled(toml) is True
    assert app.handle("POST", "/act", ME, "action=brent_pause&nonce=forged")[0] == 403
    n2 = nonce_for(app, "brent_resume")
    assert app.handle("POST", "/act", ME, f"action=brent_pause&nonce={n2}")[0] == 403   # other action
    assert app.handle("POST", "/act", ME, "action=rm_rf&nonce=x")[0] == 400             # not an action
    assert app.handle("POST", "/act", {**ME, "origin": "https://evil.example"}, f"action=brent_pause&nonce={n2}")[0] == 403
    assert read_alerts_enabled(toml) is True


def test_nonce_expires(tmp_path):
    t = [1000.0]
    app, _ = make(tmp_path, now=lambda: t[0], nonce_ttl=600)
    n = nonce_for(app, "brent_pause")
    t[0] += 601
    assert app.handle("POST", "/act", ME, f"action=brent_pause&nonce={n}")[0] == 403


def test_restart_news_runs_only_the_configured_command(tmp_path):
    calls = []
    app, _ = make(tmp_path, restart_news_command="systemctl restart brent-news",
                  run=lambda cmd, **k: calls.append(cmd) or SimpleNamespace(returncode=0))
    n = nonce_for(app, "brent_restart_news")
    code, _, page = app.handle("POST", "/act", ME, f"action=brent_restart_news&nonce={n}&cmd=evil")
    assert code == 200 and calls == ["systemctl restart brent-news"] and "exited with code 0" in page
    (tmp_path / "b").mkdir()
    app2, _ = make(tmp_path / "b")        # no command configured -> failed, reported, not executed
    n = nonce_for(app2, "brent_restart_news")
    _, _, page = app2.handle("POST", "/act", ME, f"action=brent_restart_news&nonce={n}")
    assert "no restart_news_command" in page


def test_listener_must_be_localhost_and_a_user_is_required(tmp_path):
    with pytest.raises(ValueError):
        ControlConfig(allowed_users=["a"], brent_toml="x", host="0.0.0.0")
    with pytest.raises(ValueError):
        ControlConfig(allowed_users=[], brent_toml="x")


def test_stock_status_after_rollback_never_reads_as_no_runs(tmp_path):
    # audit finding 5: a journal path that does not exist is reported, not shown as "none"
    app, _ = make(tmp_path, demo_journal=str(tmp_path / "missing"))
    lines = app.demo_lines()
    assert "not found" in lines[0] and "none" not in " ".join(lines)


def test_stock_status_from_pc_snapshot_with_staleness(tmp_path):
    from datetime import UTC, datetime

    from tradingagents.control.app import snapshot_lines

    snap = tmp_path / "data.json"
    assert "not found" in snapshot_lines(snap)[0]
    snap.write_text(json.dumps({
        "built_at": "2026-11-05T13:30:00+00:00",
        "runs": [{"trade_date": "2026-11-05", "dry_run": False,
                  "end": {"status": "partial", "completed": 2, "attempted": 3, "errored": 1}}],
        "account": {"closed": [{}], "positions": [
            {"status": "pending"}, {"status": "open"},
            {"status": "closing", "exit_requested_on": "2026-11-05"}]}}))
    fresh = snapshot_lines(snap, 26, now=datetime(2026, 11, 5, 15, tzinfo=UTC))
    assert "STALE" not in fresh[0] and "1.5 h ago" in fresh[0]
    assert "finished partial: 2/3" in fresh[1]
    assert "pending 1, open 1, exit pending 1, closed 1" in fresh[2]
    old = snapshot_lines(snap, 26, now=datetime(2026, 11, 7, 15, tzinfo=UTC))
    assert "STALE" in old[0]
    app, _ = make(tmp_path, demo_snapshot=str(snap))
    assert any("exit pending" in x for x in app.demo_lines())


def test_feedback_page_records_answers_with_history_and_one_time_nonces(tmp_path):
    from tradingagents.brent import feedback
    from tradingagents.brent.store import Store

    app, _ = make(tmp_path)
    st = Store(tmp_path / "j")
    st.write("info", id="BI-20261106-001", kind="brief", sent=True, accepted=True,
             text="[BRENT INFO] BI-20261106-001 | 07:00 ET BRIEF\nBrent mid 84.10")
    st.write("info", id="BI-20261106-002", kind="brief", sent=False, accepted=None, shadow=True, text="x")
    code, _, page = app.handle("GET", "/feedback", ME)
    assert code == 200 and "BI-20261106-001" in page and "BI-20261106-002" not in page   # shadow not listed
    assert "Took it" not in page                                                         # INFO only
    n = re.search(r"name=value value=useful><input type=hidden name=nonce value=([\w-]+)>", page).group(1)
    body = f"action=feedback&id=BI-20261106-001&kind=INFO+brief&value=useful&nonce={n}"
    code, _, out = app.handle("POST", "/act", ME, body)
    assert code == 200 and "Recorded: Useful" in out
    assert app.handle("POST", "/act", ME, body)[0] == 409                                # replay refused
    # a revision keeps the history; the latest answer wins
    _, _, page = app.handle("GET", "/feedback", ME)
    n = re.search(r"name=value value=too_late><input type=hidden name=nonce value=([\w-]+)>", page).group(1)
    app.handle("POST", "/act", ME, f"action=feedback&id=BI-20261106-001&kind=INFO+brief&value=too_late&nonce={n}")
    hist = feedback.history(tmp_path / "j")
    assert [r["value"] for r in hist] == ["useful", "too_late"]
    assert feedback.latest(tmp_path / "j")["BI-20261106-001|rating"]["value"] == "too_late"
    with pytest.raises(ValueError):
        feedback.record(tmp_path / "j", "u", "BI-1", "INFO brief", "took_it", datetime_now())


def datetime_now():
    from datetime import UTC, datetime
    return datetime.now(UTC)


def test_weekly_review_preview_is_descriptive(tmp_path):
    from datetime import UTC, datetime

    from tradingagents.brent import feedback

    now = datetime(2026, 11, 8, 23, tzinfo=UTC)
    ev = [{"ts": "2026-11-06T12:01:00+00:00", "event": "info", "id": "BI-1", "kind": "brief", "sent": True,
           "accepted": True, "text": "a\nb"},
          {"ts": "2026-11-06T15:00:00+00:00", "event": "info", "id": "BI-2", "kind": "move", "sent": True,
           "accepted": True, "change": 1.2, "text": "a\nb"},
          {"ts": "2026-11-06T16:00:00+00:00", "event": "info_held", "reason": "routine cap 2/day reached"},
          {"ts": "2026-11-06T17:00:00+00:00", "event": "paper_exit", "id": "BS-1", "net_r": 1.4}]
    fb = {"BI-1|rating": {"value": "useful"}}
    w = feedback.weekly(ev, fb, now)
    assert w["info"]["sent"] == 2 and w["info"]["held_by_reason"] == {"routine cap 2/day reached": 1}
    assert w["feedback"]["INFO brief"] == {"useful": 1} and w["feedback"]["INFO move"] == {"no answer": 1}
    assert len(w["retrospective_context"]["fast_moves_without_news_info_in_prior_2h"]) == 1
    text = feedback.weekly_text(w, preview=True)
    assert text.startswith("[BRENT WEEKLY] PREVIEW") and "not scored" in text and "net R after costs [1.4]" in text
