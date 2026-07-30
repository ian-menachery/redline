# Design decisions

Why redline is built the way it is. These were debated up front and are locked
(`CLAUDE.md` §4); this is the recruiter-readable version.

## Scope: quality over coverage
- **Fixed 8-ticker watchlist, 4 sectors.** Depth of analysis matters more than
  breadth. A universe-wide crawler is a different (and less interesting) project.
- **Scheduled, not real-time.** EDGAR's fair-access policy caps polling; the
  value here is *structured analysis*, not latency. Framing is honest: "15-minute
  scheduled monitoring."
- **Information surfacing, not alpha.** No buy/sell signals, no
  over-/under-valued verdicts. That framing invites "does it beat the market?"
  scrutiny that distracts from the engineering, and it isn't the goal.

## Disclosure diffing: the noise filter is first-class
Risk Factors are notoriously *sticky* — companies copy-paste them year to year
with minor counsel edits. Naively diffing produces mostly noise. So the diff runs
a **three-stage filter**, cheap-to-expensive:
1. **Deterministic Stage 1** — canonical-token normalization (dates, currency,
   %, large ints → placeholders) + paragraph diff. Kills the "headcount rolled,
   percentages refreshed" class *before any LLM cost*.
2. **Cheap LLM gate** — a binary "is this substantive?" classifier on survivors.
3. **Quality LLM summary** — structured, materiality-scored summary on passes.

Measured effect: normalization eliminated ~50 cosmetic diffs in a 10-Q-vs-10-Q
pair but only 3 of 117 in a 10-K-vs-10-K — 10-Ks are signal-dominated, 10-Qs
noise-dominated. The filter is the architecture, not an afterthought.

## Insider trading: define "anomalous" precisely or don't ship it
Form 4 transactions happen weekly at large firms, mostly under pre-arranged
**10b5-1 plans** that are uncorrelated with then-current filings *by design*. The
correlator therefore **excludes 10b5-1 plan trades** and scores three signals
(multi-insider cluster, per-insider volume z-score, direction flip) that the
quality LLM synthesizes into a verdict. The one graded correlator event
(Palantir/Karp, late 2024) is a **documented FAIL by design**: every Karp trade
in the window was 10b5-1, so the system correctly declined to flag it. Surfacing
a "miss" that's actually correct behavior is more honest than hiding it.

## DCF valuation: use the right tool, and say when you can't
- **Event-driven revaluation from real filing numbers** — XBRL financials +
  stated 8-K guidance, never an LLM guessing "growth seems to be slowing." Output
  is a **bear/base/bull range + sensitivity**, never a false-precision point.
- **Only where FCF-DCF applies.** Banks (SCHW, KEY) are excluded — unlevered-FCF
  DCF is wrong for financials. High-multiple / turnaround names (NET, PLTR, MRNA,
  CVNA) are **monitored, not valued**: a forward FCF-DCF doesn't reconcile, and a
  feasibility gate showed a *reverse* DCF can't rationalize their prices in a
  plausible growth band either (`NOTES.md` §12). Refusing to print a misleading
  number is the decision.
- **Cost of capital is a dated manual constant**, not a live feed — keeping the
  "no market data" line clean; the dashboard greys stale reference prices.

## Evaluation: pre-registered and locked
Accuracy is measured against a **pre-registered, locked** event set
(`config/eval_events.yaml`, `locked_at` per entry) so cherry-picking is
structurally impossible. Fresh events go in a separate live-operation log, never
into the graded set. The harness runs deterministically against an isolated DB;
results are published to `EVAL.md`.

## Engineering guardrails
- Everything typed (mypy) and linted (ruff) in CI on 3.11 + 3.12; ≥70% coverage.
- Pydantic validates all config and every LLM output; every LLM call is logged
  with cost, behind a hard spend cap.
- Dashboards are strictly read-only (no keys, no writes) and render only baked
  snapshot data — never importing the engine or reading config at load time
  (a Streamlit-Cloud stale-venv lesson).
