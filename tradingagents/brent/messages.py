"""Telegram texts. Every message starts with a [BRENT ...] label; alerts say no order was placed."""

from __future__ import annotations

from datetime import UTC, datetime

from .rules import LONG, NY


def hms(dt: datetime, tz=NY) -> str:
    return dt.astimezone(tz).strftime("%H:%M:%S")


def stamp(dt: datetime) -> str:
    """'14:32:10 ET (19:32:10 UTC)'."""
    return f"{hms(dt)} ET ({hms(dt, UTC)} UTC)"


def alert_text(sig: dict, extra: dict) -> str:
    """The actionable alert: short block first (what to do, why, main risk), details after."""
    s, q, p = sig["setup"], sig["quote"], sig["plan"]
    long_ = s["direction"] == LONG
    side, px_name = ("BUY", "ask") if long_ else ("SELL", "bid")
    expiry = datetime.fromisoformat(sig["expires_at"])
    qt = datetime.fromisoformat(q["time"])
    lines = [
        f"[BRENT PAPER/ADVISORY] {sig['id']} | ENTRY | NO ORDER PLACED | {sig.get('label', '')}"
        + (f" | follows WATCH {sig['from_watch']}" if sig.get("from_watch") else ""),
        f"{s['direction']} {sig['instrument']}: {side} only if {px_name} is "
        f"{s['zone_lo']:.2f}-{s['zone_hi']:.2f}",
        f"Valid until {stamp(expiry)}, ONLY while the executable {px_name} stays within "
        f"{s['zone_lo']:.2f}-{s['zone_hi']:.2f} and spread is below {extra['max_spread']:g}. "
        "Otherwise skip. Check the broker quote before you submit.",
        f"Quote {stamp(qt)}: bid {q['bid']:.3f} / ask {q['ask']:.3f} / spread {q['spread']:.3f}",
        f"Suggested stop {s['stop']:.2f} (no order placed). A 15-min close "
        f"{'below' if long_ else 'above'} {s['invalid_close']:.2f} cancels this entry suggestion "
        "while it is open; after entry, the paper record exits only at the stop, the 2R target "
        "or the time limit.",
        f"Targets (conditional): 1R {p['t1']:.2f} | 2R {p['t2']:.2f} | hold up to "
        f"{extra['max_hold_hours']:g}h",
        f"News (headlines/snippets only): {sig.get('news_line', 'n/a')}",
        f"Chart: {sig.get('chart_line', 'n/a')}",
        f"Main risk / reason to skip: {sig.get('contradiction', 'n/a')}",
        f"Report and chart: {sig.get('report_url', 'n/a')}",
        "",
        "---",
        f"Reward/risk after costs: {p['rr1']:.2f} at 1R, {p['rr2']:.2f} at 2R ({p['costs_note']})",
        f"Size: {sig['sizing']}",
        "Other reasons to skip: " + ("; ".join(sig.get("skip_reasons") or [])
                                     or "none flagged by the checks (still your call)"),
        f"Position check: {sig['position_note']}",
        f"Policy row: {sig.get('policy_row', 'n/a')}. Decided "
        f"{stamp(datetime.fromisoformat(sig['decided_at']))}. Rules {sig['rules']} cfg "
        f"{sig['config_hash']}. Paper tracking only.",
    ]
    return "\n".join(lines)


ENTRY_ONLY = ("This cancels the entry suggestion only. No broker order or position was "
              "changed; if you already entered, this does not close it: manage it yourself.")


def invalidated_text(sig_id: str, when: datetime, reason: str) -> str:
    return (f"[BRENT PAPER/ADVISORY] {sig_id} SKIP: entry no longer valid at {stamp(when)}: "
            f"{reason}. Do not chase. {ENTRY_ONLY}")


def expired_text(sig_id: str, when: datetime) -> str:
    return (f"[BRENT PAPER/ADVISORY] {sig_id} EXPIRED at {stamp(when)}: the entry window ended. "
            f"Do not enter. {ENTRY_ONLY}")


def range_text(mid: float, low: float, high: float, inside: bool) -> str:
    if inside:
        return (f"[BRENT] Brent mid {mid:.2f} is back inside your {low:g}-{high:g} operating range. "
                "New entry suggestions resume when a setup qualifies.")
    where = "above" if mid > high else "below"
    return (f"[BRENT] Brent mid {mid:.2f} is {where} your {low:g}-{high:g} operating range. "
            "New entry suggestions are paused; monitoring and open paper signals continue. "
            "The range is yours to change; the monitor never changes it.")


def ops_text(problem: str) -> str:
    return f"[BRENT OPS] {problem}"


def start_text(version: str, cfg_hash: str, account_line: str, experiment: str = "",
               alerts_enabled: bool | None = None) -> str:
    mode = "" if alerts_enabled is None else (
        " Experimental entry alerts are ENABLED." if alerts_enabled
        else " Entry alerts are OFF (shadow mode: decisions logged, nothing sent).")
    exp = f", {experiment}" if experiment else ""
    return (f"[BRENT TEST] Brent alert monitor started ({version}{exp}, cfg {cfg_hash}). Alerts "
            f"only, no orders.{mode} {account_line} This message also measures how long the "
            "notify command takes to accept a message (not when your phone shows it).")


def switch_text(enabled: bool, m: dict) -> str:
    if enabled:
        return (f"[BRENT STATUS] Experimental Brent entry alerts are ENABLED ({m['experiment']}, "
                f"code {m['code_commit']}, rules {m['rules']}, {m['news_policy']}, "
                f"{m['paper_rules']}). Alerts only: no orders are placed and your positions are "
                "not touched. Qualifying setups will send entry suggestions with SKIP/EXPIRED "
                "updates. Untested strategy; paper tracking only.")
    return ("[BRENT STATUS] Brent entry alerts are OFF (shadow mode): decisions and paper "
            "tracking continue, nothing actionable is sent.")


def watch_text(watch_id: str, s: dict, mid: float, blocker: str, news_line: str, chart_line: str,
               first: bool, report_url: str = "") -> str:
    """WATCH: a defined setup is forming or its state changed. Never an entry instruction."""
    long_ = s["direction"] == LONG
    px_name = "ask" if long_ else "bid"
    head = "WATCH" if first else "WATCH UPDATE"
    return "\n".join([
        f"[BRENT WATCH] {watch_id} | {head} | NOT AN ENTRY | no order is placed",
        f"{s['kind']} {s['direction']} BCO_USD around {s['level_name']} {s['level']:.2f}: a setup is "
        f"developing. Possible future entry zone {s['zone_lo']:.2f}-{s['zone_hi']:.2f} ({px_name}); "
        f"mid now {mid:.2f}.",
        f"Current blocker: {blocker}",
        f"Next confirmation: the {px_name} inside the zone with every entry check passing; only then "
        "would a separate ENTRY message follow. This message is not a signal to trade.",
        f"News (headlines/snippets only): {news_line}",
        f"Chart: {chart_line}",
        f"Setup {s['key']}. Dropped if a 15-min close goes back through {s['invalid_close']:.2f} or "
        f"it is not triggered by {stamp(datetime.fromisoformat(s['expires_at']))}."
        + (f" Chart: {report_url}" if report_url else ""),
    ])


def watch_end_text(watch_id: str, when: datetime, reason: str, entry_id: str = "") -> str:
    if entry_id:
        return ""
    return (f"[BRENT WATCH] {watch_id} | CANCELLED at {stamp(when)}: {reason}. The setup is no "
            "longer developing. Nothing to do; no order was placed and no position was changed.")
