"""Jira Cloud REST access for the agenda, task digests, and reply-to-update.

MOSTLY read. Three write operations exist - transition, comment, set due date -
and they are reachable from exactly one place: a reply Alex sends to his own
task digest, from his own mailbox, matched to a digest marker the assistant
generated. Nothing else in the codebase calls them.

That is a real escalation from the original read-only design and is worth
saying out loud: a bug in the reply parser can now change the board, not just
misreport it. Three things bound the damage. Operations are restricted to
issues that were listed in the digest being replied to, so a bad parse cannot
reach an arbitrary issue. Transitions are chosen from the ones Jira itself
offers for that issue, so an invented status is impossible. And every applied
change is echoed back in a confirmation email, so a wrong one is visible
within the hour rather than discovered weeks later.

Issues are never deleted and no field is overwritten destructively; the worst
case is a wrong status on a known issue, which is one click to undo in Jira.

Two Jira-specific things this file exists to absorb:

1. `/rest/api/3/search` is deprecated. Atlassian replaced it with
   `/rest/api/3/search/jql`, which pages by opaque `nextPageToken` instead of
   `startAt` and requires `fields` to be named explicitly - the new endpoint
   returns id and key alone if you do not ask for more. The old path is tried
   only as a fallback, for sites where the new one is not answering yet.

2. Descriptions come back as Atlassian Document Format, a nested JSON tree, not
   text. `adf_to_text` flattens it. Without that the model gets a wall of
   `{"type": "paragraph", "content": [...]}` and spends its tokens on the
   envelope rather than the issue.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

API = "/rest/api/3"

# Named explicitly: the enhanced search endpoint returns nothing but id and key
# otherwise. Description is fetched but truncated hard - the agenda needs enough
# to tell two similarly named tickets apart, not the whole ticket.
ISSUE_FIELDS = [
    "summary",
    "status",
    "priority",
    "resolutiondate",
    "duedate",
    "labels",
    "issuetype",
    "parent",
    "updated",
    "created",
    "description",
]

MAX_DESCRIPTION_CHARS = 400


class JiraError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"Jira returned {status}: {body[:400]}")
        self.status = status
        self.body = body


@dataclass
class Issue:
    key: str
    summary: str
    status: str
    # The category ("To Do" / "In Progress" / "Done") rather than the name.
    # Status names are per-project on this site - TASK says "Done", OPS says
    # "Working" - so anything that groups or filters must use the category.
    status_category: str = ""
    priority: str | None = None
    due: str | None = None
    labels: tuple[str, ...] = ()
    issue_type: str | None = None
    parent: str | None = None
    updated: str | None = None
    created: str | None = None
    resolved: str | None = None
    description: str = ""
    url: str = ""

    def for_model(self) -> dict:
        """The subset worth spending tokens on."""
        return {
            "key": self.key,
            "summary": self.summary,
            "status": self.status,
            "status_category": self.status_category,
            "priority": self.priority,
            "due": self.due,
            "type": self.issue_type,
            "labels": list(self.labels),
            "parent": self.parent,
            "last_updated": self.updated,
            "description": self.description,
        }


def adf_to_text(node: Any, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    """Flatten an Atlassian Document Format tree to plain text.

    Deliberately crude: text nodes joined, block nodes separated by a space.
    Formatting, panels, tables and media all reduce to their text, which is all
    the agenda needs.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node

    chunks: list[str] = []

    def walk(item: Any) -> None:
        if len(" ".join(chunks)) > limit:
            return
        if isinstance(item, list):
            for child in item:
                walk(child)
            return
        if not isinstance(item, dict):
            return
        if item.get("type") == "text" and item.get("text"):
            chunks.append(item["text"])
        elif item.get("type") == "hardBreak":
            chunks.append(" ")
        walk(item.get("content"))

    walk(node)
    text = " ".join(c for c in chunks if c).strip()
    text = " ".join(text.split())
    return text[:limit]


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, timeout: int = 30):
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()
        self._email = email
        # A short hash, never the token. Enough to tell "the app is using the
        # value I think it is" from "the app is using a stale one", which is
        # not answerable any other way without printing a secret to a log.
        self.token_fingerprint = hashlib.sha256(api_token.encode()).hexdigest()[:12]
        self.token_length = len(api_token)
        token = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict) -> dict:
        resp = self._session.post(
            f"{self._base}{path}",
            headers=self._headers,
            json=payload,
            timeout=self._timeout,
        )
        if not resp.ok:
            raise JiraError(resp.status_code, resp.text)
        return self._json(resp)

    def _get(self, path: str, params: dict | None = None) -> dict:
        resp = self._session.get(
            f"{self._base}{path}",
            headers=self._headers,
            params=params or {},
            timeout=self._timeout,
        )
        if not resp.ok:
            raise JiraError(resp.status_code, resp.text)
        return self._json(resp)

    def _put(self, path: str, payload: dict) -> None:
        resp = self._session.put(
            f"{self._base}{path}",
            headers=self._headers,
            json=payload,
            timeout=self._timeout,
        )
        if not resp.ok:
            raise JiraError(resp.status_code, resp.text)

    @staticmethod
    def _json(resp) -> dict:
        """Jira answers 204 with an empty body for transitions and edits.

        Anything else that will not parse as JSON is an ERROR, not an empty
        result. The first version of this swallowed ValueError and returned
        {}, which turned "the response was a WAF challenge page" into "this
        person finished nothing and has nothing open" - a wrong answer that
        looks like a real one. Silence is the worst possible failure mode for
        a digest.
        """
        if getattr(resp, "status_code", 200) == 204:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            body = (getattr(resp, "text", "") or "")[:300]
            raise JiraError(
                getattr(resp, "status_code", 0),
                f"response was not JSON ({exc}): {body}",
            ) from exc

    def whoami(self) -> dict:
        """Who Jira thinks this client is. The first thing to check when a
        query returns an empty result that should not be empty."""
        return self._get(f"{API}/myself")

    def search(self, jql: str, limit: int = 50) -> list[Issue]:
        """Run a JQL query, newest API first, old API as a fallback."""
        try:
            return self._search_enhanced(jql, limit)
        except JiraError as exc:
            if exc.status not in (404, 410):
                raise
            log.warning("Enhanced search unavailable (%s); falling back to /search.", exc.status)
            return self._search_legacy(jql, limit)

    def _search_enhanced(self, jql: str, limit: int) -> list[Issue]:
        issues: list[Issue] = []
        token: str | None = None

        while len(issues) < limit:
            payload: dict[str, Any] = {
                "jql": jql,
                "maxResults": min(limit - len(issues), 50),
                "fields": ISSUE_FIELDS,
            }
            if token:
                payload["nextPageToken"] = token

            data = self._post(f"{API}/search/jql", payload)
            batch = data.get("issues", [])
            if not batch and not issues:
                # An empty first page is worth a line in the log: it is the
                # difference between "nothing matched" and "something is wrong
                # with the query or the credentials".
                log.info("Jira returned 0 issues for JQL: %s (keys: %s)",
                         jql, sorted(data)[:8])
            issues.extend(self._to_issue(raw) for raw in batch)

            token = data.get("nextPageToken")
            if not token or data.get("isLast"):
                break

        return issues[:limit]

    def _search_legacy(self, jql: str, limit: int) -> list[Issue]:
        data = self._post(
            f"{API}/search",
            {"jql": jql, "maxResults": limit, "fields": ISSUE_FIELDS},
        )
        return [self._to_issue(raw) for raw in data.get("issues", [])][:limit]

    def _to_issue(self, raw: dict) -> Issue:
        fields = raw.get("fields") or {}
        parent = (fields.get("parent") or {}).get("fields") or {}
        status = fields.get("status") or {}
        return Issue(
            key=raw.get("key", "?"),
            summary=fields.get("summary") or "(no summary)",
            status=status.get("name") or "unknown",
            status_category=((status.get("statusCategory") or {}).get("name")) or "",
            priority=((fields.get("priority") or {}).get("name")),
            resolved=(fields.get("resolutiondate") or "")[:10] or None,
            due=fields.get("duedate"),
            labels=tuple(fields.get("labels") or ()),
            issue_type=((fields.get("issuetype") or {}).get("name")),
            parent=parent.get("summary"),
            updated=(fields.get("updated") or "")[:10] or None,
            created=(fields.get("created") or "")[:10] or None,
            description=adf_to_text(fields.get("description")),
            url=f"{self._base}/browse/{raw.get('key', '')}",
        )

    def todo_issues(self, project_key: str = "TASK", limit: int = 50) -> list[Issue]:
        """Everything on Alex's to-do list on the board.

        `statusCategory = "To Do"` rather than a named status: Jira's To Do
        column can contain several statuses (To Do, Backlog, Selected for
        Development) and their names differ per project, but every one of them
        rolls up to the same status category. `resolution = EMPTY` drops issues
        closed straight out of To Do without a status change.
        """
        jql = os.environ.get("JIRA_JQL") or (
            f'project = "{project_key}" '
            'AND statusCategory = "To Do" '
            "AND assignee = currentUser() "
            "AND resolution = EMPTY "
            "ORDER BY priority DESC, duedate ASC, updated DESC"
        )
        return self.search(jql, limit=limit)


    # ---- queries used by the digests ------------------------------------

    def my_open_issues(self, project_key: str = "TASK", limit: int = 100) -> list[Issue]:
        """Everything of Alex's that is not finished, worst-first.

        Wider than `todo_issues`: this one includes In Progress, because the
        Monday digest is "here is everything on your plate", not "here is what
        to start next".
        """
        jql = os.environ.get("JIRA_MY_TASKS_JQL") or (
            f'project = "{project_key}" '
            'AND statusCategory != "Done" '
            "AND assignee = currentUser() "
            "ORDER BY duedate ASC, priority DESC, updated DESC"
        )
        return self.search(jql, limit=limit)

    def done_since(self, scope_jql: str, days: int = 7, limit: int = 50) -> list[Issue]:
        """Issues under `scope_jql` resolved in the last `days` days.

        Uses `resolved`, not "status is Done and updated recently". Every Done
        issue on this site carries a resolutiondate, and `updated` would count
        an issue somebody merely re-labelled this week as finished this week.
        """
        jql = (
            f"({scope_jql}) AND statusCategory = Done AND resolved >= -{int(days)}d "
            "ORDER BY resolved DESC"
        )
        return self.search(jql, limit=limit)

    def open_under(self, scope_jql: str, limit: int = 100) -> list[Issue]:
        jql = (
            f'({scope_jql}) AND statusCategory != "Done" '
            "ORDER BY duedate ASC, priority DESC, updated DESC"
        )
        return self.search(jql, limit=limit)

    # ---- writes ---------------------------------------------------------
    #
    # Reachable only from the digest-reply path. See the module docstring.

    def transitions(self, issue_key: str) -> list[dict]:
        """The transitions Jira will actually accept for this issue right now.

        Asked rather than assumed. Status names differ per project on this site
        - TASK uses "Done", OPS uses "Working" and "To Be Qualified" - and a
        hardcoded transition id would break the moment a workflow changed.
        """
        data = self._get(f"{API}/issue/{issue_key}/transitions")
        return data.get("transitions") or []

    def find_transition(self, issue_key: str, target_category: str) -> dict | None:
        """Pick the transition landing in a status category ("Done", "In Progress").

        Matched on the destination's status CATEGORY, not its name, because
        the name is per-project and the category is not.
        """
        wanted = target_category.strip().lower()
        for option in self.transitions(issue_key):
            to = option.get("to") or {}
            category = ((to.get("statusCategory") or {}).get("name") or "").lower()
            if category == wanted:
                return option
        return None

    def transition(self, issue_key: str, transition_id: str) -> None:
        self._post(f"{API}/issue/{issue_key}/transitions",
                   {"transition": {"id": str(transition_id)}})

    def comment(self, issue_key: str, text: str) -> None:
        self._post(f"{API}/issue/{issue_key}/comment", {"body": text_to_adf(text)})

    def set_due_date(self, issue_key: str, due: str) -> None:
        """`due` is YYYY-MM-DD, or None-ish to clear it."""
        self._put(f"{API}/issue/{issue_key}", {"fields": {"duedate": due or None}})

    def create_issue(
        self,
        project_key: str,
        summary: str,
        assignee_account_id: str | None = None,
        description: str = "",
        issue_type: str = "Task",
        due: str | None = None,
    ) -> dict:
        """Create an issue and return {key, url}.

        Assignee is set in the SAME call rather than by a follow-up edit. A
        create that succeeds followed by an assign that fails leaves an
        unassigned orphan in the board that nobody is watching, which is worse
        than a create that fails outright and gets reported.
        """
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary[:250],
            "issuetype": {"name": issue_type},
        }
        if assignee_account_id:
            fields["assignee"] = {"accountId": assignee_account_id}
        if description:
            fields["description"] = text_to_adf(description)
        if due:
            fields["duedate"] = due

        created = self._post(f"{API}/issue", {"fields": fields})
        key = created.get("key", "")
        return {"key": key, "url": f"{self._base}/browse/{key}" if key else ""}


def text_to_adf(text: str) -> dict:
    """Plain text -> a minimal Atlassian Document Format doc.

    v3 of the API refuses a plain string for a comment body. One paragraph per
    blank-line-separated block is all the structure a comment from an email
    reply ever needs.
    """
    blocks = [b.strip() for b in (text or "").split("\n\n") if b.strip()] or [""]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": block}]}
            for block in blocks
        ],
    }


def client_from_env() -> JiraClient | None:
    """Build a client, or None when Jira is not configured.

    None rather than an exception. The agenda is useful without Jira, and an
    unset token must degrade the recommendations section rather than cost Alex
    the whole email.
    """
    base = os.environ.get("JIRA_BASE_URL")
    email = os.environ.get("JIRA_EMAIL")
    token = os.environ.get("JIRA_API_TOKEN")
    if not (base and email and token):
        log.info("Jira not configured; agenda will omit recommendations.")
        return None
    return JiraClient(base, email, token)
