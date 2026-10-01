# Stage 3-C baseline evaluation

Evaluation: `20260930-stage3c-eval-r2-r2`; validation status: **incomplete**.

Only released `mep2-confirmatory` observations count as newly found results. `active` is positive, `inactive` negative, and inconclusive/unspecified/conflicted results remain unknown or ambiguous. The primary-screen label is only an eligibility prerequisite.

Recall denominator is the snapshot-known binary positive set inside the public prerequisite-eligible candidate universe, excluding an identical follow-up result already public at run start. It is not recall over unmeasured compounds or the biological target in general.

`synthetic_credit` is an assumed replay budget unit, not an experimental cost. `H/L` is the active fraction among newly released binary results, not precision. Recovery controls are excluded.

## revision-20260917-r2

| selector / seed | steps | released | no_record | spent | new Active H | new binary L | H/L | known-positive recall | first positive step |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| random seed None | — | — | — | — | — | — | — | — | — |
| random seed None | — | — | — | — | — | — | — | — | — |
| random seed None | — | — | — | — | — | — | — | — | — |
| random seed None | — | — | — | — | — | — | — | — | — |
| random seed None | — | — | — | — | — | — | — | — | — |
| random seed None | — | — | — | — | — | — | — | — | — |

Seeded random n(H)=0, mean=null, sample SD=null, range=null–null; recall n=0, mean=null, sample SD=null.

Common actual range: step 1–None; spent 0–0 synthetic_credit. Curves stop at observed states; cost duplicates are retained.

Curves: `figures/r2-cumulative-positive-by-step.svg` and `figures/r2-cumulative-positive-by-cost.svg`.

## revision-20260918-primary-active-all

| selector / seed | steps | released | no_record | spent | new Active H | new binary L | H/L | known-positive recall | first positive step |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| random seed None | — | — | — | — | — | — | — | — | — |
| random seed None | — | — | — | — | — | — | — | — | — |
| random seed None | — | — | — | — | — | — | — | — | — |
| random seed None | — | — | — | — | — | — | — | — | — |
| random seed None | — | — | — | — | — | — | — | — | — |
| random seed None | — | — | — | — | — | — | — | — | — |

Seeded random n(H)=0, mean=null, sample SD=null, range=null–null; recall n=0, mean=null, sample SD=null.

Common actual range: step 1–None; spent 0–0 synthetic_credit. Curves stop at observed states; cost duplicates are retained.

Curves: `figures/expanded-cumulative-positive-by-step.svg` and `figures/expanded-cumulative-positive-by-cost.svg`.

## Interpretation limits

These two snapshots are evaluated separately. Their repeated records do not establish missing-at-random coverage, and five seeds measure order variation on the same campaign rather than five independent targets. No ROC/PR AUC, enrichment factor, significance test, or winner claim is calculated.
