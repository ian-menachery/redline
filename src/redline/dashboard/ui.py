"""Shared dashboard UI: pure Altair chart builders + theme constants.

Every function here is **pure** — it takes plain data and returns an
``alt.Chart``. No Streamlit calls, no DB, no engine import — so the builders are
unit-testable and safe to import into any dashboard/page (Altair ships with
Streamlit, so no new dependency).

Design (per the dataviz method): form chosen by the data's job; **one axis**
(never dual-scale); categorical hues assigned in a fixed, CVD-safe order
(Okabe-Ito) so identity never depends on rank; single-series magnitude uses the
brand navy; recessive grid; tooltips on every mark; a legend whenever ≥2 series.
"""
from __future__ import annotations

from typing import Any

import altair as alt

# --- palette ---------------------------------------------------------------
NAVY = "#1e3a5f"       # brand primary — single-series magnitude
NEUTRAL = "#6b7280"    # reference / baseline marks
GRID = "#e5e7eb"
# Okabe-Ito (colorblind-safe by construction), fixed order for categoricals.
CATEGORICAL = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9"]
_FONT = "Inter, -apple-system, Segoe UI, sans-serif"


def page_css() -> str:
    """Shared page CSS for both dashboards — one font, one severity/chip system,
    consistent spacing. Kept here (a module both apps already import) rather than
    a new module, so adding it needs no ``requirements.txt`` bump on Streamlit
    Cloud. Severity accents are drawn from the same navy/Okabe-Ito family as the
    charts so pills and plots read as one system; ``#eef1f5`` matches the theme's
    ``secondaryBackgroundColor``."""
    return """
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
  html, body, [class*="css"], .stMarkdown, .stMetric, .stDataFrame {
    font-family: 'Inter', -apple-system, 'Segoe UI', sans-serif;
  }
  .severity-pill {
    display: inline-block; padding: 2px 10px; border-radius: 4px;
    font-size: 0.72rem; font-weight: 700; letter-spacing: 0.06em;
    text-transform: uppercase; margin-right: 10px; vertical-align: middle;
  }
  .severity-major   { background: #f7e4e0; color: #b23524; border: 1px solid #e0a99f; }
  .severity-notable { background: #fbf0dd; color: #8a5a08; border: 1px solid #e3c489; }
  .severity-minor   { background: #eef1f5; color: #1e3a5f; border: 1px solid #cfd8dc; }
  .severity-routine { background: #eef1f5; color: #4a5b6a; border: 1px solid #cfd8dc; }
  .topic-chip {
    display: inline-block; background: #eef1f5; color: #1f2933;
    border: 1px solid #cfd8dc; padding: 2px 10px; border-radius: 12px;
    font-size: 0.78rem; margin: 3px 4px 3px 0;
  }
  .meta-row { color: #5d6d7e; font-size: 0.85rem; margin-top: 4px; margin-bottom: 6px; }
  .meta-row strong { color: #1f2933; }
  .stMetric { padding-top: 0.25rem; }
  hr { border-color: #d6dbe0 !important; }
</style>
"""


def _themed(chart: Any, *, height: int = 240) -> alt.Chart:
    """Apply recessive grid / clean axes / consistent type to any chart
    (``Chart`` or ``LayerChart`` — Altair's ``.configure_*`` live on both)."""
    return (
        chart.properties(height=height)
        .configure_view(strokeWidth=0)
        .configure_axis(
            grid=True, gridColor=GRID, gridOpacity=0.7, domain=False, tickSize=0,
            labelColor="#4b5563", titleColor="#374151", labelFont=_FONT, titleFont=_FONT,
        )
        .configure_legend(labelFont=_FONT, titleFont=_FONT, orient="top")
        .configure_title(font=_FONT, fontSize=14, anchor="start", color="#1f2933")
    )


# --- valuation charts ------------------------------------------------------

def range_bar(*, ticker: str, bear: float, base: float, bull: float,
              reference: float | None) -> alt.Chart:
    """Bear→bull modeled range with a base-case tick and a reference-price mark.
    One company, one $/share axis; direct-labeled (no legend needed)."""
    span = alt.Chart(
        alt.Data(values=[{"low": bear, "high": bull, "ticker": ticker}])
    ).mark_bar(size=18, cornerRadius=4, color=NAVY, opacity=0.85).encode(
        x=alt.X("low:Q", title="Value per share ($)", scale=alt.Scale(zero=False)),
        x2="high:Q",
        tooltip=[alt.Tooltip("low:Q", title="Conservative", format="$,.0f"),
                 alt.Tooltip("high:Q", title="Optimistic", format="$,.0f")],
    )
    base_pt = alt.Chart(alt.Data(values=[{"base": base}])).mark_tick(
        thickness=3, size=26, color="#ffffff",
    ).encode(x="base:Q", tooltip=[alt.Tooltip("base:Q", title="Base case", format="$,.0f")])
    layers = [span, base_pt]
    if reference is not None:
        ref = alt.Chart(alt.Data(values=[{"ref": reference}])).mark_point(
            shape="diamond", size=90, filled=True, color=NEUTRAL,
        ).encode(x="ref:Q",
                 tooltip=[alt.Tooltip("ref:Q", title="Reference price", format="$,.2f")])
        layers.append(ref)
    return _themed(alt.layer(*layers).properties(title=f"{ticker} — modeled range vs. reference"),
                   height=120)


def fcf_projection(projection: list[dict]) -> alt.Chart:
    """Per-year free cash flow and its present value. Both are dollars → one
    axis, two series (fixed order FCF, PV) with a legend."""
    rows: list[dict] = []
    for r in projection:
        rows.append({"year": r["year"], "series": "Free cash flow", "value": r["fcf"]})
        rows.append({"year": r["year"], "series": "PV of FCF", "value": r["pv"]})
    chart = alt.Chart(alt.Data(values=rows)).mark_bar(cornerRadius=3).encode(
        x=alt.X("year:O", title="Projection year"),
        xOffset="series:N",
        y=alt.Y("value:Q", title="Amount (USD)"),
        color=alt.Color("series:N", title=None,
                        scale=alt.Scale(domain=["Free cash flow", "PV of FCF"],
                                        range=CATEGORICAL[:2])),
        tooltip=[alt.Tooltip("year:O", title="Year"), "series:N",
                 alt.Tooltip("value:Q", title="Amount", format="$,.0f")],
    )
    return _themed(chart.properties(title="Free-cash-flow projection"))


def ev_split(base_result: dict) -> alt.Chart:
    """Enterprise value = PV of explicit FCFs + PV of terminal value. A single
    stacked bar (two segments, fixed order) showing the terminal-vs-explicit mix."""
    rows = [
        {"part": "Explicit horizon", "value": base_result["pv_explicit"], "o": 0},
        {"part": "Terminal value", "value": base_result["pv_terminal"], "o": 1},
    ]
    chart = alt.Chart(alt.Data(values=rows)).mark_bar(cornerRadius=3).encode(
        x=alt.X("value:Q", stack="normalize", title="Share of enterprise value",
                axis=alt.Axis(format="%")),
        color=alt.Color("part:N", title=None, sort=["Explicit horizon", "Terminal value"],
                        scale=alt.Scale(range=CATEGORICAL[:2])),
        order="o:Q",
        tooltip=["part:N", alt.Tooltip("value:Q", title="PV ($)", format="$,.0f")],
    )
    return _themed(chart.properties(title="Enterprise value: explicit vs. terminal"), height=130)


def sensitivity_tornado(sensitivity: dict, *, base_per_share: float) -> alt.Chart:
    """How per-share value swings across each driver's sweep (WACC, growth). One
    bar per driver from its min→max outcome; a rule marks the base case."""
    rows: list[dict] = []
    labels = {"wacc": "WACC", "revenue_growth_shift": "Revenue growth"}
    for key, pts in (sensitivity or {}).items():
        vals = [ps for _, ps in pts] if pts else []
        if vals:
            rows.append({"driver": labels.get(key, key), "low": min(vals), "high": max(vals)})
    if not rows:
        rows = [{"driver": "(no sensitivity data)", "low": base_per_share, "high": base_per_share}]
    # Band-relative bars (no fixed pixel size, which overflows a compressed band
    # in a narrow column and makes the rows overlap) + generous band padding, and
    # a height that scales with the driver count so rows are always separated.
    bars = alt.Chart(alt.Data(values=rows)).mark_bar(cornerRadius=4, color=NAVY).encode(
        y=alt.Y("driver:N", title=None,
                scale=alt.Scale(paddingInner=0.45, paddingOuter=0.3)),
        x=alt.X("low:Q", title="Value per share ($)", scale=alt.Scale(zero=False)),
        x2="high:Q",
        tooltip=["driver:N", alt.Tooltip("low:Q", format="$,.0f"),
                 alt.Tooltip("high:Q", format="$,.0f")],
    )
    rule = alt.Chart(alt.Data(values=[{"base": base_per_share}])).mark_rule(
        color=NEUTRAL, strokeDash=[4, 3]).encode(x="base:Q")
    return _themed(alt.layer(bars, rule).properties(title="Sensitivity (per-share value)"),
                   height=72 + 54 * len(rows))


# --- disclosure / overview charts ------------------------------------------

def materiality_hist(materialities: list[float]) -> alt.Chart:
    """Distribution of finding materiality scores. Single-series magnitude."""
    rows = [{"m": m} for m in materialities if m is not None]
    chart = alt.Chart(alt.Data(values=rows or [{"m": 0}])).mark_bar(
        color=NAVY, cornerRadius=2, opacity=0.85).encode(
        x=alt.X("m:Q", bin=alt.Bin(maxbins=10), title="Materiality (0–1)"),
        y=alt.Y("count():Q", title="Findings"),
        tooltip=[alt.Tooltip("count():Q", title="Findings")],
    )
    return _themed(chart.properties(title="Finding materiality distribution"), height=200)


def form4_timeline(trades: list[dict]) -> alt.Chart:
    """Form 4 transactions over time, sized by shares, split buy vs. sell.
    Buy/sell are categories (not good/bad) → two fixed categorical hues."""
    rows = []
    for t in trades:
        code = t.get("code")
        rows.append({
            "date": str(t.get("trade_date")),
            "shares": float(t.get("shares") or 0),
            "side": "Buy" if code == "P" else ("Sell" if code == "S" else "Other"),
            "insider": t.get("insider_name", ""),
        })
    chart = alt.Chart(alt.Data(values=rows or [{"date": "", "shares": 0, "side": "Other", "insider": ""}])
                      ).mark_circle(opacity=0.8).encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("shares:Q", title="Shares"),
        size=alt.Size("shares:Q", legend=None),
        color=alt.Color("side:N", title=None,
                        scale=alt.Scale(domain=["Buy", "Sell", "Other"], range=CATEGORICAL[:3])),
        tooltip=["date:T", "side:N", alt.Tooltip("shares:Q", format=",.0f"), "insider:N"],
    )
    return _themed(chart.properties(title="Insider transactions in window"), height=200)


def pipeline_funnel(stage_counts: list[tuple[str, int]]) -> alt.Chart:
    """Filings by pipeline stage (fetched → parsed → analyzed → flagged)."""
    rows = [{"stage": s, "count": c, "o": i} for i, (s, c) in enumerate(stage_counts)]
    chart = alt.Chart(alt.Data(values=rows)).mark_bar(color=NAVY, cornerRadius=3).encode(
        x=alt.X("count:Q", title="Filings"),
        y=alt.Y("stage:N", title=None, sort=alt.SortField("o")),
        tooltip=["stage:N", alt.Tooltip("count:Q", title="Filings")],
    )
    return _themed(chart.properties(title="Pipeline"), height=160)


def watchlist_by_sector(rows: list[dict]) -> alt.Chart:
    """Watchlist company count by sector."""
    data = [{"sector": r.get("sector", "?")} for r in rows]
    chart = alt.Chart(alt.Data(values=data or [{"sector": "?"}])).mark_bar(
        color=NAVY, cornerRadius=3).encode(
        x=alt.X("count():Q", title="Companies"),
        y=alt.Y("sector:N", title=None, sort="-x"),
        tooltip=["sector:N", alt.Tooltip("count():Q", title="Companies")],
    )
    return _themed(chart.properties(title="Watchlist by sector"), height=160)


def chart_to_spec(chart: alt.Chart) -> dict[str, Any]:
    """Compile a chart to its Vega-Lite spec (used by tests to assert structure)."""
    return chart.to_dict()
