"""Stage 2-B approval, budget, persistence, and replay execution contracts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading

import pytest
from pydantic import ValidationError

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.data.build import build_campaign, load_config
from assaypilot.domain import ActionRequest, Cost, DataSource
from assaypilot.execution import (
    ActionRejectedError,
    ApprovalError,
    BudgetError,
    ExecutionControlError,
    ExecutionCoordinator,
    IdempotencyConflictError,
    PrivateResultError,
    RunBindingError,
    RunNotFoundError,
)
from assaypilot.replay import ReplayError, ReplayOracle, load_replay_store


ROOT = Path(__file__).resolve().parents[1]
CONFIG_SOURCE = ROOT / "examples/stage1_configs/synthetic_linked.json"
CACHE_SOURCE = ROOT / "examples/stage1_fixture"


class FixedClock:
    def __init__(self, value: datetime | None = None):
        self.value = value or datetime(2025, 1, 2, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


class SpyOracle:
    def __init__(self, oracle: ReplayOracle):
        self.oracle = oracle
        self.store = oracle.store
        self.call_count = 0
        self.lock = threading.Lock()
        self.error: Exception | None = None

    def lookup(self, candidate_id: str, assay_id: str):
        with self.lock:
            self.call_count += 1
        if self.error is not None:
            raise self.error
        return self.oracle.lookup(candidate_id, assay_id)


def _refresh_manifest(root: Path) -> None:
    inventory = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "snapshot_manifest.json"
    }
    (root / "snapshot_manifest.json").write_text(json.dumps({
        "snapshot_id": "synthetic-execution-r1",
        "campaign_id": "stage1-synthetic-linked",
        "full_sha256": inventory,
    }, indent=2, sort_keys=True) + "\n")


def _make_snapshot(
    tmp_path: Path,
    *,
    prerequisite_verdict: str = "active",
    cost: str = "0.1",
    budget: str = "0.3",
    empty_hidden: bool = False,
):
    """Build an offline, internally consistent replay fixture."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_data = json.loads(CONFIG_SOURCE.read_text())
    config_data["budget"]["amount"] = budget
    followup = next(item for item in config_data["assays"] if item["role"] != "primary")
    followup["cost"]["amount"] = cost
    followup["prerequisites"][0]["verdict"] = prerequisite_verdict
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config_data))
    config = load_config(config_path)
    cache = tmp_path / "cache"
    shutil.copytree(CACHE_SOURCE, cache)
    root = tmp_path / "snapshot"
    root.mkdir()
    shutil.copytree(cache, root / "raw")
    build_campaign(config, cache, root / "bundle")
    (root / "config").mkdir()
    shutil.copyfile(config_path, root / "config/synthetic_linked.json")
    if empty_hidden:
        (root / "bundle/curator/hidden_followup_measurements.json").write_text("[]\n")
    _refresh_manifest(root)
    public = PublicBundleAdapter().load(DataSource(
        kind="public_bundle", location=str(root / "bundle/public/manifest.json")
    ))
    oracle = ReplayOracle(load_replay_store(root, public))
    return root, public, oracle


@pytest.fixture
def execution_snapshot(tmp_path: Path):
    """An offline bundle with exact decimal test prices and primary prerequisites."""
    return _make_snapshot(tmp_path)


def _action(public, candidate_id: str | None = None, *, action_id: str = "action-1", assay_id: str = "confirm-activity"):
    return ActionRequest(
        action_id=action_id,
        campaign_id=public.campaign.campaign_id,
        candidate_id=candidate_id or public.candidates[0].candidate_id,
        assay_id=assay_id,
    )


def _service(tmp_path, public, oracle, *, clock=None, db_name="runtime.sqlite", policy="public-assay-cost-v1"):
    return ExecutionCoordinator(
        tmp_path / db_name,
        public,
        oracle,
        cost_policy_version=policy,
        clock=clock or FixedClock(),
    )


def _start(service, *, run_id="run-1", amount="0.3"):
    return service.initialize_run(run_id, Cost(amount=Decimal(amount), unit="USD", assumed=True))


def _approve(service, public, action=None, *, run_id="run-1"):
    return service.approve_action(
        run_id, action or _action(public), approver_id="trusted-reviewer",
        reason="explicit synthetic test approval",
    )


def test_run_requires_explicit_budget_and_binds_fixed_inputs(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    clock = FixedClock()
    service = _service(tmp_path, public, oracle, clock=clock)
    with pytest.raises(BudgetError, match="unit"):
        service.initialize_run("wrong-unit", Cost(amount=Decimal("1"), unit="EUR", assumed=True))
    first = _start(service)
    second = _start(service)
    assert first == second
    assert first.snapshot_id == oracle.store.snapshot_id
    assert first.budget.total == Decimal("0.3")
    assert first.budget.available == Decimal("0.3")
    assert first.created_at == clock.value
    with pytest.raises(RunBindingError):
        _start(service, amount="0.4")


@pytest.mark.parametrize("amount", [True, 0.1, -1, "NaN", "Infinity", "-0.1"])
def test_money_contract_rejects_non_decimal_invalid_amounts(amount):
    with pytest.raises(ValidationError):
        Cost(amount=amount, unit="USD", assumed=True)


def test_approval_is_explicit_and_denial_or_cancel_never_calls_oracle(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    service = _service(tmp_path, public, spy)
    _start(service)
    action = _action(public)
    with pytest.raises(ApprovalError, match="explicit approval"):
        service.execute("run-1", "req-no-approval", action)
    assert spy.call_count == 0
    assert service.get_budget("run-1").budget.available == Decimal("0.3")

    decision = service.reject_action(
        "run-1", action, approver_id="reviewer", reason="denied by test"
    )
    assert decision.decision == "rejected"
    with pytest.raises(ApprovalError, match="rejected or canceled"):
        service.execute("run-1", "req-rejected", action)
    assert spy.call_count == 0
    assert service.get_budget("run-1").budget.reserved == 0


def test_approved_true_is_not_a_domain_action_field(execution_snapshot):
    _, public, _ = execution_snapshot
    with pytest.raises(ValidationError):
        ActionRequest(
            action_id="action", campaign_id=public.campaign.campaign_id,
            candidate_id=public.candidates[0].candidate_id,
            assay_id="confirm-activity", approved=True,
        )


@pytest.mark.parametrize(
    ("candidate_id", "assay_id", "code"),
    [
        ("unknown-candidate", "confirm-activity", "unknown_candidate"),
        (None, "primary-activity", "primary_lookup_forbidden"),
        (None, "unknown-assay", "unknown_assay"),
    ],
)
def test_unknown_and_primary_actions_are_rejected_before_oracle(execution_snapshot, tmp_path, candidate_id, assay_id, code):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    service = _service(tmp_path, public, spy)
    _start(service)
    action = _action(public, candidate_id, assay_id=assay_id)
    with pytest.raises(ActionRejectedError) as error:
        service.approve_action("run-1", action, approver_id="reviewer", reason="test")
    assert error.value.code == code
    assert spy.call_count == 0


def test_parameters_and_changed_run_or_action_do_not_reuse_approval(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    service = _service(tmp_path, public, spy)
    _start(service)
    first, second = public.candidates
    action_one = _action(public, first.candidate_id)
    service.approve_action("run-1", action_one, approver_id="reviewer", reason="test")
    changed_action = _action(public, second.candidate_id)
    with pytest.raises(ApprovalError, match="explicit approval"):
        service.execute("run-1", "req-other-candidate", changed_action)
    with pytest.raises(RunNotFoundError):
        service.execute("missing-run", "req-other-run", action_one)
    assert spy.call_count == 0
    assert service.get_budget("run-1").budget.available == Decimal("0.3")


def test_cost_authority_cannot_be_supplied_by_request_or_changed_after_approval(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    service = _service(tmp_path, public, spy)
    _start(service)
    action = _action(public)
    _approve(service, public, action)
    with sqlite3.connect(service.database_path) as db:
        db.execute("UPDATE approvals SET cost_amount = '9' WHERE run_id = 'run-1'")
    with pytest.raises(ApprovalError, match="cost no longer matches"):
        service.execute("run-1", "req-tampered-cost", action)
    assert spy.call_count == 0
    assert service.get_budget("run-1").budget.available == Decimal("0.3")


def test_policy_or_snapshot_binding_mismatch_rejects_reopened_database(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    service = _service(tmp_path, public, oracle)
    _start(service)
    changed_policy = _service(tmp_path, public, oracle, policy="public-assay-cost-v2")
    with pytest.raises(RunBindingError):
        changed_policy.get_budget("run-1")


def test_initial_public_prerequisite_is_rechecked_before_reservation(execution_snapshot, tmp_path):
    _, _, _ = execution_snapshot
    _, public, oracle = _make_snapshot(tmp_path / "prerequisite", prerequisite_verdict="inactive")
    spy = SpyOracle(oracle)
    service = _service(tmp_path, public, spy)
    _start(service)
    action = _action(public)
    service.approve_action("run-1", action, approver_id="reviewer", reason="approved before state check")
    with pytest.raises(ActionRejectedError) as error:
        service.execute("run-1", "req-prerequisite", action)
    assert error.value.code == "prerequisite_unmet"
    assert spy.call_count == 0
    assert service.get_budget("run-1").budget.available == Decimal("0.3")


def test_records_found_reserves_one_cost_and_keeps_all_measurements_private(execution_snapshot, tmp_path):
    root, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    service = _service(tmp_path, public, spy)
    _start(service)
    action = _action(public)
    decision = _approve(service, public, action)
    assert decision.approved_action is not None and decision.decision == "approved"
    before_public = public.model_dump(mode="json")
    receipt = service.execute("run-1", "req-found", action)
    assert receipt.status == "ready_for_release"
    assert receipt.reserved == Decimal("0.1")
    assert receipt.available == Decimal("0.2")
    assert receipt.error_code is None
    assert spy.call_count == 1
    budget = service.get_budget("run-1").budget
    assert (budget.total, budget.spent, budget.reserved, budget.available) == (
        Decimal("0.3"), Decimal("0"), Decimal("0.1"), Decimal("0.2")
    )
    private = service.read_private_result("run-1", receipt.execution_id)
    assert private.status == "records_found" and len(private.measurements) == 1
    candidate = next(item for item in public.candidates if item.candidate_id == action.candidate_id)
    assert candidate.source_id == f"SID:{private.measurements[0].sid}"
    original_sid = private.measurements[0].raw_row["SID"]
    private.measurements[0].raw_row["SID"] = "999999"
    assert service.read_private_result("run-1", receipt.execution_id).measurements[0].raw_row["SID"] == original_sid
    assert public.model_dump(mode="json") == before_public
    assert not hasattr(receipt, "measurements")
    assert not any(key in asdict(receipt) for key in ("verdict", "raw_row", "coverage", "measurements"))
    assert (root / "bundle/public/manifest.json").is_file()


def test_no_record_releases_reservation_without_fabricating_observation(execution_snapshot, tmp_path):
    root, public, oracle = execution_snapshot
    (root / "bundle/curator/hidden_followup_measurements.json").write_text("[]\n")
    _refresh_manifest(root)
    oracle = ReplayOracle(load_replay_store(root, public))
    spy = SpyOracle(oracle)
    service = _service(tmp_path, public, spy)
    _start(service)
    action = _action(public)
    _approve(service, public, action)
    original_observations = public.observations
    receipt = service.execute("run-1", "req-absent", action)
    assert receipt.status == "no_record"
    assert receipt.reserved == 0 and receipt.available == Decimal("0.3")
    assert service.get_budget("run-1").budget.spent == 0
    assert service.get_budget("run-1").budget.reserved == 0
    assert public.observations == original_observations
    with pytest.raises(PrivateResultError, match="only ready_for_release"):
        service.read_private_result("run-1", receipt.execution_id)


def test_known_lookup_error_is_failed_not_no_record_and_releases_cost(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    spy.error = ReplayError("fixture_lookup_failure", "private detail must not escape")
    service = _service(tmp_path, public, spy)
    _start(service)
    action = _action(public)
    _approve(service, public, action)
    receipt = service.execute("run-1", "req-failed", action)
    assert receipt.status == "failed" and receipt.error_code == "fixture_lookup_failure"
    assert "private detail" not in repr(receipt)
    assert receipt.reserved == 0 and receipt.available == Decimal("0.3")
    assert service.get_budget("run-1").budget.spent == 0
    with pytest.raises(PrivateResultError):
        service.read_private_result("run-1", receipt.execution_id)


def test_zero_cost_exact_budget_and_decimal_string_roundtrip(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    action = _action(public)
    # Exact boundary: available equals configured cost.
    service = _service(tmp_path, public, oracle)
    _start(service, amount="0.1")
    _approve(service, public, action)
    receipt = service.execute("run-1", "req-exact", action)
    assert receipt.reserved == Decimal("0.1") and receipt.available == Decimal("0.0")
    assert service.get_budget("run-1").budget.available == Decimal("0.0")

    # A valid zero-cost public assay with zero budget remains executable.
    zero_root = tmp_path / "zero-snapshot"
    shutil.copytree(execution_snapshot[0], zero_root)
    zero_config_path = zero_root / "config/synthetic_linked.json"
    zero_config = json.loads(zero_config_path.read_text())
    zero_config["assays"][1]["cost"]["amount"] = "0"
    zero_config["budget"]["amount"] = "0"
    zero_config_path.write_text(json.dumps(zero_config))
    # This derivation is used only to make an internally consistent fixture.
    # Rebuild through the stage-1 builder so public/config/raw/hash all agree.
    cache = tmp_path / "zero-cache"
    shutil.copytree(CACHE_SOURCE, cache)
    shutil.rmtree(zero_root / "bundle")
    build_campaign(load_config(zero_config_path), cache, zero_root / "bundle")
    _refresh_manifest(zero_root)
    zero_public = PublicBundleAdapter().load(DataSource(
        kind="public_bundle", location=str(zero_root / "bundle/public/manifest.json")
    ))
    zero_oracle = ReplayOracle(load_replay_store(zero_root, zero_public))
    zero_service = _service(tmp_path, zero_public, zero_oracle, db_name="zero.sqlite")
    _start(zero_service, amount="0")
    zero_action = _action(zero_public)
    _approve(zero_service, zero_public, zero_action)
    zero_receipt = zero_service.execute("run-1", "req-zero", zero_action)
    assert zero_receipt.status == "ready_for_release"
    assert zero_receipt.reserved == zero_receipt.available == Decimal("0")


def test_insufficient_budget_does_not_call_oracle(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    service = _service(tmp_path, public, spy)
    _start(service, amount="0.09")
    action = _action(public)
    _approve(service, public, action)
    with pytest.raises(BudgetError) as error:
        service.execute("run-1", "req-poor", action)
    assert error.value.code == "insufficient_budget"
    assert spy.call_count == 0
    assert service.get_budget("run-1").budget.available == Decimal("0.09")


def test_request_idempotency_conflict_and_action_deduplication(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    service = _service(tmp_path, public, spy)
    _start(service)
    candidate_one, candidate_two = public.candidates
    action_one = _action(public, candidate_one.candidate_id, action_id="first-action")
    _approve(service, public, action_one)
    first = service.execute("run-1", "same-request", action_one)
    replayed = service.execute("run-1", "same-request", action_one)
    assert replayed == first
    assert spy.call_count == 1
    changed = _action(public, candidate_two.candidate_id, action_id="different-action")
    with pytest.raises(IdempotencyConflictError):
        service.execute("run-1", "same-request", changed)
    assert service.get_budget("run-1").budget.reserved == Decimal("0.1")
    duplicate = _action(public, candidate_one.candidate_id, action_id="renamed-action")
    duplicate_receipt = service.execute("run-1", "new-request-same-action", duplicate)
    assert duplicate_receipt.execution_id == first.execution_id
    assert duplicate_receipt.request_id == "new-request-same-action"
    assert spy.call_count == 1
    assert service.get_budget("run-1").budget.reserved == Decimal("0.1")


def test_different_runs_have_independent_actions_and_budgets(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    service = _service(tmp_path, public, spy)
    _start(service, run_id="run-a", amount="0.1")
    _start(service, run_id="run-b", amount="0.1")
    action = _action(public)
    _approve(service, public, action, run_id="run-a")
    _approve(service, public, action, run_id="run-b")
    one = service.execute("run-a", "req-a", action)
    two = service.execute("run-b", "req-b", action)
    assert one.execution_id != two.execution_id
    assert spy.call_count == 2
    assert service.get_budget("run-a").budget.reserved == Decimal("0.1")
    assert service.get_budget("run-b").budget.reserved == Decimal("0.1")


def test_database_reopen_restores_approval_execution_and_private_result(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    clock = FixedClock()
    service = _service(tmp_path, public, spy, clock=clock)
    _start(service)
    action = _action(public)
    _approve(service, public, action)
    receipt = service.execute("run-1", "req-persist", action)
    reopened = _service(tmp_path, public, spy, clock=clock)
    assert reopened.get_budget("run-1").budget.reserved == Decimal("0.1")
    assert reopened.approval_status("run-1", action).status == "approved"
    assert reopened.execute("run-1", "req-persist", action) == receipt
    restored = reopened.read_private_result("run-1", receipt.execution_id)
    assert restored.status == "records_found"
    assert restored.measurements[0].measurement_id == oracle.lookup(action.candidate_id, action.assay_id).measurements[0].measurement_id
    assert spy.call_count == 1


def test_unexpected_oracle_exception_rolls_back_and_same_request_can_retry(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    spy.error = RuntimeError("unexpected internal failure")
    service = _service(tmp_path, public, spy)
    _start(service)
    action = _action(public)
    _approve(service, public, action)
    with pytest.raises(RuntimeError, match="unexpected internal failure"):
        service.execute("run-1", "req-rollback", action)
    budget = service.get_budget("run-1").budget
    assert (budget.spent, budget.reserved, budget.available) == (0, 0, Decimal("0.3"))
    with sqlite3.connect(service.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM budget_ledger").fetchone()[0] == 0
    spy.error = None
    receipt = service.execute("run-1", "req-rollback", action)
    assert receipt.status == "ready_for_release"
    assert spy.call_count == 2


def test_private_result_write_failure_rolls_back_reservation_and_ledger(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    service = _service(tmp_path, public, spy)
    _start(service)
    action = _action(public)
    _approve(service, public, action)
    with sqlite3.connect(service.database_path) as db:
        db.execute("""CREATE TRIGGER fail_private_result BEFORE INSERT ON private_results
                     BEGIN SELECT RAISE(ABORT, 'simulated private store failure'); END""")
    with pytest.raises(ExecutionControlError) as error:
        service.execute("run-1", "req-write-fail", action)
    assert error.value.code == "database_write_failed"
    assert spy.call_count == 1
    assert service.get_budget("run-1").budget.reserved == 0
    with sqlite3.connect(service.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM budget_ledger").fetchone()[0] == 0


def test_two_connections_submit_same_action_once(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    first = _service(tmp_path, public, spy)
    second = _service(tmp_path, public, spy)
    _start(first)
    action = _action(public)
    _approve(first, public, action)
    barrier = threading.Barrier(2)

    def submit(service):
        barrier.wait(timeout=5)
        return service.execute("run-1", "req-concurrent", action)

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(submit, (first, second)))
    assert receipts[0] == receipts[1]
    assert spy.call_count == 1
    assert first.get_budget("run-1").budget.reserved == Decimal("0.1")
    with sqlite3.connect(first.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM executions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM budget_ledger").fetchone()[0] == 1


def test_concurrent_actions_compete_against_reserved_decimal_budget(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    first = _service(tmp_path, public, spy)
    second = _service(tmp_path, public, spy)
    _start(first, amount="0.1")
    action_one = _action(public, public.candidates[0].candidate_id, action_id="action-one")
    action_two = _action(public, public.candidates[1].candidate_id, action_id="action-two")
    _approve(first, public, action_one)
    _approve(first, public, action_two)
    barrier = threading.Barrier(2)

    def submit(service, action, request_id):
        barrier.wait(timeout=5)
        try:
            return service.execute("run-1", request_id, action)
        except BudgetError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda args: submit(*args),
            ((first, action_one, "req-one"), (second, action_two, "req-two")),
        ))
    assert sum(not isinstance(item, BudgetError) for item in results) == 1
    assert sum(isinstance(item, BudgetError) for item in results) == 1
    assert first.get_budget("run-1").budget.reserved == Decimal("0.1")
    assert first.get_budget("run-1").budget.available == 0
    assert spy.call_count == 1


def test_approval_cancellation_is_persistent_and_precedes_lookup(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    service = _service(tmp_path, public, spy)
    _start(service)
    action = _action(public)
    _approve(service, public, action)
    status = service.cancel_approval("run-1", action)
    assert status.status == "canceled"
    assert service.approval_status("run-1", action).status == "canceled"
    with pytest.raises(ApprovalError, match="rejected or canceled"):
        service.execute("run-1", "req-canceled", action)
    assert spy.call_count == 0


def test_runtime_database_is_private_and_public_loader_ignores_it(execution_snapshot, tmp_path):
    root, public, oracle = execution_snapshot
    runtime_dir = root / "runtime-private"
    service = ExecutionCoordinator(
        runtime_dir / "execution.sqlite", public, oracle,
        cost_policy_version="public-assay-cost-v1", clock=FixedClock(),
    )
    _start(service)
    assert (service.database_path.stat().st_mode & 0o777) == 0o600
    before = {
        p.relative_to(root / "bundle/public").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / "bundle/public").rglob("*") if p.is_file()
    }
    loaded_again = PublicBundleAdapter().load(DataSource(
        kind="public_bundle", location=str(root / "bundle/public/manifest.json")
    ))
    after = {
        p.relative_to(root / "bundle/public").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / "bundle/public").rglob("*") if p.is_file()
    }
    assert loaded_again == public
    assert before == after
    assert service.database_path.parent != root / "bundle/public"


def test_release_publishes_source_rows_and_settles_once(execution_snapshot, tmp_path):
    root, public, oracle = execution_snapshot
    clock = FixedClock()
    service = _service(tmp_path, public, oracle, clock=clock)
    _start(service)
    original_action = _action(public, action_id="original-action")
    _approve(service, public, original_action)
    ready = service.execute("run-1", "request-original", original_action)
    alias = _action(public, action_id="renamed-alias")
    retried = service.execute("run-1", "request-alias", alias)
    assert retried.action_id == "original-action"

    expected = oracle.lookup(original_action.candidate_id, original_action.assay_id)
    published = service.release_result("run-1", ready.execution_id)
    assert published.result.action_id == "original-action"
    assert len(published.result.observations) == len(expected.measurements)
    assert published.result.observations[0].value == expected.measurements[0].value
    assert published.result.observations[0].raw_verdict == expected.measurements[0].raw_verdict
    assert published.result.observations[0].released_at == clock.value
    assert service.execute("run-1", "request-alias", alias).action_id == "original-action"

    observation = published.result.observations[0]
    evidence = service.get_public_evidence("run-1", observation.evidence_ids[0])
    assert evidence.sha256 == hashlib.sha256(evidence.payload).hexdigest()
    evidence_doc = json.loads(evidence.payload)
    measurement = expected.measurements[0]
    assert evidence_doc["measurement_id"] == measurement.measurement_id
    assert evidence_doc["source_row_id"] == measurement.source_row_id
    assert evidence_doc["source_file_sha256"] == measurement.source_file_sha256
    assert evidence_doc["sid"] == measurement.sid and evidence_doc["aid"] == measurement.aid
    assert evidence_doc["raw_row"]["SID"] == str(measurement.sid)
    assert set(evidence_doc["raw_row"]) <= {
        "AID", "SID", "CID", "Activity Outcome", "Activity Name", "Activity Value [uM]",
    }

    budget = service.get_current_budget("run-1")
    assert (budget.total, budget.spent, budget.reserved, budget.available) == (
        Decimal("0.3"), Decimal("0.1"), Decimal("0"), Decimal("0.2"),
    )
    assert service.execute("run-1", "request-alias", alias).reserved == Decimal("0.1")
    assert service.get_current_budget("run-1") == budget
    assert service.get_public_execution("run-1", ready.execution_id) == published

    repeated = service.release_result("run-1", ready.execution_id)
    assert repeated == published
    assert service.get_current_budget("run-1") == budget
    reopened = _service(tmp_path, public, oracle, clock=clock)
    state = reopened.get_public_state("run-1")
    assert state.public.as_of == clock.value
    assert len(state.public.observations) == len(public.observations) + len(expected.measurements)
    assert state.state_version == published.state_version
    assert reopened.get_public_execution("run-1", ready.execution_id) == published
    assert reopened.get_public_evidence("run-1", observation.evidence_ids[0]) == evidence
    assert root.exists()


def test_release_failure_rolls_back_every_public_write(execution_snapshot, tmp_path, monkeypatch):
    import assaypilot.execution as execution_module

    _, public, oracle = execution_snapshot
    service = _service(tmp_path, public, oracle)
    _start(service)
    action = _action(public)
    _approve(service, public, action)
    receipt = service.execute("run-1", "req-release-failure", action)
    version_before = service.get_public_state("run-1").state_version

    def rejected(*args, **kwargs):
        from assaypilot.domain import AuditIssue, AuditResult
        return AuditResult(issues=[AuditIssue(
            target_id="release-test", field="observations", code="test_failure", reason="synthetic rejection",
        )])

    with monkeypatch.context() as patcher:
        patcher.setattr(execution_module, "validate_execution", rejected)
        with pytest.raises(ExecutionControlError) as error:
            service.release_result("run-1", receipt.execution_id)
    assert error.value.code == "release_validation_failed"

    with sqlite3.connect(service.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM published_evidence").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM published_executions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM release_settlements").fetchone()[0] == 0
        assert db.execute("SELECT release_status FROM executions").fetchone()[0] == "pending"
        assert db.execute("SELECT state_version FROM run_public_state").fetchone()[0] == version_before
    budget = service.get_current_budget("run-1")
    assert (budget.spent, budget.reserved) == (Decimal("0"), Decimal("0.1"))
    assert service.read_private_result("run-1", receipt.execution_id).status == "records_found"
    assert service.release_result("run-1", receipt.execution_id).result.observations


@pytest.mark.parametrize(
    ("table", "trigger_name"),
    [("published_evidence", "fail_evidence"),
     ("release_settlements", "fail_settlement"),
     ("run_public_state", "fail_state")],
)
def test_release_storage_failures_keep_reservation_and_allow_retry(
    execution_snapshot, tmp_path, table, trigger_name,
):
    _, public, oracle = execution_snapshot
    service = _service(tmp_path, public, oracle)
    _start(service)
    action = _action(public)
    _approve(service, public, action)
    receipt = service.execute("run-1", "req-storage-failure", action)
    with sqlite3.connect(service.database_path) as db:
        if table == "run_public_state":
            event = "UPDATE"
            body = "BEFORE UPDATE OF observations_json ON run_public_state"
        else:
            event = "INSERT"
            body = f"BEFORE INSERT ON {table}"
        db.execute(
            f"CREATE TRIGGER {trigger_name} {body} BEGIN SELECT RAISE(ABORT, 'synthetic storage failure'); END"
        )
    with pytest.raises(ExecutionControlError):
        service.release_result("run-1", receipt.execution_id)
    with sqlite3.connect(service.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM published_evidence").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM published_executions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM release_settlements").fetchone()[0] == 0
        assert db.execute("SELECT release_status FROM executions").fetchone()[0] == "pending"
    assert service.get_current_budget("run-1").spent == 0
    assert service.get_current_budget("run-1").reserved == Decimal("0.1")
    with sqlite3.connect(service.database_path) as db:
        db.execute(f"DROP TRIGGER {trigger_name}")
    published = service.release_result("run-1", receipt.execution_id)
    assert len(published.result.observations) > 0
    assert service.get_current_budget("run-1").spent == Decimal("0.1")


def test_cancel_pending_release_is_idempotent_and_cannot_be_reexecuted(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    service = _service(tmp_path, public, spy)
    _start(service)
    action = _action(public)
    _approve(service, public, action)
    receipt = service.execute("run-1", "req-cancel-release", action)
    canceled = service.cancel_pending_release("run-1", receipt.execution_id, "trusted caller canceled")
    assert canceled.status == "cancelled"
    assert service.cancel_pending_release("run-1", receipt.execution_id, "retry") == canceled
    budget = service.get_current_budget("run-1")
    assert (budget.spent, budget.reserved, budget.available) == (0, 0, Decimal("0.3"))
    with sqlite3.connect(service.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM budget_ledger WHERE event = 'release'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM release_settlements").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM published_evidence").fetchone()[0] == 0
    assert service.execute("run-1", "new-request-after-cancel", _action(public, action_id="alias")).status == "cancelled"
    assert spy.call_count == 1
    with pytest.raises(PrivateResultError):
        service.read_private_result("run-1", receipt.execution_id)
    with pytest.raises(PrivateResultError):
        service.release_result("run-1", receipt.execution_id)


def test_release_racing_cancel_has_one_terminal_outcome(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    first = _service(tmp_path, public, oracle)
    second = _service(tmp_path, public, oracle)
    _start(first)
    action = _action(public)
    _approve(first, public, action)
    receipt = first.execute("run-1", "req-release-cancel-race", action)
    barrier = threading.Barrier(2)

    def release():
        barrier.wait(timeout=5)
        try:
            return first.release_result("run-1", receipt.execution_id)
        except ExecutionControlError as exc:
            return exc

    def cancel():
        barrier.wait(timeout=5)
        try:
            return second.cancel_pending_release("run-1", receipt.execution_id, "race")
        except ExecutionControlError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda fn: fn(), (release, cancel)))
    with sqlite3.connect(first.database_path) as db:
        release_status = db.execute("SELECT release_status FROM executions").fetchone()[0]
        evidence_count = db.execute("SELECT COUNT(*) FROM published_evidence").fetchone()[0]
        settlement_count = db.execute("SELECT COUNT(*) FROM release_settlements").fetchone()[0]
    assert release_status in {"released", "cancelled"}
    if release_status == "released":
        assert settlement_count == 1 and evidence_count > 0
        assert first.get_current_budget("run-1").spent == Decimal("0.1")
    else:
        assert settlement_count == 0 and evidence_count == 0
        assert first.get_current_budget("run-1").spent == 0
    assert len(outcomes) == 2


def test_public_stdio_is_run_bound_and_hides_pending_results(execution_snapshot, tmp_path):
    from io import BytesIO, StringIO
    from assaypilot.public_api import handle_public_request, serve_public_stdio

    _, public, oracle = execution_snapshot
    service = _service(tmp_path, public, oracle)
    _start(service)
    reader = service.public_reader("run-1")
    before = handle_public_request(reader, b'{"op":"state"}')
    assert before["public"]["observations"] == public.model_dump(mode="json")["observations"]
    assert handle_public_request(reader, b'{"op":"budget"}') == {
        "total": "0.3", "spent": "0", "reserved": "0", "available": "0.3", "unit": "USD",
    }
    assert handle_public_request(reader, b'{"op":"state","run_id":"other-run"}') == {"error": "request_rejected"}
    assert handle_public_request(reader, b'{"op":"evidence","evidence_id":"../../private.sqlite"}') == {"error": "request_rejected"}
    assert handle_public_request(reader, b'{"op":"shell","command":"cat"}') == {"error": "request_rejected"}
    assert handle_public_request(reader, b" " * 4097) == {"error": "request_rejected"}

    action = _action(public)
    _approve(service, public, action)
    receipt = service.execute("run-1", "req-stdio-pending", action)
    assert handle_public_request(reader, json.dumps({"op": "execution", "execution_id": receipt.execution_id})) == {
        "error": "request_rejected",
    }
    input_stream = BytesIO(b'{"op":"budget"}\n{"op":"shell"}\n')
    output = StringIO()
    serve_public_stdio(reader, input_stream, output)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]["reserved"] == "0.1"
    assert responses[1] == {"error": "request_rejected"}

    published = service.release_result("run-1", receipt.execution_id)
    response = handle_public_request(reader, json.dumps({"op": "execution", "execution_id": receipt.execution_id}))
    assert response["result"]["observations"]
    evidence_id = published.result.observations[0].evidence_ids[0]
    evidence_response = handle_public_request(reader, json.dumps({"op": "evidence", "evidence_id": evidence_id}))
    assert evidence_response["payload"]["evidence_id"] == evidence_id


def test_one_unit_budget_cannot_start_another_lookup_after_found_result(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    spy = SpyOracle(oracle)
    service = _service(tmp_path, public, spy)
    _start(service, amount="0.1")
    first, second = public.candidates
    action_one = _action(public, first.candidate_id, action_id="first")
    action_two = _action(public, second.candidate_id, action_id="second")
    _approve(service, public, action_one)
    _approve(service, public, action_two)
    first_result = service.execute("run-1", "req-first", action_one)
    assert first_result.status == "ready_for_release"
    assert spy.call_count == 1
    with pytest.raises(BudgetError) as error:
        service.execute("run-1", "req-second", action_two)
    assert error.value.code == "insufficient_budget"
    assert spy.call_count == 1
    assert service.get_current_budget("run-1").reserved == Decimal("0.1")


def test_schema_v1_database_migrates_without_losing_pending_execution(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    service = _service(tmp_path, public, oracle)
    _start(service)
    action = _action(public)
    _approve(service, public, action)
    receipt = service.execute("run-1", "req-before-migration", action)
    private_before = service.read_private_result("run-1", receipt.execution_id)

    with sqlite3.connect(service.database_path) as db:
        for table in ("release_settlements", "published_executions", "published_evidence", "run_public_state"):
            db.execute(f"DROP TABLE {table}")
        for column in (
            "prerequisite_state_version", "cancel_reason", "cancelled_at", "released_at", "release_status",
        ):
            db.execute(f"ALTER TABLE executions DROP COLUMN {column}")
        db.execute("PRAGMA user_version = 1")

    reopened = _service(tmp_path, public, oracle)
    with sqlite3.connect(service.database_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM private_results").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM budget_ledger WHERE event = 'reserve'").fetchone()[0] == 1
    assert reopened.get_current_budget("run-1").reserved == Decimal("0.1")
    assert reopened.read_private_result("run-1", receipt.execution_id) == private_before
    assert reopened.release_result("run-1", receipt.execution_id).result.observations


def test_schema_v2_database_adds_loop_tables_without_losing_publication(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    service = _service(tmp_path, public, oracle)
    _start(service)
    action = _action(public)
    _approve(service, public, action)
    ready = service.execute("run-1", "req-before-v3-migration", action)
    published = service.release_result("run-1", ready.execution_id)

    # Model the preserved Stage 2C v2 shape: retain its execution, publication,
    # evidence and settlement tables while removing only Stage 3-A tables.
    with sqlite3.connect(service.database_path) as db:
        db.execute("DROP TABLE loop_steps")
        db.execute("DROP TABLE loop_runs")
        db.execute("PRAGMA user_version = 2")

    reopened = _service(tmp_path, public, oracle)
    with sqlite3.connect(service.database_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'",
        )}
        assert {"loop_runs", "loop_steps", "published_executions", "release_settlements"} <= tables
        assert db.execute("SELECT COUNT(*) FROM release_settlements WHERE run_id = 'run-1'").fetchone()[0] == 1
    assert reopened.get_public_execution("run-1", ready.execution_id) == published
    assert reopened.get_current_budget("run-1").spent == Decimal("0.1")


def test_future_runtime_database_version_is_rejected(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    service = _service(tmp_path, public, oracle)
    _start(service)
    with sqlite3.connect(service.database_path) as db:
        db.execute("PRAGMA user_version = 999")
    with pytest.raises(ExecutionControlError) as error:
        _service(tmp_path, public, oracle)
    assert error.value.code == "unsupported_database_version"


def test_followup_prerequisites_use_only_same_run_committed_observations(execution_snapshot, tmp_path):
    from assaypilot.data.schemas import NormalizedMeasurement
    from assaypilot.domain import AssaySpec, Cost, EvidenceRef, Prerequisite, Verdict
    from assaypilot.replay import ReplayLookupResult

    _, base_public, base_oracle = execution_snapshot
    secondary = AssaySpec(
        assay_id="second-followup", name="Synthetic second follow-up", role="confirmatory",
        endpoint="categorical secondary endpoint", unit="category",
        verdict_meaning={
            Verdict.ACTIVE: "configured active", Verdict.INACTIVE: "configured inactive",
            Verdict.INCONCLUSIVE: "configured inconclusive", Verdict.UNSPECIFIED: "not reported",
        },
        prerequisites=[Prerequisite(
            assay_id="confirm-activity", kind="verdict", verdict=Verdict.ACTIVE,
        )],
        cost=Cost(amount=Decimal("0.1"), unit="USD", assumed=True),
    )
    extra_ref = EvidenceRef(
        evidence_id="evidence-secondary-policy", source_kind="public_bundle_evidence",
        source_id="PubChem AID:103", location="evidence/secondary-policy.json",
    )
    public = base_public.validated_replace(
        assays=[*base_public.assays, secondary], evidence=[*base_public.evidence, extra_ref],
    )
    extra_payload = json.dumps({
        "evidence_id": extra_ref.evidence_id, "name": secondary.name,
        "endpoint": secondary.endpoint, "unit": secondary.unit, "aid": 103,
        "raw_outcome_column": "Activity Outcome", "raw_endpoint_column": None,
        "protocol_location": "synthetic-protocol:AID103",
    }, sort_keys=True).encode()
    public_hash = hashlib.sha256(public.model_dump_json().encode("utf-8")).hexdigest()
    store = replace(
        base_oracle.store,
        public_campaign_sha256=public_hash,
        _supported_assays=frozenset({*base_oracle.store._supported_assays, "second-followup"}),
        _public_evidence_payloads={
            **base_oracle.store._public_evidence_payloads,
            extra_ref.evidence_id: extra_payload,
        },
    )

    class ChainOracle:
        def __init__(self):
            self.store = store
            self.calls = []

        def lookup(self, candidate_id, assay_id):
            self.calls.append(assay_id)
            if assay_id == "confirm-activity":
                return base_oracle.lookup(candidate_id, assay_id)
            candidate = next(item for item in public.candidates if item.candidate_id == candidate_id)
            sid = int(candidate.source_id.removeprefix("SID:"))
            measurement = NormalizedMeasurement(
                measurement_id="measurement-second-followup", assay_id=assay_id,
                aid=103, sid=sid, cid=2001, raw_verdict="SECOND_ACTIVE", verdict=Verdict.ACTIVE,
                value=None, unit=None, comparison=None,
                replicate_id="not_reported:replicate-secondary",
                condition_id="not_reported:condition-secondary",
                source_row_id="source-row-secondary", source_row_number=1,
                source_file_sha256="a" * 64, protocol_location="synthetic-protocol:AID103",
                raw_row={
                    "AID": "103", "SID": str(sid), "CID": "2001",
                    "Activity Outcome": "SECOND_ACTIVE", "unapproved_private_column": "do not publish",
                },
            )
            return ReplayLookupResult(
                status="records_found", snapshot_id=store.snapshot_id,
                campaign_id=store.campaign_id, candidate_id=candidate_id,
                assay_id=assay_id, measurements=(measurement,),
            )

    chain_oracle = ChainOracle()
    service = _service(tmp_path, public, chain_oracle)
    _start(service, amount="0.2")
    candidate = next(item for item in public.candidates if item.source_id == "SID:1001")
    first_action = _action(public, candidate.candidate_id, action_id="first-followup")
    second_action = _action(public, candidate.candidate_id, action_id="second-followup", assay_id="second-followup")
    _approve(service, public, first_action)
    _approve(service, public, second_action)
    first_receipt = service.execute("run-1", "req-first-followup", first_action)
    assert first_receipt.status == "ready_for_release"
    with pytest.raises(ActionRejectedError) as blocked:
        service.execute("run-1", "req-second-before-release", second_action)
    assert blocked.value.code == "prerequisite_unmet"
    assert chain_oracle.calls == ["confirm-activity"]

    service.release_result("run-1", first_receipt.execution_id)
    second_receipt = service.execute("run-1", "req-second-after-release", second_action)
    assert second_receipt.status == "ready_for_release"
    assert chain_oracle.calls == ["confirm-activity", "second-followup"]
    second_published = service.release_result("run-1", second_receipt.execution_id)
    second_observation = second_published.result.observations[0]
    assert second_observation.verdict == Verdict.ACTIVE
    assert second_observation.raw_verdict == "SECOND_ACTIVE"
    second_evidence = service.get_public_evidence("run-1", second_observation.evidence_ids[0])
    assert "unapproved_private_column" not in json.loads(second_evidence.payload)["raw_row"]


def test_release_preserves_each_conflicting_zero_and_missing_measurement(execution_snapshot, tmp_path):
    from assaypilot.domain import Comparison, Verdict
    from assaypilot.replay import ReplayLookupResult

    _, public, base_oracle = execution_snapshot

    class MultipleMeasurementOracle:
        def __init__(self):
            self.store = base_oracle.store

        def lookup(self, candidate_id, assay_id):
            original = base_oracle.lookup(candidate_id, assay_id)
            first = original.measurements[0].validated_replace(
                measurement_id="multi-original",
                raw_row={**original.measurements[0].raw_row, "unapproved_private_column": "private"},
            )
            second = first.validated_replace(
                measurement_id="multi-conflicting-zero",
                source_row_id="source-row-conflicting-zero",
                source_row_number=original.measurements[0].source_row_number + 1,
                raw_verdict="Inactive",
                verdict=Verdict.INACTIVE,
                value=0.0,
                unit="uM",
                comparison=Comparison.EQ,
                raw_row={
                    **first.raw_row,
                    "Activity Outcome": "Inactive",
                    "Activity Value [uM]": "0",
                    "unapproved_private_column": "private",
                },
            )
            third = second.validated_replace(
                measurement_id="multi-inconclusive-missing",
                source_row_id="source-row-inconclusive-missing",
                source_row_number=original.measurements[0].source_row_number + 2,
                raw_verdict="Inconclusive",
                verdict=Verdict.INCONCLUSIVE,
                value=None,
                unit=None,
                comparison=None,
                raw_row={
                    **second.raw_row,
                    "Activity Outcome": "Inconclusive",
                    "Activity Value [uM]": "",
                },
            )
            fourth = third.validated_replace(
                measurement_id="multi-blank-outcome",
                source_row_id="source-row-blank-outcome",
                source_row_number=original.measurements[0].source_row_number + 3,
                raw_verdict=None,
                verdict=Verdict.INACTIVE,
                value=0.0,
                unit="uM",
                comparison=Comparison.EQ,
                raw_row={
                    **third.raw_row,
                    "Activity Outcome": "",
                    "Activity Value [uM]": "",
                },
            )
            return ReplayLookupResult(
                status="records_found", snapshot_id=original.snapshot_id,
                campaign_id=original.campaign_id, candidate_id=candidate_id,
                assay_id=assay_id, measurements=(first, second, third, fourth),
            )

    service = _service(tmp_path, public, MultipleMeasurementOracle())
    _start(service)
    action = _action(public)
    _approve(service, public, action)
    receipt = service.execute("run-1", "req-multiple-measurements", action)
    published = service.release_result("run-1", receipt.execution_id)
    assert len(published.result.observations) == 4

    by_measurement = {}
    for observation in published.result.observations:
        payload = service.get_public_evidence("run-1", observation.evidence_ids[0]).payload
        evidence = json.loads(payload)
        by_measurement[evidence["measurement_id"]] = (observation, evidence)
        assert "unapproved_private_column" not in evidence["raw_row"]
    assert set(by_measurement) == {
        "multi-original", "multi-conflicting-zero", "multi-inconclusive-missing",
        "multi-blank-outcome",
    }
    zero, zero_evidence = by_measurement["multi-conflicting-zero"]
    assert (zero.verdict, zero.raw_verdict, zero.value, zero.unit, zero.comparison) == (
        Verdict.INACTIVE, "Inactive", 0.0, "uM", Comparison.EQ,
    )
    assert zero_evidence["raw_row"]["Activity Value [uM]"] == "0"
    missing, missing_evidence = by_measurement["multi-inconclusive-missing"]
    assert (missing.verdict, missing.raw_verdict, missing.value, missing.unit, missing.comparison) == (
        Verdict.INCONCLUSIVE, "Inconclusive", None, None, None,
    )
    assert missing_evidence["raw_row"]["Activity Value [uM]"] == ""
    blank, blank_evidence = by_measurement["multi-blank-outcome"]
    assert (blank.verdict, blank.raw_verdict, blank.value, blank.unit, blank.comparison) == (
        Verdict.INACTIVE, None, 0.0, "uM", Comparison.EQ,
    )
    assert blank_evidence["raw_row"]["Activity Outcome"] == ""
    assert service.release_result("run-1", receipt.execution_id) == published


def test_same_execution_concurrent_release_settles_and_publishes_once(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    first = _service(tmp_path, public, oracle)
    second = _service(tmp_path, public, oracle)
    _start(first)
    action = _action(public)
    _approve(first, public, action)
    receipt = first.execute("run-1", "req-same-release-race", action)
    barrier = threading.Barrier(2)

    def release(service):
        barrier.wait(timeout=5)
        return service.release_result("run-1", receipt.execution_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(release, (first, second)))
    assert outcomes[0] == outcomes[1]
    with sqlite3.connect(first.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM release_settlements").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM published_executions").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM published_evidence").fetchone()[0] == len(outcomes[0].result.observations)
    assert first.get_current_budget("run-1").spent == Decimal("0.1")
    assert first.get_public_state("run-1").state_version == outcomes[0].state_version


def test_distinct_concurrent_releases_merge_latest_public_state(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    first = _service(tmp_path, public, oracle)
    second = _service(tmp_path, public, oracle)
    _start(first, amount="0.2")
    actions = [_action(public, candidate.candidate_id, action_id=f"action-{index}")
               for index, candidate in enumerate(public.candidates)]
    assert len(actions) >= 2
    for action in actions[:2]:
        _approve(first, public, action)
    receipts = [first.execute("run-1", f"req-concurrent-{index}", action)
                for index, action in enumerate(actions[:2])]
    assert all(receipt.status == "ready_for_release" for receipt in receipts)
    barrier = threading.Barrier(2)

    def release(pair):
        service, receipt = pair
        barrier.wait(timeout=5)
        return service.release_result("run-1", receipt.execution_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        released = list(pool.map(release, ((first, receipts[0]), (second, receipts[1]))))
    view = first.get_public_state("run-1")
    initial_ids = {item.observation_id for item in public.observations}
    runtime_observations = [item for item in view.public.observations if item.observation_id not in initial_ids]
    expected_count = sum(len(item.result.observations) for item in released)
    assert len(runtime_observations) == expected_count
    assert len({item.observation_id for item in runtime_observations}) == expected_count
    assert view.state_version == max(item.state_version for item in released)
    assert first.get_current_budget("run-1").spent == Decimal("0.2")
    assert first.get_current_budget("run-1").reserved == 0


def test_approval_and_execution_reject_clock_regression_from_current_public_state(execution_snapshot, tmp_path):
    _, public, base_oracle = execution_snapshot
    oracle = SpyOracle(base_oracle)
    start_time = datetime(2025, 1, 2, tzinfo=timezone.utc)
    clock = FixedClock(start_time)
    service = _service(tmp_path, public, oracle, clock=clock)
    _start(service)
    actions = [
        _action(public, candidate.candidate_id, action_id=f"clock-action-{index}")
        for index, candidate in enumerate(public.candidates[:2])
    ]
    assert len(actions) == 2
    for action in actions:
        _approve(service, public, action)

    first = service.execute("run-1", "clock-request-0", actions[0])
    assert first.status == "ready_for_release"
    clock.value = start_time.replace(second=1)
    service.release_result("run-1", first.execution_id)

    clock.value = start_time
    with pytest.raises(ExecutionControlError) as approval_error:
        _approve(service, public, actions[1])
    assert approval_error.value.code == "time_order"
    with pytest.raises(ExecutionControlError) as execution_error:
        service.execute("run-1", "clock-request-1", actions[1])
    assert execution_error.value.code == "time_order"
    assert oracle.call_count == 1
    assert service.get_current_budget("run-1").spent == Decimal("0.1")
    assert service.get_current_budget("run-1").reserved == 0


def test_run_clock_must_be_timezone_aware(execution_snapshot, tmp_path):
    _, public, oracle = execution_snapshot
    service = _service(
        tmp_path, public, oracle,
        clock=FixedClock(datetime(2025, 1, 2)),
    )
    with pytest.raises(ExecutionControlError) as error:
        _start(service)
    assert error.value.code == "invalid_clock"


def test_zero_cost_publication_still_records_state_and_settlement(execution_snapshot, tmp_path):
    root, public, oracle = _make_snapshot(tmp_path / "zero-cost", cost="0", budget="0")
    service = _service(tmp_path / "zero-runtime", public, oracle)
    initial = _start(service, amount="0")
    action = _action(public)
    _approve(service, public, action)
    receipt = service.execute("run-1", "req-zero-cost-release", action)
    published = service.release_result("run-1", receipt.execution_id)
    assert published.result.observations
    assert published.state_version > 0
    assert service.get_public_state("run-1").public.as_of == published.published_at
    assert service.get_current_budget("run-1").total == initial.budget.total == 0
    with sqlite3.connect(service.database_path) as db:
        settlement = db.execute(
            "SELECT amount, unit FROM release_settlements WHERE execution_id = ?",
            (receipt.execution_id,),
        ).fetchone()
    assert settlement == ("0", "USD")
    assert service.release_result("run-1", receipt.execution_id) == published
    assert root.exists()


def test_chrooted_public_reader_process_cannot_reach_private_files(execution_snapshot, tmp_path):
    from assaypilot.public_api import handle_public_request

    unshare = shutil.which("unshare")
    busybox = shutil.which("busybox")
    if not unshare or not busybox:
        pytest.skip("Linux user/mount/network namespaces and static busybox are required")

    root, public, oracle = execution_snapshot
    service = _service(tmp_path, public, oracle)
    _start(service)
    reader = service.public_reader("run-1")
    private_dir = tmp_path / "host-private"
    private_dir.mkdir()
    canary = private_dir / "temporary-canary.txt"
    canary.write_text("synthetic-private-canary-value")
    curator_path = root / "bundle/curator/hidden_followup_measurements.json"
    assert curator_path.is_file() and service.database_path.is_file()

    sandbox_root = tmp_path / "public-reader-root"
    (sandbox_root / "bin").mkdir(parents=True)
    (sandbox_root / "public").mkdir()
    shutil.copyfile(busybox, sandbox_root / "bin/busybox")
    os.chmod(sandbox_root / "bin/busybox", 0o555)
    initial_state = handle_public_request(reader, b'{"op":"state"}')
    (sandbox_root / "public/state.json").write_text(json.dumps(initial_state, separators=(",", ":")))
    os.chmod(sandbox_root / "public/state.json", 0o444)
    (sandbox_root / "public/escape").symlink_to(canary)
    client = sandbox_root / "bin/reader.sh"
    client.write_text(
        """#!/bin/busybox sh
printf '%s\\n' '{\"op\":\"state\"}'
IFS= read -r state_response || exit 40
printf 'response:%s\\n' \"$state_response\"
printf '%s\\n' '{\"op\":\"shell\",\"command\":\"id\"}'
IFS= read -r denied_response || exit 41
printf 'response:%s\\n' \"$denied_response\"
public_state=$(/bin/busybox cat /public/state.json) || exit 42
for private_path in \"$CANARY_PATH\" \"$DATABASE_PATH\" \"$CURATOR_PATH\"; do
    if probe=$(/bin/busybox cat \"$private_path\"); then exit 43; fi
done
if probe=$(/bin/busybox cat /public/escape); then exit 44; fi
if probe=$(/bin/busybox cat \"$UPPER_PATH\"); then exit 45; fi
printf '%s\\n' 'filesystem_boundary=ok'
"""
    )
    os.chmod(client, 0o555)
    for directory in (sandbox_root / "bin", sandbox_root / "public", sandbox_root):
        os.chmod(directory, 0o555)

    environment = {
        "PATH": "/usr/bin:/bin",
        "SANDBOX_ROOT": str(sandbox_root),
        "BUSYBOX_PATH": busybox,
        "CANARY_PATH": str(canary),
        "DATABASE_PATH": str(service.database_path),
        "CURATOR_PATH": str(curator_path),
        "UPPER_PATH": "/../../../../" + str(canary).lstrip("/"),
    }
    setup = (
        "mount --make-rprivate / && "
        'mount --bind "$SANDBOX_ROOT" "$SANDBOX_ROOT" && '
        'mount -o remount,bind,ro "$SANDBOX_ROOT" && '
        'cd "$SANDBOX_ROOT" && '
        'exec "$BUSYBOX_PATH" chroot "$SANDBOX_ROOT" /bin/busybox sh /bin/reader.sh'
    )
    process = subprocess.Popen(
        [unshare, "--user", "--map-root-user", "--mount", "--net", "--fork", "sh", "-c", setup],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd="/", env=environment,
    )
    assert process.stdout is not None and process.stdin is not None
    first_request = process.stdout.readline().strip()
    assert json.loads(first_request) == {"op": "state"}
    process.stdin.write(json.dumps(handle_public_request(reader, first_request), separators=(",", ":")) + "\n")
    process.stdin.flush()
    state_response = json.loads(process.stdout.readline().removeprefix("response:"))
    assert state_response["run_id"] == "run-1"
    assert len(state_response["public"]["candidates"]) == len(public.candidates)

    second_request = process.stdout.readline().strip()
    assert json.loads(second_request) == {"op": "shell", "command": "id"}
    process.stdin.write(json.dumps(handle_public_request(reader, second_request), separators=(",", ":")) + "\n")
    process.stdin.flush()
    assert json.loads(process.stdout.readline().removeprefix("response:")) == {"error": "request_rejected"}
    remaining = process.stdout.read()
    stderr = process.stderr.read() if process.stderr is not None else ""
    assert process.wait(timeout=10) == 0, stderr
    assert remaining.strip() == "filesystem_boundary=ok"
    assert "synthetic-private-canary-value" not in remaining + stderr
