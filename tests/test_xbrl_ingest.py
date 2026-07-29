"""Tests for companyfacts ingestion (`redline.valuation.xbrl`).

`edgar.Company` is mocked — no network. Covers the upsert projection, re-ingest
idempotency, restatement refresh (latest value wins), and bank exclusion.
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest

from redline.config import RedlineConfig
from redline.storage.db import connect
from redline.storage.schema import init_full_schema
from redline.valuation import xbrl


def _facts_df(rows: list[dict]) -> pd.DataFrame:
    cols = ["concept", "label", "value", "numeric_value", "unit",
            "period_type", "period_start", "period_end", "fiscal_year", "fiscal_period"]
    return pd.DataFrame([{c: r.get(c) for c in cols} for r in rows])


def _fact(concept, fy, val, *, fp="FY", start="2024-01-01", end="2024-12-31",
          unit="USD", ptype="duration", label="lbl"):
    return dict(concept=concept, label=label, value=val, numeric_value=val,
                unit=unit, period_type=ptype, period_start=start, period_end=end,
                fiscal_year=fy, fiscal_period=fp)


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_full_schema(conn)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO watchlist (cik, ticker, name, sector, added_at) VALUES (?,?,?,?,?)",
        [
            ("0000000001", "PLTR", "Palantir", "tech", now),
            ("0000000002", "MRNA", "Moderna", "healthcare", now),
            ("0000000003", "KEY", "KeyCorp", "financials", now),  # bank -> excluded
        ],
    )
    yield conn
    conn.close()


def _config() -> RedlineConfig:
    return RedlineConfig.from_toml("config/settings.toml")


def test_eligible_excludes_banks(db):
    tickers = [r["ticker"] for r in xbrl.dcf_eligible_companies(db)]
    assert tickers == ["MRNA", "PLTR"]  # KEY (financials) excluded, ordered by ticker


def test_upsert_projects_and_skips_undated_and_nan(db):
    df = _facts_df([
        _fact("us-gaap:Revenues", 2024, 1000.0),
        _fact("us-gaap:Revenues", None, 5.0),           # no fiscal_year -> skip
        dict(concept="us-gaap:X", label="l", value=None, numeric_value=float("nan"),
             unit="USD", period_type="duration", period_start="2024-01-01",
             period_end="2024-12-31", fiscal_year=2024, fiscal_period="FY"),  # NaN -> skip
    ])
    n = xbrl._upsert_facts(db, cik="0000000001", df=df)
    assert n == 1
    row = db.execute("SELECT concept, numeric_value FROM xbrl_facts").fetchone()
    assert row["concept"] == "us-gaap:Revenues"
    assert row["numeric_value"] == 1000.0


def test_reingest_is_idempotent(db):
    df = _facts_df([_fact("us-gaap:Revenues", 2024, 1000.0),
                    _fact("us-gaap:Revenues", 2023, 900.0)])
    xbrl._upsert_facts(db, cik="0000000001", df=df)
    xbrl._upsert_facts(db, cik="0000000001", df=df)
    count = db.execute("SELECT COUNT(*) AS c FROM xbrl_facts").fetchone()["c"]
    assert count == 2  # no duplication on re-ingest


def test_restatement_refreshes_value(db):
    xbrl._upsert_facts(db, cik="0000000001",
                       df=_facts_df([_fact("us-gaap:Revenues", 2024, 1000.0)]))
    # Same natural key, revised value.
    xbrl._upsert_facts(db, cik="0000000001",
                       df=_facts_df([_fact("us-gaap:Revenues", 2024, 1010.0)]))
    rows = db.execute("SELECT numeric_value FROM xbrl_facts").fetchall()
    assert len(rows) == 1
    assert rows[0]["numeric_value"] == 1010.0  # latest reported wins


def test_run_once_mocks_edgar(db, monkeypatch):
    def fake_company(ticker):
        m = MagicMock()
        m.get_facts.return_value.to_dataframe.return_value = _facts_df([
            _fact("us-gaap:Revenues", 2024, 100.0 if ticker == "PLTR" else 200.0),
        ])
        return m

    monkeypatch.setattr(xbrl.edgar, "set_identity", lambda *a, **k: None)
    monkeypatch.setattr(xbrl.edgar, "Company", fake_company)

    summary = xbrl.run_once(_config(), db)
    assert summary["considered"] == 2  # PLTR + MRNA, not KEY
    assert summary["ingested"] == 2 and summary["failed"] == 0
    ciks = {r["cik"] for r in db.execute("SELECT DISTINCT cik FROM xbrl_facts")}
    assert ciks == {"0000000001", "0000000002"}


def test_run_once_records_failure(db, monkeypatch):
    def boom(ticker):
        raise RuntimeError("SEC 503")

    monkeypatch.setattr(xbrl.edgar, "set_identity", lambda *a, **k: None)
    monkeypatch.setattr(xbrl.edgar, "Company", boom)
    summary = xbrl.run_once(_config(), db)
    assert summary["failed"] == 2 and summary["ingested"] == 0
    assert all("error" in c for c in summary["per_company"])
