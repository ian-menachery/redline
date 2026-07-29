"""Phase 0b — 8-K earnings-exhibit guidance feasibility (THROWAWAY).

The deferred differentiated hook needs forward guidance to actually live in the
8-K EX-99.1 earnings releases (Phase 0 proved it is NOT in the 10-K/10-Q MD&A —
NOTES §6). This gate quantifies, for the 6 DCF names: of the recent item-2.02
("Results of Operations") 8-Ks, how many carry EXTRACTABLE quantitative forward
guidance (a forward-cued sentence with a $/% figure, historical churn excluded)?

Decision rule (fixed before results):
  guidance_rate = releases-with-guidance / item-2.02-releases-examined
  >= 0.50  -> build the 8-K guidance extractor (the real flag->model hook)
  0.20-0.50 -> build narrow (only the names/figure types that hit)
  < 0.20   -> not viable; keep XBRL-only, document the null.

Outputs: stdout + spike/guidance_8k_feasibility.json.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml
from edgar import Company, set_identity

set_identity("Redline menachery.i@northeastern.edu")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WATCHLIST = Path(__file__).resolve().parents[1] / "config" / "watchlist.yaml"
EXCLUDED_SECTORS = {"financials"}
EIGHTKS_PER_TICKER = 8

_SENT = re.compile(r"(?<=[.;:])\s+|\n+|●|•")
_GUID = re.compile(
    r"guidance|outlook|we\s+now\s+(?:expect|anticipate)|expect(?:s|ed|ing)?|anticipat|"
    r"for\s+(?:fiscal|the\s+full[-\s]?year|full[-\s]?year\s+fiscal)|"
    r"full[-\s]?year\s+20\d{2}|fiscal\s+(?:year\s+)?20\d{2}|reaffirm|raising|lowering",
    re.IGNORECASE,
)
_HIST = re.compile(
    r"increased|decreased|declined|grew|rose|fell|compared\s+(?:to|with)|"
    r"(?:three|six|nine|twelve)\s+months\s+ended|prior\s+year|year[-\s]over[-\s]year|"
    r"versus|last\s+year",
    re.IGNORECASE,
)
_FIG = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?|\d{1,3}(?:\.\d+)?\s?%")


def load_dcf_tickers() -> list[str]:
    with WATCHLIST.open(encoding="utf-8") as f:
        wl = yaml.safe_load(f)
    return [w["ticker"] for w in wl if w["sector"] not in EXCLUDED_SECTORS]


def _ex991_text(filing) -> str | None:
    try:
        atts = filing.attachments
    except Exception:
        return None
    for a in atts:
        dt = str(getattr(a, "document_type", "") or "")
        if dt.upper().startswith("EX-99"):
            try:
                t = a.text() if callable(getattr(a, "text", None)) else getattr(a, "text", "")
            except Exception:
                t = None
            if t:
                return str(t)
    return None


def _guidance_hits(text: str) -> list[str]:
    hits = []
    for sent in _SENT.split(text):
        sent = sent.strip()
        if len(sent) > 400 or not sent:
            continue
        if _GUID.search(sent) and _FIG.search(sent) and not _HIST.search(sent):
            hits.append(sent)
    return hits


def analyze(ticker: str) -> dict:
    print(f"\n{'='*72}\n{ticker}\n{'='*72}")
    co = Company(ticker)
    filings = co.get_filings(form="8-K").head(EIGHTKS_PER_TICKER)

    examined = 0
    with_guidance = 0
    samples: list[str] = []
    for f in filings:
        try:
            items = list(getattr(f.obj(), "items", []) or [])
        except Exception:
            items = []
        if not any("2.02" in str(i) for i in items):
            continue
        text = _ex991_text(f)
        if not text:
            print(f"  {f.filing_date} 2.02 but no EX-99.1 text")
            continue
        examined += 1
        hits = _guidance_hits(text)
        has = len(hits) > 0
        with_guidance += int(has)
        print(f"  {f.filing_date} ex99={len(text):>6}c guidance_lines={len(hits)} {'HIT' if has else '-'}")
        for h in hits[:3]:
            print(f"       · {h[:150]}")
        if has and len(samples) < 4:
            samples.append(hits[0][:200])

    rate = with_guidance / examined if examined else 0.0
    print(f"  -> examined={examined} with_guidance={with_guidance} rate={rate:.2f}")
    return {"ticker": ticker, "examined": examined, "with_guidance": with_guidance,
            "rate": rate, "samples": samples}


def main() -> int:
    results = {}
    tot_ex = 0
    tot_hit = 0
    for t in load_dcf_tickers():
        try:
            r = analyze(t)
        except Exception as e:
            print(f"  FAILED {t}: {type(e).__name__}: {e}")
            r = {"ticker": t, "examined": 0, "with_guidance": 0, "rate": 0.0,
                 "error": f"{type(e).__name__}: {e}", "samples": []}
        results[t] = r
        tot_ex += r["examined"]
        tot_hit += r["with_guidance"]

    rate = tot_hit / tot_ex if tot_ex else 0.0
    print(f"\n{'='*72}\nGATE\n{'='*72}")
    print(f"  guidance_rate = {tot_hit}/{tot_ex} = {rate:.3f}")
    if rate >= 0.50:
        branch = "BUILD the 8-K guidance extractor"
    elif rate >= 0.20:
        branch = "BUILD NARROW (only hitting names/figure types)"
    else:
        branch = "NOT VIABLE — keep XBRL-only, document the null"
    print(f"  -> {branch}")

    out = {"guidance_rate": rate, "total_examined": tot_ex, "total_with_guidance": tot_hit,
           "branch": branch, "per_ticker": results}
    (Path(__file__).parent / "guidance_8k_feasibility.json").write_text(
        json.dumps(out, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
