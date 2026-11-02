"""Earnings-release times from SEC EDGAR 8-K filings (item 2.02), a second source for Yahoo's dates."""

from __future__ import annotations

import pandas as pd

from tradingagents.dataflows.vendors.sec_edgar import _fetch_json, cik_for

_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"


def earnings_release_times(ticker: str, limit: int = 12) -> list[pd.Timestamp]:
    """Acceptance times (UTC) of the filer's recent 8-Ks reporting results of operations, newest first.

    Empty for a ticker that is not a US filer. Raises VendorUnavailableError when EDGAR cannot answer.
    """
    cik = cik_for(ticker)
    if cik is None:
        return []
    recent = _fetch_json(_SUBMISSIONS_URL.format(cik=cik)).get("filings", {}).get("recent", {})
    out = []
    for form, items, accepted in zip(recent.get("form", []), recent.get("items", []),
                                     recent.get("acceptanceDateTime", []), strict=False):
        if form == "8-K" and "2.02" in str(items).split(","):
            out.append(pd.Timestamp(accepted).tz_convert("UTC") if pd.Timestamp(accepted).tzinfo
                       else pd.Timestamp(accepted, tz="UTC"))
    return sorted(out, reverse=True)[:limit]
