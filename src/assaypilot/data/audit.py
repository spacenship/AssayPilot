"""공개 Campaign Auditor와 개발자용 측정 감사 보조 함수."""
from __future__ import annotations

from collections import Counter

from assaypilot.domain import AuditIssue, AuditResult, PublicCampaign, validate_public_campaign

from .schemas import NormalizedMeasurement


class PublicCampaignAuditor:
    """공개 패키지에만 접근하는 Auditor Protocol 구현."""

    def audit(self, campaign: PublicCampaign) -> AuditResult:
        """domain 참조 검사와 공개 SMILES 화학 파싱 오류를 합쳐 반환한다."""
        result = validate_public_campaign(campaign)
        issues = list(result.issues)
        try:
            from rdkit import Chem
        except ImportError:
            issues.append(AuditIssue(target_id=campaign.campaign.campaign_id,
                                     field="candidates", code="rdkit_unavailable",
                                     reason="chemical SMILES audit was not performed"))
            return AuditResult(issues=issues)
        for candidate in campaign.candidates:
            if Chem.MolFromSmiles(candidate.original_smiles) is None:
                issues.append(AuditIssue(target_id=candidate.candidate_id,
                                         field="original_smiles", code="invalid_smiles",
                                         reason="RDKit cannot parse public source SMILES"))
        return AuditResult(issues=issues)


def measurement_statistics(measurements: list[NormalizedMeasurement]) -> tuple[dict[str, int], dict[str, int]]:
    """원본을 합치지 않고 시험별 측정 수와 판정 분포를 센다."""
    return dict(Counter(m.assay_id for m in measurements)), dict(Counter(m.verdict.value for m in measurements))
