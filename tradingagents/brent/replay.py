"""Replay lab v1 (2026-11-12, Irakli's list item 6): preregistered replays of the bot's own setups on history.

Three rules, enforced in code and printed on every report:
1. **Preregister first.** A replay is described in a JSON file (hypothesis, instrument, setups, optional lab
   variant, period, pass rule). `register` hashes it into docs/research/replay-registry.jsonl; `run` refuses a file
   that is not registered or was edited after registering. A changed idea is a new preregistration.
2. **A filter, not proof.** A pass only means "worth forward-testing" (lab or research track). Nothing goes live
   or changes a rule because of a replay.
3. **Contaminated history is stated.** Brent's 2013-2026 history was already used to design and test the live
   setups (prereg-21/22/23, brent-exp-1..4 tuning, families v1 written by someone who had seen it), so every Brent
   replay over that period is in-sample and says so.

Simulation (documented approximations, same code as the live bot where possible):
- completed 15-minute mid bars and New York trading days (17:00 roll) built from M1 bid/ask;
- setups from the bot's own rules.detect (yesterday's high/low breaks) and families.detect;
- entry: the first 1-minute close after arming where the bot's own rules.entry_blockers is empty (zone, spread,
  stop distance, daily close, blackouts), at the executable price plus one spread (paper-v2 slippage), unless a
  15-minute close invalidates the setup first; the operating price range is ignored for history;
- exit: outcomes.manage "plain" (stop one spread worse, 2R target or the variant's target, time limit); minus
  financing for the full holding time (conservative) and the CAD conversion cost as in paper-v2;
- one trade per setup key; the live cooldown and open-trade limits are not modelled.

    python -m tradingagents.brent.replay register PREREG.json
    python -m tradingagents.brent.replay run PREREG.json M1.csv OUT_DIR
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import families, outcomes, rules
from .config import BrentConfig, load_config
from .lab import BASES, BOUNDS

REGISTRY = Path(__file__).resolve().parents[2] / "docs" / "research" / "replay-registry.jsonl"
CONTAMINATED = {
    "BCO_USD": ("2026-11-11", "Brent 2013-2026 was used for prereg-21/22/23, the brent-exp tuning and seen before families v1; "
                "any replay before 2026-11-11 is in-sample."),
}
KEYS = {"name", "written", "hypothesis", "instrument", "setups", "variant", "period", "pass_rule", "config_file",
        "config_overrides"}


def canon(pr: dict) -> str:
    return json.dumps(pr, sort_keys=True, separators=(",", ":"))


def prereg_hash(pr: dict) -> str:
    return hashlib.sha256(canon(pr).encode()).hexdigest()[:16]


def check(pr: dict) -> dict:
    if not isinstance(pr, dict) or not {"name", "written", "hypothesis", "instrument", "setups", "period",
                                        "pass_rule"} <= set(pr) or set(pr) - KEYS:
        raise ValueError(f"preregistration needs exactly these fields: {sorted(KEYS)}")
    if not pr["setups"] or set(pr["setups"]) - set(BASES):
        raise ValueError(f"setups must be from {BASES}")
    v = pr.get("variant")
    if v is not None:
        if set(v) != {"direction", "hours_et", *BOUNDS}:
            raise ValueError("variant needs direction, hours_et, stop_mult, target_r, hold_hours")
        for k, (lo, hi) in BOUNDS.items():
            if not lo <= v[k] <= hi:
                raise ValueError(f"variant {k} out of bounds")
    pr_ = pr["pass_rule"]
    if set(pr_) != {"min_trades", "ci_low_above", "mean_at_least"}:
        raise ValueError("pass_rule needs min_trades, ci_low_above, mean_at_least")
    datetime.fromisoformat(pr["period"]["start"])
    datetime.fromisoformat(pr["period"]["end"])
    return pr


def register(path: str, registry: Path = REGISTRY, now: datetime | None = None) -> str:
    pr = check(json.loads(Path(path).read_text(encoding="utf-8")))
    h = prereg_hash(pr)
    rows = [json.loads(x) for x in registry.read_text(encoding="utf-8").splitlines() if x.strip()] \
        if registry.exists() else []
    for r in rows:
        if r["name"] == pr["name"]:
            if r["hash"] != h:
                raise ValueError(f"{pr['name']} is already registered with different content: use a new name")
            return h
    registry.parent.mkdir(parents=True, exist_ok=True)
    with registry.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"name": pr["name"], "hash": h, "registered_at": (now or datetime.now(UTC)).isoformat(),
                            "file": Path(path).name}) + "\n")
    return h


def registered(pr: dict, registry: Path = REGISTRY) -> dict:
    h = prereg_hash(pr)
    rows = [json.loads(x) for x in registry.read_text(encoding="utf-8").splitlines() if x.strip()] \
        if registry.exists() else []
    hit = next((r for r in rows if r["hash"] == h and r["name"] == pr["name"]), None)
    if not hit:
        raise ValueError("not preregistered (or edited after registering): run `register` first; results stay unseen")
    return hit


# ------------------------------------------------------------------------------------------ data
def load_m1(path: str, start: datetime, end: datetime) -> list[dict]:
    import pandas as pd

    d = pd.read_csv(path)
    t = pd.to_datetime(d.pop("time"), unit="s", utc=True)
    keep = (t >= start - timedelta(days=40)) & (t < end + timedelta(days=2))
    d, t = d[keep], t[keep]
    cols = ["bid_o", "bid_h", "bid_l", "bid_c", "ask_o", "ask_h", "ask_l", "ask_c"]
    return [{"time": ts.to_pydatetime(), "complete": True, **dict(zip(cols, row, strict=True))}
            for ts, row in zip(t, d[cols].itertuples(index=False, name=None), strict=True)]


def _mid(b: dict, f: str) -> float:
    return (b[f"bid_{f}"] + b[f"ask_{f}"]) / 2


def bars(m1: list[dict], minutes: int = 15) -> list[dict]:
    """Completed mid bars on the clock (start time), plus the index range of their M1 rows."""
    out, cur = [], None
    for i, b in enumerate(m1):
        t = b["time"]
        start = t.replace(minute=t.minute - t.minute % minutes, second=0, microsecond=0)
        if cur is None or cur["time"] != start:
            cur = {"time": start, "o": _mid(b, "o"), "h": _mid(b, "h"), "l": _mid(b, "l"), "c": _mid(b, "c"),
                   "i0": i, "i1": i}
            out.append(cur)
        else:
            cur.update(h=max(cur["h"], _mid(b, "h")), l=min(cur["l"], _mid(b, "l")), c=_mid(b, "c"), i1=i)
    return out


def trading_days(m1: list[dict]) -> list[dict]:
    """New York trading days rolling at 17:00 (the bot's daily candles)."""
    out, cur = [], None
    for b in m1:
        day = (rules.ny(b["time"]) + timedelta(hours=7)).date().isoformat()
        if cur is None or cur["day"] != day:
            cur = {"day": day, "time": b["time"], "o": _mid(b, "o"), "h": _mid(b, "h"), "l": _mid(b, "l"),
                   "c": _mid(b, "c"), "complete": True}
            out.append(cur)
        else:
            cur.update(h=max(cur["h"], _mid(b, "h")), l=min(cur["l"], _mid(b, "l")), c=_mid(b, "c"))
    return out


# ------------------------------------------------------------------------------------ simulation
def simulate(m1: list[dict], cfg: BrentConfig, setups: list[str], variant: dict | None,
             start: datetime, end: datetime) -> list[dict]:
    cfg = replace(cfg, range_low=1e-9, range_high=1e12)   # history ranges differ: operating range not applied
    m15, days = bars(m1), trading_days(m1)
    by_day = {d["day"]: k for k, d in enumerate(days)}
    trades, seen, recent = [], set(), []
    want_base = {"BREAKOUT", "FAILED_BREAK"} & set(setups)
    want_fam = set(families.FAMILIES) & set(setups)
    for j, bar in enumerate(m15):
        close_t = bar["time"] + timedelta(minutes=15)
        recent = (recent + [bar])[-40:]
        if not (start <= close_t < end) or bar["i1"] - bar["i0"] < 10:
            continue
        k = by_day[(rules.ny(bar["time"]) + timedelta(hours=7)).date().isoformat()]
        daily = days[max(0, k - 60):k]
        if len(daily) < cfg.atr_period + 1:
            continue
        a = rules.atr(daily, cfg.atr_period)
        found = []
        if want_base:
            pd_ = rules.previous_day(daily)
            found += [s for s in rules.detect(bar, close_t, pd_["h"], pd_["l"], a, days[k]["day"], cfg)
                      if s.kind in want_base]
        if want_fam:
            found += [s for s in families.detect(bar, close_t, recent, a, daily, cfg.family_arm_hours,
                                                 cfg.price_decimals, cfg.eia_family, cfg.orb_ny_start) if s.kind in want_fam]
        for s in found:
            if s.key in seen:
                continue
            seen.add(s.key)
            t = _trade(m1, m15, j, s, cfg, variant)
            if t:
                trades.append(t)
    return trades


def _trade(m1, m15, j, s, cfg, variant):
    d = 1 if s.direction == rules.LONG else -1
    if variant and variant["direction"] not in ("both", s.direction):
        return None
    expires = datetime.fromisoformat(s.expires_at)
    i = m15[j]["i1"] + 1
    nxt = j + 1
    while i < len(m1) and m1[i]["time"] < expires:
        b = m1[i]
        while nxt < len(m15) and m15[nxt]["i1"] < i:          # a 15-minute close beyond invalid_close cancels it
            if rules.close_invalidates(s, m15[nxt]):
                return None
            nxt += 1
        now = b["time"] + timedelta(minutes=1)
        if not rules.entry_blockers(s, b["bid_c"], b["ask_c"], now, True, now, cfg):
            if variant and not variant["hours_et"][0] <= rules.ny(now).hour < variant["hours_et"][1]:
                return None
            spread = b["ask_c"] - b["bid_c"]
            entry = rules.executable(s.direction, b["bid_c"], b["ask_c"]) + d * spread
            dist = abs(entry - s.stop) * (variant["stop_mult"] if variant else 1.0)
            if dist <= 0:
                return None
            stop = entry - d * dist
            tr = variant["target_r"] if variant else 2.0
            hold = variant["hold_hours"] if variant else cfg.max_hold_hours
            deadline = min(now + timedelta(hours=hold), rules.weekly_close(now) - timedelta(minutes=15))
            window = [x for x in m1[i + 1:i + 1 + int(hold * 60) + 1500] if x["time"] < deadline + timedelta(minutes=1)]
            m = outcomes.manage(window, s.direction, entry, stop, dist, entry + d * dist, entry + d * tr * dist,
                                now, deadline)
            if not m:
                return None
            fin = entry * cfg.financing_pct_year / 100 * hold / 8760 / dist
            net = m["plain"] - fin
            net *= (1 - cfg.fx_loss_pct / 100) if net > 0 else (1 + cfg.fx_loss_pct / 100)
            base = outcomes.baseline(window, dist, now, now + timedelta(minutes=1), hold, f"{s.key}", n=20)
            return {"key": s.key, "kind": s.kind, "direction": s.direction, "entry_time": now.isoformat(),
                    "gross_r": m["plain"], "net_r": round(net, 3), "baseline_r": base["mean_r"] if base else None}
        i += 1
    return None


def stats(rs: list[float]) -> dict:
    n = len(rs)
    if not n:
        return {"n": 0}
    mean = sum(rs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) if n > 1 else 0.0
    cum = peak = worst = 0.0
    for x in rs:
        cum += x
        peak = max(peak, cum)
        worst = min(worst, cum - peak)
    half = 1.96 * sd / math.sqrt(n) if n > 1 else None
    return {"n": n, "mean": round(mean, 3), "win": round(sum(x > 0 for x in rs) / n, 3), "total": round(sum(rs), 2),
            "range": [round(mean - half, 3), round(mean + half, 3)] if half is not None else None,
            "worst_run": round(worst, 2)}


def verdict(pr: dict, trades: list[dict]) -> dict:
    rs = [t["net_r"] for t in trades]
    s, half = stats(rs), len(rs) // 2
    a, b = stats(rs[:half]), stats(rs[half:])
    diffs = [t["net_r"] - t["baseline_r"] for t in trades if t["baseline_r"] is not None]
    ch = stats(diffs)
    rule = pr["pass_rule"]
    ok = (s["n"] >= rule["min_trades"] and s.get("range") and s["range"][0] > rule["ci_low_above"]
          and s["mean"] >= rule["mean_at_least"] and a.get("mean", -1) > 0 and b.get("mean", -1) > 0
          and ch.get("range") and ch["range"][0] > 0)
    return {"all": s, "first_half": a, "second_half": b, "vs_random": ch,
            "by_setup": {k: {**(x := stats([t["net_r"] for t in trades if t["kind"] == k])), "label": label(x, rule)}
                         for k in pr["setups"]},
            "passes_filter": bool(ok)}


def label(s: dict, rule: dict) -> str:
    """Per setup: kill (clearly negative), promising (passes the mean and range parts of the rule), else watch."""
    if s["n"] < 30 or not s.get("range"):
        return "watch (too few)"
    if s["range"][1] < 0:
        return "kill"
    if s["n"] >= rule["min_trades"] and s["range"][0] > rule["ci_low_above"] and s["mean"] >= rule["mean_at_least"]:
        return "promising"
    return "watch"


def contamination(pr: dict) -> str:
    c = CONTAMINATED.get(pr["instrument"])
    if c and pr["period"]["start"] < c[0]:
        return f"IN-SAMPLE: {c[1]}"
    if c:
        return "Period starts after the contamination date: out-of-sample for the live setups."
    return ("Not used in an earlier preregistration in this project, but public history that the setups' designer may "
            "have seen; treat as weakly out-of-sample.")


def report(pr: dict, reg: dict, v: dict) -> str:
    s = v["all"]
    lines = [f"# Replay: {pr['name']}", "",
             f"Preregistered {reg['registered_at']} (hash {reg['hash']}). Hypothesis: {pr['hypothesis']}", "",
             "**A filter, not proof.** A pass means only: worth forward-testing in the lab or research track.", "",
             f"**History:** {contamination(pr)}", "",
             f"**Result:** {s['n']} trades, average {s.get('mean')} R after costs, likely {s.get('range')}, "
             f"worst losing run {s.get('worst_run')} R; halves {v['first_half'].get('mean')} / "
             f"{v['second_half'].get('mean')} R; vs a coin-flip direction at the same minutes {v['vs_random'].get('mean')} R "
             f"(likely {v['vs_random'].get('range')}).", "",
             f"**Filter:** {'PASSES: worth a forward test' if v['passes_filter'] else 'FAILS'} "
             f"(rule {pr['pass_rule']}, both halves positive, beats the coin-flip direction).", "",
             "Caveats: the likely ranges assume independent trades; trades that overlap in time make them too narrow. "
             "The coin-flip comparison has no financing or currency cost (the setups' R does), so it leans against the setups.", "",
             "| Setup | Trades | Avg R | Likely range | Verdict |", "|---|---|---|---|---|"]
    lines += [f"| {k} | {x['n']} | {x.get('mean')} | {x.get('range')} | {x['label']} |" for k, x in v["by_setup"].items()]
    return "\n".join(lines) + "\n"


def provenance(csv: str, cfg: BrentConfig) -> dict:
    """What the preregistration hash does not cover: the code version, the effective config and the data file."""
    import dataclasses
    import subprocess
    root = Path(__file__).resolve().parents[2]
    git = lambda *a: subprocess.run(["git", *a], cwd=root, capture_output=True, text=True).stdout.strip()   # noqa: E731
    h = hashlib.sha256()
    with open(csv, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    conf = json.dumps(dataclasses.asdict(cfg), sort_keys=True, default=str)
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain", "tradingagents")),
            "config_sha256": hashlib.sha256(conf.encode()).hexdigest()[:16], "csv_sha256": h.hexdigest()[:16]}


def run(prereg_path: str, csv: str, out_dir: str, registry: Path = REGISTRY) -> dict:
    pr = check(json.loads(Path(prereg_path).read_text(encoding="utf-8")))
    reg = registered(pr, registry)
    cfg = load_config(pr["config_file"]) if pr.get("config_file") else BrentConfig(instrument=pr["instrument"])
    if pr.get("config_overrides"):
        cfg = replace(cfg, **pr["config_overrides"])
    start = datetime.fromisoformat(pr["period"]["start"]).replace(tzinfo=UTC)
    end = datetime.fromisoformat(pr["period"]["end"]).replace(tzinfo=UTC)
    trades = simulate(load_m1(csv, start, end), cfg, pr["setups"], pr.get("variant"), start, end)
    v = verdict(pr, trades)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    prov = provenance(csv, cfg)
    (out / f"{pr['name']}.json").write_text(json.dumps({"prereg": pr, "registered": reg, "verdict": v, "provenance": prov,
                                                        "contamination": contamination(pr), "trades": trades}, indent=1))
    with (registry.parent / "replay-runs.jsonl").open("a", encoding="utf-8") as f:   # every run is logged, pass or fail
        f.write(json.dumps({"name": pr["name"], "hash": reg["hash"], "ran_at": datetime.now(UTC).isoformat(),
                            "passes_filter": v["passes_filter"], "n": v["all"]["n"], "mean": v["all"].get("mean"), **prov}) + chr(10))
    (out / f"{pr['name']}.md").write_text(report(pr, reg, v), encoding="utf-8")
    return v


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[0] == "register":
        print(f"registered {register(argv[1])}")
        return 0
    if len(argv) >= 4 and argv[0] == "run":
        v = run(argv[1], argv[2], argv[3])
        print(json.dumps({"passes_filter": v["passes_filter"], "all": v["all"]}))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
