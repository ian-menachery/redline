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
    assert hasattr(mod, "main")
