"""Per-space runtime containers: the physical isolation boundary for memory data.

Every memory space other than ``default`` owns a directory with its own JME
SQLite database and its own vector directory. No query can cross databases, so
a principal bound to one space cannot read or change another space's rows even
if a route forgets a filter.

The ``default`` space uses the data directory root. Its database is created by
``python -m jarvismind init-db``. Non-default spaces are created only through
``provision_new``, which refuses to touch existing storage.
"""
from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import replace
from pathlib import Path
from typing import Dict, List

from .config import Settings
from .container import Container
from .control.errors import ControlConflictError, SpaceNotProvisionedError
from .control.service import DEFAULT_SPACE_ID, validate_space_id
from .database.bootstrap import create_fresh_database
from .database.connection import DatabaseManager

SPACES_DIRNAME = "spaces"
DATABASE_FILENAME = "jarvismind.db"


class SpaceRegistry:
    def __init__(
        self,
        settings: Settings,
        default_container: Container,
        logger: logging.Logger,
        started_at: float,
    ) -> None:
        self._settings = settings
        self._default = default_container
        self._logger = logger
        self._started_at = started_at
        self._lock = threading.RLock()
        self._containers: Dict[str, Container] = {}

    def storage_dir(self, space_id: str) -> Path:
        if space_id == DEFAULT_SPACE_ID:
            return self._settings.data_dir
        return self._settings.data_dir / SPACES_DIRNAME / validate_space_id(space_id)

    def settings_for(self, space_id: str) -> Settings:
        if space_id == DEFAULT_SPACE_ID:
            return self._settings
        directory = self.storage_dir(space_id)
        database_path = directory / DATABASE_FILENAME
        # A shared vector path would let one space's index answer for another,
        # so every non-default space gets its own vector directory.
        return replace(
            self._settings,
            data_dir=directory,
            database_url=f"sqlite+pysqlite:///{database_path.as_posix()}",
            qdrant_path_override="",
        )

    def is_provisioned(self, space_id: str) -> bool:
        return (self.storage_dir(space_id) / DATABASE_FILENAME).is_file()

    def ensure_provisioned(self, space_id: str) -> None:
        """Make a space usable again before it is re-activated."""
        if space_id == DEFAULT_SPACE_ID or self.is_provisioned(space_id):
            return
        if self.storage_dir(space_id).exists():
            raise ControlConflictError(
                "the storage of this memory space exists but is not initialized; "
                "repair it offline before activating the space"
            )
        self.provision_new(space_id)

    def provision_new(self, space_id: str) -> int:
        """Create a brand-new space database. Never touches existing storage."""
        validate_space_id(space_id)
        if space_id == DEFAULT_SPACE_ID:
            raise ControlConflictError("the default space is created by init-db")
        with self._lock:
            directory = self.storage_dir(space_id)
            if directory.exists():
                raise ControlConflictError("storage for this memory space already exists")
            directory.mkdir(parents=True)
            settings = self.settings_for(space_id)
            manager = DatabaseManager(settings)
            try:
                version = create_fresh_database(manager)
            except Exception:
                manager.dispose()
                shutil.rmtree(directory, ignore_errors=True)
                raise
            self._containers[space_id] = self._build(manager, settings)
            self._logger.info("memory space provisioned: %s (schema v%s)", space_id, version)
            return version

    def discard(self, space_id: str) -> None:
        """Remove storage created by provision_new after its control row failed."""
        if space_id == DEFAULT_SPACE_ID:
            return
        with self._lock:
            container = self._containers.pop(space_id, None)
            if container is not None:
                self._close(container)
            directory = self.storage_dir(space_id)
            if directory.is_dir():
                shutil.rmtree(directory, ignore_errors=True)

    def container_for(self, space_id: str) -> Container:
        if space_id == DEFAULT_SPACE_ID:
            return self._default
        with self._lock:
            cached = self._containers.get(space_id)
            if cached is not None:
                return cached
            if not self.is_provisioned(space_id):
                raise SpaceNotProvisionedError()
            settings = self.settings_for(space_id)
            manager = DatabaseManager(settings)
            container = self._build(manager, settings)
            self._containers[space_id] = container
            return container

    def _build(self, manager: DatabaseManager, settings: Settings) -> Container:
        container = Container(
            settings=settings,
            manager=manager,
            logger=self._logger,
            started_at=self._started_at,
        )
        # The embedding model is large and stateless, so every space shares the
        # default space's provider instead of loading its own copy.
        container._embedding_provider = self._default.embedding_provider
        if bool(getattr(settings, "p1_governance_enabled", False)):
            # Same rule as the default container: the write gate must be active
            # before the first request reaches this database.
            _ = container.governance
        return container

    @staticmethod
    def _close(container: Container) -> None:
        try:
            container.close()
        finally:
            container.manager.dispose()

    def open_space_ids(self) -> List[str]:
        with self._lock:
            return sorted(self._containers)

    def close(self) -> None:
        with self._lock:
            for container in self._containers.values():
                self._close(container)
            self._containers.clear()
