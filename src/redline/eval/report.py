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
_FCF_PREFIX = "fcf_validation:"


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


def render_eval_markdown(rows: list[dict]) -> str:
    """Pure renderer: latest ``eval_runs`` rows -> EVAL.md text."""
    latest = _latest_per_event(rows)
    graded = [r for r in latest if ":" not in r["event_id"]]
    guidance = [r for r in latest if r["event_id"].startswith(_GUIDANCE_PREFIX)]
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
        out += ["## Guidance extraction (8-K)", "",
                "| Metric | Precision | Recall | F1 | TP | FP | FN |",
                "|---|---|---|---|---|---|---|"]
        for r in guidance:
            metric = r["event_id"][len(_GUIDANCE_PREFIX):]
            s = _loads(r.get("judge_result")) or {}

            def _f(x: Any) -> str:
                return f"{x:.3f}" if isinstance(x, (int, float)) else "n/a"
            out.append(
                f"| {metric} | {_f(s.get('precision'))} | {_f(s.get('recall'))} | "
                f"{_f(s.get('f1'))} | {s.get('tp', '')} | {s.get('fp', '')} | "
                f"{s.get('fn', '')} |"
            )
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
    Path(args.out).write_text(render_eval_markdown(rows), encoding="utf-8")
    print(f"wrote {args.out} from {len(rows)} eval_runs rows ({db_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
