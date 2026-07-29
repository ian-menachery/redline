"""Settings loader for redline.

Loads ``config/settings.toml`` into a typed Pydantic tree via
``RedlineConfig.from_toml()``. The toml file holds operational tunables only;
per-provider price rates live in code (`src/redline/llm/client.py`) because
they change too infrequently to warrant a config knob.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class OpenAIConfig(BaseModel):
    cheap_model: str
    quality_model: str


class AnthropicConfig(BaseModel):
    cheap_model: str
    quality_model: str


class LLMConfig(BaseModel):
    provider: str = "openai"
    openai: OpenAIConfig
    anthropic: AnthropicConfig
    # Hard stop: refuse any LLM call once cumulative logged spend
    # (SUM(cost_usd) in llm_call_log for the active DB) reaches this. Counts
    # ALL prior spend in that DB, not just the current run.
    max_spend_usd: float = 3.0


class DiffConfig(BaseModel):
    min_words: int = 22
    normalize_tokens: bool = True
    number_only_skip: bool = True
    materiality_threshold: float = Field(ge=0.0, le=1.0, default=0.6)
    comparison_strategy: str = "most_recent_same_type"


class CorrelatorConfig(BaseModel):
    window_days: int = 14


class PollerConfig(BaseModel):
    cadence_seconds: int = 900
    edgar_user_agent: str


class StorageConfig(BaseModel):
    db_path: str


class ValuationConfig(BaseModel):
    """DCF valuation layer (Subsystem 7). See NOTES.md §6.

    Per-company driver schedules, WACC, terminal growth, reference price and
    known-FCF validation constants live in ``assumptions_path`` (YAML); these
    are the global tunables.
    """

    projection_years: int = 5
    terminal_growth_default: float = 0.025
    sensitivity_band_pct: float = 0.02
    assumptions_path: str = "config/valuation/assumptions.yaml"
    fcf_mapping_path: str = "config/valuation/fcf_mapping_v1.yaml"
    # Fractional tolerance for validate_fcf: reconstructed vs hand-recorded
    # known_fcf. Above this, the CIK ships as "unvalidated base."
    fcf_validation_tolerance: float = 0.10
    reference_price_stale_days: int = 120
    # A guidance figure revalues only at/above this confidence (and with a
    # populated period/basis); below -> stored as manual_review, not a trigger.
    min_trigger_confidence: float = 0.75


class RedlineConfig(BaseModel):
    llm: LLMConfig
    diff: DiffConfig
    correlator: CorrelatorConfig
    poller: PollerConfig
    storage: StorageConfig
    # Optional so existing settings.toml files (and tests) load without a
    # [valuation] section; defaults apply.
    valuation: ValuationConfig = Field(default_factory=ValuationConfig)

    @classmethod
    def from_toml(cls, path: str | Path = "config/settings.toml") -> "RedlineConfig":
        with Path(path).open("rb") as f:
            data = tomllib.load(f)
        # REDLINE_DB_PATH overrides storage.db_path. Used by the hosted
        # Streamlit Cloud deployment to point at the committed read-only
        # snapshot (data/demo.db) without disturbing local poller writes
        # to the gitignored data/redline.db.
        if env_db := os.environ.get("REDLINE_DB_PATH"):
            data.setdefault("storage", {})["db_path"] = env_db
        return cls(**data)
