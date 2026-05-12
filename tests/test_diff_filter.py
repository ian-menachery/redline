"""Tests for Stage 1 of the diff filter (rule-based, no LLM).

Covers: normalization regexes (dates, currency, pct, big ints),
paragraph splitting, identity case (no changes), all-cosmetic case
(eliminated by normalization), substantive change (preserved),
min_words filtering.
"""
from __future__ import annotations

from redline.diff.filter import (
    CURRENCY_RE,
    DATE_RE,
    INT_RE,
    PCT_RE,
    Stage1Change,
    normalize,
    split_paragraphs,
    stage1_filter,
)


# ----- regexes -------------------------------------------------------------


def test_date_regex_matches_common_formats():
    assert DATE_RE.findall("filed September 30, 2024 and 2023-04-15") == [
        "September 30, 2024",
        "2023-04-15",
    ]


def test_date_regex_matches_quarters_and_fy():
    assert DATE_RE.findall("Q3 2024 and FY 2023") == ["Q3 2024", "FY 2023"]


def test_currency_regex_matches_common_formats():
    matches = CURRENCY_RE.findall("$1.2 billion and $700,000 and $.05")
    # Currency tokens, in order
    assert any("1.2 billion" in m for m in matches)
    assert any("700,000" in m for m in matches)


def test_pct_regex_matches_percent_word():
    assert PCT_RE.findall("12.3% and 18 percent and 5 percentage points") == [
        "12.3%",
        "18 percent",
        "5 percentage points",
    ]


def test_int_regex_skips_small_numbers():
    # Should match 3,838 / 3838 / 12,345 but not 99 (small enumerators).
    # Comma-thousands form takes precedence (matches "12,345" not "345").
    assert INT_RE.findall("99 employees grew to 3838 then 12,345") == ["3838", "12,345"]


# ----- normalize() ---------------------------------------------------------


def test_normalize_replaces_all_token_classes():
    text = "On September 30, 2024 we had 3,838 employees and $700,000 in cash (12.3%)."
    norm = normalize(text)
    assert "<DATE>" in norm
    assert "<CURRENCY>" in norm
    assert "<PCT>" in norm
    assert "<INT>" in norm
    assert "3,838" not in norm
    assert "September 30, 2024" not in norm


def test_normalize_is_idempotent():
    text = "On 2024-01-01 we had $1.2 million."
    once = normalize(text)
    twice = normalize(once)
    assert once == twice


def test_normalize_preserves_real_words():
    text = "Risk of customer reluctance to generative AI products."
    assert normalize(text) == text


# ----- split_paragraphs() --------------------------------------------------


def test_split_paragraphs_on_blank_lines():
    text = "para one\n\npara two\n\n\npara three"
    assert split_paragraphs(text) == ["para one", "para two", "para three"]


def test_split_paragraphs_collapses_internal_whitespace():
    text = "this   has    weird\nspacing"
    # Single paragraph with collapsed whitespace
    assert split_paragraphs(text) == ["this has weird spacing"]


def test_split_paragraphs_drops_empty():
    text = "\n\n\n\n"
    assert split_paragraphs(text) == []


# ----- stage1_filter() -----------------------------------------------------


def test_stage1_filter_identity_returns_empty():
    text = (
        "Our top three customers together accounted for 18% of revenue.\n\n"
        "We operate in the United States and internationally.\n\n"
        "Our headcount as of December 31, 2023 was 3,735 full-time employees."
    )
    assert stage1_filter(text, text) == []


def test_stage1_filter_all_cosmetic_eliminated_by_normalization():
    """Dates / dollar / pct / headcount-only changes should produce no survivors
    once normalization is applied."""
    old = (
        "Our top three customers together accounted for 17% of revenue for the year "
        "ended December 31, 2022. Our headcount as of December 31, 2022 was 3,838 "
        "full-time employees. Cash and equivalents totaled $2.6 billion."
    )
    new = (
        "Our top three customers together accounted for 18% of revenue for the year "
        "ended December 31, 2023. Our headcount as of December 31, 2023 was 3,735 "
        "full-time employees. Cash and equivalents totaled $3.0 billion."
    )
    survivors = stage1_filter(old, new, normalize_tokens=True)
    assert survivors == []


def test_stage1_filter_substantive_change_preserved():
    """Adding a net-new bullet with new substance should survive Stage 1."""
    old = (
        "Risk factors:\n\n"
        "We face competition from established enterprise software vendors and emerging startups.\n\n"
        "Our growth depends on our ability to attract and retain customers."
    )
    # New paragraph needs > min_words=22; this one has ~30.
    new = (
        "Risk factors:\n\n"
        "We face competition from established enterprise software vendors and emerging startups.\n\n"
        "Reluctance of customers to purchase products incorporating generative AI may "
        "limit adoption of our Artificial Intelligence Platform offerings, and adverse "
        "regulatory developments around AI may further constrain our ability to expand.\n\n"
        "Our growth depends on our ability to attract and retain customers."
    )
    survivors = stage1_filter(old, new)
    assert len(survivors) >= 1
    has_gen_ai = any(
        s.new and "generative AI" in s.new and "Artificial Intelligence Platform" in s.new
        for s in survivors
    )
    assert has_gen_ai


def test_stage1_filter_below_min_words_dropped():
    """A change shorter than min_words should be filtered out."""
    old = "Hello world."
    new = "Hello world today."
    # 2-3 word change, below default min_words=22
    assert stage1_filter(old, new) == []


def test_stage1_filter_above_min_words_kept():
    """A long-enough change should be retained."""
    old = "paragraph one.\n\nparagraph two stays the same."
    new_para_long = " ".join(["word"] * 30)  # 30 words
    new = f"paragraph one.\n\n{new_para_long}\n\nparagraph two stays the same."
    survivors = stage1_filter(new, new)  # identity check first
    assert survivors == []
    survivors = stage1_filter(old, new)
    # The 30-word insert should survive
    assert any(s.tag in ("insert", "replace") for s in survivors)


def test_stage1_change_max_words_helper():
    c = Stage1Change(tag="replace", old="one two three", new="four five")
    assert c.max_words() == 3


def test_stage1_change_handles_none_chunks():
    c_insert = Stage1Change(tag="insert", old=None, new="a b c d e")
    c_delete = Stage1Change(tag="delete", old="a b c", new=None)
    assert c_insert.max_words() == 5
    assert c_delete.max_words() == 3


def test_stage1_filter_can_disable_normalization():
    """When normalize_tokens=False, cosmetic changes are NOT eliminated."""
    old = " ".join(["headcount", "of", "3838", "as", "of", "Dec", "31"] + ["filler"] * 20)
    new = " ".join(["headcount", "of", "3735", "as", "of", "Dec", "31"] + ["filler"] * 20)
    with_norm = stage1_filter(old, new, normalize_tokens=True)
    without_norm = stage1_filter(old, new, normalize_tokens=False)
    assert with_norm == []
    assert len(without_norm) >= 1
