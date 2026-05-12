"""Historical filing replay for the eval harness (Subsystem 6).

Per ARCHITECTURE.md §12. Each eval event has bespoke backfill needs:

- ``key_10k_fy22`` — KEY FY2022 10-K + FY2021 prior, then diff_analyzer
- ``cvna_10k_fy22`` — same shape for CVNA
- ``pltr_karp_form4_2024`` — PLTR Q3 2024 10-Q (correlator trigger) +
  Karp Form 4 cluster in Nov 2024 (the discretionary trades), then
  correlator

Backfill writes into the same DB as live operation. The replay handler
is responsible for inserting ``filings_seen`` rows at status='fetched';
the existing fetcher / diff_analyzer / correlator subsystems pick them
up via their normal status-driven queries. Each handler returns the
"accession to grade" — the filing whose post-pipeline outputs the
grader will read.

Phase 1 deviation from ARCHITECTURE.md §12: ``eval_run_id`` plumbing is
deferred. Backfilled rows write with ``eval_run_id=NULL`` and live in
the same tables as live data. Phase 2 will add the plumbing + dashboard
filtering by ``eval_run_id IS NULL`` for the default view.
"""
from __future__ import annotations

import datetime
import logging
import sqlite3
from typing import Callable

import edgar

from redline.config import RedlineConfig
from redline.correlator.analyzer import run_once as correlator_run_once
from redline.diff.analyzer import run_once as diff_run_once
from redline.eval.models import EvalEvent
from redline.fetcher import run_once as fetcher_run_once
from redline.llm.client import LLMClient

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Common backfill helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _ensure_filings_seen_row(
    conn: sqlite3.Connection, filing: edgar.EntityFiling,  # type: ignore[attr-defined]
) -> str:
    """Insert (idempotently) one filings_seen row at status='fetched'.

    Returns the accession. The CIK is zero-padded to 10 digits to match the
    watchlist schema convention.
    """
    cik = str(filing.cik).zfill(10)
    accession = filing.accession_no
    period_end = getattr(filing, "period_of_report", None)
    conn.execute(
        """
        INSERT OR IGNORE INTO filings_seen (
            accession, cik, filing_type, period_end, filed_at, status,
            retry_count, discovered_at
        ) VALUES (?, ?, ?, ?, ?, 'fetched', 0, ?)
        """,
        (
            accession, cik, filing.form,
            str(period_end) if period_end else None,
            str(filing.filing_date),
            _now_iso(),
        ),
    )
    return accession


def _fetch_period_filing(
    *, ticker: str, form: str, period_label: str,
) -> edgar.EntityFiling | None:  # type: ignore[name-defined]
    """Find the filing for ``ticker`` of ``form`` covering ``period_label``.

    ``period_label`` examples: ``"FY2022"`` -> period_of_report = 2022-12-31
    for calendar-year filers; ``"Q3 2024"`` -> period_of_report = 2024-09-30.
    """
    target_period = _period_label_to_iso(period_label)
    if target_period is None:
        return None
    company = edgar.Company(ticker)
    filings = company.get_filings(form=form)
    for f in filings:
        pr = getattr(f, "period_of_report", None)
        if pr is None:
            continue
        if str(pr)[:10] == target_period:
            return f
    return None


def _period_label_to_iso(label: str) -> str | None:
    """Map a fiscal label to an ISO date for period_of_report comparison.

    Phase 1 supports calendar-year filers only. The 8 watchlist names are
    all calendar-year reporters; non-CY filers can be added per-ticker
    if/when needed.
    """
    label = label.strip()
    # FY2022 -> 2022-12-31
    if label.upper().startswith("FY"):
        try:
            year = int(label[2:].strip())
            return f"{year}-12-31"
        except ValueError:
            return None
    # Q3 2024 -> 2024-09-30
    if label.startswith("Q"):
        try:
            q = int(label[1])
            year = int(label.split(" ", 1)[1])
            month, day = {1: ("03", "31"), 2: ("06", "30"),
                          3: ("09", "30"), 4: ("12", "31")}[q]
            return f"{year}-{month}-{day}"
        except (ValueError, IndexError, KeyError):
            return None
    return None


def _find_prior_period_10k(
    *, ticker: str, current_filed_date: str,
) -> edgar.EntityFiling | None:  # type: ignore[name-defined]
    """The 10-K filed immediately before ``current_filed_date`` for ``ticker``."""
    company = edgar.Company(ticker)
    candidates = company.get_filings(form="10-K")
    best: edgar.EntityFiling | None = None  # type: ignore[name-defined]
    best_date = ""
    for f in candidates:
        fd = str(f.filing_date)
        if fd < current_filed_date and fd > best_date:
            best = f
            best_date = fd
    return best


# ---------------------------------------------------------------------------
# Per-event handlers
# ---------------------------------------------------------------------------

def _replay_10k_diff_event(
    conn: sqlite3.Connection, config: RedlineConfig, client: LLMClient,
    *, ticker: str, period_label: str,
) -> tuple[str | None, str]:
    """Shared shape for KEY/CVNA 10-K diff events.

    Returns (accession_to_grade, notes). ``accession_to_grade`` is None on
    fatal backfill failure (e.g. couldn't locate the target filing).
    """
    edgar.set_identity(config.poller.edgar_user_agent)

    target = _fetch_period_filing(ticker=ticker, form="10-K", period_label=period_label)
    if target is None:
        return None, f"could not locate {ticker} 10-K for {period_label}"

    target_acc = _ensure_filings_seen_row(conn, target)
    prior = _find_prior_period_10k(ticker=ticker, current_filed_date=str(target.filing_date))
    if prior is None:
        # No prior available — diff_analyzer will mark target as analyzed
        # without a diff. Continue; pass_criteria will fail naturally.
        _LOG.warning("No prior 10-K found for %s before %s", ticker, target.filing_date)
        prior_acc = None
    else:
        prior_acc = _ensure_filings_seen_row(conn, prior)

    # Parse both filings
    fetcher_run_once(config, conn)

    # Run diff analyzer against the eligible pair
    diff_run_once(config, conn, client)

    return target_acc, (
        f"backfilled {ticker} 10-K {period_label} (target={target_acc}, "
        f"prior={prior_acc or 'none'})"
    )


def replay_key_10k_fy22(
    conn: sqlite3.Connection, config: RedlineConfig, client: LLMClient,
    event: EvalEvent,
) -> tuple[str | None, str]:
    return _replay_10k_diff_event(
        conn, config, client, ticker="KEY", period_label="FY2022",
    )


def replay_cvna_10k_fy22(
    conn: sqlite3.Connection, config: RedlineConfig, client: LLMClient,
    event: EvalEvent,
) -> tuple[str | None, str]:
    return _replay_10k_diff_event(
        conn, config, client, ticker="CVNA", period_label="FY2022",
    )


def replay_pltr_karp_form4_2024(
    conn: sqlite3.Connection, config: RedlineConfig, client: LLMClient,
    event: EvalEvent,
) -> tuple[str | None, str]:
    """Backfill PLTR Q3 2024 10-Q + Karp Form 4 cluster, then run correlator.

    The correlator's trigger is a non-Form-4 filing in the window. The Karp
    cluster (Nov 2024) is in the ±14d window of PLTR's Q3 2024 10-Q filed
    2024-11-05 (NOTES.md spike confirmed accession 0001321655-24-000209).
    """
    edgar.set_identity(config.poller.edgar_user_agent)

    # Trigger filing: PLTR Q3 2024 10-Q
    trigger = _fetch_period_filing(ticker="PLTR", form="10-Q", period_label="Q3 2024")
    if trigger is None:
        return None, "could not locate PLTR Q3 2024 10-Q"
    trigger_acc = _ensure_filings_seen_row(conn, trigger)

    # Form 4s for PLTR in 2024-11-01:2024-12-31 (event.period)
    company = edgar.Company("PLTR")
    f4_set = company.get_filings(form="4", filing_date="2024-11-01:2024-12-31")
    inserted_f4 = 0
    for f4 in f4_set:
        _ensure_filings_seen_row(conn, f4)
        inserted_f4 += 1

    fetcher_run_once(config, conn)  # parse trigger + form 4s
    # diff_analyzer will skip the 10-Q (no prior 10-Q in DB unless we backfilled);
    # that's fine, the event's tests = [correlator].
    correlator_run_once(config, conn, client)

    return trigger_acc, (
        f"backfilled PLTR Q3 2024 trigger ({trigger_acc}) + {inserted_f4} "
        f"Form 4s in 2024-11-01:2024-12-31"
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

ReplayFn = Callable[
    [sqlite3.Connection, RedlineConfig, LLMClient, EvalEvent],
    tuple[str | None, str],
]

HANDLERS: dict[str, ReplayFn] = {
    "key_10k_fy22": replay_key_10k_fy22,
    "cvna_10k_fy22": replay_cvna_10k_fy22,
    "pltr_karp_form4_2024": replay_pltr_karp_form4_2024,
}


def replay(
    conn: sqlite3.Connection, config: RedlineConfig, client: LLMClient,
    event: EvalEvent,
) -> tuple[str | None, str]:
    """Dispatch to the per-event handler. Returns (accession_to_grade, notes)."""
    handler = HANDLERS.get(event.id)
    if handler is None:
        return None, f"no replay handler registered for event_id={event.id}"
    return handler(conn, config, client, event)
