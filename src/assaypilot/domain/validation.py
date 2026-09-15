"""여러 객체 사이의 정합성 검사. 입력을 변경하거나 규칙을 실행하지 않는다."""
from collections.abc import Iterable
from typing import Any

from .common import Contract
from .containers import ObservationBatch, PublicCampaign, RunState
from .exchange import (
    AuditIssue, AuditResult, ExecutionReceipt, ExecutionResult, PredictionBatch, Report,
)
from .records import ActionRequest, Observation


class _Checks:
    def __init__(self) -> None:
        self.issues: list[AuditIssue] = []

    def issue(self, target: str, field: str, code: str, reason: str) -> None:
        self.issues.append(AuditIssue(target_id=target, field=field, code=code, reason=reason))

    def unique(self, objects: Iterable[Any], key: str, field: str, owner: str) -> set[str]:
        seen: set[str] = set()
        for obj in objects:
            ident = getattr(obj, key)
            if ident in seen:
                self.issue(ident, field, "duplicate_id", f"duplicate {key} in {owner}")
            seen.add(ident)
        return seen

    def ref(self, value: str, known: set[str], target: str, field: str) -> None:
        if value not in known:
            self.issue(target, field, "unknown_reference", f"unknown ID: {value}")

    def refs(self, obj: Contract, field: str, known: set[str], target: str) -> None:
        seen: set[str] = set()
        for i, ident in enumerate(getattr(obj, field)):
            path = f"{field}[{i}]"
            self.ref(ident, known, target, path)
            if ident in seen:
                self.issue(target, path, "duplicate_reference", f"repeated ID: {ident}")
            seen.add(ident)

    def observations(self, observations: list[Observation], public: PublicCampaign, as_of: Any) -> None:
        self.unique(observations, "observation_id", "observation_id", public.campaign.campaign_id)
        candidates = {c.candidate_id for c in public.candidates}
        assays = {a.assay_id: a for a in public.assays}
        evidence = {e.evidence_id for e in public.evidence}
        for obs in observations:
            target = obs.observation_id
            self.ref(obs.candidate_id, candidates, target, "candidate_id")
            self.ref(obs.assay_id, set(assays), target, "assay_id")
            self.refs(obs, "evidence_ids", evidence, target)
            if obs.unit is not None and obs.assay_id in assays and obs.unit != assays[obs.assay_id].unit:
                self.issue(target, "unit", "unit_mismatch", "observation unit differs from assay unit")
            if obs.released_at > as_of:
                self.issue(target, "released_at", "not_public", "release is later than public snapshot")

    def action(self, action: ActionRequest, public: PublicCampaign) -> None:
        self.ref(action.campaign_id, {public.campaign.campaign_id}, action.action_id, "campaign_id")
        self.ref(action.candidate_id, {c.candidate_id for c in public.candidates}, action.action_id, "candidate_id")
        self.ref(action.assay_id, {a.assay_id for a in public.assays}, action.action_id, "assay_id")

    def result(self) -> AuditResult:
        return AuditResult(issues=self.issues)


def validate_public_campaign(public: PublicCampaign) -> AuditResult:
    """후보·시험·근거·실측 참조, 비용 단위 및 선행조건 순환을 검사한다."""
    check = _Checks()
    cid = public.campaign.campaign_id
    check.unique(public.candidates, "candidate_id", "candidates", cid)
    assays = check.unique(public.assays, "assay_id", "assays", cid)
    check.unique(public.evidence, "evidence_id", "evidence", cid)
    graph = {a.assay_id: [p.assay_id for p in a.prerequisites] for a in public.assays}
    for assay in public.assays:
        if assay.cost.unit != public.campaign.budget.unit:
            check.issue(assay.assay_id, "cost.unit", "unit_mismatch", "cost and campaign budget units differ")
        for i, prereq in enumerate(assay.prerequisites):
            check.ref(prereq.assay_id, assays, assay.assay_id, f"prerequisites[{i}].assay_id")
    # Iterative DFS also handles long dependency chains without recursion limits.
    color: dict[str, int] = {}
    for root in graph:
        if color.get(root, 0):
            continue
        color[root] = 1
        stack = [(root, iter(enumerate(graph[root])))]
        while stack:
            node, edges = stack[-1]
            edge = next(edges, None)
            if edge is None:
                color[node] = 2
                stack.pop()
                continue
            i, neighbor = edge
            if neighbor not in graph:
                continue
            if color.get(neighbor) == 1:
                check.issue(node, f"prerequisites[{i}].assay_id", "cycle", f"dependency cycle through {neighbor}")
            elif color.get(neighbor, 0) == 0:
                color[neighbor] = 1
                stack.append((neighbor, iter(enumerate(graph[neighbor]))))
    assay_map = {a.assay_id: a for a in public.assays}
    for i, condition in enumerate(public.campaign.success_conditions):
        check.ref(condition.assay_id, assays, cid, f"success_conditions[{i}].assay_id")
        if condition.unit is not None and condition.assay_id in assay_map and condition.unit != assay_map[condition.assay_id].unit:
            check.issue(cid, f"success_conditions[{i}].unit", "unit_mismatch", "condition and assay units differ")
    check.observations(public.observations, public, public.as_of)
    return check.result()


def validate_run_state(state: RunState, public: PublicCampaign) -> AuditResult:
    """외부 공개 카탈로그를 기준으로 현재 상태의 참조·시점·예산을 검사한다."""
    check = _Checks()
    cid = state.campaign_id
    check.ref(cid, {public.campaign.campaign_id}, cid, "campaign_id")
    if state.as_of < public.as_of:
        check.issue(cid, "as_of", "time_mismatch", "state predates public catalog snapshot")
    check.observations(state.observations, public, state.as_of)
    hypotheses = check.unique(state.hypotheses, "hypothesis_id", "hypotheses", cid)
    check.unique(state.plans, "plan_id", "plans", cid)
    check.unique((a.action for a in state.pending_actions), "action_id", "pending_actions", cid)
    check.unique(state.pending_actions, "approval_id", "pending_actions.approval_id", cid)
    evidence = {e.evidence_id for e in public.evidence}
    assays = {a.assay_id for a in public.assays}
    for hypothesis in state.hypotheses:
        check.refs(hypothesis, "supporting_evidence_ids", evidence, hypothesis.hypothesis_id)
        check.refs(hypothesis, "opposing_evidence_ids", evidence, hypothesis.hypothesis_id)
    for plan in state.plans:
        check.refs(plan, "hypothesis_ids", hypotheses, plan.plan_id)
        check.refs(plan, "evidence_ids", evidence, plan.plan_id)
        check.refs(plan, "proposed_assay_ids", assays, plan.plan_id)
    for approved in state.pending_actions:
        check.action(approved.action, public)
        if approved.reviewed_at > state.as_of:
            check.issue(approved.approval_id, "reviewed_at", "time_mismatch", "approval is later than state")
    if state.budget.unit != public.campaign.budget.unit:
        check.issue(cid, "budget.unit", "unit_mismatch", "state and campaign budget units differ")
    if state.budget.total != public.campaign.budget.amount:
        check.issue(cid, "budget.total", "budget_mismatch", "state total differs from configured campaign budget")
    return check.result()


def validate_observation_batch(batch: ObservationBatch, public: PublicCampaign) -> AuditResult:
    """공개 스냅샷 시점에 전달 가능한 실측 묶음과 카탈로그 참조를 검사한다."""
    check = _Checks()
    check.ref(batch.campaign_id, {public.campaign.campaign_id}, batch.campaign_id, "campaign_id")
    check.observations(batch.observations, public, public.as_of)
    return check.result()


def validate_predictions(batch: PredictionBatch, public: PublicCampaign) -> AuditResult:
    """예측의 캠페인·후보·시험 참조 및 후보×시험 키 중복을 검사한다."""
    check = _Checks()
    check.ref(batch.campaign_id, {public.campaign.campaign_id}, batch.campaign_id, "campaign_id")
    seen: set[tuple[str, str]] = set()
    for i, prediction in enumerate(batch.predictions):
        check.ref(prediction.candidate_id, {c.candidate_id for c in public.candidates}, batch.campaign_id, f"predictions[{i}].candidate_id")
        check.ref(prediction.assay_id, {a.assay_id for a in public.assays}, batch.campaign_id, f"predictions[{i}].assay_id")
        key = (prediction.candidate_id, prediction.assay_id)
        if key in seen:
            check.issue(batch.campaign_id, f"predictions[{i}]", "duplicate_id", f"duplicate prediction key: {key}")
        seen.add(key)
    return check.result()


def validate_execution(action: ActionRequest, receipt: ExecutionReceipt, result: ExecutionResult, public: PublicCampaign) -> AuditResult:
    """단일 행동→접수→결과→후보·시험·관측의 연결을 검사한다."""
    check = _Checks()
    check.action(action, public)
    if receipt.action_id != action.action_id:
        check.issue(receipt.receipt_id, "action_id", "link_mismatch", "receipt does not refer to action")
    if result.receipt_id != receipt.receipt_id:
        check.issue(result.receipt_id, "receipt_id", "link_mismatch", "result does not refer to receipt")
    if result.action_id != action.action_id:
        check.issue(result.receipt_id, "action_id", "link_mismatch", "result does not refer to action")
    check.observations(result.observations, public, public.as_of)
    for obs in result.observations:
        if obs.candidate_id != action.candidate_id:
            check.issue(obs.observation_id, "candidate_id", "link_mismatch", "observation differs from action candidate")
        if obs.assay_id != action.assay_id:
            check.issue(obs.observation_id, "assay_id", "link_mismatch", "observation differs from action assay")
        if obs.released_at < receipt.accepted_at:
            check.issue(obs.observation_id, "released_at", "time_mismatch", "observation predates receipt")
    return check.result()


def validate_report(report: Report, public: PublicCampaign) -> AuditResult:
    """보고 문장이 같은 캠페인의 공개 근거만 참조하는지 검사한다."""
    check = _Checks()
    check.ref(report.campaign_id, {public.campaign.campaign_id}, report.campaign_id, "campaign_id")
    evidence = {e.evidence_id for e in public.evidence}
    for i, statement in enumerate(report.statements):
        before = len(check.issues)
        check.refs(statement, "evidence_ids", evidence, report.campaign_id)
        for issue in check.issues[before:]:
            issue.field = f"statements[{i}].{issue.field}"
    return check.result()
