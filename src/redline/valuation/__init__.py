"""DCF valuation layer (Subsystem 7).

Event-driven revaluation: build a per-company DCF, refresh it from the XBRL
financial statements every quarter, and re-value when a filing carries a real
numeric change into a model input. Outputs bear/base/bull ranges, never point
estimates. See plan `cheerful-moseying-conway.md` and ARCHITECTURE.md.

Framing (CLAUDE.md §4): information surfacing / analyst-style revaluation, not
alpha generation. No buy/sell/signal language in this package.
"""
