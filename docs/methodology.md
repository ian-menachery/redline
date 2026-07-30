# Methodology — how a filing flows through redline

End-to-end, in plain terms. (Subsystem internals + the SQLite schema live in
`ARCHITECTURE.md`.)

## The pipeline

```
EDGAR ──poll──▶ fetch+parse ──▶ diff analyzer ─┐
 (15-min)        (structured     (3-stage       ├─▶ flagged_events ─▶ dashboards
                 sections)        filter)        │
                     └──▶ correlator (Form 4) ───┘
                     └──▶ DCF revaluation (XBRL + 8-K guidance) ─▶ dcf_valuations
```

1. **Poll.** Every 15 minutes, check each watchlist CIK for new 10-K / 10-Q /
   8-K / Form 4 accession numbers; persist last-seen state in SQLite.
2. **Fetch + parse.** Pull the filing via `edgartools`; extract the sections that
   matter (MD&A, Risk Factors, Legal Proceedings, Quantitative Disclosures) into
   structured rows. Aggressive caching keeps re-runs cheap.
3. **Diff analyze** (10-K / 10-Q). Compare each section to the prior same-type
   filing through the three-stage filter (deterministic normalization → cheap
   LLM gate → quality LLM summary). Material changes become `flagged_events` with
   a materiality score and affected topics.
4. **Correlate** (any non-Form-4 filing). Join Form 4 insider transactions on a
   ±14-day window, drop 10b5-1 plan trades, compute three anomaly signals, and
   let a quality LLM synthesize a verdict. Anomalies become `flagged_events`.
5. **Revalue** (DCF-eligible names). On a new periodic filing or a filed 8-K
   revenue-guidance figure, rebuild the DCF base from XBRL, recompute a
   bear/base/bull range, and write an **immutable** before/after
   `dcf_valuations` row with an audit link from the changed input to the source
   filing.
6. **Surface.** Two read-only Streamlit dashboards render the curated snapshot:
   the disclosure monitor (findings, diffs, correlator, Form 4) and the DCF
   valuation app (ranges, model detail, charts).

## Pipeline state machine
Filings move `fetched → parsed → analyzed → flagged`. On failure a filing stays
at its stage with `last_attempted` + `failure_reason`; each cycle retries stale
failures; after the retry cap it goes `failed_permanent`. Nothing silently
disappears.

## LLM discipline
Four call sites (diff gate, diff summary, correlator verdict, guidance
extraction) each have a role (cheap vs quality) and a Pydantic output schema. A
parse failure is a call failure (retry once, then fail). Every call is logged to
`llm_call_log` with tokens + cost, behind a hard spend cap. Provider is
OpenAI-first with automatic Anthropic fallover on quota exhaustion.

## Evaluation
The harness replays each **pre-registered, locked** event at its historical
point in time against an isolated DB, grades against a binary `pass_criteria`
(LLM-judge fallback only when the rule can't evaluate), and writes `eval_runs`.
`python -m redline.eval.report` renders those into `EVAL.md`. Guidance-extraction
accuracy is a separate precision/recall grader (measured on `scope='total'`
figures against a frozen gold set).
