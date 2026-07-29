"""Step 2 (throwaway): targeted backfill of item-2.02 8-Ks (+ light periodic
history) for PLTR, NET, CVNA into filings_seen, so the guidance extractor +
eval have real data. Depth sized to the eval floor (>=15 curated gold figures).

Inserts are idempotent (INSERT OR IGNORE), mirroring
eval/replay.py::_ensure_filings_seen_row. No LLM calls here.
"""
from __future__ import annotations

import datetime
import sys

import edgar

from redline.config import RedlineConfig
from redline.storage.db import open_db
from redline.storage.schema import init_full_schema
from redline.valuation import guidance

edgar.set_identity("Redline menachery.i@northeastern.edu")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NAMES = {"PLTR": "0001321655", "NET": "0001477333", "CVNA": "0001690820"}
N_EARNINGS = 3      # most-recent item-2.02 8-Ks per name
N_PERIODIC = 3      # recent 10-Q/10-K per name (realism; not load-bearing)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _insert(conn, *, accession, cik, form, filed_at) -> int:
    cur = conn.execute(
        """INSERT OR IGNORE INTO filings_seen
           (accession, cik, filing_type, filed_at, status, retry_count, discovered_at)
           VALUES (?, ?, ?, ?, 'fetched', 0, ?)""",
        (accession, cik, form, str(filed_at), _now()),
    )
    return cur.rowcount


def main() -> int:
    cfg = RedlineConfig.from_toml("config/settings.toml")
    with open_db(cfg.storage.db_path) as conn:
        init_full_schema(conn)
        for tk, cik in NAMES.items():
            earnings, periodic = [], []
            for f in edgar.Company(tk).get_filings(form="8-K").head(16):
                if len(earnings) >= N_EARNINGS:
                    break
                if guidance._is_earnings_8k(f):
                    earnings.append(f)
            for f in edgar.Company(tk).get_filings(form=["10-Q", "10-K"]).head(N_PERIODIC):
                periodic.append(f)

            e_ins = sum(_insert(conn, accession=f.accession_no, cik=cik,
                                form="8-K", filed_at=f.filing_date) for f in earnings)
            p_ins = sum(_insert(conn, accession=f.accession_no, cik=cik,
                                form=f.form, filed_at=f.filing_date) for f in periodic)
            print(f"\n{tk}: earnings-8K inserted={e_ins}/{len(earnings)}  "
                  f"periodic inserted={p_ins}/{len(periodic)}")
            for f in earnings:
                print(f"    2.02 8-K  {f.accession_no}  {f.filing_date}")
            for f in periodic:
                print(f"    {f.form:5}    {f.accession_no}  {f.filing_date}")

        pend = conn.execute(
            """SELECT w.ticker, COUNT(*) c FROM filings_seen fs JOIN watchlist w ON w.cik=fs.cik
               WHERE fs.filing_type='8-K' AND fs.accession NOT IN (SELECT accession FROM guidance_runs)
               GROUP BY w.ticker ORDER BY w.ticker""").fetchall()
        print("\npending 8-Ks for guidance extraction (by ticker):",
              {r["ticker"]: r["c"] for r in pend})
    return 0


if __name__ == "__main__":
    sys.exit(main())
