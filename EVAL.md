# Eval results

Generated from the persisted `eval_runs` table by `python -m redline.eval.report`. The graded event set is pre-registered and locked (`config/eval_events.yaml`); see CLAUDE.md section 4.5.

## Pre-registered events

**Global: 2/3 passed.**

| Subsystem | Score |
|---|---|
| correlator | 0/1 |
| diff_analyzer | 2/2 |

| Event | Subsystems | Binary | Result | Notes |
|---|---|---|---|---|
| cvna_10k_fy22 | diff_analyzer | pass | PASS | pass_criteria satisfied for accession 0001690820-23-000052 |
| key_10k_fy22 | diff_analyzer | pass | PASS | pass_criteria satisfied for accession 0000091576-23-000026 |
| pltr_karp_form4_2024 | correlator | fail | FAIL | pass_criteria evaluated False for accession 0001321655-24-000209 |

## Guidance extraction (8-K)

Panel selected by mechanical Rule R, locked at `2026-07-30T13:58:28Z` (tag `guidance-eval-registration-v1`).

**Panel size:** full n = 12 accessions (6 companies); held-out (never-seen) n = 8 accessions (5 companies).

**Full panel** (all registered accessions):

| Metric | Precision | Recall | F1 | TP | FP | FN |
|---|---|---|---|---|---|---|
| comparable_sales | 0.000 | n/a | n/a | 0 | 2 | 0 |
| ebitda | 1.000 | 1.000 | 1.000 | 1 | 0 | 0 |
| eps | 0.833 | 0.833 | 0.833 | 5 | 1 | 1 |
| operating_income | 0.750 | 1.000 | 0.857 | 9 | 3 | 0 |
| other | 0.250 | 1.000 | 0.400 | 2 | 6 | 0 |
| revenue | 0.923 | 1.000 | 0.960 | 12 | 1 | 0 |

**Held-out sub-panel** (never-seen accessions only, `previously_observed: false`):

| Metric | Precision | Recall | F1 | TP | FP | FN |
|---|---|---|---|---|---|---|
| comparable_sales | 0.000 | n/a | n/a | 0 | 2 | 0 |
| ebitda | 1.000 | 1.000 | 1.000 | 1 | 0 | 0 |
| eps | 0.750 | 0.750 | 0.750 | 3 | 1 | 1 |
| operating_income | 0.500 | 1.000 | 0.667 | 3 | 3 | 0 |
| other | 0.000 | n/a | n/a | 0 | 6 | 0 |
| revenue | 0.857 | 1.000 | 0.923 | 6 | 1 | 0 |

## FCF-base validation

| Ticker | Validated |
|---|---|
| CVNA | yes |
| MRNA | yes |
| NET | yes |
| PLTR | yes |
| ULTA | yes |
| VRTX | yes |

## Reproducibility

The graded eval runs deterministically against an isolated, freshly-seeded database, so a result never depends on prior state:

```
python -m redline.eval.harness --all --db-path data/eval_run.db --fresh
```

The guidance-extraction panel is selected by a mechanical rule (Rule R) over persisted DB state — a pure, deterministic read (no network at selection time). Reproduce end-to-end:

```
python scripts/backfill_8ks.py --months 15   # live EDGAR, no LLM
python -m redline.valuation.guidance --once   # extraction (needs an LLM key)
python -m redline.valuation.guidance_eval     # precision/recall on scope=total
python -m redline.eval.report                 # regenerate this file
```

