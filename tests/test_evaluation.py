from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from assaypilot.evaluation import (
    EvaluationError,
    TruthSnapshot,
    _aggregate_group,
    _check_evidence,
    _common_endpoints,
    _run_metrics,
    calculate_science_metrics,
    canonical_json,
    classify_verdicts,
    sha256_file,
    sha256_bytes,
    validate_run_artifacts,
)
from assaypilot.domain import Verdict
from assaypilot.domain.catalog import AssaySpec, Cost, EvidenceRef
from assaypilot.domain.records import Observation


def test_known_positive_recall_uses_released_hits_only_and_unattempted_truth_only_changes_denominator():
    released = {("candidate-a", "followup"): "positive"}
    truth = {
        ("candidate-a", "followup"): "positive",
        ("candidate-b", "followup"): "positive",
        ("candidate-c", "followup"): "negative",
    }
    first = calculate_science_metrics(
        truth_labels=truth, released_labels=released, initially_public=set(), spent=Decimal("1"),
    )
    changed = dict(truth)
    changed[("candidate-c", "followup")] = "positive"
    second = calculate_science_metrics(
        truth_labels=changed, released_labels=released, initially_public=set(), spent=Decimal("1"),
    )
    assert first["new_followup_positive_count"] == 1
    assert first["known_positive_new_count"] == 2
    assert first["known_positive_recall"] == 0.5
    assert second["new_followup_positive_count"] == first["new_followup_positive_count"]
    assert second["newly_labeled_count"] == first["newly_labeled_count"]
    assert second["known_positive_new_count"] == 3
    assert second["known_positive_recall"] == pytest.approx(1 / 3)


def test_binary_fraction_excludes_unknown_and_no_record_and_deduplicates_assay_unit():
    truth = {
        ("candidate-a", "followup"): "positive",
        ("candidate-b", "followup"): "negative",
        ("candidate-c", "followup"): "missing",
        ("candidate-d", "followup"): "ambiguous",
    }
    released = {
        ("candidate-a", "followup"): "positive",
        ("candidate-b", "followup"): "negative",
        ("candidate-d", "followup"): "ambiguous",
    }
    result = calculate_science_metrics(
        truth_labels=truth, released_labels=released, initially_public=set(), spent=Decimal("2"),
    )
    assert result["new_followup_positive_count"] == 1
    assert result["newly_labeled_count"] == 2
    assert result["observed_positive_fraction"] == 0.5
    assert result["unknown_or_ambiguous_released_count"] == 1
    assert result["missing_count"] == 1


def test_initial_primary_is_not_a_followup_hit_and_previously_public_assay_is_excluded():
    released = {
        ("primary-active-candidate", "followup"): "positive",
        ("already-public-candidate", "followup"): "positive",
    }
    truth = dict(released)
    result = calculate_science_metrics(
        truth_labels=truth,
        released_labels=released,
        initially_public={("already-public-candidate", "followup")},
        spent=Decimal("1"),
    )
    assert result["new_followup_positive_count"] == 1
    assert result["newly_labeled_count"] == 1
    assert result["known_positive_new_count"] == 1
    assert result["known_positive_recall"] == 1


def test_repeated_active_observations_from_alias_retry_or_resume_are_one_candidate_assay_hit():
    metric, _ = _run_metrics(
        _summary(spent="1"), _trace([("released", "1"), ("no_record", "1"), ("released", "1")]),
        _archive([
            _archive_execution("execution-1", "candidate-a", ["active", "active"], 1),
            _archive_execution("execution-3", "candidate-c", ["active"], 3),
        ]),
        None, assay_id="followup",
    )
    assert metric["new_followup_positive_count"] == 2
    assert metric["newly_labeled_count"] == 2
    assert metric["unique_executions"] == 3


def test_unknown_conflict_zero_denominators_and_zero_spend_keep_null_semantics():
    assert classify_verdicts([]) == "missing"
    assert classify_verdicts(["inconclusive"]) == "unknown"
    assert classify_verdicts(["active", "inactive"]) == "ambiguous"
    result = calculate_science_metrics(
        truth_labels={("candidate-x", "followup"): "negative"},
        released_labels={("candidate-x", "followup"): "unknown"},
        initially_public=set(), spent=Decimal("0"),
    )
    assert result["new_followup_positive_count"] == 0
    assert result["newly_labeled_count"] == 0
    assert result["observed_positive_fraction"] is None
    assert result["observed_positive_fraction_null_reason"] == "no_new_binary_results"
    assert result["known_positive_recall"] is None
    assert result["known_positive_recall_null_reason"] == "no_known_new_positive"
    assert result["positives_per_credit"] is None
    assert result["positives_per_credit_null_reason"] == "zero_spent"


def test_archive_only_metrics_count_public_label_but_mark_truth_dependent_fields_unavailable():
    public_context, _, _ = _evidence_fixture()
    public_context = replace(public_context, labels={}, measurements={})
    metrics, _ = _run_metrics(
        _summary("1"), _trace([("released", "1")]),
        _archive([_archive_execution("execution-1", "candidate-1", ["active"], 1)]),
        public_context, assay_id="followup",
    )
    assert metrics["new_followup_positive_count"] == 1
    assert metrics["newly_labeled_count"] == 1
    assert metrics["known_positive_recall"] is None
    assert metrics["known_positive_recall_null_reason"] == "truth_unavailable"
    assert metrics["universe_size"] is None


def test_positive_denominators_are_assay_specific_and_never_join_by_cid():
    labels = {
        ("candidate-sid-1", "assay-a"): "positive",
        ("candidate-sid-2", "assay-b"): "positive",
    }
    result = calculate_science_metrics(
        truth_labels=labels, released_labels=labels, initially_public=set(), spent=Decimal("1"),
    )
    assert result["universe_size"] == 2
    assert result["known_positive_new_count"] == 2
    assert set(labels) == {("candidate-sid-1", "assay-a"), ("candidate-sid-2", "assay-b")}


def test_step_and_cost_curves_preserve_early_stop_repeated_cost_and_first_positive():
    trace = _trace([("no_record", "0"), ("released", "0"), ("released", "1")])
    archive = _archive([
        _archive_execution("execution-2", "candidate-b", ["inactive"], 2),
        _archive_execution("execution-3", "candidate-c", ["active"], 3),
    ])
    metrics, curve = _run_metrics(_summary("1"), trace, archive, None, assay_id="followup")
    assert metrics["durable_steps"] == 3
    assert metrics["new_followup_positive_count"] == 1
    assert metrics["first_positive_step"] == 3
    assert metrics["first_positive_spent"] == "1"
    assert [point["spent"] for point in curve["cost_points"]] == ["0", "0", "1"]
    assert [point["new_positive_count"] for point in curve["step_points"]] == [0, 0, 1]
    assert metrics["known_positive_recall"] is None
    assert metrics["known_positive_recall_null_reason"] == "truth_unavailable"


def test_common_axis_uses_actual_shared_endpoints_without_interpolation():
    rows = [{"run_id": "short"}, {"run_id": "long"}]
    curves = [
        {"run_id": "short", "step_points": [_point(1, "0", 0), _point(2, "1", 1)],
         "cost_points": [_point(1, "0", 0), _point(2, "1", 1)]},
        {"run_id": "long", "step_points": [_point(1, "0", 0), _point(2, "0", 0), _point(3, "2", 2)],
         "cost_points": [_point(1, "0", 0), _point(2, "0", 0), _point(3, "2", 2)]},
    ]
    common = _common_endpoints(rows, curves)
    assert common["common_step_range_end"] == 2
    assert common["common_spent_range_end"] == "1"
    assert common["by_run_at_or_before_common_spent_endpoint"]["long"]["step"] == 2


def test_seed_aggregate_uses_sample_sd_and_keeps_zero_positive_runs():
    result = _aggregate_group([
        {"new_followup_positive_count": 0, "observed_positive_fraction": None},
        {"new_followup_positive_count": 1, "observed_positive_fraction": 1.0},
    ])
    assert result["n_runs"] == 2
    assert result["new_followup_positive_count"]["n"] == 2
    assert result["new_followup_positive_count"]["mean"] == 0.5
    assert result["new_followup_positive_count"]["sample_sd"] == pytest.approx(2 ** -0.5)
    assert result["observed_positive_fraction"]["n"] == 1


def test_missing_evidence_reference_and_tampered_hash_fail_archive_validation():
    truth, execution, trace_step = _evidence_fixture()
    execution["evidence"] = []
    with pytest.raises(EvaluationError, match="missing archived EvidenceRef"):
        _check_evidence(execution, trace_step, truth)

    truth, execution, trace_step = _evidence_fixture()
    execution["evidence"][0]["sha256"] = "0" * 64
    with pytest.raises(EvaluationError, match="payload hash mismatch"):
        _check_evidence(execution, trace_step, truth)


def test_public_receipt_and_result_are_checked_against_execution_identity():
    truth, execution, trace_step = _evidence_fixture()
    execution["receipt"]["receipt_id"] = "receipt-other-execution"
    with pytest.raises(EvaluationError, match="receipt/result identity"):
        _check_evidence(execution, trace_step, truth)


def test_evidence_sid_and_source_row_must_match_validated_candidate_mapping():
    truth, execution, trace_step = _evidence_fixture()
    execution["evidence"][0]["payload"]["sid"] = 999
    execution["evidence"][0]["sha256"] = sha256_bytes(canonical_json(execution["evidence"][0]["payload"]))
    with pytest.raises(EvaluationError, match="SID/AID"):
        _check_evidence(execution, trace_step, truth)


def test_complete_run_artifact_chain_validates_and_archive_tampering_is_rejected(tmp_path, monkeypatch):
    import assaypilot.evaluation as evaluation

    monkeypatch.setattr(evaluation, "ROOT", tmp_path)
    baseline_root = tmp_path / "reports/stage3/baselines/fixture"
    run_dir = baseline_root / "runs/revision-fixture/run-1"
    run_dir.mkdir(parents=True)
    truth, execution, trace_step = _evidence_fixture()
    archive_path = run_dir / "published_results.json"
    trace_path = run_dir / "trace.json"
    summary_path = run_dir / "summary.json"

    plan_sha = "a" * 64
    selector = {"kind": "fixed_order", "seed": None, "algorithm_version": None}
    observation = execution["observations"][0]
    evidence = execution["evidence"][0]
    trace_step.update({
        "status": "released",
        "published_observations": [{
            "observation_id": observation["observation_id"],
            "evidence": [{"evidence_id": evidence["reference"]["evidence_id"], "sha256": evidence["sha256"]}],
        }],
        "budget_after_step": {"spent": "1", "reserved": "0", "available": "4", "unit": "synthetic_credit"},
        "request_id": "request-1",
    })
    trace = {
        "run_id": "run-1", "snapshot_revision": "revision-fixture", "selector": selector,
        "stop_reason": "max_steps", "budget_current": {
            "spent": "1", "reserved": "0", "available": "4", "unit": "synthetic_credit",
        },
        "steps": [trace_step],
    }
    archive = {
        "schema_version": evaluation.ARCHIVE_SCHEMA, "plan_sha256": plan_sha,
        "run_id": "run-1", "snapshot_revision": "revision-fixture", "baseline_id": "fixture",
        "executions": [execution],
    }
    archive_path.write_text(json.dumps(archive), encoding="utf-8")
    trace_path.write_text(json.dumps(trace), encoding="utf-8")
    config = {
        "selector_kind": "fixed_order", "max_steps": 1,
        "max_duration_seconds": 300, "max_action_retries": 1, "max_release_retries": 2,
        "selector_timeout_seconds": 5.0, "cost_policy_version": "preserved-public-assay-cost-v1",
        "approval_policy": "bounded_replay",
        "initial_budget": {"amount": "5", "unit": "synthetic_credit", "assumed": True},
    }
    run_summary = {
        "selection_steps": 1, "unique_executions": 1,
        "released_executions": 1, "no_record_executions": 0,
        "failed_executions": 0, "cancelled_executions": 0,
        "observations_added": 1, "spent": "1", "reserved": "0", "available": "4",
        "unit": "synthetic_credit",
    }
    summary = {
        "plan_sha256": plan_sha, "baseline_id": "fixture", "snapshot_revision": "revision-fixture",
        "run_id": "run-1", "selector": selector, "public_tree_sha256": "public-hash",
        "run_config": config, "summary": run_summary,
        "published_results_path": archive_path.relative_to(tmp_path).as_posix(),
        "published_results_sha256": sha256_file(archive_path),
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    plan = {
        "baseline_id": "fixture", "fixed_conditions": {
            "initial_budget": "5", "budget_unit": "synthetic_credit", "budget_assumed": True,
            "cost_policy_version": "preserved-public-assay-cost-v1", "approval_policy": "bounded_replay",
            "max_duration_seconds": 300, "max_action_retries": 1,
            "max_release_retries": 2, "selector_timeout_seconds": 5.0,
        },
        "snapshots": [{"revision": "revision-fixture", "public_tree_sha256": "public-hash", "runs": [{
            "run_id": "run-1", "role": "baseline", "selector_kind": "fixed_order", "seed": None,
            "algorithm_version": None, "max_steps": 1,
        }]}],
    }
    run_row = {
        "revision": "revision-fixture", "run_id": "run-1", "durable_steps": 1,
        "executions": 1, "released": 1, "no_record": 0, "failed": 0, "cancelled": 0,
        "observations_added": 1, "spent": "1", "reserved": "0", "available": "4",
        "unit": "synthetic_credit", "summary_path": summary_path.relative_to(tmp_path).as_posix(),
        "trace_path": trace_path.relative_to(tmp_path).as_posix(),
        "published_results_path": archive_path.relative_to(tmp_path).as_posix(),
    }
    assert validate_run_artifacts(
        baseline_root=baseline_root, plan=plan, plan_sha256=plan_sha,
        run_row=run_row, truth=truth,
    )[2]["run_id"] == "run-1"

    archive["executions"][0]["observations"][0]["raw_verdict"] = "Inactive"
    archive_path.write_text(json.dumps(archive), encoding="utf-8")
    with pytest.raises(EvaluationError, match="archive file hash mismatch"):
        validate_run_artifacts(
            baseline_root=baseline_root, plan=plan, plan_sha256=plan_sha,
            run_row=run_row, truth=truth,
        )


def _summary(spent: str) -> dict:
    return {
        "run_config": {"initial_budget": {"amount": "5", "unit": "synthetic_credit", "assumed": True}},
        "summary": {"unique_executions": 3, "observations_added": 2},
    }


def _trace(status_cost_pairs: list[tuple[str, str]]) -> dict:
    steps = []
    for index, (status, spent) in enumerate(status_cost_pairs, start=1):
        steps.append({
            "step_no": index, "status": status,
            "execution_id": None if status == "rejected" else f"execution-{index}",
            "budget_after_step": {"spent": spent, "reserved": "0", "available": str(Decimal("5") - Decimal(spent))},
        })
    return {"run_id": "fixture-run", "snapshot_revision": "fixture", "selector": {"kind": "fixed_order", "seed": None, "algorithm_version": None},
            "stop_reason": "max_steps", "budget_current": {"spent": status_cost_pairs[-1][1], "reserved": "0", "available": str(Decimal("5") - Decimal(status_cost_pairs[-1][1])), "unit": "synthetic_credit"},
            "steps": steps}


def _archive_execution(execution_id: str, candidate_id: str, verdicts: list[str], step: int) -> dict:
    return {"execution_id": execution_id, "candidate_id": candidate_id, "assay_id": "followup", "step_no": step,
            "observations": [{"verdict": verdict} for verdict in verdicts]}


def _archive(executions: list[dict]) -> dict:
    return {"executions": executions}


def _point(step: int, spent: str, positives: int) -> dict:
    return {"step": step, "spent": spent, "new_positive_count": positives, "new_binary_count": positives}


def _evidence_fixture():
    now = datetime.now(timezone.utc)
    candidate_id = "candidate-1"
    evidence_id = "evidence-1"
    source_row_id = "source-row-1"
    measurement_id = "measurement-1"
    observation = Observation(
        observation_id="observation-1", candidate_id=candidate_id, assay_id="followup",
        raw_verdict="Active", verdict=Verdict.ACTIVE, evidence_ids=[evidence_id], released_at=now,
        replicate_id="rep-1", condition_id="cond-1",
    ).model_dump(mode="json")
    payload = {
        "schema_version": "1.0.0", "evidence_id": evidence_id, "measurement_id": measurement_id,
        "source_row_id": source_row_id, "candidate_id": candidate_id, "sid": 17, "aid": 27,
        "cid": 99, "raw_outcome": "Active", "raw_row": {"SID": "17", "AID": "27", "CID": "99", "Activity Outcome": "Active"},
    }
    evidence = {
        "reference": EvidenceRef(evidence_id=evidence_id, source_kind="pubchem_runtime_measurement",
                                 source_id=source_row_id, location="runtime/evidence-1.json").model_dump(mode="json"),
        "sha256": sha256_bytes(canonical_json(payload)), "payload": payload,
    }
    measurement = SimpleNamespace(measurement_id=measurement_id, source_row_id=source_row_id, sid=17, aid=27,
                                  raw_verdict="Active", verdict=Verdict.ACTIVE)
    public = SimpleNamespace(assays=[AssaySpec(
        assay_id="followup", name="fixture", role="confirmatory", endpoint="Activity Outcome", unit="categorical",
        verdict_meaning={Verdict.ACTIVE: "positive", Verdict.INACTIVE: "negative"}, cost=Cost(amount=1, unit="credit", assumed=True),
    )])
    truth = TruthSnapshot(
        assay_id="followup", public=public, oracle=None, sid_by_candidate={candidate_id: 17}, assay_aid=27,
        eligible_units=frozenset({(candidate_id, "followup")}), labels={(candidate_id, "followup"): "positive"},
        measurements={(candidate_id, "followup"): (measurement,)}, initially_public=frozenset(), initial_public_labels={},
    )
    execution = {
        "step_no": 1, "action_id": "action-1", "candidate_id": candidate_id, "assay_id": "followup",
        "execution_id": "execution-1",
        "receipt": {
            "schema_version": "0.1.0", "receipt_id": "receipt-execution-1", "action_id": "action-1",
            "accepted_at": now.isoformat(),
        },
        "result": {
            "schema_version": "0.1.0", "receipt_id": "receipt-execution-1", "action_id": "action-1",
            "status": "completed", "observations": [observation], "error": None,
        },
        "observations": [observation],
        "evidence": [evidence],
    }
    trace_step = {"step_no": 1, "action_id": "action-1", "candidate_id": candidate_id, "assay_id": "followup", "execution_id": "execution-1"}
    return truth, execution, trace_step
