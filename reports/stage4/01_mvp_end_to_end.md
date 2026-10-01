# Stage 4 MVP: end-to-end report

Date: 2026-10-01 (Asia/Seoul)

## Implemented

- Added a local web page and HTTP API. A browser request creates one durable run; a separate worker process invokes the existing `assaypilot.run_loop_cli` entry point. Selector isolation, approval and budget checks, replay lookup, and publication remain in the existing Stage 2/3 execution path.
- Added strict request validation and an allowlist for the two preserved snapshots and the `fixed_order` and `seeded_random_priority` selectors. The server enforces one active run, UUID idempotency keys, bounded `budget`/`max_steps`, and separates service status (`queued`, `running`, `completed`, `interrupted`, `failed`) from the loop `stop_reason`.
- Added restart-visible run state, worker recovery checks, progress artifact generations, and public downloads. Public evidence references contain `evidence_id`, `source_kind`, and `source_id`; runtime-relative locations are removed. Result observations are taken from the run-bound public reader after release. `no_record` is a step status and is not turned into an Inactive observation.
- Runtime state defaults to `runtime/stage4/`, outside the snapshots. The directory is gitignored. The service creates private runtime directories/files with restrictive permissions. It binds to loopback only and does not provide authentication or an external URL.
- Added a web interface with the three requested saved configurations, run progress, released results/evidence, run history, and a download link. API errors do not return worker tracebacks or private filesystem paths.

## Local service

The service was started in the `drug` Conda environment and remained available at the end of verification:

```bash
cd /data1/miplab/wjyang/AssayPilot
conda run -n drug python -m assaypilot.stage4_web --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765/> on the same host. `/api/readiness` returned HTTP 200 with both actual bundles available (5 and 1,682 public candidates) and the isolated selector sandbox available. The page and its JS/CSS assets returned HTTP 200. The API and download routes were exercised locally. No external/public service address exists. A browser binary or browser automation package was unavailable in the `drug` environment, so browser rendering and interactive visual behavior were not independently automated.

## Actual example runs

All three runs used the actual preserved snapshots, the existing replay CLI, the configured isolated selector, and the service API. Budget is `synthetic_credit` with `assumed=true`; it is a replay budget, not a wet-lab budget. `H` is released Active follow-up pairs; `L` is released Active or Inactive follow-up pairs. `no_record` means this snapshot has no linked follow-up record for that attempted action.

| Example / run ID | Campaign | Selector | Budget / max steps | Result |
| --- | --- | --- | --- | --- |
| A · `stage4-94107ad741a84ed798d6262fc34bebb7` | `revision-20260917-r2` (5 candidates) | `fixed_order` | 5 / 5 | Service `completed`; stop `max_steps`; 5 steps, 1 released Inactive observation, 4 `no_record`, 0 failed; spent 1, available 4; H=0, L=1, H/L=0.0. |
| B · `stage4-0160c1d8677d414f8a07d4035e88a5d7` | `revision-20260918-primary-active-all` (1,682 candidates) | `fixed_order` | 5 / 10 | Service `completed`; stop `max_steps`; 10 steps, 2 released observations (1 Active, 1 Inactive), 8 `no_record`, 0 failed; spent 2, available 3; H=1, L=2, H/L=0.5. |
| C · `stage4-9d64220199fc49e5bf65aab2c43055e9` | `revision-20260918-primary-active-all` (1,682 candidates) | `seeded_random_priority`, seed 3, `random-priority-v1` | 5 / 30 | Service `completed`; stop `budget_exhausted`; 14 steps, 5 released observations (1 Active, 4 Inactive), 9 `no_record`, 0 failed; spent 5, available 0; H=1, L=5, H/L=0.2. |

The selected candidate IDs and step outcomes below come from each saved public trace. The order is the order actually returned by the configured selector.

### A — fixed order, r2

1. `candidate-1a57bb2924b5be73bdfa` — `no_record`
2. `candidate-2c73f8343bf257a5b5f9` — `no_record`
3. `candidate-49b7e78f5e670ee32961` — released Inactive
4. `candidate-54837185aa746c46406e` — `no_record`
5. `candidate-e47244fd029571b8e1dd` — `no_record`

### B — fixed order, expanded snapshot

1. `candidate-0013fe448f989571e188` — released Inactive
2. `candidate-0032e9c084a637f026de` — `no_record`
3. `candidate-00748f56c546b376caa6` — `no_record`
4. `candidate-007fb99acaca74d1eaac` — `no_record`
5. `candidate-0084b1c2def80ae4bb71` — `no_record`
6. `candidate-0088aecf0827f08f2c77` — released Active
7. `candidate-00a4e98acea3c480fd81` — `no_record`
8. `candidate-00b4f983de847345d167` — `no_record`
9. `candidate-00b8a320a832d52c34f4` — `no_record`
10. `candidate-0105cb3d860be9a063e2` — `no_record`

### C — seeded random priority, expanded snapshot

1. `candidate-8d4997a5a558d2ba37b2` — `no_record`
2. `candidate-69bcd1a3a7beae612e19` — `no_record`
3. `candidate-95453bbec38742e1b73e` — released Active
4. `candidate-66703067a37d48122b03` — `no_record`
5. `candidate-27f47400200d5773f193` — `no_record`
6. `candidate-4e2bb09e1873d198de35` — `no_record`
7. `candidate-7ff54a8a2966fc88456a` — `no_record`
8. `candidate-4849f0b862a483344e77` — released Inactive
9. `candidate-62130c39c60c65888641` — `no_record`
10. `candidate-d9ef19a3e0fd26cb336d` — released Inactive
11. `candidate-8b72d6804c40400303b5` — `no_record`
12. `candidate-60bd5da702a5571f40e3` — `no_record`
13. `candidate-5499f5dc314cc3bda4c8` — released Inactive
14. `candidate-2b9f48d1aee52acd0df9` — released Inactive

These are replay outputs for the configured snapshots. They do not establish selector quality or predict experimental activity.

## Persistence and public download checks

- Repeated an actual create request with the same idempotency key and same normalized configuration; the API returned the existing run (`idempotent_replay=true`). A conflicting reuse of a key is covered by the service test and returns `idempotency_conflict`.
- Restarted the web service, then read the prior run and downloaded all three public exports. Run status and artifacts remained available.
- Reparsed each downloaded JSON export; checked that every released Observation's evidence IDs resolve within the included evidence and that canonical evidence JSON matches its SHA-256. Recomputed H/L from public released observations and checked it against the summary; checked step count against trace length.
- A legacy A artifact had been written before runtime-location redaction was added. The current API strips `EvidenceRef.location` while serving that saved export; the post-restart download check confirmed the private runtime location was absent. The source protocol URL remains in the public evidence payload.
- Actual readiness verified the snapshots and isolated-selector probe. Invalid campaign, over-limit budget, and a seeded selector without a seed each returned HTTP 400 with a stable error code.

## Tests and remaining limits

Final verification in `conda` environment `drug`:

```text
conda run -n drug python -m pytest -q
278 passed in 7.32s

conda run -n drug python -m compileall -q \
  src/assaypilot/stage4_service.py src/assaypilot/stage4_web.py \
  src/assaypilot/stage4_worker.py tests/test_stage4_service.py
passed

git diff --check
passed
```

Stage 4 service tests cover request validation, canonical decimal budgets, idempotent replay and conflicting key use, single-run concurrency, safe worker failure without a fabricated public artifact, service status vs. stop reason, and evidence-location redaction. The failed-worker case is a synthetic fixture test, not a deliberately corrupted production snapshot run.

There is no authentication, remote deployment, or public URL. The UI was served and its assets/API were checked, but no real browser rendering test was possible in this environment. Stage 4 does not add a wet-lab executor, train a model, or define a success classifier; the displayed observations and budget are limited to the existing replay/publication contracts.
