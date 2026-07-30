"""Tests for the EVAL.md renderer (``redline.eval.report``)."""
from __future__ import annotations

import json

from redline.eval.report import _latest_per_event, render_eval_markdown


def _row(event_id, graded_pass, *, ran_at="2026-07-30", subs=None,
         binary_result=None, judge_result=None, notes=""):
    return {
        "event_id": event_id, "ran_at": ran_at,
        "binary_result": binary_result,
        "judge_result": judge_result,
        "graded_pass": int(graded_pass),
        "subsystems_tested": json.dumps(subs or []),
        "notes": notes,
    }


def test_latest_per_event_keeps_newest():
    rows = [_row("e", True, ran_at="2026-01-01"), _row("e", False, ran_at="2026-02-01")]
    latest = _latest_per_event(rows)
    assert len(latest) == 1
    assert latest[0]["ran_at"] == "2026-02-01"
    assert latest[0]["graded_pass"] == 0


def test_render_scorecard_subsystems_and_events():
    rows = [
        _row("key_10k_fy22", True, subs=["diff_analyzer"], binary_result=1),
        _row("pltr_karp", False, subs=["correlator"], notes="documented miss"),
    ]
    md = render_eval_markdown(rows)
    assert "Global: 1/2 passed" in md
    assert "diff_analyzer | 1/1" in md
    assert "correlator | 0/1" in md
    assert "key_10k_fy22" in md and "PASS" in md
    assert "pltr_karp" in md and "FAIL" in md and "documented miss" in md


def test_render_guidance_and_fcf_sections():
    rows = [
        _row("guidance_extraction:revenue", True, judge_result=json.dumps(
            {"precision": 0.9, "recall": 0.8, "f1": 0.85, "tp": 9, "fp": 1, "fn": 2})),
        _row("fcf_validation:VRTX", True),
    ]
    md = render_eval_markdown(rows)
    assert "Guidance extraction (8-K)" in md
    assert "0.900" in md and "0.800" in md
    assert "FCF-base validation" in md and "VRTX" in md and "yes" in md


def test_namespaced_events_excluded_from_graded_global():
    # guidance_extraction:* / fcf_validation:* must not count toward the graded set.
    rows = [
        _row("key_10k_fy22", True, subs=["diff_analyzer"]),
        _row("guidance_extraction:revenue", False),
        _row("guidance_extraction_heldout:revenue", False),
        _row("fcf_validation:VRTX", True),
    ]
    md = render_eval_markdown(rows)
    assert "Global: 1/1 passed" in md  # only the one pre-registered event


def _stats(precision, recall, f1, tp, fp, fn):
    return json.dumps({"precision": precision, "recall": recall, "f1": f1,
                       "tp": tp, "fp": fp, "fn": fn})


def test_render_heldout_subpanel_and_panel_sizes():
    rows = [
        _row("guidance_extraction:revenue", True,
             judge_result=_stats(0.95, 1.0, 0.97, 10, 1, 0)),
        _row("guidance_extraction_heldout:revenue", True,
             judge_result=_stats(1.0, 1.0, 1.0, 4, 0, 0)),
    ]
    registration = {
        "locked_at": "2026-07-30T00:00:00Z",
        "per_company": 2,
        "accessions": [
            {"ticker": "PLTR", "accession": "p1", "previously_observed": True},
            {"ticker": "PLTR", "accession": "p2", "previously_observed": True},
            {"ticker": "VRTX", "accession": "v1", "previously_observed": False},
            {"ticker": "VRTX", "accession": "v2", "previously_observed": False},
            {"ticker": "ULTA", "accession": "u1", "previously_observed": False},
        ],
    }
    md = render_eval_markdown(rows, registration)
    assert "Full panel" in md and "Held-out sub-panel" in md
    assert "full n = 5 accessions (3 companies)" in md
    assert "held-out (never-seen) n = 3 accessions (2 companies)" in md
    # ULTA contributed only 1 accession -> named as undershoot.
    assert "Undershoot" in md and "ULTA (1)" in md
    assert "locked at `2026-07-30T00:00:00Z`" in md
    assert "guidance-eval-registration-v1" in md


def test_headline_leads_with_revenue_trigger_eligible():
    rows = [
        _row("guidance_extraction:revenue", True, judge_result=_stats(0.92, 1.0, 0.96, 12, 1, 0)),
        _row("guidance_extraction:other", False, judge_result=_stats(0.25, 1.0, 0.4, 2, 6, 0)),
        _row("guidance_extraction_eligible:revenue", True,
             judge_result=_stats(1.0, 1.0, 1.0, 12, 0, 0)),
        _row("guidance_extraction_eligible:other", True,
             judge_result=_stats(0.4, 1.0, 0.57, 2, 3, 0)),
    ]
    md = render_eval_markdown(rows)
    assert "Headline" in md
    # revenue trigger-eligible leads, at 1.000/1.000
    assert "Revenue guidance" in md and "precision 1.000 / recall 1.000" in md
    # gated overall aggregates the eligible namespace: tp=14 fp=3 -> 0.824
    assert "0.824" in md
    # raw overall is still shown (tp=14 fp=7 -> 0.667) and the acted-upon table present
    assert "Acted-upon" in md
