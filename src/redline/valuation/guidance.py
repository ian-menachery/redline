"""8-K earnings-guidance extraction — the differentiated "flagged change ->
model input" hook (Subsystem 7).

Phase 0b (`spike/guidance_8k_feasibility.py`) verified guidance is reliably
present in 8-K EX-99.1 earnings releases (~0.80 of item-2.02 releases) — unlike
the MD&A, where it is absent (NOTES §6). This module:

1. finds item-2.02 ("Results of Operations") 8-Ks for the DCF-eligible names,
2. pulls the EX-99.1 press-release text,
3. runs a quality-role LLM to extract TYPED, RANGED, basis/period-qualified
   guidance figures (regex is too noisy — the feasibility spike caught balance
   figures with a "fiscal 20XX" cue),
4. stores them in ``extracted_figures`` with a confidence gate
   (``trigger_eligible`` vs ``manual_review``) and a period-over-period delta
   (raised / lowered / reaffirmed / initiated) vs the prior release's guidance
   for the same metric/period/basis.

A ``trigger_eligible`` guidance change is what ``revalue`` consumes to move a
model input. LLM calls go through ``LLMClient`` (logged + validated).
"""
from __future__ import annotations

import argparse
import datetime
import logging
import sqlite3
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import edgar

from redline.config import RedlineConfig
from redline.llm.client import LLMClient
from redline.llm.schemas import GuidanceExtraction, GuidanceFigure
from redline.valuation.xbrl import DCF_EXCLUDED_SECTORS

_LOG = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
PARSER_VERSION = "v1"
CALL_SITE = "guidance_extract"
# Guidance sits in the release highlights / a dedicated outlook section; cap
# the text we send (cost) — v1 limitation: a very long release could bury
# guidance past the cap (logged in NOTES for Phase-2 refinement).
_MAX_EXHIBIT_CHARS = 24000
_REAFFIRM_TOL = 0.005  # midpoint within 0.5% -> treated as reaffirmed, not raised/lowered


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _load_prompt(prompts_dir: str | Path) -> str:
    return Path(prompts_dir, f"guidance_extract_{PROMPT_VERSION}.txt").read_text(encoding="utf-8")


def fetch_guidance_exhibit(filing) -> str | None:
    """Return the EX-99.x earnings-release text, or None."""
    try:
        atts = filing.attachments
    except Exception:
        return None
    for a in atts:
        dt = str(getattr(a, "document_type", "") or "")
        if dt.upper().startswith("EX-99"):
            try:
                t = a.text() if callable(getattr(a, "text", None)) else getattr(a, "text", "")
            except Exception:
                t = None
            if t:
                return str(t)
    return None


def _is_earnings_8k(filing) -> bool:
    try:
        items = list(getattr(filing.obj(), "items", []) or [])
    except Exception:
        return False
    return any("2.02" in str(i) for i in items)


def extract_guidance(
    client: LLMClient, *, exhibit_text: str, ticker: str,
    prompts_dir: str | Path = "config/prompts",
    max_chars: int = _MAX_EXHIBIT_CHARS,
) -> GuidanceExtraction:
    """One quality-role LLM call: EX-99.1 text -> typed guidance figures.

    ``max_chars`` caps the exhibit text sent (ValuationConfig.max_exhibit_chars)."""
    system = _load_prompt(prompts_dir)
    user = f"Company: {ticker}\n\nEARNINGS RELEASE:\n{exhibit_text[:max_chars]}"
    return client.complete(
        system=system, user=user, schema=GuidanceExtraction,
        role="quality", call_site=CALL_SITE, prompt_version=PROMPT_VERSION,
    )


def _midpoint(low: float | None, high: float | None) -> float | None:
    vals = [v for v in (low, high) if v is not None]
    return sum(vals) / len(vals) if vals else None


def _review_status(fig: GuidanceFigure, min_conf: float) -> str:
    # Metric-scoped basis exception (fix #3): forward REVENUE guidance is
    # routinely stated as plain "total revenue of $X" with no GAAP/non-GAAP
    # qualifier, so basis='unspecified' is faithful and must not disqualify it.
    # For every OTHER metric the strict basis!='unspecified' requirement holds.
    basis_ok = fig.basis != "unspecified" or fig.metric == "revenue"
    if (fig.confidence >= min_conf and fig.period
            and (fig.low is not None or fig.high is not None)
            and basis_ok):
        return "trigger_eligible"
    return "manual_review"


def _prior_guidance(
    conn: sqlite3.Connection, *, cik: str, metric: str, period: str, basis: str,
    exclude_accession: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT low, high, extracted_at FROM extracted_figures
        WHERE cik = ? AND metric = ? AND period = ? AND basis = ?
          AND accession != ?
        ORDER BY extracted_at DESC LIMIT 1
        """,
        (cik, metric, period, basis, exclude_accession),
    ).fetchone()


def _delta(
    conn: sqlite3.Connection, *, cik: str, fig: GuidanceFigure, accession: str,
    reaffirm_tol: float = _REAFFIRM_TOL,
) -> str:
    """Delta direction vs the prior release's same figure:
    initiated / raised / lowered / reaffirmed. Midpoint moves within
    ``reaffirm_tol`` (fractional) count as reaffirmed."""
    prior = _prior_guidance(conn, cik=cik, metric=fig.metric, period=fig.period,
                            basis=fig.basis, exclude_accession=accession)
    if prior is None:
        return "initiated"
    cur_mid = _midpoint(fig.low, fig.high)
    prior_mid = _midpoint(prior["low"], prior["high"])
    if cur_mid is None or prior_mid is None or prior_mid == 0:
        return "reaffirmed" if fig.is_reaffirmed else "initiated"
    change = (cur_mid - prior_mid) / abs(prior_mid)
    if abs(change) <= reaffirm_tol:
        return "reaffirmed"
    return "raised" if change > 0 else "lowered"


def _store_figure(
    conn: sqlite3.Connection, *, accession: str, cik: str, fig: GuidanceFigure,
    min_conf: float, reaffirm_tol: float = _REAFFIRM_TOL,
) -> str:
    review = _review_status(fig, min_conf)
    direction = _delta(conn, cik=cik, fig=fig, accession=accession,
                       reaffirm_tol=reaffirm_tol)
    # Resolve the prior accession for the audit trail (may be None).
    prior = _prior_guidance(conn, cik=cik, metric=fig.metric, period=fig.period,
                            basis=fig.basis, exclude_accession=accession)
    prior_accession = None
    if prior is not None:
        row = conn.execute(
            """SELECT accession FROM extracted_figures
               WHERE cik=? AND metric=? AND period=? AND basis=? AND accession!=?
               ORDER BY extracted_at DESC LIMIT 1""",
            (cik, fig.metric, fig.period, fig.basis, accession),
        ).fetchone()
        prior_accession = row["accession"] if row else None
    conn.execute(
        """
        INSERT OR REPLACE INTO extracted_figures (
            accession, cik, metric, scope, period, low, high, unit, basis,
            is_reaffirmed, confidence, context, review_status,
            delta_direction, delta_prior_accession,
            prompt_version, parser_version, extracted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            accession, cik, fig.metric, fig.scope, fig.period, fig.low, fig.high, fig.unit,
            fig.basis, int(fig.is_reaffirmed), fig.confidence, fig.context, review,
            direction, prior_accession, PROMPT_VERSION, PARSER_VERSION, _now_iso(),
        ),
    )
    return review


def _pending_8ks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in DCF_EXCLUDED_SECTORS)
    return conn.execute(
        f"""
        SELECT fs.accession, fs.cik, w.ticker
        FROM filings_seen fs
        JOIN watchlist w ON w.cik = fs.cik
        WHERE fs.filing_type = '8-K'
          AND w.sector NOT IN ({placeholders})
          AND fs.accession NOT IN (SELECT accession FROM guidance_runs)
        ORDER BY fs.filed_at
        """,
        tuple(DCF_EXCLUDED_SECTORS),
    ).fetchall()


def _record_run(conn, *, accession, is_earnings, has_guidance, figures_found) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO guidance_runs
           (accession, ran_at, is_earnings, has_guidance, figures_found)
           VALUES (?, ?, ?, ?, ?)""",
        (accession, _now_iso(), int(is_earnings), int(has_guidance), figures_found),
    )


def run_once(
    config: RedlineConfig,
    conn: sqlite3.Connection,
    client: LLMClient,
    *,
    filing_factory: Callable[[str], Any] | None = None,
) -> dict:
    """Extract guidance from unprocessed item-2.02 8-Ks for the DCF names."""
    if filing_factory is None:
        filing_factory = edgar.find
    edgar.set_identity(config.poller.edgar_user_agent)
    min_conf = config.valuation.min_trigger_confidence
    reaffirm_tol = config.valuation.guidance_reaffirm_tolerance
    max_chars = config.valuation.max_exhibit_chars

    per_filing: list[dict] = []
    processed = 0
    with_guidance = 0
    trigger_eligible = 0
    for row in _pending_8ks(conn):
        accession, cik, ticker = row["accession"], row["cik"], row["ticker"]
        try:
            filing = filing_factory(accession)
            if not _is_earnings_8k(filing):
                _record_run(conn, accession=accession, is_earnings=False,
                            has_guidance=False, figures_found=0)
                per_filing.append({"accession": accession, "status": "not_earnings"})
                continue
            exhibit = fetch_guidance_exhibit(filing)
            if not exhibit:
                _record_run(conn, accession=accession, is_earnings=True,
                            has_guidance=False, figures_found=0)
                per_filing.append({"accession": accession, "status": "no_exhibit"})
                continue

            extraction = extract_guidance(
                client, exhibit_text=exhibit, ticker=ticker, max_chars=max_chars,
            )
            n_elig = 0
            stored = 0
            for fig in extraction.figures:
                # Suppress phantom nulls: a "figure" with no number is a
                # qualitative statement, not guidance — do not store it.
                if fig.low is None and fig.high is None:
                    continue
                review = _store_figure(conn, accession=accession, cik=cik,
                                       fig=fig, min_conf=min_conf,
                                       reaffirm_tol=reaffirm_tol)
                n_elig += int(review == "trigger_eligible")
                stored += 1
            _record_run(conn, accession=accession, is_earnings=True,
                        has_guidance=extraction.has_guidance,
                        figures_found=stored)
            processed += 1
            with_guidance += int(extraction.has_guidance)
            trigger_eligible += n_elig
            per_filing.append({
                "accession": accession, "ticker": ticker, "status": "extracted",
                "figures": len(extraction.figures), "trigger_eligible": n_elig,
            })
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            _LOG.warning("guidance extraction failed for %s: %s", accession, reason)
            per_filing.append({"accession": accession, "status": "error", "error": reason})

    return {
        "considered": len(per_filing),
        "processed": processed,
        "with_guidance": with_guidance,
        "trigger_eligible_figures": trigger_eligible,
        "per_filing": per_filing,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="8-K guidance extraction for redline DCF.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--settings", default="config/settings.toml")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from dotenv import load_dotenv
    load_dotenv()

    config = RedlineConfig.from_toml(args.settings)
    from redline.storage.db import open_db
    from redline.storage.schema import init_full_schema

    with open_db(config.storage.db_path) as conn:
        init_full_schema(conn)
        client = LLMClient(config, conn)
        summary = run_once(config, conn, client)
        _LOG.info("cycle: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
