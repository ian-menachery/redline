"""Generate the curated read-only snapshot for the hosted DCF valuation demo.

Unlike ``snapshot_demo_db.py`` (which ``VACUUM INTO``-copies the whole disclosure
DB), the valuation demo must show ONLY presentable data, so this rebuilds a fresh
``data/valuation_demo.db`` containing:

  - ``watchlist`` (all 8 names — used only for labels)
  - the latest DCF valuation for the two DCF-credible names (VRTX, ULTA)
  - the NET guidance "mechanism demonstration" pair: the earliest baseline
    (``run_reason='new_filing'``) and the latest ``guidance_change`` row, plus
    that row's ``valuation_input_links`` and the triggering ``filings_seen`` row

It EXCLUDES everything not fit to show a recruiter: ``llm_call_log`` / spend,
``xbrl_facts``, ``extracted_figures``, ``flagged_events``, and the
high-multiple / negative-FCF valuation cards (NET/PLTR/MRNA are surfaced by the
dashboard as "monitored, not DCF-valued", from watchlist labels only).

Rows are selected by CRITERIA (not hardcoded ids) so this stays correct after a
future ``revalue --once --force``. The dashboard (``valuation_app.py``) reads
``data/valuation_demo.db`` by default (no secret needed); the file is committed
via a ``!data/valuation_demo.db`` .gitignore exception.

Run before pushing a refreshed snapshot:
    python scripts/snapshot_valuation_demo.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from redline.storage.schema import init_full_schema

SRC = Path("data/redline.db")
DST = Path("data/valuation_demo.db")

VALUED = ("VRTX", "ULTA")          # DCF-credible names shown as valuation cards
MECHANISM_TICKER = "NET"           # guidance before/after mechanism demonstration


def _cik(src: sqlite3.Connection, ticker: str) -> str | None:
    row = src.execute("SELECT cik FROM watchlist WHERE ticker = ?", (ticker,)).fetchone()
    return row[0] if row else None


def _keep_valuation_ids(src: sqlite3.Connection) -> list[int]:
    """Valuation rows to include, chosen by criteria (robust to id churn)."""
    ids: list[int] = []
    for ticker in VALUED:
        cik = _cik(src, ticker)
        if cik is None:
            continue
        row = src.execute(
            "SELECT MAX(id) FROM dcf_valuations WHERE cik = ?", (cik,)).fetchone()
        if row and row[0] is not None:
            ids.append(row[0])   # latest valuation for the credible name

    net = _cik(src, MECHANISM_TICKER)
    if net is not None:
        base = src.execute(
            "SELECT id FROM dcf_valuations WHERE cik = ? AND run_reason = 'new_filing' "
            "ORDER BY id LIMIT 1", (net,)).fetchone()
        guid = src.execute(
            "SELECT id FROM dcf_valuations WHERE cik = ? AND run_reason = 'guidance_change' "
            "ORDER BY id DESC LIMIT 1", (net,)).fetchone()
        for row in (base, guid):
            if row and row[0] is not None:
                ids.append(row[0])
    return ids


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"source DB not found: {SRC}")

    src = sqlite3.connect(SRC)
    keep = _keep_valuation_ids(src)
    src.close()
    if not keep:
        raise SystemExit("no valuations found to snapshot — run revalue first")

    # Fresh destination in default (rollback) journal mode: no -wal/-shm
    # companion, so the committed snapshot is a single self-contained file.
    for p in (DST, DST.with_suffix(".db-wal"), DST.with_suffix(".db-shm")):
        if p.exists():
            p.unlink()
    dst = sqlite3.connect(DST)
    try:
        dst.execute("PRAGMA foreign_keys = ON")
        init_full_schema(dst)
        dst.execute("ATTACH DATABASE ? AS live", (str(SRC.resolve()),))
        ph = ",".join("?" * len(keep))
        # FK-safe insert order: watchlist -> filings_seen -> dcf_valuations -> links
        dst.execute("INSERT INTO watchlist SELECT * FROM live.watchlist")
        dst.execute(
            f"""INSERT INTO filings_seen SELECT * FROM live.filings_seen
                WHERE accession IN (
                    SELECT DISTINCT trigger_accession FROM live.dcf_valuations
                    WHERE id IN ({ph}) AND trigger_accession IS NOT NULL)""", keep)
        dst.execute(f"INSERT INTO dcf_valuations SELECT * FROM live.dcf_valuations WHERE id IN ({ph})", keep)
        dst.execute(f"INSERT INTO valuation_input_links SELECT * FROM live.valuation_input_links WHERE valuation_id IN ({ph})", keep)
        dst.commit()

        fk = dst.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            raise SystemExit(f"foreign-key violations in snapshot: {fk}")
        dst.execute("DETACH DATABASE live")
        dst.execute("VACUUM")          # compact to a tidy single file
        valued = [r[0] for r in dst.execute(
            """SELECT w.ticker FROM dcf_valuations v JOIN watchlist w ON w.cik = v.cik
               WHERE v.reference_price IS NOT NULL ORDER BY w.ticker""")]
        counts = {t: dst.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("watchlist", "dcf_valuations", "valuation_input_links",
                            "extracted_figures", "xbrl_facts", "llm_call_log")}
    finally:
        dst.close()

    print(f"wrote {DST} ({DST.stat().st_size:,} bytes)")
    print(f"  valuation cards (reference_price set): {valued}")
    print(f"  row counts: {counts}")
    print("  excluded (must be 0): "
          f"extracted_figures={counts['extracted_figures']} "
          f"xbrl_facts={counts['xbrl_facts']} llm_call_log={counts['llm_call_log']}")


if __name__ == "__main__":
    main()
