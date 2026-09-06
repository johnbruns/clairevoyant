"""Tests for the two authentication questions, and the do-not-forward notice.

The header strings here are real ones, copied from the mailbox this runs
against, because the whole module is an exercise in reading what Microsoft
actually emits rather than what the documentation implies.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import mailauth, notice

# A message Alex sent to himself. Note dmarc=none: intra-organisation mail
# never leaves the tenant to be evaluated, so a DMARC-based gate would refuse
# every genuine reply he ever sends.
SELF_SENT = {
    "internetMessageHeaders": [
        {"name": "Authentication-Results",
         "value": "dkim=none (message not signed) header.d=none;"
                  "dmarc=none action=none header.from=example.org;"},
        {"name": "X-MS-Exchange-Organization-AuthAs", "value": "Internal"},
        {"name": "X-MS-Exchange-Organization-MessageDirectionality",
         "value": "Originating"},
    ]
}

# An ordinary external sender that passes everything.
GOOD_EXTERNAL = {
    "internetMessageHeaders": [
        {"name": "Authentication-Results",
         "value": "spf=pass (sender IP is 198.2.185.227) smtp.mailfrom=mail.example.com;"
                  " dkim=pass (signature was verified) header.d=example.com;"
                  " dmarc=pass action=none header.from=example.com;"
                  " compauth=pass reason=100"},
        {"name": "X-MS-Exchange-Organization-AuthAs", "value": "Anonymous"},
        {"name": "X-MS-Exchange-Organization-MessageDirectionality",
         "value": "Incoming"},
    ]
}


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


def with_results(value, auth_as="Anonymous"):
    return {"internetMessageHeaders": [
        {"name": "Authentication-Results", "value": value},
        {"name": "X-MS-Exchange-Organization-AuthAs", "value": auth_as},
    ]}


# ---- "did this come from inside?" ---------------------------------------

def test_self_sent_mail_is_internal():
    assert mailauth.is_internal(SELF_SENT)


def test_external_mail_is_not_internal():
    assert not mailauth.is_internal(GOOD_EXTERNAL)


def test_a_spoof_of_the_owners_address_is_still_not_internal():
    """The From address is not consulted, and that is the point.

    Exchange stamps AuthAs on receipt and strips any copy the sender supplied,
    so this header says where the message actually entered the system.
    """
    spoof = {"from": {"emailAddress": {"address": "alex@example.org"}},
             "internetMessageHeaders": [
                 {"name": "X-MS-Exchange-Organization-AuthAs", "value": "Anonymous"}]}
    assert not mailauth.is_internal(spoof)


def test_a_forged_internal_header_from_outside_cannot_be_distinguished_here():
    """Documenting the boundary: this module trusts Exchange, not the sender.

    If a message reaches the mailbox carrying AuthAs: Internal, it is because
    Exchange put it there. The protection is Exchange stripping inbound copies,
    not anything this code does - so there is nothing to test but the contract.
    """
    assert mailauth.is_internal(
        {"internetMessageHeaders": [
            {"name": "x-ms-exchange-organization-authas", "value": "internal"}]})


def test_no_headers_is_not_internal():
    assert not mailauth.is_internal({})
    assert not mailauth.is_internal({"internetMessageHeaders": []})


def test_directionality_alone_is_enough():
    assert mailauth.is_internal({"internetMessageHeaders": [
        {"name": "X-MS-Exchange-Organization-MessageDirectionality",
         "value": "Originating"}]})


# ---- "did the sender prove who they are?" -------------------------------

def test_a_fully_passing_sender_has_no_failure():
    assert mailauth.failure(GOOD_EXTERNAL) == ""


def test_dmarc_fail_is_a_failure():
    assert "DMARC fail" in mailauth.failure(
        with_results("spf=pass; dkim=fail; dmarc=fail action=oreject"))


def test_spf_softfail_is_a_failure():
    # softfail is "probably not authorised". On a junk rescue that is enough.
    assert "SPF softfail" in mailauth.failure(with_results("spf=softfail; dmarc=none"))


def test_composite_auth_failure_is_named_plainly():
    why = mailauth.failure(with_results("spf=none; dmarc=none; compauth=fail reason=001"))
    assert "spoofed" in why


def test_dmarc_none_is_not_a_failure():
    """A domain that published no policy is ordinary, not evidence of forgery.

    Treating `none` as failure would silently hide every small sender - which
    is most of the mail worth rescuing from a junk folder.
    """
    assert mailauth.failure(with_results("spf=pass; dkim=none; dmarc=none")) == ""


def test_dkim_none_with_spf_pass_is_not_a_failure():
    assert mailauth.failure(with_results("spf=pass; dkim=none; dmarc=pass")) == ""


def test_absent_results_are_treated_as_unproven():
    assert "left no authentication results" in mailauth.failure({})


def test_self_sent_internal_mail_reports_no_failure():
    # dmarc=none on intra-org mail must not read as a forgery.
    assert mailauth.failure(SELF_SENT) == ""


# ---- what gets shown to Alex --------------------------------------------

def test_the_summary_names_the_checks_without_reassuring():
    summary = mailauth.parse(GOOD_EXTERNAL).summary
    assert "SPF=pass" in summary and "DMARC=pass" in summary
    assert "safe" not in summary.lower()


def test_a_message_with_no_results_says_so():
    assert mailauth.parse({}).summary == "no authentication results"


def test_the_domain_is_the_part_worth_reading():
    # A lookalike domain is the whole trick, so it is lower-cased and shown
    # on its own line rather than left inside a display name.
    assert mailauth.domain("a.b@Mail.Example.COM") == "mail.example.com"
    assert mailauth.domain("") == ""
    assert mailauth.domain("no-at-sign") == ""


# ---- the do-not-forward notice ------------------------------------------

def test_the_notice_says_what_is_actually_at_stake():
    body = notice.html()
    assert "don't forward" in body
    assert "seven days" in body, "the window is the part that makes it concrete"


def test_the_marker_variant_is_about_the_reply_handle_not_buttons():
    body = notice.html(notice.MARKER)
    assert "subject line" in body
    assert "buttons" not in body


def test_the_notice_carries_its_own_style_hook():
    assert "noforward" in notice.html()
    assert ".noforward{" in notice.STYLE


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(name, fn) for name, fn in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
