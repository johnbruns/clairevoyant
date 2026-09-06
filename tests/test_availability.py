import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.availability import (
    Slot,
    WorkingHours,
    busy_intervals,
    format_for_email,
    free_slots,
    merge_contiguous,
)
from shared.holidays import holidays_between

TZ = ZoneInfo("America/New_York")

# 9am-4pm in half-hours. Fourteen, not the sixteen a 9-5 day held: the window
# was cut to 4pm, and every count in this file follows from that.
FULL_DAY = 14


def ev(start: str, end: str, show_as="busy", cancelled=False, all_day=False):
    return {
        "subject": "x",
        "start": {"dateTime": start, "timeZone": "Eastern Standard Time"},
        "end": {"dateTime": end, "timeZone": "Eastern Standard Time"},
        "showAs": show_as,
        "isCancelled": cancelled,
        "isAllDay": all_day,
    }


def slot(day, h1, m1, h2, m2):
    return Slot(
        dt.datetime(2026, 9, day, h1, m1, tzinfo=TZ),
        dt.datetime(2026, 9, day, h2, m2, tzinfo=TZ),
    )


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


# Wednesday 2026-09-02, 6am local: whole working day ahead.
NOW = dt.datetime(2026, 9, 2, 6, 0, tzinfo=TZ)


def test_empty_calendar_full_day():
    slots = free_slots([], NOW, days=1)
    assert len(slots) == FULL_DAY, f"expected {FULL_DAY} half-hours in 9-4, got {len(slots)}"
    assert slots[0].start.hour == 9 and slots[0].start.minute == 0
    assert slots[-1].end.hour == 16


def test_working_day_ends_at_four_not_five():
    # Alex's rule. A 4:30 slot offered to a stranger is a 4:30 meeting.
    slots = free_slots([], NOW, days=1)
    assert not [s for s in slots if s.end.hour > 16], [s.label() for s in slots[-3:]]


def test_busy_blocks_overlap():
    events = [ev("2026-09-02T10:00:00", "2026-09-02T11:00:00")]
    slots = free_slots(events, NOW, days=1)
    blocked = [s for s in slots if 10 <= s.start.hour < 11]
    assert not blocked, f"10-11 should be blocked, got {[s.label() for s in blocked]}"
    assert len(slots) == FULL_DAY - 2


def test_partial_overlap_blocks_whole_slot():
    # 10:15-10:45 sits inside two half-hour slots and must kill both.
    events = [ev("2026-09-02T10:15:00", "2026-09-02T10:45:00")]
    slots = free_slots(events, NOW, days=1)
    labels = [s.label() for s in slots]
    assert "10-10:30am" not in labels and "10:30-11am" not in labels, labels
    assert "9:30-10am" in labels and "11-11:30am" in labels, labels


def test_free_showas_does_not_block():
    events = [ev("2026-09-02T10:00:00", "2026-09-02T11:00:00", show_as="free")]
    assert len(free_slots(events, NOW, days=1)) == FULL_DAY


def test_tentative_does_block():
    events = [ev("2026-09-02T10:00:00", "2026-09-02T11:00:00", show_as="tentative")]
    assert len(free_slots(events, NOW, days=1)) == FULL_DAY - 2


def test_cancelled_ignored():
    events = [ev("2026-09-02T10:00:00", "2026-09-02T11:00:00", cancelled=True)]
    assert len(free_slots(events, NOW, days=1)) == FULL_DAY


def test_all_day_busy_blocks_day():
    events = [ev("2026-09-02T00:00:00", "2026-09-03T00:00:00", all_day=True)]
    assert free_slots(events, NOW, days=1) == []


def test_lead_time_excludes_imminent():
    now = dt.datetime(2026, 9, 2, 10, 5, tzinfo=TZ)
    slots = free_slots([], now, days=1, lead_minutes=60)
    # 10:05 + 60m = 11:05, rounds up to 11:30.
    assert slots[0].start.hour == 11 and slots[0].start.minute == 30, slots[0].label()


def test_weekends_skipped():
    # Friday 2026-09-04 -> next weekday is Monday 2026-09-07.
    friday = dt.datetime(2026, 9, 4, 18, 0, tzinfo=TZ)
    slots = free_slots([], friday, days=4)
    days = sorted({s.start.date() for s in slots})
    assert days == [dt.date(2026, 9, 7)], days


def test_weekends_included_when_allowed():
    friday = dt.datetime(2026, 9, 4, 18, 0, tzinfo=TZ)
    slots = free_slots([], friday, days=3, hours=WorkingHours(weekdays_only=False))
    days = sorted({s.start.date() for s in slots})
    assert days == [dt.date(2026, 9, 5), dt.date(2026, 9, 6)], days


def test_holidays_are_skipped():
    # Monday 2026-09-07 is Labor Day. Without the holiday set it is a normal
    # Monday, which is how you offer a stranger 9am on a day the office is shut.
    friday = dt.datetime(2026, 9, 4, 18, 0, tzinfo=TZ)
    holidays = holidays_between(dt.date(2026, 9, 4), dt.date(2026, 9, 14))
    assert dt.date(2026, 9, 7) in holidays, holidays

    without = {s.start.date() for s in free_slots([], friday, days=4)}
    with_holidays = {s.start.date() for s in free_slots([], friday, days=4, holidays=holidays)}
    assert dt.date(2026, 9, 7) in without
    assert with_holidays == set(), with_holidays


def test_horizon_is_ten_calendar_days_not_ten_working_days():
    # Ten calendar days from Friday 4 September reaches Sunday 13 September, so
    # the last working day inside the window is Friday 11 September.
    friday = dt.datetime(2026, 9, 4, 18, 0, tzinfo=TZ)
    slots = free_slots([], friday, days=10)
    assert max(s.start.date() for s in slots) == dt.date(2026, 9, 11)


def test_overlapping_events_merge():
    events = [
        ev("2026-09-02T10:00:00", "2026-09-02T11:00:00"),
        ev("2026-09-02T10:30:00", "2026-09-02T12:00:00"),
    ]
    merged = busy_intervals(events, TZ)
    assert len(merged) == 1, merged
    assert merged[0][1].hour == 12


def test_buffer_pads_meetings():
    events = [ev("2026-09-02T10:00:00", "2026-09-02T10:30:00")]
    slots = free_slots(events, NOW, days=1, buffer_minutes=30)
    labels = [s.label() for s in slots]
    assert "9:30-10am" not in labels and "10:30-11am" not in labels, labels
    assert "9-9:30am" in labels and "11-11:30am" in labels, labels


def test_dst_fallback_day_has_correct_count():
    # 2026-11-01 is the fall-back Sunday; the following Monday must still be full.
    now = dt.datetime(2026, 10, 30, 6, 0, tzinfo=TZ)
    slots = free_slots([], now, days=5)
    monday = [s for s in slots if s.start.date() == dt.date(2026, 11, 2)]
    assert len(monday) == FULL_DAY, len(monday)
    assert monday[0].start.utcoffset() == dt.timedelta(hours=-5), "should be EST after fallback"


def test_utc_datetimes_are_localised():
    events = [
        {
            "start": {"dateTime": "2026-09-02T14:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-09-02T15:00:00Z", "timeZone": "UTC"},
            "showAs": "busy",
            "isCancelled": False,
        }
    ]
    # 14:00Z == 10:00 EDT
    slots = free_slots(events, NOW, days=1)
    labels = [s.label() for s in slots]
    assert "10-10:30am" not in labels, labels
    assert "9:30-10am" in labels, labels


# ---- blocks --------------------------------------------------------------


def test_contiguous_slots_merge_into_one_block():
    slots = [slot(2, 9, 0, 9, 30), slot(2, 9, 30, 10, 0), slot(2, 10, 0, 10, 30)]
    blocks = merge_contiguous(slots)
    assert len(blocks) == 1
    assert blocks[0].label() == "9-10:30am", blocks[0].label()


def test_a_gap_splits_the_block():
    slots = [slot(2, 9, 0, 9, 30), slot(2, 11, 0, 11, 30), slot(2, 11, 30, 12, 0)]
    assert [b.label() for b in merge_contiguous(slots)] == ["9-9:30am", "11am-12pm"]


def test_whole_free_day_is_stated_as_one_block():
    # "If I'm available for a large block of time, state that full block."
    slots = free_slots([], NOW, days=1)
    text = format_for_email(slots)
    assert "Wednesday, September 2: 9am-4pm" in text, text


def test_format_groups_by_day_as_blocks():
    slots = [
        slot(2, 9, 0, 9, 30),
        slot(2, 9, 30, 10, 0),
        slot(2, 14, 0, 14, 30),
        slot(3, 11, 0, 11, 30),
    ]
    text = format_for_email(slots)
    assert "Wednesday, September 2: 9-10am, 2-2:30pm" in text, text
    assert "Thursday, September 3: 11-11:30am" in text, text


def test_format_names_the_timezone():
    assert format_for_email(free_slots([], NOW, days=1)).endswith("All times EDT.")


def test_empty_availability_is_empty_string_not_a_sentence():
    # The caller decides the wording. A hardcoded "no open slots in the next
    # week" would be wrong in a reply and wrong in the agenda.
    assert format_for_email([]) == ""


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(name, fn) for name, fn in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
