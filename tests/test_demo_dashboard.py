"""Dashboard page built from the journal and paper account. Offline."""

import pytest

from tradingagents.demo.dashboard import collect, render, write_dashboard
from tradingagents.demo.journal import Journal

pytestmark = pytest.mark.unit


def _events():
    return [
        {"event": "run_start", "trade_date": "2026-11-02", "nav": 100000, "start_nav": 100000,
         "benchmark_pct": 0.0, "missed_runs": 0, "dry_run": False},
        {"event": "earnings_scan", "reported": ["NVDA"]},
        {"event": "decision", "instrument": "NVDA", "trade_date": "2026-11-02", "rating": "Buy",
         "actions": ["open"], "decision": "<script>alert(1)</script> strong quarter"},
        {"event": "open", "instrument": "NVDA", "rating": "Buy", "trade_id": "1"},
        {"event": "run_start", "trade_date": "2026-11-03", "nav": 99000, "dry_run": True},  # ignored
        {"event": "decision", "instrument": "AMD", "rating": "Sell", "actions": ["open"]},   # dry: ignored
        {"event": "run_start", "trade_date": "2026-11-05", "nav": 101000, "start_nav": 100000,
         "benchmark_pct": 0.5, "missed_runs": 1, "dry_run": False},
        {"event": "error", "instrument": "AAPL", "error": "429"},
    ]


def test_collect_skips_dry_runs_and_links_ratings_to_trades():
    closed = [{"id": "1", "instrument": "NVDA", "units": 5, "price": 100, "realizedPL": 250.0,
               "financing": 0, "opened": "2026-11-03", "closed": "2026-11-10", "reason": "rating",
               "spread_cost": 1.0}]
    d = collect(_events(), {"open": [], "closed": closed})
    assert [r["date"] for r in d["runs"]] == ["2026-11-02", "2026-11-05"]
    assert d["runs"][1]["errors"] == 1 and d["runs"][1]["missed"] == 1
    assert d["curve"][-1] == {"date": "2026-11-05", "acct": pytest.approx(1.0), "bench": 0.5}
    assert d["by_rating"]["Buy"] == {"n": 1, "wins": 1, "pnl": 250.0}
    assert dict(d["rating_counts"]) == {"Buy": 1}


def test_render_escapes_model_text_and_handles_empty(tmp_path):
    page = render(collect(_events(), None))
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "Account vs SPY" in page and "1 missed day(s)" in page and "1 error(s)" in page
    empty = render(collect([], None))
    assert "No runs yet" in empty and "Too early to judge" in empty
    j = Journal(tmp_path)
    j.write("run_start", trade_date="2026-11-02", nav=100000, start_nav=100000, dry_run=False)
    out = write_dashboard(j, tmp_path / "missing.json", tmp_path / "dashboard.html")
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
