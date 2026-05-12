"""Streamlit dashboard for redline.

Per ARCHITECTURE.md §6: read-only against SQLite (PRAGMA query_only=ON),
default view is last N flagged events sorted by recency, per-event detail
expands the diff summaries, Stage 1 raw chunks, correlator output, and
Form 4 transactions in window.

Launch: ``streamlit run src/redline/dashboard/app.py``.

Phase 1 surface only. Alerts are Phase 2 (ARCHITECTURE.md §6).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import streamlit as st

from redline.config import RedlineConfig
from redline.storage.db import connect


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

@st.cache_resource
def _conn() -> sqlite3.Connection:
    config = RedlineConfig.from_toml("config/settings.toml")
    return connect(config.storage.db_path, read_only=True)


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def _watchlist(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT ticker, name, sector FROM watchlist ORDER BY ticker"
    )]


def _flagged_filings(
    conn, *, ticker: str | None, filing_type: str | None,
    flag_reason: str | None, min_materiality: float,
    limit: int,
) -> list[dict]:
    """One row per (accession, flag_reason) — both diff and correlator flags surface."""
    where = ["1 = 1"]
    params: list = []
    if ticker:
        where.append("w.ticker = ?")
        params.append(ticker)
    if filing_type:
        where.append("fs.filing_type = ?")
        params.append(filing_type)
    if flag_reason:
        where.append("fe.flag_reason = ?")
        params.append(flag_reason)
    where.append("(fe.materiality_max IS NULL OR fe.materiality_max >= ?)")
    params.append(min_materiality)

    sql = f"""
        SELECT
            fe.id              AS event_id,
            fe.accession,
            fe.flag_reason,
            fe.materiality_max,
            fe.flagged_at,
            fs.filing_type,
            fs.filed_at,
            fs.period_end,
            w.ticker,
            w.name             AS company_name,
            w.sector,
            fs.cik
        FROM flagged_events fe
        JOIN filings_seen fs ON fs.accession = fe.accession
        JOIN watchlist w     ON w.cik = fs.cik
        WHERE {" AND ".join(where)}
        ORDER BY fe.flagged_at DESC
        LIMIT ?
    """
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def _diff_summaries_for_event(conn, event_id: int) -> list[dict]:
    """Stage 3 summaries stored as JSON list in flagged_events.diff_summary."""
    row = conn.execute(
        "SELECT diff_summary FROM flagged_events WHERE id = ?", (event_id,),
    ).fetchone()
    if not row or not row["diff_summary"]:
        return []
    return json.loads(row["diff_summary"])


def _diff_results(conn, *, accession: str, stage: int | None = None) -> list[dict]:
    if stage is None:
        rows = conn.execute(
            "SELECT * FROM diff_results WHERE accession = ? ORDER BY section, stage, id",
            (accession,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM diff_results WHERE accession = ? AND stage = ? "
            "ORDER BY section, id",
            (accession, stage),
        ).fetchall()
    return [dict(r) for r in rows]


def _correlator_payload(conn, event_id: int) -> dict | None:
    row = conn.execute(
        "SELECT correlator_payload FROM flagged_events WHERE id = ?", (event_id,),
    ).fetchone()
    if not row or not row["correlator_payload"]:
        return None
    return json.loads(row["correlator_payload"])


def _correlator_run(conn, accession: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM correlator_runs WHERE accession = ?", (accession,),
    ).fetchone()
    return dict(row) if row else None


def _form4_transactions_in_window(
    conn, *, cik: str, center_date: str, window_days: int = 14,
) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT trade_date, insider_name, code, shares, price, is_10b5_1,
               plan_adopted_date, explanation
        FROM form4_transactions
        WHERE cik = ?
          AND trade_date >= date(?, '-{window_days} day')
          AND trade_date <= date(?, '+{window_days} day')
        ORDER BY trade_date, insider_name
        """,
        (cik, center_date, center_date),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------

def _edgar_url(accession: str, cik: str) -> str:
    cik_short = str(int(cik))  # strip zero padding for the URL
    accession_nodashes = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_short}/"
        f"{accession_nodashes}/{accession}-index.htm"
    )


def _materiality_pill(m: float | None) -> str:
    if m is None:
        return "—"
    color = "🔴" if m >= 0.8 else "🟠" if m >= 0.6 else "🟡"
    return f"{color} {m:.2f}"


def _flag_reason_pill(reason: str) -> str:
    return {
        "diff_material": "📑 diff",
        "correlator_anomaly": "📊 correlator",
        "both": "📑📊 both",
    }.get(reason, reason)


def _render_event_card(conn, event: dict) -> None:
    accession = event["accession"]
    ticker = event["ticker"]
    title = f"**{ticker}** {event['filing_type']}  {_flag_reason_pill(event['flag_reason'])}  {_materiality_pill(event['materiality_max'])}"

    with st.expander(title, expanded=False):
        # Filing metadata
        c1, c2, c3 = st.columns(3)
        c1.markdown(f"**Company:** {event['company_name']}")
        c1.markdown(f"**Sector:** {event['sector']}")
        c2.markdown(f"**Filed:** {event['filed_at']}")
        c2.markdown(f"**Period:** {event['period_end'] or '—'}")
        c3.markdown(f"**Accession:** `{accession}`")
        c3.markdown(f"[Open on EDGAR ↗]({_edgar_url(accession, event['cik'])})")
        st.markdown(f"_Flagged at {event['flagged_at']}_")
        st.divider()

        # Diff summaries (Stage 3)
        if event["flag_reason"] in ("diff_material", "both"):
            summaries = _diff_summaries_for_event(conn, event["event_id"])
            if summaries:
                st.markdown(f"#### Diff summaries ({len(summaries)})")
                ranked = sorted(summaries, key=lambda s: s.get("materiality", 0), reverse=True)
                for s in ranked:
                    st.markdown(
                        f"**[{s.get('section', '?')}] "
                        f"{_materiality_pill(s.get('materiality'))} "
                        f"{s.get('change_type', '?')}** — "
                        f"_{', '.join(s.get('affected_topics', []) or ['(no topics)'])}_"
                    )
                    st.markdown(f"> {s.get('summary', '')}")
                st.markdown("")

        # Stage 1 raw chunks (collapsed for "show me what changed")
        stage1_2 = _diff_results(conn, accession=accession, stage=2)
        if stage1_2:
            with st.expander(f"Raw Stage-1 chunks (n={len(stage1_2)}) — what the diff actually saw"):
                for row in stage1_2:
                    decision = json.loads(row["gate_decision"]) if row["gate_decision"] else {}
                    badge = "✅ substantive" if decision.get("substantive") else "⛔ cosmetic"
                    st.markdown(f"**[{row['section']}] {badge}** — _{decision.get('reason', '')}_")
                    cols = st.columns(2)
                    cols[0].markdown("**OLD**")
                    cols[0].text(row["chunk_old"] or "(empty)")
                    cols[1].markdown("**NEW**")
                    cols[1].text(row["chunk_new"] or "(empty)")
                    st.divider()

        # Correlator
        if event["flag_reason"] in ("correlator_anomaly", "both"):
            payload = _correlator_payload(conn, event["event_id"])
            if payload:
                st.markdown("#### Correlator verdict")
                v = payload["verdict"]
                cols = st.columns(3)
                cols[0].metric("Anomalous", "yes" if v["anomalous"] else "no")
                cols[1].metric("Confidence", f"{v['confidence']:.2f}")
                cols[2].metric("Cluster size", payload["cluster"]["max_cluster_size"])
                if v.get("drivers"):
                    st.markdown("**Drivers:**")
                    for d in v["drivers"]:
                        st.markdown(f"- {d}")
                with st.expander("Raw signals"):
                    st.json(payload)

        # Form 4 transactions in window — relevant for both reasons
        txs = _form4_transactions_in_window(
            conn, cik=event["cik"], center_date=event["filed_at"], window_days=14,
        )
        if txs:
            st.markdown(f"#### Form 4 transactions in ±14d window (n={len(txs)})")
            st.dataframe(txs, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="redline", layout="wide")
    conn = _conn()

    st.title("redline")
    st.caption(
        "Scheduled SEC EDGAR monitoring for an 8-ticker watchlist. "
        "Flags surfaced via QoQ section diffs and Form 4 insider-trading correlation."
    )

    # ---- sidebar filters --------------------------------------------------
    st.sidebar.header("Filters")
    watchlist = _watchlist(conn)
    ticker_opts = ["(all)"] + [w["ticker"] for w in watchlist]
    ticker_sel = st.sidebar.selectbox("Ticker", ticker_opts)

    filing_type_opts = ["(all)", "10-K", "10-Q", "8-K", "4"]
    filing_type_sel = st.sidebar.selectbox("Filing type", filing_type_opts)

    flag_reason_opts = ["(all)", "diff_material", "correlator_anomaly", "both"]
    flag_reason_sel = st.sidebar.selectbox("Flag reason", flag_reason_opts)

    min_materiality = st.sidebar.slider(
        "Min materiality", min_value=0.0, max_value=1.0, value=0.0, step=0.05,
    )
    limit = st.sidebar.slider("Max events", min_value=10, max_value=200, value=50, step=10)

    st.sidebar.divider()
    st.sidebar.markdown("### Status")
    counts = conn.execute(
        "SELECT status, COUNT(*) AS n FROM filings_seen GROUP BY status"
    ).fetchall()
    for r in counts:
        st.sidebar.markdown(f"- `{r['status']}`: {r['n']}")

    # ---- main: list of flagged events -------------------------------------
    events = _flagged_filings(
        conn,
        ticker=ticker_sel if ticker_sel != "(all)" else None,
        filing_type=filing_type_sel if filing_type_sel != "(all)" else None,
        flag_reason=flag_reason_sel if flag_reason_sel != "(all)" else None,
        min_materiality=min_materiality,
        limit=limit,
    )

    if not events:
        st.info(
            "No flagged events match the current filters. "
            "Lower the materiality threshold or clear filters in the sidebar."
        )
        return

    st.markdown(f"**{len(events)} flagged event(s)** — click to expand")
    for event in events:
        _render_event_card(conn, event)


if __name__ == "__main__":
    main()
