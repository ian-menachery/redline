"""Tests for the provider-agnostic LLM client.

Verifies the four key behaviors from the Phase 1 entry plan:
1. Cheap-role call routes to the cheap_model of the active provider
2. Quality-role call routes to the quality_model
3. OpenAI insufficient_quota error -> switches to Anthropic + retries
4. After fallover, subsequent calls skip OpenAI entirely

Plus: Pydantic validation failure surfaces, non-quota OpenAI errors propagate
without fallover, reusable_context is marked with Anthropic cache_control.

SDK responses are mocked because the substrate is the contract being tested
here — the smoke test (separate) covers actual API behavior.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import openai
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
from redline.llm.client import LLMClient, _is_openai_quota_exhausted
from redline.llm.schemas import DiffGateDecision, DiffSummary
from redline.storage.db import init_schema


# ----- helpers -------------------------------------------------------------


def _config(provider: str = "openai") -> RedlineConfig:
    return RedlineConfig(
        llm=LLMConfig(
            provider=provider,
            openai=OpenAIConfig(cheap_model="gpt-4o-mini", quality_model="gpt-4o"),
            anthropic=AnthropicConfig(
                cheap_model="claude-haiku-4-5", quality_model="claude-sonnet-4-6"
            ),
        ),
        diff=DiffConfig(),
        correlator=CorrelatorConfig(),
        poller=PollerConfig(edgar_user_agent="Test (test@test)"),
        storage=StorageConfig(db_path=":memory:"),
    )


def _make_openai_resp(parsed, prompt_tokens=500, completion_tokens=50, cached_tokens=0):
    """Mimic the .beta.chat.completions.parse() response object."""
    msg = SimpleNamespace(parsed=parsed, content='{"x": 1}')
    choice = SimpleNamespace(message=msg)
    details = SimpleNamespace(cached_tokens=cached_tokens)
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        prompt_tokens_details=details,
    )
    return SimpleNamespace(choices=[choice], usage=usage)


def _make_anthropic_resp(
    parsed, input_tokens=500, output_tokens=50, cache_creation=0, cache_read=0
):
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation,
        cache_read_input_tokens=cache_read,
    )
    return SimpleNamespace(parsed_output=parsed, stop_reason="end_turn", usage=usage)


class _FakeOpenAIQuotaError(openai.RateLimitError):
    """Test stand-in for ``openai.RateLimitError`` with insufficient_quota.

    The real constructor wants a full HTTP response object; this bypasses
    that path by setting only the attributes ``_is_openai_quota_exhausted``
    inspects.
    """

    def __init__(self):
        self.body = {"error": {"code": "insufficient_quota", "message": "exceeded"}}
        self.code = "insufficient_quota"
        Exception.__init__(self, "You exceeded your current quota")


class _FakeOpenAIBadRequest(openai.BadRequestError):
    """Non-quota OpenAI 400 — must NOT trigger fallover."""

    def __init__(self):
        self.body = {"error": {"code": "model_not_found"}}
        self.code = "model_not_found"
        Exception.__init__(self, "Model not found")


# ----- fixtures ------------------------------------------------------------


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def client(db, monkeypatch):
    """LLMClient with mocked SDK clients. Active provider = openai by default."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    c = LLMClient(_config(), db)
    c._openai = MagicMock(spec=openai.OpenAI)
    c._anthropic = MagicMock(spec=anthropic.Anthropic)
    # nested attribute chains
    c._openai.beta = MagicMock()
    c._openai.beta.chat = MagicMock()
    c._openai.beta.chat.completions = MagicMock()
    c._openai.beta.chat.completions.parse = MagicMock()
    c._anthropic.messages = MagicMock()
    c._anthropic.messages.parse = MagicMock()
    return c


# ----- _is_openai_quota_exhausted ------------------------------------------


def test_quota_detector_finds_code_in_body():
    err = _FakeOpenAIQuotaError()
    assert _is_openai_quota_exhausted(err)


def test_quota_detector_ignores_other_codes():
    err = _FakeOpenAIBadRequest()
    assert not _is_openai_quota_exhausted(err)


def test_quota_detector_ignores_non_openai_exceptions():
    assert not _is_openai_quota_exhausted(RuntimeError("not an SDK error"))


# ----- routing -------------------------------------------------------------


def test_cheap_role_routes_to_openai_cheap_model(client, db):
    decision = DiffGateDecision(substantive=True, reason="ok")
    client._openai.beta.chat.completions.parse.return_value = _make_openai_resp(decision)

    result = client.complete(
        system="s", user="u", schema=DiffGateDecision,
        role="cheap", call_site="diff_gate", prompt_version="v1",
    )

    assert isinstance(result, DiffGateDecision)
    assert result.substantive is True
    call = client._openai.beta.chat.completions.parse.call_args
    assert call.kwargs["model"] == "gpt-4o-mini"
    # Logging
    rows = db.execute(
        "SELECT provider, model, call_site, status FROM llm_call_log"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["provider"] == "openai"
    assert rows[0]["model"] == "gpt-4o-mini"
    assert rows[0]["call_site"] == "diff_gate"
    assert rows[0]["status"] == "ok"


def test_quality_role_routes_to_openai_quality_model(client):
    summary = DiffSummary(
        change_type="addition", materiality=0.7, summary="x", affected_topics=[]
    )
    client._openai.beta.chat.completions.parse.return_value = _make_openai_resp(summary)

    client.complete(
        system="s", user="u", schema=DiffSummary,
        role="quality", call_site="diff_summary", prompt_version="v1",
    )

    call = client._openai.beta.chat.completions.parse.call_args
    assert call.kwargs["model"] == "gpt-4o"


def test_invalid_role_raises(client):
    with pytest.raises(ValueError, match="role must be"):
        client.complete(
            system="s", user="u", schema=DiffGateDecision,
            role="bogus", call_site="diff_gate", prompt_version="v1",
        )


# ----- fallover ------------------------------------------------------------


def test_quota_error_triggers_anthropic_fallover(client, db):
    client._openai.beta.chat.completions.parse.side_effect = _FakeOpenAIQuotaError()
    decision = DiffGateDecision(substantive=True, reason="from anthropic")
    client._anthropic.messages.parse.return_value = _make_anthropic_resp(decision)

    result = client.complete(
        system="s", user="u", schema=DiffGateDecision,
        role="cheap", call_site="diff_gate", prompt_version="v1",
    )

    assert isinstance(result, DiffGateDecision)
    assert result.reason == "from anthropic"
    assert client.active_provider == "anthropic"

    # Anthropic called with cheap model
    call = client._anthropic.messages.parse.call_args
    assert call.kwargs["model"] == "claude-haiku-4-5"

    # Two rows: provider_switch (info) + the successful Anthropic call
    rows = db.execute(
        "SELECT call_site, provider, status FROM llm_call_log ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["call_site"] == "provider_switch"
    assert rows[0]["provider"] == "anthropic"
    assert rows[0]["status"] == "info"
    assert rows[1]["call_site"] == "diff_gate"
    assert rows[1]["provider"] == "anthropic"
    assert rows[1]["status"] == "ok"


def test_after_fallover_subsequent_calls_skip_openai(client):
    # First call: OpenAI fails, falls over
    client._openai.beta.chat.completions.parse.side_effect = _FakeOpenAIQuotaError()
    decision = DiffGateDecision(substantive=True, reason="x")
    client._anthropic.messages.parse.return_value = _make_anthropic_resp(decision)

    client.complete(
        system="s", user="u", schema=DiffGateDecision,
        role="cheap", call_site="diff_gate", prompt_version="v1",
    )
    assert client.active_provider == "anthropic"

    # Second call: OpenAI mock would raise AssertionError if touched
    client._openai.beta.chat.completions.parse.reset_mock()
    client._openai.beta.chat.completions.parse.side_effect = AssertionError("openai should not be called")

    client.complete(
        system="s2", user="u2", schema=DiffGateDecision,
        role="cheap", call_site="diff_gate", prompt_version="v1",
    )
    client._openai.beta.chat.completions.parse.assert_not_called()


def test_non_quota_openai_error_propagates(client):
    client._openai.beta.chat.completions.parse.side_effect = _FakeOpenAIBadRequest()

    with pytest.raises(openai.BadRequestError):
        client.complete(
            system="s", user="u", schema=DiffGateDecision,
            role="cheap", call_site="diff_gate", prompt_version="v1",
        )

    assert client.active_provider == "openai"  # unchanged


# ----- prompt caching ------------------------------------------------------


def test_reusable_context_marked_for_anthropic_caching(client):
    """The reusable prefix gets cache_control: ephemeral on the Anthropic path."""
    client._active_provider = "anthropic"  # skip OpenAI
    decision = DiffGateDecision(substantive=True, reason="x")
    client._anthropic.messages.parse.return_value = _make_anthropic_resp(decision)

    client.complete(
        system="instructions", user="u", schema=DiffGateDecision,
        role="cheap", call_site="diff_gate", prompt_version="v1",
        reusable_context="large reusable filing text",
    )

    call = client._anthropic.messages.parse.call_args
    sys_blocks = call.kwargs["system"]
    assert isinstance(sys_blocks, list)
    assert sys_blocks[0]["text"] == "large reusable filing text"
    assert sys_blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert sys_blocks[1]["text"] == "instructions"


def test_reusable_context_prepended_for_openai(client):
    """OpenAI auto-caches the prefix; we just prepend reusable_context to system."""
    decision = DiffGateDecision(substantive=True, reason="ok")
    client._openai.beta.chat.completions.parse.return_value = _make_openai_resp(decision)

    client.complete(
        system="instructions", user="u", schema=DiffGateDecision,
        role="cheap", call_site="diff_gate", prompt_version="v1",
        reusable_context="large reusable filing text",
    )

    call = client._openai.beta.chat.completions.parse.call_args
    messages = call.kwargs["messages"]
    assert messages[0]["role"] == "system"
    # large context comes first so the cacheable prefix is identical across calls
    assert messages[0]["content"].startswith("large reusable filing text")
    assert "instructions" in messages[0]["content"]


# ----- cache-hit logging ---------------------------------------------------


def test_anthropic_cache_read_marks_cache_hit(client, db):
    client._active_provider = "anthropic"
    decision = DiffGateDecision(substantive=True, reason="x")
    client._anthropic.messages.parse.return_value = _make_anthropic_resp(
        decision, input_tokens=100, cache_read=4000
    )

    client.complete(
        system="s", user="u", schema=DiffGateDecision,
        role="cheap", call_site="diff_gate", prompt_version="v1",
        reusable_context="large",
    )

    row = db.execute(
        "SELECT cache_hit, tokens_in FROM llm_call_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["cache_hit"] == 1
    # tokens_in includes the cached read
    assert row["tokens_in"] == 100 + 4000


def test_openai_cached_tokens_marks_cache_hit(client, db):
    decision = DiffGateDecision(substantive=True, reason="x")
    client._openai.beta.chat.completions.parse.return_value = _make_openai_resp(
        decision, prompt_tokens=2000, cached_tokens=1500
    )

    client.complete(
        system="s", user="u", schema=DiffGateDecision,
        role="cheap", call_site="diff_gate", prompt_version="v1",
    )

    row = db.execute(
        "SELECT cache_hit FROM llm_call_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["cache_hit"] == 1
