"""Tests for the diff analyzer orchestrator (Subsystem 3).

Mocks ``LLMClient.complete`` so tests are deterministic and offline.
Covers:
- Substantive change passes Stage 2 + Stage 3, materiality >= threshold ->
  flagged_events row
- Cosmetic change passes Stage 1 but fails Stage 2 -> no flagged event,
  but a Stage 2 diff_results row is recorded
- Materiality below threshold -> Stage 3 row written but no flagged event
- No prior filing -> filing goes to ``analyzed`` without LLM calls
- LLM exception -> analysis_failed + retry_count++
- Prior-period lookup picks most-recent-same-type
"""
from __future__ import annotations

import datetime
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from redline.config import (
    AnthropicConfig,
    CorrelatorConfig,
    DiffConfig,
    LLMConfig,
    OpenAIConfig,
    PollerConfig,
    RedlineConfig,
    StorageConfig,
)
from redline.diff.analyzer import MAX_RETRIES, run_once
from redline.llm.schemas import DiffGateDecision, DiffSummary
from redline.storage.db import connect
from redline.storage.schema import (
    init_full_schema,
    seed_watchlist_from_yaml,
)


# ----- fixtures ------------------------------------------------------------


def _config(materiality_threshold: float = 0.6) -> RedlineConfig:
    return RedlineConfig(
        llm=LLMConfig(
            openai=OpenAIConfig(cheap_model="x", quality_model="y"),
            anthropic=AnthropicConfig(cheap_model="x", quality_model="y"),
        ),
        diff=DiffConfig(materiality_threshold=materiality_threshold),
        correlator=CorrelatorConfig(),
        poller=PollerConfig(edgar_user_agent="Test (t@t)"),
        storage=StorageConfig(db_path=":memory:"),
    )


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_full_schema(conn)
    wl = tmp_path / "watchlist.yaml"
    wl.write_text(
        '- cik: "0001321655"\n'
        "  ticker: PLTR\n"
        "  name: Palantir\n"
        "  sector: tech\n",
        encoding="utf-8",
    )
    seed_watchlist_from_yaml(conn, wl)
    yield conn
    conn.close()


def _seed_filing_with_content(
    conn,
    *,
    accession: str,
    filing_type: str = "10-Q",
    filed_at: str,
    sections: dict[str, str],
    status: str = "parsed",
    cik: str = "0001321655",
    retry_count: int = 0,
    last_attempted: str | None = None,
) -> None:
    """Insert into filings_seen + filings_content as if the fetcher had parsed it."""
    conn.execute(
        """
        INSERT INTO filings_seen (
            accession, cik, filing_type, filed_at, status,
            retry_count, last_attempted, discovered_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (accession, cik, filing_type, filed_at, status,
         retry_count, last_attempted, "2026-05-01T00:00:00Z"),
    )
    conn.execute(
        """
        INSERT INTO filings_content (
            accession, raw_content, sections, is_empty,
            parser_version, extracted_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (accession, None, json.dumps(sections), json.dumps(
            {k: (v is None or not v) for k, v in sections.items()}
        ), "v1", "2026-05-01T00:00:00Z"),
    )


def _build_client_mock(
    gate_decisions: list[DiffGateDecision] | None = None,
    summaries: list[DiffSummary] | None = None,
    raise_on_call: Exception | None = None,
) -> MagicMock:
    """Build a mock LLMClient that returns the supplied schema instances in order.

    Dispatch by schema type: DiffGateDecision -> consume from gate_decisions,
    DiffSummary -> consume from summaries.
    """
    client = MagicMock()
    gate_iter = iter(gate_decisions or [])
    summary_iter = iter(summaries or [])

    def _complete(**kwargs):
        if raise_on_call:
            raise raise_on_call
        schema = kwargs["schema"]
        if schema is DiffGateDecision:
            try:
                return next(gate_iter)
            except StopIteration:
                raise AssertionError("Unexpected extra Stage 2 call")
        if schema is DiffSummary:
            try:
                return next(summary_iter)
            except StopIteration:
                raise AssertionError("Unexpected extra Stage 3 call")
        raise AssertionError(f"Unexpected schema {schema}")

    client.complete = MagicMock(side_effect=_complete)
    return client


# ----- content helpers -----------------------------------------------------


_BASE_SECTIONS = {
    "mdna": " ".join(["mdna"] * 100),
    "risk_factors": (
        "Risk factors:\n\nWe face competition from established enterprise software "
        "vendors and emerging startups across all market segments. "
        + " ".join(["filler"] * 30)
    ),
    "legal": " ".join(["legal"] * 50),
    "qdmr": " ".join(["qdmr"] * 50),
}


def _sections_with_new_risk() -> dict[str, str]:
    """Same as base but with a substantive new risk bullet inserted."""
    new_rf = (
        "Risk factors:\n\nWe face competition from established enterprise software "
        "vendors and emerging startups across all market segments. "
        + " ".join(["filler"] * 30)
        + "\n\nReluctance of customers to purchase products incorporating generative "
        "AI may limit adoption of our Artificial Intelligence Platform offerings, "
        "and adverse regulatory developments around AI may further constrain our "
        "business as we expand our AIP platform into new markets and customer "
        "segments around the world during this fiscal year and beyond as expected."
    )
    return {**_BASE_SECTIONS, "risk_factors": new_rf}


# ----- tests ---------------------------------------------------------------


def test_no_prior_filing_skips_with_analyzed_status(db):
    """Filing with no prior is marked analyzed without any LLM calls."""
    _seed_filing_with_content(
        db, accession="acc-current", filed_at="2026-05-01", sections=_BASE_SECTIONS,
    )
    client = MagicMock()
    client.complete = MagicMock(side_effect=AssertionError("LLM should not be called"))

    summary = run_once(_config(), db, client)

    assert summary["analyzed"] == 1
    assert summary["failed"] == 0
    assert summary["flagged"] == 0
    row = db.execute(
        "SELECT status FROM filings_seen WHERE accession = ?", ("acc-current",)
    ).fetchone()
    assert row["status"] == "analyzed"


def test_substantive_change_flags_event(db):
    """Stage 1 -> Stage 2 substantive -> Stage 3 high materiality -> flagged_events."""
    _seed_filing_with_content(
        db, accession="acc-prior", filed_at="2026-02-01", sections=_BASE_SECTIONS,
        status="analyzed",
    )
    _seed_filing_with_content(
        db, accession="acc-current", filed_at="2026-05-01",
        sections=_sections_with_new_risk(),
    )

    gate = DiffGateDecision(substantive=True, reason="Introduces a new gen-AI risk topic.")
    summary = DiffSummary(
        change_type="addition", materiality=0.85,
        summary="New risk: customer reluctance to gen-AI products.",
        affected_topics=["generative_ai"],
    )
    client = _build_client_mock(gate_decisions=[gate], summaries=[summary])

    result = run_once(_config(), db, client)

    assert result["analyzed"] == 1
    assert result["flagged"] == 1

    # filings_seen status transitioned
    status = db.execute(
        "SELECT status FROM filings_seen WHERE accession = ?", ("acc-current",)
    ).fetchone()["status"]
    assert status == "analyzed"

    # diff_results: Stage 2 + Stage 3 rows for risk_factors
    stage_rows = db.execute(
        "SELECT stage, section, materiality FROM diff_results "
        "WHERE accession = ? ORDER BY stage",
        ("acc-current",),
    ).fetchall()
    assert [r["stage"] for r in stage_rows] == [2, 3]
    assert stage_rows[1]["materiality"] == 0.85

    # flagged_events row
    flagged = db.execute(
        "SELECT flag_reason, materiality_max, diff_summary "
        "FROM flagged_events WHERE accession = ?",
        ("acc-current",),
    ).fetchone()
    assert flagged["flag_reason"] == "diff_material"
    assert flagged["materiality_max"] == 0.85
    summaries_payload = json.loads(flagged["diff_summary"])
    assert summaries_payload[0]["section"] == "risk_factors"
    assert "generative_ai" in summaries_payload[0]["affected_topics"]


def test_cosmetic_change_fails_stage2_no_flag(db):
    """Stage 1 surviving change classified non-substantive at Stage 2:
    Stage 2 row recorded; no Stage 3 call, no flagged_events row."""
    _seed_filing_with_content(
        db, accession="acc-prior", filed_at="2026-02-01", sections=_BASE_SECTIONS,
        status="analyzed",
    )
    _seed_filing_with_content(
        db, accession="acc-current", filed_at="2026-05-01",
        sections=_sections_with_new_risk(),
    )
    gate = DiffGateDecision(substantive=False, reason="Counsel rewording, no new concept.")
    # No Stage 3 needed; assert raises if invoked
    client = _build_client_mock(gate_decisions=[gate], summaries=[])

    result = run_once(_config(), db, client)

    assert result["analyzed"] == 1
    assert result["flagged"] == 0

    stages = db.execute(
        "SELECT stage FROM diff_results WHERE accession = ?", ("acc-current",)
    ).fetchall()
    assert {r["stage"] for r in stages} == {2}  # only Stage 2 rows

    assert db.execute(
        "SELECT COUNT(*) FROM flagged_events WHERE accession = ?", ("acc-current",)
    ).fetchone()[0] == 0


def test_materiality_below_threshold_no_flag(db):
    """Stage 3 returns a low materiality: stage row recorded, no flag."""
    _seed_filing_with_content(
        db, accession="acc-prior", filed_at="2026-02-01", sections=_BASE_SECTIONS,
        status="analyzed",
    )
    _seed_filing_with_content(
        db, accession="acc-current", filed_at="2026-05-01",
        sections=_sections_with_new_risk(),
    )
    gate = DiffGateDecision(substantive=True, reason="Minor topic addition.")
    summary = DiffSummary(
        change_type="addition", materiality=0.3,
        summary="Adds a minor sentence.",
        affected_topics=["filler"],
    )
    client = _build_client_mock(gate_decisions=[gate], summaries=[summary])

    result = run_once(_config(materiality_threshold=0.6), db, client)

    assert result["analyzed"] == 1
    assert result["flagged"] == 0

    flagged_count = db.execute(
        "SELECT COUNT(*) FROM flagged_events WHERE accession = ?", ("acc-current",)
    ).fetchone()[0]
    assert flagged_count == 0


def test_prior_lookup_picks_most_recent_same_type(db):
    """When multiple priors exist, the diff uses the most recent same-type."""
    _seed_filing_with_content(
        db, accession="prior-old", filed_at="2025-08-01", sections=_BASE_SECTIONS,
        status="analyzed",
    )
    _seed_filing_with_content(
        db, accession="prior-recent", filed_at="2026-02-01", sections=_BASE_SECTIONS,
        status="analyzed",
    )
    _seed_filing_with_content(
        db, accession="acc-current", filed_at="2026-05-01",
        sections=_sections_with_new_risk(),
    )

    gate = DiffGateDecision(substantive=True, reason="x")
    summary = DiffSummary(change_type="addition", materiality=0.8, summary="x", affected_topics=[])
    client = _build_client_mock(gate_decisions=[gate], summaries=[summary])

    run_once(_config(), db, client)

    row = db.execute(
        "SELECT prior_accession FROM diff_results WHERE accession = ? LIMIT 1",
        ("acc-current",),
    ).fetchone()
    assert row["prior_accession"] == "prior-recent"


def test_llm_failure_marks_analysis_failed(db):
    _seed_filing_with_content(
        db, accession="acc-prior", filed_at="2026-02-01", sections=_BASE_SECTIONS,
        status="analyzed",
    )
    _seed_filing_with_content(
        db, accession="acc-current", filed_at="2026-05-01",
        sections=_sections_with_new_risk(),
    )
    client = _build_client_mock(raise_on_call=RuntimeError("api blip"))

    result = run_once(_config(), db, client)

    assert result["failed"] == 1
    row = db.execute(
        "SELECT status, retry_count, failure_reason FROM filings_seen WHERE accession = ?",
        ("acc-current",),
    ).fetchone()
    assert row["status"] == "analysis_failed"
    assert row["retry_count"] == 1
    assert "RuntimeError" in row["failure_reason"]


def test_failed_permanent_after_max_retries(db):
    _seed_filing_with_content(
        db, accession="acc-prior", filed_at="2026-02-01", sections=_BASE_SECTIONS,
        status="analyzed",
    )
    _seed_filing_with_content(
        db, accession="acc-current", filed_at="2026-05-01",
        sections=_sections_with_new_risk(),
        status="analysis_failed", retry_count=MAX_RETRIES - 1,
        last_attempted=None,
    )
    client = _build_client_mock(raise_on_call=RuntimeError("still broken"))

    run_once(_config(), db, client)

    status = db.execute(
        "SELECT status, retry_count FROM filings_seen WHERE accession = ?",
        ("acc-current",),
    ).fetchone()
    assert status["status"] == "failed_permanent"
    assert status["retry_count"] == MAX_RETRIES


def test_only_10k_and_10q_picked_up(db):
    """8-K and Form 4 rows at status='parsed' are NOT pulled into the diff queue."""
    _seed_filing_with_content(
        db, accession="acc-8k", filing_type="8-K",
        filed_at="2026-05-01", sections={"items_list": ["Item 1.01"]},
    )
    _seed_filing_with_content(
        db, accession="acc-f4", filing_type="4",
        filed_at="2026-05-01", sections={"insider_name": "X", "transaction_count": 1},
    )
    client = MagicMock()
    client.complete = MagicMock(side_effect=AssertionError("LLM should not be called"))

    result = run_once(_config(), db, client)

    assert result["considered"] == 0
    # Both rows still at 'parsed' (not touched)
    for acc in ("acc-8k", "acc-f4"):
        status = db.execute(
            "SELECT status FROM filings_seen WHERE accession = ?", (acc,)
        ).fetchone()["status"]
        assert status == "parsed"
