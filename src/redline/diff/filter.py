"""Stage 1 of the diff filter: deterministic, no LLM.

Per ARCHITECTURE.md §4 the pipeline is:

  Step 1a — canonical-token normalization (dates / currency / percentages /
            large integers -> sentinel tokens). Eliminates the "headcount
            3,838 -> 3,735" class of changes before the diff runs.
  Step 1b — paragraph-level diff over normalized text + rule filtering
            (whitespace-only / citation-only / below-min-words dropped).

Surviving changes reference the ORIGINAL text — so Stage 2 and Stage 3 see
human-readable wording, not the sentinel-laden normalized form.

These regexes were validated during the Phase 0.5 PLTR FY22 vs FY23 10-K
spike (see NOTES.md §1 and spike/pltr_10k_riskdiff_spike.py).
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# Order matters: dates before currency before pct before int. Otherwise
# digits inside dates / dollar amounts get caught by INT_RE.

DATE_RE = re.compile(
    r"\b("
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}/\d{1,2}/\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|(?:Q[1-4]|Fiscal\s+Year|FY)\s*\d{4}"
    r")\b",
    re.IGNORECASE,
)
CURRENCY_RE = re.compile(
    r"\$\s*\d[\d,]*(?:\.\d+)?\s*(?:billion|million|thousand|B|M|K)?",
    re.IGNORECASE,
)
# Note: no trailing \b on PCT_RE. `\b` requires a word/non-word transition;
# after `%` (non-word) followed by space or EOS (also non-word), there is
# no boundary so the match fails. The discriminator (% or "percent" keyword)
# is distinctive enough without a trailing anchor.
PCT_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:%|percent(?:age)?(?:\s+points?)?)",
    re.IGNORECASE,
)
# Match comma-thousands form first (e.g. 3,838) then bare 3+ digits (e.g. 3838).
# Skips 1-2 digit integers so enumerators like "three" or "12 employees" pass.
INT_RE = re.compile(r"\b(?:\d{1,3}(?:,\d{3})+|\d{3,})\b")


def normalize(text: str) -> str:
    """Apply canonical-token replacements. Idempotent."""
    text = DATE_RE.sub("<DATE>", text)
    text = CURRENCY_RE.sub("<CURRENCY>", text)
    text = PCT_RE.sub("<PCT>", text)
    text = INT_RE.sub("<INT>", text)
    return text


def split_paragraphs(text: str) -> list[str]:
    """Split on blank lines; collapse internal whitespace within each paragraph."""
    paras = re.split(r"\n\s*\n+", text)
    return [re.sub(r"\s+", " ", p).strip() for p in paras if p.strip()]


@dataclass(frozen=True)
class Stage1Change:
    """A change that survived Stage 1.

    ``tag`` is one of difflib's opcode tags (``replace``/``insert``/``delete``).
    ``old`` / ``new`` are the ORIGINAL (un-normalized) chunk text; either can
    be ``None`` for pure inserts/deletes.
    """

    tag: str
    old: str | None
    new: str | None

    def max_words(self) -> int:
        return max(
            len((self.old or "").split()),
            len((self.new or "").split()),
        )


def stage1_filter(
    old: str,
    new: str,
    *,
    min_words: int = 22,
    normalize_tokens: bool = True,
) -> list[Stage1Change]:
    """Run Stage 1 over a pair of section texts.

    Returns surviving changes ready for Stage 2 gating. Empty list when the
    two texts are byte-identical OR every diff falls below ``min_words``.
    """
    old_paras = split_paragraphs(old)
    new_paras = split_paragraphs(new)

    if normalize_tokens:
        old_keys = [normalize(p) for p in old_paras]
        new_keys = [normalize(p) for p in new_paras]
    else:
        old_keys = old_paras
        new_keys = new_paras

    sm = difflib.SequenceMatcher(a=old_keys, b=new_keys, autojunk=False)
    survivors: list[Stage1Change] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        old_chunk = "\n\n".join(old_paras[i1:i2]) if i1 < i2 else None
        new_chunk = "\n\n".join(new_paras[j1:j2]) if j1 < j2 else None
        change = Stage1Change(tag=tag, old=old_chunk, new=new_chunk)
        if change.max_words() < min_words:
            continue
        survivors.append(change)
    return survivors
