import datetime as dt
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.state import RETENTION_DAYS, FileProcessedStore

NOW = dt.datetime(2026, 9, 2, 12, 0, tzinfo=dt.timezone.utc)


def run(name, fn):
    try:
        fn()
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    print(f"ok    {name}")
    return True


def test_marks_and_reports_seen():
    with tempfile.TemporaryDirectory() as d:
        s = FileProcessedStore(Path(d) / "p.json")
        assert s.seen() == set()
        s.mark(["a", "b"], NOW)
        assert s.seen() == {"a", "b"}


def test_marking_is_additive():
    with tempfile.TemporaryDirectory() as d:
        s = FileProcessedStore(Path(d) / "p.json")
        s.mark(["a"], NOW)
        s.mark(["b"], NOW)
        assert s.seen() == {"a", "b"}


def test_first_seen_timestamp_is_not_refreshed():
    # If re-marking reset the clock, an id touched every cycle would never expire.
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "p.json"
        s = FileProcessedStore(path)
        s.mark(["a"], NOW)
        original = json.loads(path.read_text())["a"]
        s.mark(["a"], NOW + dt.timedelta(days=1))
        assert json.loads(path.read_text())["a"] == original


def test_entries_expire_after_retention():
    with tempfile.TemporaryDirectory() as d:
        s = FileProcessedStore(Path(d) / "p.json")
        s.mark(["old"], NOW)
        s.mark(["new"], NOW + dt.timedelta(days=RETENTION_DAYS + 1))
        assert s.seen() == {"new"}, s.seen()


def test_corrupt_file_is_survivable():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "p.json"
        path.write_text("{not json")
        s = FileProcessedStore(path)
        assert s.seen() == set()
        s.mark(["a"], NOW)
        assert s.seen() == {"a"}


def test_unparseable_timestamp_is_dropped():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "p.json"
        path.write_text(json.dumps({"bad": "not-a-date", "good": NOW.isoformat()}))
        s = FileProcessedStore(path)
        s.mark([], NOW)
        assert s.seen() == {"good"}, s.seen()


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(n, f) for n, f in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
