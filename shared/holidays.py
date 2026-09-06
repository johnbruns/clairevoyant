"""US federal holidays, treated as non-business days.

These dates end up suppressing slots in mail sent under Alex's name, so the
observed-date rules matter more than the nominal ones: Independence Day 2026
falls on a Saturday and the working world takes Friday 3 July off. Offering
9am Friday because the calendar happens to be empty is exactly the kind of
wrong-but-plausible output that costs a real reschedule.

No dependency on the `holidays` package. Eleven rules that have not changed
since 2021 are not worth a wheel on the Functions host, and a computed set is
testable without a network.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

log = logging.getLogger(__name__)

# name -> (month, day)
FIXED_DATE = {
    "New Year's Day": (1, 1),
    "Juneteenth": (6, 19),
    "Independence Day": (7, 4),
    "Veterans Day": (11, 11),
    "Christmas Day": (12, 25),
}

# name -> (month, weekday with Monday=0, nth occurrence; -1 means last)
NTH_WEEKDAY = {
    "Martin Luther King Jr. Day": (1, 0, 3),
    "Washington's Birthday": (2, 0, 3),
    "Memorial Day": (5, 0, -1),
    "Labor Day": (9, 0, 1),
    "Columbus Day": (10, 0, 2),
    "Thanksgiving Day": (11, 3, 4),
}


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> dt.date:
    if nth > 0:
        first = dt.date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + dt.timedelta(days=offset + 7 * (nth - 1))

    last_day = (
        dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    ) - dt.timedelta(days=1)
    return last_day - dt.timedelta(days=(last_day.weekday() - weekday) % 7)


def _observed(day: dt.date) -> dt.date:
    """Saturday holidays are taken the Friday before, Sunday ones the Monday after."""
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


def federal_holidays(year: int) -> dict[dt.date, str]:
    """Observed federal holiday dates falling within `year`.

    Keyed by observed date, not nominal date, because the observed date is the
    one the office is shut. A 1 January that falls on a Saturday is observed on
    31 December of the year before, so that case is folded in here rather than
    silently lost at the year boundary.
    """
    found: dict[dt.date, str] = {}

    for name, (month, day) in FIXED_DATE.items():
        found[_observed(dt.date(year, month, day))] = name

    for name, (month, weekday, nth) in NTH_WEEKDAY.items():
        found[_nth_weekday(year, month, weekday, nth)] = name

    # Next year's New Year's Day, when observed, can land on 31 December.
    spillover = _observed(dt.date(year + 1, 1, 1))
    if spillover.year == year:
        found[spillover] = "New Year's Day (observed)"

    return {d: n for d, n in found.items() if d.year == year}


def _parse_dates(raw: str) -> set[dt.date]:
    out: set[dt.date] = set()
    for token in raw.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.add(dt.date.fromisoformat(token))
        except ValueError:
            log.warning("Ignoring unparseable holiday date %r (want YYYY-MM-DD).", token)
    return out


def holidays_between(
    start: dt.date,
    end: dt.date,
    extra: set[dt.date] | None = None,
    ignored: set[str] | None = None,
) -> dict[dt.date, str]:
    """Every observed holiday in [start, end], inclusive."""
    ignored = {n.strip().lower() for n in (ignored or set()) if n.strip()}
    found: dict[dt.date, str] = {}

    for year in range(start.year, end.year + 1):
        for day, name in federal_holidays(year).items():
            if name.lower() in ignored:
                continue
            if start <= day <= end:
                found[day] = name

    for day in extra or set():
        if start <= day <= end:
            found[day] = "Office closed"

    return found


def from_env(start: dt.date, end: dt.date) -> dict[dt.date, str]:
    """Holidays for a window, with the two config escape hatches applied.

    EXTRA_HOLIDAYS adds comma-separated YYYY-MM-DD dates (a company closure,
    a vacation day). IGNORED_HOLIDAYS drops federal ones by name for the days
    Alex actually works - Columbus Day and Veterans Day are the usual two.
    """
    return holidays_between(
        start,
        end,
        extra=_parse_dates(os.environ.get("EXTRA_HOLIDAYS", "")),
        ignored=set(os.environ.get("IGNORED_HOLIDAYS", "").split(",")),
    )


def is_business_day(day: dt.date, holidays: dict[dt.date, str] | None = None) -> bool:
    return day.weekday() < 5 and day not in (holidays or {})


def next_business_day(day: dt.date, holidays: dict[dt.date, str] | None = None) -> dt.date:
    candidate = day + dt.timedelta(days=1)
    for _ in range(14):
        if is_business_day(candidate, holidays):
            return candidate
        candidate += dt.timedelta(days=1)
    return candidate
