"""Import smoke tests for the Streamlit dashboards.

The dashboards are exempt from behavior tests (§7), but they still execute
module-level code on import (constants, decorator definitions). A load-time
crash there — e.g. reaching into a config field that a stale deployed env
lacks — otherwise ships green because nothing else imports these modules.
These tests import both so any module-level error fails CI. main()/_conn()
are guarded and not invoked here.
"""
from __future__ import annotations

import importlib


def test_disclosure_dashboard_imports_clean():
    mod = importlib.import_module("redline.dashboard.app")
    assert hasattr(mod, "main")


def test_valuation_dashboard_imports_clean():
    mod = importlib.import_module("redline.dashboard.valuation_app")
    # multipage app: entrypoint + every page render function must be importable
    for name in ("main", "page_overview", "page_valuations",
                 "page_disclosure", "page_methodology"):
        assert callable(getattr(mod, name)), name


def test_dashboard_data_and_ui_import_clean():
    data = importlib.import_module("redline.dashboard.data")
    uimod = importlib.import_module("redline.dashboard.ui")
    assert callable(data._flagged_filings)
    assert callable(uimod.range_bar)
