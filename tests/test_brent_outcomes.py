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


# ------------------------------------------------------------ random baseline (upgrade 3)
def flat(n=600):
    return [m1(i, 100.0, 100.0) for i in range(n)]


def test_baseline_is_reproducible_uses_only_the_window_and_needs_data():
    bars = [m1(i, 99.0 + (i * 7 % 13) * 0.2, 99.5 + (i * 7 % 13) * 0.2) for i in range(600)]
    win = (T + timedelta(minutes=60), T + timedelta(minutes=300))
    a = outcomes.baseline(bars, 1.0, *win, 2, "SH-1")
    assert a == outcomes.baseline(bars, 1.0, *win, 2, "SH-1") and a["n"] == 50
    # bars before the window never matter; entries never start outside it
    assert outcomes.baseline(bars[60:], 1.0, *win, 2, "SH-1") == a
    assert outcomes.baseline(bars[:60], 1.0, *win, 2, "SH-1") is None
    assert outcomes.baseline([], 1.0, *win, 2, "SH-1") is None
    assert outcomes.baseline(bars, 0, *win, 2, "SH-1") is None


def test_baseline_on_a_flat_market_loses_about_the_costs():
    b = outcomes.baseline(flat(), 1.0, T, T + timedelta(minutes=240), 2, "SH-2")
    # entry pays the 0.02 spread plus one more spread of slippage; nothing else moves
    assert b["n"] == 50 and -0.06 <= b["mean_r"] < 0


def open_sig(now, sid="SH-P", kind="ORB_NY", carded=True):
    sig = {"id": sid, "arm": "family", "status": "valid", "decided_at": now.isoformat(),
           "expires_at": (now + timedelta(seconds=60)).isoformat(),
           "setup": {"direction": "LONG", "kind": kind, "stop": 99.0}, "plan": {"t1": 101.0, "t2": 102.0},
           "paper": {"state": "open", "entry": 100.0, "entry_time": now.isoformat(), "risk": 1.0,
                     "mfe": 0.0, "mae": 0.0, "t1": False}}
    if carded:
        sig["research_call"] = {"grade": "B", "sent": True}
    return sig


def test_paper_exit_queues_one_baseline_two_hours_either_side_and_it_is_recorded(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    now = clock()
    mon.paper_exit(open_sig(now), 102.0, now + timedelta(minutes=30), "target 2R")
    q = [c for c in mon.st["post_checks"] if c["id"] == "SH-P"]
    assert len(q) == 1 and q[0]["kind"] == "baseline"
    due = now + timedelta(hours=2 + mon.cfg.max_hold_hours, minutes=2)
    assert [datetime.fromisoformat(q[0][k]) for k in ("start", "end", "due")] ==         [now - timedelta(hours=2), now + timedelta(hours=2), due]
    client.candles = lambda inst, g, n: [dict(m1(0, 100.0, 100.0), time=now + timedelta(minutes=i - 120))
                                         for i in range(n)]
    mon.post_checks(due - timedelta(minutes=1))
    assert not [e for e in store.events() if e["event"] == "baseline"]      # not due yet
    mon.post_checks(due)
    ev = [e for e in store.events() if e["event"] == "baseline"]
    assert len(ev) == 1 and ev[0]["id"] == "SH-P" and ev[0]["setup_kind"] == "ORB_NY"
    assert ev[0]["n"] == 50 and -0.06 <= ev[0]["mean_r"] < 0
    assert not mon.st["post_checks"]


# ------------------------------------------------------ score line + result card (upgrade 4)
def test_research_card_shows_the_score_line_and_a_result_card_follows_only_a_carded_trade(tmp_path):
    from tests.test_brent_markets import market
    mon, sender, store, clock = market(tmp_path)
    for _ in range(3):
        mon.tick()
        clock.advance(60)
    card = next(t for t in sender.texts if "RESEARCH CALL" in t)
    assert "Score so far (GOLD, paper, after costs): no closed paper trades yet" in card
    now, n = clock(), len(sender.texts)
    mon.paper_exit(open_sig(now, "SH-1"), 102.0, now, "target 2R")
    mon.paper_exit(open_sig(now, "SH-2"), 99.0, now, "stop")
    mon.paper_exit(open_sig(now, "SH-3", "BREAKOUT", carded=False), 100.5, now, "time")
    res = [t for t in sender.texts[n:] if "RESEARCH RESULT" in t]
    assert len(res) == 2 and len(sender.texts) == n + 2                       # nothing for the uncarded one
    assert "ORB_NY LONG GOLD" in res[0] and "+1.98R after costs (target 2R)" in res[0]
    assert "SH-1 · paper only" in res[0] and "-1.01R after costs (stop)" in res[1] and "BRENT" not in res[0]
    sig = dict(open_sig(now, "SH-4"), quote={"time": now.isoformat(), "bid": 1, "ask": 1, "spread": 0},
               rules="r", config_hash="h")
    sig["setup"].update(zone_lo=100, zone_hi=100.3, invalid_close=99.5)
    from tradingagents.brent import messages
    line = "Score so far (GOLD, paper, after costs): 3 trades, avg +0.49R · ORB_NY here: 1 of 2 won (50%)"
    assert line in messages.research_text(sig, "B", "x", mon._card_extra(sig))
    # a restart without the running stats rebuilds them once from the journal (kind unknown: no signal event)
    del mon.st["scores"]
    assert sum(k["n"] for k in mon._scores().values()) == 3
    assert round(sum(k["sum"] for k in mon._scores().values()), 3) == 1.465     # fx_loss_pct 1% each way
