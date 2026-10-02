# Stage 4 MVP service

## What it runs

The browser submits a bounded request to the local Stage 4 service. The service creates one persistent run and starts a separate worker process. Baseline requests invoke the existing `assaypilot.run_loop_cli`; scientific requests invoke `assaypilot.scientific_run_loop_cli`, which runs the existing Stage 5-B controller. Both reuse the trusted approval and budget checks, `ReplayOracle`, and publication flow. Stage 4 adds the web request boundary and saves a public-only view of the run.

The service supports only these preserved campaigns:

| Campaign key | Snapshot | Public candidates |
| --- | --- | ---: |
| `revision-20260917-r2` | `data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2` | 5 |
| `revision-20260918-primary-active-all` | `data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all` | 1,682 |

The `fixed_order` selector chooses the first currently executable `(candidate_id, assay_id)` in sorted order. `seeded_random_priority` orders eligible actions using the Stage 3 fixed-seed SHA-256 priority and records the seed and algorithm version. Neither selector predicts activity. `synthetic_credit` is an assumed replay budget; it is not a wet-lab budget.

## Install and start

From the repository root, use the existing `drug` Conda environment:

```bash
conda run -n drug python -m pip install -e .
conda run -n drug python -m assaypilot.stage4_web --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765/> on the same machine. The server binds only to IPv4 loopback. It intentionally has no user authentication and refuses non-loopback binds; remote access needs a separately configured authenticated deployment proxy. Stop the foreground server with Ctrl-C. There is no external demo URL in this MVP.

Readiness at `/api/readiness` checks both preserved public bundles and starts a real isolated-selector probe. If the sandbox probe fails, the service does not switch to an in-process selector. The run request is rejected with the readiness error.

Scientific readiness reports the public snapshot, worker availability, and whether server-side provider settings can be loaded. `connection` remains `not_checked`; page load does not send a paid provider request. Scientific requests do not depend on the existing isolated selector worker because the trusted Stage 5-B controller makes the request. The isolated selector worker remains unchanged and isolated for its existing path.

## Web and API

The page offers the three saved configurations:

| Example | Campaign | Selector | Seed | Budget | `max_steps` |
| --- | --- | --- | ---: | ---: | ---: |
| A | r2 | `fixed_order` | — | `5` | 5 |
| B | expanded | `fixed_order` | — | `5` | 10 |
| C | expanded | `seeded_random_priority` | 3 | `5` | 30 |

The scientific mode is a separate, fixed research scope: AID 2016 primary Active candidates are considered for the AID 2272 yeast TOR-pathway GFP confirmatory assay and its categorical public outcome. It does not claim direct binding or therapeutic efficacy. Its server-validated defaults are budget `5 synthetic_credit`, 10 actions, 300 seconds, 24 LLM calls, shortlist size 24, seed 3. The seed must be a non-negative signed 32-bit integer. The page shows the advanced limits, and the server rejects values outside the Stage 5-B bounds. Provider endpoint, model credentials, and auth headers are read only in the worker from `.env.stage5a.local` and are never accepted in the browser request.

`max_steps` is an upper bound. Budget, prerequisites, deadline, and executable actions can end a run earlier. The server fixes the cost policy, approval policy, approver ID, duration limit, retries, and selector timeout. Budget is validated and persisted as a `Decimal` string with unit `synthetic_credit` and `assumed=true`; the server caps it at 50. The request does not accept a path, command, policy, or arbitrary Python value.

| Request | Purpose |
| --- | --- |
| `GET /api/readiness` | Snapshot availability, candidate counts, isolated selector readiness, server limits |
| `GET /api/runs` | Recent run IDs and public status metadata |
| `POST /api/runs` | Create a run. Requires `Idempotency-Key: <UUID>` and JSON `{campaign, selector, seed?, budget, max_steps}` |
| `GET /api/runs/{run_id}` | Public run configuration, status, summary, trace, released results, and budget |
| `POST /api/runs/{run_id}/resume` | Resume an interrupted run only when the persisted controller says it is resumable |
| `GET /api/runs/{run_id}/download` | Download the saved `public_export.json`; this does not re-run the selector or Oracle |

For scientific mode, `POST /api/runs` takes `{mode:"scientific_reasoner", campaign, budget, max_steps, max_duration_seconds, max_llm_calls, shortlist_size, shortlist_seed}`. It returns after queuing the worker; it does not wait for an LLM call. `GET /api/runs/{stage4-run-id}` returns the current immutable public projection, and the same route with `/download` downloads that projection. The saved Stage 5-B example is served read-only at `GET /api/scientific-runs/{stage5b-run-id}` and `/download`; these endpoints read its stored archive without invoking a provider or modifying its DB/artifacts. `GET /api/runs` lists both web runs and the saved example.

The HTTP request returns after run creation. A separate worker owns the execution, and the page polls the saved run ID every 1.2 seconds; GET and download do not start work. Service state (`queued`, `running`, `completed`, `interrupted`, `failed`) is separate from the loop's `stop_reason`; for example, `max_steps` is a normal bounded completion. Repeating a request with the same idempotency key and same normalized conditions returns its original run. Reusing that key with different conditions is a conflict. Only one run can be queued or running at once. The scientific duration deadline begins when the existing controller creates its run record after the worker starts; time waiting for the worker is not added to the controller duration. A resume uses the persisted absolute deadline and never resets it.

The page updates from text nodes and `textContent`. API responses and downloads use explicit allowlists; they do not serialize the private SQLite database or replay store. Scientific projection revisions are written atomically under each Stage 4 run's separate `public/` directory after a consistent read-only SQLite transaction. The DTO includes only validated DecisionContext/ScientificDecision fields, released public observations and hash-checked evidence rows, controller step status/budget, hypothesis events, and safe aggregate call metadata. It omits authentication, endpoint settings, request IDs, diagnostics, private database paths, and Oracle data. A decision's execution is linked by `scientific_decision_id`; the execution's observation IDs come from the controller's exact public-attempt records, not a candidate/assay guess. Saved Stage 5-B records use the same allowlisted projection logic against their immutable artifacts. Baseline downloads continue to use their existing public artifact. The baseline archive is built from the run-bound `ExecutionCoordinator.public_reader()` after release, with Observation, the public identity fields of each EvidenceRef (`evidence_id`, `source_kind`, `source_id`), and hash-checked canonical evidence payloads. Its public DTO omits the runtime-relative `EvidenceRef.location`. Unreleased outputs and snapshot-wide hidden labels are excluded. `no_record` is a replay step status meaning this snapshot has no linked follow-up record; it is not an Inactive observation.

`H` counts distinct candidate/assay pairs with a released Active follow-up Observation; `L` counts distinct pairs with a released Active or Inactive Observation. Unknown/categorical verdicts do not enter `L`. The displayed fraction is `H/L` or blank when `L=0`. These are replay observation counts, not potency, binding, treatment, or clinical outcomes.

## Runtime files and restart

By default, Stage 4 writes under `runtime/stage4/`, outside both preserved snapshots. That directory is gitignored and created with mode `0700`; the service index and run state/artifacts use mode `0600`. Each run receives its own private `execution.sqlite` under `runs/{run_id}/private/`. The loop writes its existing durable controller state there. Stage 5-B source artifacts remain private; a scientific web run adds immutable allowlisted projection files under `runs/{run_id}/public/rev-NNNNNN/`. The saved actual Stage 5-B archive under `runtime/stage5b/` is read-only in this web path.

The worker atomically publishes immutable artifact generations under `runs/{run_id}/artifacts/rev-NNNNNN/`:

- `summary.json` — configuration fingerprints, loop stop reason, public metrics and budget.
- `trace.json` — public action IDs, candidate/assay IDs, selector reason, statuses, public-view digests, evidence links, and budget checkpoints.
- `published_results.json` — only released result observations and verified public evidence.
- `public_export.json` — the combined downloadable copy.

A small state file points at the last complete generation. A browser refresh reads the same run from disk; a server restart can see a still-running worker by PID and continues serving its state. If the worker disappears, the service marks the run `interrupted` rather than `completed`. A resume is allowed only when the persisted Stage 3 controller marked that run resumable.

To recover a run after checking that its API state is `interrupted` and `resume_available` is true, the API can start the existing service worker in resume mode:

```bash
conda run -n drug python -m assaypilot.stage4_worker \
  --runtime-root /data1/miplab/wjyang/AssayPilot/runtime/stage4 \
  --run-id stage4-<32-lowercase-hex-digits> --resume
```

The worker invokes the existing run-loop CLI `resume` command with the saved campaign snapshot and per-run DB, then republishes the run-bound public archive. The web resume route performs the same operation and is only enabled for resumable interrupted runs. Do not start a second worker when the run is queued/running. If the run is not resumable, retain its public artifact and create a new run instead.

## Failure behavior

- Invalid campaign, selector, seed, budget, or step bound returns a specific `4xx` error.
- Missing public snapshot or unavailable isolated selector returns `503`; execution never falls back to the host process.
- A second active run returns `409` (`concurrent_run_limit`).
- Worker or selector failure is exposed as `failed` with a stable error code and generic message. Tracebacks and internal paths stay in private worker files, not the browser response.
- Failure to write/hash-check the public artifact marks the run `failed`; the API does not substitute a canned result.
- An interrupted but resumable run can use the saved Stage 3 controller DB. A run that is not marked resumable cannot be resumed through this service.

Private files are retained for recovery and audit; the service does not delete the replay DB. The filesystem permissions assume a trusted single-user host. Use a separate authenticated deployment boundary before allowing remote clients.
