import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import planning
from shared.planning import DayPlan, recommend_day

FOR_DATE = dt.date(2026, 9, 3)


class Issue:
    """Stand-in for jira_client.Issue - importing that would pull in requests."""

    def __init__(self, key, summary="Do the thing", **fields):
        self.key = key
        self.summary = summary
        self.url = f"https://crc.atlassian.net/browse/{key}"
        self._fields = fields

    def for_model(self):
        return {"key": self.key, "summary": self.summary, **self._fields}


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


def plan_json(*keys, focus="A clear afternoon."):
    return json.dumps(
        {"focus": focus, "picks": [{"key": k, "why": f"because {k}"} for k in keys]}
    )


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


def test_picks_are_matched_back_to_real_issues():
    client = FakeClient([plan_json("TASK-1")])
    plan = recommend_day(client, [Issue("TASK-1", "Draft the letter")], [], ["9am-4pm"], FOR_DATE)

    assert plan.focus == "A clear afternoon."
    assert len(plan.picks) == 1
    assert plan.picks[0].summary == "Draft the letter", "summary comes from Jira, not the model"
    assert plan.picks[0].url.endswith("/browse/TASK-1")


def test_an_invented_issue_key_is_dropped():
    # A ticket number that goes nowhere is worse than no recommendation: Alex
    # clicks it, gets a 404, and stops trusting the section.
    client = FakeClient([plan_json("TASK-1", "TASK-999")])
    plan = recommend_day(client, [Issue("TASK-1")], [], [], FOR_DATE)
    assert [p.key for p in plan.picks] == ["TASK-1"]


def test_picks_are_capped():
    client = FakeClient([plan_json("TASK-1", "TASK-2", "TASK-3", "TASK-4", "TASK-5")])
    issues = [Issue(f"TASK-{i}") for i in range(1, 6)]
    plan = recommend_day(client, issues, [], [], FOR_DATE)
    assert len(plan.picks) == planning.MAX_PICKS


def test_an_empty_backlog_skips_the_model_entirely():
    client = FakeClient([])
    plan = recommend_day(client, [], [], [], FOR_DATE)
    assert client.calls == [], "no backlog means nothing to spend a model call on"
    assert plan.empty
    assert "Nothing on the TASK board" in plan.note


def test_a_model_failure_degrades_to_a_note():
    # The agenda still has to go out; the calendar half is useful on its own.
    client = FakeClient([RuntimeError("overloaded")])
    plan = recommend_day(client, [Issue("TASK-1")], [], [], FOR_DATE)
    assert plan.picks == []
    assert "overloaded" in plan.note


def test_unparseable_output_degrades_to_a_note():
    client = FakeClient(["I'd rather not answer that."])
    plan = recommend_day(client, [Issue("TASK-1")], [], [], FOR_DATE)
    assert "Could not build recommendations" in plan.note


def test_the_calendar_shape_reaches_the_model():
    # The whole point is fitting work to the day. Without the meetings and the
    # open blocks the model is just re-ranking the backlog.
    events = [{
        "subject": "Board sync",
        "start": {"dateTime": "2026-09-03T10:00:00.0000000"},
        "end": {"dateTime": "2026-09-03T11:00:00.0000000"},
    }]
    client = FakeClient([plan_json("TASK-1")])
    recommend_day(client, [Issue("TASK-1")], events, ["11am-4pm"], FOR_DATE)

    sent = client.calls[0]["messages"][0]["content"]
    assert "Board sync" in sent
    assert '"start": "10:00"' in sent, sent
    assert "11am-4pm" in sent


def test_cancelled_meetings_are_not_described_as_the_day():
    events = [{"subject": "Called off", "isCancelled": True,
               "start": {"dateTime": "2026-09-03T10:00:00"},
               "end": {"dateTime": "2026-09-03T11:00:00"}}]
    client = FakeClient([plan_json("TASK-1")])
    recommend_day(client, [Issue("TASK-1")], events, [], FOR_DATE)
    assert "Called off" not in client.calls[0]["messages"][0]["content"]


def test_backlog_is_capped_before_it_reaches_the_model():
    issues = [Issue(f"TASK-{i}") for i in range(100)]
    client = FakeClient([plan_json("TASK-1")])
    recommend_day(client, issues, [], [], FOR_DATE)
    sent = client.calls[0]["messages"][0]["content"]
    assert sent.count('"key"') <= planning.MAX_ISSUES_CONSIDERED + 1, sent.count('"key"')


def test_which_day_is_passed_so_wording_can_differ():
    client = FakeClient([plan_json("TASK-1")])
    recommend_day(client, [Issue("TASK-1")], [], [], FOR_DATE, label="tomorrow")
    assert '"which_day": "tomorrow"' in client.calls[0]["messages"][0]["content"]


def test_prompt_frames_jira_content_as_data():
    # Issue summaries are written by anyone with board access, and the planner
    # reads them with a model. Same untrusted-content rule as the inbox.
    assert "DATA" in planning.PLANNING_SYSTEM
    assert "Never invent an issue" in planning.PLANNING_SYSTEM


def test_summarise_team_writes_a_summary_onto_each_person():
    import json as _json
    from shared.planning import TeamSection, summarise_team

    secs = [TeamSection(name="Alex"), TeamSection(name="Priya")]
    client = FakeClient([_json.dumps({
        "overall": "Priya is the bottleneck this week.",
        "people": {
            "Alex": {"recap": "Alex closed seven.", "attention": "Nothing pressing."},
            "Priya": {"recap": "Priya closed nothing.", "attention": "66 open, oldest from June."},
        },
    })])
    overall = summarise_team(client, secs, days=7)

    assert overall == "Priya is the bottleneck this week."
    assert secs[0].recap == "Alex closed seven."
    assert secs[0].attention == "Nothing pressing."
    assert secs[1].recap == "Priya closed nothing."
    assert secs[1].attention == "66 open, oldest from June."


def test_a_summary_failure_leaves_the_sections_usable():
    from shared.planning import TeamSection, summarise_team

    secs = [TeamSection(name="Alex")]
    assert summarise_team(FakeClient([RuntimeError("overloaded")]), secs) == ""
    assert secs[0].recap == "" and secs[0].attention == ""


def test_a_person_missing_from_the_model_output_is_not_faked():
    import json as _json
    from shared.planning import TeamSection, summarise_team

    secs = [TeamSection(name="Alex"), TeamSection(name="Priya")]
    summarise_team(FakeClient([_json.dumps(
        {"overall": "x",
         "people": {"Alex": {"recap": "only Alex", "attention": "none"}}})]), secs)
    assert secs[0].recap == "only Alex"
    assert secs[1].recap == "", "Priya gets no summary rather than an invented one"


def test_the_older_single_string_shape_is_tolerated():
    # If the model answers in the previous format, keep the prose rather than
    # dropping it on the floor.
    import json as _json
    from shared.planning import TeamSection, summarise_team

    secs = [TeamSection(name="Alex")]
    summarise_team(FakeClient([_json.dumps(
        {"overall": "x", "people": {"Alex": "plain string"}})]), secs)
    assert secs[0].recap == "plain string"


def test_the_prompt_keeps_the_three_jobs_distinct():
    assert "ACROSS THE TEAM" in planning.TEAM_SUMMARY_SYSTEM
    assert "WHERE IT STANDS" in planning.TEAM_SUMMARY_SYSTEM
    assert "NEEDS ATTENTION" in planning.TEAM_SUMMARY_SYSTEM
    assert "do not manufacture a concern" in planning.TEAM_SUMMARY_SYSTEM


def test_the_roster_is_empty_until_it_is_configured():
    """No built-in roster on purpose.

    A guessed project key queries something that does not exist and reports a
    team where nobody finished anything, every week, which reads as a working
    feature saying bad news rather than an unconfigured one.
    """
    import importlib, os

    from shared import team

    assert team.roster() == ()

    os.environ["TEAM_ROSTER"] = '[{"name": "Sam", "jql": "project = \'ST\'"}]'
    try:
        importlib.reload(team)
        people = team.roster()
        assert [p.name for p in people] == ["Sam"]
        assert people[0].jql == "project = 'ST'"
    finally:
        del os.environ["TEAM_ROSTER"]
        importlib.reload(team)


def test_day_plan_empty_reports_correctly():
    assert DayPlan().empty
    assert not DayPlan(focus="something").empty


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(name, fn) for name, fn in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
