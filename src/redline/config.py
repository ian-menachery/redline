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
    # Default max output tokens for a completion when a call site doesn't override.
    max_tokens_default: int = Field(default=4096, ge=1)


class DiffConfig(BaseModel):
    min_words: int = 22
    normalize_tokens: bool = True
    number_only_skip: bool = True
    materiality_threshold: float = Field(ge=0.0, le=1.0, default=0.6)
    comparison_strategy: str = "most_recent_same_type"
    # Char caps on the prior/current text handed to the Stage 2 gate and the
    # Stage 3 summary prompts (cost/context control).
    gate_context_chars: int = Field(default=6000, ge=1)
    summary_context_chars: int = Field(default=8000, ge=1)
    # Dashboard "high severity" band; the "medium" band is materiality_threshold.
    severity_high: float = Field(default=0.8, ge=0.0, le=1.0)


class CorrelatorConfig(BaseModel):
    window_days: int = 14
    # Insider-baseline lookback for volume/direction signals (NOTES.md §3.1).
    baseline_months: int = Field(default=12, ge=1)
    # Min historical discretionary trades before a per-insider signal scores
    # (below this the signal abstains rather than guess).
    min_baseline_trades: int = Field(default=3, ge=1)
    # Anomaly-score saturation points: the cluster score hits 1.0 at this many
    # same-direction insiders; the volume z-score hits 1.0 at this many stdevs.
    cluster_saturation: float = Field(default=3.0, gt=0.0)
    zscore_saturation: float = Field(default=2.0, gt=0.0)


class PollerConfig(BaseModel):
    cadence_seconds: int = 900
    edgar_user_agent: str
    # Pipeline retry policy (fetcher + diff analyzer): a stale failure is retried
    # up to max_retries times, no sooner than retry_after_hours after the last
    # attempt; then -> failed_permanent. See ARCHITECTURE.md §7.
    max_retries: int = Field(default=3, ge=0)
    retry_after_hours: int = Field(default=1, ge=0)


class StorageConfig(BaseModel):
    db_path: str
    # SQLite busy_timeout: how long a write waits on a competing writer before
    # raising SQLITE_BUSY (poller vs. a manual/overlapping run_once).
    busy_timeout_ms: int = Field(default=5000, ge=0)


class ValuationConfig(BaseModel):
    """DCF valuation layer (Subsystem 7). See NOTES.md §6.

    Per-company driver schedules, WACC, terminal growth, reference price and
    known-FCF validation constants live in ``assumptions_path`` (YAML); these
    are the global tunables.
    """

    projection_years: int = Field(default=5, ge=1)
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
    # Fractional band within which a new guidance figure counts as "reaffirmed"
    # (vs raised/lowered) against the prior period.
    guidance_reaffirm_tolerance: float = 0.005
    # Char cap on the 8-K EX-99.1 exhibit text handed to the guidance extractor.
    max_exhibit_chars: int = Field(default=24000, ge=1)
    # Guidance-extraction eval: value-match tolerance and the F1 pass bar.
    guidance_eval_tolerance: float = 0.02
    guidance_eval_f1_pass: float = Field(default=0.8, ge=0.0, le=1.0)


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
