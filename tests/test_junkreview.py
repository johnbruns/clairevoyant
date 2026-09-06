"""Tests for the Friday junk sweep.

This is the only feature that moves and deletes real mail, so the tests that
matter most are the ones about what it refuses to do: never surface phishing,
never act on a GET, never destroy anything outright, never offer an event the
events digest has already offered.
"""

import datetime as dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import actions, pages
from shared import junkreview as jr
from shared.junkmail import build_junk_review

TODAY = dt.date(2026, 9, 11)      # a Friday
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
    return jr.FileJunkStore(Path(tempfile.mkdtemp()) / "junk.json")


def make(store, **kw):
    kw.setdefault("message_id", "m1")
    kw.setdefault("subject", "Regional Tech Council fall programming")
    kw.setdefault("sender_name", "Regional Tech Council")
    kw.setdefault("sender_address", "events@mdtechcouncil.org")
    kw.setdefault("why", "A local trade body the clinic works with.")
    kw.setdefault("kind", "organisation")
    return store.put(jr.JunkCandidate.new(**kw))


def an_event(store, **kw):
    kw.setdefault("kind", "event")
    kw.setdefault("event_title", "SME Lunch and Learn")
    kw.setdefault("event_start", "2026-09-22T12:00:00")
    return make(store, **kw)


# ---- what the prompt refuses ---------------------------------------------

def test_the_prompt_rules_out_phishing_by_name():
    """The most legitimate-looking mail in a junk folder is the phishing."""
    text = jr.JUNK_SYSTEM.lower()
    assert "phishing" in text
    assert "credential harvesting" in text
    for lure in ("microsoft", "bank", "courier", "verify your account"):
        assert lure in text, f"{lure} should be named as a lure to ignore"


def test_the_prompt_defaults_to_junk():
    text = jr.JUNK_SYSTEM.lower()
    assert "default to junk" in text
    assert "untrusted data" in text


def test_an_ai_instruction_in_the_body_is_itself_a_reason_to_bin_it():
    assert "instructions aimed at an ai" in jr.JUNK_SYSTEM.lower()


# ---- validation ----------------------------------------------------------

def test_an_item_without_a_reason_is_not_surfaced():
    assert not jr.valid({"message_id": "m1", "kind": "person", "why": " "})


def test_an_item_with_an_invented_kind_is_not_surfaced():
    assert not jr.valid({"message_id": "m1", "kind": "vip", "why": "looks real"})


def test_an_item_with_no_message_is_not_surfaced():
    assert not jr.valid({"message_id": "", "kind": "person", "why": "looks real"})


def test_a_well_formed_item_is_surfaced():
    assert jr.valid({"message_id": "m1", "kind": "person", "why": "A real person."})


# ---- the blocklist -------------------------------------------------------

def test_a_blocked_sender_is_remembered():
    store = a_store()
    store.block("Spam@Example.COM")
    assert "spam@example.com" in store.blocked(), "matching must be case-insensitive"


def test_the_blocklist_survives_pruning():
    # Records expire; a block does not. A block that quietly lapsed after
    # four months would re-surface a sender Alex has already judged.
    store = a_store()
    store.block("spam@example.com")
    stale = jr.JunkCandidate.new(
        message_id="old", seen=(dt.datetime.now(dt.timezone.utc)
                                - dt.timedelta(days=jr.RETENTION_DAYS + 5)).isoformat())
    store.put(stale)
    make(store)                                  # any write triggers the prune
    assert store.get(stale.id) is None
    assert "spam@example.com" in store.blocked()


def test_the_blocklist_is_not_returned_as_a_candidate():
    store = a_store()
    store.block("spam@example.com")
    assert store.get(jr.BLOCKLIST_KEY) is None


# ---- decisions -----------------------------------------------------------

def test_a_decision_can_only_be_made_once():
    store = a_store()
    candidate = make(store)
    assert store.decide(candidate.id, jr.RESCUED) is not None
    assert store.decide(candidate.id, jr.BLOCKED) is None
    assert store.get(candidate.id).status == jr.RESCUED


def test_only_an_event_with_a_real_start_can_be_calendared():
    store = a_store()
    assert an_event(store).is_event
    assert not make(store, kind="event", event_start="").is_event
    assert not make(store, kind="organisation").is_event


# ---- the email -----------------------------------------------------------

def test_the_review_states_the_five_oclock_deadline():
    _, body = build_junk_review([make(a_store())], TODAY, scanned=97)
    assert "5:00 PM" in body
    assert "97" in body, "he should know how much was read, not just what was kept"


def test_the_review_promises_nothing_is_destroyed():
    _, body = build_junk_review([make(a_store())], TODAY, scanned=10)
    assert "Deleted Items" in body
    assert "thirty days" in body


def test_the_review_carries_the_persona_greeting():
    _, body = build_junk_review([make(a_store())], TODAY, scanned=1)
    assert "Claire Voyant" in body


def test_an_event_gets_the_calendar_button_and_others_do_not():
    store = a_store()
    event, plain = an_event(store), make(store, message_id="m2")
    links = {c.id: {a: f"https://x/api/junk?t={a}-{c.id}" for a in actions.JUNK_ACTIONS}
             for c in (event, plain)}

    _, with_event = build_junk_review([event], TODAY, links, scanned=2)
    _, without = build_junk_review([plain], TODAY, links, scanned=2)

    assert "add to calendar" in with_event
    assert "add to calendar" not in without
    for body in (with_event, without):
        assert "Move to inbox" in body
        assert "Block sender &amp; delete" in body or "Block sender & delete" in body


def test_a_date_only_event_shows_no_invented_start_time():
    _, body = build_junk_review(
        [an_event(a_store(), event_start="2026-09-22T09:00:00")], TODAY, scanned=1)
    assert "9:00 AM" not in body


def test_a_hostile_subject_cannot_inject_markup():
    _, body = build_junk_review(
        [make(a_store(), subject="<script>alert(1)</script>")], TODAY, scanned=1)
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def test_the_subject_line_carries_the_deadline():
    subject, _ = build_junk_review([make(a_store())], TODAY, scanned=5)
    assert "5 PM" in subject
    assert "(1)" in subject


# ---- routing and confirmation -------------------------------------------

def test_junk_links_point_at_the_junk_endpoint():
    for action in actions.JUNK_ACTIONS:
        url = actions.action_url(action, "j1", KEY, base="https://x/api")
        assert url.startswith("https://x/api/junk?t="), url


def test_the_four_action_groups_do_not_overlap():
    groups = (actions.JUNK_ACTIONS, actions.EVENT_ACTIONS, actions.AGENDA_ACTIONS)
    for i, first in enumerate(groups):
        for second in groups[i + 1:]:
            assert set(first).isdisjoint(second)
    assert all(a in actions.ACTIONS for a in actions.JUNK_ACTIONS)


def test_nothing_happens_on_a_get():
    # Every one of these three moves or deletes mail, and link scanners follow
    # every URL in every message that arrives.
    body = pages.confirm_junk(make(a_store()), "junk_block", "tok")
    assert "method='post'" in body


def test_the_block_page_is_honest_about_what_block_means():
    body = pages.confirm_junk(make(a_store()), "junk_block", "tok")
    assert "thirty days" in body
    assert "blocked-senders list" in body, "it must not imply an Outlook block"


def test_the_rescue_page_says_it_survives_the_sweep():
    body = pages.confirm_junk(make(a_store()), "junk_rescue", "tok")
    assert "5:00 PM" in body


def test_the_calendar_page_promises_no_invitations():
    body = pages.confirm_junk(an_event(a_store()), "junk_calendar", "tok")
    assert "No invitation is sent" in body


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(name, fn) for name, fn in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
