"""Tests for the pending-draft queue.

The single-use guarantee lives here, not in the token, because the token cannot
know it has been spent. Every "already resolved" path in the endpoint depends
on `resolve` returning None exactly once, so that is what most of this file is
about.
"""

import datetime as dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.pending import (
    DISCARDED,
    PENDING,
    SENT,
    FilePendingStore,
    PendingDraft,
    PendingStore,
)


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


def store():
    tmp = tempfile.mkdtemp()
    return FilePendingStore(Path(tmp) / "pending.json")


def a_draft(**over):
    fields = dict(
        message_id="AAMk-msg-1",
        subject="Capstone timing",
        sender_name="Priya Nair",
        sender_address="priya@x.org",
        the_ask="Wants 30 minutes next week about the capstone timeline.",
        request_type="meeting",
        body="Glad to meet.\n\nAlex",
    )
    fields.update(over)
    return PendingDraft.new(**fields)


def test_put_and_get_round_trip():
    s = store()
    saved = s.put(a_draft())
    loaded = s.get(saved.id)

    assert loaded is not None
    assert loaded.message_id == "AAMk-msg-1"
    assert loaded.sender_name == "Priya Nair"
    assert loaded.the_ask.startswith("Wants 30 minutes")
    assert loaded.body == "Glad to meet.\n\nAlex"
    assert loaded.status == PENDING
    assert loaded.open


def test_ids_are_unique():
    assert a_draft().id != a_draft().id


def test_missing_draft_is_none_not_an_exception():
    assert store().get("nope") is None


# ---- single use ----------------------------------------------------------


def test_a_draft_resolves_exactly_once():
    # Two clicks on "approve and send" must send one email.
    s = store()
    d = s.put(a_draft())

    assert s.resolve(d.id, SENT) is not None, "first click should win"
    assert s.resolve(d.id, SENT) is None, "second click must be refused"


def test_send_and_discard_race_resolves_once():
    s = store()
    d = s.put(a_draft())
    assert s.resolve(d.id, SENT) is not None
    assert s.resolve(d.id, DISCARDED) is None


def test_resolved_status_is_readable_afterwards():
    # The endpoint tells Alex what already happened, so it has to know which.
    s = store()
    d = s.put(a_draft())
    s.resolve(d.id, DISCARDED, "discarded from recap")

    after = s.get(d.id)
    assert after.status == DISCARDED
    assert not after.open
    assert after.resolution_note == "discarded from recap"
    assert after.resolved


def test_resolving_an_unknown_draft_is_none():
    assert store().resolve("nope", SENT) is None


def test_rollback_reopens_a_claimed_draft():
    # A send that failed after the claim must be retryable, or the draft reads
    # "sent" forever having never been sent.
    s = store()
    d = s.put(a_draft())
    s.resolve(d.id, SENT)
    s.resolve_rollback(d.id)

    assert s.get(d.id).open
    assert s.resolve(d.id, SENT) is not None, "should be sendable again"


def test_rollback_of_an_open_draft_is_a_no_op():
    s = store()
    d = s.put(a_draft())
    s.resolve_rollback(d.id)
    assert s.get(d.id).open


# ---- editing -------------------------------------------------------------


def test_body_can_be_edited_while_pending():
    s = store()
    d = s.put(a_draft())
    s.update_body(d.id, "Rewritten by Alex.")
    assert s.get(d.id).body == "Rewritten by Alex."


def test_an_already_sent_draft_cannot_be_edited():
    # Otherwise the record would misrepresent what actually went out.
    s = store()
    d = s.put(a_draft())
    s.resolve(d.id, SENT)
    s.update_body(d.id, "too late")
    assert s.get(d.id).body == "Glad to meet.\n\nAlex"


# ---- listing and pruning -------------------------------------------------


def test_open_drafts_excludes_resolved_ones():
    s = store()
    keep = s.put(a_draft())
    gone = s.put(a_draft())
    s.resolve(gone.id, SENT)

    assert [d.id for d in s.open_drafts()] == [keep.id]


def test_old_records_are_pruned():
    old = {"created": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)).isoformat()}
    fresh = {"created": dt.datetime.now(dt.timezone.utc).isoformat()}
    kept = PendingStore._prune({"old": old, "fresh": fresh})
    assert set(kept) == {"fresh"}


def test_unparseable_timestamps_are_dropped_not_kept_forever():
    kept = PendingStore._prune({"bad": {"created": "not-a-date"}})
    assert kept == {}


def test_retention_matches_the_token_lifetime():
    # A record that outlived its token would be dead weight; the reverse fails
    # closed already. They are meant to be the same week.
    from shared import actions, pending

    assert pending.RETENTION_DAYS * 24 * 3600 == actions.DEFAULT_TTL_SECONDS


def test_unknown_fields_in_storage_do_not_break_loading():
    # A record written by a newer version must not crash an older one.
    s = store()
    d = s.put(a_draft())
    raw = s._load()
    raw[d.id]["some_future_field"] = "surprise"
    s._save(raw)
    assert s.get(d.id).message_id == "AAMk-msg-1"


def test_outlook_draft_id_is_optional_and_preserved():
    s = store()
    without = s.put(a_draft())
    with_id = s.put(a_draft(outlook_draft_id="AAMk-draft-9"))

    assert s.get(without.id).outlook_draft_id is None
    assert s.get(with_id.id).outlook_draft_id == "AAMk-draft-9"


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(name, fn) for name, fn in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
