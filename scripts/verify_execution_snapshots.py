#!/usr/bin/env python
"""Developer-only Stage 2-B exercise against preserved replay snapshots.

The script selects one eligible found and one eligible no-record candidate per
snapshot, records both through an explicitly approved private runtime run, and
prints aggregate verification facts only. It never prints measurements or
changes files under a preserved snapshot.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.data.schemas import CampaignConfig, NormalizedMeasurement
from assaypilot.domain import ActionRequest, Cost, DataSource
from assaypilot.execution import ExecutionCoordinator, PrivateResultError
from assaypilot.replay import ReplayLookupResult, ReplayOracle, load_replay_store


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = (
    ROOT / "data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2",
    ROOT / "data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all",
)


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class RecordingOracle:
    """Count and retain one private return value for the verification only."""

    def __init__(self, oracle: ReplayOracle) -> None:
        self.oracle = oracle
        self.store = oracle.store
        self.call_count = 0
        self.last_result: ReplayLookupResult | None = None

    def lookup(self, candidate_id: str, assay_id: str) -> ReplayLookupResult:
        self.call_count += 1
        self.last_result = self.oracle.lookup(candidate_id, assay_id)
        return self.last_result


def _public_hashes(snapshot_root: Path) -> dict[str, str]:
    public_root = snapshot_root / "bundle/public"
    return {
        path.relative_to(public_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(public_root.rglob("*")) if path.is_file()
    }


def _lookup_json(result: ReplayLookupResult) -> str:
    return json.dumps({
        "status": result.status,
        "snapshot_id": result.snapshot_id,
        "campaign_id": result.campaign_id,
        "candidate_id": result.candidate_id,
        "assay_id": result.assay_id,
        "measurements": [item.model_dump(mode="json") for item in result.measurements],
    }, sort_keys=True, separators=(",", ":"))


def _candidate_is_eligible(public, candidate_id: str, assay) -> bool:
    for prerequisite in assay.prerequisites:
        matching = [
            observation for observation in public.observations
            if observation.candidate_id == candidate_id
            and observation.assay_id == prerequisite.assay_id
        ]
        if prerequisite.kind == "observed" and not matching:
            return False
        if prerequisite.kind == "verdict" and not any(
            observation.verdict == prerequisite.verdict for observation in matching
        ):
            return False
    return True


def _select_examples(snapshot_root: Path, public, assay_id: str):
    candidates_by_source_id = {
        candidate.source_id: candidate
        for candidate in public.candidates
        if candidate.source == "pubchem_sid"
    }
    hidden_path = snapshot_root / "bundle/curator/hidden_followup_measurements.json"
    hidden = [NormalizedMeasurement.model_validate(item) for item in json.loads(hidden_path.read_text())]
    recorded_candidate_ids = {
        candidates_by_source_id[f"SID:{item.sid}"].candidate_id
        for item in hidden
        if item.assay_id == assay_id and f"SID:{item.sid}" in candidates_by_source_id
    }
    eligible = {
        candidate.candidate_id for candidate in public.candidates
        if _candidate_is_eligible(public, candidate.candidate_id,
                                  next(assay for assay in public.assays if assay.assay_id == assay_id))
    }
    found = sorted(eligible & recorded_candidate_ids)
    absent = sorted(eligible - recorded_candidate_ids)
    if not found or not absent:
        raise AssertionError(
            f"{snapshot_root.name} lacks an eligible found/no_record example for {assay_id}"
        )
    return found[0], absent[0], len(hidden)


def _run_case(
    snapshot_root: Path,
    public,
    oracle: RecordingOracle,
    *,
    candidate_id: str,
    assay_id: str,
    expected_status: str,
    cost: Cost,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if now < public.as_of:
        now = public.as_of
    run_id = f"verify-{snapshot_root.name}-{expected_status}"
    request_id = f"request-{expected_status}"
    action = ActionRequest(
        action_id=f"action-{expected_status}",
        campaign_id=public.campaign.campaign_id,
        candidate_id=candidate_id,
        assay_id=assay_id,
    )
    with tempfile.TemporaryDirectory(prefix="assaypilot-stage2b-") as runtime:
        database_path = Path(runtime) / "private-runtime" / "execution.sqlite"
        coordinator = ExecutionCoordinator(
            database_path,
            public,
            oracle,
            cost_policy_version="preserved-public-assay-cost-v1",
            clock=FixedClock(now),
        )
        initial = coordinator.initialize_run(run_id, cost)
        coordinator.approve_action(
            run_id, action, approver_id="local-stage2b-verifier",
            reason="explicit approval for a bounded snapshot verification case",
        )
        receipt = coordinator.execute(run_id, request_id, action)
        if receipt.status != expected_status:
            raise AssertionError(f"expected {expected_status}, got {receipt.status}")
        first_call_count = oracle.call_count
        expected_lookup_status = "records_found" if expected_status == "ready_for_release" else "no_record"
        if oracle.last_result is None or oracle.last_result.status != expected_lookup_status:
            raise AssertionError("execution outcome differs from the Oracle lookup outcome")

        expected_private_json = None
        if expected_status == "ready_for_release":
            private = coordinator.read_private_result(run_id, receipt.execution_id)
            expected_private_json = _lookup_json(oracle.last_result)
            if _lookup_json(private) != expected_private_json:
                raise AssertionError("persisted private result differs from the Oracle result")
            if receipt.reserved != cost.amount or coordinator.get_budget(run_id).budget.spent != 0:
                raise AssertionError("found result did not retain one reservation without spending")
        else:
            if receipt.reserved != 0 or coordinator.get_budget(run_id).budget.reserved != 0:
                raise AssertionError("no_record did not release the reservation")
            with sqlite3.connect(database_path) as db:
                stored = db.execute(
                    "SELECT result_json FROM private_results WHERE execution_id = ?",
                    (receipt.execution_id,),
                ).fetchone()
            if stored is None or json.loads(stored[0])["status"] != "no_record":
                raise AssertionError("no_record was not persisted as a distinct private state")
            try:
                coordinator.read_private_result(run_id, receipt.execution_id)
            except PrivateResultError:
                pass
            else:
                raise AssertionError("no_record was exposed through the 2-C ready-result reader")

        retried = coordinator.execute(run_id, request_id, action)
        if retried != receipt or oracle.call_count != first_call_count:
            raise AssertionError("same-request retry did not reuse the stored execution")

        reopened = ExecutionCoordinator(
            database_path,
            public,
            oracle,
            cost_policy_version="preserved-public-assay-cost-v1",
            clock=FixedClock(now),
        )
        restored = reopened.get_budget(run_id)
        if restored.budget != coordinator.get_budget(run_id).budget:
            raise AssertionError("budget did not restore after reopening the runtime database")
        restored_receipt = reopened.execute(run_id, request_id, action)
        if restored_receipt != receipt or oracle.call_count != first_call_count:
            raise AssertionError("reopened retry did not reuse the persisted execution")
        if expected_private_json is not None:
            restored_result = reopened.read_private_result(run_id, receipt.execution_id)
            if _lookup_json(restored_result) != expected_private_json:
                raise AssertionError("private result did not restore byte-for-byte after database reopen")

        return {
            "status": receipt.status,
            "execution_reused_on_retry_and_reopen": True,
            "oracle_calls": first_call_count,
            "initial_budget": str(initial.budget.total),
            "assay_cost": str(cost.amount),
            "cost_assumed": cost.assumed,
        }


def verify_snapshot(snapshot_root: Path) -> dict[str, Any]:
    public = PublicBundleAdapter().load(DataSource(
        kind="public_bundle",
        location=str(snapshot_root / "bundle/public/manifest.json"),
    ))
    config_paths = sorted((snapshot_root / "config").glob("*.json"))
    if len(config_paths) != 1:
        raise AssertionError(f"expected exactly one config under {snapshot_root / 'config'}")
    config = CampaignConfig.model_validate_json(config_paths[0].read_bytes())
    store = load_replay_store(snapshot_root, public)
    oracle = RecordingOracle(ReplayOracle(store))
    followup_assays = [assay for assay in public.assays if assay.role.value != "primary"]
    if len(followup_assays) != 1:
        raise AssertionError(f"expected one follow-up assay in {snapshot_root.name}")
    assay = followup_assays[0]
    configured_assay = next(item for item in config.assays if item.assay_id == assay.assay_id)
    found_candidate_id, absent_candidate_id, hidden_rows = _select_examples(
        snapshot_root, public, assay.assay_id
    )

    before_public = public.model_dump(mode="json")
    before_hashes = _public_hashes(snapshot_root)
    # The explicit validation budget is one configured snapshot assay cost.
    # The cost may be marked assumed; it is not presented as an experiment quote.
    validation_budget = Cost(
        amount=configured_assay.cost.amount,
        unit=configured_assay.cost.unit,
        assumed=configured_assay.cost.assumed,
    )
    cases = {}
    for expected_status, candidate_id in (
        ("ready_for_release", found_candidate_id),
        ("no_record", absent_candidate_id),
    ):
        cases[expected_status] = _run_case(
            snapshot_root,
            public,
            oracle,
            candidate_id=candidate_id,
            assay_id=assay.assay_id,
            expected_status=expected_status,
            cost=validation_budget,
        )

    if public.model_dump(mode="json") != before_public:
        raise AssertionError("PublicCampaign changed during execution verification")
    after_hashes = _public_hashes(snapshot_root)
    if after_hashes != before_hashes:
        raise AssertionError("public snapshot tree changed during execution verification")
    return {
        "snapshot_id": store.snapshot_id,
        "campaign_id": store.campaign_id,
        "candidate_count": len(public.candidates),
        "hidden_followup_row_count": hidden_rows,
        "assay_id": assay.assay_id,
        "found_case": cases["ready_for_release"],
        "no_record_case": cases["no_record"],
        "public_campaign_unchanged": True,
        "public_tree_hashes_unchanged": True,
        "initial_observations_unchanged": public.model_dump(mode="json")["observations"]
        == before_public["observations"],
    }


def main() -> None:
    missing = [str(root) for root in SNAPSHOTS if not root.is_dir()]
    if missing:
        raise FileNotFoundError("required preserved snapshot(s) missing: " + ", ".join(missing))
    print(json.dumps([verify_snapshot(root) for root in SNAPSHOTS], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
