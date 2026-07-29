"""Tests for the XBRL base builder, FCF reconstruction, validation, and the
assumptions -> DcfInputs bridge (`redline.valuation.fcf` / `.models`).

edgartools is faked — no network.
"""
from __future__ import annotations

import pandas as pd
import pytest

from redline.storage.db import connect
from redline.storage.schema import init_full_schema
from redline.valuation import fcf
from redline.valuation.dcf import value_dcf
from redline.valuation.models import (
    XbrlBase,
    load_assumptions,
    to_dcf_inputs,
)

ASSUMPTIONS = "config/valuation/assumptions.yaml"
MAPPING = "config/valuation/fcf_mapping_v1.yaml"


# --- fakes for build_base_from_edgar ---------------------------------------

class _FakeFin:
    def get_revenue(self): return 1000.0
    def get_operating_income(self): return 200.0
    def get_capital_expenditures(self): return 50.0
    def get_operating_cash_flow(self): return 300.0
    def get_free_cash_flow(self): return 250.0
    def get_shares_outstanding_diluted(self): return 100.0


class _FakeFacts:
    def to_dataframe(self):
        return pd.DataFrame([
            {"concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
             "numeric_value": 1000.0, "fiscal_year": 2025,
             "period_start": "2025-01-01", "period_end": "2025-12-31"},
            {"concept": "us-gaap:CashAndCashEquivalentsAtCarryingValue",
             "numeric_value": 500.0, "fiscal_year": 2025,
             "period_start": None, "period_end": "2025-12-31"},
            {"concept": "us-gaap:LongTermDebt",
             "numeric_value": 200.0, "fiscal_year": 2025,
             "period_start": None, "period_end": "2025-12-31"},
            # older cash row must NOT win over the latest
            {"concept": "us-gaap:CashAndCashEquivalentsAtCarryingValue",
             "numeric_value": 111.0, "fiscal_year": 2024,
             "period_start": None, "period_end": "2024-12-31"},
        ])


class _FakeCompany:
    def __init__(self, ticker): self.ticker = ticker
    def get_facts(self): return _FakeFacts()
    def get_financials(self): return _FakeFin()


def _mapping():
    return fcf.load_fcf_mapping(MAPPING)


def test_build_base_from_edgar():
    base = fcf.build_base_from_edgar("PLTR", "0001321655",
                                     company_factory=_FakeCompany, mapping=_mapping())
    assert base.base_revenue == 1000.0
    assert base.shares_diluted == 100.0
    assert base.free_cash_flow == 250.0
    assert base.net_debt == -300.0  # debt 200 - latest cash 500 = net cash
    assert base.fiscal_year == 2025
    assert base.operating_margin == 0.2


def test_build_base_missing_revenue_raises():
    class NoRev(_FakeFin):
        def get_revenue(self): return None

    class C(_FakeCompany):
        def get_financials(self): return NoRev()

    with pytest.raises(ValueError):
        fcf.build_base_from_edgar("X", "0", company_factory=C, mapping=_mapping())


# --- validation -------------------------------------------------------------

def _base(fcf_val=250.0) -> XbrlBase:
    return XbrlBase(cik="0", ticker="X", base_revenue=1000.0, shares_diluted=100.0,
                    free_cash_flow=fcf_val, as_of="now")


def test_validate_base_pass_within_tolerance():
    r = fcf.validate_base(_base(250.0), known_fcf=260.0, tolerance=0.10)
    assert r.passed and r.relative_error is not None and r.relative_error < 0.10


def test_validate_base_fail_outside_tolerance():
    r = fcf.validate_base(_base(250.0), known_fcf=200.0, tolerance=0.10)
    assert not r.passed and r.relative_error == pytest.approx(0.25)


def test_validate_base_no_known_fails_gracefully():
    r = fcf.validate_base(_base(250.0), known_fcf=None, tolerance=0.10)
    assert not r.passed and "no hand-recorded" in " ".join(r.notes)


# --- canonical annual selection (the multi-value-per-year hazard) -----------

@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_full_schema(conn)
    yield conn
    conn.close()


def _insert_fact(conn, *, cik, concept, fy, val, start, end):
    conn.execute(
        """INSERT INTO xbrl_facts
           (cik, concept, fiscal_year, fiscal_period, period_start, period_end,
            numeric_value, ingested_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (cik, concept, fy, "FY", start, end, val, "now"),
    )


def test_annual_series_keys_off_period_end_not_fiscal_year(db):
    # The `fiscal_year` column is the FILING's year: fy=2025 bundles full-year
    # rows for periods ending 2024 AND 2025 (comparatives). Keying off fiscal_year
    # would confuse them; keying off period_end must separate them.
    ocf = "us-gaap:NetCashProvidedByUsedInOperatingActivities"
    _insert_fact(db, cik="1", concept=ocf, fy=2025, val=1154.0,
                 start="2024-01-01", end="2024-12-31")   # comparative, ends 2024
    _insert_fact(db, cik="1", concept=ocf, fy=2025, val=2134.0,
                 start="2025-01-01", end="2025-12-31")   # current, ends 2025
    _insert_fact(db, cik="1", concept=ocf, fy=2025, val=999.0,
                 start="2025-07-01", end="2025-12-31")   # partial, rejected
    series = fcf._annual_series_from_db(db, cik="1", concepts=[ocf])
    assert series == {2024: 1154.0, 2025: 2134.0}


def test_reconstruct_uses_latest_common_year(db):
    mapping = fcf.load_fcf_mapping(MAPPING)
    ocf = "us-gaap:NetCashProvidedByUsedInOperatingActivities"
    capex = "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"
    # OCF has 2024 + 2025; capex only 2025 -> latest common is 2025.
    _insert_fact(db, cik="1", concept=ocf, fy=2025, val=1154.0, start="2024-01-01", end="2024-12-31")
    _insert_fact(db, cik="1", concept=ocf, fy=2025, val=2134.0, start="2025-01-01", end="2025-12-31")
    _insert_fact(db, cik="1", concept=capex, fy=2025, val=34.0, start="2025-01-01", end="2025-12-31")
    val, year, gaps = fcf.reconstruct_fcf_from_facts(db, cik="1", mapping=mapping)
    assert year == 2025 and val == pytest.approx(2100.0) and gaps == []


def test_reconstruct_reports_gaps(db):
    mapping = fcf.load_fcf_mapping(MAPPING)
    val, year, gaps = fcf.reconstruct_fcf_from_facts(db, cik="9", mapping=mapping)
    assert val is None and year is None
    assert "operating_cash_flow" in gaps and "capex" in gaps


# --- assumptions loading + bridge ------------------------------------------

def test_assumptions_yaml_loads_six_non_financials():
    a = load_assumptions(ASSUMPTIONS)
    assert set(a) == {"PLTR", "NET", "MRNA", "VRTX", "CVNA", "ULTA"}
    # WACC + reference prices verified & transcribed 2026-07-28 -> no longer placeholder.
    assert not any(v.is_placeholder for v in a.values())


def test_to_dcf_inputs_scenario_ordering():
    a = load_assumptions(ASSUMPTIONS)["ULTA"]  # profitable, well-behaved
    base = XbrlBase(cik="0001403568", ticker="ULTA", base_revenue=12_392_800_000.0,
                    shares_diluted=45_000_000.0, net_debt=0.0, as_of="now")
    results = {}
    for scenario in ("bear", "base", "bull"):
        inp = to_dcf_inputs(base, a, scenario=scenario, projection_years=5,
                            terminal_growth_default=0.025)
        results[scenario] = value_dcf(inp).per_share
    assert results["bear"] <= results["base"] <= results["bull"]
    assert results["base"] > 0


def test_to_dcf_inputs_pads_growth_to_horizon():
    a = load_assumptions(ASSUMPTIONS)["VRTX"]
    base = XbrlBase(cik="0000875320", ticker="VRTX", base_revenue=12e9,
                    shares_diluted=258e6, net_debt=0.0, as_of="now")
    inp = to_dcf_inputs(base, a, scenario="base", projection_years=7,
                        terminal_growth_default=0.025)
    assert inp.drivers.horizon == 7  # 5-year schedule padded to 7
