"""객체 간 참조·순환·공개 시점·실행 연결의 검증."""
from datetime import timedelta
from pathlib import Path
import pytest
from assaypilot.domain import (
    ActionRequest, ApprovedAction, BudgetState, ExecutionReceipt, ExecutionResult, Hypothesis,
    ObservationBatch, Plan, Prediction, PredictionBatch, Prerequisite, PublicCampaign, Report,
    ReportStatement, RunState, validate_execution, validate_observation_batch, validate_predictions,
    validate_public_campaign, validate_report, validate_run_state,
)


def state_for(public):
    """카탈로그에 대응하는 공개 상태를 만든다."""
    return RunState(campaign_id=public.campaign.campaign_id, as_of=public.as_of,
        observations=public.observations, budget=BudgetState(total="100", spent="0", reserved="0",
        unit="synthetic_credit"), status="ready")


def test_valid_campaigns_and_purity(public):
    original = public.model_dump_json()
    assert validate_public_campaign(public).ok
    assert validate_run_state(state_for(public), public).ok
    assert public.model_dump_json() == original
    path = Path(__file__).resolve().parents[1] / "examples" / "synthetic_other_campaign.json"
    other = PublicCampaign.model_validate_json(path.read_text())
    assert other.campaign.target != public.campaign.target
    assert validate_public_campaign(other).ok
    other.campaign.success_conditions[0].unit = "nM"
    assert any(i.code == "unit_mismatch" for i in validate_public_campaign(other).issues)


@pytest.mark.parametrize("collection", ["candidates", "assays", "evidence", "observations"])
def test_duplicate_ids(public, collection):
    items = getattr(public, collection)
    items.append(items[0])
    assert any(i.code == "duplicate_id" for i in validate_public_campaign(public).issues)


@pytest.mark.parametrize("field", ["candidate_id", "assay_id", "evidence_ids"])
def test_missing_observation_references(public, field):
    setattr(public.observations[0], field, ["missing"] if field == "evidence_ids" else "missing")
    assert any(i.target_id == "o1" and i.field.startswith(field) and i.code == "unknown_reference"
               for i in validate_public_campaign(public).issues)


def test_duplicate_evidence_references(public):
    public.observations[0].evidence_ids.append("e1")
    assert any(i.code == "duplicate_reference" for i in validate_public_campaign(public).issues)


@pytest.mark.parametrize("cycle", ["self", "two", "three"])
def test_prerequisite_cycles(public, cycle):
    public.assays[0].prerequisites = [Prerequisite(assay_id="screen" if cycle == "self" else "confirmation", kind="observed")]
    if cycle == "three":
        public.assays[1].prerequisites = [Prerequisite(assay_id="interference", kind="observed")]
    assert any(i.code == "cycle" and i.field.startswith("prerequisites") for i in validate_public_campaign(public).issues)


def test_unknown_prerequisite_and_success_assay(public):
    public.assays[0].prerequisites = [Prerequisite(assay_id="missing", kind="observed")]
    public.campaign.success_conditions[0].assay_id = "absent"
    issues = validate_public_campaign(public).issues
    assert any(i.field.startswith("prerequisites") and i.code == "unknown_reference" for i in issues)
    assert any(i.field.startswith("success_conditions") and i.code == "unknown_reference" for i in issues)


def test_units_and_future_observation(public):
    public.assays[0].cost.unit = "USD"
    public.observations[0].unit = "uM"
    public.observations[1].released_at = public.as_of + timedelta(seconds=1)
    assert {(i.target_id, i.field, i.code) for i in validate_public_campaign(public).issues} >= {
        ("screen", "cost.unit", "unit_mismatch"), ("o1", "unit", "unit_mismatch"), ("o2", "released_at", "not_public")}


def test_state_references_budget_and_ids(public):
    state = state_for(public)
    hypothesis = Hypothesis(hypothesis_id="h", content="synthetic", supporting_evidence_ids=["absent"],
                            opposing_evidence_ids=["absent"], falsification_conditions=["synthetic"])
    plan = Plan(plan_id="p", hypothesis_ids=["absent"], evidence_ids=["absent"], next_question="synthetic",
                proposed_assay_ids=["absent"], rationale="synthetic")
    action = ApprovedAction(action=ActionRequest(action_id="a", campaign_id="absent", candidate_id="absent",
        assay_id="absent"), approval_id="approval", reviewed_at=public.as_of, reason="synthetic")
    state.hypotheses = [hypothesis, hypothesis]
    state.plans = [plan, plan]
    state.pending_actions = [action, action]
    state.budget.unit = "USD"
    state.budget.total = "99"
    issues = validate_run_state(state, public).issues
    assert {i.code for i in issues} >= {"unknown_reference", "duplicate_id", "unit_mismatch", "budget_mismatch"}
    assert {i.field for i in issues if i.code == "unknown_reference"} >= {
        "supporting_evidence_ids[0]", "opposing_evidence_ids[0]", "hypothesis_ids[0]",
        "evidence_ids[0]", "proposed_assay_ids[0]", "campaign_id", "candidate_id", "assay_id"}


def test_state_times_and_campaign(public):
    state = state_for(public)
    state.campaign_id = "missing"
    state.as_of = public.as_of - timedelta(days=1)
    assert {i.code for i in validate_run_state(state, public).issues} >= {"unknown_reference", "time_mismatch", "not_public"}


def test_prediction_batch_refs_and_duplicates(public):
    item = Prediction(candidate_id="missing", assay_id="missing", target_meaning="synthetic", model_version="untrained",
                      status="unavailable", reason="untrained")
    batch = PredictionBatch(campaign_id="missing", predictions=[item, item])
    assert {i.code for i in validate_predictions(batch, public).issues} == {"unknown_reference", "duplicate_id"}


def test_batch_and_report_refs(public):
    batch = ObservationBatch(campaign_id="missing", observations=public.observations)
    assert not validate_observation_batch(batch, public).ok
    report = Report(campaign_id=public.campaign.campaign_id,
                    statements=[ReportStatement(text="synthetic", evidence_ids=["missing"])])
    issue = validate_report(report, public).issues[0]
    assert issue.field == "statements[0].evidence_ids[0]" and issue.code == "unknown_reference"


def test_execution_links_and_observation_identity(public):
    action = ActionRequest(action_id="a", campaign_id=public.campaign.campaign_id, candidate_id="c1", assay_id="screen")
    receipt = ExecutionReceipt(receipt_id="r", action_id="a", accepted_at=public.observations[0].released_at)
    result = ExecutionResult(receipt_id="r", action_id="a", status="completed", observations=[public.observations[0]])
    assert validate_execution(action, receipt, result, public).ok
    result.receipt_id = "wrong-receipt"
    result.action_id = "wrong-action"
    receipt.action_id = "wrong-action"
    result.observations[0].candidate_id = "c2"
    result.observations[0].assay_id = "confirmation"
    issues = validate_execution(action, receipt, result, public).issues
    assert {i.field for i in issues if i.code == "link_mismatch"} == {"receipt_id", "action_id", "candidate_id", "assay_id"}
    result.observations[0].released_at = receipt.accepted_at - timedelta(seconds=1)
    assert any(i.code == "time_mismatch" for i in validate_execution(action, receipt, result, public).issues)
