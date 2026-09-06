"""The Monday task digest, the team digest, and the reply confirmation.

Kept out of recap.py, which is already the inbox's whole user interface. These
share its stylesheet so the four emails look like one assistant, but nothing
else.

The task digest is the only email the assistant sends that expects a reply, so
it says so plainly at the top rather than relying on Alex remembering. Its
subject carries the digest marker; without that a reply cannot be matched back
to its issues.
"""

from __future__ import annotations

import datetime as dt
import html

from . import persona
from .availability import long_date
from .digests import marker_tag
from .recap import STYLE

EXTRA_STYLE = """
.grp{margin:18px 0 6px;font-size:13px;font-weight:700;color:#334155;
letter-spacing:.03em;text-transform:uppercase}
.task{border-left:3px solid #cbd5e1;padding:4px 0 4px 12px;margin:0 0 10px}
.task.over{border-left-color:#dc2626}
.task.soon{border-left-color:#d97706}
.task .k{font-weight:600;text-decoration:none;color:#2563eb}
.task .s{color:#1a1a1a}
.tag{display:inline-block;font-size:12px;color:#64748b;margin-left:6px}
.tag.red{color:#b91c1c;font-weight:600}
.how{background:#f0f5ff;border-radius:6px;padding:12px 14px;margin:0 0 18px;font-size:14px}
.how code{background:#fff;border:1px solid #dbe3f0;border-radius:3px;padding:1px 5px}
.person{margin:20px 0 4px;font-size:16px;font-weight:700}
.count{color:#64748b;font-weight:400;font-size:14px}
.did{color:#15803d}
.none2{color:#94a3b8;font-size:14px;margin:2px 0 0}
.psum{color:#334155;font-size:14px;line-height:1.5;margin:2px 0 10px;padding-left:10px;border-left:2px solid #cbd5e1}
.psum.warn{border-left-color:#d97706;color:#7c2d12}
.grp.warn{color:#b45309}
.overall-label{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#64748b;margin:0 0 4px}
li{margin:2px 0}
"""


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def _style() -> str:
    return f"<style>{STYLE}{EXTRA_STYLE}{persona.STYLE}</style>"


def _due_tag(issue, today: dt.date) -> str:
    if not issue.due:
        return ""
    try:
        due = dt.date.fromisoformat(issue.due)
    except (ValueError, TypeError):
        return ""
    delta = (due - today).days
    if delta < 0:
        return f"<span class='tag red'>overdue by {abs(delta)}d</span>"
    if delta == 0:
        return "<span class='tag red'>due today</span>"
    if delta <= 7:
        return f"<span class='tag'>due {due.strftime('%a')}</span>"
    return f"<span class='tag'>due {due.isoformat()}</span>"


def _urgency(issue, today: dt.date) -> str:
    if not issue.due:
        return ""
    try:
        delta = (dt.date.fromisoformat(issue.due) - today).days
    except (ValueError, TypeError):
        return ""
    if delta < 0:
        return " over"
    return " soon" if delta <= 2 else ""


def _task_line(issue, today: dt.date) -> str:
    key = (
        f"<a class='k' href='{html.escape(issue.url, quote=True)}'>{_esc(issue.key)}</a>"
        if issue.url
        else f"<span class='k'>{_esc(issue.key)}</span>"
    )
    priority = (
        f"<span class='tag'>{_esc(issue.priority)}</span>"
        if issue.priority and issue.priority.lower() not in ("medium", "none")
        else ""
    )
    return (
        f"<div class='task{_urgency(issue, today)}'>{key} "
        f"<span class='s'>{_esc(issue.summary)}</span>"
        f"{_due_tag(issue, today)}{priority}</div>"
    )


def _group(issues: list, today: dt.date) -> dict[str, list]:
    """Overdue first, then dated, then the rest. Status is secondary.

    Grouping by status would put an overdue item three groups down just because
    nobody has started it, which is precisely backwards.
    """
    overdue, this_week, later, undated = [], [], [], []
    for issue in issues:
        if not issue.due:
            undated.append(issue)
            continue
        try:
            delta = (dt.date.fromisoformat(issue.due) - today).days
        except (ValueError, TypeError):
            undated.append(issue)
            continue
        (overdue if delta < 0 else this_week if delta <= 7 else later).append(issue)

    in_progress = [i for i in undated if i.status_category == "In Progress"]
    rest = [i for i in undated if i.status_category != "In Progress"]

    return {
        "Overdue": overdue,
        "Due this week": this_week,
        "In progress": in_progress,
        "Due later": later,
        "Everything else": rest,
    }


def build_task_digest(
    issues: list,
    marker: str,
    for_date: dt.date,
    project_key: str = "TASK",
) -> tuple[str, str]:
    """Return (subject, html) for the Monday "everything on your plate" email."""
    parts = [_style(), persona.greeting_html()]
    parts.append(f"<h2>Your tasks &mdash; {_esc(long_date(for_date))}</h2>")

    if not issues:
        parts.append(f"<p class='none'>Nothing open in {_esc(project_key)}. "
                     "Either you are genuinely clear, or the board is out of date.</p>")
        return f"Your tasks - nothing open {marker_tag(marker)}", "".join(parts)

    parts.append(
        "<div class='how'><b>Reply to this email to update Jira.</b> Plain "
        "English is fine &mdash; <code>did the MOU one</code>, "
        "<code>TASK-41 done</code>, <code>started the grant letter</code>, "
        "<code>push TASK-12 to Friday</code>, "
        "<code>note on TASK-8: waiting on Dana</code>. "
        "You will get a confirmation of exactly what changed.</div>"
    )

    groups = _group(issues, for_date)
    for label, group in groups.items():
        if not group:
            continue
        parts.append(f"<div class='grp'>{_esc(label)} ({len(group)})</div>")
        parts.extend(_task_line(i, for_date) for i in group)

    overdue = len(groups["Overdue"])
    parts.append(
        f"<div class='foot'>{len(issues)} open in {_esc(project_key)}"
        + (f" &middot; {overdue} overdue" if overdue else "")
        + f" &middot; {marker_tag(marker)}</div>"
    )

    subject = f"Your tasks - {len(issues)} open"
    if overdue:
        subject += f", {overdue} overdue"
    return f"{subject} {marker_tag(marker)}", "".join(parts)


def build_reply_confirmation(result, when: dt.datetime) -> tuple[str, str]:
    """Return (subject, html) confirming what a reply actually changed."""
    parts = [_style(), persona.greeting_html()]

    if result.error:
        parts.append("<h2>Nothing was changed</h2>")
        parts.append(f"<p class='none'>{_esc(result.error)}</p>")
        return "Task update - nothing changed", "".join(parts)

    applied, failed = result.applied, result.failed
    created = [o for o in applied if o.action == "create"]
    changed = [o for o in applied if o.action != "create"]

    parts.append(f"<h2>Updated {len(changed)} task(s)"
                 + (f", created {len(created)}" if created else "")
                 + "</h2>")

    if changed:
        parts.append("<ul>")
        for op in changed:
            parts.append(
                f"<li><b>{_esc(op.key)}</b> &mdash; {_esc(op.outcome)}"
                f"<span class='tag'>{_esc(op.note)}</span></li>"
            )
        parts.append("</ul>")
    elif not created:
        parts.append("<p class='none'>Nothing was applied.</p>")

    if created:
        parts.append("<div class='grp'>New tasks</div><ul>")
        for op in created:
            link = (
                f"<a class='k' href='{html.escape(op.created_url, quote=True)}'>"
                f"{_esc(op.created_key)}</a>"
                if op.created_url
                else f"<b>{_esc(op.created_key)}</b>"
            )
            extra = f" <span class='tag'>due {_esc(op.due)}</span>" if op.due else ""
            note = (
                f" <span class='tag'>{_esc(op.outcome)}</span>"
                if op.outcome != "created"
                else ""
            )
            parts.append(f"<li>{link} {_esc(op.value)}{extra}{note}</li>")
        parts.append("</ul>")

    if failed:
        parts.append(f"<div class='grp'>Could not apply ({len(failed)})</div><ul>")
        for op in failed:
            label = op.key or f"new task: {op.value[:60]}"
            parts.append(
                f"<li><b>{_esc(label)}</b> &mdash; {_esc(op.action)}: "
                f"{_esc(op.outcome or 'no reason given')}</li>"
            )
        parts.append("</ul>")

    if result.skipped:
        parts.append(f"<div class='grp'>Left alone ({len(result.skipped)})</div><ul>")
        parts.extend(f"<li>{_esc(s)}</li>" for s in result.skipped)
        parts.append("</ul>")

    parts.append(
        "<div class='foot'>Reply to your task list again any time. "
        "Anything wrong here is one click to undo in Jira.</div>"
    )

    subject = f"Task update - {len(changed)} changed"
    if created:
        subject += f", {len(created)} created"
    if failed or result.skipped:
        subject += f", {len(failed) + len(result.skipped)} not"
    return subject, "".join(parts)


def build_team_digest(
    sections: list,
    for_date: dt.date,
    days: int = 7,
    summary: str = "",
    summary_failed: bool = False,
) -> tuple[str, str]:
    """Return (subject, html) for the team digest.

    `sections` is a list of TeamSection (see planning.team_sections).
    """
    parts = [_style(), persona.greeting_html()]
    parts.append(f"<h2>Team &mdash; last {days} days to {_esc(long_date(for_date))}</h2>")

    if summary:
        parts.append("<div class='overall-label'>Across the team</div>")
        parts.append(f"<div class='focus'>{_esc(summary)}</div>")
    elif summary_failed:
        # Say the prose is missing rather than quietly shipping bare lists.
        # A digest that silently loses half its content looks like a digest
        # that had nothing to say.
        parts.append(
            "<p class='none2'>The written summaries could not be generated for "
            "this run - the lists below are complete and unaffected.</p>"
        )

    total_done = sum(len(s.done) for s in sections)

    for section in sections:
        parts.append(f"<div class='person'>{_esc(section.name)} "
                     f"<span class='count'>&mdash; {len(section.done)} finished, "
                     f"{section.open_count} open</span></div>")

        if section.error:
            parts.append(f"<p class='none2'>Could not read: {_esc(section.error)}</p>")
            continue

        # Labelled, because unlabelled prose blocks are indistinguishable from
        # each other and from the team paragraph above them.
        if section.recap:
            parts.append("<div class='grp'>Where it stands</div>")
            parts.append(f"<div class='psum'>{_esc(section.recap)}</div>")
        if section.attention:
            parts.append("<div class='grp warn'>Needs attention</div>")
            parts.append(f"<div class='psum warn'>{_esc(section.attention)}</div>")

        if section.done:
            parts.append("<div class='grp did'>Finished</div><ul>")
            for issue in section.done:
                parts.append(f"<li><b>{_esc(issue.key)}</b> {_esc(issue.summary)}</li>")
            parts.append("</ul>")
        else:
            parts.append("<p class='none2'>Nothing closed this week.</p>")

        if section.open:
            # section.shown of 0 means "all of them". Alex asked for the
            # complete list: a truncated one is a list he has to leave the
            # email to finish reading.
            shown = section.open[: section.shown] if section.shown else section.open
            parts.append(f"<div class='grp'>Still to do ({section.open_count})</div><ul>")
            for issue in shown:
                due = f" <span class='tag'>due {_esc(issue.due)}</span>" if issue.due else ""
                status = (
                    f" <span class='tag'>{_esc(issue.status)}</span>"
                    if issue.status_category == "In Progress"
                    else ""
                )
                parts.append(
                    f"<li><b>{_esc(issue.key)}</b> {_esc(issue.summary)}{status}{due}</li>"
                )
            parts.append("</ul>")
            hidden = section.open_count - len(shown)
            if hidden > 0:
                parts.append(f"<p class='none2'>&hellip; and {hidden} more.</p>")

    parts.append(
        f"<div class='foot'>{total_done} item(s) finished across "
        f"{len(sections)} people in the last {days} days.</div>"
    )

    return f"Team - {total_done} finished last week", "".join(parts)
