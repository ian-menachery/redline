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

## FCF-base validation

| Ticker | Validated |
|---|---|
| CVNA | yes |
| MRNA | yes |
| NET | yes |
| PLTR | yes |
| ULTA | yes |
| VRTX | yes |

