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

import datetime
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
    return connect(
        config.storage.db_path, read_only=True, check_same_thread=False,
    )


# ---------------------------------------------------------------------------
# Humanizers
# ---------------------------------------------------------------------------

_FILING_LABELS = {
    "10-K": "Annual report (10-K)",
    "10-Q": "Quarterly report (10-Q)",
    "8-K":  "Material event (8-K)",
    "4":    "Insider transaction (Form 4)",
}

_FLAG_REASON_LABELS = {
    "diff_material":      "Disclosure change",
    "correlator_anomaly": "Unusual insider trading",
    "both":               "Disclosure change + insider trading",
}

_CHANGE_TYPE_LABELS = {
    "addition":     "New content added",
    "removal":      "Content removed",
    "modification": "Content materially modified",
    "restructure":  "Section restructured",
}

_SECTION_LABELS = {
    "mdna":         "Management Discussion & Analysis",
    "risk_factors": "Risk Factors",
    "legal":        "Legal Proceedings",
    "qdmr":         "Quantitative Disclosures",
}


def _humanize_filing_type(t: str | None) -> str:
    return _FILING_LABELS.get(t or "", t or "—")


def _humanize_flag_reason(r: str | None) -> str:
    return _FLAG_REASON_LABELS.get(r or "", r or "—")


def _humanize_section(s: str | None) -> str:
    if not s:
        return "—"
    if s in _SECTION_LABELS:
        return _SECTION_LABELS[s]
    return s.replace("_", " ").title()


def _humanize_topic(t: str) -> str:
    return t.replace("_", " ")


def _humanize_date(iso_str: str | None) -> tuple[str, str]:
    """Return (absolute, relative) like ('Feb 23, 2023', '3 years ago')."""
    if not iso_str:
        return "—", ""
    try:
        date_part = iso_str.split("T")[0]
        dt = datetime.datetime.fromisoformat(date_part)
    except (ValueError, AttributeError):
        return iso_str, ""
    now = datetime.datetime.now()
    delta = (now.date() - dt.date()).days
    if delta < 0:
        rel = "today"
    elif delta == 0:
        rel = "today"
    elif delta < 7:
        rel = f"{delta} day{'s' if delta != 1 else ''} ago"
    elif delta < 60:
        weeks = delta // 7
        rel = f"{weeks} week{'s' if weeks != 1 else ''} ago"
    elif delta < 365:
        months = delta // 30
        rel = f"{months} month{'s' if months != 1 else ''} ago"
    else:
        years = delta // 365
        rel = f"{years} year{'s' if years != 1 else ''} ago"
    abs_str = dt.strftime("%b %d, %Y")
    # strip leading zero on day on Windows ("Feb 03, 2023" -> "Feb 3, 2023")
    abs_str = abs_str.replace(" 0", " ")
    return abs_str, rel


def _severity(materiality: float | None) -> tuple[str, str]:
    """Return (label, css_class). Major / Notable / Minor / Routine."""
    if materiality is None:
        return "Routine", "routine"
    if materiality >= 0.8:
        return "Major", "major"
    if materiality >= 0.6:
        return "Notable", "notable"
    return "Minor", "minor"


# ---------------------------------------------------------------------------
# Data access (unchanged from previous; queries reused as-is)
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


def _edgar_url(accession: str, cik: str) -> str:
    cik_short = str(int(cik))
    accession_nodashes = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_short}/"
        f"{accession_nodashes}/{accession}-index.htm"
    )


# ---------------------------------------------------------------------------
# Data-quality caveat detection
# ---------------------------------------------------------------------------

def _is_known_data_quality_caveat(event: dict, payload: dict | None) -> bool:
    """Detect the documented Phase 1 issuer-name-as-insider bug.

    When a correlator-flagged event's drivers include the issuer's own name
    rather than a person, we know the Form 4 parser picked up an
    issuer-name placeholder as an insider (NOTES.md §11). Phase 2 LLM
    extractor will filter these at the data layer.
    """
    if event.get("flag_reason") != "correlator_anomaly" or not payload:
        return False
    company_name = (event.get("company_name") or "").lower()
    if not company_name:
        return False
    drivers = (payload.get("verdict") or {}).get("drivers") or []
    return any(company_name in (d or "").lower() for d in drivers)


# ---------------------------------------------------------------------------
# Headline synthesis
# ---------------------------------------------------------------------------

def _synthesize_headline(
    event: dict, summaries: list[dict], correlator_payload: dict | None,
) -> str:
    """One-sentence plain-English headline for the card."""
    reason = event.get("flag_reason")

    if reason in ("diff_material", "both") and summaries:
        topics: list[str] = []
        for s in sorted(summaries, key=lambda x: x.get("materiality", 0), reverse=True)[:3]:
            for t in s.get("affected_topics") or []:
                topics.append(_humanize_topic(t))
        # dedup preserving order
        seen: set[str] = set()
        unique_topics: list[str] = []
        for t in topics:
            if t not in seen:
                seen.add(t)
                unique_topics.append(t)
        topic_str = ", ".join(unique_topics[:3])
        n = len(summaries)
        what = "disclosure change" if n == 1 else f"{n} disclosure changes"
        if topic_str:
            return f"{what} flagged — {topic_str}"
        return f"{what} flagged"

    if reason in ("correlator_anomaly", "both") and correlator_payload:
        v = correlator_payload.get("verdict") or {}
        drivers = v.get("drivers") or []
        if drivers:
            return f"Insider trading flagged — {drivers[0][:120]}"
        return "Unusual insider trading pattern flagged"

    return "Event flagged"


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """
<style>
  /* Severity pills */
  .severity-pill {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 4px;
    font-size: 0.72rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    margin-right: 10px;
    vertical-align: middle;
  }
  .severity-major   { background: #fbecec; color: #c0392b; border: 1px solid #e8b5b5; }
  .severity-notable { background: #fdf4e7; color: #b97a0a; border: 1px solid #f0d3a8; }
  .severity-minor   { background: #ecf0f1; color: #5d6d7e; border: 1px solid #cfd8dc; }
  .severity-routine { background: #f4f6f7; color: #7f8c8d; border: 1px solid #d6dbe0; }

  /* Topic chips */
  .topic-chip {
    display: inline-block;
    background: #eef1f5;
    color: #1f2933;
    border: 1px solid #cfd8dc;
    padding: 2px 10px;
    border-radius: 12px;
    font-size: 0.78rem;
    margin: 3px 4px 3px 0;
  }

  /* Caveat banner */
  .caveat-banner {
    background: #fdf4e7;
    color: #5e4422;
    border: 1px solid #f0d3a8;
    padding: 10px 14px;
    border-radius: 4px;
    font-size: 0.86rem;
    margin: 8px 0 14px 0;
    line-height: 1.5;
  }
  .caveat-banner a { color: #1e3a5f; }

  /* Card meta row */
  .meta-row {
    color: #5d6d7e;
    font-size: 0.85rem;
    margin-top: 4px;
    margin-bottom: 6px;
  }
  .meta-row strong { color: #1f2933; }

  /* Tighten header spacing */
  .stMetric { padding-top: 0.25rem; }

  /* Subdued divider */
  hr { border-color: #d6dbe0 !important; }
</style>
"""


def _inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Render: hero
# ---------------------------------------------------------------------------

def _render_hero(conn: sqlite3.Connection) -> None:
    st.markdown("# redline")
    st.markdown(
        "_Scheduled SEC filing monitor. Watches 8 public companies for "
        "substantive disclosure changes and unusual insider-trading patterns._"
    )

    analyzed = conn.execute(
        "SELECT COUNT(*) AS n FROM filings_seen WHERE status IN ('analyzed', 'flagged')"
    ).fetchone()["n"]
    flagged_distinct = conn.execute(
        "SELECT COUNT(DISTINCT accession) AS n FROM flagged_events"
    ).fetchone()["n"]

    cols = st.columns(3)
    cols[0].metric("Companies monitored", "8")
    cols[1].metric("Filings analyzed", analyzed)
    cols[2].metric("Findings", flagged_distinct)

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
            "**Built as a resume artifact for Dec 2026 full-time recruiting.** "
            "Design + locked decisions live in [`CLAUDE.md`](https://github.com/ian-menachery/redline/blob/master/CLAUDE.md) "
            "and [`ARCHITECTURE.md`](https://github.com/ian-menachery/redline/blob/master/ARCHITECTURE.md). "
            "An eval harness scores the system against pre-registered historical events — see the "
            "[`eval-pre-registration-v1`](https://github.com/ian-menachery/redline/releases/tag/eval-pre-registration-v1) tag.\n\n"
            "**Eval scorecard:** 2/3 on the 3 pre-registered events. The one failure is itself "
            "evidence the locked 10b5-1 filter is working — the criterion expected Karp's 2024 "
            "sales to be flagged, but they're 100% plan-driven and the system correctly excluded "
            "them. Full reasoning in [`NOTES.md §11`](https://github.com/ian-menachery/redline/blob/master/NOTES.md#11--eval-findings-phase-1)."
        )


# ---------------------------------------------------------------------------
# Render: sidebar
# ---------------------------------------------------------------------------

def _render_sidebar(conn: sqlite3.Connection) -> dict:
    st.sidebar.markdown("## redline")

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
    company_opts = ["All companies"] + [f"{w['name']} ({w['ticker']})" for w in watchlist]
    company_sel = st.sidebar.selectbox("Company", company_opts)
    selected_ticker = None
    if company_sel != "All companies":
        # "Carvana Co. (CVNA)" -> "CVNA"
        try:
            selected_ticker = company_sel.rsplit("(", 1)[1].rstrip(")")
        except IndexError:
            selected_ticker = None

    filing_type_opts: list[tuple[str, str | None]] = [
        ("All filings", None),
        ("Annual reports", "10-K"),
        ("Quarterly reports", "10-Q"),
        ("Material events", "8-K"),
        ("Insider transactions", "4"),
    ]
    ft_label = st.sidebar.selectbox(
        "Filing type", [o[0] for o in filing_type_opts]
    )
    filing_type_sel = next(o[1] for o in filing_type_opts if o[0] == ft_label)

    flag_opts: list[tuple[str, str | None]] = [
        ("All findings", None),
        ("Disclosure changes", "diff_material"),
        ("Unusual insider trading", "correlator_anomaly"),
    ]
    fl_label = st.sidebar.selectbox(
        "Type of finding", [o[0] for o in flag_opts]
    )
    flag_reason_sel = next(o[1] for o in flag_opts if o[0] == fl_label)

    severity_options: dict[str, float] = {
        "Minor and up": 0.0,
        "Notable and up": 0.6,
        "Major only": 0.8,
    }
    sev_label = st.sidebar.selectbox(
        "Minimum severity", list(severity_options.keys()), index=0
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
        "[Repo on GitHub](https://github.com/ian-menachery/redline) · "
        "[Pre-registration tag](https://github.com/ian-menachery/redline/releases/tag/eval-pre-registration-v1)"
    )

    return {
        "ticker": selected_ticker,
        "filing_type": filing_type_sel,
        "flag_reason": flag_reason_sel,
        "min_materiality": min_materiality,
        "limit": 50,
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

        # Caveat banner for known data-quality issues
        if _is_known_data_quality_caveat(event, payload):
            st.markdown(
                '<div class="caveat-banner">'
                "⚠ <strong>Known data-quality caveat.</strong> "
                "This finding reflects a Phase 1 limitation: the Form 4 parser treats "
                "issuer-name placeholders as if they were insiders. The signal you'd "
                "actually want flagged here (Karp's November 2024 sales) is correctly "
                "filtered by the system because those trades were 100% 10b5-1 plan-driven — "
                "see the locked decision in CLAUDE.md §4.4 and the full pre-registration "
                "story in "
                '<a href="https://github.com/ian-menachery/redline/blob/master/NOTES.md#11--eval-findings-phase-1" target="_blank">NOTES.md §11</a>.'
                "</div>",
                unsafe_allow_html=True,
            )

        # Headline
        st.markdown(f"#### {headline}")

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
                f"[Open this filing on EDGAR ↗]({_edgar_url(accession, event['cik'])})"
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
                    st.markdown(s.get("summary", ""))
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
                ic[0].metric("Anomalous", "Yes" if v.get("anomalous") else "No")
                ic[1].metric(
                    "Confidence",
                    f"{v.get('confidence', 0):.2f}" if v.get("confidence") is not None else "—",
                )
                cluster_size = (payload.get("cluster") or {}).get("max_cluster_size", 0)
                ic[2].metric("Cluster size", cluster_size)
                drivers = v.get("drivers") or []
                if drivers:
                    st.markdown("**Specific signals identified:**")
                    for d in drivers:
                        st.markdown(f"- {d}")

            # Technical detail (nested)
            with st.expander("Technical detail — raw chunks, gate decisions, transactions"):
                stage2 = _diff_results(conn, accession=accession, stage=2)
                if stage2:
                    st.markdown(
                        "**Stage 1 chunks the diff filter saw, and the Stage 2 gate's decision** "
                        f"({len(stage2)} total)"
                    )
                    for row in stage2:
                        decision = (
                            json.loads(row["gate_decision"]) if row["gate_decision"] else {}
                        )
                        badge = (
                            "✓ substantive" if decision.get("substantive") else "× cosmetic"
                        )
                        st.markdown(
                            f"**{_humanize_section(row['section'])}** · {badge} "
                            f"— _{decision.get('reason', '')}_"
                        )
                        cols = st.columns(2)
                        cols[0].markdown("_OLD_")
                        cols[0].text(row["chunk_old"] or "(empty)")
                        cols[1].markdown("_NEW_")
                        cols[1].text(row["chunk_new"] or "(empty)")
                        st.divider()

                if payload:
                    st.markdown("**Correlator raw payload**")
                    st.json(payload, expanded=False)

                txs = _form4_transactions_in_window(
                    conn, cik=event["cik"],
                    center_date=event["filed_at"], window_days=14,
                )
                if txs:
                    st.markdown(
                        f"**Form 4 transactions in ±14d window** ({len(txs)} total)"
                    )
                    st.dataframe(txs, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_SEVERITY_RANK = {"major": 0, "notable": 1, "minor": 2, "routine": 3}


def main() -> None:
    st.set_page_config(
        page_title="redline · SEC filing monitor",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _inject_css()
    conn = _conn()

    _render_hero(conn)
    st.divider()

    filters = _render_sidebar(conn)
    events = _flagged_filings(conn, **filters)

    if not events:
        st.info(
            "No findings match the current filters. "
            "Loosen the severity filter or clear filters in the sidebar."
        )
        return

    # Sort by severity then most-recent flagged_at first.
    # `events` already comes back from SQL ordered by flagged_at DESC, so a
    # stable sort by severity_rank preserves the date order within each band.
    events_sorted = sorted(
        events, key=lambda e: _SEVERITY_RANK[_severity(e["materiality_max"])[1]]
    )

    st.markdown(f"### Findings ({len(events_sorted)})")
    for event in events_sorted:
        _render_finding_card(conn, event)


if __name__ == "__main__":
    main()
