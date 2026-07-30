"""Guidance-eval registration (Rule R) — mechanical, deterministic selection of
the earnings-8-K accessions the guidance-extraction eval scores.

**Rule R** (locked before scoring, recorded verbatim in the gold file header):

  * **Company set** = ``watchlist`` rows whose ``sector NOT IN
    DCF_EXCLUDED_SECTORS`` — the code's own DCF-eligible filter, not a hand-list.
  * **Qualifying filing** = a plain ``8-K`` (form exactly ``8-K``, **not**
    ``8-K/A`` — an amendment re-states an existing earnings event, not a new one)
    with item 2.02 AND an EX-99.x exhibit, per the flags persisted in
    ``earnings_8k_qualification`` at backfill time.
  * **Selection** = the ``per_company`` (=2) most recent qualifying filings per
    company by ``filed_at`` descending, ties broken by ``accession`` ascending,
    as of the lock date.

Selection is a **pure function of committed DB state — no network calls.** All
live fetching, and the item-2.02 / EX-99.x evaluation, happens earlier in the
backfill (``scripts/backfill_8ks.py``), which persists the qualification flags
here so two selection runs on the same DB return byte-identical lists.
"""
from __future__ import annotations

import datetime
import sqlite3
from dataclasses import dataclass

from redline.valuation.xbrl import DCF_EXCLUDED_SECTORS

# The panel target: 2 most-recent qualifying earnings 8-Ks per DCF-eligible name.
DEFAULT_PER_COMPANY = 2


@dataclass(frozen=True)
class RegistrationEntry:
    """One selected accession in the registered guidance-eval panel."""

    ticker: str
    cik: str
    accession: str
    filed_at: str
    previously_observed: bool


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def record_qualification(
    conn: sqlite3.Connection,
    *,
    accession: str,
    form: str,
    is_earnings: bool,
    has_ex99: bool,
    checked_at: str | None = None,
) -> None:
    """Persist an 8-K's Rule-R qualification flags (called by the backfill).

    ``form`` is the authoritative EDGAR form ('8-K' vs '8-K/A'); Rule R requires
    a plain '8-K' so an amendment — which re-states an existing earnings event,
    not a new one — never enters the panel. Idempotent: re-checking overwrites."""
    conn.execute(
        """INSERT OR REPLACE INTO earnings_8k_qualification
           (accession, form, is_earnings, has_ex99, checked_at)
           VALUES (?, ?, ?, ?, ?)""",
        (accession, form, int(is_earnings), int(has_ex99), checked_at or _now_iso()),
    )


def _qualifying_rows(
    conn: sqlite3.Connection, *, as_of: str | None = None
) -> list[sqlite3.Row]:
    """All qualifying 8-Ks for DCF-eligible names, ordered so that per ticker the
    first N rows are the N most recent (tie-break ``accession`` ascending).

    ``as_of`` (the lock date) excludes filings filed after it, so the frozen
    panel stays reproducible even if the DB is later backfilled with newer 8-Ks
    — "2 most recent AS OF the lock date", not "2 most recent in the DB now"."""
    placeholders = ",".join("?" for _ in DCF_EXCLUDED_SECTORS)
    as_of_clause = "AND fs.filed_at <= ?" if as_of else ""
    params: tuple = tuple(DCF_EXCLUDED_SECTORS) + ((as_of,) if as_of else ())
    return conn.execute(
        f"""
        SELECT fs.accession, fs.cik, fs.filed_at, w.ticker
        FROM filings_seen fs
        JOIN watchlist w ON w.cik = fs.cik
        JOIN earnings_8k_qualification q ON q.accession = fs.accession
        WHERE fs.filing_type = '8-K'
          AND w.sector NOT IN ({placeholders})
          AND q.form = '8-K'
          AND q.is_earnings = 1
          AND q.has_ex99 = 1
          {as_of_clause}
        ORDER BY w.ticker ASC, fs.filed_at DESC, fs.accession ASC
        """,
        params,
    ).fetchall()


def _is_previously_observed(conn: sqlite3.Connection, accession: str) -> bool:
    """``previously_observed`` is COMPUTED, never hand-assigned: True iff a prior
    run artifact for this accession already exists at lock time — any
    ``extracted_figures`` OR ``guidance_runs`` row. It is never derived from a
    company name. (There is no run-record ambiguity to resolve here; if one ever
    arose, the honest default is True — fail toward disclosure.)"""
    for table in ("extracted_figures", "guidance_runs"):
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE accession = ? LIMIT 1", (accession,)
        ).fetchone()
        if row is not None:
            return True
    return False


def select_registration(
    conn: sqlite3.Connection, *, per_company: int = DEFAULT_PER_COMPANY,
    as_of: str | None = None,
) -> list[RegistrationEntry]:
    """Apply Rule R over committed DB state. Deterministic; performs no network
    call. ``as_of`` (the lock date) freezes the panel — filings filed after it
    are excluded. Undershoot (a name with < ``per_company`` qualifying filings)
    is surfaced honestly — the name simply contributes fewer entries."""
    rows = _qualifying_rows(conn, as_of=as_of)
    by_ticker: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)

    selected: list[RegistrationEntry] = []
    for ticker in sorted(by_ticker):
        for r in by_ticker[ticker][:per_company]:
            selected.append(
                RegistrationEntry(
                    ticker=ticker,
                    cik=r["cik"],
                    accession=r["accession"],
                    filed_at=str(r["filed_at"]),
                    previously_observed=_is_previously_observed(conn, r["accession"]),
                )
            )
    return selected


def manifest_dicts(entries: list[RegistrationEntry], *, locked_at: str) -> list[dict]:
    """Render selected entries into the YAML-serializable manifest shape written
    into ``guidance_labels.yaml`` under ``registration.accessions``."""
    return [
        {
            "ticker": e.ticker,
            "accession": e.accession,
            "filed_at": e.filed_at,
            "locked_at": locked_at,
            "previously_observed": e.previously_observed,
        }
        for e in entries
    ]
