"""현재 실행 시점의 결과 수신과 검증 후 객체 교체 규약의 회귀 테스트."""
from datetime import timedelta, timezone

import pytest
from pydantic import ValidationError

from assaypilot.domain import (
    ActionRequest, BudgetState, ExecutionReceipt, ExecutionResult, ObservationBatch,
    RunState, validate_execution, validate_observation_batch, validate_public_campaign,
    validate_run_state,
)


def execution_at(public, released_at):
    """초기 로딩 이후 실행에서 공개되는 합성 완료 결과를 만든다."""
    action = ActionRequest(action_id="a", campaign_id=public.campaign.campaign_id,
                           candidate_id="c1", assay_id="screen")
    receipt = ExecutionReceipt(receipt_id="r", action_id="a", accepted_at=public.as_of)
    observation = public.observations[0].validated_replace(
        observation_id="later-observation", released_at=released_at)
    result = ExecutionResult(receipt_id="r", action_id="a", status="completed",
                             observations=[observation])
    return action, receipt, result


@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_execution_uses_current_time_and_blocks_future(public, offset):
    now = public.as_of + timedelta(days=1)
    action, receipt, result = execution_at(public, now + timedelta(seconds=offset))
    audit = validate_execution(action, receipt, result, public, as_of=now)
    assert audit.ok is (offset <= 0)
    if offset > 0:
        assert any(i.target_id == "later-observation" and i.field == "released_at"
                   and i.code == "not_public" for i in audit.issues)
    # 초기 데이터 로딩에는 계속 초기 스냅샷 시점을 적용한다.
    initial = public.validated_replace(observations=result.observations)
    assert any(i.code == "not_public" for i in validate_public_campaign(initial).issues)
    batch = ObservationBatch(campaign_id=public.campaign.campaign_id, observations=result.observations)
    assert any(i.code == "not_public" for i in validate_observation_batch(batch, public).issues)


def test_execution_time_compares_instants_across_offsets(public):
    now = public.as_of + timedelta(days=1)
    action, receipt, result = execution_at(public, now)
    assert validate_execution(action, receipt, result, public, as_of=now.astimezone(timezone.utc)).ok


def test_execution_requires_explicit_aware_current_time(public):
    action, receipt, result = execution_at(public, public.as_of)
    with pytest.raises(TypeError, match="as_of"):
        validate_execution(action, receipt, result, public)
    with pytest.raises(ValidationError):
        validate_execution(action, receipt, result, public, as_of=public.as_of.replace(tzinfo=None))


def test_execution_rejects_clock_before_snapshot_or_receipt(public):
    action, receipt, result = execution_at(public, public.as_of)
    audit = validate_execution(action, receipt, result, public, as_of=public.as_of - timedelta(seconds=1))
    assert any(i.field == "as_of" and i.code == "time_mismatch" for i in audit.issues)
    receipt = receipt.validated_replace(accepted_at=public.as_of + timedelta(seconds=1))
    audit = validate_execution(action, receipt, result, public, as_of=public.as_of)
    assert any(i.field == "accepted_at" and i.code == "time_mismatch" for i in audit.issues)


def test_failed_budget_replacement_preserves_original():
    budget = BudgetState(total="10", spent="6", reserved="4", unit="credits")
    original = budget
    before = budget.model_dump_json()
    with pytest.raises(ValidationError, match="exceeds total"):
        budget = budget.validated_replace(spent="7")
    assert budget is original
    assert budget.model_dump_json() == before
    # 여러 필드를 함께 검증하여 중간의 초과 상태를 만들지 않는다.
    updated = budget.validated_replace(spent="7", reserved="3")
    assert updated is not budget and updated.spent == 7 and updated.reserved == 3
    assert budget.model_dump_json() == before


def test_failed_execution_transition_preserves_original(public):
    result = ExecutionResult(receipt_id="r", action_id="a", status="pending")
    before = result.model_dump_json()
    with pytest.raises(ValidationError, match="completed requires"):
        result = result.validated_replace(status="completed")
    assert result.model_dump_json() == before
    updated = result.validated_replace(status="completed", observations=public.observations[:1])
    assert updated.status == "completed" and result.status == "pending"


def test_nested_failed_replacement_preserves_original(public):
    state = RunState(campaign_id=public.campaign.campaign_id, as_of=public.as_of,
                     observations=public.observations, budget=BudgetState(total="100", spent="0",
                     reserved="0", unit="synthetic_credit"), status="ready")
    before = state.model_dump_json()
    invalid_budget = {"total": "100", "spent": "90", "reserved": "20", "unit": "synthetic_credit"}
    with pytest.raises(ValidationError):
        state = state.validated_replace(budget=invalid_budget)
    assert state.model_dump_json() == before and invalid_budget["reserved"] == "20"
    # 단일 모델 검증이 통과해도 참조 검사에 실패하면 교체하지 않는다.
    proposed = state.validated_replace(observations=[
        state.observations[0].validated_replace(assay_id="missing")])
    audit = validate_run_state(proposed, public)
    if audit.ok:
        state = proposed
    assert not audit.ok and state.model_dump_json() == before


def test_replacement_detaches_nested_containers_and_revalidates_instances(public):
    before = public.model_dump_json()
    supplied = list(public.observations)
    updated = public.validated_replace(observations=supplied)
    # 중첩 객체 공유가 없는지 확인하기 위해 테스트에서 의도적으로 변경한다.
    updated.observations[0].evidence_ids.append("new-evidence")
    updated.observations.clear()
    assert public.model_dump_json() == before
    assert supplied[0].evidence_ids == ["e1"]
    corrupted = public.observations[0].model_copy(deep=True)
    corrupted.evidence_ids.clear()
    with pytest.raises(ValidationError):
        public.validated_replace(observations=[corrupted])
    assert public.model_dump_json() == before and corrupted.evidence_ids == []


def test_replacement_does_not_ignore_unknown_fields():
    budget = BudgetState(total="10", spent="0", reserved="0", unit="credits")
    with pytest.raises(ValidationError):
        budget.validated_replace(available="10")
