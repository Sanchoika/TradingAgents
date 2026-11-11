"""Telegram texts in the "6A card" style (Telegram HTML): an icon and bold title, a heavy
divider, label/value rows with prices in code boxes, one italic note, a short footer. The long
detail (exact validity rule, quote, costs, news and chart lines, position check) sits in a
collapsed quote under the card, so every safety statement is still in the message.
Every message carries its paper/no-order status in the footer."""

from __future__ import annotations

import html
from datetime import UTC, datetime

from .rules import LONG, NY

DIV = "━━━━━━━━━━━━"


def hms(dt: datetime, tz=NY) -> str:
    return dt.astimezone(tz).strftime("%H:%M:%S")


def hm(dt: datetime, tz=NY) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


def stamp(dt: datetime) -> str:
    """'14:32:10 ET (19:32:10 UTC)'."""
    return f"{hms(dt)} ET ({hms(dt, UTC)} UTC)"


def esc(x) -> str:
    return html.escape(str(x), quote=False)


def code(x) -> str:
    return f"<code>{esc(x)}</code>"


def link(url: str, label: str = "report") -> str:
    if url and url.startswith(("http://", "https://")):
        return f'<a href="{html.escape(url, quote=True)}">{esc(label)}</a>'
    return ""


def card(icon: str, title: str, rows=(), note: str = "", foot: str = "", details=(),
         lead: str = "", url: str = "") -> str:
    """One message. ``rows`` are (label, value, boxed) with value already a plain string;
    ``details`` are plain lines shown in a collapsed quote. All plain text is escaped here."""
    if "BRENT" not in title:          # the chat also gets the stock digest: always say Brent
        title = f"BRENT {title}"
    out = [f"{icon} <b>{esc(title)}</b>", DIV]
    if lead:
        out.append(f"<b>{esc(lead)}</b>")
    for label, value, *boxed in rows:
        out.append(f"{esc(label)}  {code(value) if (boxed[0] if boxed else True) else esc(value)}")
    if note:
        out += [DIV, f"<i>{esc(note)}</i>"]
    tail = " · ".join(x for x in (esc(foot) if foot else "", link(url)) if x)
    if tail:
        out.append(tail)
    text = "\n".join(out)
    det = [d for d in details if d]
    if det:
        text += "\n<blockquote expandable>" + "\n".join(esc(d) for d in det) + "</blockquote>"
    return text


def alert_text(sig: dict, extra: dict) -> str:
    """ENTRY card: what to do, until when, stop, targets, main risk; full detail collapsed."""
    s, q, p = sig["setup"], sig["quote"], sig["plan"]
    long_ = s["direction"] == LONG
    side, px_name = ("BUY", "ask") if long_ else ("SELL", "bid")
    expiry = datetime.fromisoformat(sig["expires_at"])
    qt = datetime.fromisoformat(q["time"])
    skips = "; ".join(sig.get("skip_reasons") or [])
    note = f"Risk: {sig.get('contradiction', 'n/a')}" + (f" Also: {skips}." if skips else "")
    rows = [("Entry", f"{side} {px_name} {s['zone_lo']:.2f}–{s['zone_hi']:.2f}"),
            ("Stop", f"{s['stop']:.2f}"),
            ("Target", f"{p['t1']:.2f} · {p['t2']:.2f}"),
            ("Until", f"{hms(expiry)} ET")]
    if sig.get("grade"):
        rows.append(("Grade", f"{sig['grade']} · {sig.get('grade_reason', '')}"))
    details = [
        f"{sig['id']} | {sig.get('label', '')}"
        + (f" | upgrade of research call {sig['from_research']}" if sig.get("from_research") else "")
        + (f" | follows WATCH {sig['from_watch']}" if sig.get("from_watch") else ""),
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
        f"Reward/risk after costs: {p['rr1']:.2f} at 1R, {p['rr2']:.2f} at 2R ({p['costs_note']})",
        f"Size: {sig['sizing']}",
        "Other reasons to skip: " + (skips or "none flagged by the checks (still your call)"),
        f"Position check: {sig['position_note']}",
        f"Policy row: {sig.get('policy_row', 'n/a')}. Decided "
        f"{stamp(datetime.fromisoformat(sig['decided_at']))}. Rules {sig['rules']} cfg "
        f"{sig['config_hash']}. Paper tracking only.",
        f"Report and chart: {sig['report_url']}" if sig.get("report_url") else "",
    ]
    return card("🟢" if long_ else "🔴", f"{s['direction']} BRENT", rows, note,
                f"paper · NO ORDER PLACED · {sig['id']}", details, url=sig.get("report_url", ""))


def research_text(sig: dict, grade: str, reason: str, extra: dict) -> str:
    """RESEARCH CALL card: a chart setup that is NOT a confirmed alert (grade B or C)."""
    s, q, p = sig["setup"], sig["quote"], sig["plan"]
    long_ = s["direction"] == LONG
    side, px_name = ("BUY", "ask") if long_ else ("SELL", "bid")
    expiry = datetime.fromisoformat(sig["expires_at"])
    rows = [("Entry", f"{side} {px_name} {s['zone_lo']:.2f}–{s['zone_hi']:.2f}"),
            ("Stop", f"{s['stop']:.2f}"),
            ("Target", f"{p['t1']:.2f} · {p['t2']:.2f}"),
            ("Grade", f"{grade} · {reason}")]
    note = ("Research only: NOT confirmed by news. " + ("News conflicts: information only." if grade == "C"
            else "Chart setup without news support.") + " Paper and practice track it; a real alert may follow.")
    details = [
        f"{sig['id']} | research track ({s['kind']} {s['direction']}) | grade {grade}",
        f"Quote {stamp(datetime.fromisoformat(q['time']))}: bid {q['bid']:.3f} / ask {q['ask']:.3f} / spread {q['spread']:.3f}",
        f"Zone valid until {stamp(expiry)}; stop {s['stop']:.2f}; a 15-min close {'below' if long_ else 'above'} "
        f"{s['invalid_close']:.2f} cancels it. Targets 1R {p['t1']:.2f} | 2R {p['t2']:.2f}; hold up to {extra['max_hold_hours']:g}h.",
        f"Why: {sig.get('why', '')}",
        f"Rules {sig['rules']} cfg {sig['config_hash']}. Paper tracking only; no order placed.",
    ]
    return card("🔬", f"RESEARCH CALL · {s['direction']} BRENT", rows, note,
                f"research · NOT AN ALERT · {sig['id']}", details)


ENTRY_ONLY = ("This cancels the entry suggestion only. No broker order or position was "
              "changed; if you already entered, this does not close it: manage it yourself.")


def invalidated_text(sig_id: str, when: datetime, reason: str) -> str:
    return card("⛔", f"SKIP {sig_id}", [("Why", reason, False), ("At", f"{hms(when)} ET")],
                "Do not chase. Open trades not touched.", "paper · no order placed",
                [f"Entry no longer valid at {stamp(when)}: {reason}.", ENTRY_ONLY])


def expired_text(sig_id: str, when: datetime) -> str:
    return card("⌛", f"EXPIRED {sig_id}", [("At", f"{hms(when)} ET")],
                "Entry window ended. Do not enter.", "paper · no order placed", [ENTRY_ONLY])


def range_text(mid: float, low: float, high: float, inside: bool) -> str:
    rows = [("Brent", f"{mid:.2f}"), ("Range", f"{low:g}–{high:g}")]
    if inside:
        return card("📏", "BACK IN RANGE", rows,
                    f"Back inside your {low:g}-{high:g} operating range. New entry suggestions "
                    "resume when a setup qualifies.")
    where = "above" if mid > high else "below"
    return card("📏", f"{where.upper()} RANGE", rows,
                f"Brent is {where} your {low:g}-{high:g} operating range. New entry suggestions "
                "paused; monitoring continues.", "",
                ["Open paper signals continue. The range is yours to change; "
                 "the monitor never changes it."])


def ops_text(problem: str) -> str:
    return card("🛠", "PROBLEM", lead=problem)


def start_text(version: str, cfg_hash: str, account_line: str, experiment: str = "",
               alerts_enabled: bool | None = None) -> str:
    mode = "" if alerts_enabled is None else ("entry alerts ON" if alerts_enabled
                                             else "entry alerts OFF (shadow mode)")
    rows = [("Rules", version + (f", {experiment}" if experiment else "")), ("Config", cfg_hash)]
    if mode:
        rows.append(("Mode", mode, False))
    return card("⚙️", "MONITOR STARTED", rows, "Alerts only, no orders.", "test message",
                [account_line, "This message also measures how long the notify command takes to "
                 "accept a message (not when your phone shows it)."])


def switch_text(enabled: bool, m: dict) -> str:
    if enabled:
        return card("⚙️", "ENTRY ALERTS ON",
                    [("Code", m["code_commit"]), ("Rules", m["rules"]),
                     ("Policy", f"{m['news_policy']}, {m['paper_rules']}")],
                    "Alerts only: no orders, your positions are not touched.",
                    f"experiment {m['experiment']}",
                    ["Qualifying setups will send entry suggestions with SKIP/EXPIRED updates. "
                     "Untested strategy; paper tracking only."])
    return card("⚙️", "ENTRY ALERTS OFF", note="Shadow mode: decisions and paper tracking "
                "continue, nothing actionable is sent.")


def watch_text(watch_id: str, s: dict, mid: float, blocker: str, news_line: str, chart_line: str,
               first: bool, report_url: str = "") -> str:
    """WATCH: a defined setup is forming or its state changed. Never an entry instruction."""
    long_ = s["direction"] == LONG
    px_name = "ask" if long_ else "bid"
    head = "WATCH" if first else "WATCH UPDATE"
    exp = datetime.fromisoformat(s["expires_at"])
    rows = [("Zone", f"{s['zone_lo']:.2f}–{s['zone_hi']:.2f} ({px_name})"),
            ("Now", f"{mid:.2f}"),
            ("Needs", blocker, False),
            ("Off", f"close through {s['invalid_close']:.2f} / {hm(exp)} ET")]
    return card("👀", f"{head} {s['direction']}", rows,
                f"{s['kind']} at {s['level_name']} {s['level']:.2f}.",
                f"NOT AN ENTRY · no order placed · {watch_id}",
                [f"Next confirmation: a separate ENTRY message follows only if the {px_name} is inside the zone with "
                 "every entry check passing. This message is not a signal to trade.",
                 f"News (headlines/snippets only): {news_line}", f"Chart: {chart_line}",
                 f"Setup {s['key']}. Dropped if a 15-min close goes back through "
                 f"{s['invalid_close']:.2f} or it is not triggered by {stamp(exp)}."],
                url=report_url)


def watch_end_text(watch_id: str, when: datetime, reason: str, entry_id: str = "") -> str:
    if entry_id:
        return ""
    return card("✖️", f"WATCH CANCELLED {watch_id}", [("Why", reason, False), ("At", f"{hms(when)} ET")],
                "Setup no longer developing. Nothing to do.", "no order placed",
                [f"Cancelled at {stamp(when)}. No order was placed and no position was changed."])
