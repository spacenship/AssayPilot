#!/usr/bin/env python
"""Exercise bounded Stage 3-A runs on the two preserved public snapshots.

This verifier selects only from the public campaign through the real isolated
fixed-order selector. Replay lookup remains inside the trusted coordinator;
output contains aggregate execution and publication counts only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.domain import Cost, DataSource
from assaypilot.execution import ExecutionCoordinator
from assaypilot.replay import ReplayOracle, ReplayLookupResult, load_replay_store
from assaypilot.run_loop import (
    IsolatedFixedOrderSelector,
    RunLoopConfig,
    RunLoopController,
)


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = (
    ROOT / "data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2",
    ROOT / "data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all",
)
_PUBLIC_SMOKE_STEPS = 5
_PUBLIC_EXTENSION_STEPS = 30
_PAID_ACTIONS_BUDGETED = 5
_MAX_DURATION_SECONDS = 300
_COST_POLICY_VERSION = "preserved-public-assay-cost-v1"


class RecordingOracle:
    """Count trusted lookups without exposing their private return values."""

    def __init__(self, oracle: ReplayOracle) -> None:
        self._oracle = oracle
        self.store = oracle.store
        self.lookup_count = 0

    def lookup(self, candidate_id: str, assay_id: str) -> ReplayLookupResult:
        self.lookup_count += 1
        return self._oracle.lookup(candidate_id, assay_id)


def _public_hashes(snapshot_root: Path) -> dict[str, str]:
    public_root = snapshot_root / "bundle/public"
    return {
        path.relative_to(public_root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sorted(public_root.rglob("*")) if path.is_file()
    }


def _bounded_budget(public, store) -> Cost:
    supported = store._supported_assays
    costs = [
        assay.cost for assay in public.assays
        if assay.role.value != "primary" and assay.assay_id in supported
    ]
    if not costs:
        raise AssertionError("snapshot has no supported public follow-up assay")
    units = {cost.unit for cost in costs}
    if units != {public.campaign.budget.unit}:
        raise AssertionError("follow-up costs do not share the public campaign budget unit")
    maximum = max((cost.amount for cost in costs), default=Decimal("0"))
    return Cost(
        amount=maximum * _PAID_ACTIONS_BUDGETED,
        unit=public.campaign.budget.unit,
        assumed=True,
    )


def verify_snapshot(snapshot_root: Path) -> dict[str, Any]:
    before_hashes = _public_hashes(snapshot_root)
    public = PublicBundleAdapter().load(DataSource(
        kind="public_bundle",
        location=str(snapshot_root / "bundle/public/manifest.json"),
    ))
    if snapshot_root.name == "revision-20260917-r2" and len(public.candidates) != 5:
        raise AssertionError("r2 validation snapshot must retain its five public candidates")
    oracle = RecordingOracle(ReplayOracle(load_replay_store(snapshot_root, public)))
    budget = _bounded_budget(public, oracle.store)
    max_steps = (
        _PUBLIC_SMOKE_STEPS if snapshot_root.name == "revision-20260917-r2"
        else _PUBLIC_EXTENSION_STEPS
    )
    initial_observation_ids = {item.observation_id for item in public.observations}
    now = max(datetime.now(timezone.utc), public.as_of)

    with tempfile.TemporaryDirectory(prefix="assaypilot-stage3a-") as temporary:
        temp_root = Path(temporary)
        database = temp_root / "private-runtime" / "execution.sqlite"
        canary = temp_root / "private-canary.txt"
        canary.write_text("selector-private-canary", encoding="utf-8")
        coordinator = ExecutionCoordinator(
            database, public, oracle,
            cost_policy_version=_COST_POLICY_VERSION,
            clock=lambda: now,
        )
        run_id = f"stage3a-{snapshot_root.name}"
        config = RunLoopConfig(
            run_id=run_id,
            snapshot_id=oracle.store.snapshot_id,
            runtime_database=str(database.absolute()),
            initial_budget=budget,
            cost_policy_version=_COST_POLICY_VERSION,
            approval_policy="bounded_replay",
            approver_id="local-stage3a-bounded-replay-policy",
            selector_kind="fixed_order",
            max_steps=max_steps,
            max_duration_seconds=_MAX_DURATION_SECONDS,
            max_action_retries=1,
            max_release_retries=2,
            selector_timeout_seconds=5.0,
        )

        # Persist two steps, stop the same run, then restart with a fresh
        # isolated selector process. No candidate is selected using curator or
        # replay-record availability.
        with IsolatedFixedOrderSelector(
            timeout_seconds=config.selector_timeout_seconds,
            private_canary_path=canary,
        ) as selector:
            start_summary = RunLoopController(coordinator, selector).start(
                config, stop_after_new_steps=2,
            )
            canary_denied = selector.canary_denied
        if start_summary.stop_reason != "user_interrupt" or start_summary.selection_steps != 2:
            raise AssertionError("snapshot did not persist the expected two-step resumable prefix")

        with IsolatedFixedOrderSelector(
            timeout_seconds=config.selector_timeout_seconds,
            private_canary_path=canary,
        ) as selector:
            summary = RunLoopController(coordinator, selector).resume(run_id)
            canary_denied = canary_denied and selector.canary_denied
        if not canary_denied:
            raise AssertionError("real isolated selector did not deny the private canary")

        reader = coordinator.public_reader(run_id)
        current = reader.current_state()
        runtime_observations = [
            observation for observation in current.public.observations
            if observation.observation_id not in initial_observation_ids
        ]
        if len(runtime_observations) != summary.observations_added:
            raise AssertionError("runtime observation delta differs from durable loop checkpoints")

        evidence_resolved = 0
        for execution in current.released_executions:
            published = reader.released_execution(execution.execution_id)
            if published != execution:
                raise AssertionError("public reader changed a released execution")
            for observation in published.result.observations:
                if not observation.evidence_ids:
                    raise AssertionError("released public observation has no evidence reference")
                for evidence_id in observation.evidence_ids:
                    evidence = reader.evidence(evidence_id)
                    if sha256(evidence.payload).hexdigest() != evidence.sha256:
                        raise AssertionError("resolved runtime evidence hash is invalid")
                    if evidence.reference.evidence_id != evidence_id:
                        raise AssertionError("runtime evidence identity differs from observation reference")
                    evidence_resolved += 1

        with sqlite3.connect(database) as db:
            steps = db.execute(
                "SELECT action_id, request_id, execution_id, status FROM loop_steps WHERE run_id = ? ORDER BY step_no",
                (run_id,),
            ).fetchall()
            execution_request_count = db.execute(
                "SELECT COUNT(*) FROM execution_requests WHERE run_id = ?", (run_id,),
            ).fetchone()[0]
            settlement_count = db.execute(
                "SELECT COUNT(*) FROM release_settlements WHERE run_id = ?", (run_id,),
            ).fetchone()[0]
        if len(steps) != summary.selection_steps:
            raise AssertionError("durable step count differs from run summary")
        action_ids = [row[0] for row in steps]
        request_ids = [row[1] for row in steps]
        execution_ids = [row[2] for row in steps if row[2] is not None]
        if len(action_ids) != len(set(action_ids)) or len(request_ids) != len(set(request_ids)):
            raise AssertionError("durable action or request identifiers were reused")
        if len(execution_ids) != len(set(execution_ids)):
            raise AssertionError("one execution was counted in more than one loop step")
        if len(execution_ids) != summary.unique_executions:
            raise AssertionError("unique execution summary differs from the persisted run")
        if execution_request_count != summary.unique_executions:
            raise AssertionError("resume created an extra execution request")
        if settlement_count != summary.released_executions:
            raise AssertionError("published execution settlement count differs from loop state")
        if oracle.lookup_count != summary.unique_executions:
            raise AssertionError("resume repeated a replay lookup")

        initial_budget = budget.amount
        if (Decimal(summary.spent) + Decimal(summary.reserved) + Decimal(summary.available)
                != initial_budget):
            raise AssertionError("run summary violates exact budget conservation")

        report = {
            "snapshot_revision": snapshot_root.name,
            "public_candidate_count": len(public.candidates),
            "configured_max_steps": max_steps,
            "interrupted_after_steps": start_summary.selection_steps,
            "resumed_same_run": summary.run_id == run_id,
            "selection_steps": summary.selection_steps,
            "unique_executions": summary.unique_executions,
            "released_executions": summary.released_executions,
            "no_record_executions": summary.no_record_executions,
            "failed_executions": summary.failed_executions,
            "rejected_steps": summary.rejected_steps,
            "runtime_observations_added": summary.observations_added,
            "runtime_evidence_payloads_resolved_and_hashed": evidence_resolved,
            "selector_private_canary_denied": canary_denied,
            "oracle_lookups_without_resume_duplicates": oracle.lookup_count,
            "settlements": settlement_count,
            "budget": {
                "initial": str(initial_budget),
                "spent": summary.spent,
                "reserved": summary.reserved,
                "available": summary.available,
                "unit": summary.unit,
            },
            "stop_reason": summary.stop_reason,
            "resumable": summary.resumable,
        }

    if before_hashes != _public_hashes(snapshot_root):
        raise AssertionError("preserved public snapshot files changed during the run")
    report["preserved_public_files_unchanged"] = True
    return report


def main() -> None:
    missing = [str(path) for path in SNAPSHOTS if not path.is_dir()]
    if missing:
        raise FileNotFoundError("required preserved snapshot(s) missing: " + ", ".join(missing))
    print(json.dumps([verify_snapshot(path) for path in SNAPSHOTS], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
