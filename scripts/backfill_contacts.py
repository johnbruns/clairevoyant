#!/usr/bin/env python3
"""Add the people Alex actually corresponds with to his Contacts.

    python scripts/backfill_contacts.py            # dry run, creates nothing
    python scripts/backfill_contacts.py --apply    # create them
    python scripts/backfill_contacts.py --undo <created.json>

WHY SENT ITEMS AND NOT THE INBOX. Receiving mail from someone says nothing -
every newsletter and vendor does that. REPLYING is the signal: a person Alex
has written to five times is, by definition, someone he corresponds with. The
hourly triage only ever sees who wrote to him, in a 48-hour window, which is
why 311 of his 358 real correspondents were missing from a 136-entry contacts
list while type-ahead quietly failed.

MERGING. A person with two addresses must be ONE contact. Two entries for the
same human is exactly the ambiguity that makes type-ahead unhelpful, so
merging is not a tidiness nicety here - it is the point.

Every created contact id is written to a JSON file so the whole run can be
undone with one command.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from shared.contacts import derive_name, split_name  # noqa: E402
from shared.contacts import NOISE, ROLE  # noqa: E402
from shared.graph_client import client_from_env  # noqa: E402

# Every address that is you. Anything sent only between these is not a
# correspondent, and an alias left out here becomes a contact for yourself.
ME = {a.strip().lower() for a in os.environ.get("OWNER_ADDRESSES", "").split(",") if a.strip()}
ME.add(os.environ.get("ASSISTANT_EMAIL", "").strip().lower())
ME.discard("")

# People who write from two addresses. Primary first - that is the one
# Outlook offers first in type-ahead. Fill this in for your own contacts;
# empty simply means no merging is attempted.
#
# A useful rule of thumb: lead with the address you would actually reply to,
# which is not always the work one.
MERGES: list[list[str]] = [
    # ["sam@example.org", "sam.okafor@personal.example.com"],
]

# Addresses that should be used verbatim as the contact name, because no
# message ever carried a display name and the address is the only handle
# there is - findable, and obviously incomplete rather than invented.
ADDRESS_AS_NAME: set[str] = set()

THRESHOLD = 5
SCAN_LIMIT = 2000
DAYS = 365


def gather(graph):
    known = set()
    for c in graph._paged("/me/contacts", params={"$select": "emailAddresses", "$top": "100"}):
        for e in c.get("emailAddresses") or []:
            if e.get("address"):
                known.add(e["address"].lower())

    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    params = {
        "$filter": f"sentDateTime ge {since}",
        "$orderby": "sentDateTime desc",
        "$top": "100",
        "$select": "toRecipients,ccRecipients,sentDateTime",
    }
    counts, names, scanned = collections.Counter(), {}, 0
    for message in graph._paged("/me/mailFolders/sentitems/messages", params=params):
        scanned += 1
        for field in ("toRecipients", "ccRecipients"):
            for recipient in message.get(field) or []:
                node = recipient.get("emailAddress") or {}
                address = (node.get("address") or "").lower().strip()
                if not address or address in ME:
                    continue
                counts[address] += 1
                name = (node.get("name") or "").strip()
                if name and name.lower() != address and address not in names:
                    names[address] = name
        if scanned >= SCAN_LIMIT:
            break
    return known, counts, names, scanned


def plan(known, counts, names):
    """Merged groups first, then everyone else above the threshold."""
    merged_of = {addr: group[0] for group in MERGES for addr in group}
    entries, seen = [], set()

    for group in MERGES:
        total = sum(counts.get(a, 0) for a in group)
        present = [a for a in group if counts.get(a)]
        if not present or all(a in known for a in group):
            continue
        display = next((names[a] for a in group if a in names), derive_name(group[0]))
        entries.append({"display": display, "addresses": group, "count": total,
                        "merged": True})
        seen.update(group)

    for address, count in counts.items():
        if address in seen or address in merged_of or address in known:
            continue
        if NOISE.search(address) or ROLE.match(address) or count < THRESHOLD:
            continue
        display = (address if address in ADDRESS_AS_NAME
                   else names.get(address) or derive_name(address))
        entries.append({"display": display, "addresses": [address], "count": count,
                        "merged": False})

    entries.sort(key=lambda e: -e["count"])
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--undo", metavar="FILE")
    args = parser.parse_args()

    graph = client_from_env()

    if args.undo:
        created = json.loads(pathlib.Path(args.undo).read_text(encoding="utf-8"))
        removed = 0
        for row in created.get("created", []):
            try:
                graph.delete_contact(row["id"])
                removed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  could not delete {row['display']}: {str(exc)[:90]}")
        print(f"Removed {removed}/{len(created.get('created', []))} contact(s).")
        return 0

    known, counts, names, scanned = gather(graph)
    entries = plan(known, counts, names)

    print(f"{'APPLYING' if args.apply else 'DRY RUN - nothing will be created'}")
    print(f"Scanned {scanned} sent messages; {len(entries)} contact(s) to create.\n")

    merged = [e for e in entries if e["merged"]]
    if merged:
        print("Merged (one person, several addresses):")
        for entry in merged:
            print(f"  {entry['count']:4d}  {entry['display']}")
            for i, address in enumerate(entry["addresses"]):
                print(f"          {'primary' if i == 0 else 'also   '}  {address}")
        print()

    created = []
    for entry in entries:
        given, surname = split_name(entry["display"])
        if args.apply:
            try:
                result = graph.create_contact(
                    display_name=entry["display"],
                    emails=entry["addresses"],
                    given_name=given or None,
                    surname=surname or None,
                )
                created.append({"id": result.get("id"), "display": entry["display"],
                                "addresses": entry["addresses"]})
            except Exception as exc:  # noqa: BLE001
                print(f"  FAILED {entry['display']}: {str(exc)[:120]}")
                continue

    if args.apply:
        out = pathlib.Path(__file__).resolve().parents[2] / "contacts-created.json"
        out.write_text(json.dumps({"when": dt.datetime.now().isoformat(),
                                   "created": created}, indent=2), encoding="utf-8")
        print(f"Created {len(created)} contact(s).")
        print(f"Rollback file: {out}")
        print(f"  undo with: python scripts/backfill_contacts.py --undo \"{out}\"")
    else:
        print(f"Would create {len(entries)} contact(s). Re-run with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
