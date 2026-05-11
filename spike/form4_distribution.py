"""Phase 0.5 Step D — Form 4 distribution inspection.

Pulls ~3 months of Form 4s for PLTR (high-volume) and VRTX (steadier),
then computes the distributions that feed Phase 1 design decisions:

  - Transactions per insider per month
  - Transaction-code distribution (P/S vs A/M/F)
  - 10b5-1 footnote-detection success rate (regex hit-rate on .footnotes)
  - Per-insider tx counts
  - Plan-adoption-date extraction success rate

Outputs:
  - stdout summary
  - spike/form4_distribution.json (machine-readable; gitignored)
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from edgar import Company, set_identity

set_identity("Redline hatcher.ry@northeastern.edu")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Window: most recent 3 months from today's date.
# Date arithmetic without datetime — use a string window edgartools understands.
FROM_DATE = "2026-02-11"
TO_DATE = "2026-05-11"

TICKERS = ["PLTR", "VRTX"]

# 10b5-1 detection regexes (run against Form4.footnotes string repr)
TEN_B5_1_RE = re.compile(r"\b10b5[-_]?1\b", re.IGNORECASE)
PLAN_ADOPTED_RE = re.compile(
    r"(?:plan\s+adopted|adopted\s+on|entered\s+into\s+on|pursuant\s+to\s+a\s+(?:preexisting\s+)?(?:rule\s+)?10b5[-_]?1\s+(?:trading\s+)?plan[^.]*?entered\s+into\s+on)\s+"
    r"([A-Z][a-z]+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


def head(s): print(f"\n{'=' * 72}\n{s}\n{'=' * 72}")


def analyze_ticker(ticker: str) -> dict:
    head(f"{ticker} — Form 4s {FROM_DATE} to {TO_DATE}")
    co = Company(ticker)
    filings = co.get_filings(form="4", filing_date=f"{FROM_DATE}:{TO_DATE}")
    print(f"  Form 4 filings in window: {len(filings)}")

    per_insider_tx_counts: dict[str, int] = defaultdict(int)
    per_insider_filing_counts: dict[str, int] = defaultdict(int)
    code_counter: Counter = Counter()
    ten_b5_1_hits = 0
    plan_date_hits = 0
    total_filings_examined = 0
    parse_errors = 0
    samples: list[dict] = []

    for ent in filings:
        try:
            obj = ent.obj()
        except Exception as e:
            parse_errors += 1
            print(f"    parse err on {ent.accession_no}: {type(e).__name__}: {e}")
            continue
        total_filings_examined += 1
        insider = getattr(obj, "insider_name", "<unknown>")
        per_insider_filing_counts[insider] += 1

        # Footnote 10b5-1 detection
        footnote_str = str(getattr(obj, "footnotes", ""))
        if TEN_B5_1_RE.search(footnote_str):
            ten_b5_1_hits += 1
            if PLAN_ADOPTED_RE.search(footnote_str):
                plan_date_hits += 1

        # Transaction-code distribution + per-insider tx count
        try:
            df = obj.to_dataframe()
            if df is not None and len(df) > 0:
                per_insider_tx_counts[insider] += len(df)
                for code in df["Code"].dropna():
                    code_counter[str(code).strip()] += 1
        except Exception as e:
            pass  # filing-level counts captured; tx-level skipped silently

        if len(samples) < 5:
            samples.append({
                "accession": ent.accession_no,
                "filing_date": str(ent.filing_date),
                "insider": insider,
                "footnote_has_10b5_1": bool(TEN_B5_1_RE.search(footnote_str)),
                "footnote_has_plan_date": bool(PLAN_ADOPTED_RE.search(footnote_str)),
                "footnote_preview": footnote_str[:240] + ("..." if len(footnote_str) > 240 else ""),
            })

    print(f"  total filings examined (parse OK): {total_filings_examined}")
    print(f"  parse errors: {parse_errors}")
    print(f"  unique insiders: {len(per_insider_filing_counts)}")
    print(f"  top 5 insiders by filing count: {sorted(per_insider_filing_counts.items(), key=lambda x: -x[1])[:5]}")
    print(f"  top 5 insiders by tx count:     {sorted(per_insider_tx_counts.items(), key=lambda x: -x[1])[:5]}")
    print(f"  transaction-code distribution: {dict(code_counter.most_common())}")
    print(f"  10b5-1 footnote-hit rate: {ten_b5_1_hits}/{total_filings_examined} ({100*ten_b5_1_hits/max(1, total_filings_examined):.1f}%)")
    print(f"  plan-adoption-date extraction rate (given 10b5-1 hit): {plan_date_hits}/{ten_b5_1_hits} ({100*plan_date_hits/max(1, ten_b5_1_hits):.1f}%)")

    # Estimate volume-baseline-window viability
    insiders_with_3plus = sum(1 for n in per_insider_filing_counts.values() if n >= 3)
    print(f"  insiders with >=3 filings in 3mo window: {insiders_with_3plus}/{len(per_insider_filing_counts)} ({100*insiders_with_3plus/max(1, len(per_insider_filing_counts)):.1f}%)")

    return {
        "ticker": ticker,
        "window": f"{FROM_DATE}:{TO_DATE}",
        "total_filings": len(filings),
        "parsed_filings": total_filings_examined,
        "parse_errors": parse_errors,
        "unique_insiders": len(per_insider_filing_counts),
        "per_insider_filing_counts": dict(per_insider_filing_counts),
        "per_insider_tx_counts": dict(per_insider_tx_counts),
        "code_distribution": dict(code_counter),
        "ten_b5_1_hit_rate": ten_b5_1_hits / max(1, total_filings_examined),
        "ten_b5_1_hits": ten_b5_1_hits,
        "plan_date_hits": plan_date_hits,
        "insiders_with_3plus_filings": insiders_with_3plus,
        "samples": samples,
    }


def main():
    out = {"window_from": FROM_DATE, "window_to": TO_DATE, "tickers": {}}
    for t in TICKERS:
        out["tickers"][t] = analyze_ticker(t)

    out_path = Path(__file__).parent / "form4_distribution.json"
    out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
