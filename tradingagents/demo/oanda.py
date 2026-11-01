"""Minimal OANDA v20 REST client, locked to the practice (demo) endpoint.

Only the handful of calls the demo runner needs. The base URL is a constant and
cannot be configured, so this code cannot place orders on a live account.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import requests

PRACTICE_URL = "https://api-fxpractice.oanda.com/v3"
TAG = "tradingagents"


class OandaError(RuntimeError):
    """OANDA refused a request or answered with something unexpected."""


@dataclass(frozen=True)
class InstrumentSpec:
    name: str
    display_precision: int
    units_precision: int
    min_units: float


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    tradeable: bool
    # Account currency per unit of the instrument's quote currency.
    quote_to_home: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


def credentials_from_env(env=os.environ) -> tuple[str, str]:
    token = (env.get("OANDA_API_KEY") or "").strip()
    account = (env.get("OANDA_ACCOUNT_ID") or "").strip()
    missing = [n for n, v in (("OANDA_API_KEY", token), ("OANDA_ACCOUNT_ID", account)) if not v]
    if missing:
        raise OandaError(f"set {' and '.join(missing)} in your .env file (practice account only)")
    return token, account


class OandaPractice:
    def __init__(self, token: str, account_id: str, session: requests.Session | None = None,
                 timeout: float = 20.0):
        self.account_id = account_id
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept-Datetime-Format": "RFC3339",
        })

    # -- plumbing ---------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{PRACTICE_URL}{path}"
        resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            msg = body.get("errorMessage") or resp.text[:300]
            raise OandaError(f"{method} {path} -> HTTP {resp.status_code}: {msg}")
        return body

    def _acct(self, suffix: str) -> str:
        return f"/accounts/{self.account_id}{suffix}"

    # -- reads ------------------------------------------------------------
    def summary(self) -> dict:
        return self._request("GET", self._acct("/summary"))["account"]

    def nav(self) -> float:
        return float(self.summary()["NAV"])

    def instrument(self, name: str) -> InstrumentSpec:
        body = self._request("GET", self._acct("/instruments"), params={"instruments": name})
        items = body.get("instruments") or []
        if not items:
            raise OandaError(f"{name} is not tradeable on this account")
        i = items[0]
        return InstrumentSpec(
            name=name,
            display_precision=int(i["displayPrecision"]),
            units_precision=int(i.get("tradeUnitsPrecision", 0)),
            min_units=float(i.get("minimumTradeSize", 1)),
        )

    def quote(self, name: str) -> Quote:
        body = self._request("GET", self._acct("/pricing"),
                             params={"instruments": name, "includeHomeConversions": "true"})
        prices = body.get("prices") or []
        if not prices:
            raise OandaError(f"no price for {name}")
        p = prices[0]
        bid, ask = float(p["bids"][0]["price"]), float(p["asks"][0]["price"])
        return Quote(bid=bid, ask=ask, tradeable=bool(p.get("tradeable", True)),
                     quote_to_home=_quote_to_home(name, p, body))

    def daily_candles(self, name: str, count: int = 40) -> list[dict]:
        """Completed daily mid candles as dicts of floats (o, h, l, c)."""
        body = self._request("GET", f"/instruments/{name}/candles",
                             params={"granularity": "D", "count": count, "price": "M"})
        return [
            {k: float(c["mid"][k]) for k in ("o", "h", "l", "c")}
            for c in body.get("candles", []) if c.get("complete")
        ]

    def our_open_trades(self, name: str | None = None) -> list[dict]:
        trades = self._request("GET", self._acct("/openTrades")).get("trades", [])
        return [t for t in trades if _is_ours(t) and (name is None or t["instrument"] == name)]

    def our_closed_trades(self, count: int = 500) -> list[dict]:
        trades = self._request("GET", self._acct("/trades"),
                               params={"state": "CLOSED", "count": count}).get("trades", [])
        return [t for t in trades if _is_ours(t)]

    # -- writes -----------------------------------------------------------
    def market_order(self, name: str, units: float, stop_price: str, comment: str) -> dict:
        ext = {"tag": TAG, "comment": comment[:120]}
        order = {
            "type": "MARKET",
            "instrument": name,
            "units": _fmt_units(units),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": stop_price, "timeInForce": "GTC"},
            "clientExtensions": ext,
            "tradeClientExtensions": ext,
        }
        body = self._request("POST", self._acct("/orders"), json={"order": order})
        if "orderCancelTransaction" in body:
            raise OandaError(f"order cancelled: {body['orderCancelTransaction'].get('reason')}")
        fill = body.get("orderFillTransaction")
        if not fill:
            raise OandaError(f"order not filled: {sorted(body)}")
        return fill

    def close_trade(self, trade_id: str) -> dict:
        body = self._request("PUT", self._acct(f"/trades/{trade_id}/close"))
        fill = body.get("orderFillTransaction")
        if not fill:
            raise OandaError(f"close of trade {trade_id} not filled: {sorted(body)}")
        return fill


def _is_ours(trade: dict) -> bool:
    return (trade.get("clientExtensions") or {}).get("tag") == TAG


def _fmt_units(units: float) -> str:
    return str(int(units)) if float(units).is_integer() else repr(float(units))


def _quote_to_home(name: str, price: dict, body: dict) -> float:
    factors = price.get("quoteHomeConversionFactors")
    if factors:
        return float(factors["positiveUnits"])
    quote_ccy = name.split("_")[-1]
    for conv in body.get("homeConversions") or []:
        if conv.get("currency") == quote_ccy:
            return float(conv.get("positionValue") or conv["accountGain"])
    raise OandaError(f"no home-currency conversion for {name}")
