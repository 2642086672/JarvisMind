"""SQLite connection management.

A single engine is created per process. Every session enables the
``foreign_keys`` pragma so SQLite actually enforces referential integrity, and
WAL mode so readers do not block the writer during API traffic.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, event, text
from sqlalchemy.engine import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings


@event.listens_for(Engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """Apply per-connection SQLite pragmas (foreign keys + WAL)."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()
    # P0 fix: DBAPI-level authorizer. SQLAlchemy Engine 'before_cursor_execute'
    # cannot see statements executed inside SQLite TRIGGERs or through
    # raw_connection(); set_authorizer is the last common choke point.
    from ..p1_gate import install_sqlite_authorizer

    install_sqlite_authorizer(dbapi_connection)


class DatabaseManager:
    """Owns the engine and the session factory for one database file."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            settings.database_url,
            echo=settings.db_echo,
            future=True,
            connect_args={"check_same_thread": False},
        )
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
            future=True,
        )
        # P0-2 fix: install the connection-level write gate on EVERY engine,
        # including engines created outside build_governance(). The guard is a
        # no-op while governance is inactive, so legacy behaviour is preserved.
        from ..p1_gate import install_connection_gate

        install_connection_gate(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield a session that commits on success and rolls back on error."""
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def verify_connection(self) -> bool:
        """Return True when a trivial query succeeds."""
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def dispose(self) -> None:
        self.engine.dispose()
