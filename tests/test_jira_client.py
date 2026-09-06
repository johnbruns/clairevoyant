"""Tests for the Jira query shape and response parsing.

Same stdlib-only discipline as test_graph_client: `requests` is stubbed into
sys.modules rather than imported, so this runs against a bare interpreter with
no network and no API token.
"""

import base64
import json
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def json(self):
        return self._payload


class RecordingSession:
    """Stands in for requests.Session and records every call made through it."""

    def __init__(self, responses=None):
        self.calls = []
        self._responses = list(responses or [FakeResponse({"issues": []})])

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if not self._responses:
            return FakeResponse({"issues": []})
        return self._responses.pop(0)


_fake_requests = types.ModuleType("requests")
_fake_requests.Session = RecordingSession
sys.modules.setdefault("requests", _fake_requests)

from shared import jira_client  # noqa: E402
from shared.jira_client import JiraClient, JiraError, adf_to_text  # noqa: E402


def make_client(responses=None):
    session = RecordingSession(responses)
    _fake_requests.Session = lambda: session
    client = JiraClient("https://crc.atlassian.net/", "alex@example.org", "tok")
    return client, session


def issue_json(key="TASK-1", summary="Do the thing", status="To Do", **fields):
    base = {
        "summary": summary,
        "status": {"name": status, "statusCategory": {"key": "new"}},
        "priority": {"name": "High"},
        "duedate": "2026-09-04",
        "labels": ["grant"],
        "issuetype": {"name": "Task"},
        "updated": "2026-08-30T11:12:13.000-0400",
    }
    base.update(fields)
    return {"key": key, "fields": base}


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


def test_uses_the_enhanced_search_endpoint():
    # /rest/api/3/search is deprecated. Hitting it by default is a slow-motion
    # outage: it works until Atlassian turns it off on the site.
    client, session = make_client()
    client.search("project = TASK")
    assert session.calls[0]["url"].endswith("/rest/api/3/search/jql"), session.calls[0]["url"]


def test_fields_are_named_explicitly():
    # The enhanced endpoint returns id and key ONLY unless fields are requested.
    # Without this the agenda would show ticket numbers and nothing else.
    client, session = make_client()
    client.search("project = TASK")
    sent = session.calls[0]["json"]["fields"]
    for required in ("summary", "status", "priority", "duedate"):
        assert required in sent, sent


def test_basic_auth_header_is_built_correctly():
    client, session = make_client()
    client.search("project = TASK")
    header = session.calls[0]["headers"]["Authorization"]
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
    assert decoded == "alex@example.org:tok", decoded


def test_trailing_slash_on_base_url_does_not_double_up():
    client, session = make_client()
    client.search("project = TASK")
    assert "//rest" not in session.calls[0]["url"], session.calls[0]["url"]


def test_issue_fields_are_parsed():
    client, _ = make_client([FakeResponse({"issues": [issue_json()], "isLast": True})])
    issues = client.search("project = TASK")

    assert len(issues) == 1
    issue = issues[0]
    assert issue.key == "TASK-1"
    assert issue.summary == "Do the thing"
    assert issue.status == "To Do"
    assert issue.priority == "High"
    assert issue.due == "2026-09-04"
    assert issue.labels == ("grant",)
    assert issue.updated == "2026-08-30", issue.updated
    assert issue.url == "https://crc.atlassian.net/browse/TASK-1", issue.url


def test_missing_optional_fields_do_not_crash():
    # Priority and due date are routinely unset on a real board.
    raw = {"key": "TASK-2", "fields": {"summary": "Bare", "status": {"name": "To Do"}}}
    client, _ = make_client([FakeResponse({"issues": [raw], "isLast": True})])
    issue = client.search("project = TASK")[0]
    assert issue.priority is None and issue.due is None
    assert issue.labels == ()


def test_pagination_follows_next_page_token():
    # The enhanced endpoint pages by opaque token, not startAt. Treating it like
    # the old API returns page one forever.
    pages = [
        FakeResponse({"issues": [issue_json("TASK-1")], "nextPageToken": "abc"}),
        FakeResponse({"issues": [issue_json("TASK-2")], "isLast": True}),
    ]
    client, session = make_client(pages)
    issues = client.search("project = TASK", limit=10)

    assert [i.key for i in issues] == ["TASK-1", "TASK-2"]
    assert session.calls[1]["json"]["nextPageToken"] == "abc"


def test_limit_is_honoured():
    pages = [FakeResponse({"issues": [issue_json(f"TASK-{i}") for i in range(5)], "isLast": True})]
    client, _ = make_client(pages)
    assert len(client.search("project = TASK", limit=3)) == 3


def test_falls_back_to_the_legacy_endpoint_on_404():
    pages = [
        FakeResponse({"errorMessages": ["gone"]}, status=404),
        FakeResponse({"issues": [issue_json("TASK-9")]}),
    ]
    client, session = make_client(pages)
    issues = client.search("project = TASK")

    assert [i.key for i in issues] == ["TASK-9"]
    assert session.calls[1]["url"].endswith("/rest/api/3/search"), session.calls[1]["url"]


def test_a_401_is_raised_not_swallowed_into_a_fallback():
    # A bad token must surface as an error Alex can see, not silently retry a
    # second endpoint and report an empty backlog.
    client, _ = make_client([FakeResponse({"message": "Unauthorized"}, status=401)])
    try:
        client.search("project = TASK")
    except JiraError as exc:
        assert exc.status == 401
    else:
        raise AssertionError("expected JiraError")


def test_todo_query_targets_the_board_column_by_status_category():
    # Status NAMES differ per project (To Do / Backlog / Selected for
    # Development) but all of them roll up to the same category.
    client, session = make_client()
    client.todo_issues("TASK")
    jql = session.calls[0]["json"]["jql"]

    assert 'project = "TASK"' in jql, jql
    assert 'statusCategory = "To Do"' in jql, jql
    assert "assignee = currentUser()" in jql, jql
    assert "resolution = EMPTY" in jql, jql


def test_jql_can_be_overridden_from_config():
    os.environ["JIRA_JQL"] = "project = TASK AND sprint in openSprints()"
    try:
        client, session = make_client()
        client.todo_issues("TASK")
        assert session.calls[0]["json"]["jql"] == "project = TASK AND sprint in openSprints()"
    finally:
        del os.environ["JIRA_JQL"]


def test_client_from_env_returns_none_when_unconfigured():
    # Jira missing must cost the recommendations section, not the whole agenda.
    for key in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        os.environ.pop(key, None)
    assert jira_client.client_from_env() is None


def test_client_from_env_needs_all_three_settings():
    os.environ["JIRA_BASE_URL"] = "https://crc.atlassian.net"
    os.environ["JIRA_EMAIL"] = "alex@example.org"
    os.environ.pop("JIRA_API_TOKEN", None)
    try:
        assert jira_client.client_from_env() is None, "a missing token must not build a client"
    finally:
        os.environ.pop("JIRA_BASE_URL", None)
        os.environ.pop("JIRA_EMAIL", None)


# ---- Atlassian Document Format ------------------------------------------


def test_adf_is_flattened_to_text():
    doc = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Draft the letter."}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Send it to Priya."}]},
        ],
    }
    assert adf_to_text(doc) == "Draft the letter. Send it to Priya."


def test_adf_handles_nested_lists():
    doc = {
        "type": "doc",
        "content": [{
            "type": "bulletList",
            "content": [{
                "type": "listItem",
                "content": [{
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "one"}],
                }],
            }],
        }],
    }
    assert adf_to_text(doc) == "one"


def test_adf_missing_or_empty_is_empty_string():
    assert adf_to_text(None) == ""
    assert adf_to_text({}) == ""
    assert adf_to_text({"type": "doc", "content": []}) == ""


def test_adf_is_truncated():
    doc = {"type": "doc", "content": [
        {"type": "text", "text": "x" * 50} for _ in range(100)
    ]}
    assert len(adf_to_text(doc, limit=100)) == 100


def test_plain_string_description_still_works():
    # Some endpoints and some older issues return a plain string.
    assert adf_to_text("just text") == "just text"


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(name, fn) for name, fn in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
