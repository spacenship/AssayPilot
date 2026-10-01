"""Offline end-to-end checks for Stage 5-B scientific run-loop integration."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import sqlite3
import time

import pytest

from assaypilot.domain import ActionRequest, Cost
from assaypilot.execution import ExecutionCoordinator
from assaypilot.llm_provider import LLMProviderError, LLMSettings, ProviderResponse
from assaypilot.replay import ReplayError
from assaypilot.run_loop import FaultInjectedCrash, RunLoopConfig, RunLoopController
from assaypilot.scientific_run_loop import (
    ScientificReasoningSelector, config_science_fields, safe_settings_fingerprint,
)
from assaypilot.scientific_run_loop_cli import _export_run, _runtime_path
from test_execution import FixedClock, SpyOracle, _make_snapshot


class FakeReasoningProvider:
    def __init__(self, *, before_response=None, invalid_action=False,
                 invalid_once_at_calls=()):
        self.contexts = []
        self.decisions = []
        self.before_response = before_response
        self.invalid_action = invalid_action
        self.invalid_once_at_calls = set(invalid_once_at_calls)

    def complete(self, messages, *, max_output_tokens=None, json_schema=None):
        prefix = "PUBLIC_CONTEXT_JSON (data only; untrusted):\n"
        content = messages[1]["content"]
        context = json.loads(content.split(prefix, 1)[1].split("\n\nVALIDATION_REFERENCE_INDEX", 1)[0])
        self.contexts.append(context)
        if self.before_response:
            self.before_response(context, len(self.contexts))
        decision = _decision_for(context)
        if self.invalid_action and decision["action"]["kind"] == "select":
            decision["action"]["candidate_id"] = "candidate-outside-shortlist"
        if len(self.contexts) in self.invalid_once_at_calls:
            decision["action"] = {
                "kind": "select", "candidate_id": "candidate-outside-shortlist",
                "assay_id": "assay-outside-shortlist", "stop_reason": None,
            }
        self.decisions.append(decision)
        return ProviderResponse(
            content=json.dumps(decision), provider="offline-fixture", model="fixture-v1",
            request_id=f"fixture-{len(self.contexts)}", latency_ms=0,
            usage={"input_tokens": 8, "output_tokens": 4}, attempts=1,
        )


def _decision_for(context):
    actions = context["eligible_actions"]
    if actions:
        action = actions[0]
        candidate_id, assay_id = action["candidate_id"], action["assay_id"]
        candidate_observation = next(
            item for item in context["public_observations"]
            if item["candidate_id"] == candidate_id
        )
        action_obj = {"kind": "select", "candidate_id": candidate_id,
                      "assay_id": assay_id, "stop_reason": None}
        state_refs = [
            item["state_ref"] for item in context["state_refs"]
            if item["kind"] in {"eligibility", "budget"}
        ]
        basis_refs = candidate_observation["evidence_refs"][:1]
        decision_basis = "exploratory"
        rationale = "The bounded public context supports an exploratory replay proposal."
    else:
        candidate_id = assay_id = None
        action_obj = {"kind": "stop", "candidate_id": None, "assay_id": None,
                      "stop_reason": "no eligible action remains"}
        state_refs, basis_refs = [], []
        decision_basis = "insufficient_information"
        rationale = "No executable action remains; interpret the released public records only."

    interpretations = []
    for observation_id in context["newly_released_observation_ids"]:
        observation = next(item for item in context["public_observations"]
                           if item["observation_id"] == observation_id)
        verdict = observation["verdict"]
        outcome = verdict if verdict in {"active", "inactive"} else "unknown"
        interpretations.append({
            "candidate_id": observation["candidate_id"], "assay_id": observation["assay_id"],
            "outcome": outcome, "observation_id": observation_id, "state_ref": None,
            "evidence_refs": observation["evidence_refs"],
            "interpretation": f"The named assay records {outcome} for this candidate under the supplied conditions.",
        })

    hypotheses = []
    if actions and not any(
        item["hypothesis_kind"] == "assay_activity"
        and item["candidate_id"] == candidate_id and item["assay_id"] == assay_id
        for item in context["prior_hypotheses"]
    ):
        hypotheses.append({
            "hypothesis_id": f"h-activity-{candidate_id}-{assay_id}",
            "hypothesis_kind": "assay_activity",
            "statement": f"Candidate {candidate_id} is expected to be active in assay {assay_id}.",
            "candidate_id": candidate_id, "assay_id": assay_id,
            "expected_outcome": "active", "status": "proposed", "evidence_refs": [],
            "limitations": ["A single assay result is limited to its named conditions."],
        })

    prior_updates = []
    for prior in context["prior_hypotheses"]:
        matching = [item for item in context["newly_released_observation_ids"]
                    if (observation := next(o for o in context["public_observations"]
                                            if o["observation_id"] == item))["assay_id"] == prior["assay_id"]
                    and (prior["candidate_id"] is None
                         or observation["candidate_id"] == prior["candidate_id"])]
        if matching:
            observation = next(o for o in context["public_observations"]
                               if o["observation_id"] == matching[0])
            if prior["hypothesis_kind"] == "assay_activity":
                if observation["verdict"] not in {"active", "inactive"}:
                    status = "unresolved"
                elif observation["verdict"] == prior["expected_outcome"]:
                    status = "supported"
                else:
                    status = "weakened"
            else:
                status = "unresolved"
            prior_updates.append({
                "hypothesis_id": prior["hypothesis_id"], "previous_status": prior["status"],
                "new_status": status, "observation_refs": matching,
                "evidence_refs": observation["evidence_refs"],
                "rationale": f"The same assay recorded {observation['verdict']}; this updates the scoped hypothesis only.",
            })

    return {
        "schema_version": "assaypilot.scientific-decision.v2",
        "decision_basis": decision_basis,
        "action": action_obj,
        "hypotheses": hypotheses,
        "prior_updates": prior_updates,
        "basis_evidence_refs": basis_refs,
        "state_refs": state_refs,
        "concise_rationale": rationale,
        "expected_information": "A released result may reduce uncertainty for the named assay.",
        "interpretations": interpretations,
        "information_gaps": [],
        "limitations": ["Replay records are not a claim of clinical benefit or direct binding."],
    }


def _science_config(coordinator, *, run_id="science-loop", max_steps=2,
                    max_duration_seconds=120, max_llm_calls=10,
                    interpretation_reserve_calls=2):
    settings = LLMSettings(
        provider="offline-fixture", endpoint="https://fixture.invalid/v1/responses",
        model="fixture-v1", api_key="fixture-secret", response_format="prompt_only",
        timeout_seconds=5, retry_attempts=0, api_mode="responses",
    )
    fingerprint = safe_settings_fingerprint(
        settings, output_tokens=1200, shortlist_size=24, shortlist_seed=3,
        max_llm_calls=max_llm_calls,
        interpretation_reserve_calls=interpretation_reserve_calls,
    )
    scientific_fields = config_science_fields(
        settings, settings_sha256=fingerprint, output_tokens=1200,
        shortlist_size=24, shortlist_seed=3, max_llm_calls=max_llm_calls,
        interpretation_reserve_calls=interpretation_reserve_calls,
        max_stale_redecisions=2,
    )
    return settings, RunLoopConfig(
        run_id=run_id, snapshot_id=coordinator.snapshot_id,
        runtime_database=str(coordinator.database_path),
        initial_budget=Cost(amount=Decimal("5"), unit="USD", assumed=False),
        cost_policy_version=coordinator.cost_policy_version,
        approval_policy="bounded_replay", approver_id="test-scientific-policy",
        selector_kind="scientific_reasoner", max_steps=max_steps,
        max_duration_seconds=max_duration_seconds, max_action_retries=0,
        max_release_retries=1, selector_timeout_seconds=5.0, **scientific_fields,
    )


def _coordinator(tmp_path, public, oracle, clock):
    return ExecutionCoordinator(
        tmp_path / "runtime.sqlite", public, oracle,
        cost_policy_version="stage3a-fixture-cost-v1", clock=clock,
    )


def _controller(coordinator, settings, fake, *, fault_hook=None, max_llm_calls=10,
                interpretation_reserve_calls=2):
    selector = ScientificReasoningSelector(
        coordinator, settings, research_goal="Interpret public confirmatory replay evidence.",
        provider_factory=lambda _settings: fake, shortlist_size=24, shortlist_seed=3,
        max_llm_calls=max_llm_calls,
        interpretation_reserve_calls=interpretation_reserve_calls,
        max_stale_redecisions=2,
    )
    return RunLoopController(coordinator, selector, fault_hook=fault_hook)


def test_managed_runtime_directory_permissions_are_private_on_start_and_resume(tmp_path, monkeypatch):
    import stat
    import assaypilot.scientific_run_loop_cli as cli

    monkeypatch.setattr(cli, "ROOT", tmp_path)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    database = _runtime_path(snapshot, "run-private", None)
    managed = tmp_path / "runtime" / "stage5b"
    assert database == managed / "run-private" / "private" / "execution.sqlite"
    assert stat.S_IMODE(managed.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.parent.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.parent.stat().st_mode) == 0o700

    database.parent.parent.chmod(0o755)
    resumed = _runtime_path(snapshot, "run-private", database)
    assert resumed == database
    assert stat.S_IMODE(database.parent.parent.stat().st_mode) == 0o700


def test_scientific_loop_replays_actions_interprets_observations_and_updates_same_hypothesis(tmp_path):
    _, public, base_oracle = _make_snapshot(tmp_path / "snapshot", cost="0.1", budget="5")
    oracle = SpyOracle(base_oracle)
    clock = FixedClock(datetime(2025, 1, 2, tzinfo=timezone.utc))
    coordinator = _coordinator(tmp_path / "private", public, oracle, clock)
    settings, config = _science_config(coordinator)
    fake = FakeReasoningProvider()

    summary = _controller(coordinator, settings, fake).start(config)

    assert summary.selection_steps == 2
    assert summary.unique_executions == summary.released_executions == 2
    assert summary.stop_reason == "max_steps"
    assert oracle.call_count == 2
    assert len(fake.contexts) == 3
    # Each action reserves and then settles budget, advancing public state twice.
    assert [item["public_state_version"] for item in fake.contexts] == [0, 2, 4]
    first_action = fake.decisions[0]["action"]
    first_hypothesis_id = f"h-activity-{first_action['candidate_id']}-{first_action['assay_id']}"
    assert fake.contexts[1]["prior_hypotheses"][0]["hypothesis_id"] == first_hypothesis_id
    assert fake.contexts[1]["prior_hypotheses"][0]["status"] == "proposed"
    assert fake.contexts[1]["prior_hypotheses"][0]["hypothesis_kind"] == "assay_activity"
    assert fake.contexts[1]["prior_hypotheses"][0]["expected_outcome"] == "active"
    assert fake.contexts[1]["prior_hypotheses"][0]["statement"] == (
        f"Candidate {first_action['candidate_id']} is expected to be active in assay {first_action['assay_id']}."
    )
    first_update = next(item for item in fake.decisions[1]["prior_updates"]
                        if item["hypothesis_id"] == first_hypothesis_id)
    updated_observation = next(
        item for item in fake.contexts[1]["public_observations"]
        if item["observation_id"] in first_update["observation_refs"]
    )
    assert (updated_observation["candidate_id"], updated_observation["assay_id"]) == (
        first_action["candidate_id"], first_action["assay_id"],
    )
    expected_update_status = (
        "supported" if updated_observation["verdict"] == "active" else
        "weakened" if updated_observation["verdict"] == "inactive" else "unresolved"
    )
    assert first_update["new_status"] == expected_update_status
    assert first_update["evidence_refs"] == updated_observation["evidence_refs"]
    assert all(fake.contexts[0]["shortlist"][key] == value for key, value in {
        "generation_public_state_version": 0,
        "context_candidate_count": 2,
        "current_eligible_action_count": 2,
    }.items())

    with sqlite3.connect(coordinator.database_path) as db:
        decisions = db.execute(
            "SELECT status,action_step_no FROM loop_scientific_decisions WHERE run_id=? ORDER BY decision_no",
            (config.run_id,),
        ).fetchall()
        hypothesis = json.loads(db.execute(
            "SELECT hypothesis_json FROM loop_scientific_hypotheses WHERE run_id=? AND hypothesis_id=?",
            (config.run_id, first_hypothesis_id),
        ).fetchone()[0])
        events = db.execute(
            "SELECT event_type,previous_status,new_status FROM loop_scientific_hypothesis_events "
            "WHERE run_id=? ORDER BY event_no", (config.run_id,),
        ).fetchall()
        interpretations = db.execute(
            "SELECT COUNT(*) FROM loop_scientific_interpretations WHERE run_id=?", (config.run_id,),
        ).fetchone()[0]
        steps = db.execute(
            "SELECT candidate_id,assay_id,status,scientific_decision_id FROM loop_steps "
            "WHERE run_id=? ORDER BY step_no",
            (config.run_id,),
        ).fetchall()
        applied_actions = db.execute(
            "SELECT selected_candidate_id,selected_assay_id,action_step_no FROM loop_scientific_decisions "
            "WHERE run_id=? AND status='applied' AND action_step_no IS NOT NULL ORDER BY decision_no",
            (config.run_id,),
        ).fetchall()
        execution_actions = db.execute(
            "SELECT candidate_id,assay_id FROM executions WHERE run_id=?",
            (config.run_id,),
        ).fetchall()
    assert decisions == [("applied", 1), ("applied", 2), ("applied", None)]
    assert events[0] == ("proposed", None, "proposed")
    assert ("updated", "proposed", hypothesis["status"]) in events
    assert hypothesis["interpretation"].startswith("The same assay recorded")
    assert interpretations == 2
    assert all(row[2] == "released" and row[3] for row in steps)
    # The model's selected action is the action persisted and executed by the
    # trusted controller; no fixed-order/seeded selector substitutes it.
    assert [(row[0], row[1], row[2]) for row in applied_actions] == [
        (decision["action"]["candidate_id"], decision["action"]["assay_id"], index)
        for index, decision in enumerate(fake.decisions[:2], start=1)
    ]
    assert [(row[0], row[1]) for row in steps] == [
        (decision["action"]["candidate_id"], decision["action"]["assay_id"])
        for decision in fake.decisions[:2]
    ]
    assert {(row[0], row[1]) for row in execution_actions} == {
        (decision["action"]["candidate_id"], decision["action"]["assay_id"])
        for decision in fake.decisions[:2]
    }


def test_reserved_calls_cover_action_repair_and_final_interpretation_repair(tmp_path):
    _, public, base_oracle = _make_snapshot(tmp_path / "snapshot", cost="0.1", budget="5")
    oracle = SpyOracle(base_oracle)
    clock = FixedClock(datetime(2025, 1, 2, tzinfo=timezone.utc))
    coordinator = _coordinator(tmp_path / "private", public, oracle, clock)
    settings, config = _science_config(
        coordinator, run_id="reserved-calls-loop", max_steps=1, max_llm_calls=4,
    )
    # Call 1 is an invalid action and call 3 is an invalid interpretation-only
    # action. Their single repairs must fit inside the same four-call cap.
    fake = FakeReasoningProvider(invalid_once_at_calls={1, 3})

    summary = _controller(coordinator, settings, fake, max_llm_calls=4).start(config)

    assert summary.stop_reason == "max_steps"
    assert summary.selection_steps == summary.released_executions == 1
    assert summary.unique_executions == oracle.call_count == 1
    assert len(fake.contexts) == 4
    assert fake.contexts[0]["decision_mode"] == fake.contexts[1]["decision_mode"] == "action"
    assert fake.contexts[2]["decision_mode"] == fake.contexts[3]["decision_mode"] == "interpretation_only"
    assert fake.contexts[2]["eligible_actions"] == []
    assert len(fake.contexts[2]["newly_released_observation_ids"]) == 1
    selected = fake.decisions[1]["action"]
    released = next(
        item for item in fake.contexts[2]["public_observations"]
        if item["observation_id"] in fake.contexts[2]["newly_released_observation_ids"]
    )
    hypothesis_id = f"h-activity-{selected['candidate_id']}-{selected['assay_id']}"
    final = fake.decisions[3]
    assert final["action"]["kind"] == "stop"
    assert final["hypotheses"] == []
    assert final["prior_updates"][0]["hypothesis_id"] == hypothesis_id
    assert final["prior_updates"][0]["observation_refs"] == [released["observation_id"]]
    expected_status = (
        "supported" if released["verdict"] == "active" else
        "weakened" if released["verdict"] == "inactive" else "unresolved"
    )
    assert final["prior_updates"][0]["new_status"] == expected_status

    with sqlite3.connect(coordinator.database_path) as db:
        call_rows = db.execute(
            "SELECT decision_id,COUNT(*) FROM loop_scientific_api_calls "
            "WHERE run_id=? GROUP BY decision_id ORDER BY MIN(call_no)",
            (config.run_id,),
        ).fetchall()
        step = db.execute(
            "SELECT candidate_id,assay_id,status FROM loop_steps WHERE run_id=?",
            (config.run_id,),
        ).fetchone()
        stored_hypothesis = json.loads(db.execute(
            "SELECT hypothesis_json FROM loop_scientific_hypotheses "
            "WHERE run_id=? AND hypothesis_id=?", (config.run_id, hypothesis_id),
        ).fetchone()[0])
        run_state = db.execute(
            "SELECT interpretation_complete,pending_observation_ids_json "
            "FROM loop_scientific_run_state WHERE run_id=?", (config.run_id,),
        ).fetchone()
        interpreted_count = db.execute(
            "SELECT COUNT(*) FROM loop_scientific_interpretations WHERE run_id=?",
            (config.run_id,),
        ).fetchone()[0]
    assert len(call_rows) == 2 and [row[1] for row in call_rows] == [2, 2]
    assert step == (selected["candidate_id"], selected["assay_id"], "released")
    assert stored_hypothesis["status"] == expected_status
    assert stored_hypothesis["interpretation"].startswith("The same assay recorded")
    assert run_state == (1, "[]")
    assert interpreted_count == 1
    artifact = _export_run(coordinator.database_path, config, summary)
    exported_summary = json.loads((artifact / "summary.json").read_text())
    metrics = exported_summary["scientific_metrics"]
    assert metrics["schema_valid_decisions"] == 2
    assert metrics["replay_executions"] == 1
    assert metrics["released_observations"] == 1
    assert metrics["interpreted_new_observations"] == 1
    assert metrics["pending_new_observations"] == 0
    assert metrics["interpretation_complete"] is True
    assert metrics["api_calls"] == {
        "total": 4, "action_decision_calls": 2, "action_decision_initial_calls": 1,
        "stale_redecision_calls": 0, "finalization_calls": 2,
        "finalization_initial_calls": 1, "action_repairs": 1,
        "finalization_repairs": 1, "failed": 0,
    }
    assert metrics["api_token_usage_by_field"] == {"input_tokens": 32, "output_tokens": 16}
    assert metrics["api_usage_unreported_calls"] == 0
    assert metrics["hypotheses"]["assay_activity"]["current_by_status"] == {expected_status: 1}


def test_reserve_prevents_new_action_when_only_three_calls_remain_and_no_observation_is_pending(tmp_path):
    _, public, base_oracle = _make_snapshot(tmp_path / "snapshot", cost="0.1", budget="5")
    oracle = SpyOracle(base_oracle)
    clock = FixedClock(datetime(2025, 1, 2, tzinfo=timezone.utc))
    coordinator = _coordinator(tmp_path / "private", public, oracle, clock)
    settings, config = _science_config(
        coordinator, run_id="three-calls-no-pending", max_steps=1, max_llm_calls=3,
    )
    fake = FakeReasoningProvider()

    summary = _controller(coordinator, settings, fake, max_llm_calls=3).start(config)

    assert summary.stop_reason == "llm_call_limit"
    assert summary.selection_steps == summary.unique_executions == oracle.call_count == 0
    assert fake.contexts == []


def test_stale_provider_result_is_saved_but_not_applied_then_redecided(tmp_path):
    _, public, base_oracle = _make_snapshot(tmp_path / "snapshot", cost="0.1", budget="5")
    oracle = SpyOracle(base_oracle)
    clock = FixedClock(datetime(2025, 1, 2, tzinfo=timezone.utc))
    coordinator = _coordinator(tmp_path / "private", public, oracle, clock)
    settings, config = _science_config(coordinator, run_id="stale-loop", max_steps=1)
    external_done = False

    def publish_during_provider(context, call_no):
        nonlocal external_done
        if call_no != 1 or external_done:
            return
        selected = context["eligible_actions"][0]
        action = ActionRequest(
            action_id="concurrent-publication", campaign_id=public.campaign.campaign_id,
            candidate_id=selected["candidate_id"], assay_id=selected["assay_id"],
        )
        coordinator.approve_action(config.run_id, action, approver_id="fixture", reason="stale-state fixture")
        receipt = coordinator.execute(config.run_id, "concurrent-publication-request", action)
        coordinator.release_result(config.run_id, receipt.execution_id)
        external_done = True

    fake = FakeReasoningProvider(before_response=publish_during_provider)
    summary = _controller(coordinator, settings, fake).start(config)

    assert summary.selection_steps == 1
    # The summary counts controller-owned steps; the concurrent fixture action
    # is separately visible in the coordinator's authoritative execution table.
    assert summary.unique_executions == 1
    assert oracle.call_count == 2
    assert fake.contexts[0]["public_state_version"] == 0
    assert [item["public_state_version"] for item in fake.contexts] == [0, 2, 4]
    with sqlite3.connect(coordinator.database_path) as db:
        statuses = db.execute(
            "SELECT status,selected_candidate_id,action_step_no FROM loop_scientific_decisions "
            "WHERE run_id=? ORDER BY decision_no", (config.run_id,),
        ).fetchall()
        hypotheses = db.execute(
            "SELECT COUNT(*) FROM loop_scientific_hypotheses WHERE run_id=?", (config.run_id,),
        ).fetchone()[0]
        steps = db.execute("SELECT candidate_id,status FROM loop_steps WHERE run_id=?", (config.run_id,)).fetchall()
        all_executions = db.execute(
            "SELECT COUNT(*) FROM executions WHERE run_id=?", (config.run_id,),
        ).fetchone()[0]
    assert statuses[0] == ("stale", None, None)
    assert statuses[1][0] == "applied"
    assert hypotheses == 1  # stale proposal did not create its hypothesis
    assert len(steps) == 1 and steps[0][1] == "released"
    assert all_executions == 2  # external publication and the one controller step
    assert steps[0][0] != fake.contexts[0]["eligible_actions"][0]["candidate_id"]


@pytest.mark.parametrize(
    "crash_stage",
    ["after_proposal", "after_approval", "after_execute", "after_release"],
)
def test_interrupted_execution_resumes_without_repeating_provider_action_or_lookup(tmp_path, crash_stage):
    _, public, base_oracle = _make_snapshot(tmp_path / "snapshot", cost="0.1", budget="5")
    oracle = SpyOracle(base_oracle)
    clock = FixedClock(datetime(2025, 1, 2, tzinfo=timezone.utc))
    coordinator = _coordinator(tmp_path / "private", public, oracle, clock)
    settings, config = _science_config(coordinator, run_id="crash-loop", max_steps=1)
    fake = FakeReasoningProvider()

    def crash_at_boundary(stage, _step_no):
        if stage == crash_stage:
            raise FaultInjectedCrash(f"crash at durable boundary {crash_stage}")

    with pytest.raises(FaultInjectedCrash):
        _controller(coordinator, settings, fake, fault_hook=crash_at_boundary).start(config)
    assert len(fake.contexts) == 1

    reopened = ExecutionCoordinator(
        coordinator.database_path, public, oracle,
        cost_policy_version="stage3a-fixture-cost-v1", clock=clock,
    )
    summary = _controller(reopened, settings, fake).resume(config.run_id)

    assert summary.selection_steps == 1
    assert summary.released_executions == 1
    assert summary.stop_reason == "max_steps"
    assert oracle.call_count == 1
    assert len(fake.contexts) == 2  # only the required final observation interpretation
    with sqlite3.connect(reopened.database_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM loop_steps WHERE run_id=?", (config.run_id,),
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM loop_scientific_interpretations WHERE run_id=?", (config.run_id,),
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM loop_scientific_api_calls WHERE run_id=?", (config.run_id,),
        ).fetchone()[0] == 2


def test_invalid_model_action_is_recorded_without_changing_public_or_execution_state(tmp_path):
    _, public, base_oracle = _make_snapshot(tmp_path / "snapshot", cost="0.1", budget="5")
    oracle = SpyOracle(base_oracle)
    clock = FixedClock(datetime(2025, 1, 2, tzinfo=timezone.utc))
    coordinator = _coordinator(tmp_path / "private", public, oracle, clock)
    settings, config = _science_config(coordinator, run_id="invalid-action-loop", max_steps=1)
    fake = FakeReasoningProvider(invalid_action=True)

    summary = _controller(coordinator, settings, fake).start(config)

    assert summary.selection_steps == summary.unique_executions == 0
    assert summary.stop_reason == "decision_validation_failed"
    assert oracle.call_count == 0
    assert len(fake.contexts) == 2  # initial structured reply and one bounded repair
    assert fake.contexts[0]["public_state_version"] == fake.contexts[1]["public_state_version"] == 0
    with sqlite3.connect(coordinator.database_path) as db:
        decision = db.execute(
            "SELECT status,error_code FROM loop_scientific_decisions WHERE run_id=?",
            (config.run_id,),
        ).fetchone()
        steps = db.execute("SELECT COUNT(*) FROM loop_steps WHERE run_id=?", (config.run_id,)).fetchone()[0]
        hypotheses = db.execute(
            "SELECT COUNT(*) FROM loop_scientific_hypotheses WHERE run_id=?", (config.run_id,),
        ).fetchone()[0]
        calls = db.execute(
            "SELECT COUNT(*),SUM(status='completed') FROM loop_scientific_api_calls WHERE run_id=?",
            (config.run_id,),
        ).fetchone()
    current = coordinator.get_public_state(config.run_id)
    assert decision == ("failed", "decision_validation_failed")
    assert steps == hypotheses == oracle.call_count == 0
    assert calls == (2, 2)
    assert current.state_version == 0
    assert current.state.budget.spent == Decimal("0")
    assert current.state.budget.reserved == Decimal("0")


def test_provider_factory_failure_closes_recorded_call_without_applying_decision(tmp_path):
    _, public, base_oracle = _make_snapshot(tmp_path / "snapshot", cost="0.1", budget="5")
    oracle = SpyOracle(base_oracle)
    clock = FixedClock(datetime(2025, 1, 2, tzinfo=timezone.utc))
    coordinator = _coordinator(tmp_path / "private", public, oracle, clock)
    settings, config = _science_config(coordinator, run_id="provider-factory-loop", max_steps=1)

    def fail_factory(_settings):
        raise LLMProviderError("fixture_provider_setup_failed", "fixture provider setup failed")

    selector = ScientificReasoningSelector(
        coordinator, settings, research_goal="Test safe provider initialization failure.",
        provider_factory=fail_factory,
        max_llm_calls=config.science_max_llm_calls,
        interpretation_reserve_calls=config.science_interpretation_reserve_calls,
    )
    summary = RunLoopController(coordinator, selector).start(config)

    assert summary.stop_reason == "fixture_provider_setup_failed"
    assert summary.selection_steps == summary.unique_executions == oracle.call_count == 0
    with sqlite3.connect(coordinator.database_path) as db:
        call = db.execute(
            "SELECT status,error_code,diagnostic_json FROM loop_scientific_api_calls WHERE run_id=?",
            (config.run_id,),
        ).fetchone()
        decision = db.execute(
            "SELECT status,error_code FROM loop_scientific_decisions WHERE run_id=?",
            (config.run_id,),
        ).fetchone()
        steps = db.execute("SELECT COUNT(*) FROM loop_steps WHERE run_id=?", (config.run_id,)).fetchone()[0]
    assert call[:2] == ("failed", "fixture_provider_setup_failed")
    diagnostic = json.loads(call[2])
    assert diagnostic == {
        "failure_stage": "client_creation",
        "exception_class": "LLMProviderError",
        "cause_code": "fixture_provider_setup_failed",
        "cause_class": None,
        "http_response_received": False,
        "http_status": None,
        "latency_ms": diagnostic["latency_ms"],
    }
    assert isinstance(diagnostic["latency_ms"], int)
    assert decision == ("failed", "fixture_provider_setup_failed")
    assert steps == 0


def test_provider_connection_diagnostics_are_persisted_without_raw_error(tmp_path):
    _, public, base_oracle = _make_snapshot(tmp_path / "snapshot", cost="0.1", budget="5")
    oracle = SpyOracle(base_oracle)
    clock = FixedClock(datetime(2025, 1, 2, tzinfo=timezone.utc))
    coordinator = _coordinator(tmp_path / "private", public, oracle, clock)
    settings, config = _science_config(coordinator, run_id="dns-diagnostic-loop", max_steps=1)

    class FailingProvider:
        def complete(self, *_args, **_kwargs):
            raise LLMProviderError(
                "provider_connection_error", "provider connection failed or timed out",
                failure_stage="connection", exception_class="URLError",
                cause_code="dns_temporary_failure", cause_class="gaierror",
                http_response_received=False, latency_ms=17,
            )

    selector = ScientificReasoningSelector(
        coordinator, settings, research_goal="Test persisted connection diagnostics.",
        provider_factory=lambda _settings: FailingProvider(),
        max_llm_calls=config.science_max_llm_calls,
        interpretation_reserve_calls=config.science_interpretation_reserve_calls,
    )
    summary = RunLoopController(coordinator, selector).start(config)

    assert summary.stop_reason == "provider_connection_error"
    with sqlite3.connect(coordinator.database_path) as db:
        status, error_code, latency_ms, diagnostic_json, usage_json = db.execute(
            "SELECT status,error_code,latency_ms,diagnostic_json,usage_json "
            "FROM loop_scientific_api_calls WHERE run_id=?", (config.run_id,),
        ).fetchone()
    diagnostic = json.loads(diagnostic_json)
    assert status == "failed" and error_code == "provider_connection_error"
    assert diagnostic == {
        "failure_stage": "connection", "exception_class": "URLError",
        "cause_code": "dns_temporary_failure", "cause_class": "gaierror",
        "http_response_received": False, "http_status": None, "latency_ms": 17,
    }
    assert isinstance(latency_ms, int) and latency_ms >= 0
    assert usage_json == "{}"  # usage unreported, not zero tokens
    assert "connection failed" not in diagnostic_json
    assert oracle.call_count == 0
    artifact = _export_run(coordinator.database_path, config, summary)
    exported_call = json.loads((artifact / "api_calls.json").read_text())[0]
    assert exported_call["diagnostic"] == diagnostic
    assert "connection failed" not in json.dumps(exported_call)


@pytest.mark.parametrize("outcome", ["no_record", "failure"])
def test_no_record_and_failed_attempts_are_not_fabricated_as_inactive(tmp_path, outcome):
    _, public, base_oracle = _make_snapshot(
        tmp_path / "snapshot", cost="0.1", budget="5", empty_hidden=outcome == "no_record",
    )
    if outcome == "no_record":
        oracle = SpyOracle(base_oracle)
    else:
        class OneFailureOracle:
            def __init__(self, inner):
                self.inner = inner
                self.store = inner.store
                self.failed_candidate = None
                self.call_count = 0

            def lookup(self, candidate_id, assay_id):
                self.call_count += 1
                if self.failed_candidate is None:
                    self.failed_candidate = candidate_id
                    raise ReplayError("fixture_lookup_failure", "bounded failure fixture")
                return self.inner.lookup(candidate_id, assay_id)

        oracle = OneFailureOracle(base_oracle)
    clock = FixedClock(datetime(2025, 1, 2, tzinfo=timezone.utc))
    coordinator = _coordinator(tmp_path / "private", public, oracle, clock)
    settings, config = _science_config(
        coordinator, run_id=f"attempt-{outcome}", max_steps=2,
    )
    fake = FakeReasoningProvider()
    summary = _controller(coordinator, settings, fake).start(config)

    assert summary.selection_steps == 2
    assert len(fake.contexts) == (2 if outcome == "no_record" else 3)
    second = fake.contexts[1]
    assert second["newly_released_observation_ids"] == []
    first_attempt = second["public_attempts"][0]
    assert first_attempt["status"] == ("no_record" if outcome == "no_record" else "failed")
    assert all(item["assay_id"] != first_attempt["assay_id"] for item in second["public_observations"])
    assert fake.decisions[1]["interpretations"] == []
    assert first_attempt["candidate_id"] in {item["candidate_id"] for item in second["candidate_contexts"]}
    assert summary.stop_reason == "max_steps"
    first_action = fake.decisions[0]["action"]
    first_hypothesis_id = f"h-activity-{first_action['candidate_id']}-{first_action['assay_id']}"
    assert any(item["hypothesis_id"] == first_hypothesis_id
               and item["status"] == "proposed" for item in second["prior_hypotheses"])
    with sqlite3.connect(coordinator.database_path) as db:
        stored = json.loads(db.execute(
            "SELECT hypothesis_json FROM loop_scientific_hypotheses "
            "WHERE run_id=? AND hypothesis_id=?", (config.run_id, first_hypothesis_id),
        ).fetchone()[0])
        updates = db.execute(
            "SELECT COUNT(*) FROM loop_scientific_hypothesis_events "
            "WHERE run_id=? AND hypothesis_id=? AND event_type='updated'",
            (config.run_id, first_hypothesis_id),
        ).fetchone()[0]
    assert stored["status"] == "proposed"
    assert updates == 0  # a failed/no_record attempt is not activity evidence


def test_deadline_during_provider_call_discards_decision_and_stops(tmp_path):
    _, public, base_oracle = _make_snapshot(tmp_path / "snapshot", cost="0.1", budget="5")
    oracle = SpyOracle(base_oracle)
    clock = FixedClock(datetime(2025, 1, 2, tzinfo=timezone.utc))
    coordinator = _coordinator(tmp_path / "private", public, oracle, clock)
    settings, config = _science_config(
        coordinator, run_id="deadline-loop", max_steps=1, max_duration_seconds=10,
    )

    def expire_after_reply(_context, _call_no):
        clock.value += timedelta(seconds=10)

    fake = FakeReasoningProvider(before_response=expire_after_reply)
    summary = _controller(coordinator, settings, fake).start(config)

    assert summary.stop_reason == "deadline"
    assert summary.selection_steps == summary.unique_executions == 0
    assert oracle.call_count == 0
    with sqlite3.connect(coordinator.database_path) as db:
        decision = db.execute(
            "SELECT status,error_code FROM loop_scientific_decisions WHERE run_id=?", (config.run_id,),
        ).fetchone()
    assert decision == ("failed", "deadline")


def test_provider_timeout_shrinks_to_subsecond_remaining_run_deadline(tmp_path):
    _, public, base_oracle = _make_snapshot(tmp_path / "snapshot", cost="0.1", budget="5")
    oracle = SpyOracle(base_oracle)
    clock = FixedClock(datetime(2025, 1, 2, tzinfo=timezone.utc))
    coordinator = _coordinator(tmp_path / "private", public, oracle, clock)
    settings, config = _science_config(
        coordinator, run_id="subsecond-timeout-loop", max_steps=1, max_duration_seconds=10,
    )
    calls = 0

    def jump_near_deadline():
        nonlocal calls
        calls += 1
        # The selector sees the full budget when building context, then the
        # clock advances inside that step before it starts the network request.
        return time.monotonic() + (0.0 if calls == 1 else 9.25)

    bounded_timeouts = []
    fake = FakeReasoningProvider()
    selector = ScientificReasoningSelector(
        coordinator, settings, research_goal="Check bounded request timeout.",
        provider_factory=lambda bounded: (bounded_timeouts.append(bounded.timeout_seconds) or fake),
        max_llm_calls=config.science_max_llm_calls,
        interpretation_reserve_calls=config.science_interpretation_reserve_calls,
        monotonic=jump_near_deadline,
    )
    summary = RunLoopController(coordinator, selector).start(config)

    assert summary.stop_reason == "max_steps"
    assert summary.selection_steps == summary.released_executions == 1
    assert len(bounded_timeouts) == 2  # selection and final observation interpretation
    assert all(0 < timeout < 1 for timeout in bounded_timeouts)
