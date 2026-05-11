# NOTES.md

Running notebook for gotchas, decisions, and learnings. Maintained as the project evolves. Sections are stable; content within them grows.

## §1 — Known hard constraint: Risk Factors stickiness

Risk Factors sections are notorious for year-over-year copy-paste with minor legal-counsel edits. This is the single biggest risk to the project's headline diff-analysis feature. If the three-stage filter (`ARCHITECTURE.md` §4) doesn't suppress noise effectively, the diff analyzer surfaces mostly junk and the headline feature is hollow.

**Day-0 validation task (Phase 0.5):** before committing to Stage 1 / Stage 2 prompt logic, manually diff PLTR Q2 2024 vs Q3 2024 Risk Factors by eye. Confirm there ARE real changes worth surfacing. If 80%+ of the diff is boilerplate moves and counsel reword, that's a Stage 1 + Stage 2 calibration signal — the noise filter has to be aggressive or the system flags everything.

### Day-0 manual diff — scratchpad

**Filings to compare:**

| Quarter | Accession | Filed | Period end | EDGAR index URL |
|---|---|---|---|---|
| Q3 2024 | `0001321655-24-000209` | 2024-11-05 | 2024-09-30 | https://www.sec.gov/Archives/edgar/data/1321655/000132165524000209/0001321655-24-000209-index.htm |
| Q2 2024 | `0001321655-24-000135` | 2024-08-06 | 2024-06-30 | https://www.sec.gov/Archives/edgar/data/1321655/000132165524000135/0001321655-24-000135-index.htm |

Risk Factors live at **Part II, Item 1A** in each 10-Q. Spike confirmed extraction returns ~298k chars for Q3 2024 and similar order of magnitude for Q2 2024.

**Reading template — fill one row per noticed change. Aim for 15–30 rows total.**

| # | Change (1-line summary) | Substantive? (y/n) | Class (new-risk / reword / moved / boilerplate) | Notes |
|---|---|---|---|---|
| 1 | _(example) Added language about export controls on AI/ML to government customers_ | y | new-risk | Captures real evolving exposure |
| 2 | | | | |
| 3 | | | | |

**Calibration question to answer at the end:** of the rows marked substantive, what fraction would a sensible reader want to see surfaced on a dashboard? That fraction is roughly the materiality bar Stage 3 should target.

## §2 — Form 4 / 10b5-1 quirks

The 10b5-1 plan filter (`ARCHITECTURE.md` §5) has four edge cases worth memorializing:

1. **Form 4 checkbox only exists on filings filed on or after 2023-04-01.** Pre-this date, no structured indicator at all.
2. **The 2023 rule doesn't apply to plans adopted before 2023-02-27.** A pre-Feb-2023 plan still trading in 2024 has the Form 4 checkbox UNCHECKED, even though the trade IS plan-driven. False negative.
3. **Plan-adoption dates for older plans come from free-text "Explanation of Responses."** Format varies wildly. "Sale pursuant to 10b5-1 plan adopted October 12, 2022" is one of many. NLP / regex extraction will be imperfect; document the failure modes below as they surface.
4. **Pre-April-2023 events are accepted as noisier for MVP.** Acknowledged in the README accuracy section. Not a defect — a known limitation of the input data.

Form 144 ingestion (Phase 2, `ROADMAP.md`) would help — Form 144 is required for proposed open-market sales by affiliates and provides another lens on plan vs. discretionary.

## §3 — Form 4 transaction codes that matter

| Code | Meaning | Economic content |
|---|---|---|
| P | Open-market purchase | Meaningful — insider used cash |
| S | Open-market sale | Meaningful — insider received cash |
| A | Grant or award | Administrative — comp event, not insider choice |
| M | Derivative exercise | Administrative — converting options on schedule |
| F | Tax withholding via share surrender | Administrative — auto-triggered on vest |

**Decision rule:** the correlator anomaly score (volume signal in particular) considers only P and S as economically meaningful. A / M / F are excluded from baselines AND from in-window counts. They're recorded in `form4_transactions` for completeness, just not scored.

Why this matters: a CEO with 50 Form 4s per year (mostly F at vest dates) looks like a high-volume trader without filtering. The signal of interest is discretionary buying/selling — P and S only.

Other codes (G gift, D return-to-issuer, etc.) are rare and currently ignored. Revisit if a meaningful eval miss is traced to one.

## §4 — SEC EDGAR fair-access policy

EDGAR's fair-access rules — violation gets HTTP 403 quickly:

- **≤ 10 requests/second**, across the whole IP/process
- **Descriptive User-Agent** required. Format: `<ProjectName> <contact-email>`. Example: `Redline (hatcher.ry@northeastern.edu)`. A generic `Mozilla/5.0` UA gets blocked.
- **Exponential backoff on retry.** 1s, 2s, 4s, … cap at 60s. After 3 failures, log and skip.
- **No commercial scraping at scale.** 10 req/sec is plenty for our 8-ticker watchlist.

`edgartools` may set its UA automatically — verify during Phase 0.5 that it picks up our config or that we override explicitly.

## §5 — edgartools quirks

Placeholder. Filled during Phase 0.5 Day 0 spike. Things to investigate and document here:

- How `edgartools` handles rate limiting (does it back off automatically? respect 429? expose retry hooks?)
- Accession number formats (`0001234567-24-000012` vs `0001234567-24-000012-index.htm`, etc.)
- 10-K vs 10-Q section extraction reliability across filers — KEY's banking-specific sections, CVNA's auto-retail vocabulary, PLTR's tech disclosures
- 8-K item extraction — per-item structure or full text? How are 5.02 (officer departures) and 2.02 (results of operations) labeled?
- Form 4 transaction parsing — are A/M/F codes distinguished from P/S in the structured output, or is post-processing required?
- XBRL handling for QDMR — is structured numerical extraction available, or text-only?

Each finding gets a dated entry below.

### 2026-05-11 — Initial spike findings (edgartools 5.31.0)

**Section access on 10-Q (PLTR Q3 2024, accession `0001321655-24-000209`):**
- `Filing.obj()` returns a `TenQ` object. The `.items` attribute lists items as strings like `'Part II, Item 1A'`, `'Part I, Item 2'`, etc. — 11 items for a typical 10-Q.
- **Correct API for section text:** `tenq.get_item_with_part(part, item)` — **part comes first, then item.** First instinct of `("Item 1A", "Part II")` silently returns `None`; correct call `("Part II", "Item 1A")` returns 297,913 chars of Risk Factors. Reverse-arg failure mode is silent — wrap with a sanity check (`len > 0`) in the Phase 1 parser.
- MD&A is at `("Part I", "Item 2")` → ~50k chars.
- Alternative path: `tenq.document.get_sec_section(...)` or `tenq.document.get_section(...)` exist; not probed further since `get_item_with_part` works.
- `Filing.markdown()` also returns the entire filing as markdown (~526k chars for that 10-Q) — usable as a fallback or for the chunked Stage 1 input.

**Form 4 (PLTR Karp 2024 cluster):**
- `Filing.obj()` returns a `Form4` object. `to_dataframe()` returns a clean per-transaction DataFrame with 13 columns: `Transaction Type`, `Code`, `Description`, `Shares`, `Price`, `Value`, `Date`, `Form`, `Issuer`, `Ticker`, `Insider`, `Position`, `Remaining Shares`.
- Transaction codes seen in the Karp 2024 cluster: `S` (Sale), `M` (Derivative_Sale / Option Exercise), `C` (Conversion). Consistent with `NOTES.md` §3.
- Karp's Nov 2024 selling pattern (from spike sample): on 2024-11-13 alone, exercised + sold ~6.3M shares worth ~$400M. Three sequential Form 4s (Nov 15, Nov 20, Nov 22) cover staggered tranches.
- **10b5-1 indicator is NOT exposed as a structured attribute.** No `Form4` field contains "10b5", "plan", "rule", or "trading" in its name. The raw XML accessible via `Filing.xml()` (23k chars) does not contain XBRL-style tags like `rule10b5_1Flag` or `tradingPlan`.
- **10b5-1 status IS reliably present in `Form4.footnotes`** (a dict-like `Footnotes` object). Karp Nov 15 2024 filing's footnote `F1`: *"This transaction is part of a related series of transactions undertaken on November 13, 2024 pursuant to a preexisting Rule 10b5-1 trading plan, intended to satisfy the affirmative defense conditions of Rule 10b5-1(c), entered into on December 12, 2023."* Plan-adoption date IS extractable from this text.
- **Decision for Phase 1:** detect 10b5-1 via regex on `Form4.footnotes` text. If accuracy is poor in eval, fall back to raw XML parsing of the form's primary document (`Attachments` exposes the `.xml` file). The free-text approach also generalizes to the pre-2023 plan case (`NOTES.md` §2), so a single code path covers both regimes.

**8-K (CVNA 2023-07-19, debt restructuring):**
- Two 8-Ks on that date:
  - `0001193125-23-189188` — items `['Item 1.01', 'Item 7.01', 'Item 9.01']`, 6 exhibits, no press release. Item 1.01 = Entry into Material Definitive Agreement — this is the actual restructuring announcement.
  - `0001690820-23-000218` — items `['Item 2.02', 'Item 9.01']`, 3 exhibits, has press release. Item 2.02 = Results of Operations — likely a preliminary earnings update co-filed.
- `CurrentReport.items` returns a clean list of item-number strings. Works as expected; no surprises.

**Encoding note (Windows):** writing UTF-8 chars (e.g. arrow `→`) to stdout crashes on Windows default cp1252. Force `sys.stdout.reconfigure(encoding="utf-8")` at the top of any script that prints non-ASCII. Phase 1 implication: all CLI tools should set this explicitly.

**Open follow-ups (Phase 0.5 task 4):**
- Form 4 distribution inspection across ~3 months for PLTR + VRTX to inform anomaly score combination formula + `volume_baseline_window` default.
- The raw XML for Karp Nov 2024 Form 4 didn't contain expected XBRL-style 10b5-1 tags — worth confirming on a 2024 Form 4 from a different issuer to rule out a Palantir-specific filer-software quirk.

## §6 — Eval set hard cases worth flagging

A few of the 12 events deserve specific attention because they stress different parts of the system or are particularly diagnostic:

- **MRNA FY2022 → FY2023 (event 7).** Big and obvious. COVID vaccine revenue collapse + risk-factor pivot to pipeline / commercialization. Sanity check — if the diff analyzer misses this, something is broken.
- **ULTA Q1 2024 (event 11).** Subtler. The guidance cut + demand-language change is the kind of thing humans pick up on in a careful read but a Stage 2 gate might dismiss as "minor wording change." High diagnostic value for noise-filter calibration.
- **PLTR Karp 2024 cluster (event 6).** Correlator-only. No diff comparison applies. Pure test of the anomaly score's ability to distinguish a concentrated discretionary selling pattern from baseline insider activity.
- **KEY Q4 2024 8-K (event 2).** Parser + event-detection only — no diff analyzer involvement (8-K is a one-off, no prior comparator). Tests whether the parser correctly extracts the $7B securities sale and the $700M loss from the 8-K body, and whether downstream detection surfaces it.

Half the eval is "big and obvious" (1, 3, 7, 9), half is subtler. If subtle events fail and big ones pass, that's noise-filter calibration. If big ones fail too, the issue is deeper.

## §7 — LLM prompt iteration log

Template per entry:

```
### YYYY-MM-DD — <prompt name> v<n>
- Changed: <what changed in the prompt>
- Eval score before: <per-subsystem + global>
- Eval score after: <per-subsystem + global>
- Cost per call before / after: $X / $Y
- Notes: <why it changed, what was learned>
```

_(empty)_

## §8 — Cost tracking

Weekly running total of LLM spend (from `llm_call_log` aggregation).

Template per entry:

```
### Week of YYYY-MM-DD
- Total: $X.XX
- By model: Haiku $X.XX / Sonnet $X.XX
- By call site: diff_gate / diff_summary / correlator / eval_judge
- Notable: <anomalies, e.g. "Sunday eval run cost $4 — investigate prompt-caching">
```

Budget: $5–15 nominal MVP, $30–50 realistic with iteration. No hard cap, but anything over $10 in a single day warrants a check.

_(empty)_

## §9 — Bug / surprise log

Template per entry:

```
### YYYY-MM-DD — <one-line description>
- What happened: <observation>
- Root cause: <if found>
- Resolution: <if resolved>
- File touched: <pointer>
```

_(empty)_

## §10 — Streamlit + SQLite concurrency

The dashboard and poller run as separate processes against the same SQLite file. This is fine at our scale because:

1. **Poller writes are batched and short.** Each filing → a few INSERTs in one transaction. Sub-second.
2. **Dashboard is strictly read-only.** `PRAGMA query_only=ON` on its connection. Streamlit's frequent rerun (on every interaction) means many short SELECTs.
3. **WAL mode** (`PRAGMA journal_mode=WAL`) allows concurrent reader + writer without blocking.

What would break this:
- Dashboard issuing writes (e.g. a "mark as read" UI feature — would require explicit transaction discipline)
- Long-running transactions in either process (e.g. a backup script holding an exclusive lock)
- Running across multiple machines on a network mount (don't)

If concurrency does become an issue, the failure mode is "database is locked" exceptions, not data corruption. Mitigation: retry-with-backoff in both processes.
