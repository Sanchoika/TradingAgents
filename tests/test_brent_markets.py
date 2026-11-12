"""Multi-market desk v1 (docs/multi-market-desk-v1.md): research-only market instances. Fakes only."""
from tests.test_brent_alerts import Clock, FakeClient, Sender, alerts
from tradingagents.brent import families
from tradingagents.brent.config import BrentConfig
from tradingagents.brent.engine import Monitor
from tradingagents.brent.store import Store


def market(tmp_path, **kw):
    clock = Clock()
    cfg = BrentConfig(journal_dir=str(tmp_path / "m"), research_only=True, display_name="GOLD",
                      alerts_enabled=False, info_alerts=False, **kw)
    client, sender, store = FakeClient(clock), Sender(clock), Store(cfg.journal_dir)
    return Monitor(cfg, client, store, sender, now=clock), sender, store, clock


def test_research_only_market_sends_a_research_card_never_a_real_alert_and_needs_no_news(tmp_path):
    mon, sender, store, clock = market(tmp_path)
    for _ in range(3):                     # the same with-trend setup that is a real alert on Brent
        mon.tick()
        clock.advance(60)
    assert not alerts(sender) and not [t for t in sender.texts if "WATCH" in t.split("\n")[0]]
    cards = [t for t in sender.texts if "RESEARCH CALL" in t]
    assert len(cards) == 1 and "GOLD" in cards[0] and "no news check" in cards[0] and "BRENT" not in cards[0]
    from tradingagents.brent import messages
    start = messages.start_text("v", "h", "acct", "", False, "GOLD", True)
    assert "GOLD MONITOR STARTED" in start and "research cards only" in start and "BRENT" not in start
    rng = messages.range_text(1.08123, 0.8, 1.6, False, "EURUSD", 5)
    assert "EURUSD ABOVE RANGE" in rng or "EURUSD BELOW RANGE" in rng
    assert "1.08123" in rng and "BRENT" not in rng
    assert not any(e["event"] == "signal" and e["arm"] == "combined" for e in store.events())
    assert not mon.watch_news_process
    calls = [e for e in store.events() if e["event"] == "research_call"]
    assert calls[0]["grade"] == "B"


def test_brent_defaults_unchanged_and_decimals_flow_into_rounding():
    b = BrentConfig()
    assert (b.research_only, b.price_decimals, b.display_name, b.eia_family) == (False, 3, "BRENT", True)
    assert BrentConfig().config_hash() == BrentConfig(display_name="X", research_only=True).config_hash()
    assert "research_only" not in __import__("tradingagents.brent.config", fromlist=["RULE_FIELDS"]).RULE_FIELDS


def test_eia_family_off_for_non_oil_markets():
    from datetime import UTC, datetime, timedelta
    t = datetime(2026, 11, 18, 15, 30, tzinfo=UTC)          # Wed 10:30 ET release candle
    b = {"time": t, "o": 1.08000, "h": 1.08500, "l": 1.07990, "c": 1.08480}
    on = families.detect(b, t + timedelta(minutes=15), [b], 0.008, None, 2, 5, True)
    assert [x.kind for x in on] == ["EIA_REACTION"] and on[0].zone_hi == 1.0848 and on[0].stop == 1.0795
    assert families.detect(b, t + timedelta(minutes=15), [b], 0.008, None, 2, 5, False) == []


def test_shipped_market_configs_are_research_only_with_their_own_journals():
    from pathlib import Path

    from tradingagents.brent.config import load_config
    seen = set()
    for p in sorted(Path("deploy/markets").glob("*.toml")):
        c = load_config(p)
        assert c.research_only and not c.alerts_enabled and not c.info_alerts and c.risk_pct is None
        assert c.research_call_cap <= 3 and c.journal_dir.endswith(p.stem) and c.instrument != "BCO_USD"
        assert c.eia_family == (c.instrument == "WTICO_USD")
        seen.add(c.instrument)
    assert seen == {"XAU_USD", "WTICO_USD", "EUR_USD", "NAS100_USD", "USD_CAD"}
