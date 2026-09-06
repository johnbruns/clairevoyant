"""Azure Functions entry points for Alex's assistant.

Schedules assume WEBSITE_TIME_ZONE=Eastern Standard Time, so the NCRONTAB
expressions below are local wall-clock and follow DST automatically. Without
that app setting they would silently be UTC and everything would run five
hours off.

NCRONTAB here is {second} {minute} {hour} {day} {month} {day-of-week}, six
fields, with Sunday as 0 in the last one. Note that this is not the five-field
crontab shape - a five-field expression will load and then run at the wrong
time, which is the single easiest mistake to make in this file.

Jobs:
  07:00-16:00 hourly, Mon-Fri   triage the inbox (also handles task replies)
  15:00       Sun-Thu           tomorrow's agenda
  06:00       Mon-Fri           today's agenda
  07:00       Mon               Alex's task list, repliable
  15:00       Sun               team digest
  08:00       Mon               team digest again
  05:45       daily             health check
Plus one HTTP trigger, draft_action, behind the recap buttons.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import os
from zoneinfo import ZoneInfo

import azure.functions as func
from anthropic import Anthropic

from shared import actions, contacts, pages
from shared.availability import (
    DEFAULT_HORIZON_DAYS,
    WorkingHours,
    format_for_email,
    free_slots,
)
from shared.graph_client import GraphError, TokenExpired, client_from_env
from shared.holidays import from_env as holidays_from_env
from shared.holidays import is_business_day
from shared.jira_client import JiraError
from shared.jira_client import client_from_env as jira_from_env
from shared.digests import TaskDigest, default_digest_store, find_marker
from shared import events as ev
from shared.eventmail import build_event_digest
from shared import junkreview as jr
from shared.junkmail import build_junk_review
from shared.pending import DISCARDED, SENT, PendingDraft, default_pending_store
from shared.planning import DayPlan, recommend_day, summarise_team, team_sections
from shared.taskmail import build_reply_confirmation, build_task_digest, build_team_digest
from shared.taskreply import apply_operations, parse_reply
from shared.team import roster
from shared.agendamail import build_agenda
from shared.recap import agenda_free_blocks, build_recap, is_actionable
from shared.state import default_processed_store
from shared.triage import classify, draft_reply, draft_to_html

log = logging.getLogger(__name__)

app = func.FunctionApp()

TIMEZONE = os.environ.get("ASSISTANT_TIMEZONE", "America/New_York")
ASSISTANT_EMAIL = os.environ.get("ASSISTANT_EMAIL", "alex@example.org")
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "48"))
MAX_DRAFTS_PER_RUN = int(os.environ.get("MAX_DRAFTS_PER_RUN", "8"))
MAX_MESSAGES_PER_RUN = int(os.environ.get("MAX_MESSAGES_PER_RUN", "50"))
# Drafts now live in the recap, not the Drafts folder. Set this to "true" to
# ALSO mirror each one into Outlook - useful if you want them reachable from a
# phone offline, at the cost of the folder filling up again, which is the
# problem the buttons were built to solve.
CREATE_OUTLOOK_DRAFTS = os.environ.get("CREATE_OUTLOOK_DRAFTS", "false").lower() == "true"
AVAILABILITY_DAYS = int(os.environ.get("AVAILABILITY_DAYS", str(DEFAULT_HORIZON_DAYS)))
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "TASK")
USER_NAME = os.environ.get("ASSISTANT_USER_NAME", "Alex")
# Board 135, read off the Agile API rather than guessed. Overridable
# because a board id changes if the project is recreated.
JIRA_BOARD_URL = os.environ.get(
    "JIRA_BOARD_URL",
    "https://your-site.atlassian.net/jira/software/projects/TASK/boards/135",
)
# How many open items per person the team digest will fetch. High enough
# that "complete list" is honest for this team; a cap still exists so one
# runaway project cannot produce a megabyte of email.
TEAM_OPEN_LIMIT = int(os.environ.get("TEAM_OPEN_LIMIT", "200"))

# Business hours, as Alex defined them: 9am to 4pm. The triage schedule starts
# earlier, at 7am, because reading mail before the day starts is the point.
WORKING_HOURS = WorkingHours(start_hour=9, end_hour=16)


def _anthropic() -> Anthropic:
    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _alert(subject: str, body: str) -> None:
    """Best-effort loud failure. If this cannot send, the logs are all we have."""
    try:
        client_from_env().send_mail([ASSISTANT_EMAIL], subject, body)
    except Exception:  # noqa: BLE001
        log.exception("Could not send alert: %s", subject)


def _availability(graph, now: dt.datetime) -> str | None:
    """Alex's open business-hours time over the next AVAILABILITY_DAYS days.

    Returns None only when the calendar could not be read at all. An empty
    string means the calendar was read and there is genuinely nothing open,
    which is a different thing and must not be reported as an outage.
    """
    end = now + dt.timedelta(days=AVAILABILITY_DAYS)
    try:
        events = graph.calendar_view(now, end, TIMEZONE)
    except GraphError:
        log.exception("Calendar read failed; meeting replies will omit availability.")
        return None

    slots = free_slots(
        events,
        now,
        timezone=TIMEZONE,
        days=AVAILABILITY_DAYS,
        hours=WORKING_HOURS,
        slot_minutes=30,
        buffer_minutes=15,
        holidays=holidays_from_env(now.date(), end.date()),
    )
    return format_for_email(slots, TIMEZONE)


def run_triage(source: str) -> None:
    tz = ZoneInfo(TIMEZONE)
    now = dt.datetime.now(tz)

    try:
        graph = client_from_env()
        messages = graph.recent_messages(
            since=now - dt.timedelta(hours=LOOKBACK_HOURS),
            limit=MAX_MESSAGES_PER_RUN,
        )
    except TokenExpired:
        log.error("Refresh token is dead - re-consent required.")
        _alert(
            "Assistant needs re-authorisation",
            "<p>The Microsoft refresh token is no longer valid, so the assistant has "
            "stopped checking mail. Re-run scripts/get_refresh_token.py and update the "
            "vault secret.</p>",
        )
        return
    except GraphError:
        log.exception("Graph call failed during %s run", source)
        return

    store = default_processed_store()
    seen = store.seen()
    fresh = [m for m in messages if m.get("id") not in seen]

    if not fresh:
        log.info(
            "%s run: nothing new (%d message(s) in window, all already recapped).",
            source, len(messages),
        )
        return

    log.info(
        "%s run: %d new message(s) of %d in the %dh window.",
        source, len(fresh), len(messages), LOOKBACK_HOURS,
    )

    claude = _anthropic()

    # Replies to a task digest are handled first and removed from the batch.
    # Left in, they would be triaged as ordinary mail - Alex emailing himself
    # "did the MOU one" reads exactly like a message owing a reply.
    task_replies = [m for m in fresh if _is_task_reply(m)]
    if task_replies:
        for message in task_replies:
            try:
                _handle_task_reply(graph, claude, message)
            except Exception:  # noqa: BLE001
                # A reply that blows up must not cost the rest of the run.
                # It did once: the whole hourly triage failed after the reply
                # had already been applied, so the recap never went out.
                log.exception("Task reply handling failed for %s", message.get("id"))
        handled = {m["id"] for m in task_replies}
        store.mark(list(handled))
        fresh = [m for m in fresh if m["id"] not in handled]
        if not fresh:
            log.info("%s run: %d task reply/replies handled, nothing else new.",
                     source, len(task_replies))
            return
        log.info("%s run: %d task reply/replies handled, %d message(s) left to triage.",
                 source, len(task_replies), len(fresh))
    try:
        known = {
            (e.get("address") or "").lower()
            for c in graph.contacts()
            for e in (c.get("emailAddresses") or [])
        }
    except GraphError:
        log.warning("Could not read contacts; treating every sender as new.")
        known = set()

    result = classify(claude, fresh, known)
    by_id = {m["id"]: m for m in fresh}

    # Availability is read once per run, and only when a meeting ask is actually
    # in this batch. Ten days of calendarView on every hourly run would be a
    # Graph call for nothing on most of them.
    availability: str | None = None
    if any(v.request_type == "meeting" for v in result.needing_draft):
        availability = _availability(graph, now)

    pending_store = default_pending_store()
    signing_key = actions.signing_key()
    action_links: dict[str, dict[str, str]] = {}

    drafted = 0
    for verdict in result.needing_draft:
        if drafted >= MAX_DRAFTS_PER_RUN:
            log.warning("Hit MAX_DRAFTS_PER_RUN (%d); remaining shown without drafts.",
                        MAX_DRAFTS_PER_RUN)
            break
        message = by_id.get(verdict.message_id)
        if not message:
            continue
        try:
            body = graph.message_body(verdict.message_id)
        except GraphError:
            body = message.get("bodyPreview", "")
        draft_reply(claude, verdict, message, body, availability)
        drafted += 1

        if not verdict.draft:
            continue

        _attach_review_link(graph, verdict, message)

        outlook_draft_id = None
        if CREATE_OUTLOOK_DRAFTS:
            try:
                saved = graph.create_reply_draft(
                    verdict.message_id, draft_to_html(verdict.draft)
                )
                outlook_draft_id = saved.get("id")
                verdict.draft_saved = True
                verdict.draft_link = saved.get("webLink")
            except GraphError:
                # The draft text still reaches the recap, so a failure here
                # degrades the workflow rather than losing the work.
                log.exception("Could not save Outlook draft for %s", verdict.message_id)

        links = _record_pending(
            pending_store, signing_key, verdict, message, outlook_draft_id
        )
        if links:
            action_links[verdict.message_id] = links

    # Contacts are NOT added because someone emailed Alex - that signal is far
    # too weak, and it is how a 136-entry directory came to be missing 311 real
    # correspondents including his own team. They are added when he REPLIES:
    # immediately in _send() for a reply sent from the recap, and weekly in
    # contacts_weekly() for everything he answered by hand in Outlook.
    added: list[str] = []

    if not is_actionable(result.verdicts):
        # Nothing here is Alex's to act on, so nothing is sent. The messages are
        # still marked processed: they were genuinely reviewed, and leaving them
        # unmarked would re-triage the same mail every hour at full model cost.
        log.info(
            "%s run: %d message(s) reviewed, none actionable - no recap sent.",
            source, len(result.verdicts),
        )
        store.mark([m["id"] for m in fresh])
        return

    subject, html_body = build_recap(
        result.verdicts, by_id, now, TIMEZONE, added, action_links=action_links
    )
    try:
        graph.send_mail([ASSISTANT_EMAIL], subject, html_body)
    except GraphError:
        log.exception("Recap send failed; NOT marking messages processed so they retry.")
        return

    # Only after a recap has actually been delivered.
    store.mark([m["id"] for m in fresh])


def _is_task_reply(message: dict) -> bool:
    """A reply from Alex, to a digest this assistant sent.

    BOTH halves are required. The marker alone would let anyone who ever
    received a forwarded digest drive Jira by quoting its subject line; the
    sender alone would fire on any mail Alex sends himself.
    """
    if not find_marker(message.get("subject")):
        return False
    sender = ((message.get("from") or {}).get("emailAddress") or {}).get("address", "")
    return sender.strip().lower() == ASSISTANT_EMAIL.strip().lower()


def _handle_task_reply(graph, claude, message: dict) -> None:
    """Parse one reply, apply it to Jira, and confirm what changed."""
    tz = ZoneInfo(TIMEZONE)
    now = dt.datetime.now(tz)
    marker = find_marker(message.get("subject"))

    digest = default_digest_store().get(marker)
    if digest is None:
        log.warning("Task reply carries unknown marker %s; ignoring.", marker)
        _send_mail(graph, *build_reply_confirmation(
            _no_op_result("That task list is too old to update from - it has aged out. "
                          "Reply to a newer one, or update Jira directly."), now))
        return

    jira = jira_from_env()
    if jira is None:
        _send_mail(graph, *build_reply_confirmation(
            _no_op_result("Jira is not configured, so nothing could be updated."), now))
        return

    try:
        body = graph.message_body(message["id"])
    except GraphError:
        body = message.get("bodyPreview", "")

    result = parse_reply(claude, body, digest.issues, today=now.date())
    if not result.error and result.operations:
        apply_operations(jira, result, project_key=JIRA_PROJECT_KEY)

    log.info("Task reply %s: %d applied, %d failed, %d skipped.",
             marker, len(result.applied), len(result.failed), len(result.skipped))
    _send_mail(graph, *build_reply_confirmation(result, now))


def _no_op_result(message: str):
    from shared.taskreply import ReplyResult

    return ReplyResult(error=message)


def _send_mail(graph, subject: str, body: str) -> None:
    """Named apart from _send(store, draft), which sends a drafted REPLY.

    Two functions called _send in one module is how the second one silently
    shadows the first.
    """
    try:
        graph.send_mail([ASSISTANT_EMAIL], subject, body)
    except Exception:  # noqa: BLE001 - TokenExpired is not a GraphError
        log.exception("Could not send %r", subject)


def _attach_review_link(graph, verdict, message: dict) -> None:
    """Point the review button at whatever this message actually has.

    Two outcomes, and the difference matters enough that the button label says
    which one Alex is getting: a cloud attachment has its own URL and opens the
    file, while a file attachment lives inside the message and the best that
    can be offered is the email it is in.

    Never raises. A missing review button is a small loss; a triage run that
    dies looking for one loses the whole batch.
    """
    if not message.get("hasAttachments"):
        return
    try:
        found = graph.attachments(verdict.message_id)
        if not found:
            return
        # The first real attachment. Messages carrying several are usually
        # several parts of one ask, and four buttons is already the limit of
        # what fits on a phone.
        first = found[0]
        verdict.attachment_name = first.get("name") or ""
        direct = graph.attachment_link(verdict.message_id, first)
        if direct:
            verdict.attachment_url = direct
            verdict.attachment_direct = True
        elif message.get("webLink"):
            verdict.attachment_url = message["webLink"]
            verdict.attachment_direct = False
    except Exception:  # noqa: BLE001
        log.exception("Could not build a review link for %s", verdict.message_id)


def _record_pending(
    store,
    signing_key: bytes | None,
    verdict,
    message: dict,
    outlook_draft_id: str | None,
) -> dict[str, str]:
    """Queue a drafted reply and mint its three button links.

    Returns {} rather than raising if anything here fails. A recap without
    buttons is a degraded recap; a run that dies at the last step because a
    table write failed loses the whole batch.
    """
    if not signing_key:
        return {}

    sender = ((message.get("from") or {}).get("emailAddress") or {})
    try:
        pending = store.put(
            PendingDraft.new(
                message_id=verdict.message_id,
                subject=message.get("subject") or "",
                sender_name=sender.get("name") or "",
                sender_address=sender.get("address") or "",
                the_ask=verdict.the_ask,
                request_type=verdict.request_type,
                body=verdict.draft or "",
                outlook_draft_id=outlook_draft_id,
            )
        )
        base = actions.base_url()
        if not base:
            log.warning("No WEBSITE_HOSTNAME or ACTION_BASE_URL; buttons omitted.")
            return {}
        return {
            name: actions.action_url(name, pending.id, signing_key, base=base)
            for name in actions.ACTIONS
        }
    except Exception:  # noqa: BLE001 - buttons are a nicety, the recap is not
        log.exception("Could not queue pending draft for %s", verdict.message_id)
        return {}


def run_agenda(for_date: dt.date, is_today: bool) -> None:
    """Build and send one day's agenda. Skips days Alex is not working."""
    tz = ZoneInfo(TIMEZONE)

    # Holidays do NOT suppress the agenda. Alex still takes meetings on them,
    # and an agenda that goes silent on the one day the calendar is unusual is
    # the day he most needs to see it. Holidays still suppress *offered
    # availability* in meeting replies, which is a different question: what he
    # is doing that day versus what he is willing to promise a stranger.
    if for_date.weekday() >= 5:
        log.info("Skipping agenda for %s (weekend).", for_date)
        return

    start = dt.datetime.combine(for_date, dt.time(0, 0), tzinfo=tz)
    end = start + dt.timedelta(days=1)

    try:
        graph = client_from_env()
        events = [
            e for e in graph.calendar_view(start, end, TIMEZONE) if not e.get("isCancelled")
        ]
    except TokenExpired:
        _alert("Assistant needs re-authorisation", "<p>Agenda skipped: token expired.</p>")
        return
    except GraphError:
        log.exception("Agenda calendar read failed for %s.", for_date)
        return

    # weekdays_only off and no holiday set: the day was already chosen, so the
    # only question left is which hours of it are free.
    slots = free_slots(
        events,
        start,
        timezone=TIMEZONE,
        days=1,
        hours=dataclasses.replace(WORKING_HOURS, weekdays_only=False),
        lead_minutes=0,
    )
    blocks = agenda_free_blocks(slots, for_date)

    plan = _day_plan(events, blocks, for_date, is_today)

    # Buttons: mark a recommended task done, or block a free slot. Both reuse
    # the recap's signed-token machinery; without a signing key the agenda
    # simply renders without them.
    key = actions.signing_key()
    base = actions.base_url()
    task_links: dict[str, str] = {}
    block_links: dict = {}
    if key and base:
        for pick in (plan.picks if plan else []):
            task_links[pick.key] = actions.action_url("task_done", pick.key, key, base=base)
        for block in agenda_free_slots(slots, for_date):
            span = f"{block.start.isoformat()}/{block.end.isoformat()}"
            block_links[(block.start, block.end)] = actions.action_url(
                "block_time", span, key, base=base
            )

    subject, body = build_agenda(
        events, slots, for_date, TIMEZONE, plan=plan, is_today=is_today,
        user_name=USER_NAME, task_links=task_links, block_links=block_links,
        me=ASSISTANT_EMAIL, board_url=JIRA_BOARD_URL,
    )
    try:
        graph.send_mail([ASSISTANT_EMAIL], subject, body)
    except GraphError:
        log.exception("Agenda send failed for %s.", for_date)


def _day_plan(
    events: list[dict], blocks: list[str], for_date: dt.date, is_today: bool
) -> DayPlan | None:
    """Jira recommendations, or None when Jira is not configured.

    None means "leave the section out entirely". A DayPlan carrying a note means
    "Jira was configured and something went wrong", which Alex should see - a
    silently missing section would look identical to an empty backlog.
    """
    jira = jira_from_env()
    if jira is None:
        return None

    # Same trap as the digests: a dead token makes todo_issues return [], and
    # recommend_day would then report "nothing on the board is assigned to
    # you" - a confident wrong answer rather than an error.
    try:
        jira.whoami()
    except Exception as exc:  # noqa: BLE001
        log.error("Jira credentials are not working (agenda): %s", str(exc)[:200])
        return DayPlan(note="Jira rejected the API token, so there are no "
                            "recommendations for today.")

    try:
        issues = jira.todo_issues(JIRA_PROJECT_KEY)
    except JiraError as exc:
        log.exception("Jira read failed for %s.", for_date)
        return DayPlan(note=f"Could not read the {JIRA_PROJECT_KEY} board: {exc}")
    except Exception as exc:  # noqa: BLE001 - the agenda must still go out
        log.exception("Unexpected Jira failure for %s.", for_date)
        return DayPlan(note=f"Could not read the {JIRA_PROJECT_KEY} board: {str(exc)[:120]}")

    return recommend_day(
        _anthropic(),
        issues,
        events,
        blocks,
        for_date,
        label="today" if is_today else "tomorrow",
    )


def _html(body: str, status: int = 200) -> func.HttpResponse:
    """Every page is uncacheable and leaks no Referer.

    The URL contains a capability token. A shared cache holding it, or a
    Referer header carrying it to another origin, would hand someone else the
    ability to send mail as Alex.
    """
    return func.HttpResponse(
        body,
        status_code=status,
        mimetype="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


def _draft_body_html(text: str) -> str:
    return draft_to_html(text)


@app.route(route="draft", methods=["GET", "POST"], auth_level=func.AuthLevel.ANONYMOUS)
def draft_action(req: func.HttpRequest) -> func.HttpResponse:
    """The endpoint behind the recap buttons.

    GET renders a confirmation page and changes NOTHING. POST performs the
    action. That split is not cosmetic: Outlook Safe Links, Defender and
    ordinary antivirus follow links in email with a GET, often within seconds
    of delivery, so a GET that sent mail would have the scanner send every
    draft before Alex ever opened the recap.

    The route is anonymous because it is reached from an email client that
    carries no Azure credentials. The signed token IS the authentication, and
    the pending record's status is the replay protection.
    """
    key = actions.signing_key()
    if not key:
        return _html(pages.error("The assistant is not configured for buttons."), 503)

    token = (req.params.get("t") or "").strip()
    form = {}
    if req.method == "POST":
        try:
            form = dict(req.form or {})
        except Exception:  # noqa: BLE001 - malformed body is a 400, not a 500
            form = {}
        token = (form.get("t") or token or "").strip()

    try:
        payload = actions.verify(token, key)
    except actions.ActionError as exc:
        return _html(pages.error(str(exc)), 400)

    if payload["a"] in actions.AGENDA_ACTIONS:
        # An agenda token that arrived here means a link was built with the
        # wrong route. Say so, rather than looking it up as a draft and
        # reporting "that draft is no longer available" - which is both wrong
        # and makes a working feature look broken.
        return _html(pages.error(
            "That button belongs to the agenda, not the inbox recap. The link "
            "was built with the wrong address; the agenda itself is fine."), 400)

    store = default_pending_store()
    draft = store.get(payload["p"])
    if draft is None:
        return _html(pages.gone(), 410)
    if not draft.open:
        return _html(pages.already_resolved(draft), 200)

    action = payload["a"]

    if req.method == "GET":
        if action == "send":
            return _html(pages.confirm_send(draft, token))
        if action == "edit":
            return _html(pages.confirm_edit(draft, token))
        return _html(pages.confirm_discard(draft, token))

    return _perform(store, draft, token, action, form)


def _perform(store, draft, token: str, action: str, form: dict) -> func.HttpResponse:
    """The POST half: the only place anything actually changes."""
    confirm = (form.get("confirm") or "").strip()

    # "Edit first" / "Keep and edit" from the other two pages.
    if confirm == "to_edit":
        return _html(pages.confirm_edit(draft, token))

    edited = form.get("body")
    if edited is not None and action == "edit":
        edited = edited.replace("\r\n", "\n").strip()
        if edited and edited != draft.body:
            store.update_body(draft.id, edited)
            draft.body = edited
        if confirm == "save":
            return _html(
                pages.result(
                    "Draft saved.",
                    "The buttons in your recap still work when you are ready to send.",
                )
            )

    if confirm == "discard" or (action == "discard" and confirm != "send"):
        return _discard(store, draft)

    if confirm != "send":
        # A POST that named no button. Show the confirmation rather than guess.
        return _html(pages.confirm_send(draft, token))

    return _send(store, draft)


def _send(store, draft) -> func.HttpResponse:
    """Send, then mark. Order matters and the failure modes differ.

    `resolve` is claimed FIRST so two concurrent clicks cannot both reach
    Graph - the second gets None and stops. If the send then fails, the claim
    is rolled back so Alex can retry, because a draft stuck in "sent" that was
    never sent is the worst of the available outcomes.
    """
    claimed = store.resolve(draft.id, SENT, "sent from recap")
    if claimed is None:
        fresh = store.get(draft.id)
        return _html(pages.already_resolved(fresh) if fresh else pages.gone())

    try:
        graph = client_from_env()
        if draft.outlook_draft_id:
            graph.send_existing_draft(draft.outlook_draft_id, _draft_body_html(draft.body))
        else:
            graph.send_reply(draft.message_id, _draft_body_html(draft.body))
    except TokenExpired:
        store.resolve_rollback(draft.id)
        return _html(
            pages.error(
                "The assistant's Microsoft sign-in has expired, so nothing was sent. "
                "Re-run the consent script, then use this link again."
            ),
            503,
        )
    except GraphError:
        log.exception("Send failed for pending draft %s", draft.id)
        store.resolve_rollback(draft.id)
        return _html(
            pages.error("Outlook refused the send. Nothing went out; try again shortly."),
            502,
        )

    who = draft.sender_name or draft.sender_address or "them"

    # He just replied to this person, which is the strongest possible signal
    # that they belong in type-ahead.
    note = draft.subject
    try:
        graph_client = client_from_env()
        if draft.sender_address.lower() not in contacts.known_addresses(graph_client):
            if contacts.add_person(graph_client, draft.sender_address, draft.sender_name):
                note = f"{draft.subject} · added to your contacts"
    except Exception:  # noqa: BLE001 - the reply went out; this is a bonus
        log.exception("Could not add %s to contacts after sending", draft.sender_address)

    return _html(pages.result(f"Sent to {who}.", note))


def _discard(store, draft) -> func.HttpResponse:
    claimed = store.resolve(draft.id, DISCARDED, "discarded from recap")
    if claimed is None:
        fresh = store.get(draft.id)
        return _html(pages.already_resolved(fresh) if fresh else pages.gone())

    if draft.outlook_draft_id:
        try:
            client_from_env().delete_message(draft.outlook_draft_id)
        except (GraphError, TokenExpired):
            # The record is resolved either way; a stranded Outlook draft is
            # untidy, not harmful, and saying "discarded" is still true of the
            # thing Alex was looking at.
            log.warning("Could not delete Outlook draft %s", draft.outlook_draft_id)

    who = draft.sender_name or draft.sender_address or "them"
    return _html(pages.result(f"Discarded. Nothing was sent to {who}.", draft.subject))


def _jira_credentials_work(jira, what: str) -> bool:
    """Prove the token authenticates BEFORE trusting any query result.

    This exists because of a genuinely dangerous asymmetry in Jira Cloud:

        GET /rest/api/3/myself       with a dead token -> 401
        POST /rest/api/3/search/jql  with a dead token -> 200 {"issues": []}

    Search falls back to anonymous access, and anonymous can see nothing. So a
    revoked token does not produce an error - it produces a confident,
    well-formatted digest reporting that every person on the team finished
    nothing and has nothing open. That is worse than an outage, because it
    looks like news.

    An unauthenticated run therefore sends an alert and no digest.
    """
    try:
        me = jira.whoami()
    except Exception as exc:  # noqa: BLE001
        log.error("Jira credentials are not working (%s): %s", what, str(exc)[:200])
        _alert(
            "Assistant cannot reach Jira",
            f"<p>The {what} was not sent: Jira rejected the API token.</p>"
            f"<p>{_esc_plain(str(exc)[:300])}</p>"
            "<p>Most likely the token in Key Vault was rotated and the Function App "
            "is still holding the previous one. Re-applying the app settings forces "
            "the Key Vault reference to re-resolve.</p>",
        )
        return False

    log.info("Jira identity: %s <%s> account=%s | token fp=%s len=%d",
             me.get("displayName"), me.get("emailAddress"), me.get("accountId"),
             jira.token_fingerprint, jira.token_length)
    return True


def _esc_plain(text: str) -> str:
    import html as _html

    return _html.escape(text or "", quote=False)


def run_task_digest() -> None:
    """Everything on Alex's plate, as an email he can reply to."""
    tz = ZoneInfo(TIMEZONE)
    today = dt.datetime.now(tz).date()

    jira = jira_from_env()
    if jira is None:
        log.info("Jira not configured; task digest skipped.")
        return

    if not _jira_credentials_work(jira, "task digest"):
        return

    try:
        issues = jira.my_open_issues(JIRA_PROJECT_KEY, limit=100)
    except Exception:  # noqa: BLE001
        log.exception("Could not read %s for the task digest.", JIRA_PROJECT_KEY)
        return

    # Stored BEFORE sending. A digest Alex can reply to but whose issues were
    # never recorded is worse than one that never arrives - the reply would be
    # met with "that list is too old", which is both wrong and confusing.
    digest = TaskDigest.new({i.key: i.summary for i in issues})
    try:
        default_digest_store().put(digest)
    except Exception:  # noqa: BLE001
        log.exception("Could not record the task digest; sending without reply support.")

    subject, body = build_task_digest(issues, digest.marker, today, JIRA_PROJECT_KEY)
    try:
        client_from_env().send_mail([ASSISTANT_EMAIL], subject, body)
    except (GraphError, TokenExpired):
        log.exception("Task digest send failed.")


def run_team_digest(days: int = 7) -> None:
    """What the team finished last week and what is still open."""
    tz = ZoneInfo(TIMEZONE)
    today = dt.datetime.now(tz).date()

    jira = jira_from_env()
    if jira is None:
        log.info("Jira not configured; team digest skipped.")
        return

    if not _jira_credentials_work(jira, "team digest"):
        return

    sections = team_sections(jira, roster(), days=days, open_limit=TEAM_OPEN_LIMIT)
    if all(s.error for s in sections):
        log.error("Every teammate's Jira read failed; not sending an empty digest.")
        return

    summary = summarise_team(_anthropic(), sections, days=days)
    # No prose anywhere means the summary pass failed, not that it had nothing
    # to say. The digest states that rather than shipping bare lists.
    summary_failed = not summary and not any(s.recap or s.attention for s in sections)
    if summary_failed:
        log.error("Team digest: summaries unavailable; sending lists only.")

    subject, body = build_team_digest(
        sections, today, days=days, summary=summary, summary_failed=summary_failed
    )
    try:
        client_from_env().send_mail([ASSISTANT_EMAIL], subject, body)
    except (GraphError, TokenExpired):
        log.exception("Team digest send failed.")


# Monday 07:00 - the week's task list, between the 6am agenda and the 8am team
# digest, so the three morning emails arrive in a deliberate order.
@app.timer_trigger(schedule="0 0 7 * * 1", arg_name="timer", run_on_startup=False)
def task_digest_monday(timer: func.TimerRequest) -> None:
    run_task_digest()


# Sunday 15:00 and Monday 08:00 - the same team digest twice, on purpose. The
# Sunday copy is for reading before the week starts; the Monday one is for the
# week itself, and catches anything closed on Sunday evening.
@app.timer_trigger(schedule="0 0 15 * * 0", arg_name="timer", run_on_startup=False)
def team_digest_sunday(timer: func.TimerRequest) -> None:
    run_team_digest()


@app.timer_trigger(schedule="0 0 8 * * 1", arg_name="timer", run_on_startup=False)
def team_digest_monday(timer: func.TimerRequest) -> None:
    run_team_digest()


def agenda_free_slots(slots, for_date):
    from shared.availability import merge_contiguous

    return merge_contiguous([s for s in slots if s.start.date() == for_date])


@app.route(route="agenda", methods=["GET", "POST"], auth_level=func.AuthLevel.ANONYMOUS)
def agenda_action(req: func.HttpRequest) -> func.HttpResponse:
    """The agenda's buttons: mark a task done, block a free slot.

    Same GET-renders / POST-acts split as /api/draft, for the same reason:
    link scanners follow every URL in an email with a GET, and a GET that
    wrote to Jira or the calendar would fire before Alex saw the agenda.
    """
    key = actions.signing_key()
    if not key:
        return _html(pages.error("The assistant is not configured for buttons."), 503)

    token = (req.params.get("t") or "").strip()
    form = {}
    if req.method == "POST":
        try:
            form = dict(req.form or {})
        except Exception:  # noqa: BLE001
            form = {}
        token = (form.get("t") or token or "").strip()

    try:
        payload = actions.verify(token, key)
    except actions.ActionError as exc:
        return _html(pages.error(str(exc)), 400)

    action, value = payload["a"], payload["p"]
    if action not in actions.AGENDA_ACTIONS:
        return _html(pages.error("That link belongs somewhere else."), 400)

    if action == "task_done":
        if req.method == "GET":
            return _html(pages.confirm_task_done(value, token))
        if (form.get("confirm") or "") != "done":
            return _html(pages.confirm_task_done(value, token))
        return _mark_task_done(value)

    start, _, end = value.partition("/")
    label = _slot_label(start, end)
    if req.method == "GET":
        return _html(pages.confirm_block_time(label, token))
    reason = (form.get("reason") or "").strip()
    if (form.get("confirm") or "") != "block" or not reason:
        return _html(pages.confirm_block_time(label, token))
    return _block_time(start, end, reason, label)


def _slot_label(start: str, end: str) -> str:
    from shared.agendamail import ampm

    try:
        a, b = dt.datetime.fromisoformat(start), dt.datetime.fromisoformat(end)
        return f"{ampm(a)} - {ampm(b)} on {a.strftime('%A, %B')} {a.day}"
    except (ValueError, TypeError):
        return f"{start} to {end}"


def _mark_task_done(issue_key: str) -> func.HttpResponse:
    jira = jira_from_env()
    if jira is None:
        return _html(pages.error("Jira is not configured."), 503)
    try:
        option = jira.find_transition(issue_key, "Done")
        if option is None:
            return _html(pages.error(
                f"Jira offers no transition to Done for {issue_key} right now."), 409)
        jira.transition(issue_key, option["id"])
    except Exception as exc:  # noqa: BLE001
        log.exception("Could not mark %s done", issue_key)
        return _html(pages.error(f"Jira refused it: {str(exc)[:200]}"), 502)

    return _html(pages.result(f"{issue_key} marked done.",
                              "One click to undo in Jira if that was wrong."))


def _block_time(start: str, end: str, reason: str, label: str) -> func.HttpResponse:
    try:
        begins, ends = dt.datetime.fromisoformat(start), dt.datetime.fromisoformat(end)
    except (ValueError, TypeError):
        return _html(pages.error("That link does not name a valid time."), 400)

    try:
        client_from_env().create_event(
            subject=reason[:120], start=begins, end=ends, timezone=TIMEZONE,
            show_as="busy", body="Blocked from the daily agenda.",
        )
    except TokenExpired:
        return _html(pages.error(
            "The Microsoft sign-in has expired, so nothing was blocked."), 503)
    except GraphError as exc:
        log.exception("Could not block %s", label)
        detail = str(exc)[:200]
        if getattr(exc, "status", 0) == 403:
            detail = ("Graph refused the write. The assistant's consent covers "
                      "Calendars.Read only - re-run the consent flow to grant "
                      "Calendars.ReadWrite.")
        return _html(pages.error(detail), 502)

    return _html(pages.result(f"Blocked: {reason}", label))


EVENT_LOOKBACK_DAYS = 14
EVENT_SCAN_CAP = 200         # per folder; the model sees 10 at a time

# 60 was the first guess and it was wrong in a way that looked like success:
# Alex's inbox takes well over 60 messages in two DAYS, so a 60-message cap on
# a 14-day window silently scanned two days and reported one event. The digest
# promises the last two weeks, so the cap has to be big enough to mean it.
# 200 per folder is roughly 40 model calls per run, twice a week.


def run_event_scan(days: int = EVENT_LOOKBACK_DAYS) -> None:
    """Twice a week: find events in the inbox and junk, and offer them.

    Junk is scanned deliberately. Conference and webinar invitations are the
    single most common thing Outlook mis-files, which makes the junk folder the
    highest-yield place to look rather than an afterthought.

    Sends nothing when there is nothing new. The rule for this whole assistant
    is that mail arrives only when something needs Alex, and "no events this
    week" does not.
    """
    try:
        graph = client_from_env()
    except TokenExpired:
        _alert("Assistant: Microsoft sign-in expired",
               "<p>The event scan could not run.</p>")
        return

    today = dt.datetime.now(ZoneInfo(TIMEZONE)).date()
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)

    messages: list[dict] = []
    for folder in ("inbox", "junkemail"):
        try:
            found = graph.folder_messages(folder, since, limit=EVENT_SCAN_CAP)
        except GraphError:
            log.exception("Could not read %s for the event scan", folder)
            continue
        for msg in found:
            msg["_folder"] = "junk" if folder == "junkemail" else "inbox"
        messages.extend(found)

    if not messages:
        log.info("Event scan: no mail in the last %s days.", days)
        return

    store = ev.default_event_store()
    seen = store.decided_fingerprints() | store.open_fingerprints()
    by_id = {m.get("id"): m for m in messages}

    raw = ev.extract(_anthropic(), messages, today)

    fresh: list[ev.EventCandidate] = []
    names: set[str] = set()
    # Earliest first, so when one conference is announced twice the card that
    # survives is the one quoting the day it actually starts.
    raw.sort(key=lambda r: str(r.get("start") or "9999"))

    for item in raw:
        if not ev.valid(item, today):
            continue
        mark = ev.fingerprint(item.get("title", ""), item.get("start", ""))
        if mark in seen:
            continue
        name = ev.title_key(item.get("title", ""))
        if name and name in names:
            continue        # same event, second announcement, this run
        seen.add(mark)      # dedupe within this run as well as against history
        names.add(name)

        source = by_id.get(item.get("message_id")) or {}
        sender = ((source.get("from") or {}).get("emailAddress") or {})
        fresh.append(store.put(ev.EventCandidate.new(
            fingerprint=mark,
            title=(item.get("title") or "").strip()[:200],
            start=item.get("start") or "",
            end=item.get("end") or "",
            location=(item.get("location") or "")[:200],
            online=bool(item.get("online")),
            cost=(item.get("cost") or "")[:60],
            why=(item.get("why") or "")[:400],
            source_subject=(source.get("subject") or "")[:200],
            source_from=(sender.get("name") or sender.get("address") or "")[:120],
            source_message_id=source.get("id") or "",
            source_folder=source.get("_folder") or "",
        )))

    if not fresh:
        log.info("Event scan: %s messages, nothing new to offer.", len(messages))
        return

    fresh.sort(key=lambda c: c.start or "9999")

    key = actions.signing_key()
    links: dict[str, dict[str, str]] = {}
    if key:
        links = {
            c.id: {a: actions.action_url(a, c.id, key) for a in actions.EVENT_ACTIONS}
            for c in fresh
        }

    subject, body = build_event_digest(fresh, today, links, days_scanned=days)
    _send_mail(graph, subject, body)
    log.info("Event scan: offered %s events from %s messages.", len(fresh), len(messages))


@app.timer_trigger(schedule="0 0 12 * * 2,5", arg_name="timer", run_on_startup=False)
def events_biweekly(timer: func.TimerRequest) -> None:
    """Midday Tuesday and Friday."""
    run_event_scan()


@app.route(route="event", methods=["GET", "POST"], auth_level=func.AuthLevel.ANONYMOUS)
def event_action(req: func.HttpRequest) -> func.HttpResponse:
    """Yes / might / no thanks, from the events digest.

    GET renders, POST acts - the same split as the other two endpoints, and for
    the same reason: "no thanks" DELETES an email, so a link scanner following
    the URL would bin mail Alex never saw.
    """
    key = actions.signing_key()
    if not key:
        return _html(pages.error("The assistant is not configured for buttons."), 503)

    token = (req.params.get("t") or "").strip()
    form = {}
    if req.method == "POST":
        try:
            form = dict(req.form or {})
        except Exception:  # noqa: BLE001
            form = {}
        token = (form.get("t") or token or "").strip()

    try:
        payload = actions.verify(token, key)
    except actions.ActionError as exc:
        return _html(pages.error(str(exc)), 400)

    action, event_id = payload["a"], payload["p"]
    if action not in actions.EVENT_ACTIONS:
        return _html(pages.error("That link belongs somewhere else."), 400)

    store = ev.default_event_store()
    candidate = store.get(event_id)
    if candidate is None:
        return _html(pages.result(
            "That event is no longer on file.",
            "Events are kept for a few months. Nothing changed just now.",
            good=False), 404)

    if not candidate.open:
        already = {ev.ACCEPTED: "already on your calendar",
                   ev.TENTATIVE: "already down as tentative",
                   ev.DECLINED: "already declined"}.get(candidate.status, "already answered")
        return _html(pages.result(f"That one is {already}.", candidate.title, good=True))

    if req.method == "GET" or (form.get("confirm") or "") != "ok":
        return _html(pages.confirm_event(candidate, action, token))

    if action == "event_no":
        return _decline_event(store, candidate)
    return _accept_event(store, candidate, tentative=(action == "event_maybe"))


def _accept_event(store, candidate, tentative: bool) -> func.HttpResponse:
    """Put it on the calendar. Decide only AFTER Graph has accepted it.

    Order matters: marking it decided first and then failing the write would
    leave an event that is neither on the calendar nor offerable again, which
    is the one outcome Alex cannot recover from himself.
    """
    starts = candidate.starts_at()
    if starts is None:
        return _html(pages.error("That event has no usable start time."), 400)

    try:
        ends = dt.datetime.fromisoformat(candidate.end) if candidate.end else None
    except (ValueError, TypeError):
        ends = None
    if ends is None or ends <= starts:
        ends = starts + dt.timedelta(hours=1)

    where = candidate.location or ("Online" if candidate.online else "")
    note = [ev.MAYBE_NOTE] if tentative else []
    if where:
        note.append(f"Where: {where}")
    if candidate.source_subject:
        note.append(f"From the email: {candidate.source_subject}")

    try:
        client_from_env().create_event(
            subject=candidate.title[:120],
            start=starts, end=ends, timezone=TIMEZONE,
            show_as="tentative" if tentative else "busy",
            body="\n".join(note),
        )
    except TokenExpired:
        return _html(pages.error(
            "The Microsoft sign-in has expired, so nothing was added."), 503)
    except GraphError as exc:
        log.exception("Could not add event %s", candidate.id)
        detail = str(exc)[:200]
        if getattr(exc, "status", 0) == 403:
            detail = ("Graph refused the write. The assistant's consent covers "
                      "Calendars.Read only - re-run the consent flow to grant "
                      "Calendars.ReadWrite.")
        return _html(pages.error(detail), 502)

    store.decide(candidate.id, ev.TENTATIVE if tentative else ev.ACCEPTED)
    headline = (f"Added as tentative: {candidate.title}" if tentative
                else f"Added to your calendar: {candidate.title}")
    detail = (f'Marked "{ev.MAYBE_NOTE}".' if tentative
              else _slot_label(starts.isoformat(), ends.isoformat()))
    return _html(pages.result(headline, detail))


def _decline_event(store, candidate) -> func.HttpResponse:
    """Bin the email and never offer this event again.

    The decision is recorded even when the delete fails. Not showing it again
    is the part Alex actually asked for; the message being gone is a
    convenience, and a message that survives is far better than an event that
    keeps coming back.
    """
    store.decide(candidate.id, ev.DECLINED)

    detail = "I won't show it to you again."
    if candidate.source_message_id:
        try:
            client_from_env().delete_message(candidate.source_message_id)
            detail = "The email is in Deleted Items and I won't show it again."
        except (GraphError, TokenExpired):
            log.exception("Could not delete message for event %s", candidate.id)
            detail = ("I couldn't delete the email, but I won't show this event "
                      "to you again.")

    return _html(pages.result(f"Dropped: {candidate.title}", detail))


JUNK_SCAN_CAP = 300


def run_junk_review() -> None:
    """Friday noon: surface the junk that is not junk, before the 5pm sweep.

    Sends nothing when the folder is empty or when everything in it really is
    junk. That is the common case and it is the correct one - an email saying
    "I found nothing worth keeping" is exactly the mail this assistant does
    not send.
    """
    try:
        graph = client_from_env()
    except TokenExpired:
        _alert("Assistant: Microsoft sign-in expired",
               "<p>The Friday junk review could not run, so nothing will be "
               "purged at 5 PM either.</p>")
        return

    today = dt.datetime.now(ZoneInfo(TIMEZONE)).date()
    # Everything currently in the folder, not a time window: the 5pm sweep
    # takes the whole folder, so the review has to have looked at the whole
    # folder or it is reviewing less than it is about to throw away.
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=365)
    try:
        messages = graph.folder_messages("junkemail", since, limit=JUNK_SCAN_CAP)
    except GraphError:
        log.exception("Could not read the junk folder")
        return

    if not messages:
        log.info("Junk review: folder is empty, nothing to do.")
        return

    store = jr.default_junk_store()
    blocked = store.blocked()

    # A sender Alex has already blocked never gets a second hearing. Their mail
    # is simply left for the 5pm sweep.
    fresh = [
        m for m in messages
        if (((m.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
        not in blocked
    ]
    if not fresh:
        log.info("Junk review: all %s message(s) are from blocked senders.", len(messages))
        return

    raw = jr.review(_anthropic(), fresh, today)
    by_id = {m.get("id"): m for m in fresh}

    # An event already offered by the Tuesday/Friday events digest must not
    # also show up here - the two emails arrive within minutes of each other
    # on a Friday, and the same conference in both reads as a bug.
    try:
        events_store = ev.default_event_store()
        known_events = events_store.decided_fingerprints() | events_store.open_fingerprints()
    except Exception:  # noqa: BLE001 - the review is still useful without this
        log.exception("Could not read the events store for de-duplication")
        events_store, known_events = None, set()

    keep: list[jr.JunkCandidate] = []
    for item in raw:
        if not jr.valid(item):
            continue
        source = by_id.get(item.get("message_id"))
        if not source:
            continue      # a message_id the model invented

        if item.get("kind") == "event" and item.get("title"):
            if ev.fingerprint(item["title"], item.get("start", "")) in known_events:
                continue

        sender = ((source.get("from") or {}).get("emailAddress") or {})
        keep.append(store.put(jr.JunkCandidate.new(
            message_id=source["id"],
            subject=(source.get("subject") or "")[:200],
            sender_name=(sender.get("name") or "")[:120],
            sender_address=(sender.get("address") or "")[:200],
            received=source.get("receivedDateTime") or "",
            why=(item.get("why") or "")[:400],
            kind=item.get("kind") or "organisation",
            event_title=(item.get("title") or "")[:200],
            event_start=item.get("start") or "",
            event_end=item.get("end") or "",
            event_location=(item.get("location") or "")[:200],
            event_online=bool(item.get("online")),
        )))

    if not keep:
        log.info("Junk review: %s message(s) read, all of it genuinely junk.",
                 len(fresh))
        return

    key = actions.signing_key()
    links: dict[str, dict[str, str]] = {}
    if key:
        links = {
            c.id: {a: actions.action_url(a, c.id, key) for a in actions.JUNK_ACTIONS}
            for c in keep
        }

    subject, body = build_junk_review(keep, today, links, scanned=len(messages))
    _send_mail(graph, subject, body)
    log.info("Junk review: held back %s of %s message(s).", len(keep), len(messages))


def run_junk_purge() -> None:
    """Friday 5pm: empty the junk folder.

    Everything still in Junk goes to Deleted Items, where it is recoverable
    for thirty days. Anything Alex rescued at noon is already in his inbox and
    is therefore untouched by definition.

    Silent on success. A weekly "I deleted 214 things" email is precisely the
    notification this assistant exists not to send.
    """
    try:
        graph = client_from_env()
    except TokenExpired:
        _alert("Assistant: Microsoft sign-in expired",
               "<p>The Friday 5 PM junk sweep did not run. Your junk folder was "
               "left exactly as it is.</p>")
        return

    try:
        removed = graph.empty_folder("junkemail")
    except GraphError:
        log.exception("Junk purge failed")
        _alert("Assistant: could not empty your junk folder",
               "<p>The Friday 5 PM sweep failed. Nothing was lost - the folder "
               "is untouched - but it will need clearing by hand or it will "
               "carry over to next week.</p>")
        return

    log.info("Junk purge: moved %s message(s) to Deleted Items.", removed)


@app.timer_trigger(schedule="0 0 12 * * 5", arg_name="timer", run_on_startup=False)
def junk_review_friday(timer: func.TimerRequest) -> None:
    """Noon Friday, five hours before the folder is emptied."""
    run_junk_review()


@app.timer_trigger(schedule="0 0 17 * * 5", arg_name="timer", run_on_startup=False)
def junk_purge_friday(timer: func.TimerRequest) -> None:
    """5 PM Friday. Whatever is still in Junk goes to Deleted Items."""
    run_junk_purge()


@app.route(route="junk", methods=["GET", "POST"], auth_level=func.AuthLevel.ANONYMOUS)
def junk_action(req: func.HttpRequest) -> func.HttpResponse:
    """Rescue, rescue-and-calendar, or block - from the Friday review.

    GET renders, POST acts. It matters more here than anywhere else in the app:
    these are the only buttons that move or delete real mail, and Outlook's own
    link scanners follow every URL in every message that arrives.
    """
    key = actions.signing_key()
    if not key:
        return _html(pages.error("The assistant is not configured for buttons."), 503)

    token = (req.params.get("t") or "").strip()
    form = {}
    if req.method == "POST":
        try:
            form = dict(req.form or {})
        except Exception:  # noqa: BLE001
            form = {}
        token = (form.get("t") or token or "").strip()

    try:
        payload = actions.verify(token, key)
    except actions.ActionError as exc:
        return _html(pages.error(str(exc)), 400)

    action, candidate_id = payload["a"], payload["p"]
    if action not in actions.JUNK_ACTIONS:
        return _html(pages.error("That link belongs somewhere else."), 400)

    store = jr.default_junk_store()
    candidate = store.get(candidate_id)
    if candidate is None:
        return _html(pages.result(
            "That message is no longer on file.",
            "Junk decisions are kept for a few months. Nothing changed just now.",
            good=False), 404)

    if not candidate.open:
        already = {jr.RESCUED: "already back in your inbox",
                   jr.CALENDARED: "already in your inbox and on your calendar",
                   jr.BLOCKED: "already blocked and deleted"}.get(
                       candidate.status, "already dealt with")
        return _html(pages.result(f"That one is {already}.", candidate.subject))

    if req.method == "GET" or (form.get("confirm") or "") != "ok":
        return _html(pages.confirm_junk(candidate, action, token))

    if action == "junk_block":
        return _block_junk_sender(store, candidate)
    return _rescue_junk(store, candidate, calendar=(action == "junk_calendar"))


def _rescue_junk(store, candidate, calendar: bool) -> func.HttpResponse:
    """Move it back to the inbox, and optionally put the event on the calendar.

    The move happens first and the calendar second, because a message rescued
    without its calendar entry is a small failure Alex can finish by hand,
    while a calendar entry for a message still sitting in a folder that empties
    at 5pm is a booking with no source.
    """
    try:
        graph = client_from_env()
        graph.move_message(candidate.message_id, "inbox")
    except TokenExpired:
        return _html(pages.error(
            "The Microsoft sign-in has expired, so nothing was moved."), 503)
    except GraphError as exc:
        log.exception("Could not rescue %s", candidate.id)
        if getattr(exc, "status", 0) == 404:
            return _html(pages.error(
                "That message is not in your junk folder any more. If this is "
                "after 5 PM on a Friday it was swept - look in Deleted Items, "
                "where it keeps for thirty days."), 404)
        return _html(pages.error(f"Outlook refused the move: {str(exc)[:160]}"), 502)

    note = "It is back in your inbox, unread."

    if calendar and candidate.is_event:
        starts = candidate.starts_at()
        try:
            ends = (dt.datetime.fromisoformat(candidate.event_end)
                    if candidate.event_end else None)
        except (ValueError, TypeError):
            ends = None
        if ends is None or ends <= starts:
            ends = starts + dt.timedelta(hours=1)

        where = candidate.event_location or ("Online" if candidate.event_online else "")
        body = [f"Rescued from your junk folder: {candidate.subject}"]
        if where:
            body.insert(0, f"Where: {where}")

        try:
            client_from_env().create_event(
                subject=(candidate.event_title or candidate.subject)[:120],
                start=starts, end=ends, timezone=TIMEZONE,
                show_as="busy", body="\n".join(body),
            )
            note = "It is back in your inbox and the event is on your calendar."
            _remember_event(candidate)
        except (GraphError, TokenExpired) as exc:
            log.exception("Rescued %s but could not add the event", candidate.id)
            store.decide(candidate.id, jr.RESCUED)
            return _html(pages.result(
                f"Moved to your inbox: {candidate.subject}",
                "The message is safe, but the calendar entry failed: "
                f"{str(exc)[:140]}", good=True))

    store.decide(candidate.id, jr.CALENDARED if calendar else jr.RESCUED)
    return _html(pages.result(f"Rescued: {candidate.subject}", note))


def _remember_event(candidate) -> None:
    """Tell the events digest this one is handled, so it is never re-offered."""
    try:
        store = ev.default_event_store()
        title = candidate.event_title or candidate.subject
        store.decide(store.put(ev.EventCandidate.new(
            fingerprint=ev.fingerprint(title, candidate.event_start),
            title=title, start=candidate.event_start,
            source_subject=candidate.subject,
            source_from=candidate.sender_name or candidate.sender_address,
            source_message_id=candidate.message_id, source_folder="junk",
        )).id, ev.ACCEPTED)
    except Exception:  # noqa: BLE001 - the event is already on the calendar
        log.exception("Could not record the rescued event %s", candidate.id)


def _block_junk_sender(store, candidate) -> func.HttpResponse:
    """Delete the message and never surface that sender again.

    The blocklist is recorded FIRST. It is the part Alex actually asked for,
    it is the part that cannot be redone by hand, and a delete that fails is
    a message still sitting in a folder that empties at 5pm anyway.
    """
    store.block(candidate.sender_address)
    store.decide(candidate.id, jr.BLOCKED)

    detail = ("I will never surface that sender to you again. Note this does "
              "not change Outlook's own blocked-senders list.")
    try:
        client_from_env().delete_message(candidate.message_id)
        detail = ("The message is in Deleted Items and I will never surface "
                  "that sender to you again.")
    except (GraphError, TokenExpired):
        log.exception("Blocked %s but could not delete the message", candidate.id)

    who = candidate.sender_address or candidate.sender_name or "that sender"
    return _html(pages.result(f"Blocked {who}.", detail))


def run_contacts_sweep(days: int = 30, threshold: int = 2) -> None:
    """Add anyone Alex replied to by hand who is not yet a contact."""
    try:
        graph = client_from_env()
    except TokenExpired:
        log.error("Contacts sweep skipped: token expired.")
        return

    try:
        added = contacts.sweep(
            graph, days=days, threshold=threshold,
            me={ASSISTANT_EMAIL, "alex.rivera@example.com"},
        )
    except Exception:  # noqa: BLE001
        log.exception("Contacts sweep failed.")
        return

    if not added:
        log.info("Contacts sweep: nothing new in the last %d days.", days)
        return

    log.info("Contacts sweep added %d contact(s).", len(added))
    rows = "".join(f"<li>{_esc_plain(name)}</li>" for name in sorted(added))
    _send_mail(
        graph,
        f"Added {len(added)} contact(s)",
        f"<p>These people you have emailed recently were not in your contacts, "
        f"so type-ahead would not have found them. They have been added.</p>"
        f"<ul>{rows}</ul>"
        f"<p style='color:#666;font-size:13px'>Anyone wrong here is one click to "
        f"delete in Outlook.</p>",
    )


# Monday 08:30, after the team digest. Weekly is often enough - the button
# path already catches replies the assistant sends itself in real time.
@app.timer_trigger(schedule="0 30 8 * * 1", arg_name="timer", run_on_startup=False)
def contacts_weekly(timer: func.TimerRequest) -> None:
    run_contacts_sweep()


# Hourly, on the hour, 7am through 4pm, Monday to Friday. Ten runs a working
# day and none at all on a weekend.
@app.timer_trigger(schedule="0 0 7-16 * * 1-5", arg_name="timer", run_on_startup=False)
def inbox_hourly(timer: func.TimerRequest) -> None:
    run_triage("hourly")


# 3pm Sunday through Thursday: tomorrow's agenda, while there is still time to
# move something. Sunday is included so Monday gets a preview; Friday and
# Saturday are not, because nobody needs a Saturday agenda at 3pm on Friday.
@app.timer_trigger(schedule="0 0 15 * * 0-4", arg_name="timer", run_on_startup=False)
def agenda_tomorrow(timer: func.TimerRequest) -> None:
    tz = ZoneInfo(TIMEZONE)
    run_agenda((dt.datetime.now(tz) + dt.timedelta(days=1)).date(), is_today=False)


# 6am weekdays: today's agenda, the one to act on.
@app.timer_trigger(schedule="0 0 6 * * 1-5", arg_name="timer", run_on_startup=False)
def agenda_today(timer: func.TimerRequest) -> None:
    tz = ZoneInfo(TIMEZONE)
    run_agenda(dt.datetime.now(tz).date(), is_today=True)


# 5:45am, before the 6am agenda, so a dead token is reported rather than
# discovered as a missing email.
@app.timer_trigger(schedule="0 45 5 * * *", arg_name="timer", run_on_startup=False)
def health_check(timer: func.TimerRequest) -> None:
    """Proves the credential chain works by using it, not by trusting a date."""
    try:
        client_from_env().calendar_view(
            dt.datetime.now(dt.timezone.utc),
            dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1),
            TIMEZONE,
        )
        log.info("Health check passed.")
    except TokenExpired:
        _alert(
            "Assistant needs re-authorisation",
            "<p>The daily health check could not redeem the Microsoft refresh token. "
            "The assistant is not running. Re-run scripts/get_refresh_token.py.</p>",
        )
    except Exception:  # noqa: BLE001
        log.exception("Health check failed.")
