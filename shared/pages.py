"""The web pages behind the recap buttons.

Four pages, all server-rendered, no JavaScript, no external assets. They open
on a phone from an email client's in-app browser as often as on a desktop, and
a page that needs a CDN to render is a page that sometimes does not.

The GET/POST split is the whole security design and is worth stating plainly:

    GET  /api/draft?t=...   renders a confirmation page. Changes NOTHING.
    POST /api/draft         performs the action.

Outlook Safe Links, Defender for Office 365 and ordinary antivirus all follow
links in email with a GET, sometimes within seconds of delivery. If "approve
and send" sent on GET, every draft would be sent by the scanner before Alex
ever saw the recap. The confirmation page costs one extra click and is the
difference between a working feature and a catastrophe.

`no-store` and a no-referrer policy are set on every response so the token
does not end up in a shared cache or leak to another origin through Referer.
"""

from __future__ import annotations

import html

STYLE = """
*{box-sizing:border-box}
body{font:16px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
color:#1a1a1a;background:#f4f5f7;margin:0;padding:24px 16px}
.card{max-width:640px;margin:0 auto;background:#fff;border-radius:10px;
padding:24px;box-shadow:0 1px 3px rgba(0,0,0,.12)}
h1{font-size:19px;margin:0 0 4px}
.sub{color:#666;font-size:14px;margin:0 0 20px}
.meta{color:#666;font-size:14px}
.label{font-size:12px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;
color:#888;margin:20px 0 6px}
.quote{background:#f6f7f9;border-left:3px solid #d0d0d0;border-radius:0 4px 4px 0;
padding:10px 12px;font-size:15px}
.body{background:#f6f7f9;border-radius:6px;padding:14px;white-space:pre-wrap;font-size:15px}
textarea{width:100%;min-height:260px;font:15px/1.5 -apple-system,BlinkMacSystemFont,
'Segoe UI',sans-serif;padding:12px;border:1px solid #cfd3d8;border-radius:6px;resize:vertical}
.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}
button{font:600 15px/1 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
padding:13px 20px;border-radius:7px;border:0;cursor:pointer}
.go{background:#2563eb;color:#fff}
.danger{background:#dc2626;color:#fff}
.ghost{background:#fff;color:#333;border:1px solid #cfd3d8}
a.ghost{display:inline-block;text-decoration:none;padding:13px 20px;border-radius:7px}
.ok{color:#15803d;font-weight:600}
.bad{color:#b91c1c;font-weight:600}
.foot{color:#999;font-size:13px;margin-top:22px;border-top:1px solid #eee;padding-top:14px}
"""


def _esc(text: str | None) -> str:
    return html.escape(text or "", quote=False)


def _attr(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def _page(title: str, inner: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='referrer' content='no-referrer'>"
        f"<title>{_esc(title)}</title><style>{STYLE}</style></head>"
        f"<body><div class='card'>{inner}</div></body></html>"
    )


def _who(draft) -> str:
    name = _esc(draft.sender_name or draft.sender_address or "(unknown sender)")
    addr = _esc(draft.sender_address)
    return f"<b>{name}</b>" + (f" <span class='meta'>&lt;{addr}&gt;</span>" if addr else "")


def _context(draft) -> str:
    """Sender, subject and their actual ask - the same three facts as the recap."""
    out = [f"<p class='sub'>Reply to {_who(draft)}</p>"]
    if draft.subject:
        out.append(f"<div class='label'>Subject</div><div class='meta'>{_esc(draft.subject)}</div>")
    if draft.the_ask:
        out.append(f"<div class='label'>What they asked</div>"
                   f"<div class='quote'>{_esc(draft.the_ask)}</div>")
    return "".join(out)


def confirm_send(draft, token: str) -> str:
    inner = (
        "<h1>Send this reply?</h1>"
        + _context(draft)
        + f"<div class='label'>Your reply</div><div class='body'>{_esc(draft.body)}</div>"
        + "<form method='post'>"
        + f"<input type='hidden' name='t' value='{_attr(token)}'>"
        + "<input type='hidden' name='confirm' value='send'>"
        + "<div class='row'>"
        + "<button class='go' type='submit'>Send it now</button>"
        + f"<button class='ghost' type='submit' name='confirm' value='to_edit'>Edit first</button>"
        + "</div></form>"
        + "<div class='foot'>Nothing has been sent yet. This page is the only place "
        "the send actually happens.</div>"
    )
    return _page("Send this reply?", inner)


def confirm_edit(draft, token: str) -> str:
    inner = (
        "<h1>Edit before sending</h1>"
        + _context(draft)
        + "<form method='post'>"
        + f"<input type='hidden' name='t' value='{_attr(token)}'>"
        + "<div class='label'>Your reply</div>"
        + f"<textarea name='body'>{_esc(draft.body)}</textarea>"
        + "<div class='row'>"
        + "<button class='go' type='submit' name='confirm' value='send'>Send it now</button>"
        + "<button class='ghost' type='submit' name='confirm' value='save'>Save, don't send</button>"
        + "</div></form>"
        + "<div class='foot'>Saving keeps the draft here and leaves the buttons in your "
        "recap working. Nothing is sent until you press Send.</div>"
    )
    return _page("Edit before sending", inner)


def confirm_discard(draft, token: str) -> str:
    inner = (
        "<h1>Discard this draft?</h1>"
        + _context(draft)
        + f"<div class='label'>Your reply</div><div class='body'>{_esc(draft.body)}</div>"
        + "<form method='post'>"
        + f"<input type='hidden' name='t' value='{_attr(token)}'>"
        + "<div class='row'>"
        + "<button class='danger' type='submit' name='confirm' value='discard'>"
        "Discard it</button>"
        + "<button class='ghost' type='submit' name='confirm' value='to_edit'>Keep and edit"
        "</button>"
        + "</div></form>"
        + "<div class='foot'>The original message stays in your inbox either way. "
        "Only the drafted reply is thrown away.</div>"
    )
    return _page("Discard this draft?", inner)


def confirm_task_done(issue_key: str, token: str) -> str:
    inner = (
        "<h1>Mark this task done?</h1>"
        f"<p class='sub'>{_esc(issue_key)}</p>"
        "<form method='post'>"
        f"<input type='hidden' name='t' value='{_attr(token)}'>"
        "<div class='row'>"
        "<button class='go' type='submit' name='confirm' value='done'>"
        "Yes, mark it done</button>"
        "</div></form>"
        "<div class='foot'>This moves the issue to Done in Jira. One click to "
        "undo there if it was a mistake.</div>"
    )
    return _page("Mark task done?", inner)


def confirm_block_time(label: str, token: str) -> str:
    """Ask what the time is FOR before blocking it.

    A nameless "Busy" block is one Alex will look at next week with no idea why
    he made it, so the reason is required rather than optional.
    """
    inner = (
        "<h1>Block this time?</h1>"
        f"<p class='sub'>{_esc(label)}</p>"
        "<form method='post'>"
        f"<input type='hidden' name='t' value='{_attr(token)}'>"
        "<div class='label'>What is it for?</div>"
        "<input name='reason' required autofocus placeholder='Grant letter, "
        "deep work, lunch...' style='width:100%;padding:12px;font-size:15px;"
        "border:1px solid #cfd3d8;border-radius:6px'>"
        "<div class='row'>"
        "<button class='go' type='submit' name='confirm' value='block'>"
        "Block it</button>"
        "</div></form>"
        "<div class='foot'>Creates a private event on your calendar with no "
        "attendees, so nobody is invited and nobody is notified.</div>"
    )
    return _page("Block this time?", inner)


def confirm_event(candidate, action: str, token: str) -> str:
    """One page for all three answers; only the verb and the button change."""
    heading, button, colour = {
        "event_yes": ("Add this to your calendar?", "Yes, add it", "go"),
        "event_maybe": ("Add this as tentative?", "Add as tentative", "go"),
        "event_no": ("Not interested?", "Delete it and hide it", "danger"),
    }[action]

    detail = []
    if candidate.start:
        detail.append(f"<div class='label'>When</div>"
                      f"<div class='meta'>{_esc(candidate.start.replace('T', ' at '))}</div>")
    where = candidate.location or ("Online" if candidate.online else "")
    if where:
        detail.append(f"<div class='label'>Where</div><div class='meta'>{_esc(where)}</div>")

    footer = {
        "event_yes": "Goes on your calendar as busy. No invitations are sent to anyone.",
        "event_maybe": ("Goes on as TENTATIVE, annotated \"Thinking about it\", so it "
                        "holds the slot without committing you."),
        "event_no": ("The email it came from is moved to Deleted Items, and this event "
                     "will not be offered again."),
    }[action]

    inner = (
        f"<h1>{_esc(heading)}</h1>"
        f"<p class='sub'>{_esc(candidate.title)}</p>"
        + "".join(detail)
        + "<form method='post'>"
        + f"<input type='hidden' name='t' value='{_attr(token)}'>"
        + "<div class='row'>"
        + f"<button class='{colour}' type='submit' name='confirm' value='ok'>"
        + f"{_esc(button)}</button>"
        + "</div></form>"
        + f"<div class='foot'>{footer}</div>"
    )
    return _page(heading, inner)


def confirm_junk(candidate, action: str, token: str) -> str:
    """Confirm a move or a delete, saying exactly where the message ends up.

    These three are the only buttons in the assistant that relocate real mail,
    so the footer states the destination rather than describing the intent.
    """
    heading, button, colour = {
        "junk_rescue": ("Move this back to your inbox?", "Move to inbox", "go"),
        "junk_calendar": ("Move it back and put it on your calendar?",
                          "Move it and add the event", "go"),
        "junk_block": ("Block this sender?", "Block and delete", "danger"),
    }[action]

    detail = []
    if candidate.sender_address:
        detail.append("<div class='label'>From</div>"
                      f"<div class='meta'>{_esc(candidate.sender_name)} "
                      f"&lt;{_esc(candidate.sender_address)}&gt;</div>")
    if candidate.why:
        detail.append("<div class='label'>Why I kept it back</div>"
                      f"<div class='quote'>{_esc(candidate.why)}</div>")
    if action == "junk_calendar" and candidate.is_event:
        starts = candidate.starts_at()
        detail.append("<div class='label'>Event</div>"
                      f"<div class='meta'>{_esc(candidate.event_title or candidate.subject)}"
                      + (f" &middot; {_esc(starts.strftime('%A, %B %d'))}" if starts else "")
                      + "</div>")

    footer = {
        "junk_rescue": ("The message moves to your inbox, unread, and survives "
                        "the 5:00 PM sweep."),
        "junk_calendar": ("The message moves to your inbox and the event goes on "
                          "your calendar as busy. No invitation is sent to anyone."),
        "junk_block": ("The message moves to Deleted Items, where it is "
                       "recoverable for thirty days, and I will never surface "
                       "that sender to you again. This does not change Outlook's "
                       "own blocked-senders list."),
    }[action]

    inner = (
        f"<h1>{_esc(heading)}</h1>"
        f"<p class='sub'>{_esc(candidate.subject)}</p>"
        + "".join(detail)
        + "<form method='post'>"
        + f"<input type='hidden' name='t' value='{_attr(token)}'>"
        + "<div class='row'>"
        + f"<button class='{colour}' type='submit' name='confirm' value='ok'>"
        + f"{_esc(button)}</button>"
        + "</div></form>"
        + f"<div class='foot'>{footer}</div>"
    )
    return _page(heading, inner)


def result(headline: str, detail: str = "", good: bool = True) -> str:
    tone = "ok" if good else "bad"
    inner = (
        f"<h1 class='{tone}'>{_esc(headline)}</h1>"
        + (f"<p class='sub'>{_esc(detail)}</p>" if detail else "")
        + "<div class='foot'>You can close this tab.</div>"
    )
    return _page(headline, inner)


def already_resolved(draft) -> str:
    """A second click on a link that has already been used.

    Deliberately not an error page. The usual cause is Alex clicking twice, or
    an inbox rule following the link, and telling him what already happened is
    more useful than telling him he did something wrong.
    """
    what = {
        "sent": "That reply has already been sent.",
        "discarded": "That draft was already discarded.",
    }.get(draft.status, "That draft has already been dealt with.")
    return result(what, "Nothing changed just now.", good=True)


def gone() -> str:
    return result(
        "That draft is no longer available.",
        "Drafts are kept for a week. The original message is still in your inbox.",
        good=False,
    )


def error(message: str) -> str:
    return result("Something went wrong.", message, good=False)
