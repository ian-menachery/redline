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

### Verdict — PLTR Q2-vs-Q3 2024 (2026-05-11)

Locked decision #3 (Risk Factors stickiness, `CLAUDE.md` §4.3) **fully confirmed**. The expanded Risk Factors body between consecutive PLTR 10-Qs is ~99% identical. The Risk Factor Summary bullets are byte-identical. The expanded body differs only in:

- Date references rolled forward
- Dollar amounts, percentages, and headcount updated inside otherwise identical sentences
- One ~12-word parenthetical expansion in the AI-development risk (added "generative AI" and "operationalize" language)

**No new risk categories. No removed risk categories. No materially rewritten paragraphs.**

**Implication for Stage 1** (propagated into `ARCHITECTURE.md` §4): the rule filter must (a) raise the min-substantive-change threshold from the default 10 words to a candidate 22 (range 20–25), and (b) normalize dates, currency amounts, percentages, and integers to canonical tokens BEFORE computing the diff. Without normalization, Stage 1 would pass ~50 cosmetic changes per filing to Stage 2 and burn Haiku budget for nothing.

**Caveat — this is the easy case.** 10-Q Item 1A is defined as a delta from the prior 10-K, so signal density between consecutive 10-Qs is low by construction. The real stress test is 10-K-vs-10-K. Sitting 2 of Phase 0.5 adds a 30-min PLTR FY22 vs FY23 spike (Step C) — substantive examples for the Stage 2 few-shot block must come from that pair, not this one.

**Not a problem for the eval.** Of the 12 pre-registered events, the diff_analyzer-tagged ones (1, 3, 5, 7, 8, 9, 10, 11, 12) all involve periods where real business events occurred (banking stress, debt restructuring, AIP launch, regulatory approval, guidance cut, etc.). These should produce real substantive changes, distinguishable from the cosmetic noise documented here.

### 10-K Risk Factors spike — PLTR FY22 vs FY23 (2026-05-11)

Fetched and diffed by `spike/pltr_10k_riskdiff_spike.py`. 595 → 605 paragraphs. 117 raw paragraph-diff changes; 85 surviving after Stage 1 normalization + 22-word threshold. Gated by an exploratory Stage 2 simulation (OpenAI gpt-4o-mini via `spike/stage2_dryrun.py` — Phase 0.5 only; Phase 1 uses Anthropic Haiku per `ARCHITECTURE.md` §9). Gated output at `spike/pltr_10k_riskdiff_gated.md`.

**Normalization-effectiveness finding (cross-reference to `ARCHITECTURE.md` §4):** canonical-token normalization eliminated only **3 of 117** changes in the 10-K-vs-10-K case, vs. the ~50 expected from the Q2-vs-Q3 finding. 10-K-vs-10-K is signal-dominated; 10-Q-vs-10-Q is noise-dominated. The normalization pre-step is still architecturally correct (catches real false positives at zero LLM cost), but its effectiveness varies sharply by filing-type pair. Worth keeping in mind for per-filing-type cost projections in Phase 1.

**Over-flagging observation (Stage 2 prompt design implication):** the exploratory gpt-4o-mini gate flagged **48/85 (56%) as substantive**. Two probable causes: (a) the draft prompt's "substantive" bar is looser than production should be; (b) PLTR FY22→FY23 genuinely has unusually high signal density (AIP launch, AI-risk expansion, Share Repurchase Program, Israel/Hamas addition). Both are true. The Phase 1 Anthropic Haiku prompt must define "substantive" more precisely than this draft — the few-shot examples below anchor it.

**Stage 2 few-shot examples — POSITIVES (substantive; should pass the gate):**

1. **Chunk 10** (`replace`) — Risk Factors body adds explicit reference to *"Artificial Intelligence Platform (AIP)"* as a new product offering. Canonical "new product/platform" example. *This is the upstream story for eval event #6 (PLTR Karp insider sales around the AIP launch).*
2. **Chunk 11** (`replace`) — new risk bullet: *"reluctance of customers to purchase products incorporating generative AI."* Canonical "new risk topic" example.
3. **Chunk 14** (`insert`) — wholly new paragraph on hybrid / remote-workforce risk. Canonical "new explanatory paragraph" example.
4. **Chunk 58** (`delete`) — risk category around NOLs and tax credits removed entirely. Canonical "risk REMOVED" example — the negative-space case the gate must handle.
5. **Chunk 56** (`replace`) — new risk language around the Inflation Reduction Act and stock-buyback excise tax. Canonical "new regulatory exposure" example.

**Stage 2 few-shot examples — NEGATIVES (cosmetic; should NOT pass even though they survive the 22-word threshold):**

1. **Chunk 5** (`replace`) — top-3 customer concentration ratios update (17%/18%/5yrs → 18%/17%/8yrs). Pure number rolls inside otherwise identical sentences.
2. **Chunk 12** (`replace`) — headcount 3,838 → 3,735. **Borderline:** the decline is mildly substantive economically, but Risk Factors stickiness convention says rolled numbers are not the disclosure event. Useful to anchor the gate at exactly this boundary.
3. **Chunk 79** (`replace`) — date rolls + Founders' ages updated. Pure cosmetic.

**Missing example type (harvest later):** "pure cosmetic that LOOKS substantive at first glance" — e.g., a counsel-reword preserving the same legal concept with fresh vocabulary. None of PLTR's 85 chunks hits that case cleanly; harvest from a different issuer (KEY FY22 or CVNA FY22 candidates) when designing the production prompt.

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

Other codes (G gift, D return-to-issuer, etc.) are rare and currently ignored. Revisit if a meaningful eval miss is traced to one. (G observed once in the Phase 0.5 Form 4 distribution spike on PLTR — see §3.1.)

## §3.1 — Form 4 distribution spike (Phase 0.5 Step D, 2026-05-11)

3-month window (2026-02-11 → 2026-05-11) across two structurally different watchlist names. Run by `spike/form4_distribution.py`; raw output at `spike/form4_distribution.json`.

| | PLTR | VRTX |
|---|---|---|
| Filings | 11 | 66 |
| Unique insiders | 9 | 21 |
| Code distribution | S=54, C=8, M=8, A=1, G=1 (no P) | S=57, F=42, A=30, D=4, M=4 |
| 10b5-1 footnote hit-rate | 73% (8/11) | 48% (32/66) |
| Plan-adoption-date regex extraction (given 10b5-1 hit) | 50% (4/8) | 0% (0/32) |
| Insiders with ≥3 trades in window | 0/9 | 11/21 |

**Findings that feed Phase 1 design:**

1. **`volume_baseline_window` should default to 12 months, not 3.** PLTR-style names are sparse traders — zero insiders had ≥3 trades in 3 months. A trailing-12-month per-insider baseline with issuer-wide aggregate fallback (as `ARCHITECTURE.md` §5 already anticipated) is the right default. Final lock during Phase 1 correlator implementation; revisit if eval results suggest otherwise.

2. **A/M/F filtering is non-optional.** VRTX's 66 filings contain 42 F-codes (tax-withholding via share surrender, auto-triggered on vest) and 30 A-codes (grants). Without filtering these from the baseline AND from the in-window count, every Vertex exec looks like a high-volume discretionary trader. Confirms §3.

3. **Plan-adoption-date extraction needs a Haiku call, not regex.** The regex `(plan adopted|adopted on|entered into on|pursuant to ... entered into on) <DATE>` matched 50% of PLTR 10b5-1 footnotes and 0% of VRTX's. Filer-template language varies widely. Phase 1 should make this a structured-output Haiku call against `Form4.footnotes` text (Form4Plan extraction), not a hand-rolled regex. Logged here so we don't waste effort tuning regex.

4. **10b5-1 footnote hit-rate varies sharply by issuer.** PLTR 73% vs VRTX 48% in the same window. Hit-rate alone isn't a reliable plan-vs-discretionary classifier — it depends on filer-template conventions. Phase 1 should treat footnote-text scan as a *signal*, not a *decision rule*.

5. **Code `G` (gift) seen in the wild.** Rare but real (1 instance in PLTR's 11 filings). Currently ignored; revisit if eval miss is traced to one.

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

**Starting balance (2026-05-11):** **$4.98 OpenAI credits** + $0 Anthropic. Phase 1 starts on OpenAI to consume the credits, then falls over to Anthropic on `insufficient_quota` per `ARCHITECTURE.md` §9 (Provider Fallover). The Phase 0.5 Stage-2 dry-run already spent ~$0.01, leaving roughly $4.97.

Weekly running total of LLM spend (from `llm_call_log` aggregation), broken down by provider.

Template per entry:

```
### Week of YYYY-MM-DD
- Total: $X.XX  (OpenAI $X.XX, Anthropic $X.XX)
- By role: cheap $X.XX / quality $X.XX
- By call site: diff_gate / diff_summary / correlator / eval_judge
- Notable: anomalies, e.g. "Sunday eval run cost $4 — investigate prompt-caching"
```

Template per `provider_switch` event (logged to `llm_call_log` with `call_site='provider_switch'`):

```
### YYYY-MM-DD HH:MM — switched provider
- Triggered by: <error class + message>
- OpenAI spend at switch: $X.XX
- Process restarted on OpenAI? <yes/no>
```

Budget: $5–15 nominal MVP, $30–50 realistic with iteration. No hard cap. The OpenAI credits provide a free runway; Anthropic spend after fallover is the real cost.

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

## §11 — Eval findings (Phase 1)

Recorded as the eval harness runs uncover real signal vs. assumptions. Per `CLAUDE.md` §4.5 honesty rules: events stay locked, misses are documented here.

### `pltr_karp_form4_2024` — pre-registration miss (2026-05-12)

**Result:** FAIL (binary, no judge fallback).

**Why:** the pass_criteria requires `'Karp' in correlator_payload.drivers`, but the correlator's verdict correctly named other PLTR insiders, not Karp. Investigation:

- All 24 of Karp's Form 4 transactions in 2024-11-01:2024-12-31 carry `is_10b5_1=1` — the footnote regex hit on every one of them ("pursuant to a preexisting Rule 10b5-1 trading plan, entered into on December 12, 2023" pattern). Karp's Nov 2024 sales are 100% plan-driven.
- The correlator filters 10b5-1 trades from the discretionary set per `CLAUDE.md` §4.4 (locked decision — the plan filter is non-optional). Karp's trades are correctly excluded.
- The 6 trades that *did* survive the filter have `insider_name="Palantir Technologies Inc."` (the issuer itself, not a human) and prices of $0.10–$0.13/share — clearly admin entries or share-issuance records, not market sales. Form 4 parser limitation: it accepts issuer-name placeholders as "insiders." Phase 2 LLM extractor (`NOTES.md` §3.1) should normalize this.
- The LLM verdict named "Palantir Technologies Inc. Oct 30 & Nov 4 sales" as drivers, with confidence=0.7 — anomalous=True. But "Karp" isn't in the drivers string.

**What this means:**

The eval criterion was written under the assumption that Karp's Nov 2024 selling around the AIP launch would be flagged. That assumption was wrong — the trades were plan-driven, and the locked 10b5-1 filter correctly excludes them. The system did exactly what its design says.

Per `CLAUDE.md` §4.5: this event stays at FAIL in the scorecard. The README will explain: "8/12 and here's what the 4 misses taught me" is a stronger story than swapping the criterion to make it pass.

**Phase 2 follow-ups identified by this miss:**
- ~~Form 4 parser: filter out issuer-name placeholders from `insider_name`.~~ **Landed.** `_is_issuer_placeholder` in `fetcher.py` normalizes corporate suffixes (Inc., Corp., Co., LLC, etc.) and skips rows whose insider equals the filing's issuer name. 7 spurious PLTR rows removed from `form4_transactions`; PLTR Q3 2024 re-runs as `anomalous=False` (52 trades in window, all 10b5-1 plan-driven, 0 discretionary). 4 new tests in `tests/test_fetcher.py`.
- LLM-judge fallback would have helped here: the rubric explicitly says "Karp identified as a primary contributor," which the criterion encodes literally. A judge call could note "Karp's trades were correctly filtered as plan-driven; the criterion is inconsistent with the locked 10b5-1 design" and return partial credit. (Phase 1 grader only falls back to judge when binary returns None, not when binary returns False — Phase 2 could broaden the trigger.)
- Possibly revise eval event #6 in a Phase 2 pre-registration v2 (separate tag, separate `locked_at`) to test a different aspect of the correlator that doesn't conflict with locked design.

### `key_10k_fy22` — PASS via binary (2026-05-12)

**Result:** PASS (binary, no judge fallback).

KEY FY2022 10-K (accession `0000091576-23-000026`, filed 2023-02-21) diff'd against FY2021 10-K (`0000091576-22-000029`). Stage 3 produced 100+ summaries; aggregate materiality_max = 0.9. `affected_topics` union included `interest_rates`, `deposits`, `liquidity`, `risk_management`, `loans`, `credit_losses`, `SOFR`, `cybersecurity`. The pass_criteria asked for any of `[available-for-sale, afs, deposits, deposit_composition, capital_ratio, unrealized_losses]` at materiality ≥ 0.6 — the system surfaced `deposits` cleanly.

Classic regional-bank disclosure pivot after the 2022 rate spike (KEY's AFS unrealized losses + deposit-composition shift were front-page stories). The diff analyzer caught the broad theme without hand-tuning.

### `cvna_10k_fy22` — PASS via binary (2026-05-12)

**Result:** PASS (binary, no judge fallback).

CVNA FY2022 10-K (`0001690820-23-000052`) vs FY2021 10-K (`0001690820-22-000080`). Aggregate materiality_max = 0.9. Topics surfaced included `liquidity`, `adesa_acquisition`, `cybersecurity`, `financing_activities`, `going_concern`, `debt_covenant`. The pass_criteria asked for any of `[liquidity, going_concern, substantial_doubt, debt_covenant, refinancing]` — three of the five matched.

CVNA's liquidity stress was well-documented at the time; the diff analyzer surfaced exactly what an analyst would expect.

### Phase 1 final scorecard (3 of 12 pre-registered events)

```
global:        2/3
correlator     0/1   (pltr_karp_form4_2024 — documented miss)
diff_analyzer  2/2   (key_10k_fy22, cvna_10k_fy22)
```

Total OpenAI spend for the 3-event scorecard: ~$1.27 (398 Stage 2 + 199 Stage 3 + 1 correlator calls). Run time: ~17 minutes wall-clock (TPM-throttled Stage 3 calls dominate). The remaining 9 events stay un-pre-registered until Phase 2.

## §6 — DCF valuation layer: Phase 0 feasibility findings (2026-07-27)

Diagnostic: `spike/valuation_feasibility.py` (throwaway) + `spike/valuation_feasibility.json`. Answers the gate that decides the DCF-layer design (plan `cheerful-moseying-conway.md`). **Answers the long-open §5 XBRL question above: structured numeric extraction IS available and clean via `Company.get_facts()`.**

**edgartools financials API (5.31.0) — confirmed, undocumented until now:**
- `Company.get_facts().to_dataframe()` → companyfacts as a DataFrame, columns `concept, label, value, numeric_value, unit, period_type, period_start, period_end, fiscal_year, fiscal_period`. **`concept` is namespaced** (`us-gaap:Revenues`) — matching bare names silently yields nothing (cost me a false "0/8" first pass).
- `Company.get_financials()` → `Financials` with ready helpers: `get_free_cash_flow()`, `get_capital_expenditures()`, `get_operating_cash_flow()`, `get_revenue()`, `get_shares_outstanding_basic/diluted()`. Gives the FCF mapping a canonical primary path + a free independent second opinion for `validate_fcf`.

**XBRL spine: 8/8 CLEAN → PROCEED.** Every watchlist name has clean multi-year FCF-concept coverage (revenue / operating cash flow / capex): PLTR 6y, NET 7y, MRNA 8y, ULTA 8y, CVNA 7y, KEY 10y, VRTX 16y, SCHW 17y. `get_free_cash_flow()` works for all 8. The reliable quarterly-refresh spine is viable.

**Narrative guidance from MD&A: NOT viable (mechanical hit_rate 0.281 is mostly false positives).** Verified by hand: genuine forward revenue/EPS guidance essentially does not appear in the 10-K/10-Q MD&A that Redline diffs. The mechanical hits concentrate in MRNA (deferred-revenue realization language — "$37M is *expected* to be realized", not guidance) and SCHW (residual results-of-operations churn). Root cause: **companies issue quantitative guidance in 8-K earnings-release exhibits (Ex-99.1), not in MD&A.** A first naive detector scored 0.844 — 100% churn false positives ("expense increased 5.0%"); a sentence-bounded forward-only detector (perf lesson: whole-document lazy-quantifier regex catastrophically backtracks on 300k-char bank filings) drops it to ~0 genuine.

**Two design decisions taken with Ian (2026-07-27):**
1. **XBRL-only revaluation core now.** The differentiated "flagged narrative change → model input" hook is deferred to a separately-feasibility-gated **8-K earnings-exhibit** extraction follow-on (that is where guidance lives). Not built on the false MD&A premise.
2. **Banks excluded from DCF.** SCHW + KEY are financials; an unlevered-FCF DCF (OCF − capex) is not a meaningful model for them. DCF the other 6 (PLTR, NET, MRNA, VRTX, CVNA, ULTA); show the 2 banks as "not DCF-modeled (financial sector)".

## §7 — 8-K guidance extraction (the differentiated hook, 2026-07-27)

**Feasibility gate PASSED** (`spike/guidance_8k_feasibility.py`, `.log`): guidance_rate = 20/25 = **0.80** of item-2.02 8-Ks carry extractable forward guidance — the opposite of MD&A (§6). Genuine ranges confirmed by hand: VRTX "FY2026 revenue guidance of $12.95B to $13.1B", MRNA "expects 2025 revenue of ~$1.9B", CVNA "adjusted EBITDA of $2.0 to $2.2B for full year 2025", ULTA "fiscal 2026 net sales growth of 6.0% to 7.0%".

**edgartools 8-K exhibit access:** `filing.obj().items` lists items (`"Item 2.02"`); `filing.attachments` yields `Attachment` objects with `document_type` (e.g. `"EX-99.1"`) and `.text()` → the press-release text (~36-113k chars). The earnings release is EX-99.1, not the 8-K body.

**Built:** `valuation/guidance.py` (fetch EX-99.1 → quality-role LLM typed extraction → `extracted_figures` with confidence gate `trigger_eligible`/`manual_review` + period-over-period `raised/lowered/reaffirmed/initiated` delta), `GuidanceFigure`/`GuidanceExtraction` schemas, `guidance_extract_v1.txt` prompt, `guidance_runs` marker table. `revalue.run_guidance_revaluations` maps a filed revenue-guidance figure → year-1 growth override → immutable valuation with a `guidance`-source before/after link. `guidance_eval.py` = precision/recall grader (pure `grade_guidance`, unit-tested) vs `guidance_labels.yaml`. Regex alone is too noisy (feasibility spike caught balance figures with a "fiscal 20XX" cue) — hence LLM-typed.

**⚠ BLOCKER (RESOLVED 2026-07-28, see §12).** At build time OpenAI returned **429 insufficient_quota** and there was no `ANTHROPIC_API_KEY`, so the guidance path was mock-only. It has since run live on `claude-sonnet-4-6` via the fallover, and was later expanded to a mechanically-selected, locked 12-accession panel and reframed to the pipeline-faithful measure (revenue guidance — the only DCF input — trigger-eligible 1.000/1.000; raw all-metrics 0.714). The earlier 0.875 was a mis-scoring of correctly-classified segment figures against the totals-only gold. See §12 / `EVAL.md` for the final-state numbers.

## §12 — DCF valuation layer: final state (close-out 2026-07-29)

The valuation subsystem is code-complete and closed out. This section is the as-built summary and the honest limitations ledger. (`edgartools`/XBRL and 8-K-exhibit mechanics live in §6/§7; this is the shipped-state overview.)

**What shipped (`src/redline/valuation/`):**
- **XBRL spine** — `xbrl.py` ingests companyfacts via `Company.get_facts()`; `fcf.py` reconstructs unlevered FCF against a versioned concept mapping (`config/valuation/fcf_mapping_v1.yaml`) with an independent second-opinion reconciliation, validated by `validation_eval.py` (edgartools FCF vs DB reconstruction agree to 0.00%).
- **DCF engine** — `dcf.py`, pure functions: driver schedule → FCF projection → Gordon terminal value → bear/base/bull **range** + one-driver sensitivity. No point estimates.
- **Guidance hook** — `guidance.py` extracts typed forward guidance from 8-K EX-99.1 earnings exhibits (quality-role LLM, confidence-gated `trigger_eligible`/`manual_review`, `scope` total/segment); `revalue.py` maps a filed FY total-revenue guidance figure → year-1 growth override → an **immutable** before/after `dcf_valuations` pair with a `guidance`-source input link. Ran live on `claude-sonnet-4-6`; NET is the clean exemplar pair (growth 28.0% → 29.6%).
- **Cost-of-capital constants** — `config/valuation/assumptions.yaml`, verified and transcribed (all six `is_placeholder: false`): Rf 4.65% (FRED DGS10), ERP 4.50% (Damodaran), statutory tax 21%; per-company WACC + dated manual reference prices.
- **Hosted recruiter dashboard** — `dashboard/valuation_app.py`, strictly read-only (`query_only=ON`, no LLM import, no key), reads the committed curated snapshot `data/valuation_demo.db` (rebuilt by `scripts/snapshot_valuation_demo.py`).

**Branch decisions taken:** banks (SCHW, KEY) excluded — unlevered-FCF DCF is wrong for financials; NET/PLTR/MRNA are monitored-not-valued (high-multiple / negative-FCF, DCF doesn't reconcile); MD&A narrative extraction deferred (guidance is in 8-K Ex-99.1, not MD&A — §6). Only VRTX + ULTA are presented as DCF-valued.

**Known limitations / future work** (documented, NOT to be built now):
1. **Guidance precision (RESOLVED 2026-07-30).** The earlier 0.875 counted 3 *correctly-classified segment figures* (e.g. PLTR US-commercial revenue) as false positives against the totals-only frozen gold — the extractor was right, the gold is totals-only. Measured on `scope='total'` (what the gold covers), precision is **0.966** (recall 1.000, F1 0.982; 28 TP / 1 FP / 0 FN, 3 segment figures excluded) — clears the 0.90 bar. `guidance_eval._extracted_rows` now defaults to `scope='total'` and reports the excluded-segment count; the frozen gold is untouched. (The confidence gate also quarantines actual junk.)

   **Eval expanded n=3→12 companies via a mechanical rule (2026-07-30, pre-scoring lock).** The guidance panel is now selected by **Rule R** — a deterministic, discretion-free rule (all DCF-eligible names; the 2 most-recent *plain* 8-Ks (item 2.02 + EX-99.x, **8-K/A amendments excluded**) per name as of the lock date) — recorded in `config/valuation/guidance_labels.yaml` under a `registration:` manifest with `locked_at` + `previously_observed` (computed from prior artifacts, never hand-assigned), to be sealed by tag `guidance-eval-registration-v1`. Selection is a pure DB read over persisted qualification flags (`earnings_8k_qualification`, populated by `scripts/backfill_8ks.py` — the only network step; no LLM). Panel = **12** (2 per name, no undershoot); **8 never-seen** accessions form a held-out sub-panel reported separately by `eval.report`. Held-out framing = "rule governs all; disclose observed" (§ plan). Independence is claimed only for never-seen accessions; pre-seen gold is inherited as-is (decision (a)).

   **Scored 2026-07-30 (Anthropic Sonnet fallover; OpenAI still 429).** Extraction of the 8 never-seen accessions cost ~$0.24 (logged spend $1.76→$2.00, under the $3 cap). Real panel numbers (scope=total, panel-restricted): **precision 0.690, recall 0.967, F1 0.806** (29 TP / 13 FP / 1 FN) — down from the n=3 0.966, which is the honest **generalization gap** a mechanically-selected held-out panel is meant to expose. **The headline is framed to what the pipeline actually consumes** (EVAL.md leads with these): the DCF's only guidance input is *revenue*, and on the figures the confidence gate marks `trigger_eligible` (what actually reaches the model), **revenue extraction is precision 1.000 / recall 1.000** on the held-out panel; the confidence-gated overall (all metrics acted on) is **precision 0.818 / recall 0.871** (the gate trades some recall for precision); the raw all-figures overall is **precision 0.714 / recall 0.968** (30 TP / 12 FP / 1 FN, after the ULTA fix below). Raw is kept in EVAL.md with the full FP breakdown — no scrubbing. The raw precision drag is concentrated in `other` (0.250) and `comparable_sales` (0.000): ~10 of the 12 raw FPs are the extractor *correctly* emitting real forward guidance the totals-only / included-metrics scoping deliberately EXCLUDES — VRTX combined R&D+SG&A expense (~$6.3-6.45B) and tax rate (19.5-20.5%), MRNA opex, ULTA comparable-sales and capex — a gold-*boundary* effect analogous to the segment-figure exclusion, not hallucination (recall ~1.0). **ULTA `027061` gold fix (source-faithful, 2026-07-30):** its "Fiscal 2026 Outlook" table gives operating-income-growth 6-9% (was omitted) and diluted EPS as the $-level $28.05-28.55 (gold had wrongly used the summary bullet's growth-% 9.4-11.4); both corrected from the table, raising operating_income to 0.833. The remaining eps FP is an honest **basis mismatch** (extractor labels EPS `gaap`; the text is unqualified → gold convention `unspecified`) — left as-is, not tuned. **Percentage-guidance scoping (resolved 2026-07-30, Ian):** ULTA (×2) guides in firm two-sided % ranges → **included** as `unit=pct` gold (consistent with NOTES §7, which already treated ULTA's "net sales growth 6.0%–7.0%" as extractable); MRNA (×2) states only a one-sided soft "plan to deliver up to 10% revenue growth" → **excluded** as an aspiration (zero gold → hallucination test). Comparable-sales growth excluded as an operational metric. Gold now = 36 figures over 9 of the 12 panel accessions (MRNA ×2 + the pre-seen CVNA `-000034` are intentionally zero-gold).
2. **Growth / turnaround names are not DCF-valuable — confirmed by a reverse-DCF gate (2026-07-30).** NET/PLTR/MRNA/CVNA don't reconcile under a forward unlevered-FCF DCF. A feasibility spike (`spike/growthname_feasibility.py`) tested the obvious next idea — **reverse-DCF** (solve for the constant revenue growth that makes the base-case DCF match the manual reference price) — and it *also* fails to rationalize them: NET has no solution in a [-50%, 150%] growth bracket (price ~$264 vs DCF base ~$11), PLTR implies ~104% sustained growth, MRNA ~84%; only CVNA (~7%) lands in a plausible [0, 60%] band. Pre-registered gate (≥3/4 in-band) → **1/4 → DOCUMENT + SKIP**; these stay monitored, not valued. The finding is the point: neither a forward nor a reverse FCF-DCF is the right tool for these prices. A multiples / reverse-DCF *display* remains possible future work but is not justified by the data today.
3. **WACC and reference prices are dated manual constants** (no live market feed, by design — keeps the §4 "no market data" line clean). Reference prices go stale; the dashboard greys them past `STALE_DAYS` (120).
4. **MRNA and CVNA use sector-anchored WACC judgment calls** (Damodaran sector proxies, not clean CAPM — CVNA's raw 5Y beta is distorted by the 2022 crash; MRNA's CAPM is unreliable for a cash-burning biotech). Documented inline in `assumptions.yaml`.
5. **`revalue` is not point-in-time** — it always builds the base from the *latest* companyfacts, so re-running periodic filings does not produce an input-change pair; the only genuine before/after pair is the guidance-triggered one (NET).
6. **NET mechanism tile copy (RESOLVED 2026-07-29).** The tile previously rendered a standalone dollar value ("Modeled base (before → after)", ~$11.97) that a skimmer could misread as a NET price target. Fixed: the tile now shows the *relative effect on the estimate* (e.g. +0.9%) under "Effect on the modeled estimate" — no absolute NET dollar figure is displayed anywhere, so the misread is structurally impossible while the "a filing moved the model" mechanism is preserved.
7. **Hosted valuation dashboard — LIVE (2026-07-29).** Deployed to Streamlit Community Cloud at `redline-valuations.streamlit.app`, **public**, and confirmed to render for a logged-out visitor (incognito). Read-only against the committed `data/valuation_demo.db`; no key/secret (and specifically no `REDLINE_DB_PATH` secret, which would mis-point it at the disclosure DB).
8. **Streamlit markdown renders paired `$` as LaTeX (FIXED 2026-07-30).** `st.markdown`/`st.caption` treat text between two `$` as KaTeX math, so money strings (`_fmt_money` → "$12.9B") in the "how this was modeled" detail garbled into stacked math. Fixed with `dashboard.data.md_escape()` (escapes `$` → `\$`, a visual no-op for lone `$`) applied at every money/LLM-text markdown sink in both apps (never `st.metric`/`st.text`, which don't render KaTeX). The `EVAL.md` render is deliberately NOT escaped (it's our own controlled rich markdown; `report.py` emits no bare `$`). **Residual (minor, not fixed):** LLM free-text can still trigger other markdown (`_italic_`, `:shortcode:`); not worth over-escaping our own intended formatting. Also fixed the sensitivity tornado overlap (band-relative bars + height scaling with driver count) and the cramped `ev_split` height.

**Deployment gotcha (learned 2026-07-29, cost the `redline-edgar` app an outage).**
Streamlit Community Cloud runs **only the read-only dashboards** and caches its
venv — it does not reliably reinstall the `redline` package on a source-only push.
So a dashboard that reads a **newly-added** config-model field at import time can
crash on a stale env with `AttributeError` (the field exists in the repo but not
in the installed package). Rule: **dashboard presentation constants are named
module constants, never `SomeConfig().new_field` at import time.** The pipeline
(poller/fetcher/diff/correlator/valuation), which legitimately consumes config,
runs **locally** — Cloud never needs those fields. `tests/test_dashboard_import.py`
imports both dashboards so a load-time crash fails CI (dashboards are otherwise
untested per §7, which is how this shipped green at 199 passing).
