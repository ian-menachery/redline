"""Phase-0 feasibility spike (THROWAWAY): reverse-DCF for the monitored names.

Question the gate answers before any build: can a reverse-DCF put a *sensible*
market-implied growth on the high-multiple / turnaround names (NET, PLTR, MRNA,
CVNA)? For each, root-find the constant revenue growth at which the base-case
DCF per-share equals the manual reference price, and compare to the trailing
revenue CAGR from xbrl_facts.

Gate (fixed before results): reverse-DCF converges to a finite implied growth in
[0, 60%] for >=3/4 names AND a trailing CAGR is computable -> PROCEED to build a
"priced-in growth" surface. Else -> document the null result and SKIP the build
(these stay monitored). Information-surfacing only; no verdict, no LLM.

    python -m spike.growthname_feasibility   (or run the file directly)
"""
from __future__ import annotations

import sqlite3

import edgar

from redline.config import RedlineConfig
from redline.valuation import fcf
from redline.valuation.dcf import value_dcf
from redline.valuation.models import load_assumptions, to_dcf_inputs

NAMES = ["NET", "PLTR", "MRNA", "CVNA"]
DB = "data/redline.db"
GATE_BAND = (0.0, 0.60)


def _implied_growth(base_inputs, reference: float, lo: float = -0.5, hi: float = 1.5):
    """Uniform revenue growth g s.t. base-case per-share == reference. None if
    the reference lies outside the [lo, hi] growth bracket (un-rationalizable)."""
    d0 = base_inputs.drivers

    def ps(g: float) -> float:
        d = d0.model_copy(update={"revenue_growth": [g] * d0.horizon})
        return value_dcf(base_inputs.model_copy(update={"drivers": d})).per_share

    if not (ps(lo) <= reference <= ps(hi)):
        return None
    for _ in range(60):
        mid = (lo + hi) / 2.0
        if ps(mid) < reference:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _trailing_cagr(conn: sqlite3.Connection, cik: str):
    rows = conn.execute(
        """SELECT period_end, numeric_value FROM xbrl_facts
           WHERE cik = ?
             AND (concept LIKE '%Revenues'
                  OR concept LIKE '%RevenueFromContractWithCustomerExcludingAssessedTax')
             AND numeric_value > 0
           ORDER BY period_end""",
        (cik,),
    ).fetchall()
    # keep one value per calendar year (annual-ish), earliest vs latest
    by_year: dict[str, float] = {}
    for pe, val in rows:
        by_year[str(pe)[:4]] = float(val)
    if len(by_year) < 2:
        return None
    years = sorted(by_year)
    first, last = by_year[years[0]], by_year[years[-1]]
    n = int(years[-1]) - int(years[0])
    if n <= 0 or first <= 0:
        return None
    return (last / first) ** (1.0 / n) - 1.0


def main() -> None:
    config = RedlineConfig.from_toml("config/settings.toml")
    edgar.set_identity(config.poller.edgar_user_agent)
    assumptions = load_assumptions(config.valuation.assumptions_path)
    mapping = fcf.load_fcf_mapping(config.valuation.fcf_mapping_path)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cik_by_ticker = {r["ticker"]: r["cik"] for r in conn.execute("SELECT ticker, cik FROM watchlist")}

    print(f"{'ticker':<6}{'ref_price':>10}{'base_ps':>10}{'implied_g':>11}{'trail_CAGR':>11}  status")
    converged = 0
    cagr_ok = 0
    for t in NAMES:
        a = assumptions.get(t)
        cik = cik_by_ticker.get(t)
        if a is None or cik is None:
            print(f"{t:<6}  (no assumptions / cik)")
            continue
        try:
            base = fcf.build_base_from_edgar(t, cik, company_factory=edgar.Company, mapping=mapping)
            bi = to_dcf_inputs(
                base, a, scenario="base",
                projection_years=config.valuation.projection_years,
                terminal_growth_default=config.valuation.terminal_growth_default,
            )
            base_ps = value_dcf(bi).per_share
            g = _implied_growth(bi, a.reference_price) if a.reference_price else None
            cagr = _trailing_cagr(conn, cik)
            in_band = g is not None and GATE_BAND[0] <= g <= GATE_BAND[1]
            converged += int(in_band)
            cagr_ok += int(cagr is not None)
            g_s = f"{g:.1%}" if g is not None else "n/a"
            c_s = f"{cagr:.1%}" if cagr is not None else "n/a"
            status = "in-band" if in_band else ("outside band" if g is not None else "no bracket")
            print(f"{t:<6}{a.reference_price:>10.2f}{base_ps:>10.0f}{g_s:>11}{c_s:>11}  {status}")
        except Exception as e:
            print(f"{t:<6}  ERROR {type(e).__name__}: {e}")
    conn.close()

    passed = converged >= 3 and cagr_ok >= 1
    print(f"\nGATE: {converged}/4 in [{GATE_BAND[0]:.0%},{GATE_BAND[1]:.0%}], "
          f"CAGR computable for {cagr_ok}/4 -> {'PROCEED' if passed else 'DOCUMENT + SKIP'}")


if __name__ == "__main__":
    main()
