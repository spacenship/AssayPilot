# Stage 3-C Evaluation Report

## Result

Stage 3-C is implemented and evaluated. Only public results that were released during a run count toward `H` and `L`. Snapshot truth is accessed by a separate trusted evaluator path and is never written as a candidate-level result map. Both snapshots have six scored baselines: one fixed-order run and five seeded-random runs. The recovery control is reported by the runner and excluded from score aggregation.

Implementation files: `src/assaypilot/evaluation.py` adds the registered offline evaluator and CLI; `scripts/verify_stage3_baselines.py` supplements future baseline runs with a hash-checked archive of results returned by the Publication Reader; `tests/test_evaluation.py` covers the metric, archive, and information-boundary contracts. `docs/stage3_evaluation.md` documents the contract and commands.

## 3-B handoff and preservation

The original 3-B baseline at `reports/stage3/baselines/20260930-seeded-priority-v1` preserved trace IDs and hashes, but not released Observation values and evidence payloads. Since its private runtime database had been removed, the old trace alone could not recover the published Active/Inactive labels. It was not reconstructed from hidden data or overwritten.

The new archive baseline is `reports/stage3/baselines/20260930-stage3c-publication-archive-r2`, registered before execution with plan SHA-256 `79555f0bba18316b8f7e3620a9fefe46b7bf376d39dde1f6e3f8cd63c46042bf`. Its per-run `published_results.json` contains only Publication Reader released results, Observation contracts, EvidenceRefs, and allowlisted source payloads with hashes. The recovery control has its own archive but is not one of the 12 baseline observations.

All 12 new runs exactly matched the corresponding old run's ordered `(candidate_id, assay_id)` sequence, per-step status and budget checkpoint (`spent`, `reserved`, `available`), and stop reason. The runner also recorded `resumed_action_order_matches_control: true` for the interrupted/resumed seed-0 run and its uninterrupted control.

Before/after preservation checks found all 28 files of the original baseline unchanged. All 56 files recorded in the r2 snapshot manifest and all 128 files in the expanded snapshot manifest still match their manifest SHA-256 values. Their pre/post tree digests also match. No prior snapshot, plan, or report was replaced.

The first archive-run attempt, `20260930-stage3c-publication-archive-r1`, stopped during suite summary writing after the runner tried to read a nonexistent `cancelled_executions` summary field. Its first completed run and archive were retained. The runner summary was corrected to derive cancellation count from the trace, a new r2 plan was registered, and r2 completed.

Intermediate evaluator attempts were retained separately: `...eval-r2-r1` failed during spec registration because the evaluator omitted the selector-version import; `...eval-r2-r2` registered but rejected all runs because it expected receipt fields that are not part of the `ExecutionReceipt` contract; the first archive-only evaluation treated public context as hidden truth and rejected labels. These code paths were corrected and covered by focused tests. `...eval-r2-r3` and `...eval-r2-r4` were complete truth-enabled runs under the earlier evaluator version. The final evaluations below were each registered against the corrected implementation and are the reported outputs.

## Evaluation population

The evaluator uses public SID source IDs for candidate identity and the public primary-assay prerequisite for `mep2-confirmatory` (AID 2272). Hidden measurements are loaded from each snapshot's validated ReplayStore only inside the trusted evaluator. `Active` is positive, `Inactive` negative, and other/unresolved labels are not coerced to negative. Both snapshots had zero target-assay observations already public at the starting snapshot.

| Snapshot | U: eligible public units | K: known binary | P_new: known Active | Missing | Ambiguous | Unknown | Conflicts |
|---|---:|---:|---:|---:|---:|---:|---:|
| `revision-20260917-r2` | 5 | 1 | 0 | 4 | 0 | 0 | 0 |
| `revision-20260918-primary-active-all` | 1,682 | 295 | 30 | 1,387 | 0 | 0 | 0 |

Thus r2 has no snapshot-known new positive in its recall denominator; recall is null, not zero. In the expanded snapshot, the 30 known Active labels are the recall denominator. These figures describe the available snapshot only; they do not describe unmeasured candidates as negatives.

Across the union of source rows actually released by the six runs per snapshot, the archives contain one distinct r2 source measurement (`Inactive`) and 29 distinct expanded source measurements (2 `Active`, 27 `Inactive`). The evaluator matched each released source row by SID/AID, measurement/source-row identity, and normalized label. The expanded fixed-order run and random seed 3 each released one Active; other random runs released none.

## Per-run metrics

`H/L` is the observed Active fraction among newly released binary results. Recall is `H / P_new`. Budget is assumed `synthetic_credit`, not a real assay expense. Failed and cancelled counts were zero for all 12 runs.

| Snapshot | Run | Steps | Released | no_record | Spent | H | L | H/L | Known-positive recall | First positive (step / spent) | Stop |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| r2 | fixed | 5 | 1 | 4 | 1 | 0 | 1 | 0 | null (P_new=0) | — | max_steps |
| r2 | random seed 0 | 5 | 1 | 4 | 1 | 0 | 1 | 0 | null (P_new=0) | — | max_steps |
| r2 | random seed 1 | 5 | 1 | 4 | 1 | 0 | 1 | 0 | null (P_new=0) | — | max_steps |
| r2 | random seed 2 | 5 | 1 | 4 | 1 | 0 | 1 | 0 | null (P_new=0) | — | max_steps |
| r2 | random seed 3 | 5 | 1 | 4 | 1 | 0 | 1 | 0 | null (P_new=0) | — | max_steps |
| r2 | random seed 4 | 5 | 1 | 4 | 1 | 0 | 1 | 0 | null (P_new=0) | — | max_steps |
| expanded | fixed | 30 | 4 | 26 | 4 | 1 | 4 | 0.25 | 1/30 (0.0333) | 6 / 2 | max_steps |
| expanded | random seed 0 | 28 | 5 | 23 | 5 | 0 | 5 | 0 | 0 | — | budget_exhausted |
| expanded | random seed 1 | 26 | 5 | 21 | 5 | 0 | 5 | 0 | 0 | — | budget_exhausted |
| expanded | random seed 2 | 20 | 5 | 15 | 5 | 0 | 5 | 0 | 0 | — | budget_exhausted |
| expanded | random seed 3 | 14 | 5 | 9 | 5 | 1 | 5 | 0.20 | 1/30 (0.0333) | 3 / 1 | budget_exhausted |
| expanded | random seed 4 | 24 | 5 | 19 | 5 | 0 | 5 | 0 | 0 | — | budget_exhausted |

Seeded-random aggregates, with five runs per snapshot:

| Snapshot | Metric | n | Mean | Sample SD | Min–max |
|---|---|---:|---:|---:|---:|
| r2 | H | 5 | 0 | 0 | 0–0 |
| r2 | H/L | 5 | 0 | 0 | 0–0 |
| r2 | known-positive recall | 0 | null | null | null |
| expanded | H | 5 | 0.2 | 0.4472 | 0–1 |
| expanded | H/L | 5 | 0.04 | 0.0894 | 0–0.2 |
| expanded | known-positive recall | 5 | 0.00667 | 0.01491 | 0–0.0333 |

The actual shared comparison range was step 1–5 and spend 0–1 credit for r2, and step 1–14 and spend 0–4 credits for expanded. The expanded fixed run completed 30 steps; random seeds completed 14–28. Endpoint differences remain visible and are not treated as equal-budget or equal-step strategy comparisons. No significance test or winner claim is made.

## Artifacts and validation

The final truth-enabled evaluation was registered twice to test repeatability. The two output directories are `reports/stage3/evaluations/20260930-stage3c-eval-final-r1` and `...-final-r2`; both validate all 12 runs. After excluding the evaluation ID, the parsed JSON values in `run_metrics.json`, `curves.json`, and `aggregate_metrics.json` are equal.

The archive-only evaluation at `reports/stage3/evaluations/20260930-stage3c-eval-final-archive-only` also validates all 12 runs without parsing hidden measurements into truth labels. Its H and L match the truth-enabled outputs for every run; truth-dependent universe and recall fields are explicitly unavailable. The registered spec still records the hidden-source SHA-256 fingerprint, as required for reproducibility.

Each final evaluation directory contains four SVG curves: step and synthetic-credit axes for r2 and expanded. For example, the truth-enabled final-r1 curves are [r2 by step](evaluations/20260930-stage3c-eval-final-r1/figures/r2-cumulative-positive-by-step.svg), [r2 by cost](evaluations/20260930-stage3c-eval-final-r1/figures/r2-cumulative-positive-by-cost.svg), [expanded by step](evaluations/20260930-stage3c-eval-final-r1/figures/expanded-cumulative-positive-by-step.svg), and [expanded by cost](evaluations/20260930-stage3c-eval-final-r1/figures/expanded-cumulative-positive-by-cost.svg). They are generated from the saved `curves.json`, including zero-positive curves.

Commands run in the requested `drug` conda environment:

```text
conda run -n drug pytest -q tests/test_evaluation.py
14 passed

conda run -n drug pytest -q
273 passed in 6.43s

conda run -n drug python -m assaypilot.evaluation --evaluate reports/stage3/evaluations/20260930-stage3c-eval-final-r1/evaluation_spec.json
12/12 passed; overall_status=complete

conda run -n drug python -m assaypilot.evaluation --evaluate reports/stage3/evaluations/20260930-stage3c-eval-final-archive-only/evaluation_spec.json --without-truth
12/12 passed; overall_status=complete
```

The fixture suite checks released-only recall, missing/unknown/ambiguous outcomes, zero denominators and zero spend, repeated observations, assay-specific units, cost and step curves, sample-SD aggregation, receipt/Observation/EvidenceRef linkage, candidate SID/AID mapping, and altered archive rejection. Existing Stage 3-B run-loop, recovery, and selector-isolation tests are included in the 273-test full regression.

## Limits and next comparison point

The snapshots have partial follow-up coverage (1,387 of 1,682 eligible expanded units are missing), so recall is limited to source-known positives and does not estimate all biological hits. No unknown/ambiguous labels occurred in these snapshots; their behavior is covered by fixtures, not claimed as an observed-data result. `synthetic_credit` is assumed replay accounting. Five random seeds are repeat orders on one campaign, not independent targets.

Future model selectors can be compared using the same snapshots, assay, eligibility rules, budget, stop limits, released-result archive, H/L/recall definitions, and curve axes. The current registered-plan validator deliberately accepts only the fixed-order and five seeded-random Stage 3-B runs. A model-selector comparison therefore needs an explicit versioned plan-contract extension; this Stage 3-C implementation does not add or train that selector. Stage 3-C ends here.
