"""Tests for point-in-time-critical helpers in the eval replay harness.

``_period_label_to_iso`` maps a fiscal label to the ISO ``period_of_report``
date used to select which prior filing to diff against — a wrong mapping
silently grades against the wrong filing, so it is worth pinning. The
prior-10-K selector is covered with a mocked ``edgar.Company`` (no network).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from redline.eval.replay import _find_prior_period_10k, _period_label_to_iso


@pytest.mark.parametrize("label,expected", [
    ("FY2022", "2022-12-31"),
    ("FY2026", "2026-12-31"),
    ("fy2023", "2023-12-31"),      # case-insensitive
    (" FY2024 ", "2024-12-31"),    # surrounding whitespace tolerated
    ("Q1 2024", "2024-03-31"),
    ("Q2 2024", "2024-06-30"),
    ("Q3 2024", "2024-09-30"),
    ("Q4 2024", "2024-12-31"),
])
def test_period_label_to_iso_valid(label, expected):
    assert _period_label_to_iso(label) == expected


@pytest.mark.parametrize("label", ["", "2024", "FYxx", "Q5 2024", "Q3", "garbage"])
def test_period_label_to_iso_invalid_returns_none(label):
    assert _period_label_to_iso(label) is None


def test_find_prior_period_10k_picks_most_recent_before(monkeypatch):
    filings = [
        SimpleNamespace(filing_date="2022-02-20"),
        SimpleNamespace(filing_date="2024-02-15"),  # most recent BEFORE cutoff
        SimpleNamespace(filing_date="2025-02-18"),  # after cutoff -> excluded
        SimpleNamespace(filing_date="2023-02-21"),
    ]
    fake_company = SimpleNamespace(get_filings=lambda form: filings)
    monkeypatch.setattr("redline.eval.replay.edgar.Company", lambda ticker: fake_company)

    got = _find_prior_period_10k(ticker="PLTR", current_filed_date="2025-01-01")
    assert got is not None and got.filing_date == "2024-02-15"


def test_find_prior_period_10k_none_when_all_after(monkeypatch):
    filings = [SimpleNamespace(filing_date="2025-02-18")]
    fake_company = SimpleNamespace(get_filings=lambda form: filings)
    monkeypatch.setattr("redline.eval.replay.edgar.Company", lambda ticker: fake_company)

    assert _find_prior_period_10k(ticker="PLTR", current_filed_date="2024-01-01") is None
