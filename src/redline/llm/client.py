"""Provider-agnostic LLM client with exception-driven OpenAI -> Anthropic fallover.

Phase 1 starts on OpenAI (`gpt-4o-mini` cheap / `gpt-4o` quality) to consume
Ian's $4.98 of OpenAI credits. When OpenAI returns ``insufficient_quota``, the
client logs a ``provider_switch`` event and flips its process-level
``_active_provider`` flag to ``anthropic`` (`claude-haiku-4-5` / `claude-sonnet-4-6`).
Subsequent calls in the same process skip OpenAI entirely. A fresh process
restarts on OpenAI by default — if credits are still out at that point, the
first call falls over again immediately (cheap: a single fast API error).

See ARCHITECTURE.md §9 (Provider Fallover) for the design and CLAUDE.md §9
for the role-to-model mapping and per-call-site schemas.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import TypeVar

import anthropic
import openai
from pydantic import BaseModel, ValidationError

from redline.config import RedlineConfig
from redline.llm.log import log_call, log_provider_switch

T = TypeVar("T", bound=BaseModel)

_LOG = logging.getLogger(__name__)

# Per-1M-token rates ($). Update when providers re-price.
_OPENAI_RATES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),     # (input, output)
    "gpt-4o":      (2.50, 10.00),
}
# Anthropic rates: (input, output, cache_write_5min, cache_read).
_ANTHROPIC_RATES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-4-5":  (1.00, 5.00,  1.25, 0.10),
    "claude-sonnet-4-6": (3.00, 15.00, 3.75, 0.30),
}


def _openai_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    rate_in, rate_out = _OPENAI_RATES.get(model, (0.0, 0.0))
    return (tokens_in * rate_in + tokens_out * rate_out) / 1_000_000


def _anthropic_cost(
    model: str,
    tokens_in: int,
    tokens_out: int,
    cache_write: int,
    cache_read: int,
) -> float:
    rates = _ANTHROPIC_RATES.get(model)
    if rates is None:
        return 0.0
    ri, ro, rcw, rcr = rates
    return (
        tokens_in * ri
        + tokens_out * ro
        + cache_write * rcw
        + cache_read * rcr
    ) / 1_000_000


def _is_openai_quota_exhausted(e: BaseException) -> bool:
    """True if ``e`` indicates OpenAI credits exhausted.

    ``insufficient_quota`` surfaces on multiple exception classes depending on
    SDK version + response shape. Check both the structured ``body.error.code``
    and the stringified message, since hosted-key tiers occasionally raise it
    via ``RateLimitError`` without the code field populated.
    """
    if not isinstance(e, (openai.RateLimitError, openai.BadRequestError, openai.APIStatusError)):
        return False
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("code") == "insufficient_quota":
            return True
    if getattr(e, "code", None) == "insufficient_quota":
        return True
    msg = str(e).lower()
    return "insufficient_quota" in msg or "exceeded your current quota" in msg


class LLMClient:
    """Single-process LLM client with role-based model dispatch and fallover.

    Construct once per process. The active provider is process-state — once a
    call triggers fallover, every subsequent call from this instance (and any
    other instance sharing the same process) uses Anthropic.
    """

    def __init__(self, config: RedlineConfig, db: sqlite3.Connection) -> None:
        self.config = config
        self.db = db
        self._active_provider: str = config.llm.provider
        self._openai = openai.OpenAI()        # reads OPENAI_API_KEY
        self._anthropic = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    @property
    def active_provider(self) -> str:
        return self._active_provider

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        role: str,                       # "cheap" | "quality"
        call_site: str,                  # diff_gate | diff_summary | correlator | eval_judge
        prompt_version: str,
        reusable_context: str | None = None,
        max_tokens: int = 4096,
    ) -> T:
        """Run one LLM call against the active provider; return a validated schema instance.

        ``reusable_context`` is the large-and-shared prefix (e.g. the prior
        filing section being diffed across multiple Stage 3 calls in one batch).
        On Anthropic it's marked with ``cache_control: ephemeral`` for the
        ~90% read-cost discount; OpenAI auto-caches prefixes >1024 tokens with
        no client-side action, so we prepend it to the system content and let
        OpenAI's caching layer handle it.
        """
        if role not in ("cheap", "quality"):
            raise ValueError(f"role must be 'cheap' or 'quality', got {role!r}")

        if self._active_provider == "openai":
            try:
                return self._openai_complete(
                    system=system, user=user, schema=schema, role=role,
                    call_site=call_site, prompt_version=prompt_version,
                    reusable_context=reusable_context, max_tokens=max_tokens,
                )
            except Exception as e:
                if _is_openai_quota_exhausted(e):
                    _LOG.warning(
                        "OpenAI quota exhausted (%s: %s); falling over to Anthropic.",
                        type(e).__name__, e,
                    )
                    log_provider_switch(
                        self.db,
                        from_provider="openai",
                        to_provider="anthropic",
                        reason=f"{type(e).__name__}: {e}",
                    )
                    self._active_provider = "anthropic"
                    # fall through to Anthropic
                else:
                    raise

        return self._anthropic_complete(
            system=system, user=user, schema=schema, role=role,
            call_site=call_site, prompt_version=prompt_version,
            reusable_context=reusable_context, max_tokens=max_tokens,
        )

    # ---- OpenAI -----------------------------------------------------------

    def _openai_complete(
        self, *, system: str, user: str, schema: type[T], role: str,
        call_site: str, prompt_version: str,
        reusable_context: str | None, max_tokens: int,
    ) -> T:
        model = (
            self.config.llm.openai.cheap_model if role == "cheap"
            else self.config.llm.openai.quality_model
        )
        # OpenAI auto-caches prefixes >1024 tokens. Put the large context FIRST
        # so the cacheable prefix is identical across calls in the batch.
        sys_content = (
            f"{reusable_context}\n\n---\n\n{system}"
            if reusable_context else system
        )

        start = time.monotonic()
        try:
            resp = self._openai.beta.chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": sys_content},
                    {"role": "user", "content": user},
                ],
                response_format=schema,
                max_tokens=max_tokens,
            )
        except ValidationError as e:
            log_call(
                self.db, provider="openai", model=model, call_site=call_site,
                prompt_version=prompt_version, tokens_in=0, tokens_out=0,
                cost_usd=0.0,
                latency_ms=int((time.monotonic() - start) * 1000),
                cache_hit=False, status="parse_error", error_reason=str(e),
            )
            raise
        latency_ms = int((time.monotonic() - start) * 1000)

        parsed = resp.choices[0].message.parsed
        if parsed is None:
            raise RuntimeError(
                f"OpenAI returned no parsed output for {call_site}; "
                f"raw content: {resp.choices[0].message.content!r}"
            )

        usage = resp.usage
        # OpenAI's automatic prompt caching reports cached_tokens under
        # prompt_tokens_details (when available on the SDK version + model).
        details = getattr(usage, "prompt_tokens_details", None)
        cached_tokens = int(getattr(details, "cached_tokens", 0) or 0) if details else 0

        cost = _openai_cost(model, usage.prompt_tokens, usage.completion_tokens)

        log_call(
            self.db, provider="openai", model=model, call_site=call_site,
            prompt_version=prompt_version,
            tokens_in=int(usage.prompt_tokens),
            tokens_out=int(usage.completion_tokens),
            cost_usd=cost, latency_ms=latency_ms,
            cache_hit=cached_tokens > 0, status="ok",
        )
        return parsed

    # ---- Anthropic --------------------------------------------------------

    def _anthropic_complete(
        self, *, system: str, user: str, schema: type[T], role: str,
        call_site: str, prompt_version: str,
        reusable_context: str | None, max_tokens: int,
    ) -> T:
        model = (
            self.config.llm.anthropic.cheap_model if role == "cheap"
            else self.config.llm.anthropic.quality_model
        )

        # Anthropic prompt caching: mark the large reusable context with
        # cache_control. Subsequent calls within ~5 minutes get a ~90% discount
        # on those tokens.
        if reusable_context:
            sys_blocks: list[dict] | str = [
                {
                    "type": "text",
                    "text": reusable_context,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": system},
            ]
        else:
            sys_blocks = system

        start = time.monotonic()
        try:
            resp = self._anthropic.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=sys_blocks,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
        except ValidationError as e:
            log_call(
                self.db, provider="anthropic", model=model, call_site=call_site,
                prompt_version=prompt_version, tokens_in=0, tokens_out=0,
                cost_usd=0.0,
                latency_ms=int((time.monotonic() - start) * 1000),
                cache_hit=False, status="parse_error", error_reason=str(e),
            )
            raise
        latency_ms = int((time.monotonic() - start) * 1000)

        parsed = resp.parsed_output
        if parsed is None:
            raise RuntimeError(
                f"Anthropic returned no parsed_output for {call_site}; "
                f"stop_reason={getattr(resp, 'stop_reason', None)}"
            )

        usage = resp.usage
        cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cost = _anthropic_cost(
            model, int(usage.input_tokens), int(usage.output_tokens),
            cache_write, cache_read,
        )

        log_call(
            self.db, provider="anthropic", model=model, call_site=call_site,
            prompt_version=prompt_version,
            # tokens_in counts everything billed as input, including cached reads
            tokens_in=int(usage.input_tokens) + cache_read,
            tokens_out=int(usage.output_tokens),
            cost_usd=cost, latency_ms=latency_ms,
            cache_hit=cache_read > 0, status="ok",
        )
        return parsed
