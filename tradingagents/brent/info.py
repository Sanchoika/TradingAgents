"""Brent information lane (INFO): useful context messages that are never trade suggestions.

Separate from the trade lifecycle (WATCH / ENTRY / UPDATE / CANCEL): nothing here creates a
setup, a signal or a paper fill, and no INFO text carries an entry, stop or target. Every number
comes from code (quotes, candles, the classified news log); no model call is made here.

Kinds:
  brief     scheduled session briefs (default 07:00 and 12:00 New York, active market days)
  headline  a material news item (classified by the collector) with the measured price reaction
  update    a confirmation / update / denial / correction / resolution of an event already sent
  level     Brent held beyond a key level (prior-day or 20-day high/low) for 15 minutes
  move      a fast move: at least ``info_move_atr`` x daily ATR within 60 minutes

Caps (defaults, per New York day): 4 non-urgent messages including 2 slots kept for the briefs,
at least 45 minutes between unscheduled routine messages; urgent headlines and fast moves skip
the spacing but have their own ceiling; updates of already-sent information are never blocked by
the routine cap but have their own ceiling (no loops). INFO may run outside the trade range,
with a label. With ``info_alerts`` off, everything is computed and logged as shadow, not sent.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from . import news, rules

INFO_VERSION = "brent-info-1"
MID_HISTORY = timedelta(hours=6)
HEADLINE_MAX_AGE = timedelta(hours=2)     # older news is not "fresh" any more
BRIEF_LATE = timedelta(minutes=45)        # a brief this late is skipped, not sent stale
LEVEL_HOLD = timedelta(minutes=15)
MOVE_WINDOW = timedelta(minutes=60)
MOVE_REPEAT = timedelta(hours=3)
BRENT_HOLIDAYS = ("01-01", "12-25")       # ICE Brent closed; other US holidays it trades


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def active_day(now: datetime) -> bool:
    t = rules.ny(now)
    return t.weekday() < 5 and t.strftime("%m-%d") not in BRENT_HOLIDAYS


def us_federal_holidays(year: int) -> set[str]:
    """US federal holidays (observed dates), from the statutory rules."""
    def nth(month, weekday, n):
        d = date(year, month, 1)
        return d + timedelta(days=(weekday - d.weekday()) % 7, weeks=n - 1)

    def last(month, weekday):
        d = date(year, month + 1, 1) - timedelta(days=1)
        return d - timedelta(days=(d.weekday() - weekday) % 7)

    def obs(d):
        return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d

    days = {obs(date(year, 1, 1)), nth(1, 0, 3), nth(2, 0, 3), last(5, 0), obs(date(year, 6, 19)),
            obs(date(year, 7, 4)), nth(9, 0, 1), nth(10, 0, 2), obs(date(year, 11, 11)), nth(11, 3, 4),
            obs(date(year, 12, 25))}
    return {d.isoformat() for d in days}


def next_eia(now: datetime) -> tuple[datetime, bool]:
    """Next weekly petroleum status report time on the normal schedule (Wednesday 10:30 ET) and
    whether that week holds a US federal holiday Mon-Wed, when EIA often moves it (then the time
    is NOT verified and is shown as such)."""
    t = rules.ny(now)
    d = t.date() + timedelta(days=(2 - t.weekday()) % 7)
    at = datetime(d.year, d.month, d.day, 10, 30, tzinfo=rules.NY)
    if at <= t:
        d += timedelta(days=7)
        at = datetime(d.year, d.month, d.day, 10, 30, tzinfo=rules.NY)
    monday = d - timedelta(days=2)
    hol = us_federal_holidays(d.year)
    shifted = any((monday + timedelta(days=i)).isoformat() in hol for i in range(3))
    return at, shifted


def _countdown(at: datetime, now: datetime) -> str:
    mins = int((at - now).total_seconds() // 60)
    if mins < 60:
        return f"in {mins} min"
    h, m = divmod(mins, 60)
    return f"in {h} h {m:02d} min" if h < 24 else f"{rules.ny(at):%a %d %b %H:%M} ET"


class InfoLane:
    """Owned by the monitor. ``st`` is the monitor's state dict (persisted with it)."""

    def __init__(self, cfg, store, st: dict, send):
        self.cfg, self.store, self.send = cfg, store, send
        self.st = st.setdefault("info", {})
        self.st.setdefault("seq", 0)
        self.st.setdefault("days", {})
        self.st.setdefault("mids", [])
        self.st.setdefault("sent_items", {})     # news item key -> INFO id
        self.st.setdefault("sent_events", {})    # event cluster -> INFO id
        self.st.setdefault("levels", {})         # level name@price -> NY date sent
        self.st.setdefault("moves", {})          # "up"/"down" -> time sent
        self.st.setdefault("briefs", [])         # "YYYY-MM-DD HH:MM" done (sent, shadowed or skipped)

    # ---------------------------------------------------------------- bookkeeping
    def _day(self, now: datetime) -> dict:
        key = rules.ny(now).date().isoformat()
        days = self.st["days"]
        for k in [k for k in days if k < (rules.ny(now).date() - timedelta(days=7)).isoformat()]:
            del days[k]
        return days.setdefault(key, {"brief": 0, "routine": 0, "urgent": 0, "update": 0,
                                     "last_routine": None})

    def record_mid(self, mid: float, now: datetime):
        mids = self.st["mids"]
        if not mids or now - datetime.fromisoformat(mids[-1][0]) >= timedelta(seconds=55):
            mids.append([_iso(now), round(mid, 3)])
        cut = now - MID_HISTORY
        while mids and datetime.fromisoformat(mids[0][0]) < cut:
            mids.pop(0)

    def mid_at(self, t: datetime) -> float | None:
        """Recorded mid nearest to ``t`` (within 2 minutes), else None (not measured)."""
        best = None
        for ts, m in self.st["mids"]:
            gap = abs((datetime.fromisoformat(ts) - t).total_seconds())
            if gap <= 120 and (best is None or gap < best[0]):
                best = (gap, m)
        return best[1] if best else None

    def allowed(self, kind: str, now: datetime) -> str | None:
        """None if a message of this class may go out now, else why not."""
        c, d = self.cfg, self._day(now)
        if kind == "brief":
            return None if d["brief"] < c.info_brief_slots else "brief slots used"
        if kind == "update":
            return None if d["update"] < c.info_updates_per_day else "update ceiling reached"
        if kind == "urgent":
            return None if d["urgent"] < c.info_urgent_per_day else "urgent ceiling reached"
        routine_cap = c.info_per_day - c.info_brief_slots
        if d["routine"] >= routine_cap:
            return f"routine cap {routine_cap}/day reached"
        last = d["last_routine"]
        if last and now - datetime.fromisoformat(last) < timedelta(minutes=c.info_min_gap_minutes):
            return f"less than {c.info_min_gap_minutes} min since the last routine message"
        return None

    def emit(self, kind: str, cls: str, text: str, now: datetime, ref: dict) -> str | None:
        """Send (or shadow-log) one INFO. ``cls`` is brief / routine / urgent / update."""
        why = self.allowed(cls, now)
        if why:
            self.store.write("info_held", kind=kind, cls=cls, reason=why, **ref)
            return None
        self.st["seq"] += 1
        info_id = f"BI-{rules.ny(now):%Y%m%d}-{self.st['seq']:03d}"
        text = text.replace("{ID}", info_id)
        live = bool(self.cfg.info_alerts)
        ok = self.send(text, info_id, now) if live else False
        d = self._day(now)
        d[cls] += 1
        if cls == "routine":
            d["last_routine"] = _iso(now)
        self.store.write("info", id=info_id, kind=kind, cls=cls, sent=live, accepted=ok if live else None,
                         shadow=not live, version=INFO_VERSION, text=text, **ref)
        return info_id

    # ---------------------------------------------------------------- the step
    def step(self, q, ctx: dict | None, daily: list[dict] | None, news_folder: str, now: datetime):
        if q is None or not getattr(q, "tradeable", True):
            return
        self.record_mid(q.mid, now)
        if not active_day(now) or rules.market_closed_by_clock(now):
            return
        items = news.classified_items(news_folder, now)
        self.briefs(q, ctx, daily, items, news_folder, now)
        self.headlines(q, items, now)
        self.updates(q, items, now)
        if ctx:
            self.levels(q, ctx, daily, now)
            self.moves(q, ctx, now)

    # ---------------------------------------------------------------- briefs
    def briefs(self, q, ctx, daily, items, news_folder, now):
        t = rules.ny(now)
        for hm in self.cfg.info_briefs_et:
            h, m = (int(x) for x in hm.split(":"))
            at = t.replace(hour=h, minute=m, second=0, microsecond=0)
            key = f"{t.date().isoformat()} {hm}"
            if t < at or key in self.st["briefs"]:
                continue
            self.st["briefs"] = (self.st["briefs"] + [key])[-20:]
            if t - at > BRIEF_LATE:
                self.store.write("info_held", kind="brief", cls="brief", reason="too late to be useful",
                                 slot=key)
                continue
            text = self.brief_text(q, ctx, daily, items, news_folder, now, hm)
            self.emit("brief", "brief", text, now, {"slot": key})

    def brief_text(self, q, ctx, daily, items, news_folder, now, hm) -> str:
        lines = [f"[BRENT INFO] {{ID}} | {hm} ET BRIEF | INFORMATION ONLY | no trade suggested"]
        lines.append(f"Brent mid {q.mid:.2f} (spread {q.ask - q.bid:.3f}) at {rules.ny(now):%H:%M} ET."
                     + self._range_note(q))
        done = [b for b in (daily or []) if b.get("complete", True)]
        if len(done) >= 2 and ctx:
            prev = done[-1]
            chg = q.mid - prev["c"]
            lines.append(f"Since the last daily close {prev['c']:.2f}: {chg:+.2f} ({chg / ctx['atr']:+.2f} daily ATR; "
                         f"ATR {ctx['atr']:.2f}). Yesterday's range {ctx['pdl']:.2f} to {ctx['pdh']:.2f}.")
            last20 = done[-20:]
            if len(last20) == 20:
                hi, lo = max(b["h"] for b in last20), min(b["l"] for b in last20)
                lines.append(f"20-day range {lo:.2f} to {hi:.2f}; now at {100 * (q.mid - lo) / (hi - lo):.0f}% of it."
                             if hi > lo else "20-day range: flat")
        else:
            lines.append("Daily candles unavailable: change and levels not shown.")
        at, shifted = next_eia(now)
        lines.append("Next EIA weekly report: " + (
            f"{_countdown(at, now)} (Wed 10:30 ET normal schedule)" if not shifted else
            "this is a holiday week, so the release day is not verified (normally Wed 10:30 ET)"))
        nv = news.news_view(news_folder, now)
        mat = sorted((i for i in items if i["material"]), key=lambda i: i["first_seen"], reverse=True)[:3]
        if nv.get("state") == "unavailable":
            lines.append(f"News check: unavailable ({nv.get('why', 'unknown')}).")
        else:
            lines.append(f"News check: {nv.get('state')} (material items, model reading of headlines).")
        for i in mat:
            c = i["classification"]
            lines.append(f"- {i['title'][:140]} ({i.get('publisher') or '?'}, {c['status']}, "
                         f"{c['bias']}; {self._evidence_label(i)})")
        if not mat and nv.get("state") != "unavailable":
            lines.append("No material oil news in the last 24 hours.")
        return "\n".join(lines)

    # ---------------------------------------------------------------- headlines
    def headlines(self, q, items, now):
        for i in sorted(items, key=lambda x: x["first_seen"]):
            if not i["material"] or i["changes_event"] or i["key"] in self.st["sent_items"]:
                continue
            cluster = i.get("cluster") or i["key"]
            if cluster in self.st["sent_events"]:
                self.st["sent_items"][i["key"]] = self.st["sent_events"][cluster]
                continue
            if now - datetime.fromisoformat(i["first_seen"]) > HEADLINE_MAX_AGE:
                self.st["sent_items"][i["key"]] = None      # stale by the time it was classified
                self.store.write("info_held", kind="headline", cls="routine", reason="older than 2 h",
                                 item=i["key"])
                continue
            cls = "urgent" if i["urgent"] else "routine"
            if self.allowed(cls, now):
                continue                                    # held; may go out later while fresh
            info_id = self.emit("headline", cls, self.headline_text(q, i, now), now,
                                {"item": i["key"], "cluster": cluster})
            if info_id:
                self.st["sent_items"][i["key"]] = info_id
                self.st["sent_events"][cluster] = info_id

    def headline_text(self, q, i, now) -> str:
        c = i["classification"]
        seen = datetime.fromisoformat(i["first_seen"])
        pub = f"published {rules.ny(datetime.fromisoformat(i['published'])):%H:%M} ET, " if i.get("published") else ""
        lines = ["[BRENT INFO] {ID} | NEWS | INFORMATION ONLY | no trade suggested",
                 f"{i['title'][:200]} ({i.get('publisher') or 'unknown publisher'}; {c['status'].replace('_', ' ')})",
                 f"{pub}first seen {rules.ny(seen):%H:%M} ET. Evidence: {self._evidence_label(i)}.",
                 f"Model reading: {c['bias']}. {c['mechanism'] or 'No mechanism stated.'}"
                 + (f" Against it: {c['contradiction']}" if c.get("contradiction") else ""),
                 self._reaction(q, seen, now) + self._range_note(q)]
        return "\n".join(lines)

    # ---------------------------------------------------------------- updates
    def updates(self, q, items, now):
        for i in sorted(items, key=lambda x: x["first_seen"]):
            if not i["changes_event"] or i["key"] in self.st["sent_items"]:
                continue
            cluster = i.get("cluster") or i["key"]
            earlier = self.st["sent_events"].get(cluster)
            if not earlier:
                continue          # nothing was sent about this event: a material one goes as NEWS
            if now - datetime.fromisoformat(i["first_seen"]) > timedelta(hours=6):
                self.st["sent_items"][i["key"]] = None
                continue
            if self.allowed("update", now):
                continue
            c = i["classification"]
            rel = i["relation"]
            text = "\n".join([
                f"[BRENT INFO] {{ID}} | UPDATE to {earlier}: {rel.upper()} | INFORMATION ONLY",
                f"{i['title'][:200]} ({i.get('publisher') or '?'}; {c['status'].replace('_', ' ')}; "
                f"evidence: {self._evidence_label(i)})",
                "This reports the " + rel + " as published; it does not by itself prove or disprove the "
                "earlier report." if rel in ("denial", "correction") else f"Reported as a {rel} of the earlier item.",
                self._reaction(q, datetime.fromisoformat(i["first_seen"]), now)])
            info_id = self.emit("update", "update", text, now, {"item": i["key"], "cluster": cluster,
                                                                "updates": earlier, "relation": rel})
            self.st["sent_items"][i["key"]] = info_id

    # ---------------------------------------------------------------- levels and moves
    def levels(self, q, ctx, daily, now):
        a = ctx["atr"]
        lv = [("yesterday's high", ctx["pdh"], 1), ("yesterday's low", ctx["pdl"], -1)]
        done = [b for b in (daily or []) if b.get("complete", True)][-20:]
        if len(done) == 20:
            lv += [("20-day high", max(b["h"] for b in done), 1), ("20-day low", min(b["l"] for b in done), -1)]
        today = rules.ny(now).date().isoformat()
        self.st["levels"] = {k: v for k, v in self.st["levels"].items() if v == today}
        recent = [(datetime.fromisoformat(t), m) for t, m in self.st["mids"]
                  if now - datetime.fromisoformat(t) <= LEVEL_HOLD]
        span_ok = recent and now - recent[0][0] >= LEVEL_HOLD - timedelta(minutes=2)
        for name, price, side in lv:
            key = f"{name}@{price:.2f}"
            if key in self.st["levels"] or not span_ok:
                continue
            beyond = all(side * (m - price) >= 0.05 * a for _, m in recent)
            if not beyond or self.allowed("routine", now):
                continue
            word = "above" if side > 0 else "below"
            text = "\n".join([
                "[BRENT INFO] {ID} | LEVEL | INFORMATION ONLY | no trade suggested",
                f"Brent has held {word} {name} {price:.2f} for 15 minutes: mid {q.mid:.2f} "
                f"({(q.mid - price) / a:+.2f} daily ATR from it).",
                "Any trade setup is handled separately by WATCH/ENTRY messages." + self._range_note(q)])
            if self.emit("level", "routine", text, now, {"level": name, "price": price}):
                self.st["levels"][key] = today

    def moves(self, q, ctx, now):
        past = self.mid_at(now - MOVE_WINDOW)
        if past is None:
            return
        chg = q.mid - past
        a = ctx["atr"]
        if abs(chg) < self.cfg.info_move_atr * a:
            return
        way = "up" if chg > 0 else "down"
        last = self.st["moves"].get(way)
        if last and now - datetime.fromisoformat(last) < MOVE_REPEAT:
            return
        if self.allowed("urgent", now):
            return
        text = "\n".join([
            f"[BRENT INFO] {{ID}} | FAST MOVE {way.upper()} | INFORMATION ONLY | no trade suggested",
            f"Brent {chg:+.2f} in the last 60 minutes ({chg / a:+.2f} daily ATR): {past:.2f} -> {q.mid:.2f}.",
            "Cause not established; check the news before acting." + self._range_note(q)])
        if self.emit("move", "urgent", text, now, {"change": round(chg, 3), "atr": a}):
            self.st["moves"][way] = _iso(now)

    # ---------------------------------------------------------------- helpers
    def _range_note(self, q) -> str:
        if self.cfg.range_low <= q.mid <= self.cfg.range_high:
            return ""
        return (f" Note: Brent is outside the {self.cfg.range_low:g}-{self.cfg.range_high:g} range, "
                "where no trade alerts are made.")

    @staticmethod
    def _evidence_label(i) -> str:
        return {"full_text": "article read", "snippet_only": "headline and snippet only",
                "redirect_unresolved": "headline and snippet only (link not resolved)",
                "paywall": "headline and snippet only (paywalled)"}.get(
            i.get("evidence"), "headline and snippet only")

    def _reaction(self, q, seen: datetime, now: datetime) -> str:
        base = self.mid_at(seen)
        if base is None:
            return "Price reaction since first seen: not measured (no price recorded then)."
        mins = int((now - seen).total_seconds() // 60)
        return (f"Price since first seen ({mins} min): {base:.2f} -> {q.mid:.2f} ({q.mid - base:+.2f}); "
                "a move after the news is not proof it was caused by it.")


def today_counts(st: dict, day: date) -> dict:
    return ((st.get("info") or {}).get("days") or {}).get(day.isoformat(), {})
