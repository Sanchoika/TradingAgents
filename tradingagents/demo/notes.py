"""Human-readable output of a run: a short summary, an Obsidian-style note, a notifier."""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

SIGN_IN_HINT = "ChatGPT sign-in has lapsed: run  tradingagents login chatgpt  on the PC"


def auth_hint(error: str) -> str:
    """A plain next step when an error means the ChatGPT plan sign-in ended."""
    low = error.lower()
    if "chatgptautherror" in low or "login chatgpt" in low or "refresh token" in low:
        return SIGN_IN_HINT
    return ""


def weekly_text(result: dict) -> str:
    """Monday add-on to the digest: how the whole test stands."""
    n = result.get("trades", 0)
    if not n:
        return "Weekly scorecard: no closed trades yet."
    sides = ", ".join(f"{k} {v['trades']} ({v['mean_pnl']:+,.0f} avg)" for k, v in result.get("by_side", {}).items())
    lo, hi = result["mean_pnl_ci95"]
    return (f"Weekly scorecard: {n}/150 closed, P&L {result['total_pnl']:+,.0f}, win {100 * result['win_rate']:.0f}%, "
            f"avg/trade {result['mean_pnl']:+,.0f} (95% CI {lo:+,.0f} to {hi:+,.0f}), "
            f"random as good {100 * result['p_random_as_good']:.0f}%" + (f"; {sides}" if sides else ""))


def run_summary(rows: list[dict]) -> str:
    """A few lines a person can read on a phone."""
    start = next((r for r in rows if r["event"] == "run_start"), {})
    head = (f"TradingAgents demo {start.get('trade_date', '')}: NAV {start.get('nav', 0):,.0f} "
            f"({-start.get('drawdown_pct', 0):+.1f}% since start")
    if start.get("benchmark_pct") is not None:
        head += f"; {start['benchmark']} {start['benchmark_pct']:+.1f}%"
    lines = [head + ")"]
    if start.get("missed_runs"):
        lines.append(f"WARNING: {start['missed_runs']} scheduled weekday run(s) missed since the last run")
    for r in rows:
        ev = r["event"]
        if ev == "earnings_scan":
            odd = [t for t, v in r.get("edgar_check", {}).items() if v == "NOT confirmed by EDGAR"]
            if odd:
                lines.append(f"CHECK: earnings date not confirmed by SEC EDGAR for {', '.join(odd)}")
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
            hint = auth_hint(r["error"])
            if hint and hint not in lines:
                lines.append(f"  {hint}")
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


def failure_text(exc: BaseException) -> str:
    text = f"TradingAgents demo run FAILED: {type(exc).__name__}: {str(exc)[:300]}"
    hint = auth_hint(f"{type(exc).__name__} {exc}")
    return f"{text}\n{hint}" if hint else text


RUN_END_SINCE = "2026-11-05"   # first trade date whose run writes run_end


def failed_run(events: list[dict]) -> dict | None:
    """The latest real run_end, if that run did not fully succeed (status partial or failed;
    run_ends written before statuses existed count by their ``ok`` flag)."""
    ends = [e for e in events if e["event"] == "run_end" and not e.get("dry_run")]
    if not ends:
        return None
    last = ends[-1]
    status = last.get("status") or ("success" if last.get("ok", True) else "failed")
    return last if status != "success" else None


def missing_run(events: list[dict], now, run_hour: int = 8, grace_hours: int = 1) -> str | None:
    """Date of the latest weekday whose scheduled run has no journal entry, else None.

    ``now`` is a timezone-aware time; the schedule is New York time. A run counts when the
    journal has a real (not dry-run) ``run_end`` for that date or a later one: a run that
    crashed after ``run_start`` is not a success. A bare ``run_start`` is trusted only for
    dates before ``RUN_END_SINCE`` (when ``run_end`` was introduced) and before the first
    ``run_end``, so a journal that stops writing ``run_end`` cannot pass as healthy.
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    et = now.astimezone(ZoneInfo("America/New_York"))
    day = et.date()
    if et.hour < run_hour + grace_hours:
        day -= timedelta(days=1)
    while day.weekday() > 4:
        day -= timedelta(days=1)
    ends = [e["trade_date"] for e in events if e["event"] == "run_end" and not e.get("dry_run")]
    first_end = min(ends, default=None)
    done = ends + [e["trade_date"] for e in events
                   if e["event"] == "run_start" and not e.get("dry_run")
                   and e["trade_date"] < RUN_END_SINCE
                   and (first_end is None or e["trade_date"] < first_end)]
    return None if done and max(done) >= day.isoformat() else day.isoformat()
