"""Tests for the eval grader (pass_criteria evaluator + judge fallback)."""
from __future__ import annotations

import datetime
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from redline.eval.grader import (
    build_context,
    evaluate_pass_criteria,
    grade_event,
    normalize_pass_criteria,
)
from redline.eval.models import EvalEvent
from redline.llm.schemas import EvalJudgeVerdict
from redline.storage.db import connect
from redline.storage.schema import init_full_schema, seed_watchlist_from_yaml


# ----- normalize_pass_criteria --------------------------------------------


def test_normalize_yaml_to_python_keywords():
    s = "x.materiality_max >= 0.6 AND any('a' in x.topics for a in ['a'])"
    out = normalize_pass_criteria(s)
    assert " and " in out
    assert " AND " not in out


def test_normalize_yaml_booleans():
    assert normalize_pass_criteria("flag == true") == "flag == True"
    assert normalize_pass_criteria("flag == false") == "flag == False"
    assert normalize_pass_criteria("x is null") == "x is None"


def test_normalize_preserves_strings():
    """Operator-replacement must be word-boundary-anchored."""
    s = 'topic == "STAND_AND_DELIVER"'
    out = normalize_pass_criteria(s)
    assert "STAND_AND_DELIVER" in out  # the AND inside the literal isn't replaced


# ----- evaluate_pass_criteria ---------------------------------------------


def test_evaluate_simple_true():
    ctx = {"x": SimpleNamespace(value=10)}
    assert evaluate_pass_criteria("x.value >= 5", ctx) is True


def test_evaluate_simple_false():
    ctx = {"x": SimpleNamespace(value=10)}
    assert evaluate_pass_criteria("x.value < 5", ctx) is False


def test_evaluate_with_and():
    ctx = {"x": SimpleNamespace(a=10, b=20)}
    assert evaluate_pass_criteria("x.a >= 5 AND x.b >= 5", ctx) is True
    assert evaluate_pass_criteria("x.a >= 5 AND x.b > 100", ctx) is False


def test_evaluate_with_any_and_in():
    ctx = {"x": SimpleNamespace(topics=["a", "b", "c"])}
    expr = 'any(t in x.topics for t in ["b", "z"])'
    assert evaluate_pass_criteria(expr, ctx) is True
    expr = 'any(t in x.topics for t in ["y", "z"])'
    assert evaluate_pass_criteria(expr, ctx) is False


def test_evaluate_missing_attribute_returns_none():
    """When the context doesn't have the referenced attribute, return None
    (signal to fall back to LLM judge)."""
    ctx = {"x": SimpleNamespace(only_field=1)}
    assert evaluate_pass_criteria("x.absent_field >= 5", ctx) is None


def test_evaluate_missing_top_level_name_returns_none():
    ctx = {}
    assert evaluate_pass_criteria("flagged_events.materiality_max >= 0.6", ctx) is None


def test_evaluate_syntax_error_returns_none():
    assert evaluate_pass_criteria("this is not (valid python", {}) is None


def test_evaluate_none_value_short_circuit():
    """A field that's None compared to a number should return None (TypeError)
    rather than crash; the grader will fall back to judge."""
    ctx = {"x": SimpleNamespace(materiality=None)}
    assert evaluate_pass_criteria("x.materiality >= 0.6", ctx) is None


def test_evaluate_pltr_karp_pass_criteria_shape():
    """Sanity check against the actual pre-registered KEY/PLTR criteria shape."""
    ctx = {
        "correlator_payload": SimpleNamespace(
            anomalous=True, drivers=["Karp Nov 13 sale"], confidence=0.85,
        ),
    }
    expr = (
        "correlator_payload.anomalous == true "
        "AND any('Karp' in d for d in correlator_payload.drivers) "
        "AND correlator_payload.confidence >= 0.7"
    )
    assert evaluate_pass_criteria(expr, ctx) is True


# ----- build_context ------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_full_schema(conn)
    wl = tmp_path / "watchlist.yaml"
    wl.write_text(
        '- cik: "0001321655"\n'
        "  ticker: PLTR\n  name: Palantir\n  sector: tech\n",
        encoding="utf-8",
    )
    seed_watchlist_from_yaml(conn, wl)
    # Need a filings_seen row for the FK on flagged_events.
    conn.execute(
        """
        INSERT INTO filings_seen (
            accession, cik, filing_type, filed_at, status,
            retry_count, discovered_at
        ) VALUES (?, ?, ?, ?, ?, 0, ?)
        """,
        ("acc-test", "0001321655", "10-Q", "2026-05-01", "analyzed",
         "2026-05-01T00:00:00Z"),
    )
    yield conn
    conn.close()


def test_build_context_no_flagged_events(db):
    ctx = build_context(db, "acc-test")
    assert ctx["flagged_events"].exists is False
    assert ctx["flagged_events"].count == 0
    assert ctx["flagged_events"].materiality_max is None
    assert ctx["diff_summary"].exists is False
    assert ctx["correlator_payload"].exists is False


def test_build_context_with_diff_summaries(db):
    summaries = [
        {"section": "risk_factors", "change_type": "addition", "materiality": 0.8,
         "summary": "New gen-AI risk", "affected_topics": ["generative_ai"]},
        {"section": "mdna", "change_type": "modification", "materiality": 0.65,
         "summary": "Revenue growth", "affected_topics": ["revenue", "growth"]},
    ]
    db.execute(
        """
        INSERT INTO flagged_events (
            accession, flag_reason, diff_summary, materiality_max, flagged_at
        ) VALUES (?, 'diff_material', ?, ?, ?)
        """,
        ("acc-test", json.dumps(summaries), 0.8, "2026-05-01T00:00:00Z"),
    )

    ctx = build_context(db, "acc-test")
    assert ctx["flagged_events"].exists is True
    assert ctx["flagged_events"].materiality_max == 0.8
    assert "generative_ai" in ctx["diff_summary"].affected_topics
    assert "revenue" in ctx["diff_summary"].affected_topics
    assert ctx["diff_summary"].materiality_max == 0.8


def test_build_context_with_correlator_payload(db):
    payload = {
        "verdict": {"anomalous": True, "drivers": ["Karp cluster"], "confidence": 0.9},
        "cluster": {"max_cluster_size": 3},
    }
    db.execute(
        """
        INSERT INTO flagged_events (
            accession, flag_reason, correlator_payload, materiality_max, flagged_at
        ) VALUES (?, 'correlator_anomaly', ?, ?, ?)
        """,
        ("acc-test", json.dumps(payload), 0.9, "2026-05-01T00:00:00Z"),
    )

    ctx = build_context(db, "acc-test")
    corr = ctx["correlator_payload"]
    assert corr.exists is True
    assert corr.anomalous is True
    assert "Karp cluster" in corr.drivers
    assert corr.confidence == 0.9


# ----- grade_event --------------------------------------------------------


def _event(*, pass_criteria: str, rubric: str = "Did the system catch X?") -> EvalEvent:
    return EvalEvent(
        id="test_event", ticker="PLTR", filing_type="10-Q", period="Q3 2024",
        tests=["diff_analyzer"],
        pass_criteria=pass_criteria, llm_judge_rubric=rubric,
        locked_at=datetime.datetime(2026, 5, 11, 17, 30, tzinfo=datetime.timezone.utc),
    )


def test_grade_event_binary_pass(db):
    summaries = [
        {"section": "risk_factors", "change_type": "addition", "materiality": 0.85,
         "summary": "x", "affected_topics": ["generative_ai", "ai_regulation"]},
    ]
    db.execute(
        "INSERT INTO flagged_events (accession, flag_reason, diff_summary, "
        "materiality_max, flagged_at) VALUES (?, 'diff_material', ?, ?, ?)",
        ("acc-test", json.dumps(summaries), 0.85, "2026-05-01T00:00:00Z"),
    )

    event = _event(
        pass_criteria=(
            "flagged_events.materiality_max >= 0.6 "
            "AND any(t in diff_summary.affected_topics for t in "
            "['generative_ai', 'ai_regulation'])"
        ),
    )
    grade = grade_event(db, MagicMock(), event, accession_to_grade="acc-test")
    assert grade.binary_result is True
    assert grade.graded_pass is True
    assert grade.judge_result is None


def test_grade_event_binary_fail(db):
    """Pass criteria evaluates to False — graded as failure, no judge consulted."""
    summaries = [
        {"section": "risk_factors", "change_type": "addition", "materiality": 0.45,
         "summary": "x", "affected_topics": ["unrelated_topic"]},
    ]
    db.execute(
        "INSERT INTO flagged_events (accession, flag_reason, diff_summary, "
        "materiality_max, flagged_at) VALUES (?, 'diff_material', ?, ?, ?)",
        ("acc-test", json.dumps(summaries), 0.45, "2026-05-01T00:00:00Z"),
    )

    event = _event(
        pass_criteria=(
            "flagged_events.materiality_max >= 0.6 "
            "AND any(t in diff_summary.affected_topics for t in ['ai_regulation'])"
        ),
    )
    client = MagicMock()
    client.complete = MagicMock(side_effect=AssertionError("judge should NOT be called"))

    grade = grade_event(db, client, event, accession_to_grade="acc-test")
    assert grade.binary_result is False
    assert grade.graded_pass is False
    assert grade.judge_result is None


def test_grade_event_falls_back_to_judge_on_none(db):
    """No flagged_events row -> pass_criteria evaluates against None ->
    binary returns None -> judge consulted."""
    event = _event(
        pass_criteria=(
            "flagged_events.materiality_max >= 0.6 "
            "AND any(t in diff_summary.affected_topics for t in ['x'])"
        ),
    )
    client = MagicMock()
    client.complete = MagicMock(return_value=EvalJudgeVerdict(
        passed=True, reasoning="The system caught the relevant pattern by other means.",
        partial_credit=0.7,
    ))

    grade = grade_event(db, client, event, accession_to_grade="acc-test")
    assert grade.binary_result is None
    assert grade.judge_result is not None
    assert grade.graded_pass is True  # judge said pass
    assert "judge fallback" in grade.notes


def test_grade_event_judge_says_fail(db):
    event = _event(
        pass_criteria="flagged_events.materiality_max >= 0.6 AND diff_summary.exists",
    )
    client = MagicMock()
    client.complete = MagicMock(return_value=EvalJudgeVerdict(
        passed=False, reasoning="No flagged event; system missed the disclosure.",
        partial_credit=0.0,
    ))

    grade = grade_event(db, client, event, accession_to_grade="acc-test")
    assert grade.binary_result is None
    assert grade.graded_pass is False


def test_grade_event_use_judge_on_none_disabled(db):
    event = _event(pass_criteria="flagged_events.materiality_max >= 0.6")
    client = MagicMock()
    client.complete = MagicMock(side_effect=AssertionError("judge should NOT be called"))

    grade = grade_event(
        db, client, event, accession_to_grade="acc-test", use_judge_on_none=False,
    )
    assert grade.binary_result is None
    assert grade.graded_pass is False
    assert "judge disabled" in grade.notes
