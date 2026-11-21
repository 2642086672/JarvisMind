"""SQLite access for the control database.

The database file is created on first use, so building the application never
opens a connection (startup-safety rule). Schema creation is idempotent DDL and
touches only control-plane tables. The metadata row and the default-space seed
are written in one transaction afterwards; readiness is recorded only after that
transaction commits, so a failed seed is repeated on the next call.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..core.timeutils import utc_now_iso
from .models import CONTROL_SCHEMA_VERSION, ControlBase, ControlMeta

CONTROL_DATABASE_FILENAME = "control.db"

SeedCallback = Callable[[Session], None]


def _apply_sqlite_pragmas(dbapi_connection, _record) -> None:
    """Enforce foreign keys and wait for locks on every control connection.

    Set here rather than relying on the process-wide listener in ``database.connection``,
    so the control database behaves the same whichever module was imported first.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


class ControlDatabase:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._ready = False
        self._seed: Optional[SeedCallback] = None
        self.engine = create_engine(
            f"sqlite+pysqlite:///{self.path.as_posix()}",
            future=True,
            connect_args={"check_same_thread": False},
        )
        event.listen(self.engine, "connect", _apply_sqlite_pragmas)
        self._factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
            future=True,
        )

    def set_on_ready(self, callback: SeedCallback) -> None:
        """Register an idempotent seeding step, run once inside the schema transaction."""
        self._seed = callback

    def ensure_schema(self) -> None:
        with self._lock:
            if self._ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            ControlBase.metadata.create_all(self.engine)
            session = self._factory()
            try:
                row = session.get(ControlMeta, "schema_version")
                if row is None:
                    session.add(
                        ControlMeta(
                            key="schema_version",
                            value=str(CONTROL_SCHEMA_VERSION),
                            updated_at=utc_now_iso(),
                        )
                    )
                elif row.value != str(CONTROL_SCHEMA_VERSION):
                    raise RuntimeError(f"unsupported control schema version {row.value!r}")
                if self._seed is not None:
                    self._seed(session)
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
            self._ready = True

    @contextmanager
    def session(self) -> Iterator[Session]:
        self.ensure_schema()
        session = self._factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
