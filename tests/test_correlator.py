"""Tests for the insider-trading correlator (Subsystem 4)."""
from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock

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
from redline.correlator.analyzer import _sweep_form4_to_analyzed, run_once
from redline.correlator.signals import (
    MIN_BASELINE_TRADES,
    Trade,
    cluster_signal,
    direction_flip_signal,
    load_insider_baseline,
    load_trades_in_window,
    volume_signal,
)
from redline.llm.schemas import CorrelatorVerdict
from redline.storage.db import connect
from redline.storage.schema import init_full_schema, seed_watchlist_from_yaml


# ---- fixtures -------------------------------------------------------------


def _config(window_days: int = 14) -> RedlineConfig:
    return RedlineConfig(
        llm=LLMConfig(
            openai=OpenAIConfig(cheap_model="x", quality_model="y"),
            anthropic=AnthropicConfig(cheap_model="x", quality_model="y"),
        ),
        diff=DiffConfig(),
        correlator=CorrelatorConfig(window_days=window_days),
        poller=PollerConfig(edgar_user_agent="Test (t@t)"),
        storage=StorageConfig(db_path=":memory:"),
    )


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "test.db")
    init_full_schema(conn)
    wl = tmp_path / "watchlist.yaml"
    wl.write_text(
        '- cik: "0001321655"\n'
        "  ticker: PLTR\n"
        "  name: Palantir\n"
        "  sector: tech\n",
        encoding="utf-8",
    )
    seed_watchlist_from_yaml(conn, wl)
    yield conn
    conn.close()


def _seed_filing(
    conn, *, accession, filing_type, filed_at,
    cik="0001321655", status="parsed",
):
    conn.execute(
        """
        INSERT INTO filings_seen (
            accession, cik, filing_type, filed_at, status,
            retry_count, discovered_at
        ) VALUES (?, ?, ?, ?, ?, 0, ?)
        """,
        (accession, cik, filing_type, filed_at, status, "2026-05-01T00:00:00Z"),
    )


def _seed_form4_filing(conn, *, accession, filed_at, cik="0001321655"):
    """Form 4s need a filings_seen row before form4_transactions can FK-reference them."""
    _seed_filing(conn, accession=accession, filing_type="4",
                 filed_at=filed_at, cik=cik, status="parsed")


def _seed_tx(
    conn, *, accession, insider_name, trade_date, code, shares,
    price=None, is_10b5_1=None, cik="0001321655",
):
    conn.execute(
        """
        INSERT INTO form4_transactions (
            accession, cik, insider_name, trade_date, code,
            shares, price, is_10b5_1
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (accession, cik, insider_name, trade_date, code, shares, price, is_10b5_1),
    )


# ---- Trade.is_discretionary ----------------------------------------------


def test_trade_is_discretionary_filters_codes():
    assert Trade("X", "2026-05-01", "P", 100, 10.0, None).is_discretionary
    assert Trade("X", "2026-05-01", "S", 100, 10.0, None).is_discretionary
    for code in ("A", "M", "F", "D", "G", "C"):
        assert not Trade("X", "2026-05-01", code, 100, 10.0, None).is_discretionary


def test_trade_is_discretionary_filters_10b5_1():
    assert Trade("X", "2026-05-01", "S", 100, 10.0, 1).is_discretionary is False
    assert Trade("X", "2026-05-01", "S", 100, 10.0, 0).is_discretionary is True
    assert Trade("X", "2026-05-01", "S", 100, 10.0, None).is_discretionary is True


# ---- cluster_signal -------------------------------------------------------


def test_cluster_signal_empty():
    res = cluster_signal([])
    assert res["max_cluster_size"] == 0
    assert res["score"] == 0


def test_cluster_signal_single_insider():
    res = cluster_signal([
        Trade("A", "2026-05-01", "S", 100, 10.0, None),
    ])
    assert res["max_cluster_size"] == 1
    assert res["score"] == pytest.approx(1 / 3)


def test_cluster_signal_three_same_direction():
    res = cluster_signal([
        Trade(f"insider_{i}", "2026-05-01", "S", 100, 10.0, None)
        for i in range(3)
    ])
    assert res["max_cluster_size"] == 3
    assert res["score"] == 1.0
    assert set(res["sellers"]) == {"insider_0", "insider_1", "insider_2"}


def test_cluster_signal_excludes_10b5_1_and_admin():
    """10b5-1 trades and admin codes (A/F) should not count in the cluster."""
    res = cluster_signal([
        Trade("A", "2026-05-01", "S", 100, 10.0, 1),  # plan-driven
        Trade("B", "2026-05-01", "F", 50, None, None),  # tax withholding
        Trade("C", "2026-05-01", "S", 200, 12.0, None),  # discretionary
    ])
    assert res["max_cluster_size"] == 1
    assert res["sellers"] == ["C"]


# ---- volume_signal --------------------------------------------------------


def test_volume_signal_abstains_on_insufficient_baseline():
    window = [Trade("X", "2026-05-01", "S", 1000, 60.0, None)]
    baseline = [Trade("X", "2026-04-01", "S", 100, 50.0, None)]  # only 1 historical
    res = volume_signal(window, baseline)
    assert res["score"] is None
    assert res["reason"] == "insufficient_baseline"


def test_volume_signal_normal_volume_low_score():
    """Window trade size close to baseline mean -> low score."""
    baseline = [Trade("X", f"2026-0{m}-01", "S", 1000, 50.0, None) for m in range(1, 5)]
    window = [Trade("X", "2026-05-01", "S", 1000, 50.0, None)]  # same as baseline
    res = volume_signal(window, baseline)
    assert res["score"] == 0.0  # at baseline, no anomaly


def test_volume_signal_large_window_high_score():
    """Window trade is many stdevs above baseline -> high score."""
    baseline = [Trade("X", f"2026-0{m}-01", "S", 1000, 50.0, None) for m in range(1, 5)]
    window = [Trade("X", "2026-05-01", "S", 100000, 50.0, None)]  # 100x baseline
    res = volume_signal(window, baseline)
    assert res["score"] == 1.0  # clamped to max


# ---- direction_flip_signal ------------------------------------------------


def test_direction_flip_abstains_on_insufficient_baseline():
    baseline = [Trade("X", "2026-01-01", "S", 100, 50.0, None)]
    window = [Trade("X", "2026-05-01", "P", 100, 50.0, None)]
    assert direction_flip_signal(window, baseline)["score"] is None


def test_direction_flip_consistent_direction():
    """Baseline net-seller, window also selling -> no flip."""
    baseline = [Trade("X", f"2026-0{m}-01", "S", 1000, 50.0, None) for m in range(1, 5)]
    window = [Trade("X", "2026-05-01", "S", 500, 50.0, None)]
    res = direction_flip_signal(window, baseline)
    assert res["flipped"] is False
    assert res["score"] == 0.0


def test_direction_flip_detected():
    """Baseline net-seller, window large buy -> flipped."""
    baseline = [Trade("X", f"2026-0{m}-01", "S", 1000, 50.0, None) for m in range(1, 5)]
    window = [Trade("X", "2026-05-01", "P", 5000, 50.0, None)]  # buy
    res = direction_flip_signal(window, baseline)
    assert res["flipped"] is True
    assert res["score"] > 0


# ---- DB loaders -----------------------------------------------------------


def test_load_trades_in_window_respects_dates(db):
    _seed_form4_filing(db, accession="acc-f4", filed_at="2026-05-01")
    _seed_tx(db, accession="acc-f4", insider_name="A",
             trade_date="2026-04-25", code="S", shares=100)
    _seed_tx(db, accession="acc-f4", insider_name="B",
             trade_date="2026-05-10", code="S", shares=200)
    _seed_tx(db, accession="acc-f4", insider_name="C",
             trade_date="2026-03-01", code="S", shares=300)  # outside window

    trades = load_trades_in_window(
        db, cik="0001321655", center_date="2026-05-01", window_days=14,
    )
    insiders = {t.insider_name for t in trades}
    assert insiders == {"A", "B"}


def test_load_insider_baseline_excludes_window_trades(db):
    _seed_form4_filing(db, accession="acc-f4", filed_at="2026-05-01")
    _seed_tx(db, accession="acc-f4", insider_name="K", trade_date="2026-04-15",
             code="S", shares=100)
    _seed_tx(db, accession="acc-f4", insider_name="K", trade_date="2026-04-30",
             code="S", shares=200)  # before "before_date"
    _seed_tx(db, accession="acc-f4", insider_name="K", trade_date="2026-05-05",
             code="S", shares=300)  # ON OR AFTER "before_date" — should be excluded

    baseline = load_insider_baseline(
        db, cik="0001321655", insider_name="K",
        before_date="2026-05-01", months_back=12,
    )
    # Both 2026-04-15 (before) and 2026-04-30 (before 2026-05-01) included.
    assert len(baseline) == 2


def test_load_insider_baseline_excludes_10b5_1_trades(db):
    _seed_form4_filing(db, accession="acc-f4", filed_at="2026-05-01")
    _seed_tx(db, accession="acc-f4", insider_name="K", trade_date="2026-04-01",
             code="S", shares=100, is_10b5_1=1)  # plan-driven
    _seed_tx(db, accession="acc-f4", insider_name="K", trade_date="2026-04-02",
             code="S", shares=100, is_10b5_1=0)  # discretionary
    _seed_tx(db, accession="acc-f4", insider_name="K", trade_date="2026-04-03",
             code="A", shares=100)  # admin — not P/S, excluded

    baseline = load_insider_baseline(
        db, cik="0001321655", insider_name="K", before_date="2026-05-01",
    )
    assert len(baseline) == 1
    assert baseline[0].is_10b5_1 == 0


# ---- run_once: no-trades path --------------------------------------------


def test_run_once_no_trades_in_window_no_llm_call(db):
    _seed_filing(db, accession="acc-10q", filing_type="10-Q",
                 filed_at="2026-05-01", status="analyzed")
    client = MagicMock()
    client.complete = MagicMock(side_effect=AssertionError("LLM should not be called"))

    result = run_once(_config(), db, client)
    assert result["analyzed"] == 1
    assert result["flagged"] == 0

    row = db.execute(
        "SELECT trades_in_window, anomalous FROM correlator_runs WHERE accession = ?",
        ("acc-10q",),
    ).fetchone()
    assert row["trades_in_window"] == 0
    assert row["anomalous"] == 0  # False


def test_run_once_all_plan_driven_no_llm_call(db):
    """Trades exist but all are 10b5-1 or admin codes -> no LLM call."""
    _seed_filing(db, accession="acc-10q", filing_type="10-Q",
                 filed_at="2026-05-01", status="analyzed")
    _seed_form4_filing(db, accession="acc-f4", filed_at="2026-05-03")
    _seed_tx(db, accession="acc-f4", insider_name="K",
             trade_date="2026-05-02", code="S", shares=1000, price=60.0, is_10b5_1=1)
    _seed_tx(db, accession="acc-f4", insider_name="L",
             trade_date="2026-05-02", code="A", shares=500, price=60.0)  # grant

    client = MagicMock()
    client.complete = MagicMock(side_effect=AssertionError("LLM should not be called"))

    result = run_once(_config(), db, client)
    assert result["analyzed"] == 1
    assert result["flagged"] == 0
    assert db.execute(
        "SELECT discretionary_count FROM correlator_runs WHERE accession = ?",
        ("acc-10q",),
    ).fetchone()["discretionary_count"] == 0


# ---- run_once: anomalous path ---------------------------------------------


def test_run_once_anomalous_flagged(db):
    """Multi-insider cluster -> LLM called -> anomalous -> flagged event."""
    _seed_filing(db, accession="acc-10q", filing_type="10-Q",
                 filed_at="2026-05-01", status="analyzed")
    _seed_form4_filing(db, accession="acc-f4", filed_at="2026-05-02")
    for i in range(3):
        _seed_tx(db, accession="acc-f4", insider_name=f"insider_{i}",
                 trade_date="2026-04-30", code="S", shares=50000, price=60.0)

    client = MagicMock()
    client.complete = MagicMock(return_value=CorrelatorVerdict(
        anomalous=True, drivers=["3-insider sell cluster"], confidence=0.85,
    ))

    result = run_once(_config(), db, client)
    assert result["analyzed"] == 1
    assert result["flagged"] == 1

    flagged = db.execute(
        "SELECT flag_reason, correlator_payload FROM flagged_events WHERE accession = ?",
        ("acc-10q",),
    ).fetchone()
    assert flagged["flag_reason"] == "correlator_anomaly"
    payload = json.loads(flagged["correlator_payload"])
    assert payload["verdict"]["anomalous"] is True
    assert payload["cluster"]["max_cluster_size"] == 3


def test_run_once_llm_says_not_anomalous_no_flag(db):
    _seed_filing(db, accession="acc-10q", filing_type="10-Q",
                 filed_at="2026-05-01", status="analyzed")
    _seed_form4_filing(db, accession="acc-f4", filed_at="2026-05-02")
    _seed_tx(db, accession="acc-f4", insider_name="K",
             trade_date="2026-04-30", code="S", shares=100, price=60.0)

    client = MagicMock()
    client.complete = MagicMock(return_value=CorrelatorVerdict(
        anomalous=False, drivers=[], confidence=0.3,
    ))

    result = run_once(_config(), db, client)
    assert result["analyzed"] == 1
    assert result["flagged"] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM flagged_events WHERE accession = ?", ("acc-10q",)
    ).fetchone()[0] == 0


# ---- idempotency / no re-scan --------------------------------------------


def test_idempotent_re_run_skips_already_scanned(db):
    _seed_filing(db, accession="acc-10q", filing_type="10-Q",
                 filed_at="2026-05-01", status="analyzed")
    client = MagicMock()
    client.complete = MagicMock(side_effect=AssertionError("LLM should not be called"))

    run_once(_config(), db, client)
    second = run_once(_config(), db, client)
    assert second["considered"] == 0


# ---- Form 4 sweep ---------------------------------------------------------


def test_form4_sweep_transitions_parsed_to_analyzed(db):
    _seed_form4_filing(db, accession="f4-a", filed_at="2026-05-01")
    _seed_form4_filing(db, accession="f4-b", filed_at="2026-05-02")

    count = _sweep_form4_to_analyzed(db)
    assert count == 2

    statuses = {
        r["accession"]: r["status"]
        for r in db.execute("SELECT accession, status FROM filings_seen").fetchall()
    }
    assert statuses["f4-a"] == "analyzed"
    assert statuses["f4-b"] == "analyzed"


# ---- type scope -----------------------------------------------------------


def test_form4_rows_not_picked_up_as_triggers(db):
    _seed_form4_filing(db, accession="f4-only", filed_at="2026-05-01")
    client = MagicMock()
    client.complete = MagicMock(side_effect=AssertionError("LLM should not be called"))

    result = run_once(_config(), db, client)
    # Form 4 row swept to analyzed but not considered as a trigger.
    assert result["considered"] == 0
    assert result["form4_swept_to_analyzed"] == 1


# ---- failure semantics ---------------------------------------------------


def test_llm_exception_recorded_as_failure(db):
    """LLM raising mid-call doesn't crash the whole cycle; per-filing error
    is recorded in the summary and no flagged_events row is inserted."""
    _seed_filing(db, accession="acc-10q", filing_type="10-Q",
                 filed_at="2026-05-01", status="analyzed")
    _seed_form4_filing(db, accession="acc-f4", filed_at="2026-05-02")
    for i in range(3):
        _seed_tx(db, accession="acc-f4", insider_name=f"insider_{i}",
                 trade_date="2026-04-30", code="S", shares=50000, price=60.0)

    client = MagicMock()
    client.complete = MagicMock(side_effect=RuntimeError("api blip"))

    result = run_once(_config(), db, client)
    assert result["failed"] == 1
    assert result["analyzed"] == 0
    assert db.execute(
        "SELECT COUNT(*) FROM flagged_events WHERE accession = ?", ("acc-10q",)
    ).fetchone()[0] == 0
