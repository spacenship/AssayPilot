"""1단계 준비 설정, 정규화 중간값 및 개발자용 감사 보고서."""
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from assaypilot.domain import (
    AssayRole, Comparison, Cost, Prerequisite, SuccessCondition, Verdict,
)
from assaypilot.domain.common import Contract, Finite, ID, Text


class RawFileSpec(Contract):
    """원본 캐시의 상대 파일명과 요청 식별 정보."""
    key: ID
    request_path: Text
    format: Literal["csv", "json"]


class AssayMapping(Contract):
    """하나의 PubChem AID를 내부 시험 계약으로 바꾸는 명시적 설정."""
    assay_id: ID
    aid: int = Field(gt=0)
    name: Text
    role: AssayRole
    endpoint: Text
    unit: Text
    verdict_meaning: dict[Verdict, Text]
    raw_outcome_column: Text = "Activity Outcome"
    raw_endpoint_column: Text | None = "Activity Value [uM]"
    raw_unit: Text | None = "uM"
    conversion: Literal["identity"] = "identity"
    verdict_mapping: dict[Text, Verdict]
    protocol_location: Text
    concise_cache_key: ID
    description_cache_key: ID
    cost: Cost
    prerequisites: list[Prerequisite] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_endpoint_rule(self) -> "AssayMapping":
        """수치 endpoint를 읽으면 원본 단위와 동일 변환 정책을 명시한다."""
        if self.raw_endpoint_column is not None and self.raw_unit is None:
            raise ValueError("raw_unit is required with raw_endpoint_column")
        return self


class CandidateRule(Contract):
    """후속 자료를 보지 않는 초기 후보 선정 규칙."""
    primary_assay_id: ID
    include: Literal["primary_active", "primary_tested"]
    limit: int = Field(gt=0)
    seed: int = Field(ge=0)


class CampaignConfig(Contract):
    """데이터 준비자만 읽는 버전 있는 캠페인 설정."""
    schema_version: Literal["1.0.0"] = "1.0.0"
    campaign_id: ID
    goal: Text
    target: Text | None = None
    biological_context: Text | None = None
    assays: list[AssayMapping] = Field(min_length=1)
    success_conditions: list[SuccessCondition]
    budget: Cost
    candidate_rule: CandidateRule
    initial_as_of: AwareDatetime
    normalization_policy_version: Text
    data_kind: Literal["pubchem", "synthetic"]
    subset_note: Text
    raw_files: list[RawFileSpec]
    smiles_cache_key: ID

    @model_validator(mode="after")
    def check_ids(self) -> "CampaignConfig":
        """설정 안의 시험·원본 캐시 키와 primary 선택 대상을 확인한다."""
        ids = [assay.assay_id for assay in self.assays]
        if len(ids) != len(set(ids)):
            raise ValueError("assay_id values must be unique")
        if self.candidate_rule.primary_assay_id not in ids:
            raise ValueError("candidate_rule.primary_assay_id is unknown")
        if next(assay for assay in self.assays if assay.assay_id == self.candidate_rule.primary_assay_id).role is not AssayRole.PRIMARY:
            raise ValueError("candidate_rule.primary_assay_id must have primary role")
        if len({raw.key for raw in self.raw_files}) != len(self.raw_files):
            raise ValueError("raw file keys must be unique")
        keys = {raw.key for raw in self.raw_files}
        if self.smiles_cache_key not in keys:
            raise ValueError("smiles_cache_key is unknown")
        for assay in self.assays:
            if assay.concise_cache_key not in keys or assay.description_cache_key not in keys:
                raise ValueError("assay cache key is unknown")
        return self


class NormalizedMeasurement(Contract):
    """released_at 없이 원본 행을 추적하는 개발자용 실측 중간 타입."""
    measurement_id: ID
    assay_id: ID
    aid: int = Field(gt=0)
    sid: int = Field(gt=0)
    cid: int | None = Field(default=None, gt=0)
    original_smiles: Text | None = None
    raw_verdict: Text | None = None
    verdict: Verdict = Verdict.UNSPECIFIED
    value: Finite | None = None
    unit: Text | None = None
    comparison: Comparison | None = None
    replicate_id: ID
    condition_id: ID
    source_row_id: ID
    source_file_sha256: Text
    protocol_location: Text
    raw_row: dict[str, str]

    @model_validator(mode="after")
    def check_measurement(self) -> "NormalizedMeasurement":
        """수치·단위·비교의 동시 존재와 빈 행 금지를 적용한다."""
        if self.value is None and self.raw_verdict is None:
            raise ValueError("measurement requires raw verdict or numeric value")
        if self.value is None and (self.unit is not None or self.comparison is not None):
            raise ValueError("unit/comparison require value")
        if self.value is not None and (self.unit is None or self.comparison is None):
            raise ValueError("numeric value requires unit and comparison")
        return self


class DataIssue(Contract):
    """개발자용 준비 과정의 오류 또는 warning. 공개 AuditResult와 분리된다."""
    severity: Literal["error", "warning"]
    code: Text
    location: Text
    message: Text


class DataAuditReport(Contract):
    """원본·정규화·분리 과정의 통계와 진단. 에이전트 입력이 아니다."""
    schema_version: Literal["1.0.0"] = "1.0.0"
    campaign_id: ID
    created_at: AwareDatetime
    source_files: dict[Text, Text]
    included_rows: int
    excluded_rows: int
    measurements_by_assay: dict[Text, int]
    verdict_counts: dict[Text, int]
    selected_candidates: int
    hidden_followup_measurements: int
    issues: list[DataIssue]
    selection_bias_note: Text

    @property
    def has_errors(self) -> bool:
        """필수 오류 때문에 build를 중단해야 하는지 나타낸다."""
        return any(issue.severity == "error" for issue in self.issues)
