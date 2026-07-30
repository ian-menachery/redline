"""Tests for 8-K guidance extraction (`redline.valuation.guidance`).

The LLM client and edgar are mocked — no network, no API spend.
"""
from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest

from redline.config import RedlineConfig
from redline.llm.schemas import GuidanceExtraction, GuidanceFigure
from redline.storage.db import connect
from redline.storage.schema import init_full_schema
from redline.valuation import guidance

CIK = "0000875320"  # VRTX


def _fig(metric="revenue", period="FY2026", low=12.95, high=13.1, unit="usd_billions",
         basis="non_gaap", reaffirmed=False, conf=0.9, scope="total",
         context="revenue guidance of $12.95B to $13.1B"):
    return GuidanceFigure(metric=metric, scope=scope, period=period, low=low, high=high,
                          unit=unit, basis=basis, is_reaffirmed=reaffirmed,
                          confidence=conf, context=context)


class _Att:
    def __init__(self, dt, text): self.document_type, self._t = dt, text
    def text(self): return self._t


class _Obj:
    def __init__(self, items): self.items = items


class _Filing:
    def __init__(self, items, exhibit):
        self._items = items
        self.attachments = [_Att("8-K", "body"), _Att("EX-99.1", exhibit)]
    def obj(self): return _Obj(self._items)


@pytest.fixture()
def db(tmp_path):
    conn = connect(tmp_path / "t.db")
    init_full_schema(conn)
    now = datetime.datetime.now(datetime.UTC).isoformat()
    conn.execute("INSERT INTO watchlist (cik,ticker,name,sector,added_at) VALUES (?,?,?,?,?)",
                 (CIK, "VRTX", "Vertex", "healthcare", now))
    yield conn
    conn.close()


def _seed_8k(conn, accession, filed_at="2026-02-12"):
    conn.execute(
        """INSERT INTO filings_seen (accession, cik, filing_type, filed_at, status,
               retry_count, discovered_at) VALUES (?, ?, '8-K', ?, 'analyzed', 0, ?)""",
        (accession, CIK, filed_at, filed_at))


def _cfg():
    return RedlineConfig.from_toml("config/settings.toml")


# --- gating + delta units ---------------------------------------------------

def test_review_status_confidence_gate():
    assert guidance._review_status(_fig(conf=0.9), 0.75) == "trigger_eligible"
    assert guidance._review_status(_fig(conf=0.6), 0.75) == "manual_review"
    # unspecified basis disqualifies a NON-revenue metric even at high confidence
    # (revenue is the fix-#3 exception, covered in its own test).
    assert guidance._review_status(
        _fig(metric="ebitda", conf=0.95, basis="unspecified"), 0.75) == "manual_review"


def test_review_status_revenue_unspecified_exception():
    # fix #3: revenue guidance with unspecified basis IS trigger-eligible...
    assert guidance._review_status(
        _fig(metric="revenue", basis="unspecified", conf=0.9), 0.75) == "trigger_eligible"
    # ...but the strict basis gate still holds for every other metric.
    assert guidance._review_status(
        _fig(metric="ebitda", basis="unspecified", conf=0.95), 0.75) == "manual_review"


def test_delta_direction(db):
    _seed_8k(db, "PRIOR", "2025-11-03")
    _seed_8k(db, "CUR", "2026-02-12")
    # store a prior FY2026 revenue guidance of 11.9-12.0
    guidance._store_figure(db, accession="PRIOR", cik=CIK,
                           fig=_fig(low=11.9, high=12.0), min_conf=0.75)
    # a raised guidance in the current filing
    review = guidance._store_figure(db, accession="CUR", cik=CIK,
                                    fig=_fig(low=12.95, high=13.1), min_conf=0.75)
    assert review == "trigger_eligible"
    row = db.execute("SELECT delta_direction, delta_prior_accession FROM extracted_figures "
                     "WHERE accession='CUR'").fetchone()
    assert row["delta_direction"] == "raised"
    assert row["delta_prior_accession"] == "PRIOR"


def test_delta_initiated_and_reaffirmed(db):
    _seed_8k(db, "A", "2025-11-03")
    _seed_8k(db, "B", "2026-02-12")
    guidance._store_figure(db, accession="A", cik=CIK, fig=_fig(low=13.0, high=13.0), min_conf=0.75)
    first = db.execute("SELECT delta_direction FROM extracted_figures WHERE accession='A'").fetchone()
    assert first["delta_direction"] == "initiated"
    guidance._store_figure(db, accession="B", cik=CIK, fig=_fig(low=13.0, high=13.0), min_conf=0.75)
    second = db.execute("SELECT delta_direction FROM extracted_figures WHERE accession='B'").fetchone()
    assert second["delta_direction"] == "reaffirmed"


# --- run_once ---------------------------------------------------------------

def test_run_once_extracts_and_marks(db, monkeypatch):
    _seed_8k(db, "EARN1")
    monkeypatch.setattr(guidance.edgar, "set_identity", lambda *a, **k: None)
    monkeypatch.setattr(guidance.edgar, "find",
                        lambda acc: _Filing(["Item 2.02", "Item 9.01"], "…guidance…$12.95B…"))
    client = MagicMock()
    client.complete.return_value = GuidanceExtraction(has_guidance=True, figures=[_fig()])

    summary = guidance.run_once(_cfg(), db, client)
    assert summary["processed"] == 1 and summary["with_guidance"] == 1
    assert summary["trigger_eligible_figures"] == 1
    assert db.execute("SELECT COUNT(*) c FROM extracted_figures").fetchone()["c"] == 1
    # marked processed -> not reconsidered on a second run
    summary2 = guidance.run_once(_cfg(), db, client)
    assert summary2["considered"] == 0


def test_run_once_skips_non_earnings(db, monkeypatch):
    _seed_8k(db, "GOVT")
    monkeypatch.setattr(guidance.edgar, "set_identity", lambda *a, **k: None)
    monkeypatch.setattr(guidance.edgar, "find",
                        lambda acc: _Filing(["Item 5.02"], "officer change"))
    client = MagicMock()
    client.complete.side_effect = AssertionError("LLM should not be called for non-earnings 8-K")

    summary = guidance.run_once(_cfg(), db, client)
    assert summary["processed"] == 0
    row = db.execute("SELECT is_earnings, has_guidance FROM guidance_runs WHERE accession='GOVT'").fetchone()
    assert row["is_earnings"] == 0 and row["has_guidance"] == 0


# --- precision-pass fixes ---------------------------------------------------

def test_scope_is_required():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        GuidanceFigure(metric="revenue", period="FY2026", low=1.0, high=1.0,
                       unit="usd_billions", basis="gaap", is_reaffirmed=False,
                       confidence=0.9, context="x")  # no scope -> invalid


def test_scope_persisted(db):
    _seed_8k(db, "S1")
    guidance._store_figure(db, accession="S1", cik=CIK, fig=_fig(scope="segment"), min_conf=0.75)
    row = db.execute("SELECT scope FROM extracted_figures WHERE accession='S1'").fetchone()
    assert row["scope"] == "segment"


def test_run_once_suppresses_phantom_nulls(db, monkeypatch):
    _seed_8k(db, "EARN2")
    monkeypatch.setattr(guidance.edgar, "set_identity", lambda *a, **k: None)
    monkeypatch.setattr(guidance.edgar, "find",
                        lambda acc: _Filing(["Item 2.02", "Item 9.01"], "…guidance…"))
    client = MagicMock()
    # one real figure + one phantom-null (no number) -> only the real one stored
    client.complete.return_value = GuidanceExtraction(
        has_guidance=True,
        figures=[_fig(), _fig(metric="operating_income", low=None, high=None, context="GAAP op income each quarter")],
    )
    guidance.run_once(_cfg(), db, client)
    assert db.execute("SELECT COUNT(*) c FROM extracted_figures").fetchone()["c"] == 1
    assert db.execute(
        "SELECT COUNT(*) c FROM extracted_figures WHERE low IS NULL AND high IS NULL"
    ).fetchone()["c"] == 0
