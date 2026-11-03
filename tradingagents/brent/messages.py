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
    """The actionable alert: short block first, details after the divider."""
    s, q, p = sig["setup"], sig["quote"], sig["plan"]
    long_ = s["direction"] == LONG
    side, px_name = ("BUY", "ask") if long_ else ("SELL", "bid")
    expiry = datetime.fromisoformat(sig["expires_at"])
    qt = datetime.fromisoformat(q["time"])
    lines = [
        f"[BRENT PAPER/ADVISORY] {sig['id']} | NO ORDER PLACED",
        f"{s['direction']} {sig['instrument']} (Brent CFD, OANDA practice prices)",
        f"Entry: {side} only if {px_name} is {s['zone_lo']:.2f}-{s['zone_hi']:.2f}",
        f"Valid until {stamp(expiry)}, ONLY while the executable {px_name} stays within "
        f"{s['zone_lo']:.2f}-{s['zone_hi']:.2f} and spread is below {extra['max_spread']:g}. "
        "Otherwise skip. Check the broker quote before you submit.",
        f"Quote {stamp(qt)}: bid {q['bid']:.3f} / ask {q['ask']:.3f} / spread {q['spread']:.3f}",
        f"Stop-loss order (broker): {s['stop']:.2f}",
        f"Setup invalid if a 15-min candle closes {'below' if long_ else 'above'} "
        f"{s['invalid_close']:.2f} (a reason to skip or exit, not a broker order)",
        f"Targets (conditional): 1R {p['t1']:.2f} | 2R {p['t2']:.2f}",
        f"Holding period: up to {extra['max_hold_hours']:g}h; forecast judged at +4h",
        f"Reward/risk after costs: {p['rr1']:.2f} at 1R, {p['rr2']:.2f} at 2R "
        f"({p['costs_note']})",
        f"Size: {sig['sizing']}",
        "",
        "---",
        f"Why: {sig['why']}",
    ]
    lines.append("Reasons to skip: " + ("; ".join(sig.get("skip_reasons") or [])
                                        or "none flagged by the checks (still your call)"))
    lines.append(f"Position check: {sig['position_note']}")
    lines.append("News (context only; not a direction call, may already be priced in):")
    if s.get("news"):
        for n in s["news"][:3]:
            lines.append(f"- [{n['scope']}] {n['title']} ({n['publisher']}; published "
                         f"{n['published']}, received {n['received']}) {n['link']}")
    else:
        lines.append(f"- {s.get('news_error') or 'no Brent or oil items in the last 12h from Yahoo'}")
    lines.append(f"Decided {stamp(datetime.fromisoformat(sig['decided_at']))}. Rules {sig['rules']} "
                 f"cfg {sig['config_hash']}. Paper tracking only.")
    return "\n".join(lines)


def invalidated_text(sig_id: str, when: datetime, reason: str) -> str:
    return (f"[BRENT PAPER/ADVISORY] {sig_id} SKIP: no longer valid at {stamp(when)}: {reason}. "
            "Do not chase. A new evaluation and a new signal are needed.")


def expired_text(sig_id: str, when: datetime) -> str:
    return (f"[BRENT PAPER/ADVISORY] {sig_id} EXPIRED at {stamp(when)}. Do not enter; "
            "a new evaluation and a new signal are needed.")


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


def start_text(version: str, cfg_hash: str, account_line: str) -> str:
    return (f"[BRENT TEST] Brent alert monitor started ({version}, cfg {cfg_hash}). Alerts only, "
            f"no orders. {account_line} This message also measures Telegram delivery time.")
