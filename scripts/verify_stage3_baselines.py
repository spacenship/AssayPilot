#!/usr/bin/env python
"""Pre-register and run reproducible Stage 3-B selector baselines.

The output contains only run summaries and information already public through
the campaign/runtime publication boundary. Replay data remains in a temporary
private SQLite database and is removed when each run finishes.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import argparse
import json
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.domain import Cost, DataSource
from assaypilot.execution import ExecutionCoordinator
from assaypilot.replay import ReplayLookupResult, ReplayOracle, load_replay_store
from assaypilot.run_loop import (
    IsolatedSelector,
    RANDOM_PRIORITY_VERSION,
    RunLoopConfig,
    RunLoopController,
)


ROOT = Path(__file__).resolve().parents[1]
BASELINE_ID = "20260930-seeded-priority-v1"
DEFAULT_OUTPUT = ROOT / "reports/stage3/baselines" / BASELINE_ID
SNAPSHOTS = (
    ROOT / "data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2",
    ROOT / "data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all",
)
IMPLEMENTATION_FILES = (
    "src/assaypilot/run_loop.py",
    "src/assaypilot/selector_worker.py",
    "src/assaypilot/run_loop_cli.py",
    "src/assaypilot/execution.py",
    "src/assaypilot/replay.py",
    "tests/test_run_loop.py",
    "tests/test_run_loop_cli.py",
    "scripts/verify_stage3_baselines.py",
)
COST_POLICY_VERSION = "preserved-public-assay-cost-v1"
INITIAL_BUDGET = Cost(amount=Decimal("5"), unit="synthetic_credit", assumed=True)
MAX_DURATION_SECONDS = 300
MAX_ACTION_RETRIES = 1
MAX_RELEASE_RETRIES = 2
SELECTOR_TIMEOUT_SECONDS = 5.0


class RecordingOracle:
    """Count trusted replay requests without copying their private values."""

    def __init__(self, oracle: ReplayOracle) -> None:
        self._oracle = oracle
        self.store = oracle.store
        self.lookup_count = 0

    def lookup(self, candidate_id: str, assay_id: str) -> ReplayLookupResult:
        self.lookup_count += 1
        return self._oracle.lookup(candidate_id, assay_id)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _public_file_hashes(snapshot_root: Path) -> dict[str, str]:
    public_root = snapshot_root / "bundle/public"
    return {
        path.relative_to(public_root).as_posix(): _sha256(path)
        for path in sorted(public_root.rglob("*")) if path.is_file()
    }


def _public_tree_digest(snapshot_root: Path) -> str:
    return sha256(_canonical_json(_public_file_hashes(snapshot_root))).hexdigest()


def _implementation_fingerprint() -> dict[str, Any]:
    files = {relative: _sha256(ROOT / relative) for relative in IMPLEMENTATION_FILES}
    try:
        import subprocess

        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
            capture_output=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        head = None
    return {
        "git_head": head,
        "file_sha256": files,
        "fingerprint_sha256": sha256(_canonical_json(files)).hexdigest(),
    }


def _selector_specs(revision: str, max_steps: int, *, run_prefix: str = "stage3b") -> list[dict[str, Any]]:
    prefix = "r2" if revision == "revision-20260917-r2" else "expanded"
    specs = [{
        "run_id": f"{run_prefix}-{prefix}-fixed-order",
        "selector_kind": "fixed_order",
        "seed": None,
        "algorithm_version": None,
        "max_steps": max_steps,
        "interrupt_after_steps": None,
        "role": "baseline",
    }]
    for seed in range(5):
        specs.append({
            "run_id": f"{run_prefix}-{prefix}-random-priority-seed-{seed}",
            "selector_kind": "seeded_random_priority",
            "seed": seed,
            "algorithm_version": RANDOM_PRIORITY_VERSION,
            "max_steps": max_steps,
            "interrupt_after_steps": 2 if revision == "revision-20260917-r2" and seed == 0 else None,
            "role": "baseline",
        })
    return specs


def _load_snapshot_metadata(snapshot_root: Path, *, baseline_id: str = BASELINE_ID) -> dict[str, Any]:
    if not snapshot_root.is_dir():
        raise FileNotFoundError(f"required preserved snapshot is missing: {snapshot_root}")
    public = PublicBundleAdapter().load(DataSource(
        kind="public_bundle",
        location=str(snapshot_root / "bundle/public/manifest.json"),
    ))
    if public.campaign.budget.unit != INITIAL_BUDGET.unit:
        raise AssertionError(f"{snapshot_root.name} campaign budget unit differs from the registered unit")
    if len(public.candidates) != (5 if snapshot_root.name == "revision-20260917-r2" else 1682):
        raise AssertionError(f"unexpected public candidate count in {snapshot_root.name}")
    public_hashes = _public_file_hashes(snapshot_root)
    return {
        "revision": snapshot_root.name,
        "snapshot_root": snapshot_root.relative_to(ROOT).as_posix(),
        "snapshot_id": json.loads((snapshot_root / "snapshot_manifest.json").read_text())[
            "snapshot_id"
        ],
        "campaign_id": public.campaign.campaign_id,
        "candidate_count": len(public.candidates),
        "public_manifest_sha256": public_hashes["manifest.json"],
        "public_file_count": len(public_hashes),
        "public_tree_sha256": sha256(_canonical_json(public_hashes)).hexdigest(),
        "max_steps": 5 if snapshot_root.name == "revision-20260917-r2" else 30,
        "runs": _selector_specs(
            snapshot_root.name,
            5 if snapshot_root.name == "revision-20260917-r2" else 30,
            run_prefix=("stage3b" if baseline_id == BASELINE_ID else "stage3c"),
        ),
    }


def register_plan(output_dir: Path, *, baseline_id: str = BASELINE_ID) -> Path:
    if not isinstance(baseline_id, str) or not baseline_id.strip() or "/" in baseline_id or "\\" in baseline_id:
        raise ValueError("baseline_id must be a non-empty directory-safe identifier")
    output_dir = output_dir.resolve()
    plan_path = output_dir / "plan.json"
    if output_dir.exists():
        if any(output_dir.iterdir()):
            raise FileExistsError(f"baseline output directory is not empty: {output_dir}")
    else:
        output_dir.mkdir(parents=True)
    snapshots = [_load_snapshot_metadata(path, baseline_id=baseline_id) for path in SNAPSHOTS]
    run_prefix = "stage3b" if baseline_id == BASELINE_ID else "stage3c"
    recovery = {
        "snapshot_revision": "revision-20260917-r2",
        "seed": 0,
        "algorithm_version": RANDOM_PRIORITY_VERSION,
        "control_run_id": f"{run_prefix}-r2-random-priority-seed-0-uninterrupted-control",
        "max_steps": 5,
        "purpose": "compare the interrupted/resumed seed-0 action order with an uninterrupted run under the same public inputs",
    }
    plan = {
        "schema_version": "assaypilot.stage3b.baseline-plan.v1",
        "baseline_id": baseline_id,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "registration_state": "pre-registered-before-any-baseline-run",
        "execution_command": (
            "conda run -n drug python scripts/verify_stage3_baselines.py "
            f"--execute-plan {output_dir.relative_to(ROOT).as_posix()}/plan.json"
        ),
        "implementation": _implementation_fingerprint(),
        "selector_algorithm": {
            "kind": "seeded_random_priority",
            "version": RANDOM_PRIORITY_VERSION,
            "priority_tuple": "(sha256_digest_bytes, candidate_id, assay_id)",
            "canonical_json": "UTF-8, ensure_ascii=false, separators=(',', ':'), array [version, seed, candidate_id, assay_id]",
        },
        "fixed_conditions": {
            "initial_budget": str(INITIAL_BUDGET.amount),
            "budget_unit": INITIAL_BUDGET.unit,
            "budget_assumed": INITIAL_BUDGET.assumed,
            "cost_policy_version": COST_POLICY_VERSION,
            "approval_policy": "bounded_replay",
            "approver_id": "local-stage3b-bounded-replay-policy",
            "max_duration_seconds": MAX_DURATION_SECONDS,
            "max_action_retries": MAX_ACTION_RETRIES,
            "max_release_retries": MAX_RELEASE_RETRIES,
            "selector_timeout_seconds": SELECTOR_TIMEOUT_SECONDS,
            "isolated_selector": "unshare user/mount/network namespace + read-only chroot Python stdlib worker",
            "private_canary": "a private marker file outside the chroot is checked for denial on each worker start",
            "runtime_db": "fresh temporary private SQLite database per run, outside each snapshot; deleted after safe summary/trace extraction",
        },
        "snapshots": snapshots,
        "recovery_control": recovery,
        "trace_schema_version": "assaypilot.stage3b.public-run-trace.v1",
        "trace_allowlist": [
            "step/action/request/execution IDs", "candidate_id and assay_id",
            "public view state version/digest", "selector reason", "execution status",
            "durable current budget checkpoint", "public observation/evidence IDs",
            "final stop reason",
        ],
        "trace_excludes": [
            "raw hidden or curator arrays", "measurement values from private results",
            "unattempted-candidate record availability", "private runtime database",
        ],
        "publication_archive": {
            "schema_version": "assaypilot.stage3c.published-results.v1",
            "source": "run-bound Publication Reader after successful release",
            "contains": ["released Observation", "EvidenceRef", "allowlisted public evidence payload", "canonical payload SHA-256"],
            "excludes": ["private result dump", "unreleased executions", "unattempted candidates", "snapshot-wide hidden labels"],
        },
    }
    with plan_path.open("xb") as handle:
        handle.write(json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    return plan_path


def _current_clock(public):
    initial_as_of = public.as_of.astimezone(timezone.utc)
    return lambda: max(datetime.now(timezone.utc), initial_as_of)


def _config(coordinator, spec: dict[str, Any], *, approver_id: str) -> RunLoopConfig:
    return RunLoopConfig(
        run_id=spec["run_id"],
        snapshot_id=coordinator.snapshot_id,
        runtime_database=str(coordinator.database_path.resolve()),
        initial_budget=INITIAL_BUDGET,
        cost_policy_version=COST_POLICY_VERSION,
        approval_policy="bounded_replay",
        approver_id=approver_id,
        selector_kind=spec["selector_kind"],
        max_steps=spec["max_steps"],
        max_duration_seconds=MAX_DURATION_SECONDS,
        max_action_retries=MAX_ACTION_RETRIES,
        max_release_retries=MAX_RELEASE_RETRIES,
        selector_timeout_seconds=SELECTOR_TIMEOUT_SECONDS,
        selector_seed=spec["seed"],
        selector_algorithm_version=spec["algorithm_version"],
    )


def _selector(config: RunLoopConfig, canary: Path) -> IsolatedSelector:
    return IsolatedSelector(
        selector_kind=config.selector_kind,
        seed=config.selector_seed,
        algorithm_version=config.selector_algorithm_version,
        timeout_seconds=config.selector_timeout_seconds,
        private_canary_path=canary,
    )


def _order_from_trace(trace: dict[str, Any]) -> list[tuple[str, str]]:
    return [(step["candidate_id"], step["assay_id"]) for step in trace["steps"]]


def _archive_published_execution(reader, published, *, step: dict[str, Any]) -> dict[str, Any]:
    evidence_rows = []
    for observation in published.result.observations:
        for evidence_id in observation.evidence_ids:
            evidence = reader.evidence(evidence_id)
            if sha256(evidence.payload).hexdigest() != evidence.sha256:
                raise AssertionError("public evidence hash failed during archive extraction")
            payload = json.loads(evidence.payload)
            if _canonical_json(payload) != evidence.payload:
                raise AssertionError("public evidence payload differs from its canonical stored bytes")
            evidence_rows.append({
                "reference": evidence.reference.model_dump(mode="json"),
                "sha256": evidence.sha256,
                "payload": payload,
            })
    return {
        "step_no": step["step_no"],
        "action_id": step["action_id"],
        "candidate_id": step["candidate_id"],
        "assay_id": step["assay_id"],
        "execution_id": published.execution_id,
        "receipt": published.receipt.model_dump(mode="json"),
        "published_at": published.published_at.isoformat(),
        "state_version": published.state_version,
        "cost": str(published.cost),
        "unit": published.unit,
        "result": published.result.model_dump(mode="json"),
        "observations": [item.model_dump(mode="json") for item in published.result.observations],
        "evidence": evidence_rows,
    }


def _execute_one(
    snapshot_root: Path, spec: dict[str, Any], output_root: Path, *, plan_sha256: str,
    baseline_id: str = BASELINE_ID,
) -> dict[str, Any]:
    public_hashes_before = _public_file_hashes(snapshot_root)
    public = PublicBundleAdapter().load(DataSource(
        kind="public_bundle", location=str(snapshot_root / "bundle/public/manifest.json"),
    ))
    base_oracle = ReplayOracle(load_replay_store(snapshot_root, public))
    oracle = RecordingOracle(base_oracle)
    run_dir = output_root / "runs" / snapshot_root.name / spec["run_id"]
    run_dir.mkdir(parents=True, exist_ok=False)
    initial_observation_ids = {item.observation_id for item in public.observations}
    started_at = datetime.now(timezone.utc)
    interruption_summary = None
    canary_denied = True

    with tempfile.TemporaryDirectory(prefix="assaypilot-stage3b-private-") as temporary:
        private_root = Path(temporary)
        database = private_root / "runtime" / "execution.sqlite"
        database.parent.mkdir(mode=0o700)
        canary = private_root / "private-canary.txt"
        canary.write_text("stage3b-private-selector-canary", encoding="utf-8")
        coordinator = ExecutionCoordinator(
            database, public, oracle, cost_policy_version=COST_POLICY_VERSION,
            clock=_current_clock(public),
        )
        config = _config(
            coordinator, spec, approver_id="local-stage3b-bounded-replay-policy",
        )
        if spec["interrupt_after_steps"] is not None:
            with _selector(config, canary) as selector:
                interruption_summary = RunLoopController(coordinator, selector).start(
                    config, stop_after_new_steps=spec["interrupt_after_steps"],
                )
                canary_denied = selector.canary_denied
            if (interruption_summary.stop_reason != "user_interrupt"
                    or interruption_summary.selection_steps != spec["interrupt_after_steps"]
                    or not interruption_summary.resumable):
                raise AssertionError("seed-0 recovery run did not persist the registered two-step prefix")
            with _selector(config, canary) as selector:
                summary = RunLoopController(coordinator, selector).resume(spec["run_id"])
                canary_denied = canary_denied and selector.canary_denied
        else:
            with _selector(config, canary) as selector:
                summary = RunLoopController(coordinator, selector).start(config)
                canary_denied = selector.canary_denied

        if not canary_denied:
            raise AssertionError("actual isolated worker did not deny the private canary")
        current = coordinator.get_public_state(spec["run_id"])
        reader = coordinator.public_reader(spec["run_id"])
        with sqlite3.connect(database) as db:
            db.row_factory = sqlite3.Row
            step_rows = db.execute(
                """SELECT step_no, action_id, request_id, candidate_id, assay_id,
                          view_state_version, view_digest, selected_reason, status,
                          execution_id, observations_added, budget_spent,
                          budget_reserved, budget_available, error_code
                   FROM loop_steps WHERE run_id = ? ORDER BY step_no""",
                (spec["run_id"],),
            ).fetchall()
            run_row = db.execute(
                "SELECT config_sha256, stop_reason FROM loop_runs WHERE run_id = ?",
                (spec["run_id"],),
            ).fetchone()
            execution_request_count = db.execute(
                "SELECT COUNT(*) FROM execution_requests WHERE run_id = ?", (spec["run_id"],),
            ).fetchone()[0]
            settlement_count = db.execute(
                "SELECT COUNT(*) FROM release_settlements WHERE run_id = ?", (spec["run_id"],),
            ).fetchone()[0]
            db_budget = db.execute(
                "SELECT initial_budget, spent, reserved, unit FROM runs WHERE run_id = ?",
                (spec["run_id"],),
            ).fetchone()

        trace_steps: list[dict[str, Any]] = []
        archived_executions: list[dict[str, Any]] = []
        public_observation_count = 0
        for row in step_rows:
            published_observations: list[dict[str, Any]] = []
            if row["status"] == "released" and row["execution_id"]:
                published = reader.released_execution(row["execution_id"])
                step_identity = {
                    "step_no": int(row["step_no"]), "action_id": row["action_id"],
                    "candidate_id": row["candidate_id"], "assay_id": row["assay_id"],
                }
                archived_executions.append(_archive_published_execution(
                    reader, published, step=step_identity,
                ))
                for observation in published.result.observations:
                    if not observation.evidence_ids:
                        raise AssertionError("released public observation has no evidence IDs")
                    evidence_refs = []
                    for evidence_id in observation.evidence_ids:
                        evidence = reader.evidence(evidence_id)
                        if sha256(evidence.payload).hexdigest() != evidence.sha256:
                            raise AssertionError("public evidence hash failed during trace extraction")
                        evidence_refs.append({
                            "evidence_id": evidence_id,
                            "sha256": evidence.sha256,
                        })
                    published_observations.append({
                        "observation_id": observation.observation_id,
                        "evidence": evidence_refs,
                    })
                    public_observation_count += 1
            trace_steps.append({
                "step_no": int(row["step_no"]),
                "action_id": row["action_id"],
                "request_id": row["request_id"],
                "execution_id": row["execution_id"],
                "candidate_id": row["candidate_id"],
                "assay_id": row["assay_id"],
                "public_view": {
                    "state_version": int(row["view_state_version"]),
                    "digest": row["view_digest"],
                },
                "selection_reason": row["selected_reason"],
                "status": row["status"],
                "error_code": row["error_code"],
                "budget_after_step": {
                    "spent": row["budget_spent"],
                    "reserved": row["budget_reserved"],
                    "available": row["budget_available"],
                    "unit": db_budget["unit"],
                },
                "published_observations": published_observations,
            })

        trace = {
            "schema_version": "assaypilot.stage3b.public-run-trace.v1",
            "run_id": spec["run_id"],
            "snapshot_revision": snapshot_root.name,
            "selector": {
                "kind": spec["selector_kind"],
                "seed": spec["seed"],
                "algorithm_version": spec["algorithm_version"],
            },
            "public_view_schema_version": "assaypilot.selector-view.v1",
            "stop_reason": summary.stop_reason,
            "budget_current": {
                "total": str(current.state.budget.total),
                "spent": str(current.state.budget.spent),
                "reserved": str(current.state.budget.reserved),
                "available": str(current.state.budget.available),
                "unit": current.state.budget.unit,
            },
            "steps": trace_steps,
        }
        if (len(trace_steps) != summary.selection_steps
                or len({step["action_id"] for step in trace_steps}) != len(trace_steps)
                or len({step["request_id"] for step in trace_steps}) != len(trace_steps)
                or len({step["execution_id"] for step in trace_steps if step["execution_id"]})
                != summary.unique_executions):
            raise AssertionError("trace identifiers/counts differ from durable loop summary")
        if sum(step["status"] == "released" for step in trace_steps) != summary.released_executions:
            raise AssertionError("trace released count differs from run summary")
        if sum(step["status"] == "no_record" for step in trace_steps) != summary.no_record_executions:
            raise AssertionError("trace no_record count differs from run summary")
        if sum(step["status"] == "failed" for step in trace_steps) != summary.failed_executions:
            raise AssertionError("trace failed count differs from run summary")
        runtime_observations = [
            item for item in current.public.observations
            if item.observation_id not in initial_observation_ids
        ]
        if (len(runtime_observations) != summary.observations_added
                or public_observation_count != summary.observations_added):
            raise AssertionError("published observations differ from trace or durable checkpoints")
        if run_row["stop_reason"] != summary.stop_reason:
            raise AssertionError("durable stop reason differs from returned summary")
        if execution_request_count != summary.unique_executions:
            raise AssertionError("resume duplicated an execution request")
        if settlement_count != summary.released_executions:
            raise AssertionError("settlement count differs from the released execution count")
        if oracle.lookup_count != summary.unique_executions:
            raise AssertionError("recovery repeated a private replay lookup")
        if (Decimal(db_budget["initial_budget"]) != INITIAL_BUDGET.amount
                or Decimal(db_budget["spent"]) != Decimal(summary.spent)
                or Decimal(db_budget["reserved"]) != Decimal(summary.reserved)
                or Decimal(summary.spent) + Decimal(summary.reserved) + Decimal(summary.available)
                != INITIAL_BUDGET.amount):
            raise AssertionError("database, summary, and current budget do not agree")

        ended_at = datetime.now(timezone.utc)
        summary_document = {
            "schema_version": "assaypilot.stage3b.run-summary.v1",
            "baseline_id": baseline_id,
            "plan_sha256": plan_sha256,
            "implementation_fingerprint_sha256": _implementation_fingerprint()["fingerprint_sha256"],
            "snapshot_revision": snapshot_root.name,
            "snapshot_id": oracle.store.snapshot_id,
            "public_tree_sha256": _public_tree_digest(snapshot_root),
            "run_id": spec["run_id"],
            "selector": trace["selector"],
            "run_config": config.as_json_object(),
            "run_config_sha256": run_row["config_sha256"],
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "interrupted_after_steps": (
                interruption_summary.selection_steps if interruption_summary else None
            ),
            "interruption_summary": asdict(interruption_summary) if interruption_summary else None,
            "resumed_same_run": interruption_summary is not None,
            "isolated_worker": "unshare user/mount/network namespace + read-only chroot",
            "private_canary_denied": canary_denied,
            "summary": asdict(summary),
            "durable_consistency": {
                "trace_steps": len(trace_steps),
                "execution_requests": execution_request_count,
                "settlements": settlement_count,
                "replay_lookups": oracle.lookup_count,
                "published_observations_in_trace": public_observation_count,
                "private_runtime_database_preserved": False,
            },
        }
        (run_dir / "trace.json").write_bytes(
            json.dumps(trace, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        )
        archive_document = {
            "schema_version": "assaypilot.stage3c.published-results.v1",
            "baseline_id": baseline_id,
            "plan_sha256": plan_sha256,
            "snapshot_revision": snapshot_root.name,
            "snapshot_id": oracle.store.snapshot_id,
            "run_id": spec["run_id"],
            "source": "trusted run-bound Publication Reader; released executions only",
            "executions": archived_executions,
        }
        archive_path = run_dir / "published_results.json"
        archive_path.write_bytes(
            json.dumps(archive_document, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        )
        summary_document["published_results_path"] = archive_path.relative_to(ROOT).as_posix()
        summary_document["published_results_sha256"] = _sha256(archive_path)
        (run_dir / "summary.json").write_bytes(
            json.dumps(summary_document, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        )

    if public_hashes_before != _public_file_hashes(snapshot_root):
        raise AssertionError("public snapshot files changed during the baseline run")
    return {
        "snapshot_revision": snapshot_root.name,
        "run_id": spec["run_id"],
        "selector_kind": spec["selector_kind"],
        "seed": spec["seed"],
        "summary_path": (run_dir / "summary.json").relative_to(ROOT).as_posix(),
        "trace_path": (run_dir / "trace.json").relative_to(ROOT).as_posix(),
        "published_results_path": (run_dir / "published_results.json").relative_to(ROOT).as_posix(),
        "published_results_sha256": _sha256(run_dir / "published_results.json"),
        "summary": asdict(summary),
        "trace": trace,
    }


def execute_plan(plan_path: Path) -> dict[str, Any]:
    plan_path = plan_path.resolve()
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if plan.get("schema_version") != "assaypilot.stage3b.baseline-plan.v1":
        raise ValueError("unsupported baseline plan schema")
    baseline_id = plan.get("baseline_id")
    if not isinstance(baseline_id, str) or not baseline_id:
        raise ValueError("baseline plan ID is missing")
    output_root = plan_path.parent
    if (output_root / "runs").exists() or (output_root / "execution_summary.json").exists():
        raise FileExistsError("baseline run outputs already exist; refusing to overwrite them")
    if plan["implementation"] != _implementation_fingerprint():
        raise RuntimeError("implementation files changed after pre-registration")
    expected_by_revision = {item["revision"]: item for item in plan["snapshots"]}
    for revision, expected in expected_by_revision.items():
        root = ROOT / expected["snapshot_root"]
        if _public_tree_digest(root) != expected["public_tree_sha256"]:
            raise RuntimeError(f"public snapshot changed after pre-registration: {revision}")

    plan_sha256 = _sha256(plan_path)
    suite_started_at = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    for snapshot in plan["snapshots"]:
        snapshot_root = ROOT / snapshot["snapshot_root"]
        for spec in snapshot["runs"]:
            result = _execute_one(
                snapshot_root, spec, output_root, plan_sha256=plan_sha256,
                baseline_id=baseline_id,
            )
            results.append(result)
            summary = result["summary"]
            summary_rows.append({
                "revision": snapshot["revision"],
                "run_id": spec["run_id"],
                "selector": spec["selector_kind"],
                "seed": spec["seed"],
                "durable_steps": summary["selection_steps"],
                "executions": summary["unique_executions"],
                "released": summary["released_executions"],
                "no_record": summary["no_record_executions"],
                "failed": summary["failed_executions"],
                "cancelled": sum(step["status"] == "cancelled" for step in result["trace"]["steps"]),
                "observations_added": summary["observations_added"],
                "summary_path": result["summary_path"],
                "trace_path": result["trace_path"],
                "published_results_path": result["published_results_path"],
                "published_results_sha256": result["published_results_sha256"],
                "spent": summary["spent"],
                "reserved": summary["reserved"],
                "available": summary["available"],
                "unit": summary["unit"],
                "stop_reason": summary["stop_reason"],
            })

    recovery = plan["recovery_control"]
    recovery_spec = {
        "run_id": recovery["control_run_id"],
        "selector_kind": "seeded_random_priority",
        "seed": recovery["seed"],
        "algorithm_version": recovery["algorithm_version"],
        "max_steps": recovery["max_steps"],
        "interrupt_after_steps": None,
        "role": "recovery_control",
    }
    recovery_snapshot = expected_by_revision[recovery["snapshot_revision"]]
    control = _execute_one(
        ROOT / recovery_snapshot["snapshot_root"], recovery_spec, output_root,
        plan_sha256=plan_sha256, baseline_id=baseline_id,
    )
    interrupted_spec = next(
        spec for snapshot in plan["snapshots"] for spec in snapshot["runs"]
        if spec["interrupt_after_steps"] is not None
    )
    interrupted = next(
        item for item in results if item["run_id"] == interrupted_spec["run_id"]
    )
    order_matches = _order_from_trace(interrupted["trace"]) == _order_from_trace(control["trace"])
    if not order_matches:
        raise AssertionError("resumed seed-0 action order differs from the independent uninterrupted control")

    for snapshot in plan["snapshots"]:
        root = ROOT / snapshot["snapshot_root"]
        if _public_tree_digest(root) != snapshot["public_tree_sha256"]:
            raise AssertionError(f"public snapshot hash changed during baseline suite: {snapshot['revision']}")

    result_doc = {
        "schema_version": "assaypilot.stage3b.baseline-results.v1",
        "baseline_id": plan["baseline_id"],
        "plan_path": plan_path.relative_to(ROOT).as_posix(),
        "plan_sha256": plan_sha256,
        "execution_command": plan["execution_command"],
        "started_at": suite_started_at.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "baseline_runs": summary_rows,
        "recovery_check": {
            "interrupted_run_id": interrupted["run_id"],
            "uninterrupted_control_run_id": control["run_id"],
            "same_seed": recovery["seed"],
            "interrupted_after_steps": interrupted_spec["interrupt_after_steps"],
            "resumed_action_order_matches_control": order_matches,
            "control_summary_path": control["summary_path"],
            "control_trace_path": control["trace_path"],
            "control_published_results_path": control["published_results_path"],
            "control_published_results_sha256": control["published_results_sha256"],
        },
        "public_snapshot_hashes_unchanged": True,
        "private_runtime_database_preserved": False,
    }
    result_path = output_root / "execution_summary.json"
    with result_path.open("xb") as handle:
        handle.write(json.dumps(result_doc, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    return result_doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--register-plan", type=Path, metavar="PATH")
    commands.add_argument("--execute-plan", type=Path, metavar="PATH")
    parser.add_argument("--baseline-id", default=BASELINE_ID)
    args = parser.parse_args(argv)
    if args.register_plan:
        path = register_plan(
            args.register_plan.parent if args.register_plan.suffix == ".json" else args.register_plan,
            baseline_id=args.baseline_id,
        )
        print(json.dumps({"registered_plan": str(path), "sha256": _sha256(path)}, indent=2))
        return 0
    result = execute_plan(args.execute_plan)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
