"""Built-in paper account on free daily prices: no broker, no signup.

Same interface as ``OandaPractice``, so the runner does not care which one it
drives. To avoid trading on a price the decision already saw, orders are not
filled at once: a new order or a close is queued and filled at the **next
daily open** after the order date, plus half the configured spread. Stops are
checked against each later day's high/low; a gap through the stop fills at
that day's open. P&L is in the instrument's own currency (USD for US stocks,
crypto -USD pairs and xxxUSD forex). Shorts pay a borrow fee (borrow_bps_per_year) for the days held; longs pay no financing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

from .oanda import InstrumentSpec, Quote

# (symbol, as_of_date) -> DataFrame with Date, Open, High, Low, Close
Bars = Callable[[str, str], pd.DataFrame]


def yahoo_bars(symbol: str, as_of_date: str) -> pd.DataFrame:
    from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

    return load_ohlcv(symbol, as_of_date, fill_gaps=False)


class PaperBroker:
    def __init__(self, path: str | Path, start_cash: float = 100_000.0, spread_bps: float = 5.0,
                 bars: Bars = yahoo_bars, today: Callable[[], date] = date.today,
                 borrow_bps_per_year: float = 0.0):
        self.path = Path(path)
        self.spread_bps = spread_bps
        self.borrow_bps_per_year = borrow_bps_per_year
        self.bars = bars
        self.today = today
        self.account_id = "paper"
        if self.path.exists():
            self.s = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.s = {"cash": start_cash, "next_id": 1, "open": [], "closed": []}

    # -- persistence -----------------------------------------------------
    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.s, indent=1), encoding="utf-8")

    def _frame(self, symbol: str) -> pd.DataFrame:
        df = self.bars(symbol, self.today().isoformat()).dropna(subset=["Open", "High", "Low", "Close"])
        if df.empty:
            raise ValueError(f"no daily prices for {symbol}")
        return df.reset_index(drop=True)

    def _half(self, price: float) -> float:
        return price * self.spread_bps / 2e4

    # -- the OandaPractice interface --------------------------------------
    def instrument(self, name: str) -> InstrumentSpec:
        return InstrumentSpec(name, display_precision=6, units_precision=4, min_units=0.0001)

    def quote(self, name: str) -> Quote:
        c = float(self._frame(name)["Close"].iloc[-1])
        h = self._half(c)
        return Quote(bid=c - h, ask=c + h, tradeable=True, quote_to_home=1.0)

    def daily_candles(self, name: str, count: int = 40) -> list[dict]:
        df = self._frame(name).tail(count)
        return [{"o": float(r.Open), "h": float(r.High), "l": float(r.Low), "c": float(r.Close)}
                for r in df.itertuples()]

    def nav(self) -> float:
        value = self.s["cash"]
        for t in self.s["open"]:
            if t["status"] != "pending":
                value += t["units"] * (float(self._frame(t["instrument"])["Close"].iloc[-1]) - t["price"])
        return value

    def summary(self) -> dict:
        return {"NAV": round(self.nav(), 2), "currency": "USD (paper)",
                "openTradeCount": len(self.s["open"])}

    def our_open_trades(self, name: str | None = None) -> list[dict]:
        return [{"id": t["id"], "instrument": t["instrument"], "currentUnits": str(t["units"]),
                 "status": t["status"], "opened": t.get("filled_on") or t["ordered_on"]}
                for t in self.s["open"]
                if t["status"] != "closing" and (name is None or t["instrument"] == name)]

    def our_closed_trades(self, count: int = 500) -> list[dict]:
        return self.s["closed"][-count:]

    def market_order(self, name: str, units: float, stop_price: str, comment: str) -> dict:
        ref = float(self._frame(name)["Close"].iloc[-1])
        tid = str(self.s["next_id"])
        self.s["next_id"] += 1
        self.s["open"].append({"id": tid, "instrument": name, "units": units, "status": "pending",
                               "ordered_on": self.today().isoformat(), "stop_distance": abs(ref - float(stop_price)),
                               "comment": comment})
        self._save()
        return {"price": None, "tradeOpened": {"tradeID": tid}, "halfSpreadCost": None,
                "note": "fills at the next daily open"}

    def close_trade(self, trade_id: str) -> dict:
        t = self._find(trade_id)
        if t["status"] == "pending":
            self.s["open"].remove(t)
            self._save()
            return {"price": None, "pl": "0", "note": "pending order cancelled"}
        t["status"], t["close_ordered_on"] = "closing", self.today().isoformat()
        self._save()
        return {"price": None, "pl": None, "note": "closes at the next daily open"}

    # -- paper-only: settle queued orders and stops up to today -----------
    def sync(self) -> list[dict]:
        events = []
        for t in list(self.s["open"]):
            df = self._frame(t["instrument"])
            dates = pd.to_datetime(df["Date"]).dt.date.astype(str)
            if t["status"] == "pending":
                later = df[dates > t["ordered_on"]]
                if later.empty:
                    continue
                bar = later.iloc[0]
                side = 1 if t["units"] > 0 else -1
                t["price"] = float(bar.Open) + side * self._half(float(bar.Open))
                t["stop"] = t["price"] - side * t["stop_distance"]
                t["filled_on"], t["status"] = str(dates[later.index[0]]), "open"
                events.append({"event": "paper_fill", "trade_id": t["id"], "price": t["price"]})
            if t["status"] == "closing":
                later = df[dates > t["close_ordered_on"]]
                if not later.empty:
                    events.append(self._close(t, float(later.iloc[0].Open), str(dates[later.index[0]]), "rating"))
                continue
            # stops: every bar from the fill day on
            for i in df.index[dates >= t["filled_on"]]:
                bar, side = df.loc[i], (1 if t["units"] > 0 else -1)
                hit = bar.Low <= t["stop"] if side > 0 else bar.High >= t["stop"]
                if hit:
                    gap = bar.Open < t["stop"] if side > 0 else bar.Open > t["stop"]
                    exit_px = float(bar.Open) if gap else t["stop"]
                    events.append(self._close(t, exit_px, str(dates[i]), "stop"))
                    break
        self._save()
        return events

    def _close(self, t: dict, mid: float, on: str, reason: str) -> dict:
        side = 1 if t["units"] > 0 else -1
        px = mid - side * self._half(mid)
        pl = t["units"] * (px - t["price"])
        # Short borrow fee on the entry value for the calendar days held; longs pay none.
        days = max((date.fromisoformat(on) - date.fromisoformat(t["filled_on"])).days, 0)
        borrow = (abs(t["units"]) * t["price"] * self.borrow_bps_per_year / 1e4 * days / 365
                  if t["units"] < 0 else 0.0)
        self.s["cash"] += pl - borrow
        self.s["open"].remove(t)
        rec = {"id": t["id"], "instrument": t["instrument"], "units": t["units"], "price": t["price"],
               "close_price": px, "realizedPL": round(pl, 2), "financing": round(-borrow, 2), "opened": t["filled_on"],
               "closed": on, "reason": reason,
               "spread_cost": round(abs(t["units"]) * (self._half(t["price"]) + self._half(mid)), 2),
               "recorded": datetime.now(UTC).isoformat(timespec="seconds")}
        self.s["closed"].append(rec)
        return {"event": "paper_close", **rec}

    def _find(self, trade_id: str) -> dict:
        for t in self.s["open"]:
            if t["id"] == str(trade_id):
                return t
        raise KeyError(f"no open paper trade {trade_id}")
