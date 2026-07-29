"""Stage 3: quality-role LLM summary.

One call per Stage-2-passing chunk. Uses ``reusable_context`` to mark the
prior-period section text with ``cache_control: ephemeral`` on Anthropic
(or rely on OpenAI's auto-cache); subsequent Stage 3 calls in the same
batch hit cache for ~90% read-cost reduction on the prior section.
"""
from __future__ import annotations

from pathlib import Path

from redline.diff.filter import Stage1Change
from redline.llm.client import LLMClient
from redline.llm.schemas import DiffSummary

PROMPT_VERSION = "v1"


def _load_prompt(prompts_dir: str | Path) -> str:
    return Path(prompts_dir, f"diff_summary_{PROMPT_VERSION}.txt").read_text(encoding="utf-8")


def _format_user(
    section: str, change: Stage1Change, gate_reason: str | None, context_chars: int,
) -> str:
    old = change.old or "(nothing — pure insertion)"
    new = change.new or "(nothing — pure deletion)"
    parts = [
        f"Section: {section}",
        f"Change type: {change.tag}",
    ]
    if gate_reason:
        parts.append(f"Stage 2 gate reason: {gate_reason}")
    parts.append("")
    parts.append("OLD:")
    parts.append(old[:context_chars])
    parts.append("")
    parts.append("NEW:")
    parts.append(new[:context_chars])
    return "\n".join(parts)


def summarize(
    client: LLMClient,
    *,
    section: str,
    change: Stage1Change,
    prior_section_text: str | None = None,
    gate_reason: str | None = None,
    prompts_dir: str | Path = "config/prompts",
    context_chars: int = 8000,
) -> DiffSummary:
    """Run a single chunk through the Stage 3 summary.

    ``context_chars`` caps the OLD/NEW text per side (DiffConfig.summary_context_chars).
    """
    system = _load_prompt(prompts_dir)
    user = _format_user(section, change, gate_reason, context_chars)
    return client.complete(
        system=system,
        user=user,
        schema=DiffSummary,
        role="quality",
        call_site="diff_summary",
        prompt_version=PROMPT_VERSION,
        reusable_context=prior_section_text,
    )
