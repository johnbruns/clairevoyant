"""Recap and agenda emails.

The recap is the assistant's whole user interface. Every decision it made is
visible here with the reasoning attached, which is what makes the thing
diagnosable without opening Azure. Drafts are shown for approval; nothing is
sent on Alex's behalf automatically.

The recap is now conditional. It is sent only when something in it is Alex's to
act on - see `is_actionable`. An hourly job that mails "0 need a reply" ten
times a working day trains the reader to archive it unread, which costs more
than the one run it would have been useful.
"""

from __future__ import annotations

import datetime as dt
import html
import os
from zoneinfo import ZoneInfo

from . import notice, persona
from .availability import Slot, long_date, merge_contiguous
from .planning import DayPlan
from .triage import Verdict

STYLE = """
body{font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1a1a1a;margin:0;padding:16px}
h2{font-size:17px;margin:24px 0 8px}
h2:first-child{margin-top:0}
.msg{border-left:3px solid #d0d0d0;padding:2px 0 2px 12px;margin:0 0 18px}
.msg.reply{border-left-color:#2563eb}
.msg.err{border-left-color:#dc2626}
.msg.susp{border-left-color:#d97706}
.from{font-weight:600}
.subj{color:#333}
.lead{font-size:15px;font-weight:700;color:#0f172a;margin:0 0 6px}
.line{color:#333;font-size:14px;margin-top:2px}
.line b{color:#555;font-weight:600}
.meta{color:#666;font-size:13px;margin-top:2px}
.why{color:#444;font-size:13px;font-style:italic;margin-top:4px}
.label{font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:#888;margin:12px 0 4px}
.quote{background:#fff;border-left:3px solid #94a3b8;border-radius:0 4px 4px 0;padding:8px 10px;font-size:14px;color:#222}
.draft{background:#f6f7f9;border-radius:4px;padding:10px;margin-top:8px;white-space:pre-wrap;font-size:14px}
.saved{color:#15803d;font-size:13px;margin-top:6px}
.notsaved{color:#b45309;font-size:13px;margin-top:6px}
a.open{font-weight:600;text-decoration:none;color:#2563eb}
.none{color:#666}
ul{margin:4px 0;padding-left:20px}
.focus{background:#f0f5ff;border-radius:4px;padding:10px 12px;margin:4px 0 12px}
.pick{margin:0 0 12px}
.pick .key{font-weight:600;text-decoration:none;color:#2563eb}
.foot{color:#888;font-size:12px;margin-top:28px;border-top:1px solid #e5e5e5;padding-top:10px}
"""

# A prompt-injection attempt is worth knowing about but is not a task, and the
# whole point of the new send rule is that Alex only hears from the assistant
# when there is something to do. Flip this on to be told anyway.
SUSPICIOUS_IS_ACTIONABLE = False

# The name is Alex's, not the assistant's - persona.NAME is Claire. Same env
# var function_app.py uses for the agenda greeting, so the two never drift.
USER_NAME = os.environ.get("ASSISTANT_USER_NAME", "Alex")
GREETING = f"Hi {USER_NAME}, I think these emails need a reply!"


def _run_stamp(when: dt.datetime) -> str:
    """"9:30am edt on wednesday, september 2" - the recap footer timestamp.

    Hand-built for the same reason as long_date: "%-I" and "%-d" are glibc
    extensions and raise ValueError on Windows.
    """
    hour = when.hour % 12 or 12
    return (
        f"{hour}:{when.strftime('%M%p')} {when.strftime('%Z')} on {long_date(when.date())}"
    ).lower()


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def _sender(message: dict) -> tuple[str, str]:
    node = ((message.get("from") or {}).get("emailAddress") or {})
    return node.get("name") or node.get("address") or "(unknown)", node.get("address") or ""


def is_actionable(
    verdicts: list[Verdict],
    suspicious_counts: bool = SUSPICIOUS_IS_ACTIONABLE,
) -> bool:
    """Whether this run produced anything Alex has to do.

    Three things qualify. A message owing a reply is the obvious one - whether
    or not a draft was written for it, since the ones without drafts are exactly
    the ones only he can answer. A classification error qualifies because a
    message the assistant could not read might have been the important one, and
    silence there would be the assistant hiding its own failure.
    """
    if any(v.reply_owed and not v.error for v in verdicts):
        return True
    if any(v.error for v in verdicts):
        return True
    if suspicious_counts and any(v.category == "suspicious" for v in verdicts):
        return True
    return False


def _button(url: str, label: str, background: str, colour: str = "#ffffff") -> str:
    """One bulletproof email button.

    Table-and-bgcolor rather than a styled <a>: Outlook on Windows renders mail
    through Word, which drops padding and background on inline elements. A
    button that collapses to blue underlined text in the one client Alex
    actually uses is not a button.
    """
    return (
        "<td bgcolor='" + background + "' style='border-radius:6px'>"
        f"<a href='{html.escape(url, quote=True)}' "
        "style='display:inline-block;padding:12px 18px;font-family:-apple-system,"
        "BlinkMacSystemFont,Segoe UI,sans-serif;font-size:14px;font-weight:600;"
        f"color:{colour};text-decoration:none;border-radius:6px'>{_esc(label)}</a></td>"
    )


def _action_row(links: dict[str, str], v: Verdict | None = None) -> str:
    """Approve / edit / discard / review, in that order of likelihood."""
    cells = []
    if links.get("send"):
        cells.append(_button(links["send"], "Approve and send", "#2563eb"))
    if links.get("edit"):
        cells.append(_button(links["edit"], "Edit", "#ffffff", colour="#333333"))
    if links.get("discard"):
        cells.append(_button(links["discard"], "Do not send - delete", "#dc2626"))

    # Someone asking you to look at something they attached is the one case
    # where approving blind is a bad idea, so the way to look at it sits in
    # the same row as the buttons rather than somewhere you have to go find.
    if v is not None and v.attachment_url:
        label = "Review Attachment" if v.attachment_direct else "Review Attachment in Email"
        cells.append(_button(v.attachment_url, label, "#0f766e"))

    if not cells:
        return ""

    spacer = "<td style='width:8px'>&nbsp;</td>"
    return (
        "<table role='presentation' cellpadding='0' cellspacing='0' border='0' "
        "style='margin-top:12px'><tr>" + spacer.join(cells) + "</tr></table>"
    )


def _ask_block(v: Verdict) -> str:
    if not v.the_ask:
        return ""
    return f"<div class='label'>What they asked</div><div class='quote'>{_esc(v.the_ask)}</div>"


def _draft_block(v: Verdict, links: dict[str, str] | None = None) -> str:
    """The drafted reply plus the three buttons that resolve it.

    The buttons are the point: Alex should never have to open the Drafts folder
    to deal with a drafted reply. When they cannot be built - no signing key -
    the block degrades to the old behaviour rather than disappearing.
    """
    if not v.draft:
        if v.request_type in ("other", "none"):
            return (
                "<div class='meta'>No draft - not a meeting ask or a request, "
                "so this one is yours to answer.</div>"
            )
        return "<div class='meta'>No draft generated.</div>"

    parts = [f"<div class='label'>Drafted reply</div><div class='draft'>{_esc(v.draft)}</div>"]

    row = _action_row(links or {}, v)
    if row:
        parts.append(row)
        if v.draft_saved and v.draft_link:
            parts.append(
                f"<div class='meta' style='margin-top:8px'>Also in your Drafts folder &middot; "
                f"<a class='open' href='{html.escape(v.draft_link, quote=True)}'>"
                "open in Outlook</a></div>"
            )
    elif v.draft_saved and v.draft_link:
        parts.append(
            f"<div class='saved'>&#10003; Saved to your Drafts folder &middot; "
            f"<a class='open' href='{html.escape(v.draft_link, quote=True)}'>"
            "Open in Outlook</a></div>"
        )
    else:
        parts.append(
            "<div class='notsaved'>Buttons unavailable - copy the text above, "
            "or check ACTION_SIGNING_KEY.</div>"
        )

    return "".join(parts)


_REQUEST_LABEL = {
    "meeting": "wants to meet",
    "request": "asking you for something",
    "other": "reply owed, nothing asked",
    "none": "",
}


def _lead(v: Verdict) -> str:
    """The line that opens every card.

    "Needs Something" is the point of the whole recap and is worth saying in
    those words. It would be a lie on a message that asks for nothing, though,
    and a card that overstates what is wanted is how you learn to skim them -
    so those say what is actually true instead.
    """
    if v.request_type in ("meeting", "request"):
        return "Needs Something"
    return "Reply Owed"


def build_recap(
    verdicts: list[Verdict],
    messages_by_id: dict[str, dict],
    ran_at: dt.datetime,
    timezone: str = "America/New_York",
    added_contacts: list[str] | None = None,
    action_links: dict[str, dict[str, str]] | None = None,
) -> tuple[str, str]:
    """Return (subject, html_body) for the approval recap.

    action_links maps a message id to {"send": url, "edit": url,
    "discard": url}. Absent, the recap renders exactly as it did before the
    buttons existed.
    """
    action_links = action_links or {}
    tz = ZoneInfo(timezone)
    ran_at = ran_at.astimezone(tz)

    needs_reply = [v for v in verdicts if v.reply_owed and not v.error]
    suspicious = [v for v in verdicts if v.category == "suspicious"]
    errors = [v for v in verdicts if v.error]
    fyi = [
        v
        for v in verdicts
        if not v.reply_owed and not v.error and v.category != "suspicious"
        and v.sender_type == "human"
    ]
    automated = [v for v in verdicts if v.sender_type == "automated" and not v.error]

    drafted = [v for v in needs_reply if v.draft]
    undrafted = [v for v in needs_reply if not v.draft]

    parts = [f"<style>{STYLE}{persona.STYLE}{notice.STYLE}</style>",
             persona.greeting_html()]

    parts.append(f"<h2>{_esc(GREETING)}</h2>")
    if not needs_reply:
        parts.append("<p class='none'>Nothing waiting on you.</p>")
    # Drafted first: those are one click from done.
    for v in drafted + undrafted:
        msg = messages_by_id.get(v.message_id, {})
        name, addr = _sender(msg)
        who = f"{name} &lt;{_esc(addr)}&gt;" if addr else _esc(name)
        parts.append(
            f"<div class='msg reply'>"
            f"<div class='lead'>{_lead(v)}</div>"
            f"<div class='line'><b>Sender:</b> {_esc(name)}"
            + (f" &lt;{_esc(addr)}&gt;" if addr else "")
            + "</div>"
            f"<div class='line'><b>Subject:</b> {_esc(msg.get('subject'))}</div>"
            + _ask_block(v)
            + _draft_block(v, action_links.get(v.message_id))
            + "</div>"
        )

    if suspicious:
        parts.append(f"<h2>Flagged as suspicious ({len(suspicious)})</h2>")
        parts.append(
            "<p class='meta'>Content in these messages tried to instruct the assistant. "
            "No drafts were produced for them.</p>"
        )
        for v in suspicious:
            msg = messages_by_id.get(v.message_id, {})
            name, addr = _sender(msg)
            parts.append(
                f"<div class='msg susp'>"
                f"<div><span class='from'>{_esc(name)}</span> "
                f"<span class='meta'>&lt;{_esc(addr)}&gt;</span></div>"
                f"<div class='subj'>{_esc(msg.get('subject'))}</div>"
                f"<div class='why'>{_esc(v.reasoning)}</div></div>"
            )

    if errors:
        parts.append(f"<h2>Could not classify ({len(errors)})</h2>")
        parts.append(
            "<p class='meta'>Surfaced rather than dropped - one of these may have needed "
            "an answer.</p>"
        )
        for v in errors:
            msg = messages_by_id.get(v.message_id, {})
            name, addr = _sender(msg)
            parts.append(
                f"<div class='msg err'>"
                f"<div><span class='from'>{_esc(name)}</span> "
                f"<span class='meta'>&lt;{_esc(addr)}&gt;</span></div>"
                f"<div class='subj'>{_esc(msg.get('subject'))}</div>"
                f"<div class='meta'>{_esc(v.error)}</div></div>"
            )

    if fyi:
        parts.append(f"<h2>From people, no reply owed ({len(fyi)})</h2><ul>")
        for v in fyi:
            msg = messages_by_id.get(v.message_id, {})
            name, _ = _sender(msg)
            parts.append(
                f"<li>{_esc(name)} - {_esc(msg.get('subject'))} "
                f"<span class='meta'>({_esc(v.reasoning)})</span></li>"
            )
        parts.append("</ul>")

    if added_contacts:
        parts.append("<h2>Added to contacts</h2><ul>")
        parts.extend(f"<li>{_esc(c)}</li>" for c in added_contacts)
        parts.append("</ul>")

    parts.append(notice.html())
    parts.append(
        f"<div class='foot'>{len(verdicts)} message(s) reviewed &middot; "
        f"{len(automated)} automated &middot; "
        f"run at {_run_stamp(ran_at)}</div>"
    )

    # Counted on whether a draft exists, not on whether Outlook holds one.
    # Drafts live in this email now, so "ready in Outlook" would have been a
    # lie in the default configuration.
    if drafted and undrafted:
        subject = (
            f"Inbox recap - {len(drafted)} ready to send, "
            f"{len(undrafted)} to answer yourself"
        )
    elif drafted:
        subject = f"Inbox recap - {len(drafted)} ready to send"
    elif needs_reply:
        subject = f"Inbox recap - {len(needs_reply)} to answer"
    else:
        # Reached only when errors are the sole reason this recap was sent.
        subject = "Inbox recap"
    if errors:
        subject += f", {len(errors)} unclassified"
    return subject, "".join(parts)


def _plan_block(plan: DayPlan | None) -> list[str]:
    """The Jira recommendations section. Absent Jira is silent, not an error."""
    if plan is None:
        return []

    parts = ["<h2>What to get done</h2>"]
    if plan.focus:
        parts.append(f"<div class='focus'>{_esc(plan.focus)}</div>")

    if plan.picks:
        for pick in plan.picks:
            key = (
                f"<a class='key' href='{html.escape(pick.url, quote=True)}'>{_esc(pick.key)}</a>"
                if pick.url
                else f"<span class='key'>{_esc(pick.key)}</span>"
            )
            parts.append(
                f"<div class='pick'>{key} &mdash; {_esc(pick.summary)}"
                f"<div class='why'>{_esc(pick.why)}</div></div>"
            )
    elif plan.note:
        parts.append(f"<p class='none'>{_esc(plan.note)}</p>")
    elif not plan.focus:
        parts.append("<p class='none'>No recommendations.</p>")

    return parts


def build_agenda(
    events: list[dict],
    free: list[Slot],
    for_date: dt.date,
    timezone: str = "America/New_York",
    plan: DayPlan | None = None,
    is_today: bool = False,
) -> tuple[str, str]:
    """Return (subject, html_body) for a day's agenda.

    Sent twice for every working day: at 3pm the day before, and at 6am on the
    day itself. The 3pm copy is for deciding what tomorrow looks like while
    there is still time to move something; the 6am copy is the one to act on.
    """
    parts = [f"<style>{STYLE}</style>"]
    when = "Today" if is_today else "Tomorrow"
    parts.append(f"<h2>{when} &mdash; {_esc(long_date(for_date))}</h2>")

    live = [e for e in events if not e.get("isCancelled")]
    if not live:
        parts.append("<p class='none'>Nothing on the calendar.</p>")
    else:
        parts.append("<ul>")
        for ev in live:
            start = (ev.get("start") or {}).get("dateTime", "")[11:16]
            end = (ev.get("end") or {}).get("dateTime", "")[11:16]
            tentative = " (tentative)" if ev.get("showAs") == "tentative" else ""
            parts.append(
                f"<li><b>{_esc(start)}-{_esc(end)}</b> {_esc(ev.get('subject'))}{tentative}</li>"
            )
        parts.append("</ul>")

    blocks = agenda_free_blocks(free, for_date)
    if blocks:
        parts.append("<h2>Open time</h2><ul>")
        parts.extend(f"<li>{_esc(b)}</li>" for b in blocks)
        parts.append("</ul>")

    parts.extend(_plan_block(plan))

    return f"{when}: {long_date(for_date)}", "".join(parts)


def agenda_free_blocks(free: list[Slot], for_date: dt.date) -> list[str]:
    """Readable open-time blocks for one day."""
    same_day = [s for s in free if s.start.date() == for_date]
    return [b.label() for b in merge_contiguous(same_day)]
