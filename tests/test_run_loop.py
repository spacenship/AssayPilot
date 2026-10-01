"""Stage 3-A bounded replay-loop, recovery, and selector boundary tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.data.build import build_campaign, load_config
from assaypilot.domain import Cost, DataSource
from assaypilot.execution import ExecutionCoordinator
from assaypilot.replay import ReplayError, ReplayOracle, load_replay_store
from assaypilot.run_loop import (
    AssayInfo,
    AttemptSummary,
    CandidateInfo,
    ExecutableAction,
    FaultInjectedCrash,
    FixedOrderSelector,
    IsolatedFixedOrderSelector,
    IsolatedSelector,
    ObservationInfo,
    RANDOM_PRIORITY_VERSION,
    RunLoopConfig,
    RunLoopController,
    RunLoopError,
    SeededRandomPrioritySelector,
    SelectorProposal,
    SelectorView,
    _validate_proposal,
)
from test_execution import FixedClock, SpyOracle, _make_snapshot


ROOT = Path(__file__).resolve().parents[1]


class StopSelector:
    def select(self, _view):
        return {"kind": "stop", "stop_reason": "selector_stop"}


class BadSelector:
    def __init__(self, result):
        self.result = result

    def select(self, _view):
        return self.result


class FailingOracle:
    def __init__(self, oracle, *, failures: int | None = None, replay_failure: bool = False):
        self.oracle = oracle
        self.store = oracle.store
        self.failures = failures
        self.replay_failure = replay_failure
        self.call_count = 0

    def lookup(self, candidate_id, assay_id):
        self.call_count += 1
        if self.replay_failure:
            raise ReplayError("fixture_lookup_failure", "synthetic known replay failure")
        if self.failures is None or self.failures > 0:
            if self.failures is not None:
                self.failures -= 1
            raise RuntimeError("synthetic transient failure")
        return self.oracle.lookup(candidate_id, assay_id)


def _config(coordinator, budget: str, *, run_id="loop-run", max_steps=10,
            max_duration_seconds=120, action_retries=1, release_retries=2,
            selector_timeout=1.0):
    return RunLoopConfig(
        run_id=run_id,
        snapshot_id=coordinator.snapshot_id,
        runtime_database=str(coordinator.database_path),
        initial_budget=Cost(amount=Decimal(budget), unit="USD", assumed=True),
        cost_policy_version=coordinator.cost_policy_version,
        approval_policy="bounded_replay",
        approver_id="test-bounded-replay-policy",
        selector_kind="fixed_order",
        max_steps=max_steps,
        max_duration_seconds=max_duration_seconds,
        max_action_retries=action_retries,
        max_release_retries=release_retries,
        selector_timeout_seconds=selector_timeout,
    )


def _coordinator(tmp_path, public, oracle, *, clock=None, name="runtime.sqlite"):
    return ExecutionCoordinator(
        tmp_path / name, public, oracle,
        cost_policy_version="stage3a-fixture-cost-v1",
        clock=clock or FixedClock(datetime(2025, 1, 2, tzinfo=timezone.utc)),
    )


def _view(actions):
    return SelectorView(
        schema_version="assaypilot.selector-view.v1",
        state_version=0,
        public_as_of="2025-01-01T00:00:00+00:00",
        candidates=(CandidateInfo("candidate-b", "pubchem_sid", "SID:2"),
                    CandidateInfo("candidate-a", "pubchem_sid", "SID:1")),
        assays=(AssayInfo("assay-z", "Z", "confirmatory", "category", "category", "1", "USD", ()),),
        observations=(ObservationInfo("observation-1", "candidate-a", "assay-z", None,
                                      None, None, "active"),),
        budget_total="3", budget_spent="0", budget_reserved="0", budget_available="3",
        budget_unit="USD", executable_actions=tuple(actions), attempted_actions=(), view_digest="digest",
    )


def test_fixed_order_is_stable_and_ignores_input_array_order():
    actions = [
        ExecutableAction("candidate-z", "assay-a", "1", "USD"),
        ExecutableAction("candidate-a", "assay-z", "1", "USD"),
        ExecutableAction("candidate-a", "assay-b", "1", "USD"),
    ]
    selector = FixedOrderSelector()
    first = selector.select(_view(actions))
    second = selector.select(_view(list(reversed(actions))))
    assert (first.candidate_id, first.assay_id) == ("candidate-a", "assay-b")
    assert second == first


def test_seeded_priority_uses_canonical_utf8_hash_and_ignores_action_order():
    actions = [
        ExecutableAction("candidate-z", "assay-a", "1", "USD"),
        ExecutableAction("candidate-a", "assay-z", "1", "USD"),
        ExecutableAction("candidate-a", "assay-b", "1", "USD"),
    ]
    encoded = json.dumps(
        [RANDOM_PRIORITY_VERSION, 17, "candidate-a", "assay-b"],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    assert encoded == b'["random-priority-v1",17,"candidate-a","assay-b"]'
    expected = hashlib.sha256(encoded).digest()
    from assaypilot.run_loop import _priority_digest
    assert _priority_digest(17, "candidate-a", "assay-b") == expected

    selector = SeededRandomPrioritySelector(17)
    first = selector.select(_view(actions))
    second = selector.select(_view(list(reversed(actions))))
    assert first == second
    assert first.reason == "first eligible action by the fixed seeded SHA-256 priority"


def test_seeded_priority_ties_use_candidate_then_assay_order(monkeypatch):
    import assaypilot.run_loop as run_loop_module

    monkeypatch.setattr(run_loop_module, "_priority_digest", lambda *_args: b"same-digest")
    actions = [
        ExecutableAction("candidate-b", "assay-a", "1", "USD"),
        ExecutableAction("candidate-a", "assay-z", "1", "USD"),
        ExecutableAction("candidate-a", "assay-b", "1", "USD"),
    ]
    selected = SeededRandomPrioritySelector(3).select(_view(actions))
    assert (selected.candidate_id, selected.assay_id) == ("candidate-a", "assay-b")


@pytest.mark.parametrize("seed", [True, 1.0, "1", None])
def test_seeded_selector_rejects_ambiguous_seed_types(seed):
    with pytest.raises(RunLoopError) as error:
        SeededRandomPrioritySelector(seed)  # type: ignore[arg-type]
    assert error.value.code == "invalid_selector_seed"


def test_seeded_selector_rejects_unknown_version_and_empty_actions_stop():
    with pytest.raises(RunLoopError) as error:
        SeededRandomPrioritySelector(1, "random-priority-v2")
    assert error.value.code == "unsupported_selector_version"
    stopped = SeededRandomPrioritySelector(1).select(_view([]))
    assert stopped == SelectorProposal(kind="stop", stop_reason="no_executable_actions")


@pytest.mark.parametrize(
    ("selector_kind", "seed", "version", "error_code"),
    [
        ("unknown", None, None, "unsupported_selector"),
        ("seeded_random_priority", None, RANDOM_PRIORITY_VERSION, "invalid_selector_seed"),
        ("seeded_random_priority", 1, None, "unsupported_selector_version"),
        ("fixed_order", 1, RANDOM_PRIORITY_VERSION, "invalid_selector_config"),
    ],
)
def test_run_config_rejects_invalid_selector_binding(
    tmp_path, selector_kind, seed, version, error_code,
):
    _, public, oracle = _make_snapshot(tmp_path / "invalid-selector-config")
    coordinator = _coordinator(tmp_path / "invalid-selector-runtime", public, oracle)
    with pytest.raises(RunLoopError) as invalid:
        replace(
            _config(coordinator, "0.3"), selector_kind=selector_kind,
            selector_seed=seed, selector_algorithm_version=version,
        )
    assert invalid.value.code == error_code


def test_legacy_fixed_order_config_keeps_its_original_hash_shape(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "legacy-config")
    coordinator = _coordinator(tmp_path / "legacy-db", public, oracle)
    original = _config(coordinator, "0.3")
    payload = json.dumps(original.as_json_object(), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    restored = RunLoopConfig.from_json(payload, expected_sha256=digest)
    assert restored == original
    assert "selector_seed" not in restored.as_json_object()
    assert "selector_algorithm_version" not in restored.as_json_object()
    with pytest.raises(RunLoopError) as corrupt:
        RunLoopConfig.from_json(payload, expected_sha256="0" * 64)
    assert corrupt.value.code == "loop_config_corrupt"


def test_seeded_run_recovery_matches_uninterrupted_same_seed_and_other_run_id(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "seed-recovery")
    interrupted_oracle = SpyOracle(oracle)
    interrupted_coordinator = _coordinator(tmp_path / "interrupted-db", public, interrupted_oracle)
    config = replace(
        _config(interrupted_coordinator, "0.3", run_id="seed-recover", max_steps=2),
        selector_kind="seeded_random_priority", selector_seed=29,
        selector_algorithm_version=RANDOM_PRIORITY_VERSION,
    )

    def crash_after_publication(stage, step_no):
        if stage == "after_release" and step_no == 1:
            raise FaultInjectedCrash("published-before-loop-checkpoint")

    with pytest.raises(FaultInjectedCrash):
        RunLoopController(
            interrupted_coordinator,
            SeededRandomPrioritySelector(29),
            fault_hook=crash_after_publication,
        ).start(config)
    resumed_coordinator = _coordinator(tmp_path / "interrupted-db", public, interrupted_oracle)
    resumed = RunLoopController(
        resumed_coordinator, SeededRandomPrioritySelector(29),
    ).resume(config.run_id)

    control_oracle = SpyOracle(oracle)
    control_coordinator = _coordinator(tmp_path / "control-db", public, control_oracle)
    control_config = RunLoopConfig(
        run_id="seed-control-different-run-id",
        snapshot_id=control_coordinator.snapshot_id,
        runtime_database=str(control_coordinator.database_path),
        initial_budget=Cost(amount=Decimal("0.3"), unit="USD", assumed=True),
        cost_policy_version=control_coordinator.cost_policy_version,
        approval_policy="bounded_replay", approver_id="test-bounded-replay-policy",
        selector_kind="seeded_random_priority", selector_seed=29,
        selector_algorithm_version=RANDOM_PRIORITY_VERSION,
        max_steps=2, max_duration_seconds=120, max_action_retries=1,
        max_release_retries=2, selector_timeout_seconds=1.0,
    )
    control = RunLoopController(
        control_coordinator, SeededRandomPrioritySelector(29),
    ).start(control_config)

    def order(coordinator, run_id):
        with sqlite3.connect(coordinator.database_path) as db:
            return db.execute(
                "SELECT candidate_id, assay_id FROM loop_steps WHERE run_id = ? ORDER BY step_no",
                (run_id,),
            ).fetchall()

    assert order(resumed_coordinator, config.run_id) == order(control_coordinator, control_config.run_id)
    assert resumed.selection_steps == control.selection_steps == 2
    assert resumed.unique_executions == control.unique_executions == 2
    assert resumed.observations_added == control.observations_added
    assert interrupted_oracle.call_count == control_oracle.call_count == 2
    with sqlite3.connect(resumed_coordinator.database_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM release_settlements WHERE run_id = ?", (config.run_id,),
        ).fetchone()[0] == resumed.released_executions


def test_resume_rejects_different_persisted_selector_seed(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "seed-binding")
    spy = SpyOracle(oracle)
    coordinator = _coordinator(tmp_path, public, spy)
    base = _config(coordinator, "0.3", run_id="seed-binding", max_steps=3)
    config = RunLoopConfig(
        run_id=base.run_id, snapshot_id=base.snapshot_id,
        runtime_database=base.runtime_database, initial_budget=base.initial_budget,
        cost_policy_version=base.cost_policy_version, approval_policy=base.approval_policy,
        approver_id=base.approver_id, selector_kind="seeded_random_priority",
        selector_seed=11, selector_algorithm_version=RANDOM_PRIORITY_VERSION,
        max_steps=base.max_steps, max_duration_seconds=base.max_duration_seconds,
        max_action_retries=base.max_action_retries, max_release_retries=base.max_release_retries,
        selector_timeout_seconds=base.selector_timeout_seconds,
    )
    started = RunLoopController(coordinator, SeededRandomPrioritySelector(11)).start(
        config, stop_after_new_steps=1,
    )
    assert started.stop_reason == "user_interrupt" and started.resumable
    with pytest.raises(RunLoopError) as mismatch:
        RunLoopController(coordinator, SeededRandomPrioritySelector(12)).resume(config.run_id)
    assert mismatch.value.code == "selector_binding_mismatch"
    assert spy.call_count == 1


def test_hidden_followup_changes_do_not_change_initial_selector_view_or_choice(tmp_path):
    hidden_root, hidden_public, hidden_oracle = _make_snapshot(tmp_path / "with-hidden")
    empty_root, empty_public, empty_oracle = _make_snapshot(tmp_path / "without-hidden", empty_hidden=True)
    assert hidden_public.model_dump_json() == empty_public.model_dump_json()
    services = [
        _coordinator(tmp_path / name, public, oracle, name="runtime.sqlite")
        for name, public, oracle in (
            ("with", hidden_public, hidden_oracle), ("without", empty_public, empty_oracle),
        )
    ]
    views = []
    for index, service in enumerate(services):
        service.initialize_run(f"hidden-{index}", Cost(amount=Decimal("0.3"), unit="USD", assumed=True))
        public_view, statuses = service.get_loop_snapshot(f"hidden-{index}")
        controller = RunLoopController(service, FixedOrderSelector())
        eligible, _ = controller._enumerate_actions(public_view, statuses)
        view = controller._selector_view(public_view, eligible, [])
        views.append(view)
    assert views[0].as_json_object() == views[1].as_json_object()
    assert FixedOrderSelector().select(views[0]) == FixedOrderSelector().select(views[1])
    assert SeededRandomPrioritySelector(91).select(views[0]) == SeededRandomPrioritySelector(91).select(views[1])
    assert hidden_root != empty_root


def test_loop_runs_multiple_steps_and_no_record_stays_a_non_observation(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "empty", empty_hidden=True)
    spy = SpyOracle(oracle)
    coordinator = _coordinator(tmp_path, public, spy)
    summary = RunLoopController(coordinator, FixedOrderSelector()).start(
        _config(coordinator, "0.2", max_steps=5),
    )
    assert summary.selection_steps == 2
    assert summary.unique_executions == 2
    assert summary.no_record_executions == 2
    assert summary.released_executions == summary.failed_executions == 0
    assert summary.observations_added == 0
    assert summary.stop_reason == "no_executable_actions"
    assert Decimal(summary.spent) == 0 and Decimal(summary.reserved) == 0
    assert spy.call_count == 2
    assert len(coordinator.get_public_state("loop-run").public.observations) == len(public.observations)
    before_calls = summary.selector_calls
    reopened = _coordinator(tmp_path, public, spy)
    resumed = RunLoopController(reopened, FixedOrderSelector()).resume("loop-run")
    assert resumed == summary
    assert resumed.selector_calls == before_calls
    assert spy.call_count == 2


def test_zero_cost_actions_remain_executable_at_zero_budget(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "zero", cost="0", budget="0")
    coordinator = _coordinator(tmp_path, public, oracle)
    summary = RunLoopController(coordinator, FixedOrderSelector()).start(
        _config(coordinator, "0", max_steps=5),
    )
    assert summary.selection_steps == 2
    assert summary.released_executions == 2
    assert summary.spent == "0" and summary.available == "0"
    assert summary.stop_reason == "no_executable_actions"


def test_exact_budget_executes_once_then_reports_budget_exhausted(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "exact")
    coordinator = _coordinator(tmp_path, public, oracle)
    summary = RunLoopController(coordinator, FixedOrderSelector()).start(
        _config(coordinator, "0.1", max_steps=5),
    )
    assert summary.selection_steps == summary.released_executions == 1
    assert Decimal(summary.spent) == Decimal("0.1")
    assert Decimal(summary.reserved) == 0 and Decimal(summary.available) == 0
    assert summary.stop_reason == "budget_exhausted"


def test_insufficient_budget_and_unmet_prerequisites_do_not_call_selector_or_oracle(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "low")
    low_spy = SpyOracle(oracle)
    coordinator = _coordinator(tmp_path / "low-db", public, low_spy)
    selector = BadSelector({"kind": "stop", "stop_reason": "selector_stop"})
    summary = RunLoopController(coordinator, selector).start(_config(coordinator, "0.05"))
    assert summary.stop_reason == "budget_exhausted"
    assert summary.selector_calls == 0 and low_spy.call_count == 0

    _, unmet_public, unmet_oracle = _make_snapshot(tmp_path / "unmet", prerequisite_verdict="inactive")
    unmet_spy = SpyOracle(unmet_oracle)
    unmet_coordinator = _coordinator(tmp_path / "unmet-db", unmet_public, unmet_spy)
    unmet_summary = RunLoopController(unmet_coordinator, selector).start(_config(
        unmet_coordinator, "0.3", run_id="unmet-run",
    ))
    assert unmet_summary.stop_reason == "prerequisites_unmet"
    assert unmet_summary.selector_calls == 0 and unmet_spy.call_count == 0


def test_selector_stop_malformed_oversize_and_timeout_are_bounded(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "selector")
    cases = [
        (StopSelector(), "selector_stop"),
        (BadSelector({"kind": "select", "candidate_id": "x", "assay_id": "y", "reason": "x" * 20000}),
         "selector_output_too_large"),
        (BadSelector({"kind": "select", "candidate_id": "missing", "assay_id": "confirm-activity",
                      "reason": "fabricated"}), "policy_error"),
        (BadSelector(SelectorProposal("bad")), "selector_schema_error"),
        (BadSelectorTimeout(), "selector_timeout"),
    ]
    for index, (selector, expected) in enumerate(cases):
        coordinator = _coordinator(tmp_path / f"case-{index}", public, oracle, name="runtime.sqlite")
        summary = RunLoopController(coordinator, selector).start(_config(
            coordinator, "0.3", run_id=f"selector-{index}",
        ))
        assert summary.stop_reason == expected
        assert summary.unique_executions == 0


class BadSelectorTimeout:
    def select(self, _view):
        raise RunLoopError("selector_timeout", "bounded fixture timeout")


@pytest.mark.parametrize("stage", ["after_proposal", "after_approval", "after_execute", "after_release"])
def test_crash_at_each_durable_boundary_resumes_without_duplicate_effects(tmp_path, stage):
    _, public, oracle = _make_snapshot(tmp_path / stage)
    spy = SpyOracle(oracle)
    coordinator = _coordinator(tmp_path / stage / "runtime", public, spy)
    config = _config(coordinator, "0.2", run_id=f"recover-{stage}", max_steps=1)

    def crash_at(target, _step):
        if target == stage:
            raise FaultInjectedCrash(stage)

    interrupted = RunLoopController(coordinator, FixedOrderSelector(), fault_hook=crash_at)
    with pytest.raises(FaultInjectedCrash):
        interrupted.start(config)
    reopened = _coordinator(tmp_path / stage / "runtime", public, spy)
    summary = RunLoopController(reopened, FixedOrderSelector()).resume(config.run_id)
    assert summary.selection_steps == 1
    assert summary.unique_executions == summary.released_executions == 1
    assert summary.observations_added == 1
    assert summary.stop_reason == "max_steps"
    assert spy.call_count == 1
    assert reopened.get_current_budget(config.run_id).spent == Decimal("0.1")
    with sqlite3.connect(reopened.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM release_settlements WHERE run_id = ?",
                          (config.run_id,)).fetchone()[0] == 1
        row = db.execute("SELECT status, approval_status, execution_status, release_status FROM loop_steps WHERE run_id = ?",
                         (config.run_id,)).fetchone()
        assert row == ("released", "approved", "ready_for_release", "released")


def test_action_retry_reuses_same_step_and_request_after_database_reopen(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "retry")
    flaky = FailingOracle(oracle, failures=1)
    coordinator = _coordinator(tmp_path, public, flaky)
    config = _config(coordinator, "0.2", max_steps=1, action_retries=1)
    summary = RunLoopController(coordinator, FixedOrderSelector()).start(config)
    assert summary.retries == 1 and summary.unique_executions == 1
    assert summary.released_executions == 1 and flaky.call_count == 2
    with sqlite3.connect(coordinator.database_path) as db:
        row = db.execute("SELECT action_retries, request_id FROM loop_steps WHERE run_id = ?",
                         (config.run_id,)).fetchone()
        assert row[0] == 1
        assert row[1].startswith("loop-request-")


def test_action_retry_exhaustion_is_resumable_without_spending(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "retry-exhaust")
    flaky = FailingOracle(oracle, failures=None)
    coordinator = _coordinator(tmp_path, public, flaky)
    config = _config(coordinator, "0.2", max_steps=1, action_retries=1)
    stopped = RunLoopController(coordinator, FixedOrderSelector()).start(config)
    assert stopped.stop_reason == "retry_exhausted" and stopped.resumable
    assert stopped.selection_steps == 1 and stopped.unique_executions == 0
    assert stopped.retries == 2 and coordinator.get_current_budget(config.run_id).reserved == 0
    flaky.failures = 0
    resumed = RunLoopController(_coordinator(tmp_path, public, flaky), FixedOrderSelector()).resume(config.run_id)
    assert resumed.selection_steps == 1 and resumed.released_executions == 1
    assert resumed.unique_executions == 1 and resumed.stop_reason == "max_steps"
    assert resumed.spent == "0.1"


def test_release_retry_exhaustion_keeps_reservation_and_resume_finishes_same_execution(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "release-retry")
    spy = SpyOracle(oracle)
    coordinator = _coordinator(tmp_path, public, spy)
    config = _config(coordinator, "0.2", max_steps=1, release_retries=0)
    release = coordinator.release_result

    def unavailable(_run_id, _execution_id):
        raise RuntimeError("temporary synthetic release failure")

    coordinator.release_result = unavailable
    stopped = RunLoopController(coordinator, FixedOrderSelector()).start(config)
    assert stopped.stop_reason == "retry_exhausted" and stopped.resumable
    assert stopped.pending_releases == 1 and stopped.reserved == "0.1" and stopped.spent == "0"
    assert spy.call_count == 1
    coordinator.release_result = release
    resumed = RunLoopController(coordinator, FixedOrderSelector()).resume(config.run_id)
    assert resumed.pending_releases == 0 and resumed.released_executions == 1
    assert Decimal(resumed.spent) == Decimal("0.1") and Decimal(resumed.reserved) == 0
    assert spy.call_count == 1


def test_run_config_corruption_and_same_run_worker_lock_are_rejected(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "locking")
    coordinator = _coordinator(tmp_path, public, oracle)
    controller = RunLoopController(coordinator, StopSelector())
    config = _config(coordinator, "0.2", run_id="locked-run")
    first = controller.start(config)
    assert first.stop_reason == "selector_stop"
    with controller.repository.lock(config.run_id):
        with pytest.raises(RunLoopError) as locked:
            controller.resume(config.run_id)
        assert locked.value.code == "run_locked"

    with sqlite3.connect(coordinator.database_path) as db:
        db.execute("UPDATE loop_runs SET config_json = ? WHERE run_id = ?", ("{}", config.run_id))
    with pytest.raises(RunLoopError) as corrupted:
        controller.resume(config.run_id)
    assert corrupted.value.code == "loop_config_corrupt"


def test_independent_run_uses_a_distinct_worker_lock(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "locks")
    coordinator = _coordinator(tmp_path, public, oracle)
    controller = RunLoopController(coordinator, StopSelector())
    first = _config(coordinator, "0.2", run_id="lock-one")
    second = _config(coordinator, "0.2", run_id="lock-two")
    controller.start(first)
    with controller.repository.lock(first.run_id):
        result = controller.start(second)
    assert result.run_id == second.run_id and result.stop_reason == "selector_stop"


def test_deadline_expires_before_persisting_new_selection(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "deadline")
    coordinator = _coordinator(tmp_path, public, oracle)
    monotonic_now = [0.0]

    class AdvancesClock:
        def select(self, _view):
            monotonic_now[0] = 2.0
            return FixedOrderSelector().select(_view)

    summary = RunLoopController(
        coordinator, AdvancesClock(), monotonic=lambda: monotonic_now[0],
    ).start(_config(coordinator, "0.2", max_duration_seconds=1))
    assert summary.stop_reason == "deadline"
    assert summary.selection_steps == summary.unique_executions == 0
    assert summary.selector_calls == 1


@pytest.mark.parametrize("crash_stage", ["after_approval", "after_execute"])
def test_expired_resume_cancels_unstarted_action_but_recovers_committed_execution(tmp_path, crash_stage):
    _, public, oracle = _make_snapshot(tmp_path / crash_stage)
    spy = SpyOracle(oracle)
    clock = FixedClock(datetime(2025, 1, 2, tzinfo=timezone.utc))
    coordinator = _coordinator(tmp_path / crash_stage / "runtime", public, spy, clock=clock)
    config = _config(
        coordinator, "0.2", run_id=f"expired-{crash_stage}", max_steps=2,
        max_duration_seconds=1,
    )

    def crash_at(stage, _step):
        if stage == crash_stage:
            raise FaultInjectedCrash(stage)

    with pytest.raises(FaultInjectedCrash):
        RunLoopController(coordinator, FixedOrderSelector(), fault_hook=crash_at).start(config)
    clock.value += timedelta(seconds=2)

    reopened = _coordinator(tmp_path / crash_stage / "runtime", public, spy, clock=clock)
    summary = RunLoopController(reopened, FixedOrderSelector()).resume(config.run_id)
    assert summary.stop_reason == "deadline" and not summary.resumable
    assert summary.selection_steps == 1
    if crash_stage == "after_approval":
        assert summary.unique_executions == summary.released_executions == 0
        assert summary.rejected_steps == 1
        assert summary.reserved == "0" and summary.spent == "0"
        assert spy.call_count == 0
        with sqlite3.connect(reopened.database_path) as db:
            approval = db.execute(
                "SELECT status FROM approvals WHERE run_id = ?", (config.run_id,),
            ).fetchone()
            step = db.execute(
                "SELECT status, approval_status, error_code FROM loop_steps WHERE run_id = ?",
                (config.run_id,),
            ).fetchone()
        assert approval == ("canceled",)
        assert step == ("rejected", "cancelled", "deadline_expired_before_execution")
    else:
        assert summary.unique_executions == summary.released_executions == 1
        assert summary.observations_added == 1
        assert spy.call_count == 1


def test_action_retry_stops_at_deadline_and_cancels_approval(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "retry-deadline")
    monotonic_now = [0.0]

    class SlowFailingOracle:
        def __init__(self, store):
            self.store = store
            self.call_count = 0

        def lookup(self, _candidate_id, _assay_id):
            self.call_count += 1
            monotonic_now[0] = 2.0
            raise RuntimeError("one bounded in-flight lookup failure")

    slow_oracle = SlowFailingOracle(oracle.store)
    coordinator = _coordinator(tmp_path / "retry-deadline" / "runtime", public, slow_oracle)
    summary = RunLoopController(
        coordinator, FixedOrderSelector(), monotonic=lambda: monotonic_now[0],
    ).start(_config(coordinator, "0.2", max_steps=2, max_duration_seconds=1))
    assert summary.stop_reason == "deadline" and not summary.resumable
    assert summary.selection_steps == 1 and summary.rejected_steps == 1
    assert summary.unique_executions == 0 and summary.spent == summary.reserved == "0"
    assert summary.retries == slow_oracle.call_count == 1
    with sqlite3.connect(coordinator.database_path) as db:
        approval = db.execute(
            "SELECT status FROM approvals WHERE run_id = ?", (summary.run_id,),
        ).fetchone()
        step = db.execute(
            "SELECT status, action_retries, error_code FROM loop_steps WHERE run_id = ?",
            (summary.run_id,),
        ).fetchone()
    assert approval == ("canceled",)
    assert step == ("rejected", 1, "deadline_expired_before_execution")


def _make_dependent_snapshot(tmp_path: Path):
    config_data = json.loads((ROOT / "examples/stage1_configs/synthetic_linked.json").read_text())
    config_data["budget"]["amount"] = "0.5"
    first = next(item for item in config_data["assays"] if item["role"] != "primary")
    first["cost"]["amount"] = "0.1"
    second = json.loads(json.dumps(first))
    second.update({
        "assay_id": "second-confirm", "aid": 103,
        "name": "Synthetic second confirmatory assay",
        "concise_cache_key": "second_concise.csv",
        "description_cache_key": "second_description.json",
        "verdict_mapping": {"SECONDARY_POSITIVE": "active"},
        "prerequisites": [{"assay_id": "confirm-activity", "kind": "verdict", "verdict": "active"}],
    })
    config_data["assays"].append(second)
    config_data["raw_files"].extend([
        {"key": "second_concise.csv", "request_path": "fixture/second/concise.csv", "format": "csv"},
        {"key": "second_description.json", "request_path": "fixture/second/description.json", "format": "json"},
    ])
    temp = tmp_path / "dependent-source"
    temp.mkdir(parents=True)
    config_path = temp / "config.json"
    config_path.write_text(json.dumps(config_data))
    config = load_config(config_path)
    cache = temp / "cache"
    import shutil
    shutil.copytree(ROOT / "examples/stage1_fixture", cache)
    (cache / "second_concise.csv").write_text(
        "AID,SID,CID,Activity Outcome,Activity Value [uM]\n"
        "103,1001,2244,SECONDARY_POSITIVE,2.0\n"
        "103,1004,5957,SECONDARY_POSITIVE,3.0\n",
        encoding="utf-8",
    )
    (cache / "second_description.json").write_text(
        '{"PC_AssayContainer":[{"assay":{"descr":{"aid":{"id":103},'
        '"name":"Synthetic second confirmatory assay"}}}]}\n', encoding="utf-8",
    )
    root = temp / "snapshot"
    root.mkdir()
    shutil.copytree(cache, root / "raw")
    build_campaign(config, cache, root / "bundle")
    (root / "config").mkdir()
    shutil.copyfile(config_path, root / "config/campaign.json")
    from test_execution import _refresh_manifest
    _refresh_manifest(root)
    public = PublicBundleAdapter().load(DataSource(
        kind="public_bundle", location=str(root / "bundle/public/manifest.json"),
    ))
    oracle = ReplayOracle(load_replay_store(root, public))
    return root, public, oracle


def test_pending_private_result_does_not_unlock_next_action_until_release(tmp_path):
    _, public, oracle = _make_dependent_snapshot(tmp_path)
    spy = SpyOracle(oracle)
    coordinator = _coordinator(tmp_path, public, spy)
    config = _config(coordinator, "0.5", run_id="prereq-run", max_steps=3)

    def interrupt_after_execution(stage, step_no):
        if stage == "after_execute" and step_no == 1:
            raise FaultInjectedCrash(stage)

    with pytest.raises(FaultInjectedCrash):
        RunLoopController(
            coordinator, FixedOrderSelector(), fault_hook=interrupt_after_execution,
        ).start(config)
    with sqlite3.connect(coordinator.database_path) as db:
        selected = db.execute(
            "SELECT candidate_id, assay_id, status FROM loop_steps WHERE run_id = ?",
            (config.run_id,),
        ).fetchone()
    assert selected[1] == "confirm-activity" and selected[2] == "approved"
    with sqlite3.connect(coordinator.database_path) as db:
        execution_id = db.execute(
            "SELECT execution_id FROM executions WHERE run_id = ? AND candidate_id = ? AND assay_id = ?",
            (config.run_id, selected[0], selected[1]),
        ).fetchone()[0]
    pending_private = coordinator.read_private_result(config.run_id, execution_id)
    assert pending_private.measurements
    before = coordinator.get_public_state(config.run_id)
    assert len(before.public.observations) == len(public.observations)
    assert not coordinator.prerequisites_satisfied(selected[0], "second-confirm", before.public.observations)

    resumed = RunLoopController(coordinator, FixedOrderSelector()).resume(config.run_id)
    assert resumed.released_executions == 3
    state = coordinator.get_public_state(config.run_id)
    for observation in state.public.observations:
        if observation.assay_id == "second-confirm":
            assert any(
                prerequisite.candidate_id == observation.candidate_id
                and prerequisite.assay_id == "confirm-activity"
                and prerequisite.verdict.value == "active"
                for prerequisite in state.public.observations
            )


def test_seeded_selector_uses_released_prerequisite_in_the_next_eligible_set(tmp_path):
    _, public, oracle = _make_dependent_snapshot(tmp_path / "seeded-dependent")
    coordinator = _coordinator(tmp_path / "seeded-dependent-runtime", public, oracle)

    class RecordingSeededSelector:
        selector_kind = "seeded_random_priority"
        seed = 41
        algorithm_version = RANDOM_PRIORITY_VERSION

        def __init__(self):
            self.delegate = SeededRandomPrioritySelector(self.seed)
            self.views = []

        def select(self, view):
            self.views.append(view)
            return self.delegate.select(view)

    selector = RecordingSeededSelector()
    base = _config(coordinator, "0.5", run_id="seeded-prerequisite-run", max_steps=3)
    config = replace(
        base, selector_kind=selector.selector_kind, selector_seed=selector.seed,
        selector_algorithm_version=selector.algorithm_version,
    )
    summary = RunLoopController(coordinator, selector).start(config)

    assert summary.released_executions == summary.selection_steps == 3
    assert selector.views[0].executable_actions
    assert all(action.assay_id != "second-confirm" for action in selector.views[0].executable_actions)
    unlocked = [
        action for view in selector.views[1:] for action in view.executable_actions
        if action.assay_id == "second-confirm"
    ]
    assert unlocked
    for view in selector.views:
        attempted = {(item.candidate_id, item.assay_id) for item in view.attempted_actions}
        eligible = {(item.candidate_id, item.assay_id) for item in view.executable_actions}
        assert attempted.isdisjoint(eligible)


def test_known_replay_failure_is_recorded_without_observation_or_reselection(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "known-failure")
    failing = FailingOracle(oracle, replay_failure=True)
    coordinator = _coordinator(tmp_path, public, failing)
    summary = RunLoopController(coordinator, FixedOrderSelector()).start(
        _config(coordinator, "0.3", max_steps=5),
    )
    assert summary.failed_executions == 2 and summary.selection_steps == 2
    assert summary.observations_added == 0 and summary.spent == "0"
    assert summary.stop_reason == "no_executable_actions"
    assert failing.call_count == 2


def test_isolated_python_selector_roundtrip_and_private_canary_denial(tmp_path):
    _, public, oracle = _make_snapshot(tmp_path / "isolated")
    canary = tmp_path / "private-canary.txt"
    canary.write_text("private-selector-canary", encoding="utf-8")
    spy = SpyOracle(oracle)
    coordinator = _coordinator(tmp_path, public, spy)
    probe = IsolatedFixedOrderSelector(timeout_seconds=5.0, private_canary_path=canary)
    probe_result = probe.select(_view([ExecutableAction("candidate-a", "assay-z", "1", "USD")]))
    assert probe_result.candidate_id == "candidate-a"
    assert probe.canary_denied
    probe.close()

    selector = IsolatedFixedOrderSelector(timeout_seconds=5.0, private_canary_path=canary)
    summary = RunLoopController(coordinator, selector).start(
        _config(coordinator, "0.1", max_steps=1, selector_timeout=5.0),
    )
    selector.close()
    assert selector.canary_denied, summary
    assert summary.selection_steps == summary.released_executions == 1
    assert summary.stop_reason == "max_steps"
    assert spy.call_count == 1


def test_isolated_seeded_selector_roundtrip_uses_seeded_worker_and_denies_canary(tmp_path):
    canary = tmp_path / "seeded-private-canary.txt"
    canary.write_text("seeded-worker-private", encoding="utf-8")
    view = _view([
        ExecutableAction("candidate-b", "assay-z", "1", "USD"),
        ExecutableAction("candidate-a", "assay-z", "1", "USD"),
    ])
    expected = SeededRandomPrioritySelector(73).select(view)
    with IsolatedSelector(
        selector_kind="seeded_random_priority", seed=73,
        algorithm_version=RANDOM_PRIORITY_VERSION, timeout_seconds=5.0,
        private_canary_path=canary,
    ) as selector:
        actual = selector.select(view)
        assert selector.canary_denied
    assert actual == expected


def test_proposal_validation_rejects_non_json_and_wrong_stop_contract():
    with pytest.raises(RunLoopError) as malformed:
        _validate_proposal({"kind": "select", "candidate_id": "c", "assay_id": "a", "reason": "ok", "extra": True})
    assert malformed.value.code == "selector_schema_error"
    with pytest.raises(RunLoopError) as stop:
        _validate_proposal({"kind": "stop", "stop_reason": "records_found"})
    assert stop.value.code == "selector_schema_error"
