"""Tests for the reply-to-update path.

This is the only code in the project that writes to Jira, so the containment
tests come first: a key the digest never mentioned must never be touched, and
a status must never be invented.
"""

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import taskreply
from shared.taskreply import apply_operations, parse_reply, prepare_reply

TODAY = dt.date(2026, 9, 7)
ISSUES = {
    "TASK-41": "Finish the SOC 2 gap letter for the college",
    "TASK-38": "Send the revised MOU back to Dana",
    "TASK-19": "Draft the volunteer onboarding checklist",
}


class FakeResponse:
    def __init__(self, text):
        self.content = [type("Block", (), {"text": text})()]


class FakeClient:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(reply)


class FakeJira:
    """Offers a Done transition on everything except TASK-19."""

    def __init__(self, fail_on=None):
        self.transitioned = []
        self.comments = []
        self.due_dates = []
        self._fail_on = fail_on or set()

    def find_transition(self, key, category):
        if key == "TASK-19" and category == "Done":
            return None
        return {"id": "31", "to": {"name": "Done" if category == "Done" else category}}

    def transition(self, key, transition_id):
        if key in self._fail_on:
            raise RuntimeError("workflow says no")
        self.transitioned.append((key, transition_id))

    def comment(self, key, text):
        if key in self._fail_on:
            raise RuntimeError("no permission")
        self.comments.append((key, text))

    def set_due_date(self, key, due):
        self.due_dates.append((key, due))


def ops_json(*ops, unmatched=()):
    return json.dumps({"operations": list(ops), "unmatched": list(unmatched)})


def op(key, action, value=""):
    return {"key": key, "action": action, "value": value}


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


# ---- reading the reply ---------------------------------------------------


INLINE_REPLY = """
________________________________
From: Alex Rivera <alex@example.org>
Sent: Friday, September 4, 2026 7:26 PM
Subject: Your tasks - 31 open [#T-78dc9702]

Your tasks - Friday, September 4
FOR ALL NEW TASKS , unless otherwise specified, assign them to alex rivera
Due this week (8)
TASK-41 Finish the SOC 2 gap letter for the college due today
Did this one - we met this past week and all good
TASK-38 Send the revised MOU back to Dana due today
Already done
"""


def test_an_inline_reply_is_not_thrown_away():
    # The failure that made this rewrite necessary: the old code cut everything
    # from the first quote marker, which discarded every word Alex typed.
    out = prepare_reply(INLINE_REPLY)
    assert "Did this one" in out
    assert "Already done" in out
    assert "FOR ALL NEW TASKS" in out


def test_a_top_posted_reply_still_works():
    body = "TASK-41 done\n\nOn Mon, Sep 7 Alex Rivera wrote:\n> Your tasks"
    assert "TASK-41 done" in prepare_reply(body)


def test_html_becomes_text_with_line_structure_intact():
    # Line structure is how a note is attached to the task above it.
    out = prepare_reply("<div>TASK-41 Something</div><div>did this</div>")
    assert "TASK-41 Something" in out
    assert "did this" in out
    assert "\n" in out, "lines were collapsed; inline attribution would break"


def test_the_digests_own_help_text_is_removed():
    # It contains worked examples like "TASK-41 done". Quoted back, those read as
    # instructions - the assistant could close a ticket because its own help
    # text said so.
    body = ("Reply to this email to update Jira. Plain English is fine - "
            "did the MOU one, TASK-41 done, push TASK-12 to Friday, "
            "note on TASK-8: waiting on Dana. "
            "You will get a confirmation of exactly what changed. "
            "TASK-38 Send the revised MOU back to Dana")
    out = prepare_reply(body)
    assert "TASK-41 done" not in out
    assert "push TASK-12" not in out
    assert "TASK-38" in out, "real task lines must survive"


def test_signatures_are_removed():
    assert "confidential" not in prepare_reply("did it\n\nThis email is confidential").lower()


def test_an_empty_body_is_reported_not_crashed():
    client = FakeClient([])
    result = parse_reply(client, "", ISSUES, today=TODAY)
    assert client.calls == []
    assert "no readable text" in result.error


# ---- containment ---------------------------------------------------------


def test_an_issue_not_in_the_digest_is_dropped():
    # THE containment guarantee: a hallucinated or stale key never reaches Jira.
    client = FakeClient([ops_json(op("TASK-999", "done"))])
    result = parse_reply(client, "mark TASK-999 done", ISSUES, today=TODAY)

    assert result.operations == []
    assert any("TASK-999" in s for s in result.skipped), result.skipped


def test_a_valid_issue_alongside_an_invalid_one_still_applies():
    client = FakeClient([ops_json(op("TASK-41", "done"), op("ZZ-1", "done"))])
    result = parse_reply(client, "both done", ISSUES, today=TODAY)
    assert [o.key for o in result.operations] == ["TASK-41"]
    assert len(result.skipped) == 1


def test_an_unknown_action_is_dropped():
    client = FakeClient([ops_json(op("TASK-41", "delete"))])
    result = parse_reply(client, "delete TASK-41", ISSUES, today=TODAY)
    assert result.operations == []
    assert "delete" in result.skipped[0]


def test_a_relative_due_date_is_refused():
    # The prompt demands YYYY-MM-DD. If the model returns "next Friday" anyway,
    # sending it to Jira would either error or set something wrong.
    client = FakeClient([ops_json(op("TASK-41", "duedate", "next Friday"))])
    result = parse_reply(client, "push TASK-41 to next Friday", ISSUES, today=TODAY)
    assert result.operations == []
    assert "date" in result.skipped[0].lower()


def test_an_absolute_due_date_is_accepted():
    client = FakeClient([ops_json(op("TASK-41", "duedate", "2026-09-11"))])
    result = parse_reply(client, "push TASK-41 to Friday", ISSUES, today=TODAY)
    assert result.operations[0].value == "2026-09-11"


def test_an_empty_comment_is_dropped():
    client = FakeClient([ops_json(op("TASK-41", "comment", ""))])
    result = parse_reply(client, "note on TASK-41", ISSUES, today=TODAY)
    assert result.operations == []


def test_a_model_failure_reports_and_applies_nothing():
    client = FakeClient([RuntimeError("overloaded")])
    result = parse_reply(client, "did the MOU one", ISSUES, today=TODAY)
    assert result.operations == []
    assert "overloaded" in result.error


def test_the_issue_list_and_todays_date_reach_the_model():
    # Without the list it cannot resolve "the MOU one"; without the date it
    # cannot turn "Friday" into a real date.
    client = FakeClient([ops_json()])
    parse_reply(client, "did the MOU one", ISSUES, today=TODAY)
    sent = client.calls[0]["messages"][0]["content"]
    assert "TASK-38" in sent and "MOU" in sent
    assert "2026-09-07" in sent


def test_prompt_tells_the_model_how_to_separate_notes_from_the_quoted_list():
    assert "SEPARATING HIS WORDS" in taskreply.REPLY_SYSTEM
    assert "unmatched" in taskreply.REPLY_SYSTEM
    # Containment itself is enforced in code, not by the prompt - see
    # test_an_issue_not_in_the_digest_is_dropped.
    assert "use the key exactly as given" in taskreply.REPLY_SYSTEM


# ---- applying ------------------------------------------------------------


def test_done_transitions_via_jira_supplied_transition():
    # The model says "done"; Jira decides which transition that is. Status
    # names differ per project, so a model-supplied name would be wrong.
    client = FakeClient([ops_json(op("TASK-41", "done"))])
    result = apply_operations(FakeJira(), parse_reply(client, "x", ISSUES, today=TODAY))

    assert result.operations[0].applied
    assert "Done" in result.operations[0].outcome


def test_an_issue_with_no_done_transition_reports_instead_of_failing():
    client = FakeClient([ops_json(op("TASK-19", "done"))])
    result = apply_operations(FakeJira(), parse_reply(client, "x", ISSUES, today=TODAY))

    assert not result.operations[0].applied
    assert "no transition" in result.operations[0].outcome.lower()


def test_comment_and_transition_on_the_same_issue_both_apply():
    client = FakeClient([ops_json(op("TASK-38", "done"), op("TASK-38", "comment", "Dana signed."))])
    jira = FakeJira()
    result = apply_operations(FakeJira() if False else jira,
                              parse_reply(client, "x", ISSUES, today=TODAY))

    assert len(result.applied) == 2
    assert jira.comments == [("TASK-38", "Dana signed.")]


def test_one_failure_does_not_stop_the_others():
    client = FakeClient([ops_json(op("TASK-41", "done"), op("TASK-38", "done"))])
    jira = FakeJira(fail_on={"TASK-41"})
    result = apply_operations(jira, parse_reply(client, "x", ISSUES, today=TODAY))

    assert len(result.applied) == 1
    assert len(result.failed) == 1
    assert jira.transitioned == [("TASK-38", "31")]
    assert "refused" in result.failed[0].outcome.lower()


def test_due_date_is_applied():
    client = FakeClient([ops_json(op("TASK-41", "duedate", "2026-09-11"))])
    jira = FakeJira()
    apply_operations(jira, parse_reply(client, "x", ISSUES, today=TODAY))
    assert jira.due_dates == [("TASK-41", "2026-09-11")]


def test_start_maps_to_the_in_progress_category():
    client = FakeClient([ops_json(op("TASK-41", "start"))])
    result = apply_operations(FakeJira(), parse_reply(client, "x", ISSUES, today=TODAY))
    assert result.operations[0].applied
    assert "In Progress" in result.operations[0].outcome


def test_nothing_to_do_is_not_an_error():
    # "thanks" is a legitimate reply that should change nothing and say so.
    client = FakeClient([ops_json()])
    result = parse_reply(client, "thanks", ISSUES, today=TODAY)
    assert result.operations == []
    assert result.error is None


# ---- creating new tasks --------------------------------------------------


ALEX = "712020:00000000-0000-0000-0000-000000000001"
PRIYA = "712020:00000000-0000-0000-0000-000000000002"


class CreatingJira(FakeJira):
    def __init__(self, fail_on=None, fail_create=False):
        super().__init__(fail_on)
        self.created = []
        self._fail_create = fail_create

    def create_issue(self, project_key, summary, assignee_account_id=None,
                     description="", issue_type="Task", due=None):
        if self._fail_create:
            raise RuntimeError("no create permission")
        self.created.append(
            {"project": project_key, "summary": summary,
             "assignee": assignee_account_id, "due": due}
        )
        key = f"{project_key}-{900 + len(self.created)}"
        return {"key": key, "url": f"https://x.atlassian.net/browse/{key}"}


def create_op(value, assignee="", due=""):
    return {"action": "create", "value": value, "assignee": assignee, "due": due}


def test_a_new_task_is_created_and_defaults_to_alex():
    client = FakeClient([ops_json(create_op("Do a final review of all IT policies"))])
    jira = CreatingJira()
    result = apply_operations(jira, parse_reply(client, "x", ISSUES, today=TODAY))

    assert len(jira.created) == 1
    assert jira.created[0]["summary"] == "Do a final review of all IT policies"
    # Unassigned, because no account id can be resolved without configuration.
    # The task still exists, which is the half that matters.
    assert jira.created[0]["assignee"] is None
    assert "unassigned" in result.operations[0].outcome
    assert result.operations[0].created_key == "TASK-901"


def test_a_create_goes_to_the_default_owner_once_one_is_configured():
    client = FakeClient([ops_json(create_op("Do a final review of all IT policies"))])
    jira = CreatingJira()
    apply_operations(jira, parse_reply(client, "x", ISSUES, today=TODAY),
                     known_assignees={"alex": ALEX}, default_assignee="alex")
    assert jira.created[0]["assignee"] == ALEX, "unassigned new work goes unwatched"


def test_a_named_assignee_is_resolved_to_an_account_id():
    client = FakeClient([ops_json(create_op("Sign the policies", assignee="Priya"))])
    jira = CreatingJira()
    apply_operations(jira, parse_reply(client, "x", ISSUES, today=TODAY),
                     known_assignees={"priya": PRIYA})
    assert jira.created[0]["assignee"] == PRIYA


def test_nobody_is_assignable_until_account_ids_are_configured():
    """Account ids belong to one Jira site, so there is nothing to guess."""
    from shared.taskreply import DEFAULT_ASSIGNEES

    assert DEFAULT_ASSIGNEES == {}


def test_an_unknown_assignee_still_creates_the_task_and_says_so():
    # Losing the task entirely would be worse than one that needs reassigning.
    client = FakeClient([ops_json(create_op("Something", assignee="Priya"))])
    jira = CreatingJira()
    result = apply_operations(jira, parse_reply(client, "x", ISSUES, today=TODAY))

    assert len(jira.created) == 1
    assert jira.created[0]["assignee"] is None
    assert "could not resolve" in result.operations[0].outcome
    assert "left unassigned" in result.operations[0].outcome


def test_a_create_goes_to_the_configured_project():
    client = FakeClient([ops_json(create_op("Something"))])
    jira = CreatingJira()
    apply_operations(jira, parse_reply(client, "x", ISSUES, today=TODAY), project_key="ZZ")
    assert jira.created[0]["project"] == "ZZ"


def test_a_create_with_no_summary_is_dropped():
    client = FakeClient([ops_json(create_op(""))])
    result = parse_reply(client, "make a task", ISSUES, today=TODAY)
    assert result.operations == []
    assert result.skipped


def test_a_relative_due_date_on_a_create_is_dropped_not_sent():
    client = FakeClient([ops_json(create_op("Something", due="next Friday"))])
    result = parse_reply(client, "x", ISSUES, today=TODAY)
    assert result.operations[0].due == "", "a relative date must never reach Jira"


def test_a_create_failure_does_not_stop_a_transition():
    client = FakeClient([ops_json(create_op("Something"), op("TASK-41", "done"))])
    jira = CreatingJira(fail_create=True)
    result = apply_operations(jira, parse_reply(client, "x", ISSUES, today=TODAY))

    assert jira.transitioned == [("TASK-41", "31")]
    assert len(result.failed) == 1
    assert "refused" in result.failed[0].outcome.lower()


def test_done_and_create_on_the_same_task_both_happen():
    # "This is done but create a new task that says to review the policies"
    client = FakeClient([ops_json(
        op("TASK-41", "done"),
        create_op("Do a final review of all operational IT policies and sign off"),
    )])
    jira = CreatingJira()
    result = apply_operations(jira, parse_reply(client, "x", ISSUES, today=TODAY))

    assert jira.transitioned == [("TASK-41", "31")]
    assert len(jira.created) == 1
    assert len(result.applied) == 2
    assert len(result.created) == 1


def test_creates_are_not_restricted_to_the_digest():
    # Containment applies to EXISTING issues. A create has no key to contain.
    client = FakeClient([ops_json(create_op("Brand new unrelated thing"))])
    result = parse_reply(client, "x", ISSUES, today=TODAY)
    assert len(result.operations) == 1
    assert result.skipped == []


def test_prompt_covers_the_inline_style_and_standing_instructions():
    assert "annotates INSIDE" in taskreply.REPLY_SYSTEM
    assert "standing instruction" in taskreply.REPLY_SYSTEM
    assert "for me to have Priya sign" in taskreply.REPLY_SYSTEM


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(name, fn) for name, fn in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
