"""Diff analyzer orchestrator (Subsystem 3).

For each ``status='parsed'`` 10-K / 10-Q row:

1. Find prior-period filing (same CIK, same filing_type, filed earlier)
2. If no prior: status -> ``analyzed``, no diff to compute
3. For each of {mdna, risk_factors, legal, qdmr}:
   - Stage 1 (filter.py): paragraph diff + normalization + min_words filter
   - For each survivor: Stage 2 (gate.py); if substantive, Stage 3 (summarize.py)
4. Write ``diff_results`` rows for Stage 2 and Stage 3 outputs
5. Aggregate materiality across sections; if max >= threshold,
   insert a ``flagged_events`` row with reason='diff_material'
6. Transition status to ``analyzed`` (success) or ``analysis_failed``
   (exception; retry on next cycle per ARCHITECTURE.md §7 semantics).

Filing-type scope: 10-K and 10-Q only. 8-K event_detection and Form 4
correlator analysis happen in other subsystems.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sqlite3
import sys
from typing import Any

from redline.config import RedlineConfig
from redline.diff.filter import Stage1Change, stage1_filter
from redline.diff.gate import PROMPT_VERSION as GATE_PROMPT_VERSION
from redline.diff.gate import gate as stage2_gate
from redline.diff.summarize import PROMPT_VERSION as SUMMARY_PROMPT_VERSION
from redline.diff.summarize import summarize as stage3_summarize
from redline.llm.client import LLMClient

_LOG = logging.getLogger(__name__)

# 10-K and 10-Q share these section labels (different part/item but same
# semantic content per ARCHITECTURE.md §3).
SECTIONS: list[str] = ["mdna", "risk_factors", "legal", "qdmr"]

MAX_RETRIES = 3
RETRY_AFTER_HOURS = 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _pending_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Rows eligible for diff: 10-K/10-Q at status='parsed' or analysis_failed
    older than the retry window."""
    return conn.execute(
        f"""
        SELECT accession, cik, filing_type, filed_at, retry_count
        FROM filings_seen
        WHERE filing_type IN ('10-K', '10-Q')
          AND (
              status = 'parsed'
              OR (
                  status = 'analysis_failed'
                  AND retry_count < {MAX_RETRIES}
                  AND (
                      last_attempted IS NULL
                      OR datetime(last_attempted)
                         < datetime('now', '-{RETRY_AFTER_HOURS} hour')
                  )
              )
          )
        """
    ).fetchall()


def _find_prior(
    conn: sqlite3.Connection, *, cik: str, filing_type: str, filed_at: str,
) -> sqlite3.Row | None:
    """Most-recent-same-type prior filing for the CIK, with content available."""
    return conn.execute(
        """
        SELECT fs.accession, fc.sections
        FROM filings_seen fs
        JOIN filings_content fc ON fc.accession = fs.accession
        WHERE fs.cik = ?
          AND fs.filing_type = ?
          AND fs.filed_at < ?
          AND fs.status IN ('parsed', 'analyzed', 'flagged')
        ORDER BY fs.filed_at DESC
        LIMIT 1
        """,
        (cik, filing_type, filed_at),
    ).fetchone()


def _load_sections(conn: sqlite3.Connection, accession: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT sections FROM filings_content WHERE accession = ?", (accession,),
    ).fetchone()
    if not row:
        return {}
    return json.loads(row["sections"])


def _insert_diff_result(
    conn: sqlite3.Connection,
    *, accession: str, prior_accession: str, section: str, stage: int,
    chunk_old: str | None, chunk_new: str | None,
    gate_decision: dict | None, summary: dict | None,
    materiality: float | None, prompt_version: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO diff_results (
            accession, prior_accession, section, stage,
            chunk_old, chunk_new, gate_decision, summary,
            materiality, prompt_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            accession, prior_accession, section, stage,
            chunk_old, chunk_new,
            json.dumps(gate_decision) if gate_decision is not None else None,
            json.dumps(summary) if summary is not None else None,
            materiality, prompt_version, _now_iso(),
        ),
    )
    return cur.lastrowid


def _insert_flagged_event(
    conn: sqlite3.Connection,
    *, accession: str, summaries: list[dict], materiality_max: float,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO flagged_events (
            accession, flag_reason, diff_summary,
            materiality_max, flagged_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            accession, "diff_material",
            json.dumps(summaries, default=str),
            materiality_max, _now_iso(),
        ),
    )
    return cur.lastrowid


def _mark_analyzed(conn: sqlite3.Connection, accession: str) -> None:
    conn.execute(
        """
        UPDATE filings_seen SET
            status = 'analyzed',
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
    new_status = "failed_permanent" if new_count >= MAX_RETRIES else "analysis_failed"
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
# Per-filing pipeline
# ---------------------------------------------------------------------------

def _analyze_one(
    conn: sqlite3.Connection,
    client: LLMClient,
    config: RedlineConfig,
    *, accession: str, cik: str, filing_type: str, filed_at: str,
) -> dict:
    """Run the three-stage diff for one filing. Returns summary dict.

    Side effects: inserts ``diff_results`` rows, possibly a ``flagged_events``
    row; the caller is responsible for the status transition. Any prior
    ``diff_results`` / ``flagged_events`` rows for this accession are cleared
    so retries don't leave duplicate partial output.
    """
    prior = _find_prior(conn, cik=cik, filing_type=filing_type, filed_at=filed_at)
    if prior is None:
        return {"skipped": True, "reason": "no_prior", "stage1_total": 0,
                "stage2_substantive": 0, "stage3_summarized": 0, "materiality_max": 0.0}

    # Idempotent re-run: clear any partial output from a prior failed attempt.
    conn.execute("DELETE FROM diff_results WHERE accession = ?", (accession,))
    conn.execute("DELETE FROM flagged_events WHERE accession = ?", (accession,))

    prior_accession = prior["accession"]
    prior_sections = json.loads(prior["sections"])
    current_sections = _load_sections(conn, accession)

    stage1_total = 0
    stage2_substantive = 0
    stage3_summarized = 0
    materiality_max = 0.0
    flagged_summaries: list[dict] = []

    for section_name in SECTIONS:
        old_text = (prior_sections or {}).get(section_name)
        new_text = (current_sections or {}).get(section_name)
        if not old_text or not new_text:
            continue
        changes = stage1_filter(
            old_text, new_text,
            min_words=config.diff.min_words,
            normalize_tokens=config.diff.normalize_tokens,
        )
        stage1_total += len(changes)
        if not changes:
            continue

        for change in changes:
            gate_decision = stage2_gate(client, section=section_name, change=change)
            _insert_diff_result(
                conn,
                accession=accession, prior_accession=prior_accession,
                section=section_name, stage=2,
                chunk_old=change.old, chunk_new=change.new,
                gate_decision=gate_decision.model_dump(),
                summary=None, materiality=None,
                prompt_version=GATE_PROMPT_VERSION,
            )
            if not gate_decision.substantive:
                continue
            stage2_substantive += 1

            # Phase 1: don't pass prior_section_text as reusable_context.
            # PLTR-class 10-Qs have ~75k-token Risk Factors sections, and
            # Ian's OpenAI tier caps at 30K TPM — caching benefit can't
            # offset that. The change.old/new chunks already carry the
            # contextual surroundings the model needs. When we move to a
            # higher TPM tier (or fall over to Anthropic), revisit and pass
            # prior_section_text=old_text for ~90% prompt-caching savings.
            summary = stage3_summarize(
                client, section=section_name, change=change,
                gate_reason=gate_decision.reason,
            )
            summary_dict = summary.model_dump()
            _insert_diff_result(
                conn,
                accession=accession, prior_accession=prior_accession,
                section=section_name, stage=3,
                chunk_old=change.old, chunk_new=change.new,
                gate_decision=gate_decision.model_dump(),
                summary=summary_dict, materiality=summary.materiality,
                prompt_version=SUMMARY_PROMPT_VERSION,
            )
            stage3_summarized += 1
            materiality_max = max(materiality_max, summary.materiality)
            if summary.materiality >= config.diff.materiality_threshold:
                flagged_summaries.append({
                    "section": section_name,
                    **summary_dict,
                })

    if flagged_summaries:
        _insert_flagged_event(
            conn, accession=accession,
            summaries=flagged_summaries, materiality_max=materiality_max,
        )

    return {
        "skipped": False,
        "prior_accession": prior_accession,
        "stage1_total": stage1_total,
        "stage2_substantive": stage2_substantive,
        "stage3_summarized": stage3_summarized,
        "materiality_max": materiality_max,
        "flagged": len(flagged_summaries) > 0,
    }


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def run_once(
    config: RedlineConfig,
    conn: sqlite3.Connection,
    client: LLMClient,
) -> dict:
    """One diff-analysis pass over all parsed 10-K/10-Q filings."""
    rows = _pending_rows(conn)
    per_filing: list[dict] = []
    analyzed = 0
    failed = 0
    flagged = 0

    for row in rows:
        accession = row["accession"]
        try:
            result = _analyze_one(
                conn, client, config,
                accession=accession,
                cik=row["cik"], filing_type=row["filing_type"],
                filed_at=row["filed_at"],
            )
            _mark_analyzed(conn, accession)
            analyzed += 1
            if result.get("flagged"):
                flagged += 1
            per_filing.append({"accession": accession, "status": "analyzed", **result})
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            _LOG.warning("Diff analysis failed for %s: %s", accession, reason)
            _mark_failed(
                conn, accession=accession,
                retry_count=row["retry_count"], reason=reason,
            )
            failed += 1
            per_filing.append({
                "accession": accession, "status": "analysis_failed", "error": reason,
            })

    return {
        "considered": len(rows),
        "analyzed": analyzed,
        "failed": failed,
        "flagged": flagged,
        "per_filing": per_filing,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diff analyzer for redline.")
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit.")
    parser.add_argument("--settings", default="config/settings.toml")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Load secrets from .env (OPENAI_API_KEY, ANTHROPIC_API_KEY). The LLM
    # client reads these from os.environ; .env is gitignored.
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
