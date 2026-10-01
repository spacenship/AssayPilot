# Stage 3-C Offline Evaluation

Stage 3-C evaluates completed Stage 3-B runs. It does not select candidates, approve actions, execute assays, modify budgets, train a predictor, or infer potency. The supported evaluation target is the public `mep2-confirmatory` assay (PubChem AID 2272), whose outcome is categorical Activity Outcome. An Active observation means active in this assay; it is not a direct-binding, IC50, or clinical-efficacy claim.

## Inputs and result archive

The evaluator reads a registered baseline plan, `execution_summary.json`, each run's `summary.json` and `trace.json`, the public campaign snapshot, and a per-run `published_results.json` archive. Stage 3-B's earlier baseline retained result IDs and hashes, but not the public Observation values or payloads after its temporary runtime database was removed. Those artifacts cannot independently recover whether a result was Active or Inactive.

The archive supplement was produced by a new, separately registered baseline revision. It copies only executions returned as released by the trusted Publication Reader. Each archived execution includes its execution/action/candidate/assay/step identity, public `ExecutionReceipt` and completed `ExecutionResult`, full Observation fields, EvidenceRef, allowlisted source payload, and canonical payload SHA-256. No private result object, unreleased measurement, or candidate-to-hidden-label map is serialized. The archive schema is `assaypilot.stage3c.published-results.v1`; its path and file hash are recorded in the run summary and execution summary. A payload hash detects changes; it is not a signature or source-authentication proof.

The evaluator verifies the plan's original bytes against the suite's plan hash; exact planned and executed run sets; registered selector, seed, version, budget and execution limits; snapshot and artifact paths; summary/trace/archive hashes and identities; contiguous steps; unique execution IDs; terminal status counts; budget checkpoints and final conservation; Observation and EvidenceRef contracts; evidence payload hashes; and SID/AID/CID/source-row consistency. It rejects an invalid run and marks the overall evaluation incomplete rather than silently omitting that run.

## Evaluation universe and information boundary

The public `Campaign` is loaded through `PublicBundleAdapter`. Candidate identity is resolved from the public `candidate.source_id` form `SID:<integer>`. SID is the source unit; CID is retained in evidence and is never used to merge candidates. The target assay's public prerequisite rules define the eligible universe `U`. This evaluator supports prerequisites that point to primary assays; unsupported non-primary prerequisite paths fail explicitly.

The trusted truth adapter uses the snapshot's validated `ReplayStore` to obtain normalized measurements for `(SID, AID)`. It does not pass hidden labels to the selector worker or run loop. Hidden labels are held in evaluator memory only and are not written as per-candidate output. The registered evaluation specification records public and hidden-source fingerprints for reproducibility; SHA-256 values detect later changes but do not establish external provenance.

For each candidate/assay unit, labels are interpreted as follows:

- `Active` is positive and `Inactive` is negative.
- Inconclusive, Unspecified, and other non-binary outcomes are unknown.
- No source measurement is missing; conflicting normalized verdicts are ambiguous.
- A replay `no_record`, failed action, cancelled action, or unreleased action is not a negative result.
- Repeated observations for one candidate/assay are grouped once. Conflicting labels stay ambiguous; the evaluator never chooses the favorable row.
- An identical target-assay result already public at the initial snapshot is excluded from new-hit and new-positive-recall counts. Initial primary-screen Active is only an eligibility condition.

`K` is the number of units in `U` with a resolved binary label. `P_new` is the number of known Active units in `U` that were not already public for the same follow-up assay at run start. Recall is therefore recall of source-known positives in this snapshot, not recall over unmeasured compounds or all biologically active compounds. No missing-at-random assumption is made.

## Metrics and curves

Only released observations contribute scientific results. For one run:

- `H`: distinct newly released Active candidate/assay units.
- `L`: distinct newly released binary candidate/assay units (Active or Inactive).
- `H/L`: observed positive fraction among new binary results, not precision over all candidates; null when `L = 0`.
- `known_positive_recall = H / |P_new|`; null with reason `no_known_new_positive` when the denominator is zero.
- `positives_per_credit = H / spent`; null with reason `zero_spent` when no credits were spent.
- `first_positive_step` and `first_positive_spent` are null if no new positive was released.
- Record yield is released unique executions divided by terminal execution outcomes (`released`, `no_record`, `failed`, or `cancelled`). No-record fraction uses the same denominator. Rejected and pending steps are reported separately and are not completed executions.
- Initial, spent, reserved, and available budget are kept as Decimal strings with unit and assumed flag. `synthetic_credit` is a replay assumption, not an experimental cost. A free no-record lookup means spend alone does not measure the number of candidates explored.

The evaluator writes per-step and per-spend cumulative counts. Repeated spend coordinates are retained as observed; there is no interpolation or future-state extension. Common comparisons use the actual shared step range and actual shared spend range within each snapshot. Seeded-random results are all listed; aggregates report defined-value `n`, mean, sample SD (`ddof=1`), minimum, and maximum. Fixed order is a single separate run. Five seeds describe order variation on this campaign, not five independent targets. No AUC, significance test, or winner claim is produced.

## CLI and outputs

From the repository root, choose an unused evaluation ID, register that directory before computing metrics, then evaluate it:

```bash
evaluation_id=local-evaluation-001

conda run -n drug python -m assaypilot.evaluation \
  --register-spec \
  reports/stage3/baselines/20260930-stage3c-publication-archive-r2 \
  "reports/stage3/evaluations/$evaluation_id" \
  mep2-confirmatory

conda run -n drug python -m assaypilot.evaluation \
  --evaluate "reports/stage3/evaluations/$evaluation_id/evaluation_spec.json"
```

The registered `evaluation_spec.json` freezes the plan and artifact hashes, source fingerprints, implementation hashes, assay label rule, unit, denominator, and comparison axes before scoring. This is an evaluation-rule registration, not a pre-run scientific-hypothesis registration. A separate archive-only mode is available:

```bash
evaluation_id=local-archive-only-001

conda run -n drug python -m assaypilot.evaluation \
  --register-spec \
  reports/stage3/baselines/20260930-stage3c-publication-archive-r2 \
  "reports/stage3/evaluations/$evaluation_id" \
  mep2-confirmatory

conda run -n drug python -m assaypilot.evaluation \
  --evaluate "reports/stage3/evaluations/$evaluation_id/evaluation_spec.json" \
  --without-truth
```

Archive-only mode validates public Observation/evidence relationships and calculates operations, `H`, `L`, and `H/L`. Truth-dependent universe, missing-label, and recall fields are null with `truth_unavailable` where applicable. It does not parse hidden measurements into labels; registration still records the hidden-source SHA-256 fingerprint required by the evaluation specification.

The output directory contains `validation.json`, `run_metrics.json`, `curves.json`, `aggregate_metrics.json`, `comparison.md`, and SVG cumulative-positive curves. A repeat evaluation must use a new output directory; compare metric/curve/aggregate values after ignoring the evaluation ID. The implementation currently accepts the registered fixed-order plus five seeded-random baseline contract on the two Stage 3-B snapshots. A future model selector can use these metric functions and fixed execution conditions, but adding a selector requires an explicit, versioned plan-contract extension before it can be evaluated here.
