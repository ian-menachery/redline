"""DDL for redline tables owned outside of the LLM substrate.

Phase 1 step 1 (poller) owns ``watchlist`` and ``filings_seen``. The remaining
tables from ``ARCHITECTURE.md`` §10 (``filings_content``, ``form4_transactions``,
``diff_results``, ``flagged_events``, ``eval_runs``, ``live_operation_log``)
land alongside their owning subsystems.

``llm_call_log`` lives in ``src/redline/storage/db.py`` with the connection
factory and is layered in by ``init_full_schema()`` so any subsystem can
``CREATE IF NOT EXISTS`` independently.
"""
from __future__ import annotations

import datetime
import sqlite3
from pathlib import Path

import yaml

WATCHLIST_DDL = """
CREATE TABLE IF NOT EXISTS watchlist (
    cik       TEXT PRIMARY KEY,
    ticker    TEXT NOT NULL,
    name      TEXT NOT NULL,
    sector    TEXT NOT NULL,
    added_at  TIMESTAMP NOT NULL
);
"""

FILINGS_SEEN_DDL = """
CREATE TABLE IF NOT EXISTS filings_seen (
    accession       TEXT PRIMARY KEY,
    cik             TEXT NOT NULL REFERENCES watchlist(cik),
    filing_type     TEXT NOT NULL,
    period_end      DATE,
    filed_at        TIMESTAMP NOT NULL,
    status          TEXT NOT NULL,
    last_attempted  TIMESTAMP,
    failure_reason  TEXT,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    discovered_at   TIMESTAMP NOT NULL,
    eval_run_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_filings_seen_cik_type_filed
    ON filings_seen (cik, filing_type, filed_at);
CREATE INDEX IF NOT EXISTS idx_filings_seen_status_attempt
    ON filings_seen (status, last_attempted);
"""

# Subsystem 2 (fetcher + parser) owns these.
FILINGS_CONTENT_DDL = """
CREATE TABLE IF NOT EXISTS filings_content (
    accession      TEXT PRIMARY KEY REFERENCES filings_seen(accession),
    raw_content    BLOB,
    sections       TEXT NOT NULL,
    is_empty       TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    extracted_at   TIMESTAMP NOT NULL
);
"""

# Subsystem 2 populates this; Subsystem 4 (correlator) reads from it.
# Schema deviation from ARCHITECTURE.md §10: ownership and insider_cik are
# nullable in Phase 1 because reliable extraction from edgartools is
# best-effort (see NOTES §3.1). Phase 2 can tighten when an LLM-based
# extractor lands.
FORM4_TRANSACTIONS_DDL = """
CREATE TABLE IF NOT EXISTS form4_transactions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    accession         TEXT NOT NULL REFERENCES filings_seen(accession),
    cik               TEXT NOT NULL,
    insider_cik       TEXT,
    insider_name      TEXT NOT NULL,
    trade_date        DATE NOT NULL,
    code              TEXT NOT NULL,
    shares            REAL NOT NULL,
    price             REAL,
    ownership         TEXT,
    is_10b5_1         INTEGER,
    plan_adopted_date DATE,
    explanation       TEXT
);
CREATE INDEX IF NOT EXISTS idx_form4_tx_cik_date
    ON form4_transactions (cik, trade_date);
CREATE INDEX IF NOT EXISTS idx_form4_tx_insider_date
    ON form4_transactions (insider_name, trade_date);
"""


def init_full_schema(conn: sqlite3.Connection) -> None:
    """Idempotently create every table any subsystem in redline currently uses.

    Safe to call repeatedly; CREATE IF NOT EXISTS on every statement.
    """
    from redline.storage.db import init_schema as _init_llm_call_log

    _init_llm_call_log(conn)
    conn.executescript(WATCHLIST_DDL)
    conn.executescript(FILINGS_SEEN_DDL)
    conn.executescript(FILINGS_CONTENT_DDL)
    conn.executescript(FORM4_TRANSACTIONS_DDL)


def seed_watchlist_from_yaml(conn: sqlite3.Connection, path: str | Path) -> int:
    """Seed the ``watchlist`` table from ``config/watchlist.yaml``.

    Idempotent: existing rows (by CIK) are not overwritten. Returns the number
    of new rows inserted.
    """
    with Path(path).open(encoding="utf-8") as f:
        entries = yaml.safe_load(f)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    inserted = 0
    for entry in entries:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO watchlist (cik, ticker, name, sector, added_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entry["cik"], entry["ticker"], entry["name"], entry["sector"], now),
        )
        inserted += cur.rowcount
    return inserted
