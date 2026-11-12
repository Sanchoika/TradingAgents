"""Replay lab v1: preregister first, filter not proof, contaminated history stated. Synthetic prices only."""
import json
from datetime import UTC, datetime, timedelta

import pytest

from tradingagents.brent import replay

PR = {"name": "t-breakout", "written": "2026-11-12", "hypothesis": "breakouts work on this synthetic market",
      "instrument": "TEST_USD", "setups": ["BREAKOUT", "FAILED_BREAK"], "variant": None,
      "period": {"start": "2026-01-25", "end": "2026-02-10"},
      "pass_rule": {"min_trades": 5, "ci_low_above": 0, "mean_at_least": 0.1},
      "config_overrides": {"blackouts": [], "max_spread": 1.0}}


def csv(tmp_path):
    """A rising staircase: each New York day trades 1.0 higher, so yesterday's high breaks every day."""
    rows, t, px = ["time,bid_o,bid_h,bid_l,bid_c,ask_o,ask_h,ask_l,ask_c,volume"], datetime(2026, 1, 1, tzinfo=UTC), 100.0
    while t < datetime(2026, 2, 12, tzinfo=UTC):
        if t.weekday() < 5 or (t.weekday() == 6 and t.hour >= 22):
            px += 0.0007
            wob = 0.05 if t.minute % 2 else -0.05
            b = px + wob
            rows.append(f"{t.timestamp()},{b},{b + 0.03},{b - 0.03},{b},{b + 0.02},{b + 0.05},{b - 0.01},{b + 0.02},1")
        t += timedelta(minutes=1)
    p = tmp_path / "m1.csv"
    p.write_text("\n".join(rows) + "\n")
    return p


def test_preregister_first_and_edited_files_are_refused(tmp_path):
    reg, f = tmp_path / "reg.jsonl", tmp_path / "pr.json"
    f.write_text(json.dumps(PR))
    with pytest.raises(ValueError, match="not preregistered"):
        replay.run(str(f), "unused.csv", str(tmp_path / "out"), registry=reg)
    h = replay.register(str(f), registry=reg)
    assert replay.register(str(f), registry=reg) == h                       # idempotent
    f.write_text(json.dumps({**PR, "hypothesis": "changed after registering"}))
    with pytest.raises(ValueError):
        replay.register(str(f), registry=reg)                               # same name, new content
    with pytest.raises(ValueError, match="not preregistered"):
        replay.run(str(f), "unused.csv", str(tmp_path / "out"), registry=reg)
    with pytest.raises(ValueError):
        replay.check({**PR, "setups": ["MOON"]})


def test_run_uses_the_bot_rules_and_labels_filter_and_contamination(tmp_path):
    reg, f = tmp_path / "reg.jsonl", tmp_path / "pr.json"
    f.write_text(json.dumps(PR))
    replay.register(str(f), registry=reg)
    v = replay.run(str(f), str(csv(tmp_path)), str(tmp_path / "out"), registry=reg)
    md = (tmp_path / "out" / "t-breakout.md").read_text()
    assert v["all"]["n"] >= 1 and "BREAKOUT" in v["by_setup"]
    assert "A filter, not proof" in md and "weakly out-of-sample" in md
    data = json.loads((tmp_path / "out" / "t-breakout.json").read_text())
    assert all(t["kind"] in ("BREAKOUT", "FAILED_BREAK") for t in data["trades"])
    assert replay.contamination({**PR, "instrument": "BCO_USD"}).startswith("IN-SAMPLE")
    assert "out-of-sample" in replay.contamination({**PR, "instrument": "BCO_USD", "period": {"start": "2026-12-01"}})


def test_verdict_needs_both_halves_and_beating_random():
    t = [{"kind": "BREAKOUT", "net_r": 1.0, "baseline_r": 0.0}] * 10 + [{"kind": "BREAKOUT", "net_r": -0.5, "baseline_r": 0.0}] * 10
    v = replay.verdict(PR, t)
    assert v["first_half"]["mean"] > 0 and v["second_half"]["mean"] < 0 and v["passes_filter"] is False
