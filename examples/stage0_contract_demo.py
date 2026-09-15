"""SYNTHETIC 데이터 로딩·객체 생성·참조 검사·JSON 왕복만 수행하는 독립 예제."""
from pathlib import Path

from assaypilot.domain import (
    ActionRequest, ApprovedAction, BudgetState, ExecutionReceipt, ExecutionResult,
    Hypothesis, ObservationBatch, Plan, Prediction, PredictionBatch, PublicCampaign,
    Report, ReportStatement, RunState, validate_execution, validate_observation_batch,
    validate_predictions, validate_public_campaign, validate_report, validate_run_state,
)


def main() -> None:
    """두 합성 캠페인과 교환 객체의 계약을 검증하고 요약을 출력한다."""
    folder = Path(__file__).resolve().parent
    for name in ("synthetic_campaign.json", "synthetic_other_campaign.json"):
        public = PublicCampaign.model_validate_json((folder / name).read_text())
        assert validate_public_campaign(public).ok
        assert PublicCampaign.model_validate_json(public.model_dump_json()) == public
        print(f"SYNTHETIC {public.campaign.campaign_id}: {len(public.candidates)} candidates, "
              f"{len(public.assays)} assays, {len(public.observations)} observations; references + JSON OK")
    public = PublicCampaign.model_validate_json((folder / "synthetic_campaign.json").read_text())
    hypothesis = Hypothesis(hypothesis_id="h1", content="SYNTHETIC: reporter 효과가 재현될 수 있다",
                            supporting_evidence_ids=["e1"], falsification_conditions=["확인시험에서 효과 미재현"])
    plan = Plan(plan_id="p1", hypothesis_ids=["h1"], evidence_ids=["e1"],
                next_question="SYNTHETIC: 간섭이 관측되는가?", proposed_assay_ids=["interference"],
                rationale="SYNTHETIC 구분 질문의 예시")
    action = ActionRequest(action_id="a1", campaign_id=public.campaign.campaign_id,
                           candidate_id="c1", assay_id="interference", parameters={"synthetic": True})
    # 승인·접수는 실제 Governor/Executor 실행이 아닌 명시적인 합성 객체다.
    approved = ApprovedAction(action=action, approval_id="synthetic-approval", reviewed_at=public.as_of,
                              reason="SYNTHETIC 계약 검사용 승인 메타데이터")
    state = RunState(campaign_id=public.campaign.campaign_id, as_of=public.as_of,
                     observations=public.observations, hypotheses=[hypothesis], plans=[plan],
                     pending_actions=[approved], budget=BudgetState(total="100.00", spent="0", reserved="0",
                     unit="synthetic_credit"), status="ready")
    batch = PredictionBatch(campaign_id=state.campaign_id, predictions=[
        Prediction(candidate_id="c1", assay_id="interference", target_meaning="SYNTHETIC 간섭 active 확률",
                   model_version="synthetic-hand-authored-v0", status="available", probability=0.2,
                   uncertainty_kind="probability_stddev", uncertainty=0.1, calibrated=False),
        Prediction(candidate_id="c2", assay_id="interference", target_meaning="SYNTHETIC 간섭 active 확률",
                   model_version="untrained", status="unavailable", reason="학습 전 계약 예제"),
    ])
    receipt = ExecutionReceipt(receipt_id="synthetic-receipt", action_id="a1", accepted_at=public.as_of)
    result = ExecutionResult(receipt_id=receipt.receipt_id, action_id="a1", status="pending")
    observations = ObservationBatch(campaign_id=state.campaign_id, observations=state.observations)
    report = Report(campaign_id=state.campaign_id, statements=[ReportStatement(
        text="SYNTHETIC 일차 hit는 직접 결합이나 치료 효능의 증거가 아니다", evidence_ids=["e1"])])
    for audit in (validate_run_state(state, public), validate_predictions(batch, public),
                  validate_execution(action, receipt, result, public),
                  validate_observation_batch(observations, public), validate_report(report, public)):
        assert audit.ok, audit.model_dump_json()
    for obj in (state, batch, receipt, result, observations, report):
        assert type(obj).model_validate_json(obj.model_dump_json()) == obj
    print("SYNTHETIC exchange contracts: references + JSON OK; available budget =", state.budget.available)
    print("Stage 0 only: no training, budget mutation, Oracle execution, or biological validation performed.")


if __name__ == "__main__":
    main()
