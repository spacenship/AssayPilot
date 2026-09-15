"""향후 모듈의 타입 계약. 구현 및 영구 상태 변경은 이 계층 밖에서 수행한다."""
from typing import Protocol, Sequence

from .catalog import AssaySpec, Candidate, EvidenceRef
from .common import Contract, Text
from .containers import ObservationBatch, PublicCampaign, RunState
from .exchange import (
    ApprovedAction, AuditResult, ExecutionReceipt, ExecutionResult,
    GovernanceDecision, PredictionBatch, Report,
)
from .records import ActionRequest, Plan


class DataSource(Contract):
    """어댑터 입력의 출처 종류와 위치. 공개 캠페인에는 포함하지 않는다."""
    kind: Text
    location: Text


class CampaignAdapter(Protocol):
    """출처별 데이터를 공개 스키마로 변환하는 경계."""
    def load(self, source: DataSource) -> PublicCampaign:
        """데이터 출처를 받아 현재 공개할 수 있는 캠페인을 반환한다."""
        ...


class Auditor(Protocol):
    """공개 캠페인에 대한 검사 모듈."""
    def audit(self, campaign: PublicCampaign) -> AuditResult:
        """공개 필드와 참조를 검사하고 대상·필드·사유를 반환한다."""
        ...


class Predictor(Protocol):
    """분자 특징 또는 특정 모델 구현에 종속되지 않는 예측기."""
    def fit(self, candidates: Sequence[Candidate], assays: Sequence[AssaySpec], observations: ObservationBatch) -> None:
        """후보 구조와 시험 맥락 및 공개 관측만으로 내부 모델을 학습한다."""
        ...

    def predict(self, candidates: Sequence[Candidate], assays: Sequence[AssaySpec], observations: ObservationBatch) -> PredictionBatch:
        """지정 후보×시험의 사건 확률 또는 unavailable과 그 사유를 반환한다."""
        ...


class Planner(Protocol):
    """공개 정보로 가설 구분 계획을 제안한다."""
    def plan(self, campaign: PublicCampaign, state: RunState, evidence: Sequence[EvidenceRef]) -> Plan:
        """공개 카탈로그·현재 상태·근거에서 후보 배치 전 계획을 반환한다."""
        ...


class Selector(Protocol):
    """계획을 후보별 제안 행동으로 변환한다."""
    def select(self, plan: Plan, predictions: PredictionBatch, state: RunState) -> list[ActionRequest]:
        """계획·예측·상태를 받아 승인되지 않은 행동 목록을 반환한다."""
        ...


class Governor(Protocol):
    """제안 행동의 승인 권한 경계."""
    def review(self, action: ActionRequest, campaign: PublicCampaign, state: RunState) -> GovernanceDecision:
        """시험 비용·선행조건·공개 상태를 검토해 승인 또는 거절 사유를 반환한다."""
        ...


class Executor(Protocol):
    """승인 행동의 접수와 비동기 결과 조회 계약."""
    def submit(self, action: ApprovedAction) -> ExecutionReceipt:
        """승인된 단일 행동을 접수하고 행동 ID가 연결된 접수를 반환한다."""
        ...

    def collect(self, receipt: ExecutionReceipt) -> ExecutionResult:
        """접수 ID로 대기·완료 관측·실패 정보 중 하나를 반환한다."""
        ...


class Reporter(Protocol):
    """공개 실행 기록에 근거를 연결하는 보고 모듈."""
    def render(self, campaign: PublicCampaign, history: Sequence[RunState]) -> Report:
        """공개 상태 기록과 근거 카탈로그로 근거 ID가 연결된 보고를 반환한다."""
        ...
