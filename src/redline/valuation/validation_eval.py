"""FCF-base validation eval (mandatory) — Subsystem 7.

The DCF is only as honest as its base. This eval measures how well the
reconstructed free cash flow agrees across three independent sources, per
DCF-eligible company:

1. ``accessor_fcf`` — edgartools ``Financials.get_free_cash_flow()`` (the base path)
2. ``reconstructed_fcf`` — operating cash flow − capex rebuilt from the stored
   ``xbrl_facts`` via ``fcf_mapping_v1.yaml`` (a genuinely independent second
   implementation — this is the real cross-check today)
3. ``known_fcf`` — the hand-recorded 10-K figure in ``assumptions.yaml``

Honest reporting caveat: while ``known_fcf`` is a placeholder pulled from the
same source as the accessor, ``accessor_fcf`` vs ``reconstructed_fcf`` is a true
independent comparison. Once Ian replaces ``known_fcf`` with a hand-read 10-K
value, the accessor-vs-known column becomes an external ground-truth check too.

Results are written to ``eval_runs`` under the ``fcf_validation:<ticker>`` event
namespace — deliberately separate from the locked graded-12 filing eval (§4.5).
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sqlite3
import sys
import uuid

import edgar

from redline.config import RedlineConfig
from redline.valuation import fcf
from redline.valuation.models import load_assumptions
from redline.valuation.xbrl import dcf_eligible_companies

_LOG = logging.getLogger(__name__)

EVENT_PREFIX = "fcf_validation"


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _rel_error(a: float | None, b: float | None) -> float | None:
    """Relative error of ``a`` vs reference ``b`` (|a-b|/|b|). None if not computable."""
    if a is None or b is None or b == 0:
        return None
    return abs(a - b) / abs(b)


def evaluate_company(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    cik: str,
    accessor_fcf: float | None,
    fiscal_year: int | None,
    known_fcf: float | None,
    mapping: dict[str, list[str]],
    tolerance: float,
) -> dict:
    """Compute the three-way FCF agreement for one company. Pure-ish (reads DB)."""
    reconstructed_fcf, recon_year, gaps = fcf.reconstruct_fcf_from_facts(
        conn, cik=cik, mapping=mapping,
    )

    err_known = _rel_error(accessor_fcf, known_fcf)
    err_recon = _rel_error(accessor_fcf, reconstructed_fcf)

    # Pass = accessor agrees with the hand-recorded known_fcf within tolerance.
    passed = err_known is not None and err_known <= tolerance
    notes: list[str] = []
    if reconstructed_fcf is None:
        notes.append(f"DB reconstruction unavailable (gaps: {gaps or 'no facts ingested'})")
    if known_fcf is None:
        notes.append("no hand-recorded known_fcf")
    if err_recon is not None and err_recon > tolerance:
        notes.append(f"accessor vs DB-reconstruction disagree by {err_recon:.1%}")

    return {
        "ticker": ticker,
        "accessor_fcf": accessor_fcf,
        "reconstructed_fcf": reconstructed_fcf,
        "known_fcf": known_fcf,
        "err_accessor_vs_known": err_known,
        "err_accessor_vs_reconstructed": err_recon,
        "passed": passed,
        "notes": notes,
    }


def _record(conn: sqlite3.Connection, result: dict) -> None:
    conn.execute(
        """
        INSERT INTO eval_runs (
            id, event_id, ran_at, prompt_versions, binary_result,
            judge_result, graded_pass, subsystems_tested, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            f"{EVENT_PREFIX}:{result['ticker']}",
            _now_iso(),
            None,
            1 if result["passed"] else 0,
            json.dumps({k: result[k] for k in (
                "accessor_fcf", "reconstructed_fcf", "known_fcf",
                "err_accessor_vs_known", "err_accessor_vs_reconstructed")}, default=str),
            1 if result["passed"] else 0,
            json.dumps(["valuation"]),
            "; ".join(result["notes"]) or None,
        ),
    )


def run_validation(
    config: RedlineConfig,
    conn: sqlite3.Connection,
    *,
    company_factory=edgar.Company,
) -> dict:
    """Run the FCF-base validation across DCF-eligible companies. Writes eval_runs."""
    edgar.set_identity(config.poller.edgar_user_agent)
    assumptions = load_assumptions(config.valuation.assumptions_path)
    mapping = fcf.load_fcf_mapping(config.valuation.fcf_mapping_path)
    tolerance = config.valuation.fcf_validation_tolerance

    per_company: list[dict] = []
    passed = 0
    for row in dcf_eligible_companies(conn):
        ticker, cik = row["ticker"], row["cik"]
        a = assumptions.get(ticker)
        if a is None:
            continue
        try:
            base = fcf.build_base_from_edgar(
                ticker, cik, company_factory=company_factory, mapping=mapping)
            result = evaluate_company(
                conn, ticker=ticker, cik=cik, accessor_fcf=base.free_cash_flow,
                fiscal_year=base.fiscal_year, known_fcf=a.known_fcf,
                mapping=mapping, tolerance=tolerance,
            )
        except Exception as e:
            result = {"ticker": ticker, "accessor_fcf": None, "reconstructed_fcf": None,
                      "known_fcf": a.known_fcf, "err_accessor_vs_known": None,
                      "err_accessor_vs_reconstructed": None, "passed": False,
                      "notes": [f"{type(e).__name__}: {e}"]}
        _record(conn, result)
        passed += int(result["passed"])
        per_company.append(result)

    errs = [c["err_accessor_vs_reconstructed"] for c in per_company
            if c["err_accessor_vs_reconstructed"] is not None]
    mean_recon_err = sum(errs) / len(errs) if errs else None
    return {
        "companies": len(per_company),
        "passed": passed,
        "mean_accessor_vs_reconstructed_error": mean_recon_err,
        "per_company": per_company,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FCF-base validation eval for redline DCF.")
    parser.add_argument("--settings", default="config/settings.toml")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = RedlineConfig.from_toml(args.settings)
    from redline.storage.db import open_db
    from redline.storage.schema import init_full_schema

    with open_db(config.storage.db_path) as conn:
        init_full_schema(conn)
        summary = run_validation(config, conn)
        print(f"\nFCF validation: {summary['passed']}/{summary['companies']} passed "
              f"(accessor vs known, tol={config.valuation.fcf_validation_tolerance:.0%})")
        for c in summary["per_company"]:
            print(f"  {c['ticker']:5} pass={c['passed']!s:5} "
                  f"accessor={_m(c['accessor_fcf'])} recon={_m(c['reconstructed_fcf'])} "
                  f"known={_m(c['known_fcf'])} "
                  f"err_recon={_pct(c['err_accessor_vs_reconstructed'])}")
        mre = summary["mean_accessor_vs_reconstructed_error"]
        print(f"  mean accessor-vs-reconstruction error: {_pct(mre)}")
    return 0


def _m(v) -> str:
    return f"{v/1e9:.2f}B" if isinstance(v, (int, float)) else "n/a"


def _pct(v) -> str:
    return f"{v:.1%}" if isinstance(v, (int, float)) else "n/a"


if __name__ == "__main__":
    sys.exit(main())
