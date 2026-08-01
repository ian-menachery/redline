"""Tests for shared dashboard formatters (``redline.dashboard.data``)."""
from __future__ import annotations

from redline.dashboard.data import md_escape


def test_md_escape_neutralizes_dollar_latex():
    # Streamlit renders paired '$' as KaTeX; escaping stops amounts garbling.
    assert md_escape("$5B + $6B") == "\\$5B + \\$6B"
    assert md_escape("one $ only") == "one \\$ only"       # lone '$' still escaped (no-op visually)
    assert md_escape("no dollars here") == "no dollars here"
    assert md_escape("") == ""
