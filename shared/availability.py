"""Compute open half-hour slots from a Graph calendar view.

Used when the assistant replies to a meeting request, so the numbers here end
up in mail sent under Alex's name. Wrong output is worse than no output: an
offered slot that is actually taken costs a real reschedule.

Two rules drive the shape of the output. The window is business hours only -
9am to 4pm, weekdays, no federal holidays - and adjacent half-hours are
collapsed into blocks before anyone reads them. Ten days of thirty-minute
slots listed individually is 140 lines nobody will scan; the same ten days as
blocks is ten lines, and "Monday, September 7: 9am-4pm" is what a person would
have written by hand.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .holidays import is_business_day

# showAs values that do NOT block time. Everything else blocks, including
# "tentative" - offering a slot held by a tentative meeting is how you end up
# double-booked.
NON_BLOCKING = {"free", "workingElsewhere"}

# Ten calendar days ahead, 9am-4pm. Both are Alex's stated rules rather than
# tunable defaults: the horizon is how far ahead he will commit, and the window
# is when he takes meetings.
DEFAULT_HORIZON_DAYS = 10


@dataclass(frozen=True)
class Slot:
    start: dt.datetime
    end: dt.datetime

    def label(self) -> str:
        """"9-9:30am", not "9:00am-9:30am".

        The meridiem is dropped from the start when it matches the end, which
        is how a person writes a time range.
        """
        same_meridiem = self.start.strftime("%p") == self.end.strftime("%p")
        return f"{_fmt_time(self.start, meridiem=not same_meridiem)}-{_fmt_time(self.end)}"

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True)
class WorkingHours:
    start_hour: int = 9
    start_minute: int = 0
    end_hour: int = 16
    end_minute: int = 0
    weekdays_only: bool = True


def _fmt_time(moment: dt.datetime, meridiem: bool = True) -> str:
    text = moment.strftime("%I:%M").lstrip("0")
    if text.endswith(":00"):
        text = text[:-3]
    if meridiem:
        text += moment.strftime("%p").lower()
    return text


def _parse_graph_datetime(node: dict, tz: ZoneInfo) -> dt.datetime:
    """Parse a Graph dateTimeTimeZone node.

    With the Prefer: outlook.timezone header, Graph returns wall-clock times in
    the requested zone but with no offset attached, so they must be localised
    rather than assumed UTC.
    """
    raw = node["dateTime"]
    if raw.endswith("Z"):
        return dt.datetime.fromisoformat(raw[:-1]).replace(tzinfo=dt.timezone.utc).astimezone(tz)
    parsed = dt.datetime.fromisoformat(raw[:26]) if "." in raw else dt.datetime.fromisoformat(raw)
    zone = node.get("timeZone", "")
    if parsed.tzinfo is not None:
        return parsed.astimezone(tz)
    if zone in ("UTC", "GMT Standard Time"):
        return parsed.replace(tzinfo=dt.timezone.utc).astimezone(tz)
    return parsed.replace(tzinfo=tz)


def busy_intervals(events: list[dict], tz: ZoneInfo) -> list[tuple[dt.datetime, dt.datetime]]:
    intervals: list[tuple[dt.datetime, dt.datetime]] = []
    for event in events:
        if event.get("isCancelled"):
            continue
        if (event.get("showAs") or "busy") in NON_BLOCKING:
            continue
        start = _parse_graph_datetime(event["start"], tz)
        end = _parse_graph_datetime(event["end"], tz)
        if end <= start:
            continue
        intervals.append((start, end))
    return _merge(intervals)


def _merge(
    intervals: list[tuple[dt.datetime, dt.datetime]],
) -> list[tuple[dt.datetime, dt.datetime]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def free_slots(
    events: list[dict],
    now: dt.datetime,
    timezone: str = "America/New_York",
    days: int = DEFAULT_HORIZON_DAYS,
    hours: WorkingHours | None = None,
    slot_minutes: int = 30,
    lead_minutes: int = 60,
    buffer_minutes: int = 0,
    max_per_day: int | None = None,
    holidays: dict[dt.date, str] | None = None,
) -> list[Slot]:
    """Open slots over the next `days` CALENDAR days, in chronological order.

    Calendar days, not business days. "The next 10 days" counts weekends and
    holidays and then skips over them, so the horizon is a fixed wall-clock
    promise rather than something that quietly stretches to three weeks across
    a holiday.

    lead_minutes keeps the assistant from offering a slot fifteen minutes from
    now, which reads as desperate and is usually unusable anyway.
    """
    hours = hours or WorkingHours()
    tz = ZoneInfo(timezone)
    now = now.astimezone(tz)
    earliest = now + dt.timedelta(minutes=lead_minutes)

    busy = busy_intervals(events, tz)
    if buffer_minutes:
        pad = dt.timedelta(minutes=buffer_minutes)
        busy = _merge([(s - pad, e + pad) for s, e in busy])

    step = dt.timedelta(minutes=slot_minutes)
    found: list[Slot] = []

    for offset in range(days):
        day = (now + dt.timedelta(days=offset)).date()
        if hours.weekdays_only and not is_business_day(day, holidays):
            continue

        window_start = dt.datetime.combine(
            day, dt.time(hours.start_hour, hours.start_minute), tzinfo=tz
        )
        window_end = dt.datetime.combine(
            day, dt.time(hours.end_hour, hours.end_minute), tzinfo=tz
        )

        cursor = max(window_start, _ceil_to_slot(earliest, slot_minutes, tz))
        per_day = 0
        while cursor + step <= window_end:
            slot_end = cursor + step
            if not any(start < slot_end and cursor < end for start, end in busy):
                found.append(Slot(cursor, slot_end))
                per_day += 1
                if max_per_day and per_day >= max_per_day:
                    break
            cursor = slot_end

    return found


def _ceil_to_slot(moment: dt.datetime, slot_minutes: int, tz: ZoneInfo) -> dt.datetime:
    moment = moment.astimezone(tz).replace(second=0, microsecond=0)
    remainder = moment.minute % slot_minutes
    if remainder:
        moment += dt.timedelta(minutes=slot_minutes - remainder)
    return moment


def merge_contiguous(slots: list[Slot]) -> list[Slot]:
    """Collapse adjacent half-hours into the largest block each belongs to.

    This is the "if I'm available for a large block, state the full block" rule.
    A run of fourteen consecutive slots becomes one 9am-4pm block; a single
    meeting in the middle splits it into two.
    """
    if not slots:
        return []
    ordered = sorted(slots, key=lambda s: s.start)
    blocks = [[ordered[0]]]
    for slot in ordered[1:]:
        if slot.start == blocks[-1][-1].end:
            blocks[-1].append(slot)
        else:
            blocks.append([slot])
    return [Slot(b[0].start, b[-1].end) for b in blocks]


def long_date(day: dt.date) -> str:
    """"Wednesday, September 2" - no zero-padding on the day.

    Built by hand instead of with strftime("%A, %B %-d"). The "%-d" padding
    modifier is a glibc extension: it works on the Linux Functions host but
    raises ValueError on Windows, which made both the recap and the agenda
    unrunnable on the machine this assistant is calibrated from.
    """
    return f"{day.strftime('%A, %B')} {day.day}"


def format_for_email(slots: list[Slot], timezone: str = "America/New_York") -> str:
    """Group slots by day, as merged blocks, one line per day.

    Returns "" - not a sentence - when nothing is open. The caller decides what
    an empty calendar means, because the right words differ between a reply to
    another person and a line in Alex's own agenda.
    """
    if not slots:
        return ""

    by_day: dict[dt.date, list[Slot]] = {}
    for slot in slots:
        by_day.setdefault(slot.start.date(), []).append(slot)

    lines = []
    for day, day_slots in sorted(by_day.items()):
        blocks = merge_contiguous(day_slots)
        times = ", ".join(b.label() for b in blocks)
        lines.append(f"{long_date(day)}: {times}")

    abbrev = dt.datetime.now(ZoneInfo(timezone)).strftime("%Z")
    return "\n".join(lines) + f"\n\nAll times {abbrev}."
