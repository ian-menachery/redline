"""Tests for the pure Altair chart builders (``redline.dashboard.ui``).

Each builder must produce a valid Vega-Lite spec from representative data and
degrade gracefully on empty input (dashboards render live from snapshots that
may be sparse). We assert the spec compiles, not pixels.
"""
from __future__ import annotations

from redline.dashboard import ui

_PROJ = [
    {"year": i, "revenue_growth": 0.08, "revenue": 1.1e10, "ebit": 3.3e9,
     "nopat": 2.6e9, "fcf": 3.1e9, "pv": 2.9e9}
    for i in range(1, 6)
]
_BASE_RESULT = {"pv_explicit": 5.9e9, "pv_terminal": 1.24e11, "enterprise_value": 1.3e11,
                "equity_value": 1.25e11, "per_share": 483.0, "terminal_value_fraction": 0.95}
_SENS = {"wacc": [[0.04, 620.0], [0.06, 483.0], [0.08, 360.0]],
         "revenue_growth_shift": [[-0.02, 410.0], [0.0, 483.0], [0.02, 560.0]]}


def _ok(chart) -> None:
    spec = ui.chart_to_spec(chart)
    assert isinstance(spec, dict) and ("mark" in spec or "layer" in spec or "encoding" in spec)


def test_range_bar_with_and_without_reference():
    _ok(ui.range_bar(ticker="VRTX", bear=352, base=483, bull=592, reference=490.39))
    _ok(ui.range_bar(ticker="X", bear=1.0, base=2.0, bull=3.0, reference=None))


def test_fcf_projection():
    _ok(ui.fcf_projection(_PROJ))


def test_ev_split():
    _ok(ui.ev_split(_BASE_RESULT))


def test_sensitivity_tornado_and_empty():
    _ok(ui.sensitivity_tornado(_SENS, base_per_share=483.0))
    _ok(ui.sensitivity_tornado({}, base_per_share=483.0))


def test_sensitivity_tornado_layout_scales_and_pads():
    # Regression: at a fixed 140px with a 20px bar the two rows overlapped in a
    # narrow column. Height must scale with driver count and bars be band-padded.
    spec = ui.chart_to_spec(ui.sensitivity_tornado(_SENS, base_per_share=483.0))
    assert spec.get("height", 0) >= 170          # 2 drivers -> 180 (was 140)
    assert "paddingInner" in str(spec)            # band padding so rows separate


def test_ev_split_height_not_cramped():
    spec = ui.chart_to_spec(ui.ev_split(_BASE_RESULT))
    assert spec.get("height", 0) >= 120           # was a cramped 90


def test_materiality_hist_and_empty():
    _ok(ui.materiality_hist([0.9, 0.6, 0.8, 0.3]))
    _ok(ui.materiality_hist([]))


def test_form4_timeline_and_empty():
    _ok(ui.form4_timeline([
        {"trade_date": "2026-05-01", "shares": 1000, "code": "S", "insider_name": "K"},
        {"trade_date": "2026-05-03", "shares": 500, "code": "P", "insider_name": "L"},
    ]))
    _ok(ui.form4_timeline([]))


def test_pipeline_funnel():
    _ok(ui.pipeline_funnel([("Fetched", 8), ("Parsed", 8), ("Analyzed", 6), ("Flagged", 3)]))


def test_page_css_is_style_block():
    css = ui.page_css()
    assert css.strip().startswith("<style>") and css.strip().endswith("</style>")
    # one shared severity/chip system + the chart font, so both apps read alike
    for token in ("severity-major", "severity-notable", "topic-chip", "Inter"):
        assert token in css


def test_chart_unit_labels_present():
    # units/scale must be explicit, not bare "$"
    spec = ui.chart_to_spec(ui.fcf_projection(_PROJ))
    assert "Amount (USD)" in str(spec)
    assert "Materiality (0" in str(ui.chart_to_spec(ui.materiality_hist([0.5])))


def test_watchlist_by_sector_and_empty():
    _ok(ui.watchlist_by_sector([{"sector": "tech"}, {"sector": "tech"}, {"sector": "financials"}]))
    _ok(ui.watchlist_by_sector([]))
