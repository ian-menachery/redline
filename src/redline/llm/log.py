"""Writer for ``llm_call_log`` rows.

Every LLM call hits this module on the way out (success or failure) — see
``CLAUDE.md`` §9 rule: "Every LLM call is logged to SQLite ... No exceptions.
Logging lives in the LLM client wrapper; bypassing it is a bug."
"""
from __future__ import annotations

import datetime
import sqlite3


def log_call(
    conn: sqlite3.Connection,
    *,
    call_site: str,
    provider: str,
    model: str,
    prompt_version: str,
    tokens_in: int,
    tokens_out: int,
    cost_usd: float,
    latency_ms: int,
    cache_hit: bool,
    status: str = "ok",
    error_reason: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO llm_call_log (
            called_at, call_site, provider, model, prompt_version,
            tokens_in, tokens_out, cost_usd, latency_ms, cache_hit,
            status, error_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
            call_site, provider, model, prompt_version,
            tokens_in, tokens_out, cost_usd, latency_ms, int(cache_hit),
            status, error_reason,
        ),
    )
    return cur.lastrowid


def log_provider_switch(
    conn: sqlite3.Connection,
    *,
    from_provider: str,
    to_provider: str,
    reason: str,
) -> int:
    """Mark the OpenAI -> Anthropic fallover with a sentinel row.

    Zero tokens, zero cost, ``status='info'``. ``error_reason`` captures the
    triggering exception so we can correlate the switch back to a specific
    cause in ``NOTES.md`` §8.
    """
    return log_call(
        conn,
        call_site="provider_switch",
        provider=to_provider,
        model="-",
        prompt_version="-",
        tokens_in=0,
        tokens_out=0,
        cost_usd=0.0,
        latency_ms=0,
        cache_hit=False,
        status="info",
        error_reason=f"switched from {from_provider}: {reason}",
    )
