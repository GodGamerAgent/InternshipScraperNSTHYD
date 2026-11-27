"""SQLite engine creation with the pragmas every connection needs."""

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, event
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine

MEMORY = ":memory:"


def _set_pragmas(dbapi_connection: Any, _record: Any, *, wal: bool) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    if wal:
        cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def create_db_engine(db_path: Path | str) -> Engine:
    """Create an engine for a SQLite file (parent dirs auto-created) or ``":memory:"``.

    Foreign keys are enforced and file databases use WAL journaling. In-memory databases share a
    single connection (``StaticPool``) so every repository call sees the same data.
    """
    if str(db_path) == MEMORY:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        wal = False
    else:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{path}")
        wal = True
    event.listen(engine, "connect", lambda conn, rec: _set_pragmas(conn, rec, wal=wal))
    return engine
