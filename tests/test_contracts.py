"""단일 객체 제약과 JSON 의미 보존에 대한 회귀 테스트."""
from datetime import datetime
from decimal import Decimal
import json

import pytest
from pydantic import ValidationError

from assaypilot.domain import (
    ActionRequest, ApprovedAction, BudgetState, Comparison, Cost, ExecutionError,
    ExecutionReceipt, ExecutionResult, GovernanceDecision, Observation, ObservationBatch,
    Prediction, PredictionBatch, Prerequisite, PublicCampaign, RunState, SuccessCondition, Verdict,
)


def prediction(**changes):
    """available 예측 기본 입력에 개별 경계값을 적용한다."""
    data = dict(candidate_id="c1", assay_id="screen", target_meaning="SYNTHETIC active probability",
                model_version="synthetic-v0", status="available", probability=0.2, calibrated=False)
    return Prediction(**(data | changes))


def test_round_trip_preserves_semantics(public):
    encoded = public.model_dump_json()
    restored = PublicCampaign.model_validate_json(encoded)
    assert restored == public
    assert isinstance(restored.campaign.budget.amount, Decimal)
    assert isinstance(restored.observations[0].verdict, Verdict)
    assert restored.observations[1].comparison is Comparison.LT
    assert restored.observations[0].released_at.utcoffset().total_seconds() == 9 * 3600
    assert restored.candidates[0].source_id == "example-1"
    assert json.loads(encoded)["campaign"]["budget"]["amount"] == "100.00"
    budget = BudgetState(total="100.00", spent="1.10", reserved="2.20", unit="credits")
    assert BudgetState.model_validate_json(budget.model_dump_json()).available == Decimal("96.70")
    assert "available" not in json.loads(budget.model_dump_json())
    batch = PredictionBatch(campaign_id="synthetic-alpha", predictions=[prediction(), prediction(
        candidate_id="c2", status="unavailable", reason="untrained", probability=None, calibrated=None)])
    assert PredictionBatch.model_validate_json(batch.model_dump_json()) == batch


def test_verdicts_are_not_missingness(public):
    assert {obs.verdict for obs in public.observations} == set(Verdict)
    numeric_only = public.observations[2]
    assert numeric_only.value == 42 and numeric_only.raw_verdict is None
    assert numeric_only.verdict is Verdict.UNSPECIFIED
    assert not any(o.assay_id == "interference" for o in public.observations)
    assert len({(o.replicate_id, o.condition_id) for o in public.observations
                if o.candidate_id == "c1" and o.assay_id == "screen"}) == 2
    success = {s.assay_id: s.verdict for s in public.campaign.success_conditions}
    assert success["confirmation"] is Verdict.ACTIVE
    assert success["interference"] is Verdict.INACTIVE


@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf"), float("-inf")])
def test_invalid_probability(value):
    with pytest.raises(ValidationError):
        prediction(probability=value)


@pytest.mark.parametrize("kind,value", [
    ("probability_stddev", -1), ("probability_stddev", 0.51),
    ("probability_variance", 0.26), ("entropy_nats", 0.70),
    ("entropy_nats", float("nan")), ("probability_stddev", float("inf")),
    ("unknown", 0.1), (None, 0.1), ("probability_stddev", None),
])
def test_invalid_uncertainty(kind, value):
    with pytest.raises(ValidationError):
        prediction(uncertainty_kind=kind, uncertainty=value)


@pytest.mark.parametrize("changes", [
    {"status": "unavailable"}, {"probability": None}, {"calibrated": None},
    {"status": "unavailable", "probability": None, "calibrated": None},
    {"status": "unavailable", "reason": "untrained", "probability": None, "calibrated": None, "uncertainty": 0.1},
])
def test_prediction_status_consistency(changes):
    with pytest.raises(ValidationError):
        prediction(**changes)


@pytest.mark.parametrize("amount", ["-1", "NaN", "Infinity", "-Infinity", 1.1, True, "bad"])
def test_invalid_money(amount):
    with pytest.raises(ValidationError):
        Cost(amount=amount, unit="credits", assumed=True)
    with pytest.raises(ValidationError):
        BudgetState(total="100", spent=amount, reserved="0", unit="credits")


def test_overcommitted_budget():
    with pytest.raises(ValidationError, match="exceeds total"):
        BudgetState(total="10", spent="6", reserved="4.01", unit="credits")


@pytest.mark.parametrize("parameters", [
    {"value": Decimal("1")}, {"value": float("nan")}, {"nested": [float("inf")]},
    {1: "bad key"}, {"value": (1, 2)}, {"value": {1, 2}}, {"value": datetime.now()},
])
def test_non_json_parameters(parameters):
    with pytest.raises(ValidationError):
        ActionRequest(action_id="a1", campaign_id="c", candidate_id="c1", assay_id="s", parameters=parameters)


def test_json_parameters_preserved():
    action = ActionRequest(action_id="a", campaign_id="c", candidate_id="x", assay_id="s",
                           parameters={"nested": [None, True, 2, 0.25, {"label": "synthetic"}]})
    assert ActionRequest.model_validate_json(action.model_dump_json()) == action


@pytest.mark.parametrize("changes", [
    {"released_at": "2026-01-01T00:00:00"}, {"observation_id": " "},
    {"candidate_id": ""}, {"value": float("nan")}, {"unit": None}, {"comparison": None},
])
def test_observation_shape(public, changes):
    data = public.observations[0].model_dump() | changes
    with pytest.raises(ValidationError):
        Observation.model_validate(data)


def test_absent_measurement_cannot_be_an_empty_observation(public):
    data = public.observations[0].model_dump() | dict(value=None, unit=None, comparison=None,
                                                   raw_verdict=None, verdict="unspecified")
    with pytest.raises(ValidationError):
        Observation.model_validate(data)


@pytest.mark.parametrize("kind,verdict", [("observed", "active"), ("verdict", None)])
def test_prerequisite_shape(kind, verdict):
    with pytest.raises(ValidationError):
        Prerequisite(assay_id="screen", kind=kind, verdict=verdict)


def test_success_condition_must_have_unambiguous_shape():
    with pytest.raises(ValidationError):
        SuccessCondition(assay_id="s", meaning="threshold", kind="numeric", value=3)
    with pytest.raises(ValidationError):
        SuccessCondition(assay_id="s", meaning="hit", kind="verdict", verdict="active", value=3)


@pytest.mark.parametrize("status,has_obs,has_error", [
    ("pending", True, False), ("pending", False, True),
    ("completed", False, False), ("completed", True, True),
    ("failed", False, False), ("failed", True, True),
])
def test_execution_status_rejects_invalid_payloads(public, status, has_obs, has_error):
    with pytest.raises(ValidationError):
        ExecutionResult(receipt_id="r", action_id="a", status=status,
                        observations=public.observations[:1] if has_obs else [],
                        error=ExecutionError(code="synthetic", message="example") if has_error else None)


@pytest.mark.parametrize("status", ["pending", "completed", "failed"])
def test_valid_execution_status_round_trip(public, status):
    result = ExecutionResult(receipt_id="r", action_id="a", status=status,
                             observations=public.observations[:1] if status == "completed" else [],
                             error=ExecutionError(code="synthetic", message="example") if status == "failed" else None)
    assert ExecutionResult.model_validate_json(result.model_dump_json()) == result
    if status == "pending":
        assert result.observations == []


def test_approval_cannot_be_embedded_in_proposal_or_mislinked(public):
    data = dict(action_id="a", campaign_id="synthetic-alpha", candidate_id="c1", assay_id="screen")
    with pytest.raises(ValidationError):
        ActionRequest(**data, approved=True)
    approved = ApprovedAction(action=ActionRequest(**data), approval_id="approval", reviewed_at=public.as_of, reason="synthetic")
    with pytest.raises(ValidationError):
        GovernanceDecision(action_id="other", decision="approved", reason="synthetic", approved_action=approved)
    with pytest.raises(ValidationError):
        GovernanceDecision(action_id="a", decision="rejected", reason="synthetic", approved_action=approved)
    with pytest.raises(ValidationError):
        GovernanceDecision(action_id="a", decision="approved", reason="synthetic")
    decision = GovernanceDecision(action_id="a", decision="approved", reason="synthetic", approved_action=approved)
    assert GovernanceDecision.model_validate_json(decision.model_dump_json()) == decision


@pytest.mark.parametrize("field", ["sealed_labels", "answer_file_path", "oracle"])
def test_public_extra_fields_forbidden(public, field):
    with pytest.raises(ValidationError):
        PublicCampaign.model_validate(public.model_dump() | {field: "private"})
    with pytest.raises(ValidationError):
        ObservationBatch(campaign_id="c", observations=[], **{field: "private"})
    with pytest.raises(ValidationError):
        RunState(campaign_id="c", as_of=public.as_of, budget=BudgetState(
            total="0", spent="0", reserved="0", unit="credits"), status="ready", **{field: "private"})


def test_nested_extra_version_and_naive_receipt_forbidden(public):
    data = public.model_dump()
    data["candidates"][0]["sealed_label"] = "active"
    with pytest.raises(ValidationError):
        PublicCampaign.model_validate(data)
    with pytest.raises(ValidationError):
        PublicCampaign.model_validate(public.model_dump() | {"schema_version": "1.0.0"})
    with pytest.raises(ValidationError):
        ExecutionReceipt(receipt_id="r", action_id="a", accepted_at="2026-01-01T00:00:00")
