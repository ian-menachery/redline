"""Tests for the eval-harness CLI determinism plumbing (``--db-path``/``--fresh``).

The full harness run hits EDGAR + the LLM, so these tests stub ``run`` and the
LLM client and only assert the DB-isolation plumbing: ``--fresh`` requires an
explicit path, and ``--db-path`` runs against a fresh, watchlist-seeded DB.
"""
from __future__ import annotations

import pytest

from redline.eval import harness


def test_fresh_requires_db_path():
    with pytest.raises(SystemExit):
        harness.main(["--all", "--fresh"])


def test_db_path_runs_on_isolated_seeded_db(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_run(config, conn, client, *, events, use_judge_on_none):
        captured["file"] = conn.execute("PRAGMA database_list").fetchone()[2]
        captured["watchlist"] = conn.execute(
            "SELECT COUNT(*) FROM watchlist").fetchone()[0]
        return {"global": "0/0", "per_subsystem": {}, "per_event": []}

    monkeypatch.setattr(harness, "run", fake_run)
    monkeypatch.setattr(harness, "LLMClient", lambda config, conn: object())

    db = tmp_path / "eval.db"
    assert harness.main(["--all", "--db-path", str(db), "--fresh"]) == 0
    assert db.exists()
    # The run executed against the given DB, freshly seeded with the 8-name watchlist.
    assert captured["file"].replace("\\", "/").endswith("eval.db")
    assert captured["watchlist"] == 8
