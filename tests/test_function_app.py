"""Wiring tests for the two orchestrators, run_triage and run_agenda.

Everything Alex actually asked for lives in the seams between modules - whether
an email is sent at all, whether the calendar is read, whether a message is
marked processed - and none of it is visible from a unit test of any single
module. These drive the real functions with fake collaborators.

azure.functions, anthropic and requests are stubbed into sys.modules before the
import, same discipline as the other suites: no network, no credentials, no
installed packages.
"""

import datetime as dt
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_azure = types.ModuleType("azure")
_functions = types.ModuleType("azure.functions")


class _FunctionApp:
    def __init__(self):
        self.registered = []
        self.routes = []

    def timer_trigger(self, schedule, arg_name, run_on_startup=False):
        def decorate(fn):
            self.registered.append((fn.__name__, schedule))
            return fn
        return decorate

    def route(self, route, methods=None, auth_level=None):
        def decorate(fn):
            self.routes.append(
                {"name": fn.__name__, "route": route,
                 "methods": tuple(methods or ()), "auth_level": auth_level}
            )
            return fn
        return decorate


class _AuthLevel:
    ANONYMOUS = "anonymous"
    FUNCTION = "function"
    ADMIN = "admin"


class _HttpRequest:
    def __init__(self, method="GET", params=None, form=None):
        self.method = method
        self.params = params or {}
        self.form = form or {}


class _HttpResponse:
    def __init__(self, body, status_code=200, mimetype="text/plain", headers=None):
        self.body = body
        self.status_code = status_code
        self.mimetype = mimetype
        self.headers = headers or {}

    def get_body(self):
        return self.body.encode() if isinstance(self.body, str) else self.body


_functions.FunctionApp = _FunctionApp
_functions.TimerRequest = object
_functions.AuthLevel = _AuthLevel
_functions.HttpRequest = _HttpRequest
_functions.HttpResponse = _HttpResponse
_azure.functions = _functions
sys.modules.setdefault("azure", _azure)
sys.modules.setdefault("azure.functions", _functions)

_anthropic = types.ModuleType("anthropic")
_anthropic.Anthropic = lambda **kwargs: object()
sys.modules.setdefault("anthropic", _anthropic)

_requests = types.ModuleType("requests")
_requests.Session = lambda: None
_requests.post = lambda *a, **k: None
sys.modules.setdefault("requests", _requests)

import function_app as fa  # noqa: E402
from shared.graph_client import GraphError  # noqa: E402
from shared.planning import DayPlan  # noqa: E402
from shared.triage import TriageResult, Verdict  # noqa: E402


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


def verdict(mid="m1", **over):
    base = dict(
        message_id=mid, sender_type="human", reply_owed=True, is_meeting_request=False,
        request_type="request", urgency="today", category="question", reasoning="asked",
    )
    base.update(over)
    return Verdict(**base)


def message(mid="m1"):
    return {
        "id": mid,
        "from": {"emailAddress": {"name": "Priya", "address": "priya@x.org"}},
        "subject": "Can you look at this?",
        "bodyPreview": "please review",
        "receivedDateTime": "2026-09-02T12:00:00Z",
    }


class FakeGraph:
    def __init__(self, messages=None, events=None, fail_send=False):
        self.messages = messages if messages is not None else [message()]
        self.events = events or []
        self.fail_send = fail_send
        self.sent = []
        self.calendar_reads = 0
        self.drafts_created = []

    def recent_messages(self, since, limit=50):
        return self.messages

    def contacts(self):
        return []

    def message_body(self, message_id):
        return "please review this deck"

    def calendar_view(self, start, end, timezone):
        self.calendar_reads += 1
        return self.events

    def create_reply_draft(self, message_id, body_html):
        self.drafts_created.append((message_id, body_html))
        return {"id": "draft-1", "webLink": "https://outlook.office.com/x"}

    def send_mail(self, to, subject, body):
        if self.fail_send:
            raise GraphError(500, "send exploded")
        self.sent.append({"to": to, "subject": subject, "body": body})


class FakeStore:
    def __init__(self, seen=()):
        self._seen = set(seen)
        self.marked = []

    def seen(self):
        return set(self._seen)

    def mark(self, ids, now=None):
        self.marked.extend(ids)


class Harness:
    """Swaps every collaborator function_app reaches for, then puts them back."""

    def __init__(self, graph, store=None, verdicts=None, plan=None):
        self.graph = graph
        self.store = store or FakeStore()
        self.verdicts = verdicts if verdicts is not None else [verdict()]
        self.plan = plan
        self.drafted = []
        self._saved = {}

    def __enter__(self):
        def fake_draft_reply(client, v, msg, body, availability=None, **kw):
            self.drafted.append((v.message_id, v.request_type, availability))
            v.draft = "drafted text"
            return v

        patches = {
            "client_from_env": lambda: self.graph,
            "default_processed_store": lambda: self.store,
            "_anthropic": lambda: object(),
            "classify": lambda client, msgs, known: TriageResult(verdicts=list(self.verdicts)),
            "draft_reply": fake_draft_reply,
            "_day_plan": lambda events, blocks, for_date, is_today: self.plan,
        }
        for name, value in patches.items():
            self._saved[name] = getattr(fa, name)
            setattr(fa, name, value)
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            setattr(fa, name, value)
        return False


# ---- the schedules Alex asked for ---------------------------------------


def test_inbox_runs_hourly_on_weekdays_only():
    schedules = dict((n, s) for n, s in fa.app.registered)
    assert schedules["inbox_hourly"] == "0 0 7-16 * * 1-5", schedules["inbox_hourly"]


def test_no_off_hours_inbox_job_remains():
    # The old build checked mail around the clock. That job must be gone, not
    # merely rescheduled - two triage timers would double every recap.
    names = [n for n, _ in fa.app.registered]
    assert names.count("inbox_hourly") == 1
    assert not any("off_hours" in n for n in names), names


def test_agendas_run_at_three_and_six():
    schedules = dict((n, s) for n, s in fa.app.registered)
    assert schedules["agenda_tomorrow"].startswith("0 0 15 "), schedules["agenda_tomorrow"]
    assert schedules["agenda_today"].startswith("0 0 6 "), schedules["agenda_today"]


def test_every_schedule_has_six_ncrontab_fields():
    # A five-field crontab loads happily and then runs at the wrong hour. It is
    # the easiest mistake to make in this file and invisible until a missed run.
    for name, schedule in fa.app.registered:
        assert len(schedule.split()) == 6, f"{name}: {schedule!r}"


# ---- when a recap is sent ------------------------------------------------


def test_quiet_run_sends_nothing():
    graph = FakeGraph()
    quiet = [verdict(reply_owed=False, request_type="none", category="newsletter")]
    with Harness(graph, verdicts=quiet) as h:
        fa.run_triage("test")

    assert graph.sent == [], "an inbox with nothing to do must not produce an email"
    assert h.store.marked == ["m1"], "reviewed messages must still be marked, or we re-pay hourly"


def test_actionable_run_sends_the_recap():
    graph = FakeGraph()
    with Harness(graph) as h:
        fa.run_triage("test")

    assert len(graph.sent) == 1
    assert h.store.marked == ["m1"]


def test_a_send_failure_leaves_messages_unprocessed_so_they_retry():
    graph = FakeGraph(fail_send=True)
    with Harness(graph) as h:
        fa.run_triage("test")
    assert h.store.marked == [], "a failed send must not silently consume the mail"


def test_nothing_new_short_circuits_before_any_model_call():
    graph = FakeGraph()
    store = FakeStore(seen={"m1"})
    with Harness(graph, store=store) as h:
        fa.run_triage("test")
    assert graph.sent == []
    assert h.drafted == []


# ---- drafting gate -------------------------------------------------------


def test_only_draftable_messages_reach_the_drafting_pass():
    graph = FakeGraph(messages=[message("m1"), message("m2"), message("m3")])
    verdicts = [
        verdict("m1", request_type="request"),
        verdict("m2", request_type="other"),
        verdict("m3", request_type="none", reply_owed=False),
    ]
    with Harness(graph, verdicts=verdicts) as h:
        fa.run_triage("test")

    assert [d[0] for d in h.drafted] == ["m1"], h.drafted


def test_calendar_is_read_only_when_a_meeting_is_in_the_batch():
    graph = FakeGraph()
    with Harness(graph, verdicts=[verdict(request_type="request")]):
        fa.run_triage("test")
    assert graph.calendar_reads == 0, "ten days of calendarView on every hourly run"

    graph2 = FakeGraph()
    with Harness(graph2, verdicts=[verdict(request_type="meeting", is_meeting_request=True)]):
        fa.run_triage("test")
    assert graph2.calendar_reads == 1


def test_meeting_drafts_receive_availability_and_others_do_not():
    graph = FakeGraph(messages=[message("m1"), message("m2")])
    verdicts = [
        verdict("m1", request_type="meeting", is_meeting_request=True),
        verdict("m2", request_type="request"),
    ]
    with Harness(graph, verdicts=verdicts) as h:
        fa.run_triage("test")

    passed = dict((mid, avail) for mid, _, avail in h.drafted)
    assert passed["m1"] is not None, "a meeting reply with no times is the bug we fixed"
    # m2 is handed the same value; triage.draft_reply is what ignores it for
    # non-meetings, and test_triage covers that.
    assert set(passed) == {"m1", "m2"}


def test_a_dead_calendar_does_not_stop_the_run():
    class Broken(FakeGraph):
        def calendar_view(self, start, end, timezone):
            raise GraphError(503, "calendar down")

    graph = Broken()
    with Harness(graph, verdicts=[verdict(request_type="meeting", is_meeting_request=True)]) as h:
        fa.run_triage("test")

    assert h.drafted[0][2] is None, "availability should be None, not an exception"
    assert len(graph.sent) == 1, "the recap still goes out"


def test_outlook_mirroring_still_works_when_switched_on():
    # CREATE_OUTLOOK_DRAFTS is off by default now, but the path has to keep
    # working: it is the fallback if the buttons ever have to be turned off.
    graph = FakeGraph()
    saved = fa.CREATE_OUTLOOK_DRAFTS
    fa.CREATE_OUTLOOK_DRAFTS = True
    try:
        with Harness(graph):
            fa.run_triage("test")
    finally:
        fa.CREATE_OUTLOOK_DRAFTS = saved

    assert len(graph.drafts_created) == 1
    assert graph.drafts_created[0][0] == "m1"


# ---- agendas -------------------------------------------------------------


def test_agenda_sends_on_a_working_day():
    graph = FakeGraph()
    with Harness(graph, plan=DayPlan(focus="Clear afternoon.")):
        fa.run_agenda(dt.date(2026, 9, 3), is_today=True)   # Thursday

    assert len(graph.sent) == 1
    assert graph.sent[0]["subject"] == "Your Daily Agenda: 09/03/2026"
    assert "Clear afternoon." in graph.sent[0]["body"]


def test_agenda_skips_weekends():
    graph = FakeGraph()
    with Harness(graph):
        fa.run_agenda(dt.date(2026, 9, 5), is_today=False)  # Saturday
    assert graph.sent == []


def test_agenda_still_sends_on_a_holiday():
    # Monday 7 September 2026 is Labor Day. Alex still takes meetings on
    # holidays, and the day the calendar is unusual is the day the agenda
    # matters most. Holidays suppress OFFERED availability, not the agenda.
    graph = FakeGraph()
    with Harness(graph):
        fa.run_agenda(dt.date(2026, 9, 7), is_today=True)

    assert len(graph.sent) == 1
    assert graph.sent[0]["subject"] == "Your Daily Agenda: 09/07/2026"


def test_a_holiday_agenda_still_shows_open_time():
    # free_slots would normally skip a non-business day entirely. For an agenda
    # the day is already chosen, so the only question is which hours are free.
    graph = FakeGraph()
    with Harness(graph):
        fa.run_agenda(dt.date(2026, 9, 7), is_today=True)
    assert "Free / available" in graph.sent[0]["body"]


def test_agenda_still_skips_weekends():
    graph = FakeGraph()
    with Harness(graph):
        fa.run_agenda(dt.date(2026, 9, 5), is_today=False)  # Saturday
    assert graph.sent == []


def test_agenda_goes_out_even_when_jira_is_unavailable():
    graph = FakeGraph()
    with Harness(graph, plan=DayPlan(note="Could not read the TASK board: 401")):
        fa.run_agenda(dt.date(2026, 9, 3), is_today=False)

    assert len(graph.sent) == 1
    assert "Could not read the TASK board" in graph.sent[0]["body"]


def test_tomorrow_agenda_is_labelled_tomorrow():
    graph = FakeGraph()
    with Harness(graph):
        fa.run_agenda(dt.date(2026, 9, 3), is_today=False)
    assert graph.sent[0]["subject"].startswith("Tomorrow's Agenda:")


# ---- the recap buttons ---------------------------------------------------
#
# The GET/POST split is the security design, so it is asserted first and from
# several angles. A regression that lets GET send mail would have every draft
# sent by Outlook Safe Links before Alex opened the recap.


import tempfile  # noqa: E402

from shared import actions  # noqa: E402
from shared.pending import DISCARDED, PENDING, SENT, FilePendingStore, PendingDraft  # noqa: E402

KEY = b"k" * 48


class SendingGraph(FakeGraph):
    def __init__(self, fail=None, **kw):
        super().__init__(**kw)
        self.replies_sent = []
        self.existing_sent = []
        self.deleted = []
        self._fail = fail

    def send_reply(self, message_id, body_html):
        if self._fail:
            raise self._fail
        self.replies_sent.append((message_id, body_html))
        return {"id": "draft-x"}

    def send_existing_draft(self, draft_id, body_html=None):
        if self._fail:
            raise self._fail
        self.existing_sent.append((draft_id, body_html))

    def delete_message(self, message_id):
        self.deleted.append(message_id)


class Buttons:
    """Real pending store + real tokens, fake Graph."""

    def __init__(self, graph=None, **draft_fields):
        self.graph = graph or SendingGraph()
        self.store = FilePendingStore(Path(tempfile.mkdtemp()) / "pending.json")
        fields = dict(
            message_id="AAMk-msg-1", subject="Capstone timing",
            sender_name="Priya Nair", sender_address="priya@x.org",
            the_ask="Wants 30 minutes next week.", request_type="meeting",
            body="Glad to meet.\n\nAlex",
        )
        fields.update(draft_fields)
        self.draft = self.store.put(PendingDraft.new(**fields))
        self._saved = {}

    def __enter__(self):
        patches = {
            "default_pending_store": lambda: self.store,
            "client_from_env": lambda: self.graph,
        }
        for name, value in patches.items():
            self._saved[name] = getattr(fa, name)
            setattr(fa, name, value)
        self._saved_key = fa.actions.signing_key
        fa.actions.signing_key = lambda: KEY
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            setattr(fa, name, value)
        fa.actions.signing_key = self._saved_key
        return False

    def token(self, action="send"):
        return actions.sign(action, self.draft.id, KEY)

    def get(self, action="send"):
        return fa.draft_action(_HttpRequest("GET", params={"t": self.token(action)}))

    def post(self, action="send", **form):
        form.setdefault("t", self.token(action))
        return fa.draft_action(_HttpRequest("POST", form=form))


def test_the_endpoint_is_registered_anonymously_for_get_and_post():
    route = next(r for r in fa.app.routes if r["name"] == "draft_action")
    assert route["route"] == "draft"
    assert set(route["methods"]) == {"GET", "POST"}
    # Anonymous is correct: the link is clicked from an email client that holds
    # no Azure credentials. The signed token is the authentication.
    assert route["auth_level"] == "anonymous"


def test_get_never_sends_anything():
    # THE critical test. Link scanners GET every URL in an email.
    with Buttons() as b:
        resp = b.get("send")

    assert resp.status_code == 200
    assert b.graph.replies_sent == [], "GET sent mail - link scanners would send every draft"
    assert b.store.get(b.draft.id).status == PENDING
    assert "Send it now" in resp.body, "should render a confirmation page"


def test_get_on_discard_deletes_nothing():
    with Buttons() as b:
        resp = b.get("discard")
    assert b.store.get(b.draft.id).status == PENDING
    assert "Discard it" in resp.body


def test_post_actually_sends():
    with Buttons() as b:
        resp = b.post("send", confirm="send")

    assert resp.status_code == 200
    assert len(b.graph.replies_sent) == 1
    assert b.graph.replies_sent[0][0] == "AAMk-msg-1"
    assert "Glad to meet." in b.graph.replies_sent[0][1]
    assert b.store.get(b.draft.id).status == SENT
    assert "Sent to Priya Nair" in resp.body


def test_a_second_click_does_not_send_twice():
    with Buttons() as b:
        b.post("send", confirm="send")
        resp = b.post("send", confirm="send")

    assert len(b.graph.replies_sent) == 1, "double click sent two emails"
    assert "already been sent" in resp.body


def test_discard_marks_without_sending():
    with Buttons() as b:
        resp = b.post("discard", confirm="discard")

    assert b.graph.replies_sent == []
    assert b.store.get(b.draft.id).status == DISCARDED
    assert "Discarded" in resp.body


def test_discard_removes_the_outlook_draft_when_there_is_one():
    with Buttons(outlook_draft_id="AAMk-draft-9") as b:
        b.post("discard", confirm="discard")
    assert b.graph.deleted == ["AAMk-draft-9"]


def test_editing_changes_what_gets_sent():
    with Buttons() as b:
        resp = b.post("edit", confirm="send", body="Rewritten by Alex.\n\nAlex")

    assert "Rewritten by Alex." in b.graph.replies_sent[0][1]
    assert "Glad to meet." not in b.graph.replies_sent[0][1]
    assert "Sent to Priya Nair" in resp.body


def test_saving_an_edit_does_not_send():
    with Buttons() as b:
        resp = b.post("edit", confirm="save", body="Later.")

    assert b.graph.replies_sent == []
    assert b.store.get(b.draft.id).body == "Later."
    assert b.store.get(b.draft.id).status == PENDING, "must stay actionable"
    assert "saved" in resp.body.lower()


def test_edit_first_from_the_send_page_shows_the_editor():
    with Buttons() as b:
        resp = b.post("send", confirm="to_edit")
    assert "<textarea" in resp.body
    assert b.graph.replies_sent == []


def test_a_post_naming_no_button_does_not_send():
    # A stray POST - a scanner, a resubmitted form - must not be read as consent.
    with Buttons() as b:
        resp = b.post("send")
    assert b.graph.replies_sent == []
    assert "Send it now" in resp.body


def test_an_unsigned_token_is_refused():
    with Buttons() as b:
        resp = fa.draft_action(_HttpRequest("GET", params={"t": "forged"}))
    assert resp.status_code == 400
    assert b.graph.replies_sent == []


def test_a_token_for_another_key_is_refused():
    with Buttons() as b:
        bad = actions.sign("send", b.draft.id, b"z" * 48)
        resp = fa.draft_action(_HttpRequest("POST", form={"t": bad, "confirm": "send"}))
    assert resp.status_code == 400
    assert b.graph.replies_sent == []


def test_an_expired_token_is_refused():
    with Buttons() as b:
        old = actions.sign("send", b.draft.id, KEY, ttl=1, now=1000)
        resp = fa.draft_action(_HttpRequest("POST", form={"t": old, "confirm": "send"}))
    assert resp.status_code == 400
    assert b.graph.replies_sent == []


def test_a_token_for_a_pruned_draft_is_a_clean_410():
    with Buttons() as b:
        token = actions.sign("send", "no-such-draft", KEY)
        resp = fa.draft_action(_HttpRequest("GET", params={"t": token}))
    assert resp.status_code == 410
    assert "no longer available" in resp.body


def test_a_failed_send_leaves_the_draft_sendable():
    # The claim is taken before the Graph call so two clicks cannot both send.
    # If the send then fails, that claim has to come back.
    with Buttons(graph=SendingGraph(fail=GraphError(502, "smtp sad"))) as b:
        resp = b.post("send", confirm="send")

    assert resp.status_code == 502
    assert b.store.get(b.draft.id).status == PENDING, "draft stuck as sent but never sent"
    assert "Nothing went out" in resp.body


def test_responses_are_uncacheable_and_leak_no_referer():
    with Buttons() as b:
        resp = b.get("send")
    assert "no-store" in resp.headers.get("Cache-Control", "")
    assert resp.headers.get("Referrer-Policy") == "no-referrer"
    assert "noindex" in resp.headers.get("X-Robots-Tag", "")


def test_pages_do_not_leak_the_token_into_a_link():
    # The token belongs in a POST body, never in an href a browser might send
    # as a Referer or a user might copy.
    with Buttons() as b:
        resp = b.get("send")
    assert f"href='{b.token('send')}" not in resp.body
    assert "<form method='post'>" in resp.body


def test_buttons_are_absent_when_no_signing_key_is_configured():
    saved = fa.actions.signing_key
    fa.actions.signing_key = lambda: None
    try:
        resp = fa.draft_action(_HttpRequest("GET", params={"t": "x"}))
        assert resp.status_code == 503
    finally:
        fa.actions.signing_key = saved


# ---- the recap end of it -------------------------------------------------


def test_drafting_a_reply_queues_it_and_mints_links():
    graph = FakeGraph()
    store = FilePendingStore(Path(tempfile.mkdtemp()) / "pending.json")

    saved_store, saved_key = fa.default_pending_store, fa.actions.signing_key
    fa.default_pending_store = lambda: store
    fa.actions.signing_key = lambda: KEY
    os.environ["ACTION_BASE_URL"] = "https://x.example/api"
    try:
        with Harness(graph):
            fa.run_triage("test")
    finally:
        fa.default_pending_store, fa.actions.signing_key = saved_store, saved_key
        os.environ.pop("ACTION_BASE_URL", None)

    queued = store.open_drafts()
    assert len(queued) == 1, "the drafted reply should be queued for a decision"
    assert queued[0].message_id == "m1"
    assert queued[0].body == "drafted text"

    body = graph.sent[0]["body"]
    assert "Approve and send" in body
    assert "Do not send - delete" in body
    assert "https://x.example/api/draft?t=" in body


def test_no_outlook_draft_is_created_by_default():
    # The whole complaint: the Drafts folder was filling up.
    graph = FakeGraph()
    assert fa.CREATE_OUTLOOK_DRAFTS is False
    with Harness(graph):
        fa.run_triage("test")
    assert graph.drafts_created == []


# ---- task digest and reply-to-update -------------------------------------


from shared.digests import FileDigestStore, TaskDigest, marker_tag  # noqa: E402


def test_task_and_team_digests_run_when_asked():
    schedules = dict((n, s) for n, s in fa.app.registered)
    assert schedules["task_digest_monday"] == "0 0 7 * * 1", schedules["task_digest_monday"]
    assert schedules["team_digest_sunday"] == "0 0 15 * * 0", schedules["team_digest_sunday"]
    assert schedules["team_digest_monday"] == "0 0 8 * * 1", schedules["team_digest_monday"]


class FakeJira:
    def __init__(self, issues=None, auth_ok=True):
        self._issues = issues or []
        self._auth_ok = auth_ok
        self.transitioned = []
        self.comments = []
        self.token_fingerprint = "deadbeefcafe"
        self.token_length = 192

    def whoami(self):
        if not self._auth_ok:
            raise RuntimeError("Jira returned 401")
        return {"displayName": "Alex Rivera", "emailAddress": "alex@example.org",
                "accountId": "712020:abc"}

    def my_open_issues(self, project_key="TASK", limit=100):
        return self._issues

    def done_since(self, jql, days=7, limit=25):
        return []

    def open_under(self, jql, limit=60):
        return []

    def find_transition(self, key, category):
        return {"id": "31", "to": {"name": "Done"}}

    def transition(self, key, tid):
        self.transitioned.append(key)

    def comment(self, key, text):
        self.comments.append((key, text))

    def set_due_date(self, key, due):
        pass


class Issue:
    def __init__(self, key, summary):
        self.key = key
        self.summary = summary
        self.status = "To Do"
        self.status_category = "To Do"
        self.due = None
        self.priority = None
        self.url = ""


class Digests:
    """Real digest store + fake Jira, for the reply path."""

    def __init__(self, jira=None, issues=None):
        self.store = FileDigestStore(Path(tempfile.mkdtemp()) / "d.json")
        self.jira = jira if jira is not None else FakeJira(issues or [])
        self._saved = {}

    def __enter__(self):
        patches = {
            "default_digest_store": lambda: self.store,
            "jira_from_env": lambda: self.jira,
        }
        for name, value in patches.items():
            self._saved[name] = getattr(fa, name)
            setattr(fa, name, value)
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            setattr(fa, name, value)
        return False


def reply_message(marker, body="did the MOU one", sender=None):
    return {
        "id": "r1",
        "from": {"emailAddress": {"name": "Alex Rivera",
                                  "address": sender or fa.ASSISTANT_EMAIL}},
        "subject": f"RE: Your tasks - 3 open {marker_tag(marker)}",
        "bodyPreview": body,
        "receivedDateTime": "2026-09-07T12:00:00Z",
    }


def test_a_reply_from_alex_with_a_valid_marker_is_a_task_reply():
    assert fa._is_task_reply(reply_message("a1b2c3d4"))


def test_a_marker_from_someone_else_is_not_acted_on():
    # A forwarded digest must not let its recipient drive Alex's Jira board.
    assert not fa._is_task_reply(reply_message("a1b2c3d4", sender="attacker@evil.com"))


def test_ordinary_mail_from_alex_is_not_a_task_reply():
    msg = message("m9")
    msg["from"] = {"emailAddress": {"address": fa.ASSISTANT_EMAIL}}
    assert not fa._is_task_reply(msg)


def test_task_replies_are_handled_and_kept_out_of_triage():
    # Left in the batch, "did the MOU one" would be triaged as a message owing
    # a reply and would get a draft written back to Alex himself.
    graph = FakeGraph(messages=[reply_message("aaaaaaaa"), message("m2")])
    jira = FakeJira()

    with Digests(jira=jira) as d:
        d.store.put(TaskDigest(marker="aaaaaaaa", sent="2026-09-07T11:00:00+00:00",
                               issues={"TASK-38": "Send the revised MOU back to Dana"}))
        harness = Harness(graph, verdicts=[verdict("m2")])
        saved_parse, saved_apply = fa.parse_reply, fa.apply_operations
        fa.parse_reply = lambda c, b, i, today=None: __import__(
            "shared.taskreply", fromlist=["ReplyResult"]
        ).ReplyResult(operations=[])
        fa.apply_operations = lambda j, r: r
        try:
            with harness as h:
                fa.run_triage("test")
        finally:
            fa.parse_reply, fa.apply_operations = saved_parse, saved_apply

    # Two emails: the reply confirmation, then the recap for the other message.
    subjects = [s["subject"] for s in graph.sent]
    assert any("Task update" in s for s in subjects), subjects
    assert [d[0] for d in h.drafted] == ["m2"], "the reply must not be drafted to"


def test_an_unknown_marker_is_answered_not_silently_dropped():
    graph = FakeGraph(messages=[reply_message("ffffffff")])
    with Digests():
        with Harness(graph, verdicts=[]):
            fa.run_triage("test")

    assert len(graph.sent) == 1
    assert "aged out" in graph.sent[0]["body"]


def test_the_digest_is_recorded_before_it_is_sent():
    # A digest Alex can reply to but whose issues were never stored would
    # answer his reply with "that list is too old", which is simply wrong.
    graph = FakeGraph()
    jira = FakeJira([Issue("TASK-41", "SOC 2 gap letter")])

    with Digests(jira=jira) as d:
        saved = fa.client_from_env
        fa.client_from_env = lambda: graph
        try:
            fa.run_task_digest()
        finally:
            fa.client_from_env = saved

    assert len(graph.sent) == 1
    marker = fa.find_marker(graph.sent[0]["subject"])
    assert marker, graph.sent[0]["subject"]
    stored = d.store.get(marker)
    assert stored is not None and stored.issues == {"TASK-41": "SOC 2 gap letter"}


def test_no_jira_means_no_task_digest_rather_than_a_crash():
    graph = FakeGraph()
    saved_jira, saved_graph = fa.jira_from_env, fa.client_from_env
    fa.jira_from_env = lambda: None
    fa.client_from_env = lambda: graph
    try:
        fa.run_task_digest()
        fa.run_team_digest()
    finally:
        fa.jira_from_env, fa.client_from_env = saved_jira, saved_graph
    assert graph.sent == []


def test_a_dead_jira_token_sends_an_alert_not_a_digest_of_zeros():
    # THE failure this guard exists for. Jira's search endpoint answers a
    # revoked token with 200 {"issues": []} rather than 401, so without this
    # check the digest reports that everyone finished nothing - a confident
    # wrong answer that reads as news.
    graph = FakeGraph()
    with Digests(jira=FakeJira(auth_ok=False)):
        saved = fa.client_from_env
        fa.client_from_env = lambda: graph
        try:
            fa.run_team_digest()
            fa.run_task_digest()
        finally:
            fa.client_from_env = saved

    subjects = [s["subject"] for s in graph.sent]
    assert subjects, "a dead token must produce an alert, not silence"
    assert all("cannot reach Jira" in s for s in subjects), subjects
    assert not any("Team -" in s or "Your tasks" in s for s in subjects),         "no digest may be sent when the credential is dead"


def test_a_working_token_still_sends_the_digest():
    from shared.team import Teammate

    graph = FakeGraph()
    with Digests(jira=FakeJira(auth_ok=True)):
        saved = (fa.client_from_env, fa._anthropic, fa.summarise_team, fa.roster)
        fa.client_from_env = lambda: graph
        fa._anthropic = lambda: object()
        fa.summarise_team = lambda client, sections, days=7: "a quiet week"
        fa.roster = lambda: (Teammate("Sam", "project = 'ST'"),)
        try:
            fa.run_team_digest()
        finally:
            fa.client_from_env, fa._anthropic, fa.summarise_team, fa.roster = saved

    assert len(graph.sent) == 1
    assert graph.sent[0]["subject"].startswith("Team -")


def test_no_roster_means_no_team_digest():
    """An unconfigured install stays silent rather than reporting an empty team.

    "Nobody finished anything this week", from a roster that queries nothing,
    is a working-looking feature delivering false bad news every Sunday.
    """
    graph = FakeGraph()
    with Digests(jira=FakeJira(auth_ok=True)):
        saved = (fa.client_from_env, fa._anthropic, fa.roster)
        fa.client_from_env = lambda: graph
        fa._anthropic = lambda: object()
        fa.roster = lambda: ()
        try:
            fa.run_team_digest()
        finally:
            fa.client_from_env, fa._anthropic, fa.roster = saved

    assert graph.sent == []


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(name, fn) for name, fn in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
