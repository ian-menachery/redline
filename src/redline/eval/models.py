"""Pydantic schemas for the eval harness.

Scoped to the absolute minimum needed for Phase 0.5 pre-registration: the
``EvalEvent`` model and a loader that validates `config/eval_events.yaml`.
Phase 1 will add the grading and replay logic alongside.

See ARCHITECTURE.md §11 and CLAUDE.md §4.5 for the design rationale.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

SubsystemTag = Literal["diff_analyzer", "correlator", "parser", "event_detection"]
FilingType = Literal["10-K", "10-Q", "8-K", "4", "4-cluster"]


class EvalEvent(BaseModel):
    """A pre-registered eval event.

    The ``locked_at`` timestamp inside each entry is the pre-registration
    receipt — cherry-picking is structurally prevented because the file is
    committed and tagged before any measurement code runs.
    """

    id: str = Field(..., description="Stable unique identifier, e.g. 'key_10k_fy22'.")
    ticker: str = Field(..., min_length=1)
    filing_type: FilingType
    period: str = Field(
        ...,
        description=(
            "Free-form period label. For 10-K/10-Q: fiscal period (e.g. 'FY2022', 'Q3 2024'). "
            "For 8-K: filing date or short window. For Form 4 clusters: ISO date range."
        ),
    )
    tests: list[SubsystemTag] = Field(..., min_length=1)
    pass_criteria: str = Field(
        ...,
        description="Rule expression evaluated against the run's outputs. See ARCHITECTURE.md §11.",
    )
    llm_judge_rubric: str = Field(
        ...,
        description="Free-text rubric for the LLM-as-judge fallback when pass_criteria is contradicted or inapplicable.",
    )
    locked_at: datetime = Field(
        ...,
        description="UTC pre-registration timestamp. Sealed by the eval-pre-registration-v1 tag.",
    )


def load_eval_events(path: Path) -> list[EvalEvent]:
    """Load and validate eval events from a YAML file.

    Raises ``pydantic.ValidationError`` if any entry fails schema validation.
    """
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, list):
        raise ValueError(f"Top-level YAML must be a list, got {type(raw).__name__}")
    return [EvalEvent(**entry) for entry in raw]
