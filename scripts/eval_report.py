"""Thin CLI shim — see ``redline.eval.report`` for the implementation.

    python scripts/eval_report.py            # equivalent to
    python -m redline.eval.report
"""
from __future__ import annotations

from redline.eval.report import main

if __name__ == "__main__":
    raise SystemExit(main())
