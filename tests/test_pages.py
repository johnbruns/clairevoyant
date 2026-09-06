"""Tests for the confirmation pages.

Every one of these renders sender-supplied text - a name, a subject, a model
summary of an attacker's email - into HTML that Alex opens in a browser. The
escaping tests are the ones that matter.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import pages
from shared.pending import SENT, PendingDraft

TOKEN = "abc.def"


def draft(**over):
    fields = dict(
        message_id="AAMk-1",
        subject="Capstone timing",
        sender_name="Priya Nair",
        sender_address="priya@x.org",
        the_ask="Wants 30 minutes next week about the capstone timeline.",
        request_type="meeting",
        body="Glad to meet.\n\nAlex",
    )
    fields.update(over)
    return PendingDraft.new(**fields)


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


def test_send_page_shows_who_what_and_the_draft():
    html = pages.confirm_send(draft(), TOKEN)
    assert "Priya Nair" in html
    assert "priya@x.org" in html
    assert "Capstone timing" in html
    assert "Wants 30 minutes next week" in html
    assert "Glad to meet." in html
    assert "Send it now" in html


def test_send_page_says_nothing_has_been_sent_yet():
    # Alex arrives here from a link he may have clicked by accident.
    assert "Nothing has been sent yet" in pages.confirm_send(draft(), TOKEN)


def test_every_page_posts_rather_than_links_the_action():
    # A GET that acts would be followed by every link scanner in the path.
    for html in (
        pages.confirm_send(draft(), TOKEN),
        pages.confirm_edit(draft(), TOKEN),
        pages.confirm_discard(draft(), TOKEN),
    ):
        assert "<form method='post'>" in html
        assert "<button" in html


def test_edit_page_puts_the_draft_in_a_textarea():
    html = pages.confirm_edit(draft(), TOKEN)
    assert "<textarea name='body'>" in html
    assert "Glad to meet." in html
    assert "Save, don&#x27;t send" in html or "Save, don't send" in html


def test_discard_page_reassures_about_the_original():
    html = pages.confirm_discard(draft(), TOKEN)
    assert "Discard it" in html
    assert "original message stays in your inbox" in html


def test_the_token_travels_in_a_hidden_field():
    html = pages.confirm_send(draft(), TOKEN)
    assert f"value='{TOKEN}'" in html
    assert f"href='{TOKEN}" not in html, "token must not be in a link"


# ---- escaping ------------------------------------------------------------


def test_sender_name_is_escaped():
    html = pages.confirm_send(draft(sender_name="<script>alert(1)</script>"), TOKEN)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_subject_and_ask_are_escaped():
    html = pages.confirm_send(
        draft(subject="<img src=x onerror=1>", the_ask="<b>bold</b>"), TOKEN
    )
    assert "<img src=x" not in html
    assert "<b>bold</b>" not in html


def test_draft_body_is_escaped_in_the_textarea():
    # A draft containing </textarea> would otherwise break out of the field.
    html = pages.confirm_edit(draft(body="</textarea><script>alert(1)</script>"), TOKEN)
    assert "</textarea><script>" not in html
    assert "&lt;/textarea&gt;" in html


def test_token_is_attribute_escaped():
    html = pages.confirm_send(draft(), "abc'onmouseover='alert(1)")
    assert "onmouseover='alert(1)'" not in html


# ---- result pages --------------------------------------------------------


def test_result_page_reads_cleanly():
    html = pages.result("Sent to Priya Nair.", "Capstone timing")
    assert "Sent to Priya Nair." in html
    assert "Capstone timing" in html
    assert "close this tab" in html


def test_already_resolved_is_informative_not_an_error():
    # Usual cause is a double click. Telling Alex off for it is unhelpful.
    html = pages.already_resolved(draft(status=SENT))
    assert "already been sent" in html
    assert "Nothing changed just now" in html


def test_gone_page_points_at_the_inbox():
    html = pages.gone()
    assert "no longer available" in html
    assert "still in your inbox" in html


def test_every_page_is_a_complete_mobile_ready_document():
    # These open in an email client's in-app browser as often as a desktop one.
    for html in (
        pages.confirm_send(draft(), TOKEN),
        pages.confirm_edit(draft(), TOKEN),
        pages.confirm_discard(draft(), TOKEN),
        pages.result("ok"),
        pages.gone(),
        pages.error("nope"),
    ):
        assert html.startswith("<!doctype html>")
        assert "width=device-width" in html
        assert "no-referrer" in html
        assert "</html>" in html


def test_no_external_assets_are_referenced():
    # A page that needs a CDN is a page that sometimes does not render.
    html = pages.confirm_send(draft(), TOKEN)
    assert "http://" not in html
    assert "<script" not in html


def test_a_draft_with_no_ask_still_renders():
    html = pages.confirm_send(draft(the_ask=""), TOKEN)
    assert "What they asked" not in html
    assert "Send it now" in html


def test_an_unknown_sender_does_not_render_as_none():
    html = pages.confirm_send(draft(sender_name="", sender_address=""), TOKEN)
    assert "None" not in html
    assert "unknown sender" in html


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(name, fn) for name, fn in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
