"""Tests for Rule R guidance-eval registration (``guidance_registration``).

Rule R must be a pure, deterministic function of committed DB state: no network,
2-most-recent-per-name, tie-break accession ascending, undershoot tolerated,
``previously_observed`` computed from artifacts (never a company name).
"""
from __future__ import annotations

import sqlite3

from redline.storage.schema import init_full_schema
from redline.valuation.guidance_registration import (
    manifest_dicts,
    record_qualification,
    select_registration,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_full_schema(conn)
    return conn


def _watch(conn, cik, ticker, sector="tech"):
    conn.execute(
        "INSERT OR IGNORE INTO watchlist (cik, ticker, name, sector, added_at) "
        "VALUES (?,?,?,?,?)",
        (cik, ticker, ticker, sector, "t"),
    )


def _filing(conn, accession, cik, filed_at, *, form="8-K", is_earnings=True,
            has_ex99=True):
    conn.execute(
        "INSERT OR IGNORE INTO filings_seen (accession, cik, filing_type, filed_at, "
        "status, retry_count, discovered_at) VALUES (?,?,?,?,?,?,?)",
        (accession, cik, "8-K", filed_at, "fetched", 0, "t"),
    )
    record_qualification(conn, accession=accession, form=form,
                         is_earnings=is_earnings, has_ex99=has_ex99)


def test_two_most_recent_per_company():
    conn = _conn()
    _watch(conn, "1", "AAA")
    _filing(conn, "a-2025-11", "1", "2025-11-03")
    _filing(conn, "a-2026-02", "1", "2026-02-10")
    _filing(conn, "a-2026-05", "1", "2026-05-04")
    sel = select_registration(conn)
    assert [e.accession for e in sel] == ["a-2026-05", "a-2026-02"]  # 2 most recent


def test_tie_break_accession_ascending():
    conn = _conn()
    _watch(conn, "1", "AAA")
    _filing(conn, "z-same", "1", "2026-05-04")
    _filing(conn, "a-same", "1", "2026-05-04")
    _filing(conn, "old", "1", "2025-01-01")
    sel = select_registration(conn)
    # same filed_at -> accession asc picks 'a-same' before 'z-same'.
    assert [e.accession for e in sel] == ["a-same", "z-same"]


def test_undershoot_takes_what_exists():
    conn = _conn()
    _watch(conn, "1", "AAA")
    _filing(conn, "only-one", "1", "2026-05-04")
    sel = select_registration(conn)
    assert [e.accession for e in sel] == ["only-one"]


def test_non_qualifying_excluded():
    conn = _conn()
    _watch(conn, "1", "AAA")
    _filing(conn, "no-item", "1", "2026-05-04", is_earnings=False)
    _filing(conn, "no-ex99", "1", "2026-04-01", has_ex99=False)
    _filing(conn, "good", "1", "2026-03-01")
    sel = select_registration(conn)
    assert [e.accession for e in sel] == ["good"]


def test_amendment_excluded():
    # an 8-K/A re-states an existing earnings event and must not enter the panel,
    # even with item 2.02 + EX-99.x present.
    conn = _conn()
    _watch(conn, "1", "AAA")
    _filing(conn, "orig", "1", "2026-05-07", form="8-K")
    _filing(conn, "amend", "1", "2026-05-07", form="8-K/A")
    _filing(conn, "prior", "1", "2026-02-10", form="8-K")
    sel = [e.accession for e in select_registration(conn)]
    assert sel == ["orig", "prior"]  # amendment dropped, two distinct quarters


def test_excluded_sector_dropped():
    conn = _conn()
    _watch(conn, "1", "BANK", sector="financials")
    _filing(conn, "bank-8k", "1", "2026-05-04")
    assert select_registration(conn) == []


def test_previously_observed_computed_from_artifacts():
    conn = _conn()
    _watch(conn, "1", "AAA")
    _filing(conn, "seen", "1", "2026-05-04")
    _filing(conn, "unseen", "1", "2026-02-01")
    # an extraction artifact marks 'seen' as previously_observed.
    conn.execute(
        "INSERT INTO extracted_figures (accession, cik, metric, scope, period, low, "
        "high, unit, basis, is_reaffirmed, confidence, review_status, prompt_version, "
        "parser_version, extracted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("seen", "1", "revenue", "total", "FY2026", 1.0, 2.0, "usd_billions",
         "unspecified", 0, 0.9, "trigger_eligible", "v1", "v1", "t"),
    )
    by_acc = {e.accession: e.previously_observed for e in select_registration(conn)}
    assert by_acc["seen"] is True
    assert by_acc["unseen"] is False


def test_previously_observed_from_guidance_runs():
    conn = _conn()
    _watch(conn, "1", "AAA")
    _filing(conn, "ran", "1", "2026-05-04")
    conn.execute(
        "INSERT INTO guidance_runs (accession, ran_at, is_earnings, has_guidance, "
        "figures_found) VALUES (?,?,?,?,?)",
        ("ran", "t", 1, 0, 0),
    )
    sel = select_registration(conn)
    assert sel[0].previously_observed is True


def test_selection_is_deterministic():
    conn = _conn()
    _watch(conn, "1", "AAA")
    _watch(conn, "2", "BBB")
    for acc, cik, d in [("a1", "1", "2026-05-04"), ("a2", "1", "2026-02-01"),
                        ("b1", "2", "2026-04-01"), ("b2", "2", "2026-01-01")]:
        _filing(conn, acc, cik, d)
    first = [(e.ticker, e.accession) for e in select_registration(conn)]
    second = [(e.ticker, e.accession) for e in select_registration(conn)]
    assert first == second  # pure DB read, no state mutation
    assert first == [("AAA", "a1"), ("AAA", "a2"), ("BBB", "b1"), ("BBB", "b2")]


def test_as_of_freezes_panel_against_later_filings():
    conn = _conn()
    _watch(conn, "1", "AAA")
    _filing(conn, "old-1", "1", "2026-02-01")
    _filing(conn, "old-2", "1", "2026-03-01")
    # a newer filing that lands AFTER the lock date must not change the frozen set.
    _filing(conn, "after-lock", "1", "2026-09-01")
    frozen = [e.accession for e in select_registration(conn, as_of="2026-07-30")]
    assert frozen == ["old-2", "old-1"]  # 'after-lock' excluded
    # without as_of the newest wins.
    now = [e.accession for e in select_registration(conn)]
    assert now[0] == "after-lock"


def test_manifest_dicts_shape():
    conn = _conn()
    _watch(conn, "1", "AAA")
    _filing(conn, "a1", "1", "2026-05-04")
    m = manifest_dicts(select_registration(conn), locked_at="2026-07-30T00:00:00Z")
    assert m == [{
        "ticker": "AAA", "accession": "a1", "filed_at": "2026-05-04",
        "locked_at": "2026-07-30T00:00:00Z", "previously_observed": False,
    }]
