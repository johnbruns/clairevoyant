"""Signed, expiring, single-use tokens for the approve/edit/discard buttons.

Every button in the recap is a link to an anonymous HTTP endpoint that can send
mail as Alex. The token in the URL is the ONLY thing standing between that
endpoint and anyone who guesses a URL, so the rules here are deliberately
strict.

Three properties, each defending against a specific way this goes wrong:

1. **Signed.** HMAC-SHA256 over the payload with a secret that lives in Key
   Vault. Without this, the endpoint is a public "send arbitrary mail as Alex"
   API for anyone who can construct a pending id.

2. **Expiring.** A recap sits in a mailbox forever. A token that never expires
   is a live send-button in every backup, archive and forwarded copy of that
   email, years later.

3. **Single-use, enforced at the store, not here.** The token itself cannot
   know it has been spent, so `pending.py` marks the record and refuses a
   second action. Replay protection lives with the state, which is the only
   place it can be correct.

What this file does NOT defend against, because it cannot: anyone with read
access to Alex's mailbox can click these buttons. That is the same trust
boundary as the mailbox itself - someone in his inbox can already send mail as
him from Outlook - so the buttons add convenience, not exposure. The exposure
that WOULD be new is a leaked signing key, which is why it is a vault secret
and never an app setting literal.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time

log = logging.getLogger(__name__)

# Actions the endpoint will honour. A token naming anything else is rejected
# before it reaches a handler, so a typo cannot fall through to a default.
ACTIONS = ("send", "edit", "discard", "task_done", "block_time",
           "event_yes", "event_maybe", "event_no",
           "junk_rescue", "junk_calendar", "junk_block")

# The agenda's buttons reuse this whole mechanism. Their "pending id" is not a
# stored record but the thing itself - a Jira key for task_done, an ISO time
# range for block_time. That is safe precisely because the value is signed:
# the endpoint only ever acts on a string this assistant generated and put its
# own HMAC on, so there is nothing for a caller to tamper with.
AGENDA_ACTIONS = ("task_done", "block_time")

# The event digest's three answers. Same signed-token machinery again;
# the "pending id" here is a stored EventCandidate id, because an event
# carries far more state than a key or a time range.
EVENT_ACTIONS = ("event_yes", "event_maybe", "event_no")

# The Friday junk review. Its pending id is a stored JunkCandidate id, and its
# actions are the only ones in this file that MOVE or DELETE mail, which is
# why the confirmation pages for these say plainly where the message ends up.
JUNK_ACTIONS = ("junk_rescue", "junk_calendar", "junk_block")

DEFAULT_TTL_SECONDS = 7 * 24 * 3600  # a week, matching state.RETENTION_DAYS


class ActionError(Exception):
    """Token is unusable. The message is shown to Alex, so keep it plain."""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def signing_key() -> bytes | None:
    """The HMAC secret, or None when the feature is not configured.

    None disables the buttons rather than crashing the run: a recap with no
    buttons is still a useful recap, and an assistant that stops emailing
    because a secret is missing is worse than one that emails without buttons.
    """
    raw = os.environ.get("ACTION_SIGNING_KEY", "").strip()
    if not raw:
        return None
    if len(raw) < 32:
        # A short key is a typo or a placeholder, not a secret. Refusing is
        # safer than signing with something guessable.
        log.error("ACTION_SIGNING_KEY is shorter than 32 characters; buttons disabled.")
        return None
    return raw.encode()


def sign(action: str, pending_id: str, key: bytes, ttl: int = DEFAULT_TTL_SECONDS,
         now: float | None = None) -> str:
    """Build a token for one action on one pending draft."""
    if action not in ACTIONS:
        raise ActionError(f"unknown action {action!r}")

    now = time.time() if now is None else now
    payload = {"v": 1, "a": action, "p": pending_id, "e": int(now + ttl)}
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = _b64e(hmac.new(key, body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def verify(token: str, key: bytes, now: float | None = None) -> dict:
    """Return the payload of a valid token, or raise ActionError.

    Signature is checked BEFORE the payload is parsed as JSON, so unsigned
    input never reaches the parser.
    """
    if not token or "." not in token:
        raise ActionError("This link is malformed.")

    body, _, mac = token.partition(".")
    expected = _b64e(hmac.new(key, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, expected):
        # Constant-time compare: a timing oracle here would leak the signature
        # a byte at a time.
        raise ActionError("This link is not valid.")

    try:
        payload = json.loads(_b64d(body))
    except (ValueError, TypeError) as exc:
        raise ActionError("This link is malformed.") from exc

    if payload.get("v") != 1:
        raise ActionError("This link was made by an older version of the assistant.")
    if payload.get("a") not in ACTIONS:
        raise ActionError("This link asks for something the assistant does not do.")
    if not payload.get("p"):
        raise ActionError("This link does not name a draft.")

    now = time.time() if now is None else now
    if float(payload.get("e", 0)) < now:
        raise ActionError(
            "This link has expired. Recaps stay valid for a week; the draft "
            "itself is gone, but the original message is still in your inbox."
        )

    return payload


def base_url() -> str:
    """Where the buttons point.

    Derived from WEBSITE_HOSTNAME, which Azure sets on every Function App, so
    this needs no configuration in the normal case and cannot drift from the
    app it is actually running on. ACTION_BASE_URL overrides it for local runs
    and for a custom domain.
    """
    override = os.environ.get("ACTION_BASE_URL", "").strip().rstrip("/")
    if override:
        return override
    host = os.environ.get("WEBSITE_HOSTNAME", "").strip().rstrip("/")
    if not host:
        return ""
    return f"https://{host}/api"


def route_for(action: str) -> str:
    """Which endpoint handles this action.

    There are two, and getting this wrong is silent: an agenda token sent to
    /api/draft is a perfectly valid signed token that the draft handler then
    looks up as a pending draft, fails to find, and reports as "that draft is
    no longer available". The link looks right, the signature verifies, and
    the user is told something false about a feature that works.

    So the route is derived from the action rather than hardcoded at each call
    site. `test_the_url_route_matches_the_action` is the regression guard.
    """
    if action in JUNK_ACTIONS:
        return "junk"
    if action in EVENT_ACTIONS:
        return "event"
    return "agenda" if action in AGENDA_ACTIONS else "draft"


def action_url(action: str, pending_id: str, key: bytes, base: str | None = None,
               ttl: int = DEFAULT_TTL_SECONDS) -> str:
    base = base_url() if base is None else base.rstrip("/")
    if not base:
        return ""
    return f"{base}/{route_for(action)}?t={sign(action, pending_id, key, ttl=ttl)}"
