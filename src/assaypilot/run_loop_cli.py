"""Command line entry point for bounded Stage 3-A replay runs."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.domain import Cost, DataSource
from assaypilot.execution import ExecutionCoordinator
from assaypilot.replay import ReplayOracle, load_replay_store
from assaypilot.run_loop import (
    IsolatedSelector,
    RANDOM_PRIORITY_VERSION,
    RunLoopConfig,
    RunLoopController,
    RunLoopError,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="assaypilot-loop")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="start a new bounded replay loop")
    start.add_argument("--snapshot", type=Path, required=True)
    start.add_argument("--runtime-db", type=Path, required=True)
    start.add_argument("--run-id", required=True)
    start.add_argument("--budget", required=True)
    start.add_argument("--budget-unit", required=True)
    start.add_argument("--budget-assumed", action=argparse.BooleanOptionalAction, default=True)
    start.add_argument("--cost-policy-version", required=True)
    start.add_argument("--approval-policy", choices=("bounded_replay",), required=True)
    start.add_argument("--approver-id", required=True)
    start.add_argument(
        "--selector", choices=("fixed_order", "seeded_random_priority"), default="fixed_order",
    )
    start.add_argument("--seed", type=int)
    start.add_argument(
        "--selector-algorithm-version", choices=(RANDOM_PRIORITY_VERSION,),
    )
    start.add_argument("--max-steps", type=int, required=True)
    start.add_argument("--max-duration-seconds", type=int, required=True)
    start.add_argument("--max-action-retries", type=int, default=2)
    start.add_argument("--max-release-retries", type=int, default=3)
    start.add_argument("--selector-timeout-seconds", type=float, default=5.0)
    start.add_argument("--private-canary", type=Path)

    resume = commands.add_parser("resume", help="resume a persisted Stage 3-A run")
    resume.add_argument("--snapshot", type=Path, required=True)
    resume.add_argument("--runtime-db", type=Path, required=True)
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--private-canary", type=Path)
    return parser


def _load_public_and_oracle(snapshot_root: Path):
    root = snapshot_root.expanduser().resolve()
    public = PublicBundleAdapter().load(DataSource(
        kind="public_bundle", location=str(root / "bundle/public/manifest.json"),
    ))
    oracle = ReplayOracle(load_replay_store(root, public))
    return root, public, oracle


def _runtime_database_path(snapshot_root: Path, database: Path) -> Path:
    """Resolve a private runtime path and refuse destinations inside a snapshot."""
    root = snapshot_root.expanduser().resolve()
    path = database.expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return path
    raise RunLoopError(
        "runtime_database_inside_snapshot",
        "runtime database must be outside the preserved snapshot directory",
    )


def _read_saved_config(database: Path, run_id: str) -> RunLoopConfig:
    try:
        with sqlite3.connect(database) as db:
            row = db.execute(
                "SELECT config_json, config_sha256 FROM loop_runs WHERE run_id = ?", (run_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        raise RunLoopError("loop_not_found", "runtime database has no readable Stage 3-A run") from exc
    if row is None:
        raise RunLoopError("loop_not_found", "runtime database has no persisted controller for this run")
    config = RunLoopConfig.from_json(row[0], expected_sha256=row[1])
    if config.run_id != run_id or config.runtime_database != str(database):
        raise RunLoopError("resume_binding_mismatch", "saved run ID or runtime database binding differs")
    return config


def _clock(public):
    initial_as_of = public.as_of.astimezone(timezone.utc)
    return lambda: max(datetime.now(timezone.utc), initial_as_of)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "start":
            if args.selector == "seeded_random_priority":
                if args.seed is None or args.selector_algorithm_version is None:
                    raise RunLoopError(
                        "invalid_selector_config",
                        "seeded_random_priority requires --seed and --selector-algorithm-version",
                    )
            elif args.seed is not None or args.selector_algorithm_version is not None:
                raise RunLoopError(
                    "invalid_selector_config", "--seed and selector version apply only to seeded_random_priority",
                )
        snapshot_root, public, oracle = _load_public_and_oracle(args.snapshot)
        database = _runtime_database_path(snapshot_root, args.runtime_db)
        coordinator = ExecutionCoordinator(
            database, public, oracle,
            cost_policy_version=(args.cost_policy_version if args.command == "start"
                                 else _read_saved_config(database, args.run_id).cost_policy_version),
            clock=_clock(public),
        )
        if args.command == "start":
            config = RunLoopConfig(
                run_id=args.run_id,
                snapshot_id=oracle.store.snapshot_id,
                runtime_database=str(database),
                initial_budget=Cost(
                    amount=args.budget, unit=args.budget_unit, assumed=args.budget_assumed,
                ),
                cost_policy_version=args.cost_policy_version,
                approval_policy=args.approval_policy,
                approver_id=args.approver_id,
                selector_kind=args.selector,
                max_steps=args.max_steps,
                max_duration_seconds=args.max_duration_seconds,
                max_action_retries=args.max_action_retries,
                max_release_retries=args.max_release_retries,
                selector_timeout_seconds=args.selector_timeout_seconds,
                selector_seed=args.seed,
                selector_algorithm_version=args.selector_algorithm_version,
            )
            selector_timeout = config.selector_timeout_seconds
        else:
            config = _read_saved_config(database, args.run_id)
            if config.snapshot_id != oracle.store.snapshot_id:
                raise RunLoopError("snapshot_mismatch", "provided snapshot differs from the persisted run")
            if config.runtime_database != str(database):
                raise RunLoopError("database_mismatch", "provided runtime DB differs from the persisted run")
            selector_timeout = config.selector_timeout_seconds

        with IsolatedSelector(
            selector_kind=config.selector_kind,
            seed=config.selector_seed,
            algorithm_version=config.selector_algorithm_version,
            timeout_seconds=selector_timeout,
            private_canary_path=args.private_canary,
        ) as selector:
            controller = RunLoopController(coordinator, selector)
            if args.command == "start":
                summary = controller.start(config)
            else:
                summary = controller.resume(args.run_id)
        print(json.dumps(asdict(summary), sort_keys=True, indent=2))
        return 0
    except (RunLoopError, ValueError, OSError) as exc:
        error = getattr(exc, "code", "configuration_error")
        print(json.dumps({"error": error, "message": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
