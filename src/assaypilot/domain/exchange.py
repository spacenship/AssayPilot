"""예측·예산·승인·비동기 실행·감사·보고의 교환 타입."""
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from .common import Contract, Envelope, ID, Money, Nonnegative, Probability, Text
from .records import ActionRequest, Observation


class Prediction(Contract):
    """후보·시험의 명시한 사건 확률. unavailable에는 예측 수치를 넣지 않는다."""
    candidate_id: ID
    assay_id: ID
    target_meaning: Text
    model_version: Text
    status: Literal["available", "unavailable"]
    reason: Text | None = None
    probability: Probability | None = None
    uncertainty_kind: Literal["probability_stddev", "probability_variance", "entropy_nats"] | None = None
    uncertainty: Nonnegative | None = None
    calibrated: bool | None = None

    @model_validator(mode="after")
    def check_status(self) -> "Prediction":
        """상태와 수치의 대응 및 Bernoulli 불확실성 지표의 범위를 검사한다."""
        if self.status == "unavailable":
            if self.reason is None or any(x is not None for x in (
                self.probability, self.uncertainty_kind, self.uncertainty, self.calibrated
            )):
                raise ValueError("unavailable requires reason and no prediction metrics")
        elif self.probability is None or self.calibrated is None:
            raise ValueError("available requires probability and calibrated")
        if (self.uncertainty_kind is None) != (self.uncertainty is None):
            raise ValueError("uncertainty requires kind and value together")
        limits = {"probability_stddev": 0.5, "probability_variance": 0.25, "entropy_nats": 0.6931471805599453}
        if self.uncertainty_kind is not None and self.uncertainty > limits[self.uncertainty_kind]:
            raise ValueError("uncertainty exceeds declared Bernoulli metric range")
        return self


class PredictionBatch(Envelope):
    """한 캠페인의 예측 묶음. 모델 학습 전에는 빈 값 대신 unavailable 사용."""
    campaign_id: ID
    predictions: list[Prediction]


class BudgetState(Contract):
    """총액·지출·예약만 저장하며 가용액은 Decimal 계산 속성으로 제공한다."""
    total: Money
    spent: Money
    reserved: Money
    unit: Text

    @model_validator(mode="after")
    def check_balance(self) -> "BudgetState":
        """총예산을 초과한 지출 및 예약을 거절한다."""
        if self.spent + self.reserved > self.total:
            raise ValueError("spent + reserved exceeds total")
        return self

    @property
    def available(self) -> Decimal:
        """총액에서 지출과 예약을 뺀 값. JSON에는 중복 저장하지 않는다."""
        return self.total - self.spent - self.reserved


class ApprovedAction(Contract):
    """Governor의 승인 메타데이터와 연결된 행동. 예약·차감의 증명은 아니다."""
    action: ActionRequest
    approval_id: ID
    reviewed_at: AwareDatetime
    reason: Text


class GovernanceDecision(Contract):
    """단일 행동에 대한 승인 또는 사유가 있는 거절."""
    action_id: ID
    decision: Literal["approved", "rejected"]
    reason: Text
    approved_action: ApprovedAction | None = None

    @model_validator(mode="after")
    def check_decision(self) -> "GovernanceDecision":
        """승인 결과에만 일치하는 행동과 승인 정보를 요구한다."""
        if (self.decision == "approved") != (self.approved_action is not None):
            raise ValueError("approved_action required only for approval")
        if self.approved_action and self.approved_action.action.action_id != self.action_id:
            raise ValueError("approved_action.action.action_id mismatch")
        return self


class ExecutionReceipt(Envelope):
    """승인 행동 하나의 접수 식별자. 상태는 ExecutionResult에서 조회한다."""
    receipt_id: ID
    action_id: ID
    accepted_at: AwareDatetime


class ExecutionError(Contract):
    """실패 원인과 재시도 가능성에 대한 구조적 정보."""
    code: Text
    message: Text
    retryable: bool = False


class ExecutionResult(Envelope):
    """접수의 pending/completed/failed 스냅샷. 대기는 음성 판정이 아니다."""
    receipt_id: ID
    action_id: ID
    status: Literal["pending", "completed", "failed"]
    observations: list[Observation] = Field(default_factory=list)
    error: ExecutionError | None = None

    @model_validator(mode="after")
    def check_result(self) -> "ExecutionResult":
        """완료는 관측, 실패는 오류만 요구하며 대기에는 둘 다 허용하지 않는다."""
        if self.status == "completed":
            if not self.observations or self.error is not None:
                raise ValueError("completed requires observations and no error")
        elif self.status == "failed":
            if self.error is None or self.observations:
                raise ValueError("failed requires error and no observations")
        elif self.observations or self.error is not None:
            raise ValueError("pending cannot contain observations or error")
        return self


class AuditIssue(Contract):
    """검사 대상과 필드 경로, 원인 코드를 가진 정합성 문제."""
    target_id: ID
    field: Text
    code: Text
    reason: Text


class AuditResult(Envelope):
    """순수 정합성 검사 결과. 생물학적 타당성을 보증하지 않는다."""
    issues: list[AuditIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """보고된 정합성 문제가 없는지 반환한다."""
        return not self.issues


class ReportStatement(Contract):
    """공개 근거 ID와 연결된 보고 문장."""
    text: Text
    evidence_ids: list[ID] = Field(min_length=1)


class Report(Envelope):
    """Reporter의 공개 근거 기반 보고 출력."""
    campaign_id: ID
    statements: list[ReportStatement]
