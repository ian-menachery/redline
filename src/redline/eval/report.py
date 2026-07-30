"""Render EVAL.md from the persisted ``eval_runs`` table.

The eval harness, the guidance grader (``valuation.guidance_eval``), and the
FCF-base validator (``valuation.validation_eval``) all write ``eval_runs`` rows
but nothing reads them. This module is that reader: it takes the latest row per
``event_id`` and renders a committed, recruiter-readable EVAL.md — the graded
scorecard (global + per-subsystem + per-event), the guidance-extraction
precision/recall, and the FCF-base validation results.

Read-only against the DB; writes only the output file. No LLM.

    python -m redline.eval.report                       # from settings.storage.db_path
    python -m redline.eval.report --db-path data/eval_run.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from redline.config import RedlineConfig
from redline.storage.db import connect

_GUIDANCE_PREFIX = "guidance_extraction:"
_GUIDANCE_HELDOUT_PREFIX = "guidance_extraction_heldout:"
_GUIDANCE_ELIGIBLE_PREFIX = "guidance_extraction_eligible:"
_FCF_PREFIX = "fcf_validation:"
_DEFAULT_PER_COMPANY = 2


def _overall(rows: list[dict]) -> dict:
    """Aggregate per-metric rows into one precision/recall/F1 by summing TP/FP/FN."""
    tp = fp = fn = 0
    for r in rows:
        s = _loads(r.get("judge_result")) or {}
        tp += int(s.get("tp") or 0)
        fp += int(s.get("fp") or 0)
        fn += int(s.get("fn") or 0)
    p = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    return {"precision": p, "recall": rec, "tp": tp, "fp": fp, "fn": fn}


def _metric_row(rows: list[dict], prefix: str, metric: str) -> dict | None:
    """The per-metric stats dict for a single metric in a namespace, if present."""
    for r in rows:
        if r["event_id"] == f"{prefix}{metric}":
            return _loads(r.get("judge_result")) or {}
    return None


def _pr(stats: dict | None) -> str:
    """Format 'precision X / recall Y' from a stats dict."""
    if not stats:
        return "n/a"
    def _f(x: Any) -> str:
        return f"{x:.3f}" if isinstance(x, (int, float)) else "n/a"
    return f"precision {_f(stats.get('precision'))} / recall {_f(stats.get('recall'))}"


def _latest_per_event(rows: list[dict]) -> list[dict]:
    """Keep the most recent row (by ``ran_at``) for each ``event_id``."""
    latest: dict[str, dict] = {}
    for r in rows:
        eid = r["event_id"]
        if eid not in latest or str(r.get("ran_at", "")) > str(latest[eid].get("ran_at", "")):
            latest[eid] = r
    return sorted(latest.values(), key=lambda r: r["event_id"])


def _loads(value: Any) -> Any:
    if value in (None, ""):
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _metric_table(rows: list[dict], prefix: str) -> list[str]:
    """Markdown precision/recall table for one guidance panel."""
    out = ["| Metric | Precision | Recall | F1 | TP | FP | FN |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        metric = r["event_id"][len(prefix):]
        s = _loads(r.get("judge_result")) or {}

        def _f(x: Any) -> str:
            return f"{x:.3f}" if isinstance(x, (int, float)) else "n/a"
        out.append(
            f"| {metric} | {_f(s.get('precision'))} | {_f(s.get('recall'))} | "
            f"{_f(s.get('f1'))} | {s.get('tp', '')} | {s.get('fp', '')} | "
            f"{s.get('fn', '')} |"
        )
    return out


def _panel_sizes(registration: dict | None) -> list[str]:
    """Explicit, numeric per-panel n + named undershoot. Never implies a larger
    n than the manifest supports (reporting-transparency, not a selection change)."""
    if not registration:
        return []
    accessions = list(registration.get("accessions") or [])
    per_company = int(registration.get("per_company") or _DEFAULT_PER_COMPANY)
    heldout = [a for a in accessions if not a.get("previously_observed", True)]

    counts: dict[str, int] = {}
    for a in accessions:
        counts[a.get("ticker", "?")] = counts.get(a.get("ticker", "?"), 0) + 1
    undershoot = sorted(t for t, c in counts.items() if c < per_company)

    out = [
        f"**Panel size:** full n = {len(accessions)} accessions "
        f"({len(counts)} companies); held-out (never-seen) n = {len(heldout)} "
        f"accessions ({len({a.get('ticker') for a in heldout})} companies).",
    ]
    if undershoot:
        out.append(
            "**Undershoot** — fewer than the target "
            f"{per_company} accessions: "
            + ", ".join(f"{t} ({counts[t]})" for t in undershoot) + "."
        )
    out.append("")
    return out


def render_eval_markdown(rows: list[dict], registration: dict | None = None) -> str:
    """Pure renderer: latest ``eval_runs`` rows -> EVAL.md text.

    ``registration`` is the guidance-eval manifest (``registration:`` block of
    ``guidance_labels.yaml``); when present it drives the explicit per-panel n
    and undershoot reporting."""
    latest = _latest_per_event(rows)
    graded = [r for r in latest if ":" not in r["event_id"]]
    guidance = [r for r in latest if r["event_id"].startswith(_GUIDANCE_PREFIX)]
    heldout = [r for r in latest if r["event_id"].startswith(_GUIDANCE_HELDOUT_PREFIX)]
    eligible = [r for r in latest if r["event_id"].startswith(_GUIDANCE_ELIGIBLE_PREFIX)]
    fcf = [r for r in latest if r["event_id"].startswith(_FCF_PREFIX)]

    out: list[str] = ["# Eval results", ""]
    out.append(
        "Generated from the persisted `eval_runs` table by "
        "`python -m redline.eval.report`. The graded event set is pre-registered "
        "and locked (`config/eval_events.yaml`); see CLAUDE.md section 4.5."
    )
    out.append("")

    # --- Graded scorecard ---
    n_pass = sum(1 for r in graded if r["graded_pass"])
    out += ["## Pre-registered events", "", f"**Global: {n_pass}/{len(graded)} passed.**", ""]

    per_sub_pass: dict[str, int] = {}
    per_sub_total: dict[str, int] = {}
    for r in graded:
        for s in (_loads(r.get("subsystems_tested")) or []):
            per_sub_total[s] = per_sub_total.get(s, 0) + 1
            if r["graded_pass"]:
                per_sub_pass[s] = per_sub_pass.get(s, 0) + 1
    if per_sub_total:
        out += ["| Subsystem | Score |", "|---|---|"]
        for s in sorted(per_sub_total):
            out.append(f"| {s} | {per_sub_pass.get(s, 0)}/{per_sub_total[s]} |")
        out.append("")

    out += ["| Event | Subsystems | Binary | Result | Notes |",
            "|---|---|---|---|---|"]
    for r in graded:
        subs = ", ".join(_loads(r.get("subsystems_tested")) or [])
        binary = ("n/a" if r.get("binary_result") is None
                  else ("pass" if r["binary_result"] else "fail"))
        result = "PASS" if r["graded_pass"] else "FAIL"
        notes = (r.get("notes") or "").replace("|", "\\|").replace("\n", " ")
        out.append(f"| {r['event_id']} | {subs} | {binary} | {result} | {notes} |")
    out.append("")

    # --- Guidance extraction ---
    if guidance:
        out += ["## Guidance extraction (8-K)", ""]
        if registration and registration.get("locked_at"):
            out.append(
                f"Panel selected by mechanical Rule R, locked at "
                f"`{registration['locked_at']}` (tag `guidance-eval-registration-v1`)."
            )
            out.append("")

        # Headline: the metric the DCF actually consumes (revenue), scored on the
        # figures the pipeline actually acts on (trigger-eligible). Then the
        # confidence-gated overall, then the raw all-figures overall — so the
        # narrow, decision-relevant number and the broad number are both visible.
        rev_elig = _metric_row(eligible, _GUIDANCE_ELIGIBLE_PREFIX, "revenue")
        if rev_elig:
            out += [
                "### Headline", "",
                f"- **Revenue guidance — the only figure the DCF consumes, "
                f"trigger-eligible: {_pr(rev_elig)}.**",
            ]
            if eligible:
                out.append(
                    f"- Confidence-gated overall (all metrics the pipeline acts on): "
                    f"{_pr(_overall(eligible))} — the gate trades some recall for precision."
                )
            out.append(
                f"- Raw, every extracted figure (incl. out-of-scope metrics the model "
                f"never uses): {_pr(_overall(guidance))}."
            )
            out.append("")

        out += _panel_sizes(registration)
        out += ["**Full panel** — every extracted figure, all metrics (raw):", ""]
        out += _metric_table(guidance, _GUIDANCE_PREFIX)
        out.append("")
        if eligible:
            out += ["**Acted-upon** — only trigger-eligible figures (what the DCF "
                    "consumes; `manual_review` figures are quarantined by the gate):", ""]
            out += _metric_table(eligible, _GUIDANCE_ELIGIBLE_PREFIX)
            out.append("")
        if heldout:
            out += ["**Held-out sub-panel** — never-seen accessions only "
                    "(`previously_observed: false`):", ""]
            out += _metric_table(heldout, _GUIDANCE_HELDOUT_PREFIX)
            out.append("")

    # --- FCF validation ---
    if fcf:
        out += ["## FCF-base validation", "", "| Ticker | Validated |", "|---|---|"]
        for r in fcf:
            ticker = r["event_id"][len(_FCF_PREFIX):]
            out.append(f"| {ticker} | {'yes' if r['graded_pass'] else 'no'} |")
        out.append("")

    out += [
        "## Reproducibility", "",
        "The graded eval runs deterministically against an isolated, "
        "freshly-seeded database, so a result never depends on prior state:", "",
        "```",
        "python -m redline.eval.harness --all --db-path data/eval_run.db --fresh",
        "```", "",
    ]
    if guidance:
        out += [
            "The guidance-extraction panel is selected by a mechanical rule "
            "(Rule R) over persisted DB state — a pure, deterministic read (no "
            "network at selection time). Reproduce end-to-end:", "",
            "```",
            "python scripts/backfill_8ks.py --months 15   # live EDGAR, no LLM",
            "python -m redline.valuation.guidance --once   # extraction (needs an LLM key)",
            "python -m redline.valuation.guidance_eval     # precision/recall on scope=total",
            "python -m redline.eval.report                 # regenerate this file",
            "```", "",
        ]
    return "\n".join(out) + "\n"


def _read_rows(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute(
        "SELECT event_id, ran_at, binary_result, judge_result, graded_pass, "
        "subsystems_tested, notes FROM eval_runs"
    )
    return [dict(r) for r in cur.fetchall()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render EVAL.md from eval_runs.")
    parser.add_argument("--settings", default="config/settings.toml")
    parser.add_argument("--db-path", help="Override settings.storage.db_path.")
    parser.add_argument("--labels", default="config/valuation/guidance_labels.yaml",
                        help="Guidance gold/registration file (for per-panel n).")
    parser.add_argument("--out", default="EVAL.md")
    args = parser.parse_args(argv)

    config = RedlineConfig.from_toml(args.settings)
    db_path = args.db_path or config.storage.db_path
    conn = connect(db_path, read_only=True, check_same_thread=False)
    try:
        rows = _read_rows(conn)
    finally:
        conn.close()

    if not rows:
        print(f"No eval_runs rows in {db_path}; nothing to report.")
        return 1

    registration = None
    labels_path = Path(args.labels)
    if labels_path.exists():
        from redline.valuation.guidance_eval import load_registration
        registration = load_registration(labels_path)
    Path(args.out).write_text(render_eval_markdown(rows, registration), encoding="utf-8")
    print(f"wrote {args.out} from {len(rows)} eval_runs rows ({db_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
