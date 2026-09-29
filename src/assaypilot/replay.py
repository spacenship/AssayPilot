"""Trusted, read-only lookup of preserved follow-up measurements.

This module is an internal replay component.  It is deliberately not imported
by the public campaign adapter or exposed as an agent tool.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

from pydantic import TypeAdapter, ValidationError

from assaypilot.data.normalize import NormalizationError, parse_numeric
from assaypilot.data.schemas import AssayMapping, CampaignConfig, NormalizedMeasurement
from assaypilot.domain import (
    AssaySpec,
    DataSource,
    PublicCampaign,
    Verdict,
    validate_public_campaign,
)
from assaypilot.data.adapter import PublicBundleAdapter


_HIDDEN_FILE = "bundle/curator/hidden_followup_measurements.json"
_HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SID_SOURCE = re.compile(r"SID:([1-9][0-9]*)\Z")
_MEASUREMENTS = TypeAdapter(list[NormalizedMeasurement])


class ReplayError(ValueError):
    """Base error with a stable machine-readable reason code."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ReplayLoadError(ReplayError):
    """Snapshot, schema, integrity, or reference error during store loading."""


class ReplayRequestError(ReplayError):
    """Invalid lookup request, distinct from an empty snapshot result."""


@dataclass(frozen=True, slots=True)
class ReplayLookupResult:
    """One candidate/assay lookup; it carries no campaign-wide coverage data."""

    status: Literal["records_found", "no_record"]
    snapshot_id: str
    campaign_id: str
    candidate_id: str
    assay_id: str
    measurements: tuple[NormalizedMeasurement, ...]

    def __post_init__(self) -> None:
        if self.status == "records_found" and not self.measurements:
            raise ValueError("records_found requires at least one measurement")
        if self.status == "no_record" and self.measurements:
            raise ValueError("no_record cannot contain measurements")
        if self.status not in ("records_found", "no_record"):
            raise ValueError("unsupported replay result status")
        if any(item.assay_id != self.assay_id for item in self.measurements):
            raise ValueError("lookup result contains a measurement for another assay")


@dataclass(frozen=True, slots=True)
class ReplayStore:
    """Immutable index scoped to one verified snapshot and public campaign."""

    snapshot_id: str
    campaign_id: str
    public_campaign_sha256: str
    _candidate_sids: Mapping[str, int]
    _supported_assays: frozenset[str]
    _measurements: Mapping[tuple[str, str], tuple[bytes, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "_candidate_sids", MappingProxyType(dict(self._candidate_sids)))
        object.__setattr__(self, "_supported_assays", frozenset(self._supported_assays))
        object.__setattr__(self, "_measurements", MappingProxyType(dict(self._measurements)))


@dataclass(frozen=True, slots=True)
class ReplayOracle:
    """Read-only lookup facade intended for a trusted execution boundary."""

    store: ReplayStore

    def lookup(self, candidate_id: str, assay_id: str) -> ReplayLookupResult:
        """Return all preserved rows for one known candidate and follow-up assay."""
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise ReplayRequestError("invalid_candidate_id", "candidate_id must be a non-empty string")
        if candidate_id not in self.store._candidate_sids:
            raise ReplayRequestError("unknown_candidate", f"unknown candidate_id: {candidate_id!r}")
        if not isinstance(assay_id, str) or not assay_id.strip():
            raise ReplayRequestError("invalid_assay_id", "assay_id must be a non-empty string")
        if assay_id not in self.store._supported_assays:
            raise ReplayRequestError("unsupported_assay", f"assay is not a supported follow-up: {assay_id!r}")

        payloads = self.store._measurements.get((candidate_id, assay_id), ())
        # Parse fresh objects from immutable bytes so caller mutations cannot
        # alter the index or a later lookup result, including nested raw_row.
        measurements = tuple(NormalizedMeasurement.model_validate_json(payload) for payload in payloads)
        return ReplayLookupResult(
            status="records_found" if measurements else "no_record",
            snapshot_id=self.store.snapshot_id,
            campaign_id=self.store.campaign_id,
            candidate_id=candidate_id,
            assay_id=assay_id,
            measurements=measurements,
        )


def _relative_parts(relative: str) -> tuple[str, ...]:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ReplayLoadError("invalid_manifest_path", f"invalid snapshot path: {relative!r}")
    if relative.startswith("/"):
        raise ReplayLoadError("invalid_manifest_path", f"absolute snapshot path is forbidden: {relative!r}")
    parts = tuple(relative.split("/"))
    if any(part in ("", ".", "..") for part in parts):
        raise ReplayLoadError("invalid_manifest_path", f"unsafe snapshot path: {relative!r}")
    return parts


def _open_beneath(root: Path, relative: str) -> int:
    """Open one regular file without following symlinks below a trusted root."""
    parts = _relative_parts(relative)
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ReplayLoadError("unsafe_platform", "safe snapshot reads require POSIX O_NOFOLLOW and O_DIRECTORY")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd: int | None = os.open(root, directory_flags)
    except OSError as exc:
        raise ReplayLoadError("snapshot_file_unavailable", f"cannot open snapshot root safely: {exc}") from exc
    file_fd: int | None = None
    try:
        for part in parts[:-1]:
            assert directory_fd is not None
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            previous_fd = directory_fd
            directory_fd = next_fd
            os.close(previous_fd)
        assert directory_fd is not None
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ReplayLoadError("invalid_snapshot_file", f"snapshot entry is not a regular file: {relative!r}")
        opened_fd = file_fd
        file_fd = None
        return opened_fd
    except OSError as exc:
        raise ReplayLoadError("snapshot_file_unavailable", f"cannot safely read {relative!r}: {exc}") from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _read_beneath(root: Path, relative: str) -> bytes:
    """Read one regular file without following symlinks below a trusted root."""
    file_fd = _open_beneath(root, relative)
    with os.fdopen(file_fd, "rb") as stream:
        return stream.read()


def _hash_beneath(root: Path, relative: str) -> str:
    """Hash a registered file in chunks without parsing or retaining its contents."""
    file_fd = _open_beneath(root, relative)
    digest = hashlib.sha256()
    with os.fdopen(file_fd, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(raw: bytes, location: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ReplayLoadError("invalid_json", f"invalid JSON in {location}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReplayLoadError("invalid_snapshot_manifest", f"{location} must contain a JSON object")
    return value


def _registered_bytes(root: Path, inventory: Mapping[str, object], relative: str) -> bytes:
    expected = inventory.get(relative)
    if not isinstance(expected, str) or not _HEX_SHA256.fullmatch(expected):
        raise ReplayLoadError("unregistered_file", f"snapshot manifest has no valid SHA-256 for {relative!r}")
    raw = _read_beneath(root, relative)
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise ReplayLoadError("hash_mismatch", f"snapshot SHA-256 mismatch for {relative!r}")
    return raw


def _verify_inventory(root: Path, inventory: Mapping[str, object]) -> None:
    """Verify every file named by full_sha256 using streaming, no-follow reads."""
    for relative, expected in inventory.items():
        _relative_parts(relative)
        if not isinstance(expected, str) or not _HEX_SHA256.fullmatch(expected):
            raise ReplayLoadError("unsupported_snapshot_manifest", f"invalid SHA-256 entry for {relative!r}")
        actual = _hash_beneath(root, relative)
        if actual != expected:
            raise ReplayLoadError("hash_mismatch", f"snapshot SHA-256 mismatch for {relative!r}")


def _verify_public_paths_beneath(root: Path) -> None:
    """Reject public-bundle symlinks before delegating content checks to its adapter."""
    raw = _read_beneath(root, "bundle/public/manifest.json")
    manifest = _json_object(raw, "bundle/public/manifest.json")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ReplayLoadError("invalid_public_bundle", "public manifest files must be an object")
    for relative in files:
        if not isinstance(relative, str):
            raise ReplayLoadError("invalid_public_bundle", "public manifest contains a non-string file path")
        parts = _relative_parts(relative)
        _read_beneath(root, "/".join(("bundle", "public", *parts)))


def _validated_config(raw: bytes, location: str) -> CampaignConfig:
    try:
        return CampaignConfig.model_validate_json(raw)
    except ValidationError as exc:
        raise ReplayLoadError("unsupported_config", f"unsupported or invalid campaign config at {location}: {exc}") from exc


def _assay_contracts_match(config: CampaignConfig, campaign: PublicCampaign) -> dict[str, AssaySpec]:
    public_assays: dict[str, AssaySpec] = {}
    for assay in campaign.assays:
        if assay.assay_id in public_assays:
            raise ReplayLoadError("duplicate_assay_id", f"duplicate public assay_id: {assay.assay_id!r}")
        public_assays[assay.assay_id] = assay
    if len({item.assay_id for item in config.assays}) != len(config.assays):
        raise ReplayLoadError("duplicate_assay_id", "config contains duplicate assay_id values")
    if set(public_assays) != {item.assay_id for item in config.assays}:
        raise ReplayLoadError("campaign_config_mismatch", "public campaign and config assay identifiers differ")
    for mapping in config.assays:
        expected = AssaySpec(
            assay_id=mapping.assay_id,
            name=mapping.name,
            role=mapping.role,
            endpoint=mapping.endpoint,
            unit=mapping.unit,
            verdict_meaning=mapping.verdict_meaning,
            prerequisites=mapping.prerequisites,
            cost=mapping.cost,
        )
        if public_assays[mapping.assay_id] != expected:
            raise ReplayLoadError(
                "campaign_config_mismatch",
                f"public assay contract differs from config for {mapping.assay_id!r}",
            )
    spec = campaign.campaign
    if (
        spec.campaign_id != config.campaign_id
        or spec.goal != config.goal
        or spec.target != config.target
        or spec.biological_context != config.biological_context
        or spec.success_conditions != config.success_conditions
        or spec.budget != config.budget
    ):
        raise ReplayLoadError("campaign_config_mismatch", "public campaign contract differs from snapshot config")
    return public_assays


def _candidate_sid_index(campaign: PublicCampaign) -> dict[str, int]:
    by_candidate: dict[str, int] = {}
    candidate_by_sid: dict[int, str] = {}
    for candidate in campaign.candidates:
        if candidate.candidate_id in by_candidate:
            raise ReplayLoadError("duplicate_candidate_id", f"duplicate candidate_id: {candidate.candidate_id!r}")
        if candidate.source != "pubchem_sid":
            raise ReplayLoadError("unsupported", f"unsupported candidate source: {candidate.source!r}")
        match = _SID_SOURCE.fullmatch(candidate.source_id)
        if match is None:
            raise ReplayLoadError("invalid_candidate_source_id", f"invalid SID source_id for {candidate.candidate_id!r}")
        sid = int(match.group(1))
        if sid in candidate_by_sid:
            raise ReplayLoadError(
                "duplicate_candidate_sid",
                f"SID {sid} maps to both {candidate_by_sid[sid]!r} and {candidate.candidate_id!r}",
            )
        candidate_by_sid[sid] = candidate.candidate_id
        by_candidate[candidate.candidate_id] = sid
    if not by_candidate:
        raise ReplayLoadError("empty_candidate_catalog", "public campaign contains no candidates")
    return by_candidate


def _validate_raw_identity(measurement: NormalizedMeasurement, mapping: AssayMapping) -> None:
    raw = measurement.raw_row
    expected_identity = {"AID": measurement.aid, "SID": measurement.sid}
    if measurement.cid is not None:
        expected_identity["CID"] = measurement.cid
    for field, expected in expected_identity.items():
        value = raw.get(field, "").strip()
        if not value.isdecimal() or int(value) != expected:
            raise ReplayLoadError(
                "raw_identity_mismatch",
                f"measurement {measurement.measurement_id!r} has inconsistent raw {field}",
            )
    if measurement.cid is None and raw.get("CID", "").strip():
        raise ReplayLoadError("raw_identity_mismatch", f"measurement {measurement.measurement_id!r} has an unmodeled raw CID")

    raw_outcome = raw.get(mapping.raw_outcome_column, "").strip() or None
    if raw_outcome != measurement.raw_verdict:
        raise ReplayLoadError("raw_verdict_mismatch", f"raw outcome differs for {measurement.measurement_id!r}")
    expected_verdict = Verdict.UNSPECIFIED if raw_outcome is None else mapping.verdict_mapping.get(raw_outcome)
    if expected_verdict is None or measurement.verdict != expected_verdict:
        raise ReplayLoadError("verdict_mapping_mismatch", f"verdict differs from config for {measurement.measurement_id!r}")
    if measurement.protocol_location != mapping.protocol_location:
        raise ReplayLoadError("protocol_mismatch", f"protocol location differs for {measurement.measurement_id!r}")

    if mapping.activity_name_policy == "blank_in_concise" and raw.get("Activity Name", "").strip():
        raise ReplayLoadError("raw_endpoint_mismatch", f"Activity Name is populated for {measurement.measurement_id!r}")
    if mapping.endpoint_scope == "categorical_activity_outcome_only":
        if measurement.value is not None or measurement.unit is not None or measurement.comparison is not None:
            raise ReplayLoadError("numeric_category_measurement", f"categorical measurement has a numeric value: {measurement.measurement_id!r}")
        if any(key.startswith("Activity Value") and value.strip() for key, value in raw.items()):
            raise ReplayLoadError("raw_endpoint_mismatch", f"categorical raw row contains a numeric endpoint: {measurement.measurement_id!r}")
    elif mapping.raw_endpoint_column is None:
        if measurement.value is not None:
            raise ReplayLoadError("numeric_endpoint_mismatch", f"unconfigured numeric value in {measurement.measurement_id!r}")
    else:
        raw_numeric = raw.get(mapping.raw_endpoint_column, "").strip()
        if not raw_numeric:
            if measurement.value is not None:
                raise ReplayLoadError("numeric_endpoint_mismatch", f"unexpected numeric value in {measurement.measurement_id!r}")
        else:
            try:
                value, comparison = parse_numeric(raw_numeric)
            except NormalizationError as exc:
                raise ReplayLoadError("raw_endpoint_mismatch", f"invalid raw numeric value for {measurement.measurement_id!r}") from exc
            if (
                measurement.value != value
                or measurement.comparison != comparison
                or measurement.unit != mapping.unit
                or mapping.raw_unit != mapping.unit
            ):
                raise ReplayLoadError("numeric_endpoint_mismatch", f"normalized numeric value differs for {measurement.measurement_id!r}")


def _build_index(
    config: CampaignConfig,
    campaign: PublicCampaign,
    raw_measurements: bytes,
    public_assays: Mapping[str, AssaySpec],
    inventory: Mapping[str, object],
) -> tuple[frozenset[str], dict[tuple[str, str], tuple[bytes, ...]]]:
    try:
        measurements = _MEASUREMENTS.validate_json(raw_measurements)
    except ValidationError as exc:
        raise ReplayLoadError("invalid_measurements", f"hidden measurements do not match NormalizedMeasurement: {exc}") from exc

    by_assay = {item.assay_id: item for item in config.assays}
    supported = frozenset(item.assay_id for item in config.assays if item.role.value != "primary")
    candidate_sids = _candidate_sid_index(campaign)
    candidate_by_sid = {sid: candidate_id for candidate_id, sid in candidate_sids.items()}
    seen_measurement_ids: set[str] = set()
    sid_cids: dict[int, set[int]] = {}
    grouped: dict[tuple[str, str], list[tuple[str, bytes]]] = {}

    for measurement in measurements:
        if measurement.measurement_id in seen_measurement_ids:
            raise ReplayLoadError("duplicate_measurement_id", f"duplicate measurement_id: {measurement.measurement_id!r}")
        seen_measurement_ids.add(measurement.measurement_id)
        mapping = by_assay.get(measurement.assay_id)
        if mapping is None or measurement.assay_id not in public_assays:
            raise ReplayLoadError("unknown_assay_reference", f"hidden measurement refers to unknown assay {measurement.assay_id!r}")
        if mapping.role.value == "primary":
            raise ReplayLoadError("hidden_primary_measurement", f"hidden file contains primary measurement {measurement.measurement_id!r}")
        if measurement.aid != mapping.aid:
            raise ReplayLoadError("aid_mismatch", f"measurement AID differs from config for {measurement.measurement_id!r}")
        candidate_id = candidate_by_sid.get(measurement.sid)
        if candidate_id is None:
            raise ReplayLoadError("unmapped_sid", f"hidden measurement SID {measurement.sid} has no public candidate")
        source_path = "/".join(("raw", *_relative_parts(mapping.concise_cache_key)))
        source_hash = inventory.get(source_path)
        if not isinstance(source_hash, str) or not _HEX_SHA256.fullmatch(source_hash):
            raise ReplayLoadError(
                "unregistered_source_file",
                f"snapshot inventory does not register the raw source for assay {mapping.assay_id!r}",
            )
        if measurement.source_file_sha256 != source_hash:
            raise ReplayLoadError(
                "source_hash_mismatch",
                f"measurement source hash differs from snapshot raw file for {measurement.measurement_id!r}",
            )
        _validate_raw_identity(measurement, mapping)
        if measurement.cid is not None:
            sid_cids.setdefault(measurement.sid, set()).add(measurement.cid)
            if len(sid_cids[measurement.sid]) > 1:
                raise ReplayLoadError("sid_cid_conflict", f"SID {measurement.sid} maps to multiple CIDs")
        payload = measurement.model_dump_json().encode("utf-8")
        grouped.setdefault((candidate_id, measurement.assay_id), []).append((measurement.measurement_id, payload))

    index = {
        key: tuple(payload for _, payload in sorted(values, key=lambda pair: pair[0]))
        for key, values in grouped.items()
    }
    return supported, index


def load_replay_store(snapshot_root: str | Path, public_campaign: PublicCampaign) -> ReplayStore:
    """Load and verify one snapshot's hidden follow-up array into an immutable index.

    ``snapshot_root`` is a trusted developer/executor path.  The loader verifies
    the snapshot inventory and curator bytes, then binds those records to the
    exact public campaign in ``bundle/public``.  It does not establish OS-level
    access isolation or authenticate a rewritten snapshot manifest.
    """
    supplied_root = Path(snapshot_root).expanduser()
    if supplied_root.is_symlink():
        raise ReplayLoadError("unsafe_snapshot_root", "snapshot_root must not itself be a symlink")
    try:
        root = supplied_root.resolve(strict=True)
    except OSError as exc:
        raise ReplayLoadError("snapshot_root_unavailable", f"snapshot root is unavailable: {exc}") from exc
    if not root.is_dir():
        raise ReplayLoadError("snapshot_root_unavailable", "snapshot_root must be a directory")

    manifest_bytes = _read_beneath(root, "snapshot_manifest.json")
    manifest = _json_object(manifest_bytes, "snapshot_manifest.json")
    inventory = manifest.get("full_sha256")
    snapshot_id = manifest.get("snapshot_id")
    campaign_id = manifest.get("campaign_id")
    if not isinstance(inventory, dict) or not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise ReplayLoadError("unsupported_snapshot_manifest", "snapshot manifest has an unsupported structure")
    if not isinstance(campaign_id, str) or not campaign_id.strip():
        raise ReplayLoadError("unsupported_snapshot_manifest", "snapshot manifest has no campaign_id")
    _verify_inventory(root, inventory)

    config_paths = [
        path for path in inventory
        if path.startswith("config/") and path.endswith(".json") and len(_relative_parts(path)) == 2
    ]
    if len(config_paths) != 1:
        raise ReplayLoadError("unsupported_config", "snapshot must register exactly one config/*.json file")
    config_path = config_paths[0]
    config_bytes = _registered_bytes(root, inventory, config_path)
    config = _validated_config(config_bytes, config_path)
    if config.campaign_id != campaign_id:
        raise ReplayLoadError("campaign_config_mismatch", "snapshot manifest and config campaign_id differ")

    if _HIDDEN_FILE not in inventory:
        raise ReplayLoadError("unregistered_file", f"snapshot manifest does not register {_HIDDEN_FILE!r}")
    hidden_bytes = _registered_bytes(root, inventory, _HIDDEN_FILE)

    _verify_public_paths_beneath(root)
    public_manifest_path = root / "bundle/public/manifest.json"
    try:
        snapshot_campaign = PublicBundleAdapter().load(DataSource(
            kind="public_bundle", location=str(public_manifest_path)
        ))
    except (OSError, ValueError, ValidationError) as exc:
        raise ReplayLoadError("invalid_public_bundle", f"snapshot public bundle is invalid: {exc}") from exc
    if snapshot_campaign.campaign.campaign_id != campaign_id:
        raise ReplayLoadError("campaign_config_mismatch", "snapshot manifest and public campaign_id differ")
    if public_campaign != snapshot_campaign:
        raise ReplayLoadError("campaign_mismatch", "provided PublicCampaign differs from this snapshot")
    audit = validate_public_campaign(snapshot_campaign)
    if not audit.ok:
        raise ReplayLoadError("invalid_public_campaign", "snapshot PublicCampaign failed domain reference validation")

    public_assays = _assay_contracts_match(config, snapshot_campaign)
    supported, index = _build_index(config, snapshot_campaign, hidden_bytes, public_assays, inventory)
    candidate_sids = _candidate_sid_index(snapshot_campaign)
    return ReplayStore(
        snapshot_id=snapshot_id,
        campaign_id=campaign_id,
        public_campaign_sha256=hashlib.sha256(snapshot_campaign.model_dump_json().encode("utf-8")).hexdigest(),
        _candidate_sids=candidate_sids,
        _supported_assays=supported,
        _measurements=index,
    )
