"""공개 데이터 경계와 최소 실행 상태 컨테이너."""
from typing import Literal
from pydantic import AwareDatetime, Field

from .catalog import AssaySpec, CampaignSpec, Candidate, EvidenceRef
from .common import Envelope, ID
from .exchange import ApprovedAction, BudgetState
from .records import Hypothesis, Observation, Plan


class ObservationBatch(Envelope):
    """동일 캠페인의 공개 실측 전송 묶음."""
    campaign_id: ID
    observations: list[Observation]


class PublicCampaign(Envelope):
    """후보·시험·근거와 현재 공개 실측만 담는 어댑터 출력."""
    campaign: CampaignSpec
    candidates: list[Candidate]
    assays: list[AssaySpec]
    evidence: list[EvidenceRef]
    observations: list[Observation] = Field(default_factory=list)
    as_of: AwareDatetime


class RunState(Envelope):
    """공개된 현재 상태. 고정 카탈로그는 campaign_id로 외부 참조한다."""
    campaign_id: ID
    as_of: AwareDatetime
    observations: list[Observation] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    plans: list[Plan] = Field(default_factory=list)
    pending_actions: list[ApprovedAction] = Field(default_factory=list)
    budget: BudgetState
    status: Literal["ready", "running", "paused", "completed", "failed"]
