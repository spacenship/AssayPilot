"""CLI path safety checks for Stage 3-A runtime state."""
import pytest

from assaypilot.run_loop import RunLoopError
from assaypilot.run_loop_cli import main
from assaypilot.run_loop_cli import _runtime_database_path


def test_runtime_database_must_be_outside_preserved_snapshot(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()

    with pytest.raises(RunLoopError) as nested:
        _runtime_database_path(snapshot, snapshot / "private" / "runtime.sqlite")
    assert nested.value.code == "runtime_database_inside_snapshot"

    outside = _runtime_database_path(snapshot, tmp_path / "private" / "runtime.sqlite")
    assert outside == (tmp_path / "private" / "runtime.sqlite").resolve()


def test_runtime_database_symlink_cannot_target_preserved_snapshot(tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    alias = tmp_path / "snapshot-alias"
    alias.symlink_to(snapshot, target_is_directory=True)

    with pytest.raises(RunLoopError) as nested:
        _runtime_database_path(snapshot, alias / "runtime.sqlite")
    assert nested.value.code == "runtime_database_inside_snapshot"


def test_cli_rejects_missing_seed_binding_before_opening_snapshot_or_database(tmp_path):
    database = tmp_path / "runtime.sqlite"
    result = main([
        "start", "--snapshot", str(tmp_path / "does-not-exist"),
        "--runtime-db", str(database), "--run-id", "missing-seed",
        "--budget", "1", "--budget-unit", "USD",
        "--cost-policy-version", "fixture-v1",
        "--approval-policy", "bounded_replay", "--approver-id", "fixture-policy",
        "--selector", "seeded_random_priority", "--max-steps", "1",
        "--max-duration-seconds", "60",
    ])
    assert result == 2
    assert not database.exists()
