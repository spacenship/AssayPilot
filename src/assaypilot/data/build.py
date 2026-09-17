"""로컬 캐시만 읽어 PublicCampaign과 개발자용 후속 측정을 분리 생성한다."""
from __future__ import annotations

import csv
import hashlib
import json
import random
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from assaypilot.domain import (
    AssaySpec, CampaignSpec, Candidate, EvidenceRef, Observation, PublicCampaign,
    validate_public_campaign,
)

from .audit import measurement_statistics
from .normalize import NormalizationError, normalize_concise_row, stable_id
from .pubchem import FetchError, parse_concise_csv
from .schemas import CampaignConfig, DataAuditReport, DataIssue, NormalizedMeasurement


class BuildError(RuntimeError):
    """필수 준비 오류가 있어 정상 공개 패키지를 만들지 않았음을 나타낸다."""

    def __init__(self, report: DataAuditReport) -> None:
        self.report = report
        super().__init__("campaign build failed; inspect DataAuditReport")


def load_config(path: Path) -> CampaignConfig:
    """JSON 준비 설정을 엄격하게 다시 검증한다."""
    return CampaignConfig.model_validate_json(path.read_text())


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _cache_bytes(config: CampaignConfig, cache_dir: Path) -> dict[str, bytes]:
    loaded: dict[str, bytes] = {}
    for raw in config.raw_files:
        path = cache_dir / raw.key
        if not path.is_file():
            raise FileNotFoundError(f"missing cache file: {path}")
        content = path.read_bytes()
        if not content or content.lstrip().lower().startswith((b"<html", b"<!doctype html")):
            raise FetchError(f"cache is empty or HTML: {path}")
        loaded[raw.key] = content
    return loaded


def _parse_smiles(raw: bytes) -> dict[int, str]:
    """CID 표준화 구조 CSV를 읽는다. 제출자 원본 SMILES라고 주장하지 않는다."""
    try:
        rows = csv.DictReader(raw.decode("utf-8-sig").splitlines())
    except UnicodeDecodeError as exc:
        raise FetchError("compound property CSV is not UTF-8") from exc
    if rows.fieldnames is None or "CID" not in rows.fieldnames:
        raise FetchError("compound property CSV lacks CID")
    smiles_column = next((name for name in ("ConnectivitySMILES", "CanonicalSMILES", "IsomericSMILES")
                          if name in rows.fieldnames), None)
    if smiles_column is None:
        raise FetchError("compound property CSV lacks a supported SMILES column")
    parsed: dict[int, str] = {}
    for row in rows:
        cid = (row.get("CID") or "").strip()
        smiles = (row.get(smiles_column) or "").strip()
        if cid.isdecimal() and int(cid) > 0 and smiles:
            if int(cid) in parsed and parsed[int(cid)] != smiles:
                raise FetchError(f"conflicting standardized SMILES for CID {cid}")
            parsed[int(cid)] = smiles
    return parsed


def _issue(severity: str, code: str, location: str, message: str) -> DataIssue:
    return DataIssue(severity=severity, code=code, location=location, message=message)


def _make_report(config: CampaignConfig, source_files: dict[str, str], measurements: list[NormalizedMeasurement],
                 selected: int, hidden: int, issues: list[DataIssue], excluded: int) -> DataAuditReport:
    by_assay, verdicts = measurement_statistics(measurements)
    return DataAuditReport(campaign_id=config.campaign_id, created_at=datetime.now(timezone.utc),
        source_files=source_files, included_rows=len(measurements), excluded_rows=excluded,
        measurements_by_assay=by_assay, verdict_counts=verdicts, selected_candidates=selected,
        hidden_followup_measurements=hidden, issues=issues,
        selection_bias_note=("후속 시험 측정 범위는 primary 결과 또는 역사적 운영에 따라 선택되었을 수 있다. "
                             "미측정은 음성·실패로 집계하지 않으며 complete-case를 자동 선택하지 않는다."))


def _write_json(path: Path, value: object) -> str:
    content = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return _sha(content.encode())


def build_campaign(config: CampaignConfig, cache_dir: Path, output_dir: Path) -> DataAuditReport:
    """캐시→정규화→공개/후속 분리를 실행하며 오류 시 output_dir을 만들지 않는다."""
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output_dir}")
    cached = _cache_bytes(config, cache_dir)
    source_files = {key: _sha(value) for key, value in cached.items()}
    smiles_by_cid = _parse_smiles(cached[config.smiles_cache_key])
    measurements: list[NormalizedMeasurement] = []
    issues: list[DataIssue] = []
    excluded = 0
    for mapping in config.assays:
        try:
            rows = parse_concise_csv(cached[mapping.concise_cache_key])
        except FetchError as exc:
            issues.append(_issue("error", "unsupported_raw_format", mapping.assay_id, str(exc)))
            continue
        for number, row in enumerate(rows, start=2):
            try:
                measurements.append(normalize_concise_row(row, mapping,
                    source_file_sha256=source_files[mapping.concise_cache_key],
                    smiles_by_cid=smiles_by_cid, row_number=number))
            except NormalizationError as exc:
                excluded += 1
                issues.append(_issue("error", "normalization_error", f"{mapping.assay_id}:row:{number}", str(exc)))
    primary = next(assay for assay in config.assays if assay.assay_id == config.candidate_rule.primary_assay_id)
    primary_measurements = [m for m in measurements if m.assay_id == primary.assay_id]
    eligible = [m for m in primary_measurements if config.candidate_rule.include == "primary_tested" or m.verdict.value == "active"]
    # SID is the observation identity. Repeated primary rows do not create multiple candidates.
    by_sid: dict[int, NormalizedMeasurement] = {}
    for measurement in sorted(eligible, key=lambda m: m.measurement_id):
        if measurement.sid in by_sid and by_sid[measurement.sid].cid != measurement.cid:
            issues.append(_issue("error", "sid_cid_conflict", str(measurement.sid), "same SID maps to multiple CIDs"))
        by_sid.setdefault(measurement.sid, measurement)
    ordered = sorted(by_sid.values(), key=lambda m: m.measurement_id)
    random.Random(config.candidate_rule.seed).shuffle(ordered)
    chosen = ordered[:config.candidate_rule.limit]
    if not chosen:
        issues.append(_issue("error", "empty_candidate_selection", primary.assay_id, "candidate rule selected no primary candidates"))
    candidates: list[Candidate] = []
    candidate_by_sid: dict[int, str] = {}
    for measurement in chosen:
        if measurement.cid is None or measurement.original_smiles is None:
            issues.append(_issue("error", "missing_structure", f"SID:{measurement.sid}",
                                 "selected SID has no CID standardized SMILES"))
            continue
        candidate_id = stable_id("candidate", config.campaign_id, "sid", measurement.sid)
        candidate_by_sid[measurement.sid] = candidate_id
        candidates.append(Candidate(candidate_id=candidate_id, source="pubchem_sid",
            source_id=f"SID:{measurement.sid}", original_smiles=measurement.original_smiles))
    if len(candidates) != len(chosen):
        issues.append(_issue("error", "candidate_mapping_failure", config.campaign_id,
                             "not every selected SID could form a candidate"))
    selected_sids = set(candidate_by_sid)
    hidden = [m for m in measurements if m.sid in selected_sids and m.assay_id != primary.assay_id]
    if not hidden:
        issues.append(_issue("error" if config.data_kind == "pubchem" else "warning",
            "no_linked_followup", config.campaign_id,
            "selected candidates have no normalized follow-up measurements; no data connection is claimed"))
    report = _make_report(config, source_files, measurements, len(candidates), len(hidden), issues, excluded)
    if report.has_errors:
        raise BuildError(report)
    assay_specs = [AssaySpec(assay_id=m.assay_id, name=m.name, role=m.role, endpoint=m.endpoint,
                  unit=m.unit, verdict_meaning=m.verdict_meaning, prerequisites=m.prerequisites, cost=m.cost)
                  for m in config.assays]
    evidence = [EvidenceRef(evidence_id=stable_id("evidence", config.campaign_id, m.assay_id),
                source_kind="public_bundle_evidence", source_id=f"PubChem AID:{m.aid}",
                location=f"evidence/{stable_id('evidence', config.campaign_id, m.assay_id)}.json") for m in config.assays]
    evidence_by_assay = {mapping.assay_id: item.evidence_id for mapping, item in zip(config.assays, evidence)}
    public_observations = [Observation(observation_id=stable_id("observation", config.campaign_id, m.measurement_id),
        candidate_id=candidate_by_sid[m.sid], assay_id=m.assay_id, value=m.value, unit=m.unit,
        comparison=m.comparison, raw_verdict=m.raw_verdict, verdict=m.verdict,
        evidence_ids=[evidence_by_assay[m.assay_id]], released_at=config.initial_as_of,
        replicate_id=m.replicate_id, condition_id=m.condition_id)
        for m in measurements if m.sid in selected_sids and m.assay_id == primary.assay_id]
    public = PublicCampaign(campaign=CampaignSpec(campaign_id=config.campaign_id, goal=config.goal,
        target=config.target, biological_context=config.biological_context,
        success_conditions=config.success_conditions, budget=config.budget), candidates=candidates,
        assays=assay_specs, evidence=evidence, observations=public_observations, as_of=config.initial_as_of)
    domain_audit = validate_public_campaign(public)
    if not domain_audit.ok:
        report = report.validated_replace(issues=report.issues + [_issue("error", "public_contract_error",
            f"{issue.target_id}:{issue.field}", issue.reason) for issue in domain_audit.issues])
        raise BuildError(report)
    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output_dir.name}.", dir=parent) as temporary:
        root = Path(temporary)
        public_root = root / "public"
        curator_root = root / "curator"
        hashes = {"campaign.json": _write_json(public_root / "campaign.json", public.model_dump(mode="json"))}
        for mapping, item in zip(config.assays, evidence):
            hashes[item.location] = _write_json(public_root / item.location, {
                "evidence_id": item.evidence_id, "source_kind": item.source_kind, "source_id": item.source_id,
                "aid": mapping.aid, "name": mapping.name, "endpoint": mapping.endpoint, "unit": mapping.unit,
                "protocol_location": mapping.protocol_location,
                "note": "공개 시험 정의 사본. 이 파일에는 결과 행·후속 측정 범위·통계가 없다.",
            })
        manifest = {"schema_version": "1.0.0", "kind": "assaypilot_public_bundle",
                    "campaign_id": config.campaign_id, "campaign_file": "campaign.json", "files": hashes}
        _write_json(public_root / "manifest.json", manifest)
        _write_json(curator_root / "normalized_measurements.json", [m.model_dump(mode="json") for m in measurements])
        _write_json(curator_root / "hidden_followup_measurements.json", [m.model_dump(mode="json") for m in hidden])
        _write_json(curator_root / "data_audit_report.json", report.model_dump(mode="json"))
        _write_json(curator_root / "provenance.json", {"config": config.model_dump(mode="json"),
                    "raw_file_sha256": source_files, "note": "개발자용: 공개 Adapter가 읽지 않는다."})
        shutil.move(str(root), output_dir)
    return report
