"""Generate a compact, WAL-free snapshot of the live DB for hosted demos.

Uses SQLite's ``VACUUM INTO`` so the snapshot is a single self-contained
file (no ``-wal`` / ``-shm`` companions) and is consistent at the time of
the snapshot. Output: ``data/demo.db``, committed and served by the
hosted Streamlit Cloud deployment when ``REDLINE_DB_PATH=data/demo.db``.

Run before pushing a refreshed snapshot:
    python scripts/snapshot_demo_db.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SRC = Path("data/redline.db")
DST = Path("data/demo.db")


def main() -> None:
    if not SRC.exists():
        raise SystemExit(f"source DB not found: {SRC}")
    if DST.exists():
        DST.unlink()
    conn = sqlite3.connect(SRC)
    try:
        conn.execute(f"VACUUM INTO '{DST.as_posix()}'")
    finally:
        conn.close()
    print(f"wrote {DST} ({DST.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
