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

| Metric | Precision | Recall | F1 | TP | FP | FN |
|---|---|---|---|---|---|---|
| ebitda | 1.000 | 1.000 | 1.000 | 1 | 0 | 0 |
| eps | 1.000 | 1.000 | 1.000 | 4 | 0 | 0 |
| operating_income | 1.000 | 1.000 | 1.000 | 10 | 0 | 0 |
| other | 0.750 | 1.000 | 0.857 | 3 | 1 | 0 |
| revenue | 1.000 | 1.000 | 1.000 | 10 | 0 | 0 |

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

