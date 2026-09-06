import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.availability import Slot
from shared.planning import DayPlan, Pick
from shared.agendamail import build_agenda
from shared.recap import agenda_free_blocks, build_recap, is_actionable
from shared.triage import Verdict

TZ = ZoneInfo("America/New_York")
NOW = dt.datetime(2026, 9, 2, 12, 0, tzinfo=TZ)


def msg(mid, name, address, subject):
    return {
        "id": mid,
        "from": {"emailAddress": {"name": name, "address": address}},
        "subject": subject,
    }


def v(mid, **over):
    base = dict(
        message_id=mid, sender_type="human", reply_owed=True, is_meeting_request=False,
        request_type="request", urgency="today", category="question",
        reasoning="Asked a direct question.",
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


def test_a_card_leads_with_sender_subject_ask_and_draft():
    verdicts = [v("m1", draft="Tuesday works.")]
    msgs = {"m1": msg("m1", "Priya Nair", "priya@x.org", "Capstone timing")}
    subject, body = build_recap(verdicts, msgs, NOW)

    assert subject == "Inbox recap - 1 ready to send", subject
    assert "Needs Something" in body
    assert "<b>Sender:</b> Priya Nair" in body
    assert "priya@x.org" in body
    assert "<b>Subject:</b> Capstone timing" in body
    assert "Tuesday works." in body


def test_a_card_shows_the_ask_and_not_the_assistants_reasoning():
    """Alex wants the sender's words, not the assistant's justification."""
    verdicts = [v("m1", draft="ok")]
    _, body = build_recap(verdicts, {"m1": msg("m1", "A", "a@b.c", "s")}, NOW)

    assert "Asked a direct question." not in body, "reasoning must not be shown"
    assert verdicts[0].the_ask in body, "the sender's actual ask must be shown"


def test_the_chip_line_is_gone():
    # "asking you for something - review - today - known sender" was noise
    # sitting between the subject and the thing Alex actually reads.
    verdicts = [v("m1", request_type="request", draft="ok")]
    _, body = build_recap(verdicts, {"m1": msg("m1", "A", "a@b.c", "s")}, NOW)
    assert "known sender" not in body
    assert "asking you for something" not in body


def test_a_message_that_asks_for_nothing_does_not_claim_otherwise():
    # "Needs Something" would be a lie on a heads-up, and a card that
    # overstates the ask is one you learn to skim.
    verdicts = [v("m1", request_type="other")]
    _, body = build_recap(verdicts, {"m1": msg("m1", "A", "a@b.c", "s")}, NOW)
    assert "Reply Owed" in body
    assert "Needs Something" not in body


def test_recap_escapes_html_from_untrusted_senders():
    # Sender names and subjects are attacker-controlled and land in an HTML email.
    verdicts = [v("m1", draft="ok")]
    msgs = {"m1": msg("m1", "<script>alert(1)</script>", "x@y.com", "<img src=x onerror=1>")}
    _, body = build_recap(verdicts, msgs, NOW)

    assert "<script>" not in body, "sender name was not escaped"
    assert "&lt;script&gt;" in body
    assert "onerror=1>" not in body


def test_recap_escapes_model_produced_draft():
    verdicts = [v("m1", draft="<b>not bold</b>")]
    msgs = {"m1": msg("m1", "A", "a@b.c", "s")}
    _, body = build_recap(verdicts, msgs, NOW)
    assert "<b>not bold</b>" not in body
    assert "&lt;b&gt;not bold&lt;/b&gt;" in body


def test_errors_are_surfaced_in_subject_and_body():
    verdicts = [v("m1", reply_owed=False, request_type="none",
                  category="triage-error", error="api exploded")]
    msgs = {"m1": msg("m1", "A", "a@b.c", "Subject here")}
    subject, body = build_recap(verdicts, msgs, NOW)

    assert "unclassified" in subject, subject
    assert "Could not classify" in body
    assert "api exploded" in body
    assert "Subject here" in body


def test_suspicious_shown_without_draft():
    verdicts = [v("m1", reply_owed=False, request_type="none", category="suspicious",
                  reasoning="Contained instructions aimed at an assistant.")]
    msgs = {"m1": msg("m1", "Attacker", "a@evil.com", "URGENT")}
    _, body = build_recap(verdicts, msgs, NOW)

    assert "Flagged as suspicious" in body
    assert "instructions aimed at an assistant" in body
    assert "class='draft'" not in body


# ---- when the recap is sent at all --------------------------------------


def test_a_quiet_run_is_not_actionable():
    # The whole point of the change: no more "Inbox recap - 0 need a reply".
    verdicts = [
        v("m1", reply_owed=False, request_type="none", category="newsletter"),
        v("m2", reply_owed=False, request_type="none", sender_type="automated"),
    ]
    assert not is_actionable(verdicts)


def test_an_empty_run_is_not_actionable():
    assert not is_actionable([])


def test_a_reply_owed_is_actionable_even_without_a_draft():
    # "other" gets no draft, but it is still Alex's to answer, so he must hear
    # about it. Gating the email on drafts would hide exactly these.
    assert is_actionable([v("m1", request_type="other")])


def test_a_classification_error_is_actionable():
    # The assistant must not go quiet about its own failures - the message it
    # could not read might have been the one that mattered.
    assert is_actionable([v("m1", reply_owed=False, request_type="none",
                            category="triage-error", error="boom")])


def test_suspicious_alone_is_quiet_by_default_and_loud_on_request():
    verdicts = [v("m1", reply_owed=False, request_type="none", category="suspicious")]
    assert not is_actionable(verdicts)
    assert is_actionable(verdicts, suspicious_counts=True)


def test_recap_still_renders_when_errors_are_the_only_reason_to_send():
    verdicts = [v("m1", reply_owed=False, request_type="none",
                  category="triage-error", error="boom")]
    subject, body = build_recap(verdicts, {"m1": msg("m1", "A", "a@b.c", "s")}, NOW)
    assert subject == "Inbox recap, 1 unclassified", subject
    assert "Nothing waiting on you." in body


def test_undrafted_replies_say_why_there_is_no_draft():
    verdicts = [v("m1", request_type="other")]
    _, body = build_recap(verdicts, {"m1": msg("m1", "A", "a@b.c", "s")}, NOW)
    assert "yours to answer" in body, body


def test_a_meeting_ask_leads_with_needs_something_too():
    verdicts = [v("m1", request_type="meeting", is_meeting_request=True, draft="x")]
    _, body = build_recap(verdicts, {"m1": msg("m1", "A", "a@b.c", "s")}, NOW)
    assert "Needs Something" in body


def test_drafted_replies_are_listed_before_undrafted_ones():
    verdicts = [v("m1", request_type="other"), v("m2", draft="ready to send")]
    msgs = {"m1": msg("m1", "Undrafted", "u@x.com", "s1"),
            "m2": msg("m2", "Drafted", "d@x.com", "s2")}
    _, body = build_recap(verdicts, msgs, NOW)
    assert body.index("Drafted") < body.index("Undrafted"), "one-click items should be first"


# ---- agenda --------------------------------------------------------------


def test_contiguous_slots_collapse():
    def s(h1, m1, h2, m2):
        return Slot(dt.datetime(2026, 9, 3, h1, m1, tzinfo=TZ),
                    dt.datetime(2026, 9, 3, h2, m2, tzinfo=TZ))

    blocks = agenda_free_blocks(
        [s(9, 0, 9, 30), s(9, 30, 10, 0), s(14, 0, 14, 30)], dt.date(2026, 9, 3)
    )
    assert blocks == ["9-10am", "2-2:30pm"], blocks


def test_agenda_lists_events_and_open_time():
    events = [{
        "subject": "Board sync",
        "start": {"dateTime": "2026-09-03T10:00:00.0000000"},
        "end": {"dateTime": "2026-09-03T11:00:00.0000000"},
        "showAs": "busy",
    }]
    free = [Slot(dt.datetime(2026, 9, 3, 14, 0, tzinfo=TZ),
                 dt.datetime(2026, 9, 3, 14, 30, tzinfo=TZ))]
    subject, body = build_agenda(events, free, dt.date(2026, 9, 3), is_today=False)

    assert subject == "Tomorrow's Agenda: 09/03/2026", subject
    assert "Board sync" in body
    assert "10:00 AM" in body           # the meeting keeps its full label
    assert "2\u20132:30 PM" in body      # the free chip uses the compact one


def test_today_and_tomorrow_agendas_are_distinguishable():
    # Both land in the same inbox, sixteen hours apart. If they read the same,
    # the 6am one gets mistaken for yesterday's and archived.
    today_subject, today_body = build_agenda([], [], dt.date(2026, 9, 3), is_today=True)
    tomorrow_subject, tomorrow_body = build_agenda([], [], dt.date(2026, 9, 3), is_today=False)


    assert today_subject == "Your Daily Agenda: 09/03/2026"
    assert tomorrow_subject == "Tomorrow's Agenda: 09/03/2026"
    assert "here's your daily agenda" in today_body
    assert "here's your agenda for tomorrow" in tomorrow_body


def test_agenda_escapes_event_subjects():
    events = [{
        "subject": "<script>x</script>",
        "start": {"dateTime": "2026-09-03T10:00:00"},
        "end": {"dateTime": "2026-09-03T11:00:00"},
        "showAs": "busy",
    }]
    _, body = build_agenda(events, [], dt.date(2026, 9, 3))
    assert "<script>x</script>" not in body


def test_agenda_empty_day():
    _, body = build_agenda([], [], dt.date(2026, 9, 3))
    assert "Nothing on the calendar." in body


def test_agenda_omits_cancelled_events():
    events = [{
        "subject": "Called off",
        "start": {"dateTime": "2026-09-03T10:00:00"},
        "end": {"dateTime": "2026-09-03T11:00:00"},
        "isCancelled": True,
    }]
    _, body = build_agenda(events, [], dt.date(2026, 9, 3))
    assert "Called off" not in body
    assert "Nothing on the calendar." in body


# ---- the Jira section ----------------------------------------------------


def test_agenda_shows_jira_recommendations():
    plan = DayPlan(
        focus="Four meetings and one clear afternoon; use it on the grant draft.",
        picks=[Pick("TASK-14", "Draft the SOC2 gap letter", "Due today and it blocks Priya.",
                    "https://crc.atlassian.net/browse/TASK-14")],
    )
    _, body = build_agenda([], [], dt.date(2026, 9, 3), plan=plan)

    assert "Your task dashboard" in body
    assert "use it on the grant draft" in body
    assert "TASK-14" in body
    assert "Draft the SOC2 gap letter" in body
    assert "Due today and it blocks Priya." in body
    assert "href='https://crc.atlassian.net/browse/TASK-14'" in body


def test_agenda_omits_the_section_entirely_when_jira_is_not_configured():
    _, body = build_agenda([], [], dt.date(2026, 9, 3), plan=None)
    assert "What to get done" not in body


def test_a_jira_failure_is_stated_not_hidden():
    # A silently missing section looks identical to an empty backlog, and the
    # difference matters: one means "nothing to do", the other means "broken".
    plan = DayPlan(note="Could not read the TASK board: Jira returned 401")
    _, body = build_agenda([], [], dt.date(2026, 9, 3), plan=plan)
    assert "Could not read the TASK board" in body


def test_jira_content_is_escaped():
    # Issue summaries are written by anyone with board access.
    plan = DayPlan(focus="x", picks=[Pick("TASK-1", "<script>alert(1)</script>", "why")])
    _, body = build_agenda([], [], dt.date(2026, 9, 3), plan=plan)
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


# ---- drafts --------------------------------------------------------------


def test_saved_draft_shows_outlook_link_and_subject():
    # Outlook mirroring is opt-in now, but when it is on the link still shows.
    verdicts = [v("m1", draft="Tuesday works.", draft_saved=True,
                  draft_link="https://outlook.office.com/mail/id/AAA")]
    msgs = {"m1": msg("m1", "Priya", "priya@x.org", "Timing")}
    subject, body = build_recap(verdicts, msgs, NOW)

    assert subject == "Inbox recap - 1 ready to send", subject
    assert "Saved to your Drafts folder" in body
    assert "https://outlook.office.com/mail/id/AAA" in body
    assert "Open in Outlook" in body
    assert "Tuesday works." in body, "text kept as a fallback"


def test_a_draft_with_no_buttons_says_so_rather_than_looking_normal():
    # No signing key means no buttons. Silently rendering a normal-looking card
    # would leave Alex waiting for buttons that are never coming.
    verdicts = [v("m1", draft="Tuesday works.", draft_saved=False)]
    msgs = {"m1": msg("m1", "Priya", "priya@x.org", "Timing")}
    subject, body = build_recap(verdicts, msgs, NOW)

    assert subject == "Inbox recap - 1 ready to send", subject
    assert "Buttons unavailable" in body
    assert "ACTION_SIGNING_KEY" in body
    assert "Tuesday works." in body


# ---- the action buttons --------------------------------------------------


LINKS = {
    "send": "https://x.example/api/draft?t=SEND",
    "edit": "https://x.example/api/draft?t=EDIT",
    "discard": "https://x.example/api/draft?t=DISCARD",
}


def test_all_three_buttons_render_with_their_links():
    verdicts = [v("m1", draft="Tuesday works.")]
    msgs = {"m1": msg("m1", "Priya", "priya@x.org", "Timing")}
    _, body = build_recap(verdicts, msgs, NOW, action_links={"m1": LINKS})

    assert "Approve and send" in body
    assert "Edit" in body
    assert "Do not send - delete" in body
    for url in LINKS.values():
        assert url in body, url


def test_buttons_are_table_based_for_outlook():
    # Outlook on Windows renders through Word, which drops padding and
    # background on a styled <a>. A table with bgcolor is the only shape that
    # survives, and this is the one client Alex actually uses.
    verdicts = [v("m1", draft="x")]
    _, body = build_recap(verdicts, {"m1": msg("m1", "R", "r@x.com", "s")}, NOW,
                          action_links={"m1": LINKS})
    assert "<table role='presentation'" in body
    assert "bgcolor=" in body


def test_buttons_only_attach_to_their_own_message():
    verdicts = [v("m1", draft="a"), v("m2", draft="b")]
    msgs = {"m1": msg("m1", "A", "a@x.com", "s1"), "m2": msg("m2", "B", "b@x.com", "s2")}
    _, body = build_recap(verdicts, msgs, NOW, action_links={"m1": LINKS})
    assert body.count("Approve and send") == 1, "buttons leaked onto the wrong message"


def test_a_message_with_no_draft_gets_no_buttons():
    verdicts = [v("m1", request_type="other")]
    _, body = build_recap(verdicts, {"m1": msg("m1", "A", "a@x.com", "s")}, NOW,
                          action_links={"m1": LINKS})
    assert "Approve and send" not in body


def test_button_urls_are_attribute_escaped():
    hostile = dict(LINKS, send="https://x.example/a'onmouseover='alert(1)")
    verdicts = [v("m1", draft="x")]
    _, body = build_recap(verdicts, {"m1": msg("m1", "A", "a@x.com", "s")}, NOW,
                          action_links={"m1": hostile})
    assert "onmouseover='alert(1)'" not in body
    assert "&#x27;" in body or "&apos;" in body


def test_the_ask_is_shown_next_to_the_draft():
    # The point of the whole card: approve without opening the original.
    verdicts = [v("m1", draft="Glad to meet.",
                  the_ask="Wants 30 minutes next week about the capstone timeline.")]
    _, body = build_recap(verdicts, {"m1": msg("m1", "Priya", "priya@x.org", "Timing")}, NOW,
                          action_links={"m1": LINKS})

    assert "What they asked" in body
    assert "Wants 30 minutes next week about the capstone timeline." in body


def test_the_ask_is_escaped():
    # It is a model summary of attacker-controlled text.
    verdicts = [v("m1", draft="x", the_ask="<img src=x onerror=1>")]
    _, body = build_recap(verdicts, {"m1": msg("m1", "A", "a@x.com", "s")}, NOW)
    assert "<img src=x" not in body
    assert "&lt;img" in body


def test_an_empty_ask_leaves_no_empty_heading():
    verdicts = [v("m1", draft="x", the_ask="")]
    _, body = build_recap(verdicts, {"m1": msg("m1", "A", "a@x.com", "s")}, NOW)
    assert "What they asked" not in body


def test_mixed_saved_and_unsaved_in_subject():
    verdicts = [
        v("m1", draft="a", draft_saved=True),
        v("m2", request_type="other"),
    ]
    msgs = {"m1": msg("m1", "A", "a@x.com", "s1"), "m2": msg("m2", "B", "b@x.com", "s2")}
    subject, _ = build_recap(verdicts, msgs, NOW)
    assert subject == "Inbox recap - 1 ready to send, 1 to answer yourself", subject


def test_draft_link_is_attribute_escaped():
    verdicts = [v("m1", draft="x", draft_saved=True,
                  draft_link='https://o.com/a"onmouseover="alert(1)')]
    msgs = {"m1": msg("m1", "A", "a@x.com", "s")}
    _, body = build_recap(verdicts, msgs, NOW)
    assert 'onmouseover="alert(1)"' not in body
    assert "&quot;" in body


def test_draft_to_html_escapes_and_keeps_breaks():
    from shared.triage import draft_to_html
    out = draft_to_html("Hi <b>Priya</b>,\nTuesday works.\n\nAlex")
    assert "<b>Priya</b>" not in out
    assert "&lt;b&gt;Priya&lt;/b&gt;" in out
    assert "Hi &lt;b&gt;Priya&lt;/b&gt;,<br>Tuesday works." in out
    assert out.count("<p>") == 2, out
    assert draft_to_html("") == "<p></p>"


# ---- travel detection ----------------------------------------------------


def _ev(subject, location="", online=False, start="2026-09-03T15:30:00",
        end="2026-09-03T16:00:00"):
    loc = ("https://us06web.zoom.us/j/123?pwd=abc" if online else location)
    return {"subject": subject, "showAs": "busy",
            "start": {"dateTime": start}, "end": {"dateTime": end},
            "location": {"displayName": loc}, "attendees": [], "webLink": "https://x"}


def test_a_drive_event_is_flagged_red_with_a_car():
    _, body = build_agenda([_ev("Drive to Centennial")], [], dt.date(2026, 9, 3))
    assert "ev drive" in body
    assert "&#128663;" in body, "car icon missing"
    assert "needs travel today" in body


def test_an_in_person_meeting_with_an_address_is_flagged():
    _, body = build_agenda([_ev("Board meeting", location="120 Main St, Baltimore")],
                           [], dt.date(2026, 9, 3))
    assert "ev drive" in body
    assert "In person" in body


def test_a_venue_in_the_title_is_flagged():
    # "Lanny Volleyball @ Centennial" - no location set, venue only in the name.
    _, body = build_agenda([_ev("Lanny Volleyball @ Centennial")], [], dt.date(2026, 9, 3))
    assert "ev drive" in body


def test_a_zoom_meeting_is_never_travel():
    # THE false positive that matters: almost every meeting has a Zoom link in
    # `location`, and flagging them all red would make the flag meaningless.
    _, body = build_agenda([_ev("Demo update", online=True)], [], dt.date(2026, 9, 3))
    assert "ev drive" not in body
    assert "needs travel" not in body


def test_a_zoom_link_beats_a_travel_sounding_title():
    _, body = build_agenda([_ev("Drive to Centennial", online=True)], [], dt.date(2026, 9, 3))
    assert "ev drive" not in body, "an online meeting is not a commute"


def test_an_event_with_no_location_is_not_travel():
    _, body = build_agenda([_ev("Focus time")], [], dt.date(2026, 9, 3))
    assert "ev drive" not in body


def test_the_travel_banner_counts_and_names_them():
    events = [_ev("Drive to Centennial", start="2026-09-03T15:30:00",
                  end="2026-09-03T16:00:00"),
              _ev("Drive home", start="2026-09-03T18:00:00", end="2026-09-03T18:30:00")]
    _, body = build_agenda(events, [], dt.date(2026, 9, 3))
    assert "2 events need travel today" in body
    assert "Drive to Centennial at 3:30 PM" in body
    assert "Drive home at 6:00 PM" in body


# ---- free-time chips ------------------------------------------------------


def _slots(*pairs):
    return [Slot(dt.datetime(2026, 9, 3, a, b, tzinfo=TZ),
                 dt.datetime(2026, 9, 3, c, d, tzinfo=TZ)) for a, b, c, d in pairs]


def test_free_blocks_render_as_a_side_by_side_grid():
    free = _slots((9, 0, 9, 30), (11, 0, 12, 0), (14, 0, 14, 30))
    _, body = build_agenda([], free, dt.date(2026, 9, 3))
    assert "<table class='fgrid'" in body
    grid = body.split("class='fgrid'")[1].split("</table>")[0]
    assert grid.count("<tr>") == 1, "three chips fit one row of six"
    assert body.count("class='fcell'") == 6, "3 chips + 3 pad cells"
    assert "9\u20139:30 AM" in body


def test_more_than_a_row_of_chips_wraps_explicitly():
    # Word does not wrap inline-blocks, so the chunking must be in the markup
    # or the last chips run off the edge and become unreachable.
    free = _slots((9, 0, 9, 30), (10, 0, 10, 30), (11, 0, 11, 30),
                  (13, 0, 13, 30), (14, 0, 14, 30), (15, 0, 15, 30),
                  (15, 45, 16, 0))
    _, body = build_agenda([], free, dt.date(2026, 9, 3))
    grid = body.split("class='fgrid'")[1].split("</table>")[0]
    assert grid.count("<tr>") == 2, "seven chips must span two rows"
    assert body.count("class='fcell'") == 12, "7 chips + 5 pad cells"


def test_a_chip_is_one_link_not_a_separate_block_line():
    free = _slots((9, 0, 9, 30))
    links = {(free[0].start, free[0].end): "https://x.example/api/agenda?t=T"}
    _, body = build_agenda([], free, dt.date(2026, 9, 3), block_links=links)
    assert "class='fchip' href='https://x.example/api/agenda?t=T'" in body
    assert "Block this time" not in body, "the long CTA line is what made it huge"
    assert "+ Block" in body or "&#43; Block" in body


def test_chips_without_links_still_show_the_time():
    free = _slots((9, 0, 9, 30))
    _, body = build_agenda([], free, dt.date(2026, 9, 3), block_links={})
    assert "9\u20139:30 AM" in body
    assert "href" not in body.split("fgrid")[1].split("</table>")[0]


def test_a_compact_chip_label_is_still_unambiguous():
    """Six across leaves ~90px a chip, so the label is shortened, not guessed at.

    Whole hours lose ":00" and a span inside one half of the day states AM/PM
    once. A span that CROSSES noon must keep both, or "11-1 PM" reads as an
    hour that does not exist.
    """
    from shared.agendamail import _span_short

    def span(sh, sm, eh, em):
        return _span_short(dt.datetime(2026, 9, 3, sh, sm),
                           dt.datetime(2026, 9, 3, eh, em))

    assert span(9, 0, 9, 30) == "9\u20139:30 AM"
    assert span(13, 30, 14, 30) == "1:30\u20132:30 PM"
    assert span(11, 30, 12, 30) == "11:30 AM\u201312:30 PM"
    assert span(11, 0, 13, 0) == "11 AM\u20131 PM"


def test_meetings_keep_the_full_time_label():
    # Only the chips are compressed. A meeting has the width for "10:00 AM".
    events = [{
        "subject": "Board sync",
        "start": {"dateTime": "2026-09-03T10:00:00.0000000"},
        "end": {"dateTime": "2026-09-03T11:00:00.0000000"},
        "showAs": "busy",
    }]
    _, body = build_agenda(events, [], dt.date(2026, 9, 3))
    assert "10:00 AM" in body and "11:00 AM" in body


# ---- the greeting header and the review button ---------------------------

def test_the_section_header_greets_alex_by_name():
    _, body = build_recap([v("m1", draft="ok")], {"m1": msg("m1", "A", "a@b.c", "s")}, NOW)
    assert "Hi Alex, I think these emails need a reply!" in body
    assert "Needs a reply (" not in body


def test_a_cloud_attachment_gets_a_button_straight_to_the_file():
    verdict = v("m1", draft="ok")
    verdict.attachment_url = "https://crc.sharepoint.com/x/MOU.docx"
    verdict.attachment_direct = True
    _, body = build_recap([verdict], {"m1": msg("m1", "A", "a@b.c", "s")}, NOW)

    assert ">Review Attachment<" in body
    assert "Review Attachment in Email" not in body
    assert "https://crc.sharepoint.com/x/MOU.docx" in body


def test_a_file_attachment_falls_back_to_opening_the_email():
    # Bytes inside a message have no address of their own, so the button says
    # what it will actually do rather than promising the file.
    verdict = v("m1", draft="ok")
    verdict.attachment_url = "https://outlook.office.com/mail/id/AAA"
    verdict.attachment_direct = False
    _, body = build_recap([verdict], {"m1": msg("m1", "A", "a@b.c", "s")}, NOW)

    assert "Review Attachment in Email" in body
    assert "https://outlook.office.com/mail/id/AAA" in body


def test_no_attachment_means_no_fourth_button():
    _, body = build_recap([v("m1", draft="ok")], {"m1": msg("m1", "A", "a@b.c", "s")}, NOW)
    assert "Review Attachment" not in body


def test_the_review_button_sits_after_the_delete_button():
    verdict = v("m1", draft="ok")
    verdict.attachment_url = "https://x/y"
    verdict.attachment_direct = True
    links = {"send": "https://s", "edit": "https://e", "discard": "https://d"}
    _, body = build_recap([verdict], {"m1": msg("m1", "A", "a@b.c", "s")}, NOW,
                          action_links={"m1": links})
    assert body.index("Do not send") < body.index("Review Attachment")


def test_an_attachment_url_is_attribute_escaped():
    verdict = v("m1", draft="ok")
    verdict.attachment_url = "https://x/y?a=1&b='2'"
    verdict.attachment_direct = True
    _, body = build_recap([verdict], {"m1": msg("m1", "A", "a@b.c", "s")}, NOW)
    assert "&amp;b=" in body
    assert "b='2'" not in body


tests = [(k, val) for k, val in sorted(globals().items()) if k.startswith("test_")]
results = [run(n, f) for n, f in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
