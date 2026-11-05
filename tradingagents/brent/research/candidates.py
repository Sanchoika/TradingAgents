"""Research backtests for the two frozen Brent chart candidates (prereg-22, prereg-23).

Rules live in docs/research/prereg-22-*.md and prereg-23-*.md; this file implements them and nothing else.
Run once on the PC against OANDA BCO_USD M1 bid/ask:
    python -m tradingagents.brent.research.candidates BCO_USD_M1.csv OUT_DIR
Costs are the prereg-21 model. Signals use completed bars only; entries are at the NEXT bar's open.
"""

from __future__ import annotations

import json
import sys
from datetime import date, time
from pathlib import Path

import numpy as np
import pandas as pd

NY = "America/New_York"
HOLDOUT_START = date(2021, 1, 1)
FIN_RATE, FX_WIN, FX_LOSS = 0.45, 0.99, 1.01
MIN_R_SPREADS, SPREAD_MULT, MIN_TRADES = 4.0, 3.0, 200


def load_m1(path: str) -> pd.DataFrame:
    d = pd.read_csv(path)
    d.index = pd.DatetimeIndex(pd.to_datetime(d.pop("time"), unit="s", utc=True)).tz_convert(NY)
    return prepare(d)


def prepare(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    for f in "ohlc":
        d[f"mid_{f}"] = (d[f"bid_{f}"] + d[f"ask_{f}"]) / 2
    d["hs_o"] = (d["ask_o"] - d["bid_o"]) / 2
    d["hs_h"] = (d["ask_h"] - d["bid_h"]) / 2
    d["hs_l"] = (d["ask_l"] - d["bid_l"]) / 2
    d["sp_c"] = d["ask_c"] - d["bid_c"]
    return d


def fill_trade(d: pd.DataFrame, ie: int, s: int, stop: float, t_exit: pd.Timestamp, k: float = 1.0,
               r_override: float | None = None):
    """Enter at the open of bar ``ie`` (side s), stop at ``stop`` or time exit. Returns dict or (None, why)."""
    idx = d.index
    mo, mh, ml = d.mid_o.to_numpy(), d.mid_h.to_numpy(), d.mid_l.to_numpy()
    hso, hsh, hsl = d.hs_o.to_numpy(), d.hs_h.to_numpy(), d.hs_l.to_numpy()
    entry1 = mo[ie] + s * 3 * hso[ie]
    r1 = s * (entry1 - stop) if r_override is None else r_override
    if r_override is None and r1 < MIN_R_SPREADS * 2 * hso[ie]:
        return None, "stop too close"
    if r_override is not None:
        stop = entry1 - s * r1
    entry = mo[ie] + s * k * 3 * hso[ie]
    r = s * (entry - stop)
    if r <= 0:
        return None, "stop beyond entry"
    ix = idx.searchsorted(t_exit)
    if ix >= len(idx) or idx[ix] - t_exit > pd.Timedelta(minutes=10) or ix <= ie:
        return None, "no exit bar"
    px, how, j = None, "time", ix
    for j in range(ie, ix):
        x_open = mo[j] - s * k * hso[j]
        worst = (ml[j] - k * hsl[j]) if s > 0 else (mh[j] + k * hsh[j])
        if j > ie and s * (x_open - stop) <= 0:
            px, how = x_open - s * k * 2 * hso[j], "gap"
            break
        if s * (worst - stop) <= 0:
            px, how = stop - s * k * 2 * (hsl[j] if s > 0 else hsh[j]), "stop"
            break
    if px is None:
        j = ix
        px = mo[ix] - s * k * hso[ix]
    held = (idx[j] - idx[ie]).total_seconds() / 60
    fin = k * FIN_RATE * mo[ie] * held / 525_600
    R = (s * (px - entry) - fin) / r
    if k > 0:
        R = R * FX_WIN if R > 0 else R * FX_LOSS
    return {"date": idx[ie].date(), "side": s, "r": R, "exit": how, "r_dist": r1, "entry_ts": idx[ie],
            "spread_r": 2 * hso[ie] / r1, "held_min": held, "ie": ie, "stop": stop}, None


# ----------------------------------------------------------------- prereg-22: London range break + retest
def london_signal(d: pd.DataFrame, day: date):
    """(ie, side, stop, t_exit) or (None, why) for one NY date. Completed bars only."""
    idx = d.index
    ts = lambda hh, mm: pd.Timestamp(day.isoformat() + f" {hh:02d}:{mm:02d}", tz=NY)  # noqa: E731
    lo, hi = idx.searchsorted(ts(3, 0)), idx.searchsorted(ts(4, 0))
    if hi - lo != 60 or idx[lo] != ts(3, 0) or idx[hi - 1] != ts(3, 59):
        return None, "range window incomplete"
    mh, ml, mc = d.mid_h.to_numpy(), d.mid_l.to_numpy(), d.mid_c.to_numpy()
    H, L = mh[lo:hi].max(), ml[lo:hi].min()
    sp = np.median(d.sp_c.to_numpy()[lo:hi])
    if MIN_R_SPREADS * sp > H - L:
        return None, "range too narrow"
    i, end_break = hi, idx.searchsorted(ts(7, 0))
    side = 0
    while i < min(end_break, len(idx)):
        if mc[i] > H:
            side = 1
            break
        if mc[i] < L:
            side = -1
            break
        i += 1
    if not side:
        return None, "no break"
    level = H if side > 0 else L
    j, end_rt = i + 1, min(idx.searchsorted(idx[i] + pd.Timedelta(minutes=60), side="right"), len(idx))
    ir = None
    while j < end_rt:
        inside = (mc[j] <= H) if side > 0 else (mc[j] >= L)
        if inside:
            return None, "closed back inside before retest"
        touched = (ml[j] <= level) if side > 0 else (mh[j] >= level)
        if touched:
            ir = j
            break
        j += 1
    if ir is None:
        return None, "no retest"
    ie = ir + 1
    if ie >= len(idx) or idx[ie] > ts(7, 30):
        return None, "entry too late"
    if 2 * d.hs_o.to_numpy()[ie] > SPREAD_MULT * sp:
        return None, "entry spread too wide"
    return (ie, side, (H + L) / 2, ts(8, 25)), None


# ----------------------------------------------------------------- prereg-23: HTF-trend pullback
def _ema(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(span=n, adjust=False).mean()


def trend_signals(d: pd.DataFrame) -> list[tuple]:
    """All (ie, side, stop, t_exit, day) per prereg-23 with completed bars only; the day-level
    caps (2 per day, none while open) are applied by the runner."""
    m5 = d[["mid_o", "mid_h", "mid_l", "mid_c"]].resample("5min", label="left", closed="left").agg(
        {"mid_o": "first", "mid_h": "max", "mid_l": "min", "mid_c": "last"}).dropna()
    h1 = m5.resample("1h", label="left", closed="left").agg(
        {"mid_o": "first", "mid_h": "max", "mid_l": "min", "mid_c": "last"}).dropna()
    e50 = _ema(h1.mid_c, 50)
    trend = pd.Series(0, index=h1.index)
    trend[(h1.mid_c > e50) & (e50 > e50.shift(6))] = 1
    trend[(h1.mid_c < e50) & (e50 < e50.shift(6))] = -1
    # an H1 bar's trend is usable only after it completes: from the next hour's start
    usable = trend.shift(1, freq="1h")
    e20 = _ema(m5.mid_c, 20)
    tr = pd.concat([m5.mid_h - m5.mid_l, (m5.mid_h - m5.mid_c.shift()).abs(),
                    (m5.mid_l - m5.mid_c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(20).mean()
    ext_up = ((m5.mid_c - e20) >= atr).astype(int).rolling(12).max().shift(1)
    ext_dn = ((e20 - m5.mid_c) >= atr).astype(int).rolling(12).max().shift(1)
    mdx = d.index
    out = []
    tdir = usable.reindex(m5.index, method="ffill").fillna(0).to_numpy()
    lo, hi, c5 = m5.mid_l.to_numpy(), m5.mid_h.to_numpy(), m5.mid_c.to_numpy()
    e = e20.to_numpy()
    eu, ed = ext_up.fillna(0).to_numpy(), ext_dn.fillna(0).to_numpy()
    long_t = (tdir > 0) & (lo <= e) & (eu == 1)
    short_t = (tdir < 0) & (hi >= e) & (ed == 1)
    cand = np.flatnonzero(long_t | short_t)
    sp_all = d.sp_c.to_numpy()
    hso = d.hs_o.to_numpy()
    for p in cand:
        if p + 1 >= len(m5):
            continue
        s = 1 if long_t[p] else -1
        c = p + 1                                           # confirmation bar
        ok = (c5[c] > hi[p]) if s > 0 else (c5[c] < lo[p])
        if not ok:
            continue
        t_entry = m5.index[c] + pd.Timedelta(minutes=5)     # first M1 bar after the confirmation completes
        ie = mdx.searchsorted(t_entry)
        if ie >= len(mdx) or mdx[ie] != t_entry or not (time(3, 0) <= t_entry.time() < time(12, 0)):
            continue
        if 2 * hso[ie] > SPREAD_MULT * np.median(sp_all[max(0, ie - 60):ie]):
            continue
        it = mdx.searchsorted(m5.index[p])
        spread_touch = sp_all[it:it + 5].max()
        stop = float(lo[p] - spread_touch) if s > 0 else float(hi[p] + spread_touch)
        day = t_entry.date()
        t_exit = min(t_entry + pd.Timedelta(minutes=120), pd.Timestamp(day.isoformat() + " 12:30", tz=NY))
        out.append((ie, s, stop, t_exit, day))
    return out


def run_london(d: pd.DataFrame, days: list[date], k: float = 1.0):
    trades, skips = [], {}
    for day in days:
        sig, why = london_signal(d, day)
        if sig is None:
            skips[why] = skips.get(why, 0) + 1
            continue
        ie, s, stop, t_exit = sig
        tr, why = fill_trade(d, ie, s, stop, t_exit, k)
        if tr is None:
            skips[why] = skips.get(why, 0) + 1
        else:
            trades.append(tr)
    return pd.DataFrame(trades), skips


def run_trend(d: pd.DataFrame, days: set[date] | None = None, k: float = 1.0):
    trades, skips, busy_until, per_day = [], {}, {}, {}
    for ie, s, stop, t_exit, day in trend_signals(d):
        if days is not None and day not in days:
            continue
        if per_day.get(day, 0) >= 2 or d.index[ie] < busy_until.get(day, d.index[0]):
            skips["cap or busy"] = skips.get("cap or busy", 0) + 1
            continue
        tr, why = fill_trade(d, ie, s, stop, t_exit, k)
        if tr is None:
            skips[why] = skips.get(why, 0) + 1
            continue
        per_day[day] = per_day.get(day, 0) + 1
        busy_until[day] = d.index[min(ie + int(tr["held_min"]) + 1, len(d) - 1)]
        trades.append(tr)
    return pd.DataFrame(trades), skips


# ----------------------------------------------------------------------------- evaluation
def block_boot_ci(x: np.ndarray, block: int = 20, n: int = 10_000, alpha: float = 0.025, seed: int = 22):
    if len(x) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(len(x) / block))
    n = min(n, 2000)
    starts = rng.integers(0, max(1, len(x) - block + 1), size=(n, nb))
    means = np.array([np.concatenate([x[s:s + block] for s in row])[:len(x)].mean() for row in starts])
    return (float(np.quantile(means, alpha)), float(np.quantile(means, 1 - alpha)))


def verdict(base: pd.DataFrame, stress: pd.DataFrame, rand_p95: float | None, always_long: float | None):
    """Six criteria from the preregistration. Returns (verdict, criteria dict)."""
    if len(base) < MIN_TRADES:
        return "INCONCLUSIVE", {"6_min_trades": False, "n": len(base)}
    daily = base.groupby("date").r.sum().sort_index().to_numpy()
    lo, _ = block_boot_ci(daily)
    gains, losses = base.r[base.r > 0].sum(), -base.r[base.r < 0].sum()
    thirds = np.array_split(base.sort_values("date").r.to_numpy(), 3)
    top5 = np.sort(base.r.to_numpy())[:-5].sum()
    eq = np.cumsum(daily)
    dd = float((np.maximum.accumulate(eq) - eq).max())
    c = {"1_mean_ci_above_0": bool(daily.mean() > 0 and lo > 0),
         "2_profit_factor": bool(losses > 0 and gains / losses >= 1.15),
         "3_positive_at_2x_costs": bool(len(stress) and stress.r.mean() > 0),
         "4_thirds_and_without_best5": bool(sum(t.sum() > 0 for t in thirds) >= 2 and top5 > 0),
         "5_beats_random_and_always_long": bool(rand_p95 is not None and always_long is not None
                                                and base.r.mean() > rand_p95 and base.r.mean() > always_long),
         "6_trades_and_drawdown": bool(len(base) >= MIN_TRADES and dd <= 15)}
    return ("PASS" if all(c.values()) else "FAIL"), c


def summarize(tr: pd.DataFrame) -> dict:
    if not len(tr):
        return {"n": 0}
    return {"n": len(tr), "mean_r": round(float(tr.r.mean()), 4), "win_rate": round(float((tr.r > 0).mean()), 3),
            "long_share": round(float((tr.side > 0).mean()), 3), "exit_mix": tr.exit.value_counts().to_dict(),
            "median_spread_r": round(float(tr.spread_r.median()), 3)}


def evaluate(d: pd.DataFrame) -> dict:
    """One pass over development (code check) and the holdout for both candidates, per the frozen texts."""
    days = sorted({x for x in d.index.date if x.weekday() < 5})
    hold = [x for x in days if x >= HOLDOUT_START]
    dev = [x for x in days if x < HOLDOUT_START]
    out = {}
    # candidate 22
    base, sk = run_london(d, hold)
    stress, _ = run_london(d, hold, k=2.0)
    gross, _ = run_london(d, hold, k=0.0)
    devt, _ = run_london(d, dev)
    rp, al = _controls(d, base, "london")
    v, crit = verdict(base, stress, rp, al)
    out["prereg-22"] = {"verdict": v, "criteria": crit, "holdout": summarize(base), "gross": summarize(gross),
                        "stress": summarize(stress), "development": summarize(devt), "skips": sk}
    # candidate 23
    hset, dset = set(hold), set(dev)
    base, sk = run_trend(d, hset)
    stress, _ = run_trend(d, hset, k=2.0)
    gross, _ = run_trend(d, hset, k=0.0)
    devt, _ = run_trend(d, dset)
    rp, al = _controls(d, base, "trend")
    v, crit = verdict(base, stress, rp, al)
    out["prereg-23"] = {"verdict": v, "criteria": crit, "holdout": summarize(base), "gross": summarize(gross),
                        "stress": summarize(stress), "development": summarize(devt), "skips": sk}
    return out


def _controls(d: pd.DataFrame, base: pd.DataFrame, kind: str):
    """Random-direction p95 and always-long mean on the same entry bars, same stop distance and exit."""
    if not len(base):
        return None, None
    both = []
    for tr in base.itertuples():
        row = {}
        for s in (1, -1):
            t_exit = d.index[min(tr.ie + int(tr.held_min), len(d) - 1)]
            x, _ = fill_trade(d, tr.ie, s, tr.stop, t_exit, 1.0, r_override=tr.r_dist)
            row[s] = x["r"] if x else np.nan
        both.append(row)
    t = pd.DataFrame(both)
    rng = np.random.default_rng(23)
    runs = [np.nanmean(np.where(rng.random(len(t)) < 0.5, t[1], t[-1])) for _ in range(1000)]
    return float(np.quantile(runs, 0.95)), float(np.nanmean(t[1]))


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    d = load_m1(argv[0])
    res = evaluate(d)
    out = Path(argv[1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "candidates-results.json").write_text(json.dumps(res, indent=2, default=str))
    print(json.dumps({k: v["verdict"] for k, v in res.items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
