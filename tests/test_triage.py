import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import triage
from shared.triage import (
    Verdict,
    _clean,
    _extract_json_array,
    _normalise_request_type,
    classify,
    compose_meeting_reply,
    draft_reply,
    draft_to_html,
)


class FakeResponse:
    def __init__(self, text):
        self.content = [type("Block", (), {"text": text})()]


class FakeClient:
    """Records calls and returns canned text."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._replies:
            raise RuntimeError("no canned reply left")
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(reply)


def msg(mid, address, subject="hi", preview="body", to_alex=True):
    return {
        "id": mid,
        "from": {"emailAddress": {"name": "Someone", "address": address}},
        "toRecipients": (
            [{"emailAddress": {"address": "alex@example.org"}}] if to_alex else []
        ),
        "subject": subject,
        "bodyPreview": preview,
        "receivedDateTime": "2026-09-02T12:00:00Z",
    }


def verdict_json(mid, **over):
    payload = {
        "message_id": mid,
        "sender_type": "human",
        "reply_owed": True,
        "request_type": "request",
        "is_meeting_request": False,
        "urgency": "today",
        "category": "question",
        "worth_adding_to_contacts": False,
        "reasoning": "Direct question to Alex.",
    }
    payload.update(over)
    return payload


def v(mid="m1", request_type="request", **over):
    base = dict(
        message_id=mid,
        sender_type="human",
        reply_owed=True,
        is_meeting_request=request_type == "meeting",
        request_type=request_type,
        urgency="today",
        category="question",
        reasoning="because",
    )
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


def test_clean_strips_html_and_truncates():
    assert _clean("<p>hello <b>there</b></p>") == "hello there"
    assert len(_clean("x" * 99999)) == triage.MAX_BODY_CHARS


def test_extract_json_tolerates_surrounding_prose():
    parsed = _extract_json_array('Sure, here you go:\n[{"a": 1}]\nHope that helps.')
    assert parsed == [{"a": 1}]


def test_classify_parses_verdicts():
    messages = [msg("m1", "a@x.com"), msg("m2", "b@x.com")]
    canned = json.dumps(
        [verdict_json("m1"), verdict_json("m2", reply_owed=False, request_type="none")]
    )
    client = FakeClient([canned])
    result = classify(client, messages, known_addresses={"a@x.com"})

    assert len(result.verdicts) == 2, result.verdicts
    assert [v.message_id for v in result.verdicts] == ["m1", "m2"]
    assert result.verdicts[0].known_sender is True
    assert result.verdicts[1].known_sender is False
    assert [v.message_id for v in result.needing_reply] == ["m1"]


def test_classify_batches_by_batch_size():
    messages = [msg(f"m{i}", f"{i}@x.com") for i in range(triage.BATCH_SIZE * 2 + 1)]
    canned = [
        json.dumps([verdict_json(f"m{i}") for i in range(triage.BATCH_SIZE)]),
        json.dumps(
            [verdict_json(f"m{i}") for i in range(triage.BATCH_SIZE, triage.BATCH_SIZE * 2)]
        ),
        json.dumps([verdict_json(f"m{triage.BATCH_SIZE * 2}")]),
    ]
    client = FakeClient(canned)
    result = classify(client, messages, known_addresses=set())
    assert len(client.calls) == 3, len(client.calls)
    assert len(result.verdicts) == len(messages)


def test_one_bad_batch_does_not_kill_the_run():
    messages = [msg(f"m{i}", f"{i}@x.com") for i in range(triage.BATCH_SIZE + 1)]
    canned = [
        RuntimeError("api exploded"),
        json.dumps([verdict_json(f"m{triage.BATCH_SIZE}")]),
    ]
    client = FakeClient(canned)
    result = classify(client, messages, known_addresses=set())

    assert len(result.verdicts) == len(messages), result.verdicts
    failed = [v for v in result.verdicts if v.category == "triage-error"]
    assert len(failed) == triage.BATCH_SIZE
    # A message we could not classify must never be silently marked "no reply owed"
    # AND dropped - it is surfaced with the error attached.
    assert all(v.error for v in failed)
    assert result.verdicts[-1].category == "question"


def test_a_triage_error_is_never_draftable():
    # The error path sets reply_owed False, but a future change might not.
    # draftable is the gate that actually protects the Drafts folder.
    assert not v("m1", error="boom").draftable


def test_message_missing_from_response_is_recorded_as_skipped():
    messages = [msg("m1", "a@x.com"), msg("m2", "b@x.com")]
    client = FakeClient([json.dumps([verdict_json("m1")])])
    result = classify(client, messages, known_addresses=set())
    assert result.skipped == ["m2"], result.skipped
    assert len(result.verdicts) == 1


def test_cc_only_flag_is_sent_to_model():
    messages = [msg("m1", "a@x.com", to_alex=False)]
    client = FakeClient([json.dumps([verdict_json("m1")])])
    classify(client, messages, known_addresses=set())
    sent = client.calls[0]["messages"][0]["content"]
    assert '"owner_is_only_cc": true' in sent, sent


def test_untrusted_framing_present_in_both_prompts():
    messages = [msg("m1", "a@x.com")]
    client = FakeClient([json.dumps([verdict_json("m1")])])
    classify(client, messages, known_addresses=set())
    assert "UNTRUSTED DATA" in client.calls[0]["system"]
    assert "untrusted data" in client.calls[0]["messages"][0]["content"]

    client2 = FakeClient(["I will look into this and get back to you."])
    verdict = v("m1", "request")
    draft_reply(client2, verdict, messages[0], "body text")
    assert "UNTRUSTED DATA" in client2.calls[0]["system"]
    assert verdict.draft == "I will look into this and get back to you."


# ---- what does and does not get a draft ---------------------------------


def test_only_meetings_and_requests_are_draftable():
    assert v("m1", "meeting").draftable
    assert v("m1", "request").draftable
    assert not v("m1", "other").draftable
    assert not v("m1", "none").draftable


def test_no_reply_owed_is_never_draftable():
    assert not v("m1", "meeting", reply_owed=False).draftable


def test_suspicious_is_never_draftable():
    # A message that tried to instruct the assistant must not get a reply drafted
    # from its own contents, whatever request_type the model attached to it.
    assert not v("m1", "request", category="suspicious").draftable


def test_draft_reply_refuses_non_draftable_types_without_calling_the_model():
    for kind in ("other", "none"):
        client = FakeClient(["should never be used"])
        verdict = v("m1", kind)
        draft_reply(client, verdict, msg("m1", "a@x.com"), "body")
        assert verdict.draft is None, kind
        assert client.calls == [], f"{kind} reached the model"


def test_needing_draft_puts_meetings_first():
    result = triage.TriageResult(verdicts=[v("m1", "request"), v("m2", "meeting")])
    assert [x.message_id for x in result.needing_draft] == ["m2", "m1"]


def test_unrecognised_request_type_does_not_become_draftable():
    # A model that invents a label must fail closed. "escalate" is not one of
    # the two shapes Alex asked for, so it gets no draft.
    assert _normalise_request_type({"reply_owed": True, "request_type": "escalate"}) == "other"


def test_request_type_is_forced_to_none_when_no_reply_is_owed():
    payload = {"reply_owed": False, "request_type": "meeting"}
    assert _normalise_request_type(payload) == "none"


def test_legacy_meeting_flag_is_honoured_when_request_type_is_missing():
    payload = {"reply_owed": True, "is_meeting_request": True}
    assert _normalise_request_type(payload) == "meeting"


def test_is_meeting_request_always_agrees_with_request_type():
    # The two fields are independent in the model's output and a disagreement
    # would send a meeting reply to a message that is not a meeting ask.
    canned = json.dumps([verdict_json("m1", request_type="meeting", is_meeting_request=False)])
    result = classify(FakeClient([canned]), [msg("m1", "a@x.com")], known_addresses=set())
    assert result.verdicts[0].is_meeting_request is True


# ---- the meeting reply ---------------------------------------------------


AVAIL = "Monday, September 7: 9am-4pm\nTuesday, September 8: 9-10:30am\n\nAll times EDT."


def test_meeting_reply_appends_real_availability_and_booking_link():
    client = FakeClient(["Glad to meet. Looking forward to it.\n\nAlex"])
    verdict = v("m1", "meeting")
    draft_reply(client, verdict, msg("m1", "a@x.com"), "can we meet?", availability=AVAIL)

    assert "Looking forward to it." in verdict.draft
    assert "Monday, September 7: 9am-4pm" in verdict.draft
    assert triage.BOOKING_LINK in verdict.draft


def test_availability_never_reaches_the_drafting_model():
    # The times are appended by code precisely so a model cannot paraphrase
    # them. If they are in the prompt, a future prompt change could start
    # rewriting them, and a wrong offered slot costs a real reschedule.
    client = FakeClient(["prose"])
    draft_reply(client, v("m1", "meeting"), msg("m1", "a@x.com"), "meet?", availability=AVAIL)
    sent = client.calls[0]["messages"][0]["content"] + client.calls[0]["system"]
    assert "September 7" not in sent, "availability leaked into the drafting prompt"


def test_meeting_prompt_forbids_the_model_writing_times():
    assert "Do NOT write out any dates, times or availability" in triage.MEETING_DRAFT_SYSTEM


def test_request_reply_gets_no_availability_and_no_booking_link():
    client = FakeClient(["I will look into this and get back to you shortly.\n\nAlex"])
    verdict = v("m1", "request")
    draft_reply(client, verdict, msg("m1", "a@x.com"), "please review", availability=AVAIL)

    assert "book.example.com" not in (verdict.draft or "")
    assert "September 7" not in verdict.draft


def test_request_prompt_forbids_answering_the_substance():
    assert "Do NOT answer the substance" in triage.REQUEST_DRAFT_SYSTEM
    assert "get back to them as soon as" in triage.REQUEST_DRAFT_SYSTEM


def test_unreadable_calendar_leaves_a_visible_note_not_a_silent_omission():
    # A meeting reply that quietly ships with no times is one Alex sends without
    # noticing. The bracketed note is the same convention the drafting prompt
    # uses for anything a human has to fill in.
    text = compose_meeting_reply("Glad to meet.", None)
    assert "[note:" in text
    assert triage.BOOKING_LINK in text, "the booking link still works without a calendar"


LINK = "https://book.example.com/alex?anonymous&ismsaljsauthenabled"


def test_no_booking_link_is_configured_by_default():
    """A scheduling page is personal, so there is no default worth shipping."""
    assert triage.BOOKING_LINK == ""


def test_a_meeting_reply_without_a_booking_link_still_carries_the_times():
    body = compose_meeting_reply("Glad to meet.", AVAIL, booking_link="")
    assert "September 7" in body
    assert "book directly" not in body


def test_booking_link_survives_html_conversion_as_a_real_anchor():
    html = draft_to_html(compose_meeting_reply("Glad to meet.", AVAIL, booking_link=LINK))
    assert 'href="https://book.example.com/' in html
    # "&" in the query string must be escaped in the emitted HTML, and both the
    # href and the visible text carry the same escaped form.
    assert "?anonymous&amp;ismsaljsauthenabled" in html
    assert "&anonymous" not in html.replace("&amp;", "&").replace("?anonymous&", "")


def test_draft_to_html_still_escapes_untrusted_text():
    html = draft_to_html("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_draft_failure_is_captured_not_raised():
    verdict = v("m1", "request")
    client = FakeClient([RuntimeError("rate limited")])
    draft_reply(client, verdict, msg("m1", "a@x.com"), "body")
    assert verdict.draft is None
    assert "rate limited" in verdict.error


def test_triage_uses_cheap_model_and_draft_uses_strong_one():
    client = FakeClient([json.dumps([verdict_json("m1")])])
    classify(client, [msg("m1", "a@x.com")], known_addresses=set())
    assert client.calls[0]["model"] == triage.TRIAGE_MODEL

    c2 = FakeClient(["draft"])
    draft_reply(c2, v("m1", "request"), msg("m1", "a@x.com"), "body")
    assert c2.calls[0]["model"] == triage.DRAFT_MODEL
    assert triage.TRIAGE_MODEL != triage.DRAFT_MODEL


def test_read_state_reaches_the_model_as_a_signal():
    # Read mail is triaged now rather than filtered out, so the classifier has
    # to be told which messages Alex has already opened.
    m = msg("m1", "a@x.com")
    m["isRead"] = True
    client = FakeClient([json.dumps([verdict_json("m1")])])
    classify(client, [m], known_addresses=set())
    assert '"owner_has_read": true' in client.calls[0]["messages"][0]["content"]


def test_unread_mail_is_marked_unread_not_omitted():
    m = msg("m1", "a@x.com")  # no isRead key at all
    client = FakeClient([json.dumps([verdict_json("m1")])])
    classify(client, [m], known_addresses=set())
    assert '"owner_has_read": false' in client.calls[0]["messages"][0]["content"]


def test_focused_inbox_classification_reaches_the_model():
    m = msg("m1", "a@x.com")
    m["inferenceClassification"] = "other"
    client = FakeClient([json.dumps([verdict_json("m1")])])
    classify(client, [m], known_addresses=set())
    assert '"focused_inbox": "other"' in client.calls[0]["messages"][0]["content"]


def test_missing_focused_classification_is_unknown_not_a_guess():
    # Graph can omit the field. Absent must not be silently read as "other",
    # which would bias every such message towards no reply owed.
    client = FakeClient([json.dumps([verdict_json("m1")])])
    classify(client, [msg("m1", "a@x.com")], known_addresses=set())
    assert '"focused_inbox": "unknown"' in client.calls[0]["messages"][0]["content"]


def test_prompt_forbids_deciding_on_either_signal_alone():
    # Both fields are evidence. Either one used as a gate reintroduces the
    # blindness the fetch change was meant to remove.
    assert "Never set reply_owed false on this basis alone." in triage.TRIAGE_SYSTEM
    assert "never decide reply_owed on this field alone" in triage.TRIAGE_SYSTEM


def test_both_draft_prompts_ask_for_the_thanks_sign_off():
    """Alex signs off "Thanks, / Alex" - it is in the shared voice AND in each
    prompt, because the per-prompt closing instruction is the last thing the
    model reads and it wins when the two disagree."""
    from shared.triage import MEETING_DRAFT_SYSTEM, REQUEST_DRAFT_SYSTEM, VOICE

    assert '"Thanks," on its own line' in VOICE
    for prompt in (MEETING_DRAFT_SYSTEM, REQUEST_DRAFT_SYSTEM):
        assert 'End with "Thanks," on one line and "Alex" on the next.' in prompt
        assert 'End with just "Alex"' not in prompt


tests = [(k, val) for k, val in sorted(globals().items()) if k.startswith("test_")]
results = [run(n, f) for n, f in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
