"""The Brent monitor: one tick every few seconds, deterministic, no LLM, no orders.

A tick reads one quote, refreshes 15-minute context when a candle has closed, keeps each
issued signal's validity up to date (with SKIP/EXPIRED updates), advances paper fills and
exits, and sends a new alert only when every check passes on a fresh quote taken just
before sending.
"""

from __future__ import annotations

import collections
import math
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import chart, info, messages, news, rules
from .client import OandaReadError, Quote
from .config import PAPER_RULES_VERSION, RULES_VERSION, BrentConfig, code_commit, manifest
from .rules import LONG, Setup
from .store import Store

M15 = timedelta(minutes=15)
OPS_REPEAT = timedelta(minutes=30)
START_QUIET = timedelta(hours=24)   # "monitor started" card at most once a day
RANGE_REPEAT = timedelta(minutes=30)
ACCOUNT_EVERY = timedelta(minutes=5)
MARKS = (60, 240)  # directional check-points, minutes after the alert
CHART_STALE = timedelta(minutes=18)   # latest processed completed 15-min candle older -> no entries
CTX_RETRY = timedelta(seconds=20)     # wait between candle refetches while a new bar is late
NEWS_PROC_EVERY = timedelta(seconds=60)
# Retries of a failed non-entry message (WATCH, its CANCEL, ENTRY updates): waits before attempts
# 2, 3 and 4. A notice is dropped once its useful life (RETRY_LIFE) has passed. A failed ENTRY
# itself is never resent: its quote and validity window are gone (it is recorded as missed).
RETRY_WAITS = (timedelta(seconds=60), timedelta(minutes=5), timedelta(minutes=15))
RETRY_LIFE = timedelta(minutes=30)


def p90(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, math.ceil(0.9 * len(s)) - 1)]


def _setup(d: dict) -> Setup:
    """Setup from a state dict that may carry monitor bookkeeping keys."""
    return Setup(**{k: v for k, v in d.items() if k in Setup.__dataclass_fields__})


def _tracked(sig: dict) -> bool:
    """Paper/directional tracking applies to accepted alerts and to simulated (not sent)
    signals; never to an alert whose notification failed."""
    return bool(sig.get("delivered") or sig.get("simulated"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def grade_of(a: dict, direction: str) -> tuple[str, str]:
    """A = chart and news agree; B = chart only, no conflicting news; C = news conflicts."""
    pol = a["policy"]
    if pol["row"] in ("conflict", "mixed"):
        return "C", f"news conflicts ({pol['row']})"
    if pol["action"] == direction and "news-supported" in (pol.get("label") or ""):
        return "A", "chart and news agree"
    return "B", f"chart only, no conflicting news ({pol['row']})"

class Monitor:
    def __init__(self, cfg: BrentConfig, client, store: Store,
                 send: Callable[[str], tuple[bool, float]],
                 now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 news_view: Callable[[datetime], dict] | None = None,
                 report_pages: bool = True, cfg_path: str | None = None):
        self.cfg, self.client, self.store = cfg, client, store
        self._send, self.now = send, now
        # The separate news collector is watched only in production (default news view).
        self.watch_news_process = news_view is None
        self.news_view = news_view or (lambda t: news.news_view(cfg.journal_dir, t))
        self.report_pages = report_pages
        self.st = store.state() or {}
        self._init_state()
        self.info = info.InfoLane(cfg, store, self.st,
                                  lambda text, ref, t: self.send_notice(text, "info", ref, t))
        self.last_quote: Quote | None = None
        self.daily: list[dict] | None = None
        self.h1: list[dict] | None = None
        self.mids: collections.deque = collections.deque(maxlen=3000)
        self.manifest = manifest(cfg)
        self._news_proc_checked: datetime | None = None
        self.cfg_path, self._switch_checked = cfg_path, None

    # ------------------------------------------------------------------ state
    def _init_state(self):
        st = self.st
        st.setdefault("seq", 0)
        st.setdefault("setups", {})
        st.setdefault("signals", {})
        st.setdefault("seen_keys", [])
        st.setdefault("ops", {})
        st.setdefault("metrics", {"send_s": [], "quote_age_s": [], "tick_s": []})
        st.setdefault("suppressed_logged", {})
        st.setdefault("account", {})
        st.setdefault("watches", {})
        st.setdefault("outbox", [])   # failed notices awaiting retry; in state, so restarts keep it
        st["rules"], st["config_hash"] = RULES_VERSION, self.cfg.config_hash()
        c = self.cfg
        st["effective"] = {
            "experiment": manifest(c, "x")["experiment"], "range": [c.range_low, c.range_high],
            "range_convention": "mid-price gate; entry zone must also lie inside the range",
            "validity_seconds": c.validity_seconds, "max_spread": c.max_spread,
            "alerts_enabled": c.alerts_enabled, "watch_alerts": c.watch_alerts,
            "llm_per_hour": c.llm_per_hour, "llm_per_day": c.llm_per_day,
            "llm_quiet_et": list(c.llm_quiet_et), "code_commit": code_commit(),
            "phone_display": "not measured (only notify-command acceptance is)"}

    def save(self):
        self.st["seen_keys"] = self.st["seen_keys"][-200:]
        for k in ("send_s", "quote_age_s", "tick_s"):
            self.st["metrics"][k] = self.st["metrics"][k][-200:]
        self.store.save_state(self.st)

    def _metric(self, name: str, value: float):
        self.st["metrics"][name].append(round(value, 3))

    # --------------------------------------------------------------- messaging
    def send(self, text: str, kind: str, sig_id: str = "", attempt: int = 1) -> tuple[bool, float, datetime]:
        """One delivery attempt. ``ok`` means the notify command / Telegram API accepted it;
        whether a phone displayed it is unknown and never recorded as known."""
        try:
            ok, secs = self._send(text)
        except Exception as exc:  # delivery failure must not stop monitoring
            ok, secs = False, 0.0
            self.store.write("send_error", kind=kind, id=sig_id, error=str(exc)[:300])
        done = self.now()
        if ok:
            self._metric("send_s", secs)
        self.store.write("sent", kind=kind, id=sig_id, ok=ok, attempt=attempt, seconds=round(secs, 3),
                         done_at=_iso(done), phone_receipt="unknown", text=text)
        return ok, secs, done

    def send_notice(self, text: str, kind: str, ref_id: str, now: datetime) -> bool:
        """Send a non-entry notice (CANCEL, ENTRY update or expiry); queue a bounded retry on
        failure. The same text and id are resent, never a new message."""
        ok, _, _ = self.send(text, kind, ref_id)
        if not ok:
            self.st["outbox"].append({"kind": kind, "id": ref_id, "text": text, "attempts": 1,
                                      "created": _iso(now), "next_try": _iso(now + RETRY_WAITS[0])})
            self.store.write("retry_queued", kind=kind, id=ref_id, attempt=1)
        return ok

    def flush_outbox(self, now: datetime):
        keep = []
        for m in self.st["outbox"]:
            if now - _dt(m["created"]) > RETRY_LIFE:
                self.store.write("retry_dropped", kind=m["kind"], id=m["id"], attempts=m["attempts"],
                                 reason="useful life over")
                continue
            if now < _dt(m["next_try"]):
                keep.append(m)
                continue
            m["attempts"] += 1
            ok, _, _ = self.send(m["text"], m["kind"], m["id"], attempt=m["attempts"])
            if ok:
                continue
            if m["attempts"] > len(RETRY_WAITS):
                self.store.write("retry_dropped", kind=m["kind"], id=m["id"], attempts=m["attempts"],
                                 reason="attempts used up")
                continue
            m["next_try"] = _iso(now + RETRY_WAITS[m["attempts"] - 1])
            keep.append(m)
        self.st["outbox"] = keep

    def ops_notice(self, kind: str, text: str):
        last = self.st["ops"].get(kind)
        now = self.now()
        if last and now - _dt(last) < OPS_REPEAT:
            return
        self.st["ops"][kind] = _iso(now)
        # Logged only (dashboard, nightly, weekly). The health check retries/restarts and sends
        # the one rare Telegram notice when a problem outlasts that.
        self.store.write("ops", kind=kind, text=text)

    # ----------------------------------------------------------------- startup
    def startup(self, samples: int = 5, sleep: Callable[[float], None] = time.sleep):
        """Recover after a restart, measure quote freshness and Telegram delivery."""
        now = self.now()
        self.store.write("start", rules=RULES_VERSION, config_hash=self.cfg.config_hash(),
                         manifest=self.manifest,
                         config={k: getattr(self.cfg, k) for k in self.cfg.__dataclass_fields__
                                 if k != "notify_command"})
        self.recover(now)
        account_line = self.refresh_account(force=True)
        for _ in range(samples):
            try:
                q = self.client.quote(self.cfg.instrument)
                self._metric("quote_age_s", q.age(self.now()))
            except OandaReadError:
                pass
            sleep(1)
        self.save()
        # A restart (deploy, crash, auto-heal) within START_QUIET of the last card is logged only.
        last = self.st["ops"].get("start_card_at")
        if last and now - _dt(last) < START_QUIET:
            self.store.write("start_quiet", last_card=last)
        else:
            ok, _, _ = self.send(messages.start_text(RULES_VERSION, self.cfg.config_hash(), account_line,
                                                     self.manifest["experiment"], self.cfg.alerts_enabled),
                                 "start")
            if ok:
                self.st["ops"]["start_card_at"] = _iso(now)
        self.save()
        self.save()

    def recover(self, now: datetime):
        """Signals whose window passed while the monitor was down expire unfilled; open paper
        positions catch up on 1-minute bid/ask candles."""
        for sig in self.st["signals"].values():
            if sig["status"] == "valid" and now >= _dt(sig["expires_at"]):
                sig["status"] = "expired"
                self.store.write("update", id=sig["id"], status="expired", note="monitor was down")
            pp = sig["paper"]
            if pp["state"] == "waiting" and now >= _dt(sig["expires_at"]):
                pp["state"] = "missed"
                pp["reason"] = "monitor down during the window"
                self.store.write("paper_missed", id=sig["id"], reason=pp["reason"])
            if pp["state"] == "open":
                self.catch_up(sig, now)
        for s in self.st["setups"].values():
            if s["status"] == "armed" and now >= _dt(s["expires_at"]):
                s["status"] = "expired"

    # ---------------------------------------------------------------- account
    def refresh_account(self, force: bool = False) -> str:
        acc = self.st["account"]
        now = self.now()
        if not force and acc.get("checked_at") and now - _dt(acc["checked_at"]) < ACCOUNT_EVERY:
            return ""
        try:
            s = self.client.summary()
            trades = self.client.open_trades(self.cfg.instrument)
            acc.update(currency=s.get("currency"), nav=float(s.get("NAV", "nan")),
                       hedging=s.get("hedgingEnabled"), trades=trades, error="",
                       checked_at=_iso(now))
        except Exception as exc:
            acc.update(error=str(exc)[:200], checked_at=_iso(now))
            return f"Account not read: {acc['error']}."
        return f"Account {acc['currency']}, {self.position_note()}"

    def position_note(self, direction: str | None = None) -> str:
        acc = self.st["account"]
        if acc.get("error") or "trades" not in acc:
            return f"account positions not checked ({acc.get('error') or 'not read yet'})"
        trades = acc["trades"]
        if not trades:
            return f"no open {self.cfg.instrument} trades on the account"
        parts = [f"{'LONG' if t['units'] > 0 else 'SHORT'} {abs(t['units']):,.0f} (trade {t['id']} "
                 f"at {t['price']:.2f})" for t in trades]
        note = f"open {self.cfg.instrument}: " + ", ".join(parts)
        net = sum(t["units"] for t in trades)
        hedging = acc.get("hedging")
        if direction and net:
            opposite = (direction == LONG) == (net < 0)
            if opposite and hedging is False:
                note += (". Account hedging is OFF: this order would reduce or close that "
                         "position instead of opening a separate trade")
            elif opposite and hedging:
                note += ". Account hedging is ON: this order would open a separate opposite trade"
            elif opposite:
                note += ". Hedging setting unknown: check how an opposite order would net"
            else:
                note += ". Same direction: this would add to your exposure"
        return note

    # ---------------------------------------------------------------- context
    def refresh_context(self, now: datetime):
        """On each new completed 15-min candle: levels, invalidations, new setups.

        Only a fully processed candle advances ``last_bar_time``; a failed refresh is
        recorded in ``context_error`` and blocks new entries until a refresh succeeds."""
        last = self.st.get("last_bar_time")
        if last and now < _dt(last) + 2 * M15 + timedelta(seconds=5) and self.daily is not None \
                and self.h1 is not None and not self.st.get("context_error"):
            return
        retry_at = self.st.get("ctx_retry_at")
        if retry_at and now < _dt(retry_at) and self.daily is not None and self.h1 is not None:
            return
        self.st["ctx_retry_at"] = _iso(now + CTX_RETRY)
        try:
            daily = self.client.candles(self.cfg.instrument, "D", 60)
            h1 = self.client.candles(self.cfg.instrument, "H1", 120)
            bars = [b for b in self.client.candles(self.cfg.instrument, "M15", 8) if b["complete"]]
            a = rules.atr(daily, self.cfg.atr_period)
            pd = rules.previous_day(daily)
        except Exception as exc:
            self.st["context_error"] = {"at": _iso(now), "error": f"{type(exc).__name__}: {exc}"[:200]}
            raise
        self.daily, self.h1 = daily, h1
        day = rules.ny(pd["time"]).date().isoformat()
        self.st["ctx"] = {"day": day, "pdh": pd["h"], "pdl": pd["l"], "atr": round(a, 4),
                          "refreshed_at": _iso(now)}
        new = [b for b in bars if not last or b["time"] > _dt(last)]
        if not last:
            new = new[-1:]
        for b in new:
            close_t = b["time"] + M15
            self.on_bar(b, close_t, pd["h"], pd["l"], a, day)
            self.st["last_bar_time"] = _iso(b["time"])
        self.st["context_error"] = None
        if new:
            self.st["ctx_retry_at"] = None

    def chart_problem(self, now: datetime) -> str | None:
        """Why chart data is not good enough for a NEW entry suggestion (None = fine)."""
        err = self.st.get("context_error")
        if err:
            return f"chart data refresh failed at {err['at'][11:19]} UTC: {err['error']}"
        last = self.st.get("last_bar_time")
        if not last or self.daily is None or self.h1 is None:
            return "chart data not loaded yet"
        lag = now - (_dt(last) + M15)
        if lag > CHART_STALE:
            return (f"latest processed 15-min candle closed {lag.total_seconds() / 60:.0f} min ago "
                    "(chart data stale)")
        return None

    def on_bar(self, bar: dict, close_t: datetime, pdh: float, pdl: float, a: float, day: str):
        for s in self.st["setups"].values():
            if s["status"] not in ("armed", "alerted"):
                continue
            reason = rules.close_invalidates(_setup(s), bar)
            if reason:
                prev = s["status"]
                s["status"] = "invalidated"
                s["end_reason"] = reason
                self.store.write("setup_end", key=s["key"], status="invalidated", reason=reason)
                if prev == "alerted":
                    for sig in self.st["signals"].values():
                        if sig["setup"]["key"] == s["key"] and sig["status"] == "valid":
                            self.invalidate(sig, f"setup broken: {reason}")
        for s in rules.detect(bar, close_t, pdh, pdl, a, day, self.cfg):
            if s.key in self.st["seen_keys"]:
                continue
            # A failed break at a level ends any armed breakout the other way at that level.
            for other in self.st["setups"].values():
                if other["level_name"] == s.level_name and other["status"] == "armed":
                    other["status"] = "invalidated"
                    other["end_reason"] = f"opposite setup {s.kind} {s.direction} armed"
                    self.store.write("setup_end", key=other["key"], status="invalidated",
                                     reason=f"opposite setup {s.kind} {s.direction} armed")
            self.st["seen_keys"].append(s.key)
            self.st["setups"][s.key] = s.to_dict()
            self.store.write("watch", key=s.key, setup=s.to_dict())

    # ------------------------------------------------------------------- tick
    def tick(self):
        t0 = time.monotonic()
        self.st["last_tick"] = _iso(self.now())
        # Slow network work first (candles, account), then the quote, then a fresh clock, so
        # every age and validity check below uses times taken after the slow calls.
        try:
            self.refresh_context(self.now())
        except Exception as exc:
            self.store.write("context_error", error=str(exc)[:200])
            if not rules.market_closed_by_clock(self.now()):
                self.ops_notice("context", f"could not refresh 15-min/daily candles: {exc}. "
                                "New entry suggestions are blocked until chart data is fresh.")
        self.refresh_account()
        self.reload_alert_switch(self.now())
        self.check_news_process(self.now())
        try:
            q = self.client.quote(self.cfg.instrument)
            now = self.now()
            self.last_quote = q
            self.st["ops"]["quote_error_count"] = 0
            self.st["last_quote"] = {"bid": q.bid, "ask": q.ask, "time": _iso(q.time),
                                     "tradeable": q.tradeable, "fetched_at": _iso(now)}
            self._metric("quote_age_s", q.age(now))
            self.mids.append((q.time, q.mid))
        except Exception as exc:
            q, now = None, self.now()
            n = self.st["ops"].get("quote_error_count", 0) + 1
            self.st["ops"]["quote_error_count"] = n
            if n in (1, 10, 100, 1000):
                self.store.write("quote_error", count=n, error=str(exc)[:200])
            if n >= 20 and not rules.market_closed_by_clock(now):
                self.ops_notice("quote_errors", f"{n} quote requests failed in a row: {exc}. "
                                "Actionable alerts are suppressed until quotes return.")
        if q is not None:
            self.check_stale(q, now)
            self.check_range(q, now)
        self.update_signals(q, now)
        self.expire_setups(now)
        if q is not None:
            self.try_alerts(q, self.now())
            self.notify_watches(q, self.now())
        self.end_watches(self.now())
        self.flush_outbox(self.now())
        try:   # the information lane must never be able to stop monitoring or touch trades
            self.info.step(q, self.st.get("ctx"), self.daily, self.cfg.journal_dir, self.now())
        except Exception as exc:
            self.ops_notice("info_error", f"information lane error: {type(exc).__name__}: {exc}"[:300])
        self._metric("tick_s", time.monotonic() - t0)
        self.save()

    def reload_alert_switch(self, now: datetime):
        """Only ``alerts_enabled`` is re-read from the config file while running (every 60 s),
        so shadow mode can end without a restart (a restart needs the token typed again).
        Every other setting needs a restart. The change is logged and announced once."""
        if not self.cfg_path or (self._switch_checked and now - self._switch_checked < NEWS_PROC_EVERY):
            return
        self._switch_checked = now
        try:
            import tomllib

            with open(self.cfg_path, "rb") as fh:
                want = bool(tomllib.load(fh).get("alerts_enabled", False))
        except (OSError, ValueError) as exc:
            self.store.write("switch_error", error=str(exc)[:200])
            return
        if want == self.cfg.alerts_enabled:
            return
        self.cfg.alerts_enabled = want
        self.manifest = manifest(self.cfg, self.manifest.get("code_commit"))
        self.store.write("alerts_switch", enabled=want, manifest=self.manifest)
        self.send(messages.switch_text(want, self.manifest), "status")

    def check_news_process(self, now: datetime):
        """The news collector is a separate process: notice when it stops or goes stale."""
        if not self.watch_news_process or \
                (self._news_proc_checked and now - self._news_proc_checked < NEWS_PROC_EVERY):
            return
        self._news_proc_checked = now
        try:
            locked = Store(Path(self.cfg.journal_dir) / "newscheck").is_locked()
            nv = self.news_view(now)
        except Exception as exc:
            locked, nv = False, {"status": "collector_missing", "why": str(exc)}
        problem = ""
        if not locked:
            problem = "the Brent news collector is not running"
        elif nv.get("status") in ("collector_stale", "collector_missing"):
            problem = nv.get("why", "news collector stale")
        elif nv.get("status") == "missing_coverage":
            problem = nv.get("why", "news coverage missing")
        self.st["news_process"] = {"checked_at": _iso(now), "running": locked,
                                   "status": nv.get("status"), "problem": problem}
        if problem:
            self.ops_notice("news_process", f"{problem}. Entry alerts that need news are "
                            "suppressed (WATCH) until it recovers.")

    def check_stale(self, q: Quote, now: datetime):
        age = q.age(now)
        if not rules.market_closed_by_clock(now) and q.tradeable and age > 120:
            self.ops_notice("stale", f"latest {self.cfg.instrument} quote is {age:.0f}s old while "
                            "the market should be open. Alerts are suppressed.")

    def check_range(self, q: Quote, now: datetime):
        if not q.tradeable or q.age(now) > self.cfg.max_quote_age_seconds:
            return
        inside = self.cfg.range_low <= q.mid <= self.cfg.range_high
        prev = self.st.get("range_inside")
        if prev is inside:
            return
        self.st["range_inside"] = inside
        self.store.write("range", inside=inside, mid=q.mid)
        last = self.st["ops"].get("range")
        if prev is None and inside:
            return  # starting inside the range is the normal case: stay quiet
        if last and now - _dt(last) < RANGE_REPEAT:
            return  # flapping at the boundary: logged, not re-sent
        self.st["ops"]["range"] = _iso(now)
        self.send(messages.range_text(q.mid, self.cfg.range_low, self.cfg.range_high, inside), "range")

    def expire_setups(self, now: datetime):
        for s in self.st["setups"].values():
            if s["status"] == "armed" and now >= _dt(s["expires_at"]):
                s["status"] = "expired"
                s["end_reason"] = "the arming window ended without an entry"
                self.store.write("setup_end", key=s["key"], status="expired")
        # Drop finished setups older than a day from state (the journal keeps them).
        cutoff = now - timedelta(days=1)
        self.st["setups"] = {k: s for k, s in self.st["setups"].items()
                             if s["status"] == "armed" or _dt(s["armed_at"]) > cutoff}

    # ----------------------------------------------------------- alert gating
    def global_blockers(self, now: datetime) -> list[str]:
        reasons = []
        last = self.st.get("last_alert_at")
        if last and now - _dt(last) < timedelta(minutes=self.cfg.cooldown_minutes):
            reasons.append(f"cooldown {self.cfg.cooldown_minutes} min after the last alert")
        if any(s["status"] == "valid" and s.get("arm") == "combined"
               for s in self.st["signals"].values()):
            reasons.append("another alert is still inside its window")
        m = self.st["metrics"]
        qa, sd = p90(m["quote_age_s"][-50:]), p90(m["send_s"][-20:])
        if qa is not None and qa > self.cfg.max_quote_age_seconds:
            reasons.append(f"measured quote age p90 {qa:.1f}s is too high")
        budget = self.cfg.validity_seconds - self.cfg.min_usable_seconds
        if sd is not None and sd > budget:
            reasons.append(f"measured Telegram send p90 {sd:.1f}s leaves too little time")
        if self.st["ops"].get("quote_error_count"):
            reasons.append("quote errors")
        return reasons

    def try_alerts(self, q: Quote, now: datetime):
        armed = sorted((s for s in self.st["setups"].values() if s["status"] == "armed"),
                       key=lambda s: s["armed_at"])
        for sd in armed:
            s = _setup(sd)
            if not rules.in_zone(s, q.bid, q.ask):
                continue  # normal waiting: quiet
            chart_blockers = rules.entry_blockers(s, q.bid, q.ask, q.time, q.tradeable, now, self.cfg)
            stale = self.chart_problem(now)
            if stale:
                chart_blockers.append(stale)
            if chart_blockers:
                self.log_suppressed(s, chart_blockers)
                continue
            # A qualifying chart candidate. The chart-only comparison arm records it once.
            if not sd.get("shadow_done"):
                sd["shadow_done"] = True
                self.issue(s, sd, arm="chart-only")
            a = self.assess(s, q, now)
            self.log_candidate(s, a)
            if a["policy"]["action"] != s.direction:
                self.research_call(sd, a, s.direction)
                continue  # WATCH / NO TRADE: the setup stays armed; news or chart may change
            blockers = self.global_blockers(now)
            if blockers:
                self.log_suppressed(s, blockers)
                self.research_call(sd, a, s.direction)
                continue
            self.issue(s, sd, arm="combined", assessment=a)
            if sd.get("status") != "alerted":
                self.research_call(sd, a, s.direction)
            return

    # ------------------------------------------------------------ WATCH notices
    def watch_state(self, s: Setup, q: Quote, now: datetime) -> tuple[str, str, dict]:
        """(category, blocker sentence, assessment). Category is the material state: it, not the
        poll, decides whether a new WATCH message is sent. 'ready' means ENTRY may follow."""
        a = self.assess(s, q, now)
        if not rules.in_zone(s, q.bid, q.ask):
            px = "ask" if s.direction == LONG else "bid"
            now_px = q.ask if s.direction == LONG else q.bid
            return "price", (f"price not in the entry zone yet ({px} {now_px:.2f}, zone "
                             f"{s.zone_lo:.2f}-{s.zone_hi:.2f})"), a
        if a["policy"]["action"] != s.direction:
            return f"news:{a['policy']['row']}", f"news/chart policy says {a['policy']['action']} " \
                f"({a['policy']['row']})", a
        other = rules.entry_blockers(s, q.bid, q.ask, q.time, q.tradeable, now, self.cfg)
        stale = self.chart_problem(now)
        if stale:
            other.append(stale)
        other += self.global_blockers(now)
        if other:
            return "checks", "entry checks not all passing: " + "; ".join(other[:3]), a
        return "ready", "none: all checks pass, an ENTRY message may follow", a

    def notify_watches(self, q: Quote, now: datetime):
        """One WATCH per setup per material state change. A WATCH creates no signal and no paper
        fill. Only for new suggestions inside the operating range and only with alerts on."""
        if not (self.cfg.alerts_enabled and self.cfg.watch_alerts) or not q.tradeable \
                or q.age(now) > self.cfg.max_quote_age_seconds:
            return
        if self.cfg.alerts_enabled and not (self.cfg.range_low <= q.mid <= self.cfg.range_high):
            return
        for sd in self.st["setups"].values():
            if sd["status"] != "armed":
                continue
            s = _setup(sd)
            if s.zone_lo < self.cfg.range_low or s.zone_hi > self.cfg.range_high:
                continue
            cat, blocker, a = self.watch_state(s, q, now)
            if cat == "ready":
                continue  # try_alerts owns the ENTRY decision
            w = self.st["watches"].get(s.key)
            tag = re.sub(r"[-+]?\d[\d.]*", "#", cat)
            if w and w.get("tag") == tag:
                continue
            if w and w.get("sent_at") and \
                    now - _dt(w["sent_at"]) < timedelta(seconds=self.cfg.watch_min_gap_seconds):
                continue
            fail = (w or {}).get("failed")
            # the same state failed before: bounded backoff, then give up
            if fail and fail["tag"] == tag and \
                    (fail["attempts"] > len(RETRY_WAITS) or now < _dt(fail["next_try"])):
                continue
            if not w:
                self.st["seq_watch"] = self.st.get("seq_watch", 0) + 1
                w = {"id": f"BW-{rules.ny(now):%Y%m%d}-{self.st['seq_watch']:03d}", "ended": False}
                self.st["watches"][s.key] = w
            ex = self.explain(s, a, w["id"])
            first = "tag" not in w
            text = messages.watch_text(w["id"], sd, q.mid, blocker, ex["news_line"], ex["chart_line"],
                                       first)
            attempt = fail["attempts"] + 1 if fail and fail["tag"] == tag else 1
            ok, secs, _ = self.send(text, "watch", w["id"], attempt=attempt)
            if ok:   # only an accepted send moves the WATCH to its new state
                w.update(tag=tag, sent_at=_iso(now), accepted=True)
                w.pop("failed", None)
            else:    # the content is rebuilt from the live state on the next attempt
                w["failed"] = {"tag": tag, "attempts": attempt,
                               "next_try": _iso(now + RETRY_WAITS[min(attempt, len(RETRY_WAITS)) - 1])}
            self.store.write("watch_notice", id=w["id"], key=s.key, state=cat, blocker=blocker,
                             accepted=ok, first=first, attempt=attempt)

    def end_watches(self, now: datetime):
        """UPDATE/CANCEL for a WATCH whose setup ended; a setup that became an ENTRY just links."""
        for key, w in self.st["watches"].items():
            if w.get("ended") or "tag" not in w:
                continue
            sd = self.st["setups"].get(key)
            status = sd["status"] if sd else "expired"
            if status == "armed":
                continue
            w["ended"] = True
            if status == "alerted":
                sig = next((x for x in self.st["signals"].values()
                            if x["setup"]["key"] == key and x.get("arm") == "combined"), None)
                w["entry_id"] = sig["id"] if sig else ""
                self.store.write("watch_end", id=w["id"], key=key, outcome="entry", entry_id=w["entry_id"])
                continue
            reason = (sd or {}).get("end_reason") or "the setup is no longer on the chart"
            self.store.write("watch_end", id=w["id"], key=key, outcome=status, reason=reason)
            if self.cfg.alerts_enabled and self.cfg.watch_alerts:
                self.send_notice(messages.watch_end_text(w["id"], now, reason), "watch", w["id"], now)

    def assess(self, s: Setup, q: Quote, now: datetime) -> dict:
        """Chart record (code) + news state (from the news log, no model call) + policy."""
        entry = rules.executable(s.direction, q.bid, q.ask)
        sd = s.to_dict()
        if not self.daily or self.h1 is None:
            ch = {"regime": "unknown", "with_trend": False, "room_r": None, "next_level": None,
                  "h1_structure": "no candles", "slope_atr_per_day": None, "atr": s.atr}
        else:
            ch = chart.assess(sd, self.daily, self.h1, entry, q.spread)
        try:
            nv = self.news_view(now)
        except Exception as exc:
            nv = {"state": "unavailable", "why": f"news view failed: {exc}", "items": []}
        pol = news.policy(s.direction, ch["regime"], s.kind, nv)
        if pol["action"] == s.direction and ch.get("room_r") is not None and ch["room_r"] < 1:
            pol = {"action": "NO TRADE", "row": "no room (under 1R to the next level)", "label": ""}
        if pol["action"] == s.direction:
            unsettled = self.unsettled(nv, now, s.atr)
            if unsettled:
                pol = {"action": "NO TRADE" if unsettled.startswith("price") else "WATCH",
                       "row": f"unsettled: {unsettled}", "label": ""}
        return {"assessed_at": _iso(now), "mid": q.mid, "chart": ch, "policy": pol,
                "news": {k: nv.get(k) for k in ("state", "status", "why", "basis", "health",
                                                "last_poll", "n_items",
                                                "last_first_seen", "support", "conflict_window")},
                "news_items": (nv.get("items") or [])[:6]}

    def unsettled(self, nv: dict, now: datetime, a: float) -> str:
        """Material news under 15 min old: abstain if price already moved > 0.5 ATR since it
        was first seen, or if that cannot be measured."""
        for it in nv.get("items") or []:
            seen = _dt(it["first_seen"])
            if now - seen > timedelta(minutes=15):
                continue
            before = [m for t, m in self.mids if t <= seen]
            if not before:
                return "fresh material news; price at first receipt not recorded"
            move = abs(self.mids[-1][1] - before[-1]) / a if self.mids else 0
            if move > 0.5:
                return f"price moved {move:.2f} ATR since fresh material news"
        return ""

    def log_candidate(self, s: Setup, a: dict):
        tag = f"{a['policy']['action']}|{a['policy']['row']}|{a['news'].get('state')}"
        if self.st.setdefault("candidates_logged", {}).get(s.key) == tag:
            return
        self.st["candidates_logged"][s.key] = tag
        self.store.write("candidate", key=s.key, kind=s.kind, direction=s.direction,
                         action=a["policy"]["action"], row=a["policy"]["row"],
                         label=a["policy"]["label"], assessment=a)

    def log_suppressed(self, s: Setup, reasons: list[str]):
        sig = "|".join(re.sub(r"[-+]?\d[\d.]*", "#", r) for r in reasons)
        if self.st["suppressed_logged"].get(s.key) == sig:
            return
        self.st["suppressed_logged"][s.key] = sig
        self.store.write("suppressed", key=s.key, direction=s.direction, reasons=reasons)

    def issue(self, s: Setup, sd: dict, arm: str = "combined", assessment: dict | None = None):
        """Final re-check on a fresh quote, then send (combined arm, alerts enabled) or record.
        The validity clock starts at that quote's timestamp. No model call happens here."""
        try:
            q = self.client.quote(self.cfg.instrument)
        except Exception as exc:
            self.log_suppressed(s, [f"final quote failed: {exc}"])
            return
        now = self.now()
        blockers = rules.entry_blockers(s, q.bid, q.ask, q.time, q.tradeable, now, self.cfg)
        telegram = arm == "combined" and self.cfg.alerts_enabled
        if arm == "combined":
            blockers += self.global_blockers(now)
            fresh = self.assess(s, q, now)
            if (fresh["policy"]["action"], fresh["policy"]["label"]) != \
                    (assessment["policy"]["action"], assessment["policy"]["label"]):
                blockers.append(f"assessment changed at final check: {fresh['policy']['row']}")
            assessment = fresh
        stale = self.chart_problem(now)
        if stale:
            blockers.append(stale)
        now = self.now()  # re-read after the news/chart work above
        if (now - q.time).total_seconds() > self.cfg.max_quote_age_seconds:
            blockers.append(f"final quote is {(now - q.time).total_seconds():.0f}s old after checks")
        expires = q.time + timedelta(seconds=self.cfg.validity_seconds)
        if telegram:
            expected_send = p90(self.st["metrics"]["send_s"][-20:]) or 5.0
            usable = (expires - now).total_seconds() - expected_send
            if usable < self.cfg.min_usable_seconds:
                blockers.append(f"only {usable:.0f}s usable after expected delivery")
        if blockers:
            if arm == "combined":
                self.log_suppressed(s, ["final check: " + b for b in blockers])
            return
        prefix, counter = ("BR", "seq") if arm == "combined" else ("SH", "seq_shadow")
        self.st[counter] = self.st.get(counter, 0) + 1
        sig_id = f"{prefix}-{rules.ny(now):%Y%m%d}-{self.st[counter]:03d}"
        entry = rules.executable(s.direction, q.bid, q.ask)
        plan = rules.plan(s, entry, q.spread, self.cfg)
        sig = {
            "id": sig_id, "arm": arm, "telegram": telegram, "instrument": self.cfg.instrument,
            "rules": RULES_VERSION, "config_hash": self.cfg.config_hash(), "setup": s.to_dict(),
            "quote": {"bid": q.bid, "ask": q.ask, "spread": round(q.spread, 4), "time": _iso(q.time),
                      "fetched_at": _iso(q.fetched_at), "rtt": round(q.rtt, 3)},
            "alert_mid": q.mid, "plan": plan, "decided_at": _iso(now), "expires_at": _iso(expires),
            "status": "valid", "why": self.why(s), "skip_reasons": self.skip_reasons(s, q, now),
            "position_note": self.position_note(s.direction), "sizing": self.sizing(s, entry, q),
            "paper": {"state": "waiting"}, "marks": {},
            "experiment": self.manifest, "paper_rules": PAPER_RULES_VERSION,
        }
        if assessment:
            sig.update(self.explain(s, assessment, sig_id))
            sig["grade"], sig["grade_reason"] = grade_of(assessment, s.direction)
        if arm == "combined" and sd.get("research_call_id"):
            sig["from_research"] = sd["research_call_id"]
        self.store.write("signal", **{k: v for k, v in sig.items() if k != "paper"})
        if arm == "combined":
            sd["status"] = "alerted"
            self.st["last_alert_at"] = _iso(now)
            if self.report_pages:
                self.write_signal_page(sig)
        self.st["signals"][sig_id] = sig
        if arm != "combined":
            sd["shadow_id"] = sig_id
        if not telegram:
            # Not sent (chart-only comparison, or alerts disabled): tracked on paper with the
            # same human-latency rule, the clock starting at the decision. Never "delivered".
            sig.update(delivered=False, simulated=True, send_seconds=None, sent_at=_iso(now),
                       notice="not sent: chart-only comparison arm" if arm != "combined"
                       else "not sent: alerts disabled")
            return
        w = self.st["watches"].get(s.key)
        if w and w.get("tag"):
            sig["from_watch"] = w["id"]
        text = messages.alert_text(sig, {"max_spread": self.cfg.max_spread,
                                         "max_hold_hours": self.cfg.max_hold_hours})
        ok, secs, done = self.send(text, "alert", sig_id)
        # "delivered" = the notify command / Telegram API accepted it; phone display is unknown.
        sig.update(delivered=ok, send_seconds=round(secs, 3), sent_at=_iso(done),
                   notice="accepted by the notify command (phone display not measured)" if ok
                   else "notify command failed")
        if not ok:
            sig["status"] = "undelivered"
            sig["paper"] = {"state": "missed", "reason": "alert not delivered"}
            self.store.write("paper_missed", id=sig_id, reason="alert not delivered")
            return
        left = (expires - done).total_seconds()
        if left < self.cfg.human_latency_seconds:
            self.invalidate(sig, f"Telegram delivery took {secs:.0f}s, only {left:.0f}s left")

    def research_call(self, sd: dict, a: dict, direction: str):
        """Send the setup's chart-only signal once as a graded RESEARCH CALL (never twice; never when
        it already became a real alert). Information only; paper tracking is unchanged."""
        if sd.get("research_call_id") or sd.get("status") == "alerted" or not self.cfg.research_calls \
                or not self.cfg.alerts_enabled:
            return
        sig = self.st["signals"].get(sd.get("shadow_id") or "")
        if not sig or sig.get("status") != "valid":
            return
        grade, reason = grade_of(a, direction)
        sd["research_call_id"] = sig["id"]
        text = messages.research_text(sig, grade, reason, {"max_hold_hours": self.cfg.max_hold_hours})
        ok, secs, _ = self.send(text, "research", sig["id"])
        sig.update(research_call={"grade": grade, "reason": reason, "sent": ok})
        self.store.write("research_call", id=sig["id"], key=sd.get("key"), grade=grade, reason=reason,
                         ok=ok, send_seconds=round(secs, 3))

    def explain(self, s: Setup, a: dict, sig_id: str) -> dict:
        """One sentence each for news and chart, plus the main contradiction."""
        nv, pol, items = a["news"], a["policy"], a["news_items"]
        want = "bullish" if s.direction == LONG else "bearish"
        sup = [i for i in items if i["bias"] == want and i["window"] == "support"]
        if sup:
            i = sup[0]
            pub = messages.stamp(_dt(i["published"])) if i.get("published") else "no publish time"
            news_line = (f"{len(sup)} material {want} item(s) in 6h, e.g. \"{i['title'][:110]}\" "
                         f"({i['publisher'] or i['feed']}, {i['status']}, {i['scope']}; published {pub}, "
                         f"first seen {messages.stamp(_dt(i['first_seen']))}): {i['mechanism']}")
            contra = i.get("contradiction") or "none stated"
        else:
            h = nv.get("health") or {}
            n = h.get("general_news_fresh", len(h.get("fresh_feeds", [])))
            news_line = (f"Fresh check of {n} general news searches (last poll "
                         f"{(nv.get('last_poll') or '?')[11:16]} UTC) found no material Brent news "
                         "in 6h; no news is not a direction signal.")
            contra = "No news support: this is a chart-led alert."
        if s.direction == LONG and a["chart"].get("next_level"):
            contra += f" Next resistance {a['chart']['next_level']['price']:.2f}."
        elif a["chart"].get("next_level"):
            contra += f" Next support {a['chart']['next_level']['price']:.2f}."
        base = self.cfg.report_base_url.rstrip("/")
        return {"label": pol["label"], "policy_row": pol["row"], "assessment": a,
                "news_line": news_line, "chart_line": chart.sentence(a["chart"], s.to_dict()),
                "contradiction": contra,
                "report_url": f"{base}/signals/{sig_id}.html" if base else
                f"signals/{sig_id}.html in the Brent folder"}

    def write_signal_page(self, sig: dict):
        from .report import write_signal_page

        for folder in filter(None, [self.cfg.journal_dir, self.cfg.site_dir]):
            try:
                write_signal_page(sig, self.h1 or [], Path(folder) / "signals" / f"{sig['id']}.html",
                                  self.now())
            except OSError as exc:
                self.store.write("page_error", id=sig["id"], error=str(exc)[:200])

    def why(self, s: Setup) -> str:
        t = s.trigger
        when = messages.stamp(_dt(s.armed_at))
        brk = self.cfg.break_atr * s.atr
        side = "high" if s.level_name == "PDH" else "low"
        base = (f"15-min candle ending {when} (O {t['o']:.2f} H {t['h']:.2f} L {t['l']:.2f} "
                f"C {t['c']:.2f}) vs yesterday's {side} {s.level:.2f}; break threshold {brk:.2f} "
                f"= {self.cfg.break_atr:g} x daily ATR {s.atr:.2f}. ")
        if s.kind == "BREAKOUT":
            return base + (f"It closed beyond the level and price is back at it. Scenario A: the break "
                           f"holds and price extends {'up' if s.direction == LONG else 'down'}. "
                           f"Scenario B: a 15-min close back through {s.level:.2f} means the break "
                           "failed. Untested rule, forward paper tracking only.")
        return base + (f"It traded beyond the level but closed back inside. Scenario A: the rejected "
                       f"break reverts into yesterday's range. Scenario B: a 15-min close beyond "
                       f"{s.level:.2f} means the break is back on. Untested rule, forward paper "
                       "tracking only.")

    def skip_reasons(self, s: Setup, q: Quote, now: datetime) -> list[str]:
        out = []
        if q.spread > 0.05:
            out.append(f"spread {q.spread:.3f} is above the usual ~0.04")
        hold_end = now + timedelta(hours=self.cfg.max_hold_hours)
        t = now
        while t < hold_end:
            b = rules.in_blackout(t, self.cfg.blackouts)
            if b:
                out.append(f"scheduled event inside the holding period ({b} ET, e.g. EIA report)")
                break
            t += timedelta(minutes=5)
        if rules.weekly_close(now) - timedelta(minutes=15) < hold_end:
            out.append("weekly close falls inside the holding period (paper exit Friday 16:45 ET)")
        if rules.minutes_to_close(now) < self.cfg.max_hold_hours * 60:
            out.append("holding period crosses the 17:00 ET daily break (wider spreads, financing)")
        if any(_dt(n["pub"]) > now - timedelta(minutes=60) for n in s.news):
            out.append("oil news in the last 60 min: fast moves possible")
        if now - _dt(s.armed_at) > timedelta(hours=2):
            out.append("setup armed over 2h ago")
        acc = self.st["account"]
        if acc.get("trades"):
            out.append("you already hold a Brent position (see position check)")
        return out

    def sizing(self, s: Setup, entry: float, q: Quote) -> str:
        acc = self.st["account"]
        risk = abs(entry - s.stop)
        ccy = acc.get("currency") or "account currency"
        conv = q.quote_to_home
        per_unit = f"Risk per unit (1 barrel) to the stop is about {risk:.2f} USD"
        if conv:
            per_unit += f" (~{risk * conv:.2f} {ccy}) before costs"
        if self.cfg.risk_pct is None:
            return (f"not shown: no approved risk % for this module (set risk_pct). {per_unit}.")
        nav = acc.get("nav")
        if not conv or not nav or math.isnan(nav):
            return f"not shown: account NAV or USD->{ccy} conversion unavailable. {per_unit}."
        loss_unit = (risk + 2 * q.spread) * conv * (1 + self.cfg.fx_loss_pct / 100)
        units = math.floor(nav * self.cfg.risk_pct / 100 / loss_unit)
        if units < 1:
            return f"0 units: {self.cfg.risk_pct:g}% of NAV is less than one unit's risk. {per_unit}."
        return (f"{units} units would lose ~{self.cfg.risk_pct:g}% of NAV ({nav:,.0f} {ccy}) at the "
                f"stop incl. slippage; notional ~{units * entry * conv:,.0f} {ccy} (notional is not "
                "the risk).")

    # ---------------------------------------------------------- live signals
    def invalidate(self, sig: dict, reason: str):
        if sig["status"] != "valid":
            return
        sig["status"] = "invalidated"
        sig["invalid_reason"] = reason
        now = self.now()
        self.store.write("update", id=sig["id"], status="invalidated", reason=reason)
        if sig.get("delivered") and sig.get("telegram", True):
            self.send_notice(messages.invalidated_text(sig["id"], now, reason), "update", sig["id"], now)

    def update_signals(self, q: Quote | None, now: datetime):
        for sig in list(self.st["signals"].values()):
            s = _setup(sig["setup"])
            if sig["status"] == "valid":
                if now >= _dt(sig["expires_at"]):
                    sig["status"] = "expired"
                    self.store.write("update", id=sig["id"], status="expired")
                    if self.cfg.send_expiry_notice and sig.get("delivered") \
                            and sig.get("telegram", True):
                        self.send_notice(messages.expired_text(sig["id"], now), "update", sig["id"], now)
                elif q is None:
                    self.invalidate(sig, "quote unavailable")
                else:
                    reason = self.validity_problem(s, q, now) or self.eligibility_problem(sig, s, q, now)
                    if reason:
                        self.invalidate(sig, reason)
            self.paper_step(sig, s, q, now)
            if q is not None:
                self.directional_marks(sig, q, now)
            marks_done = len(sig["marks"]) == len(MARKS) or not _tracked(sig)
            if sig["paper"]["state"] in ("missed", "closed") and marks_done \
                    and sig["status"] != "valid":
                self.store.write("signal_done", id=sig["id"])
                del self.st["signals"][sig["id"]]

    def eligibility_problem(self, sig: dict, s: Setup, q: Quote, now: datetime) -> str | None:
        """Combined-arm alerts stay valid only while the news + chart policy still approves the
        same direction (news can become unavailable, stale or contradictory inside the window).
        Reads the news log only: no model call inside the entry window."""
        if sig.get("arm", "combined") != "combined":
            return None
        a = self.assess(s, q, now)
        pol = a["policy"]
        if pol["action"] != s.direction:
            self.store.write("eligibility_lost", id=sig["id"], row=pol["row"],
                             news_state=a["news"].get("state"), news_status=a["news"].get("status"))
            why = a["news"].get("why") or a["news"].get("state")
            return f"news/chart policy no longer approves it: {pol['row']} (news: {why})"
        if pol["label"] != sig.get("label") and \
                sig.get("label_seen") != pol["label"]:
            sig["label_seen"] = pol["label"]
            self.store.write("label_changed", id=sig["id"], label=pol["label"], was=sig.get("label"))
        return None

    def validity_problem(self, s: Setup, q: Quote, now: datetime) -> str | None:
        if not q.tradeable:
            return "market not tradeable"
        if q.age(now) > self.cfg.max_quote_age_seconds:
            return f"quote data stale ({q.age(now):.0f}s)"
        if q.spread > self.cfg.max_spread + 1e-9:
            return f"spread widened to {q.spread:.3f} (limit {self.cfg.max_spread:g})"
        if not rules.in_zone(s, q.bid, q.ask):
            px = rules.executable(s.direction, q.bid, q.ask)
            return (f"{'ask' if s.direction == LONG else 'bid'} {px:.3f} left the entry zone "
                    f"{s.zone_lo:.2f}-{s.zone_hi:.2f}")
        return None

    # ------------------------------------------------------------------ paper
    def paper_step(self, sig: dict, s: Setup, q: Quote | None, now: datetime):
        """Paper rules (paper-v2). Entry: the earliest fill is human_latency_seconds after the
        alert was accepted (or after the decision for a signal that was not sent), only while
        the signal is still valid (zone, spread, freshness, tradeable and, for the combined
        arm, the news + chart policy), at the executable quote then (ask for a buy, bid for a
        sell) plus 1 spread of adverse slippage. The slippage can put the fill up to one spread
        beyond the zone edge; that is deliberate and recorded. Never the best price in the zone.
        Undelivered, expired or invalidated signals never get a new fill."""
        pp = sig["paper"]
        if pp["state"] == "waiting":
            earliest = _dt(sig["sent_at"]) + timedelta(seconds=self.cfg.human_latency_seconds)
            if sig["status"] != "valid" or not _tracked(sig):
                pp.update(state="missed", reason=f"alert {sig['status']} before a fill was possible")
                self.store.write("paper_missed", id=sig["id"], reason=pp["reason"])
            elif q is not None and now >= earliest and q.tradeable \
                    and q.age(now) <= self.cfg.max_quote_age_seconds:
                d = 1 if s.direction == LONG else -1
                px = rules.executable(s.direction, q.bid, q.ask)
                fill = px + d * q.spread
                deadline = min(now + timedelta(hours=self.cfg.max_hold_hours),
                               rules.weekly_close(now) - timedelta(minutes=15))
                pp.update(state="open", entry=round(fill, 4), entry_time=_iso(now),
                          quote_time=_iso(q.time), quote_bid=q.bid, quote_ask=q.ask,
                          quote_age_s=round(q.age(now), 2), executable=px,
                          beyond_zone=round(max(0.0, (fill - s.zone_hi) if d == 1 else (s.zone_lo - fill)), 4),
                          deadline=_iso(deadline), risk=max(abs(fill - s.stop), q.spread),
                          mfe=0.0, mae=0.0, t1=False, last_checked=_iso(now),
                          rules=PAPER_RULES_VERSION)
                self.store.write("paper_fill", id=sig["id"], entry=pp["entry"], at=_iso(now),
                                 bid=q.bid, ask=q.ask, quote_time=_iso(q.time),
                                 quote_age_s=pp["quote_age_s"], beyond_zone=pp["beyond_zone"],
                                 paper_rules=PAPER_RULES_VERSION)
            return
        if pp["state"] != "open" or q is None:
            return
        if now - _dt(pp["last_checked"]) > timedelta(seconds=60):
            if not self.catch_up(sig, now):
                return  # not reconciled: keep last_checked, retry on a later tick
            if pp["state"] != "open":
                return
        if not q.tradeable or q.age(now) > self.cfg.max_quote_age_seconds:
            return  # a stale or non-tradeable quote is not an executable price
        px = q.bid if s.direction == LONG else q.ask   # exit side: sell at bid, buy back at ask
        self.paper_price(sig, s, px, px, q.bid, q.ask, now, live=True)
        pp["last_checked"] = _iso(now)

    def paper_price(self, sig, s: Setup, lo_exit, hi_exit, close_bid, close_ask, now,
                    bar_open: tuple | None = None, live: bool = False):
        """Check stop, 2R target and time exit on one exit-side price observation.

        lo/hi are exit-side prices only (bid for a long, ask for a short): for a live quote
        both are that one price; for a 1-minute candle its low/high on the exit side. If one
        candle touches both stop and target, the stop is assumed first. Stop fills (adverse):
        - live quote at or through the stop: that executable price minus 1 spread (a poll can
          miss the moment the stop was crossed, so the stop price itself is not assumed);
        - candle that opens through the stop: the open minus 1 spread;
        - otherwise: the stop minus 1 spread.
        Targets fill at the target price once the exit-side price reaches it.
        """
        pp, d = sig["paper"], (1 if s.direction == LONG else -1)
        spread = close_ask - close_bid
        worst = lo_exit if d == 1 else hi_exit
        best = hi_exit if d == 1 else lo_exit
        pp["mfe"] = max(pp["mfe"], (best - pp["entry"]) * d / pp["risk"])
        pp["mae"] = min(pp["mae"], (worst - pp["entry"]) * d / pp["risk"])
        t1 = sig["plan"]["t1"]
        if (best - t1) * d >= 0:
            pp["t1"] = True
        if (worst - s.stop) * d <= 0:
            if live:
                px, how = worst - d * spread, "live price through the stop"
            elif bar_open is not None and (bar_open[0] - s.stop) * d <= 0:
                px, how = bar_open[0] - d * spread, "candle opened through the stop"
            else:
                px, how = s.stop - d * spread, "stop touched"
            return self.paper_exit(sig, px, now, "stop", how)
        if (best - sig["plan"]["t2"]) * d >= 0:
            return self.paper_exit(sig, sig["plan"]["t2"], now, "target 2R", "exit-side price reached target")
        if now >= _dt(pp["deadline"]):
            px = close_bid if d == 1 else close_ask
            return self.paper_exit(sig, px, now, "time", "exit-side price at the time limit")

    def paper_exit(self, sig: dict, px: float, now: datetime, reason: str, how: str = ""):
        pp = sig["paper"]
        d = 1 if sig["setup"]["direction"] == LONG else -1
        gross = (px - pp["entry"]) * d
        minutes = (now - _dt(pp["entry_time"])).total_seconds() / 60
        fin = pp["entry"] * self.cfg.financing_pct_year / 100 * minutes / 525_600
        net = gross - fin
        net *= (1 - self.cfg.fx_loss_pct / 100) if net > 0 else (1 + self.cfg.fx_loss_pct / 100)
        pp.update(state="closed", exit=round(px, 4), exit_time=_iso(now), exit_reason=reason,
                  exit_basis=how, gross_r=round(gross / pp["risk"], 3), net_r=round(net / pp["risk"], 3))
        self.store.write("paper_exit", id=sig["id"], exit=pp["exit"], reason=reason, basis=how,
                         gross_r=pp["gross_r"], net_r=pp["net_r"], mfe_r=round(pp["mfe"], 3),
                         mae_r=round(pp["mae"], 3), touched_t1=pp["t1"],
                         paper_rules=pp.get("rules", "paper-v1"), data_gap=pp.get("data_gap", False))

    def catch_up(self, sig: dict, now: datetime) -> bool:
        """Replay completed 1-minute bid/ask candles since the last reconciled time (after a
        restart or a gap). Returns False when the data could not be fetched; then the last
        reconciled time is NOT advanced. Missing minutes while the market should be open are
        recorded as a data gap on the paper record."""
        pp, s = sig["paper"], _setup(sig["setup"])
        since = _dt(pp["last_checked"])
        minutes = int((now - since).total_seconds() // 60) + 2
        try:
            bars = self.client.candles(self.cfg.instrument, "M1", min(5000, max(minutes, 2)))
        except Exception as exc:
            self.store.write("catch_up_error", id=sig["id"], error=str(exc)[:200],
                             since=_iso(since))
            return False
        long_ = s.direction == LONG
        cursor = since
        for b in sorted(bars, key=lambda b: b["time"]):
            end = b["time"] + timedelta(minutes=1)
            if end <= since or not b["complete"]:
                continue
            gap = (b["time"] - cursor).total_seconds() / 60
            if gap > 2 and not rules.market_closed_by_clock(cursor + timedelta(minutes=1)):
                pp["data_gap"] = True
                self.store.write("catch_up_gap", id=sig["id"], start=_iso(cursor),
                                 end=_iso(b["time"]), minutes=round(gap, 1))
            lo, hi = (b["bid_l"], b["bid_h"]) if long_ else (b["ask_l"], b["ask_h"])
            opn = (b["bid_o"] if long_ else b["ask_o"],)
            self.paper_price(sig, s, lo, hi, b["bid_c"], b["ask_c"], min(end, now), bar_open=opn)
            pp["last_checked"] = _iso(end)
            cursor = end
            if pp["state"] != "open":
                return True
        if (now - cursor).total_seconds() > 120 and not rules.market_closed_by_clock(cursor):
            pp["data_gap"] = True
            self.store.write("catch_up_gap", id=sig["id"], start=_iso(cursor), end=_iso(now),
                             minutes=round((now - cursor).total_seconds() / 60, 1))
        return True

    def directional_marks(self, sig: dict, q: Quote, now: datetime):
        """Forecast check: signed mid move at +60 and +240 min, in daily-ATR units, for every
        delivered alert whether or not a paper fill happened."""
        if not _tracked(sig) or q.age(now) > self.cfg.max_quote_age_seconds:
            return
        d = 1 if sig["setup"]["direction"] == LONG else -1
        for m in MARKS:
            key = str(m)
            if key in sig["marks"] or now < _dt(sig["decided_at"]) + timedelta(minutes=m):
                continue
            move = (q.mid - sig["alert_mid"]) * d / sig["setup"]["atr"]
            late = (now - _dt(sig["decided_at"])).total_seconds() / 60 - m
            sig["marks"][key] = round(move, 3)
            self.store.write("mark", id=sig["id"], minutes=m, move_atr=round(move, 3),
                             late_minutes=round(late, 1))

    # ------------------------------------------------------------------- loop
    def run(self, stop: Callable[[], bool] = lambda: False, sleep: Callable[[float], None] = time.sleep,
            on_tick: Callable[[], None] | None = None):
        while not stop():
            try:
                self.tick()
            except Exception as exc:  # keep monitoring; report once per half hour
                self.store.write("tick_error", error=f"{type(exc).__name__}: {exc}"[:300])
                self.ops_notice("tick_error", f"monitor error: {type(exc).__name__}: {exc}"[:300])
            if on_tick:
                on_tick()
            sleep(self.cfg.poll_seconds)
