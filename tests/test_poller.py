"""Tests for the EDGAR poller (Subsystem 1).

Mocks ``edgar.Company`` so tests are deterministic and offline. Covers:
- First-run behavior: only the most recent filing per CIK is inserted (D2)
- Steady state: net-new filings inserted, existing ones not duplicated
- Idempotent re-run: same call twice -> same row count
- Per-ticker error containment: one bad ticker doesn't crash the cycle
- Watchlist seeding is idempotent
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from redline.config import (
    AnthropicConfig,
    CorrelatorConfig,
    DiffConfig,
    LLMConfig,
    OpenAIConfig,
    PollerConfig,
    RedlineConfig,
    StorageConfig,
)
from redline.poller import run_once
from redline.storage.db import connect
from redline.storage.schema import init_full_schema, seed_watchlist_from_yaml


# ----- fixtures ------------------------------------------------------------


def _config() -> RedlineConfig:
    return RedlineConfig(
        llm=LLMConfig(
            openai=OpenAIConfig(cheap_model="x", quality_model="y"),
            anthropic=AnthropicConfig(cheap_model="x", quality_model="y"),
        ),
        diff=DiffConfig(),
        correlator=CorrelatorConfig(),
        poller=PollerConfig(edgar_user_agent="Test (test@test)"),
        storage=StorageConfig(db_path=":memory:"),
    )


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_full_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def watchlist_yaml(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        '- cik: "0001321655"\n'
        "  ticker: PLTR\n"
        "  name: Palantir Technologies Inc.\n"
        "  sector: tech\n"
        '- cik: "0000875320"\n'
        "  ticker: VRTX\n"
        "  name: Vertex Pharmaceuticals Inc\n"
        "  sector: healthcare\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def seeded_db(db, watchlist_yaml):
    seed_watchlist_from_yaml(db, watchlist_yaml)
    return db


# ----- mock filing helpers ------------------------------------------------


def _filing(accession, form, filed_date, period=None):
    return SimpleNamespace(
        accession_no=accession,
        form=form,
        filing_date=filed_date,
        period_of_report=period,
    )


def _filings_collection(items):
    """Mock the edgartools ``EntityFilings`` shape: iterable + ``.latest(n)``."""
    mock = MagicMock()
    mock.__iter__ = lambda self: iter(items)
    mock.__len__ = lambda self: len(items)
    mock.latest = lambda n: items[:n]
    return mock


def _patch_companies(map_: dict):
    """Patch ``edgar.Company(ticker)`` to return the configured per-ticker mock."""

    def _factory(ticker):
        if ticker not in map_:
            raise KeyError(f"unexpected ticker {ticker!r}")
        return map_[ticker]

    p = patch("redline.poller.edgar")
    mock_edgar = p.start()
    mock_edgar.Company.side_effect = _factory
    mock_edgar.set_identity = MagicMock()
    return p, mock_edgar


# ----- seeding -------------------------------------------------------------


def test_seed_watchlist_idempotent(db, watchlist_yaml):
    first = seed_watchlist_from_yaml(db, watchlist_yaml)
    second = seed_watchlist_from_yaml(db, watchlist_yaml)
    assert first == 2
    assert second == 0
    rows = db.execute("SELECT cik FROM watchlist ORDER BY cik").fetchall()
    assert [r["cik"] for r in rows] == ["0000875320", "0001321655"]


# ----- first run -----------------------------------------------------------


def test_first_run_inserts_latest_one_per_ticker(seeded_db):
    """D2: first run for each CIK inserts only the most-recent filing."""
    pltr_filings = [
        _filing("acc-1", "10-Q", "2026-05-01"),
        _filing("acc-2", "10-Q", "2026-02-01"),
        _filing("acc-3", "10-K", "2026-01-15"),
    ]
    vrtx_filings = [
        _filing("acc-4", "4", "2026-04-30"),
        _filing("acc-5", "4", "2026-04-29"),
    ]

    p, _ = _patch_companies({
        "PLTR": MagicMock(get_filings=MagicMock(return_value=_filings_collection(pltr_filings))),
        "VRTX": MagicMock(get_filings=MagicMock(return_value=_filings_collection(vrtx_filings))),
    })
    try:
        summary = run_once(_config(), seeded_db)
    finally:
        p.stop()

    assert summary["inserted_total"] == 2
    assert summary["tickers_polled"] == 2
    assert summary["tickers_errored"] == 0

    rows = seeded_db.execute(
        "SELECT cik, accession, filing_type FROM filings_seen ORDER BY cik, accession"
    ).fetchall()
    assert [(r["cik"], r["accession"]) for r in rows] == [
        ("0000875320", "acc-4"),
        ("0001321655", "acc-1"),
    ]
    # Every inserted row has status='fetched'
    statuses = seeded_db.execute("SELECT DISTINCT status FROM filings_seen").fetchall()
    assert {r["status"] for r in statuses} == {"fetched"}


# ----- steady state --------------------------------------------------------


def test_steady_state_inserts_only_new(seeded_db):
    """Once a CIK has any row, the next cycle picks up net-new accessions only."""
    # Pre-seed an existing filing for PLTR
    seeded_db.execute(
        """
        INSERT INTO filings_seen (
            accession, cik, filing_type, filed_at, status, retry_count, discovered_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("existing-1", "0001321655", "10-Q", "2026-04-01", "parsed", 0, "2026-04-01T00:00:00Z"),
    )

    pltr_filings = [
        _filing("new-1", "10-Q", "2026-05-01"),
        _filing("existing-1", "10-Q", "2026-04-01"),  # already known
        _filing("new-2", "4", "2026-04-15"),
    ]

    p, _ = _patch_companies({
        "PLTR": MagicMock(get_filings=MagicMock(return_value=_filings_collection(pltr_filings))),
        "VRTX": MagicMock(get_filings=MagicMock(return_value=_filings_collection([]))),
    })
    try:
        summary = run_once(_config(), seeded_db)
    finally:
        p.stop()

    assert summary["inserted_total"] == 2

    rows = seeded_db.execute(
        "SELECT accession FROM filings_seen WHERE cik = ? ORDER BY accession",
        ("0001321655",),
    ).fetchall()
    accessions = [r["accession"] for r in rows]
    assert sorted(accessions) == ["existing-1", "new-1", "new-2"]


# ----- idempotency ---------------------------------------------------------


def test_re_run_is_idempotent(seeded_db):
    """Running run_once twice doesn't duplicate rows."""
    pltr_filings = [_filing("acc-1", "10-Q", "2026-05-01")]

    pltr_company = MagicMock(get_filings=MagicMock(return_value=_filings_collection(pltr_filings)))
    vrtx_company = MagicMock(get_filings=MagicMock(return_value=_filings_collection([])))
    p, _ = _patch_companies({"PLTR": pltr_company, "VRTX": vrtx_company})
    try:
        run_once(_config(), seeded_db)
        run_once(_config(), seeded_db)
    finally:
        p.stop()

    count = seeded_db.execute(
        "SELECT COUNT(*) FROM filings_seen WHERE accession = ?", ("acc-1",)
    ).fetchone()[0]
    assert count == 1


# ----- error containment ---------------------------------------------------


def test_per_ticker_error_does_not_crash_cycle(seeded_db):
    """A failing ticker is logged and skipped; other tickers complete."""
    pltr_filings = [_filing("acc-1", "10-Q", "2026-05-01")]

    pltr_company = MagicMock(get_filings=MagicMock(return_value=_filings_collection(pltr_filings)))
    vrtx_company = MagicMock(get_filings=MagicMock(side_effect=RuntimeError("network blip")))
    p, _ = _patch_companies({"PLTR": pltr_company, "VRTX": vrtx_company})
    try:
        summary = run_once(_config(), seeded_db)
    finally:
        p.stop()

    assert summary["tickers_polled"] == 1
    assert summary["tickers_errored"] == 1
    rows = seeded_db.execute("SELECT cik FROM filings_seen").fetchall()
    assert [r["cik"] for r in rows] == ["0001321655"]
    # The errored ticker's entry carries an error field
    errored = [t for t in summary["per_ticker"] if "error" in t]
    assert errored and errored[0]["ticker"] == "VRTX"
    assert "RuntimeError" in errored[0]["error"]


# ----- empty results -------------------------------------------------------


def test_empty_filings_results_insert_nothing(seeded_db):
    p, _ = _patch_companies({
        "PLTR": MagicMock(get_filings=MagicMock(return_value=_filings_collection([]))),
        "VRTX": MagicMock(get_filings=MagicMock(return_value=_filings_collection([]))),
    })
    try:
        summary = run_once(_config(), seeded_db)
    finally:
        p.stop()

    assert summary["inserted_total"] == 0
    assert summary["tickers_errored"] == 0
    assert seeded_db.execute("SELECT COUNT(*) FROM filings_seen").fetchone()[0] == 0


# ----- user-agent setup ----------------------------------------------------


def test_set_identity_called_with_config_ua(seeded_db):
    """The poller must set the EDGAR identity from config (NOTES §4)."""
    p, mock_edgar = _patch_companies({
        "PLTR": MagicMock(get_filings=MagicMock(return_value=_filings_collection([]))),
        "VRTX": MagicMock(get_filings=MagicMock(return_value=_filings_collection([]))),
    })
    try:
        run_once(_config(), seeded_db)
    finally:
        p.stop()

    mock_edgar.set_identity.assert_called_with("Test (test@test)")
