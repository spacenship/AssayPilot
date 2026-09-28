"""공개 Campaign Auditor와 개발자용 측정 감사 보조 함수."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from assaypilot.domain import AuditIssue, AuditResult, PublicCampaign, validate_public_campaign

from .schemas import NormalizedMeasurement


@dataclass(frozen=True)
class ChemicalAuditSummary:
    """한 번의 실제 화학 감사 범위. AuditResult 계약과 분리된 data 계층 정보."""
    backend: str
    candidates: int
    structures_present: int
    checked: int
    passed: int
    failed: int


def _load_rdkit_chem():
    """선택 의존성의 실제 RDKit Chem 모듈을 지연 import한다."""
    try:
        from rdkit import Chem
    except ImportError:
        return None
    return Chem


class PublicCampaignAuditor:
    """공개 패키지에만 접근하는 Auditor Protocol 구현."""

    def __init__(self) -> None:
        self.chemical_summary: ChemicalAuditSummary | None = None

    def audit(self, campaign: PublicCampaign) -> AuditResult:
        """domain 참조 검사와 공개 SMILES 화학 파싱 오류를 합쳐 반환한다."""
        result = validate_public_campaign(campaign)
        issues = list(result.issues)
        Chem = _load_rdkit_chem()
        if Chem is None:
            self.chemical_summary = ChemicalAuditSummary(
                backend="unavailable", candidates=len(campaign.candidates),
                structures_present=len(campaign.candidates), checked=0, passed=0, failed=0,
            )
            issues.append(AuditIssue(target_id=campaign.campaign.campaign_id,
                                     field="candidates", code="rdkit_unavailable",
                                     reason="chemical SMILES audit was not performed"))
            return AuditResult(issues=issues)
        passed = failed = 0
        for candidate in campaign.candidates:
            try:
                molecule = Chem.MolFromSmiles(candidate.original_smiles, sanitize=True)
            except Exception:
                molecule = None
            if molecule is None:
                failed += 1
                issues.append(AuditIssue(target_id=candidate.candidate_id,
                                         field="original_smiles", code="invalid_smiles",
                                         reason="RDKit cannot parse public source SMILES"))
            else:
                passed += 1
        self.chemical_summary = ChemicalAuditSummary(
            backend="rdkit", candidates=len(campaign.candidates),
            structures_present=len(campaign.candidates), checked=len(campaign.candidates),
            passed=passed, failed=failed,
        )
        return AuditResult(issues=issues)


def measurement_statistics(measurements: list[NormalizedMeasurement]) -> tuple[dict[str, int], dict[str, int]]:
    """원본을 합치지 않고 시험별 측정 수와 판정 분포를 센다."""
    return dict(Counter(m.assay_id for m in measurements)), dict(Counter(m.verdict.value for m in measurements))
