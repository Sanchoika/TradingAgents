"""Request handling for the control page. Pure functions over (method, path, headers, body), so
every rule is testable without a server.

Security model (see docs/CONTROL.md):
- The listener binds to 127.0.0.1 only. Tailscale Serve proxies tailnet requests to it and adds the
  caller's identity in ``Tailscale-User-Login``; a request without an allowed identity is refused.
- Viewing (GET) never changes anything. Actions are POST only, each with a one-time nonce issued
  for that user and that action (CSRF token and replay guard in one). A used, expired, foreign or
  unknown nonce is refused and audited.
- Only the actions in ACTIONS exist. No shell input, no broker orders, no arbitrary commands: the
  only command that can run is the fixed ``restart_news_command`` from the config file.
- Every request that reaches an action is appended to an audit log (who, what, result).
- An acknowledgement is not proof: the response shows the state read back afterwards.
"""

from __future__ import annotations

import html
import json
import re
import secrets
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs

ACTIONS = {
    "brent_pause": "Pause Brent entry alerts",
    "brent_resume": "Resume Brent entry alerts",
    "brent_restart_news": "Restart the Brent news collector",
}
DISABLED = {"stock_retry": "Retry a failed stock run: the stock runner is not hosted here yet"}


@dataclass
class ControlConfig:
    allowed_users: list[str]
    brent_toml: str
    brent_journal: str = "~/.tradingagents/brent"
    demo_journal: str = ""
    # The PC's read-only dashboard snapshot, synced here after each run (syncguard). Used for the
    # stock status while the stock test runs on the PC; it is never written by this page.
    demo_snapshot: str = ""
    demo_snapshot_stale_hours: float = 26.0
    restart_news_command: str = ""
    audit_path: str = "~/.tradingagents/control/audit.jsonl"
    host: str = "127.0.0.1"
    port: int = 8787
    nonce_ttl: int = 600
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.allowed_users:
            raise ValueError("allowed_users must name at least one Tailscale login")
        if self.host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the control page must listen on localhost only (Tailscale Serve proxies it)")


def load_config(path: str | Path) -> ControlConfig:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    known = ControlConfig.__dataclass_fields__
    unknown = sorted(set(data) - set(known))
    if unknown:
        raise ValueError(f"unknown settings in {path}: {', '.join(unknown)}")
    return ControlConfig(**data)


def set_alerts_enabled(toml_path: str | Path, enabled: bool) -> bool:
    """Rewrite only the ``alerts_enabled`` line (atomic). Returns the value read back."""
    p = Path(toml_path).expanduser()
    text = p.read_text(encoding="utf-8")
    line = f"alerts_enabled = {'true' if enabled else 'false'}"
    new, n = re.subn(r"(?m)^alerts_enabled\s*=.*$", line, text)
    if n == 0:
        new = text.rstrip("\n") + "\n" + line + "\n"
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(new, encoding="utf-8")
    tmp.replace(p)
    return read_alerts_enabled(p)


def read_alerts_enabled(toml_path: str | Path) -> bool:
    with open(Path(toml_path).expanduser(), "rb") as fh:
        return bool(tomllib.load(fh).get("alerts_enabled", False))


class ControlApp:
    def __init__(self, cfg: ControlConfig, now=time.time, run=subprocess.run):
        self.cfg, self.now, self._run = cfg, now, run
        self.nonces: dict[str, tuple[str, str, float]] = {}
        self.used: set[str] = set()

    # ---------------------------------------------------------------- audit
    def audit(self, user: str, action: str, result: str, **extra):
        p = Path(self.cfg.audit_path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "user": user,
               "action": action, "result": result, **extra}
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    # ---------------------------------------------------------------- nonces
    def issue(self, user: str, action: str) -> str:
        n = secrets.token_urlsafe(24)
        self.nonces[n] = (user, action, self.now() + self.cfg.nonce_ttl)
        for k in [k for k, v in self.nonces.items() if v[2] < self.now()]:
            self.nonces.pop(k, None)
        return n

    def consume(self, nonce: str, user: str, action: str) -> str | None:
        """None when the nonce is good (and now spent); otherwise the reason it was refused."""
        if nonce in self.used:
            return "replayed request (nonce already used)"
        rec = self.nonces.pop(nonce, None)
        if rec is None:
            return "unknown or expired nonce"
        self.used.add(nonce)
        if rec[0] != user or rec[1] != action:
            return "nonce was issued for another user or action"
        if rec[2] < self.now():
            return "nonce expired"
        return None

    # --------------------------------------------------------------- routing
    def handle(self, method: str, path: str, headers: dict[str, str], body: str = ""):
        h = {k.lower(): v for k, v in headers.items()}
        user = h.get("tailscale-user-login", "")
        if user not in self.cfg.allowed_users:
            self.audit(user or "-", "request", "refused: not an allowed identity", path=path)
            return 403, "text/plain", "Forbidden"
        origin = h.get("origin")
        if method == "POST" and origin and re.sub(r"^https?://", "", origin) != h.get("host", ""):
            self.audit(user, "request", "refused: cross-origin POST", origin=origin)
            return 403, "text/plain", "Forbidden"
        if method == "GET" and path.split("?")[0] == "/":
            return 200, "text/html; charset=utf-8", self.page(user)
        if method == "GET" and path.split("?")[0] == "/feedback":
            return 200, "text/html; charset=utf-8", self.feedback_page(user)
        if method == "POST" and path == "/act":
            return self.act(user, parse_qs(body))
        return 404, "text/plain", "Not found"

    # --------------------------------------------------------------- actions
    def act(self, user: str, form: dict[str, list[str]]):
        action = (form.get("action") or [""])[0]
        nonce = (form.get("nonce") or [""])[0]
        if action == "feedback":
            return self.act_feedback(user, form, nonce)
        if action not in ACTIONS:
            self.audit(user, action or "-", "refused: not an allowed action")
            return 400, "text/plain", "Unknown action"
        bad = self.consume(nonce, user, action)
        if bad:
            self.audit(user, action, f"refused: {bad}")
            return (409 if "replayed" in bad else 403), "text/plain", f"Refused: {bad}"
        try:
            ack, state = getattr(self, f"do_{action}")()
            result = "done"
        except Exception as exc:  # report, never crash the page
            ack, state, result = f"failed: {type(exc).__name__}: {str(exc)[:200]}", "unchanged or unknown", "failed"
        self.audit(user, action, result, ack=ack, state=state)
        return 200, "text/html; charset=utf-8", self.result_page(action, ack, state)

    def act_feedback(self, user: str, form: dict[str, list[str]], nonce: str):
        """Record one feedback answer. A record only: it cannot place or change anything."""
        from tradingagents.brent import feedback

        mid, kind, value = ((form.get(k) or [""])[0] for k in ("id", "kind", "value"))
        bad = self.consume(nonce, user, f"feedback:{mid}")
        if bad:
            self.audit(user, "feedback", f"refused: {bad}", id=mid)
            return (409 if "replayed" in bad else 403), "text/plain", f"Refused: {bad}"
        try:
            feedback.record(Path(self.cfg.brent_journal).expanduser(), user, mid, kind, value,
                            datetime.now(UTC))
            ack, result = f"Recorded: {feedback.VALUES.get(value) or feedback.ENTRY_VALUES[value]} for {mid}.", "done"
        except (ValueError, OSError) as exc:
            ack, result = f"not recorded: {exc}", "failed"
        self.audit(user, "feedback", result, id=mid, value=value)
        return 200, "text/html; charset=utf-8", _shell(
            f"<h1>Feedback</h1><p>{html.escape(ack)}</p><p class=note>A later answer replaces this one; "
            "the history is kept. Feedback never places or changes an order.</p>"
            "<p><a href=/feedback>Back to messages</a></p>")

    def feedback_page(self, user: str) -> str:
        from datetime import timedelta

        from tradingagents.brent import feedback
        from tradingagents.brent.store import Store

        folder = Path(self.cfg.brent_journal).expanduser()
        msgs = feedback.sent_messages(Store(folder).events(), datetime.now(UTC) - timedelta(days=3))
        last = feedback.latest(folder)
        rows = []
        for m in reversed(msgs[-40:]):
            choices = {**feedback.VALUES, **(feedback.ENTRY_VALUES if m["kind"] == "ENTRY" else {})}
            n = self.issue(user, f"feedback:{m['id']}")
            buttons = "".join(
                f"<form method=post action=/act style='display:inline'><input type=hidden name=action value=feedback>"
                f"<input type=hidden name=id value={html.escape(m['id'])}><input type=hidden name=kind "
                f"value='{html.escape(m['kind'])}'><input type=hidden name=value value={v}>"
                f"<input type=hidden name=nonce value={n}><button type=submit>{html.escape(label)}</button></form>"
                for v, label in choices.items())
            got = [r["value"] for k, r in last.items() if k.startswith(m["id"] + "|")]
            rows.append(f"<div class=msg><b>{html.escape(m['id'])}</b> {html.escape(m['kind'])} "
                        f"{html.escape(m['ts'][:16])}<br>{html.escape(m['summary'])}<br>"
                        f"<span class=note>your answer: {html.escape(', '.join(got) or 'none yet')}</span><br>"
                        f"{buttons}</div>")
        return _shell("<h1>Rate recent messages</h1><p class=note>Last 3 days of messages that were accepted "
                      "for delivery. One tap records an answer; no answer is not counted as negative. "
                      "Nothing here places, changes or cancels an order.</p>"
                      + ("".join(rows) or "<p>No messages in the last 3 days.</p>")
                      + "<p><a href=/>Back to status</a></p>")

    def do_brent_pause(self):
        v = set_alerts_enabled(self.cfg.brent_toml, False)
        return ("Setting written. The monitor re-reads it within about 60 s.",
                f"alerts_enabled in the config file is now {str(v).lower()} (check the status panel for the "
                "monitor's own confirmation)")

    def do_brent_resume(self):
        v = set_alerts_enabled(self.cfg.brent_toml, True)
        return ("Setting written. The monitor re-reads it within about 60 s.",
                f"alerts_enabled in the config file is now {str(v).lower()} (check the status panel for the "
                "monitor's own confirmation)")

    def do_brent_restart_news(self):
        cmd = self.cfg.restart_news_command
        if not cmd:
            raise RuntimeError("no restart_news_command is configured")
        r = self._run(cmd, shell=True, capture_output=True, text=True, timeout=90)
        time.sleep(0 if self.now is not time.time else 5)
        running = self.news_running()
        return (f"Restart command exited with code {r.returncode}.",
                f"news collector running now: {'yes' if running else 'NO'}")

    # ------------------------------------------------------------ read-only
    def news_running(self) -> bool:
        from tradingagents.brent.store import Store

        return Store(Path(self.cfg.brent_journal).expanduser() / "newscheck").is_locked()

    def status_text(self) -> str:
        lines = []
        try:
            from tradingagents.brent.config import load_config as load_brent
            from tradingagents.brent.report import news_health, status_lines
            from tradingagents.brent.store import Store

            bcfg = load_brent(Path(self.cfg.brent_toml).expanduser())
            bcfg.journal_dir = str(Path(self.cfg.brent_journal).expanduser())
            store = Store(bcfg.journal_dir)
            lines.append("BRENT (alerts only)")
            lines += status_lines(store.state(), store.is_locked(),
                                  news=news_health(bcfg.journal_dir, cfg=bcfg))
            lines.append(f"config file: alerts_enabled = {str(read_alerts_enabled(self.cfg.brent_toml)).lower()}")
        except Exception as exc:
            lines.append(f"Brent status unavailable: {type(exc).__name__}: {str(exc)[:150]}")
        lines.append("")
        lines.append("STOCK DEMO (paper)")
        try:
            lines += self.demo_lines()
        except Exception as exc:
            lines.append(f"stock status unavailable: {type(exc).__name__}: {str(exc)[:150]}")
        return "\n".join(lines)

    def demo_lines(self) -> list[str]:
        if self.cfg.demo_journal:
            return self._journal_lines(Path(self.cfg.demo_journal).expanduser())
        if self.cfg.demo_snapshot:
            return snapshot_lines(Path(self.cfg.demo_snapshot).expanduser(), self.cfg.demo_snapshot_stale_hours)
        return ["not hosted on this server; its dashboard is on the main page"]

    @staticmethod
    def _journal_lines(d: Path) -> list[str]:
        if not (d / "state.json").exists():
            return [f"stock journal not found at {d}: the stock test is not running from this server"]
        state = json.loads((d / "state.json").read_text(encoding="utf-8"))
        out = [f"last run date in state: {state.get('last_run_date', 'none')}"]
        acct = d / "paper_account.json"
        if acct.exists():
            a = json.loads(acct.read_text(encoding="utf-8"))
            out.append(f"paper account: cash {a['cash']:,.0f}; pending/open/exit-queued "
                       f"{len(a['open'])}; closed {len(a['closed'])} (queued orders are not fills)")
        return out

    # --------------------------------------------------------------- html
    def page(self, user: str) -> str:
        buttons = "".join(
            f"<form method=post action=/act><input type=hidden name=action value={a}>"
            f"<input type=hidden name=nonce value={self.issue(user, a)}>"
            f"<button type=submit>{html.escape(label)}</button></form>" for a, label in ACTIONS.items())
        dis = "".join(f"<p class=note>Unavailable: {html.escape(t)}</p>" for t in DISABLED.values())
        body = (f"<h1>Trading control</h1><p class=note>Signed in as {html.escape(user)}. Viewing or "
                "refreshing this page does nothing. Actions are logged. A button press is not proof "
                f"it worked: read the state after.</p><h2>Actions</h2>{buttons}{dis}"
                f"<p><a href=/feedback>Rate recent Brent messages</a></p>"
                f"<h2>Status</h2><pre>{html.escape(self.status_text())}</pre>"
                "<p class=note>Phone display of Telegram messages is not measured.</p>")
        return _shell(body)

    def result_page(self, action: str, ack: str, state: str) -> str:
        return _shell(f"<h1>{html.escape(ACTIONS[action])}</h1><p>{html.escape(ack)}</p>"
                      f"<p><b>State read back:</b> {html.escape(state)}</p>"
                      "<p><a href=/>Back to status</a></p>")


def snapshot_lines(path: Path, stale_hours: float = 26.0, now: datetime | None = None) -> list[str]:
    """Stock status from the PC's synced read-only snapshot (data.json). Says how old it is and
    marks it stale; a missing or unreadable file is reported as such, never as 'no runs'."""
    if not path.exists():
        return [f"stock snapshot not found at {path}: the PC has not synced one yet"]
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"stock snapshot unreadable: {type(exc).__name__}"]
    now = now or datetime.now(UTC)
    built = d.get("built_at")
    try:
        age_h = (now - datetime.fromisoformat(built)).total_seconds() / 3600
    except (TypeError, ValueError):
        age_h = None
    head = f"snapshot from the PC, built {built or 'unknown'}"
    if age_h is None or age_h > stale_hours:
        head += f"  STALE: older than {stale_hours:g} h or undated; figures below may be out of date"
    elif age_h is not None:
        head += f" ({age_h:.1f} h ago)"
    out = [head]
    runs = [r for r in d.get("runs") or [] if not r.get("dry_run")]
    if runs:
        last = runs[-1]
        end = last.get("end")
        fin = (f"finished {end.get('status')}: {end.get('completed')}/{end.get('attempted')} analyses "
               f"completed, {end.get('errored')} errored" if end else "no finish recorded")
        out.append(f"last run {last.get('trade_date')}: {fin}")
    else:
        out.append("no real run in the snapshot")
    acct = d.get("account")
    if acct:
        pos = acct.get("positions") or []
        exit_pending = sum(1 for p in pos if p.get("exit_requested_on"))
        pending = sum(1 for p in pos if p.get("status") == "pending")
        opened = sum(1 for p in pos if p.get("status") == "open" and not p.get("exit_requested_on"))
        out.append(f"paper positions: pending {pending}, open {opened}, exit pending {exit_pending}, "
                   f"closed {len(acct.get('closed') or [])} (pending orders are not fills)")
    return out


def _shell(body: str) -> str:
    return ("<!doctype html><html><head><meta charset=utf-8><meta name=viewport "
            "content='width=device-width,initial-scale=1'><title>Trading control</title><style>"
            ":root{--bg:#fff;--fg:#1a1a1a;--mute:#666}@media (prefers-color-scheme:dark){"
            ":root{--bg:#141414;--fg:#e8e8e8;--mute:#999}}body{background:var(--bg);color:var(--fg);"
            "font:16px/1.45 system-ui,sans-serif;margin:0 auto;max-width:760px;padding:16px}"
            "button{font:inherit;padding:12px 16px;margin:6px 0;width:100%}pre{white-space:pre-wrap;"
            "font-size:13px}.note{color:var(--mute);font-size:13px}</style></head><body>"
            f"{body}</body></html>")
