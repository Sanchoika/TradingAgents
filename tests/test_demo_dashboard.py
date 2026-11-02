"""Read-only dashboard: snapshot from saved files, static site, no account changes."""

import hashlib
import json
from datetime import UTC, datetime

import pandas as pd
import pytest

from tradingagents.demo.config import DemoConfig
from tradingagents.demo.dashboard import publishable, render, write_site
from tradingagents.demo.snapshot import build_snapshot

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)


def _cfg(tmp_path, **kw):
    return DemoConfig(journal_dir=str(tmp_path), mode="earnings", universe=["NVDA", "AMD", "AAPL"],
                      notify_command="hermes send --to telegram SECRET-CMD", **kw)


def _rows(report):
    return [
        {"ts": "2026-11-01T04:21:22+00:00", "event": "run_start", "trade_date": "2026-11-01", "nav": 100000.0,
         "start_nav": 100000.0, "drawdown_pct": 0.0, "halted": False, "dry_run": True, "mode": "earnings"},
        {"ts": "2026-11-01T04:21:30+00:00", "event": "decision", "instrument": "AMD", "ticker": "AMD",
         "trade_date": "2026-11-01", "rating": "Sell", "target": -1, "actions": ["open"], "decision": "dry"},
        {"ts": "2026-11-02T13:00:00+00:00", "event": "run_start", "trade_date": "2026-11-02", "nav": 100000.0,
         "start_nav": 100000.0, "drawdown_pct": 0.0, "halted": False, "dry_run": False, "mode": "earnings",
         "missed_runs": 0, "benchmark": "SPY", "benchmark_pct": 0.0, "llm": "chatgpt:gpt-6-luna/gpt-6-sol"},
        {"ts": "2026-11-02T13:00:30+00:00", "event": "earnings_scan", "reported": ["NVDA", "AAPL", "AMD"],
         "analysing": ["NVDA", "AAPL"], "skipped_over_limit": ["AMD"],
         "report_times": {"NVDA": "2026-11-01T21:00:00+00:00 (after close)"},
         "edgar_check": {"NVDA": "confirmed by EDGAR"}},
        {"ts": "2026-11-02T13:10:00+00:00", "event": "decision", "instrument": "NVDA", "ticker": "NVDA",
         "trade_date": "2026-11-02", "rating": "Buy", "target": 1, "actions": ["open"],
         "decision": "<script>alert(1)</script> strong quarter", "report": report},
        {"ts": "2026-11-02T13:10:05+00:00", "event": "open", "instrument": "NVDA", "rating": "Buy", "trade_id": "1",
         "units": 10, "stop": 180.0},
        {"ts": "2026-11-02T13:20:00+00:00", "event": "decision", "instrument": "AAPL", "ticker": "AAPL",
         "trade_date": "2026-11-02", "rating": "Hold", "target": 0, "actions": [], "decision": "wait"},
        {"ts": "2026-11-02T13:25:00+00:00", "event": "error", "instrument": "MSFT", "ticker": "MSFT",
         "error": "RateLimitError: 429"},
        {"ts": "2026-11-05T13:00:00+00:00", "event": "run_start", "trade_date": "2026-11-05", "nav": 101000.0,
         "start_nav": 100000.0, "drawdown_pct": -1.0, "halted": False, "dry_run": False, "mode": "earnings",
         "missed_runs": 1, "benchmark": "SPY", "benchmark_pct": 0.5},
    ]


def _paper():
    return {"cash": 100250.0, "next_id": 5, "open": [
        {"id": "1", "instrument": "NVDA", "units": 10, "status": "open", "ordered_on": "2026-11-02",
         "filled_on": "2026-11-03", "price": 200.0, "stop": 180.0, "stop_distance": 20.0, "comment": "Buy"},
        {"id": "3", "instrument": "AMD", "units": -5, "status": "pending", "ordered_on": "2026-11-05",
         "stop_distance": 8.0, "comment": "Sell"},
        {"id": "4", "instrument": "AAPL", "units": 3, "status": "closing", "ordered_on": "2026-10-20",
         "filled_on": "2026-10-21", "price": 250.0, "stop": 240.0, "close_ordered_on": "2026-11-05"}],
        "closed": [{"id": "2", "instrument": "XOM", "units": -20, "price": 110.0, "close_price": 97.5,
                    "realizedPL": 250.0, "financing": -1.5, "opened": "2026-11-03", "closed": "2026-11-04",
                    "reason": "stop", "spread_cost": 2.1, "recorded": "2026-11-04T13:00:00+00:00"}]}


def _setup(tmp_path):
    rep = tmp_path / "reports" / "2026-11-02_NVDA"
    rep.mkdir(parents=True)
    (rep / "complete_report.md").write_text("# NVDA report", encoding="utf-8")
    (rep / "complete_report.html").write_text("<h1>NVDA report</h1>", encoding="utf-8")
    with (tmp_path / "journal.jsonl").open("w", encoding="utf-8") as fh:
        for r in _rows(str(rep / "complete_report.md")):
            fh.write(json.dumps(r) + "\n")
        fh.write("{not json\n")
    (tmp_path / "paper_account.json").write_text(json.dumps(_paper()), encoding="utf-8")
    (tmp_path / "state.json").write_text(json.dumps({"last_run_date": "2026-11-05", "retry": {"AMD": "2026-11-02"}}),
                                         encoding="utf-8")


def _bars(symbol, as_of):
    return pd.DataFrame({"Date": pd.bdate_range("2026-10-01", "2026-11-11"), "Open": 210.0, "Close": 210.0})


def _digest(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}


def test_snapshot_reads_saved_files_and_never_changes_them(tmp_path):
    _setup(tmp_path)
    before = _digest(tmp_path)
    snap = build_snapshot(_cfg(tmp_path), bars=_bars, schedule=False, now=NOW)
    assert _digest(tmp_path) == before  # read-only: journal, account and state untouched

    assert [r["trade_date"] for r in snap["runs"] if not r["dry_run"]] == ["2026-11-02", "2026-11-05"]
    assert snap["sources"]["journal"]["bad_lines"] == 1
    by = {d["symbol"]: d for d in snap["decisions"]}
    assert "AMD" in by and by["AMD"]["status"] == "not analysed"          # over the limit, not the dry-run Sell
    assert by["NVDA"]["status"] == "order placed" and by["NVDA"]["trade_id"] == "1"
    assert by["NVDA"]["edgar_check"] == "confirmed by EDGAR" and by["NVDA"]["model"].startswith("chatgpt")
    assert by["AAPL"]["status"] == "no trade" and by["MSFT"]["status"] == "failed"

    acct = snap["account"]
    pos = {p["symbol"]: p for p in acct["positions"]}
    assert pos["AMD"]["status"] == "pending" and pos["AMD"]["mark"]["close"] == 210.0
    assert "unrealized" not in pos["AMD"]["mark"]                         # not filled: no P&L
    assert pos["NVDA"]["mark"]["unrealized"] == pytest.approx(100.0)
    assert pos["NVDA"]["planned_exit_request"] == "2026-11-10"             # 5 weekdays after the fill
    assert pos["AAPL"]["exit_requested_on"] == "2026-11-05"
    assert pos["NVDA"]["decision_index"] is not None
    assert acct["realized_pl"] == 250.0 and acct["financing"] == -1.5
    # equity at the last run (101,000) minus cash at that time (100,000 + 250 - 1.5)
    assert acct["unrealized_last_run"] == pytest.approx(751.5)
    assert snap["extras"]["score"]["trades"] == 1
    assert snap["completion"]["recorded"] is False


def test_page_escapes_text_hides_paths_and_secrets(tmp_path):
    _setup(tmp_path)
    snap = build_snapshot(_cfg(tmp_path), bars=_bars, schedule=False, now=NOW)
    page = render(snap)
    assert "<script>alert(1)</script>" not in page
    assert "SECRET-CMD" not in page and "notify_command" not in page
    assert str(tmp_path) not in json.dumps(publishable(snap))              # local paths stay on the PC
    assert "PAPER MODE" in page and "completion not recorded" in page.lower()


def test_site_folder_copies_reports_and_works_offline(tmp_path):
    _setup(tmp_path)
    snap = build_snapshot(_cfg(tmp_path), bars=_bars, schedule=False, now=NOW)
    index = write_site(snap, tmp_path / "site", tmp_path / "dashboard.html")
    site = tmp_path / "site"
    assert index.exists() and (site / "data.json").exists() and (site / "manifest.webmanifest").exists()
    assert (site / "reports" / "2026-11-02_NVDA.html").read_text(encoding="utf-8") == "<h1>NVDA report</h1>"
    data = json.loads((site / "data.json").read_text(encoding="utf-8"))
    nvda = next(d for d in data["decisions"] if d["symbol"] == "NVDA")
    assert nvda["report_href"] == "reports/2026-11-02_NVDA.html"
    assert "site/reports/2026-11-02_NVDA.html" in (tmp_path / "dashboard.html").read_text(encoding="utf-8")


def test_empty_and_missing_files(tmp_path):
    snap = build_snapshot(_cfg(tmp_path), prices=False, schedule=False, now=NOW)
    assert snap["sources"]["journal"]["status"] == "missing"
    assert snap["account"] is None and snap["decisions"] == [] and snap["runs"] == []
    assert render(snap).startswith("<!doctype html>")
    (tmp_path / "paper_account.json").write_text("{broken", encoding="utf-8")
    snap = build_snapshot(_cfg(tmp_path), prices=False, schedule=False, now=NOW)
    assert snap["sources"]["paper_account"]["status"].startswith("unreadable")


def test_price_failure_is_unavailable_not_zero(tmp_path):
    _setup(tmp_path)

    def broken(symbol, as_of):
        raise ConnectionError("offline")

    snap = build_snapshot(_cfg(tmp_path), bars=broken, schedule=False, now=NOW)
    assert all("error" in p["mark"] for p in snap["account"]["positions"])
    assert snap["extras"]["prices"]["ok"] == 0
