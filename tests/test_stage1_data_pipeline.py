"""1단계 캐시·정규화·공개 bundle 경계를 검증한다."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.data.audit import PublicCampaignAuditor
from assaypilot.data.build import BuildError, build_campaign, load_config
from assaypilot.data.normalize import NormalizationError, normalize_concise_row, parse_numeric
from assaypilot.data.pubchem import FetchError, PubChemClient
from assaypilot.data.schemas import AssayMapping
from assaypilot.domain import DataSource, Verdict, validate_public_campaign


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def fixture_cache(tmp_path: Path) -> Path:
    cache = tmp_path / "cache"
    shutil.copytree(EXAMPLES / "stage1_fixture", cache)
    return cache


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
    followup.write_text(followup.read_text().replace("FOLLOWUP_SECRET_POSITIVE", "FOLLOWUP_SECRET_NEGATIVE"))
    build_campaign(linked_config(), first_cache, tmp_path / "first-bundle")
    build_campaign(linked_config(), second_cache, tmp_path / "second-bundle")
    first_public = tmp_path / "first-bundle" / "public"
    second_public = tmp_path / "second-bundle" / "public"
    assert (first_public / "campaign.json").read_bytes() == (second_public / "campaign.json").read_bytes()
    assert (first_public / "manifest.json").read_bytes() == (second_public / "manifest.json").read_bytes()


def test_primary_only_configuration_records_warning_without_claiming_followup(tmp_path: Path) -> None:
    config = load_config(EXAMPLES / "stage1_configs" / "synthetic_primary_only.json")
    report = build_campaign(config, fixture_cache(tmp_path), tmp_path / "bundle")
    assert not report.has_errors
    assert any(issue.code == "no_linked_followup" and issue.severity == "warning" for issue in report.issues)
    hidden = json.loads((tmp_path / "bundle" / "curator" / "hidden_followup_measurements.json").read_text())
    assert hidden == []


def test_real_pubchem_snapshot_configuration_is_versioned_and_explicit() -> None:
    config = load_config(EXAMPLES / "stage1_configs" / "pubchem_tor_mep2_snapshot.json")
    assert config.data_kind == "pubchem"
    assert [(assay.aid, assay.role.value) for assay in config.assays] == [(2016, "primary"), (2272, "confirmatory")]
    assert config.candidate_rule.include == "primary_active"


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


def test_sid_cid_conflict_stops_build(tmp_path: Path) -> None:
    cache = fixture_cache(tmp_path)
    primary = cache / "primary_concise.csv"
    primary.write_text(primary.read_text() + "101,1001,1983,Active,1\n")
    with pytest.raises(BuildError) as raised:
        build_campaign(linked_config(), cache, tmp_path / "bundle")
    assert any(issue.code == "sid_cid_conflict" for issue in raised.value.report.issues)


def test_adapter_rejects_tampered_public_file(tmp_path: Path) -> None:
    build_campaign(linked_config(), fixture_cache(tmp_path), tmp_path / "bundle")
    campaign_file = tmp_path / "bundle" / "public" / "campaign.json"
    campaign_file.write_text(campaign_file.read_text().replace("SYNTHETIC_TARGET", "TAMPERED_TARGET"))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(tmp_path / "bundle/public/manifest.json")))


def test_auditor_reports_rdkit_unavailable_or_invalid_smiles(tmp_path: Path) -> None:
    build_campaign(linked_config(), fixture_cache(tmp_path), tmp_path / "bundle")
    campaign = PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(tmp_path / "bundle/public/manifest.json")))
    audit = PublicCampaignAuditor().audit(campaign)
    assert any(issue.code == "rdkit_unavailable" for issue in audit.issues)


def test_auditor_reports_invalid_smiles_when_rdkit_is_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_campaign(linked_config(), fixture_cache(tmp_path), tmp_path / "bundle")
    campaign = PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(tmp_path / "bundle/public/manifest.json")))
    bad_candidate = campaign.candidates[0].validated_replace(original_smiles="not a smiles")
    bad_campaign = campaign.validated_replace(candidates=[bad_candidate, *campaign.candidates[1:]])
    fake_rdkit = ModuleType("rdkit")
    fake_rdkit.Chem = SimpleNamespace(MolFromSmiles=lambda smiles: None if smiles == "not a smiles" else object())
    monkeypatch.setitem(sys.modules, "rdkit", fake_rdkit)
    audit = PublicCampaignAuditor().audit(bad_campaign)
    assert any(issue.code == "invalid_smiles" and issue.target_id == bad_candidate.candidate_id for issue in audit.issues)


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
