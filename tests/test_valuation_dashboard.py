"""Classification coverage for the valuation dashboard.

The dashboard sorts every watchlist company into exactly one of three buckets:
valued (VALUED), monitored (`_monitored`), or not-DCF-modeled banks
(`_bank_names`). A company that matches none silently disappears from the page
(this happened to CVNA). These tests assert the buckets are exhaustive and
disjoint over the full 8-ticker watchlist.
"""
from __future__ import annotations

import sqlite3

import pytest

from redline.dashboard import valuation_app as va
from redline.storage.schema import init_full_schema

# The locked 8-ticker watchlist (ticker, sector) — matches config/watchlist.yaml.
_WATCHLIST = [
    ("0000000001", "PLTR", "tech"),
    ("0000000002", "NET", "tech"),
    ("0000000003", "SCHW", "financials"),
    ("0000000004", "KEY", "financials"),
    ("0000000005", "MRNA", "healthcare"),
    ("0000000006", "VRTX", "healthcare"),
    ("0000000007", "CVNA", "consumer"),
    ("0000000008", "ULTA", "consumer"),
]


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_full_schema(c)
    c.executemany(
        "INSERT INTO watchlist (cik, ticker, name, sector, added_at) "
        "VALUES (?, ?, ?, ?, 't')",
        [(cik, t, t, sector) for cik, t, sector in _WATCHLIST],
    )
    yield c
    c.close()


def test_every_watchlist_name_bucketed_exactly_once(conn):
    valued = set(va.VALUED)
    monitored = {r["ticker"] for r in va._monitored(conn)}
    banks = {b["ticker"] for b in va._bank_names(conn)}
    all_tickers = {t for _, t, _ in _WATCHLIST}

    # exhaustive: no company drops off the page
    assert valued | monitored | banks == all_tickers
    # disjoint: no company shown twice
    assert valued.isdisjoint(monitored)
    assert valued.isdisjoint(banks)
    assert monitored.isdisjoint(banks)


def test_cvna_is_monitored(conn):
    # The regression: CVNA (consumer, not valued) must appear as monitored.
    assert "CVNA" in {r["ticker"] for r in va._monitored(conn)}
