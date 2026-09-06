"""Who is on the team and where their work lives.

Discovered from the live site on 4 September 2026 rather than guessed, which
matters because two of these are not what you would predict:

  - Marco's task project has the key **NCT**, not LT or LS. The key is the
    organisation's initials; the project name is "Marco's Tasks".
  - Dana has no personal project. His work sits in **OPS**
    ("IT & Cyber Operations Tooling"), which is a shared project, so his scope
    filters on assignee as well. Without that filter his digest would include
    everyone else's tooling tickets.

Status NAMES differ per project on this site - TASK uses "To Do"/"Done", OPS
uses "To Be Qualified"/"Working" - so every query here filters on
statusCategory, which is consistent everywhere.

Override the whole roster with the TEAM_ROSTER app setting: a JSON array of
{"name": ..., "jql": ...}. That is the escape hatch for a new hire or a
project rename, so neither needs a redeploy.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Account ids live in TEAM_ROSTER, not in source. See below.


@dataclass(frozen=True)
class Teammate:
    name: str
    jql: str          # scope: everything of theirs, any status


# Empty on purpose. A roster is specific to one organisation's Jira, and a
# built-in guess would query projects that do not exist and report an empty
# team every week. Switch the team digests on with TEAM_ROSTER, for example:
#
#   [{"name": "Sam",   "jql": "project = 'ST'"},
#    {"name": "Priya", "jql": "project = 'PT'"},
#    {"name": "Dana",  "jql": "project = 'OPS' AND assignee = '712020:...'"}]
#
# The owner belongs in it too - seeing your own load beside everyone else's is
# the point of the digest. Find an account id at
# /rest/api/3/user/search?query=<email> on your own Jira site.
DEFAULT_ROSTER: tuple["Teammate", ...] = ()


def roster() -> tuple[Teammate, ...]:
    raw = os.environ.get("TEAM_ROSTER", "").strip()
    if not raw:
        return DEFAULT_ROSTER
    try:
        parsed = json.loads(raw)
        found = tuple(
            Teammate(str(entry["name"]), str(entry["jql"]))
            for entry in parsed
            if entry.get("name") and entry.get("jql")
        )
    except (ValueError, TypeError, KeyError):
        log.exception("TEAM_ROSTER is not valid JSON; using the built-in roster.")
        return DEFAULT_ROSTER
    if not found:
        log.warning("TEAM_ROSTER parsed to nothing; using the built-in roster.")
        return DEFAULT_ROSTER
    return found
