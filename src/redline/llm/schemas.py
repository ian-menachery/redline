"""Pydantic output schemas for LLM call sites.

See ARCHITECTURE.md §9 for the call-site-to-schema mapping and CLAUDE.md §9
for usage conventions. These shapes are deliberately small — the Stage 3
summary and the correlator verdict are the user-facing surface in the
dashboard, so terse and specific is the right default.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    """Base for schemas sent to OpenAI structured outputs.

    OpenAI requires ``additionalProperties: false`` (we enforce via
    ``model_config``) and no extra fields in the JSON response.
    """

    model_config = ConfigDict(extra="forbid")


class DiffGateDecision(_StrictModel):
    """Stage 2 binary classifier (``cheap`` role).

    Gates which Stage-1-surviving chunks proceed to Stage 3 synthesis.
    Reason should be one short sentence — it lands in the dashboard and the
    eval analysis when a call disagrees with ground truth.
    """

    substantive: bool = Field(
        ..., description="True if a thoughtful reader would want this change flagged."
    )
    reason: str = Field(..., description="One short sentence justifying the call.")


class DiffSummary(_StrictModel):
    """Stage 3 summary (``quality`` role).

    User-facing on the dashboard. Materiality is a 0-1 score; values at or
    above ``diff.materiality_threshold`` (default 0.6) contribute to a
    ``flagged_events`` row.
    """

    change_type: Literal["addition", "removal", "modification", "restructure"]
    materiality: float = Field(ge=0.0, le=1.0)
    summary: str = Field(..., description="1-3 sentences for dashboard display.")
    affected_topics: list[str] = Field(
        default_factory=list,
        description="Short topic tags (e.g. 'deposits', 'capital_ratios', 'generative_ai').",
    )


class CorrelatorVerdict(_StrictModel):
    """Synthesized verdict from the insider-trading correlator (``quality`` role).

    One call per filing event, not per transaction. Drivers name the specific
    transactions or patterns that pushed the score above threshold.
    """

    anomalous: bool
    drivers: list[str] = Field(
        default_factory=list,
        description="Named transactions or patterns that drove the verdict.",
    )
    confidence: float = Field(ge=0.0, le=1.0)


class EvalJudgeVerdict(_StrictModel):
    """LLM-as-judge fallback (``quality`` role).

    Used by the eval harness when binary ``pass_criteria`` is inapplicable or
    contradicted by inspection. ``partial_credit`` allows nuanced scoring.
    """

    passed: bool
    reasoning: str
    partial_credit: float = Field(ge=0.0, le=1.0)
