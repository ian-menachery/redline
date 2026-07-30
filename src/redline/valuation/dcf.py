"""DCF engine — pure functions, no I/O.

An unlevered-FCF discounted-cash-flow model driven by an explicit per-year
schedule (revenue growth, operating margin, capex %, D&A %, working-capital %).
Everything here is deterministic and fully unit-testable; the XBRL base and the
per-company assumptions are supplied by callers (`valuation/revalue.py`).

Design commitments (plan §Decisions):

- **Ranges, never points.** The public surface is `run_scenarios(...)` returning
  a bear/base/bull `ScenarioBand`; `value_dcf` on a single input set is the
  building block, not the headline output.
- **Terminal-value transparency.** `DcfResult.terminal_value_fraction` surfaces
  how much of enterprise value sits in the (assumption-sensitive) terminal
  value, so a walkthrough can honestly flag TV dominance.

FCF_t = EBIT_t*(1 - tax) + D&A_t - capex_t - ΔNWC_t, with each line a % of that
year's projected revenue. Terminal value uses Gordon growth on the final-year
FCF; `terminal_growth < wacc` is enforced.
"""
from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, Field, model_validator


class DcfDrivers(BaseModel):
    """Per-projection-year driver schedule. All lists share one length = horizon."""

    revenue_growth: list[float] = Field(..., description="YoY revenue growth per year.")
    operating_margin: list[float] = Field(..., description="EBIT margin per year.")
    capex_pct_revenue: list[float] = Field(..., description="Capex as a fraction of revenue.")
    da_pct_revenue: list[float] = Field(..., description="D&A as a fraction of revenue.")
    nwc_pct_revenue: list[float] = Field(
        ..., description="Net working capital held as a fraction of revenue."
    )

    @model_validator(mode="after")
    def _equal_lengths(self) -> "DcfDrivers":
        lengths = {
            len(self.revenue_growth), len(self.operating_margin),
            len(self.capex_pct_revenue), len(self.da_pct_revenue),
            len(self.nwc_pct_revenue),
        }
        if len(lengths) != 1:
            raise ValueError("all driver lists must have the same length (the horizon)")
        if lengths == {0}:
            raise ValueError("driver schedule cannot be empty")
        return self

    @property
    def horizon(self) -> int:
        return len(self.revenue_growth)


class DcfInputs(BaseModel):
    """A complete single-scenario valuation input set."""

    base_revenue: float = Field(..., gt=0, description="Trailing revenue the projection grows from.")
    base_nwc: float = Field(..., description="Prior-year net working-capital level (for the first ΔNWC).")
    shares_outstanding: float = Field(..., gt=0)
    net_debt: float = Field(..., description="Debt minus cash; subtracted from EV to reach equity.")
    tax_rate: float = Field(..., ge=0.0, le=1.0)
    wacc: float = Field(..., gt=0.0)
    terminal_growth: float = Field(...)
    drivers: DcfDrivers

    @model_validator(mode="after")
    def _gordon_convergence(self) -> "DcfInputs":
        if self.terminal_growth >= self.wacc:
            raise ValueError("terminal_growth must be strictly less than wacc (Gordon growth)")
        return self


class DcfResult(BaseModel):
    """Single-scenario valuation output."""

    per_share: float
    enterprise_value: float
    equity_value: float
    pv_explicit: float = Field(..., description="PV of the explicit-horizon FCFs.")
    pv_terminal: float = Field(..., description="PV of the terminal value.")
    terminal_value_fraction: float = Field(
        ..., description="pv_terminal / enterprise_value — the TV-dominance check."
    )


class ProjectionYear(BaseModel):
    """One explicit-horizon year of the FCF projection (for auditability)."""

    year: int                 # 1-indexed year of the projection
    revenue_growth: float
    revenue: float
    ebit: float
    nopat: float
    fcf: float
    pv: float                 # PV of this year's FCF, discounted at WACC


class ScenarioBand(BaseModel):
    """Bear/base/bull range — the headline output. Never a single point."""

    bear: DcfResult
    base: DcfResult
    bull: DcfResult

    @model_validator(mode="after")
    def _ordered(self) -> "ScenarioBand":
        # A mis-signed scenario delta in assumptions.yaml (e.g. a positive bear
        # growth_delta) would otherwise produce a silently inverted range that
        # the per_share_low/high property names claim is ordered.
        if not (self.bear.per_share <= self.base.per_share <= self.bull.per_share):
            raise ValueError(
                "scenario band must satisfy bear <= base <= bull "
                f"(got {self.bear.per_share}, {self.base.per_share}, {self.bull.per_share})"
            )
        return self

    @property
    def per_share_low(self) -> float:
        return self.bear.per_share

    @property
    def per_share_base(self) -> float:
        return self.base.per_share

    @property
    def per_share_high(self) -> float:
        return self.bull.per_share


def project_fcf(inputs: DcfInputs) -> list[ProjectionYear]:
    """The explicit-horizon FCF projection, year by year. Pure; the single
    source of the projection math shared by `value_dcf` and the dashboard's
    "how this was modeled" view."""
    d = inputs.drivers
    revenue = inputs.base_revenue
    prev_nwc = inputs.base_nwc
    discount = 1.0 + inputs.wacc

    rows: list[ProjectionYear] = []
    for t in range(d.horizon):
        revenue = revenue * (1.0 + d.revenue_growth[t])
        ebit = revenue * d.operating_margin[t]
        nopat = ebit * (1.0 - inputs.tax_rate)
        da = revenue * d.da_pct_revenue[t]
        capex = revenue * d.capex_pct_revenue[t]
        nwc = revenue * d.nwc_pct_revenue[t]
        change_in_nwc = nwc - prev_nwc
        prev_nwc = nwc

        fcf = nopat + da - capex - change_in_nwc
        rows.append(ProjectionYear(
            year=t + 1, revenue_growth=d.revenue_growth[t], revenue=revenue,
            ebit=ebit, nopat=nopat, fcf=fcf, pv=fcf / (discount ** (t + 1)),
        ))
    return rows


def value_dcf(inputs: DcfInputs) -> DcfResult:
    """Value one scenario. Pure; the building block under `run_scenarios`."""
    d = inputs.drivers
    discount = 1.0 + inputs.wacc

    projection = project_fcf(inputs)
    pv_explicit = sum(r.pv for r in projection)
    last_fcf = projection[-1].fcf  # DcfDrivers enforces horizon >= 1

    # Gordon-growth terminal value on the final-year FCF, discounted back.
    # DcfInputs._gordon_convergence enforces wacc > terminal_growth on
    # construction, but the sensitivity mutators use model_copy (which skips
    # validators), so guard the denominator directly against a divergent sweep.
    denom = inputs.wacc - inputs.terminal_growth
    if denom <= 0:
        raise ValueError(
            f"wacc ({inputs.wacc}) must exceed terminal_growth "
            f"({inputs.terminal_growth}) for a convergent Gordon terminal value"
        )
    terminal_value = last_fcf * (1.0 + inputs.terminal_growth) / denom
    pv_terminal = terminal_value / (discount ** d.horizon)

    enterprise_value = pv_explicit + pv_terminal
    equity_value = enterprise_value - inputs.net_debt
    per_share = equity_value / inputs.shares_outstanding
    tv_fraction = pv_terminal / enterprise_value if enterprise_value else 0.0

    return DcfResult(
        per_share=per_share,
        enterprise_value=enterprise_value,
        equity_value=equity_value,
        pv_explicit=pv_explicit,
        pv_terminal=pv_terminal,
        terminal_value_fraction=tv_fraction,
    )


def run_scenarios(bear: DcfInputs, base: DcfInputs, bull: DcfInputs) -> ScenarioBand:
    """Value three assumption sets into a range. The headline valuation call."""
    return ScenarioBand(
        bear=value_dcf(bear),
        base=value_dcf(base),
        bull=value_dcf(bull),
    )


def sensitivity(
    base: DcfInputs,
    *,
    mutate: Callable[[DcfInputs, float], DcfInputs],
    values: list[float],
) -> list[tuple[float, float]]:
    """One-axis sensitivity sweep: per-share vs. a varied driver.

    ``mutate`` maps ``(base_inputs, value) -> new_inputs`` so callers can vary
    any driver (WACC, terminal growth, a uniform growth shift) without this
    function knowing the shape. Returns ``[(value, per_share), ...]``.
    """
    out: list[tuple[float, float]] = []
    for v in values:
        out.append((v, value_dcf(mutate(base, v)).per_share))
    return out


def shift_wacc(inputs: DcfInputs, wacc: float) -> DcfInputs:
    """Sensitivity mutator: replace WACC."""
    return inputs.model_copy(update={"wacc": wacc})


def shift_terminal_growth(inputs: DcfInputs, terminal_growth: float) -> DcfInputs:
    """Sensitivity mutator: replace terminal growth."""
    return inputs.model_copy(update={"terminal_growth": terminal_growth})


def shift_revenue_growth(inputs: DcfInputs, delta: float) -> DcfInputs:
    """Sensitivity mutator: add ``delta`` to every year's revenue growth."""
    d = inputs.drivers
    new_drivers = d.model_copy(
        update={"revenue_growth": [g + delta for g in d.revenue_growth]}
    )
    return inputs.model_copy(update={"drivers": new_drivers})
