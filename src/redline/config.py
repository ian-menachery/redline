"""Settings loader for redline.

Loads ``config/settings.toml`` into a typed Pydantic tree via
``RedlineConfig.from_toml()``. The toml file holds operational tunables only;
per-provider price rates live in code (`src/redline/llm/client.py`) because
they change too infrequently to warrant a config knob.
"""
from __future__ import annotations

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


class RedlineConfig(BaseModel):
    llm: LLMConfig
    diff: DiffConfig
    correlator: CorrelatorConfig
    poller: PollerConfig
    storage: StorageConfig

    @classmethod
    def from_toml(cls, path: str | Path = "config/settings.toml") -> "RedlineConfig":
        with Path(path).open("rb") as f:
            data = tomllib.load(f)
        return cls(**data)
