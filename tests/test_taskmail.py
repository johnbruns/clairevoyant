import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import digests
from shared.digests import FileDigestStore, TaskDigest, find_marker, marker_tag
from shared.planning import TeamSection
from shared.taskmail import build_reply_confirmation, build_task_digest, build_team_digest
from shared.taskreply import Operation, ReplyResult

MONDAY = dt.date(2026, 9, 7)
NOW = dt.datetime(2026, 9, 7, 7, 0)


class Issue:
    def __init__(self, key, summary, status="To Do", category="To Do",
                 due=None, priority=None, url=""):
        self.key = key
        self.summary = summary
        self.status = status
        self.status_category = category
        self.due = due
        self.priority = priority
        self.url = url or f"https://your-site.atlassian.net/browse/{key}"


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


# ---- the task digest -----------------------------------------------------


def test_digest_lists_tasks_and_carries_a_marker():
    issues = [Issue("TASK-41", "SOC 2 gap letter"), Issue("TASK-38", "Revised MOU")]
    subject, body = build_task_digest(issues, "a1b2c3d4", MONDAY)

    assert "[#T-a1b2c3d4]" in subject
    assert "TASK-41" in body and "SOC 2 gap letter" in body
    assert "2 open" in subject


def test_the_marker_survives_a_reply_prefix():
    # This is the whole matching mechanism: RE: must not break it.
    subject, _ = build_task_digest([Issue("TASK-1", "x")], "a1b2c3d4", MONDAY)
    assert find_marker(f"RE: {subject}") == "a1b2c3d4"
    assert find_marker(f"Re: Re: FW: {subject}") == "a1b2c3d4"


def test_digest_tells_alex_he_can_reply():
    # If he does not know replying works, the feature does not exist.
    _, body = build_task_digest([Issue("TASK-1", "x")], "m", MONDAY)
    assert "Reply to this email" in body
    assert "TASK-41 done" in body or "did the MOU one" in body


def test_overdue_is_grouped_first_and_flagged():
    # Keys chosen not to collide with the TASK-41/TASK-12/TASK-8 examples in the
    # "how to reply" block, which sits above the task list.
    issues = [
        Issue("TASK-777", "Later thing", due="2026-10-01"),
        Issue("TASK-888", "Late thing", due="2026-09-01"),
    ]
    subject, body = build_task_digest(issues, "m", MONDAY)

    assert body.index("TASK-888") < body.index("TASK-777"), "overdue must lead"
    assert "overdue" in subject
    assert "overdue by 6d" in body


def test_due_today_is_called_out():
    _, body = build_task_digest([Issue("TASK-1", "x", due="2026-09-07")], "m", MONDAY)
    assert "due today" in body


def test_undated_in_progress_is_grouped_separately():
    issues = [
        Issue("TASK-1", "Started", status="In Progress", category="In Progress"),
        Issue("TASK-2", "Not started"),
    ]
    _, body = build_task_digest(issues, "m", MONDAY)
    assert "In progress" in body
    assert body.index("TASK-1") < body.index("TASK-2")


def test_an_unparseable_due_date_does_not_crash():
    _, body = build_task_digest([Issue("TASK-1", "x", due="not-a-date")], "m", MONDAY)
    assert "TASK-1" in body


def test_an_empty_board_reads_cleanly():
    subject, body = build_task_digest([], "m", MONDAY)
    assert "nothing open" in subject.lower()
    assert "board is out of date" in body


def test_task_digest_escapes_summaries():
    _, body = build_task_digest([Issue("TASK-1", "<script>alert(1)</script>")], "m", MONDAY)
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


# ---- the confirmation ----------------------------------------------------


def test_confirmation_lists_what_changed():
    result = ReplyResult(operations=[
        Operation("done", "TASK-41", note="SOC 2 gap letter", applied=True,
                  outcome="moved to Done"),
    ])
    subject, body = build_reply_confirmation(result, NOW)

    assert "1 changed" in subject
    assert "TASK-41" in body and "moved to Done" in body


def test_confirmation_separates_failures_and_skips():
    result = ReplyResult(
        operations=[
            Operation("done", "TASK-41", applied=True, outcome="moved to Done"),
            Operation("done", "TASK-19", outcome="Jira offers no transition to Done right now"),
        ],
        skipped=["TASK-999 - not in that task list"],
    )
    subject, body = build_reply_confirmation(result, NOW)

    assert "Could not apply" in body
    assert "no transition" in body
    assert "Left alone" in body
    assert "TASK-999" in body
    assert "1 changed, 2 not" in subject


def test_confirmation_on_a_parse_error_says_nothing_changed():
    subject, body = build_reply_confirmation(ReplyResult(error="Could not read that."), NOW)
    assert "nothing changed" in subject.lower()
    assert "Could not read that." in body


def test_confirmation_mentions_undo():
    result = ReplyResult(operations=[Operation("done", "TASK-1", applied=True, outcome="done")])
    _, body = build_reply_confirmation(result, NOW)
    assert "undo" in body.lower()


# ---- the team digest -----------------------------------------------------


def sections():
    return [
        TeamSection(
            name="Sam",
            done=[Issue("TT-3", "Shipped the scanner config", category="Done")],
            open=[Issue("TT-4", "Write the runbook")],
            open_count=1,
        ),
        TeamSection(
            name="Priya",
            done=[],
            open=[Issue(f"RT-{i}", f"Thing {i}") for i in range(1, 12)],
            open_count=11,
        ),
    ]


def test_team_digest_shows_finished_and_remaining_per_person():
    subject, body = build_team_digest(sections(), MONDAY, days=7)

    assert "Sam" in body and "Priya" in body
    assert "Shipped the scanner config" in body
    assert "Write the runbook" in body
    assert "1 finished" in subject


def test_a_person_who_closed_nothing_is_stated_not_omitted():
    # Silence would read as "no data". "Nothing closed" is the actual finding.
    _, body = build_team_digest(sections(), MONDAY)
    assert "Nothing closed this week" in body


def test_the_open_list_is_complete_by_default():
    # Alex asked for the whole list. A truncated one is a list he has to leave
    # the email to finish reading, which defeats the digest.
    _, body = build_team_digest(sections(), MONDAY)
    for n in range(1, 12):
        assert f"RT-{n}</b>" in body, f"RT-{n} missing from the list"
    assert "more." not in body
    assert "Still to do (11)" in body, "the count belongs in the heading"


def test_the_list_can_still_be_capped():
    secs = sections()
    secs[1].shown = 4          # TEAM_MAX_OPEN_SHOWN set to a number
    _, body = build_team_digest(secs, MONDAY)
    assert "RT-4</b>" in body
    assert "RT-5</b>" not in body
    assert "and 7 more" in body


def test_a_per_person_recap_is_shown_and_labelled():
    secs = sections()
    secs[0].recap = "Sam closed nothing and all three of his items are MSP onboarding."
    _, body = build_team_digest(secs, MONDAY)
    assert "all three of his items are MSP onboarding" in body
    assert "Where it stands" in body


def test_a_missing_per_person_summary_leaves_no_empty_block():
    _, body = build_team_digest(sections(), MONDAY)
    assert "class='psum'" not in body


def test_per_person_summaries_are_escaped():
    secs = sections()
    secs[0].recap = "<script>alert(1)</script>"
    _, body = build_team_digest(secs, MONDAY)
    assert "<script>" not in body


def test_a_failed_section_says_so_and_does_not_kill_the_digest():
    secs = sections() + [TeamSection(name="Dana", error="Jira returned 404")]
    _, body = build_team_digest(secs, MONDAY)
    assert "Dana" in body
    assert "Could not read" in body
    assert "Sam" in body, "one failure must not lose the rest"


def test_a_failed_summary_pass_is_stated_not_hidden():
    # Bare lists with no explanation look like a digest that had nothing to
    # say. The same silent-degradation trap as the zero-issue digest.
    _, body = build_team_digest(sections(), MONDAY, summary="", summary_failed=True)
    assert "could not be generated" in body
    assert "RT-1</b>" in body, "the lists must still be complete"


def test_no_note_when_the_summary_simply_was_not_requested():
    _, body = build_team_digest(sections(), MONDAY, summary="", summary_failed=False)
    assert "could not be generated" not in body


def test_team_summary_is_included_when_present():
    _, body = build_team_digest(sections(), MONDAY, summary="Priya has closed nothing in a month.")
    assert "Priya has closed nothing in a month." in body


def test_team_digest_escapes_content():
    secs = [TeamSection(name="Sam", done=[Issue("TT-1", "<img src=x onerror=1>")],
                        open=[], open_count=0)]
    _, body = build_team_digest(secs, MONDAY)
    assert "<img src=x" not in body


# ---- the digest store ----------------------------------------------------


def test_digest_round_trips():
    import tempfile

    store = FileDigestStore(Path(tempfile.mkdtemp()) / "d.json")
    saved = store.put(TaskDigest.new({"TASK-1": "One", "TASK-2": "Two"}))
    loaded = store.get(saved.marker)

    assert loaded is not None
    assert loaded.issues == {"TASK-1": "One", "TASK-2": "Two"}


def test_unknown_marker_is_none():
    import tempfile

    assert FileDigestStore(Path(tempfile.mkdtemp()) / "d.json").get("ffffffff") is None


def test_old_digests_are_pruned():
    old = {"sent": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=60)).isoformat()}
    fresh = {"sent": dt.datetime.now(dt.timezone.utc).isoformat()}
    kept = digests.DigestStore._prune({"old": old, "new": fresh})
    assert set(kept) == {"new"}


def test_markers_are_unique():
    assert len({digests.new_marker() for _ in range(200)}) == 200


def test_marker_tag_and_find_are_inverses():
    marker = digests.new_marker()
    assert find_marker(f"Your tasks {marker_tag(marker)}") == marker


def test_a_subject_with_no_marker_returns_none():
    assert find_marker("Re: lunch tomorrow") is None
    assert find_marker(None) is None


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(name, fn) for name, fn in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
