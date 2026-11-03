"""Read-only OANDA v20 client for the Brent alerts module.

It has GET requests only: no method here can place, change or close an order. The host is
the practice endpoint shared with the demo runner, so it cannot reach a live account.
"""

from __future__ import annotations

import getpass
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import requests

from tradingagents.demo.oanda import PRACTICE_URL


class OandaReadError(RuntimeError):
    """OANDA refused a read or answered with something unexpected."""


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    time: datetime          # OANDA's timestamp of this price
    fetched_at: datetime    # local clock when the answer arrived
    tradeable: bool
    quote_to_home: float | None = None  # account currency per USD (None if not given)
    rtt: float = 0.0        # request round trip, seconds

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def age(self, now: datetime) -> float:
        return (now - self.time).total_seconds()


def parse_time(text: str) -> datetime:
    """OANDA RFC3339 with nanoseconds -> aware datetime (microseconds kept)."""
    text = text.rstrip("Z")
    if "." in text:
        head, frac = text.split(".")
        text = f"{head}.{frac[:6].ljust(6, '0')}"
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def token_from_env_or_prompt(env=os.environ) -> str:
    """Token from OANDA_API_KEY / OANDA_TOKEN, else a hidden prompt. Never saved or printed."""
    token = (env.get("OANDA_API_KEY") or env.get("OANDA_TOKEN") or "").strip()
    if not token:
        token = getpass.getpass("OANDA practice token (hidden, not saved): ").strip()
    if not token:
        raise OandaReadError("no OANDA practice token given")
    return token


class ReadOnlyOanda:
    def __init__(self, token: str, account_id: str | None = None,
                 session: requests.Session | None = None, timeout: float = 5.0):
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept-Datetime-Format": "RFC3339",
        })
        self.account_id = account_id or (os.environ.get("OANDA_ACCOUNT_ID") or "").strip() or None
        if not self.account_id:
            accounts = self._get("/accounts").get("accounts") or []
            if len(accounts) != 1:
                raise OandaReadError(f"{len(accounts)} accounts on this token; set OANDA_ACCOUNT_ID")
            self.account_id = accounts[0]["id"]

    def _get(self, path: str, **params) -> dict:
        resp = self.session.get(f"{PRACTICE_URL}{path}", params=params or None, timeout=self.timeout)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            raise OandaReadError(f"GET {path} -> HTTP {resp.status_code}: "
                                 f"{body.get('errorMessage') or resp.text[:200]}")
        return body

    def _acct(self, suffix: str) -> str:
        return f"/accounts/{self.account_id}{suffix}"

    def summary(self) -> dict:
        return self._get(self._acct("/summary"))["account"]

    def instrument(self, name: str) -> dict:
        items = self._get(self._acct("/instruments"), instruments=name).get("instruments") or []
        if not items:
            raise OandaReadError(f"{name} is not offered on this account")
        return items[0]

    def quote(self, name: str) -> Quote:
        t0 = time.monotonic()
        body = self._get(self._acct("/pricing"), instruments=name, includeHomeConversions="true")
        rtt = time.monotonic() - t0
        prices = body.get("prices") or []
        if not prices or not prices[0].get("bids") or not prices[0].get("asks"):
            raise OandaReadError(f"no bid/ask for {name}")
        p = prices[0]
        conv = None
        factors = p.get("quoteHomeConversionFactors")
        if factors:
            conv = float(factors["negativeUnits"])  # the less favourable factor (losses)
        else:
            for c in body.get("homeConversions") or []:
                if c.get("currency") == name.split("_")[-1]:
                    conv = float(c.get("accountLoss") or c.get("positionValue"))
        return Quote(bid=float(p["bids"][0]["price"]), ask=float(p["asks"][0]["price"]),
                     time=parse_time(p["time"]), fetched_at=datetime.now(UTC),
                     tradeable=bool(p.get("tradeable", False)), quote_to_home=conv, rtt=rtt)

    def candles(self, name: str, granularity: str, count: int) -> list[dict]:
        """Candles with bid and ask; each has time, complete, mid o/h/l/c, bid_* and ask_*."""
        params = {"granularity": granularity, "count": count, "price": "BA"}
        if granularity == "D":
            params.update(dailyAlignment=17, alignmentTimezone="America/New_York")
        out = []
        for c in self._get(f"/instruments/{name}/candles", **params).get("candles", []):
            b, a = c["bid"], c["ask"]
            row = {"time": parse_time(c["time"]), "complete": bool(c.get("complete"))}
            for k in ("o", "h", "l", "c"):
                row[f"bid_{k}"], row[f"ask_{k}"] = float(b[k]), float(a[k])
                row[k] = (float(b[k]) + float(a[k])) / 2
            out.append(row)
        return out

    def open_trades(self, name: str) -> list[dict]:
        trades = self._get(self._acct("/openTrades")).get("trades", [])
        return [{"id": t["id"], "units": float(t["currentUnits"]), "price": float(t["price"])}
                for t in trades if t.get("instrument") == name]
