"""Tests for the Inbox query shape.

Three of these are named for the failures found on 2 September 2026: the
oldest-first cap that starved triage, the isRead filter that hid read mail, and
the missing Focused/Other signal. They assert on the query Graph is asked for,
which is where all three defects lived.
"""

import datetime as dt
import inspect
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.ok = 200 <= status < 300
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload


class RecordingSession:
    """Stands in for requests.Session and records every call made through it."""

    def __init__(self, pages=None):
        self.calls = []
        self._pages = list(pages or [{"value": []}])

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        payload = self._pages.pop(0) if self._pages else {"value": []}
        return FakeResponse(payload)


# graph_client imports requests at module scope. This suite is deliberately
# stdlib-only - no network, no credentials, no installed packages - so a stand-in
# goes into sys.modules before the import rather than pulling requests in.
_fake_requests = types.ModuleType("requests")
_fake_requests.Session = RecordingSession
_fake_requests.post = lambda *a, **k: FakeResponse({}, 500)
sys.modules.setdefault("requests", _fake_requests)

from shared.graph_client import GraphClient  # noqa: E402


class FakeTokens:
    def access_token(self):
        return "fake-access-token"


def make_client(pages=None):
    session = RecordingSession(pages)
    _fake_requests.Session = lambda: session
    return GraphClient(FakeTokens()), session


def message(mid, received):
    return {"id": mid, "receivedDateTime": received, "subject": mid}


SINCE = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc)


def params_of(session):
    return session.calls[0]["params"]


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


def test_orders_newest_first_so_a_capped_window_cannot_starve():
    # The original defect: "$orderby: receivedDateTime asc" with "$top" capped.
    # Once 50+ messages inside the lookback window were marked processed, every
    # run re-fetched that same oldest page, found nothing new, and reported
    # "nothing new" while newer mail was never queried at all.
    client, session = make_client()
    client.recent_messages(since=SINCE)
    assert params_of(session)["$orderby"] == "receivedDateTime desc", params_of(session)


def test_read_mail_is_still_triaged():
    # The original defect: "isRead eq false" in the filter meant a message Alex
    # had merely glanced at was permanently invisible to triage. Most of the
    # event invites it missed had already been read.
    client, session = make_client()
    client.recent_messages(since=SINCE)
    filt = params_of(session)["$filter"]
    assert "isRead" not in filt, filt
    assert "receivedDateTime ge 2026-09-01T12:00:00Z" in filt, filt


def test_focused_and_read_state_are_selected_as_signals():
    # inferenceClassification was never requested, so mail Outlook put in Other
    # was treated identically to mail Alex actually reads. Both fields are
    # selected and handed to the classifier as evidence, not used as a gate.
    client, session = make_client()
    client.recent_messages(since=SINCE)
    select = params_of(session)["$select"]
    assert "inferenceClassification" in select, select
    assert "isRead" in select, select


def test_limit_is_honoured_across_pages():
    pages = [
        {
            "value": [message(f"m{i}", "2026-09-02T12:00:00Z") for i in range(3)],
            "@odata.nextLink": "https://graph.microsoft.com/v1.0/next",
        },
        {"value": [message(f"m{i}", "2026-09-02T11:00:00Z") for i in range(3, 6)]},
    ]
    client, session = make_client(pages)
    out = client.recent_messages(since=SINCE, limit=4)
    assert [m["id"] for m in out] == ["m0", "m1", "m2", "m3"], out
    assert len(session.calls) == 2, session.calls


def test_top_stays_within_the_graph_page_maximum():
    client, session = make_client()
    client.recent_messages(since=SINCE, limit=500)
    assert params_of(session)["$top"] == "50", params_of(session)


def test_inbox_folder_is_the_only_thing_queried():
    client, session = make_client()
    client.recent_messages(since=SINCE)
    assert session.calls[0]["url"].endswith("/me/mailFolders/inbox/messages"), session.calls[0]
    assert session.calls[0]["method"] == "GET"


def test_calendar_write_is_opt_in_and_off_by_default():
    # Widening SCOPES broke every Graph call on 4 September. A refresh token is
    # bound to the scopes it was issued for; asking for one it does not carry
    # fails the whole exchange, so this must never drift back on by default.
    import os

    from shared.graph_client import _scopes

    os.environ.pop("GRAPH_CALENDAR_WRITE", None)
    default = " ".join(_scopes())
    assert "Calendars.Read" in default
    assert "Calendars.ReadWrite" not in default, "write scope must be opt-in"


def test_calendar_write_replaces_read_when_enabled():
    import os

    from shared.graph_client import _scopes

    os.environ["GRAPH_CALENDAR_WRITE"] = "true"
    try:
        enabled = _scopes()
        names = [s.rsplit("/", 1)[-1] for s in enabled]
        assert "Calendars.ReadWrite" in names
        assert "Calendars.Read" not in names, "asking for both is redundant"
    finally:
        os.environ.pop("GRAPH_CALENDAR_WRITE", None)


# ---- the quoted original --------------------------------------------------


GRAPH_REPLY_BODY = (
    "<html><head><style>x</style></head><body>"
    "<div>&nbsp;</div><hr>"
    "<div><b>From:</b> Priya Nair<br><b>Sent:</b> Friday<br>"
    "<b>Subject:</b> Capstone timing</div>"
    "<div>Can we talk next week?</div>"
    "</body></html>"
)


def test_the_reply_goes_above_the_quoted_original():
    """The defect this guards against: createReply returns a draft whose body
    ALREADY holds the quoted thread, and PATCHing body.content replaced it
    wholesale. Every draft arrived with no quoted original, so Alex could not
    tell what he was replying to."""
    from shared.graph_client import GraphClient

    out = GraphClient._above_quote("<p>Tuesday works.</p>", GRAPH_REPLY_BODY)

    assert "Tuesday works." in out
    assert "Can we talk next week?" in out, "the quoted original was lost"
    assert "<b>From:</b> Priya Nair" in out, "the quote header was lost"
    assert out.index("Tuesday works.") < out.index("Can we talk next week?")


def test_the_reply_lands_inside_the_body_tag():
    # Text placed before <html> is not reliably rendered.
    from shared.graph_client import GraphClient

    out = GraphClient._above_quote("<p>Hi.</p>", GRAPH_REPLY_BODY)
    assert out.lower().startswith("<html>")
    assert out.index("<p>Hi.</p>") > out.lower().index("<body>")


def test_a_draft_with_no_body_still_works():
    from shared.graph_client import GraphClient

    assert GraphClient._above_quote("<p>Hi.</p>", "") == "<p>Hi.</p>"


def test_a_body_without_a_body_tag_is_prepended():
    from shared.graph_client import GraphClient

    out = GraphClient._above_quote("<p>Hi.</p>", "<div>quoted</div>")
    assert out == "<p>Hi.</p><div>quoted</div>"


def test_create_reply_draft_preserves_the_quote_end_to_end():
    client, session = make_client([
        {"id": "d1", "body": {"contentType": "HTML", "content": GRAPH_REPLY_BODY}},
        {"id": "d1", "webLink": "https://outlook/x"},
    ])
    client.create_reply_draft("m1", "<p>Tuesday works.</p>")

    patched = session.calls[1]["json"]["body"]["content"]
    assert "Tuesday works." in patched
    assert "Can we talk next week?" in patched, "PATCH wiped the quoted original"


def test_attachments_never_downloads_the_bytes():
    """contentBytes on an hourly job is megabytes per message for a filename."""
    from shared.graph_client import GraphClient

    src = inspect.getsource(GraphClient.attachments)
    assert "contentBytes" not in src.split('"""')[2], "do not select contentBytes"
    assert "isInline" in src, "inline signature logos are not attachments"


def test_only_a_reference_attachment_yields_a_direct_link():
    from shared.graph_client import GraphClient

    client = GraphClient.__new__(GraphClient)
    # A file attachment has no address of its own - no request should be made.
    assert client.attachment_link("m1", {"@odata.type": "#microsoft.graph.fileAttachment",
                                         "id": "a1"}) == ""


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(n, f) for n, f in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
