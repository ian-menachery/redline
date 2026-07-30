# ROADMAP.md

Phased plan for `redline`. Each phase has explicit deliverables and acceptance signals. See `CLAUDE.md` §4 for locked scope and `ARCHITECTURE.md` for subsystem detail.

## Phase 0 — Planning (COMPLETE)

- Eval set drafted: 12 events, each tagged with subsystem(s), with `pass_criteria` and `llm_judge_rubric` placeholders, and a `locked_at` timestamp inside each entry
- Watchlist locked: 8 tickers, 4 sectors (PLTR, NET, SCHW, KEY, MRNA, VRTX, CVNA, ULTA)
- Critical review pass completed (sentiment dropped; insider correlator base-rate problem identified; Risk Factors stickiness recognized as the headline-feature risk)
- Four planning documents written: `CLAUDE.md`, `ARCHITECTURE.md`, `NOTES.md`, `ROADMAP.md`
- No code written

## Phase 0.5 — Day 0 spike (~4 hrs, NEXT)

Goal: validate critical assumptions before writing pipeline code.

**Tasks:**

1. **edgartools verification.** Pull each of:
   - PLTR Q3 2024 10-Q → confirm MD&A / Risk Factors / Legal / QDMR section extraction works
   - PLTR 2024 Form 4 (any of Karp's filings) → confirm transaction parsing returns structured codes, shares, dates
   - CVNA July 2023 8-K (debt restructuring) → confirm item-level extraction (1.01? 8.01? discoverable?)

   Log findings to `NOTES.md` §5.

2. **Manual Risk Factors diff.** Hand-diff PLTR Q2 2024 vs Q3 2024 Risk Factors with no automation. Read it, mark substantive vs. noise. Calibrate intuition for how aggressive Stage 1 and Stage 2 must be (`NOTES.md` §1).

3. **Write 3 eval YAMLs end-to-end.** Events 1 (KEY 10-K FY2022, diff_analyzer), 3 (CVNA 10-K FY2022, diff_analyzer), 6 (PLTR Karp Form 4 cluster, correlator). Stress-tests the Pydantic schema and the `pass_criteria` format. Confirms binary rules can be expressed cleanly.

4. **Form 4 distribution inspection (for the correlator).** Pull ~3 months of Form 4s for two watchlist names (one high-volume: PLTR; one steadier: VRTX). Look at: how many transactions per insider per month, code distribution (P/S vs A/M/F), 10b5-1 checkbox prevalence post-April-2023. This is the input data the anomaly score's combination formula needs to be designed against.

5. **Commit pre-registration artifact.** Commit `config/eval_events.yaml` + `config/watchlist.yaml` with the `locked_at` timestamps embedded. Tag the commit `eval-pre-registration-v1`. This is the receipt that the eval was locked before any code measurement.

**Exit criteria:** if any task reveals a blocker (e.g. `edgartools` can't extract Form 4 plan-adoption text and there's no workaround), pause Phase 1 and address before proceeding. The Form 4 distribution work also unblocks committing to the anomaly score combination formula.

## Phase 1 — MVP (5–7 focused days, or 10–14 part-time)

Goal: a working end-to-end system that produces eval scores against all 12 events.

**Build order (subsystems 1–5 from `ARCHITECTURE.md`):**

1. **EDGAR poller** — 15-min cadence, SQLite state, fair-access compliant. Acceptance: runs for 24h without 403s, correctly detects new filings on test data.
2. **Filing fetcher + parser** — `edgartools` integration, section extraction for all four filing types, aggressive caching. Acceptance: 4 fixtures (one per filing type) parse cleanly with expected sections.
3. **Diff analyzer (three-stage)** — Stage 1 rule-based, Stage 2 Haiku gate, Stage 3 Sonnet summary. Acceptance: events 1, 3, 5, 7, 8, 9, 10, 11 (the eight diff_analyzer-tagged events) produce flagged events with materiality scores.
4. **Insider-trading correlator** — Form 4 ingestion, ±14d join, 10b5-1 filter, three-signal anomaly score (combination formula committed AFTER Phase 0.5 distribution inspection), Sonnet verdict. Acceptance: events 6, 12 (correlator-tagged) produce anomaly verdicts with `anomalous = True`.
5. **Dashboard** — Streamlit, read-only SQLite, default to last-N flagged events, per-event detail view, filters. Acceptance: walk through three flagged events end-to-end in the UI without crashes.

**Cross-cutting deliverables:**

- **Pydantic everywhere.** `RedlineConfig` (`pydantic-settings`), `Watchlist`, `EvalEvent`, `DiffGateDecision`, `DiffSummary`, `CorrelatorVerdict`, `EvalJudgeVerdict`, `PromptTemplate`.
- **Status-driven pipeline + retry queue.** Implemented in `pipeline.py`. State transitions match `ARCHITECTURE.md` §7.
- **Replay mode.** Built second-half of Phase 1, after live polling works. Shares code with the eval harness.
- **Eval harness with hybrid grading.** Binary `pass_criteria` first, LLM-judge fallback. Per-subsystem + global scoring.
- **Tests.** Parser, three-stage filter, anomaly score, eval grading (per `CLAUDE.md` §7).
- **README with honest framing.** Per `CLAUDE.md` §1. Includes per-subsystem + global eval scores. Discusses any eval misses.

**Acceptance for Phase 1 overall:**
- Eval harness runs end-to-end against all 12 events
- Score reported per-subsystem and globally (e.g. "diff_analyzer 5/8, correlator 2/3, parser 3/4, global 8/12")
- Cost log shows < $50 total Phase 1 spend
- README written and the project is walkthrough-defensible

## Phase 2 — Hardening (post-MVP, pre-interview)

Goal: ship polish that makes the project defensible in extended interview conversations.

- **Better section parsing for edge cases.** Empty Legal Proceedings ("see prior 10-K"), S-1-style risk-factor tables, 8-Ks with malformed item headers.
- **Backoff / retry tuning on EDGAR errors.** Use 24h+ of live operation data to calibrate retry delays.
- **Email / push alerts for high-priority flags.** Provider TBD. `notifier` interface scaffolded per `ARCHITECTURE.md` §6.
- **Hosting — DONE (Streamlit Community Cloud).** Both dashboards are hosted and **public**: the disclosure monitor (`redline-edgar.streamlit.app`) and the DCF valuation dashboard (`redline-valuations.streamlit.app`). Read-only against committed curated snapshots; no cron/VPS. A VPS + scheduled-poller upgrade remains a future option, not a need.
- **Live operation log infrastructure.** `live_operation_log` table populated automatically; dashboard surface for "recent activity," separate from eval.
- **Form 144 ingestion.** Second correlator signal — proposed open-market sales by affiliates. Adds context for distinguishing 10b5-1 from discretionary.
- **Anomaly-score weight tuning.** Use Phase 1 eval results to set non-trivial weights on the three signals. Document the tuning process (cross-validation? eyeball calibration? both?) in `NOTES.md`.

## DCF valuation layer (Subsystem 7) — SHIPPED & CLOSED OUT 2026-07-29

Event-driven revaluation from XBRL financials (NOT sentiment, NOT alpha — see `CLAUDE.md` §4). Shipped: XBRL companyfacts ingestion, a range-based DCF engine (bear/base/bull + sensitivity), an immutable before/after revaluation hook, a mandatory FCF-base validation eval, the 8-K guidance extraction hook, and a hosted recruiter dashboard. Covers the 6 non-financials (banks excluded); only VRTX + ULTA are presented as DCF-valued. See `NOTES.md` §12 (final state + limitations), §6/§7, and `CLAUDE.md` §11.

**Done since the initial build:**
- **8-K earnings-exhibit guidance extraction — DONE.** Feasibility gate passed (guidance is in Ex-99.1, not MD&A); typed extractor ran live on `claude-sonnet-4-6`; extraction-accuracy eval produced a real number (precision 0.875 vs the 0.90 bar — the residual FPs are correctly-classified segment figures against a totals-only gold; see `NOTES.md` §12).
- **Assumption constants verified — DONE.** WACC / terminal growth / manual reference prices transcribed from sourced values; all six `is_placeholder: false`.
- **Hosted dashboard — SHIPPED & PUBLIC.** `dashboard/valuation_app.py` deployed to Streamlit Community Cloud (`redline-valuations.streamlit.app`), read-only against a committed curated snapshot, no key/secret; confirmed public/logged-out (see `NOTES.md` §12).

**Still deferred (each needs its own feasibility gate before building):**
- **Bank valuation model.** SCHW/KEY need a financials-appropriate model (dividend-discount / residual-income / P-TBV) if they're to be valued at all.
- **Growth-name valuation.** NET/PLTR/MRNA don't reconcile under FCF-DCF; a multiples-based or reverse-DCF approach would be needed to value rather than merely monitor them.
- **Guidance precision to 0.90.** Segment-labeled gold or scope=total-only precision measurement (see `NOTES.md` §12 limitation 1).

## Phase 3 — Stretch (optional, post-recruiting)

Goal: things worth building if there's bandwidth after recruiting, but not part of the resume artifact.

- **Watchlist expansion** to 20–25 tickers, two more sectors. Stress-tests parser across new disclosure vocabulary.
- **13F holdings correlation.** Third insider signal — institutional ownership changes around filings. Quarterly cadence, more aggregate than Form 4.
- **"Similar past filings" retrieval.** Embed historical Risk Factors per issuer, surface nearest neighbors when a new filing arrives. Helps the diff analyzer find the right comparison target when most-recent-same-type isn't ideal.
- **Small fine-tuned classifier on top of LLM outputs.** Reduces per-filing API cost at scale (> 50 tickers). Trains on labeled Stage 3 outputs.
- **Risk-factor novelty score** vs. the company's historical baseline. Distinguishes "first time mentioning [topic]" from "Nth re-mention with variation."

## Phase 4 — Out of scope (explicitly)

These are NOT on the roadmap and will not be added regardless of how the project evolves:

- **Universe-wide coverage.** Quality > coverage. See `CLAUDE.md` §4.6.
- **Custom-trained foundation model.** Out of scope; the project is about pipeline design, not model R&D.
- **Trade signal generation.** Information surfacing only. See `CLAUDE.md` §4.7.
- **Sentiment scoring as headline feature.** Sentiment is weak signal and overdone. See `CLAUDE.md` §4.1.

## Open questions to revisit

Real questions that aren't blocking but should be revisited at the indicated checkpoints.

- **Same-type vs. same-period-prior-year comparison for diff analyzer.** Default: most-recent-same-type (Q3 2024 vs Q2 2024). Alternative: same-period-prior-year (Q3 2024 vs Q3 2023), which captures seasonal disclosure changes better but misses faster-evolving narratives. **Revisit after Phase 1 eval results** — if 10-Q events miss material changes a YoY diff would catch, consider switching or adding both.
- **Anomaly score combination formula + weights.** Not yet committed; equal weights are a placeholder. Combination function shape (weighted sum vs. max vs. learned) requires Form 4 distribution inspection (Phase 0.5 task 4). **Revisit before Phase 1 correlator implementation** and again at end of Phase 1 with eval results in hand.
- **6-K filings.** Foreign private issuers file 6-K instead of 10-Q/10-K. Out of scope while watchlist is US-only; **revisit if Phase 3 watchlist expansion adds international names.**
- **`volume_baseline_window` default.** Currently unspecified — candidates include 6-month and 12-month trailing per insider with issuer-wide fallback. **Resolve at end of Phase 0.5** once real Form 4 trade-frequency distributions are known.

## Things explicitly NOT on the roadmap (with reasons)

Restating from `CLAUDE.md` §4 auto-push-back list, with the WHY:

- **Sentiment scoring** — weak signal on 10-Ks; overdone in retail-investor tools; easily dismissed at interview time.
- **Real-time / sub-minute polling** — EDGAR fair-access caps it; the value of this tool is structured analysis, not latency.
- **Trade signal generation** — invites scrutiny ("does it actually beat the market?") that distracts from the engineering story; not the project's purpose.
- **Fresh eval events post-lock** — breaks pre-registration discipline. Use `live_operation_log` for recent-activity demos.
