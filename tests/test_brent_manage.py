"""Position management in shadow (list item 4): variants replayed against plain hold. Fakes only."""
from datetime import UTC, datetime, timedelta

from tests.test_brent_alerts import make
from tradingagents.brent import outcomes

T = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)


def b(i, lo, hi, o=None, c=None):
    o = lo if o is None else o
    c = hi if c is None else c
    return {"time": T + timedelta(minutes=i), "complete": True, "bid_o": o, "ask_o": o + 0.02, "bid_l": lo, "bid_h": hi,
            "ask_l": lo + 0.02, "ask_h": hi + 0.02, "bid_c": c, "ask_c": c + 0.02}


ARGS = {"direction": "LONG", "entry": 100.0, "stop": 99.0, "risk": 1.0, "t1": 101.0, "t2": 102.0, "entry_time": T,
        "deadline": T + timedelta(hours=8)}


def test_reach_1r_then_back_to_the_stop():
    bars = [b(0, 99.9, 100.2), b(1, 100.2, 101.1), b(2, 98.9, 100.5, o=100.5)]
    m = outcomes.manage(bars, **ARGS)
    assert m["plain"] == -1.02                       # stop minus one spread
    assert m["scale_1r"] == round(0.5 * 1 + 0.5 * -1.02, 3)
    assert m["breakeven"] == -0.02                   # stopped at entry, one spread worse
    assert m["news_flip"] == m["plain"] and m["broker_tp"] == m["plain"]


def test_target_time_exit_news_flip_and_broker_take_profit_without_a_time_limit():
    bars = [b(0, 99.9, 100.2)] + [b(i, 100.1, 100.4, c=100.3) for i in range(1, 600)] + [b(600, 100.5, 102.1)]
    m = outcomes.manage(bars, **ARGS, flip_at=T + timedelta(minutes=30))
    assert m["plain"] == 0.3                         # time exit at 8 h (bid close)
    assert m["broker_tp"] == 2.0                     # the resting 2R take-profit fills at 10 h
    assert m["news_flip"] == 0.1                     # out at the open of the minute after the headline
    assert outcomes.manage(bars[5:], **ARGS) is None   # the candles do not cover the entry


def test_a_closed_paper_trade_is_queued_once_and_recorded_against_plain_hold(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    texts = list(sender.texts)
    now = clock()
    sig = {"id": "SH-M", "arm": "chart-only", "setup": {"kind": "BREAKOUT", "direction": "LONG", "stop": 99.0},
           "decided_at": now.isoformat(), "expires_at": now.isoformat(), "plan": {"t1": 101.0, "t2": 102.0},
           "paper": {"state": "open", "entry": 100.0, "entry_time": T.isoformat(), "risk": 1.0, "mfe": 0, "mae": 0,
                     "t1": False, "deadline": (T + timedelta(hours=8)).isoformat()}}
    mon.paper_exit(sig, 99.0, now, "stop")
    q = [c for c in mon.st["post_checks"] if c["kind"] == "manage"]
    assert len(q) == 1 and datetime.fromisoformat(q[0]["due"]) == T + timedelta(hours=24, minutes=2)
    mon.st["post_checks"] = q
    client.candles = lambda inst, g, n: [b(0, 99.9, 100.2), b(1, 100.2, 101.1), b(2, 98.9, 100.5, o=100.5)]
    mon.post_checks(T + timedelta(hours=24, minutes=5))
    ev = [e for e in store.events() if e["event"] == "manage_outcome"]
    assert ev and ev[0]["variants"]["breakeven"] == -0.02 and ev[0]["recorded_r"] is not None
    assert sender.texts == texts                     # shadow: nothing sent, no order
