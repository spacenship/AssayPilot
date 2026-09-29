"""Stage 2-B approval, budget, persistence, and replay execution contracts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
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
