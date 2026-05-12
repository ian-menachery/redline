"""Tests for the filing fetcher + parser (Subsystem 2).

Mocks ``edgar.find`` so tests are deterministic and offline. Covers:
- 10-Q sections extracted via ``get_item_with_part(part, item)`` (part first!)
- 10-K sections extracted with the 10-K spec
- 8-K items list captured
- Form 4 transactions populated into form4_transactions
- Parse failure -> parse_failed + retry_count incremented
- 3rd failure -> failed_permanent
- Pending query: 'fetched' picked up; 'parse_failed' picked up only after
  RETRY_AFTER_HOURS
- Re-parse is idempotent (no duplicate transactions)
- 10b5-1 detector unit tests
"""
from __future__ import annotations

import datetime
import json
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
from redline.fetcher import (
    MAX_RETRIES,
    _detect_10b5_1,
    _is_issuer_placeholder,
    _normalize_company_name,
    run_once,
)
from redline.storage.db import connect
from redline.storage.schema import init_full_schema, seed_watchlist_from_yaml


# ---- fixtures -----------------------------------------------------------


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

    # Seed a minimal watchlist so FK constraints pass when inserting filings.
    watchlist_yaml = tmp_path / "watchlist.yaml"
    watchlist_yaml.write_text(
        '- cik: "0001321655"\n'
        "  ticker: PLTR\n"
        "  name: Palantir\n"
        "  sector: tech\n",
        encoding="utf-8",
    )
    seed_watchlist_from_yaml(conn, watchlist_yaml)
    yield conn
    conn.close()


def _seed_filing(conn, *, accession, filing_type, status="fetched",
                 retry_count=0, last_attempted=None,
                 cik="0001321655", filed_at="2026-05-01"):
    conn.execute(
        """
        INSERT INTO filings_seen (
            accession, cik, filing_type, filed_at, status,
            retry_count, last_attempted, discovered_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (accession, cik, filing_type, filed_at, status,
         retry_count, last_attempted, "2026-05-01T00:00:00Z"),
    )


# ---- mock helpers --------------------------------------------------------


def _mock_tenq(section_text: dict[tuple[str, str], str | None]):
    """Build a mock TenQ whose get_item_with_part returns from a dict
    keyed by (part, item)."""
    obj = MagicMock()
    obj.get_item_with_part = MagicMock(
        side_effect=lambda part, item: section_text.get((part, item))
    )
    return obj


def _mock_filing(obj, *, markdown: str = "raw markdown content"):
    """Build a mock EntityFiling whose .obj() returns the given inner mock."""
    filing = MagicMock()
    filing.obj.return_value = obj
    filing.markdown = MagicMock(return_value=markdown)
    filing.html = MagicMock(return_value=None)
    filing.text = MagicMock(return_value=None)
    return filing


def _mock_8k(items_list, items_text=None):
    """Mock an 8-K. ``items_text`` is dict[item_name -> body str].

    edgartools' ``chunks_for_item`` returns a list of chunks (per the
    Phase 0.5 spike probe), so the mock returns a single-element list.
    """
    obj = MagicMock()
    obj.items = items_list
    if items_text:
        chunked = MagicMock()
        chunked.chunks_for_item = MagicMock(
            side_effect=lambda name: ([items_text[name]] if items_text.get(name) else None)
        )
        obj.chunked_document = chunked
    else:
        obj.chunked_document = None
    obj.text = "8-K full text fallback"
    return obj


class _FootnotesObj:
    """Behaves like edgartools Footnotes: ``str(obj)`` returns the joined text."""
    def __init__(self, text: str):
        self._text = text
    def __str__(self) -> str:
        return self._text


def _mock_form4(*, insider_name="Alexander C. Karp", transactions=None,
                footnotes_text="", remarks_text=""):
    import pandas as pd

    obj = MagicMock()
    obj.insider_name = insider_name
    obj.footnotes = _FootnotesObj(footnotes_text)
    obj.remarks = remarks_text
    if transactions is None:
        transactions = []
    obj.to_dataframe = MagicMock(return_value=pd.DataFrame(transactions))
    return obj


# ---- 10b5-1 detector ----------------------------------------------------


def test_detect_10b5_1_finds_marker_and_date_iso():
    is_10b5_1, plan_date = _detect_10b5_1(
        "Sale pursuant to a Rule 10b5-1 trading plan adopted on 2023-12-12."
    )
    assert is_10b5_1 is True
    assert plan_date == "2023-12-12"


def test_detect_10b5_1_finds_marker_and_date_long_form():
    is_10b5_1, plan_date = _detect_10b5_1(
        "Pursuant to a preexisting Rule 10b5-1 trading plan, entered into on "
        "December 12, 2023."
    )
    assert is_10b5_1 is True
    assert plan_date == "2023-12-12"


def test_detect_10b5_1_marker_without_date():
    is_10b5_1, plan_date = _detect_10b5_1(
        "Sale under a 10b5-1 plan (date not specified in footnote)."
    )
    assert is_10b5_1 is True
    assert plan_date is None


def test_detect_10b5_1_no_marker():
    is_10b5_1, plan_date = _detect_10b5_1("Open market sale.")
    assert is_10b5_1 is False
    assert plan_date is None


def test_detect_10b5_1_empty_text():
    is_10b5_1, plan_date = _detect_10b5_1("")
    assert is_10b5_1 is None
    assert plan_date is None


# ---- 10-Q -----------------------------------------------------------------


def test_run_once_10q_extracts_sections(db):
    _seed_filing(db, accession="acc-10q", filing_type="10-Q")
    obj = _mock_tenq({
        ("Part I", "Item 2"): "MD&A body text",
        ("Part II", "Item 1A"): "Risk Factors body text",
        ("Part II", "Item 1"): "",  # empty Legal Proceedings (common)
        ("Part I", "Item 3"): "QDMR body text",
    })
    filing = _mock_filing(obj, markdown="10-Q markdown")
    with patch("redline.fetcher.edgar") as mock_edgar:
        mock_edgar.find.return_value = filing
        summary = run_once(_config(), db)

    assert summary["parsed"] == 1
    assert summary["failed"] == 0

    # filings_seen transitioned
    status = db.execute(
        "SELECT status, failure_reason FROM filings_seen WHERE accession = ?",
        ("acc-10q",),
    ).fetchone()
    assert status["status"] == "parsed"
    assert status["failure_reason"] is None

    # filings_content row + sections
    row = db.execute(
        "SELECT sections, is_empty, parser_version FROM filings_content WHERE accession = ?",
        ("acc-10q",),
    ).fetchone()
    sections = json.loads(row["sections"])
    is_empty = json.loads(row["is_empty"])
    assert sections["mdna"] == "MD&A body text"
    assert sections["risk_factors"] == "Risk Factors body text"
    assert sections["legal"] is None
    assert sections["qdmr"] == "QDMR body text"
    assert is_empty == {"mdna": False, "risk_factors": False, "legal": True, "qdmr": False}


# ---- 10-K -----------------------------------------------------------------


def test_run_once_10k_uses_10k_spec(db):
    _seed_filing(db, accession="acc-10k", filing_type="10-K")
    obj = _mock_tenq({
        ("Part II", "Item 7"): "10-K MD&A",
        ("Part I", "Item 1A"): "10-K Risk Factors",
        ("Part I", "Item 3"): "10-K Legal",
        ("Part II", "Item 7A"): "10-K QDMR",
    })
    filing = _mock_filing(obj)
    with patch("redline.fetcher.edgar") as mock_edgar:
        mock_edgar.find.return_value = filing
        run_once(_config(), db)

    sections = json.loads(db.execute(
        "SELECT sections FROM filings_content WHERE accession = ?", ("acc-10k",)
    ).fetchone()["sections"])
    assert sections["mdna"] == "10-K MD&A"
    assert sections["risk_factors"] == "10-K Risk Factors"
    assert sections["legal"] == "10-K Legal"
    assert sections["qdmr"] == "10-K QDMR"


# ---- 8-K ------------------------------------------------------------------


def test_run_once_8k_extracts_items_list(db):
    _seed_filing(db, accession="acc-8k", filing_type="8-K")
    obj = _mock_8k(
        items_list=["Item 1.01", "Item 9.01"],
        items_text={"Item 1.01": "Material agreement body", "Item 9.01": "Exhibit list"},
    )
    filing = _mock_filing(obj)
    with patch("redline.fetcher.edgar") as mock_edgar:
        mock_edgar.find.return_value = filing
        run_once(_config(), db)

    sections = json.loads(db.execute(
        "SELECT sections FROM filings_content WHERE accession = ?", ("acc-8k",)
    ).fetchone()["sections"])
    assert sections["items_list"] == ["Item 1.01", "Item 9.01"]
    assert sections["items"]["Item 1.01"] == "Material agreement body"
    assert sections["items"]["Item 9.01"] == "Exhibit list"


# ---- Form 4 ---------------------------------------------------------------


def test_run_once_form4_populates_transactions(db):
    _seed_filing(db, accession="acc-f4", filing_type="4")
    obj = _mock_form4(
        insider_name="Alexander C. Karp",
        transactions=[
            {"Code": "S", "Shares": 100000, "Price": 60.0,
             "Date": "2026-04-30", "Insider": "Alexander C. Karp",
             "Description": "Open Market Sale"},
            {"Code": "S", "Shares": 50000, "Price": 60.5,
             "Date": "2026-04-30", "Insider": "Alexander C. Karp",
             "Description": "Open Market Sale"},
        ],
        footnotes_text="Pursuant to a Rule 10b5-1 trading plan entered into on December 12, 2023.",
    )
    filing = _mock_filing(obj)
    with patch("redline.fetcher.edgar") as mock_edgar:
        mock_edgar.find.return_value = filing
        run_once(_config(), db)

    # filings_content has the structured summary
    row = db.execute(
        "SELECT sections FROM filings_content WHERE accession = ?", ("acc-f4",)
    ).fetchone()
    sections = json.loads(row["sections"])
    assert sections["insider_name"] == "Alexander C. Karp"
    assert sections["transaction_count"] == 2
    assert sections["is_10b5_1"] is True
    assert sections["plan_adopted_date"] == "2023-12-12"

    # form4_transactions has the rows
    txs = db.execute(
        "SELECT code, shares, price, is_10b5_1, plan_adopted_date "
        "FROM form4_transactions WHERE accession = ? ORDER BY shares DESC",
        ("acc-f4",),
    ).fetchall()
    assert len(txs) == 2
    assert txs[0]["code"] == "S"
    assert txs[0]["shares"] == 100000
    assert txs[0]["price"] == 60.0
    assert txs[0]["is_10b5_1"] == 1
    assert txs[0]["plan_adopted_date"] == "2023-12-12"


# ---- issuer-name placeholder filter --------------------------------------


def test_normalize_company_name_strips_suffix_and_punct():
    assert _normalize_company_name("Palantir Technologies Inc.") == "palantir technologies"
    assert _normalize_company_name("PALANTIR TECHNOLOGIES, INC") == "palantir technologies"
    assert _normalize_company_name("Carvana Co.") == "carvana"
    assert _normalize_company_name("Vertex Pharmaceuticals Inc") == "vertex pharmaceuticals"


def test_is_issuer_placeholder_matches_variants():
    issuer = "Palantir Technologies Inc."
    assert _is_issuer_placeholder("Palantir Technologies Inc.", issuer) is True
    assert _is_issuer_placeholder("PALANTIR TECHNOLOGIES, INC", issuer) is True
    assert _is_issuer_placeholder("Palantir Technologies, Inc.", issuer) is True


def test_is_issuer_placeholder_keeps_real_insiders():
    issuer = "Palantir Technologies Inc."
    assert _is_issuer_placeholder("Alexander C. Karp", issuer) is False
    assert _is_issuer_placeholder("Karp Alexander C", issuer) is False
    # A different corporate insider (e.g. 10% owner) is real signal, not a placeholder.
    assert _is_issuer_placeholder("Vanguard Group Inc.", issuer) is False


def test_run_once_form4_skips_issuer_name_placeholders(db):
    """Form 4 rows whose Insider == issuer name are dropped at ingest."""
    # The default fixture seeds Palantir with name='Palantir'. To exercise the
    # production-realistic path, replace it with the full company name.
    db.execute(
        "UPDATE watchlist SET name = ? WHERE cik = ?",
        ("Palantir Technologies Inc.", "0001321655"),
    )
    _seed_filing(db, accession="acc-f4-mixed", filing_type="4")
    obj = _mock_form4(
        insider_name="Palantir Technologies Inc.",
        transactions=[
            # Real insider row — keep.
            {"Code": "S", "Shares": 100000, "Price": 60.0,
             "Date": "2024-11-15", "Insider": "Karp Alexander C",
             "Description": "Open Market Sale"},
            # Issuer-name placeholder row — skip (NOTES.md §11).
            {"Code": "S", "Shares": 1, "Price": 0.10,
             "Date": "2024-11-15", "Insider": "Palantir Technologies Inc.",
             "Description": ""},
            # Case + punct variant of the issuer — skip.
            {"Code": "S", "Shares": 1, "Price": 0.10,
             "Date": "2024-11-15", "Insider": "PALANTIR TECHNOLOGIES, INC",
             "Description": ""},
        ],
    )
    filing = _mock_filing(obj)
    with patch("redline.fetcher.edgar") as mock_edgar:
        mock_edgar.find.return_value = filing
        run_once(_config(), db)

    txs = db.execute(
        "SELECT insider_name, shares FROM form4_transactions WHERE accession = ?",
        ("acc-f4-mixed",),
    ).fetchall()
    assert len(txs) == 1
    assert txs[0]["insider_name"] == "Karp Alexander C"
    assert txs[0]["shares"] == 100000


def test_form4_reparse_replaces_transactions(db):
    """Re-parsing the same Form 4 should not duplicate transactions."""
    _seed_filing(db, accession="acc-f4", filing_type="4")
    transactions = [
        {"Code": "S", "Shares": 100000, "Price": 60.0,
         "Date": "2026-04-30", "Insider": "K", "Description": "x"},
    ]
    obj = _mock_form4(transactions=transactions)
    filing = _mock_filing(obj)
    with patch("redline.fetcher.edgar") as mock_edgar:
        mock_edgar.find.return_value = filing
        run_once(_config(), db)
        # Reset status and re-run
        db.execute("UPDATE filings_seen SET status='fetched' WHERE accession=?", ("acc-f4",))
        run_once(_config(), db)

    count = db.execute(
        "SELECT COUNT(*) FROM form4_transactions WHERE accession = ?", ("acc-f4",)
    ).fetchone()[0]
    assert count == 1


# ---- failure semantics ----------------------------------------------------


def test_parse_failure_marks_parse_failed(db):
    _seed_filing(db, accession="acc-bad", filing_type="10-Q")
    with patch("redline.fetcher.edgar") as mock_edgar:
        mock_edgar.find.side_effect = RuntimeError("network blip")
        summary = run_once(_config(), db)

    assert summary["failed"] == 1
    assert summary["parsed"] == 0
    row = db.execute(
        "SELECT status, retry_count, failure_reason FROM filings_seen WHERE accession = ?",
        ("acc-bad",),
    ).fetchone()
    assert row["status"] == "parse_failed"
    assert row["retry_count"] == 1
    assert "RuntimeError" in row["failure_reason"]


def test_failed_permanent_after_max_retries(db):
    _seed_filing(db, accession="acc-doomed", filing_type="10-Q",
                 status="parse_failed", retry_count=MAX_RETRIES - 1,
                 last_attempted=None)
    with patch("redline.fetcher.edgar") as mock_edgar:
        mock_edgar.find.side_effect = RuntimeError("still broken")
        run_once(_config(), db)

    row = db.execute(
        "SELECT status, retry_count FROM filings_seen WHERE accession = ?",
        ("acc-doomed",),
    ).fetchone()
    assert row["status"] == "failed_permanent"
    assert row["retry_count"] == MAX_RETRIES


def test_recent_parse_failed_not_picked_up(db):
    """parse_failed rows attempted within the retry window are skipped."""
    just_now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _seed_filing(db, accession="acc-recent-fail", filing_type="10-Q",
                 status="parse_failed", retry_count=1, last_attempted=just_now)
    with patch("redline.fetcher.edgar") as mock_edgar:
        mock_edgar.find.side_effect = AssertionError("should NOT be called")
        summary = run_once(_config(), db)

    mock_edgar.find.assert_not_called()
    assert summary["considered"] == 0


def test_old_parse_failed_picked_up(db):
    """parse_failed rows attempted longer than RETRY_AFTER_HOURS ago retry."""
    old = (datetime.datetime.now(datetime.timezone.utc)
           - datetime.timedelta(hours=2)).isoformat()
    _seed_filing(db, accession="acc-old-fail", filing_type="10-Q",
                 status="parse_failed", retry_count=1, last_attempted=old)

    obj = _mock_tenq({
        ("Part I", "Item 2"): "MD&A",
        ("Part II", "Item 1A"): "RF",
        ("Part II", "Item 1"): None,
        ("Part I", "Item 3"): "QDMR",
    })
    with patch("redline.fetcher.edgar") as mock_edgar:
        mock_edgar.find.return_value = _mock_filing(obj)
        summary = run_once(_config(), db)

    assert summary["parsed"] == 1
    row = db.execute(
        "SELECT status, failure_reason FROM filings_seen WHERE accession = ?",
        ("acc-old-fail",),
    ).fetchone()
    assert row["status"] == "parsed"
    assert row["failure_reason"] is None


# ---- UA + identity --------------------------------------------------------


def test_set_identity_called_with_config_ua(db):
    _seed_filing(db, accession="acc-x", filing_type="10-Q")
    obj = _mock_tenq({})
    with patch("redline.fetcher.edgar") as mock_edgar:
        mock_edgar.find.return_value = _mock_filing(obj)
        run_once(_config(), db)
    mock_edgar.set_identity.assert_called_with("Test (test@test)")
