"""Tests for the pure DCF engine (`redline.valuation.dcf`).

The headline case is hand-computed with horizon=1 so the arithmetic is pinned
exactly (see the docstring in `test_value_dcf_closed_form`).
"""
from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from redline.valuation.dcf import (
    DcfDrivers,
    DcfInputs,
    run_scenarios,
    sensitivity,
    shift_revenue_growth,
    shift_terminal_growth,
    shift_wacc,
    value_dcf,
)


def _drivers(
    *, growth=0.10, margin=0.20, capex=0.05, da=0.05, nwc=0.10, years=1
) -> DcfDrivers:
    return DcfDrivers(
        revenue_growth=[growth] * years,
        operating_margin=[margin] * years,
        capex_pct_revenue=[capex] * years,
        da_pct_revenue=[da] * years,
        nwc_pct_revenue=[nwc] * years,
    )


def _inputs(**overrides) -> DcfInputs:
    base = dict(
        base_revenue=1000.0,
        base_nwc=100.0,
        shares_outstanding=100.0,
        net_debt=437.5,
        tax_rate=0.25,
        wacc=0.10,
        terminal_growth=0.02,
        drivers=_drivers(),
    )
    base.update(overrides)
    return DcfInputs(**base)


def test_value_dcf_closed_form():
    """Horizon=1, hand-computed:

    revenue     = 1000 * 1.10           = 1100
    EBIT        = 1100 * 0.20           = 220
    NOPAT       = 220 * (1-0.25)        = 165
    D&A         = 1100 * 0.05           = 55
    capex       = 1100 * 0.05           = 55
    NWC         = 1100 * 0.10 = 110; ΔNWC = 110 - 100 = 10
    FCF         = 165 + 55 - 55 - 10    = 155
    PV(FCF)     = 155 / 1.10            = 140.9090909...
    TV          = 155 * 1.02 / 0.08     = 1976.25
    PV(TV)      = 1976.25 / 1.10        = 1796.5909090...
    EV          = 1937.5
    equity      = 1937.5 - 437.5        = 1500
    per_share   = 1500 / 100            = 15.0
    """
    r = value_dcf(_inputs())
    assert math.isclose(r.enterprise_value, 1937.5, rel_tol=1e-9)
    assert math.isclose(r.equity_value, 1500.0, rel_tol=1e-9)
    assert math.isclose(r.per_share, 15.0, rel_tol=1e-9)
    assert math.isclose(r.terminal_value_fraction, 1796.590909090909 / 1937.5, rel_tol=1e-9)


def test_terminal_value_fraction_in_unit_interval():
    r = value_dcf(_inputs())
    assert 0.0 < r.terminal_value_fraction < 1.0
    # A low-growth, long-horizon model is still TV-dominated — sanity, not a bug.
    assert r.terminal_value_fraction > 0.5


def test_scenario_ordering_bear_le_base_le_bull():
    base = _inputs()
    bear = base.model_copy(update={"drivers": _drivers(growth=0.02, margin=0.15)})
    bull = base.model_copy(update={"drivers": _drivers(growth=0.18, margin=0.25)})
    band = run_scenarios(bear=bear, base=base, bull=bull)
    assert band.per_share_low <= band.per_share_base <= band.per_share_high


def test_gordon_convergence_enforced():
    with pytest.raises(ValidationError):
        _inputs(terminal_growth=0.10)  # == wacc
    with pytest.raises(ValidationError):
        _inputs(terminal_growth=0.12)  # > wacc


def test_driver_lengths_must_match():
    with pytest.raises(ValidationError):
        DcfDrivers(
            revenue_growth=[0.1, 0.1],
            operating_margin=[0.2],
            capex_pct_revenue=[0.05],
            da_pct_revenue=[0.05],
            nwc_pct_revenue=[0.10],
        )


def test_empty_schedule_rejected():
    with pytest.raises(ValidationError):
        DcfDrivers(
            revenue_growth=[], operating_margin=[], capex_pct_revenue=[],
            da_pct_revenue=[], nwc_pct_revenue=[],
        )


def test_sensitivity_wacc_monotonic_decreasing():
    base = _inputs()
    curve = sensitivity(base, mutate=shift_wacc, values=[0.08, 0.10, 0.12, 0.14])
    per_shares = [ps for _, ps in curve]
    assert per_shares == sorted(per_shares, reverse=True)  # higher WACC -> lower value


def test_sensitivity_growth_monotonic_increasing():
    base = _inputs(drivers=_drivers(years=5))
    curve = sensitivity(base, mutate=shift_revenue_growth, values=[-0.02, 0.0, 0.02, 0.04])
    per_shares = [ps for _, ps in curve]
    assert per_shares == sorted(per_shares)  # more growth -> higher value


def test_sensitivity_terminal_growth_monotonic_increasing():
    base = _inputs()
    curve = sensitivity(base, mutate=shift_terminal_growth, values=[0.00, 0.01, 0.02, 0.03])
    per_shares = [ps for _, ps in curve]
    assert per_shares == sorted(per_shares)


def test_multi_year_horizon_runs():
    r = value_dcf(_inputs(drivers=_drivers(years=5)))
    assert r.per_share > 0
    assert r.pv_explicit > 0 and r.pv_terminal > 0
