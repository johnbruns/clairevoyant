"""Tests for keeping the contacts directory current.

The rule these enforce is the whole point: a contact is somebody Alex WROTE
TO, not somebody who wrote to him. Getting that backwards is how a 136-entry
directory ended up missing his own team.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.contacts import add_person, derive_name, is_person, split_name


class FakeGraph:
    def __init__(self, fail=False):
        self.created = []
        self._fail = fail

    def create_contact(self, display_name, email=None, emails=None,
                       given_name=None, surname=None):
        if self._fail:
            raise RuntimeError("graph said no")
        self.created.append({"display": display_name, "emails": emails or [email],
                             "given": given_name, "surname": surname})
        return {"id": f"c{len(self.created)}"}


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


def test_real_people_are_contacts():
    for addr in ("sam@example.org", "melissa.binkley@frazierdeeter.com",
                 "someone@example.com"):
        assert is_person(addr), addr


def test_role_mailboxes_are_not_people():
    # A shared mailbox in type-ahead makes it worse, not better.
    for addr in ("info@x.org", "support@x.com", "partners@example.org",
                 "billing@vendor.io", "media@fpvsp.org"):
        assert not is_person(addr), addr


def test_automated_senders_are_not_people():
    for addr in ("noreply@github.com", "no-reply@x.io", "notifications@y.com",
                 "bounce@z.net", "someone@x.onmicrosoft.com"):
        assert not is_person(addr), addr


def test_rubbish_is_rejected():
    for addr in ("", None, "not-an-address", "   "):
        assert not is_person(addr)


def test_names_split_for_outlook_search():
    # Outlook sorts and searches on givenName/surname, not displayName alone.
    assert split_name("Sam Okafor") == ("Sam", "Okafor")
    assert split_name("Michelle Riendeau Miller") == ("Michelle Riendeau", "Miller")


def test_the_edu_last_comma_first_form_is_handled():
    # Community college directories send "Richmond, Jeffrey".
    assert split_name("Richmond, Jeffrey") == ("Jeffrey", "Richmond")
    assert split_name("Day, Matt") == ("Matt", "Day")


def test_a_single_word_name_does_not_crash():
    assert split_name("mehul") == ("mehul", "")


def test_a_derived_name_is_readable():
    assert derive_name("melissa.binkley@frazierdeeter.com") == "Melissa Binkley"
    assert derive_name("mitchell.kenny83@yahoo.com") == "Mitchell Kenny"


def test_adding_a_person_sets_the_name_fields():
    graph = FakeGraph()
    add_person(graph, "sam@example.org", "Sam Okafor")
    assert graph.created[0]["given"] == "Sam"
    assert graph.created[0]["surname"] == "Okafor"


def test_a_role_address_is_never_added():
    graph = FakeGraph()
    assert add_person(graph, "info@example.org", "Info") is None
    assert graph.created == []


def test_a_graph_failure_does_not_raise():
    # This runs immediately after a reply was sent. A contacts problem must
    # never turn a successful send into an error page.
    assert add_person(FakeGraph(fail=True), "a@b.com", "A B") is None


def test_a_missing_display_name_falls_back_to_the_address():
    graph = FakeGraph()
    add_person(graph, "krishpatel203@example.com", "")
    assert graph.created[0]["display"] == "Krishpatel"


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(name, fn) for name, fn in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
