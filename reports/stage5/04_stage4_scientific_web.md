# Stage 4 web connection for Stage 5-B scientific runs

Date: 2026-10-02

## Implemented path

The existing Stage 4 service accepts a bounded `scientific_reasoner` request at `POST /api/runs`, creates one run, and starts its existing background worker. The worker calls the Stage 5-B controller through `assaypilot.scientific_run_loop_cli`; the controller continues to own LLM decisions, validation, replay approval, budget accounting, result release, interpretation, and hypothesis persistence. Baseline selector routes remain available.

The browser polls the run-specific GET route and downloads the public projection. GET, history loading, and download do not start a controller or replay action. Stage 4 retains its idempotency key and one-active-run limits. The Stage 5-B controller starts its deadline after its run record is created; a resume uses its stored deadline.

## Public and private boundary

`scientific_public.py` constructs a field-allowlisted projection from a consistent read-only SQLite transaction. It exposes validated decisions and context, released observations, hash-checked public evidence, hypothesis history, exact decision-to-execution links, safe aggregate call usage, budget, and stop reason. API credentials, provider request diagnostics, hidden Oracle values, internal database paths, and private artifacts are not returned. The web download returns the same projection.

The saved Stage 5-B run `stage5b-a3dd1a84d0d84c8db2d212ed4d9f09ac` is available read-only from its preserved archive. Opening it does not invoke a provider or alter its source artifacts.

## Local use

Start the service from the AssayPilot repository with the existing Conda environment:

```bash
conda run -n drug python -m assaypilot.stage4_web --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765/> on the same host. Choose **저장된 실제 실행 기록** in the run-history selector to open the preserved run. To create a new scientific run, select **과학적 판단 agent**, choose one of the two available campaigns, and submit. Server defaults are budget `5 synthetic_credit`, 10 steps, 300 seconds, 24 LLM calls, shortlist size 24, and seed 3. Provider settings remain server-side; readiness reports their presence and leaves connection status `not_checked` until an actual run.

## One web-created run

Exactly one new run was submitted through the Stage 4 HTTP API after read-only readiness, history, and saved-run checks. The request used the expanded primary-active campaign and the specified limits. The API returned HTTP 202; no second POST, resume, or CLI-started run was used.

| Measure | Result |
| --- | --- |
| Stage 4 run | `stage4-875dc1abd26c4f06a2d232d2867b6135` |
| Service / stop state | `completed` / `max_steps` |
| Replay actions | 10 of 10 |
| LLM calls | 11 of 24 |
| Pair validation | All 10 applied selected candidate-assay pairs matched their replay execution pairs |
| Replay records | 3 released observations, all Inactive; 7 `no_record` |
| Interpretation | 3 of 3 released observations interpreted; 0 pending |
| Hypotheses | `assay_activity`: 10 total, 7 proposed and 3 weakened; `data_availability`: 0 |
| Same-hypothesis updates | All 3 Inactive observations linked to an interpretation and evidence; each corresponding same-ID update changed `proposed` to `weakened` |
| Replay budget | 3 spent, 0 reserved, 2 available `synthetic_credit` |
| Provider-reported token usage | 190,196 input; 9,956 output; 200,152 total |

The first live projection counted no-record interpretation entries as public observations interpreted, displaying 5 for 3 observations. The metric now counts only released public observations with a linked interpretation. A regression test covers this distinction. The immutable public projection advanced from revision 29 to revision 30; earlier revisions were preserved. The corrected projection reports 3 interpreted observations and 0 pending.

The final HTTP checks returned 200 for the page, JavaScript and CSS assets, the run API, its public download, and the saved-run API. The run API and download returned the same run ID and public decision set. The API projection passed the allowlisted-key check.

## Verification and browser limits

`conda run -n drug pytest -q` — **348 passed**. The focused Stage 4/public-projection tests passed (**11 passed**), including idempotency and single-run limits, polling without extra worker calls, exact observation linking, no-record metric separation, and appending a new immutable projection after an existing revision.

Browser interaction and screenshot verification were unavailable: this environment has no installed browser binary or JavaScript runtime. Therefore saved-run selection, timeline expansion, form submission by clicking, and browser refresh were not verified in a browser. HTTP/API verification is reported separately above and is not presented as browser verification. No screenshot was produced.

## Remaining deployment work

The service is local-only at `127.0.0.1:8765`; there is no external demo URL. Remote demonstration requires a deployment host, HTTPS, an authenticated reverse proxy and access policy, a supervised persistent service, protected persistent runtime storage, server-side secret provisioning, and a browser-based acceptance check on the deployed host. This work did not deploy the service.
