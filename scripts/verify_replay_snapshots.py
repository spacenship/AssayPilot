#!/usr/bin/env python
"""Developer-only verification for the two preserved Stage 1 snapshots."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from pydantic import TypeAdapter

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.data.schemas import CampaignConfig, NormalizedMeasurement
from assaypilot.domain import DataSource
from assaypilot.replay import ReplayOracle, load_replay_store


ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = (
    ROOT / "data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2",
    ROOT / "data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all",
)
MEASUREMENT_LIST = TypeAdapter(list[NormalizedMeasurement])


def public_hashes(root: Path) -> dict[str, str]:
    public_root = root / "bundle/public"
    return {
        path.relative_to(public_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(public_root.rglob("*")) if path.is_file()
    }


def verify_snapshot(root: Path) -> dict[str, object]:
    public_manifest = root / "bundle/public/manifest.json"
    public = PublicBundleAdapter().load(DataSource(
        kind="public_bundle", location=str(public_manifest)
    ))
    config_paths = list((root / "config").glob("*.json"))
    if len(config_paths) != 1:
        raise AssertionError(f"expected one snapshot config under {root / 'config'}")
    config = CampaignConfig.model_validate_json(config_paths[0].read_bytes())
    before_hashes = public_hashes(root)
    before_public = public.model_dump(mode="json")
    oracle = ReplayOracle(load_replay_store(root, public))
    followup_assays = [item.assay_id for item in config.assays if item.role.value != "primary"]
    if not followup_assays:
        raise AssertionError("snapshot has no configured follow-up assay")

    result_counts: Counter[str] = Counter()
    raw_verdict_counts: Counter[str] = Counter()
    found_candidate_ids: set[str] = set()
    found_measurements: list[NormalizedMeasurement] = []
    for candidate in public.candidates:
        for assay_id in followup_assays:
            result = oracle.lookup(candidate.candidate_id, assay_id)
            result_counts[result.status] += 1
            if result.status == "records_found":
                found_candidate_ids.add(candidate.candidate_id)
                found_measurements.extend(result.measurements)
                raw_verdict_counts.update(
                    item.raw_verdict if item.raw_verdict is not None else "<missing>"
                    for item in result.measurements
                )

    result: dict[str, object] = {
        "snapshot_id": oracle.store.snapshot_id,
        "campaign_id": oracle.store.campaign_id,
        "candidate_count": len(public.candidates),
        "followup_assays": followup_assays,
        "candidate_assay_status_counts": dict(sorted(result_counts.items())),
        "candidates_with_records": len(found_candidate_ids),
        "candidates_without_records": result_counts["no_record"],
        "hidden_measurement_count": len(found_measurements),
        "raw_verdict_counts": dict(sorted(raw_verdict_counts.items())),
    }

    if root.name == "revision-20260918-primary-active-all":
        hidden_path = root / "bundle/curator/hidden_followup_measurements.json"
        hidden = MEASUREMENT_LIST.validate_json(hidden_path.read_bytes())
        candidate_source_ids = {
            candidate.source_id: candidate.candidate_id
            for candidate in public.candidates
            if candidate.source == "pubchem_sid"
        }
        primary_assays = {item.assay_id for item in config.assays if item.role.value == "primary"}
        expected_hidden: dict[str, object] = {}
        normalized_path = root / "bundle/curator/normalized_measurements.json"
        normalized = MEASUREMENT_LIST.validate_json(normalized_path.read_bytes())
        for item in normalized:
            if f"SID:{item.sid}" in candidate_source_ids and item.assay_id not in primary_assays:
                if item.measurement_id in expected_hidden:
                    raise AssertionError(f"duplicate normalized measurement_id: {item.measurement_id}")
                expected_hidden[item.measurement_id] = item.model_dump(mode="json")
        actual_hidden: dict[str, object] = {}
        for item in hidden:
            if item.measurement_id in actual_hidden:
                raise AssertionError(f"duplicate hidden measurement_id: {item.measurement_id}")
            actual_hidden[item.measurement_id] = item.model_dump(mode="json")
        if actual_hidden != expected_hidden:
            raise AssertionError("hidden follow-up file is not the exact selected-SID nonprimary subset")
        result["normalized_measurement_count"] = len(normalized)
        result["hidden_subset_matches_normalized"] = True
        result["hidden_subset_measurement_count"] = len(actual_hidden)

    if public.model_dump(mode="json") != before_public:
        raise AssertionError("public campaign changed during lookup")
    after_hashes = public_hashes(root)
    if before_hashes != after_hashes:
        raise AssertionError("public files changed during lookup")
    result["public_hashes_unchanged"] = True
    result["initial_released_at_unchanged"] = all(
        observation.released_at == public.as_of for observation in public.observations
    )
    return result


def main() -> None:
    missing = [str(root) for root in SNAPSHOTS if not root.is_dir()]
    if missing:
        raise FileNotFoundError("required preserved snapshot(s) missing: " + ", ".join(missing))
    report = [verify_snapshot(root) for root in SNAPSHOTS]
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
