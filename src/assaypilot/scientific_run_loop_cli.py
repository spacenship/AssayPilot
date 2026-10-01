"""Start/resume Stage 5-B public scientific reasoning replay runs."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.domain import Cost, DataSource
from assaypilot.execution import ExecutionCoordinator
from assaypilot.llm_provider import LLMProviderError, LLMSettings
from assaypilot.replay import ReplayOracle, load_replay_store
from assaypilot.run_loop import RunLoopConfig, RunLoopController, RunLoopError
from assaypilot.scientific_reasoner import PROMPT_VERSION
from assaypilot.scientific_run_loop import (
    ScientificReasoningSelector, config_science_fields, safe_settings_fingerprint,
)
from assaypilot.scientific_context import CONTEXT_SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SNAPSHOT = ROOT / "data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all"
DEFAULT_RUN_ID = "stage5b-" + uuid4().hex


def _parser():
    parser = argparse.ArgumentParser(prog="assaypilot-stage5b")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="start a bounded Stage 5-B replay run")
    start.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    start.add_argument("--runtime-db", type=Path)
    start.add_argument("--run-id", default=DEFAULT_RUN_ID)
    start.add_argument("--env-file", type=Path, default=ROOT / ".env.stage5a.local")
    start.add_argument("--budget", default="5")
    start.add_argument("--budget-unit", default="synthetic_credit")
    start.add_argument("--max-steps", type=int, default=10)
    start.add_argument("--max-duration-seconds", type=int, default=300)
    start.add_argument("--max-llm-calls", type=int, default=24)
    start.add_argument("--shortlist-size", type=int, default=24)
    start.add_argument("--shortlist-seed", type=int, default=3)
    start.add_argument("--max-stale-redecisions", type=int, default=2)
    start.add_argument("--output-tokens", type=int, default=1200)

    resume = commands.add_parser("resume", help="resume a persisted Stage 5-B run")
    resume.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    resume.add_argument("--runtime-db", type=Path, required=True)
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--env-file", type=Path, default=ROOT / ".env.stage5a.local")
    return parser


def _load_snapshot(snapshot: Path):
    root = snapshot.expanduser().resolve()
    public = PublicBundleAdapter().load(DataSource(
        kind="public_bundle", location=str(root / "bundle/public/manifest.json"),
    ))
    oracle = ReplayOracle(load_replay_store(root, public))
    return root, public, oracle


def _runtime_path(snapshot_root: Path, run_id: str, supplied: Path | None) -> Path:
    path = (supplied or ROOT / "runtime/stage5b" / run_id / "private/execution.sqlite").expanduser().resolve()
    try:
        path.relative_to(snapshot_root)
    except ValueError:
        pass
    else:
        raise RunLoopError("runtime_database_inside_snapshot", "runtime database must be outside the preserved snapshot")

    managed_root = (ROOT / "runtime/stage5b").resolve()
    if supplied is None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        relative = path.relative_to(managed_root)
    except ValueError:
        return path
    if not relative.parts:
        raise RunLoopError("invalid_runtime_database", "runtime database must be a file below the Stage 5-B runtime root")
    current = managed_root
    directories = [current]
    for part in relative.parts[:-1]:
        current = current / part
        directories.append(current)
    for directory in directories:
        if directory.exists():
            try:
                os.chmod(directory, 0o700)
            except OSError as exc:
                raise RunLoopError("private_runtime_permissions", "Stage 5-B runtime directory permissions could not be protected") from exc
    return path


def _saved_config(database: Path, run_id: str) -> RunLoopConfig:
    try:
        with sqlite3.connect(database) as db:
            row = db.execute(
                "SELECT config_json,config_sha256 FROM loop_runs WHERE run_id=?", (run_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise RunLoopError("loop_not_found", "runtime database has no readable Stage 5-B run") from exc
    if row is None:
        raise RunLoopError("loop_not_found", "runtime database has no Stage 5-B run")
    config = RunLoopConfig.from_json(row[0], expected_sha256=row[1])
    if config.run_id != run_id or config.runtime_database != str(database):
        raise RunLoopError("resume_binding_mismatch", "saved run or database binding differs")
    return config


def _clock(public):
    initial = public.as_of.astimezone(timezone.utc)
    return lambda: max(datetime.now(timezone.utc), initial)


def _settings(args):
    return LLMSettings.from_environment(env_file=args.env_file.expanduser().resolve())


def _new_config(args, coordinator, settings, database):
    fingerprint = safe_settings_fingerprint(
        settings, output_tokens=args.output_tokens,
        shortlist_size=args.shortlist_size, shortlist_seed=args.shortlist_seed,
        max_llm_calls=args.max_llm_calls,
    )
    values = config_science_fields(
        settings, settings_sha256=fingerprint, output_tokens=args.output_tokens,
        shortlist_size=args.shortlist_size, shortlist_seed=args.shortlist_seed,
        max_llm_calls=args.max_llm_calls,
        max_stale_redecisions=args.max_stale_redecisions,
    )
    return RunLoopConfig(
        run_id=args.run_id, snapshot_id=coordinator.snapshot_id,
        runtime_database=str(database),
        initial_budget=Cost(amount=args.budget, unit=args.budget_unit, assumed=False),
        cost_policy_version=coordinator.cost_policy_version,
        approval_policy="bounded_replay", approver_id="stage5b-bounded-replay-policy",
        selector_kind="scientific_reasoner", max_steps=args.max_steps,
        max_duration_seconds=args.max_duration_seconds, max_action_retries=1,
        max_release_retries=2, selector_timeout_seconds=5.0, **values,
    )


def _selector(config, settings, coordinator):
    return ScientificReasoningSelector(
        coordinator, settings, research_goal=(
            "For compounds with a primary Active Activity Outcome in PubChem assay AID 2016, "
            "interpret released categorical evidence only within the named yeast TOR-pathway "
            "GFP confirmatory assay AID 2272 and propose an eligible replay action. This is "
            "not a direct-binding or therapeutic-efficacy claim."
        ),
        output_tokens=config.science_output_tokens,
        shortlist_size=config.science_shortlist_size,
        shortlist_seed=config.science_shortlist_seed,
        max_llm_calls=config.science_max_llm_calls,
        interpretation_reserve_calls=config.science_interpretation_reserve_calls,
        max_stale_redecisions=config.science_max_stale_redecisions,
    )


def _write_private(path: Path, value) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _scientific_metrics(decisions, calls, steps, hypotheses, history,
                        interpretations, interpretation_status, summary):
    decision_by_id = {
        row["decision_id"]: {"status": row["status"], "mode": row["decision_mode"]}
        for row in decisions
    }
    calls_by_decision: dict[str, int] = {}
    for row in calls:
        decision_id = row["decision_id"]
        calls_by_decision[decision_id] = calls_by_decision.get(decision_id, 0) + 1
    action_calls = finalization_calls = stale_calls = 0
    action_initial_calls = finalization_initial_calls = 0
    action_repairs = finalization_repairs = 0
    valid_decisions = stale_decisions = failed_calls = 0
    for decision_id, meta in decision_by_id.items():
        count = calls_by_decision.get(decision_id, 0)
        if meta["status"] in {"applied", "stale"}:
            valid_decisions += 1
        if meta["status"] == "stale":
            stale_decisions += 1
            stale_calls += count
        if meta["mode"] == "interpretation_only":
            finalization_calls += count
            finalization_initial_calls += count > 0
            finalization_repairs += max(0, count - 1)
        else:
            action_calls += count
            action_initial_calls += count > 0
            action_repairs += max(0, count - 1)
    failed_calls = sum(row["status"] == "failed" for row in calls)

    token_usage: dict[str, int] = {}
    usage_reported_calls = 0
    usage_unreported_calls = 0
    for row in calls:
        usage = row.get("usage") or {}
        fields = {
            key: value for key, value in usage.items()
            if key.endswith("_tokens") and isinstance(value, int) and not isinstance(value, bool)
        }
        if fields:
            usage_reported_calls += 1
            for key, value in fields.items():
                token_usage[key] = token_usage.get(key, 0) + value
        else:
            usage_unreported_calls += 1
    if not token_usage:
        token_usage = None

    hypotheses_by_id = {row["hypothesis"]["hypothesis_id"]: row["hypothesis"]
                        for row in hypotheses}
    hypothesis_metrics = {}
    for kind in ("assay_activity", "data_availability"):
        selected = [item for item in hypotheses_by_id.values()
                    if item.get("hypothesis_kind") == kind]
        status_counts = {}
        for item in selected:
            status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
        updated = sum(
            row["event_type"] == "updated"
            and hypotheses_by_id.get(row["hypothesis_id"], {}).get("hypothesis_kind") == kind
            for row in history
        )
        hypothesis_metrics[kind] = {
            "current_count": len(selected), "current_by_status": status_counts,
            "updates": updated,
        }

    interpreted_ids = {
        item["interpretation"].get("observation_id") for item in interpretations
        if item["interpretation"].get("outcome") != "no_record"
        and item["interpretation"].get("observation_id") is not None
    }
    pending_ids = interpretation_status["pending_observation_ids"]
    return {
        "schema_version": "assaypilot.stage5b.metrics.v1",
        "schema_valid_decisions": valid_decisions,
        "stale_decisions_not_applied": stale_decisions,
        "api_calls": {
            "total": len(calls), "action_decision_calls": action_calls,
            "action_decision_initial_calls": action_initial_calls,
            "stale_redecision_calls": stale_calls,
            "finalization_calls": finalization_calls,
            "finalization_initial_calls": finalization_initial_calls,
            "action_repairs": action_repairs,
            "finalization_repairs": finalization_repairs,
            "failed": failed_calls,
        },
        "replay_executions": int(summary.unique_executions),
        "no_record_replay_executions": int(summary.no_record_executions),
        "failed_replay_executions": int(summary.failed_executions),
        "released_observations": int(summary.observations_added),
        "interpreted_new_observations": len(interpreted_ids),
        "pending_new_observations": len(pending_ids),
        "interpretation_complete": bool(interpretation_status["interpretation_complete"]),
        "no_observations_to_interpret": (
            len(interpreted_ids) == 0 and len(pending_ids) == 0
            and int(summary.observations_added) == 0
        ),
        "hypotheses": hypothesis_metrics,
        "api_token_usage_by_field": token_usage,
        "api_usage_reported_calls": usage_reported_calls,
        "api_usage_unreported_calls": usage_unreported_calls,
        "replay_budget": {
            "spent": summary.spent, "reserved": summary.reserved,
            "available": summary.available, "unit": summary.unit,
        },
        "termination_reason": summary.stop_reason,
    }


def _export_run(database: Path, config: RunLoopConfig, summary) -> Path:
    root = database.parent.parent / "artifacts"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    existing = [int(p.name.removeprefix("rev-")) for p in root.glob("rev-[0-9]*") if p.is_dir()]
    revision = max(existing, default=0) + 1
    target = root / f"rev-{revision:06d}"
    target.mkdir(mode=0o700)
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        decisions = [dict(row) for row in db.execute(
            "SELECT decision_no,decision_id,state_version,context_digest,context_json,status,result_json,"
            "validation_json,selected_candidate_id,selected_assay_id,action_step_no,error_code "
            "FROM loop_scientific_decisions WHERE run_id=? ORDER BY decision_no", (config.run_id,),
        )]
        calls = [dict(row) for row in db.execute(
            "SELECT call_no,decision_id,status,provider,model,request_id,latency_ms,transport_attempts,"
            "usage_json,error_code,diagnostic_json,started_at,completed_at FROM loop_scientific_api_calls "
            "WHERE run_id=? ORDER BY call_no", (config.run_id,),
        )]
        steps = [dict(row) for row in db.execute(
            "SELECT step_no,action_id,request_id,candidate_id,assay_id,view_state_version,view_digest,"
            "scientific_decision_id,status,execution_id,error_code,budget_spent,budget_reserved,budget_available "
            "FROM loop_steps WHERE run_id=? ORDER BY step_no", (config.run_id,),
        )]
        hypotheses = [dict(row) for row in db.execute(
            "SELECT hypothesis_id,hypothesis_json,updated_decision_id,updated_at FROM loop_scientific_hypotheses "
            "WHERE run_id=? ORDER BY hypothesis_id", (config.run_id,),
        )]
        history = [dict(row) for row in db.execute(
            "SELECT event_no,decision_id,hypothesis_id,event_type,previous_status,new_status,event_json,created_at "
            "FROM loop_scientific_hypothesis_events WHERE run_id=? ORDER BY event_no", (config.run_id,),
        )]
        interpretations = [dict(row) for row in db.execute(
            "SELECT observation_id,decision_id,interpretation_json,created_at "
            "FROM loop_scientific_interpretations WHERE run_id=? ORDER BY observation_id", (config.run_id,),
        )]
        run_state_row = db.execute(
            "SELECT interpretation_complete,pending_observation_ids_json,incomplete_reason,updated_at "
            "FROM loop_scientific_run_state WHERE run_id=?", (config.run_id,),
        ).fetchone()
    for row in decisions:
        context = json.loads(row.pop("context_json"))
        row["decision_mode"] = context["decision_mode"]
        _write_private(target / "contexts" / f"context-{row['decision_no']:04d}.json", context)
        result = row.pop("result_json")
        row["result"] = json.loads(result) if result else None
        row["validation_history"] = json.loads(row.pop("validation_json"))
    for row in calls:
        row["usage"] = json.loads(row.pop("usage_json"))
        diagnostic = row.pop("diagnostic_json")
        row["diagnostic"] = json.loads(diagnostic) if diagnostic else None
    for row in hypotheses:
        row["hypothesis"] = json.loads(row.pop("hypothesis_json"))
    for row in history:
        row["event"] = json.loads(row.pop("event_json"))
    for row in interpretations:
        row["interpretation"] = json.loads(row.pop("interpretation_json"))
    state = dict(run_state_row) if run_state_row else {
        "interpretation_complete": 0, "pending_observation_ids_json": "[]",
        "incomplete_reason": "no_decision_artifact", "updated_at": None,
    }
    state["pending_observation_ids"] = json.loads(state.pop("pending_observation_ids_json"))
    metrics = _scientific_metrics(decisions, calls, steps, hypotheses, history,
                                  interpretations, state, summary)
    _write_private(target / "configuration.json", config.as_json_object())
    _write_private(target / "decisions.json", decisions)
    _write_private(target / "api_calls.json", calls)
    _write_private(target / "execution_steps.json", steps)
    _write_private(target / "hypotheses.json", hypotheses)
    _write_private(target / "hypothesis_history.json", history)
    _write_private(target / "interpretations.json", interpretations)
    _write_private(target / "interpretation_status.json", state)
    _write_private(target / "summary.json", asdict(summary) | {"scientific_metrics": metrics})
    return target


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = _settings(args)
        snapshot_root, public, oracle = _load_snapshot(args.snapshot)
        if args.command == "start":
            database = _runtime_path(snapshot_root, args.run_id, args.runtime_db)
            coordinator = ExecutionCoordinator(
                database, public, oracle, cost_policy_version="stage5b-public-replay-cost-v1",
                clock=_clock(public),
            )
            config = _new_config(args, coordinator, settings, database)
        else:
            database = _runtime_path(snapshot_root, args.run_id, args.runtime_db)
            config = _saved_config(database, args.run_id)
            if config.selector_kind != "scientific_reasoner":
                raise RunLoopError("resume_binding_mismatch", "saved run is not a Stage 5-B scientific run")
            coordinator = ExecutionCoordinator(
                database, public, oracle, cost_policy_version=config.cost_policy_version,
                clock=_clock(public),
            )
            fingerprint = safe_settings_fingerprint(
                settings, output_tokens=config.science_output_tokens,
                shortlist_size=config.science_shortlist_size,
                shortlist_seed=config.science_shortlist_seed,
                max_llm_calls=config.science_max_llm_calls,
                interpretation_reserve_calls=config.science_interpretation_reserve_calls,
            )
            if fingerprint != config.science_settings_sha256:
                raise RunLoopError("resume_settings_mismatch", "saved provider/model/request settings differ")
        selector = _selector(config, settings, coordinator)
        controller = RunLoopController(coordinator, selector)
        summary = (controller.start(config) if args.command == "start"
                   else controller.resume(args.run_id))
        artifacts = _export_run(database, config, summary)
        print(json.dumps({"summary": asdict(summary), "artifacts": str(artifacts)}, sort_keys=True, indent=2))
        return 0
    except (LLMProviderError, RunLoopError, ValueError, OSError, sqlite3.Error) as exc:
        code = getattr(exc, "code", "configuration_error")
        print(json.dumps({"error": code, "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
