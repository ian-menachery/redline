"""Versioned revaluation — Subsystem 7 hook.

Recomputes each DCF-eligible company's valuation and writes an **immutable**
``dcf_valuations`` row (the deliberate break from the project's latest-state
storage — that is what makes the before/after story possible). A run is
triggered when:

- a new periodic filing (10-K / 10-Q) has landed for the company since its last
  valuation (``run_reason='new_filing'``), which refreshes the XBRL base, or
- a forced refresh (``force=True`` / ``--force``; ``run_reason='refresh'``).

"Before" = the company's prior ``dcf_valuations`` row; "after" = the new one.
``valuation_input_links`` records which base inputs changed (e.g. ``base_revenue``
after a new 10-K) and by how much — the audit trail proving a real number moved
a real input.

Financials (banks) are excluded upstream (``dcf_eligible_companies``). A company
whose base fails FCF validation is skipped and reported, not valued on a bad
base (plan Phase 1).
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sqlite3
import sys
from collections.abc import Callable
from typing import Any, cast

import edgar

from redline.config import RedlineConfig
from redline.valuation import fcf
from redline.valuation.dcf import (
    DcfInputs,
    ScenarioBand,
    project_fcf,
    sensitivity,
    shift_revenue_growth,
    shift_wacc,
    value_dcf,
)
from redline.valuation.models import (
    CompanyAssumptions,
    XbrlBase,
    load_assumptions,
    to_dcf_inputs,
)
from redline.valuation.xbrl import dcf_eligible_companies

_LOG = logging.getLogger(__name__)

MODEL_VERSION = "v1"
# Base inputs whose change we surface as a before/after link.
_LINKED_INPUTS = ("base_revenue", "net_debt", "shares_diluted", "fiscal_year")


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _latest_periodic(conn: sqlite3.Connection, cik: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT accession, filing_type, filed_at
        FROM filings_seen
        WHERE cik = ? AND filing_type IN ('10-K', '10-Q')
        ORDER BY filed_at DESC LIMIT 1
        """,
        (cik,),
    ).fetchone()


def _last_valuation(conn: sqlite3.Connection, cik: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM dcf_valuations WHERE cik = ? ORDER BY valued_at DESC LIMIT 1",
        (cik,),
    ).fetchone()


def _assumptions_snapshot(base: XbrlBase, a: CompanyAssumptions, band_inputs: DcfInputs) -> dict:
    """Compact, diffable record of the inputs behind a valuation."""
    return {
        "base_revenue": base.base_revenue,
        "net_debt": base.net_debt,
        "shares_diluted": base.shares_diluted,
        "fiscal_year": base.fiscal_year,
        "wacc": band_inputs.wacc,
        "terminal_growth": band_inputs.terminal_growth,
        "tax_rate": a.tax_rate,
        "revenue_growth": list(band_inputs.drivers.revenue_growth),
        "operating_margin": band_inputs.drivers.operating_margin[0],
        "is_placeholder": a.is_placeholder,
        "low_confidence_note": a.low_confidence_note,
    }


def _sensitivity(base_inputs: DcfInputs, band: float) -> dict:
    """One-axis sweeps for the dashboard: WACC and a uniform growth shift."""
    wacc_pts = [round(base_inputs.wacc + d, 4) for d in (-2 * band, -band, 0.0, band, 2 * band)]
    # Drop any downside WACC point that lands at/below terminal growth: the
    # Gordon TV diverges there and value_dcf would raise. Excluded, never
    # fabricated. (The revenue-growth-shift axis only moves explicit-horizon
    # growth, not WACC or terminal growth, so it needs no such guard.)
    wacc_pts = [w for w in wacc_pts if w > base_inputs.terminal_growth]
    growth_pts = [-band, 0.0, band]
    return {
        "wacc": sensitivity(base_inputs, mutate=shift_wacc, values=wacc_pts),
        "revenue_growth_shift": sensitivity(
            base_inputs, mutate=shift_revenue_growth, values=growth_pts
        ),
    }


def _compute_band(
    base: XbrlBase, a: CompanyAssumptions, config: RedlineConfig,
    *, revenue_growth_y1_override: float | None = None,
) -> tuple[ScenarioBand, DcfInputs]:
    py = config.valuation.projection_years
    tg_default = config.valuation.terminal_growth_default
    scenarios = {
        s: to_dcf_inputs(base, a, scenario=s, projection_years=py, terminal_growth_default=tg_default,
                         revenue_growth_y1_override=revenue_growth_y1_override)
        for s in ("bear", "base", "bull")
    }
    band = ScenarioBand(
        bear=value_dcf(scenarios["bear"]),
        base=value_dcf(scenarios["base"]),
        bull=value_dcf(scenarios["bull"]),
    )
    return band, scenarios["base"]


def _insert_valuation(
    conn: sqlite3.Connection,
    *,
    base: XbrlBase,
    a: CompanyAssumptions,
    band: ScenarioBand,
    base_inputs: DcfInputs,
    run_reason: str,
    trigger_accession: str | None,
    config: RedlineConfig,
    prior_snapshot: dict | None,
    extra_links: list[dict] | None = None,
) -> int:
    snapshot = _assumptions_snapshot(base, a, base_inputs)
    # Bake the base-case FCF projection + result rollup so the dashboard can show
    # "how this was modeled" from stored data alone (no engine recompute at load).
    snapshot["projection"] = [r.model_dump() for r in project_fcf(base_inputs)]
    snapshot["base_result"] = band.base.model_dump()
    conn.execute("BEGIN")
    try:
        cur = conn.execute(
            """
            INSERT INTO dcf_valuations (
                cik, run_reason, trigger_accession, wacc, terminal_growth,
                assumptions_json, per_share_bear, per_share_base, per_share_bull,
                sensitivity_json, reference_price, reference_price_asof,
                model_version, valued_at, eval_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                base.cik, run_reason, trigger_accession, base_inputs.wacc,
                base_inputs.terminal_growth, json.dumps(snapshot, default=str),
                band.bear.per_share, band.base.per_share, band.bull.per_share,
                json.dumps(_sensitivity(base_inputs, config.valuation.sensitivity_band_pct),
                           default=str),
                a.reference_price, a.reference_price_asof,
                MODEL_VERSION, _now_iso(), None,
            ),
        )
        valuation_id = cast(int, cur.lastrowid)  # SQLite sets lastrowid after INSERT
        _insert_input_links(conn, valuation_id=valuation_id,
                            current=snapshot, prior=prior_snapshot)
        for link in (extra_links or []):
            conn.execute(
                """INSERT INTO valuation_input_links
                   (valuation_id, input_name, old_value, new_value, source)
                   VALUES (?, ?, ?, ?, ?)""",
                (valuation_id, link["input_name"], link.get("old_value"),
                 link.get("new_value"), link["source"]),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return valuation_id


def _insert_input_links(
    conn: sqlite3.Connection, *, valuation_id: int, current: dict, prior: dict | None,
) -> None:
    if prior is None:
        return
    for name in _LINKED_INPUTS:
        old = prior.get(name)
        new = current.get(name)
        if old is None or new is None or old == new:
            continue
        conn.execute(
            """
            INSERT INTO valuation_input_links (valuation_id, input_name, old_value, new_value, source)
            VALUES (?, ?, ?, ?, ?)
            """,
            (valuation_id, name, _as_float(old), _as_float(new), "xbrl"),
        )


def _as_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def run_once(
    config: RedlineConfig,
    conn: sqlite3.Connection,
    *,
    company_factory: Callable[[str], Any] = edgar.Company,
    force: bool = False,
) -> dict:
    """One revaluation pass over the DCF-eligible watchlist companies."""
    edgar.set_identity(config.poller.edgar_user_agent)
    assumptions = load_assumptions(config.valuation.assumptions_path)
    mapping = fcf.load_fcf_mapping(config.valuation.fcf_mapping_path)

    per_company: list[dict] = []
    valued = 0
    skipped = 0
    for row in dcf_eligible_companies(conn):
        ticker, cik = row["ticker"], row["cik"]
        a = assumptions.get(ticker)
        if a is None:
            per_company.append({"ticker": ticker, "status": "no_assumptions"})
            skipped += 1
            continue

        latest = _latest_periodic(conn, cik)
        trigger_accession = latest["accession"] if latest else None
        prior = _last_valuation(conn, cik)

        run_reason, do = _decide(prior, trigger_accession, force)
        if not do:
            per_company.append({"ticker": ticker, "status": "up_to_date"})
            continue

        try:
            base = fcf.build_base_from_edgar(ticker, cik,
                                             company_factory=company_factory, mapping=mapping)
            validation = fcf.validate_base(
                base, known_fcf=a.known_fcf,
                tolerance=config.valuation.fcf_validation_tolerance,
            )
            if not validation.passed:
                per_company.append({"ticker": ticker, "status": "unvalidated_base",
                                    "notes": validation.notes})
                skipped += 1
                continue

            band, base_inputs = _compute_band(base, a, config)
            prior_snapshot = json.loads(prior["assumptions_json"]) if prior else None
            valuation_id = _insert_valuation(
                conn, base=base, a=a, band=band, base_inputs=base_inputs,
                run_reason=run_reason, trigger_accession=trigger_accession,
                config=config, prior_snapshot=prior_snapshot,
            )
            valued += 1
            per_company.append({
                "ticker": ticker, "status": "valued", "valuation_id": valuation_id,
                "run_reason": run_reason, "trigger_accession": trigger_accession,
                "per_share": [round(band.bear.per_share, 2), round(band.base.per_share, 2),
                              round(band.bull.per_share, 2)],
                "low_confidence": bool(a.low_confidence_note),
            })
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            _LOG.warning("revalue failed for %s: %s", ticker, reason)
            per_company.append({"ticker": ticker, "status": "error", "error": reason})
            skipped += 1

    return {
        "considered": len(per_company),
        "valued": valued,
        "skipped": skipped,
        "per_company": per_company,
    }


_REVENUE_UNIT_FACTOR = {"usd": 1.0, "usd_millions": 1e6, "usd_billions": 1e9}


def _guidance_abs_revenue(row: sqlite3.Row) -> float | None:
    """Absolute-dollar midpoint of a revenue-guidance figure, or None."""
    factor = _REVENUE_UNIT_FACTOR.get(row["unit"])
    if factor is None:
        return None
    mid = _midpoint(row["low"], row["high"])
    return mid * factor if mid is not None else None


def _midpoint(low, high) -> float | None:
    vals = [v for v in (low, high) if v is not None]
    return sum(vals) / len(vals) if vals else None


def _latest_unused_revenue_guidance(conn: sqlite3.Connection, cik: str) -> sqlite3.Row | None:
    """Most recent trigger-eligible revenue guidance not yet applied to a valuation.

    Selector guard: only a COMPANY-TOTAL, ANNUAL (FY) headline revenue figure may
    drive the year-1 total-revenue-growth model input. A segment figure (e.g. US
    commercial revenue), an aspirational target, or a quarterly period is excluded
    — never silently substituted. This is the structural fix for the PLTR
    corruption, where a US-commercial segment figure was used as the total.
    """
    return conn.execute(
        """
        SELECT ef.accession, ef.low, ef.high, ef.unit, ef.period, ef.delta_direction
        FROM extracted_figures ef
        JOIN filings_seen fs ON fs.accession = ef.accession
        WHERE ef.cik = ? AND ef.metric = 'revenue'
          AND ef.scope = 'total'
          AND ef.period LIKE 'FY%'
          AND ef.review_status = 'trigger_eligible'
          AND ef.delta_direction IN ('raised', 'lowered', 'initiated')
          AND ef.accession NOT IN (
              SELECT trigger_accession FROM dcf_valuations
              WHERE run_reason = 'guidance_change' AND trigger_accession IS NOT NULL
          )
        ORDER BY fs.filed_at DESC, ef.id DESC
        LIMIT 1
        """,
        (cik,),
    ).fetchone()


def run_guidance_revaluations(
    config: RedlineConfig,
    conn: sqlite3.Connection,
    *,
    company_factory: Callable[[str], Any] | None = None,
) -> dict:
    """Revalue when a filed 8-K revenue-guidance figure moves the year-1 input.

    This is the differentiated hook: a real number from a filing replaces the
    year-1 revenue-growth assumption, and the before/after is logged against the
    triggering 8-K accession with a ``guidance`` source link.
    """
    if company_factory is None:
        company_factory = edgar.Company
    edgar.set_identity(config.poller.edgar_user_agent)
    assumptions = load_assumptions(config.valuation.assumptions_path)
    mapping = fcf.load_fcf_mapping(config.valuation.fcf_mapping_path)

    per_company: list[dict] = []
    valued = 0
    for row in dcf_eligible_companies(conn):
        ticker, cik = row["ticker"], row["cik"]
        a = assumptions.get(ticker)
        if a is None:
            continue
        g = _latest_unused_revenue_guidance(conn, cik)
        if g is None:
            continue
        try:
            base = fcf.build_base_from_edgar(ticker, cik,
                                             company_factory=company_factory, mapping=mapping)
            validation = fcf.validate_base(
                base, known_fcf=a.known_fcf,
                tolerance=config.valuation.fcf_validation_tolerance)
            if not validation.passed:
                per_company.append({"ticker": ticker, "status": "unvalidated_base"})
                continue

            guidance_revenue = _guidance_abs_revenue(g)
            if guidance_revenue is None or base.base_revenue <= 0:
                per_company.append({"ticker": ticker, "status": "unmappable_guidance"})
                continue
            implied_growth = guidance_revenue / base.base_revenue - 1.0
            old_growth = a.revenue_growth[0]

            band, base_inputs = _compute_band(base, a, config,
                                              revenue_growth_y1_override=implied_growth)
            prior = _last_valuation(conn, cik)
            prior_snapshot = json.loads(prior["assumptions_json"]) if prior else None
            valuation_id = _insert_valuation(
                conn, base=base, a=a, band=band, base_inputs=base_inputs,
                run_reason="guidance_change", trigger_accession=g["accession"],
                config=config, prior_snapshot=prior_snapshot,
                extra_links=[{
                    "input_name": "revenue_growth_y1",
                    "old_value": old_growth, "new_value": implied_growth,
                    "source": "guidance",
                }],
            )
            valued += 1
            per_company.append({
                "ticker": ticker, "status": "revalued", "valuation_id": valuation_id,
                "trigger_accession": g["accession"], "guidance_direction": g["delta_direction"],
                "implied_y1_growth": round(implied_growth, 4),
                "per_share": [round(band.bear.per_share, 2), round(band.base.per_share, 2),
                              round(band.bull.per_share, 2)],
            })
        except Exception as e:
            per_company.append({"ticker": ticker, "status": "error",
                                "error": f"{type(e).__name__}: {e}"})

    return {"considered": len(per_company), "revalued": valued, "per_company": per_company}


def _decide(prior: sqlite3.Row | None, trigger_accession: str | None, force: bool) -> tuple[str, bool]:
    """Return (run_reason, should_revalue)."""
    if prior is None:
        return "new_filing", True
    if force:
        return "refresh", True
    if trigger_accession is not None and trigger_accession != prior["trigger_accession"]:
        return "new_filing", True
    return "refresh", False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DCF revaluation for redline.")
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit.")
    parser.add_argument("--force", action="store_true",
                        help="Recompute even if no new filing landed.")
    parser.add_argument("--guidance", action="store_true",
                        help="Run guidance-driven revaluations instead of the XBRL pass.")
    parser.add_argument("--settings", default="config/settings.toml")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = RedlineConfig.from_toml(args.settings)
    from redline.storage.db import open_db
    from redline.storage.schema import init_full_schema

    with open_db(config.storage.db_path) as conn:
        init_full_schema(conn)
        if args.guidance:
            summary = run_guidance_revaluations(config, conn)
        else:
            summary = run_once(config, conn, force=args.force)
        _LOG.info("cycle: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
