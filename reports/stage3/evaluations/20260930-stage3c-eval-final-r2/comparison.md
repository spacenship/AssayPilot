# Stage 3-C baseline evaluation

Evaluation: `20260930-stage3c-eval-final-r2`; validation status: **complete**.

Only released `mep2-confirmatory` observations count as newly found results. `active` is positive, `inactive` negative, and inconclusive/unspecified/conflicted results remain unknown or ambiguous. The primary-screen label is only an eligibility prerequisite.

Recall denominator is the snapshot-known binary positive set inside the public prerequisite-eligible candidate universe, excluding an identical follow-up result already public at run start. It is not recall over unmeasured compounds or the biological target in general.

`synthetic_credit` is an assumed replay budget unit, not an experimental cost. `H/L` is the active fraction among newly released binary results, not precision. Recovery controls are excluded.

## revision-20260917-r2

| selector / seed | steps | released | no_record | spent | new Active H | new binary L | H/L | known-positive recall | first positive step |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fixed_order | 5 | 1 | 4 | 1 | 0 | 1 | 0 | null | null |
| random seed 0 | 5 | 1 | 4 | 1 | 0 | 1 | 0 | null | null |
| random seed 1 | 5 | 1 | 4 | 1 | 0 | 1 | 0 | null | null |
| random seed 2 | 5 | 1 | 4 | 1 | 0 | 1 | 0 | null | null |
| random seed 3 | 5 | 1 | 4 | 1 | 0 | 1 | 0 | null | null |
| random seed 4 | 5 | 1 | 4 | 1 | 0 | 1 | 0 | null | null |

Seeded random n(H)=5, mean=0, sample SD=0, range=0–0; recall n=0, mean=null, sample SD=null.

Common actual range: step 1–5; spent 0–1 synthetic_credit. Curves stop at observed states; cost duplicates are retained.

Curves: `figures/r2-cumulative-positive-by-step.svg` and `figures/r2-cumulative-positive-by-cost.svg`.

## revision-20260918-primary-active-all

| selector / seed | steps | released | no_record | spent | new Active H | new binary L | H/L | known-positive recall | first positive step |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| fixed_order | 30 | 4 | 26 | 4 | 1 | 4 | 0.25 | 0.0333 | 6 |
| random seed 0 | 28 | 5 | 23 | 5 | 0 | 5 | 0 | 0 | null |
| random seed 1 | 26 | 5 | 21 | 5 | 0 | 5 | 0 | 0 | null |
| random seed 2 | 20 | 5 | 15 | 5 | 0 | 5 | 0 | 0 | null |
| random seed 3 | 14 | 5 | 9 | 5 | 1 | 5 | 0.2 | 0.0333 | 3 |
| random seed 4 | 24 | 5 | 19 | 5 | 0 | 5 | 0 | 0 | null |

Seeded random n(H)=5, mean=0.2, sample SD=0.4472, range=0–1; recall n=5, mean=0.0067, sample SD=0.0149.

Common actual range: step 1–14; spent 0–4 synthetic_credit. Curves stop at observed states; cost duplicates are retained.

Curves: `figures/expanded-cumulative-positive-by-step.svg` and `figures/expanded-cumulative-positive-by-cost.svg`.

## Interpretation limits

These two snapshots are evaluated separately. Their repeated records do not establish missing-at-random coverage, and five seeds measure order variation on the same campaign rather than five independent targets. No ROC/PR AUC, enrichment factor, significance test, or winner claim is calculated.
