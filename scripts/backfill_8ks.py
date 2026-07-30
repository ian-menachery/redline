"""Backfill historical earnings 8-Ks for the DCF-eligible watchlist names.

The live poller only captures filings going forward, so ``filings_seen`` lacks
the historical earnings 8-Ks the guidance-eval registration (Rule R) needs. This
one-off script fills that gap:

  1. For each DCF-eligible name (``sector NOT IN DCF_EXCLUDED_SECTORS``), pull its
     8-Ks over a trailing window and INSERT OR IGNORE them into ``filings_seen``
     via the poller's own ``_insert_filing`` (same persist path as live polling).
  2. For each 8-K in the window, evaluate the two Rule-R qualification predicates
     — ``_is_earnings_8k`` (item 2.02) and ``fetch_guidance_exhibit`` presence
     (an EX-99.x exhibit) — and persist them to ``earnings_8k_qualification``.

This is the **only network step** of the guidance-eval expansion. It uses **no
LLM** (the predicates read filing metadata + attachment types, not content
extraction). After it runs, ``redline.valuation.guidance_registration`` selects
the panel as a pure, deterministic DB read.

    python scripts/backfill_8ks.py --months 15
    python scripts/backfill_8ks.py --db-path data/redline.db --months 18
"""
from __future__ import annotations

import argparse
import datetime
import logging
import sys

import edgar

from redline.config import RedlineConfig
from redline.poller import _insert_filing
from redline.storage.db import open_db
from redline.storage.schema import init_full_schema, seed_watchlist_from_yaml
from redline.valuation.guidance import _is_earnings_8k
from redline.valuation.guidance_registration import record_qualification
from redline.valuation.xbrl import DCF_EXCLUDED_SECTORS

_LOG = logging.getLogger(__name__)


def _has_ex99(filing) -> bool:
    """True iff the filing carries an EX-99.x exhibit — checked by attachment
    document_type only, WITHOUT downloading the (large) exhibit text. This is the
    Rule-R qualification predicate; the extractor later reads the text itself. A
    lighter check than ``fetch_guidance_exhibit`` (which downloads ~36-113k chars
    per filing) — the distinction (an EX-99.x that is present but text-empty) does
    not occur in practice for earnings releases."""
    try:
        atts = filing.attachments
    except Exception:
        return False
    for a in atts:
        if str(getattr(a, "document_type", "") or "").upper().startswith("EX-99"):
            return True
    return False


def _as_date(value: object) -> datetime.date | None:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _dcf_eligible(conn) -> list[tuple[str, str]]:
    placeholders = ",".join("?" for _ in DCF_EXCLUDED_SECTORS)
    rows = conn.execute(
        f"SELECT cik, ticker FROM watchlist WHERE sector NOT IN ({placeholders}) "
        f"ORDER BY ticker",
        tuple(DCF_EXCLUDED_SECTORS),
    ).fetchall()
    return [(r["cik"], r["ticker"]) for r in rows]


def backfill(config: RedlineConfig, conn, *, months: int) -> dict:
    """Backfill + qualify 8-Ks over a trailing ``months`` window. Returns a
    per-ticker summary. Network-bound; no LLM."""
    edgar.set_identity(config.poller.edgar_user_agent)
    cutoff = datetime.date.today() - datetime.timedelta(days=round(months * 30.5))

    per_ticker: list[dict] = []
    for cik, ticker in _dcf_eligible(conn):
        inserted = qualified = considered = 0
        try:
            filings = edgar.Company(ticker).get_filings(form="8-K")
        except Exception as e:  # noqa: BLE001 — network/lookup failure is per-ticker
            _LOG.warning("8-K lookup failed for %s: %s: %s", ticker, type(e).__name__, e)
            per_ticker.append({"ticker": ticker, "error": f"{type(e).__name__}: {e}"})
            continue

        for f in filings:
            try:
                filed = _as_date(getattr(f, "filing_date", None))
                acc = getattr(f, "accession_no", None)
                form = str(getattr(f, "form", "") or "")
                if not acc or filed is None or filed < cutoff:
                    continue
                considered += 1
                period_end = getattr(f, "period_of_report", None)
                # Record the AUTHORITATIVE form ('8-K' vs '8-K/A'), not a
                # hardcoded string — Rule R must be able to drop amendments.
                if _insert_filing(
                    conn, accession=acc, cik=cik, filing_type=form or "8-K",
                    period_end=str(period_end) if period_end else None,
                    filed_at=str(filed),
                ):
                    inserted += 1
                is_earnings = _is_earnings_8k(f)
                has_ex99 = is_earnings and _has_ex99(f)
                record_qualification(conn, accession=acc, form=form or "8-K",
                                     is_earnings=is_earnings, has_ex99=has_ex99)
                qualified += int(has_ex99 and form == "8-K")
                conn.commit()  # commit per filing so a later timeout can't lose progress
            except Exception as e:  # noqa: BLE001 — per-filing network/parse failure
                _LOG.warning("skip filing (%s): %s: %s",
                             getattr(f, "accession_no", "?"), type(e).__name__, e)
                continue

        conn.commit()
        per_ticker.append({
            "ticker": ticker, "considered": considered,
            "inserted": inserted, "qualifying": qualified,
        })
        _LOG.info("%s: considered=%d inserted=%d qualifying=%d",
                  ticker, considered, inserted, qualified)

    return {"cutoff": cutoff.isoformat(), "per_ticker": per_ticker}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill + qualify earnings 8-Ks.")
    parser.add_argument("--settings", default="config/settings.toml")
    parser.add_argument("--db-path", help="Override settings.storage.db_path.")
    parser.add_argument("--months", type=int, default=15,
                        help="Trailing window in months (default 15).")
    parser.add_argument("--watchlist", default="config/watchlist.yaml")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = RedlineConfig.from_toml(args.settings)
    db_path = args.db_path or config.storage.db_path
    with open_db(db_path) as conn:
        init_full_schema(conn)
        seed_watchlist_from_yaml(conn, args.watchlist)
        summary = backfill(config, conn, months=args.months)
    _LOG.info("backfill summary: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
