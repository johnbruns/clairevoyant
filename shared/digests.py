"""Sent task digests, so a reply to one can be matched back to its issues.

When Alex replies "did the MOU one" to his Monday task email, two questions
have to be answerable: is this actually a reply to a digest the assistant
sent, and which issues was that digest about? Both are answered from here.

**Matching is by a marker in the subject line, not by conversationId.** Graph's
`sendMail` returns no id at all, so the conversationId of a sent message is not
knowable without composing a draft, sending it, and then hunting for it in Sent
Items. A marker the assistant generates is knowable by construction, survives
every mail client's reply prefix, and works even if Alex forwards the digest to
himself from another account.

The marker doubles as the authorisation check. A reply is acted on only when
it carries a marker matching a digest this assistant sent AND comes from Alex's
own address - so a stray email quoting the words "mark it done" does nothing.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import secrets
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

RETENTION_DAYS = 30

# "[#T-a1b2c3d4]" - short, unmistakable, and untouched by "RE:" prefixes and
# subject-line truncation in every client worth worrying about.
MARKER_RE = re.compile(r"\[#T-([0-9a-f]{8})\]")


def new_marker() -> str:
    return secrets.token_hex(4)


def marker_tag(marker: str) -> str:
    return f"[#T-{marker}]"


def find_marker(subject: str | None) -> str | None:
    match = MARKER_RE.search(subject or "")
    return match.group(1) if match else None


@dataclass
class TaskDigest:
    """One sent digest and the issues it listed."""

    marker: str
    sent: str = ""
    kind: str = "my-tasks"
    # key -> summary, so a reply naming no key can still be resolved by text.
    issues: dict = field(default_factory=dict)

    @staticmethod
    def new(issues: dict, kind: str = "my-tasks") -> "TaskDigest":
        return TaskDigest(
            marker=new_marker(),
            sent=dt.datetime.now(dt.timezone.utc).isoformat(),
            kind=kind,
            issues=dict(issues),
        )


class DigestStore(ABC):
    @abstractmethod
    def _load(self) -> dict[str, dict]: ...

    @abstractmethod
    def _save(self, data: dict[str, dict]) -> None: ...

    def put(self, digest: TaskDigest) -> TaskDigest:
        data = self._load()
        data[digest.marker] = asdict(digest)
        self._save(self._prune(data))
        return digest

    def get(self, marker: str) -> TaskDigest | None:
        raw = self._load().get(marker)
        if not raw:
            return None
        known = set(TaskDigest.__dataclass_fields__)
        return TaskDigest(**{k: v for k, v in raw.items() if k in known})

    @staticmethod
    def _prune(data: dict[str, dict], now: dt.datetime | None = None) -> dict[str, dict]:
        now = now or dt.datetime.now(dt.timezone.utc)
        cutoff = now - dt.timedelta(days=RETENTION_DAYS)
        kept = {}
        for marker, raw in data.items():
            try:
                if dt.datetime.fromisoformat(raw.get("sent", "")) >= cutoff:
                    kept[marker] = raw
            except (ValueError, TypeError):
                continue
        return kept


class FileDigestStore(DigestStore):
    def __init__(self, path: str | Path):
        self._path = Path(path)

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (ValueError, OSError):
            return {}

    def _save(self, data: dict[str, dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data))


class TableDigestStore(DigestStore):
    PARTITION = "digest"

    def __init__(self, connection_string: str, table_name: str = "assistantdigests"):
        from azure.data.tables import TableServiceClient

        service = TableServiceClient.from_connection_string(connection_string)
        self._table = service.create_table_if_not_exists(table_name)

    def _load(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for entity in self._table.query_entities(f"PartitionKey eq '{self.PARTITION}'"):
            try:
                out[entity["RowKey"]] = json.loads(entity.get("value") or "{}")
            except ValueError:
                continue
        return out

    def _save(self, data: dict[str, dict]) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        existing = {
            e["RowKey"]
            for e in self._table.query_entities(
                f"PartitionKey eq '{self.PARTITION}'", select=["RowKey"]
            )
        }
        for marker, raw in data.items():
            self._table.upsert_entity(
                {"PartitionKey": self.PARTITION, "RowKey": marker, "value": json.dumps(raw)}
            )
        for stale in existing - set(data):
            try:
                self._table.delete_entity(self.PARTITION, stale)
            except ResourceNotFoundError:
                pass


def default_digest_store() -> DigestStore:
    conn = os.environ.get("AzureWebJobsStorage")
    if conn and not conn.startswith("UseDevelopmentStorage"):
        return TableDigestStore(conn)
    return FileDigestStore(os.environ.get("DIGEST_PATH", ".local/digests.json"))
