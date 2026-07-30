"""Tests for versioned revaluation (`redline.valuation.revalue`).

edgartools is faked. Covers initial valuation, the up-to-date short-circuit,
new-filing trigger + input-link audit, forced refresh, unvalidated-base skip,
and bank exclusion.
"""
from __future__ import annotations

import datetime

import pandas as pd
import pytest

from redline.config import RedlineConfig
from redline.storage.db import connect
from redline.storage.schema import init_full_schema
from redline.valuation import revalue

# Matches assumptions.yaml PLTR known_fcf so validation passes.
PLTR_KNOWN_FCF = 2_100_600_000.0
PLTR_CIK = "0001321655"


class _Fin:
    def __init__(self, revenue, fcf):
        self._rev, self._fcf = revenue, fcf

    def get_revenue(self): return self._rev
    def get_operating_income(self): return self._rev * 0.30
    def get_capital_expenditures(self): return self._rev * 0.01
    def get_operating_cash_flow(self): return self._fcf * 1.02
    def get_free_cash_flow(self): return self._fcf
    def get_shares_outstanding_diluted(self): return 2.565e9


class _Facts:
    def to_dataframe(self):
        return pd.DataFrame([
            {"concept": "us-gaap:CashAndCashEquivalentsAtCarryingValue",
             "numeric_value": 2.29e9, "fiscal_year": 2025, "period_end": "2025-12-31"},
        ])


def _factory(revenue, fcf):
    class _Co:
        def __init__(self, ticker): self.ticker = ticker
        def get_facts(self): return _Facts()
        def get_financials(self): return _Fin(revenue, fcf)
    return _Co


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_full_schema(conn)
    now = datetime.datetime.now(datetime.UTC).isoformat()
    conn.executemany(
        "INSERT INTO watchlist (cik, ticker, name, sector, added_at) VALUES (?,?,?,?,?)",
        [(PLTR_CIK, "PLTR", "Palantir", "tech", now),
         ("0000091576", "KEY", "KeyCorp", "financials", now)],  # bank -> excluded
    )
    yield conn
    conn.close()


def _seed_filing(conn, accession, filed_at):
    conn.execute(
        """INSERT INTO filings_seen (accession, cik, filing_type, filed_at, status,
                                     retry_count, discovered_at)
           VALUES (?, ?, '10-K', ?, 'analyzed', 0, ?)""",
        (accession, PLTR_CIK, filed_at, filed_at),
    )


def _cfg():
    return RedlineConfig.from_toml("config/settings.toml")


def _valuations(conn):
    return conn.execute("SELECT * FROM dcf_valuations ORDER BY id").fetchall()


def test_initial_valuation_excludes_banks(db):
    _seed_filing(db, "ACC1", "2026-02-17")
    summary = revalue.run_once(_cfg(), db, company_factory=_factory(4.475e9, PLTR_KNOWN_FCF))
    assert summary["valued"] == 1
    tickers = {c["ticker"] for c in summary["per_company"]}
    assert tickers == {"PLTR"}  # KEY (financials) never considered
    rows = _valuations(db)
    assert len(rows) == 1
    assert rows[0]["run_reason"] == "new_filing"
    assert rows[0]["trigger_accession"] == "ACC1"
    # no prior -> no input links
    links = db.execute("SELECT COUNT(*) c FROM valuation_input_links").fetchone()["c"]
    assert links == 0


def test_second_run_up_to_date(db):
    _seed_filing(db, "ACC1", "2026-02-17")
    f = _factory(4.475e9, PLTR_KNOWN_FCF)
    revalue.run_once(_cfg(), db, company_factory=f)
    summary = revalue.run_once(_cfg(), db, company_factory=f)
    assert summary["valued"] == 0
    assert any(c["status"] == "up_to_date" for c in summary["per_company"])
    assert len(_valuations(db)) == 1  # no duplicate


def test_new_filing_triggers_revaluation_and_links(db):
    _seed_filing(db, "ACC1", "2026-02-17")
    revalue.run_once(_cfg(), db, company_factory=_factory(4.475e9, PLTR_KNOWN_FCF))
    # A newer periodic filing lands; revenue base moves.
    _seed_filing(db, "ACC2", "2026-05-05")
    summary = revalue.run_once(_cfg(), db, company_factory=_factory(5.0e9, PLTR_KNOWN_FCF))
    assert summary["valued"] == 1
    rows = _valuations(db)
    assert len(rows) == 2
    assert rows[1]["trigger_accession"] == "ACC2"
    link = db.execute(
        "SELECT input_name, old_value, new_value FROM valuation_input_links "
        "WHERE input_name='base_revenue'"
    ).fetchone()
    assert link is not None
    assert link["old_value"] == pytest.approx(4.475e9)
    assert link["new_value"] == pytest.approx(5.0e9)


def test_force_refresh_creates_row_with_refresh_reason(db):
    _seed_filing(db, "ACC1", "2026-02-17")
    f = _factory(4.475e9, PLTR_KNOWN_FCF)
    revalue.run_once(_cfg(), db, company_factory=f)
    summary = revalue.run_once(_cfg(), db, company_factory=f, force=True)
    assert summary["valued"] == 1
    rows = _valuations(db)
    assert len(rows) == 2 and rows[1]["run_reason"] == "refresh"


def test_guidance_revaluation_moves_year1_growth(db):
    # An 8-K with a trigger-eligible FY2026 revenue guidance of $5.0-5.2B.
    db.execute(
        """INSERT INTO filings_seen (accession, cik, filing_type, filed_at, status,
               retry_count, discovered_at) VALUES (?, ?, '8-K', ?, 'analyzed', 0, ?)""",
        ("GUID8K", PLTR_CIK, "2026-05-05", "2026-05-05"))
    db.execute(
        """INSERT INTO extracted_figures
           (accession, cik, metric, scope, period, low, high, unit, basis, is_reaffirmed,
            confidence, context, review_status, delta_direction, prompt_version,
            parser_version, extracted_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("GUID8K", PLTR_CIK, "revenue", "total", "FY2026", 5.0, 5.2, "usd_billions", "non_gaap",
         0, 0.9, "guidance of $5.0 to $5.2 billion", "trigger_eligible", "raised",
         "v1", "v1", "2026-05-05T00:00:00Z"))

    summary = revalue.run_guidance_revaluations(
        _cfg(), db, company_factory=_factory(4.475e9, PLTR_KNOWN_FCF))
    assert summary["revalued"] == 1
    row = db.execute("SELECT run_reason, trigger_accession FROM dcf_valuations").fetchone()
    assert row["run_reason"] == "guidance_change" and row["trigger_accession"] == "GUID8K"
    link = db.execute(
        "SELECT old_value, new_value, source FROM valuation_input_links "
        "WHERE input_name='revenue_growth_y1'").fetchone()
    assert link["source"] == "guidance"
    assert link["old_value"] == pytest.approx(0.35)                 # PLTR assumptions y1 growth
    assert link["new_value"] == pytest.approx(5.1e9 / 4.475e9 - 1, rel=1e-3)  # implied by guidance
    # a second run does not re-apply the same guidance
    assert revalue.run_guidance_revaluations(
        _cfg(), db, company_factory=_factory(4.475e9, PLTR_KNOWN_FCF))["revalued"] == 0


def test_guidance_selector_excludes_segment_and_quarterly(db):
    # The structural PLTR fix: a segment revenue figure and a quarterly total
    # figure must NEVER drive the annual total-revenue-growth input.
    db.execute(
        """INSERT INTO filings_seen (accession, cik, filing_type, filed_at, status,
               retry_count, discovered_at) VALUES (?, ?, '8-K', ?, 'analyzed', 0, ?)""",
        ("SEG8K", PLTR_CIK, "2026-05-05", "2026-05-05"))
    common = ("usd_billions", "unspecified", 0, 0.95, "ctx", "trigger_eligible",
              "raised", "v1", "v1", "2026-05-05T00:00:00Z")
    # (a) a SEGMENT FY revenue figure (US-commercial), and (b) a TOTAL but QUARTERLY figure
    db.executemany(
        """INSERT INTO extracted_figures
           (accession, cik, metric, scope, period, low, high, unit, basis, is_reaffirmed,
            confidence, context, review_status, delta_direction, prompt_version,
            parser_version, extracted_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            ("SEG8K", PLTR_CIK, "revenue", "segment", "FY2026", 3.224, 3.224, *common),
            ("SEG8K", PLTR_CIK, "revenue", "total", "Q2FY2026", 1.797, 1.801, *common),
        ])
    summary = revalue.run_guidance_revaluations(
        _cfg(), db, company_factory=_factory(4.475e9, PLTR_KNOWN_FCF))
    assert summary["revalued"] == 0          # neither figure is an eligible driver
    assert len(_valuations(db)) == 0


def test_unvalidated_base_is_skipped(db):
    _seed_filing(db, "ACC1", "2026-02-17")
    # FCF wildly off the hand-recorded known_fcf -> validation fails.
    summary = revalue.run_once(_cfg(), db, company_factory=_factory(4.475e9, 1.0e6))
    assert summary["valued"] == 0
    assert any(c["status"] == "unvalidated_base" for c in summary["per_company"])
    assert len(_valuations(db)) == 0
