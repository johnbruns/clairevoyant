"""Tests for the signed action tokens.

These are the only thing standing between an anonymous HTTP endpoint and the
ability to send mail as Alex, so the negative cases matter more than the happy
path and are written first.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared import actions
from shared.actions import ActionError, action_url, base_url, sign, signing_key, verify

KEY = b"k" * 48
OTHER_KEY = b"z" * 48


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


def expect_error(fn, fragment=""):
    try:
        fn()
    except ActionError as exc:
        assert fragment.lower() in str(exc).lower(), f"wrong message: {exc}"
        return
    raise AssertionError("expected ActionError")


def test_round_trip():
    payload = verify(sign("send", "abc123", KEY), KEY)
    assert payload["a"] == "send"
    assert payload["p"] == "abc123"


def test_every_action_round_trips():
    for action in actions.ACTIONS:
        assert verify(sign(action, "p1", KEY), KEY)["a"] == action


def test_a_token_signed_with_another_key_is_rejected():
    # The whole point. Without this the endpoint is a public send-as-Alex API.
    token = sign("send", "abc123", OTHER_KEY)
    expect_error(lambda: verify(token, KEY), "not valid")


def test_a_tampered_payload_is_rejected():
    # Flipping the action from discard to send must not survive the signature.
    token = sign("discard", "abc123", KEY)
    body, _, mac = token.partition(".")
    forged = sign("send", "abc123", OTHER_KEY).partition(".")[0]
    expect_error(lambda: verify(f"{forged}.{mac}", KEY), "not valid")


def test_an_unsigned_token_is_rejected():
    expect_error(lambda: verify("just-some-text", KEY), "malformed")
    expect_error(lambda: verify("", KEY), "malformed")


def test_garbage_after_a_valid_signature_marker_is_rejected():
    expect_error(lambda: verify("...", KEY), "not valid")


def test_an_expired_token_is_rejected():
    # A recap lives in a mailbox forever. A token that never expires is a live
    # send button in every archived copy of that email.
    token = sign("send", "abc123", KEY, ttl=60, now=time.time() - 3600)
    expect_error(lambda: verify(token, KEY), "expired")


def test_a_token_is_valid_right_up_to_its_expiry():
    now = time.time()
    token = sign("send", "abc123", KEY, ttl=60, now=now)
    assert verify(token, KEY, now=now + 59)


def test_default_lifetime_is_a_week():
    assert actions.DEFAULT_TTL_SECONDS == 7 * 24 * 3600


def test_an_unknown_action_cannot_be_signed():
    expect_error(lambda: sign("delete_everything", "p1", KEY), "unknown action")


def test_an_unknown_action_in_a_valid_signature_is_still_rejected():
    # Belt and braces: even holding the key, "forward_all" is not a thing the
    # endpoint will dispatch on.
    import base64, hmac, hashlib, json

    body = base64.urlsafe_b64encode(
        json.dumps({"v": 1, "a": "forward_all", "p": "x", "e": time.time() + 99}).encode()
    ).decode().rstrip("=")
    mac = base64.urlsafe_b64encode(
        hmac.new(KEY, body.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    expect_error(lambda: verify(f"{body}.{mac}", KEY), "does not do")


def test_a_token_naming_no_draft_is_rejected():
    import base64, hmac, hashlib, json

    body = base64.urlsafe_b64encode(
        json.dumps({"v": 1, "a": "send", "p": "", "e": time.time() + 99}).encode()
    ).decode().rstrip("=")
    mac = base64.urlsafe_b64encode(
        hmac.new(KEY, body.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    expect_error(lambda: verify(f"{body}.{mac}", KEY), "does not name")


def test_tokens_are_url_safe():
    # These go in a query string and through Safe Links rewriting.
    token = sign("send", "abc123", KEY)
    assert all(c.isalnum() or c in "-_." for c in token), token


def test_two_drafts_get_different_tokens():
    assert sign("send", "p1", KEY) != sign("send", "p2", KEY)


def test_send_and_discard_tokens_differ_for_the_same_draft():
    assert sign("send", "p1", KEY) != sign("discard", "p1", KEY)


# ---- key handling --------------------------------------------------------


def test_missing_key_disables_buttons_rather_than_crashing():
    os.environ.pop("ACTION_SIGNING_KEY", None)
    assert signing_key() is None


def test_a_short_key_is_refused():
    # A placeholder like "changeme" must not be used to sign anything.
    os.environ["ACTION_SIGNING_KEY"] = "changeme"
    try:
        assert signing_key() is None
    finally:
        del os.environ["ACTION_SIGNING_KEY"]


def test_a_real_key_is_accepted():
    os.environ["ACTION_SIGNING_KEY"] = "x" * 44
    try:
        assert signing_key() == b"x" * 44
    finally:
        del os.environ["ACTION_SIGNING_KEY"]


# ---- url building --------------------------------------------------------


def test_base_url_comes_from_the_azure_hostname():
    os.environ.pop("ACTION_BASE_URL", None)
    os.environ["WEBSITE_HOSTNAME"] = "<your-function-app>.azurewebsites.net"
    try:
        assert base_url() == "https://<your-function-app>.azurewebsites.net/api"
    finally:
        del os.environ["WEBSITE_HOSTNAME"]


def test_explicit_base_url_wins():
    os.environ["WEBSITE_HOSTNAME"] = "ignored.example.com"
    os.environ["ACTION_BASE_URL"] = "https://custom.example.org/api/"
    try:
        assert base_url() == "https://custom.example.org/api"
    finally:
        del os.environ["WEBSITE_HOSTNAME"]
        del os.environ["ACTION_BASE_URL"]


def test_no_hostname_yields_no_url_not_a_broken_one():
    # A relative or half-built link in an email is worse than no button.
    os.environ.pop("WEBSITE_HOSTNAME", None)
    os.environ.pop("ACTION_BASE_URL", None)
    assert base_url() == ""
    assert action_url("send", "p1", KEY) == ""


def test_the_url_route_matches_the_action():
    """The regression guard for a genuinely nasty bug.

    action_url used to hardcode /draft for every action. An agenda button
    therefore carried a VALID signed token to the DRAFT endpoint, which looked
    it up as a pending draft, failed, and told Alex "that draft is no longer
    available" - a false statement about a feature that worked, with a link
    that looked correct and a signature that verified.
    """
    from shared.actions import route_for

    assert route_for("send") == "draft"
    assert route_for("edit") == "draft"
    assert route_for("discard") == "draft"
    assert route_for("task_done") == "agenda"
    assert route_for("block_time") == "agenda"


def test_every_action_routes_somewhere_real():
    from shared.actions import route_for

    for action in actions.ACTIONS:
        assert route_for(action) in ("draft", "agenda", "event", "junk"), action


def test_agenda_buttons_point_at_the_agenda_endpoint():
    for action in ("task_done", "block_time"):
        url = action_url(action, "TASK-1", KEY, base="https://x.example/api")
        assert url.startswith("https://x.example/api/agenda?t="), url


def test_recap_buttons_still_point_at_the_draft_endpoint():
    for action in ("send", "edit", "discard"):
        url = action_url(action, "p1", KEY, base="https://x.example/api")
        assert url.startswith("https://x.example/api/draft?t="), url


def test_action_url_shape():
    url = action_url("send", "p1", KEY, base="https://x.example/api")
    assert url.startswith("https://x.example/api/draft?t=")
    assert verify(url.split("t=", 1)[1], KEY)["p"] == "p1"


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(name, fn) for name, fn in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
