"""Phase 0.5 — edgartools API discovery.

Probes what edgartools exposes for the three filing types we care about.
Run this FIRST to learn the real API surface; then write a tighter
verification script against the discovered API.

Not committed; lives under spike/ for the duration of Phase 0.5.
"""
from __future__ import annotations

import os

from edgar import Company, set_identity

set_identity("Redline hatcher.ry@northeastern.edu")


def heading(s: str) -> None:
    print(f"\n{'=' * 70}\n{s}\n{'=' * 70}")


def show_attrs(obj, name: str) -> None:
    public = [a for a in dir(obj) if not a.startswith("_")]
    print(f"  {name}: {type(obj).__name__}")
    print(f"  public attrs ({len(public)}): {public}")


def probe_pltr_10q():
    heading("PLTR 10-Q discovery")
    pltr = Company("PLTR")
    show_attrs(pltr, "Company('PLTR')")

    filings = pltr.get_filings(form="10-Q")
    show_attrs(filings, "get_filings(form='10-Q')")
    print(f"  total 10-Qs: {len(filings)}")

    latest5 = filings.latest(5) if hasattr(filings, "latest") else list(filings)[:5]
    print(f"  latest 5:")
    for f in latest5:
        print(f"    {getattr(f, 'filing_date', '?')}  {getattr(f, 'accession_no', '?')}")

    if latest5:
        f = latest5[0]
        show_attrs(f, "filings[0]")
        try:
            obj = f.obj()
            show_attrs(obj, "filings[0].obj()")
        except Exception as e:
            print(f"  .obj() raised: {type(e).__name__}: {e}")


def probe_pltr_form4():
    heading("PLTR Form 4 discovery")
    pltr = Company("PLTR")
    form4s = pltr.get_filings(form="4")
    print(f"  total Form 4s: {len(form4s)}")

    latest5 = form4s.latest(5) if hasattr(form4s, "latest") else list(form4s)[:5]
    print(f"  latest 5:")
    for f in latest5:
        print(f"    {getattr(f, 'filing_date', '?')}  {getattr(f, 'accession_no', '?')}")

    if latest5:
        f = latest5[0]
        try:
            obj = f.obj()
            show_attrs(obj, "form4 obj()")
        except Exception as e:
            print(f"  .obj() raised: {type(e).__name__}: {e}")


def probe_cvna_8k():
    heading("CVNA 8-K discovery (July 2023)")
    cvna = Company("CVNA")
    try:
        eights = cvna.get_filings(form="8-K", filing_date="2023-07-01:2023-07-31")
    except TypeError:
        # API may differ
        eights = cvna.get_filings(form="8-K")
    print(f"  total: {len(eights)}")
    sample = eights.latest(5) if hasattr(eights, "latest") else list(eights)[:5]
    for f in sample:
        print(f"    {getattr(f, 'filing_date', '?')}  {getattr(f, 'accession_no', '?')}")
    if sample:
        try:
            obj = sample[0].obj()
            show_attrs(obj, "8-K obj()")
        except Exception as e:
            print(f"  .obj() raised: {type(e).__name__}: {e}")


if __name__ == "__main__":
    print(f"edgartools identity: {os.environ.get('EDGAR_IDENTITY', '(set via set_identity)')}")
    probe_pltr_10q()
    probe_pltr_form4()
    probe_cvna_8k()
