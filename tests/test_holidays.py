"""Tests for the holiday calendar.

The dates below are real 2026 and 2027 dates, checked against the OPM calendar
rather than derived from the same code they test. That is the point: a rule
engine that agrees with itself proves nothing.
"""

import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import holidays as H
from shared.holidays import (
    federal_holidays,
    from_env,
    holidays_between,
    is_business_day,
    next_business_day,
)


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


def test_fixed_date_holidays_2026():
    found = federal_holidays(2026)
    assert found[dt.date(2026, 1, 1)] == "New Year's Day"
    assert found[dt.date(2026, 6, 19)] == "Juneteenth"
    assert found[dt.date(2026, 11, 11)] == "Veterans Day"
    assert found[dt.date(2026, 12, 25)] == "Christmas Day"


def test_nth_weekday_holidays_2026():
    found = federal_holidays(2026)
    assert found[dt.date(2026, 1, 19)] == "Martin Luther King Jr. Day"    # 3rd Mon Jan
    assert found[dt.date(2026, 2, 16)] == "Washington's Birthday"         # 3rd Mon Feb
    assert found[dt.date(2026, 9, 7)] == "Labor Day"                      # 1st Mon Sep
    assert found[dt.date(2026, 10, 12)] == "Columbus Day"                 # 2nd Mon Oct
    assert found[dt.date(2026, 11, 26)] == "Thanksgiving Day"             # 4th Thu Nov


def test_last_monday_in_may_is_memorial_day():
    # The one "last weekday of the month" rule, and the one most often got wrong.
    assert federal_holidays(2026)[dt.date(2026, 5, 25)] == "Memorial Day"
    assert federal_holidays(2027)[dt.date(2027, 5, 31)] == "Memorial Day"


def test_saturday_holiday_is_observed_on_the_friday_before():
    # 4 July 2026 is a Saturday; the country takes Friday 3 July.
    found = federal_holidays(2026)
    assert found.get(dt.date(2026, 7, 3)) == "Independence Day", found.get(dt.date(2026, 7, 3))
    assert dt.date(2026, 7, 4) not in found


def test_sunday_holiday_is_observed_on_the_monday_after():
    # 25 December 2027 is a Saturday, but 26 December 2027 is a Sunday for
    # Christmas 2021-style checks; use Veterans Day 2029 (Sunday 11 November).
    found = federal_holidays(2029)
    assert found.get(dt.date(2029, 11, 12)) == "Veterans Day", found.get(dt.date(2029, 11, 12))


def test_new_years_day_can_land_in_the_previous_year():
    # 1 January 2028 is a Saturday, so it is observed Friday 31 December 2027.
    # Losing that at the year boundary would offer a stranger 9am on New Year's Eve.
    found = federal_holidays(2027)
    assert dt.date(2027, 12, 31) in found, sorted(found)[-3:]


def test_no_holiday_leaks_into_the_wrong_year():
    for year in (2026, 2027, 2028):
        assert all(d.year == year for d in federal_holidays(year)), year


def test_holidays_between_is_inclusive_and_bounded():
    window = holidays_between(dt.date(2026, 9, 7), dt.date(2026, 10, 12))
    assert set(window) == {dt.date(2026, 9, 7), dt.date(2026, 10, 12)}, window


def test_holidays_can_be_ignored_by_name():
    window = holidays_between(
        dt.date(2026, 10, 1), dt.date(2026, 11, 30), ignored={"Columbus Day", "Veterans Day"}
    )
    assert dt.date(2026, 10, 12) not in window
    assert dt.date(2026, 11, 11) not in window
    assert dt.date(2026, 11, 26) in window, "Thanksgiving must survive"


def test_extra_dates_are_added():
    window = holidays_between(
        dt.date(2026, 11, 20), dt.date(2026, 11, 30), extra={dt.date(2026, 11, 27)}
    )
    assert window[dt.date(2026, 11, 27)] == "Office closed"


def test_env_config_is_applied():
    os.environ["EXTRA_HOLIDAYS"] = "2026-12-24, 2026-12-31"
    os.environ["IGNORED_HOLIDAYS"] = "Columbus Day"
    try:
        window = from_env(dt.date(2026, 10, 1), dt.date(2026, 12, 31))
        assert dt.date(2026, 12, 24) in window
        assert dt.date(2026, 12, 31) in window
        assert dt.date(2026, 10, 12) not in window
    finally:
        del os.environ["EXTRA_HOLIDAYS"]
        del os.environ["IGNORED_HOLIDAYS"]


def test_unparseable_extra_dates_are_dropped_not_fatal():
    # A typo in an app setting must not take the assistant down.
    assert H._parse_dates("2026-12-24, not-a-date, ") == {dt.date(2026, 12, 24)}


def test_is_business_day():
    assert is_business_day(dt.date(2026, 9, 2))                       # Wednesday
    assert not is_business_day(dt.date(2026, 9, 5))                   # Saturday
    assert not is_business_day(dt.date(2026, 9, 7), federal_holidays(2026))  # Labor Day


def test_next_business_day_skips_the_long_weekend():
    # Friday 4 September 2026 -> Tuesday 8 September, over Labor Day.
    holidays = federal_holidays(2026)
    assert next_business_day(dt.date(2026, 9, 4), holidays) == dt.date(2026, 9, 8)


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(name, fn) for name, fn in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
