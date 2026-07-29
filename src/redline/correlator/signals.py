"""Three insider-trading anomaly signals.

Per ARCHITECTURE.md §5 these are deliberately **not combined** into a single
score in Phase 1. The Phase 0.5 Form 4 distribution spike (NOTES.md §3.1)
showed PLTR-style sparse traders had zero insiders with 3+ historical
trades in a 3-month window, which would make a hand-tuned combination
formula a guess. Instead the orchestrator passes all three raw signals to
the Sonnet/quality-role ``CorrelatorVerdict`` call and lets the LLM combine
them — the LLM's reasoning is what gets graded against eval event #6.

Signal definitions:

- **Cluster:** distinct discretionary-insider count trading in the same
  direction within the ±14d window. Doesn't need historical baseline.

- **Volume:** the insider's in-window total trade value relative to their
  trailing baseline (default 12 months per NOTES.md §3.1 recommendation).
  Returns ``None`` when baseline has < 3 historical trades.

- **Direction flip:** whether the in-window net direction reverses the
  insider's baseline net direction. Same insufficient-baseline guard.

"Discretionary" filters per NOTES.md §3:
- Code must be P (open-market purchase) or S (open-market sale).
- ``is_10b5_1`` must be falsy (0 or NULL). 10b5-1 plan-driven trades are
  by-design uncorrelated with then-current filings.
"""
from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from typing import TypedDict

# Codes considered "economically meaningful" per NOTES.md §3.
DISCRETIONARY_CODES: set[str] = {"P", "S"}


class ClusterSignal(TypedDict):
    """cluster_signal() payload — surfaced in the LLM prompt + correlator_payload."""

    sellers: list[str]
    buyers: list[str]
    max_cluster_size: int
    score: float


class InsiderSignal(TypedDict, total=False):
    """Per-insider volume/direction payload. Keys vary: the abstain case carries
    ``score=None`` + ``reason``; the scored case carries the computed fields."""

    score: float | None
    reason: str
    baseline_n: int
    window_total: float
    baseline_mean: float
    baseline_std: float
    z_score: float
    flipped: bool
    magnitude: float
    baseline_direction: float
    window_direction: float

# Baseline threshold below which volume/direction signals abstain.
MIN_BASELINE_TRADES = 3


@dataclass(frozen=True)
class Trade:
    """Lightweight projection of a form4_transactions row."""

    insider_name: str
    trade_date: str
    code: str
    shares: float
    price: float | None
    is_10b5_1: int | None

    @property
    def is_buy(self) -> bool:
        return self.code == "P"

    @property
    def is_sell(self) -> bool:
        return self.code == "S"

    @property
    def is_discretionary(self) -> bool:
        """True iff economically meaningful AND not 10b5-1 plan-driven."""
        if self.code not in DISCRETIONARY_CODES:
            return False
        # is_10b5_1: 1 = confirmed plan-driven, 0 = confirmed not, NULL = unknown.
        # Phase 1 treats unknown as "not plan-driven" (presumption of discretion);
        # the LLM verdict can second-guess via the footnote text if needed.
        return self.is_10b5_1 != 1

    @property
    def shares_value(self) -> float | None:
        if self.price is None:
            return None
        return self.shares * self.price


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def _trade_from_row(row: sqlite3.Row) -> Trade:
    return Trade(
        insider_name=row["insider_name"],
        trade_date=row["trade_date"],
        code=row["code"],
        shares=float(row["shares"]),
        price=float(row["price"]) if row["price"] is not None else None,
        is_10b5_1=row["is_10b5_1"],
    )


def load_trades_in_window(
    conn: sqlite3.Connection,
    *,
    cik: str,
    center_date: str,
    window_days: int = 14,
) -> list[Trade]:
    """Form 4 transactions for ``cik`` within ±window_days of ``center_date``."""
    rows = conn.execute(
        f"""
        SELECT insider_name, trade_date, code, shares, price, is_10b5_1
        FROM form4_transactions
        WHERE cik = ?
          AND trade_date >= date(?, '-{window_days} day')
          AND trade_date <= date(?, '+{window_days} day')
        ORDER BY trade_date, insider_name
        """,
        (cik, center_date, center_date),
    ).fetchall()
    return [_trade_from_row(r) for r in rows]


def load_insider_baseline(
    conn: sqlite3.Connection,
    *,
    cik: str,
    insider_name: str,
    before_date: str,
    months_back: int = 12,
) -> list[Trade]:
    """Insider's prior discretionary (P/S, non-10b5-1) trades over the baseline
    window. Excludes 10b5-1 trades — we want the discretionary pattern only."""
    rows = conn.execute(
        f"""
        SELECT insider_name, trade_date, code, shares, price, is_10b5_1
        FROM form4_transactions
        WHERE cik = ?
          AND insider_name = ?
          AND trade_date < ?
          AND trade_date >= date(?, '-{months_back} months')
          AND code IN ('P', 'S')
          AND (is_10b5_1 IS NULL OR is_10b5_1 = 0)
        ORDER BY trade_date
        """,
        (cik, insider_name, before_date, before_date),
    ).fetchall()
    return [_trade_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def cluster_signal(trades: list[Trade], *, saturation: float = 3.0) -> ClusterSignal:
    """Multi-insider cluster signal.

    Score: 0 with no cluster, 1.0 once ``saturation`` insiders trade
    same-direction (default 3; the orchestrator supplies
    ``CorrelatorConfig.cluster_saturation``).
    """
    discretionary = [t for t in trades if t.is_discretionary]
    sellers = sorted({t.insider_name for t in discretionary if t.is_sell})
    buyers = sorted({t.insider_name for t in discretionary if t.is_buy})
    max_cluster = max(len(sellers), len(buyers))
    return {
        "sellers": sellers,
        "buyers": buyers,
        "max_cluster_size": max_cluster,
        "score": min(max_cluster / saturation, 1.0),
    }


def _trade_value_total(trades: list[Trade]) -> float:
    """Sum of |shares * price|. Falls back to share count when price missing."""
    total = 0.0
    for t in trades:
        if t.price is not None:
            total += t.shares * t.price
        else:
            total += t.shares  # treat shares as the magnitude when price unknown
    return total


def volume_signal(
    window_trades: list[Trade], baseline_trades: list[Trade],
    *, min_baseline: int = MIN_BASELINE_TRADES, zscore_saturation: float = 2.0,
) -> InsiderSignal:
    """Per-insider volume vs trailing baseline.

    ``window_trades`` should be a single insider's trades in the ±14d window.
    Returns ``None`` score when baseline has < ``min_baseline`` historical
    trades — the signal abstains rather than guess. ``zscore_saturation`` is the
    z-score at which the score saturates to 1.0 (both from ``CorrelatorConfig``).
    """
    if len(baseline_trades) < min_baseline:
        return {"score": None, "reason": "insufficient_baseline",
                "baseline_n": len(baseline_trades)}

    baseline_values = [
        (t.shares * t.price) if t.price is not None else t.shares
        for t in baseline_trades
    ]
    base_mean = statistics.mean(baseline_values)
    base_std = statistics.pstdev(baseline_values) or 1.0  # avoid /0
    window_total = _trade_value_total(window_trades)

    z = (window_total - base_mean) / base_std if base_std else 0.0
    # Score: clamp z-score above 0 to [0, 1]. z>=saturation -> 1.0; below mean -> 0.
    score = max(0.0, min(z / zscore_saturation, 1.0))
    return {
        "score": score,
        "window_total": window_total,
        "baseline_mean": base_mean,
        "baseline_std": base_std,
        "z_score": z,
        "baseline_n": len(baseline_trades),
    }


def direction_flip_signal(
    window_trades: list[Trade], baseline_trades: list[Trade],
    *, min_baseline: int = MIN_BASELINE_TRADES,
) -> InsiderSignal:
    """Did the insider's in-window direction reverse their baseline pattern?

    "Direction" is the sign of (buys - sells) summed by trade value.
    Returns ``None`` score on baseline < ``min_baseline`` (from CorrelatorConfig).
    """
    if len(baseline_trades) < min_baseline:
        return {"score": None, "reason": "insufficient_baseline",
                "baseline_n": len(baseline_trades)}

    def net_direction(trades: list[Trade]) -> float:
        net = 0.0
        for t in trades:
            mag = (t.shares * t.price) if t.price is not None else t.shares
            net += mag if t.is_buy else (-mag if t.is_sell else 0.0)
        return net

    base_dir = net_direction(baseline_trades)
    window_dir = net_direction(window_trades)

    flipped = (base_dir > 0 and window_dir < 0) or (base_dir < 0 and window_dir > 0)
    if not flipped:
        return {"score": 0.0, "flipped": False,
                "baseline_direction": base_dir, "window_direction": window_dir}

    # Magnitude: how big is the window flip relative to the baseline pattern?
    magnitude = abs(window_dir) / max(abs(base_dir), 1.0)
    score = min(magnitude, 1.0)
    return {
        "score": score, "flipped": True, "magnitude": magnitude,
        "baseline_direction": base_dir, "window_direction": window_dir,
    }
