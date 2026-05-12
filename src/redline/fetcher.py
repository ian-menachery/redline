"""Filing fetcher + parser (Subsystem 2).

Per ARCHITECTURE.md §3. For each pending row in ``filings_seen``:

- Pull the filing via ``edgartools``
- Extract structured sections (10-K/10-Q items, 8-K item list, Form 4
  transactions)
- Persist to ``filings_content`` (and ``form4_transactions`` for Form 4s)
- Transition ``status`` to ``parsed``

Status transitions (ARCHITECTURE.md §7):
- ``fetched`` -> ``parsed`` on success
- ``fetched`` -> ``parse_failed`` on exception (with ``failure_reason``,
  ``last_attempted``, ``retry_count`` incremented)
- ``parse_failed`` rows are eligible for retry on the next ``run_once`` if
  the last attempt was over an hour ago and ``retry_count < 3``
- After 3 failures: ``failed_permanent``

Section extraction patterns come from the Phase 0.5 spike findings in
``NOTES.md`` §5 — most importantly, ``TenQ.get_item_with_part(part, item)``
takes the part FIRST. The reverse argument order returns ``None`` silently.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import sqlite3
import zlib
from typing import Any

import edgar

from redline.config import RedlineConfig

_LOG = logging.getLogger(__name__)

PARSER_VERSION = "v1"
MAX_RETRIES = 3
RETRY_AFTER_HOURS = 1

# (label, part, item) per ARCHITECTURE.md §3. Patterns confirmed by spike.
SECTION_SPEC_10K: list[tuple[str, str, str]] = [
    ("mdna", "Part II", "Item 7"),
    ("risk_factors", "Part I", "Item 1A"),
    ("legal", "Part I", "Item 3"),
    ("qdmr", "Part II", "Item 7A"),
]
SECTION_SPEC_10Q: list[tuple[str, str, str]] = [
    ("mdna", "Part I", "Item 2"),
    ("risk_factors", "Part II", "Item 1A"),
    ("legal", "Part II", "Item 1"),
    ("qdmr", "Part I", "Item 3"),
]

# 10b5-1 detection — see NOTES.md §3.1 finding that regex hit-rates vary
# sharply by filer. Phase 1 uses this as a "signal, not decision rule."
_TEN_B5_1_RE = re.compile(r"\b10b5[-_ ]?1\b", re.IGNORECASE)
_PLAN_DATE_RE = re.compile(
    r"(?:plan\s+adopted|adopted\s+on|entered\s+into\s+on)\s+"
    r"([A-Z][a-z]+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _maybe_call(value: Any) -> Any:
    """edgartools sometimes exposes things as attributes, sometimes methods.

    Probe defensively: if it's callable, call it; otherwise return as-is.
    """
    if value is None:
        return None
    if callable(value):
        try:
            return value()
        except Exception:
            return None
    return value


def _get_raw_content(filing) -> bytes | None:
    """Best-effort raw filing content for re-parse cache. Prefer markdown
    (text form, easier to re-parse) over HTML; fall back to ``.text``."""
    for attr in ("markdown", "html", "text"):
        try:
            value = _maybe_call(getattr(filing, attr, None))
            if isinstance(value, str) and value:
                return zlib.compress(value.encode("utf-8"))
            if isinstance(value, bytes) and value:
                return zlib.compress(value)
        except Exception:
            continue
    return None


def _section_text(obj, part: str, item: str) -> str | None:
    try:
        text = obj.get_item_with_part(part, item)
    except Exception:
        return None
    if not text:
        return None
    if not isinstance(text, str):
        text = str(text)
    return text if text.strip() else None


# ---------------------------------------------------------------------------
# Per-type extractors
# ---------------------------------------------------------------------------

def _extract_periodic(obj, spec: list[tuple[str, str, str]]) -> tuple[dict, dict]:
    """10-K / 10-Q section extraction. Returns (sections, is_empty)."""
    sections: dict[str, str | None] = {}
    is_empty: dict[str, bool] = {}
    for label, part, item in spec:
        text = _section_text(obj, part, item)
        sections[label] = text
        is_empty[label] = text is None
    return sections, is_empty


def _extract_8k(obj) -> tuple[dict, dict]:
    """8-K extraction. Stores the item-number list + optional body text.

    Per-item text extraction via ``chunked_document.chunks_for_item`` is a
    best-effort attempt; if unavailable, fall back to the full filing text.
    """
    items_list = list(getattr(obj, "items", []) or [])

    per_item: dict[str, str | None] = {}
    chunked = getattr(obj, "chunked_document", None)
    chunks_for_item = getattr(chunked, "chunks_for_item", None) if chunked else None

    for item_name in items_list:
        text: str | None = None
        if callable(chunks_for_item):
            try:
                chunks = chunks_for_item(item_name)
                if chunks:
                    parts = []
                    for c in chunks:
                        s = str(c).strip()
                        if s:
                            parts.append(s)
                    text = "\n\n".join(parts) if parts else None
            except Exception:
                text = None
        per_item[item_name] = text

    if not per_item or all(v is None for v in per_item.values()):
        full = _maybe_call(getattr(obj, "text", None))
        if isinstance(full, str) and full.strip():
            per_item["_full"] = full

    is_empty = {item: (text is None) for item, text in per_item.items()}
    return {"items_list": items_list, "items": per_item}, is_empty


def _detect_10b5_1(footnotes_text: str) -> tuple[bool | None, str | None]:
    """Best-effort 10b5-1 detection from footnotes free text.

    Returns ``(is_10b5_1, plan_adopted_date_iso)``. ``is_10b5_1=None`` when
    we couldn't determine either way (no footnote text at all).
    """
    if not footnotes_text:
        return None, None
    if not _TEN_B5_1_RE.search(footnotes_text):
        return False, None
    m = _PLAN_DATE_RE.search(footnotes_text)
    if not m:
        return True, None
    raw = m.group(1)
    # Normalize "Month DD, YYYY" to ISO; leave ISO alone
    try:
        if re.match(r"\d{4}-\d{2}-\d{2}", raw):
            return True, raw
        dt = datetime.datetime.strptime(raw.replace(",", ""), "%B %d %Y")
        return True, dt.date().isoformat()
    except Exception:
        return True, None


def _extract_form4(obj) -> tuple[dict, dict, list[dict]]:
    """Form 4 extraction. Returns (sections_payload, is_empty, tx_rows).

    ``tx_rows`` is a list of dicts ready to insert into ``form4_transactions``.
    The sections payload mirrors the structured surface for the dashboard.
    """
    footnotes_obj = getattr(obj, "footnotes", None)
    footnotes_text = str(footnotes_obj) if footnotes_obj else ""
    is_10b5_1, plan_date = _detect_10b5_1(footnotes_text)

    remarks = getattr(obj, "remarks", None)
    remarks_text = str(remarks) if remarks else ""

    insider_name = getattr(obj, "insider_name", None) or ""

    tx_rows: list[dict] = []
    try:
        df = obj.to_dataframe()
    except Exception:
        df = None

    if df is not None and hasattr(df, "iterrows") and len(df) > 0:
        for _, row in df.iterrows():
            code = str(row.get("Code") or "").strip()
            shares_val = row.get("Shares")
            if shares_val is None or (hasattr(shares_val, "__float__") and _isnan(float(shares_val))):
                continue
            try:
                shares = float(shares_val)
            except (TypeError, ValueError):
                continue
            if not code:
                continue
            date_val = row.get("Date")
            trade_date = str(date_val)[:10] if date_val is not None else None
            if not trade_date:
                continue
            price_val = row.get("Price")
            try:
                price = float(price_val) if price_val is not None and not _isnan(float(price_val)) else None
            except (TypeError, ValueError):
                price = None
            tx_rows.append({
                "insider_name": str(row.get("Insider") or insider_name),
                "trade_date": trade_date,
                "code": code,
                "shares": shares,
                "price": price,
                "ownership": None,  # Phase 1 best-effort; Phase 2 refines
                "is_10b5_1": is_10b5_1,
                "plan_adopted_date": plan_date,
                "explanation": str(row.get("Description") or ""),
            })

    sections = {
        "insider_name": insider_name,
        "transaction_count": len(tx_rows),
        "footnotes": footnotes_text,
        "remarks": remarks_text,
        "is_10b5_1": is_10b5_1,
        "plan_adopted_date": plan_date,
    }
    is_empty = {"transactions": len(tx_rows) == 0}
    return sections, is_empty, tx_rows


def _isnan(x: float) -> bool:
    """``math.isnan`` without importing math just for one call."""
    return x != x


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _insert_content(
    conn: sqlite3.Connection,
    *, accession: str, raw: bytes | None,
    sections: dict, is_empty: dict,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO filings_content (
            accession, raw_content, sections, is_empty,
            parser_version, extracted_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            accession, raw,
            json.dumps(sections, default=str),
            json.dumps(is_empty),
            PARSER_VERSION, _now_iso(),
        ),
    )


def _insert_form4_transactions(
    conn: sqlite3.Connection, *, accession: str, cik: str, tx_rows: list[dict],
) -> None:
    # Idempotent re-parse: clear existing transactions for this accession first.
    conn.execute("DELETE FROM form4_transactions WHERE accession = ?", (accession,))
    for tx in tx_rows:
        conn.execute(
            """
            INSERT INTO form4_transactions (
                accession, cik, insider_cik, insider_name, trade_date,
                code, shares, price, ownership, is_10b5_1,
                plan_adopted_date, explanation
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                accession, cik, None, tx["insider_name"], tx["trade_date"],
                tx["code"], tx["shares"], tx["price"], tx["ownership"],
                int(tx["is_10b5_1"]) if isinstance(tx["is_10b5_1"], bool) else None,
                tx["plan_adopted_date"], tx["explanation"],
            ),
        )


def _mark_parsed(conn: sqlite3.Connection, accession: str) -> None:
    conn.execute(
        """
        UPDATE filings_seen SET
            status = 'parsed',
            last_attempted = ?,
            failure_reason = NULL
        WHERE accession = ?
        """,
        (_now_iso(), accession),
    )


def _mark_failed(
    conn: sqlite3.Connection, *, accession: str, retry_count: int, reason: str,
) -> None:
    new_count = retry_count + 1
    new_status = "failed_permanent" if new_count >= MAX_RETRIES else "parse_failed"
    conn.execute(
        """
        UPDATE filings_seen SET
            status = ?,
            last_attempted = ?,
            failure_reason = ?,
            retry_count = ?
        WHERE accession = ?
        """,
        (new_status, _now_iso(), reason[:512], new_count, accession),
    )


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def _pending_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rows eligible for fetch + parse.

    Picks up status='fetched' immediately. parse_failed rows are eligible
    only when retry_count < MAX_RETRIES and last_attempted is older than
    RETRY_AFTER_HOURS, matching the retry semantics in ARCHITECTURE.md §7.
    """
    return conn.execute(
        f"""
        SELECT accession, cik, filing_type, retry_count
        FROM filings_seen
        WHERE
            status = 'fetched'
            OR (
                status = 'parse_failed'
                AND retry_count < {MAX_RETRIES}
                AND (
                    last_attempted IS NULL
                    OR datetime(last_attempted) < datetime('now', '-{RETRY_AFTER_HOURS} hour')
                )
            )
        """,
    ).fetchall()


def _parse_one(filing, filing_type: str) -> tuple[dict, dict, list[dict]]:
    """Dispatch by filing type. Returns (sections, is_empty, tx_rows)."""
    obj = filing.obj()
    if filing_type == "10-K":
        sections, is_empty = _extract_periodic(obj, SECTION_SPEC_10K)
        return sections, is_empty, []
    if filing_type == "10-Q":
        sections, is_empty = _extract_periodic(obj, SECTION_SPEC_10Q)
        return sections, is_empty, []
    if filing_type == "8-K":
        sections, is_empty = _extract_8k(obj)
        return sections, is_empty, []
    if filing_type == "4":
        return _extract_form4(obj)
    raise ValueError(f"Unsupported filing_type: {filing_type!r}")


def run_once(config: RedlineConfig, conn: sqlite3.Connection) -> dict:
    """One fetch+parse pass over pending filings.

    Returns a summary dict for logging.
    """
    edgar.set_identity(config.poller.edgar_user_agent)

    rows = _pending_rows(conn)
    parsed = 0
    failed = 0
    permanent_failures = 0
    per_filing: list[dict] = []

    for row in rows:
        accession = row["accession"]
        cik = row["cik"]
        filing_type = row["filing_type"]
        retry_count = row["retry_count"]
        try:
            filing = edgar.find(accession)
            sections, is_empty, tx_rows = _parse_one(filing, filing_type)
            raw = _get_raw_content(filing)
            _insert_content(
                conn, accession=accession, raw=raw,
                sections=sections, is_empty=is_empty,
            )
            if filing_type == "4":
                _insert_form4_transactions(
                    conn, accession=accession, cik=cik, tx_rows=tx_rows,
                )
            _mark_parsed(conn, accession)
            parsed += 1
            per_filing.append({
                "accession": accession, "filing_type": filing_type,
                "status": "parsed",
                "sections_present": [k for k, v in is_empty.items() if not v],
                "tx_inserted": len(tx_rows),
            })
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            _LOG.warning("Parse failure for %s (%s): %s", accession, filing_type, reason)
            _mark_failed(
                conn, accession=accession,
                retry_count=retry_count, reason=reason,
            )
            failed += 1
            if retry_count + 1 >= MAX_RETRIES:
                permanent_failures += 1
            per_filing.append({
                "accession": accession, "filing_type": filing_type,
                "status": "parse_failed", "error": reason,
            })

    return {
        "considered": len(rows),
        "parsed": parsed,
        "failed": failed,
        "permanent_failures": permanent_failures,
        "per_filing": per_filing,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="EDGAR fetcher + parser for redline.")
    parser.add_argument("--once", action="store_true",
                        help="Run a single fetch+parse pass and exit.")
    parser.add_argument("--settings", default="config/settings.toml",
                        help="Path to settings.toml.")
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
    import sys
    sys.exit(main())
