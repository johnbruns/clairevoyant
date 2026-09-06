"""Tests for the events digest.

Three behaviours carry the whole feature, and each one is a way Alex stops
reading the email if it breaks:

1. The same conference announced four times appears once.
2. Anything he already answered never comes back.
3. "No thanks" is a delete, so it must be unreachable by GET.
"""

import datetime as dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import actions, pages
from shared import events as ev
from shared.eventmail import build_event_digest

TODAY = dt.date(2026, 9, 8)
KEY = b"k" * 48


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


def a_store():
    return ev.FileEventStore(Path(tempfile.mkdtemp()) / "events.json")


def make(store, title="Cyber Summit", start="2026-10-14T09:00:00", **kw):
    return store.put(ev.EventCandidate.new(
        fingerprint=ev.fingerprint(title, start), title=title, start=start, **kw))


# ---- deduping ------------------------------------------------------------

def test_the_same_event_announced_differently_has_one_fingerprint():
    # "Save the date" at 9am and "Last chance" at 5:30pm: one conference.
    assert ev.fingerprint("Cyber Summit 2026", "2026-10-14T09:00:00") == \
           ev.fingerprint("cyber-summit 2026", "2026-10-14T17:30:00")


def test_the_same_name_on_a_different_day_is_a_different_event():
    assert ev.fingerprint("Monthly Mixer", "2026-09-10T18:00:00") != \
           ev.fingerprint("Monthly Mixer", "2026-10-10T18:00:00")


# ---- remembering decisions ----------------------------------------------

def test_a_declined_event_is_never_offered_again():
    store = a_store()
    candidate = make(store)
    store.decide(candidate.id, ev.DECLINED)

    assert candidate.fingerprint in store.decided_fingerprints()
    assert candidate.fingerprint not in store.open_fingerprints()


def test_an_event_still_awaiting_an_answer_is_not_offered_twice():
    store = a_store()
    candidate = make(store)
    assert candidate.fingerprint in store.open_fingerprints()
    assert candidate.fingerprint not in store.decided_fingerprints()


def test_a_decision_can_only_be_made_once():
    store = a_store()
    candidate = make(store)

    assert store.decide(candidate.id, ev.ACCEPTED) is not None
    # A second click, or a scanner replaying the POST, changes nothing.
    assert store.decide(candidate.id, ev.DECLINED) is None
    assert store.get(candidate.id).status == ev.ACCEPTED


def test_a_decision_outlives_the_message_it_came_from():
    store = a_store()
    old = ev.EventCandidate.new(
        fingerprint="abc", title="Old", start="2026-01-01T09:00:00",
        seen=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat())
    store.put(old)
    assert store.get(old.id) is not None      # 30 days is well inside retention


def test_records_past_retention_are_pruned():
    store = a_store()
    stale = ev.EventCandidate.new(
        fingerprint="old", title="Ancient", start="2020-01-01T09:00:00",
        seen=(dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(days=ev.RETENTION_DAYS + 5)).isoformat())
    store.put(stale)
    make(store)                                # any write triggers the prune
    assert store.get(stale.id) is None


# ---- what counts as an event --------------------------------------------

def test_an_event_in_the_past_is_rejected():
    assert not ev.valid({"title": "Gone", "start": "2026-08-01T09:00:00"}, TODAY)


def test_an_event_without_a_date_is_rejected():
    assert not ev.valid({"title": "Someday", "start": ""}, TODAY)


def test_an_event_without_a_title_is_rejected():
    assert not ev.valid({"title": "  ", "start": "2026-10-14T09:00:00"}, TODAY)


def test_a_future_event_is_accepted():
    assert ev.valid({"title": "Summit", "start": "2026-10-14T09:00:00"}, TODAY)


def test_something_later_today_still_counts():
    assert ev.valid({"title": "Tonight", "start": "2026-09-08T18:00:00"}, TODAY)


def test_the_extraction_prompt_rules_out_deadlines_and_injection():
    text = ev.EVENT_SYSTEM.lower()
    assert "deadlines" in text
    assert "these are not events" in text
    assert "untrusted data" in text


# ---- the digest itself ---------------------------------------------------

def test_the_digest_carries_the_persona_greeting():
    _, body = build_event_digest([make(a_store())], TODAY)
    assert "Claire Voyant" in body
    assert "Senior Administrative Strategist" in body


def test_every_event_gets_all_three_answers():
    candidate = make(a_store())
    links = {candidate.id: {a: f"https://x/api/event?t={a}"
                            for a in actions.EVENT_ACTIONS}}
    _, body = build_event_digest([candidate], TODAY, links)

    assert "attend that" in body
    assert "I might attend" in body
    assert "No, thanks!" in body
    for action in actions.EVENT_ACTIONS:
        assert f"t={action}" in body


def test_nothing_found_says_so_plainly():
    subject, body = build_event_digest([], TODAY)
    assert "nothing new" in subject.lower()
    assert "Claire Voyant" in body


def test_a_date_only_event_shows_no_invented_start_time():
    # T09:00 is the extractor's placeholder for "date known, time not".
    _, body = build_event_digest([make(a_store(), start="2026-10-14T09:00:00")], TODAY)
    assert "9:00 AM" not in body


def test_a_timed_event_shows_its_time():
    _, body = build_event_digest([make(a_store(), start="2026-10-14T18:30:00")], TODAY)
    assert "6:30 PM" in body


def test_junk_is_labelled_so_he_knows_where_it_was_hiding():
    _, body = build_event_digest([make(a_store(), source_folder="junk")], TODAY)
    assert "found in Junk" in body


def test_the_subject_counts_the_events():
    store = a_store()
    subject, _ = build_event_digest(
        [make(store), make(store, title="Other", start="2026-11-01T09:00:00")], TODAY)
    assert "(2)" in subject


# ---- routing and the confirmation pages ---------------------------------

def test_event_links_point_at_the_event_endpoint():
    for action in actions.EVENT_ACTIONS:
        url = actions.action_url(action, "e1", KEY, base="https://x/api")
        assert url.startswith("https://x/api/event?t="), url


def test_the_three_action_groups_do_not_overlap():
    # route_for is the only thing keeping them apart, so an action in two
    # groups would silently send a token to the wrong handler.
    assert set(actions.EVENT_ACTIONS).isdisjoint(actions.AGENDA_ACTIONS)
    assert all(a in actions.ACTIONS for a in actions.EVENT_ACTIONS)


def test_each_answer_has_its_own_confirmation_page():
    candidate = make(a_store(), location="Baltimore, MD")

    yes = pages.confirm_event(candidate, "event_yes", "tok")
    maybe = pages.confirm_event(candidate, "event_maybe", "tok")
    no = pages.confirm_event(candidate, "event_no", "tok")

    assert "Yes, add it" in yes
    assert "Baltimore, MD" in yes
    assert ev.MAYBE_NOTE in maybe
    assert "Deleted Items" in no


def test_the_confirmation_page_only_acts_on_a_post():
    body = pages.confirm_event(make(a_store()), "event_no", "tok")
    assert "method='post'" in body
    assert "confirm" in body


def test_a_hostile_title_cannot_inject_markup():
    candidate = make(a_store(), title="<script>alert(1)</script>")
    _, body = build_event_digest([candidate], TODAY)
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_one_conference_announced_twice_collapses_within_a_run():
    # "FINAL AGENDA" quoting day two and "one week to register" quoting day
    # one are the same summit. Different fingerprints, same title key.
    a = ev.fingerprint("Billington CyberSecurity Summit", "2026-09-08T09:00:00")
    b = ev.fingerprint("Billington CyberSecurity Summit", "2026-09-09T09:00:00")
    assert a != b
    assert ev.title_key("Billington CyberSecurity Summit") ==            ev.title_key("billington cybersecurity summit")


def test_a_recurring_event_is_not_folded_across_runs():
    # September's mixer and October's are two real events with one name, so
    # the title key must never be what the STORE remembers.
    store = a_store()
    sept = make(store, title="Monthly Mixer", start="2026-09-10T18:00:00")
    store.decide(sept.id, ev.DECLINED)
    oct_print = ev.fingerprint("Monthly Mixer", "2026-10-08T18:00:00")
    assert oct_print not in store.decided_fingerprints()


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(name, fn) for name, fn in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
