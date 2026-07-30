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
from pathlib import Path

import streamlit as st

from redline.dashboard import data, ui
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
    st.altair_chart(ui.fcf_projection(d["projection"]), use_container_width=True)
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
    st.altair_chart(ui.ev_split(br), use_container_width=True)

    sens = d.get("sensitivity") or {}
    if sens.get("wacc") or sens.get("revenue_growth_shift"):
        st.markdown("**Sensitivity** (per-share value)")
        st.altair_chart(
            ui.sensitivity_tornado(sens, base_per_share=br["per_share"]),
            use_container_width=True,
        )
    st.caption("Modeled from the company's own reported cash flows — illustrative, "
               "not a recommendation.")


def _valuation_card(v: dict, ticker: str) -> None:
    with st.container(border=True):
        st.markdown(f"**{ticker}** · {v['company']}")
        cols = st.columns(3)
        cols[0].metric("Conservative", f"${v['bear']:,.0f}",
                       help="Bear-case per-share estimate (low end of the modeled range).")
        cols[1].metric("Base case", f"${v['base']:,.0f}",
                       help="Central per-share estimate from the base-case assumptions.")
        cols[2].metric("Optimistic", f"${v['bull']:,.0f}",
                       help="Bull-case per-share estimate (high end of the modeled range).")
        ref, asof = v.get("ref"), v.get("asof")
        if ref:
            disp, stale = _fmt_asof(asof)
            suffix = f"reference price ${ref:,.2f} (as of {disp})"
            if stale:
                st.caption(f":grey[{suffix} — reference may be stale]")
            else:
                st.caption(f"Estimated value range vs. {suffix}.")
        st.caption("A modeled range from the company's own reported cash flows — "
                   "not a recommendation.")
        if v.get("bear") is not None:
            st.altair_chart(
                ui.range_bar(ticker=ticker, bear=v["bear"], base=v["base"],
                             bull=v["bull"], reference=v.get("ref")),
                use_container_width=True,
            )
        detail = _model_detail(v)
        if detail:
            with st.expander("How this was modeled"):
                _render_model_detail(detail)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

_EDGAR_APP_URL = "https://redline-edgar.streamlit.app/"


def page_overview() -> None:
    conn = _conn()
    st.title("Redline — event-driven filing analysis")
    st.markdown(
        "_A scheduled SEC EDGAR monitor for a fixed 8-ticker watchlist: it detects "
        "substantive quarter-over-quarter disclosure changes, correlates Form 4 insider "
        "trades against filings, and rebuilds a DCF valuation from the numbers in each "
        "filing — surfacing information, not trade signals._"
    )
    banks = _bank_names(conn)
    monitored = _monitored(conn)
    cols = st.columns(4)
    cols[0].metric("Companies", "8", help="Fixed watchlist size — quality over coverage.")
    cols[1].metric("DCF-valued", len(VALUED),
                   help="Names where an unlevered-FCF DCF is the right tool.")
    cols[2].metric("Monitored", len(monitored),
                   help="Tracked, but an FCF-DCF does not apply — not valued here.")
    cols[3].metric("Not modeled", len(banks),
                   help="Financial-sector names; a levered/DDM model is required, not built.")

    wl = data._watchlist(conn)
    if wl:
        st.altair_chart(ui.watchlist_by_sector(wl), use_container_width=True)

    st.markdown(
        "**How the watchlist is treated.** DCF valuations are shown only for "
        f"cash-generative businesses where the method applies (**{', '.join(VALUED)}**). "
        "High-multiple growth / turnaround names are **monitored, not valued** (an "
        "FCF-DCF is the wrong tool for them), and financial-sector names are **not "
        "DCF-modeled**. That judgment — using the right tool per business — is the point."
    )
    with st.container(border=True):
        st.markdown(
            "**What this is.** Scheduled monitoring and analyst-style revaluation — "
            "not real-time, not a sentiment tool, not an alpha generator. Every number "
            "traces to a filing."
        )
    st.caption(f"Companion disclosure monitor: [{_EDGAR_APP_URL}]({_EDGAR_APP_URL})")


def page_valuations() -> None:
    conn = _conn()
    st.title("DCF valuations")
    st.markdown(
        "When a company files with the SEC, Redline rebuilds a discounted-cash-flow "
        "estimate from the numbers **in the filing itself** — reported financials and "
        "stated guidance — and logs how the estimate moved."
    )
    st.divider()

    for ticker in VALUED:
        v = _latest_valuation(conn, ticker)
        if v:
            _valuation_card(v, ticker)

    st.divider()
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
                      f"{(m['after'] / m['before'] - 1):+.1%}",
                      help="Relative change in the base-case estimate after the filing's "
                           "guidance updated the model's year-1 revenue growth.")
            st.caption("How much the estimate moved after the filing — an illustrative "
                       "mechanism. No absolute NET valuation is shown; this is not a price target.")

    st.divider()
    st.subheader("Monitored — not DCF-valued")
    for mm in _monitored(conn):
        st.markdown(f"- **{mm['ticker']} · {mm['name']}** — high-multiple growth "
                    "or negative free cash flow; an FCF-DCF is not the right tool, so it is "
                    "monitored but not valued here.")
    for b in _bank_names(conn):
        st.markdown(f"- **{b['ticker']} · {b['name']}** — not DCF-modeled (financial sector).")
    st.caption("Read-only demonstration on a curated snapshot. Informational only; not "
               "investment advice.")


def page_disclosure() -> None:
    conn = _conn()
    st.title("Disclosure monitor")
    st.markdown(
        "Substantive quarter-over-quarter changes in 10-K / 10-Q disclosures (via a "
        "three-stage diff filter) and unusual Form 4 insider trading, joined on a "
        "±14-day window."
    )
    findings = data._flagged_filings(
        conn, ticker=None, filing_type=None, flag_reason=None,
        min_materiality=0.0, limit=data._EVENT_LIMIT,
    )
    if not findings:
        st.markdown(
            f"The full interactive disclosure monitor runs as a dedicated app: "
            f"[{_EDGAR_APP_URL}]({_EDGAR_APP_URL})."
        )
        return
    mats = [f["materiality_max"] for f in findings if f["materiality_max"] is not None]
    if mats:
        st.altair_chart(ui.materiality_hist(mats), use_container_width=True)
    for f in findings:
        summaries = data._diff_summaries_for_event(conn, f["event_id"])
        payload = data._correlator_payload(conn, f["event_id"])
        headline = data._synthesize_headline(f, summaries, payload)
        sev, _ = data._severity(f["materiality_max"])
        disp, _stale = data._humanize_date(f.get("flagged_at"))
        st.markdown(
            f"**{f['ticker']} · {data._humanize_filing_type(f['filing_type'])}** — "
            f"{headline}  \n_{sev} · {disp} · "
            f"{data._humanize_flag_reason(f['flag_reason'])}_"
        )
    st.caption(f"Full interactive monitor: [{_EDGAR_APP_URL}]({_EDGAR_APP_URL}).")


def page_methodology() -> None:
    st.title("Methodology & eval")
    st.markdown(
        "**Pipeline.** Poll EDGAR (15-min cadence) → parse 10-K/10-Q/8-K/Form 4 → "
        "three-stage diff filter (deterministic rules → cheap-LLM gate → quality-LLM "
        "summary) → Form 4 correlator (10b5-1 plan trades excluded) → event-driven DCF "
        "revaluation from XBRL financials + 8-K guidance. Read-only dashboards over a "
        "curated SQLite snapshot; every LLM call is logged and Pydantic-validated.\n\n"
        "**Honest framing.** Scheduled monitoring, not real-time; information surfacing, "
        "not trade signals. DCF covers only names where unlevered-FCF DCF applies."
    )
    st.divider()
    st.subheader("Eval results")
    eval_md = Path("EVAL.md")
    if eval_md.exists():
        st.markdown(eval_md.read_text(encoding="utf-8"))
    else:
        st.caption("EVAL.md not found in this deployment.")


def main() -> None:
    st.set_page_config(page_title="Redline · filing analysis", layout="wide",
                       initial_sidebar_state="expanded")
    st.markdown(ui.page_css(), unsafe_allow_html=True)
    nav = st.navigation([
        st.Page(page_overview, title="Overview", default=True),
        st.Page(page_valuations, title="Valuations"),
        st.Page(page_disclosure, title="Disclosure monitor"),
        st.Page(page_methodology, title="Methodology & eval"),
    ])
    nav.run()


if __name__ == "__main__":
    main()
