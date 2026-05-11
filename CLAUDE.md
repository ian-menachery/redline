# CLAUDE.md

Operating manual for any Claude session working in this repo. Reference sections by number in chat (e.g. "see §4").

## §1 — Project summary

`redline` is a scheduled SEC EDGAR monitoring system for a fixed 8-ticker watchlist. It performs structured analysis on new filings (10-K / 10-Q / 8-K / Form 4) by combining (a) quarter-over-quarter section diffs on MD&A, Risk Factors, Legal Proceedings, and Quantitative Disclosures About Market Risk, and (b) Form 4 insider-trading correlation against filing events on a ±14-day window. Flagged events surface via a local Streamlit dashboard. The project is graded by an evals harness running against 12 pre-registered historical filing events; scores are reported per-subsystem and globally.

Built as a resume artifact for Dec 2026 full-time recruiting (primary: tech consulting, DS/analyst; secondary: SWE). Ian starts as an EY Technology Consulting intern in summer 2026. The system should defend well in any of those interview contexts.

### What this project is NOT

- **Not real-time.** EDGAR fair-access caps polling. Honest framing is "scheduled monitoring," 15-minute cadence.
- **Not a sentiment tool.** Sentiment on 10-Ks is weak signal and overdone. See §4 for full reasoning.
- **Not an alpha generator.** No trade-signal generation. This is an information-surfacing tool, not a quantitative strategy.
- **Not a trading system.** No order placement, no portfolio management, no broker integration.
- **Not universe-wide.** Fixed 8-ticker watchlist. Quality > coverage.

Push back on any framing that drifts these lines.

## §2 — Who Ian is

DS + Econ at Northeastern. Comfortable: Python, SQL, intermediate ML, basic web. Less practiced: production-grade pipelines, distributed-systems vocabulary, infra/deployment specifics.

**Skip explaining:**
- Basic Python syntax, list/dict comprehensions, decorators (unless used in a specialized way), context managers
- pandas / numpy basics
- SQL joins, indexes, basic schema design
- LLM-as-judge concept, prompt engineering fundamentals, structured outputs at a high level
- What a 10-K / 10-Q / 8-K / Form 4 is in plain terms
- Pydantic basics

**Explain (with reasoning, not just mechanics):**
- Non-obvious library APIs (especially `edgartools`)
- Concurrency / locking specifics when they come up (SQLite WAL, retry-on-busy, etc.)
- Anthropic SDK specifics beyond basic chat completion (prompt caching, structured output validation patterns, batch usage)
- Why a specific test or design pattern is appropriate here vs. alternatives
- Tradeoffs in modeling decisions (anomaly score variants, baseline window choices, prompt-cache key design, etc.)

## §3 — How to talk to Ian

- **Direct, blunt, opinionated.** Skip softening. If a choice is wrong, say so. "It depends" is only acceptable when followed by "but in your situation, do X because Y."
- **Why, not what.** Reasoning is the value; code is the byproduct.
- **Get it right the first time.** Slower-and-correct beats faster-and-rework.
- **Polish matters.** This is a resume artifact. Type hints, Pydantic, docstrings on public functions, no magic numbers. The bar: "I'd walk an interviewer through any file without flinching."

### Anti-patterns (do not do)

- Excessive hedging
- "Great question," "happy to help," "absolutely," any sycophancy
- Recapping the project back to Ian before answering
- Long bullet summaries when prose would do
- "Would you like me to..." trailing questions after every reply
- Asking permission for something already requested
- Trailing "let me know if..." sentences

## §4 — Locked scoping decisions (do not relitigate)

These were debated, including a critical review pass. The reasoning is preserved here verbatim so future sessions can defend the choices.

1. **Drop pure sentiment as a headline feature.** Sentiment on 10-Ks is weak signal, overdone in retail-investor tools, easily dismissed at interview time. Lead with diff analysis instead.

2. **Diff analysis + insider-trading correlation are the differentiators.** QoQ/YoY delta on MD&A, Risk Factors, Legal Proceedings, and Quantitative Disclosures About Market Risk, with LLM summarization. Form 4 trades joined to filing events on a time window.

3. **Risk Factors are sticky.** Companies copy-paste them year over year with minor legal-counsel edits. This is the single biggest risk to the project's headline feature. The noise filter is therefore first-class architecture, not an afterthought.

4. **Insider-trading correlator has a base-rate problem.** Form 4 transactions happen weekly at large companies, mostly under pre-arranged 10b5-1 plans that are uncorrelated by design with then-current filings. The correlator MUST distinguish plan-driven from discretionary trades and define "anomalous" precisely, or it produces visually impressive but substantively meaningless output.

5. **Evals harness is non-negotiable.** 12 historical filing events, pre-registered in a versioned file with `locked_at` timestamps inside each entry. Cherry-picking structurally prevented. Eval is permanently locked — fresh events go in a separate "live operation log" for recent-activity demos, never into the graded eval.

6. **Small fixed watchlist (8 tickers, 4 sectors), not universe-wide.** Quality of analysis matters more than coverage.

7. **Honest framing.** "Scheduled monitoring," not "real-time." No alpha-generation claims in the README.

### Auto-push-back list

If a future session proposes any of these, push back and reference §4:

- Adding sentiment as a headline feature
- Expanding the watchlist beyond 8 tickers in Phase 1 or 2
- Adding fresh events to the graded eval
- Removing or watering down the three-stage noise filter
- Removing the 10b5-1 plan-trade filter from the correlator
- Generating buy/sell signals
- "Real-time" or sub-minute polling
- Universe-wide coverage

## §5 — Architecture summary

Five subsystems, built in this order:

1. **EDGAR poller** — 15-min cadence, watchlist-driven, SQLite state for last-seen accession numbers
2. **Filing fetcher + parser** — `edgartools`, structured section extraction, aggressive cache
3. **Diff analyzer** — three-stage filter (rule-based → Haiku gate → Sonnet summary)
4. **Insider-trading correlator** — Form 4 + ±14d join + 10b5-1 filter + three-signal anomaly score
5. **Dashboard + alerts** — Streamlit, read-only against SQLite, alerts deferred to Phase 2

**Pipeline pattern:** status-driven, not worker-driven. Filings move `fetched → parsed → analyzed → flagged`. On failure, stay at current status with `last_attempted` and `failure_reason`; each poll cycle retries stale failures; after 3 retries → `failed_permanent`.

**Replay mode** shares code with the eval harness. Built in the second half of Phase 1, after live polling works. Critical for both eval reproduction and demo polish.

See `ARCHITECTURE.md` for the full system diagram, subsystem internals, SQLite schema, and data flow walkthrough.

## §6 — Tech stack

- **Python 3.11+** — modern type hints (`X | None`, `list[T]`); pattern matching available where it clarifies.
- **SQLite** — single source of truth (state, content cache, transactions, diff results, flagged events, eval runs, LLM call log). One DB, no other persistence layers.
- **`edgartools`** — EDGAR access. Quirks logged in `NOTES.md` §5.
- **Anthropic SDK** — `claude-haiku-4-5` for bulk/cheap, `claude-sonnet-4-6` for quality, no Opus by default (cost discipline). See §9.
- **Streamlit** — dashboard. Read-only DB connection to avoid lock contention with the poller.
- **Pydantic v2 + `pydantic-settings`** — all config (watchlist, eval events, prompt templates) AND every LLM structured output. Validation is not optional.
- **pytest** — tests for substantive code paths only (see §7).
- **Hosting:** local first. VPS / Streamlit Cloud only if the project earns it (see `ROADMAP.md` Phase 2).

## §7 — Code quality bar

Standards that are non-negotiable in committed code:

- **Type hints everywhere.** Including return types. `from __future__ import annotations` allowed when it simplifies things.
- **Pydantic for structured data.** Config inputs, LLM outputs, internal data crossing subsystem boundaries.
- **Docstrings on public functions** (anything imported from another module). Internal helpers can skip if the name is clear.
- **No magic numbers.** Thresholds, windows, weights live in `config/settings.toml` or `RedlineConfig`. The only acceptable inline numbers are `0`, `1`, `-1`, and array indices.
- **No committed TODOs.** If something is incomplete, it belongs in `NOTES.md` or `ROADMAP.md` with context. Inline `TODO:` is a code smell here.
- **No global state.** Pass dependencies (DB connection, config, LLM client) explicitly. No module-level singletons.
- **Tests required for:** parsers (raw filing → structured section), the three-stage diff filter (especially Stage 1 rule-based logic), the anomaly score calculation, the eval grading logic.
- **Tests NOT required for:** trivial wrappers, pure config files, the LLM client wrapper itself (mock it), the Streamlit app surface.
- **Public API surface should be walkthrough-defensible.** Bar: walk an interviewer through any file, line by line, without flinching.

## §8 — How to work on tasks

### Adaptive turn size

Match chunk size to context. Small focused chunks when Ian is learning a new library, debugging, or thinking through a tradeoff. Larger integrated chunks when wiring known pieces together. Read the room — if Ian is asking "why did you...", shrink. If Ian is asking "wire these together," expand.

### Decision authority

- **Small calls (just make them, briefly note alternatives):** naming, internal module organization, log-message phrasing, function signatures, test names, helper utility extraction.
- **Ask first:** locked design changes, schema changes, eval harness changes, new dependencies, anything that touches the §4 push-back list.

### Severity-graded problem detection (mid-build)

When you notice something off while working:

- **Architectural concern** (current approach won't work, or breaks a §4 decision): **stop immediately.** Surface it before continuing. Don't barrel through.
- **Significant local concern** (current sub-task is fine but a related thing is wrong): finish the sub-task, then surface at end.
- **Small or future-only risk** (will matter later but not now): log briefly in `NOTES.md`, mention once, continue.

### Interview framing

Build the right engineering solution by default. The fact that this is a resume artifact is ambient context, not an active driver. Mention the interview angle only when a real engineering tradeoff materially affects the story (e.g. "the honest-framing decision in §4.7 means we explicitly avoid `signal` language in code identifiers").

## §9 — LLM usage conventions

This project uses LLMs at four points in the pipeline. Each point has a specific model assignment and a Pydantic-validated output schema.

| Stage | Where | Model | Output schema (Pydantic) |
|-------|-------|-------|--------------------------|
| Diff gate | Stage 2 of diff filter | Haiku | `DiffGateDecision` |
| Diff summary | Stage 3 of diff filter | Sonnet | `DiffSummary` |
| Correlator reasoning | Anomaly verdict synthesis | Sonnet | `CorrelatorVerdict` |
| Eval grading (fallback) | LLM-as-judge | Sonnet | `EvalJudgeVerdict` |

See `ARCHITECTURE.md` §9 for the schemas and `ARCHITECTURE.md` §10 (`llm_call_log` table) for the persistence shape.

**Rules:**

- **Every LLM call is logged to SQLite** (`llm_call_log`: model, prompt_version, tokens in/out, cost estimate, latency, call_site). No exceptions. Logging lives in the LLM client wrapper; bypassing it is a bug.
- **Every output is Pydantic-validated.** A parse failure IS a call failure — retry once with the same prompt, then mark the filing's pipeline stage as `*_failed` (see `ARCHITECTURE.md` §7).
- **Prompt templates are versioned.** Stored as `config/prompts/<name>_v<n>.txt`. Cache key includes prompt version. Bumping a version invalidates cache for that prompt.
- **Caching is aggressive.** Cache key = `(prompt_version, model, content_hash)`. During dev iteration, this is what keeps cost under $50.
- **Model selection:** Haiku for binary classification or extraction over many inputs; Sonnet for synthesis, reasoning, or judgment. Opus only with explicit motivation.
- **Use Anthropic prompt caching** when the same large context (e.g. prior filing section) is reused across calls within a batch.

## §10 — File / directory layout

Proposed structure. Subsystems live under `src/redline/`. Tests mirror source structure. Config is YAML for watchlist/events (versioned), TOML for runtime settings (via `pydantic-settings`), text for prompts.

```
redline/
  CLAUDE.md
  ARCHITECTURE.md
  NOTES.md
  ROADMAP.md
  README.md                # later, after MVP — honest framing only
  pyproject.toml           # later
  .gitignore

  config/
    watchlist.yaml         # 8 tickers + CIKs (CIK authoritative)
    eval_events.yaml       # 12 events, each with locked_at timestamp
    settings.toml          # thresholds, windows, polling cadence, weights
    prompts/
      diff_gate_v1.txt
      diff_summary_v1.txt
      correlator_v1.txt
      judge_v1.txt

  src/redline/
    __init__.py
    config.py              # RedlineConfig (pydantic-settings), YAML loaders
    poller.py              # EDGAR polling loop
    fetcher.py             # filing retrieval + caching
    parser.py              # section extraction
    pipeline.py            # status state machine, retry queue

    diff/
      __init__.py
      filter.py            # Stage 1 (rule-based)
      gate.py              # Stage 2 (Haiku)
      summarize.py         # Stage 3 (Sonnet)

    correlator/
      __init__.py
      form4.py             # Form 4 ingestion + 10b5-1 detection
      anomaly.py           # three-signal anomaly score

    storage/
      __init__.py
      db.py                # connection management (WAL, query_only)
      schema.py            # CREATE TABLE statements
      models.py            # Pydantic models for rows

    llm/
      __init__.py
      client.py            # Anthropic SDK wrapper + caching + logging
      schemas.py           # DiffGateDecision, DiffSummary, etc.
      log.py               # llm_call_log writer

    eval/
      __init__.py
      harness.py           # run eval suite
      grading.py           # binary + LLM-judge fallback
      replay.py            # point-in-time replay

    dashboard/
      app.py               # Streamlit entry

  tests/
    test_parser.py
    test_diff_filter.py
    test_anomaly.py
    test_grading.py

  data/                    # GITIGNORED
    redline.db
    fixtures/              # cached test filings
    snapshots/             # for diff regression tests
```

Alternatives considered: a flat source layout (rejected — five subsystems will grow); a separate `signals/` directory under `correlator/` (deferred — premature with three signals).

## §11 — Current status

- **Phase 0 (Planning):** COMPLETE. Eval set drafted with subsystem tags and pass criteria. Watchlist locked. Critical review pass done. Four planning docs written (this file is one of them).
- **Phase 0.5 (Day 0 spike):** NEXT. See `ROADMAP.md` Phase 0.5.
- **No code yet.** Do not write source code until Phase 0.5 hand-validation tasks are complete and the pre-registration artifact is committed.

## §12 — Maintenance of this file

`CLAUDE.md` is for **structural and durable** session guidance. Transient stuff goes elsewhere:

- New runtime gotcha → `NOTES.md`
- Schema or subsystem design change → `ARCHITECTURE.md`
- New scope decision → `CLAUDE.md` §4 (only if it's a permanent direction-setter)
- New phase / milestone → `ROADMAP.md`
- Bug, surprise, learning → `NOTES.md`

Update rule: if the same thing is explained in two consecutive sessions, it belongs here. Otherwise, it doesn't.
