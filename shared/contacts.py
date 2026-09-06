"""Keeping the contacts directory in step with who Alex actually talks to.

THE SIGNAL IS REPLYING, NOT RECEIVING. Every newsletter, vendor and robot on
earth emails him; almost none of them are contacts. Writing back is what makes
someone a correspondent. Triage originally added contacts on receipt, in a
48-hour window, which is how a 136-entry directory ended up missing 311 real
correspondents - including his own team - while type-ahead quietly failed.

So contacts are added from two places, both keyed on his outbound mail:

  1. **The moment a reply is sent** from the recap. Immediate, and the person
     is told on the confirmation page.
  2. **A weekly sweep of Sent Items**, which catches everything he replied to
     by hand in Outlook - the majority. Without this the directory drifts out
     of date again the moment he stops using the buttons.

Role mailboxes (`info@`, `support@`) and automated senders are excluded: they
are destinations, not people, and putting them in type-ahead makes it worse.
"""

from __future__ import annotations

import collections
import datetime as dt
import logging
import re

log = logging.getLogger(__name__)

ROLE = re.compile(
    r"^(partners|info|noreply|no-reply|donotreply|support|billing|admin|contact|"
    r"hello|team|sales|help|notifications?|alerts?|updates?|careers|jobs|hr|"
    r"accounting|invoices?|security|abuse|postmaster|media)@",
    re.I,
)
NOISE = re.compile(
    r"(noreply|no-reply|donotreply|notification|mailer|bounce|@resource\.|"
    r"reply\.|calendar-notification|\.onmicrosoft\.com$)",
    re.I,
)


def is_person(address: str) -> bool:
    """Whether an address belongs to a human worth having in type-ahead."""
    address = (address or "").strip().lower()
    if not address or "@" not in address:
        return False
    return not (NOISE.search(address) or ROLE.match(address))


def known_addresses(graph) -> set[str]:
    known = set()
    for contact in graph._paged(
        "/me/contacts", params={"$select": "emailAddresses", "$top": "100"}
    ):
        for entry in contact.get("emailAddresses") or []:
            if entry.get("address"):
                known.add(entry["address"].lower())
    return known


def correspondents(graph, days: int = 30, limit: int = 500, exclude: set[str] | None = None):
    """Who Alex wrote to, and how often. Returns (counts, display names)."""
    exclude = {a.lower() for a in (exclude or set())}
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    params = {
        "$filter": f"sentDateTime ge {since}",
        "$orderby": "sentDateTime desc",
        "$top": "100",
        "$select": "toRecipients,ccRecipients,sentDateTime",
    }

    counts: collections.Counter = collections.Counter()
    names: dict[str, str] = {}
    scanned = 0
    for message in graph._paged("/me/mailFolders/sentitems/messages", params=params):
        scanned += 1
        for field in ("toRecipients", "ccRecipients"):
            for recipient in message.get(field) or []:
                node = recipient.get("emailAddress") or {}
                address = (node.get("address") or "").lower().strip()
                if not address or address in exclude:
                    continue
                counts[address] += 1
                name = (node.get("name") or "").strip()
                if name and name.lower() != address and address not in names:
                    names[address] = name
        if scanned >= limit:
            break
    return counts, names


def derive_name(address: str) -> str:
    local = address.split("@")[0]
    parts = [p for p in re.split(r"[._\-+0-9]+", local) if p]
    return " ".join(p.capitalize() for p in parts) or local


def split_name(display: str) -> tuple[str, str]:
    """given, surname. Handles "Jane Smith" and the .edu form "Smith, Jane".

    Outlook sorts and searches on these fields, not on displayName alone, so a
    contact with neither is findable by fewer of the things you might type.
    """
    display = (display or "").strip()
    if "," in display:
        surname, _, given = display.partition(",")
        return given.strip(), surname.strip()
    bits = display.split()
    if len(bits) >= 2:
        return " ".join(bits[:-1]), bits[-1]
    return display, ""


def add_person(graph, address: str, display: str | None = None) -> dict | None:
    """Add one correspondent. Returns the contact, or None if it was skipped.

    Never raises: a contact that could not be added must not cost the caller
    its actual job, which is sending a reply.
    """
    address = (address or "").strip().lower()
    if not is_person(address):
        return None

    name = (display or "").strip() or derive_name(address)
    given, surname = split_name(name)
    try:
        return graph.create_contact(
            display_name=name,
            emails=[address],
            given_name=given or None,
            surname=surname or None,
        )
    except Exception:  # noqa: BLE001
        log.exception("Could not add %s to contacts", address)
        return None


def sweep(graph, days: int = 30, threshold: int = 2, me: set[str] | None = None) -> list[str]:
    """Add anyone he has written to `threshold`+ times who is not a contact.

    Catches replies sent by hand in Outlook, which is most of them - the
    button path only sees the ones the assistant sent itself.
    """
    known = known_addresses(graph)
    counts, names = correspondents(graph, days=days, exclude=(me or set()))

    added = []
    for address, count in counts.items():
        if count < threshold or address in known or not is_person(address):
            continue
        contact = add_person(graph, address, names.get(address))
        if contact:
            added.append(names.get(address) or address)
    return added
