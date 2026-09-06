"""The Friday noon "rescued from junk" email.

Deliberately plain compared with the events digest. This one is a decision
queue, not a recommendation: Alex is being asked to confirm a judgement about
mail Outlook already got wrong once, and the layout should make the sender and
the reason easy to compare rather than make anything look appealing.

The countdown to 5pm is stated at the top of every one of these, because the
whole email is only meaningful if the deadline is obvious.
"""

from __future__ import annotations

import datetime as dt
import html

from . import notice, persona
from .availability import long_date
from .recap import STYLE

EXTRA_STYLE = """
.deadline{background:#fef3c7;border:1px solid #fcd34d;border-radius:6px;
padding:11px 13px;font-size:13.5px;color:#92400e;margin:0 0 18px}
.deadline b{color:#78350f}
.jitem{border:1px solid #e2e8f0;border-left:4px solid #64748b;border-radius:0 8px 8px 0;
padding:13px 15px;margin:0 0 12px}
.jitem.event{border-left-color:#7c3aed}
.jitem.person{border-left-color:#2563eb}
.jitem.transactional{border-left-color:#b45309}
.jitem .who{font-size:15px;font-weight:700;color:#0f172a}
.jitem .addr{font-size:12px;color:#94a3b8;font-weight:400}
.jitem .subj{font-size:14px;color:#334155;margin-top:3px}
.jitem .why{font-size:13px;color:#475569;font-style:italic;margin-top:7px;line-height:1.45}
.jitem .ev{font-size:13px;font-weight:600;color:#6d28d9;margin-top:6px}
.jitem .when2{font-size:11.5px;color:#94a3b8;margin-top:6px}
.kind{display:inline-block;font-size:10px;font-weight:700;padding:2px 7px;
border-radius:10px;margin-left:8px;vertical-align:middle;text-transform:uppercase;
letter-spacing:.04em}
.kind.event{background:#ede9fe;color:#5b21b6}
.kind.person{background:#dbeafe;color:#1e40af}
.kind.organisation{background:#f1f5f9;color:#334155}
.kind.transactional{background:#fef3c7;color:#92400e}
"""

_KIND_LABEL = {
    "event": "Event",
    "person": "A person",
    "organisation": "Organisation",
    "transactional": "Needs handling",
}


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def _ampm(moment: dt.datetime) -> str:
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def _event_line(candidate) -> str:
    starts = candidate.starts_at()
    if not starts:
        return ""
    label = long_date(starts.date())
    # 09:00 is the placeholder for "date known, time not", so a bare 9am is
    # shown as a day rather than implying a start nobody stated.
    if (starts.hour, starts.minute) != (9, 0):
        label = f"{label} at {_ampm(starts)}"
    where = candidate.event_location or ("Online" if candidate.event_online else "")
    title = candidate.event_title or candidate.subject
    return (f"<div class='ev'>{_esc(title)} &middot; {_esc(label)}"
            + (f" &middot; {_esc(where)}" if where else "") + "</div>")


def _button(url: str, label: str, background: str, colour: str = "#ffffff") -> str:
    return (
        f"<td bgcolor='{background}' style='border-radius:6px'>"
        f"<a href='{html.escape(url, quote=True)}' style='display:inline-block;"
        "padding:10px 14px;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,"
        "sans-serif;font-size:12.5px;font-weight:600;text-decoration:none;"
        f"color:{colour};border-radius:6px'>{_esc(label)}</a></td>"
    )


def _answers(candidate, links: dict[str, str]) -> str:
    cells = []
    if candidate.is_event and links.get("junk_calendar"):
        cells.append(_button(links["junk_calendar"],
                             "Move to inbox + add to calendar", "#7c3aed"))
    if links.get("junk_rescue"):
        cells.append(_button(links["junk_rescue"], "Move to inbox", "#2563eb"))
    if links.get("junk_block"):
        cells.append(_button(links["junk_block"], "Block sender & delete", "#dc2626"))
    if not cells:
        return ""
    gap = "<td style='width:8px'>&nbsp;</td>"
    return ("<table role='presentation' cellpadding='0' cellspacing='0' border='0' "
            "style='margin-top:12px'><tr>" + gap.join(cells) + "</tr></table>")


def build_junk_review(
    candidates: list,
    for_date: dt.date,
    links: dict[str, dict[str, str]] | None = None,
    scanned: int = 0,
) -> tuple[str, str]:
    """Return (subject, html). Only ever called when there is something to show."""
    links = links or {}
    parts = [f"<style>{STYLE}{EXTRA_STYLE}{persona.STYLE}{notice.STYLE}</style>",
             persona.greeting_html()]
    parts.append("<h2>Possibly not junk</h2>")

    parts.append(
        "<div class='deadline'><b>Your junk folder is emptied at 5:00 PM today.</b> "
        f"I read all {scanned} message(s) in there and these {len(candidates)} "
        "did not look like junk to me. Everything else goes. Anything you want to "
        "keep, deal with here or move it out of Junk yourself before 5:00 &mdash; "
        "and nothing is destroyed, it all lands in Deleted Items where you have "
        "thirty days to change your mind.</div>"
    )

    for candidate in candidates:
        kind = candidate.kind if candidate.kind in _KIND_LABEL else "organisation"
        rows = [
            f"<div class='who'>{_esc(candidate.sender_name or candidate.sender_address)}"
            f"<span class='kind {kind}'>{_esc(_KIND_LABEL[kind])}</span></div>"
        ]
        if candidate.sender_address:
            rows.append(f"<div class='addr'>{_esc(candidate.sender_address)}</div>")
        rows.append(f"<div class='subj'>{_esc(candidate.subject)}</div>")
        if candidate.is_event:
            rows.append(_event_line(candidate))
        if candidate.why:
            rows.append(f"<div class='why'>{_esc(candidate.why)}</div>")
        if candidate.received:
            rows.append(f"<div class='when2'>Arrived {_esc(candidate.received[:10])}</div>")

        parts.append(
            f"<div class='jitem {kind}'>"
            + "".join(rows)
            + _answers(candidate, links.get(candidate.id, {}))
            + "</div>"
        )

    parts.append(notice.html())
    parts.append(persona.signoff_html())
    return f"Possibly not junk ({len(candidates)}) - your junk folder empties at 5 PM", \
           "".join(parts)
