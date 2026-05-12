"""Tests for the SQLite connection layer and llm_call_log writer.

Mostly behavioral — confirms WAL mode, read-only enforcement, schema
idempotency, and that log_call / log_provider_switch round-trip cleanly.
"""
from __future__ import annotations

import sqlite3

import pytest

from redline.llm.log import log_call, log_provider_switch
from redline.storage.db import connect, init_schema, open_db


def test_connect_creates_parent_dir(tmp_path):
    db_path = tmp_path / "nested" / "subdir" / "redline.db"
    conn = connect(db_path)
    try:
        assert db_path.exists()
    finally:
        conn.close()


def test_wal_mode_enabled(tmp_path):
    conn = connect(tmp_path / "test.db")
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode == "wal"
    finally:
        conn.close()


def test_read_only_pragma_blocks_writes(tmp_path):
    # Create + initialize first
    with open_db(tmp_path / "test.db"):
        pass

    conn_ro = connect(tmp_path / "test.db", read_only=True)
    try:
        assert conn_ro.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            log_call(
                conn_ro, call_site="t", provider="openai", model="m",
                prompt_version="v", tokens_in=0, tokens_out=0,
                cost_usd=0.0, latency_ms=0, cache_hit=False,
            )
    finally:
        conn_ro.close()


def test_log_call_roundtrip(tmp_path):
    with open_db(tmp_path / "test.db") as conn:
        rid = log_call(
            conn, call_site="diff_gate", provider="openai", model="gpt-4o-mini",
            prompt_version="v1", tokens_in=500, tokens_out=50,
            cost_usd=0.0001, latency_ms=900, cache_hit=False,
        )
        row = conn.execute(
            "SELECT * FROM llm_call_log WHERE id = ?", (rid,)
        ).fetchone()
        assert row["call_site"] == "diff_gate"
        assert row["provider"] == "openai"
        assert row["model"] == "gpt-4o-mini"
        assert row["prompt_version"] == "v1"
        assert row["tokens_in"] == 500
        assert row["tokens_out"] == 50
        assert row["cost_usd"] == 0.0001
        assert row["latency_ms"] == 900
        assert row["cache_hit"] == 0
        assert row["status"] == "ok"
        assert row["error_reason"] is None


def test_provider_switch_sentinel(tmp_path):
    with open_db(tmp_path / "test.db") as conn:
        log_provider_switch(
            conn, from_provider="openai", to_provider="anthropic",
            reason="RateLimitError: insufficient_quota",
        )
        row = conn.execute(
            "SELECT * FROM llm_call_log WHERE call_site = 'provider_switch'"
        ).fetchone()
        assert row["provider"] == "anthropic"
        assert row["model"] == "-"
        assert row["tokens_in"] == 0
        assert row["tokens_out"] == 0
        assert row["cost_usd"] == 0.0
        assert row["status"] == "info"
        assert row["error_reason"].startswith("switched from openai")


def test_init_schema_idempotent(tmp_path):
    conn = connect(tmp_path / "test.db")
    try:
        init_schema(conn)
        init_schema(conn)  # second run must not raise
        tables = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "llm_call_log" in tables
    finally:
        conn.close()


def test_open_db_closes_connection(tmp_path):
    with open_db(tmp_path / "test.db") as conn:
        conn.execute("SELECT 1").fetchone()
        conn_ref = conn
    with pytest.raises(sqlite3.ProgrammingError):
        conn_ref.execute("SELECT 1")
