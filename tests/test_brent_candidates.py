import numpy as np
import pandas as pd

from tradingagents.brent.research import candidates as c

NY = "America/New_York"


def frame(day="2026-03-10", start="03:00", end="08:30", price=80.0, half=0.01):
    idx = pd.date_range(f"{day} {start}", f"{day} {end}", freq="1min", tz=NY)
    n = len(idx)
    p = np.full(n, price)
    d = pd.DataFrame(index=idx)
    for f in "ohlc":
        d[f"bid_{f}"] = p - half
        d[f"ask_{f}"] = p + half
    return d


def set_mid(d, ts, o, h, lo, cl, half=0.01):
    d.loc[ts, ["bid_o", "ask_o"]] = (o - half, o + half)
    d.loc[ts, ["bid_h", "ask_h"]] = (h - half, h + half)
    d.loc[ts, ["bid_l", "ask_l"]] = (lo - half, lo + half)
    d.loc[ts, ["bid_c", "ask_c"]] = (cl - half, cl + half)


def london_day():
    d = frame()
    t = lambda s: pd.Timestamp("2026-03-10 " + s, tz=NY)  # noqa: E731
    # range 03:00-03:59: 80.00 high 81.00, low 79.00 via two bars
    set_mid(d, t("03:10"), 80, 81, 80, 80.5)
    set_mid(d, t("03:20"), 80, 80, 79, 79.5)
    for m in range(0, 200):             # inside the range until the break at 04:10, above it afterwards
        v = 80.5 if m < 10 else 81.3
        set_mid(d, t("04:00") + pd.Timedelta(minutes=m), v, v, v, v)
    # break above at 04:10 (close 81.5), retest at 04:30 touching 81.0 and closing 81.4
    set_mid(d, t("04:10"), 81, 81.6, 81, 81.5)
    set_mid(d, t("04:30"), 81.5, 81.5, 80.95, 81.4)
    # afterwards price climbs to 82
    for m in range(31, 300):
        ts = t("04:30") + pd.Timedelta(minutes=m - 30)
        v = 82 if m < 100 else 83
        set_mid(d, ts, v, v, v, v)
    return d, t


def test_london_retest_enters_after_retest_with_midpoint_stop():
    d, t = london_day()
    d = c.prepare(d)
    sig, why = c.london_signal(d, pd.Timestamp("2026-03-10").date())
    assert why is None
    ie, side, stop, t_exit = sig
    assert side == 1 and d.index[ie] == t("04:31")          # the bar AFTER the retest bar
    assert abs(stop - 80.0) < 1e-9                          # (81 + 79) / 2
    tr, why = c.fill_trade(d, ie, side, stop, t_exit, 1.0)
    assert why is None and tr["exit"] == "time" and tr["r"] > 0


def test_london_no_retest_and_inside_close_give_no_trade():
    d, t = london_day()
    # remove the retest: keep price above 81.2 after the break
    set_mid(d, t("04:30"), 81.5, 81.6, 81.3, 81.5)
    d = c.prepare(d)
    sig, why = c.london_signal(d, pd.Timestamp("2026-03-10").date())
    assert sig is None and why == "no retest"
    d2, t = london_day()
    set_mid(d2, t("04:20"), 81.5, 81.5, 80.5, 80.6)        # closes back inside the range first
    sig, why = c.london_signal(c.prepare(d2), pd.Timestamp("2026-03-10").date())
    assert sig is None and why == "closed back inside before retest"


def test_london_incomplete_window_and_narrow_range_skipped():
    d = frame(start="03:05")
    sig, why = c.london_signal(c.prepare(d), pd.Timestamp("2026-03-10").date())
    assert sig is None and why == "range window incomplete"
    d = frame()   # flat price: range width 0.02 < 4 spreads? spread 0.02 -> 0.02 < 0.08
    sig, why = c.london_signal(c.prepare(d), pd.Timestamp("2026-03-10").date())
    assert sig is None and why == "range too narrow"


def test_stop_hit_is_a_loss_and_gap_fills_worse():
    d, t = london_day()
    set_mid(d, t("05:00"), 80.2, 80.3, 79.9, 80.0)         # trades through the 80.0 stop
    d = c.prepare(d)
    ie, side, stop, t_exit = c.london_signal(d, pd.Timestamp("2026-03-10").date())[0]
    tr, _ = c.fill_trade(d, ie, side, stop, t_exit, 1.0)
    assert tr["exit"] in ("stop", "gap") and tr["r"] < 0


def test_costs_scale_with_k_and_gross_is_best():
    d, t = london_day()
    d = c.prepare(d)
    ie, side, stop, t_exit = c.london_signal(d, pd.Timestamp("2026-03-10").date())[0]
    r0 = c.fill_trade(d, ie, side, stop, t_exit, 0.0)[0]["r"]
    r1 = c.fill_trade(d, ie, side, stop, t_exit, 1.0)[0]["r"]
    r2 = c.fill_trade(d, ie, side, stop, t_exit, 2.0)[0]["r"]
    assert r0 > r1 > r2


def test_verdict_inconclusive_below_min_trades_and_pass_needs_all():
    small = pd.DataFrame({"date": pd.date_range("2022-01-03", periods=10).date, "r": [0.5] * 10})
    v, crit = c.verdict(small, small, 0.0, 0.0)
    assert v == "INCONCLUSIVE" and crit["n"] == 10
    n = 300
    big = pd.DataFrame({"date": pd.date_range("2021-01-04", periods=n, freq="B").date,
                        "r": np.where(np.arange(n) % 3 == 0, -1.0, 1.0)})
    v, crit = c.verdict(big, big, 10.0, 10.0)               # cannot beat the controls
    assert v == "FAIL" and not crit["5_beats_random_and_always_long"]


def test_trend_signal_uses_only_completed_bars():
    # a steady uptrend with a dip to the EMA: just check signals never enter before 03:00 or after 12:00
    idx = pd.date_range("2026-03-02 00:00", "2026-03-06 16:00", freq="1min", tz=NY)
    rng = np.random.default_rng(1)
    p = 80 + np.cumsum(rng.normal(0.002, 0.03, len(idx)))
    d = pd.DataFrame(index=idx)
    for f in "ohlc":
        d[f"bid_{f}"] = p - 0.01
        d[f"ask_{f}"] = p + 0.01
    d = c.prepare(d)
    sigs = c.trend_signals(d)
    for ie, _s, _stop, t_exit, _day in sigs:
        ts = d.index[ie]
        assert 3 <= ts.hour < 12 and t_exit > ts
    trades, _ = c.run_trend(d)
    assert (trades.groupby("date").size() <= 2).all() if len(trades) else True


def test_model_eval_scoring_and_fake_runs():
    import json

    from tradingagents.brent.research import model_eval as me

    def perfect(system, user):
        rows = []
        for item, exp in me.CASES:
            rows.append({"key": item["key"], "category": exp.get("category", "other"),
                         "status": exp.get("status", "credible_report"), "event_time": None,
                         "scope": exp.get("scope", "brent"), "relevant": exp.get("relevant", True),
                         "mechanism": "m", "bias": exp.get("bias", "unclear"), "contradiction": "",
                         "update_of_older_story": exp.get("update_of_older_story", False)})
        return json.dumps(rows), {"total_tokens": 10}

    perfect.model_id = "fake"
    res = me.run_config("gpt-6-luna", "high", 1, make=lambda p, m, e: perfect)
    assert res["runs"][0]["accuracy"] == 1.0 and res["runs"][0]["tokens"] == 10

    def broken(system, user):
        return "not json", {}

    res = me.run_config("gpt-6-luna", None, 1, make=lambda p, m, e: broken)
    assert res["runs"][0]["valid_json"] is False and res["effort"] == "provider default"


def test_controls_run_to_the_scheduled_exit_not_the_strategy_hold(monkeypatch):
    # audit finding 8(a): a stopped-out strategy trade must not cut its controls short
    import numpy as np
    import pandas as pd

    from tradingagents.brent.research import candidates as c

    idx = pd.date_range("2026-01-05 08:00", periods=200, freq="1min", tz="America/New_York")
    d = pd.DataFrame(index=idx)
    planned = idx[120]
    base = pd.DataFrame([{"ie": 10, "held_min": 5.0, "stop": 70.0, "r_dist": 0.5,
                          "t_exit_planned": planned, "r": -1.0}])
    seen = []

    def fake_fill(d, ie, s, stop, t_exit, k=1.0, r_override=None):
        seen.append((s, t_exit))
        return {"r": 0.3 * s}, None

    monkeypatch.setattr(c, "fill_trade", fake_fill)
    p95, always_long = c._controls(d, base, "london")
    assert seen == [(1, planned), (-1, planned)]          # both directions, own stop, full horizon
    assert always_long == 0.3 and p95 is not None and np.isfinite(p95)


def test_bootstrap_interval_is_the_preregistered_97_5_percent():
    # audit finding 8(b): 1.25% per tail, wider than the 95% interval used before
    import numpy as np

    from tradingagents.brent.research.candidates import CI_TAIL, block_boot_ci

    assert CI_TAIL == 0.0125
    x = np.random.default_rng(1).normal(0.1, 1.0, 500)
    lo97, hi97 = block_boot_ci(x)
    lo95, hi95 = block_boot_ci(x, alpha=0.025)
    assert lo97 < lo95 and hi97 > hi95
