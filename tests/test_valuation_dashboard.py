"""Classification coverage for the valuation dashboard.

The dashboard sorts every watchlist company into exactly one of three buckets:
valued (VALUED), monitored (`_monitored`), or not-DCF-modeled banks
(`_bank_names`). A company that matches none silently disappears from the page
(this happened to CVNA). These tests assert the buckets are exhaustive and
disjoint over the full 8-ticker watchlist.
"""
from __future__ import annotations

import json
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


# ---- "how this was modeled" detail ---------------------------------------


def _row_with_projection() -> dict:
    assumptions = {
        "base_revenue": 11_000_000_000.0, "net_debt": 437_500_000.0,
        "shares_diluted": 256_000_000.0, "fiscal_year": 2025,
        "wacc": 0.06, "terminal_growth": 0.025, "tax_rate": 0.21,
        "revenue_growth": [0.08, 0.07], "operating_margin": 0.30,
        "is_placeholder": False, "low_confidence_note": None,
        "projection": [
            {"year": 1, "revenue_growth": 0.08, "revenue": 1.1e10,
             "ebit": 3.3e9, "nopat": 2.6e9, "fcf": 3.1e9, "pv": 2.9e9},
            {"year": 2, "revenue_growth": 0.07, "revenue": 1.2e10,
             "ebit": 3.5e9, "nopat": 2.8e9, "fcf": 3.4e9, "pv": 3.0e9},
        ],
        "base_result": {
            "per_share": 483.0, "enterprise_value": 1.3e11, "equity_value": 1.25e11,
            "pv_explicit": 5.9e9, "pv_terminal": 1.24e11, "terminal_value_fraction": 0.95,
        },
    }
    return {
        "wacc": 0.06, "terminal_growth": 0.025,
        "assumptions_json": json.dumps(assumptions),
        "sensitivity_json": json.dumps({
            "wacc": [[0.04, 620.0], [0.06, 483.0], [0.08, 360.0]],
            "revenue_growth_shift": [[-0.02, 410.0], [0.0, 483.0], [0.02, 560.0]],
        }),
    }


def test_model_detail_parses_baked_projection():
    d = va._model_detail(_row_with_projection())
    assert d is not None
    assert d["assumptions"]["horizon"] == 2
    assert d["assumptions"]["wacc"] == 0.06
    assert len(d["projection"]) == 2
    assert d["base_result"]["per_share"] == 483.0
    assert len(d["sensitivity"]["wacc"]) == 3


def test_model_detail_none_when_projection_missing():
    # A snapshot predating the baked projection must degrade gracefully, not crash.
    assert va._model_detail({"assumptions_json": json.dumps({"wacc": 0.06})}) is None
    assert va._model_detail({"assumptions_json": None}) is None
    assert va._model_detail({}) is None


def test_placeholder_guardrail_detection():
    # The ILLUSTRATIVE banner fires only when the fed assumptions are placeholders.
    assert va._assumptions_are_placeholder(
        {"assumptions_json": json.dumps({"is_placeholder": True})}) is True
    assert va._assumptions_are_placeholder(_row_with_projection()) is False  # is_placeholder False
    assert va._assumptions_are_placeholder({"assumptions_json": None}) is False
    assert va._assumptions_are_placeholder({}) is False
