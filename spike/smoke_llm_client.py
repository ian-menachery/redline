"""Phase 1 entry smoke test — exercises the real LLM client against OpenAI.

Runs two calls (one cheap, one quality), validates Pydantic round-trip, then
prints the resulting llm_call_log rows. Spends ~$0.02 against the active key.

Verifies end-to-end:
  - openai.beta.chat.completions.parse exists in the installed SDK
  - response_format=PydanticModel returns a validated instance on .parsed
  - prompt_tokens_details (auto-cache reporting) shape matches our reader
  - llm_call_log writes are correct shape (provider, model, cost, etc.)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

from redline.config import RedlineConfig
from redline.llm.client import LLMClient
from redline.llm.schemas import DiffGateDecision, DiffSummary
from redline.storage.db import open_db

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
load_dotenv(Path(__file__).parent.parent / ".env")

cfg = RedlineConfig.from_toml("config/settings.toml")

with open_db(cfg.storage.db_path) as conn:
    client = LLMClient(cfg, conn)

    # --- 1. Cheap call (Stage 2 gate sim) -----------------------------------
    print("=" * 64)
    print("CHEAP-role call (DiffGateDecision via gpt-4o-mini)")
    print("=" * 64)
    gate = client.complete(
        system=(
            "Classify whether a textual change in a 10-K Risk Factors section "
            "is substantive (worth flagging) or cosmetic. Return JSON: "
            '{"substantive": true|false, "reason": "<one sentence>"}'
        ),
        user=(
            'OLD: "We have 3,838 full-time employees."\n'
            'NEW: "We have 3,735 full-time employees."'
        ),
        schema=DiffGateDecision,
        role="cheap",
        call_site="diff_gate",
        prompt_version="smoke_v1",
    )
    print(f"  parsed.substantive: {gate.substantive}")
    print(f"  parsed.reason:      {gate.reason}")

    # --- 2. Quality call (Stage 3 summary sim) ------------------------------
    print("\n" + "=" * 64)
    print("QUALITY-role call (DiffSummary via gpt-4o)")
    print("=" * 64)
    summary = client.complete(
        system=(
            "Summarize a substantive 10-K Risk Factors change. Return JSON: "
            '{"change_type": "addition"|"removal"|"modification"|"restructure", '
            '"materiality": 0.0-1.0, "summary": "1-3 sentences", '
            '"affected_topics": ["tag1", "tag2"]}'
        ),
        user=(
            "OLD (FY22): risk factors list does not mention generative AI.\n"
            'NEW (FY23): adds bullet "reluctance of customers to purchase '
            'products incorporating generative AI" to the risk factors list.'
        ),
        schema=DiffSummary,
        role="quality",
        call_site="diff_summary",
        prompt_version="smoke_v1",
    )
    print(f"  parsed.change_type:     {summary.change_type}")
    print(f"  parsed.materiality:     {summary.materiality}")
    print(f"  parsed.summary:         {summary.summary}")
    print(f"  parsed.affected_topics: {summary.affected_topics}")

    # --- 3. Inspect llm_call_log --------------------------------------------
    print("\n" + "=" * 64)
    print("llm_call_log rows")
    print("=" * 64)
    rows = conn.execute(
        """
        SELECT id, called_at, call_site, provider, model,
               tokens_in, tokens_out, cost_usd, latency_ms,
               cache_hit, status
        FROM llm_call_log
        ORDER BY id
        """
    ).fetchall()
    for r in rows:
        d = dict(r)
        print(
            f"  #{d['id']:>2}  {d['called_at'][:19]}  {d['call_site']:<13} "
            f"{d['provider']:<9} {d['model']:<20} "
            f"in={d['tokens_in']:>5}  out={d['tokens_out']:>4}  "
            f"${d['cost_usd']:.6f}  {d['latency_ms']:>4}ms  "
            f"cache_hit={d['cache_hit']}  status={d['status']}"
        )

    total = conn.execute("SELECT SUM(cost_usd) FROM llm_call_log").fetchone()[0]
    print(f"\n  Total cost this run: ${total:.6f}")
