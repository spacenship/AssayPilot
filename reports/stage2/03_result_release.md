# Stage 2C implementation and verification report

Run date: 2026-09-29  
Environment: conda `drug`  
Scope: validated result publication, source evidence, atomic budget settlement, current run state, explicit pending-release cancellation, restricted public reader and local process-boundary verification.

## Implementation

`ExecutionCoordinator.release_result(run_id, execution_id)` reads the original stored action, approval and private `ReplayLookupResult`; it does not accept caller-supplied measurements, verdicts, costs or file paths. It creates one Observation and EvidenceRef per measurement, preserves conflicting rows and missing values, validates the new public objects at the current publication time, then commits public evidence, execution result, current RunState, release status and one settlement in a SQLite transaction. A failed validation or storage write leaves the original object state and reservation unchanged. Repeating a successful release returns the original IDs and timestamp.

`cancel_pending_release` cancels only a `ready_for_release` execution, releases only its reservation and records the internal reason/time. It publishes no observation and increments no spend. Repeated cancellation is idempotent; a released result cannot be canceled.

The runtime schema is SQLite `user_version=2`. The transaction migration adds the release state, current public state, published evidence/executions and settlement records while retaining the v1 approval, reservation, execution, private result and ledger data. Future unsupported versions are rejected. `validate_run_state` now accepts an explicit expected run budget, so it can validate an initialized runtime budget without changing the original campaign budget.

The run-bound `PublicReader` supports only `state`, `budget`, released `execution`, and `evidence` JSON requests. Requests are capped at 4 KiB; responses at 8 MiB. The full state response for the 1,682-candidate public campaign measured 1,599,749 bytes and passed through the real newline-delimited stdio adapter.

## Fixture and regression verification

Command:

```text
conda run -n drug pytest -q
213 passed in 2.92s
```

The regression includes 0/1, 2-A, 2-B and 2-C tests. Stage 2-C checks include:

- `test_release_publishes_source_rows_and_settles_once`: source evidence resolves and hashes; one action cost is settled once.
- `test_release_failure_rolls_back_every_public_write` and `test_release_storage_failures_keep_reservation_and_allow_retry`: validation/evidence/settlement write failures do not leave partial public state or spend.
- `test_cancel_pending_release_is_idempotent_and_cannot_be_reexecuted` and `test_release_racing_cancel_has_one_terminal_outcome`: cancellation retries and release/cancel race.
- `test_public_stdio_is_run_bound_and_hides_pending_results`: pending execution is not exposed and the reader cannot select another run.
- `test_one_unit_budget_cannot_start_another_lookup_after_found_result`: with initial budget 0.1 and action cost 0.1, the found action reserves the unit; a second lookup raises `insufficient_budget` before another Oracle call (`call_count` remains 1).
- `test_schema_v1_database_migrates_without_losing_pending_execution` and `test_future_runtime_database_version_is_rejected`: v1 data migration and future schema rejection.
- `test_followup_prerequisites_use_only_same_run_committed_observations`: a pending private result does not satisfy the next action; publication in the same run does.
- `test_release_preserves_each_conflicting_zero_and_missing_measurement`: separate conflicting observations, zero, missing numeric fields and blank raw outcome are preserved; a private raw-row column is excluded from evidence.
- `test_same_execution_concurrent_release_settles_and_publishes_once`, `test_distinct_concurrent_releases_merge_latest_public_state` and `test_zero_cost_publication_still_records_state_and_settlement`: idempotency, concurrent state merge and zero-cost event.
- `test_chrooted_public_reader_process_cannot_reach_private_files`: actual namespace/chroot boundary described below.

The schema migration fixture retains its approval, reservation and private result after reopening. No existing database or snapshot was deleted to make the migration pass.

## 2-B one-unit budget prerequisite

The preserved-snapshot 2-B verifier creates a separate temporary SQLite database and distinct run for each found/no-record case. It uses the configured action cost as each run's full initial budget. Both snapshots had initial budget 1 and action cost 1 (`assumed=true`). After database reopen, a `ready_for_release` run had spent 0, reserved 1, available 0; a separate `no_record` run had spent 0, reserved 0, available 1. The verifier now checks and prints those final values. Thus a found action does not leave budget for a second paid lookup in the same run; the fixture test above confirms that the second lookup is rejected before reaching the Oracle.

## Preserved snapshot verification

The following developer-only commands ran against the existing revisions. Each used temporary runtime databases. The verifier read curator rows only to choose bounded found/no-record examples; it did not print measurement content or change snapshot files.

```text
conda run -n drug python scripts/verify_replay_snapshots.py
conda run -n drug python scripts/verify_execution_snapshots.py
conda run -n drug python scripts/verify_result_release_snapshots.py
```

| Snapshot | Candidates | 2-A replay check | 2-B found / no-record | 2-C found publication | 2-C no-record |
| --- | ---: | --- | --- | --- | --- |
| `revision-20260917-r2` | 5 | 1 `records_found`, 4 `no_record` | Separate one-unit runs returned `ready_for_release` and `no_record`; retry/reopen reused each execution | 1 measurement became 1 observation; 1 evidence resolved and hash-checked; spent 1 once; retry and DB reopen returned the same publication | 0 new observations; 0 settlement rows; spent 0; available 1 |
| `revision-20260918-primary-active-all` | 1,682 | 295 `records_found`, 1,387 `no_record`; hidden subset matched all 295 selected normalized rows | Separate one-unit runs returned `ready_for_release` and `no_record`; retry/reopen reused each execution | 1 measurement became 1 observation; 1 evidence resolved and hash-checked; spent 1 once; retry and DB reopen returned the same publication | 0 new observations; 0 settlement rows; spent 0; available 1 |

Configured action costs were 1 with `assumed=true` in both snapshots. The reports retain that flag; the values are not presented as wet-lab quotes. For both revisions, the loaded initial PublicCampaign and every file hash under `bundle/public/` were unchanged after the 2-B and 2-C runs. Runtime observations exist only in temporary execution databases used by the verifier.

The expanded campaign state was also sent through `serve_public_stdio`, not just serialized in a unit test. Its 1,599,749-byte response fit under the configured 8 MiB bound. The r2 state response was 7,818 bytes.

## Actual process boundary check

`test_chrooted_public_reader_process_cannot_reach_private_files` ran successfully in the local Linux environment. It launched an untrusted reader child in user, mount and network namespaces, bind-mounted a minimal root read-only, and ran static BusyBox inside `chroot`. A trusted parent broker answered the permitted `state` request through the actual public request handler. The child received public state, while reads of a temporary private canary, runtime SQLite file, curator file, symlink escape and upper-path escape failed. An unsupported `shell` request returned only `request_rejected`. The canary contents were not emitted.

This verifies the local test sandbox and IPC boundary. It does not install a login system, network service or deployment container.

## Files changed

- `src/assaypilot/execution.py`: atomic publication and settlement, cancel, current run state, public evidence and SQLite v1→v2 migration.
- `src/assaypilot/domain/validation.py`: explicit run budget for current RunState validation.
- `src/assaypilot/replay.py`: verified public evidence payloads are available to the run-scoped evidence resolver.
- `src/assaypilot/public_api.py`: restricted public JSON/stdio reader.
- `tests/test_execution.py`: publication, rollback, cancellation, budget, state, migration, race, API and actual sandbox checks.
- `scripts/verify_result_release_snapshots.py`: bounded real-snapshot publication verification.
- `scripts/verify_execution_snapshots.py`: reports and checks per-run initial budget, spent, reserved and available for the 2-B preflight examples.
- `docs/stage2_result_release.md`, `docs/stage2_execution_control.md`, `docs/stage2_data_handoff.md`, `docs/stage2_replay_lookup.md`: current implementation contracts and stage links.

## Remaining scope

Stage 2C is implemented and the prescribed fixture, snapshot and local sandbox checks passed. This stage does not implement real laboratory execution, automatic candidate selection, model training/evaluation, user authentication or an externally deployed service. The next trusted call boundary is `ExecutionCoordinator.release_result(run_id, execution_id)`; consumers that need only committed public information should use `coordinator.public_reader(run_id)` with `serve_public_stdio`.
