"""Human-readable output of a run: a short summary, an Obsidian-style note, a notifier."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path


def run_summary(rows: list[dict]) -> str:
    """A few lines a person can read on a phone."""
    start = next((r for r in rows if r["event"] == "run_start"), {})
    lines = [f"TradingAgents demo {start.get('trade_date', '')}: NAV {start.get('nav', 0):,.0f} "
             f"({-start.get('drawdown_pct', 0):+.1f}% since start)"]
    for r in rows:
        ev = r["event"]
        if ev == "earnings_scan":
            lines.append(f"Reported: {', '.join(r['reported']) or 'none'}"
                         + (f"; skipped (limit): {', '.join(r['skipped_over_limit'])}"
                            if r["skipped_over_limit"] else ""))
        elif ev == "decision":
            lines.append(f"{r['instrument']}: {r['rating']} -> {', '.join(r['actions'])}")
        elif ev in ("open", "open_dry_run"):
            lines.append(f"  order {r['instrument']} {r['units']:+g} units, stop {r['stop']}")
        elif ev in ("close", "paper_close"):
            pl = r.get("realizedPL", r.get("pl"))
            lines.append(f"  closed {r['instrument']}" + (f" P&L {float(pl):+,.2f}" if pl not in (None, "") else "")
                         + (f" ({r['reason']})" if r.get("reason") else ""))
        elif ev == "error":
            lines.append(f"  ERROR {r.get('instrument', '')}: {r['error'][:200]}")
    if len(lines) == 1:
        lines.append("Nothing to do today.")
    return "\n".join(lines)


def write_run_note(rows: list[dict], notes_dir: str | Path, trade_date: str) -> Path:
    """Append this run to the day's note: the summary, then each analysis in full."""
    folder = Path(notes_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{trade_date} TradingAgents.md"
    parts = []
    if not path.exists():
        parts.append(f"---\ndate: {trade_date}\ntags: [tradingagents, paper-trading]\n---\n")
    parts.append(f"## Run at {datetime.now().strftime('%H:%M')}\n\n```\n{run_summary(rows)}\n```\n")
    for r in rows:
        if r["event"] == "decision" and r.get("decision"):
            parts.append(f"### {r['instrument']}: {r['rating']}\n\n{r['decision']}\n")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(parts) + "\n")
    return path


def write_scorecard(result: dict, notes_dir: str | Path) -> Path:
    folder = Path(notes_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "TradingAgents Scorecard.md"
    rows = "\n".join(f"| {k} | {v} |" for k, v in result.items())
    verdict = ("Enough trades to judge." if result.get("enough_data")
               else "Under 150 closed trades: too early to judge.")
    path.write_text(f"# TradingAgents paper test: scorecard\n\nUpdated {datetime.now():%Y-%m-%d %H:%M}. "
                    f"{verdict}\n\n| measure | value |\n|---|---|\n{rows}\n\n"
                    "`p_random_as_good` = share of coin-flip versions of the same trades that did at "
                    "least as well. An edge needs the CI above 0 and this below about 0.05.\n",
                    encoding="utf-8")
    return path


def notify(command: str, text: str, timeout: int = 120) -> int:
    """Run the user's own notify command with the summary on stdin; its exit code."""
    return subprocess.run(command, shell=True, input=text, text=True, timeout=timeout,
                          capture_output=True).returncode
