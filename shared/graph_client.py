"""Microsoft Graph access using a delegated refresh-token flow.

Delegated (not application) permissions: the app can only ever see the mailbox
of the user who consented. Least privilege, and it cannot be widened by an
accident in a policy somewhere else.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any, Iterator

import requests

from .token_store import TokenStore, default_store

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"

# NEVER widen this list without re-running the consent flow FIRST.
#
# A refresh token is bound to the scopes it was issued for. Asking Entra to
# redeem it for a scope it does not carry does not "downgrade" gracefully - it
# fails the whole exchange with invalid_grant, which this module reports as
# TokenExpired. Adding Calendars.ReadWrite here on 4 September took down every
# Graph call in the app: mail, calendar, contacts, all of it, within one
# deploy. The scope is not additive at runtime; it is a property of the token
# sitting in the table.
#
# The safe order is: add the permission in the app registration, grant admin
# consent, re-run scripts/get_refresh_token.py, store the new token, and only
# then flip GRAPH_CALENDAR_WRITE to "true".
BASE_SCOPES = [
    "offline_access",
    "https://graph.microsoft.com/Mail.ReadWrite",
    "https://graph.microsoft.com/Mail.Send",
    "https://graph.microsoft.com/Calendars.Read",
    "https://graph.microsoft.com/Contacts.ReadWrite",
]


def _scopes() -> list[str]:
    """The scopes to redeem with. Calendar write is opt-in, and off by default.

    Off by default because the cost of being wrong is asymmetric: without the
    flag the "block this time" button returns a clear 403 and everything else
    keeps working, whereas with the flag set before re-consent NOTHING works.
    """
    if os.environ.get("GRAPH_CALENDAR_WRITE", "").lower() == "true":
        return [
            s for s in BASE_SCOPES
            if s != "https://graph.microsoft.com/Calendars.Read"
        ] + ["https://graph.microsoft.com/Calendars.ReadWrite"]
    return list(BASE_SCOPES)


SCOPES = BASE_SCOPES  # kept for callers and tests that import the name


class TokenExpired(Exception):
    """The refresh token is dead. Needs a human to re-consent.

    Raised distinctly from transient failures so the caller can alert loudly
    instead of retrying into the void.
    """


class GraphError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"Graph returned {status}: {body[:400]}")
        self.status = status
        self.body = body


class TokenManager:
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        store: TokenStore | None = None,
    ):
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._store = store or default_store()
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    @property
    def _token_url(self) -> str:
        return f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"

    def access_token(self) -> str:
        # 120s of slack so a token cannot expire mid-request.
        if self._access_token and time.time() < self._expires_at - 120:
            return self._access_token

        refresh_token = self._store.read()
        if not refresh_token:
            raise TokenExpired("No refresh token stored. Run the one-time consent flow.")

        resp = requests.post(
            self._token_url,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": " ".join(_scopes()),
            },
            timeout=30,
        )

        if resp.status_code == 400:
            error = resp.json().get("error", "")
            if error in ("invalid_grant", "interaction_required"):
                raise TokenExpired(resp.text[:400])
        if not resp.ok:
            raise GraphError(resp.status_code, resp.text)

        payload = resp.json()
        self._access_token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 3600))

        # Entra rotates the refresh token. Persist immediately.
        rotated = payload.get("refresh_token")
        if rotated and rotated != refresh_token:
            self._store.write(rotated)
            log.info("Rotated refresh token persisted.")

        return self._access_token

    def seconds_until_reconsent_risk(self) -> float | None:
        """Best-effort signal for the health check.

        Entra invalidates an unused refresh token after 90 days. We refresh far
        more often than that, so the real risks are a password change or a
        conditional-access change, neither of which is visible in advance. The
        health check therefore proves liveness by actually redeeming the token
        rather than trusting a timestamp.
        """
        try:
            self.access_token()
        except TokenExpired:
            return 0.0
        return None


class GraphClient:
    def __init__(self, tokens: TokenManager, timeout: int = 30):
        self._tokens = tokens
        self._timeout = timeout
        self._session = requests.Session()

    # ---- plumbing -------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = path if path.startswith("http") else f"{GRAPH}{path}"
        for attempt in range(4):
            headers = kwargs.pop("headers", {}) or {}
            headers["Authorization"] = f"Bearer {self._tokens.access_token()}"
            resp = self._session.request(
                method, url, headers=headers, timeout=self._timeout, **kwargs
            )
            if resp.status_code in (429, 503, 504):
                wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning("Graph throttled (%s); sleeping %ss", resp.status_code, wait)
                time.sleep(min(wait, 30))
                continue
            if not resp.ok:
                raise GraphError(resp.status_code, resp.text)
            return resp
        raise GraphError(resp.status_code, resp.text)

    def _paged(self, path: str, **kwargs: Any) -> Iterator[dict]:
        url: str | None = path
        while url:
            payload = self._request("GET", url, **kwargs).json()
            yield from payload.get("value", [])
            url = payload.get("@odata.nextLink")
            kwargs.pop("params", None)  # nextLink already carries them

    # ---- mail -----------------------------------------------------------

    def recent_messages(self, since: dt.datetime, limit: int = 50) -> list[dict]:
        """Inbox mail since a timestamp, newest first, read and unread alike.

        Newest first, not oldest first. The result is capped, and an
        oldest-first cap starves: once the lookback window held more
        already-recapped messages than the cap, every run kept re-fetching that
        same oldest page, found nothing new in it, and reported "nothing new"
        while genuinely new mail sat behind the cap and was never queried.

        Read mail is included. Filtering `isRead eq false` made anything Alex
        had merely glanced at permanently invisible to triage, which is where
        most of the missed meeting invites were going. `isRead` is still
        selected and passed to the classifier as a signal rather than used as a
        gate.

        `inferenceClassification` is selected so the classifier can tell mail
        Outlook put in Focused from mail it put in Other, instead of treating
        the two identically.
        """
        return self.folder_messages("inbox", since, limit)

    def folder_messages(self, folder: str, since: dt.datetime,
                        limit: int = 50) -> list[dict]:
        """The same query against any well-known folder.

        `junkemail` is the one that matters beyond the inbox: event invitations
        land there constantly, and they are exactly the ones Alex never sees.
        A missing folder is not an error worth failing a whole run over - some
        mailboxes have no Junk folder at all - so a 404 returns nothing.
        """
        stamp = since.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "$filter": f"receivedDateTime ge {stamp}",
            "$orderby": "receivedDateTime desc",
            "$top": str(min(limit, 50)),
            "$select": (
                "id,conversationId,subject,from,toRecipients,ccRecipients,"
                "receivedDateTime,bodyPreview,isRead,inferenceClassification,"
                "internetMessageHeaders,webLink,hasAttachments"
            ),
        }
        out: list[dict] = []
        try:
            for msg in self._paged(f"/me/mailFolders/{folder}/messages", params=params):
                out.append(msg)
                if len(out) >= limit:
                    break
        except GraphError as exc:
            if getattr(exc, "status", 0) == 404:
                log.warning("No %s folder in this mailbox; skipping it.", folder)
                return []
            raise
        return out

    REFERENCE = "#microsoft.graph.referenceAttachment"

    def attachments(self, message_id: str) -> list[dict]:
        """Real attachments on a message, inline images excluded.

        `contentBytes` is deliberately NOT selected. Requesting it downloads
        every attachment in full just to find out a name, which on a deck or a
        signed PDF is megabytes per message on an hourly job.

        Inline images are filtered out because they are signature logos and
        pasted screenshots, not things anyone was asked to review.
        """
        params = {"$select": "id,name,contentType,size,isInline"}
        try:
            payload = self._request(
                "GET", f"/me/messages/{message_id}/attachments", params=params
            ).json()
        except GraphError:
            log.warning("Could not list attachments on %s", message_id)
            return []
        return [a for a in payload.get("value", []) if not a.get("isInline")]

    def attachment_link(self, message_id: str, attachment: dict) -> str:
        """A URL that opens the attachment itself, or "" when there isn't one.

        Only a REFERENCE attachment - a OneDrive or SharePoint file someone
        attached as a cloud link - has a URL that can be opened on its own. A
        file attachment is bytes living inside the message and has no address
        of its own, so the caller falls back to opening the email.
        """
        if attachment.get("@odata.type") != self.REFERENCE:
            return ""
        try:
            # Fetched without $select: a reference attachment carries no
            # contentBytes, so the whole record is small.
            full = self._request(
                "GET", f"/me/messages/{message_id}/attachments/{attachment['id']}"
            ).json()
        except GraphError:
            return ""
        return full.get("sourceUrl") or ""

    def message_body(self, message_id: str) -> str:
        params = {"$select": "body"}
        payload = self._request("GET", f"/me/messages/{message_id}", params=params).json()
        return (payload.get("body") or {}).get("content", "")

    @staticmethod
    def _above_quote(reply_html: str, draft_html: str) -> str:
        """Put the reply ABOVE the quoted original, keeping the quote.

        This exists because of a real defect. `createReply` hands back a draft
        whose body ALREADY contains the quoted thread - sender, date, subject,
        the original text. PATCHing `body.content` replaces that wholesale, so
        every draft the assistant made arrived with no quoted original at all,
        and Alex was reading a reply with no idea what it was answering.

        The reply is injected immediately after <body> rather than simply
        concatenated, because Graph returns a complete HTML document and text
        placed before <html> is not reliably rendered.
        """
        if not draft_html:
            return reply_html

        lowered = draft_html.lower()
        start = lowered.find("<body")
        if start != -1:
            end = draft_html.find(">", start)
            if end != -1:
                return draft_html[: end + 1] + reply_html + draft_html[end + 1 :]
        return reply_html + draft_html

    def create_reply_draft(self, message_id: str, body_html: str) -> dict:
        """Create a real reply draft in the Drafts folder.

        createReply is used rather than composing a new message so Outlook keeps
        the conversation threading, recipients and Re: subject that a hand-built
        message would get wrong.
        """
        draft = self._request("POST", f"/me/messages/{message_id}/createReply").json()
        quoted = (draft.get("body") or {}).get("content") or ""
        return self._request(
            "PATCH",
            f"/me/messages/{draft['id']}?$select=id,webLink",
            json={"body": {"contentType": "HTML",
                           "content": self._above_quote(body_html, quoted)}},
        ).json()

    def send_reply(self, message_id: str, body_html: str) -> dict:
        """Create a reply draft, fill it in, and send it - in that order.

        Used by the "approve and send" button. Going through createReply rather
        than composing a fresh message keeps Outlook's threading, recipients and
        Re: subject, exactly as the draft-only path does; the difference is only
        that nothing lingers in the Drafts folder.

        If the send fails after the draft exists, the draft is left in place
        rather than cleaned up: a reply Alex can find and send by hand beats a
        silent rollback that loses the text.
        """
        draft = self._request("POST", f"/me/messages/{message_id}/createReply").json()
        quoted = (draft.get("body") or {}).get("content") or ""
        self._request(
            "PATCH",
            f"/me/messages/{draft['id']}",
            json={"body": {"contentType": "HTML",
                           "content": self._above_quote(body_html, quoted)}},
        )
        self._request("POST", f"/me/messages/{draft['id']}/send")
        return draft

    def send_existing_draft(self, draft_id: str, body_html: str | None = None) -> None:
        """Send a draft that already exists, optionally rewriting it first.

        The rewrite keeps whatever is already below the reply - the quoted
        original - by injecting the new text at the top rather than replacing
        the document.
        """
        if body_html is not None:
            current = self._request(
                "GET", f"/me/messages/{draft_id}?$select=body"
            ).json()
            quoted = (current.get("body") or {}).get("content") or ""
            self._request(
                "PATCH",
                f"/me/messages/{draft_id}",
                json={"body": {"contentType": "HTML",
                               "content": self._above_quote(body_html, quoted)}},
            )
        self._request("POST", f"/me/messages/{draft_id}/send")

    def delete_message(self, message_id: str) -> None:
        """Move a message to Deleted Items. Used to bin an abandoned draft."""
        self._request("DELETE", f"/me/messages/{message_id}")

    def move_message(self, message_id: str, destination: str) -> dict:
        """Move a message to a well-known folder. Returns the moved message.

        The id CHANGES on a move - Graph mints a new one in the destination
        folder - so anything holding the old id must use the returned record
        rather than assume its own copy still resolves.
        """
        return self._request(
            "POST", f"/me/messages/{message_id}/move",
            json={"destinationId": destination},
        ).json()

    def empty_folder(self, folder: str, cap: int = 1000) -> int:
        """Move everything in a folder to Deleted Items. Returns the count.

        Deliberately a soft delete. An automated weekly purge that destroys
        mail outright is one bug away from being unrecoverable, and Deleted
        Items costs nothing and holds for thirty days.

        Re-queries from the top each pass rather than paging: every delete
        shifts the pages underneath a cursor, and a paged loop over a
        shrinking collection silently skips messages.
        """
        removed = 0
        while removed < cap:
            try:
                payload = self._request(
                    "GET", f"/me/mailFolders/{folder}/messages",
                    params={"$top": "50", "$select": "id"},
                ).json()
            except GraphError as exc:
                if getattr(exc, "status", 0) == 404:
                    return removed
                raise

            batch = payload.get("value") or []
            if not batch:
                return removed

            progressed = False
            for message in batch:
                try:
                    self.delete_message(message["id"])
                    removed += 1
                    progressed = True
                except GraphError:
                    # One stuck message must not spin this loop forever.
                    log.warning("Could not clear message %s from %s",
                                message.get("id"), folder)
                if removed >= cap:
                    break
            if not progressed:
                return removed
        log.warning("Stopped emptying %s at the %s-message cap.", folder, cap)
        return removed

    def send_mail(self, to: list[str], subject: str, body_html: str) -> None:
        self._request(
            "POST",
            "/me/sendMail",
            json={
                "message": {
                    "subject": subject,
                    "body": {"contentType": "HTML", "content": body_html},
                    "toRecipients": [{"emailAddress": {"address": a}} for a in to],
                },
                "saveToSentItems": True,
            },
        )

    def mark_read(self, message_id: str) -> None:
        """DELIBERATELY NEVER CALLED. Do not wire this up.

        Unread is Alex's own signal for what he has not dealt with. An
        assistant that reads his mail and marks it read would quietly erase
        that signal from underneath him - the inbox would look handled while
        nothing had been.

        Triage therefore reads `isRead` as evidence and never writes it, which
        is also why the classifier is told a read message can still owe a
        reply. Kept here only so that the decision is visible rather than an
        absence someone re-adds by accident.
        """
        self._request("PATCH", f"/me/messages/{message_id}", json={"isRead": True})

    # ---- calendar -------------------------------------------------------

    def calendar_view(self, start: dt.datetime, end: dt.datetime, timezone: str) -> list[dict]:
        """Expanded calendar view, which resolves recurring series properly.

        /me/events returns the recurrence master, not the instances; using it
        here is the classic way to end up offering a slot that is actually
        taken every other Tuesday.
        """
        params = {
            "startDateTime": start.isoformat(),
            "endDateTime": end.isoformat(),
            # organizer/attendees/location/webLink are what turn a row in an
            # agenda into something you can act on: who called the meeting, who
            # else is coming, where it is, and a link straight to the event.
            # Without them the agenda can only say a time and a title.
            "$select": (
                "subject,start,end,isAllDay,showAs,isCancelled,sensitivity,"
                "organizer,attendees,location,locations,webLink,bodyPreview,"
                "isOnlineMeeting,onlineMeeting,responseStatus,seriesMasterId,type"
            ),
            "$orderby": "start/dateTime",
            "$top": "100",
        }
        headers = {"Prefer": f'outlook.timezone="{timezone}"'}
        return list(self._paged("/me/calendarView", params=params, headers=headers))

    def create_event(
        self,
        subject: str,
        start: dt.datetime,
        end: dt.datetime,
        timezone: str,
        show_as: str = "busy",
        body: str = "",
    ) -> dict:
        """Put a blocking event on the calendar.

        No attendees, ever. Adding one makes Graph send an invitation the
        moment the event is created, so a private "keep this hour free" block
        would become mail in somebody else's inbox.
        """
        payload = {
            "subject": subject[:255],
            "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": timezone},
            "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": timezone},
            "showAs": show_as,
            "isReminderOn": False,
        }
        if body:
            payload["body"] = {"contentType": "text", "content": body}
        return self._request("POST", "/me/events", json=payload).json()

    # ---- contacts -------------------------------------------------------

    def contacts(self) -> list[dict]:
        params = {"$select": "displayName,emailAddresses", "$top": "100"}
        return list(self._paged("/me/contacts", params=params))

    def create_contact(
        self,
        display_name: str,
        email: str | None = None,
        emails: list[str] | None = None,
        given_name: str | None = None,
        surname: str | None = None,
    ) -> dict:
        """Create a contact, optionally with several addresses.

        Multiple addresses matter: a person who writes from both a work and a
        personal account should be ONE contact, not two. Two entries for the
        same human is precisely what makes type-ahead ambiguous - which is the
        problem this is meant to solve, not add to.

        The first address in `emails` is the primary one Outlook offers first.
        givenName and surname are set where they can be worked out, because
        Outlook sorts and searches on them, not on displayName alone.
        """
        addresses = list(emails or ([email] if email else []))
        if not addresses:
            raise ValueError("a contact needs at least one address")

        payload: dict[str, Any] = {
            "displayName": display_name,
            # Graph caps a contact at three addresses.
            "emailAddresses": [
                {"address": a, "name": display_name} for a in addresses[:3]
            ],
        }
        if given_name:
            payload["givenName"] = given_name
        if surname:
            payload["surname"] = surname
        if not given_name and not surname:
            payload["givenName"] = display_name

        return self._request("POST", "/me/contacts", json=payload).json()

    def delete_contact(self, contact_id: str) -> None:
        """Used by the backfill's rollback path."""
        self._request("DELETE", f"/me/contacts/{contact_id}")


def client_from_env() -> GraphClient:
    tokens = TokenManager(
        tenant_id=os.environ["GRAPH_TENANT_ID"],
        client_id=os.environ["GRAPH_CLIENT_ID"],
        client_secret=os.environ["GRAPH_CLIENT_SECRET"],
    )
    return GraphClient(tokens)
