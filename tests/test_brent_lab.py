"""Strategy lab v1: frozen rule files -> silent paper twins. Fakes only."""
import json
from datetime import timedelta

import pytest

from tests.test_brent_alerts import alerts, make
from tradingagents.brent import lab
from tradingagents.brent.config import BrentConfig

P = {"direction": "both", "hours_et": [0, 24], "stop_mult": 1.5, "target_r": 3.0, "hold_hours": 4.0}


def rule(inst="BCO_USD", base="BREAKOUT", params=P):
    return {"instrument": inst, "base": base, "params": params, "hash": lab.rule_hash(inst, base, params)}


def write(root, *rules):
    d = root / "BCO_USD"
    d.mkdir(parents=True, exist_ok=True)
    for i, r in enumerate(rules):
        (d / f"{i:02d}-{r['hash']}.json").write_text(json.dumps(r))


def test_validate_freezes_by_hash_and_bounds():
    assert lab.validate(rule(), "BCO_USD")["id"] == rule()["hash"]
    edited = dict(rule(), params={**P, "target_r": 3.5})
    with pytest.raises(ValueError, match="hash"):
        lab.validate(edited, "BCO_USD")
    for bad in ({**P, "stop_mult": 5}, {**P, "hours_et": [10, 9]}, {**P, "direction": "up"}, {**P, "extra": 1}):
        with pytest.raises(ValueError):
            lab.validate(rule(params=bad), "BCO_USD")
    with pytest.raises(ValueError, match="instrument"):
        lab.validate(rule(inst="XAU_USD"), "BCO_USD")
    with pytest.raises(ValueError, match="base"):
        lab.validate(rule(base="MOON"), "BCO_USD")


def test_load_caps_three_per_market_reports_bad_files_and_honours_off(tmp_path):
    rs = [rule(params={**P, "target_r": t}) for t in (1.5, 2.5, 3.0, 3.5)]
    write(tmp_path, *rs)
    (tmp_path / "BCO_USD" / "zz-bad.json").write_text("{not json")
    got, bad = lab.load(str(tmp_path), "BCO_USD")
    assert len(got) == lab.MAX_PER_MARKET and len(bad) == 1
    (tmp_path / "OFF").write_text("")
    assert lab.load(str(tmp_path), "BCO_USD") == ([], [])
    assert lab.load("", "BCO_USD") == ([], [])


def test_matching_research_signal_gets_one_silent_paper_twin_with_the_rule_applied(tmp_path):
    write(tmp_path / "lab", rule(params={**P, "direction": "LONG"}), rule(base="ORB_NY"))
    base_mon, _, base_sender, _, _ = make(tmp_path / "plain", lab_dir="")
    base_mon.tick()
    mon, client, sender, clock, store = make(tmp_path / "b", lab_dir=str(tmp_path / "lab"))
    mon.tick()
    assert sender.texts == base_sender.texts and alerts(sender)        # Telegram unchanged by the lab
    sigs = list(mon.st["signals"].values())
    twins = [s for s in sigs if s["arm"] == "lab"]
    base = next(s for s in sigs if s["id"] == twins[0]["lab_of"])
    assert len(twins) == 1 and twins[0]["id"].startswith("LB-") and twins[0]["telegram"] is False
    assert base["arm"] == "chart-only" and base["setup"]["kind"] == "BREAKOUT"
    entry = base["quote"]["ask"]
    dist = abs(entry - base["setup"]["stop"]) * 1.5
    assert twins[0]["setup"]["stop"] == pytest.approx(entry - dist, abs=1e-3)
    assert twins[0]["plan"]["t2"] == pytest.approx(entry + 3 * dist, abs=1e-3)
    assert base["setup"]["stop"] != twins[0]["setup"]["stop"]          # the base signal is untouched
    assert any(e["event"] == "signal" and e.get("arm") == "lab" for e in store.events())
    clock.advance(25)                                                  # past the 20 s human latency
    mon.tick()
    t = next(s for s in mon.st["signals"].values() if s["arm"] == "lab")
    assert t["paper"]["state"] == "open"                               # filled on the same rules as its base
    from datetime import datetime
    held = datetime.fromisoformat(t["paper"]["deadline"]) - datetime.fromisoformat(t["paper"]["entry_time"])
    assert held <= timedelta(hours=4) and held > timedelta(hours=3)     # the rule's 4 h, not the default 8 h


def test_lab_never_changes_the_fingerprint_and_lab_trades_stay_out_of_the_card_score(tmp_path):
    assert BrentConfig().config_hash() == BrentConfig(lab_dir="/elsewhere").config_hash()
    mon, *_ = make(tmp_path, lab_dir="")
    mon.st["scores"] = {}
    sig = {"id": "LB-1", "arm": "lab", "setup": {"kind": "BREAKOUT", "direction": "LONG", "stop": 99.0}, "decided_at": mon.now().isoformat(),
           "expires_at": mon.now().isoformat(), "plan": {"t2": 1},
           "paper": {"state": "open", "entry": 100.0, "entry_time": mon.now().isoformat(), "risk": 1.0, "mfe": 0, "mae": 0, "t1": False}}
    mon.paper_exit(sig, 101.0, mon.now(), "time")
    assert mon.st["scores"] == {}


def test_a_broken_lab_never_stops_the_bot(tmp_path):
    mon, client, sender, clock, store = make(tmp_path, lab_dir="")
    mon.lab_twins = lambda *a: (_ for _ in ()).throw(RuntimeError("lab bug"))
    mon.tick()
    assert alerts(sender) and any(e["event"] == "lab_error" for e in store.events())
