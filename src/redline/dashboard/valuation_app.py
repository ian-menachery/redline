"""Recruiter-facing DCF valuation dashboard (read-only, safe-by-construction).

A ~90-second skim of the Redline DCF valuation layer. STRICTLY read-only: it
opens SQLite with ``query_only=ON``, makes NO LLM calls, performs NO writes, and
needs NO API key — a public link cannot spend money or mutate data. It reads a
committed, curated snapshot (``data/valuation_demo.db`` by default; override with
``REDLINE_DB_PATH``), never the live poller DB.

Tool-fit is the story: DCF valuations are shown only for cash-generative names
where the method applies (VRTX, ULTA). High-multiple growth / turnaround names
(NET, PLTR, MRNA) and the financial-sector names (SCHW, KEY) are monitored but
not DCF-valued — presented as a judgment, not an apology. No buy/sell or
over-/under-valued verdicts anywhere.

Launch: ``streamlit run src/redline/dashboard/valuation_app.py``.
"""
from __future__ import annotations

import datetime
import os
import sqlite3

import streamlit as st

from redline.storage.db import connect

# Names the DCF is credible for vs. monitored-only. Fixed here (presentation
# policy), not inferred from data, so nothing off-method can slip into a card.
VALUED = ["VRTX", "ULTA"]
MONITORED = ["NET", "PLTR", "MRNA"]
STALE_DAYS = 120
DEFAULT_DB = "data/valuation_demo.db"


@st.cache_resource
def _conn() -> sqlite3.Connection:
    db_path = os.environ.get("REDLINE_DB_PATH", DEFAULT_DB)
    return connect(db_path, read_only=True, check_same_thread=False)


# ---------------------------------------------------------------------------
# Read-only data access
# ---------------------------------------------------------------------------

def _latest_valuation(conn, ticker: str) -> dict | None:
    row = conn.execute(
        """
        SELECT v.per_share_bear AS bear, v.per_share_base AS base,
               v.per_share_bull AS bull, v.reference_price AS ref,
               v.reference_price_asof AS asof, w.name AS company
        FROM dcf_valuations v JOIN watchlist w ON w.cik = v.cik
        WHERE w.ticker = ?
        ORDER BY v.id DESC LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    return dict(row) if row else None


def _company_name(conn, ticker: str) -> str:
    row = conn.execute("SELECT name FROM watchlist WHERE ticker = ?", (ticker,)).fetchone()
    return row["name"] if row else ticker


def _bank_names(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT ticker, name FROM watchlist WHERE sector = 'financials' ORDER BY ticker")]


def _net_mechanism(conn) -> dict | None:
    """The NET guidance event: baseline -> guidance_change, with the input link
    and the triggering 8-K. Illustrates the MECHANISM only (NET's absolute
    valuation is not a target)."""
    before = conn.execute(
        "SELECT per_share_base b FROM dcf_valuations WHERE cik='0001477333' "
        "AND run_reason='new_filing' ORDER BY id LIMIT 1").fetchone()
    after = conn.execute(
        "SELECT id, per_share_base b, trigger_accession acc FROM dcf_valuations "
        "WHERE cik='0001477333' AND run_reason='guidance_change' ORDER BY id DESC LIMIT 1").fetchone()
    if not before or not after:
        return None
    link = conn.execute(
        "SELECT input_name, old_value, new_value FROM valuation_input_links "
        "WHERE valuation_id = ? AND input_name='revenue_growth_y1'", (after["id"],)).fetchone()
    filed = conn.execute("SELECT filed_at FROM filings_seen WHERE accession = ?",
                         (after["acc"],)).fetchone()
    return {
        "before": before["b"], "after": after["b"],
        "old_growth": link["old_value"] if link else None,
        "new_growth": link["new_value"] if link else None,
        "accession": after["acc"],
        "filed_at": filed["filed_at"] if filed else None,
    }


def _edgar_url(accession: str) -> str:
    cik_short = "1477333"  # Cloudflare
    return (f"https://www.sec.gov/Archives/edgar/data/{cik_short}/"
            f"{accession.replace('-', '')}/{accession}-index.htm")


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_asof(asof: str | None) -> tuple[str, bool]:
    """Return (display, is_stale)."""
    if not asof:
        return "", False
    try:
        d = datetime.date.fromisoformat(str(asof)[:10])
    except ValueError:
        return str(asof), False
    stale = (datetime.date.today() - d).days > STALE_DAYS
    return d.strftime("%b %d, %Y").replace(" 0", " "), stale


def _valuation_card(v: dict, ticker: str) -> None:
    st.markdown(f"#### {ticker} · {v['company']}")
    cols = st.columns(3)
    cols[0].metric("Conservative", f"${v['bear']:,.0f}")
    cols[1].metric("Base case", f"${v['base']:,.0f}")
    cols[2].metric("Optimistic", f"${v['bull']:,.0f}")
    ref, asof = v.get("ref"), v.get("asof")
    if ref:
        disp, stale = _fmt_asof(asof)
        suffix = f"reference price ${ref:,.2f} (as of {disp})"
        if stale:
            st.caption(f":grey[{suffix} — reference may be stale]")
        else:
            st.caption(f"Estimated value range vs. {suffix}.")
    st.caption("A modeled range from the company's own reported cash flows — not a recommendation.")


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Redline · DCF valuations", layout="wide")
    conn = _conn()

    st.title("Redline — event-driven DCF valuations")
    st.markdown(
        "When a company files with the SEC, Redline rebuilds a discounted-cash-flow "
        "estimate from the numbers **in the filing itself** — reported financials and "
        "stated guidance — and logs how the estimate moved."
    )
    st.info(
        "DCF valuations are shown for **cash-generative businesses where the method "
        "applies**. High-multiple growth, turnaround, and financial-sector names are "
        "**monitored but not DCF-valued** — an FCF-DCF isn't the right tool for them.",
        icon="🎯",
    )
    st.divider()

    # --- Hero: the two DCF-credible valuations ---
    st.subheader("Valuations")
    for ticker in VALUED:
        v = _latest_valuation(conn, ticker)
        if v:
            _valuation_card(v, ticker)
            st.write("")

    st.divider()

    # --- Mechanism demonstration (explicitly labeled; not a NET valuation) ---
    st.subheader("How a filing moves the model")
    m = _net_mechanism(conn)
    if m:
        st.caption("Mechanism demonstration — Cloudflare (NET). Shown to illustrate the "
                   "filing→model pipeline; NET is a high-multiple name and its absolute "
                   "valuation is **not** presented as a target.")
        c1, c2 = st.columns([3, 2])
        with c1:
            st.markdown(
                f"An **8-K earnings release** (filed {m['filed_at']}) raised management's "
                f"full-year revenue guidance. Redline extracted the figure and updated the "
                f"model's year-1 revenue growth "
                f"**{m['old_growth']:.1%} → {m['new_growth']:.1%}**, then recomputed — "
                f"with the change linked to the source filing."
            )
            st.markdown(f"[View the source 8-K on SEC EDGAR]({_edgar_url(m['accession'])})")
        with c2:
            st.metric("Effect on the modeled estimate",
                      f"{(m['after'] / m['before'] - 1):+.1%}")
            st.caption("How much the estimate moved after the filing — an illustrative "
                       "mechanism. No absolute NET valuation is shown; this is not a price target.")

    st.divider()

    # --- Monitored, not valued ---
    st.subheader("Monitored — not DCF-valued")
    for ticker in MONITORED:
        st.markdown(f"- **{ticker} · {_company_name(conn, ticker)}** — high-multiple growth "
                    "or negative free cash flow; an FCF-DCF is not the right tool, so it is "
                    "monitored but not valued here.")
    for b in _bank_names(conn):
        st.markdown(f"- **{b['ticker']} · {b['name']}** — not DCF-modeled (financial sector).")

    st.divider()
    st.caption("Read-only demonstration on a curated snapshot. Informational only; not "
               "investment advice.")


if __name__ == "__main__":
    main()
