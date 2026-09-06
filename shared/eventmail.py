"""The "Events You May Want to Attend" digest.

Tuesday and Friday, midday. Each event carries three answers, and the third
one is the reason the whole thing works: "No thanks" both deletes the email and
records the decision, so the same conference cannot come back next Tuesday
under a different subject line. A digest that re-offers what you already
declined is one you stop reading.
"""

from __future__ import annotations

import datetime as dt
import html

from . import notice, persona
from .availability import long_date
from .recap import STYLE

EXTRA_STYLE = """
.intro{font-size:14px;color:#475569;margin:0 0 18px}
.evt{border:1px solid #e2e8f0;border-left:4px solid #7c3aed;border-radius:0 8px 8px 0;
padding:14px 16px;margin:0 0 14px}
.evt .name{font-size:17px;font-weight:700;color:#0f172a;margin:0 0 6px}
.evt .when{font-size:14px;font-weight:600;color:#6d28d9}
.evt .row{font-size:13px;color:#475569;margin:4px 0}
.evt .row b{color:#334155;font-weight:600}
.evt .why{font-size:13px;color:#64748b;font-style:italic;margin:8px 0 0;line-height:1.45}
.evt .src{font-size:12px;color:#94a3b8;margin:8px 0 0}
.free{display:inline-block;background:#dcfce7;color:#166534;font-size:11px;
font-weight:700;padding:2px 7px;border-radius:10px;margin-left:8px}
.soon{display:inline-block;background:#fef3c7;color:#92400e;font-size:11px;
font-weight:700;padding:2px 7px;border-radius:10px;margin-left:8px}
"""


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def _ampm(moment: dt.datetime) -> str:
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def _when(candidate) -> str:
    starts = candidate.starts_at()
    if not starts:
        return candidate.start or "date unknown"
    label = long_date(starts.date())
    # 09:00 is the placeholder the extractor uses when only a date was given,
    # so a bare 9am is shown as a day rather than implying a start time
    # nobody actually stated.
    if (starts.hour, starts.minute) == (9, 0):
        return label
    return f"{label} at {_ampm(starts)}"


def _button(url: str, label: str, background: str, colour: str = "#ffffff") -> str:
    return (
        f"<td bgcolor='{background}' style='border-radius:6px'>"
        f"<a href='{html.escape(url, quote=True)}' style='display:inline-block;"
        "padding:10px 16px;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,"
        "sans-serif;font-size:13px;font-weight:600;text-decoration:none;"
        f"color:{colour};border-radius:6px'>{_esc(label)}</a></td>"
    )


def _answers(links: dict[str, str]) -> str:
    cells = []
    if links.get("event_yes"):
        cells.append(_button(links["event_yes"], "Yeah, I'll attend that", "#16a34a"))
    if links.get("event_maybe"):
        cells.append(_button(links["event_maybe"], "I might attend", "#d97706"))
    if links.get("event_no"):
        cells.append(_button(links["event_no"], "No, thanks!", "#dc2626"))
    if not cells:
        return ""
    gap = "<td style='width:8px'>&nbsp;</td>"
    return ("<table role='presentation' cellpadding='0' cellspacing='0' border='0' "
            "style='margin-top:12px'><tr>" + gap.join(cells) + "</tr></table>")


def build_event_digest(
    candidates: list,
    for_date: dt.date,
    links: dict[str, dict[str, str]] | None = None,
    days_scanned: int = 14,
) -> tuple[str, str]:
    """Return (subject, html). Candidates are expected sorted by start date."""
    links = links or {}
    parts = [f"<style>{STYLE}{EXTRA_STYLE}{persona.STYLE}{notice.STYLE}</style>",
             persona.greeting_html()]
    parts.append("<h2>Events you may want to attend</h2>")

    if not candidates:
        parts.append(
            f"<p class='none'>Nothing new in the last {days_scanned} days that looked "
            "like an event worth your time.</p>"
        )
        parts.append(persona.signoff_html())
        return "Events you may want to attend - nothing new", "".join(parts)

    parts.append(
        f"<p class='intro'>Found in the last {days_scanned} days of your inbox and "
        "junk mail. <b>Yes</b> puts it on your calendar, <b>might</b> adds it as "
        "tentative, and <b>no thanks</b> deletes the email and stops me showing it "
        "to you again.</p>"
    )

    today = for_date
    for candidate in candidates:
        starts = candidate.starts_at()
        soon = ""
        if starts and 0 <= (starts.date() - today).days <= 7:
            soon = "<span class='soon'>THIS WEEK</span>"
        free = ("<span class='free'>FREE</span>"
                if (candidate.cost or "").strip().lower() == "free" else "")

        rows = []
        where = candidate.location or ("Online" if candidate.online else "")
        if where:
            rows.append(f"<div class='row'><b>Where</b> {_esc(where)}</div>")
        if candidate.cost and candidate.cost.strip().lower() != "free":
            rows.append(f"<div class='row'><b>Cost</b> {_esc(candidate.cost)}</div>")

        parts.append(
            "<div class='evt'>"
            f"<div class='name'>{_esc(candidate.title)}{free}{soon}</div>"
            f"<div class='when'>{_esc(_when(candidate))}</div>"
            + "".join(rows)
            + (f"<div class='why'>{_esc(candidate.why)}</div>" if candidate.why else "")
            + f"<div class='src'>From: {_esc(candidate.source_from)}"
              f"{' &middot; found in Junk' if candidate.source_folder == 'junk' else ''}"
              f" &middot; {_esc(candidate.source_subject)}</div>"
            + _answers(links.get(candidate.id, {}))
            + "</div>"
        )

    parts.append(notice.html())
    parts.append(persona.signoff_html())
    return f"Events you may want to attend ({len(candidates)})", "".join(parts)
