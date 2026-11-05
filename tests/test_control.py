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
