"""Guidance-extraction eval (mandatory) — precision / recall per metric.

Grades the stored ``extracted_figures`` against a hand-labeled gold set
(``config/valuation/guidance_labels.yaml``). A gold label matches an extracted
figure on (accession, metric, period) with both range ends within tolerance;
unmatched gold is a false negative, unmatched extraction a false positive —
reported explicitly and honestly (silent extraction error is failure mode #1).

Results go to ``eval_runs`` under the ``guidance_extraction:<metric>`` namespace,
separate from the locked graded-12 (§4.5). The grading core (`grade_guidance`)
is pure so it can be unit-tested without a live LLM.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sqlite3
import sys
import uuid
from collections import defaultdict
from pathlib import Path

import yaml

from redline.config import RedlineConfig

_LOG = logging.getLogger(__name__)
EVENT_PREFIX = "guidance_extraction"
# Held-out sub-panel = metrics scored over only the never-seen
# (previously_observed=false) accessions of the registered panel.
HELDOUT_PREFIX = "guidance_extraction_heldout"
_DEFAULT_TOL = 0.02  # 2% relative on each range end


def _within(a: float | None, b: float | None, tol: float) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if b == 0:
        return abs(a - b) <= tol
    return abs(a - b) / abs(b) <= tol


# Scale factors so value comparison is on true MAGNITUDE, not representation:
# "$1.327 billion" (usd_billions 1.327) == "$1327 million" (usd_millions 1327.0).
# This normalizes representation ONLY — a genuine 1000x error (model says a
# figure is in billions when it's really millions) still differs by 1000x and
# still FAILS. Per-share and percent have no scale ambiguity (factor 1).
_UNIT_FACTOR = {
    "usd": 1.0, "usd_millions": 1e6, "usd_billions": 1e9,
    "usd_per_share": 1.0, "pct": 1.0,
}


def _abs_magnitude(value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    factor = _UNIT_FACTOR.get(unit) if unit is not None else None
    return value * factor if factor is not None else value


def _matches(ext: dict, gold: dict, tol: float) -> bool:
    # basis is part of the match: a right-figure/wrong-basis extraction is a MISS
    # (a basis error silently corrupts a model input — failure mode #1). Values
    # are compared as absolute magnitudes (unit representation normalized), so a
    # 1000x unit-class error is still a real miss.
    return (
        ext.get("accession") == gold.get("accession")
        and ext.get("metric") == gold.get("metric")
        and ext.get("period") == gold.get("period")
        and ext.get("basis") == gold.get("basis")
        and _within(_abs_magnitude(ext.get("low"), ext.get("unit")),
                    _abs_magnitude(gold.get("low"), gold.get("unit")), tol)
        and _within(_abs_magnitude(ext.get("high"), ext.get("unit")),
                    _abs_magnitude(gold.get("high"), gold.get("unit")), tol)
    )


def grade_guidance(
    extracted: list[dict], gold: list[dict], *, tolerance: float = _DEFAULT_TOL
) -> dict:
    """Pure precision/recall grader. Returns per-metric + overall + FP/FN lists."""
    matched_gold: set[int] = set()
    matched_ext: set[int] = set()
    for gi, g in enumerate(gold):
        for ei, e in enumerate(extracted):
            if ei in matched_ext:
                continue
            if _matches(e, g, tolerance):
                matched_gold.add(gi)
                matched_ext.add(ei)
                break

    tp = len(matched_gold)
    fp = len(extracted) - len(matched_ext)
    fn = len(gold) - len(matched_gold)
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    # f1 is None only when precision/recall are undefined (no predictions / no
    # gold). When both are defined but a run got everything wrong (precision or
    # recall 0.0), f1 is a real 0.0 — not "not computable".
    if precision is None or recall is None:
        f1 = None
    elif precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)

    per_metric: dict[str, dict] = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    for gi, g in enumerate(gold):
        per_metric[g["metric"]]["tp" if gi in matched_gold else "fn"] += 1
    for ei, e in enumerate(extracted):
        if ei not in matched_ext:
            per_metric[e["metric"]]["fp"] += 1

    return {
        "tp": tp, "fp": fp, "fn": fn,
        "precision": precision, "recall": recall, "f1": f1,
        "per_metric": {k: dict(v) for k, v in per_metric.items()},
        "false_positives": [extracted[i] for i in range(len(extracted)) if i not in matched_ext],
        "false_negatives": [gold[i] for i in range(len(gold)) if i not in matched_gold],
    }


def load_gold(path: str | Path) -> list[dict]:
    """Per-figure gold labels. Accepts the v2 mapping (``labels:`` key) and,
    for backward compatibility, a bare top-level list."""
    with Path(path).open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if isinstance(data, dict):
        return list(data.get("labels") or [])
    return list(data or [])


def load_registration(path: str | Path) -> dict | None:
    """The registration manifest (``registration:`` block), or None if the file
    is a bare list / has no manifest. Used to split the graded panel into the
    full panel and the never-seen held-out sub-panel."""
    with Path(path).open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if isinstance(data, dict) and isinstance(data.get("registration"), dict):
        return data["registration"]
    return None


def _extracted_rows(conn: sqlite3.Connection, *, totals_only: bool = True) -> list[dict]:
    """Stored extractions to grade.

    By default only ``scope='total'`` figures. A segment / sub-component figure
    (e.g. "U.S. commercial revenue") is a *correctly-classified* extraction that
    the totals-only frozen gold has no entry for, so counting it as a false
    positive understates precision. Filtering to totals measures precision on
    exactly what the gold covers (NOTES.md section 12, limitation 1)."""
    where = "WHERE scope = 'total'" if totals_only else ""
    rows = conn.execute(
        f"SELECT accession, cik, metric, scope, period, low, high, unit, basis "
        f"FROM extracted_figures {where}"
    ).fetchall()
    return [dict(r) for r in rows]


def _record(conn: sqlite3.Connection, event_id: str, stats: dict, *, f1_pass: float) -> None:
    passed = 1 if (stats.get("f1") is not None and stats["f1"] >= f1_pass) else 0
    conn.execute(
        """INSERT INTO eval_runs (id, event_id, ran_at, prompt_versions, binary_result,
               judge_result, graded_pass, subsystems_tested, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), event_id,
         datetime.datetime.now(datetime.UTC).isoformat(),
         None, passed, json.dumps(stats, default=str), passed,
         json.dumps(["valuation"]), None),
    )


def _record_metrics(
    conn: sqlite3.Connection, extracted: list[dict], gold: list[dict],
    *, tol: float, f1_pass: float, prefix: str,
) -> None:
    """One ``eval_runs`` row per metric, namespaced ``<prefix>:<metric>``."""
    for metric in {g["metric"] for g in gold} | {e["metric"] for e in extracted}:
        g_m = [g for g in gold if g["metric"] == metric]
        e_m = [e for e in extracted if e["metric"] == metric]
        _record(conn, f"{prefix}:{metric}", grade_guidance(e_m, g_m, tolerance=tol),
                f1_pass=f1_pass)


def run_eval(config: RedlineConfig, conn: sqlite3.Connection, *, labels_path: str | None = None) -> dict:
    """Grade stored extractions against the gold labels. Writes per-metric eval_runs."""
    path = labels_path or "config/valuation/guidance_labels.yaml"
    tol = config.valuation.guidance_eval_tolerance
    f1_pass = config.valuation.guidance_eval_f1_pass
    gold = load_gold(path)
    extracted = _extracted_rows(conn)  # scope='total' only
    segment = [r for r in _extracted_rows(conn, totals_only=False) if r["scope"] != "total"]

    # Restrict scoring to the REGISTERED PANEL. Gold for an out-of-panel
    # accession (e.g. an excluded amendment) and stray prior extractions for
    # non-panel filings must not pollute the metrics — the eval scores exactly
    # the locked set, nothing else.
    registration = load_registration(path)
    if registration:
        panel = {a["accession"] for a in (registration.get("accessions") or [])}
        gold = [g for g in gold if g.get("accession") in panel]
        extracted = [e for e in extracted if e.get("accession") in panel]
        segment = [s for s in segment if s.get("accession") in panel]

    overall = grade_guidance(extracted, gold, tolerance=tol)
    overall["segment_excluded"] = len(segment)

    # Full-panel per-metric scorecard rows.
    _record_metrics(conn, extracted, gold, tol=tol, f1_pass=f1_pass, prefix=EVENT_PREFIX)

    # Held-out sub-panel: same metrics, restricted to never-seen accessions
    # (previously_observed=false) from the registration manifest. Reported
    # separately so a reader sees performance on data whose results were never
    # observed before the lock.
    if registration:
        heldout = {
            a["accession"]
            for a in (registration.get("accessions") or [])
            if not a.get("previously_observed", True)
        }
        if heldout:
            g_ho = [g for g in gold if g.get("accession") in heldout]
            e_ho = [e for e in extracted if e.get("accession") in heldout]
            _record_metrics(conn, e_ho, g_ho, tol=tol, f1_pass=f1_pass, prefix=HELDOUT_PREFIX)
    return overall


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guidance-extraction eval for redline.")
    parser.add_argument("--settings", default="config/settings.toml")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = RedlineConfig.from_toml(args.settings)
    from redline.storage.db import open_db
    from redline.storage.schema import init_full_schema

    with open_db(config.storage.db_path) as conn:
        init_full_schema(conn)
        stats = run_eval(config, conn)
        p = stats["precision"]
        r = stats["recall"]
        print(f"\nGuidance extraction (scope=total) — precision={p} recall={r} "
              f"f1={stats['f1']} (tp={stats['tp']} fp={stats['fp']} fn={stats['fn']}; "
              f"{stats.get('segment_excluded', 0)} segment figures excluded from FP)")
        if stats["false_negatives"]:
            print("  MISSED (false negatives):")
            for fn in stats["false_negatives"]:
                print(f"    {fn.get('ticker','?')} {fn['metric']} {fn['period']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
