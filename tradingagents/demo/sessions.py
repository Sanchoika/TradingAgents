"""NYSE session arithmetic for the hold rule.

A trade filled at the open of session D with ``hold_days`` = N is held through N sessions and
exits at the open of session D+N. The exit order is placed on the run before that open (an
08:00 run fills at the next open), so the order is due when N-1 sessions have passed since D.
Holidays come from a fixed list (extend it yearly); an unlisted year falls back to weekdays only
and says so via ``covered``.
"""

from __future__ import annotations

import numpy as np

NYSE_HOLIDAYS = [
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    # 2028 (New Year's Day 2028 falls on a Saturday, observed the preceding Friday: 2027-12-31)
    "2027-12-31", "2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19",
    "2028-07-04", "2028-09-04", "2028-11-23", "2028-12-25",
    # 2029
    "2029-01-01", "2029-01-15", "2029-02-19", "2029-03-30", "2029-05-28", "2029-06-19",
    "2029-07-04", "2029-09-03", "2029-11-22", "2029-12-25",
    # 2030
    "2030-01-01", "2030-01-21", "2030-02-18", "2030-04-19", "2030-05-27", "2030-06-19",
    "2030-07-04", "2030-09-02", "2030-11-28", "2030-12-25",
    # 2031
    "2031-01-01", "2031-01-20", "2031-02-17", "2031-04-11", "2031-05-26", "2031-06-19",
    "2031-07-04", "2031-09-01", "2031-11-27", "2031-12-25",
]
COVERED_YEARS = (2026, 2027, 2028, 2029, 2030, 2031)


def covered(date: str) -> bool:
    return int(date[:4]) in COVERED_YEARS


def sessions_between(start: str, end: str) -> int:
    """Sessions in [start, end): how many session opens passed since ``start``'s open."""
    return int(np.busday_count(start, end, holidays=NYSE_HOLIDAYS))


def exit_due(opened: str, run_date: str, hold_days: int) -> bool:
    return sessions_between(opened, run_date) >= hold_days - 1


def planned_exit_run(filled_on: str, hold_days: int) -> str:
    return str(np.busday_offset(filled_on, hold_days - 1, roll="forward", holidays=NYSE_HOLIDAYS))
