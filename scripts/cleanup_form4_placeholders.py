"""One-shot cleanup: remove Form 4 transactions whose insider name is the
issuer's own company name (NOTES.md §11 bug, fixed at ingest in fetcher.py).

Identifies spurious rows by joining `form4_transactions.cik` against
`watchlist.name` and applying ``_is_issuer_placeholder``. Deletes the rows
and any `flagged_events` / `correlator_runs` entries that were produced
from them; status on the affected `filings_seen` rows is reset to
``parsed`` so the correlator picks them up on the next run.
"""
from __future__ import annotations

from redline.config import RedlineConfig
from redline.fetcher import _is_issuer_placeholder
from redline.storage.db import connect


def main() -> None:
    cfg = RedlineConfig.from_toml("config/settings.toml")
    conn = connect(cfg.storage.db_path)
    conn.execute("BEGIN")

    wl = {r["cik"]: r["name"] for r in conn.execute("SELECT cik, name FROM watchlist")}
    rows = conn.execute(
        "SELECT id, accession, cik, insider_name FROM form4_transactions"
    ).fetchall()
    spurious = [r for r in rows if _is_issuer_placeholder(r["insider_name"], wl.get(r["cik"]))]

    print(f"Spurious form4_transactions rows: {len(spurious)} of {len(rows)}")
    if not spurious:
        conn.execute("ROLLBACK")
        return

    affected_accessions = sorted({r["accession"] for r in spurious})
    spurious_ids = [r["id"] for r in spurious]
    placeholders = ",".join("?" * len(spurious_ids))
    conn.execute(
        f"DELETE FROM form4_transactions WHERE id IN ({placeholders})",
        spurious_ids,
    )

    # Reset correlator state for any filing whose Form 4 set just changed.
    # The correlator joins on cik + window, so re-running rebuilds the
    # discretionary set from the cleaned-up form4_transactions.
    affected_ciks = sorted({r["cik"] for r in spurious})
    cik_placeholders = ",".join("?" * len(affected_ciks))
    conn.execute(
        f"""
        DELETE FROM flagged_events
        WHERE accession IN (
            SELECT accession FROM filings_seen WHERE cik IN ({cik_placeholders})
        )
        AND flag_reason = 'correlator_anomaly'
        """,
        affected_ciks,
    )
    conn.execute(
        f"""
        DELETE FROM correlator_runs
        WHERE accession IN (
            SELECT accession FROM filings_seen WHERE cik IN ({cik_placeholders})
        )
        """,
        affected_ciks,
    )
    conn.execute(
        f"""
        UPDATE filings_seen SET status = 'parsed'
        WHERE cik IN ({cik_placeholders}) AND status = 'flagged'
          AND filing_type IN ('10-K', '10-Q', '8-K')
        """,
        affected_ciks,
    )

    conn.execute("COMMIT")
    print(f"Deleted {len(spurious)} placeholder rows across "
          f"{len(affected_accessions)} accessions.")
    print(f"Reset correlator state for ciks: {affected_ciks}")


if __name__ == "__main__":
    main()
