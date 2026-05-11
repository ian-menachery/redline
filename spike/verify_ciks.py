"""Phase 0.5 — verify watchlist tickers resolve to expected CIKs.

edgartools' Company(ticker) does the SEC lookup. We confirm each watchlist
ticker resolves to a CIK matching our pre-recorded value, fetch the canonical
issuer name, and print a YAML-ready table.
"""
from __future__ import annotations

import sys
from edgar import Company, set_identity

set_identity("Redline hatcher.ry@northeastern.edu")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

EXPECTED = [
    ("PLTR", "1321655", "tech"),
    ("NET", "1477333", "tech"),
    ("SCHW", "316709", "financials"),
    ("KEY", "91576", "financials"),
    ("MRNA", "1682852", "healthcare"),
    ("VRTX", "875320", "healthcare"),
    ("CVNA", "1690820", "consumer"),
    ("ULTA", "1403568", "consumer"),
]


def main():
    print(f"{'TICKER':<8}{'EXPECTED_CIK':<14}{'ACTUAL_CIK':<14}{'OK':<5}NAME")
    print("-" * 80)
    results = []
    for ticker, expected_cik, sector in EXPECTED:
        try:
            co = Company(ticker)
            actual_cik = str(co.cik).zfill(10).lstrip("0") or "0"
            name = co.name
            ok = actual_cik == expected_cik
        except Exception as e:
            actual_cik = f"ERR {type(e).__name__}"
            name = ""
            ok = False
        results.append((ticker, expected_cik, actual_cik, ok, name, sector))
        flag = "OK" if ok else "MISMATCH"
        print(f"{ticker:<8}{expected_cik:<14}{actual_cik:<14}{flag:<5} {name}")

    bad = [r for r in results if not r[3]]
    print()
    if bad:
        print(f"⚠  {len(bad)} mismatches — fix before writing watchlist.yaml")
        sys.exit(1)
    else:
        print("All CIKs verified.")

    # Print YAML-ready
    print("\n# watchlist.yaml content:")
    for ticker, expected_cik, actual_cik, ok, name, sector in results:
        # CIK zero-padded to 10 digits
        cik_padded = actual_cik.zfill(10)
        print(f'- cik: "{cik_padded}"')
        print(f"  ticker: {ticker}")
        print(f"  name: {name}")
        print(f"  sector: {sector}")


if __name__ == "__main__":
    main()
