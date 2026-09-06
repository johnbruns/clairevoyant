"""Persistence for the delegated OAuth refresh token.

Entra rotates the refresh token on every redemption for confidential clients.
The new one MUST be persisted or the assistant dies the next time the old one
falls out of the validity window. That is the single most common way a
homegrown Graph integration stops working a few weeks in, so the store is a
first-class piece rather than an afterthought.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from abc import ABC, abstractmethod
from pathlib import Path

log = logging.getLogger(__name__)


class TokenStore(ABC):
    @abstractmethod
    def read(self) -> str | None:
        """Return the current refresh token, or None if never seeded."""

    @abstractmethod
    def write(self, refresh_token: str) -> None:
        """Persist a rotated refresh token."""


class FileTokenStore(TokenStore):
    """Local development store. Never used in Azure."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._lock = threading.Lock()

    def read(self) -> str | None:
        if not self._path.exists():
            return None
        data = json.loads(self._path.read_text())
        return data.get("refresh_token")

    def write(self, refresh_token: str) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"refresh_token": refresh_token}))
            tmp.replace(self._path)
            os.chmod(self._path, 0o600)


class TableTokenStore(TokenStore):
    """Azure Table Storage store, using the Function App's own storage account.

    No extra resource to provision and no extra cost worth measuring; the table
    lives in the storage account the Function App already requires.
    """

    PARTITION = "graph"
    ROW = "refresh_token"

    def __init__(self, connection_string: str, table_name: str = "assistanttokens"):
        from azure.data.tables import TableServiceClient

        service = TableServiceClient.from_connection_string(connection_string)
        self._table = service.create_table_if_not_exists(table_name)
        self._lock = threading.Lock()

    def read(self) -> str | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            entity = self._table.get_entity(self.PARTITION, self.ROW)
        except ResourceNotFoundError:
            return None
        return entity.get("value")

    def write(self, refresh_token: str) -> None:
        with self._lock:
            self._table.upsert_entity(
                {
                    "PartitionKey": self.PARTITION,
                    "RowKey": self.ROW,
                    "value": refresh_token,
                }
            )


class BootstrapStore(TokenStore):
    """Seeds an empty store once from GRAPH_BOOTSTRAP_REFRESH_TOKEN.

    The consent script produces the first refresh token; it goes into Key Vault
    as 'graph-refresh-token' and reaches the app as this env var via a Key Vault
    reference. On first run the value is copied into the durable store and every
    read after that comes from there, because the token rotates and the Key Vault
    copy goes stale immediately. The app setting can be deleted once the table
    has a value - it is a seed, not a source of truth.
    """

    ENV_VAR = "GRAPH_BOOTSTRAP_REFRESH_TOKEN"

    def __init__(self, inner: TokenStore):
        self._inner = inner

    def read(self) -> str | None:
        existing = self._inner.read()
        if existing:
            return existing
        seed = os.environ.get(self.ENV_VAR)
        if seed:
            log.info("Seeding token store from %s (first run).", self.ENV_VAR)
            self._inner.write(seed)
            return seed
        return None

    def write(self, refresh_token: str) -> None:
        self._inner.write(refresh_token)


def default_store() -> TokenStore:
    conn = os.environ.get("AzureWebJobsStorage")
    if conn and not conn.startswith("UseDevelopmentStorage"):
        return BootstrapStore(TableTokenStore(conn))
    return BootstrapStore(
        FileTokenStore(os.environ.get("TOKEN_STORE_PATH", ".local/token.json"))
    )
