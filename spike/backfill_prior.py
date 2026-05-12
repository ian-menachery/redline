"""Phase 1 Subsystem 3 one-shot: backfill PLTR's Q3 2025 10-Q as the
prior-period filing so the analyzer has a pair to diff against.

The poller's first-run logic seeded one filing per ticker — for PLTR
that's Q1 2026 (`0001321655-26-000028`, filed 2026-05-05). To verify
the diff pipeline against real EDGAR + real OpenAI, we need the prior
10-Q (Q3 2025) in `filings_content` too.

This script inserts the filings_seen row only. Then run the fetcher
to parse it (`python -m redline.fetcher --once`) and finally the
analyzer (`python -m redline.diff.analyzer --once`).
"""
from __future__ import annotations

import datetime
import sys

import edgar

from redline.config import RedlineConfig
from redline.storage.db import open_db
from redline.storage.schema import init_full_schema

PRIOR_ACCESSION = "0001321655-25-000131"  # PLTR Q3 2025 10-Q

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

cfg = RedlineConfig.from_toml()
edgar.set_identity(cfg.poller.edgar_user_agent)

print(f"Looking up {PRIOR_ACCESSION}...")
f = edgar.find(PRIOR_ACCESSION)
cik = str(f.cik).zfill(10)
print(f"  cik={cik}  form={f.form}  filed={f.filing_date}  period={f.period_of_report}")

with open_db(cfg.storage.db_path) as conn:
    init_full_schema(conn)
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO filings_seen (
            accession, cik, filing_type, period_end, filed_at, status,
            retry_count, discovered_at
        ) VALUES (?, ?, ?, ?, ?, 'fetched', 0, ?)
        """,
        (
            PRIOR_ACCESSION, cik, f.form,
            str(getattr(f, "period_of_report", None)),
            str(f.filing_date),
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
        ),
    )
    if cur.rowcount > 0:
        print(f"Inserted {PRIOR_ACCESSION} with status='fetched'.")
    else:
        print(f"{PRIOR_ACCESSION} already present; no insert needed.")

print("Next: python -m redline.fetcher --once   then   python -m redline.diff.analyzer --once")
