"""Insider-trading correlator orchestrator (Subsystem 4).

Per ARCHITECTURE.md §5. For each non-Form-4 filing event that hasn't been
correlator-scanned:

1. Load Form 4 transactions for the issuer in ±14d window of the filing date.
2. Apply NOTES.md §3 filters (P/S codes only, 10b5-1 plan-driven excluded).
3. If no qualifying trades: record a clean correlator_runs row, no LLM call.
4. Else: compute cluster + per-insider volume + per-insider direction signals,
   call quality-role LLM with ``CorrelatorVerdict`` schema.
5. If anomalous: insert a flagged_events row with flag_reason='correlator_anomaly'.
6. Record correlator_runs row regardless.

Form 4 rows themselves don't need correlator analysis (they're the input
data). At the start of each run we bulk-sweep ``status='parsed'`` Form 4s
to ``analyzed`` so they don't sit at 'parsed' forever.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sqlite3
import sys

from redline.config import RedlineConfig
from redline.correlator.signals import (
    Trade,
    cluster_signal,
    direction_flip_signal,
    load_insider_baseline,
    load_trades_in_window,
    volume_signal,
)
from redline.llm.client import LLMClient
from redline.llm.schemas import CorrelatorVerdict

_LOG = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
TRIGGER_TYPES: list[str] = ["10-K", "10-Q", "8-K"]
WINDOW_DAYS = 14


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _load_prompt(prompts_dir: str = "config/prompts") -> str:
    from pathlib import Path
    return Path(prompts_dir, f"correlator_{PROMPT_VERSION}.txt").read_text(encoding="utf-8")


def _pending_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Triggers: 10-K/10-Q/8-K at parsed-or-analyzed without a correlator_runs row."""
    placeholders = ",".join("?" * len(TRIGGER_TYPES))
    return conn.execute(
        f"""
        SELECT fs.accession, fs.cik, fs.filing_type, fs.filed_at,
               w.ticker
        FROM filings_seen fs
        JOIN watchlist w ON w.cik = fs.cik
        WHERE fs.filing_type IN ({placeholders})
          AND fs.status IN ('parsed', 'analyzed')
          AND NOT EXISTS (
              SELECT 1 FROM correlator_runs cr WHERE cr.accession = fs.accession
          )
        """,
        TRIGGER_TYPES,
    ).fetchall()


def _record_run(
    conn: sqlite3.Connection,
    *, accession: str, trades_in_window: int, discretionary_count: int,
    anomalous: bool | None, confidence: float | None,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO correlator_runs (
            accession, ran_at, trades_in_window, discretionary_count,
            anomalous, confidence
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (accession, _now_iso(), trades_in_window, discretionary_count,
         int(anomalous) if isinstance(anomalous, bool) else None,
         confidence),
    )


def _insert_flagged(
    conn: sqlite3.Connection,
    *, accession: str, verdict: CorrelatorVerdict, payload: dict,
) -> None:
    conn.execute(
        """
        INSERT INTO flagged_events (
            accession, flag_reason, diff_summary, correlator_payload,
            materiality_max, flagged_at
        ) VALUES (?, ?, NULL, ?, ?, ?)
        """,
        (
            accession, "correlator_anomaly",
            json.dumps(payload, default=str),
            verdict.confidence, _now_iso(),
        ),
    )


def _sweep_form4_to_analyzed(conn: sqlite3.Connection) -> int:
    """Form 4 rows have no analyzer-side work; transition them so they don't
    sit at 'parsed' forever. Returns count updated."""
    cur = conn.execute(
        """
        UPDATE filings_seen SET status = 'analyzed', last_attempted = ?
        WHERE filing_type = '4' AND status = 'parsed'
        """,
        (_now_iso(),),
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _build_user_message(
    *, ticker: str, filing_type: str, filed_at: str,
    trades: list[Trade], cluster: dict, per_insider: list[dict],
) -> str:
    lines: list[str] = [
        f"Filing: {ticker} {filing_type} filed {filed_at}",
        "",
        f"All Form 4 transactions in ±{WINDOW_DAYS} day window ({len(trades)} total):",
    ]
    for t in trades:
        plan_marker = " [10b5-1]" if t.is_10b5_1 == 1 else ""
        price_str = f"${t.price:.2f}" if t.price is not None else "?"
        lines.append(
            f"  - {t.trade_date}  {t.insider_name[:40]:<40} "
            f"{t.code:<2} shares={int(t.shares):>9,}  price={price_str}{plan_marker}"
        )
    lines.append("")

    discretionary = [t for t in trades if t.is_discretionary]
    lines.append(f"Discretionary set (after P/S + 10b5-1 filter): {len(discretionary)} trade(s)")
    lines.append("")

    lines.append("CLUSTER SIGNAL:")
    lines.append(
        f"  max_same_direction_cluster = {cluster['max_cluster_size']}  "
        f"score = {cluster['score']:.2f}"
    )
    lines.append(f"  sellers: {cluster['sellers']}")
    lines.append(f"  buyers:  {cluster['buyers']}")
    lines.append("")

    lines.append("PER-INSIDER SIGNALS:")
    if not per_insider:
        lines.append("  (no discretionary insiders to score)")
    for entry in per_insider:
        vol = entry["volume"]
        flip = entry["direction"]
        vol_repr = (
            f"score={vol['score']:.2f} z={vol.get('z_score', 0):.1f}"
            if vol["score"] is not None
            else f"abstain ({vol.get('reason', '')})"
        )
        flip_repr = (
            f"score={flip['score']:.2f} flipped={flip.get('flipped', False)}"
            if flip["score"] is not None
            else f"abstain ({flip.get('reason', '')})"
        )
        lines.append(f"  - {entry['insider']}")
        lines.append(f"      volume:    {vol_repr}")
        lines.append(f"      direction: {flip_repr}")
    return "\n".join(lines)


def _call_llm(
    client: LLMClient, *, system: str, user: str,
) -> CorrelatorVerdict:
    return client.complete(
        system=system, user=user,
        schema=CorrelatorVerdict, role="quality",
        call_site="correlator", prompt_version=PROMPT_VERSION,
    )


# ---------------------------------------------------------------------------
# Per-filing pipeline
# ---------------------------------------------------------------------------

def _analyze_one(
    conn: sqlite3.Connection,
    client: LLMClient,
    config: RedlineConfig,
    *, accession: str, cik: str, filing_type: str, filed_at: str, ticker: str,
    prompt: str,
) -> dict:
    trades = load_trades_in_window(
        conn, cik=cik, center_date=filed_at,
        window_days=config.correlator.window_days,
    )

    if not trades:
        _record_run(
            conn, accession=accession,
            trades_in_window=0, discretionary_count=0,
            anomalous=False, confidence=None,
        )
        return {"trades_in_window": 0, "discretionary": 0,
                "anomalous": False, "reason": "no_trades_in_window"}

    discretionary = [t for t in trades if t.is_discretionary]
    if not discretionary:
        _record_run(
            conn, accession=accession,
            trades_in_window=len(trades), discretionary_count=0,
            anomalous=False, confidence=None,
        )
        return {"trades_in_window": len(trades), "discretionary": 0,
                "anomalous": False, "reason": "all_plan_or_admin"}

    cluster = cluster_signal(trades)
    per_insider: list[dict] = []
    for insider in sorted({t.insider_name for t in discretionary}):
        window_trades = [t for t in discretionary if t.insider_name == insider]
        baseline = load_insider_baseline(
            conn, cik=cik, insider_name=insider,
            before_date=filed_at,
            months_back=12,  # NOTES.md §3.1 recommended default
        )
        per_insider.append({
            "insider": insider,
            "volume": volume_signal(window_trades, baseline),
            "direction": direction_flip_signal(window_trades, baseline),
        })

    user = _build_user_message(
        ticker=ticker, filing_type=filing_type, filed_at=filed_at,
        trades=trades, cluster=cluster, per_insider=per_insider,
    )
    verdict = _call_llm(client, system=prompt, user=user)

    payload = {
        "verdict": verdict.model_dump(),
        "cluster": cluster,
        "per_insider": per_insider,
        "trade_count": len(trades),
        "discretionary_count": len(discretionary),
    }

    _record_run(
        conn, accession=accession,
        trades_in_window=len(trades),
        discretionary_count=len(discretionary),
        anomalous=verdict.anomalous, confidence=verdict.confidence,
    )

    if verdict.anomalous:
        _insert_flagged(conn, accession=accession, verdict=verdict, payload=payload)

    return {
        "trades_in_window": len(trades),
        "discretionary": len(discretionary),
        "cluster_size": cluster["max_cluster_size"],
        "anomalous": verdict.anomalous,
        "confidence": verdict.confidence,
        "drivers": verdict.drivers,
    }


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def run_once(
    config: RedlineConfig,
    conn: sqlite3.Connection,
    client: LLMClient,
) -> dict:
    """One correlator pass."""
    swept = _sweep_form4_to_analyzed(conn)

    prompt = _load_prompt()
    rows = _pending_rows(conn)
    per_filing: list[dict] = []
    analyzed = 0
    failed = 0
    flagged = 0

    for row in rows:
        accession = row["accession"]
        try:
            result = _analyze_one(
                conn, client, config,
                accession=accession,
                cik=row["cik"], filing_type=row["filing_type"],
                filed_at=row["filed_at"], ticker=row["ticker"],
                prompt=prompt,
            )
            analyzed += 1
            if result.get("anomalous"):
                flagged += 1
            per_filing.append({"accession": accession, **result})
        except Exception as e:
            reason = f"{type(e).__name__}: {e}"
            _LOG.warning("Correlator failure for %s: %s", accession, reason)
            failed += 1
            per_filing.append({"accession": accession, "error": reason})

    return {
        "form4_swept_to_analyzed": swept,
        "considered": len(rows),
        "analyzed": analyzed,
        "failed": failed,
        "flagged": flagged,
        "per_filing": per_filing,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Correlator for redline.")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--settings", default="config/settings.toml")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from dotenv import load_dotenv
    load_dotenv()

    config = RedlineConfig.from_toml(args.settings)
    from redline.storage.db import open_db
    from redline.storage.schema import init_full_schema

    with open_db(config.storage.db_path) as conn:
        init_full_schema(conn)
        client = LLMClient(config, conn)
        summary = run_once(config, conn, client)
        _LOG.info("cycle: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
