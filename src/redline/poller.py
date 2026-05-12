"""EDGAR poller — detects new filings on the watchlist.

Per ``ARCHITECTURE.md`` §2: 15-minute cadence, descriptive User-Agent,
``edgartools`` handles internal rate-limiting and retries (see NOTES §4).
This module exposes ``run_once(config, db)`` as the testable unit; the
``__main__`` thin loop is a local-dev convenience only.

First-run behavior per Phase-1 D2: when no rows exist for a CIK in
``filings_seen``, fetch only the most-recent filing across the tracked forms
so the dashboard has immediate content without a history backfill. Steady
state thereafter: enumerate the recent-filings window, ``INSERT OR IGNORE``
any accession not already known.
"""
from __future__ import annotations

import argparse
import datetime
import logging
import sqlite3
import sys
import time
from pathlib import Path

import edgar

from redline.config import RedlineConfig

_LOG = logging.getLogger(__name__)

# Forms the poller tracks. See CLAUDE.md §1 / ARCHITECTURE.md §3.
TRACKED_FORMS: list[str] = ["10-K", "10-Q", "8-K", "4"]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _known_accessions(conn: sqlite3.Connection, cik: str) -> set[str]:
    rows = conn.execute(
        "SELECT accession FROM filings_seen WHERE cik = ?", (cik,)
    ).fetchall()
    return {r["accession"] for r in rows}


def _cik_has_any(conn: sqlite3.Connection, cik: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM filings_seen WHERE cik = ? LIMIT 1", (cik,)
    ).fetchone()
    return row is not None


def _insert_filing(
    conn: sqlite3.Connection,
    *,
    accession: str,
    cik: str,
    filing_type: str,
    period_end: str | None,
    filed_at: str,
) -> bool:
    """Insert a row into ``filings_seen``; return True if inserted, False if
    the accession was already present (the PRIMARY KEY clause makes the
    INSERT OR IGNORE idempotent)."""
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO filings_seen (
            accession, cik, filing_type, period_end, filed_at,
            status, retry_count, discovered_at
        ) VALUES (?, ?, ?, ?, ?, 'fetched', 0, ?)
        """,
        (accession, cik, filing_type, period_end, filed_at, _now_iso()),
    )
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def _candidates_for_cik(filings, *, first_run: bool) -> list:
    """Pick the filings to evaluate from the edgartools result set.

    First run: just the most recent one (D2). Steady state: take the whole
    page; ``_known_accessions`` filters out the ones we already have.
    """
    if first_run:
        latest = filings.latest(1) if hasattr(filings, "latest") else None
        if latest is None:
            return []
        # `.latest(n)` returns either a single filing or a list; normalize.
        if hasattr(latest, "__iter__") and not hasattr(latest, "accession_no"):
            return list(latest)
        return [latest]
    return list(filings)


def run_once(config: RedlineConfig, conn: sqlite3.Connection) -> dict:
    """One poll cycle across the watchlist.

    Returns a summary dict suitable for logging; persists new filings to
    ``filings_seen`` with ``status='fetched'``.
    """
    edgar.set_identity(config.poller.edgar_user_agent)

    watchlist_rows = conn.execute(
        "SELECT cik, ticker FROM watchlist ORDER BY ticker"
    ).fetchall()

    inserted_total = 0
    per_ticker: list[dict] = []
    for row in watchlist_rows:
        cik = row["cik"]
        ticker = row["ticker"]
        try:
            filings = edgar.Company(ticker).get_filings(form=TRACKED_FORMS)
        except Exception as e:
            _LOG.warning("Poll error for %s (%s): %s: %s", ticker, cik, type(e).__name__, e)
            per_ticker.append(
                {"ticker": ticker, "cik": cik, "error": f"{type(e).__name__}: {e}"}
            )
            continue

        first_run = not _cik_has_any(conn, cik)
        candidates = _candidates_for_cik(filings, first_run=first_run)

        known = _known_accessions(conn, cik)
        inserted_here = 0
        for f in candidates:
            acc = getattr(f, "accession_no", None)
            filed_at = getattr(f, "filing_date", None)
            form = getattr(f, "form", None)
            if not acc or not filed_at or not form:
                continue
            if acc in known:
                continue
            period_end = getattr(f, "period_of_report", None)
            if _insert_filing(
                conn,
                accession=acc, cik=cik, filing_type=form,
                period_end=str(period_end) if period_end else None,
                filed_at=str(filed_at),
            ):
                inserted_here += 1
                inserted_total += 1

        per_ticker.append({
            "ticker": ticker, "cik": cik,
            "first_run": first_run,
            "candidates": len(candidates),
            "inserted": inserted_here,
        })

    return {
        "inserted_total": inserted_total,
        "tickers_polled": sum(1 for t in per_ticker if "error" not in t),
        "tickers_errored": sum(1 for t in per_ticker if "error" in t),
        "per_ticker": per_ticker,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="EDGAR poller for redline.")
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single poll cycle and exit. Otherwise loops every cadence_seconds.",
    )
    parser.add_argument(
        "--watchlist", default="config/watchlist.yaml",
        help="Path to watchlist.yaml (seeded idempotently before polling).",
    )
    parser.add_argument(
        "--settings", default="config/settings.toml",
        help="Path to settings.toml.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = RedlineConfig.from_toml(args.settings)

    from redline.storage.db import open_db
    from redline.storage.schema import init_full_schema, seed_watchlist_from_yaml

    with open_db(config.storage.db_path) as conn:
        init_full_schema(conn)
        seeded = seed_watchlist_from_yaml(conn, Path(args.watchlist))
        if seeded:
            _LOG.info("Seeded %d new watchlist rows from %s", seeded, args.watchlist)

        if args.once:
            summary = run_once(config, conn)
            _LOG.info("cycle: %s", summary)
            return 0

        # Loop mode (local dev). For hosted, drive run_once via cron/systemd.
        cadence = config.poller.cadence_seconds
        _LOG.info("Loop mode: cadence=%ds", cadence)
        while True:
            try:
                summary = run_once(config, conn)
                _LOG.info("cycle: %s", summary)
            except KeyboardInterrupt:
                _LOG.info("Interrupted; exiting.")
                return 0
            except Exception as e:
                _LOG.exception("Unhandled error in run_once: %s", e)
            time.sleep(cadence)


if __name__ == "__main__":
    sys.exit(main())
