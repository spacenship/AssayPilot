#!/usr/bin/env python
"""Developer-only Stage 2-C release exercise against preserved snapshots.

The script reads curator data only to choose one eligible records-found and
one no-record example. It writes only temporary runtime databases, reports
aggregate facts, and verifies that the preserved public snapshot stays intact.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
from io import BytesIO, StringIO
from typing import Any

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.data.schemas import CampaignConfig, NormalizedMeasurement
from assaypilot.domain import ActionRequest, Cost, DataSource
from assaypilot.execution import ExecutionControlError, ExecutionCoordinator
from assaypilot.public_api import MAX_PUBLIC_RESPONSE_BYTES, handle_public_request, serve_public_stdio
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
    """Keep the private replay result available only inside this verifier."""

    def __init__(self, oracle: ReplayOracle) -> None:
        self.oracle = oracle
        self.store = oracle.store
        self.last_result: ReplayLookupResult | None = None

    def lookup(self, candidate_id: str, assay_id: str) -> ReplayLookupResult:
        self.last_result = self.oracle.lookup(candidate_id, assay_id)
        return self.last_result


def _public_hashes(snapshot_root: Path) -> dict[str, str]:
    public_root = snapshot_root / "bundle/public"
    return {
        path.relative_to(public_root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sorted(public_root.rglob("*")) if path.is_file()
    }


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
        candidate.source_id: candidate for candidate in public.candidates
        if candidate.source == "pubchem_sid"
    }
    hidden_path = snapshot_root / "bundle/curator/hidden_followup_measurements.json"
    hidden = [NormalizedMeasurement.model_validate(item) for item in json.loads(hidden_path.read_text())]
    found_ids = {
        candidates_by_source_id[f"SID:{item.sid}"].candidate_id
        for item in hidden
        if item.assay_id == assay_id and f"SID:{item.sid}" in candidates_by_source_id
    }
    assay = next(item for item in public.assays if item.assay_id == assay_id)
    eligible_ids = {
        candidate.candidate_id for candidate in public.candidates
        if _candidate_is_eligible(public, candidate.candidate_id, assay)
    }
    found = sorted(eligible_ids & found_ids)
    absent = sorted(eligible_ids - found_ids)
    if not found or not absent:
        raise AssertionError(f"{snapshot_root.name} lacks found and no_record examples")
    return found[0], absent[0]


def _run_release_case(snapshot_root, public, oracle, assay, candidate_id, cost, *, found: bool):
    now = max(datetime.now(timezone.utc), public.as_of)
    run_id = f"stage2c-{snapshot_root.name}-{'found' if found else 'no-record'}"
    request_id = "release-check"
    action = ActionRequest(
        action_id="stage2c-action",
        campaign_id=public.campaign.campaign_id,
        candidate_id=candidate_id,
        assay_id=assay.assay_id,
    )
    initial_public_hashes = _public_hashes(snapshot_root)
    initial_public = public.model_dump(mode="json")
    initial_observation_count = len(public.observations)

    with tempfile.TemporaryDirectory(prefix="assaypilot-stage2c-") as temp_dir:
        database_path = Path(temp_dir) / "private-runtime" / "execution.sqlite"
        coordinator = ExecutionCoordinator(
            database_path,
            public,
            oracle,
            cost_policy_version="preserved-public-assay-cost-v1",
            clock=FixedClock(now),
        )
        coordinator.initialize_run(run_id, cost)
        coordinator.approve_action(
            run_id,
            action,
            approver_id="local-stage2c-verifier",
            reason="bounded verification against the preserved replay snapshot",
        )
        receipt = coordinator.execute(run_id, request_id, action)
        expected_status = "ready_for_release" if found else "no_record"
        if receipt.status != expected_status:
            raise AssertionError(f"expected {expected_status}, got {receipt.status}")

        reader = coordinator.public_reader(run_id)
        pending = reader.current_state()
        if len(pending.state.observations) != initial_observation_count:
            raise AssertionError("pending private execution changed public observations")
        if pending.released_executions:
            raise AssertionError("pending execution appeared in public execution inventory")
        if handle_public_request(reader, json.dumps({
            "op": "execution", "execution_id": receipt.execution_id,
        })) != {"error": "request_rejected"}:
            raise AssertionError("pending execution was visible through the public API")

        # Verify the real JSON stdio path can return the entire expanded public
        # campaign under its bounded response limit.
        request_stream = BytesIO(b'{"op":"state"}\n')
        response_stream = StringIO()
        serve_public_stdio(reader, request_stream, response_stream)
        state_response = json.loads(response_stream.getvalue())
        if ("public" not in state_response
                or len(response_stream.getvalue().encode("utf-8")) > MAX_PUBLIC_RESPONSE_BYTES):
            raise AssertionError("public state was rejected or exceeded the configured API response bound")

        if found:
            private = coordinator.read_private_result(run_id, receipt.execution_id)
            expected_observations = len(private.measurements)
            if expected_observations < 1:
                raise AssertionError("found result has no normalized measurements")
            published = coordinator.release_result(run_id, receipt.execution_id)
            if len(published.result.observations) != expected_observations:
                raise AssertionError("publication did not preserve every measurement as an observation")
            budget = coordinator.get_current_budget(run_id)
            if budget.spent != cost.amount or budget.reserved != 0 or budget.available != 0:
                raise AssertionError("publication did not settle exactly the configured action cost")
            current = reader.current_state()
            if len(current.state.observations) != initial_observation_count + expected_observations:
                raise AssertionError("released observations are missing from current public state")
            if len(current.released_executions) != 1:
                raise AssertionError("published execution was not included exactly once")
            for observation in published.result.observations:
                if len(observation.evidence_ids) != 1:
                    raise AssertionError("released observation lacks its one source evidence reference")
                evidence = reader.evidence(observation.evidence_ids[0])
                if sha256(evidence.payload).hexdigest() != evidence.sha256:
                    raise AssertionError("published evidence bytes do not match their SHA-256")
                if evidence.reference.evidence_id != observation.evidence_ids[0]:
                    raise AssertionError("observation and resolved evidence identity differ")
                if evidence.reference.location.startswith("/") or ".." in evidence.reference.location.split("/"):
                    raise AssertionError("runtime evidence location is not a bounded logical location")
            with sqlite3.connect(database_path) as db:
                settlement_count = db.execute(
                    "SELECT COUNT(*) FROM release_settlements WHERE run_id = ? AND execution_id = ?",
                    (run_id, receipt.execution_id),
                ).fetchone()[0]
            if settlement_count != 1:
                raise AssertionError("release settlement was not recorded exactly once")

            # Same-process retry and reopen return the original public event.
            if coordinator.release_result(run_id, receipt.execution_id) != published:
                raise AssertionError("release retry changed the committed public result")
            reopened = ExecutionCoordinator(
                database_path, public, oracle,
                cost_policy_version="preserved-public-assay-cost-v1",
                clock=FixedClock(now),
            )
            if reopened.release_result(run_id, receipt.execution_id) != published:
                raise AssertionError("database reopen changed the committed public result")
            if reopened.get_current_budget(run_id) != budget:
                raise AssertionError("database reopen changed the settled current budget")
            if reader.released_execution(receipt.execution_id) != published:
                raise AssertionError("public API did not resolve the released execution")
            case_result = {
                "status": "released",
                "measurement_count_preserved": expected_observations,
                "evidence_count_resolved_and_hashed": expected_observations,
                "settlement_rows": settlement_count,
                "spent_once": str(budget.spent),
                "retry_and_reopen_idempotent": True,
            }
        else:
            try:
                reader.released_execution(receipt.execution_id)
            except ExecutionControlError:
                pass
            else:
                raise AssertionError("no_record execution became publicly readable")
            budget = coordinator.get_current_budget(run_id)
            current = reader.current_state()
            if (budget.spent != 0 or budget.reserved != 0 or budget.available != cost.amount
                    or len(current.state.observations) != initial_observation_count
                    or current.released_executions):
                raise AssertionError("no_record changed public observations or final spend")
            with sqlite3.connect(database_path) as db:
                settlement_count = db.execute(
                    "SELECT COUNT(*) FROM release_settlements WHERE run_id = ?", (run_id,),
                ).fetchone()[0]
            if settlement_count != 0:
                raise AssertionError("no_record created a publication settlement")
            case_result = {
                "status": "no_record",
                "public_observations_added": 0,
                "settlement_rows": settlement_count,
                "spent": str(budget.spent),
                "available": str(budget.available),
            }

    if public.model_dump(mode="json") != initial_public:
        raise AssertionError("initial PublicCampaign changed during runtime publication")
    if _public_hashes(snapshot_root) != initial_public_hashes:
        raise AssertionError("preserved public snapshot files changed")
    case_result["initial_snapshot_unchanged"] = True
    case_result["public_state_stdio_bytes"] = len(response_stream.getvalue().encode("utf-8"))
    return case_result


def verify_snapshot(snapshot_root: Path) -> dict[str, Any]:
    public = PublicBundleAdapter().load(DataSource(
        kind="public_bundle",
        location=str(snapshot_root / "bundle/public/manifest.json"),
    ))
    config_paths = sorted((snapshot_root / "config").glob("*.json"))
    if len(config_paths) != 1:
        raise AssertionError(f"expected one config in {snapshot_root.name}")
    config = CampaignConfig.model_validate_json(config_paths[0].read_bytes())
    store = load_replay_store(snapshot_root, public)
    oracle = RecordingOracle(ReplayOracle(store))
    followups = [item for item in public.assays if item.role.value != "primary"]
    if len(followups) != 1:
        raise AssertionError(f"expected one follow-up assay in {snapshot_root.name}")
    assay = followups[0]
    configured = next(item for item in config.assays if item.assay_id == assay.assay_id)
    found_id, absent_id = _select_examples(snapshot_root, public, assay.assay_id)
    cost = Cost(
        amount=configured.cost.amount,
        unit=configured.cost.unit,
        assumed=configured.cost.assumed,
    )
    found_result = _run_release_case(snapshot_root, public, oracle, assay, found_id, cost, found=True)
    absent_result = _run_release_case(snapshot_root, public, oracle, assay, absent_id, cost, found=False)
    return {
        "snapshot_id": store.snapshot_id,
        "candidate_count": len(public.candidates),
        "followup_assay_id": assay.assay_id,
        "assay_cost": str(cost.amount),
        "cost_assumed": cost.assumed,
        "found_case": found_result,
        "no_record_case": absent_result,
        "snapshot_tree_unchanged": True,
    }


def main() -> None:
    missing = [str(path) for path in SNAPSHOTS if not path.is_dir()]
    if missing:
        raise FileNotFoundError("preserved snapshot missing: " + ", ".join(missing))
    print(json.dumps([verify_snapshot(path) for path in SNAPSHOTS], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
