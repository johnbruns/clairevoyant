"""Drafts waiting for Alex's decision, held outside the Outlook Drafts folder.

The Drafts folder was the wrong place to queue work. It has no notion of "the
assistant wrote this and you have not looked at it yet", so assistant drafts
and Alex's own half-written mail pile up in the same list and neither is
findable. Holding the text here instead means the recap email is the queue,
and the Drafts folder goes back to being Alex's.

A pending record is created when a draft is written, and resolved exactly once
- sent, edited-and-sent, or discarded. That "exactly once" is the replay
protection for the emailed buttons: a token cannot know it has been spent, but
the record can, so `resolve()` is the single chokepoint where a second click on
an already-sent link is refused.

Records are pruned after RETENTION_DAYS, which is deliberately the same week
the action tokens are signed for. A record that outlived its token would be
dead weight; a token that outlived its record already fails closed, because
`get()` returns None and the handler says the draft is gone.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

RETENTION_DAYS = 7

PENDING = "pending"
SENT = "sent"
DISCARDED = "discarded"


@dataclass
class PendingDraft:
    """One drafted reply awaiting a decision."""

    id: str
    message_id: str                  # the Graph message being replied to
    subject: str = ""
    sender_name: str = ""
    sender_address: str = ""
    the_ask: str = ""                # what they actually asked for, one line
    request_type: str = ""           # "meeting" | "request"
    body: str = ""                   # the drafted reply, plain text
    outlook_draft_id: str | None = None   # set only when CREATE_OUTLOOK_DRAFTS
    status: str = PENDING
    created: str = ""
    resolved: str = ""
    resolution_note: str = ""

    @staticmethod
    def new(**kwargs) -> "PendingDraft":
        kwargs.setdefault("id", uuid.uuid4().hex)
        kwargs.setdefault("created", dt.datetime.now(dt.timezone.utc).isoformat())
        return PendingDraft(**kwargs)

    @property
    def open(self) -> bool:
        return self.status == PENDING


class PendingStore(ABC):
    @abstractmethod
    def _load(self) -> dict[str, dict]: ...

    @abstractmethod
    def _save(self, data: dict[str, dict]) -> None: ...

    def put(self, draft: PendingDraft) -> PendingDraft:
        data = self._load()
        data[draft.id] = asdict(draft)
        self._save(self._prune(data))
        return draft

    def get(self, pending_id: str) -> PendingDraft | None:
        raw = self._load().get(pending_id)
        if not raw:
            return None
        known = {f for f in PendingDraft.__dataclass_fields__}
        return PendingDraft(**{k: v for k, v in raw.items() if k in known})

    def resolve(self, pending_id: str, status: str, note: str = "") -> PendingDraft | None:
        """Mark a draft sent or discarded. Returns None if it was already resolved.

        This is the replay chokepoint. Two clicks on the same "approve and
        send" link - or a link scanner that follows the POST, or a forwarded
        recap - must send exactly one email, and this is the only place that
        can be true.
        """
        data = self._load()
        raw = data.get(pending_id)
        if not raw or raw.get("status") != PENDING:
            return None

        raw["status"] = status
        raw["resolved"] = dt.datetime.now(dt.timezone.utc).isoformat()
        raw["resolution_note"] = note
        self._save(data)

        known = {f for f in PendingDraft.__dataclass_fields__}
        return PendingDraft(**{k: v for k, v in raw.items() if k in known})

    def resolve_rollback(self, pending_id: str) -> None:
        """Put a claimed draft back to pending after the action itself failed.

        `resolve` is claimed before the Graph call so two simultaneous clicks
        cannot both send. That optimism has to be undone when the send fails,
        or the draft is stuck reading "sent" having never been sent - the one
        outcome worse than sending twice, because Alex stops watching for it.
        """
        data = self._load()
        raw = data.get(pending_id)
        if not raw or raw.get("status") == PENDING:
            return
        raw["status"] = PENDING
        raw["resolved"] = ""
        raw["resolution_note"] = ""
        self._save(data)

    def update_body(self, pending_id: str, body: str) -> None:
        data = self._load()
        raw = data.get(pending_id)
        if not raw or raw.get("status") != PENDING:
            return
        raw["body"] = body
        self._save(data)

    def open_drafts(self) -> list[PendingDraft]:
        known = {f for f in PendingDraft.__dataclass_fields__}
        out = [
            PendingDraft(**{k: v for k, v in raw.items() if k in known})
            for raw in self._load().values()
        ]
        return sorted((d for d in out if d.open), key=lambda d: d.created)

    @staticmethod
    def _prune(data: dict[str, dict], now: dt.datetime | None = None) -> dict[str, dict]:
        now = now or dt.datetime.now(dt.timezone.utc)
        cutoff = now - dt.timedelta(days=RETENTION_DAYS)
        kept = {}
        for pid, raw in data.items():
            try:
                if dt.datetime.fromisoformat(raw.get("created", "")) >= cutoff:
                    kept[pid] = raw
            except (ValueError, TypeError):
                continue  # unparseable entry is dropped rather than kept forever
        return kept


class FilePendingStore(PendingStore):
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


class TablePendingStore(PendingStore):
    """One entity per draft, so a busy week cannot outgrow a single row.

    state.py packs everything into one row because it stores nothing but ids
    and timestamps. Draft bodies are kilobytes each, and Azure Table caps an
    entity at 1 MB, so this one gets a row per record.
    """

    PARTITION = "draft"

    def __init__(self, connection_string: str, table_name: str = "assistantdrafts"):
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
        # _save is only ever reached with the full set in hand, so writing the
        # changed rows and deleting the pruned ones keeps the table in step.
        from azure.core.exceptions import ResourceNotFoundError

        existing = set()
        for entity in self._table.query_entities(
            f"PartitionKey eq '{self.PARTITION}'", select=["RowKey"]
        ):
            existing.add(entity["RowKey"])

        for pid, raw in data.items():
            self._table.upsert_entity(
                {"PartitionKey": self.PARTITION, "RowKey": pid, "value": json.dumps(raw)}
            )

        for stale in existing - set(data):
            try:
                self._table.delete_entity(self.PARTITION, stale)
            except ResourceNotFoundError:
                pass


def default_pending_store() -> PendingStore:
    conn = os.environ.get("AzureWebJobsStorage")
    if conn and not conn.startswith("UseDevelopmentStorage"):
        return TablePendingStore(conn)
    return FilePendingStore(os.environ.get("PENDING_PATH", ".local/pending.json"))
