"""SQLite connection management for redline.

WAL mode by default (poller writes + dashboard reads against the same file,
per ``NOTES.md`` §10). ``read_only=True`` flips ``PRAGMA query_only=ON`` for
the dashboard connection. Phase 1 entry only ships the ``llm_call_log`` DDL;
remaining tables land alongside their owning subsystems.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

LLM_CALL_LOG_DDL = """
CREATE TABLE IF NOT EXISTS llm_call_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    called_at       TIMESTAMP NOT NULL,
    call_site       TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_version  TEXT NOT NULL,
    tokens_in       INTEGER NOT NULL,
    tokens_out      INTEGER NOT NULL,
    cost_usd        REAL NOT NULL,
    latency_ms      INTEGER NOT NULL,
    cache_hit       INTEGER NOT NULL,
    status          TEXT NOT NULL,
    error_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_call_log_called_at
    ON llm_call_log (called_at);
CREATE INDEX IF NOT EXISTS idx_llm_call_log_call_site
    ON llm_call_log (call_site, called_at);
"""


def connect(db_path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection in WAL mode.

    Set ``read_only=True`` for the Streamlit dashboard's connection so any
    accidental write attempt fails fast instead of contending with the poller.
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # No detect_types: the default TIMESTAMP converter is deprecated in
    # Python 3.12+ and chokes on ISO 8601 (`T` separator). We store
    # timestamps as ISO 8601 strings and parse in application code when needed.
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    if read_only:
        conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create the tables this module owns (``llm_call_log``)."""
    conn.executescript(LLM_CALL_LOG_DDL)


@contextmanager
def open_db(db_path: str | Path, *, read_only: bool = False):
    """Context-managed connection. Auto-initializes schema on writeable opens."""
    conn = connect(db_path, read_only=read_only)
    try:
        if not read_only:
            init_schema(conn)
        yield conn
    finally:
        conn.close()
