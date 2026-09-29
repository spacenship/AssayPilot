"""Trusted internal lookup of fixed-snapshot follow-up measurements."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.data.build import build_campaign, load_config
from assaypilot.domain import AssayRole, DataSource, Verdict
from assaypilot.replay import (
    ReplayLookupResult,
    ReplayLoadError,
    ReplayOracle,
    ReplayRequestError,
    _candidate_sid_index,
    load_replay_store,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
HIDDEN = "bundle/curator/hidden_followup_measurements.json"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _refresh_snapshot_manifest(root: Path) -> None:
    inventory = {
        path.relative_to(root).as_posix(): _sha(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "snapshot_manifest.json"
    }
    manifest_path = root / "snapshot_manifest.json"
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    manifest_path.write_text(json.dumps({
        "snapshot_id": old.get("snapshot_id", "synthetic-replay-r1"),
        "campaign_id": old.get("campaign_id", "stage1-synthetic-linked"),
        "full_sha256": inventory,
    }, indent=2, sort_keys=True) + "\n")


@pytest.fixture
def replay_snapshot(tmp_path: Path) -> tuple[Path, object]:
    """Build an explicitly synthetic, offline bundle for replay contract tests."""
    config_path = EXAMPLES / "stage1_configs" / "synthetic_linked.json"
    config = load_config(config_path)
    cache = tmp_path / "cache"
    shutil.copytree(EXAMPLES / "stage1_fixture", cache)
    root = tmp_path / "snapshot"
    root.mkdir()
    shutil.copytree(cache, root / "raw")
    build_campaign(config, cache, root / "bundle")
    config_dir = root / "config"
    config_dir.mkdir()
    shutil.copyfile(config_path, config_dir / "synthetic_linked.json")
    _refresh_snapshot_manifest(root)
    campaign = PublicBundleAdapter().load(DataSource(
        kind="public_bundle", location=str(root / "bundle/public/manifest.json")
    ))
    return root, campaign


def _oracle(root: Path, campaign: object) -> ReplayOracle:
    return ReplayOracle(load_replay_store(root, campaign))


def _read_hidden(root: Path) -> list[dict[str, object]]:
    return json.loads((root / HIDDEN).read_text())


def _write_hidden(root: Path, rows: list[dict[str, object]]) -> None:
    (root / HIDDEN).write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    _refresh_snapshot_manifest(root)


def _candidate_for_sid(campaign: object, sid: int) -> str:
    matches = [item.candidate_id for item in campaign.candidates
               if item.source == "pubchem_sid" and item.source_id == f"SID:{sid}"]
    assert len(matches) == 1
    return matches[0]


def test_lookup_preserves_source_measurement_and_returns_a_defensive_copy(replay_snapshot) -> None:
    root, campaign = replay_snapshot
    candidate_id = _candidate_for_sid(campaign, 1001)
    before_campaign = campaign.model_dump(mode="json")
    oracle = _oracle(root, campaign)

    first = oracle.lookup(candidate_id, "confirm-activity")
    assert first.status == "records_found"
    assert len(first.measurements) == 1
    measurement = first.measurements[0]
    assert (measurement.sid, measurement.aid, measurement.raw_verdict) == (
        1001, 102, "FOLLOWUP_SECRET_POSITIVE"
    )
    assert (measurement.verdict, measurement.value, measurement.unit, measurement.comparison.value) == (
        Verdict.ACTIVE, 3.5, "uM", "="
    )
    assert measurement.raw_row["SID"] == "1001"
    assert measurement.source_row_id and measurement.source_file_sha256

    measurement.raw_row["SID"] = "9999"
    second = oracle.lookup(candidate_id, "confirm-activity")
    assert second.measurements[0].raw_row["SID"] == "1001"
    assert campaign.model_dump(mode="json") == before_campaign


def test_empty_valid_hidden_array_is_no_record_and_bad_requests_are_distinct(replay_snapshot) -> None:
    root, campaign = replay_snapshot
    _write_hidden(root, [])
    oracle = _oracle(root, campaign)
    candidate_id = _candidate_for_sid(campaign, 1001)
    result = oracle.lookup(candidate_id, "confirm-activity")
    assert result.status == "no_record" and result.measurements == ()
    assert not hasattr(result, "coverage")
    assert not hasattr(result, "candidate_statuses")

    with pytest.raises(ReplayRequestError, match="unknown candidate_id"):
        oracle.lookup("candidate-does-not-exist", "confirm-activity")
    with pytest.raises(ReplayRequestError, match="not a supported follow-up"):
        oracle.lookup(candidate_id, "primary-activity")
    with pytest.raises(ReplayRequestError, match="not a supported follow-up"):
        oracle.lookup(candidate_id, "unknown-assay")


def test_repeated_conflicting_rows_are_retained_in_stable_measurement_id_order(replay_snapshot) -> None:
    root, campaign = replay_snapshot
    rows = _read_hidden(root)
    repeated = json.loads(json.dumps(rows[0]))
    repeated.update({
        "measurement_id": "measurement-conflicting-repeat",
        "raw_verdict": "FOLLOWUP_SECRET_NEGATIVE",
        "verdict": "inactive",
        "value": 0.0,
        "comparison": "=",
        "source_row_id": "row-conflicting-repeat",
        "source_row_number": 99,
        "raw_row": {**repeated["raw_row"], "Activity Outcome": "FOLLOWUP_SECRET_NEGATIVE",
                    "Activity Value [uM]": "0"},
    })
    rows.append(repeated)
    _write_hidden(root, rows)
    first = _oracle(root, campaign).lookup(_candidate_for_sid(campaign, 1001), "confirm-activity")
    _write_hidden(root, list(reversed(rows)))
    second = _oracle(root, campaign).lookup(_candidate_for_sid(campaign, 1001), "confirm-activity")

    assert [item.measurement_id for item in first.measurements] == [
        item.measurement_id for item in second.measurements
    ]
    assert {item.verdict for item in first.measurements} == {Verdict.ACTIVE, Verdict.INACTIVE}
    assert len(first.measurements) == 2


def test_candidate_sid_mapping_keeps_same_cid_rows_separate(replay_snapshot) -> None:
    root, campaign = replay_snapshot
    rows = _read_hidden(root)
    sid_1004 = next(item for item in rows if item["sid"] == 1004)
    sid_1004["cid"] = 2244
    sid_1004["raw_row"]["CID"] = "2244"
    _write_hidden(root, rows)
    oracle = _oracle(root, campaign)

    result_1001 = oracle.lookup(_candidate_for_sid(campaign, 1001), "confirm-activity")
    result_1004 = oracle.lookup(_candidate_for_sid(campaign, 1004), "confirm-activity")
    assert result_1001.measurements[0].sid == 1001
    assert result_1004.measurements[0].sid == 1004
    assert result_1001.measurements[0].cid == result_1004.measurements[0].cid == 2244
    assert result_1004.measurements[0].value == 0.0
    assert result_1004.measurements[0].comparison.value == "="


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("duplicate_candidate_id", "duplicate_candidate_id"),
        ("duplicate_sid", "duplicate_candidate_sid"),
        ("unsupported_source", "unsupported"),
        ("malformed_source_id", "invalid_candidate_source_id"),
    ],
)
def test_candidate_identifier_mapping_conflicts_are_rejected(replay_snapshot, mutation, code) -> None:
    _, campaign = replay_snapshot
    first = campaign.candidates[0]
    if mutation == "duplicate_candidate_id":
        duplicate = first.model_copy(update={"candidate_id": campaign.candidates[1].candidate_id})
    elif mutation == "duplicate_sid":
        duplicate = first.model_copy(update={"candidate_id": "candidate-sid-alias"})
    elif mutation == "unsupported_source":
        duplicate = first.model_copy(update={"candidate_id": "candidate-unsupported", "source": "cid"})
    else:
        duplicate = first.model_copy(update={"candidate_id": "candidate-malformed", "source_id": "SID:01001"})
    changed = campaign.model_copy(update={"candidates": [*campaign.candidates, duplicate]})

    with pytest.raises(ReplayLoadError) as error:
        _candidate_sid_index(changed)
    assert error.value.code == code


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("hidden_hash", "hash_mismatch"),
        ("missing_hidden", "snapshot_file_unavailable"),
        ("unregistered_hidden", "unregistered_file"),
        ("invalid_json", "invalid_measurements"),
        ("unsafe_path", "invalid_manifest_path"),
        ("other_inventory_hash", "hash_mismatch"),
        ("tampered_source", "source_hash_mismatch"),
    ],
)
def test_inventory_and_snapshot_failures_are_not_no_record(replay_snapshot, mutation, code) -> None:
    root, campaign = replay_snapshot
    hidden = root / HIDDEN
    if mutation in {"hidden_hash", "invalid_json"}:
        hidden.write_text("{broken json\n")
        if mutation == "invalid_json":
            manifest = json.loads((root / "snapshot_manifest.json").read_text())
            manifest["full_sha256"][HIDDEN] = _sha(hidden.read_bytes())
            (root / "snapshot_manifest.json").write_text(json.dumps(manifest))
    elif mutation == "missing_hidden":
        hidden.unlink()
    elif mutation == "unregistered_hidden":
        manifest = json.loads((root / "snapshot_manifest.json").read_text())
        manifest["full_sha256"].pop(HIDDEN)
        (root / "snapshot_manifest.json").write_text(json.dumps(manifest))
    elif mutation == "unsafe_path":
        manifest = json.loads((root / "snapshot_manifest.json").read_text())
        manifest["full_sha256"]["../outside.json"] = "0" * 64
        (root / "snapshot_manifest.json").write_text(json.dumps(manifest))
    elif mutation == "other_inventory_hash":
        (root / "bundle/curator/data_audit_report.json").write_text("tampered\n")
    elif mutation == "tampered_source":
        raw_source = root / "raw/followup_concise.csv"
        raw_source.write_text(raw_source.read_text() + "\n")
        _refresh_snapshot_manifest(root)

    with pytest.raises(ReplayLoadError) as error:
        load_replay_store(root, campaign)
    assert error.value.code == code


def test_symlinked_hidden_file_and_campaign_mismatch_are_rejected(replay_snapshot, tmp_path: Path) -> None:
    root, campaign = replay_snapshot
    hidden = root / HIDDEN
    target = tmp_path / "outside-hidden.json"
    target.write_bytes(hidden.read_bytes())
    hidden.unlink()
    hidden.symlink_to(target)
    with pytest.raises(ReplayLoadError) as error:
        load_replay_store(root, campaign)
    assert error.value.code == "snapshot_file_unavailable"

    # Restore the synthetic snapshot and present a changed revision payload.
    hidden.unlink()
    hidden.write_bytes(target.read_bytes())
    _refresh_snapshot_manifest(root)
    changed_spec = campaign.campaign.model_copy(update={"goal": "different revision"})
    changed_campaign = campaign.model_copy(update={"campaign": changed_spec})
    with pytest.raises(ReplayLoadError) as error:
        load_replay_store(root, changed_campaign)
    assert error.value.code == "campaign_mismatch"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("unsupported_assay", "unknown_assay_reference"),
        ("primary_assay", "hidden_primary_measurement"),
        ("aid_mismatch", "aid_mismatch"),
        ("unmapped_sid", "unmapped_sid"),
        ("duplicate_id", "duplicate_measurement_id"),
    ],
)
def test_hash_valid_but_invalid_hidden_references_are_rejected(replay_snapshot, change, code) -> None:
    root, campaign = replay_snapshot
    rows = _read_hidden(root)
    if change == "unsupported_assay":
        rows[0]["assay_id"] = "missing-assay"
    elif change == "primary_assay":
        rows[0]["assay_id"] = "primary-activity"
        rows[0]["aid"] = 101
        rows[0]["protocol_location"] = "https://example.invalid/pubchem/AID101"
    elif change == "aid_mismatch":
        rows[0]["aid"] = 999
    elif change == "unmapped_sid":
        rows[0]["sid"] = 999999
        rows[0]["raw_row"]["SID"] = "999999"
    elif change == "duplicate_id":
        rows.append(json.loads(json.dumps(rows[0])))
    _write_hidden(root, rows)

    with pytest.raises(ReplayLoadError) as error:
        load_replay_store(root, campaign)
    assert error.value.code == code


def test_unsupported_config_version_is_not_treated_as_empty_snapshot(replay_snapshot) -> None:
    root, campaign = replay_snapshot
    config_path = root / "config/synthetic_linked.json"
    config = json.loads(config_path.read_text())
    config["schema_version"] = "9.9.9"
    config_path.write_text(json.dumps(config))
    _refresh_snapshot_manifest(root)

    with pytest.raises(ReplayLoadError) as error:
        load_replay_store(root, campaign)
    assert error.value.code == "unsupported_config"


def test_lookup_uses_hidden_array_without_parsing_full_normalized_array(replay_snapshot) -> None:
    root, campaign = replay_snapshot
    (root / "bundle/curator/normalized_measurements.json").write_text("not a normalized JSON array\n")
    _refresh_snapshot_manifest(root)

    result = _oracle(root, campaign).lookup(
        _candidate_for_sid(campaign, 1001), "confirm-activity"
    )
    assert result.status == "records_found"


def test_counter_replay_preserves_active_inconclusive_missing_and_inequality(tmp_path: Path) -> None:
    """Replay retains counter semantics and source distinctions without judging success."""
    config = load_config(EXAMPLES / "stage1_configs" / "synthetic_linked.json")
    cache = tmp_path / "cache"
    shutil.copytree(EXAMPLES / "stage1_fixture", cache)
    (cache / "followup_concise.csv").write_text(
        "AID,SID,CID,Activity Outcome,Activity Value [uM]\n"
        "102,1001,2244,FOLLOWUP_SECRET_POSITIVE,>=3.5\n"
        "102,1004,5957,FOLLOWUP_SECRET_INCONCLUSIVE,\n"
        "102,1001,2244,,<=7.25\n"
    )
    counter_mapping = config.assays[1].validated_replace(
        role=AssayRole.COUNTER,
        verdict_mapping={
            "FOLLOWUP_SECRET_POSITIVE": "active",
            "FOLLOWUP_SECRET_INCONCLUSIVE": "inconclusive",
        },
    )
    config = config.validated_replace(assays=[config.assays[0], counter_mapping])

    root = tmp_path / "counter-snapshot"
    root.mkdir()
    shutil.copytree(cache, root / "raw")
    build_campaign(config, cache, root / "bundle")
    (root / "config").mkdir()
    (root / "config/synthetic_linked.json").write_text(config.model_dump_json(indent=2) + "\n")
    _refresh_snapshot_manifest(root)
    campaign = PublicBundleAdapter().load(DataSource(
        kind="public_bundle", location=str(root / "bundle/public/manifest.json")
    ))
    public_counter = next(assay for assay in campaign.assays if assay.assay_id == "confirm-activity")
    assert public_counter.role is AssayRole.COUNTER
    assert public_counter.verdict_meaning[Verdict.ACTIVE] == "configured active"

    oracle = _oracle(root, campaign)
    active_candidate = _candidate_for_sid(campaign, 1001)
    active_rows = oracle.lookup(active_candidate, "confirm-activity")
    assert active_rows.status == "records_found"
    active = next(row for row in active_rows.measurements if row.raw_verdict == "FOLLOWUP_SECRET_POSITIVE")
    assert active.verdict is Verdict.ACTIVE
    assert (active.value, active.unit, active.comparison.value) == (3.5, "uM", ">=")

    missing_numeric_candidate = _candidate_for_sid(campaign, 1004)
    missing_numeric = oracle.lookup(missing_numeric_candidate, "confirm-activity")
    assert missing_numeric.status == "records_found" and len(missing_numeric.measurements) == 1
    inconclusive = missing_numeric.measurements[0]
    assert inconclusive.raw_verdict == "FOLLOWUP_SECRET_INCONCLUSIVE"
    assert inconclusive.verdict is Verdict.INCONCLUSIVE
    assert (inconclusive.value, inconclusive.unit, inconclusive.comparison) == (None, None, None)

    unreported = next(row for row in active_rows.measurements if row.raw_verdict is None)
    assert unreported.verdict is Verdict.UNSPECIFIED
    assert (unreported.value, unreported.unit, unreported.comparison.value) == (7.25, "uM", "<=")


def test_result_type_rejects_status_measurement_count_mismatch() -> None:
    with pytest.raises(ValueError, match="no_record cannot contain measurements"):
        ReplayLookupResult(
            status="no_record", snapshot_id="s", campaign_id="c", candidate_id="x",
            assay_id="a", measurements=(object(),),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="records_found requires at least one measurement"):
        ReplayLookupResult(
            status="records_found", snapshot_id="s", campaign_id="c", candidate_id="x",
            assay_id="a", measurements=(),
        )
