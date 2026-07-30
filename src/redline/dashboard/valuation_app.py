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
import json
import os
import sqlite3

import streamlit as st

from redline.storage.db import connect

# Names the DCF is credible for vs. monitored-only. Fixed here (presentation
# policy), not inferred from data, so nothing off-method can slip into a card.
VALUED = ["VRTX", "ULTA"]  # the DCF-credible names shown as valuation cards
MECHANISM_TICKER = "NET"   # the guidance before/after "mechanism demo" name
# Named module constant, NOT config-model attribute access at import time: this
# read-only app runs on Streamlit Cloud (cached venv), where reaching into a
# freshly-added config field can crash on a stale install. Mirrors the default.
STALE_DAYS = 120  # mirrors ValuationConfig.reference_price_stale_days
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
               v.reference_price_asof AS asof, w.name AS company,
               v.wacc AS wacc, v.terminal_growth AS terminal_growth,
               v.assumptions_json AS assumptions_json,
               v.sensitivity_json AS sensitivity_json
        FROM dcf_valuations v JOIN watchlist w ON w.cik = v.cik
        WHERE w.ticker = ?
        ORDER BY v.id DESC LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    return dict(row) if row else None


def _bank_names(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT ticker, name FROM watchlist WHERE sector = 'financials' ORDER BY ticker")]


def _monitored(conn) -> list[dict]:
    """Non-financial watchlist names that don't get a DCF card (i.e. not in
    VALUED). Derived from the watchlist — not a hand-maintained list — so every
    watchlist company always lands in exactly one bucket (valued / monitored /
    not-modeled) and none can silently drop off the page."""
    ph = ",".join("?" * len(VALUED))
    return [dict(r) for r in conn.execute(
        f"SELECT ticker, name FROM watchlist "
        f"WHERE sector != 'financials' AND ticker NOT IN ({ph}) ORDER BY ticker",
        VALUED)]


def _cik_for_ticker(conn, ticker: str) -> str | None:
    row = conn.execute("SELECT cik FROM watchlist WHERE ticker = ?", (ticker,)).fetchone()
    return row["cik"] if row else None


def _net_mechanism(conn) -> dict | None:
    """The NET guidance event: baseline -> guidance_change, with the input link
    and the triggering 8-K. Illustrates the MECHANISM only (NET's absolute
    valuation is not a target)."""
    cik = _cik_for_ticker(conn, MECHANISM_TICKER)
    if cik is None:
        return None
    before = conn.execute(
        "SELECT per_share_base b FROM dcf_valuations WHERE cik=? "
        "AND run_reason='new_filing' ORDER BY id LIMIT 1", (cik,)).fetchone()
    after = conn.execute(
        "SELECT id, per_share_base b, trigger_accession acc FROM dcf_valuations "
        "WHERE cik=? AND run_reason='guidance_change' ORDER BY id DESC LIMIT 1",
        (cik,)).fetchone()
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


def _fmt_money(v: float | None) -> str:
    """Compact USD magnitude: $12.9B / $845M / $12,345."""
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:,.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:,.1f}M"
    return f"${v:,.0f}"


def _model_detail(v: dict) -> dict | None:
    """Parse the stored assumptions/sensitivity JSON into a render-ready
    structure for the "how this was modeled" view. Pure (no engine import, no
    recompute): the projection + rollup are baked into the snapshot by
    ``revalue`` at valuation time. Returns ``None`` if a snapshot predates the
    baked projection, so the app degrades gracefully rather than crashing."""
    raw = v.get("assumptions_json")
    if not raw:
        return None
    try:
        a = json.loads(raw)
    except (TypeError, ValueError):
        return None
    projection = a.get("projection")
    base_result = a.get("base_result")
    if not projection or not base_result:
        return None  # older snapshot without the baked projection

    sens: dict = {}
    if v.get("sensitivity_json"):
        try:
            sens = json.loads(v["sensitivity_json"])
        except (TypeError, ValueError):
            sens = {}

    horizon = len(projection)
    return {
        "assumptions": {
            "wacc": v.get("wacc") if v.get("wacc") is not None else a.get("wacc"),
            "terminal_growth": (v.get("terminal_growth")
                                if v.get("terminal_growth") is not None
                                else a.get("terminal_growth")),
            "tax_rate": a.get("tax_rate"),
            "horizon": horizon,
            "fiscal_year": a.get("fiscal_year"),
            "base_revenue": a.get("base_revenue"),
            "net_debt": a.get("net_debt"),
            "shares_diluted": a.get("shares_diluted"),
        },
        "projection": projection,
        "base_result": base_result,
        "sensitivity": sens,
        "low_confidence_note": a.get("low_confidence_note"),
    }


def _render_model_detail(d: dict) -> None:
    """Render the read-only "how this was modeled" breakdown from baked data."""
    a = d["assumptions"]
    horizon = a["horizon"]
    st.markdown(
        f"**Assumptions** — WACC {a['wacc']:.1%} · terminal growth "
        f"{a['terminal_growth']:.1%} · {horizon}-year explicit horizon · tax "
        f"{a['tax_rate']:.0%}. Base year FY{a['fiscal_year']}: revenue "
        f"{_fmt_money(a['base_revenue'])}, net debt {_fmt_money(a['net_debt'])}, "
        f"{a['shares_diluted']:,.0f} diluted shares."
    )
    if d.get("low_confidence_note"):
        st.caption(f"Note: {d['low_confidence_note']}")

    st.markdown("**Base-case free-cash-flow projection**")
    st.dataframe(
        [
            {
                "Year": r["year"],
                "Rev growth": f"{r['revenue_growth']:.1%}",
                "Revenue": _fmt_money(r["revenue"]),
                "FCF": _fmt_money(r["fcf"]),
                "PV of FCF": _fmt_money(r["pv"]),
            }
            for r in d["projection"]
        ],
        hide_index=True, use_container_width=True,
    )

    br = d["base_result"]
    st.markdown(
        f"PV of explicit FCFs {_fmt_money(br['pv_explicit'])} + PV of terminal "
        f"value {_fmt_money(br['pv_terminal'])} = enterprise value "
        f"{_fmt_money(br['enterprise_value'])} − net debt = equity "
        f"{_fmt_money(br['equity_value'])} ÷ shares = **${br['per_share']:,.0f}** "
        f"base per share. Terminal value is {br['terminal_value_fraction']:.0%} "
        f"of enterprise value."
    )

    sens = d.get("sensitivity") or {}
    wacc_pts = sens.get("wacc") or []
    growth_pts = sens.get("revenue_growth_shift") or []
    if wacc_pts or growth_pts:
        st.markdown("**Sensitivity** (per-share value)")
        cols = st.columns(2)
        if wacc_pts:
            cols[0].caption("By WACC")
            cols[0].dataframe(
                [{"WACC": f"{w:.1%}", "Per share": f"${ps:,.0f}"} for w, ps in wacc_pts],
                hide_index=True, use_container_width=True,
            )
        if growth_pts:
            cols[1].caption("By revenue-growth shift")
            cols[1].dataframe(
                [{"Growth shift": f"{g:+.1%}", "Per share": f"${ps:,.0f}"}
                 for g, ps in growth_pts],
                hide_index=True, use_container_width=True,
            )
    st.caption("Modeled from the company's own reported cash flows — illustrative, "
               "not a recommendation.")


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
    detail = _model_detail(v)
    if detail:
        with st.expander("How this was modeled"):
            _render_model_detail(detail)


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
    for m in _monitored(conn):
        st.markdown(f"- **{m['ticker']} · {m['name']}** — high-multiple growth "
                    "or negative free cash flow; an FCF-DCF is not the right tool, so it is "
                    "monitored but not valued here.")
    for b in _bank_names(conn):
        st.markdown(f"- **{b['ticker']} · {b['name']}** — not DCF-modeled (financial sector).")

    st.divider()
    st.caption("Read-only demonstration on a curated snapshot. Informational only; not "
               "investment advice.")


if __name__ == "__main__":
    main()
