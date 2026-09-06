"""Tracks which messages have already been recapped.

Without this the assistant re-drafts every unread message on every cycle:
33 recap emails a day about the same mail, and a Claude bill to match. A
message is recorded once it has appeared in a recap, and entries are pruned
after RETENTION_DAYS so the record cannot grow forever.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path

log = logging.getLogger(__name__)

RETENTION_DAYS = 7


class ProcessedStore(ABC):
    @abstractmethod
    def _load(self) -> dict[str, str]: ...

    @abstractmethod
    def _save(self, data: dict[str, str]) -> None: ...

    def seen(self) -> set[str]:
        return set(self._load())

    def mark(self, message_ids: list[str], now: dt.datetime | None = None) -> None:
        now = now or dt.datetime.now(dt.timezone.utc)
        data = self._load()
        stamp = now.isoformat()
        for mid in message_ids:
            data.setdefault(mid, stamp)
        self._save(self._prune(data, now))

    @staticmethod
    def _prune(data: dict[str, str], now: dt.datetime) -> dict[str, str]:
        cutoff = now - dt.timedelta(days=RETENTION_DAYS)
        kept = {}
        for mid, stamp in data.items():
            try:
                if dt.datetime.fromisoformat(stamp) >= cutoff:
                    kept[mid] = stamp
            except ValueError:
                continue  # unparseable entry is dropped rather than kept forever
        return kept


class FileProcessedStore(ProcessedStore):
    def __init__(self, path: str | Path):
        self._path = Path(path)

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (ValueError, OSError):
            return {}

    def _save(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data))


class TableProcessedStore(ProcessedStore):
    PARTITION = "graph"
    ROW = "processed_messages"

    def __init__(self, connection_string: str, table_name: str = "assistantstate"):
        from azure.data.tables import TableServiceClient

        service = TableServiceClient.from_connection_string(connection_string)
        self._table = service.create_table_if_not_exists(table_name)

    def _load(self) -> dict[str, str]:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            entity = self._table.get_entity(self.PARTITION, self.ROW)
        except ResourceNotFoundError:
            return {}
        try:
            return json.loads(entity.get("value") or "{}")
        except ValueError:
            return {}

    def _save(self, data: dict[str, str]) -> None:
        self._table.upsert_entity(
            {"PartitionKey": self.PARTITION, "RowKey": self.ROW, "value": json.dumps(data)}
        )


def default_processed_store() -> ProcessedStore:
    conn = os.environ.get("AzureWebJobsStorage")
    if conn and not conn.startswith("UseDevelopmentStorage"):
        return TableProcessedStore(conn)
    return FileProcessedStore(os.environ.get("STATE_PATH", ".local/processed.json"))
