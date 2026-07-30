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

# Subsystem 3 (diff analyzer) owns these.
DIFF_RESULTS_DDL = """
CREATE TABLE IF NOT EXISTS diff_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    accession        TEXT NOT NULL REFERENCES filings_seen(accession),
    prior_accession  TEXT NOT NULL REFERENCES filings_seen(accession),
    section          TEXT NOT NULL,
    stage            INTEGER NOT NULL,
    chunk_old        TEXT,
    chunk_new        TEXT,
    gate_decision    TEXT,
    summary          TEXT,
    materiality      REAL,
    prompt_version   TEXT NOT NULL,
    created_at       TIMESTAMP NOT NULL,
    eval_run_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_diff_results_acc_sec_stg
    ON diff_results (accession, section, stage);
"""

FLAGGED_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS flagged_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    accession          TEXT NOT NULL REFERENCES filings_seen(accession),
    flag_reason        TEXT NOT NULL,
    diff_summary       TEXT,
    correlator_payload TEXT,
    materiality_max    REAL,
    flagged_at         TIMESTAMP NOT NULL,
    eval_run_id        TEXT
);
CREATE INDEX IF NOT EXISTS idx_flagged_events_flagged_at
    ON flagged_events (flagged_at);
CREATE INDEX IF NOT EXISTS idx_flagged_events_accession
    ON flagged_events (accession);
"""

# Subsystem 4 (correlator) tracks completion here. A row in this table
# means "the correlator has run against this filing exactly once." Avoids
# overloading the filings_seen.status enum with subsystem-completion bits.
CORRELATOR_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS correlator_runs (
    accession            TEXT PRIMARY KEY REFERENCES filings_seen(accession),
    ran_at               TIMESTAMP NOT NULL,
    trades_in_window     INTEGER NOT NULL,
    discretionary_count  INTEGER NOT NULL,
    anomalous            INTEGER,
    confidence           REAL
);
"""

# Subsystem 7 (DCF valuation). Companyfacts ingestion sink — one row per
# (concept, fiscal period). The UNIQUE key makes re-ingest idempotent and lets
# the refresh upsert the latest reported value. Restatement *history* is not
# preserved here (get_facts() returns the point-in-time-of-fetch view with no
# per-fact source accession); that would need per-filing XBRL and is out of
# scope for the XBRL-only revaluation core. See NOTES.md §6.
XBRL_FACTS_DDL = """
CREATE TABLE IF NOT EXISTS xbrl_facts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cik           TEXT NOT NULL,
    concept       TEXT NOT NULL,
    label         TEXT,
    unit          TEXT,
    period_type   TEXT,
    fiscal_year   INTEGER,
    fiscal_period TEXT,
    period_start  DATE,
    period_end    DATE,
    numeric_value REAL,
    ingested_at   TIMESTAMP NOT NULL,
    UNIQUE (cik, concept, fiscal_year, fiscal_period, period_start, period_end)
);
CREATE INDEX IF NOT EXISTS idx_xbrl_facts_cik_concept
    ON xbrl_facts (cik, concept, fiscal_year);
"""

# Subsystem 7 — typed forward-guidance figures extracted from 8-K earnings
# exhibits (the differentiated "flagged change -> model input" path; NOTES §6
# / §7). Contrast with the diff analyzer's free-text ``affected_topics``: these
# are typed, ranged, basis/period-qualified, and confidence-gated.
EXTRACTED_FIGURES_DDL = """
CREATE TABLE IF NOT EXISTS extracted_figures (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    accession              TEXT NOT NULL REFERENCES filings_seen(accession),
    cik                    TEXT NOT NULL,
    metric                 TEXT NOT NULL,
    scope                  TEXT NOT NULL,      -- total | segment (only 'total' drives a model input)
    period                 TEXT NOT NULL,
    low                    REAL,
    high                   REAL,
    unit                   TEXT NOT NULL,
    basis                  TEXT NOT NULL,
    is_reaffirmed          INTEGER NOT NULL,
    confidence             REAL NOT NULL,
    context                TEXT,
    review_status          TEXT NOT NULL,      -- trigger_eligible | manual_review
    delta_direction        TEXT,               -- raised | lowered | reaffirmed | initiated
    delta_prior_accession  TEXT REFERENCES filings_seen(accession),
    prompt_version         TEXT NOT NULL,
    parser_version         TEXT NOT NULL,
    extracted_at           TIMESTAMP NOT NULL,
    eval_run_id            TEXT,
    UNIQUE (accession, metric, scope, period, basis)
);
CREATE INDEX IF NOT EXISTS idx_extracted_figures_cik_metric
    ON extracted_figures (cik, metric, period);
"""

# Subsystem 7 valuation history — intentionally APPEND-ONLY (the deliberate
# break from the project's latest-state storage; that is what makes the
# before/after revaluation story possible). One row per revaluation.
DCF_VALUATIONS_DDL = """
CREATE TABLE IF NOT EXISTS dcf_valuations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cik                 TEXT NOT NULL,
    run_reason          TEXT NOT NULL,
    trigger_accession   TEXT REFERENCES filings_seen(accession),
    wacc                REAL NOT NULL,
    terminal_growth     REAL NOT NULL,
    assumptions_json    TEXT NOT NULL,
    per_share_bear      REAL NOT NULL,
    per_share_base      REAL NOT NULL,
    per_share_bull      REAL NOT NULL,
    sensitivity_json    TEXT,
    reference_price     REAL,
    reference_price_asof DATE,
    model_version       TEXT NOT NULL,
    valued_at           TIMESTAMP NOT NULL,
    eval_run_id         TEXT
);
CREATE INDEX IF NOT EXISTS idx_dcf_valuations_cik_valued
    ON dcf_valuations (cik, valued_at);
"""

# Audit trail: which real number moved which model input for a given
# revaluation. Empty for a plain quarterly refresh with no input change.
VALUATION_INPUT_LINKS_DDL = """
CREATE TABLE IF NOT EXISTS valuation_input_links (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    valuation_id  INTEGER NOT NULL REFERENCES dcf_valuations(id),
    input_name    TEXT NOT NULL,
    old_value     REAL,
    new_value     REAL,
    source        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_valuation_input_links_valuation
    ON valuation_input_links (valuation_id);
"""

# Subsystem 7 — marks 8-K accessions the guidance extractor has processed
# (mirrors correlator_runs), so no-guidance releases aren't re-processed each
# cycle. A row means "guidance extraction ran against this 8-K exactly once."
GUIDANCE_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS guidance_runs (
    accession      TEXT PRIMARY KEY REFERENCES filings_seen(accession),
    ran_at         TIMESTAMP NOT NULL,
    is_earnings    INTEGER NOT NULL,   -- had item 2.02 + an EX-99 exhibit
    has_guidance   INTEGER NOT NULL,
    figures_found  INTEGER NOT NULL
);
"""

# Eval harness scorecard (ARCHITECTURE.md §10).
EVAL_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS eval_runs (
    id                  TEXT PRIMARY KEY,
    event_id            TEXT NOT NULL,
    ran_at              TIMESTAMP NOT NULL,
    prompt_versions     TEXT,
    binary_result       INTEGER,
    judge_result        TEXT,
    graded_pass         INTEGER NOT NULL,
    subsystems_tested   TEXT,
    notes               TEXT
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_event_id ON eval_runs (event_id, ran_at);
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
    conn.executescript(DIFF_RESULTS_DDL)
    conn.executescript(FLAGGED_EVENTS_DDL)
    conn.executescript(CORRELATOR_RUNS_DDL)
    conn.executescript(XBRL_FACTS_DDL)
    conn.executescript(EXTRACTED_FIGURES_DDL)
    conn.executescript(DCF_VALUATIONS_DDL)
    conn.executescript(VALUATION_INPUT_LINKS_DDL)
    conn.executescript(GUIDANCE_RUNS_DDL)
    conn.executescript(EVAL_RUNS_DDL)


def seed_watchlist_from_yaml(conn: sqlite3.Connection, path: str | Path) -> int:
    """Seed the ``watchlist`` table from ``config/watchlist.yaml``.

    Idempotent: existing rows (by CIK) are not overwritten. Returns the number
    of new rows inserted.
    """
    with Path(path).open(encoding="utf-8") as f:
        entries = yaml.safe_load(f)
    now = datetime.datetime.now(datetime.UTC).isoformat()
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
