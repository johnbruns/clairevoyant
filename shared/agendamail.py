"""The daily agenda email.

Three sections, in the order the day is actually read:

  1. **At a glance** - how much of the day is already spoken for.
  2. **Meetings** - who called it, who is coming, where it is, and a link into
     the real calendar event. Green rail.
  3. **Free / available** - the gaps, with a way to claim one. Blue rail.

An earlier version opened with a half-hour timeline grid. It was dropped: it
took a screen and a half to say what the four glance tiles say in one line,
and it pushed the meetings - the part with the actual information - below the
fold.

Design notes worth keeping, all learned from this mailbox's real data:

  - **Alex organises most of his own meetings**, so "Organised by Alex Rivera"
    on five cards is noise. His own name becomes "You".
  - **Attendee lists include him.** "With: Alex Rivera, Ron Gula" is wrong; he
    knows he is going. He is filtered out of the guest list.
  - **`location` is usually a raw Zoom URL**, sometimes prefixed with stray
    punctuation from a paste. Printed literally it is 120 characters of query
    string. It is detected and rendered as a Join button instead.
  - **`bodyPreview` is mostly boilerplate** - "X is inviting you to a scheduled
    Zoom meeting", "You don't often get email from...". That noise is stripped
    before the preview is shown, and if nothing survives, no preview is shown.

Outlook on Windows renders through Word, so the glance tiles are a TABLE with
`bgcolor` rather than divs with CSS backgrounds.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from zoneinfo import ZoneInfo

from . import notice, persona
from .availability import long_date, merge_contiguous
from .recap import STYLE

MAX_GUESTS_SHOWN = 5

# Anything that means leaving the building. Two independent signals, because
# neither is reliable alone: Alex's calendar has explicit "Drive to X" events
# AND in-person meetings whose only clue is a street address in `location`.
#
# A Zoom link in `location` is the common case and must NOT trip this, which is
# why the online check runs first and wins.
_TRAVEL_WORDS = re.compile(
    r"\b(driv(e|ing)|commut(e|ing)|travel(l?ing)?|en route|"
    r"pick[- ]?up|drop[- ]?off|onsite|on-site|in[- ]person)\b",
    re.I,
)
# "Lanny Volleyball @ Centennial" - a venue in the title, with no location set.
_AT_VENUE = re.compile(r"\s@\s+\S")
MAX_PREVIEW_CHARS = 180

_ONLINE = re.compile(r"https?://[^\s;,]*(zoom\.us|teams\.microsoft|meet\.google|webex)[^\s;,]*", re.I)
_URL = re.compile(r"https?://\S+")

# Openers that carry no information, stripped before a preview is shown.
_NOISE = (
    re.compile(r"you don't often get email from.*?(learn why this is important)?", re.I),
    re.compile(r"\S+ is inviting you to a scheduled zoom meeting\.?", re.I),
    re.compile(r"join zoom meeting.*", re.I | re.S),
    re.compile(r"microsoft teams meeting.*", re.I | re.S),
    re.compile(r"you have been invited by .*? to attend an event named .*?on \w+day.*", re.I),
    re.compile(r"________+.*", re.S),
)

EXTRA_STYLE = """
.hi{font-size:15px;margin:0 0 4px;color:#334155}
.date{font-size:22px;font-weight:700;margin:0 0 16px;color:#0f172a}
.glance{width:100%;border-collapse:collapse;margin:0 0 20px}
.glance td{background:#f1f5f9;border-radius:6px;padding:10px 12px;text-align:center;
font-size:13px;color:#475569}
.glance b{display:block;font-size:19px;color:#0f172a;font-weight:700;margin-bottom:2px}
.allday{background:#fffbeb;border-left:4px solid #f59e0b;border-radius:0 5px 5px 0;
padding:9px 12px;margin:0 0 16px;font-size:14px;color:#78350f}
.sec{margin:26px 0 10px;font-size:12px;font-weight:700;letter-spacing:.07em;
text-transform:uppercase;color:#64748b}
.ev{border:1px solid #e2e8f0;border-left:4px solid #16a34a;border-radius:0 7px 7px 0;
padding:12px 14px;margin:0 0 10px}
.ev.tent{border-left-color:#94a3b8}
.ev.drive{border-left-color:#dc2626;background:#fef2f2}
.ev.drive .when{color:#b91c1c}
.chip{display:inline-block;background:#dc2626;color:#fff;font-size:11px;font-weight:700;
letter-spacing:.03em;padding:2px 7px;border-radius:10px;margin-left:8px}
.drivebar{background:#fef2f2;border-left:4px solid #dc2626;border-radius:0 5px 5px 0;
padding:9px 12px;margin:0 0 16px;font-size:14px;color:#991b1b}
.drivebar b{color:#7f1d1d}
.ev .when{font-size:13px;font-weight:700;color:#15803d}
.ev.tent .when{color:#64748b}
.ev .what{font-size:16px;font-weight:600;color:#0f172a;margin:2px 0 6px}
.ev .row2{font-size:13px;color:#475569;margin:3px 0}
.ev .row2 b{color:#334155;font-weight:600}
.ev .prev{font-size:13px;color:#64748b;margin:7px 0 0;line-height:1.45}
.lk{display:inline-block;font-size:13px;font-weight:600;text-decoration:none;
color:#2563eb;margin:9px 12px 0 0}
.fgrid{width:100%;max-width:600px;border-collapse:separate;border-spacing:4px 4px;
margin:0 0 4px}
.fcell{width:16.6%;vertical-align:top}
.fchip{display:block;background:#eff6ff;border:1px solid #bfdbfe;border-radius:6px;
padding:6px 4px;text-decoration:none;text-align:center}
.ftime{font-size:11px;font-weight:700;color:#1d4ed8;white-space:nowrap}
.fdur{font-size:10px;color:#60a5fa;margin-top:1px}
.fcta{font-size:10px;font-weight:600;color:#2563eb;margin-top:3px}
.card{border:1px solid #e2e8f0;border-radius:7px;padding:13px 15px;margin:0 0 10px}
.card .k{font-weight:700;text-decoration:none;color:#2563eb;font-size:15px}
.card .sum{color:#0f172a;font-size:15px}
.meta2{color:#64748b;font-size:12px;margin-top:7px}
.meta2 b{color:#334155;font-weight:600}
.why2{color:#475569;font-size:13px;font-style:italic;margin-top:7px}
.od{color:#b91c1c;font-weight:700}
"""


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def ampm(moment) -> str:
    """3:00 PM."""
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def _span(start: dt.datetime, end: dt.datetime) -> str:
    return f"{ampm(start)} - {ampm(end)}"


def _span_short(start: dt.datetime, end: dt.datetime) -> str:
    """"9-9:30 AM" - the same span in roughly half the pixels.

    Six chips across a 600px table leaves about 90px each, and "9:00 AM -
    9:30 AM" does not fit in 90px at any readable size. Two safe compressions:
    drop ":00" from a whole hour, and state the meridiem once when both ends
    share it. Nothing else is removed, so the worst case ("11:30-12:30 PM")
    is still unambiguous.
    """
    def bare(moment: dt.datetime) -> str:
        hour = moment.hour % 12 or 12
        return f"{hour}" if moment.minute == 0 else f"{hour}:{moment.minute:02d}"

    half = lambda m: "AM" if m.hour < 12 else "PM"  # noqa: E731
    if half(start) == half(end):
        return f"{bare(start)}–{bare(end)} {half(end)}"
    return f"{bare(start)} {half(start)}–{bare(end)} {half(end)}"


def _duration(start: dt.datetime, end: dt.datetime) -> str:
    minutes = int((end - start).total_seconds() // 60)
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours}h {rest}m"
    return f"{hours}h" if hours else f"{rest}m"


def _parse(node: dict, tz: ZoneInfo) -> dt.datetime | None:
    raw = (node or {}).get("dateTime")
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            return (dt.datetime.fromisoformat(raw[:-1])
                    .replace(tzinfo=dt.timezone.utc).astimezone(tz))
        parsed = dt.datetime.fromisoformat(raw[:26] if "." in raw else raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)
    except (ValueError, TypeError):
        return None


def _same(address: str | None, me: str) -> bool:
    return (address or "").strip().lower() == (me or "").strip().lower()


def _organiser(event: dict, me: str) -> str:
    node = ((event.get("organizer") or {}).get("emailAddress") or {})
    if _same(node.get("address"), me):
        return "You"
    return node.get("name") or node.get("address") or "(unknown)"


def _guests(event: dict, me: str) -> tuple[list[str], int]:
    """Everyone except Alex, with their reply where it is interesting."""
    names = []
    for attendee in event.get("attendees") or []:
        node = attendee.get("emailAddress") or {}
        if _same(node.get("address"), me):
            continue
        name = node.get("name") or node.get("address") or "?"
        response = ((attendee.get("status") or {}).get("response") or "").lower()
        if response == "declined":
            name += " (declined)"
        elif response == "tentativelyaccepted":
            name += " (tentative)"
        names.append(name)
    return names[:MAX_GUESTS_SHOWN], max(0, len(names) - MAX_GUESTS_SHOWN)


def _my_response(event: dict) -> str:
    response = ((event.get("responseStatus") or {}).get("response") or "").lower()
    return {
        "accepted": "You accepted",
        "tentativelyaccepted": "You marked it tentative",
        "declined": "You declined",
        "notresponded": "You have not replied",
    }.get(response, "")


def _travel(event: dict) -> str:
    """Why this event needs a car, or "" if it does not.

    Order matters: an online meeting is never travel, however it is titled, so
    that check comes first and short-circuits. A "Drive to..." event with a
    Zoom link pasted in it is a calendar mistake, not a commute.
    """
    label, join = _where(event)
    if join:
        return ""

    subject = event.get("subject") or ""
    if _TRAVEL_WORDS.search(subject):
        return "Travel"
    if _AT_VENUE.search(subject):
        return "In person"
    # A physical location - a street address, a room, a building - means going
    # somewhere. An empty one means nothing was set, which is not evidence.
    if label and not _URL.match(label):
        return "In person"
    return ""


def _where(event: dict) -> tuple[str, str]:
    """(label, join url). A raw Zoom link becomes a button, not 120 characters."""
    location = ((event.get("location") or {}).get("displayName") or "").strip(" ;,")
    online = ((event.get("onlineMeeting") or {}).get("joinUrl") or "")

    match = _ONLINE.search(online or location or "")
    if match:
        url = match.group(0)
        host = match.group(1).lower()
        label = {"zoom.us": "Zoom", "teams.microsoft": "Teams",
                 "meet.google": "Google Meet", "webex": "Webex"}.get(host, "Online")
        return label, url

    if location and not _URL.match(location):
        return location, ""
    return ("Online" if location else ""), (location if _URL.match(location or "") else "")


def _preview(event: dict) -> str:
    text = (event.get("bodyPreview") or "").replace("\r", " ")
    for pattern in _NOISE:
        text = pattern.sub(" ", text)
    text = _URL.sub("", text)
    text = re.sub(r"\s+", " ", text).strip(" -–—|,;")
    if len(text) < 12:            # nothing of substance survived
        return ""
    return text[:MAX_PREVIEW_CHARS] + ("…" if len(text) > MAX_PREVIEW_CHARS else "")


def split_events(events: list[dict]) -> tuple[list[dict], list[dict]]:
    """All-day events, and timed ones.

    Graph reports an all-day event midnight-to-midnight, which drawn literally
    becomes a 24-hour block that swamps the day - and printed literally becomes
    "00:00-00:00".
    """
    all_day, timed = [], []
    for event in events:
        if event.get("isCancelled"):
            continue
        (all_day if event.get("isAllDay") else timed).append(event)
    return all_day, timed


def _event_card(event: dict, begins, ends, me: str) -> str:
    tentative = (event.get("showAs") or "") == "tentative"
    travel = _travel(event)
    guests, extra = _guests(event, me)
    where, join = _where(event)
    preview = _preview(event)
    link = event.get("webLink") or ""

    rows = [f"<div class='row2'><b>Organised by</b> {_esc(_organiser(event, me))}</div>"]
    if guests:
        listed = ", ".join(guests) + (f" +{extra} more" if extra else "")
        rows.append(f"<div class='row2'><b>With</b> {_esc(listed)}</div>")
    else:
        rows.append("<div class='row2'><b>With</b> just you</div>")
    if where:
        rows.append(f"<div class='row2'><b>Where</b> {_esc(where)}</div>")
    mine = _my_response(event)
    if mine and _organiser(event, me) != "You":
        rows.append(f"<div class='row2'>{_esc(mine)}</div>")

    links = []
    if join:
        links.append(f"<a class='lk' href='{html.escape(join, quote=True)}'>Join &rarr;</a>")
    if link:
        links.append(
            f"<a class='lk' href='{html.escape(link, quote=True)}'>More &mdash; open in calendar &rarr;</a>"
        )

    classes = "ev" + (" drive" if travel else (" tent" if tentative else ""))
    chip = (f"<span class='chip'>&#128663; {_esc(travel)}</span>" if travel else "")

    return (
        f"<div class='{classes}'>"
        f"<div class='when'>{_span(begins, ends)} &nbsp;&middot;&nbsp; "
        f"{_duration(begins, ends)}{' &middot; tentative' if tentative else ''}{chip}</div>"
        f"<div class='what'>{'&#128663; ' if travel else ''}"
        f"{_esc(event.get('subject') or '(no subject)')}</div>"
        + "".join(rows)
        + (f"<div class='prev'>{_esc(preview)}</div>" if preview else "")
        + "".join(links)
        + "</div>"
    )


def _glance(busy: list[tuple], free_slots: list, for_date: dt.date) -> str:
    booked = sum((e - s).total_seconds() for s, e, _, _ in busy) / 3600
    blocks = merge_contiguous([s for s in free_slots if s.start.date() == for_date])
    free_hours = sum((b.end - b.start).total_seconds() for b in blocks) / 3600
    first = min((s for s, _, _, _ in busy), default=None)

    def cell(value, label):
        return f"<td><b>{value}</b>{label}</td>"

    return (
        "<table class='glance' role='presentation'><tr>"
        + cell(len(busy), "meeting" + ("s" if len(busy) != 1 else ""))
        + "<td style='width:8px;background:#fff'></td>"
        + cell(f"{booked:.1f}h", "booked")
        + "<td style='width:8px;background:#fff'></td>"
        + cell(f"{free_hours:.1f}h", "free in 9-4")
        + "<td style='width:8px;background:#fff'></td>"
        + cell(ampm(first) if first else "-", "first up")
        + "</tr></table>"
    )


def build_agenda(
    events: list[dict],
    free: list,
    for_date: dt.date,
    timezone: str = "America/New_York",
    plan=None,
    is_today: bool = True,
    user_name: str = "Alex",
    task_links: dict | None = None,
    block_links: dict | None = None,
    me: str = "alex@example.org",
    board_url: str = "",
) -> tuple[str, str]:
    """Return (subject, html) for the daily agenda."""
    tz = ZoneInfo(timezone)
    task_links = task_links or {}
    block_links = block_links or {}
    parts = [f"<style>{STYLE}{EXTRA_STYLE}{persona.STYLE}{notice.STYLE}</style>"]

    when = "daily agenda" if is_today else "agenda for tomorrow"
    parts.append(persona.greeting_html())
    parts.append(f"<p class='hi'>{_esc(user_name)}, here's your {when}.</p>")
    parts.append(f"<p class='date'>{_esc(long_date(for_date))}</p>")

    all_day, timed = split_events(events)

    busy = []
    for event in timed:
        begins, ends = _parse(event.get("start"), tz), _parse(event.get("end"), tz)
        if begins and ends and ends > begins:
            busy.append((begins, ends, event.get("subject") or "(no subject)", event))
    busy.sort(key=lambda b: b[0])

    parts.append(_glance(busy, free, for_date))

    driving = [(b, e, ev) for b, e, _, ev in busy if _travel(ev)]
    if driving:
        legs = ", ".join(f"{_esc(ev.get('subject'))} at {ampm(b)}" for b, e, ev in driving)
        parts.append(
            f"<div class='drivebar'>&#128663; <b>{len(driving)} "
            f"{'event needs' if len(driving) == 1 else 'events need'} travel today</b> "
            f"&mdash; {legs}</div>"
        )

    for event in all_day:
        parts.append(
            f"<div class='allday'><b>All day</b> &nbsp;{_esc(event.get('subject'))}</div>"
        )

    if not busy and not all_day:
        parts.append("<p class='none'>Nothing on the calendar.</p>")

    if busy:
        parts.append(f"<div class='sec'>Meetings ({len(busy)})</div>")
        parts.extend(_event_card(event, begins, ends, me)
                     for begins, ends, _, event in busy)

    blocks = merge_contiguous([s for s in free if s.start.date() == for_date])
    if blocks:
        parts.append(f"<div class='sec'>Free / available ({len(blocks)})</div>")
        parts.append(_free_grid(blocks, block_links))

    parts.extend(_dashboard(plan, for_date, task_links, board_url))

    parts.append(notice.html())
    parts.append(persona.signoff_html())

    label = "Your Daily Agenda" if is_today else "Tomorrow's Agenda"
    return f"{label}: {for_date.strftime('%m/%d/%Y')}", "".join(parts)


FREE_PER_ROW = 6


def _free_grid(blocks: list, block_links: dict, per_row: int = FREE_PER_ROW) -> str:
    """Free time as small chips, laid out side by side.

    Six across, so a light day is one line and a wide-open day wraps to two
    rows of three rather than dribbling down the page.

    A TABLE, chunked into rows of `per_row`, rather than inline-block elements
    that wrap on their own. Outlook on Windows renders through Word, which does
    not wrap inline-blocks - they would run off the right edge and the last
    chips would simply be unreachable. Chunking makes the wrap explicit and
    therefore reliable.

    The whole chip is the link. A separate "Block this time" line under each
    one is what made this section three screens long; the confirmation page is
    what protects against a stray click, not a small target.
    """
    rows = []
    for start in range(0, len(blocks), per_row):
        cells = []
        for block in blocks[start : start + per_row]:
            url = block_links.get((block.start, block.end))
            inner = (
                f"<div class='ftime'>{_span_short(block.start, block.end)}</div>"
                f"<div class='fdur'>{_duration(block.start, block.end)} free</div>"
            )
            body = (
                f"<a class='fchip' href='{html.escape(url, quote=True)}'>{inner}"
                "<div class='fcta'>&#43; Block</div></a>"
                if url
                else f"<div class='fchip'>{inner}</div>"
            )
            cells.append(f"<td class='fcell'>{body}</td>")

        # Pad the last row so two chips do not stretch to fill six slots.
        cells += ["<td class='fcell'>&nbsp;</td>"] * (per_row - len(cells))
        rows.append("<tr>" + "".join(cells) + "</tr>")

    return (
        "<table class='fgrid' role='presentation' cellpadding='0' cellspacing='0'>"
        + "".join(rows)
        + "</table>"
    )


def _issue_meta(issue, today: dt.date) -> str:
    bits = []
    if issue is None:
        return ""
    if getattr(issue, "due", None):
        try:
            late = " <span class='od'>(overdue)</span>" if (
                dt.date.fromisoformat(issue.due) - today).days < 0 else ""
        except (ValueError, TypeError):
            late = ""
        bits.append(f"<b>Due</b> {_esc(issue.due)}{late}")
    if getattr(issue, "created", None):
        bits.append(f"<b>Created</b> {_esc(issue.created)}")
    if getattr(issue, "status", None):
        bits.append(f"<b>Status</b> {_esc(issue.status)}")
    priority = getattr(issue, "priority", None)
    if priority and priority.lower() not in ("medium", "none"):
        bits.append(f"<b>Priority</b> {_esc(priority)}")
    return f"<div class='meta2'>{' &nbsp;&middot;&nbsp; '.join(bits)}</div>" if bits else ""


def _dashboard(plan, for_date: dt.date, task_links: dict,
               board_url: str = "") -> list[str]:
    if plan is None:
        return []

    parts = ["<div class='sec'>Your task dashboard</div>"]
    if board_url:
        parts.append(
            f"<a class='lk' style='margin:0 0 12px' "
            f"href='{html.escape(board_url, quote=True)}'>"
            "Open your TASK board &rarr;</a>"
        )
    if plan.focus:
        parts.append(f"<div class='focus'>{_esc(plan.focus)}</div>")

    if not plan.picks:
        parts.append(f"<p class='none'>{_esc(plan.note or 'No recommendations.')}</p>")
        return parts

    for pick in plan.picks:
        key = (
            f"<a class='k' href='{html.escape(pick.url, quote=True)}'>{_esc(pick.key)}</a>"
            if pick.url
            else f"<span class='k'>{_esc(pick.key)}</span>"
        )
        done = task_links.get(pick.key)
        parts.append(
            "<div class='card'>"
            f"{key} &nbsp;<span class='sum'>{_esc(pick.summary)}</span>"
            f"{_issue_meta(getattr(pick, 'issue', None), for_date)}"
            f"<div class='why2'>{_esc(pick.why)}</div>"
            + (f"<a class='lk' href='{html.escape(done, quote=True)}' "
               "style='color:#15803d'>&#10003; Mark done &rarr;</a>" if done else "")
            + "</div>"
        )
    return parts
