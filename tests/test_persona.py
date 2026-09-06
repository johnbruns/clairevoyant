"""The assistant's persona - and where it must NOT appear."""

import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import persona
from shared.agendamail import build_agenda
from shared.recap import build_recap
from shared.taskmail import build_reply_confirmation, build_task_digest, build_team_digest
from shared.triage import Verdict, compose_meeting_reply
from shared.taskreply import Operation, ReplyResult

TZ = ZoneInfo("America/New_York")
DAY = dt.date(2026, 9, 8)
NOW = dt.datetime(2026, 9, 8, 7, 0, tzinfo=TZ)
LINE = "Hi, this is Claire Voyant, your personal Senior Administrative Strategist!"


class Issue:
    def __init__(self, key="TASK-1", summary="Thing"):
        self.key, self.summary = key, summary
        self.status, self.status_category = "To Do", "To Do"
        self.due = self.priority = None
        self.url = ""


def v(mid="m1", **over):
    base = dict(message_id=mid, sender_type="human", reply_owed=True,
                is_meeting_request=False, request_type="request", urgency="today",
                category="question", reasoning="asked")
    base.update(over)
    return Verdict(**base)


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


def test_the_greeting_reads_exactly_as_asked():
    assert persona.greeting() == LINE


def test_the_agenda_introduces_her():
    _, body = build_agenda([], [], DAY)
    assert LINE in body


def test_the_agenda_signs_off():
    _, body = build_agenda([], [], DAY)
    assert "Claire Voyant" in body.split("class='signoff'")[-1]


def test_the_task_digest_introduces_her():
    _, body = build_task_digest([Issue()], "m", DAY)
    assert LINE in body


def test_the_team_digest_introduces_her():
    _, body = build_team_digest([], DAY)
    assert LINE in body


def test_the_reply_confirmation_introduces_her():
    result = ReplyResult(operations=[Operation("done", "TASK-1", applied=True, outcome="done")])
    _, body = build_reply_confirmation(result, NOW)
    assert LINE in body


def test_the_inbox_recap_introduces_her():
    msgs = {"m1": {"id": "m1", "from": {"emailAddress": {"name": "A", "address": "a@b.c"}},
                   "subject": "s"}}
    _, body = build_recap([v("m1", draft="x")], msgs, NOW)
    assert LINE in body


# ---- the line she must never cross ---------------------------------------


def test_a_drafted_reply_never_mentions_her():
    """THE constraint. Drafted replies go out under Alex's name to other
    people. "This is Claire Voyant" in a reply to his board would be the
    assistant introducing itself to his contacts, which is not what a drafted
    reply is."""
    text = compose_meeting_reply("Glad to meet.", "Monday: 9am-4pm")
    assert "Claire" not in text
    assert "Strategist" not in text


def test_triage_does_not_import_the_persona():
    # A drafting prompt that can reach the persona will eventually use it.
    # Checked as a real import, not a substring: "a personal note" appears in
    # the triage prompt and is not a leak.
    import ast

    source = (Path(__file__).resolve().parents[1] / "shared" / "triage.py").read_text(
        encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(a.name for a in node.names)
    assert "persona" not in imported, f"triage imports {imported & {'persona'}}"


def test_the_persona_is_configurable():
    import importlib, os

    os.environ["ASSISTANT_PERSONA_NAME"] = "Someone Else"
    try:
        importlib.reload(persona)
        assert "Someone Else" in persona.greeting()
    finally:
        del os.environ["ASSISTANT_PERSONA_NAME"]
        importlib.reload(persona)
    assert persona.greeting() == LINE


tests = [(k, val) for k, val in sorted(globals().items()) if k.startswith("test_")]
results = [run(n, f) for n, f in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
