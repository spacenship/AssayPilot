"""공개 실측, 가설, 계획 및 제안 행동. 예측은 실측과 분리한다."""
from enum import StrEnum
import math
from typing import Any

from pydantic import AwareDatetime, Field, JsonValue, field_validator, model_validator

from .common import Comparison, Contract, Finite, ID, Text, Verdict


def _json_only(value: Any) -> Any:
    """JSON 기본 값만 허용하고 NaN, 비문자 키 및 Python 전용 객체를 거절한다."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is list:
        for item in value:
            _json_only(item)
        return value
    if type(value) is dict and all(type(k) is str for k in value):
        for item in value.values():
            _json_only(item)
        return value
    raise ValueError("parameters must contain only finite JSON values")


class Observation(Contract):
    """공개 시점을 가진 실측 기록. 원본 판정을 자동 이진화하지 않는다."""
    observation_id: ID
    candidate_id: ID
    assay_id: ID
    value: Finite | None = None
    unit: Text | None = None
    comparison: Comparison | None = None
    raw_verdict: Text | None = None
    verdict: Verdict = Verdict.UNSPECIFIED
    evidence_ids: list[ID] = Field(min_length=1)
    released_at: AwareDatetime
    replicate_id: ID
    condition_id: ID

    @model_validator(mode="after")
    def check_measurement(self) -> "Observation":
        """수치 실측에는 단위·비교를 요구하며 빈 미측정 기록을 거절한다."""
        if self.value is not None:
            if self.unit is None or self.comparison is None:
                raise ValueError("numeric observation requires unit and comparison")
        elif self.unit is not None or self.comparison is not None:
            raise ValueError("unit/comparison require a value")
        if self.value is None and self.raw_verdict is None and self.verdict == Verdict.UNSPECIFIED:
            raise ValueError("observation requires measurement or verdict")
        return self


class HypothesisStatus(StrEnum):
    """근거에 대한 가설 상태이며 임상 검증이나 치료 효능의 상태가 아니다."""
    PROPOSED = "proposed"
    EVIDENCE_SUPPORTED = "evidence_supported"
    CHALLENGED = "challenged"
    REFUTED = "refuted"
    RETIRED = "retired"


class Hypothesis(Contract):
    """지지·반박 근거와 반증 조건을 명시하는 가설."""
    hypothesis_id: ID
    content: Text
    supporting_evidence_ids: list[ID] = Field(default_factory=list)
    opposing_evidence_ids: list[ID] = Field(default_factory=list)
    falsification_conditions: list[Text] = Field(min_length=1)
    status: HypothesisStatus = HypothesisStatus.PROPOSED


class Plan(Contract):
    """다음 구분 질문과 시험 제안. 실제 후보 배치는 Selector의 책임이다."""
    plan_id: ID
    hypothesis_ids: list[ID] = Field(default_factory=list)
    evidence_ids: list[ID] = Field(default_factory=list)
    next_question: Text
    proposed_assay_ids: list[ID] = Field(default_factory=list)
    rationale: Text
    stop_reason: Text | None = None
    hold_reason: Text | None = None


class ActionRequest(Contract):
    """승인 권한 없이 후보 하나에 대한 시험과 JSON 매개변수를 제안한다."""
    action_id: ID
    campaign_id: ID
    candidate_id: ID
    assay_id: ID
    parameters: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("parameters", mode="before")
    @classmethod
    def check_parameters(cls, value: Any) -> Any:
        """Pydantic의 변환 이전에 엄격한 JSON 표현 가능성을 검사한다."""
        return _json_only(value)
