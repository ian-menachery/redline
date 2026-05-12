"""Stage 2: cheap-role LLM gate.

One call per Stage-1-surviving chunk. Routes to ``gpt-4o-mini`` while
OpenAI credits remain, then ``claude-haiku-4-5`` after fallover.
"""
from __future__ import annotations

from pathlib import Path

from redline.diff.filter import Stage1Change
from redline.llm.client import LLMClient
from redline.llm.schemas import DiffGateDecision

PROMPT_VERSION = "v1"


def _load_prompt(prompts_dir: str | Path) -> str:
    return Path(prompts_dir, f"diff_gate_{PROMPT_VERSION}.txt").read_text(encoding="utf-8")


def _format_user(section: str, change: Stage1Change) -> str:
    old = change.old or "(nothing — pure insertion)"
    new = change.new or "(nothing — pure deletion)"
    return (
        f"Section: {section}\n"
        f"Change type: {change.tag}\n\n"
        f"OLD:\n{old[:6000]}\n\n"
        f"NEW:\n{new[:6000]}"
    )


def gate(
    client: LLMClient,
    *,
    section: str,
    change: Stage1Change,
    prompts_dir: str | Path = "config/prompts",
) -> DiffGateDecision:
    """Run a single chunk through the Stage 2 gate."""
    system = _load_prompt(prompts_dir)
    user = _format_user(section, change)
    return client.complete(
        system=system,
        user=user,
        schema=DiffGateDecision,
        role="cheap",
        call_site="diff_gate",
        prompt_version=PROMPT_VERSION,
    )
