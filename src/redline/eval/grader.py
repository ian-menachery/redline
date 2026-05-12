"""Eval harness grading: binary pass_criteria first, LLM judge fallback.

Per ARCHITECTURE.md §11. The ``pass_criteria`` string from each
``EvalEvent`` is evaluated as a Python expression against a context dict
built from the run's outputs. YAML idioms (``AND``, ``OR``, ``true``,
``false``) are normalized to Python keywords first.

When ``pass_criteria`` can't be evaluated cleanly (NameError /
AttributeError / etc. — usually because the run produced no output for
the referenced field), we fall back to the LLM judge with ``llm_judge_rubric``
and the full run-output context.

The eval YAML is committed and tagged at pre-registration time, so
controlled ``eval()`` is safe — the criteria are not user input.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from redline.eval.models import EvalEvent
from redline.llm.client import LLMClient
from redline.llm.schemas import EvalJudgeVerdict


@dataclass
class Grade:
    """Outcome of grading a single eval event."""

    event_id: str
    binary_result: bool | None       # None = pass_criteria couldn't evaluate
    judge_result: EvalJudgeVerdict | None
    graded_pass: bool
    notes: str

    def as_eval_runs_row(self) -> dict:
        """Project into ``eval_runs`` table fields."""
        return {
            "event_id": self.event_id,
            "binary_result": (
                int(self.binary_result) if isinstance(self.binary_result, bool) else None
            ),
            "judge_result": (
                json.dumps(self.judge_result.model_dump()) if self.judge_result else None
            ),
            "graded_pass": int(self.graded_pass),
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# pass_criteria evaluation
# ---------------------------------------------------------------------------

_YAML_TO_PY = [
    (re.compile(r"\bAND\b"), "and"),
    (re.compile(r"\bOR\b"), "or"),
    (re.compile(r"\bNOT\b"), "not"),
    (re.compile(r"\btrue\b"), "True"),
    (re.compile(r"\bfalse\b"), "False"),
    (re.compile(r"\bnull\b"), "None"),
]


def normalize_pass_criteria(criteria: str) -> str:
    """Convert YAML-style booleans/operators to Python."""
    for pat, repl in _YAML_TO_PY:
        criteria = pat.sub(repl, criteria)
    return criteria


_SAFE_BUILTINS: dict[str, Any] = {
    "any": any, "all": all, "len": len, "max": max, "min": min,
    "abs": abs, "round": round, "sum": sum,
    "True": True, "False": False, "None": None,
}


def evaluate_pass_criteria(criteria: str, context: dict) -> bool | None:
    """Evaluate ``criteria`` against ``context``.

    Returns ``True`` / ``False`` for clean evaluation; ``None`` when the
    expression couldn't be evaluated (caller falls back to LLM judge).

    Builtins (``any``, ``len``, etc.) are placed in globals — not locals —
    so they remain visible inside generator-expression scopes (genexps
    create their own scope but inherit globals).
    """
    # Merge context into the globals dict (single-namespace eval).
    # Generator expressions in eval() create their own scope that inherits
    # globals but NOT locals, so context names like ``flagged_events`` need
    # to live in globals for genexps like ``any(t in foo.bar for t in ...)``
    # to resolve cleanly.
    normalized = normalize_pass_criteria(criteria)
    namespace = {"__builtins__": {}, **_SAFE_BUILTINS, **context}
    try:
        result = eval(normalized, namespace)
        return bool(result)
    except (NameError, AttributeError, TypeError, ValueError, SyntaxError):
        return None


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------

def build_context(conn: sqlite3.Connection, accession: str) -> dict:
    """Build the evaluation context dict from the run's DB outputs.

    Keys exposed (each as a ``SimpleNamespace`` so pass_criteria can use
    dotted-access):

    - ``flagged_events`` — aggregated across all flagged_events rows for
      the accession (max materiality, list of reasons, exists flag)
    - ``diff_summary`` — union of fields across all diff summaries in
      flagged_events.diff_summary
    - ``correlator_payload`` — fields from the single correlator-anomaly
      flagged_events row, if any
    """
    flagged = conn.execute(
        """
        SELECT id, flag_reason, diff_summary, correlator_payload,
               materiality_max, flagged_at
        FROM flagged_events WHERE accession = ?
        """,
        (accession,),
    ).fetchall()

    flagged_events_ns = SimpleNamespace(
        exists=len(flagged) > 0,
        count=len(flagged),
        reasons=[r["flag_reason"] for r in flagged],
        materiality_max=max(
            (r["materiality_max"] for r in flagged if r["materiality_max"] is not None),
            default=None,
        ),
    )

    # Aggregate diff_summary fields across all summaries for this accession
    all_topics: list[str] = []
    all_change_types: list[str] = []
    all_materiality: list[float] = []
    all_summary_texts: list[str] = []
    for row in flagged:
        if not row["diff_summary"]:
            continue
        summaries = json.loads(row["diff_summary"])
        if not isinstance(summaries, list):
            summaries = [summaries]
        for s in summaries:
            if not isinstance(s, dict):
                continue
            all_topics.extend(s.get("affected_topics") or [])
            if s.get("change_type"):
                all_change_types.append(s["change_type"])
            if s.get("materiality") is not None:
                all_materiality.append(float(s["materiality"]))
            if s.get("summary"):
                all_summary_texts.append(s["summary"])
    diff_summary_ns = SimpleNamespace(
        affected_topics=all_topics,
        change_types=all_change_types,
        materiality_max=max(all_materiality, default=None),
        materialities=all_materiality,
        summaries=all_summary_texts,
        exists=len(all_materiality) > 0,
    )

    # Correlator payload (from the correlator_anomaly row if present)
    corr_ns = SimpleNamespace(
        anomalous=False, drivers=[], confidence=None, exists=False,
    )
    for row in flagged:
        if row["flag_reason"] != "correlator_anomaly" or not row["correlator_payload"]:
            continue
        payload = json.loads(row["correlator_payload"])
        v = payload.get("verdict", {})
        corr_ns = SimpleNamespace(
            anomalous=bool(v.get("anomalous")),
            drivers=list(v.get("drivers") or []),
            confidence=v.get("confidence"),
            exists=True,
            cluster_size=(payload.get("cluster") or {}).get("max_cluster_size"),
        )
        break  # first one wins

    return {
        "flagged_events": flagged_events_ns,
        "diff_summary": diff_summary_ns,
        "correlator_payload": corr_ns,
    }


# ---------------------------------------------------------------------------
# LLM judge fallback
# ---------------------------------------------------------------------------

JUDGE_SYSTEM = """You are an LLM judge for a financial-disclosure analysis eval.

Given:
- An eval event description (what the system was supposed to catch)
- A rubric (what counts as a pass)
- The system's actual outputs (flagged events, diff summaries, correlator verdict)

Return JSON conforming to the EvalJudgeVerdict schema:
- passed: true if the rubric is satisfied
- reasoning: 1-3 sentences citing the specific output that satisfies or fails the rubric
- partial_credit: 0.0-1.0 — 1.0 for clean pass, 0.0 for clean fail, intermediate for "close but missed key elements"

Be specific. Cite actual topic strings, materiality scores, or driver names from the output. Generic responses like "looks good" are not useful.

Return strictly the JSON object — no preamble, no markdown fence.
"""


def call_judge(
    client: LLMClient,
    *,
    event: EvalEvent,
    context: dict,
) -> EvalJudgeVerdict:
    """Run the LLM-as-judge fallback. Returns EvalJudgeVerdict."""
    flagged = context["flagged_events"]
    diff = context["diff_summary"]
    corr = context["correlator_payload"]
    user = (
        f"# Eval event\n"
        f"id: {event.id}\n"
        f"ticker: {event.ticker}\n"
        f"filing_type: {event.filing_type}\n"
        f"period: {event.period}\n"
        f"tests: {event.tests}\n\n"
        f"# Rubric\n{event.llm_judge_rubric}\n\n"
        f"# System outputs\n"
        f"flagged_events.exists = {flagged.exists}\n"
        f"flagged_events.count  = {flagged.count}\n"
        f"flagged_events.reasons = {flagged.reasons}\n"
        f"flagged_events.materiality_max = {flagged.materiality_max}\n\n"
        f"diff_summary.affected_topics = {diff.affected_topics}\n"
        f"diff_summary.change_types    = {diff.change_types}\n"
        f"diff_summary.materiality_max = {diff.materiality_max}\n"
        f"diff_summary.summaries       = {diff.summaries[:5]}"
        f"{' ... (truncated)' if len(diff.summaries) > 5 else ''}\n\n"
        f"correlator_payload.exists      = {corr.exists}\n"
        f"correlator_payload.anomalous   = {corr.anomalous}\n"
        f"correlator_payload.drivers     = {corr.drivers}\n"
        f"correlator_payload.confidence  = {corr.confidence}\n"
    )
    return client.complete(
        system=JUDGE_SYSTEM,
        user=user,
        schema=EvalJudgeVerdict,
        role="quality",
        call_site="eval_judge",
        prompt_version="v1",
    )


# ---------------------------------------------------------------------------
# Hybrid grader
# ---------------------------------------------------------------------------

def grade_event(
    conn: sqlite3.Connection,
    client: LLMClient,
    event: EvalEvent,
    *,
    accession_to_grade: str,
    use_judge_on_none: bool = True,
) -> Grade:
    """Grade one event against the run's outputs in ``conn``.

    Binary first; LLM judge fallback when binary returns None (criterion
    couldn't evaluate cleanly — typically a missing-data situation).
    """
    context = build_context(conn, accession_to_grade)
    binary = evaluate_pass_criteria(event.pass_criteria, context)

    if binary is True:
        return Grade(
            event_id=event.id, binary_result=True, judge_result=None,
            graded_pass=True,
            notes=f"pass_criteria satisfied for accession {accession_to_grade}",
        )
    if binary is False:
        return Grade(
            event_id=event.id, binary_result=False, judge_result=None,
            graded_pass=False,
            notes=f"pass_criteria evaluated False for accession {accession_to_grade}",
        )

    # binary is None — eval couldn't proceed (usually because the system
    # produced no output for a referenced field, e.g. flagged_events.* when
    # nothing was flagged).
    if not use_judge_on_none:
        return Grade(
            event_id=event.id, binary_result=None, judge_result=None,
            graded_pass=False,
            notes=f"pass_criteria did not evaluate; judge disabled",
        )

    judge = call_judge(client, event=event, context=context)
    return Grade(
        event_id=event.id, binary_result=None, judge_result=judge,
        graded_pass=judge.passed,
        notes=f"judge fallback (partial_credit={judge.partial_credit:.2f}): {judge.reasoning[:120]}",
    )
