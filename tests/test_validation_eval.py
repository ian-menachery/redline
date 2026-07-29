"""Tests for the FCF-base validation eval (`redline.valuation.validation_eval`)."""
from __future__ import annotations

import datetime

import pandas as pd
import pytest

from redline.config import RedlineConfig
from redline.storage.db import connect
from redline.storage.schema import init_full_schema
from redline.valuation import fcf, validation_eval

PLTR_CIK = "0001321655"
PLTR_KNOWN_FCF = 2_100_600_000.0


class _Fin:
    def __init__(self, fcf_val): self._fcf = fcf_val
    def get_revenue(self): return 4.475e9
    def get_operating_income(self): return 1.4e9
    def get_capital_expenditures(self): return 0.034e9
    def get_operating_cash_flow(self): return 2.134e9
    def get_free_cash_flow(self): return self._fcf
    def get_shares_outstanding_diluted(self): return 2.565e9


class _Facts:
    def to_dataframe(self):
        return pd.DataFrame([
            {"concept": "us-gaap:CashAndCashEquivalentsAtCarryingValue",
             "numeric_value": 2.29e9, "fiscal_year": 2025, "period_end": "2025-12-31"},
        ])


def _factory(fcf_val):
    class _Co:
        def __init__(self, ticker): self.ticker = ticker
        def get_facts(self): return _Facts()
        def get_financials(self): return _Fin(fcf_val)
    return _Co


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_full_schema(conn)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO watchlist (cik, ticker, name, sector, added_at) VALUES (?,?,?,?,?)",
        (PLTR_CIK, "PLTR", "Palantir", "tech", now),
    )
    # Seed the facts needed for DB reconstruction: OCF - capex = 2.100e9.
    for concept, val in [
        ("us-gaap:NetCashProvidedByUsedInOperatingActivities", 2.134e9),
        ("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", 0.034e9),
    ]:
        conn.execute(
            """INSERT INTO xbrl_facts (cik, concept, fiscal_year, fiscal_period,
                   period_start, period_end, numeric_value, ingested_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (PLTR_CIK, concept, 2025, "FY", "2025-01-01", "2025-12-31", val, now),
        )
    yield conn
    conn.close()


def _cfg():
    return RedlineConfig.from_toml("config/settings.toml")


def test_evaluate_company_three_way_agreement(db):
    mapping = fcf.load_fcf_mapping(_cfg().valuation.fcf_mapping_path)
    r = validation_eval.evaluate_company(
        db, ticker="PLTR", cik=PLTR_CIK, accessor_fcf=2.1006e9,
        fiscal_year=2025, known_fcf=PLTR_KNOWN_FCF, mapping=mapping, tolerance=0.10)
    assert r["reconstructed_fcf"] == pytest.approx(2.100e9)
    assert r["passed"] is True
    assert r["err_accessor_vs_reconstructed"] < 0.01


def test_run_validation_writes_eval_runs(db):
    summary = validation_eval.run_validation(_cfg(), db, company_factory=_factory(PLTR_KNOWN_FCF))
    assert summary["companies"] == 1 and summary["passed"] == 1
    row = db.execute(
        "SELECT event_id, graded_pass FROM eval_runs WHERE event_id LIKE 'fcf_validation:%'"
    ).fetchone()
    assert row["event_id"] == "fcf_validation:PLTR"
    assert row["graded_pass"] == 1


def test_accessor_reconstruction_disagreement_noted(db):
    mapping = fcf.load_fcf_mapping(_cfg().valuation.fcf_mapping_path)
    # accessor far from the DB reconstruction (2.10e9) -> disagreement note.
    r = validation_eval.evaluate_company(
        db, ticker="PLTR", cik=PLTR_CIK, accessor_fcf=5.0e9,
        fiscal_year=2025, known_fcf=5.0e9, mapping=mapping, tolerance=0.10)
    assert any("disagree" in n for n in r["notes"])
