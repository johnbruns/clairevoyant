"""The do-not-forward warning.

Every email this assistant sends with buttons in it carries live, signed URLs
that will send mail, write to Jira, or move messages on Alex's behalf. They are
valid for a week. Forwarding one hands all of that to whoever receives it -
which is the same authority as the mailbox, arriving in an inbox that was never
supposed to have it.

The task digest carries no buttons but is no safer: its subject line holds the
marker that lets a reply drive Jira, so a forwarded digest is a working handle
on the board for anyone who quotes it back.

One line, quiet, at the foot of the mail. Loud enough to read once, small
enough not to be the thing you see first every single morning.
"""

from __future__ import annotations

STYLE = """
.noforward{font-size:11.5px;color:#92400e;background:#fffbeb;
border:1px solid #fde68a;border-radius:5px;padding:8px 11px;margin:18px 0 0;
line-height:1.45}
.noforward b{color:#78350f;font-weight:700}
"""

BUTTONS = (
    "Please don't forward this email. The buttons in it are live, signed links "
    "that act as you for the next seven days - anyone holding this message can "
    "press them."
)

MARKER = (
    "Please don't forward this email. Replying to it updates Jira, and the tag "
    "in the subject line works for whoever quotes it back."
)


def html(text: str = BUTTONS, css_class: str = "noforward") -> str:
    return f"<div class='{css_class}'><b>Keep this one to yourself.</b> {text}</div>"
