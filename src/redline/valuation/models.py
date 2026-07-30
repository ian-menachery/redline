"""Data models for the DCF valuation layer: the XBRL-derived base, the
per-company assumptions, and the bridge that combines them into `DcfInputs`.

`XbrlBase` is the point-in-time financial anchor pulled from companyfacts.
`CompanyAssumptions` is the hand-maintained (placeholder until Ian verifies)
driver schedule + WACC + reference price. `to_dcf_inputs` merges the two into a
single scenario's `DcfInputs` for the engine in `dcf.py`.

Modeling note (NOTES.md §6): net working capital is modeled as a small
*operating* fraction of revenue, NOT current-assets-minus-current-liabilities —
the latter is cash-inflated (e.g. PLTR CA-CL is ~1.6x revenue) and would wrongly
crush projected FCF via a huge ΔNWC.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from redline.valuation.dcf import DcfDrivers, DcfInputs


class XbrlBase(BaseModel):
    """Point-in-time financial anchor from companyfacts (latest fiscal year)."""

    cik: str
    ticker: str
    fiscal_year: int | None = None
    base_revenue: float = Field(..., gt=0)
    operating_income: float | None = None
    capex: float | None = None
    operating_cash_flow: float | None = None
    free_cash_flow: float | None = None
    net_debt: float = 0.0
    shares_diluted: float = Field(..., gt=0)
    as_of: str
    flags: list[str] = Field(default_factory=list)

    @property
    def operating_margin(self) -> float | None:
        if self.operating_income is None or not self.base_revenue:
            return None
        return self.operating_income / self.base_revenue


class ScenarioDelta(BaseModel):
    """Additive shifts applied to base drivers to form bear / bull scenarios."""

    growth_delta: float = 0.0
    margin_delta: float = 0.0


class CompanyAssumptions(BaseModel):
    """Per-company DCF assumptions. Placeholder until Ian verifies (see
    ``is_placeholder``). Drivers are grounded in XBRL history; WACC, terminal
    growth and reference price are hand-set judgment constants."""

    ticker: str
    cik: str
    name: str
    wacc: float = Field(..., gt=0.0)
    tax_rate: float = Field(..., ge=0.0, le=1.0)
    terminal_growth: float | None = None  # overrides the global default (§5a)
    revenue_growth: list[float] = Field(..., min_length=1)
    operating_margin: float  # forward steady-state EBIT margin
    capex_pct_revenue: float
    da_pct_revenue: float
    nwc_pct_revenue: float  # OPERATING working capital, not CA-CL (see module docstring)
    bear: ScenarioDelta = Field(default_factory=ScenarioDelta)
    bull: ScenarioDelta = Field(default_factory=ScenarioDelta)
    reference_price: float | None = None
    reference_price_asof: str | None = None
    known_fcf: float | None = None
    known_fcf_asof: str | None = None
    is_placeholder: bool = True
    low_confidence_note: str | None = None  # e.g. MRNA: declining revenue, negative FCF

    @model_validator(mode="after")
    def _terminal_below_wacc(self) -> CompanyAssumptions:
        tg = self.terminal_growth
        if tg is not None and tg >= self.wacc:
            raise ValueError(f"{self.ticker}: terminal_growth must be < wacc")
        return self


def _scenario_drivers(
    a: CompanyAssumptions, scenario: str, projection_years: int,
    *, revenue_growth_y1_override: float | None = None,
) -> DcfDrivers:
    if scenario == "bear":
        growth_delta, margin_delta = a.bear.growth_delta, a.bear.margin_delta
    elif scenario == "bull":
        growth_delta, margin_delta = a.bull.growth_delta, a.bull.margin_delta
    elif scenario == "base":
        growth_delta = margin_delta = 0.0
    else:
        raise ValueError(f"unknown scenario: {scenario!r}")

    # Fit the growth schedule to the horizon: pad with the last value, or trim.
    growth = list(a.revenue_growth)
    if len(growth) < projection_years:
        growth += [growth[-1]] * (projection_years - len(growth))
    growth = [g + growth_delta for g in growth[:projection_years]]
    # A stated forward-revenue-guidance figure replaces the year-1 growth
    # assumption with the real number (the whole point — a filed figure moves a
    # model input). The scenario delta still applies around it.
    if revenue_growth_y1_override is not None:
        growth[0] = revenue_growth_y1_override + growth_delta
    margin = a.operating_margin + margin_delta

    return DcfDrivers(
        revenue_growth=growth,
        operating_margin=[margin] * projection_years,
        capex_pct_revenue=[a.capex_pct_revenue] * projection_years,
        da_pct_revenue=[a.da_pct_revenue] * projection_years,
        nwc_pct_revenue=[a.nwc_pct_revenue] * projection_years,
    )


def to_dcf_inputs(
    base: XbrlBase,
    a: CompanyAssumptions,
    *,
    scenario: str,
    projection_years: int,
    terminal_growth_default: float,
    revenue_growth_y1_override: float | None = None,
) -> DcfInputs:
    """Merge the XBRL base and a company's assumptions into one scenario's inputs.

    ``base_nwc`` is set to ``base_revenue * nwc_pct_revenue`` so the first-year
    ΔNWC is purely growth-driven (consistent operating-WC treatment).
    ``revenue_growth_y1_override`` (from a filed guidance figure) replaces the
    year-1 growth assumption.
    """
    drivers = _scenario_drivers(
        a, scenario, projection_years,
        revenue_growth_y1_override=revenue_growth_y1_override,
    )
    terminal_growth = a.terminal_growth if a.terminal_growth is not None else terminal_growth_default
    return DcfInputs(
        base_revenue=base.base_revenue,
        base_nwc=base.base_revenue * a.nwc_pct_revenue,
        shares_outstanding=base.shares_diluted,
        net_debt=base.net_debt,
        tax_rate=a.tax_rate,
        wacc=a.wacc,
        terminal_growth=terminal_growth,
        drivers=drivers,
    )


def load_assumptions(path: str | Path) -> dict[str, CompanyAssumptions]:
    """Load ``assumptions.yaml`` into ``{ticker: CompanyAssumptions}``."""
    with Path(path).open(encoding="utf-8") as f:
        entries = yaml.safe_load(f)
    if not isinstance(entries, list):
        raise ValueError("assumptions.yaml must be a top-level list")
    out: dict[str, CompanyAssumptions] = {}
    for entry in entries:
        model = CompanyAssumptions(**entry)
        out[model.ticker] = model
    return out
