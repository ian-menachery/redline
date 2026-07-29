"""Phase 0 — DCF valuation feasibility gate (THROWAWAY diagnostic).

Not shipped, not tested. Answers the two questions that decide the DCF-layer
design before any build (see plan `cheerful-moseying-conway.md` Phase 0):

  1. Narrative hit_rate — across the 8-name watchlist, how often does a
     periodic filing's MD&A carry a DCF-relevant figure (forward guidance,
     share count) that CHANGED vs. its prior-year same-period anchor?
     Decision cutoffs: >=0.40 narrative is centerpiece; 0.15-0.40 thin
     add-on (one figure_type); <0.15 skip narrative, XBRL-only.

  2. XBRL spine reliability — for each CIK, does companyfacts expose the
     FCF-relevant concepts for >=3 consecutive fiscal years? Defines
     `xbrl_clean_ciks`. >=6/8 proceed; 3-5/8 clean-only + manual base for
     the rest; <3/8 is an architectural stop.

The narrative extractor here is a deliberately-rough regex prototype — Phase 0
needs an order-of-magnitude signal, not production precision (Phase 2 builds
the real typed extractor). Over-counting churn slightly is acceptable and
noted.

Outputs: stdout summary + spike/valuation_feasibility.json (gitignored).
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

# How many periodic filings to pull per ticker. The older half serve as
# prior-year anchors for the newer half, so ~half are "evaluable."
FILINGS_PER_TICKER = 8

# MD&A location per filing type (part FIRST — NOTES §5 gotcha).
MDNA_SPEC = {"10-K": ("Part II", "Item 7"), "10-Q": ("Part I", "Item 2")}

# --- Prototype narrative figure extractor (Phase 0 only) --------------------
# LESSON from the first run: a bare "cue-word ... NN%" pattern captures the
# entire results-of-operations section ("expense increased 5.0%"), which is
# backward-looking ACTUALS churn, not forward guidance — 0.844 of pure false
# positives. This stricter version requires a genuine forward-looking cue AND
# rejects the pervasive historical-comparison clause, so the hit_rate reflects
# real forward guidance. Still rough (Phase 2 builds the typed extractor), but
# honest about what it's counting.
# LESSON 2 (perf): running a forward-cue + lazy `[^.]{0,120}?` + figure pattern
# over a raw 300k-char bank MD&A catastrophically backtracks ("for the" recurs
# thousands of times). Fix: split into sentences first, then run cheap
# per-sentence membership checks. O(n), no cross-document backtracking.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.;:])\s+|\n+")
_FWD_RE = re.compile(
    r"expect|anticipat|outlook|guidance|forecast|we\s+target|going\s+forward|"
    r"for\s+(?:fiscal|full[-\s]?year|the\s+(?:remainder|rest|full\s+year))|"
    r"(?:in|for|during)\s+fiscal\s+20\d{2}|full[-\s]?year\s+20\d{2}",
    re.IGNORECASE,
)
_HIST_RE = re.compile(
    r"increased|decreased|declined|grew|rose|fell|"
    r"compared\s+(?:to|with)|months\s+ended|"
    r"(?:prior|year[-\s]ago)\s+(?:year|quarter|period)|year[-\s]over[-\s]year|versus",
    re.IGNORECASE,
)
_DOLLAR_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s?(billion|million|B|M)\b", re.IGNORECASE)
_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")
_SHARES_RE = re.compile(r"(\d{1,3}(?:,\d{3}){1,}|\d{7,})\s+shares\b", re.IGNORECASE)

# XBRL concepts the FCF reconstruction needs (primary tags; the real Phase 1
# mapping carries fallbacks). Presence of a workable subset per year = "clean".
# NOTE: the facts-df `concept` column is namespaced ("us-gaap:Revenues"), so
# candidates carry the prefix (first run's 0/8 was a bare-name mismatch bug).
FCF_CONCEPT_GROUPS = {
    "revenue": [
        "us-gaap:Revenues",
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    ],
    "operating_cash_flow": [
        "us-gaap:NetCashProvidedByUsedInOperatingActivities",
        "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": [
        "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",
        "us-gaap:PaymentsForCapitalImprovements",
        "us-gaap:PaymentsToAcquireProductiveAssets",
    ],
}
MIN_CONSECUTIVE_YEARS = 3


def head(s: str) -> None:
    print(f"\n{'=' * 74}\n{s}\n{'=' * 74}")


def load_watchlist() -> list[dict]:
    with WATCHLIST.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Narrative side
# ---------------------------------------------------------------------------

def _mdna_text(filing) -> str | None:
    spec = MDNA_SPEC.get(filing.form)
    if not spec:
        return None
    try:
        obj = filing.obj()
        text = obj.get_item_with_part(*spec)
    except Exception:
        return None
    if not text:
        return None
    return str(text)


def _extract_figures(text: str) -> dict[str, set[str]]:
    """Sentence-bounded forward-guidance extraction. {figure_type: value tokens}.

    A $ or % figure counts as forward guidance only if its sentence has a
    forward-looking cue AND no backward-looking (results-of-operations) cue —
    the fix for the first run's 0.844 all-churn false-positive rate. Rough
    (Phase 2 builds the typed extractor); honest about what it counts.
    """
    out: dict[str, set[str]] = {
        "forward_guidance_dollar": set(),
        "forward_guidance_pct": set(),
        "shares_outstanding": set(),
    }
    for sent in _SENTENCE_SPLIT_RE.split(text):
        if len(sent) > 600:  # skip pathological unsplit blobs (tables)
            continue
        if "shares" in sent.lower() and "outstanding" in sent.lower():
            for m in _SHARES_RE.finditer(sent):
                out["shares_outstanding"].add(m.group(1).replace(",", ""))
        if not _FWD_RE.search(sent) or _HIST_RE.search(sent):
            continue
        for m in _DOLLAR_RE.finditer(sent):
            num, unit = m.group(1).replace(",", ""), m.group(2).lower()
            out["forward_guidance_dollar"].add(f"{num}{unit[0]}")
        for m in _PCT_RE.finditer(sent):
            out["forward_guidance_pct"].add(m.group(1))
    return out


def _figures_changed(cur: dict[str, set[str]], anchor: dict[str, set[str]]) -> dict[str, bool]:
    """Per figure_type: did the extracted value-set differ from the anchor?"""
    changed = {}
    for k in cur:
        changed[k] = bool(cur[k] and cur[k] != anchor.get(k, set()))
    return changed


def analyze_narrative(ticker: str) -> dict:
    head(f"{ticker} — narrative figure hit-rate")
    co = Company(ticker)
    filings = co.get_filings(form=["10-K", "10-Q"]).head(FILINGS_PER_TICKER)
    # edgartools returns most-recent-first; reverse to chronological.
    rows = list(filings)[::-1]

    # Extract figures per filing, keyed by (form, fiscal period label).
    records: list[dict] = []
    for f in rows:
        text = _mdna_text(f)
        figs = _extract_figures(text) if text else {
            "forward_guidance_dollar": set(),
            "forward_guidance_pct": set(),
            "shares_outstanding": set(),
        }
        records.append({
            "accession": f.accession_no,
            "form": f.form,
            "filing_date": str(f.filing_date),
            "mdna_chars": len(text) if text else 0,
            "figures": figs,
        })

    # Prior-year same-period anchor ~= 4 periodic filings earlier (3 10-Q + 1
    # 10-K per year). Evaluable = filings with such an anchor available.
    evaluable = 0
    hits = 0
    per_filing = []
    for i, rec in enumerate(records):
        anchor_idx = i - 4
        if anchor_idx < 0:
            continue
        evaluable += 1
        changed = _figures_changed(rec["figures"], records[anchor_idx]["figures"])
        is_hit = any(changed.values())
        hits += int(is_hit)
        per_filing.append({
            "accession": rec["accession"],
            "form": rec["form"],
            "filing_date": rec["filing_date"],
            "mdna_chars": rec["mdna_chars"],
            "anchor_accession": records[anchor_idx]["accession"],
            "figures_found": {k: sorted(v) for k, v in rec["figures"].items()},
            "changed": changed,
            "hit": is_hit,
        })

    print(f"  filings pulled: {len(records)}  evaluable (w/ prior-yr anchor): {evaluable}  hits: {hits}")
    for pf in per_filing:
        flags = [k for k, v in pf["changed"].items() if v]
        print(f"    {pf['form']:5} {pf['filing_date']}  chars={pf['mdna_chars']:>7}  "
              f"hit={'Y' if pf['hit'] else '-'}  changed={flags}")

    return {
        "ticker": ticker,
        "filings_pulled": len(records),
        "evaluable": evaluable,
        "hits": hits,
        "per_filing": per_filing,
    }


# ---------------------------------------------------------------------------
# XBRL side
# ---------------------------------------------------------------------------

def analyze_xbrl(ticker: str) -> dict:
    head(f"{ticker} — XBRL FCF-concept coverage")
    co = Company(ticker)
    try:
        df = co.get_facts().to_dataframe()
    except Exception as e:
        print(f"  get_facts FAILED: {type(e).__name__}: {e}")
        return {"ticker": ticker, "clean": False, "error": f"{type(e).__name__}: {e}"}

    # Annual (FY) facts only for the DCF base.
    fy = df[df["fiscal_period"] == "FY"]
    concepts_present = set(fy["concept"].unique())

    # Per group, which fiscal years have >=1 candidate concept populated?
    group_years: dict[str, set[int]] = {}
    for group, candidates in FCF_CONCEPT_GROUPS.items():
        sub = fy[fy["concept"].isin(candidates)]
        years = set(int(y) for y in sub["fiscal_year"].dropna().unique())
        group_years[group] = years
        matched = [c for c in candidates if c in concepts_present]
        print(f"  {group:20} years={sorted(years)}  via={matched}")

    # Years where EVERY group has coverage.
    all_years = set.intersection(*group_years.values()) if group_years else set()
    consecutive = _max_consecutive(sorted(all_years))
    clean = consecutive >= MIN_CONSECUTIVE_YEARS

    # Cross-check: does edgartools' own FCF helper work?
    fcf_helper_ok = False
    try:
        fin = co.get_financials()
        fcf = fin.get_free_cash_flow()
        fcf_helper_ok = fcf is not None
    except Exception as e:
        print(f"  get_free_cash_flow helper FAILED: {type(e).__name__}: {e}")

    print(f"  fully-covered FY years: {sorted(all_years)}  max-consecutive={consecutive}  "
          f"CLEAN={clean}  fcf_helper_ok={fcf_helper_ok}")

    return {
        "ticker": ticker,
        "group_years": {k: sorted(v) for k, v in group_years.items()},
        "fully_covered_years": sorted(all_years),
        "max_consecutive": consecutive,
        "clean": clean,
        "fcf_helper_ok": fcf_helper_ok,
    }


def _max_consecutive(years: list[int]) -> int:
    if not years:
        return 0
    best = run = 1
    for a, b in zip(years, years[1:]):
        run = run + 1 if b == a + 1 else 1
        best = max(best, run)
    return best


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

def main() -> int:
    watchlist = load_watchlist()
    tickers = [w["ticker"] for w in watchlist]

    narrative = {}
    xbrl = {}
    for t in tickers:
        try:
            narrative[t] = analyze_narrative(t)
        except Exception as e:
            print(f"  narrative FAILED for {t}: {type(e).__name__}: {e}")
            narrative[t] = {"ticker": t, "error": f"{type(e).__name__}: {e}",
                            "evaluable": 0, "hits": 0}
        try:
            xbrl[t] = analyze_xbrl(t)
        except Exception as e:
            print(f"  xbrl FAILED for {t}: {type(e).__name__}: {e}")
            xbrl[t] = {"ticker": t, "clean": False, "error": f"{type(e).__name__}: {e}"}

    total_eval = sum(n.get("evaluable", 0) for n in narrative.values())
    total_hits = sum(n.get("hits", 0) for n in narrative.values())
    hit_rate = total_hits / total_eval if total_eval else 0.0
    clean_ciks = [t for t in tickers if xbrl.get(t, {}).get("clean")]

    head("GATE DECISION")
    print(f"  narrative hit_rate = {total_hits}/{total_eval} = {hit_rate:.3f}")
    if hit_rate >= 0.40:
        narrative_branch = "CENTERPIECE (build Phase 2 in full)"
    elif hit_rate >= 0.15:
        narrative_branch = "THIN ADD-ON (single highest-hit figure_type)"
    else:
        narrative_branch = "SKIP Phase 2 (XBRL-only revaluation)"
    print(f"  -> narrative branch: {narrative_branch}")

    print(f"  xbrl_clean_ciks = {len(clean_ciks)}/8 : {clean_ciks}")
    if len(clean_ciks) >= 6:
        xbrl_branch = "PROCEED (XBRL spine as planned)"
    elif len(clean_ciks) >= 3:
        xbrl_branch = "CLEAN-ONLY (+ manual base for the rest)"
    else:
        xbrl_branch = "ARCHITECTURAL STOP (surface to user)"
    print(f"  -> xbrl branch: {xbrl_branch}")

    out = {
        "hit_rate": hit_rate,
        "total_evaluable": total_eval,
        "total_hits": total_hits,
        "narrative_branch": narrative_branch,
        "xbrl_clean_ciks": clean_ciks,
        "xbrl_branch": xbrl_branch,
        "narrative": narrative,
        "xbrl": xbrl,
    }
    out_path = Path(__file__).parent / "valuation_feasibility.json"
    out_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
