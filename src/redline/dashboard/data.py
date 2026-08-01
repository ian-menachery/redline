"""Shared dashboard data layer: read-only queries + pure formatters.

Extracted from the disclosure app so both it and the multipage valuation app's
Disclosure page use one implementation (DRY, unit-testable, no duplication).
Every query takes a ``conn`` first arg and is read-only; the formatters are pure.
No Streamlit, no engine, no config-model access at import time (dashboards run
on Streamlit Cloud with a cached venv — see the stale-install note in NOTES).
"""
from __future__ import annotations

import datetime
import json
import sqlite3

# Presentation constants mirroring the pipeline config defaults (kept literal,
# not read from the config model at import time — Cloud stale-venv guard).
_SEVERITY_MAJOR = 0.8    # mirrors DiffConfig.severity_high -> "Major"
_SEVERITY_NOTABLE = 0.6  # mirrors DiffConfig.materiality_threshold -> "Notable"
_FORM4_WINDOW_DAYS = 14  # mirrors CorrelatorConfig.window_days
_EVENT_LIMIT = 50        # findings shown in the list

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


# --- formatters (pure) -----------------------------------------------------

def md_escape(text: str) -> str:
    """Escape ``$`` so Streamlit markdown does not parse dollar amounts as LaTeX.

    Streamlit renders text between paired ``$`` as KaTeX math, so a string like
    "revenue $12.9B to $14.1B" garbles into stacked math. ``\\$`` renders as a
    literal ``$``; escaping a lone ``$`` is a visual no-op, so this is safe to
    apply to any markdown/caption sink (never ``st.metric``/``st.text`` — those
    don't render KaTeX). Apply to money- or LLM-text-bearing markdown only."""
    return text.replace("$", "\\$")


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
    if delta <= 0:
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
    abs_str = dt.strftime("%b %d, %Y").replace(" 0", " ")
    return abs_str, rel


def _severity(materiality: float | None) -> tuple[str, str]:
    """Return (label, css_class). Major / Notable / Minor / Routine."""
    if materiality is None:
        return "Routine", "routine"
    if materiality >= _SEVERITY_MAJOR:
        return "Major", "major"
    if materiality >= _SEVERITY_NOTABLE:
        return "Notable", "notable"
    return "Minor", "minor"


def _synthesize_headline(
    event: dict, summaries: list[dict], correlator_payload: dict | None,
) -> str:
    """One-sentence plain-English headline for a finding card."""
    reason = event.get("flag_reason")
    if reason in ("diff_material", "both") and summaries:
        topics: list[str] = []
        for s in sorted(summaries, key=lambda x: x.get("materiality", 0), reverse=True)[:3]:
            for t in s.get("affected_topics") or []:
                topics.append(_humanize_topic(t))
        seen: set[str] = set()
        unique_topics: list[str] = []
        for t in topics:
            if t not in seen:
                seen.add(t)
                unique_topics.append(t)
        topic_str = ", ".join(unique_topics[:3])
        n = len(summaries)
        what = "disclosure change" if n == 1 else f"{n} disclosure changes"
        return f"{what} flagged — {topic_str}" if topic_str else f"{what} flagged"
    if reason in ("correlator_anomaly", "both") and correlator_payload:
        v = correlator_payload.get("verdict") or {}
        drivers = v.get("drivers") or []
        if drivers:
            return f"Insider trading flagged — {drivers[0][:120]}"
        return "Unusual insider trading pattern flagged"
    return "Event flagged"


# --- read-only queries (conn first) ----------------------------------------

def _watchlist(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT ticker, name, sector FROM watchlist ORDER BY ticker")]


def _flagged_filings(
    conn: sqlite3.Connection, *, ticker: str | None, filing_type: str | None,
    flag_reason: str | None, min_materiality: float, limit: int,
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
        SELECT fe.id AS event_id, fe.accession, fe.flag_reason, fe.materiality_max,
               fe.flagged_at, fs.filing_type, fs.filed_at, fs.period_end,
               w.ticker, w.name AS company_name, w.sector, fs.cik
        FROM flagged_events fe
        JOIN filings_seen fs ON fs.accession = fe.accession
        JOIN watchlist w     ON w.cik = fs.cik
        WHERE {" AND ".join(where)}
        ORDER BY fe.flagged_at DESC
        LIMIT ?
    """
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def _diff_summaries_for_event(conn: sqlite3.Connection, event_id: int) -> list[dict]:
    row = conn.execute(
        "SELECT diff_summary FROM flagged_events WHERE id = ?", (event_id,)).fetchone()
    if not row or not row["diff_summary"]:
        return []
    return json.loads(row["diff_summary"])


def _diff_results(
    conn: sqlite3.Connection, *, accession: str, stage: int | None = None,
) -> list[dict]:
    if stage is None:
        rows = conn.execute(
            "SELECT * FROM diff_results WHERE accession = ? ORDER BY section, stage, id",
            (accession,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM diff_results WHERE accession = ? AND stage = ? "
            "ORDER BY section, id", (accession, stage)).fetchall()
    return [dict(r) for r in rows]


def _correlator_payload(conn: sqlite3.Connection, event_id: int) -> dict | None:
    row = conn.execute(
        "SELECT correlator_payload FROM flagged_events WHERE id = ?", (event_id,)).fetchone()
    if not row or not row["correlator_payload"]:
        return None
    return json.loads(row["correlator_payload"])


def _form4_transactions_in_window(
    conn: sqlite3.Connection, *, cik: str, center_date: str, window_days: int = 14,
) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT trade_date, insider_name, code, shares, price, is_10b5_1,
               plan_adopted_date, explanation
        FROM form4_transactions
        WHERE cik = ?
          AND trade_date >= date(?, '-{int(window_days)} day')
          AND trade_date <= date(?, '+{int(window_days)} day')
        ORDER BY trade_date, insider_name
        """,
        (cik, center_date, center_date)).fetchall()
    return [dict(r) for r in rows]


def _edgar_url(accession: str, cik: str) -> str:
    cik_short = str(int(cik))
    return (f"https://www.sec.gov/Archives/edgar/data/{cik_short}/"
            f"{accession.replace('-', '')}/{accession}-index.htm")
