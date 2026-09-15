"""후보 원본 정체성, 시험 정의, 캠페인 목표 및 근거 목록."""
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from .common import Comparison, Contract, Cost, Finite, ID, Text, Verdict


class Candidate(Contract):
    """원본 식별자와 SMILES를 변형하지 않고 보존하는 후보."""
    candidate_id: ID
    source: Text
    source_id: ID
    original_smiles: Text


class AssayRole(StrEnum):
    """시험 역할이며 성공 여부 자체를 뜻하지 않는다."""
    PRIMARY = "primary"
    CONFIRMATORY = "confirmatory"
    COUNTER = "counter"
    DOSE_RESPONSE = "dose_response"
    OTHER = "other"


class Prerequisite(Contract):
    """관측 존재 조건 또는 지정 표준 판정 조건. 실행 엔진은 별도다."""
    assay_id: ID
    kind: Literal["observed", "verdict"]
    verdict: Verdict | None = None

    @model_validator(mode="after")
    def check_kind(self) -> "Prerequisite":
        """판정 조건에서만 판정 값을 요구한다."""
        if (self.kind == "verdict") != (self.verdict is not None):
            raise ValueError("verdict is required only for kind=verdict")
        return self


class AssaySpec(Contract):
    """측정 의미와 비용, 선행 시험을 선언하는 시험 명세."""
    assay_id: ID
    name: Text
    role: AssayRole
    endpoint: Text
    unit: Text
    verdict_meaning: dict[Verdict, Text]
    prerequisites: list[Prerequisite] = Field(default_factory=list)
    cost: Cost


class SuccessCondition(Contract):
    """시험별 성공의 선언적 표현. 판정 또는 단위가 있는 수치 조건."""
    assay_id: ID
    meaning: Text
    kind: Literal["verdict", "numeric"]
    verdict: Verdict | None = None
    comparison: Comparison | None = None
    value: Finite | None = None
    unit: Text | None = None

    @model_validator(mode="after")
    def check_shape(self) -> "SuccessCondition":
        """판정과 수치 조건을 혼합하거나 불완전하게 선언하지 못하게 한다."""
        numeric = (self.comparison, self.value, self.unit)
        if self.kind == "verdict":
            if self.verdict is None or any(x is not None for x in numeric):
                raise ValueError("verdict condition requires only verdict")
        elif self.verdict is not None or any(x is None for x in numeric):
            raise ValueError("numeric condition requires comparison, value, unit only")
        return self


class CampaignSpec(Contract):
    """특정 표적에 종속되지 않는 목표와 시험별 성공 조건, 예산 설정."""
    campaign_id: ID
    goal: Text
    target: Text | None = None
    biological_context: Text | None = None
    success_conditions: list[SuccessCondition]
    budget: Cost


class EvidenceRef(Contract):
    """가설·계획·관측이 공유하는 원본 근거의 위치 참조."""
    evidence_id: ID
    source_kind: Text
    source_id: ID
    location: Text
