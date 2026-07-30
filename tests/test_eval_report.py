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
        _row("fcf_validation:VRTX", True),
    ]
    md = render_eval_markdown(rows)
    assert "Global: 1/1 passed" in md  # only the one pre-registered event
