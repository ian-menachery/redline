"""Tests for the guidance-extraction eval grader (`guidance_eval`)."""
from __future__ import annotations

import pytest

from redline.valuation.guidance_eval import grade_guidance


def _g(accession="A1", metric="revenue", period="FY2026", low=12.95, high=13.1,
       basis="gaap", unit="usd_billions"):
    return {"accession": accession, "metric": metric, "period": period,
            "low": low, "high": high, "basis": basis, "unit": unit}


def test_perfect_match():
    stats = grade_guidance([_g()], [_g()])
    assert stats["tp"] == 1 and stats["fp"] == 0 and stats["fn"] == 0
    assert stats["precision"] == 1.0 and stats["recall"] == 1.0 and stats["f1"] == 1.0


def test_wrong_value_is_miss_not_match():
    # right metric/period/accession but value off by >2% -> FP + FN, not a match.
    stats = grade_guidance([_g(low=10.0, high=10.2)], [_g(low=12.95, high=13.1)])
    assert stats["tp"] == 0 and stats["fp"] == 1 and stats["fn"] == 1
    assert stats["precision"] == 0.0 and stats["recall"] == 0.0


def test_false_positive_hallucinated_figure():
    # extractor emits a figure with no gold counterpart.
    stats = grade_guidance([_g(), _g(metric="eps", low=5.0, high=5.2)], [_g()])
    assert stats["tp"] == 1 and stats["fp"] == 1 and stats["fn"] == 0
    assert stats["precision"] == 0.5 and stats["recall"] == 1.0
    assert stats["false_positives"][0]["metric"] == "eps"


def test_false_negative_missed_guidance():
    stats = grade_guidance([], [_g()])
    assert stats["tp"] == 0 and stats["fn"] == 1
    assert stats["recall"] == 0.0
    assert stats["false_negatives"][0]["metric"] == "revenue"


def test_per_metric_breakdown():
    gold = [_g(metric="revenue"), _g(metric="eps", low=5.0, high=5.2)]
    ext = [_g(metric="revenue")]  # got revenue, missed eps
    stats = grade_guidance(ext, gold)
    assert stats["per_metric"]["revenue"] == {"tp": 1, "fp": 0, "fn": 0}
    assert stats["per_metric"]["eps"] == {"tp": 0, "fp": 0, "fn": 1}


def test_tolerance_band():
    # within 2% -> match
    assert grade_guidance([_g(low=13.0, high=13.2)], [_g(low=12.95, high=13.1)])["tp"] == 1


def test_right_figure_wrong_basis_is_miss():
    # identical figure, wrong basis -> NOT a match (FP + FN), per the requirement.
    stats = grade_guidance([_g(basis="non_gaap")], [_g(basis="gaap")])
    assert stats["tp"] == 0 and stats["fp"] == 1 and stats["fn"] == 1


def test_matching_basis_is_hit():
    stats = grade_guidance([_g(basis="adjusted")], [_g(basis="adjusted")])
    assert stats["tp"] == 1 and stats["fp"] == 0 and stats["fn"] == 0


def test_unit_representation_equal_is_hit():
    # same magnitude, different representation: 1.327 billion == 1327.0 million
    ext = _g(low=1327.0, high=1331.0, unit="usd_millions")
    gold = _g(low=1.327, high=1.331, unit="usd_billions")
    assert grade_guidance([ext], [gold])["tp"] == 1


def test_1000x_magnitude_error_still_miss():
    # GUARD: a real 1000x error (says billions when it's millions) must FAIL.
    ext = _g(low=1327.0, high=1331.0, unit="usd_billions")   # claims 1327 BILLION
    gold = _g(low=1327.0, high=1331.0, unit="usd_millions")  # truth is 1327 million
    stats = grade_guidance([ext], [gold])
    assert stats["tp"] == 0 and stats["fp"] == 1 and stats["fn"] == 1
