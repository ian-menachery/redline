"""XBRL -> DCF base: build the financial anchor and validate it.

Two paths, by design (NOTES.md §6, plan Phase 1):

- **Primary — canonical accessors.** ``build_base_from_edgar`` uses edgartools'
  ``Financials``/``EntityFacts`` accessors, which resolve the period /
  prior-year-comparative ambiguity that raw companyfacts rows carry (the same
  fiscal year appears many times with different spans). Re-deriving that from
  the raw ``xbrl_facts`` table by hand is exactly how a DCF base gets silently
  corrupted, so we let edgartools own it.
- **Fallback + validation — ``fcf_mapping_v1.yaml`` over ``xbrl_facts``.**
  ``reconstruct_fcf_from_facts`` rebuilds FCF (operating cash flow − capex) from
  the stored facts, selecting the canonical full-year row per concept. It is the
  independent second opinion that ``validate_base`` cross-checks against the
  hand-recorded ``known_fcf``.
"""
from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from redline.valuation.models import XbrlBase

# Full-year duration is ~365 days; a companyfacts annual row spans close to it.
# Rows tagged FY but spanning far from a year are partial/comparative noise.
_FULL_YEAR_DAYS = 365
_YEAR_SPAN_TOLERANCE_DAYS = 60


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _safe(fn) -> float | None:
    """Call an accessor, returning a float or None (never raising)."""
    try:
        v = fn()
    except Exception:
        return None
    if isinstance(v, (int, float)) and v == v:  # exclude NaN
        return float(v)
    return None


def load_fcf_mapping(path: str | Path) -> dict[str, list[str]]:
    """Load the concept->FCF-line mapping (component -> ordered candidate tags)."""
    with Path(path).open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return {k: list(v) for k, v in data.items()}


def build_base_from_edgar(
    ticker: str, cik: str, *, company_factory: Callable[[str], Any],
    mapping: dict[str, list[str]],
) -> XbrlBase:
    """Build the DCF base from edgartools canonical accessors.

    Flow values (revenue, operating income, capex, OCF, FCF, diluted shares)
    come from the ``Financials`` accessors, which resolve period ambiguity.
    Balance-sheet items for the net_debt bridge come from the facts DataFrame
    via ``mapping`` (edgartools' ``get_concept`` uses different canonical keys
    and warns on raw us-gaap tags). ``company_factory`` is ``edgar.Company`` in
    production and a fake in tests.
    """
    co = company_factory(ticker)
    facts = co.get_facts()
    fin = co.get_financials()
    flags: list[str] = []

    base_revenue = _safe(fin.get_revenue)
    if base_revenue is None or base_revenue <= 0:
        raise ValueError(f"{ticker}: no usable revenue from companyfacts")

    shares = _safe(fin.get_shares_outstanding_diluted)
    if shares is None or shares <= 0:
        raise ValueError(f"{ticker}: no usable diluted share count")

    try:
        df = facts.to_dataframe()
    except Exception:
        df = None

    cash = _latest_value_from_df(df, mapping.get("cash", []))
    debt = _latest_value_from_df(df, mapping.get("total_debt", []))
    if cash is None and debt is None:
        flags.append("net_debt unresolved -> 0.0")
        net_debt = 0.0
    else:
        net_debt = (debt or 0.0) - (cash or 0.0)
        if debt is None:
            flags.append("no total-debt tag; net_debt = -cash (net-cash assumed)")

    return XbrlBase(
        cik=cik,
        ticker=ticker,
        fiscal_year=_latest_fiscal_year(df),
        base_revenue=base_revenue,
        operating_income=_safe(fin.get_operating_income),
        capex=_safe(fin.get_capital_expenditures),
        operating_cash_flow=_safe(fin.get_operating_cash_flow),
        free_cash_flow=_safe(fin.get_free_cash_flow),
        net_debt=net_debt,
        shares_diluted=shares,
        as_of=_now_iso(),
        flags=flags,
    )


def _latest_value_from_df(df, candidates: list[str]) -> float | None:
    """Latest reported value for the first resolving concept (instant/BS items).

    Picks the most recent fiscal_year, then the latest period_end within it.
    """
    if df is None:
        return None
    for concept in candidates:
        sub = df[(df["concept"] == concept) & (df["numeric_value"].notna())]
        if len(sub) == 0:
            continue
        sub = sub.sort_values(["fiscal_year", "period_end"])
        return float(sub.iloc[-1]["numeric_value"])
    return None


def _latest_annual_year_from_df(df, concepts: list[str]) -> int | None:
    """Latest calendar year with a full-year row (by PERIOD END, not the
    unreliable ``fiscal_year`` column) for any of ``concepts``.

    The companyfacts ``fiscal_year`` column is the *filing's* fiscal year, so a
    single value bundles the period plus its prior-year comparatives. The period
    that actually ends latest is the current annual — key off ``period_end``.
    """
    if df is None:
        return None
    best_year: int | None = None
    for concept in concepts:
        sub = df[(df["concept"] == concept) & (df["numeric_value"].notna())]
        for r in sub.itertuples(index=False):
            year = _full_year_end(getattr(r, "period_start", None), getattr(r, "period_end", None))
            if year is not None and (best_year is None or year > best_year):
                best_year = year
    return best_year


def _latest_fiscal_year(df) -> int | None:
    """Latest complete annual year across revenue / OCF (period-end based)."""
    return _latest_annual_year_from_df(
        df,
        ["us-gaap:Revenues",
         "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
         "us-gaap:NetCashProvidedByUsedInOperatingActivities"],
    )


# ---------------------------------------------------------------------------
# Fallback reconstruction from stored xbrl_facts + validation
# ---------------------------------------------------------------------------

def _span_error(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        d0 = datetime.date.fromisoformat(str(start)[:10])
        d1 = datetime.date.fromisoformat(str(end)[:10])
    except ValueError:
        return None
    return abs((d1 - d0).days - _FULL_YEAR_DAYS)


def _full_year_end(start: str | None, end: str | None) -> int | None:
    """Return the calendar year of ``end`` iff (start, end) is a full-year span."""
    span_err = _span_error(start, end)
    if span_err is None or span_err > _YEAR_SPAN_TOLERANCE_DAYS:
        return None
    try:
        return datetime.date.fromisoformat(str(end)[:10]).year
    except ValueError:
        return None


def _annual_series_from_db(
    conn: sqlite3.Connection, *, cik: str, concepts: list[str]
) -> dict[int, float]:
    """{period_end_year: canonical full-year value} for the first resolving concept.

    Keys off ``period_end`` year (not the filing's ``fiscal_year`` column) and,
    within a year, keeps the row whose span is closest to a full year.
    """
    for concept in concepts:
        rows = conn.execute(
            """
            SELECT numeric_value, period_start, period_end
            FROM xbrl_facts
            WHERE cik = ? AND concept = ? AND fiscal_period = 'FY'
              AND numeric_value IS NOT NULL
            """,
            (cik, concept),
        ).fetchall()
        by_year: dict[int, tuple[float, float]] = {}  # year -> (span_err, value)
        for r in rows:
            span_err = _span_error(r["period_start"], r["period_end"])
            year = _full_year_end(r["period_start"], r["period_end"])
            if span_err is None or year is None:
                continue
            if year not in by_year or span_err < by_year[year][0]:
                by_year[year] = (span_err, r["numeric_value"])
        if by_year:
            return {y: v for y, (_, v) in by_year.items()}
    return {}


def reconstruct_fcf_from_facts(
    conn: sqlite3.Connection,
    *,
    cik: str,
    mapping: dict[str, list[str]],
    year: int | None = None,
) -> tuple[float | None, int | None, list[str]]:
    """FCF = operating cash flow − capex from stored facts.

    Returns ``(fcf, year, gaps)``. ``year`` defaults to the latest year for
    which BOTH operating cash flow and capex have a canonical annual value.
    """
    gaps: list[str] = []
    ocf = _annual_series_from_db(conn, cik=cik, concepts=mapping.get("operating_cash_flow", []))
    capex = _annual_series_from_db(conn, cik=cik, concepts=mapping.get("capex", []))
    if not ocf:
        gaps.append("operating_cash_flow")
    if not capex:
        gaps.append("capex")
    common = sorted(set(ocf) & set(capex))
    if year is not None:
        common = [year] if (year in ocf and year in capex) else []
    if not common:
        if ocf and capex and not gaps:
            gaps.append("no_common_year")
        return None, None, gaps
    target = common[-1]
    return ocf[target] - capex[target], target, gaps


class ValidationResult(BaseModel):
    """Cross-check of the DCF base's FCF against the hand-recorded known value."""

    ticker: str
    accessor_fcf: float | None
    reconstructed_fcf: float | None
    known_fcf: float | None
    relative_error: float | None
    passed: bool
    notes: list[str]


def validate_base(
    base: XbrlBase,
    *,
    known_fcf: float | None,
    tolerance: float,
    reconstructed_fcf: float | None = None,
) -> ValidationResult:
    """Validate the base's FCF vs the hand-recorded ``known_fcf``.

    A CIK failing validation should ship as "unvalidated base" and be excluded
    from auto-revaluation until reconciled (plan Phase 1).
    """
    notes: list[str] = []
    accessor_fcf = base.free_cash_flow
    if accessor_fcf is None:
        notes.append("accessor returned no free_cash_flow")
    if known_fcf is None:
        notes.append("no hand-recorded known_fcf to validate against")
        return ValidationResult(
            ticker=base.ticker, accessor_fcf=accessor_fcf,
            reconstructed_fcf=reconstructed_fcf, known_fcf=None,
            relative_error=None, passed=False, notes=notes,
        )

    rel_error = None
    passed = False
    if accessor_fcf is not None and known_fcf != 0:
        rel_error = abs(accessor_fcf - known_fcf) / abs(known_fcf)
        passed = rel_error <= tolerance
        if not passed:
            notes.append(f"accessor FCF off known by {rel_error:.1%} (> {tolerance:.0%})")
    elif known_fcf == 0:
        # A relative-error check is undefined against a zero reference; surface
        # why validation didn't pass rather than a bare passed=False.
        notes.append("known_fcf is 0 — relative error undefined; cannot validate")
    return ValidationResult(
        ticker=base.ticker, accessor_fcf=accessor_fcf,
        reconstructed_fcf=reconstructed_fcf, known_fcf=known_fcf,
        relative_error=rel_error, passed=passed, notes=notes,
    )
