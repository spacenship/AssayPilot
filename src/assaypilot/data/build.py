"""로컬 캐시만 읽어 PublicCampaign과 개발자용 후속 측정을 분리 생성한다."""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from assaypilot.domain import (
    AssaySpec, CampaignSpec, Candidate, EvidenceRef, Observation, PublicCampaign,
    validate_public_campaign,
)

from .audit import measurement_statistics
from .normalize import NormalizationError, normalize_concise_row, stable_id
from .pubchem import FetchError, parse_concise_csv
from .selection import select_primary_sids
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
    keys = [raw.key for raw in config.raw_files]
    for key in (config.smiles_cache_key, config.connectivity_smiles_cache_key):
        if key is not None and key not in keys:
            keys.append(key)
    for key in keys:
        path = cache_dir / key
        if not path.is_file():
            raise FileNotFoundError(f"missing cache file: {path}")
        content = path.read_bytes()
        if not content or content.lstrip().lower().startswith((b"<html", b"<!doctype html")):
            raise FetchError(f"cache is empty or HTML: {path}")
        loaded[key] = content
    return loaded


def _parse_structure_property(raw: bytes, *, property_name: str) -> dict[int, str]:
    """CID별 PubChem 속성 문자열을 바꾸지 않고 읽는다.

    PubChem CID 구조는 표준화된 CID 구조이며 depositor 제출 구조는 아니다.
    ``SMILES``는 공개 Candidate.original_smiles의 원본 문자열로 사용하고,
    ConnectivitySMILES는 provenance의 연결성 참조로만 보존한다.
    """
    try:
        rows = csv.DictReader(raw.decode("utf-8-sig").splitlines())
    except UnicodeDecodeError as exc:
        raise FetchError("compound property CSV is not UTF-8") from exc
    if rows.fieldnames is None or "CID" not in rows.fieldnames:
        raise FetchError("compound property CSV lacks CID")
    if property_name not in rows.fieldnames:
        raise FetchError(f"compound property CSV lacks {property_name}")
    parsed: dict[int, str] = {}
    for row in rows:
        cid = (row.get("CID") or "").strip()
        smiles = (row.get(property_name) or "").strip()
        if cid.isdecimal() and int(cid) > 0 and smiles:
            if int(cid) in parsed and parsed[int(cid)] != smiles:
                raise FetchError(f"conflicting {property_name} value for CID {cid}")
            parsed[int(cid)] = smiles
    return parsed


def _structure_provenance(config: CampaignConfig, source_files: dict[str, str]) -> dict[str, object]:
    """선택 속성·요청·응답 hash를 curator provenance에 명시한다."""
    raw_by_key = {raw.key: raw for raw in config.raw_files}
    selected = raw_by_key.get(config.smiles_cache_key)
    selected_request = selected.request_path if selected is not None else (
        "generated from primary-only candidate SIDs in fetch; see cache metadata"
    )
    provenance: dict[str, object] = {
        "selection_policy": config.structure_selection_policy,
        "candidate_original_smiles": {
            "property": config.structure_property,
            "cache_key": config.smiles_cache_key,
            "request_path": selected_request,
            "response_sha256": source_files[config.smiles_cache_key],
            "meaning": "PubChem standardized CID SMILES; not a depositor-submitted structure.",
        },
    }
    if config.connectivity_smiles_cache_key is not None:
        connectivity = raw_by_key.get(config.connectivity_smiles_cache_key)
        provenance["connectivity_reference"] = {
            "property": "ConnectivitySMILES",
            "cache_key": config.connectivity_smiles_cache_key,
            "request_path": connectivity.request_path if connectivity is not None else (
                "generated from primary-only candidate SIDs in fetch; see cache metadata"
            ),
            "response_sha256": source_files[config.connectivity_smiles_cache_key],
            "meaning": "Connectivity-only source retained separately; it is never used to restore missing stereochemistry or isotopes.",
        }
    return provenance


def _issue(severity: str, code: str, location: str, message: str) -> DataIssue:
    return DataIssue(severity=severity, code=code, location=location, message=message)


def _make_report(
    config: CampaignConfig,
    source_files: dict[str, str],
    measurements: list[NormalizedMeasurement],
    selected: int,
    hidden: int,
    issues: list[DataIssue],
    excluded: int,
    *,
    raw_rows_by_assay: dict[str, int],
    excluded_rows_by_assay: dict[str, int],
    error_rows_by_assay: dict[str, int],
    candidate_counts: dict[str, int],
    selected_sids: set[int],
) -> DataAuditReport:
    by_assay, verdicts = measurement_statistics(measurements)
    included_rows_by_assay = dict(by_assay)
    verdict_counts_by_assay: dict[str, dict[str, int]] = {}
    sid_to_cids: defaultdict[int, set[int]] = defaultdict(set)
    cid_to_sids: defaultdict[int, set[int]] = defaultdict(set)
    assay_sid_counts: Counter[tuple[str, int]] = Counter()
    assay_sid_verdicts: defaultdict[tuple[str, int], set[str]] = defaultdict(set)
    for measurement in measurements:
        verdict_counts_by_assay.setdefault(measurement.assay_id, {})
        verdict_counts_by_assay[measurement.assay_id][measurement.verdict.value] = (
            verdict_counts_by_assay[measurement.assay_id].get(measurement.verdict.value, 0) + 1
        )
        sid_to_cids[measurement.sid].update({measurement.cid} if measurement.cid is not None else set())
        if measurement.cid is not None:
            cid_to_sids[measurement.cid].add(measurement.sid)
        key = (measurement.assay_id, measurement.sid)
        assay_sid_counts[key] += 1
        assay_sid_verdicts[key].add(measurement.verdict.value)
    repeated_groups = sum(count > 1 for count in assay_sid_counts.values())
    conflicting_groups = sum(count > 1 for key, count in assay_sid_counts.items()
                             if len(assay_sid_verdicts[key]) > 1)
    followup = [measurement for measurement in measurements if measurement.sid in selected_sids and
                measurement.assay_id != config.candidate_rule.primary_assay_id]
    followup_sids = {measurement.sid for measurement in followup}
    followup_pairs = {(measurement.sid, measurement.assay_id) for measurement in followup}
    followup_verdicts = dict(Counter(measurement.verdict.value for measurement in followup))
    return DataAuditReport(
        campaign_id=config.campaign_id, created_at=datetime.now(timezone.utc),
        source_files=source_files, included_rows=len(measurements), excluded_rows=excluded,
        measurements_by_assay=by_assay, verdict_counts=verdicts, selected_candidates=selected,
        hidden_followup_measurements=hidden, issues=issues,
        selection_bias_note=("후속 시험 측정 범위는 primary 결과 또는 역사적 운영에 따라 선택되었을 수 있다. "
                             "미측정은 음성·실패로 집계하지 않으며 complete-case를 자동 선택하지 않는다."),
        raw_rows_by_assay=raw_rows_by_assay,
        included_rows_by_assay=included_rows_by_assay,
        excluded_rows_by_assay=excluded_rows_by_assay,
        error_rows_by_assay=error_rows_by_assay,
        verdict_counts_by_assay=verdict_counts_by_assay,
        unique_sid_count=len(sid_to_cids),
        unique_cid_count=len(cid_to_sids),
        sid_with_multiple_cids=sum(len(cids) > 1 for cids in sid_to_cids.values()),
        cid_with_multiple_sids=sum(len(sids) > 1 for sids in cid_to_sids.values()),
        repeated_assay_sid_groups=repeated_groups,
        conflicting_assay_sid_groups=conflicting_groups,
        candidate_counts=candidate_counts,
        followup_candidate_sids=len(followup_sids),
        followup_candidate_assay_pairs=len(followup_pairs),
        followup_verdict_counts=followup_verdicts,
        unmeasured_selected_candidates=len(selected_sids - followup_sids),
    )


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
    smiles_by_cid = _parse_structure_property(cached[config.smiles_cache_key], property_name=config.structure_property)
    fallback_cids: set[int] = set()
    if config.connectivity_smiles_cache_key is not None:
        connectivity_by_cid = _parse_structure_property(
            cached[config.connectivity_smiles_cache_key], property_name="ConnectivitySMILES")
        if config.structure_selection_policy == "smiles_then_connectivity_with_warning":
            for cid, connectivity_smiles in connectivity_by_cid.items():
                if cid not in smiles_by_cid:
                    smiles_by_cid[cid] = connectivity_smiles
                    fallback_cids.add(cid)
    measurements: list[NormalizedMeasurement] = []
    issues: list[DataIssue] = []
    excluded = 0
    raw_rows_by_assay: dict[str, int] = {}
    excluded_rows_by_assay: dict[str, int] = {}
    error_rows_by_assay: dict[str, int] = {}
    for mapping in config.assays:
        try:
            rows = parse_concise_csv(cached[mapping.concise_cache_key])
        except FetchError as exc:
            issues.append(_issue("error", "unsupported_raw_format", mapping.assay_id, str(exc)))
            raw_rows_by_assay[mapping.assay_id] = 0
            excluded_rows_by_assay[mapping.assay_id] = 0
            error_rows_by_assay[mapping.assay_id] = 1
            continue
        raw_rows_by_assay[mapping.assay_id] = len(rows)
        excluded_rows_by_assay[mapping.assay_id] = 0
        error_rows_by_assay[mapping.assay_id] = 0
        for number, row in enumerate(rows, start=2):
            try:
                measurements.append(normalize_concise_row(row, mapping,
                    source_file_sha256=source_files[mapping.concise_cache_key],
                    smiles_by_cid=smiles_by_cid, row_number=number))
            except NormalizationError as exc:
                excluded += 1
                excluded_rows_by_assay[mapping.assay_id] += 1
                error_rows_by_assay[mapping.assay_id] += 1
                issues.append(_issue("error", "normalization_error", f"{mapping.assay_id}:row:{number}", str(exc)))
    primary = next(assay for assay in config.assays if assay.assay_id == config.candidate_rule.primary_assay_id)
    primary_measurements = [m for m in measurements if m.assay_id == primary.assay_id]
    # SID is the candidate identity. Repeated rows remain measurements and are
    # never averaged; only the first deterministic row supplies candidate CID/
    # structure metadata. Conflicting SID→CID mappings are fatal.
    by_sid: dict[int, NormalizedMeasurement] = {}
    for measurement in sorted(primary_measurements, key=lambda m: m.measurement_id):
        if config.candidate_rule.include == "primary_active" and measurement.verdict.value != "active":
            continue
        if measurement.sid in by_sid and by_sid[measurement.sid].cid != measurement.cid:
            issues.append(_issue("error", "sid_cid_conflict", str(measurement.sid), "same SID maps to multiple CIDs"))
        by_sid.setdefault(measurement.sid, measurement)
    selected_sids_order = select_primary_sids(
        list(by_sid.values()), include=config.candidate_rule.include,
        limit=config.candidate_rule.limit, seed=config.candidate_rule.seed,
        sid_getter=lambda m: m.sid,
        active_getter=lambda m: m.verdict.value == "active",
        ordering_key=lambda m: m.measurement_id,
    )
    chosen = [by_sid[sid] for sid in selected_sids_order]
    if not chosen:
        issues.append(_issue("error", "empty_candidate_selection", primary.assay_id, "candidate rule selected no primary candidates"))
    candidates: list[Candidate] = []
    candidate_by_sid: dict[int, str] = {}
    for measurement in chosen:
        if measurement.cid is None or measurement.original_smiles is None:
            issues.append(_issue("error", "missing_smiles", f"SID:{measurement.sid}",
                                 "selected SID has no CID value in the configured PubChem SMILES response"))
            continue
        candidate_id = stable_id("candidate", config.campaign_id, "sid", measurement.sid)
        candidate_by_sid[measurement.sid] = candidate_id
        if measurement.cid in fallback_cids:
            issues.append(_issue("warning", "connectivity_smiles_fallback", f"SID:{measurement.sid}",
                                 "configured fallback used ConnectivitySMILES; stereochemistry and isotope information were not verified"))
        candidates.append(Candidate(candidate_id=candidate_id, source="pubchem_sid",
            source_id=f"SID:{measurement.sid}", original_smiles=measurement.original_smiles))
    if len(candidates) != len(chosen):
        issues.append(_issue("error", "candidate_mapping_failure", config.campaign_id,
                             "not every selected SID could form a candidate"))
    selected_sids = set(candidate_by_sid)
    hidden = [m for m in measurements if m.sid in selected_sids and m.assay_id != primary.assay_id]
    if not hidden:
        # A valid source with no selected-candidate follow-up still produces
        # the public primary bundle.  The curator report records the absence
        # separately; missing files, parse errors, and invalid rows remain
        # errors earlier in the build.
        issues.append(_issue("warning",
            "no_linked_followup", config.campaign_id,
            "selected candidates have no normalized follow-up measurements; no data connection is claimed"))
    candidate_counts = {
        "primary_rows": len(primary_measurements),
        "primary_active_rows": sum(m.verdict.value == "active" for m in primary_measurements),
        "primary_tested_unique_sids": len({m.sid for m in primary_measurements}),
        "primary_active_unique_sids": len({m.sid for m in primary_measurements if m.verdict.value == "active"}),
        "selected": len(candidates),
    }
    report = _make_report(
        config, source_files, measurements, len(candidates), len(hidden), issues, excluded,
        raw_rows_by_assay=raw_rows_by_assay,
        excluded_rows_by_assay=excluded_rows_by_assay,
        error_rows_by_assay=error_rows_by_assay,
        candidate_counts=candidate_counts,
        selected_sids=selected_sids,
    )
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
    public_observation_by_measurement = {
        measurement.measurement_id: observation
        for measurement, observation in zip(
            (m for m in measurements if m.sid in selected_sids and m.assay_id == primary.assay_id),
            public_observations,
        )
    }
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
            observation_traces = []
            if mapping.assay_id == primary.assay_id:
                for measurement in measurements:
                    observation = public_observation_by_measurement.get(measurement.measurement_id)
                    if observation is None or measurement.assay_id != mapping.assay_id:
                        continue
                    trace_columns = ["AID", "SID", "CID", mapping.raw_outcome_column, "Activity Name"]
                    if mapping.raw_endpoint_column is not None:
                        trace_columns.append(mapping.raw_endpoint_column)
                    elif mapping.endpoint_scope == "categorical_activity_outcome_only":
                        trace_columns.append("Activity Value [uM]")
                    observation_traces.append({
                        "observation_id": observation.observation_id,
                        "sid": measurement.sid,
                        "aid": measurement.aid,
                        "raw_outcome": measurement.raw_verdict,
                        "source_row_id": measurement.source_row_id,
                        "source_row_number": measurement.source_row_number,
                        "source_file_sha256": measurement.source_file_sha256,
                        "raw_row": {
                            key: measurement.raw_row[key]
                            for key in dict.fromkeys(trace_columns)
                            if key in measurement.raw_row
                        },
                    })
            hashes[item.location] = _write_json(public_root / item.location, {
                "evidence_id": item.evidence_id, "source_kind": item.source_kind, "source_id": item.source_id,
                "aid": mapping.aid, "name": mapping.name, "endpoint": mapping.endpoint, "unit": mapping.unit,
                "endpoint_scope": mapping.endpoint_scope,
                "endpoint_meaning": mapping.endpoint_meaning,
                "official_result_names": mapping.official_result_names,
                "activity_name_policy": mapping.activity_name_policy,
                "raw_outcome_column": mapping.raw_outcome_column,
                "raw_endpoint_column": mapping.raw_endpoint_column,
                "raw_unit": mapping.raw_unit,
                "protocol_location": mapping.protocol_location,
                "observation_traces": observation_traces,
                "note": "공개 시험 정의 사본. primary 관측에는 SID·AID·원본 Activity Outcome을 확인할 최소 추적 행을 포함하며, 후속 측정 범위·통계는 포함하지 않는다.",
            })
        manifest = {"schema_version": "1.0.0", "kind": "assaypilot_public_bundle",
                    "campaign_id": config.campaign_id, "campaign_file": "campaign.json", "files": hashes}
        _write_json(public_root / "manifest.json", manifest)
        _write_json(curator_root / "normalized_measurements.json", [m.model_dump(mode="json") for m in measurements])
        _write_json(curator_root / "hidden_followup_measurements.json", [m.model_dump(mode="json") for m in hidden])
        _write_json(curator_root / "data_audit_report.json", report.model_dump(mode="json"))
        _write_json(curator_root / "provenance.json", {"config": config.model_dump(mode="json"),
                    "raw_file_sha256": source_files,
                    "structure": _structure_provenance(config, source_files),
                    "note": "개발자용: 공개 Adapter가 읽지 않는다."})
        shutil.move(str(root), output_dir)
    return report
