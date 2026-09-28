"""1단계 캐시·정규화·공개 bundle 경계를 검증한다."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.data.audit import PublicCampaignAuditor
from assaypilot.data.build import BuildError, build_campaign, load_config
from assaypilot.data.normalize import NormalizationError, normalize_concise_row, parse_numeric
from assaypilot.data.pubchem import FetchError, PubChemClient, _primary_structure_cids, parse_concise_csv
from assaypilot.data.schemas import AssayMapping, RawFileSpec
from assaypilot.domain import AssayRole, DataSource, SuccessCondition, Verdict, validate_public_campaign


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def fixture_cache(tmp_path: Path) -> Path:
    cache = tmp_path / "cache"
    shutil.copytree(EXAMPLES / "stage1_fixture", cache)
    return cache


def public_tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def linked_config():
    return load_config(EXAMPLES / "stage1_configs" / "synthetic_linked.json")


def test_offline_build_public_load_and_hidden_split(tmp_path: Path) -> None:
    report = build_campaign(linked_config(), fixture_cache(tmp_path), tmp_path / "bundle")
    public_root = tmp_path / "bundle" / "public"
    campaign = PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(public_root / "manifest.json")))

    assert validate_public_campaign(campaign).ok
    assert report.hidden_followup_measurements == 2
    assert {observation.assay_id for observation in campaign.observations} == {"primary-activity"}
    public_text = "\n".join(path.read_text() for path in public_root.rglob("*.json"))
    assert "FOLLOWUP_SECRET" not in public_text
    assert not (public_root / "curator").exists()


def test_developer_audit_counts_units_and_initial_snapshot_time(tmp_path: Path) -> None:
    config = linked_config()
    report = build_campaign(config, fixture_cache(tmp_path), tmp_path / "bundle")
    assert report.raw_rows_by_assay == {"primary-activity": 4, "confirm-activity": 2}
    assert report.included_rows_by_assay == {"primary-activity": 4, "confirm-activity": 2}
    assert report.candidate_counts["primary_active_rows"] == 2
    assert report.candidate_counts["primary_active_unique_sids"] == 2
    assert report.unique_sid_count == 4
    assert report.unique_cid_count == 4
    assert report.followup_candidate_sids == 2
    assert report.followup_candidate_assay_pairs == 2
    assert report.unmeasured_selected_candidates == 0
    campaign = PublicBundleAdapter().load(DataSource(
        kind="public_bundle", location=str(tmp_path / "bundle/public/manifest.json")))
    assert all(observation.released_at == config.initial_as_of for observation in campaign.observations)


def test_public_evidence_records_scope_without_exposing_raw_mapping(tmp_path: Path) -> None:
    build_campaign(linked_config(), fixture_cache(tmp_path), tmp_path / "bundle")
    evidence_files = sorted((tmp_path / "bundle" / "public" / "evidence").glob("*.json"))
    assert evidence_files
    evidence = [json.loads(path.read_text()) for path in evidence_files]
    assert all(item["endpoint_scope"] == "configured" for item in evidence)
    assert all(item["raw_outcome_column"] == "Activity Outcome" for item in evidence)
    assert all("raw_verdict_mapping" not in item for item in evidence)


def test_primary_observations_have_minimal_public_raw_traces(tmp_path: Path) -> None:
    cache = fixture_cache(tmp_path)
    build_campaign(linked_config(), cache, tmp_path / "bundle")
    public_root = tmp_path / "bundle" / "public"
    campaign = PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(public_root / "manifest.json")))
    evidence = {
        item["aid"]: item
        for item in (json.loads(path.read_text()) for path in (public_root / "evidence").glob("*.json"))
    }
    primary = evidence[101]
    traces = {item["observation_id"]: item for item in primary["observation_traces"]}
    assert set(traces) == {observation.observation_id for observation in campaign.observations}
    assert evidence[102]["observation_traces"] == []
    primary_sha256 = hashlib.sha256((cache / "primary_concise.csv").read_bytes()).hexdigest()
    for observation in campaign.observations:
        trace = traces[observation.observation_id]
        assert trace["aid"] == 101
        assert trace["raw_outcome"] == observation.raw_verdict
        assert trace["source_file_sha256"] == primary_sha256
        assert trace["raw_row"]["AID"] == "101"
        assert "Activity Value [uM]" in trace["raw_row"]
        assert trace["raw_row"].get("Activity Name", "") == ""
        assert trace["raw_row"]["SID"] == str(next(
            int(candidate.source_id.removeprefix("SID:"))
            for candidate in campaign.candidates if candidate.candidate_id == observation.candidate_id
        ))


def test_smiles_property_preserves_stereo_and_isotope_with_connectivity_kept_in_provenance(tmp_path: Path) -> None:
    build_campaign(linked_config(), fixture_cache(tmp_path), tmp_path / "bundle")
    campaign = PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(tmp_path / "bundle/public/manifest.json")))
    original_smiles = {candidate.original_smiles for candidate in campaign.candidates}
    assert "C[C@H](O)[13CH3]" in original_smiles
    assert "C[C@@H](O)[13CH3]" in original_smiles
    assert len(original_smiles) == 2
    provenance = json.loads((tmp_path / "bundle/curator/provenance.json").read_text())
    assert provenance["structure"]["candidate_original_smiles"]["property"] == "SMILES"
    assert provenance["structure"]["connectivity_reference"]["property"] == "ConnectivitySMILES"
    normalized = json.loads((tmp_path / "bundle/curator/normalized_measurements.json").read_text())
    assert next(item for item in normalized if item["cid"] == 2244)["original_smiles"] == "C[C@H](O)[13CH3]"
    assert next(item for item in normalized if item["cid"] == 1983)["original_smiles"] == "CC(N)O"


def test_connectivity_fallback_requires_explicit_policy_and_records_warning(tmp_path: Path) -> None:
    cache = fixture_cache(tmp_path)
    smiles = cache / "compounds_smiles.csv"
    smiles.write_text("\n".join(line for line in smiles.read_text().splitlines() if not line.startswith("5957,")) + "\n")
    config = linked_config().validated_replace(structure_selection_policy="smiles_then_connectivity_with_warning")
    report = build_campaign(config, cache, tmp_path / "bundle")
    assert any(issue.code == "connectivity_smiles_fallback" and issue.severity == "warning" for issue in report.issues)
    campaign = PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(tmp_path / "bundle/public/manifest.json")))
    assert any(candidate.original_smiles == "CC(C)O" for candidate in campaign.candidates)


def test_public_copy_loads_without_curator_files(tmp_path: Path) -> None:
    build_campaign(linked_config(), fixture_cache(tmp_path), tmp_path / "bundle")
    public_copy = tmp_path / "public"
    shutil.copytree(tmp_path / "bundle" / "public", public_copy)
    campaign = PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(public_copy / "manifest.json")))
    assert campaign.campaign.campaign_id == "stage1-synthetic-linked"


def test_followup_label_change_does_not_change_public_bundle(tmp_path: Path) -> None:
    first_cache = fixture_cache(tmp_path / "first")
    second_cache = fixture_cache(tmp_path / "second")
    followup = second_cache / "followup_concise.csv"
    followup.write_text(
        "AID,SID,CID,Activity Outcome,Activity Value [uM]\n"
        "102,1004,5957,FOLLOWUP_SECRET_NEGATIVE,0\n"
        "102,1001,2244,FOLLOWUP_SECRET_POSITIVE,3.5\n"
    )
    (second_cache / "followup_description.json").write_text('{"secret": "changed and removed from public"}\n')
    build_campaign(linked_config(), first_cache, tmp_path / "first-bundle")
    build_campaign(linked_config(), second_cache, tmp_path / "second-bundle")
    first_public = tmp_path / "first-bundle" / "public"
    second_public = tmp_path / "second-bundle" / "public"
    assert public_tree_hashes(first_public) == public_tree_hashes(second_public)


def test_removing_all_followup_rows_keeps_public_bundle_and_reports_missing(tmp_path: Path) -> None:
    first_cache = fixture_cache(tmp_path / "first")
    second_cache = fixture_cache(tmp_path / "second")
    (second_cache / "followup_concise.csv").write_text(
        "AID,SID,CID,Activity Outcome,Activity Value [uM]\n"
    )
    build_campaign(linked_config(), first_cache, tmp_path / "first-bundle")
    report = build_campaign(linked_config(), second_cache, tmp_path / "second-bundle")
    assert any(issue.code == "no_linked_followup" for issue in report.issues)
    assert public_tree_hashes(tmp_path / "first-bundle/public") == public_tree_hashes(tmp_path / "second-bundle/public")
    assert report.followup_candidate_sids == 0
    assert report.unmeasured_selected_candidates == 2


def test_pubchem_no_followup_builds_public_bundle_and_reports_missing(tmp_path: Path) -> None:
    """The PubChem severity branch accepts valid primary-only coverage."""
    first_cache = fixture_cache(tmp_path / "first")
    second_cache = fixture_cache(tmp_path / "second")
    (second_cache / "followup_concise.csv").write_text(
        "AID,SID,CID,Activity Outcome,Activity Value [uM]\n"
    )
    config = linked_config().validated_replace(data_kind="pubchem")
    build_campaign(config, first_cache, tmp_path / "first-bundle")
    report = build_campaign(config, second_cache, tmp_path / "second-bundle")
    assert not report.has_errors
    assert any(issue.code == "no_linked_followup" and issue.severity == "warning"
               for issue in report.issues)
    campaign = PublicBundleAdapter().load(DataSource(
        kind="public_bundle", location=str(tmp_path / "second-bundle/public/manifest.json")))
    assert len(campaign.candidates) == 2
    assert report.followup_candidate_sids == 0
    assert report.unmeasured_selected_candidates == 2
    assert public_tree_hashes(tmp_path / "first-bundle/public") == public_tree_hashes(
        tmp_path / "second-bundle/public")


def test_pubchem_missing_followup_file_is_not_treated_as_zero_coverage(tmp_path: Path) -> None:
    config = linked_config().validated_replace(data_kind="pubchem")
    cache = fixture_cache(tmp_path)
    (cache / "followup_concise.csv").unlink()
    with pytest.raises(FileNotFoundError):
        build_campaign(config, cache, tmp_path / "bundle")


def test_counter_assay_active_meaning_survives_build_serialization_and_load(tmp_path: Path) -> None:
    """Counter active is retained as configured meaning; no success engine is implied."""
    cache = fixture_cache(tmp_path)
    (cache / "counter_concise.csv").write_text(
        "AID,SID,CID,Activity Outcome,Activity Value [uM]\n"
        "103,1001,2244,CounterActive,\n"
        "103,1004,5957,CounterInactive,\n"
    )
    base = linked_config()
    counter = base.assays[1].validated_replace(
        assay_id="counter-activity", aid=103, name="Synthetic counter assay",
        role=AssayRole.COUNTER, endpoint="Counter activity outcome",
        verdict_mapping={"CounterActive": "active", "CounterInactive": "inactive"},
        prerequisites=[{"assay_id": "primary-activity", "kind": "verdict", "verdict": "active"}],
        concise_cache_key="counter_concise.csv", description_cache_key="counter_description.json",
    )
    (cache / "counter_description.json").write_text('{"description":"synthetic counter"}\n')
    config = base.validated_replace(
        assays=[base.assays[0], counter],
        success_conditions=[SuccessCondition(
            assay_id="counter-activity", meaning="counter active is the configured condition",
            kind="verdict", verdict=Verdict.ACTIVE)],
        raw_files=[*base.raw_files,
                   RawFileSpec(key="counter_concise.csv", request_path="fixture/counter/concise.csv", format="csv"),
                   RawFileSpec(key="counter_description.json", request_path="fixture/counter/description.json", format="json")],
    )
    report = build_campaign(config, cache, tmp_path / "bundle")
    assert report.hidden_followup_measurements == 2
    campaign = PublicBundleAdapter().load(DataSource(
        kind="public_bundle", location=str(tmp_path / "bundle/public/manifest.json")))
    counter_spec = next(assay for assay in campaign.assays if assay.assay_id == "counter-activity")
    assert counter_spec.role is AssayRole.COUNTER
    assert counter_spec.verdict_meaning[Verdict.ACTIVE] == "configured active"
    assert [(condition.assay_id, condition.verdict) for condition in campaign.campaign.success_conditions] == [
        ("counter-activity", Verdict.ACTIVE)]
    normalized = json.loads((tmp_path / "bundle/curator/normalized_measurements.json").read_text())
    assert {row["assay_id"] for row in normalized} == {"primary-activity", "counter-activity"}


def test_primary_only_configuration_records_warning_without_claiming_followup(tmp_path: Path) -> None:
    config = load_config(EXAMPLES / "stage1_configs" / "synthetic_primary_only.json")
    report = build_campaign(config, fixture_cache(tmp_path), tmp_path / "bundle")
    assert not report.has_errors
    assert any(issue.code == "no_linked_followup" and issue.severity == "warning" for issue in report.issues)
    hidden = json.loads((tmp_path / "bundle" / "curator" / "hidden_followup_measurements.json").read_text())
    assert hidden == []


def test_fetch_structure_selection_uses_same_primary_only_rule(tmp_path: Path) -> None:
    config = linked_config().validated_replace(
        structure_fetch_from_primary=True,
        candidate_rule=linked_config().candidate_rule.validated_replace(limit=2),
    )
    cids = _primary_structure_cids(config, fixture_cache(tmp_path))
    assert len(cids) == 2
    assert set(cids) == {2244, 5957}


def test_real_pubchem_snapshot_configuration_is_versioned_and_explicit() -> None:
    config = load_config(EXAMPLES / "stage1_configs" / "pubchem_tor_mep2_snapshot.json")
    assert config.data_kind == "pubchem"
    assert [(assay.aid, assay.role.value) for assay in config.assays] == [(2016, "primary"), (2272, "confirmatory")]
    assert config.candidate_rule.include == "primary_active"
    assert all(assay.endpoint_scope == "categorical_activity_outcome_only" for assay in config.assays)
    assert all(assay.unit == "categorical" for assay in config.assays)
    assert all(assay.raw_endpoint_column is None and assay.raw_unit is None for assay in config.assays)
    assert all("not emitted by this checked snapshot" not in text
               for assay in config.assays for text in assay.verdict_meaning.values())
    assert config.assays[0].official_result_names == ["RESPONSE", "Z_PRIME"]
    assert config.assays[1].official_result_names[-2:] == ["RESPONSE_MOTHERS", "RESPONSE_DAUGHTERS"]


def test_bad_aid_or_undefined_outcome_stops_build_without_output(tmp_path: Path) -> None:
    cache = fixture_cache(tmp_path)
    concise = cache / "primary_concise.csv"
    concise.write_text(concise.read_text().replace("101,1001", "999,1001").replace("Inactive", "Unexpected"))
    output = tmp_path / "bundle"
    with pytest.raises(BuildError) as raised:
        build_campaign(linked_config(), cache, output)
    assert not output.exists()
    assert {issue.code for issue in raised.value.report.issues} == {"normalization_error"}


@pytest.mark.parametrize(("raw", "value", "comparison"), [("0", 0.0, "="), ("<=0", 0.0, "<="), ("> 1e-3", 0.001, ">")])
def test_numeric_parser_preserves_zero_and_comparators(raw: str, value: float, comparison: str) -> None:
    parsed_value, parsed_comparison = parse_numeric(raw)
    assert parsed_value == value
    assert parsed_comparison.value == comparison


def test_normalizer_rejects_unknown_verdict_and_missing_sid() -> None:
    mapping = linked_config().assays[0]
    common = dict(AID="101", SID="1001", CID="2244", **{"Activity Outcome": "Unknown", "Activity Value [uM]": "0"})
    with pytest.raises(NormalizationError, match="undefined raw verdict"):
        normalize_concise_row(common, mapping, source_file_sha256="a" * 64, smiles_by_cid={2244: "CC"}, row_number=2)
    common["Activity Outcome"] = "Active"
    common["SID"] = ""
    with pytest.raises(NormalizationError, match="invalid SID"):
        normalize_concise_row(common, mapping, source_file_sha256="a" * 64, smiles_by_cid={2244: "CC"}, row_number=2)


@pytest.mark.parametrize("raw", [True, float("nan"), float("inf"), "NaN", "Infinity", "one uM"])
def test_numeric_parser_rejects_bool_nonfinite_and_ambiguous_values(raw: object) -> None:
    with pytest.raises(NormalizationError):
        parse_numeric(raw)


def test_normalizer_keeps_numeric_only_as_unspecified_and_rejects_blank_row() -> None:
    mapping = linked_config().assays[0]
    numeric_only = {"AID": "101", "SID": "1001", "CID": "2244", "Activity Outcome": "", "Activity Value [uM]": "0"}
    measurement = normalize_concise_row(numeric_only, mapping, source_file_sha256="a" * 64, smiles_by_cid={2244: "CC"}, row_number=2)
    assert measurement.verdict is Verdict.UNSPECIFIED and measurement.value == 0 and measurement.raw_verdict is None
    with pytest.raises(NormalizationError, match="fully blank"):
        normalize_concise_row({key: "" for key in numeric_only}, mapping, source_file_sha256="a" * 64, smiles_by_cid={}, row_number=3)


def test_category_only_policy_rejects_activity_name_and_numeric_values() -> None:
    mapping = linked_config().assays[0].validated_replace(
        endpoint_scope="categorical_activity_outcome_only",
        endpoint="PubChem Activity Outcome",
        unit="categorical",
        raw_endpoint_column=None,
        raw_unit=None,
        activity_name_policy="blank_in_concise",
    )
    valid = {
        "AID": "101", "SID": "1001", "CID": "2244", "Activity Outcome": "Active",
        "Activity Value [uM]": "", "Activity Name": "",
    }
    measurement = normalize_concise_row(
        valid, mapping, source_file_sha256="a" * 64, smiles_by_cid={2244: "CC"}, row_number=2
    )
    assert measurement.value is None and measurement.unit is None
    named = dict(valid, **{"Activity Name": "RESPONSE"})
    with pytest.raises(NormalizationError, match="Activity Name is populated"):
        normalize_concise_row(named, mapping, source_file_sha256="a" * 64, smiles_by_cid={2244: "CC"}, row_number=2)
    numeric = dict(valid, **{"Activity Value [uM]": "10"})
    with pytest.raises(NormalizationError, match="contains numeric endpoint"):
        normalize_concise_row(numeric, mapping, source_file_sha256="a" * 64, smiles_by_cid={2244: "CC"}, row_number=2)


def test_category_only_schema_rejects_numeric_unit() -> None:
    with pytest.raises(ValueError, match="categorical assay unit"):
        linked_config().assays[0].validated_replace(
            endpoint_scope="categorical_activity_outcome_only",
            raw_endpoint_column=None,
            raw_unit=None,
        )


def test_sid_cid_conflict_stops_build(tmp_path: Path) -> None:
    cache = fixture_cache(tmp_path)
    primary = cache / "primary_concise.csv"
    primary.write_text(primary.read_text() + "101,1001,1983,Active,1\n")
    with pytest.raises(BuildError) as raised:
        build_campaign(linked_config(), cache, tmp_path / "bundle")
    assert any(issue.code == "sid_cid_conflict" for issue in raised.value.report.issues)


def test_repeated_and_conflicting_rows_remain_distinct_measurements(tmp_path: Path) -> None:
    cache = fixture_cache(tmp_path)
    primary = cache / "primary_concise.csv"
    primary.write_text(primary.read_text() + "101,1001,2244,Inactive,0\n")
    config = linked_config().validated_replace(
        candidate_rule=linked_config().candidate_rule.validated_replace(limit=4)
    )
    report = build_campaign(config, cache, tmp_path / "bundle")
    assert report.repeated_assay_sid_groups == 1
    assert report.conflicting_assay_sid_groups == 1
    normalized = json.loads((tmp_path / "bundle/curator/normalized_measurements.json").read_text())
    assert sum(item["sid"] == 1001 and item["assay_id"] == "primary-activity" for item in normalized) == 2
    public = PublicBundleAdapter().load(DataSource(
        kind="public_bundle", location=str(tmp_path / "bundle/public/manifest.json")))
    sid1001_candidate = next(candidate.candidate_id for candidate in public.candidates
                             if candidate.source_id == "SID:1001")
    assert sum(observation.candidate_id == sid1001_candidate for observation in public.observations) == 2
    assert len(public.observations) == 3


def test_adapter_rejects_tampered_public_file(tmp_path: Path) -> None:
    build_campaign(linked_config(), fixture_cache(tmp_path), tmp_path / "bundle")
    campaign_file = tmp_path / "bundle" / "public" / "campaign.json"
    campaign_file.write_text(campaign_file.read_text().replace("SYNTHETIC_TARGET", "TAMPERED_TARGET"))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(tmp_path / "bundle/public/manifest.json")))


def test_adapter_rejects_unsupported_public_manifest_version(tmp_path: Path) -> None:
    build_campaign(linked_config(), fixture_cache(tmp_path), tmp_path / "bundle")
    manifest_path = tmp_path / "bundle/public/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["schema_version"] = "9.9.9"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="unsupported public manifest"):
        PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(manifest_path)))


def test_auditor_uses_actual_rdkit_to_sanitize_fixture_smiles(tmp_path: Path) -> None:
    build_campaign(linked_config(), fixture_cache(tmp_path), tmp_path / "bundle")
    campaign = PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(tmp_path / "bundle/public/manifest.json")))
    auditor = PublicCampaignAuditor()
    audit = auditor.audit(campaign)
    assert audit.ok
    assert auditor.chemical_summary is not None
    assert auditor.chemical_summary.backend == "rdkit"
    assert (auditor.chemical_summary.checked, auditor.chemical_summary.passed, auditor.chemical_summary.failed) == (2, 2, 0)


def test_auditor_reports_invalid_smiles_with_actual_rdkit(tmp_path: Path) -> None:
    build_campaign(linked_config(), fixture_cache(tmp_path), tmp_path / "bundle")
    campaign = PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(tmp_path / "bundle/public/manifest.json")))
    bad_candidate = campaign.candidates[0].validated_replace(original_smiles="not a smiles")
    bad_campaign = campaign.validated_replace(candidates=[bad_candidate, *campaign.candidates[1:]])
    auditor = PublicCampaignAuditor()
    audit = auditor.audit(bad_campaign)
    assert any(issue.code == "invalid_smiles" and issue.target_id == bad_candidate.candidate_id for issue in audit.issues)
    assert auditor.chemical_summary is not None and auditor.chemical_summary.failed == 1


def test_auditor_keeps_rdkit_unavailable_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_campaign(linked_config(), fixture_cache(tmp_path), tmp_path / "bundle")
    campaign = PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(tmp_path / "bundle/public/manifest.json")))
    monkeypatch.setattr("assaypilot.data.audit._load_rdkit_chem", lambda: None)
    auditor = PublicCampaignAuditor()
    audit = auditor.audit(campaign)
    assert any(issue.code == "rdkit_unavailable" for issue in audit.issues)
    assert auditor.chemical_summary is not None and auditor.chemical_summary.backend == "unavailable"


def test_cache_reuse_needs_no_http_client(tmp_path: Path) -> None:
    cache = tmp_path / "response.csv"
    raw = b"AID,SID,CID,Activity Outcome\n101,1,2,Active\n"
    cache.write_bytes(raw)
    cache.with_suffix(".csv.meta.json").write_text(json.dumps({"request_path": "assay/aid/101/concise/CSV", "sha256": hashlib.sha256(raw).hexdigest()}))
    cached = PubChemClient().fetch("assay/aid/101/concise/CSV", cache)
    assert cached.reused and cached.sha256 == hashlib.sha256(raw).hexdigest()


def test_cache_with_a_different_request_path_is_not_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "response.csv"
    cache.write_bytes(b"old")
    cache.with_suffix(".csv.meta.json").write_text(json.dumps({"request_path": "old/path.csv", "sha256": hashlib.sha256(b"old").hexdigest()}))

    class HTTPError(Exception):
        pass

    class Response:
        status_code = 200
        content = b"AID,SID,CID,Activity Outcome\n101,1,2,Active\n"
        headers = {"content-type": "text/csv"}

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=lambda *_args, **_kwargs: Response(), HTTPError=HTTPError))
    fetched = PubChemClient().fetch("assay/aid/101/concise/CSV", cache)
    assert not fetched.reused


def test_connectivity_cache_is_not_reused_for_smiles_property_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "compound.csv"
    connectivity = b"CID,ConnectivitySMILES\n2244,CC(C)O\n"
    cache.write_bytes(connectivity)
    cache.with_suffix(".csv.meta.json").write_text(json.dumps({
        "request_path": "compound/cid/2244/property/ConnectivitySMILES/CSV",
        "sha256": hashlib.sha256(connectivity).hexdigest(),
    }))

    class HTTPError(Exception):
        pass

    class Response:
        status_code = 200
        content = b"CID,SMILES\n2244,C[C@H](O)[13CH3]\n"
        headers = {"content-type": "text/csv"}

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=lambda *_args, **_kwargs: Response(), HTTPError=HTTPError))
    response = PubChemClient().fetch("compound/cid/2244/property/SMILES/CSV", cache)
    assert not response.reused
    assert b"SMILES" in cache.read_bytes()


def test_fetch_retries_transient_error_then_writes_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class Response:
        def __init__(self, status_code: int, content: bytes) -> None:
            self.status_code, self.content = status_code, content
            self.headers = {"content-type": "text/csv"}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise HTTPError("bad status")

    class HTTPError(Exception):
        pass

    def get(*_args, **_kwargs):
        calls.append(1)
        return Response(503 if len(calls) == 1 else 200, b"AID,SID,CID,Activity Outcome\n101,1,2,Active\n")

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=get, HTTPError=HTTPError))
    monkeypatch.setattr("assaypilot.data.pubchem.time.sleep", lambda _seconds: None)
    response = PubChemClient(retries=1).fetch("assay/aid/101/concise/CSV", tmp_path / "download.csv")
    assert not response.reused and len(calls) == 2
    assert (tmp_path / "download.csv.meta.json").is_file()


def test_fetch_retry_after_decimal_and_http_date_are_honored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    calls: list[int] = []

    class HTTPError(Exception):
        pass

    class Response:
        def __init__(self, status_code: int, retry_after: str | None = None) -> None:
            self.status_code = status_code
            self.content = b"AID,SID,CID,Activity Outcome\n101,1,2,Active\n"
            self.headers = {"content-type": "text/csv"}
            if retry_after is not None:
                self.headers["Retry-After"] = retry_after

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise HTTPError("bad status")

    def get(*_args, **_kwargs):
        calls.append(1)
        return Response(429 if len(calls) == 1 else 200, "0.25")

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=get, HTTPError=HTTPError))
    monkeypatch.setattr("assaypilot.data.pubchem.time.sleep", lambda seconds: waits.append(seconds))
    PubChemClient(retries=1).fetch("assay/aid/101/concise/CSV", tmp_path / "download.csv")
    assert calls == [1, 1] and any(0.24 <= wait <= 0.26 for wait in waits)


def test_fetch_failure_after_file_write_preserves_previous_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "download.csv"
    old_raw = b"AID,SID,CID,Activity Outcome\n101,1,2,Inactive\n"
    old_meta = {"request_path": "assay/aid/101/concise/CSV", "sha256": hashlib.sha256(old_raw).hexdigest()}
    cache.write_bytes(old_raw)
    cache.with_suffix(".csv.meta.json").write_text(json.dumps(old_meta))

    class HTTPError(Exception):
        pass

    class Response:
        status_code = 200
        content = b"AID,SID,CID,Activity Outcome\n101,1,2,Active\n"
        headers = {"content-type": "text/csv"}

        def raise_for_status(self) -> None:
            return None

    original_replace = Path.replace

    def fail_meta_replace(self: Path, target: Path):
        if self.name.endswith(".meta.json.partial"):
            raise OSError("simulated metadata write failure")
        return original_replace(self, target)

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=lambda *_args, **_kwargs: Response(), HTTPError=HTTPError))
    monkeypatch.setattr("assaypilot.data.pubchem.Path.replace", fail_meta_replace)
    monkeypatch.setattr("assaypilot.data.pubchem.time.sleep", lambda _seconds: None)
    with pytest.raises(FetchError):
        PubChemClient(retries=1).fetch("assay/aid/101/concise/CSV", cache, refresh=True)
    assert cache.read_bytes() == old_raw
    assert json.loads(cache.with_suffix(".csv.meta.json").read_text()) == old_meta
    assert not list(tmp_path.glob("*.partial")) and not list(tmp_path.glob("*.backup"))


def test_fetch_failure_before_replacement_preserves_previous_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "download.csv"
    old_raw = b"AID,SID,CID,Activity Outcome\n101,1,2,Inactive\n"
    old_meta = {"request_path": "assay/aid/101/concise/CSV", "sha256": hashlib.sha256(old_raw).hexdigest()}
    cache.write_bytes(old_raw)
    cache.with_suffix(".csv.meta.json").write_text(json.dumps(old_meta))

    original_write_bytes = Path.write_bytes

    def fail_temporary_write(self: Path, data: bytes) -> int:
        if self.name.endswith(".partial"):
            raise OSError("simulated response write failure")
        return original_write_bytes(self, data)

    monkeypatch.setattr("assaypilot.data.pubchem.Path.write_bytes", fail_temporary_write)
    with pytest.raises(OSError, match="simulated response write failure"):
        PubChemClient()._atomic_cache_write(cache, b"new", {"sha256": "new"})
    assert cache.read_bytes() == old_raw
    assert json.loads(cache.with_suffix(".csv.meta.json").read_text()) == old_meta
    assert not list(tmp_path.glob("*.partial")) and not list(tmp_path.glob("*.backup"))


@pytest.mark.parametrize("content", [b"", b"<html>temporary error</html>"])
def test_fetch_rejects_empty_html_and_incomplete_responses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes) -> None:
    class HTTPError(Exception):
        pass

    class Response:
        status_code = 200
        headers = {"content-type": "text/csv"}

        def __init__(self) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=lambda *_args, **_kwargs: Response(), HTTPError=HTTPError))
    monkeypatch.setattr("assaypilot.data.pubchem.time.sleep", lambda _seconds: None)
    with pytest.raises(FetchError):
        PubChemClient(retries=0).fetch("assay/aid/101/concise/CSV", tmp_path / "download.csv")
    assert not (tmp_path / "download.csv").exists()


def test_fetch_timeout_is_finite_and_does_not_create_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class HTTPError(Exception):
        pass

    def get(*_args, **_kwargs):
        calls.append(1)
        raise HTTPError("timeout")

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=get, HTTPError=HTTPError))
    monkeypatch.setattr("assaypilot.data.pubchem.time.sleep", lambda _seconds: None)
    with pytest.raises(FetchError):
        PubChemClient(retries=1).fetch("assay/aid/101/concise/CSV", tmp_path / "download.csv")
    assert len(calls) == 2 and not (tmp_path / "download.csv").exists()


def test_parse_concise_csv_rejects_incomplete_headers() -> None:
    with pytest.raises(FetchError):
        parse_concise_csv(b"AID,SID\n101,1\n")


def test_build_failure_after_output_started_leaves_no_partial_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import assaypilot.data.build as build_module

    original_write = build_module._write_json
    calls = 0

    def fail_after_two(path: Path, value: object) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated bundle write failure")
        return original_write(path, value)

    monkeypatch.setattr(build_module, "_write_json", fail_after_two)
    output = tmp_path / "bundle"
    with pytest.raises(OSError):
        build_campaign(linked_config(), fixture_cache(tmp_path), output)
    assert not output.exists()


def test_build_refuses_to_damage_existing_successful_bundle(tmp_path: Path) -> None:
    output = tmp_path / "bundle"
    build_campaign(linked_config(), fixture_cache(tmp_path / "first"), output)
    before = public_tree_hashes(output / "public")
    with pytest.raises(FileExistsError):
        build_campaign(linked_config(), fixture_cache(tmp_path / "second"), output)
    assert public_tree_hashes(output / "public") == before


def test_fetch_accepts_json_content_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class HTTPError(Exception):
        pass

    class Response:
        status_code = 200
        content = b'{"PC_AssayContainer": []}'
        headers = {"content-type": "application/json; charset=utf-8"}

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=lambda *_args, **_kwargs: Response(), HTTPError=HTTPError))
    response = PubChemClient().fetch("assay/aid/1/description/JSON", tmp_path / "description.json")
    assert response.path.read_bytes() == Response.content
