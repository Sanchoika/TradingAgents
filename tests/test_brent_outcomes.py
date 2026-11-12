"""Measurement only: missed-card outcomes (and the post-check queue). Fakes only."""
from datetime import UTC, datetime, timedelta

from tests.test_brent_alerts import make
from tradingagents.brent import outcomes

T = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)


def m1(i, lo, hi, mid=None):
    mid = (lo + hi) / 2 if mid is None else mid
    return {"time": T + timedelta(minutes=i), "complete": True, "bid_o": mid, "ask_o": mid + 0.02,
            "bid_l": lo, "bid_h": hi, "ask_l": lo + 0.02, "ask_h": hi + 0.02, "bid_c": mid, "ask_c": mid + 0.02}


def test_first_hit_reports_target_stop_or_neither_and_stop_wins_a_shared_candle():
    bars = [m1(0, 99.9, 100.1), m1(1, 99.9, 102.1), m1(2, 98.0, 100.0)]
    assert outcomes.first_hit(bars, "LONG", 99.0, 102.0, T, T + timedelta(hours=8)) == "target"
    assert outcomes.first_hit(bars, "SHORT", 101.0, 98.5, T, T + timedelta(hours=8)) == "stop"
    assert outcomes.first_hit(bars[:1], "LONG", 99.0, 102.0, T, T + timedelta(hours=8)) == "neither"
    assert outcomes.first_hit([m1(0, 98.9, 102.1)], "LONG", 99.0, 102.0, T, T + timedelta(hours=1)) == "stop"
    assert outcomes.first_hit(bars, "LONG", 99.0, 102.0, T + timedelta(minutes=2), T + timedelta(hours=1)) == "stop"


def test_unfilled_research_card_is_queued_once_and_recorded_as_a_missed_outcome(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    now = clock()
    sig = {"id": "SH-T", "arm": "chart-only", "status": "expired", "decided_at": now.isoformat(),
           "expires_at": now.isoformat(), "setup": {"direction": "LONG", "kind": "BREAKOUT", "stop": 99.0,
           "zone_lo": 100, "zone_hi": 100.3, "level_name": "PDH", "level": 100, "invalid_close": 99.5,
           "atr": 2.0, "armed_at": now.isoformat(), "expires_at": now.isoformat(), "key": "k", "trigger": {}},
           "plan": {"t1": 101, "t2": 102.0}, "paper": {"state": "missed"}, "marks": {},
           "research_call": {"grade": "B", "sent": True}}
    mon.st["signals"]["SH-T"] = sig
    mon.update_signals(None, now)
    mon.update_signals(None, now)
    q = [c for c in mon.st["post_checks"] if c["id"] == "SH-T"]
    assert len(q) == 1 and q[0]["kind"] == "missed"
    client.candles = lambda inst, g, n: [dict(m1(0, 99.9, 102.5), time=now + timedelta(minutes=5))]
    later = now + timedelta(hours=mon.cfg.max_hold_hours, minutes=3)
    mon.post_checks(later)
    ev = [e for e in store.events() if e["event"] == "missed_outcome"]
    assert ev and ev[0]["id"] == "SH-T" and ev[0]["outcome"] == "target"
    assert not [c for c in mon.st["post_checks"] if c["id"] == "SH-T"]


def test_post_check_errors_are_logged_and_never_stop_the_bot(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    now = clock()
    mon.st["post_checks"] = [{"id": "X", "kind": "missed", "start": now.isoformat(), "end": now.isoformat(),
                              "due": now.isoformat(), "direction": "LONG", "setup": "BREAKOUT", "arm": "chart-only",
                              "stop": 1, "target": 2, "risk": None}]

    def boom(*_):
        raise RuntimeError("no candles")
    client.candles = boom
    mon.post_checks(now + timedelta(minutes=5))
    assert any(e["event"] == "post_check_error" for e in store.events())
    assert mon.st["post_checks"]                     # kept for a retry
