"""Phase 0.5 — edgartools targeted verification.

After discover.py established the API surface, this script verifies that
the specific extractions we need for Phase 1 actually work on real filings:

  PLTR Q3 2024 10-Q  → Risk Factors, MD&A, Legal, QDMR section access
  PLTR Q2 2024 10-Q  → same (and pre-stages NOTES.md §1 manual-diff entry)
  PLTR 2024 Form 4 (Karp) → transaction codes, share counts, dates,
                             10b5-1 indicator, plan-adoption free text
  CVNA 2023-07-19 8-K → item-level extraction (debt restructuring)

Output goes to stdout and a structured summary file at spike/findings.json.
Not committed.
"""
from __future__ import annotations

import json
from pathlib import Path

from edgar import Company, set_identity, find as find_filing

set_identity("Redline hatcher.ry@northeastern.edu")

FINDINGS: dict = {"10q": {}, "form4": {}, "8k": {}}


def head(s: str) -> None:
    print(f"\n{'=' * 72}\n{s}\n{'=' * 72}")


def trunc(s: str | None, n: int = 200) -> str:
    if not s:
        return "(empty / None)"
    s = s.strip()
    return s[:n] + (" […]" if len(s) > n else "")


# ---------- 10-Q ----------------------------------------------------------

def verify_10q(accession: str, label: str) -> dict:
    head(f"10-Q  {label}  ({accession})")
    f = find_filing(accession)
    print(f"  filing_date: {f.filing_date}  period_of_report: {f.period_of_report}")
    tenq = f.obj()

    items_attr = getattr(tenq, "items", None)
    print(f"  items: {items_attr}")
    structure = getattr(tenq, "structure", None)
    if structure is not None:
        try:
            print(f"  structure (truncated): {trunc(str(structure), 400)}")
        except Exception as e:
            print(f"  structure err: {e}")

    result = {"accession": accession, "label": label, "sections": {}}

    # Try item-based access for the sections we care about.
    # 10-Q items: Item 1A (Risk Factors), Item 2 (MD&A), Item 1 (Legal — Part II),
    #             Item 3 (QDMR — Part I)
    targets = {
        "mda": ("Item 2", "Part I"),
        "qdmr": ("Item 3", "Part I"),
        "legal": ("Item 1", "Part II"),
        "risk_factors": ("Item 1A", "Part II"),
    }

    for name, (item, part) in targets.items():
        try:
            sec = tenq.get_item_with_part(item, part)
            text = str(sec) if sec is not None else None
            result["sections"][name] = {
                "found": text is not None and len(text) > 0,
                "length": len(text) if text else 0,
                "preview": trunc(text, 200),
            }
            print(f"  {name:14s}: len={len(text) if text else 0}  preview={trunc(text, 120)}")
        except Exception as e:
            result["sections"][name] = {"error": f"{type(e).__name__}: {e}"}
            print(f"  {name:14s}: ERROR {type(e).__name__}: {e}")

    return result


# ---------- Form 4 --------------------------------------------------------

def verify_form4_karp() -> dict:
    head("Form 4  PLTR Karp 2024")
    pltr = Company("PLTR")
    form4s = pltr.get_filings(form="4", filing_date="2024-01-01:2024-12-31")
    print(f"  PLTR Form 4s in 2024: {len(form4s)}")

    karp_filings = []
    for ent in form4s:
        try:
            obj = ent.obj()
            insider = getattr(obj, "insider_name", "")
            if insider and "karp" in insider.lower():
                karp_filings.append((ent, obj))
        except Exception as e:
            print(f"  parse err on {ent.accession_no}: {e}")

    print(f"  Karp Form 4s in 2024: {len(karp_filings)}")
    summary = {"total_2024_form4s": len(form4s), "karp_count": len(karp_filings), "samples": []}

    for ent, obj in karp_filings[:3]:
        print(f"\n  --- {ent.accession_no}  {ent.filing_date}  insider={obj.insider_name}")
        sample = {
            "accession": ent.accession_no,
            "filing_date": str(ent.filing_date),
            "insider_name": obj.insider_name,
            "reporting_owners": str(getattr(obj, "reporting_owners", None))[:200],
            "footnotes_preview": trunc(str(getattr(obj, "footnotes", "")), 300),
            "remarks_preview": trunc(str(getattr(obj, "remarks", "")), 300),
        }
        # Try to extract transactions as a dataframe
        try:
            df = obj.to_dataframe()
            sample["transaction_df_shape"] = getattr(df, "shape", None)
            sample["transaction_columns"] = list(df.columns) if hasattr(df, "columns") else None
            print(f"     to_dataframe shape: {sample['transaction_df_shape']}")
            print(f"     columns: {sample['transaction_columns']}")
            if hasattr(df, "to_dict") and len(df) > 0:
                rows = df.head(5).to_dict(orient="records")
                sample["sample_rows"] = rows
                for r in rows:
                    print(f"       row: {r}")
        except Exception as e:
            sample["df_error"] = f"{type(e).__name__}: {e}"
            print(f"     to_dataframe ERROR: {e}")
        summary["samples"].append(sample)

    return summary


# ---------- 8-K -----------------------------------------------------------

def verify_8k_cvna() -> dict:
    head("8-K  CVNA 2023-07-19 (debt restructuring)")
    # Two filings on 2023-07-19; check both
    cvna = Company("CVNA")
    eights = cvna.get_filings(form="8-K", filing_date="2023-07-19:2023-07-19")
    print(f"  CVNA 8-Ks on 2023-07-19: {len(eights)}")
    summary = {"count": len(eights), "samples": []}

    for ent in eights:
        print(f"\n  --- {ent.accession_no}  {ent.filing_date}")
        obj = ent.obj()
        items = getattr(obj, "items", None)
        sample = {
            "accession": ent.accession_no,
            "filing_date": str(ent.filing_date),
            "items_type": type(items).__name__,
            "items_preview": str(items)[:400] if items is not None else None,
            "has_press_release": getattr(obj, "has_press_release", None),
        }
        print(f"     items: {sample['items_preview']}")
        print(f"     has_press_release: {sample['has_press_release']}")

        # Try exhibits
        try:
            exhibits = obj.get_exhibits()
            sample["exhibits_count"] = len(exhibits) if exhibits else 0
            print(f"     exhibits_count: {sample['exhibits_count']}")
        except Exception as e:
            sample["exhibits_error"] = f"{type(e).__name__}: {e}"

        summary["samples"].append(sample)

    return summary


# ---------- main ----------------------------------------------------------

if __name__ == "__main__":
    # PLTR Q3 2024 10-Q
    FINDINGS["10q"]["q3_2024"] = verify_10q("0001321655-24-000209", "PLTR Q3 2024")

    # Find PLTR Q2 2024 10-Q. Period would be 2024-06-30, filed ~Aug 2024
    pltr = Company("PLTR")
    qtwo = pltr.get_filings(form="10-Q", filing_date="2024-07-01:2024-09-30")
    print(f"\n[PLTR 10-Qs in Q2-filed window]: {len(qtwo)}")
    for f in qtwo:
        print(f"  {f.filing_date}  {f.accession_no}")
    if len(qtwo) > 0:
        # Use the first (should be Q2 2024)
        FINDINGS["10q"]["q2_2024"] = verify_10q(qtwo[0].accession_no, "PLTR Q2 2024")

    FINDINGS["form4"]["karp_2024"] = verify_form4_karp()
    FINDINGS["8k"]["cvna_2023_07_19"] = verify_8k_cvna()

    out = Path(__file__).parent / "findings.json"
    out.write_text(json.dumps(FINDINGS, indent=2, default=str))
    print(f"\nFindings saved → {out}")
