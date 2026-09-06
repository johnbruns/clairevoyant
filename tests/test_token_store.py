import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.token_store import BootstrapStore, FileTokenStore, TokenStore


class MemoryStore(TokenStore):
    def __init__(self, value=None):
        self.value = value
        self.writes = 0

    def read(self):
        return self.value

    def write(self, refresh_token):
        self.value = refresh_token
        self.writes += 1


class Skipped(Exception):
    """Raised by a test that cannot mean anything on this platform."""


def run(name, fn):
    try:
        fn()
    except Skipped as exc:
        print(f"skip  {name}: {exc}")
        return True
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        return False
    finally:
        os.environ.pop(BootstrapStore.ENV_VAR, None)
    print(f"ok    {name}")
    return True


def test_file_store_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        store = FileTokenStore(Path(d) / "nested" / "token.json")
        assert store.read() is None
        store.write("rt-1")
        assert store.read() == "rt-1"
        store.write("rt-2")
        assert store.read() == "rt-2"


def test_file_store_permissions_are_owner_only():
    # POSIX mode bits are meaningless on Windows: NTFS ACLs govern access and
    # os.chmod cannot express 0600, so the file reports 0666 there. Skipped
    # rather than asserted, so the suite runs on the machine Alex works from
    # while still proving the permission on the Linux host that serves it.
    if os.name != "posix":
        raise Skipped("POSIX mode bits only")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "token.json"
        FileTokenStore(path).write("rt")
        assert oct(path.stat().st_mode)[-3:] == "600", oct(path.stat().st_mode)


def test_bootstrap_seeds_empty_store_once():
    inner = MemoryStore()
    os.environ[BootstrapStore.ENV_VAR] = "seed-token"
    store = BootstrapStore(inner)

    assert store.read() == "seed-token"
    assert inner.writes == 1, "seed should be persisted immediately"
    assert store.read() == "seed-token"
    assert inner.writes == 1, "second read must not re-seed"


def test_bootstrap_never_overrides_a_rotated_token():
    # The Key Vault copy goes stale the moment Entra rotates the token.
    # If the seed ever won over the stored value, the assistant would keep
    # presenting a dead token and die.
    inner = MemoryStore(value="rotated-token")
    os.environ[BootstrapStore.ENV_VAR] = "stale-seed"
    store = BootstrapStore(inner)

    assert store.read() == "rotated-token"
    assert inner.writes == 0


def test_bootstrap_returns_none_when_nothing_available():
    store = BootstrapStore(MemoryStore())
    assert store.read() is None


def test_bootstrap_write_passes_through():
    inner = MemoryStore()
    BootstrapStore(inner).write("new-rt")
    assert inner.value == "new-rt"


tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
results = [run(n, f) for n, f in tests]
print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
