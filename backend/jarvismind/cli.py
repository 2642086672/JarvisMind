"""Operator command line: ``python -m jarvismind <command>``.

Commands
--------
``init-db``           Create a NEW, empty JarvisMind database and the control
                      database. Refuses to run on a database that already has tables.
``serve``             Run the HTTP API (default when no command is given). Refuses
                      to start while the database has not been initialized.
``version``           Print the version.
``owner-token-path``  Print where the owner token file lives (never the token).

Startup of the web application never creates or migrates a database; only the
explicit ``init-db`` command does.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

from . import __version__
from .config import Settings, load_settings


def _init_db(settings: Settings) -> int:
    from .control.database import CONTROL_DATABASE_FILENAME, ControlDatabase
    from .control.service import ControlService
    from .database.bootstrap import DatabaseAlreadyInitializedError, create_fresh_database
    from .database.connection import DatabaseManager
    from .jkg.bootstrap import install_into_fresh_database

    manager = DatabaseManager(settings)
    try:
        # The graph installer is passed in rather than imported by the database
        # layer, which must stay below JKG in the dependency order.
        version = create_fresh_database(manager, extra_installer=install_into_fresh_database)
    except DatabaseAlreadyInitializedError as exc:
        print(f"init-db refused: {exc}", file=sys.stderr)
        return 1
    finally:
        manager.dispose()
    control_db = ControlDatabase(settings.data_dir / CONTROL_DATABASE_FILENAME)
    try:
        ControlService(control_db, settings.data_dir).ensure_default_space()
    finally:
        control_db.dispose()
    print(f"JarvisMind database created: {settings.database_path}")
    print(f"Schema version: {version}")
    print("Control database ready (default memory space registered).")
    return 0


def _serve(settings: Settings) -> int:
    import uvicorn

    from .core.logging import configure_logging
    from .main import create_app

    if not settings.database_path.is_file():
        print(
            "JarvisMind is not initialized. Run `python -m jarvismind init-db` first.",
            file=sys.stderr,
        )
        return 2
    configure_logging(settings.log_level, settings.api_token)
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m jarvismind")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init-db", help="create a new empty database (refuses existing ones)")
    sub.add_parser("serve", help="run the HTTP API (default)")
    sub.add_parser("version", help="print the version")
    sub.add_parser("owner-token-path", help="print the owner token file location")
    return parser


def main(argv: Optional[Sequence[str]] = None, *, settings: Optional[Settings] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else sys.argv[1:])
    command = args.command or "serve"
    if command == "version":
        print(__version__)
        return 0
    if command in ("init-db", "serve"):
        # Database files hold memory text, so files created from here on are owner-only.
        # On Windows this has no effect; ACLs apply there instead.
        os.umask(0o077)
    resolved = settings if settings is not None else load_settings()
    if command == "init-db":
        return _init_db(resolved)
    if command == "serve":
        return _serve(resolved)
    if command == "owner-token-path":
        print(resolved.token_file)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
