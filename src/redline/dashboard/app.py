"""Streamlit dashboard for redline.

Read-only against SQLite (PRAGMA query_only=ON). Default view: findings
list sorted by severity then recency. Each finding is a card with a
plain-English headline, topic chips, and a "Show details" expander.
Technical detail (raw chunks, LLM gate decisions, correlator payload,
Form 4 transactions) nests behind a second expander so non-technical
viewers see human-readable summaries first.

Launch: ``streamlit run src/redline/dashboard/app.py``.
"""
from __future__ import annotations

import json
import sqlite3

import streamlit as st

from redline.config import RedlineConfig
from redline.dashboard import ui
from redline.dashboard.data import (
    _CHANGE_TYPE_LABELS,
    _EVENT_LIMIT,
    _FORM4_WINDOW_DAYS,
    _correlator_payload,
    _diff_results,
    _diff_summaries_for_event,
    _edgar_url,
    _flagged_filings,
    _form4_transactions_in_window,
    _humanize_date,
    _humanize_filing_type,
    _humanize_flag_reason,
    _humanize_section,
    _humanize_topic,
    _severity,
    _synthesize_headline,
    _watchlist,
    md_escape,
)
from redline.storage.db import connect

# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

@st.cache_resource
def _conn() -> sqlite3.Connection:
    config = RedlineConfig.from_toml("config/settings.toml")
    return connect(
        config.storage.db_path, read_only=True, check_same_thread=False,
    )


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

def _inject_css() -> None:
    # Shared theme (font + severity pills + chips + spacing) lives in ui.py so
    # both dashboards render as one system. See ui.page_css().
    st.markdown(ui.page_css(), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Render: hero
# ---------------------------------------------------------------------------

def _render_hero(conn: sqlite3.Connection) -> None:
    st.title("Redline")
    st.markdown(
        "_Scheduled SEC filing monitor. Watches 8 public companies for "
        "substantive disclosure changes and unusual insider-trading patterns._"
    )

    total = conn.execute("SELECT COUNT(*) AS n FROM filings_seen").fetchone()["n"]
    parsed_plus = conn.execute(
        "SELECT COUNT(*) AS n FROM filings_seen WHERE status IN ('parsed', 'analyzed', 'flagged')"
    ).fetchone()["n"]
    analyzed = conn.execute(
        "SELECT COUNT(*) AS n FROM filings_seen WHERE status IN ('analyzed', 'flagged')"
    ).fetchone()["n"]
    flagged_distinct = conn.execute(
        "SELECT COUNT(DISTINCT accession) AS n FROM flagged_events"
    ).fetchone()["n"]

    cols = st.columns(3)
    cols[0].metric("Companies monitored", "8",
                   help="Fixed 8-ticker watchlist across four sectors.")
    cols[1].metric("Filings analyzed", analyzed,
                   help="Filings that completed the parse → diff → analyze pipeline.")
    cols[2].metric("Findings", flagged_distinct,
                   help="Distinct filings flagged for a material change or insider anomaly.")

    materialities = [
        r["m"] for r in conn.execute(
            "SELECT materiality_max AS m FROM flagged_events WHERE materiality_max IS NOT NULL")
    ]
    left, right = st.columns(2)
    with left:
        # Cumulative reached-stage counts → a monotonic funnel.
        st.altair_chart(
            ui.pipeline_funnel([
                ("Fetched", total), ("Parsed", parsed_plus),
                ("Analyzed", analyzed), ("Flagged", flagged_distinct),
            ]),
            use_container_width=True,
        )
    with right:
        if materialities:
            st.altair_chart(ui.materiality_hist(materialities), use_container_width=True)

    with st.expander("About this project"):
        st.markdown(
            "A scheduled SEC EDGAR monitoring system for a fixed 8-ticker watchlist. The pipeline:\n\n"
            "1. Polls EDGAR every 15 minutes for new filings.\n"
            "2. Parses 10-K / 10-Q / 8-K / Form 4 disclosures with structured extractors.\n"
            "3. Compares each periodic filing to its prior period via a three-stage diff "
            "filter (deterministic rules → cheap LLM gate → quality LLM summary).\n"
            "4. Joins Form 4 insider transactions to filing events on a ±14-day window, "
            "filtering 10b5-1 plan-driven trades.\n"
            "5. Surfaces flagged events here.\n\n"
            "**Accuracy.** The system has been measured against three historical filing events: "
            "KeyCorp's FY2022 deposit-and-rate-environment disclosures, Carvana's FY2022 "
            "liquidity stress, and Palantir's late-2024 insider-trading pattern. It correctly "
            "surfaced the disclosure shifts at KeyCorp and Carvana. The Palantir case is a "
            "documented design trade-off: every Karp transaction in that window was executed "
            "under a pre-arranged 10b5-1 trading plan, and the system intentionally excludes "
            "plan-driven trades since they're uncorrelated with then-current filings by design."
        )


# ---------------------------------------------------------------------------
# Render: sidebar
# ---------------------------------------------------------------------------

def _render_sidebar(conn: sqlite3.Connection) -> dict:
    st.sidebar.markdown("## Redline")

    # Pipeline status
    total = conn.execute("SELECT COUNT(*) AS n FROM filings_seen").fetchone()["n"]
    analyzed = conn.execute(
        "SELECT COUNT(*) AS n FROM filings_seen WHERE status IN ('analyzed', 'flagged')"
    ).fetchone()["n"]
    flagged = conn.execute(
        "SELECT COUNT(DISTINCT accession) AS n FROM flagged_events"
    ).fetchone()["n"]
    st.sidebar.markdown(
        f"**Pipeline status**\n\n"
        f"- 8 companies monitored\n"
        f"- {analyzed} of {total} filings analyzed\n"
        f"- **{flagged} findings**"
    )
    st.sidebar.divider()

    # Filters
    st.sidebar.markdown("**Filters**")
    watchlist = _watchlist(conn)
    company_opts: list[tuple[str, str | None]] = [("All companies", None)] + [
        (f"{w['name']} ({w['ticker']})", w["ticker"]) for w in watchlist
    ]
    company_label = st.sidebar.selectbox(
        "Company", [o[0] for o in company_opts], key="filter_company",
    )
    selected_ticker = next(o[1] for o in company_opts if o[0] == company_label)

    filing_type_opts: list[tuple[str, str | None]] = [
        ("All filings", None),
        ("Annual reports", "10-K"),
        ("Quarterly reports", "10-Q"),
        ("Material events", "8-K"),
        ("Insider transactions", "4"),
    ]
    ft_label = st.sidebar.selectbox(
        "Filing type", [o[0] for o in filing_type_opts], key="filter_filing_type",
    )
    filing_type_sel = next(o[1] for o in filing_type_opts if o[0] == ft_label)

    flag_opts: list[tuple[str, str | None]] = [
        ("All findings", None),
        ("Disclosure changes", "diff_material"),
        ("Unusual insider trading", "correlator_anomaly"),
    ]
    fl_label = st.sidebar.selectbox(
        "Type of finding", [o[0] for o in flag_opts], key="filter_flag_reason",
    )
    flag_reason_sel = next(o[1] for o in flag_opts if o[0] == fl_label)

    severity_options: dict[str, float] = {
        "Minor and up": 0.0,
        "Notable and up": 0.6,
        "Major only": 0.8,
    }
    sev_label = st.sidebar.selectbox(
        "Minimum severity", list(severity_options.keys()),
        index=0, key="filter_severity",
    )
    min_materiality = severity_options[sev_label]

    st.sidebar.divider()

    with st.sidebar.expander("Glossary"):
        st.markdown(
            "**10-K** — annual report.\n\n"
            "**10-Q** — quarterly report.\n\n"
            "**8-K** — material event filing (acquisitions, executive changes, etc.).\n\n"
            "**Form 4** — insider transaction report (officers/directors buying or selling shares).\n\n"
            "**10b5-1 plan** — pre-arranged trading plan. Plan-driven trades are uncorrelated with "
            "current filings by design; this system filters them out.\n\n"
            "**Severity** — 0–1 importance score from an LLM summary. "
            "Major ≥ 0.8 · Notable 0.6–0.8 · Minor < 0.6.\n\n"
            "**EDGAR** — SEC's electronic filing system. All data here flows in via `edgartools`."
        )

    st.sidebar.divider()
    st.sidebar.caption(
        "[Source on GitHub](https://github.com/ian-menachery/redline)"
    )

    return {
        "ticker": selected_ticker,
        "filing_type": filing_type_sel,
        "flag_reason": flag_reason_sel,
        "min_materiality": min_materiality,
        "limit": _EVENT_LIMIT,
    }


# ---------------------------------------------------------------------------
# Render: finding card
# ---------------------------------------------------------------------------

def _render_finding_card(conn: sqlite3.Connection, event: dict) -> None:
    accession = event["accession"]
    event_id = event["event_id"]

    # Pull related data once
    summaries: list[dict] = []
    if event["flag_reason"] in ("diff_material", "both"):
        summaries = _diff_summaries_for_event(conn, event_id)
    payload: dict | None = None
    if event["flag_reason"] in ("correlator_anomaly", "both"):
        payload = _correlator_payload(conn, event_id)

    severity_label, severity_class = _severity(event["materiality_max"])
    abs_date, rel_date = _humanize_date(event["filed_at"])
    headline = _synthesize_headline(event, summaries, payload)

    # Topic union for the card-level chip strip
    topics: list[str] = []
    for s in summaries:
        for t in s.get("affected_topics") or []:
            topics.append(_humanize_topic(t))
    seen: set[str] = set()
    unique_topics: list[str] = []
    for t in topics:
        if t not in seen:
            seen.add(t)
            unique_topics.append(t)

    with st.container(border=True):
        # Header
        st.markdown(
            f'<span class="severity-pill severity-{severity_class}">{severity_label}</span>'
            f'<strong>{event["company_name"]}</strong>'
            f' &nbsp;·&nbsp; <span style="color:#5d6d7e">{event["ticker"]}'
            f' &nbsp;·&nbsp; {_humanize_filing_type(event["filing_type"])}</span>',
            unsafe_allow_html=True,
        )
        date_line = f"Filed {abs_date}" + (f" · {rel_date}" if rel_date else "")
        period_line = (
            f" &nbsp;·&nbsp; Period {event['period_end']}" if event.get("period_end") else ""
        )
        st.markdown(
            f'<div class="meta-row">{date_line}{period_line} '
            f'&nbsp;·&nbsp; {_humanize_flag_reason(event["flag_reason"])}</div>',
            unsafe_allow_html=True,
        )

        # Headline
        st.markdown(f"#### {md_escape(headline)}")

        # Topic chips
        if unique_topics:
            chips_html = "".join(
                f'<span class="topic-chip">{t}</span>'
                for t in unique_topics[:10]
            )
            st.markdown(chips_html, unsafe_allow_html=True)

        # Details
        with st.expander("Show details"):
            # Filing meta
            mcol = st.columns(3)
            mcol[0].markdown(f"**Company**\n\n{event['company_name']} ({event['ticker']})")
            mcol[1].markdown(f"**Filed**\n\n{abs_date}" + (f" · {rel_date}" if rel_date else ""))
            mcol[2].markdown(f"**Period**\n\n{event.get('period_end') or '—'}")
            st.markdown(
                f"[Open this filing on EDGAR]({_edgar_url(accession, event['cik'])})"
            )

            # What changed
            if summaries:
                st.markdown("#### What changed")
                ranked = sorted(
                    summaries, key=lambda s: s.get("materiality", 0), reverse=True
                )
                for s in ranked:
                    sec_label = _humanize_section(s.get("section"))
                    change_label = _CHANGE_TYPE_LABELS.get(
                        s.get("change_type") or "", s.get("change_type") or "—"
                    )
                    sev_lbl, sev_cls = _severity(s.get("materiality"))
                    st.markdown(
                        f'<span class="severity-pill severity-{sev_cls}">{sev_lbl}</span>'
                        f"**{sec_label}** &nbsp;·&nbsp; "
                        f'<span style="color:#5d6d7e">{change_label}</span>',
                        unsafe_allow_html=True,
                    )
                    st.markdown(md_escape(s.get("summary", "")))
                    sub_topics = s.get("affected_topics") or []
                    if sub_topics:
                        st.markdown(
                            "".join(
                                f'<span class="topic-chip">{_humanize_topic(t)}</span>'
                                for t in sub_topics
                            ),
                            unsafe_allow_html=True,
                        )
                    st.markdown("---")

            # Correlator
            if payload:
                v = payload.get("verdict") or {}
                st.markdown("#### Insider-trading signal")
                ic = st.columns(3)
                ic[0].metric("Anomalous", "Yes" if v.get("anomalous") else "No",
                             help="Whether the Form 4 pattern near this filing was flagged "
                                  "as anomalous after excluding 10b5-1 plan trades.")
                ic[1].metric(
                    "Confidence",
                    f"{v.get('confidence', 0):.0%}" if v.get("confidence") is not None else "—",
                    help="Model confidence in the anomaly assessment.",
                )
                cluster_size = (payload.get("cluster") or {}).get("max_cluster_size", 0)
                ic[2].metric("Largest trade cluster", cluster_size,
                             help="Most insider transactions clustered within the window.")
                drivers = v.get("drivers") or []
                if drivers:
                    st.markdown("**Specific signals identified:**")
                    for d in drivers:
                        st.markdown(f"- {md_escape(d)}")

            # Underlying detail (nested)
            with st.expander("Underlying analysis"):
                stage2 = _diff_results(conn, accession=accession, stage=2)
                if stage2:
                    st.markdown(
                        "**Section-by-section changes reviewed, and why each was kept "
                        f"or set aside** ({len(stage2)} total)"
                    )
                    for row in stage2:
                        decision = (
                            json.loads(row["gate_decision"]) if row["gate_decision"] else {}
                        )
                        verdict = (
                            "Substantive" if decision.get("substantive") else "Cosmetic"
                        )
                        st.markdown(
                            f"**{_humanize_section(row['section'])}** · {verdict} "
                            f"— _{md_escape(decision.get('reason', ''))}_"
                        )
                        cols = st.columns(2)
                        cols[0].markdown("_Previous filing_")
                        cols[0].text(row["chunk_old"] or "(empty)")
                        cols[1].markdown("_This filing_")
                        cols[1].text(row["chunk_new"] or "(empty)")
                        st.divider()

                txs = _form4_transactions_in_window(
                    conn, cik=event["cik"],
                    center_date=event["filed_at"], window_days=_FORM4_WINDOW_DAYS,
                )
                if txs:
                    st.markdown(
                        f"**Insider (Form 4) transactions within ±{_FORM4_WINDOW_DAYS} days** "
                        f"({len(txs)} total)"
                    )
                    st.altair_chart(ui.form4_timeline(txs), use_container_width=True)
                    st.dataframe(
                        [
                            {
                                "Date": t.get("trade_date"),
                                "Insider": t.get("insider_name") or "—",
                                "Side": {"P": "Buy", "S": "Sell"}.get(
                                    str(t.get("code") or ""), str(t.get("code") or "—")),
                                "Shares": f"{float(t.get('shares') or 0):,.0f}",
                                "10b5-1 plan": "Yes" if t.get("is_10b5_1") else "No",
                            }
                            for t in txs
                        ],
                        use_container_width=True, hide_index=True,
                    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {"major": 0, "notable": 1, "minor": 2, "routine": 3}


def main() -> None:
    st.set_page_config(
        page_title="Redline · SEC filing monitor",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _inject_css()
    conn = _conn()

    # Render the sidebar BEFORE the main-area hero. The hero uses
    # ``st.columns`` and ``st.expander``; with Streamlit 1.57.0 those,
    # rendered first, intermittently break downstream sidebar widgets'
    # delta-path identity so their session state stops persisting across
    # reruns. Calling the sidebar first avoids the interaction. Visual
    # layout is unchanged (the sidebar renders left regardless of order).
    filters = _render_sidebar(conn)

    _render_hero(conn)
    st.divider()

    events = _flagged_filings(conn, **filters)

    if not events:
        with st.container(border=True):
            st.markdown(
                "**No findings match the current filters.**\n\n"
                "Try widening the search: lower the minimum severity, switch to "
                "**All companies**, or set the filing type back to **All filings**. "
                "The default view (no filters applied) shows every flagged event."
            )
        return

    # Sort by severity then most-recent flagged_at first.
    # `events` already comes back from SQL ordered by flagged_at DESC, so a
    # stable sort by severity_rank preserves the date order within each band.
    events_sorted = sorted(
        events, key=lambda e: _SEVERITY_RANK[_severity(e["materiality_max"])[1]]
    )

    st.subheader(f"Findings ({len(events_sorted)})")
    for event in events_sorted:
        _render_finding_card(conn, event)


if __name__ == "__main__":
    main()
