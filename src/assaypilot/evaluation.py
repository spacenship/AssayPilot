"""Trusted offline evaluation of preserved Stage 3 baseline artifacts.

Only released public results are scored as discoveries. Hidden truth is loaded
inside this module through the snapshot's validated ReplayStore and is never
written as a candidate-level mapping.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any, Iterable, Mapping

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.domain import DataSource, Verdict
from assaypilot.domain.catalog import EvidenceRef
from assaypilot.domain.exchange import ExecutionReceipt, ExecutionResult
from assaypilot.domain.records import Observation
from assaypilot.replay import ReplayOracle, load_replay_store
from assaypilot.run_loop import RANDOM_PRIORITY_VERSION


ROOT = Path(__file__).resolve().parents[2]
SPEC_SCHEMA = "assaypilot.stage3c.evaluation-spec.v1"
ARCHIVE_SCHEMA = "assaypilot.stage3c.published-results.v1"
EVALUATOR_VERSION = "stage3c-offline-evaluator-v1"
SID_SOURCE = re.compile(r"SID:([1-9][0-9]*)\Z")
_BINARY = frozenset({"positive", "negative"})


class EvaluationError(ValueError):
    """Raised when a trusted input cannot be evaluated safely."""


def canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"JSON artifact must be an object: {path}")
    return value


def _safe_artifact(root: Path, relative: object, *, require_file: bool = True) -> Path:
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or Path(relative).is_absolute() or any(part in ("", ".", "..") for part in relative.split("/"))):
        raise EvaluationError(f"unsafe artifact path: {relative!r}")
    resolved_root = root.resolve(strict=True)
    path = (resolved_root / relative).resolve(strict=True)
    if ((require_file and not path.is_file()) or (not require_file and not path.is_dir())
            or not path.is_relative_to(resolved_root)):
        raise EvaluationError(f"artifact path escapes its registered root: {relative!r}")
    return path


def classify_verdicts(verdicts: Iterable[str | Verdict]) -> str:
    """Return positive/negative/unknown/ambiguous/missing for one assay unit."""
    values = {item.value if isinstance(item, Verdict) else str(item) for item in verdicts}
    if not values:
        return "missing"
    if len(values) > 1:
        return "ambiguous"
    only = next(iter(values))
    if only == Verdict.ACTIVE.value:
        return "positive"
    if only == Verdict.INACTIVE.value:
        return "negative"
    return "unknown"


def _decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        raise EvaluationError(f"{field} must be a Decimal string or integer")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise EvaluationError(f"invalid Decimal in {field}") from exc
    if not result.is_finite() or result < 0:
        raise EvaluationError(f"{field} must be finite and non-negative")
    return result


def calculate_science_metrics(
    *, truth_labels: Mapping[tuple[str, str], str] | None,
    released_labels: Mapping[tuple[str, str], str],
    initially_public: set[tuple[str, str]],
    spent: Decimal,
    label_conflicts: int = 0,
) -> dict[str, Any]:
    """Pure metric function used by fixtures and the trusted evaluator."""
    released_binary = {key: label for key, label in released_labels.items() if label in _BINARY}
    positives = {key for key, label in released_binary.items() if label == "positive"}
    newly_labeled = set(released_binary) - initially_public
    new_positives = positives - initially_public
    unknown_released = sum(
        key not in initially_public and label in {"unknown", "ambiguous"}
        for key, label in released_labels.items()
    )
    h = len(new_positives)
    l = len(newly_labeled)
    fraction = h / l if l else None
    output: dict[str, Any] = {
        "new_followup_positive_count": h,
        "newly_labeled_count": l,
        "unknown_or_ambiguous_released_count": unknown_released,
        "observed_positive_fraction": fraction,
        "observed_positive_fraction_null_reason": None if l else "no_new_binary_results",
        "positives_per_credit": (Decimal(h) / spent if spent > 0 else None),
        "positives_per_credit_null_reason": None if spent > 0 else "zero_spent",
    }
    if truth_labels is None:
        output.update({
            "universe_size": None,
            "known_binary_count": None,
            "known_positive_new_count": None,
            "missing_count": None,
            "ambiguous_count": None,
            "label_conflict_count": None,
            "known_positive_recall": None,
        "known_positive_recall_null_reason": "truth_unavailable",
        "unknown_truth_count": None,
        })
        return output

    known = {key: label for key, label in truth_labels.items() if label in _BINARY}
    positives_known = {
        key for key, label in known.items()
        if label == "positive" and key not in initially_public
    }
    all_units = set(truth_labels)
    output.update({
        "universe_size": len(all_units),
        "known_binary_count": len(known),
        "known_positive_new_count": len(positives_known),
        "missing_count": sum(value == "missing" for value in truth_labels.values()),
        "ambiguous_count": sum(value == "ambiguous" for value in truth_labels.values()),
        "unknown_truth_count": sum(value == "unknown" for value in truth_labels.values()),
        "label_conflict_count": label_conflicts,
        "known_positive_recall": (h / len(positives_known) if positives_known else None),
        "known_positive_recall_null_reason": None if positives_known else "no_known_new_positive",
    })
    return output


@dataclass(frozen=True)
class TruthSnapshot:
    assay_id: str
    public: Any
    oracle: ReplayOracle | None
    sid_by_candidate: Mapping[str, int]
    assay_aid: int
    eligible_units: frozenset[tuple[str, str]]
    labels: Mapping[tuple[str, str], str]
    measurements: Mapping[tuple[str, str], tuple[Any, ...]]
    initially_public: frozenset[tuple[str, str]]
    initial_public_labels: Mapping[tuple[str, str], str]


def _candidate_sid_index(public) -> dict[str, int]:
    by_candidate: dict[str, int] = {}
    seen_sids: dict[int, str] = {}
    for candidate in public.candidates:
        if candidate.source != "pubchem_sid":
            raise EvaluationError(f"unsupported candidate source {candidate.source!r}")
        match = SID_SOURCE.fullmatch(candidate.source_id)
        if match is None:
            raise EvaluationError(f"candidate has invalid SID source_id: {candidate.candidate_id!r}")
        sid = int(match.group(1))
        if sid in seen_sids:
            raise EvaluationError(f"duplicate SID maps to two candidates: {sid}")
        seen_sids[sid] = candidate.candidate_id
        by_candidate[candidate.candidate_id] = sid
    return by_candidate


def _satisfies_prerequisites(public, candidate_id: str, assay) -> bool:
    initial = [item for item in public.observations if item.candidate_id == candidate_id]
    for prerequisite in assay.prerequisites:
        matching = [item for item in initial if item.assay_id == prerequisite.assay_id]
        if prerequisite.kind == "observed" and not matching:
            return False
        if prerequisite.kind == "verdict" and not any(item.verdict == prerequisite.verdict for item in matching):
            return False
    return True


def _load_public_context(snapshot_root: Path, assay_id: str) -> TruthSnapshot:
    public = PublicBundleAdapter().load(DataSource(
        kind="public_bundle", location=str(snapshot_root / "bundle/public/manifest.json"),
    ))
    assays = {item.assay_id: item for item in public.assays}
    assay = assays.get(assay_id)
    if assay is None:
        raise EvaluationError(f"evaluation assay is absent from public campaign: {assay_id!r}")
    if assay.role.value == "primary":
        raise EvaluationError("initial primary assay cannot be scored as a new follow-up")
    for pre in assay.prerequisites:
        prerequisite_assay = assays.get(pre.assay_id)
        if prerequisite_assay is None or prerequisite_assay.role.value != "primary":
            raise EvaluationError(
                f"unsupported dynamic prerequisite path for {assay_id!r}: {pre.assay_id!r} must be primary",
            )
    sid_by_candidate = _candidate_sid_index(public)
    assay_aid = _assay_aid(snapshot_root, assay_id)

    eligible = {
        (candidate_id, assay_id)
        for candidate_id in sid_by_candidate
        if _satisfies_prerequisites(public, candidate_id, assay)
    }
    initial_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for observation in public.observations:
        if observation.assay_id == assay_id and observation.candidate_id in sid_by_candidate:
            initial_groups[(observation.candidate_id, assay_id)].append(observation.verdict.value)
    initial_labels = {unit: classify_verdicts(values) for unit, values in initial_groups.items()}
    return TruthSnapshot(
        assay_id=assay_id, public=public, oracle=None, sid_by_candidate=sid_by_candidate, assay_aid=assay_aid,
        eligible_units=frozenset(eligible), labels={}, measurements={},
        initially_public=frozenset(initial_groups), initial_public_labels=initial_labels,
    )


def _load_truth_snapshot(snapshot_root: Path, assay_id: str) -> TruthSnapshot:
    context = _load_public_context(snapshot_root, assay_id)
    oracle = ReplayOracle(load_replay_store(snapshot_root, context.public))
    by_unit: dict[tuple[str, str], tuple[Any, ...]] = {}
    labels: dict[tuple[str, str], str] = {}
    for unit in sorted(context.eligible_units):
        lookup = oracle.lookup(*unit)
        measurements = tuple(lookup.measurements)
        by_unit[unit] = measurements
        labels[unit] = classify_verdicts(item.verdict for item in measurements)
    return TruthSnapshot(
        assay_id=assay_id, public=context.public, oracle=oracle,
        sid_by_candidate=context.sid_by_candidate, assay_aid=context.assay_aid,
        eligible_units=context.eligible_units, labels=labels, measurements=by_unit,
        initially_public=context.initially_public, initial_public_labels=context.initial_public_labels,
    )


def _assay_aid(snapshot_root: Path, assay_id: str) -> int:
    config_paths = list((snapshot_root / "config").glob("*.json"))
    if len(config_paths) != 1:
        raise EvaluationError("snapshot must have exactly one campaign config")
    config = _json(config_paths[0])
    assays = config.get("assays")
    if not isinstance(assays, list):
        raise EvaluationError("snapshot config assays are invalid")
    for assay in assays:
        if isinstance(assay, dict) and assay.get("assay_id") == assay_id and isinstance(assay.get("aid"), int):
            return assay["aid"]
    raise EvaluationError(f"snapshot config has no source AID for {assay_id!r}")


def _check_evidence(execution: dict[str, Any], trace_step: dict[str, Any], truth: TruthSnapshot) -> list[Observation]:
    try:
        observations = [Observation.model_validate(item) for item in execution["observations"]]
    except Exception as exc:
        raise EvaluationError(f"released Observation does not match its contract: {exc}") from exc
    try:
        receipt = ExecutionReceipt.model_validate(execution["receipt"])
        result = ExecutionResult.model_validate(execution["result"])
    except Exception as exc:
        raise EvaluationError(f"published receipt/result does not match its contract: {exc}") from exc
    expected_receipt_id = f"receipt-{execution.get('execution_id')}"
    if (receipt.receipt_id != expected_receipt_id or result.receipt_id != receipt.receipt_id
            or receipt.action_id != execution.get("action_id") or result.action_id != execution.get("action_id")
            or result.status != "completed" or result.error is not None
            or [item.model_dump(mode="json") for item in result.observations] != execution.get("observations")):
        raise EvaluationError("archive receipt/result identity or Observation list differs")
    if not observations:
        raise EvaluationError("released archive execution contains no observations")
    if (execution.get("step_no"), execution.get("action_id"), execution.get("candidate_id"),
            execution.get("assay_id"), execution.get("execution_id")) != (
            trace_step.get("step_no"), trace_step.get("action_id"), trace_step.get("candidate_id"),
            trace_step.get("assay_id"), trace_step.get("execution_id")):
        raise EvaluationError("published result archive does not match trace step identity")
    unit = (execution["candidate_id"], execution["assay_id"])
    if unit not in truth.eligible_units or execution["assay_id"] != truth.assay_id:
        raise EvaluationError("released result is outside the evaluation assay or eligible public universe")
    sid = truth.sid_by_candidate.get(unit[0])
    if sid is None:
        raise EvaluationError("published candidate has no public SID mapping")

    evidence_rows = execution.get("evidence")
    if not isinstance(evidence_rows, list):
        raise EvaluationError("archive evidence must be a list")
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for evidence in evidence_rows:
        if not isinstance(evidence, dict):
            raise EvaluationError("malformed archived evidence row")
        reference, payload = evidence.get("reference"), evidence.get("payload")
        if not isinstance(reference, dict) or not isinstance(payload, dict):
            raise EvaluationError("archived evidence requires reference and payload objects")
        try:
            reference_model = EvidenceRef.model_validate(reference)
        except Exception as exc:
            raise EvaluationError(f"archived EvidenceRef does not match its contract: {exc}") from exc
        digest = sha256_bytes(canonical_json(payload))
        if digest != evidence.get("sha256"):
            raise EvaluationError("archived public evidence payload hash mismatch")
        evidence_id = reference_model.evidence_id
        if (not isinstance(evidence_id, str) or evidence_id in evidence_by_id
                or payload.get("evidence_id") != evidence_id
                or reference_model.source_kind != "pubchem_runtime_measurement"
                or reference_model.source_id != payload.get("source_row_id")):
            raise EvaluationError("EvidenceRef and payload identity do not match")
        if (payload.get("candidate_id"), payload.get("sid"), payload.get("aid")) != (
                unit[0], sid, truth.assay_aid):
            raise EvaluationError("public evidence SID/AID does not match candidate and assay")
        raw_row = payload.get("raw_row")
        if (not isinstance(raw_row, dict) or raw_row.get("SID") != str(sid)
                or raw_row.get("AID") != str(truth.assay_aid)):
            raise EvaluationError("public source row identity does not match SID/AID")
        if payload.get("cid") is not None and raw_row.get("CID") != str(payload["cid"]):
            raise EvaluationError("public source row CID differs from its evidence payload")
        evidence_by_id[evidence_id] = evidence

    truth_measurements = truth.measurements.get(unit, ())
    truth_by_measurement_id = {item.measurement_id: item for item in truth_measurements}
    seen_source_ids: set[str] = set()
    observed_measurement_ids: set[str] = set()
    for observation in observations:
        if observation.candidate_id != unit[0] or observation.assay_id != unit[1]:
            raise EvaluationError("Observation candidate/assay differs from its released action")
        public_assay = next(item for item in truth.public.assays if item.assay_id == unit[1])
        if (public_assay.unit == "categorical" and observation.value is not None) or (
                observation.value is None and observation.unit is not None) or (
                observation.value is not None and observation.unit != public_assay.unit):
            raise EvaluationError("Observation numeric value/unit differs from the public assay contract")
        if not observation.evidence_ids:
            raise EvaluationError("released Observation has no EvidenceRef")
        if len(observation.evidence_ids) != 1:
            raise EvaluationError("one source measurement per Observation is required by this archive schema")
        evidence_id = observation.evidence_ids[0]
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            raise EvaluationError("Observation references missing archived EvidenceRef")
        payload = evidence["payload"]
        measurement_id = payload.get("measurement_id")
        source_row_id = payload.get("source_row_id")
        measurement = truth_by_measurement_id.get(measurement_id)
        if (not isinstance(measurement_id, str) or not isinstance(source_row_id, str)
                or measurement_id in observed_measurement_ids or source_row_id in seen_source_ids):
            raise EvaluationError("released evidence does not map to one unique snapshot SID/AID source row")
        if measurement is not None:
            if (measurement.source_row_id != source_row_id
                    or (measurement.sid, measurement.aid, measurement.raw_verdict, measurement.verdict.value) != (
                        sid, truth.assay_aid, observation.raw_verdict, observation.verdict.value)):
                raise EvaluationError("released Observation label conflicts with normalized snapshot source row")
        else:
            if truth.oracle is not None:
                raise EvaluationError("released result is absent from the validated hidden source measurements")
            if (payload.get("raw_outcome") != observation.raw_verdict
                    or raw_row.get("Activity Outcome") != observation.raw_verdict):
                raise EvaluationError("released Observation outcome differs from public source evidence")
        observed_measurement_ids.add(measurement_id)
        seen_source_ids.add(source_row_id)
    if truth_measurements and observed_measurement_ids != set(truth_by_measurement_id):
        raise EvaluationError("published execution did not preserve exactly the source rows returned for its SID/AID")
    if set(evidence_by_id) != {evidence_id for observation in observations for evidence_id in observation.evidence_ids}:
        raise EvaluationError("archive contains unused or duplicate EvidenceRef payloads")
    return observations


def validate_run_artifacts(
    *, baseline_root: Path, plan: dict[str, Any], plan_sha256: str,
    run_row: dict[str, Any], truth: TruthSnapshot,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Load and cross-check one baseline summary, public trace and publication archive."""
    summary_path = _safe_artifact(ROOT, run_row.get("summary_path"))
    trace_path = _safe_artifact(ROOT, run_row.get("trace_path"))
    archive_path = _safe_artifact(ROOT, run_row.get("published_results_path"))
    if (not summary_path.is_relative_to(baseline_root.resolve())
            or not trace_path.is_relative_to(baseline_root.resolve())
            or not archive_path.is_relative_to(baseline_root.resolve())):
        raise EvaluationError("run artifact reference escapes the registered baseline directory")
    summary, trace, archive = _json(summary_path), _json(trace_path), _json(archive_path)
    if summary.get("plan_sha256") != plan_sha256 or summary.get("baseline_id") != plan.get("baseline_id"):
        raise EvaluationError("run summary plan hash or baseline identity mismatch")
    if summary.get("published_results_sha256") != sha256_file(archive_path):
        raise EvaluationError("published results archive file hash mismatch")
    if summary.get("published_results_path") != run_row.get("published_results_path"):
        raise EvaluationError("run summary points to another public results archive")
    if archive.get("schema_version") != ARCHIVE_SCHEMA or archive.get("plan_sha256") != plan_sha256:
        raise EvaluationError("unsupported publication archive schema or plan hash")
    if (archive.get("run_id"), archive.get("snapshot_revision"), archive.get("baseline_id")) != (
            run_row.get("run_id"), run_row.get("revision"), plan.get("baseline_id")):
        raise EvaluationError("archive run/revision/baseline identity mismatch")
    if (trace.get("run_id"), trace.get("snapshot_revision")) != (run_row.get("run_id"), run_row.get("revision")):
        raise EvaluationError("trace run/revision identity mismatch")
    if summary.get("snapshot_revision") != run_row.get("revision") or summary.get("run_id") != run_row.get("run_id"):
        raise EvaluationError("run summary identity mismatch")
    planned = next((spec for snapshot in plan["snapshots"] if snapshot["revision"] == run_row["revision"]
                    for spec in snapshot["runs"] if spec["run_id"] == run_row["run_id"]), None)
    if planned is None:
        raise EvaluationError("run is not in the preregistered plan")
    expected_selector = {
        "kind": planned["selector_kind"], "seed": planned["seed"],
        "algorithm_version": planned["algorithm_version"],
    }
    if trace.get("selector") != expected_selector or summary.get("selector") != expected_selector:
        raise EvaluationError("run selector configuration differs from pre-registration")
    config = summary.get("run_config", {})
    fixed = plan.get("fixed_conditions", {})
    budget_config = config.get("initial_budget", {})
    if not isinstance(budget_config, dict):
        raise EvaluationError("durable run config initial budget must be an object")
    config_bindings = {
        "selector_kind": planned["selector_kind"],
        "selector_seed": planned["seed"],
        "selector_algorithm_version": planned["algorithm_version"],
        "max_steps": planned["max_steps"],
        "max_duration_seconds": fixed.get("max_duration_seconds"),
        "max_action_retries": fixed.get("max_action_retries"),
        "max_release_retries": fixed.get("max_release_retries"),
        "selector_timeout_seconds": fixed.get("selector_timeout_seconds"),
        "cost_policy_version": fixed.get("cost_policy_version"),
        "approval_policy": fixed.get("approval_policy"),
    }
    if any(config.get(key) != value for key, value in config_bindings.items()):
        raise EvaluationError("durable run config differs from pre-registered selector/policy limits")
    if (budget_config.get("amount"), budget_config.get("unit"), budget_config.get("assumed")) != (
            fixed.get("initial_budget"), fixed.get("budget_unit"), fixed.get("budget_assumed")):
        raise EvaluationError("durable run budget differs from pre-registered budget policy")
    if summary.get("public_tree_sha256") != next(
        item["public_tree_sha256"] for item in plan["snapshots"] if item["revision"] == run_row["revision"]
    ):
        raise EvaluationError("run summary public snapshot fingerprint differs from plan")

    steps = trace.get("steps")
    if not isinstance(steps, list) or [item.get("step_no") for item in steps] != list(range(1, len(steps) + 1)):
        raise EvaluationError("trace steps are absent or not contiguous")
    if len(steps) > planned["max_steps"]:
        raise EvaluationError("trace exceeds its registered step limit")
    for field in ("action_id", "request_id"):
        ids = [item.get(field) for item in steps]
        if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
            raise EvaluationError(f"trace has missing or duplicate {field}")
    execution_ids = [item.get("execution_id") for item in steps if item.get("execution_id")]
    if len(execution_ids) != len(set(execution_ids)):
        raise EvaluationError("trace has duplicate unique execution IDs")
    if len(execution_ids) != summary.get("summary", {}).get("unique_executions"):
        raise EvaluationError("trace execution IDs differ from durable summary")

    allowed_statuses = {"released", "no_record", "failed", "cancelled", "rejected"}
    statuses = Counter(item.get("status") for item in steps)
    if set(statuses) - allowed_statuses:
        raise EvaluationError("completed baseline trace contains an unknown or incomplete step status")
    run_summary = summary.get("summary", {})
    status_pairs = {
        "released": "released_executions", "no_record": "no_record_executions",
        "failed": "failed_executions",
    }
    for status, key in status_pairs.items():
        if statuses[status] != run_summary.get(key, 0):
            raise EvaluationError(f"trace {status} count differs from durable summary")
    summary_cancelled = run_summary.get("cancelled_executions", run_summary.get("cancelled_steps", statuses["cancelled"]))
    if statuses["cancelled"] != summary_cancelled:
        raise EvaluationError("trace cancelled count differs from durable summary")
    if len(steps) != run_summary.get("selection_steps") or len(steps) != run_row.get("durable_steps"):
        raise EvaluationError("trace step count differs from summary row")
    for status, field in (("released", "released"), ("no_record", "no_record"),
                          ("failed", "failed"), ("cancelled", "cancelled")):
        if statuses[status] != run_row.get(field):
            raise EvaluationError(f"trace {status} count differs from execution summary row")
    if len(execution_ids) != run_row.get("executions"):
        raise EvaluationError("trace unique execution count differs from execution summary row")

    initial = _decimal(config.get("initial_budget", {}).get("amount") if isinstance(config.get("initial_budget"), dict)
                       else config.get("initial_budget"), "initial_budget")
    unit = config.get("initial_budget", {}).get("unit") if isinstance(config.get("initial_budget"), dict) else config.get("budget_unit")
    if unit is None:
        unit = run_summary.get("unit")
    previous_spent = Decimal("0")
    for step in steps:
        checkpoint = step.get("budget_after_step", {})
        if checkpoint.get("unit") != unit:
            raise EvaluationError("budget unit changed within trace")
        spent = _decimal(checkpoint.get("spent"), "trace budget spent")
        reserved = _decimal(checkpoint.get("reserved"), "trace budget reserved")
        available = _decimal(checkpoint.get("available"), "trace budget available")
        if spent < previous_spent or spent + reserved + available != initial:
            raise EvaluationError("trace budget is non-monotone or fails its conservation equation")
        previous_spent = spent
    final_budget = trace.get("budget_current", {})
    spent = _decimal(final_budget.get("spent"), "final spent")
    reserved = _decimal(final_budget.get("reserved"), "final reserved")
    available = _decimal(final_budget.get("available"), "final available")
    if (spent != _decimal(run_summary.get("spent"), "summary spent")
            or reserved != _decimal(run_summary.get("reserved"), "summary reserved")
            or available != _decimal(run_summary.get("available"), "summary available")
            or spent + reserved + available != initial or previous_spent != spent):
        raise EvaluationError("trace final budget differs from durable summary")
    if (run_row.get("spent"), run_row.get("reserved"), run_row.get("available"), run_row.get("unit")) != (
            str(spent), str(reserved), str(available), unit):
        raise EvaluationError("execution summary budget differs from trace")

    released_steps = {item["execution_id"]: item for item in steps if item.get("status") == "released"}
    archives = archive.get("executions")
    if not isinstance(archives, list):
        raise EvaluationError("publication archive executions must be a list")
    by_execution = {item.get("execution_id"): item for item in archives if isinstance(item, dict)}
    if len(by_execution) != len(archives) or set(by_execution) != set(released_steps):
        raise EvaluationError("archive execution set differs from released trace executions")
    archived_observation_count = 0
    for execution_id, execution in by_execution.items():
        observations = _check_evidence(execution, released_steps[execution_id], truth)
        trace_obs = released_steps[execution_id].get("published_observations", [])
        if len(trace_obs) != len(observations):
            raise EvaluationError("trace and archive Observation counts differ")
        trace_by_id = {item.get("observation_id"): item for item in trace_obs}
        if set(trace_by_id) != {item.observation_id for item in observations}:
            raise EvaluationError("trace and archive Observation IDs differ")
        for observation in observations:
            trace_evidence = trace_by_id[observation.observation_id].get("evidence", [])
            archive_evidence = [item for item in execution["evidence"] if item["reference"]["evidence_id"] in observation.evidence_ids]
            if {item.get("evidence_id") for item in trace_evidence} != set(observation.evidence_ids):
                raise EvaluationError("trace EvidenceRef IDs differ from the public Observation")
            for trace_ref in trace_evidence:
                archived_ref = next(item for item in archive_evidence if item["reference"]["evidence_id"] == trace_ref["evidence_id"])
                if trace_ref.get("sha256") != archived_ref.get("sha256"):
                    raise EvaluationError("trace evidence hash differs from archived public payload")
        archived_observation_count += len(observations)
    if archived_observation_count != run_summary.get("observations_added") or archived_observation_count != run_row.get("observations_added"):
        raise EvaluationError("archive Observation count differs from trace or durable summary")
    return summary, trace, archive


def _run_metrics(summary: dict[str, Any], trace: dict[str, Any], archive: dict[str, Any], truth: TruthSnapshot | None,
                 *, assay_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    steps = trace["steps"]
    released = sum(item["status"] == "released" for item in steps)
    no_record = sum(item["status"] == "no_record" for item in steps)
    failed = sum(item["status"] == "failed" for item in steps)
    cancelled = sum(item["status"] == "cancelled" for item in steps)
    completed = released + no_record + failed + cancelled
    budget = summary["run_config"].get("initial_budget", {})
    if not isinstance(budget, dict):
        budget = {"amount": budget, "unit": trace["budget_current"]["unit"], "assumed": True}
    spent = _decimal(trace["budget_current"]["spent"], "spent")
    initial = _decimal(budget.get("amount"), "initial budget")
    labels: dict[tuple[str, str], list[str]] = defaultdict(list)
    released_by_step: dict[tuple[str, str], int] = {}
    for execution in archive["executions"]:
        unit = (execution["candidate_id"], execution["assay_id"])
        for observation in execution["observations"]:
            labels[unit].append(observation["verdict"])
        released_by_step[unit] = min(execution["step_no"], released_by_step.get(unit, execution["step_no"]))
    released_labels = {unit: classify_verdicts(verdicts) for unit, verdicts in labels.items()}
    if any(unit[1] != assay_id for unit in released_labels):
        raise EvaluationError("archive contains a different assay from evaluation_spec")
    initially_public = set(truth.initially_public) if truth is not None else set()
    truth_available = truth is not None and truth.oracle is not None
    science = calculate_science_metrics(
        truth_labels=truth.labels if truth_available else None,
        released_labels=released_labels,
        initially_public=initially_public,
        spent=spent,
        label_conflicts=(sum(value == "ambiguous" for value in truth.labels.values()) if truth_available else 0),
    )
    if truth_available:
        for unit, label in released_labels.items():
            truth_label = truth.labels.get(unit)
            if label in _BINARY and truth_label != label:
                raise EvaluationError(f"released categorical label conflicts with snapshot truth for {unit[0]} / {assay_id}")
    first_positive_step = None
    first_positive_spent = None
    for unit, label in released_labels.items():
        if label == "positive" and unit not in initially_public:
            step_no = released_by_step[unit]
            first_positive_step = step_no if first_positive_step is None else min(first_positive_step, step_no)
    if first_positive_step is not None:
        first_positive_spent = next(
            _decimal(step["budget_after_step"]["spent"], "step spent")
            for step in steps if step["step_no"] == first_positive_step
        )
    summary_values = summary.get("summary", {})
    metrics = {
        "run_id": trace["run_id"],
        "snapshot_revision": trace["snapshot_revision"],
        "selector": trace["selector"],
        "evaluation_assay_id": assay_id,
        "evaluation_status": "complete",
        "durable_steps": len(steps),
        "unique_executions": summary_values.get("unique_executions"),
        "released_executions": released,
        "no_record_executions": no_record,
        "failed_executions": failed,
        "cancelled_executions": cancelled,
        "rejected_steps": sum(item["status"] == "rejected" for item in steps),
        "pending_steps": sum(item["status"] in {"proposed", "approved", "pending_release"} for item in steps),
        "observations_added": summary_values.get("observations_added"),
        "stop_reason": trace.get("stop_reason"),
        "initial_budget": str(initial),
        "spent": str(spent),
        "reserved": str(_decimal(trace["budget_current"]["reserved"], "reserved")),
        "available": str(_decimal(trace["budget_current"]["available"], "available")),
        "budget_unit": budget.get("unit", trace["budget_current"]["unit"]),
        "budget_assumed": budget.get("assumed", True),
        "record_yield": (released / completed if completed else None),
        "record_yield_null_reason": None if completed else "no_completed_executions",
        "no_record_fraction": (no_record / completed if completed else None),
        "no_record_fraction_null_reason": None if completed else "no_completed_executions",
        "first_positive_step": first_positive_step,
        "first_positive_spent": str(first_positive_spent) if first_positive_spent is not None else None,
        **science,
    }
    curve_steps = []
    cumulative_labels: dict[tuple[str, str], str] = {}
    seen_units: set[tuple[str, str]] = set()
    cumulative_spent = Decimal("0")
    cumulative_positive = 0
    cumulative_binary = 0
    cumulative_unknown = 0
    for step in steps:
        cumulative_spent = _decimal(step["budget_after_step"]["spent"], "step spent")
        execution = next((item for item in archive["executions"] if item["execution_id"] == step.get("execution_id")), None)
        if execution is not None:
            unit = (execution["candidate_id"], execution["assay_id"])
            if unit not in seen_units:
                seen_units.add(unit)
                label = released_labels[unit]
                if label in _BINARY and unit not in initially_public:
                    cumulative_binary += 1
                    if label == "positive":
                        cumulative_positive += 1
                elif label in {"unknown", "ambiguous"}:
                    cumulative_unknown += 1
        curve_steps.append({
            "step": step["step_no"], "spent": str(cumulative_spent),
            "new_positive_count": cumulative_positive,
            "new_binary_count": cumulative_binary,
            "released_unknown_or_ambiguous_count": cumulative_unknown,
        })
    curve = {
        "run_id": trace["run_id"], "snapshot_revision": trace["snapshot_revision"],
        "selector": trace["selector"], "step_points": curve_steps,
        "cost_points": [{"spent": point["spent"], "step": point["step"],
                         "new_positive_count": point["new_positive_count"],
                         "new_binary_count": point["new_binary_count"]} for point in curve_steps],
        "cost_curve_interpretation": "observed step states only; repeated cost coordinates are retained; no future-state extension or interpolation",
    }
    return metrics, curve


def _aggregate_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    fields = (
        "durable_steps", "unique_executions", "released_executions", "no_record_executions",
        "failed_executions", "observations_added", "new_followup_positive_count", "newly_labeled_count",
        "unknown_or_ambiguous_released_count", "observed_positive_fraction", "known_positive_recall",
        "positives_per_credit", "record_yield", "no_record_fraction", "first_positive_step",
    )
    aggregate: dict[str, Any] = {"n_runs": len(rows)}
    for field in fields:
        values = [row.get(field) for row in rows if isinstance(row.get(field), (int, float, Decimal))
                  and not isinstance(row.get(field), bool) and math.isfinite(float(row[field]))]
        aggregate[field] = {
            "n": len(values),
            "mean": (sum(values) / len(values) if values else None),
            "sample_sd": (statistics.stdev(values) if len(values) >= 2 else None),
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "null_reason": None if values else "no_defined_values",
        }
    return aggregate


def _common_endpoints(rows: list[dict[str, Any]], curves: list[dict[str, Any]]) -> dict[str, Any]:
    by_run = {curve["run_id"]: curve for curve in curves}
    step_limit = min((len(by_run[row["run_id"]]["step_points"]) for row in rows), default=0)
    cost_endpoints = []
    for row in rows:
        points = by_run[row["run_id"]]["cost_points"]
        max_cost = max((Decimal(point["spent"]) for point in points), default=Decimal("0"))
        cost_endpoints.append(max_cost)
    cost_limit = min(cost_endpoints, default=Decimal("0"))
    step_values = {}
    for row in rows:
        points = by_run[row["run_id"]]["step_points"]
        selected = next((item for item in points if item["step"] == step_limit), None) if step_limit else None
        step_values[row["run_id"]] = selected
    cost_values = {}
    for row in rows:
        eligible = [item for item in by_run[row["run_id"]]["cost_points"] if Decimal(item["spent"]) <= cost_limit]
        cost_values[row["run_id"]] = eligible[-1] if eligible else None
    return {
        "common_step_range_start": 1 if step_limit else None,
        "common_step_range_end": step_limit if step_limit else None,
        "by_run_at_common_step_endpoint": step_values,
        "common_spent_range_start": "0",
        "common_spent_range_end": str(cost_limit),
        "by_run_at_or_before_common_spent_endpoint": cost_values,
        "cost_endpoint_rule": "last observed state at or below the common actual spend; no interpolation",
    }


def _svg_curve(title: str, curves: list[dict[str, Any]], *, axis: str) -> str:
    width, height = 820, 460
    left, top, plot_w, plot_h = 72, 54, 700, 330
    all_points = [point for curve in curves for point in (curve["step_points"] if axis == "step" else curve["cost_points"])]
    xmax = max((int(point["step"]) for point in all_points), default=1) if axis == "step" else max((float(Decimal(point["spent"])) for point in all_points), default=1.0)
    ymax = max((int(point["new_positive_count"]) for point in all_points), default=0)
    ymax = max(1, ymax)
    colors = ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2"]
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
             '<rect width="100%" height="100%" fill="white"/>',
             f'<text x="{left}" y="28" font-family="sans-serif" font-size="18">{title}</text>',
             f'<path d="M{left},{top} V{top+plot_h} H{left+plot_w}" stroke="#333" fill="none"/>',
             f'<text x="{left+plot_w/2}" y="{height-18}" text-anchor="middle" font-family="sans-serif" font-size="13">{"step" if axis == "step" else "synthetic_credit spent"}</text>',
             f'<text transform="translate(18 {top+plot_h/2}) rotate(-90)" text-anchor="middle" font-family="sans-serif" font-size="13">cumulative newly released Active</text>']
    for i in range(4):
        yval = ymax * i / 3
        y = top + plot_h - plot_h * yval / ymax
        parts.append(f'<path d="M{left-4},{y:.1f} H{left+plot_w}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="{left-10}" y="{y+4:.1f}" text-anchor="end" font-family="sans-serif" font-size="11">{yval:.1f}</text>')
    for index, curve in enumerate(curves):
        points = curve["step_points"] if axis == "step" else curve["cost_points"]
        coordinates = []
        for point in points:
            xvalue = int(point["step"]) if axis == "step" else float(Decimal(point["spent"]))
            x = left + plot_w * xvalue / max(xmax, 1)
            y = top + plot_h - plot_h * point["new_positive_count"] / ymax
            coordinates.append((x, y))
        if coordinates:
            path_d = f"M{left},{top+plot_h} L{coordinates[0][0]:.1f},{coordinates[0][1]:.1f}"
            for (x1, y1), (x2, y2) in zip(coordinates, coordinates[1:]):
                path_d += f" H{x2:.1f} V{y2:.1f}"
            parts.append(f'<path d="{path_d}" fill="none" stroke="{colors[index % len(colors)]}" stroke-width="2"/>')
            label = curve["run_id"].replace("stage3c-", "")
            ly = top + 16 + (index % 6) * 17
            parts.append(f'<path d="M{left+plot_w-176},{ly-4} h18" stroke="{colors[index % len(colors)]}" stroke-width="2"/>')
            parts.append(f'<text x="{left+plot_w-153}" y="{ly}" font-family="sans-serif" font-size="10">{label}</text>')
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def _metric_serializable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _metric_serializable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_metric_serializable(item) for item in value]
    return value


def _expected_run_set(plan: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    expected = []
    for snapshot in plan.get("snapshots", []):
        for spec in snapshot.get("runs", []):
            if spec.get("role") == "baseline":
                expected.append((snapshot["revision"], spec["run_id"], spec))
    return expected


def _validate_baseline_plan_contract(plan: dict[str, Any]) -> None:
    """Reject altered selector/seed/budget plans instead of evaluating a variant."""
    if plan.get("schema_version") != "assaypilot.stage3b.baseline-plan.v1":
        raise EvaluationError("unsupported baseline plan schema")
    snapshots = plan.get("snapshots")
    if not isinstance(snapshots, list) or {item.get("revision") for item in snapshots if isinstance(item, dict)} != {
            "revision-20260917-r2", "revision-20260918-primary-active-all"} or len(snapshots) != 2:
        raise EvaluationError("baseline plan must contain the registered r2 and expanded snapshots")
    for snapshot in snapshots:
        runs = snapshot.get("runs")
        if not isinstance(runs, list) or len(runs) != 6:
            raise EvaluationError("each snapshot must contain fixed_order and five seeded baseline runs")
        fixed = [item for item in runs if item.get("selector_kind") == "fixed_order"]
        seeded = [item for item in runs if item.get("selector_kind") == "seeded_random_priority"]
        if (len(fixed) != 1 or len(seeded) != 5
                or sorted(item.get("seed") for item in seeded) != [0, 1, 2, 3, 4]
                or any(item.get("role") != "baseline" for item in runs)
                or any(item.get("max_steps") != snapshot.get("max_steps") for item in runs)
                or fixed[0].get("seed") is not None or fixed[0].get("algorithm_version") is not None
                or any(item.get("algorithm_version") != RANDOM_PRIORITY_VERSION for item in seeded)):
            raise EvaluationError("baseline selector/seed/revision/limit set differs from the Stage 3-B contract")
    fixed_conditions = plan.get("fixed_conditions", {})
    expected_conditions = {
        "initial_budget": "5", "budget_unit": "synthetic_credit", "budget_assumed": True,
        "cost_policy_version": "preserved-public-assay-cost-v1", "approval_policy": "bounded_replay",
        "max_duration_seconds": 300, "max_action_retries": 1,
        "max_release_retries": 2, "selector_timeout_seconds": 5.0,
    }
    if any(fixed_conditions.get(key) != value for key, value in expected_conditions.items()):
        raise EvaluationError("baseline fixed budget or execution policy differs from Stage 3-B")
    recovery = plan.get("recovery_control", {})
    if (recovery.get("snapshot_revision"), recovery.get("seed"), recovery.get("algorithm_version")) != (
            "revision-20260917-r2", 0, RANDOM_PRIORITY_VERSION):
        raise EvaluationError("recovery control does not match the registered seed-0 r2 control")


def register_evaluation_spec(
    baseline_root: Path, output_dir: Path, *, evaluation_assay_id: str = "mep2-confirmatory",
) -> Path:
    """Freeze exact artifact/source hashes and evaluation rules before scoring."""
    baseline_root = baseline_root.resolve(strict=True)
    if not baseline_root.is_relative_to((ROOT / "reports/stage3/baselines").resolve()):
        raise EvaluationError("baseline input must be beneath reports/stage3/baselines")
    output_dir = output_dir.resolve()
    if not output_dir.is_relative_to((ROOT / "reports/stage3/evaluations").resolve()):
        raise EvaluationError("evaluation output must be beneath reports/stage3/evaluations")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"evaluation output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    plan_path = _safe_artifact(baseline_root, "plan.json")
    result_path = _safe_artifact(baseline_root, "execution_summary.json")
    plan_bytes = plan_path.read_bytes()
    plan_hash = sha256_bytes(plan_bytes)
    plan = json.loads(plan_bytes)
    suite = _json(result_path)
    if suite.get("plan_sha256") != plan_hash:
        raise EvaluationError("execution_summary plan_sha256 does not match original plan bytes")
    expected = _expected_run_set(plan)
    actual_rows = suite.get("baseline_runs")
    if not isinstance(actual_rows, list):
        raise EvaluationError("execution summary has no baseline_runs list")
    expected_keys = {(revision, run_id) for revision, run_id, _ in expected}
    planned_by_key = {(revision, run_id): spec for revision, run_id, spec in expected}
    actual_keys = {(row.get("revision"), row.get("run_id")) for row in actual_rows if isinstance(row, dict)}
    if actual_keys != expected_keys or len(actual_rows) != len(expected):
        raise EvaluationError("execution summary does not contain exactly the preregistered baseline run set")
    for row in actual_rows:
        planned = planned_by_key[(row["revision"], row["run_id"])]
        if (row.get("selector"), row.get("seed")) != (planned["selector_kind"], planned["seed"]):
            raise EvaluationError("execution summary selector/seed differs from its registered plan")
    _validate_baseline_plan_contract(plan)

    snapshots: dict[str, dict[str, Any]] = {}
    for item in plan["snapshots"]:
        root = _safe_artifact(ROOT, item["snapshot_root"], require_file=False)
        if not root.is_dir():
            raise EvaluationError(f"registered snapshot path is not a directory: {item['revision']}")
        manifest_path = _safe_artifact(root, "snapshot_manifest.json")
        manifest = _json(manifest_path)
        hidden_path = _safe_artifact(root, "bundle/curator/hidden_followup_measurements.json")
        inventory = manifest.get("full_sha256", {})
        expected_hidden = inventory.get("bundle/curator/hidden_followup_measurements.json")
        if expected_hidden != sha256_file(hidden_path):
            raise EvaluationError(f"hidden source fingerprint does not match snapshot inventory: {item['revision']}")
        public_root = _safe_artifact(root, "bundle/public/manifest.json").parent
        public_files = {
            path.relative_to(public_root).as_posix(): sha256_file(path)
            for path in sorted(public_root.rglob("*")) if path.is_file()
        }
        public_tree = sha256_bytes(canonical_json(public_files))
        if public_tree != item.get("public_tree_sha256"):
            raise EvaluationError(f"public snapshot hash changed after baseline registration: {item['revision']}")
        public = PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(public_root / "manifest.json")))
        assay = next((assay for assay in public.assays if assay.assay_id == evaluation_assay_id), None)
        if assay is None or assay.role.value == "primary":
            raise EvaluationError(f"evaluation assay is unsupported in {item['revision']}")
        snapshots[item["revision"]] = {
            "snapshot_root": item["snapshot_root"],
            "snapshot_id": manifest.get("snapshot_id"),
            "public_tree_sha256": public_tree,
            "public_manifest_sha256": sha256_file(public_root / "manifest.json"),
            "snapshot_manifest_sha256": sha256_file(manifest_path),
            "hidden_source_path": "bundle/curator/hidden_followup_measurements.json",
            "hidden_source_sha256": sha256_file(hidden_path),
            "hidden_manifest_sha256": expected_hidden,
            "candidate_count": len(public.candidates),
            "evaluation_assay": assay.model_dump(mode="json"),
        }

    input_rows = []
    traces_by_revision: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(actual_rows, key=lambda value: (value["revision"], value["run_id"])):
        artifact_hashes = {}
        for name in ("summary_path", "trace_path", "published_results_path"):
            path = _safe_artifact(ROOT, row.get(name))
            if not path.is_relative_to(baseline_root):
                raise EvaluationError(f"{name} escapes baseline artifact root")
            artifact_hashes[name] = {"path": row[name], "sha256": sha256_file(path)}
            if name == "trace_path":
                traces_by_revision[row["revision"]].append(_json(path))
        input_rows.append({
            "revision": row["revision"], "run_id": row["run_id"],
            "selector": row["selector"], "seed": row["seed"],
            "artifact_hashes": artifact_hashes,
        })
    common_axes = {}
    for revision, traces in traces_by_revision.items():
        common_axes[revision] = {
            "step_endpoint": min((len(trace.get("steps", [])) for trace in traces), default=0),
            "spent_endpoint": str(min((
                _decimal(trace.get("budget_current", {}).get("spent"), "trace final spent")
                for trace in traces
            ), default=Decimal("0"))),
            "cost_endpoint_rule": "last observed point at or below the actual common spend; no interpolation",
        }
    source_files = [
        ROOT / "src/assaypilot/evaluation.py",
        ROOT / "scripts/verify_stage3_baselines.py",
    ]
    spec = {
        "schema_version": SPEC_SCHEMA,
        "evaluation_id": output_dir.name,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "registration_statement": "evaluation rules frozen before metrics; this is not a pre-run scientific hypothesis registration",
        "evaluator_version": EVALUATOR_VERSION,
        "baseline": {
            "baseline_id": plan.get("baseline_id"),
            "root": baseline_root.relative_to(ROOT).as_posix(),
            "plan_path": plan_path.relative_to(ROOT).as_posix(),
            "plan_sha256": plan_hash,
            "execution_summary_path": result_path.relative_to(ROOT).as_posix(),
            "execution_summary_sha256": sha256_file(result_path),
            "baseline_run_count": len(input_rows),
            "excluded_recovery_control": suite.get("recovery_check", {}).get("uninterrupted_control_run_id"),
        },
        "evaluation_contract": {
            "evaluation_assay_id": evaluation_assay_id,
            "positive_verdict": "active",
            "negative_verdict": "inactive",
            "unknown_verdicts": ["inconclusive", "unspecified", "other unrecognized categorical outcome"],
            "conflict_rule": "multiple distinct normalized verdicts for one candidate/SID and assay are ambiguous; no favorable-row selection",
            "unit": "(candidate_id, evaluation_assay_id), candidate resolved only by validated source SID",
            "universe_rule": "all public candidates satisfying the target assay's public prerequisites at initial snapshot; unsupported non-primary prerequisite paths fail explicitly",
            "known_positive_denominator": "snapshot-known binary Active units in U, excluding any identical candidate/assay result already public at run start",
            "new_discovery_rule": "only unique Active labels in released Publication Reader results; initial primary results are not follow-up hits",
            "missing_rule": "no_record is missing, never inactive; failed/cancelled/unreleased actions are not labels",
            "source_scope": "source-normalized Activity Outcome for the configured follow-up assay; no potency or biological claim is inferred",
            "cid_policy": "CID is preserved as evidence but never used to join or deduplicate evaluation units",
            "generalization_limit": "recall covers known source positives in this snapshot only; no missing-at-random assumption",
        },
        "snapshots": snapshots,
        "common_axes": common_axes,
        "inputs": input_rows,
        "excluded_runs": [{"run_id": suite.get("recovery_check", {}).get("uninterrupted_control_run_id"), "role": "recovery_control_not_scored"}],
        "implementation_sha256": {
            path.relative_to(ROOT).as_posix(): sha256_file(path) for path in source_files
        },
        "hash_semantics": "SHA-256 values detect changes; they are not external signatures or source authentication",
    }
    spec_path = output_dir / "evaluation_spec.json"
    with spec_path.open("xb") as handle:
        handle.write(json.dumps(spec, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    return spec_path


def evaluate_registered(spec_path: Path, *, with_truth: bool = True) -> dict[str, Any]:
    """Evaluate a frozen specification into a fresh, already registered directory."""
    spec_path = spec_path.resolve(strict=True)
    allowed_output_root = (ROOT / "reports/stage3/evaluations").resolve()
    if not spec_path.is_relative_to(allowed_output_root):
        raise EvaluationError("evaluation specification must be beneath reports/stage3/evaluations")
    output_dir = spec_path.parent
    spec = _json(spec_path)
    if spec.get("schema_version") != SPEC_SCHEMA:
        raise EvaluationError("unsupported evaluation specification version")
    baseline_root = _safe_artifact(ROOT, spec["baseline"]["root"], require_file=False)
    if not baseline_root.is_relative_to((ROOT / "reports/stage3/baselines").resolve()):
        raise EvaluationError("registered baseline directory is outside reports/stage3/baselines")
    plan_path = _safe_artifact(ROOT, spec["baseline"]["plan_path"])
    summary_path = _safe_artifact(ROOT, spec["baseline"]["execution_summary_path"])
    if not plan_path.is_relative_to(baseline_root) or not summary_path.is_relative_to(baseline_root):
        raise EvaluationError("registered plan or execution summary escapes the baseline directory")
    plan_raw = plan_path.read_bytes()
    plan_sha = sha256_bytes(plan_raw)
    if plan_sha != spec["baseline"]["plan_sha256"] or sha256_file(summary_path) != spec["baseline"]["execution_summary_sha256"]:
        raise EvaluationError("registered baseline plan/summary hash changed")
    _validate_baseline_plan_contract(json.loads(plan_raw))
    for relative, expected_hash in spec.get("implementation_sha256", {}).items():
        source = _safe_artifact(ROOT, relative)
        if sha256_file(source) != expected_hash:
            raise EvaluationError(f"registered evaluator implementation changed: {relative}")
    plan, suite = json.loads(plan_raw), _json(summary_path)
    if suite.get("plan_sha256") != plan_sha:
        raise EvaluationError("execution summary plan hash differs from plan bytes")
    actual_by_key = {(row["revision"], row["run_id"]): row for row in suite["baseline_runs"]}
    expected_keys = {(row["revision"], row["run_id"]) for row in spec["inputs"]}
    if set(actual_by_key) != expected_keys or len(actual_by_key) != len(spec["inputs"]):
        raise EvaluationError("baseline run inventory differs from evaluation specification")
    for registered in spec["inputs"]:
        actual = actual_by_key[(registered["revision"], registered["run_id"])]
        if (actual.get("selector"), actual.get("seed")) != (registered.get("selector"), registered.get("seed")):
            raise EvaluationError("execution summary selector/seed differs from evaluation specification")

    truths: dict[str, TruthSnapshot | None] = {}
    truth_errors: dict[str, str] = {}
    for revision, snapshot_spec in spec["snapshots"].items():
        snapshot_root = _safe_artifact(ROOT, snapshot_spec["snapshot_root"], require_file=False)
        if snapshot_root.name != revision:
            truth_errors[revision] = "snapshot path revision differs from registered revision"
            truths[revision] = None
            continue
        manifest_path = _safe_artifact(snapshot_root, "snapshot_manifest.json")
        if sha256_file(manifest_path) != snapshot_spec["snapshot_manifest_sha256"]:
            truth_errors[revision] = "snapshot manifest hash changed"
            truths[revision] = None
            continue
        public_root = _safe_artifact(snapshot_root, "bundle/public/manifest.json").parent
        if sha256_file(public_root / "manifest.json") != snapshot_spec["public_manifest_sha256"]:
            truth_errors[revision] = "public manifest hash changed"
            truths[revision] = None
            continue
        public_files = {
            path.relative_to(public_root).as_posix(): sha256_file(path)
            for path in sorted(public_root.rglob("*")) if path.is_file()
        }
        if sha256_bytes(canonical_json(public_files)) != snapshot_spec["public_tree_sha256"]:
            truth_errors[revision] = "public snapshot tree hash changed"
            truths[revision] = None
            continue
        hidden_path = _safe_artifact(snapshot_root, snapshot_spec["hidden_source_path"])
        if with_truth and sha256_file(hidden_path) != snapshot_spec["hidden_source_sha256"]:
            truth_errors[revision] = "hidden source fingerprint changed"
            truths[revision] = None
            continue
        if with_truth:
            hidden_inventory_hash = _json(manifest_path).get("full_sha256", {}).get(snapshot_spec["hidden_source_path"])
            if hidden_inventory_hash != snapshot_spec["hidden_manifest_sha256"]:
                truth_errors[revision] = "hidden source manifest fingerprint changed"
                truths[revision] = None
                continue
            try:
                truths[revision] = _load_truth_snapshot(snapshot_root, spec["evaluation_contract"]["evaluation_assay_id"])
            except Exception as exc:
                truth_errors[revision] = f"trusted truth adapter failed: {exc}"
                truths[revision] = None
        else:
            try:
                truths[revision] = _load_public_context(snapshot_root, spec["evaluation_contract"]["evaluation_assay_id"])
            except Exception as exc:
                truth_errors[revision] = f"public context adapter failed: {exc}"
                truths[revision] = None

    validations = []
    metrics_rows = []
    curves = []
    for registered in spec["inputs"]:
        key = (registered["revision"], registered["run_id"])
        row = actual_by_key[key]
        result = {"revision": key[0], "run_id": key[1], "status": "passed", "errors": []}
        try:
            for item in registered["artifact_hashes"].values():
                artifact = _safe_artifact(ROOT, item["path"])
                if sha256_file(artifact) != item["sha256"]:
                    raise EvaluationError(f"registered artifact hash changed: {item['path']}")
            if key[0] in truth_errors:
                raise EvaluationError(truth_errors[key[0]])
            truth = truths.get(key[0])
            summary, trace, archive = validate_run_artifacts(
                baseline_root=baseline_root, plan=plan, plan_sha256=plan_sha,
                run_row=row, truth=truth,
            )
            metric, curve = _run_metrics(
                summary, trace, archive, truth,
                assay_id=spec["evaluation_contract"]["evaluation_assay_id"],
            )
            if not with_truth:
                metric["universe_size"] = None
                metric["known_binary_count"] = None
                metric["known_positive_new_count"] = None
                metric["missing_count"] = None
                metric["ambiguous_count"] = None
                metric["label_conflict_count"] = None
                metric["known_positive_recall"] = None
                metric["known_positive_recall_null_reason"] = "truth_unavailable"
                metric["unknown_truth_count"] = None
            metrics_rows.append(_metric_serializable(metric))
            curves.append(curve)
        except Exception as exc:
            result["status"] = "failed"
            result["errors"].append(str(exc))
            metrics_rows.append({
                "run_id": key[1], "snapshot_revision": key[0], "evaluation_status": "failed",
                "error": str(exc),
            })
        validations.append(result)

    aggregates = {}
    for revision in spec["snapshots"]:
        snapshot_rows = [row for row in metrics_rows if row.get("snapshot_revision") == revision and row.get("evaluation_status") == "complete"]
        fixed = [row for row in snapshot_rows if row.get("selector", {}).get("kind") == "fixed_order"]
        seeded = [row for row in snapshot_rows if row.get("selector", {}).get("kind") == "seeded_random_priority"]
        snapshot_curves = [curve for curve in curves if curve["snapshot_revision"] == revision]
        aggregates[revision] = {
            "complete": len(snapshot_rows) == 6 and all(item["status"] == "passed" for item in validations if item["revision"] == revision),
            "fixed_order": fixed[0] if len(fixed) == 1 else None,
            "seeded_random_priority": _aggregate_group(seeded),
            "seeded_random_runs": [{"run_id": row["run_id"], "seed": row["selector"]["seed"], "metrics": row} for row in seeded],
            "common_axis_comparison": _common_endpoints(snapshot_rows, snapshot_curves),
        }
    validation_doc = {
        "schema_version": "assaypilot.stage3c.validation.v1",
        "evaluation_id": spec["evaluation_id"],
        "overall_status": "complete" if validations and all(item["status"] == "passed" for item in validations) and len(validations) == 12 else "incomplete",
        "truth_adapter_enabled": with_truth,
        "runs": validations,
    }
    metrics_doc = {
        "schema_version": "assaypilot.stage3c.run-metrics.v1",
        "evaluation_id": spec["evaluation_id"], "runs": metrics_rows,
    }
    curves_doc = {
        "schema_version": "assaypilot.stage3c.curves.v1",
        "evaluation_id": spec["evaluation_id"], "runs": curves,
        "common_axes": spec["common_axes"],
    }
    aggregate_doc = {
        "schema_version": "assaypilot.stage3c.aggregate-metrics.v1",
        "evaluation_id": spec["evaluation_id"], "complete": validation_doc["overall_status"] == "complete",
        "snapshots": aggregates,
    }
    docs = {
        "validation.json": validation_doc,
        "run_metrics.json": metrics_doc,
        "curves.json": curves_doc,
        "aggregate_metrics.json": aggregate_doc,
    }
    for filename, document in docs.items():
        path = output_dir / filename
        if path.exists():
            raise FileExistsError(f"refusing to overwrite evaluation output: {path}")
        path.write_text(json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8")
    figures = output_dir / "figures"
    figures.mkdir(exist_ok=False)
    for revision in spec["snapshots"]:
        snapshot_curves = [curve for curve in curves if curve["snapshot_revision"] == revision]
        name = "r2" if revision.endswith("-r2") else "expanded"
        for axis in ("step", "cost"):
            path = figures / f"{name}-cumulative-positive-by-{axis}.svg"
            path.write_text(_svg_curve(f"{revision}: released follow-up Active by {axis}", snapshot_curves, axis=axis), encoding="utf-8")
    _write_comparison(output_dir / "comparison.md", validation_doc, metrics_rows, aggregates, spec)
    return validation_doc


def _write_comparison(path: Path, validation: dict[str, Any], rows: list[dict[str, Any]], aggregates: dict[str, Any], spec: dict[str, Any]) -> None:
    output = [
        "# Stage 3-C baseline evaluation",
        "",
        f"Evaluation: `{spec['evaluation_id']}`; validation status: **{validation['overall_status']}**.",
        "",
        "Only released `mep2-confirmatory` observations count as newly found results. `active` is positive, `inactive` negative, and inconclusive/unspecified/conflicted results remain unknown or ambiguous. The primary-screen label is only an eligibility prerequisite.",
        "",
        "Recall denominator is the snapshot-known binary positive set inside the public prerequisite-eligible candidate universe, excluding an identical follow-up result already public at run start. It is not recall over unmeasured compounds or the biological target in general.",
        "",
        "`synthetic_credit` is an assumed replay budget unit, not an experimental cost. `H/L` is the active fraction among newly released binary results, not precision. Recovery controls are excluded.",
        "",
    ]
    for revision, aggregate in aggregates.items():
        output.extend([f"## {revision}", "", "| selector / seed | steps | released | no_record | spent | new Active H | new binary L | H/L | known-positive recall | first positive step |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"])
        subset = [row for row in rows if row.get("snapshot_revision") == revision]
        for row in sorted(subset, key=lambda item: (item.get("selector", {}).get("kind", ""), -1 if item.get("selector", {}).get("seed") is None else item["selector"]["seed"])):
            selector = row.get("selector", {})
            name = "fixed_order" if selector.get("kind") == "fixed_order" else f"random seed {selector.get('seed')}"
            if row.get("evaluation_status") != "complete":
                output.append(f"| {name} | — | — | — | — | — | — | — | — | — |")
                continue
            output.append(
                f"| {name} | {row['durable_steps']} | {row['released_executions']} | {row['no_record_executions']} | {row['spent']} | {row['new_followup_positive_count']} | {row['newly_labeled_count']} | {_fmt(row['observed_positive_fraction'])} | {_fmt(row['known_positive_recall'])} | {_fmt(row['first_positive_step'])} |"
            )
        random_aggregate = aggregate["seeded_random_priority"]
        h = random_aggregate["new_followup_positive_count"]
        rec = random_aggregate["known_positive_recall"]
        output.extend([
            "",
            f"Seeded random n(H)={h['n']}, mean={_fmt(h['mean'])}, sample SD={_fmt(h['sample_sd'])}, range={_fmt(h['min'])}–{_fmt(h['max'])}; recall n={rec['n']}, mean={_fmt(rec['mean'])}, sample SD={_fmt(rec['sample_sd'])}.",
            "",
            f"Common actual range: step 1–{aggregate['common_axis_comparison']['common_step_range_end']}; spent 0–{aggregate['common_axis_comparison']['common_spent_range_end']} synthetic_credit. Curves stop at observed states; cost duplicates are retained.",
            "",
            f"Curves: `figures/{'r2' if revision.endswith('-r2') else 'expanded'}-cumulative-positive-by-step.svg` and `figures/{'r2' if revision.endswith('-r2') else 'expanded'}-cumulative-positive-by-cost.svg`.",
            "",
        ])
    output.extend([
        "## Interpretation limits", "",
        "These two snapshots are evaluated separately. Their repeated records do not establish missing-at-random coverage, and five seeds measure order variation on the same campaign rather than five independent targets. No ROC/PR AUC, enrichment factor, significance test, or winner claim is calculated.",
        "",
    ])
    path.write_text("\n".join(output), encoding="utf-8")


def _fmt(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--register-spec", nargs=3, metavar=("BASELINE_DIR", "OUTPUT_DIR", "ASSAY_ID"))
    commands.add_argument("--evaluate", type=Path, metavar="SPEC_PATH")
    parser.add_argument("--without-truth", action="store_true", help="evaluate operations and released labels only")
    args = parser.parse_args(argv)
    if args.register_spec:
        baseline, output, assay = args.register_spec
        path = register_evaluation_spec(ROOT / baseline, ROOT / output, evaluation_assay_id=assay)
        print(json.dumps({"evaluation_spec": path.relative_to(ROOT).as_posix(), "sha256": sha256_file(path)}, indent=2))
        return 0
    result = evaluate_registered(args.evaluate, with_truth=not args.without_truth)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["overall_status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
