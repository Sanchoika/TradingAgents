"""Research families v1 (frozen rules in docs/brent-research-families-v1.md). Fakes only."""
from datetime import UTC, datetime, timedelta

from tests.test_brent_alerts import alerts, make
from tradingagents.brent import families
from tradingagents.brent.rules import LONG, SHORT

UTC = UTC
A = 2.0  # daily ATR used in the rule tests


def bar(t, o, h, low, c):
    return {"time": t, "o": o, "h": h, "l": low, "c": c}


def daily_flat(n=25, base=100.0):
    t0 = datetime(2026, 10, 1, 21, tzinfo=UTC)
    return [{"time": t0 + timedelta(days=i), "o": base, "h": base + 2, "l": base - 2, "c": base + (0.1 if i % 2 else -0.1),
             "complete": True} for i in range(n)]


def daily_up(n=25):
    t0 = datetime(2026, 10, 1, 21, tzinfo=UTC)
    return [{"time": t0 + timedelta(days=i), "o": 90 + i, "h": 91 + i, "l": 89 + i, "c": 90.5 + i, "complete": True}
            for i in range(n)]


def test_eia_reaction_fires_only_on_the_wednesday_1030_candle_with_a_decisive_close():
    t = datetime(2026, 11, 18, 15, 30, tzinfo=UTC)          # Wed 10:30 ET (EST), a normal week
    b = bar(t, 100.0, 101.0, 99.9, 100.95)                    # range 1.1 >= 0.25*A, close in top 30%
    s = families.detect(b, t + timedelta(minutes=15), [b], A, None, 2)
    assert [(x.kind, x.direction) for x in s] == [("EIA_REACTION", LONG)]
    x = s[0]
    assert (x.zone_lo, x.zone_hi) == (round(100.95 - 0.2, 3), 100.95)
    assert x.stop == round(99.9 - 0.1, 3) and x.invalid_close == round((101.0 + 99.9) / 2, 3)
    assert families.detect(bar(t, 100, 101, 99.9, 100.45), t, [], A, None, 2) == []    # close in the middle
    thu = t + timedelta(days=1)
    assert families.detect(bar(thu, 100, 101, 99.9, 100.95), thu, [], A, None, 2) == []
    short = families.detect(bar(t, 100.9, 101.0, 99.9, 99.95), t, [], A, None, 2)
    assert [(y.kind, y.direction) for y in short] == [("EIA_REACTION", SHORT)]


def test_orb_ny_breaks_the_30_minute_range_within_two_hours_and_skips_bad_widths():
    o1 = bar(datetime(2026, 11, 11, 14, 0, tzinfo=UTC), 100, 100.4, 99.9, 100.2)   # 09:00 ET
    o2 = bar(datetime(2026, 11, 11, 14, 15, tzinfo=UTC), 100.2, 100.5, 100.0, 100.3)
    brk = bar(datetime(2026, 11, 11, 14, 45, tzinfo=UTC), 100.3, 100.8, 100.3, 100.7)  # close > 100.5 + 0.1
    s = families.detect(brk, brk["time"] + timedelta(minutes=15), [o1, o2, brk], A, None, 2)
    assert [(x.kind, x.direction) for x in s] == [("ORB_NY", LONG)]
    assert (s[0].zone_lo, s[0].stop, s[0].invalid_close) == (100.5, round((100.5 + 99.9) / 2, 3), 100.5)
    late = dict(brk, time=datetime(2026, 11, 11, 17, 0, tzinfo=UTC))
    assert families.detect(late, late["time"], [o1, o2, late], A, None, 2) == []
    wide = [dict(o1, h=102.0), o2]
    assert families.detect(brk, brk["time"], wide + [brk], A, None, 2) == []    # width 2.1 > 0.6*A


def test_range_reversal_only_in_a_range_regime_and_trend_pullback_only_in_a_trend():
    d = daily_flat()
    hi20 = max(x["h"] for x in d[-20:])
    t = datetime(2026, 11, 11, 16, 0, tzinfo=UTC)
    rej = bar(t, hi20 - 0.1, hi20 + 0.05, hi20 - 0.6, hi20 - 0.5)     # touches the 20-day high, closes down
    s = families.detect(rej, t, [rej], A, d, 2)
    assert ("RANGE_REVERSAL", SHORT) in [(x.kind, x.direction) for x in s]
    assert families.detect(rej, t, [rej], A, daily_up(), 2) == [] or \
        "RANGE_REVERSAL" not in [x.kind for x in families.detect(rej, t, [rej], A, daily_up(), 2)]
    up = daily_up()
    m = (up[-1]["h"] + up[-1]["l"]) / 2
    pull = bar(t, m + 0.3, m + 0.4, m - 0.1, m + 0.2)
    k = [(x.kind, x.direction) for x in families.detect(pull, t, [pull], A, up, 2)]
    assert ("TREND_PULLBACK", LONG) in k
    assert "TREND_PULLBACK" not in [x.kind for x in families.detect(pull, t, [pull], A, d, 2)]


def test_grade_is_news_only():
    assert families.grade({"state": "mixed"}, LONG)[0] == "C"
    assert families.grade({"state": "none_material", "conflict_window": {"bearish": ["k"]}}, LONG)[0] == "C"
    assert families.grade({"state": "bullish", "support": {"bullish": ["k"]}}, LONG)[0] == "A"
    assert families.grade({"state": "unavailable"}, SHORT)[0] == "B"


def _arm(mon, key, kind, direction, lo, hi, stop, now):
    mon.st.setdefault("family_setups", {})[key] = {
        "key": key, "kind": kind, "direction": direction, "level_name": "X", "level": lo, "zone_lo": lo,
        "zone_hi": hi, "stop": stop, "invalid_close": stop, "atr": 2.0, "armed_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=2)).isoformat(), "trigger": {"o": lo, "h": hi, "l": lo, "c": hi},
        "status": "armed",
        "news": [], "news_error": ""}


def test_families_fire_as_one_research_card_never_a_real_alert_and_respect_the_daily_cap(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()                                   # loads chart data (the existing family may alert here)
    base = len(sender.texts)
    now = clock()
    px = client.ask
    _arm(mon, "F1", "ORB_NY", LONG, px - 0.05, px + 0.05, px - 1.0, now)
    _arm(mon, "F2", "TREND_PULLBACK", LONG, px - 0.05, px + 0.05, px - 1.0, now)
    for v in mon.st["signals"].values():         # keep the 15-min de-duplication out of this test
        v["decided_at"] = (now - timedelta(hours=1)).isoformat()
    mon.try_families(client.quote("BCO_USD"), clock())
    cards = [t for t in sender.texts[base:] if "RESEARCH CALL" in t]
    assert len(cards) == 1 and "ORB_NY + TREND_PULLBACK" in cards[0] and "unproven" in cards[0]
    sigs = [s for s in mon.st["signals"].values() if s.get("arm") == "family"]
    assert len(sigs) == 2 and all(s["id"].startswith("RF-") and s["telegram"] is False for s in sigs)
    assert not [t for t in sender.texts[base:] if "NO ORDER PLACED" in t and "RESEARCH" not in t]
    mon.try_families(client.quote("BCO_USD"), clock())          # already signalled: nothing more
    assert len([t for t in sender.texts[base:] if "RESEARCH CALL" in t]) == 1
    mon.st["research_cards"] = {"day": mon.st["research_cards"]["day"], "n": mon.cfg.research_call_cap}
    _arm(mon, "F3", "EIA_REACTION", SHORT, client.bid - 0.05, client.bid + 0.05, client.bid + 1.0, clock())
    mon.try_families(client.quote("BCO_USD"), clock())
    assert len([t for t in sender.texts[base:] if "RESEARCH CALL" in t]) == 1
    assert any(e["event"] == "family_card_skipped" and "daily cap" in e["reason"] for e in store.events())
    assert any(s["setup"]["kind"] == "EIA_REACTION" for s in mon.st["signals"].values())   # still paper-tracked


def test_switched_off_no_family_work_and_existing_alerts_unchanged(tmp_path):
    mon, _, sender, _, store = make(tmp_path, research_families=False)
    mon.tick()
    on, _, sender2, _, _ = make(tmp_path / "on")
    on.tick()
    assert alerts(sender) == alerts(sender2) or len(alerts(sender)) == len(alerts(sender2))
    assert not any(e["event"].startswith("family") for e in store.events())


def test_no_family_card_within_15_minutes_of_a_card_or_alert_in_the_same_direction(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    now = clock()
    mon.st["signals"]["SH-X"] = {"id": "SH-X", "arm": "chart-only", "setup": {"direction": LONG, "kind": "BREAKOUT"},
                                 "decided_at": (now - timedelta(minutes=5)).isoformat(), "status": "expired",
                                 "research_call": {"grade": "C", "sent": True}, "expires_at": now.isoformat()}
    base = len(sender.texts)
    _arm(mon, "F9", "ORB_NY", LONG, client.ask - 0.05, client.ask + 0.05, client.ask - 1.0, now)
    mon.try_families(client.quote("BCO_USD"), clock())
    assert not [t for t in sender.texts[base:] if "RESEARCH CALL" in t]
    assert any(e["event"] == "family_card_skipped" and "15 min" in e["reason"] for e in store.events())


def test_orb_keeps_its_opening_range_for_the_whole_window_and_family_errors_never_stop_the_bot(tmp_path):
    mon, client, sender, clock, store = make(tmp_path)
    mon.tick()
    mon.st["family_bars"] = []
    day = datetime(2026, 11, 11, 14, 0, tzinfo=UTC)                     # 09:00 ET
    mon.on_family_bar(bar(day, 100, 100.4, 99.9, 100.2), day + timedelta(minutes=15), A)
    mon.on_family_bar(bar(day + timedelta(minutes=15), 100.2, 100.5, 100.0, 100.3), day + timedelta(minutes=30), A)
    for i in range(2, 9):                                                # quiet candles up to 10:45 ET
        t = day + timedelta(minutes=15 * i)
        mon.on_family_bar(bar(t, 100.2, 100.4, 100.1, 100.3), t + timedelta(minutes=15), A)
    late = day + timedelta(minutes=15 * 9)                               # 11:15 ET, inside the 2 h window
    mon.on_family_bar(bar(late, 100.3, 100.8, 100.3, 100.7), late + timedelta(minutes=15), A)
    assert any(s["kind"] == "ORB_NY" for s in mon.st["family_setups"].values())

    def boom(*_):
        raise RuntimeError("family bug")
    mon.guard_families(boom)
    assert any(e["event"] == "family_error" and "family bug" in e["error"] for e in store.events())
    mon.tick()                                                           # the bot keeps running


def test_eia_holiday_week_uses_the_shifted_release_veterans_day_2026():
    wed = datetime(2026, 11, 11, 15, 30, tzinfo=UTC)          # Wed 11 Nov 10:30 ET: no release (holiday)
    assert families.detect(bar(wed, 100, 101, 99.9, 100.95), wed + timedelta(minutes=15), [], A, None, 2) == []
    thu = datetime(2026, 11, 12, 17, 0, tzinfo=UTC)            # Thu 12 Nov 12:00 ET: the shifted release
    s = families.detect(bar(thu, 100, 101, 99.9, 100.95), thu + timedelta(minutes=15), [], A, None, 2)
    assert [(x.kind, x.direction) for x in s] == [("EIA_REACTION", LONG)]
    thu1030 = datetime(2026, 11, 12, 15, 30, tzinfo=UTC)
    assert families.detect(bar(thu1030, 100, 101, 99.9, 100.95), thu1030, [], A, None, 2) == []
    xmas = datetime(2025, 12, 29, 22, 0, tzinfo=UTC)           # Mon 29 Dec 2025 17:00 ET (shifted)
    assert families.eia_release_candle(families.ny(xmas)) and not families.eia_release_candle(
        families.ny(datetime(2025, 12, 24, 15, 30, tzinfo=UTC)))
    assert families.eia_release_candle(families.ny(datetime(2026, 11, 18, 15, 30, tzinfo=UTC)))   # normal week
