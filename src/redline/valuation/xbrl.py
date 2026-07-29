"""Companyfacts (XBRL) ingestion — Subsystem 7, the reliable revaluation spine.

For each DCF-eligible watchlist company, pull the SEC companyfacts via
``edgar.Company(ticker).get_facts().to_dataframe()`` and upsert into
``xbrl_facts``. This is what makes revaluation happen dependably every quarter,
independent of any narrative signal (Phase 0 found guidance isn't in the MD&A —
NOTES.md §6).

Banks are excluded: an unlevered-FCF DCF is not a meaningful model for a
financial institution, so we don't ingest/serve a DCF base for them (they show
as "not DCF-modeled" on the dashboard).

The facts ``concept`` column is namespaced (``us-gaap:Revenues``); downstream
FCF reconstruction matches on the full namespaced name.
"""
from __future__ import annotations

import argparse
import datetime
import logging
import sqlite3
import sys

import edgar

from redline.config import RedlineConfig

_LOG = logging.getLogger(__name__)

# Sectors whose companies are not modeled with an unlevered-FCF DCF. See the
# 2026-07-27 decision in NOTES.md §6.
DCF_EXCLUDED_SECTORS: frozenset[str] = frozenset({"financials"})

_UPSERT = """
INSERT INTO xbrl_facts (
    cik, concept, label, unit, period_type,
    fiscal_year, fiscal_period, period_start, period_end,
    numeric_value, ingested_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (cik, concept, fiscal_year, fiscal_period, period_start, period_end)
DO UPDATE SET
    numeric_value = excluded.numeric_value,
    label         = excluded.label,
    unit          = excluded.unit,
    period_type   = excluded.period_type,
    ingested_at   = excluded.ingested_at
"""


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def dcf_eligible_companies(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Watchlist rows we build a DCF for (non-financials). Ordered by ticker."""
    placeholders = ",".join("?" for _ in DCF_EXCLUDED_SECTORS)
    return conn.execute(
        f"""
        SELECT cik, ticker, name, sector FROM watchlist
        WHERE sector NOT IN ({placeholders})
        ORDER BY ticker
        """,
        tuple(DCF_EXCLUDED_SECTORS),
    ).fetchall()


def _date_str(value) -> str:
    """Coalesce a date-ish value to ``YYYY-MM-DD`` or '' (never None).

    The UNIQUE key must be NULL-free — SQLite treats NULLs as distinct, which
    would let re-ingest duplicate instant facts (no period_start).
    """
    if value is None:
        return ""
    s = str(value)
    return s[:10] if s else ""


def _rows_from_facts(cik: str, df, ingested_at: str) -> list[tuple]:
    """Project a facts DataFrame to upsert tuples. Skips undated / non-numeric.

    Only rows with a numeric value AND a fiscal_year are kept — the rest can't
    anchor a DCF base and would only bloat the table.
    """
    out: list[tuple] = []
    for row in df.itertuples(index=False):
        numeric = getattr(row, "numeric_value", None)
        fiscal_year = getattr(row, "fiscal_year", None)
        if numeric is None or fiscal_year is None:
            continue
        if numeric != numeric:  # NaN
            continue
        try:
            fy = int(fiscal_year)
        except (TypeError, ValueError):
            continue
        out.append((
            cik,
            str(getattr(row, "concept", "")),
            _none_str(getattr(row, "label", None)),
            _none_str(getattr(row, "unit", None)),
            _none_str(getattr(row, "period_type", None)),
            fy,
            str(getattr(row, "fiscal_period", "") or ""),
            _date_str(getattr(row, "period_start", None)),
            _date_str(getattr(row, "period_end", None)),
            float(numeric),
            ingested_at,
        ))
    return out


def _none_str(value) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s and s.lower() != "nan" else None


def _upsert_facts(conn: sqlite3.Connection, *, cik: str, df) -> int:
    rows = _rows_from_facts(cik, df, _now_iso())
    if not rows:
        return 0
    conn.execute("BEGIN")
    try:
        conn.executemany(_UPSERT, rows)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


def run_once(config: RedlineConfig, conn: sqlite3.Connection) -> dict:
    """One companyfacts refresh over all DCF-eligible watchlist companies."""
    edgar.set_identity(config.poller.edgar_user_agent)

    rows = dcf_eligible_companies(conn)
    per_company: list[dict] = []
    ingested = 0
    failed = 0
    for row in rows:
        ticker = row["ticker"]
        cik = row["cik"]
        try:
            df = edgar.Company(ticker).get_facts().to_dataframe()
            n = _upsert_facts(conn, cik=cik, df=df)
            ingested += 1
            per_company.append({"ticker": ticker, "cik": cik, "facts_upserted": n})
            _LOG.info("xbrl: %s upserted %d facts", ticker, n)
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            _LOG.warning("xbrl ingest failed for %s: %s", ticker, reason)
            failed += 1
            per_company.append({"ticker": ticker, "cik": cik, "error": reason})

    return {
        "considered": len(rows),
        "ingested": ingested,
        "failed": failed,
        "per_company": per_company,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Companyfacts (XBRL) ingestion for redline DCF.")
    parser.add_argument("--once", action="store_true", help="Run a single refresh and exit.")
    parser.add_argument("--settings", default="config/settings.toml")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = RedlineConfig.from_toml(args.settings)
    from redline.storage.db import open_db
    from redline.storage.schema import init_full_schema

    with open_db(config.storage.db_path) as conn:
        init_full_schema(conn)
        summary = run_once(config, conn)
        _LOG.info("cycle: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
