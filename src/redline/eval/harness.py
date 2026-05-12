"""Eval harness orchestrator.

For each eval event in ``config/eval_events.yaml``:
1. Run the per-event replay handler (backfills filings + runs the tagged
   subsystems against them).
2. Build the run-output context and grade against ``pass_criteria``
   (with LLM-judge fallback when the binary criterion can't evaluate).
3. Insert an ``eval_runs`` row with the verdict.
4. Print a per-event + global scorecard.

CLI:
  python -m redline.eval.harness --all
  python -m redline.eval.harness --event pltr_karp_form4_2024
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sqlite3
import sys
import uuid
from collections import Counter
from pathlib import Path

from redline.config import RedlineConfig
from redline.eval.grader import Grade, grade_event
from redline.eval.models import EvalEvent, load_eval_events
from redline.eval.replay import replay
from redline.llm.client import LLMClient

_LOG = logging.getLogger(__name__)

# Prompt versions used by the harness — recorded in eval_runs for replayability.
_PROMPT_VERSIONS = {
    "diff_gate": "v1",
    "diff_summary": "v1",
    "correlator": "v1",
    "eval_judge": "v1",
}


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _record_eval_run(
    conn: sqlite3.Connection, *, event: EvalEvent, grade: Grade,
) -> str:
    run_id = str(uuid.uuid4())
    row = grade.as_eval_runs_row()
    conn.execute(
        """
        INSERT INTO eval_runs (
            id, event_id, ran_at, prompt_versions,
            binary_result, judge_result, graded_pass,
            subsystems_tested, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, event.id, _now_iso(),
            json.dumps(_PROMPT_VERSIONS),
            row["binary_result"], row["judge_result"], row["graded_pass"],
            json.dumps(event.tests),
            row["notes"],
        ),
    )
    return run_id


def run(
    config: RedlineConfig,
    conn: sqlite3.Connection,
    client: LLMClient,
    *,
    events: list[EvalEvent],
    use_judge_on_none: bool = True,
) -> dict:
    """Run the eval harness over ``events``. Returns a scorecard dict."""
    per_event: list[dict] = []
    pass_count = 0
    fail_count = 0
    subsystem_passes: Counter = Counter()
    subsystem_totals: Counter = Counter()

    for event in events:
        _LOG.info("=== eval event %s (tests=%s) ===", event.id, event.tests)
        accession_to_grade, replay_notes = replay(conn, config, client, event)
        _LOG.info("  replay: %s", replay_notes)

        if accession_to_grade is None:
            grade = Grade(
                event_id=event.id, binary_result=None, judge_result=None,
                graded_pass=False, notes=f"replay failed: {replay_notes}",
            )
        else:
            grade = grade_event(
                conn, client, event,
                accession_to_grade=accession_to_grade,
                use_judge_on_none=use_judge_on_none,
            )

        run_id = _record_eval_run(conn, event=event, grade=grade)
        _LOG.info(
            "  graded: pass=%s (binary=%s, judge=%s) notes=%s",
            grade.graded_pass, grade.binary_result,
            "yes" if grade.judge_result else "no", grade.notes,
        )

        per_event.append({
            "run_id": run_id, "event_id": event.id,
            "tests": event.tests, "graded_pass": grade.graded_pass,
            "binary_result": grade.binary_result,
            "judge_partial_credit": (
                grade.judge_result.partial_credit if grade.judge_result else None
            ),
            "notes": grade.notes,
        })

        if grade.graded_pass:
            pass_count += 1
        else:
            fail_count += 1
        for subsystem in event.tests:
            subsystem_totals[subsystem] += 1
            if grade.graded_pass:
                subsystem_passes[subsystem] += 1

    scorecard = {
        "global": f"{pass_count}/{pass_count + fail_count}",
        "per_subsystem": {
            s: f"{subsystem_passes[s]}/{subsystem_totals[s]}"
            for s in sorted(subsystem_totals)
        },
        "per_event": per_event,
    }
    return scorecard


def _print_scorecard(scorecard: dict) -> None:
    # Windows cp1252 stdout doesn't handle unicode; keep this ASCII-only.
    print()
    print("=" * 72)
    print("EVAL SCORECARD")
    print("=" * 72)
    print(f"  global: {scorecard['global']}")
    for subsystem, score in scorecard["per_subsystem"].items():
        print(f"  {subsystem:<20} {score}")
    print()
    for ev in scorecard["per_event"]:
        flag = "PASS" if ev["graded_pass"] else "FAIL"
        binary = (
            "n/a" if ev["binary_result"] is None
            else ("pass" if ev["binary_result"] else "fail")
        )
        pc = (
            f"  judge={ev['judge_partial_credit']:.2f}"
            if ev["judge_partial_credit"] is not None else ""
        )
        print(f"  [{flag}] {ev['event_id']:<30} binary={binary}{pc}")
        print(f"          {ev['notes']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Eval harness for redline.")
    parser.add_argument("--all", action="store_true", help="Run all eval events.")
    parser.add_argument("--event", action="append", default=[],
                        help="Run specific event id(s). Repeatable.")
    parser.add_argument("--no-judge", action="store_true",
                        help="Disable LLM-judge fallback (binary-only grading).")
    parser.add_argument("--settings", default="config/settings.toml")
    parser.add_argument("--events-file", default="config/eval_events.yaml")
    args = parser.parse_args(argv)

    if not args.all and not args.event:
        parser.error("Pass --all or one or more --event <id>.")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Windows cp1252 stdout fallback for any non-ASCII in INFO logs.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    from dotenv import load_dotenv
    load_dotenv()

    config = RedlineConfig.from_toml(args.settings)
    all_events = load_eval_events(Path(args.events_file))

    if args.all:
        events = all_events
    else:
        by_id = {e.id: e for e in all_events}
        events = []
        for eid in args.event:
            if eid not in by_id:
                parser.error(f"Unknown event id: {eid!r}")
            events.append(by_id[eid])

    from redline.storage.db import open_db
    from redline.storage.schema import init_full_schema

    with open_db(config.storage.db_path) as conn:
        init_full_schema(conn)
        client = LLMClient(config, conn)
        scorecard = run(
            config, conn, client,
            events=events,
            use_judge_on_none=not args.no_judge,
        )

    _print_scorecard(scorecard)
    return 0


if __name__ == "__main__":
    sys.exit(main())
