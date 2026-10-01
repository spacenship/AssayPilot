"""Bounded, resumable public-information replay loop (Stage 3-A).

Selectors receive JSON DTOs only. Approval, execution, release, durable IDs,
policy checks and recovery remain in this trusted controller.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import shutil
import sys
import tempfile
import time
from typing import Callable, Protocol
from uuid import uuid4

from assaypilot.domain import ActionRequest, Cost, Observation, PublicCampaign, Verdict
from assaypilot.execution import (
    ActionRejectedError,
    BudgetError,
    ExecutionControlError,
    ExecutionCoordinator,
    ExecutionReceiptView,
    RunBindingError,
)


MAX_SELECTOR_INPUT_BYTES = 2 * 1024 * 1024
MAX_SELECTOR_OUTPUT_BYTES = 16 * 1024
MAX_SELECTOR_REASON_LENGTH = 240
MAX_IDENTIFIER_LENGTH = 256
RANDOM_PRIORITY_VERSION = "random-priority-v1"
_TERMINAL_STEP_STATES = frozenset({"released", "no_record", "failed", "rejected", "cancelled"})
_INCOMPLETE_STEP_STATES = frozenset({"proposed", "approved", "pending_release"})


class RunLoopError(ValueError):
    """A stable, non-sensitive error at the trusted run-loop boundary."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class FaultInjectedCrash(BaseException):
    """Test-only abrupt-stop signal; intentionally bypasses normal cleanup."""


@dataclass(frozen=True, slots=True)
class CandidateInfo:
    candidate_id: str
    source: str
    source_id: str


@dataclass(frozen=True, slots=True)
class PrerequisiteInfo:
    assay_id: str
    kind: str
    verdict: str | None


@dataclass(frozen=True, slots=True)
class AssayInfo:
    assay_id: str
    name: str
    role: str
    endpoint: str
    unit: str
    cost_amount: str
    cost_unit: str
    prerequisites: tuple[PrerequisiteInfo, ...]


@dataclass(frozen=True, slots=True)
class ObservationInfo:
    observation_id: str
    candidate_id: str
    assay_id: str
    value: float | None
    unit: str | None
    comparison: str | None
    verdict: str


@dataclass(frozen=True, slots=True)
class ExecutableAction:
    candidate_id: str
    assay_id: str
    cost_amount: str
    cost_unit: str


@dataclass(frozen=True, slots=True)
class AttemptSummary:
    candidate_id: str
    assay_id: str
    status: str
    error_code: str | None


@dataclass(frozen=True, slots=True)
class SelectorView:
    """Allowlisted public projection. It contains no evidence or private handles."""

    schema_version: str
    state_version: int
    public_as_of: str
    candidates: tuple[CandidateInfo, ...]
    assays: tuple[AssayInfo, ...]
    observations: tuple[ObservationInfo, ...]
    budget_total: str
    budget_spent: str
    budget_reserved: str
    budget_available: str
    budget_unit: str
    executable_actions: tuple[ExecutableAction, ...]
    attempted_actions: tuple[AttemptSummary, ...]
    view_digest: str

    def as_json_object(self) -> dict[str, object]:
        return _view_object(self, include_digest=True)


@dataclass(frozen=True, slots=True)
class SelectorProposal:
    kind: str
    candidate_id: str | None = None
    assay_id: str | None = None
    reason: str | None = None
    stop_reason: str | None = None


class Selector(Protocol):
    def select(self, view: SelectorView) -> SelectorProposal | dict[str, object]:
        """Return one proposed public action or an explicit stop."""


class FixedOrderSelector:
    """Choose the lexicographically first currently executable public action."""

    selector_kind = "fixed_order"

    def select(self, view: SelectorView) -> SelectorProposal:
        if not view.executable_actions:
            return SelectorProposal(kind="stop", stop_reason="no_executable_actions")
        chosen = min(
            view.executable_actions,
            key=lambda item: (item.candidate_id, item.assay_id),
        )
        return SelectorProposal(
            kind="select", candidate_id=chosen.candidate_id,
            assay_id=chosen.assay_id,
            reason="first executable action in stable (candidate_id, assay_id) order",
        )


class SeededRandomPrioritySelector:
    """Choose by a seed-and-action SHA-256 priority, without PRNG state."""

    selector_kind = "seeded_random_priority"

    def __init__(self, seed: int, algorithm_version: str = RANDOM_PRIORITY_VERSION):
        _validate_random_selector(seed, algorithm_version)
        self.seed = seed
        self.algorithm_version = algorithm_version

    def select(self, view: SelectorView) -> SelectorProposal:
        if not view.executable_actions:
            return SelectorProposal(kind="stop", stop_reason="no_executable_actions")
        chosen = min(
            view.executable_actions,
            key=lambda item: (
                _priority_digest(self.seed, item.candidate_id, item.assay_id, self.algorithm_version),
                item.candidate_id,
                item.assay_id,
            ),
        )
        return SelectorProposal(
            kind="select", candidate_id=chosen.candidate_id,
            assay_id=chosen.assay_id,
            reason="first eligible action by the fixed seeded SHA-256 priority",
        )


def _validate_random_selector(seed: object, algorithm_version: object) -> None:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise RunLoopError("invalid_selector_seed", "seeded_random_priority requires an integer seed")
    if algorithm_version != RANDOM_PRIORITY_VERSION:
        raise RunLoopError("unsupported_selector_version", "seeded_random_priority algorithm version is unsupported")


def _validate_selector_binding(
    selector_kind: object, seed: object, algorithm_version: object,
) -> None:
    if selector_kind == "fixed_order":
        if seed is not None or algorithm_version is not None:
            raise RunLoopError("invalid_selector_config", "fixed_order does not accept a seed or algorithm version")
        return
    if selector_kind == "seeded_random_priority":
        _validate_random_selector(seed, algorithm_version)
        return
    if selector_kind == "scientific_reasoner":
        if seed is not None or algorithm_version is not None:
            raise RunLoopError("invalid_selector_config", "scientific_reasoner does not accept a selector seed")
        return
    raise RunLoopError("unsupported_selector", "selector kind is unsupported")


def _priority_digest(
    seed: int, candidate_id: str, assay_id: str,
    algorithm_version: str = RANDOM_PRIORITY_VERSION,
) -> bytes:
    # Compact JSON array encoding is unambiguous at string boundaries and
    # explicitly UTF-8, independent of Python hash seeds and process state.
    encoded = json.dumps(
        [algorithm_version, seed, candidate_id, assay_id],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).digest()


class IsolatedSelector:
    """Run a configured deterministic selector in a networkless chroot.

    The public catalog is sent once per worker process. Later messages contain
    only newly published observations and the changing budget/action/attempt
    projection, so a large campaign is not copied to the selector on every
    step. There is deliberately no in-process fallback.
    """

    def __init__(
        self, *, selector_kind: str = "fixed_order", seed: int | None = None,
        algorithm_version: str | None = None, timeout_seconds: float = 5.0,
        private_canary_path: str | Path | None = None,
    ):
        _validate_selector_binding(selector_kind, seed, algorithm_version)
        self.selector_kind = selector_kind
        self.seed = seed
        self.algorithm_version = algorithm_version
        self.timeout_seconds = timeout_seconds
        self.private_canary_path = str(private_canary_path) if private_canary_path else ""
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._process: subprocess.Popen[str] | None = None
        self._last_observation_ids: set[str] = set()
        self._canary_denied = False
        self._unshare = shutil.which("unshare")
        self._busybox = shutil.which("busybox")
        self._python_prefix = Path(sys.prefix).resolve()

    @property
    def canary_denied(self) -> bool:
        return self._canary_denied

    def select(self, view: SelectorView) -> SelectorProposal:
        self._ensure_started(view)
        assert self._process is not None and self._process.stdin is not None
        new_observations = [
            _jsonable(item) for item in view.observations
            if item.observation_id not in self._last_observation_ids
        ]
        self._last_observation_ids.update(item.observation_id for item in view.observations)
        message = {
            "kind": "select",
            "state_version": view.state_version,
            "public_as_of": view.public_as_of,
            "new_public_observations": new_observations,
            "budget": {
                "total": view.budget_total, "spent": view.budget_spent,
                "reserved": view.budget_reserved, "available": view.budget_available,
                "unit": view.budget_unit,
            },
            "executable_actions": _jsonable(view.executable_actions),
            "attempted_actions": _jsonable(view.attempted_actions),
        }
        response = self._exchange(message)
        return response

    def close(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        temporary, self._temporary = self._temporary, None
        if temporary is not None:
            temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _ensure_started(self, view: SelectorView) -> None:
        if self._process is not None:
            return
        if not self._unshare or not self._busybox or not (self._python_prefix / "bin/python").is_file():
            raise RunLoopError("selector_sandbox_unavailable", "required namespace, BusyBox, or Python runtime is unavailable")
        self._temporary = tempfile.TemporaryDirectory(prefix="assaypilot-selector-")
        root = Path(self._temporary.name) / "root"
        for relative in (
            "bin", "dev", "home", "usr/lib", "usr/lib64", "tmp",
            str(self._python_prefix).lstrip("/"),
        ):
            (root / relative).mkdir(mode=0o755, parents=True, exist_ok=True)
        (root / "lib").symlink_to("usr/lib")
        (root / "lib64").symlink_to("usr/lib64")
        shutil.copyfile(self._busybox, root / "bin/busybox")
        os.chmod(root / "bin/busybox", 0o555)
        worker_source = Path(__file__).with_name("selector_worker.py")
        shutil.copyfile(worker_source, root / "selector_worker.py")
        os.chmod(root / "selector_worker.py", 0o444)
        (root / "launch.sh").write_text(
            "#!/bin/busybox sh\n"
            "if [ -n \"$CANARY_PATH\" ]; then\n"
            "  if /bin/busybox cat \"$CANARY_PATH\" >/dev/null 2>&1; then\n"
            "    printf '%s\\n' CANARY_LEAKED; exit 91\n"
            "  fi\n"
            "  printf '%s\\n' CANARY_DENIED\n"
            "fi\n"
            "exec \"$PYTHON_PATH\" -I -S -B /selector_worker.py\n",
            encoding="utf-8",
        )
        os.chmod(root / "launch.sh", 0o555)
        for special in ("null", "zero", "urandom"):
            source = Path("/dev") / special
            destination = root / "dev" / special
            destination.touch(exist_ok=True)
        if not Path("/usr/lib").is_dir() or not Path("/usr/lib64").is_dir():
            raise RunLoopError("selector_sandbox_unavailable", "host dynamic runtime libraries are unavailable")

        environment = {
            "PATH": "/usr/bin:/bin",
            "SANDBOX_ROOT": str(root),
            "BUSYBOX_PATH": self._busybox,
            "PYTHON_PREFIX": str(self._python_prefix),
            "PYTHON_PATH": str(self._python_prefix / "bin/python"),
            "CANARY_PATH": self.private_canary_path,
        }
        setup = (
            "set -eu; mount --make-rprivate /; "
            'mount --bind "$SANDBOX_ROOT" "$SANDBOX_ROOT"; '
            'mount --bind "$PYTHON_PREFIX" "$SANDBOX_ROOT$PYTHON_PREFIX"; '
            'mount -o remount,bind,ro "$SANDBOX_ROOT$PYTHON_PREFIX"; '
            'mount --bind /usr/lib "$SANDBOX_ROOT/usr/lib"; '
            'mount -o remount,bind,ro "$SANDBOX_ROOT/usr/lib"; '
            'mount --bind /usr/lib64 "$SANDBOX_ROOT/usr/lib64"; '
            'mount -o remount,bind,ro "$SANDBOX_ROOT/usr/lib64"; '
            'for node in null zero urandom; do mount --bind "/dev/$node" "$SANDBOX_ROOT/dev/$node"; done; '
            'mount -o remount,bind,ro "$SANDBOX_ROOT"; '
            'cd "$SANDBOX_ROOT"; '
            'exec "$BUSYBOX_PATH" chroot "$SANDBOX_ROOT" /bin/busybox sh /launch.sh'
        )
        try:
            process = subprocess.Popen(
                [self._unshare, "--user", "--map-root-user", "--mount", "--net", "--fork", "sh", "-c", setup],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd="/", env=environment, bufsize=1,
            )
        except OSError as exc:
            self.close()
            raise RunLoopError("selector_sandbox_unavailable", "cannot start the isolated selector") from exc
        self._process = process
        try:
            if self.private_canary_path:
                marker = self._readline().strip()
                if marker != "CANARY_DENIED":
                    raise RunLoopError(
                        "selector_sandbox_failed",
                        f"selector sandbox canary check returned an unexpected marker: {marker[:80]!r}",
                    )
                self._canary_denied = True
            init = {
                "kind": "init",
                "schema_version": view.schema_version,
                "candidates": _jsonable(view.candidates),
                "assays": _jsonable(view.assays),
                "observations": _jsonable(view.observations),
                "selector_kind": self.selector_kind,
                "selector_seed": self.seed,
                "selector_algorithm_version": self.algorithm_version,
            }
            response = self._exchange(init)
            if response != {"ready": True}:
                raise RunLoopError("selector_protocol_error", "isolated selector rejected its public catalog")
            self._last_observation_ids.update(item.observation_id for item in view.observations)
        except Exception:
            self.close()
            raise

    def _exchange(self, message: dict[str, object]) -> dict[str, object] | SelectorProposal:
        process = self._process
        if process is None or process.stdin is None:
            raise RunLoopError("selector_process_error", "isolated selector process is not running")
        payload = _canonical_json(message)
        if len(payload) > MAX_SELECTOR_INPUT_BYTES:
            raise RunLoopError("selector_input_too_large", "selector input exceeds the configured limit")
        try:
            process.stdin.write(payload.decode("utf-8") + "\n")
            process.stdin.flush()
            line = self._readline()
            response_obj = json.loads(line)
        except (OSError, json.JSONDecodeError, RunLoopError) as exc:
            self.close()
            if isinstance(exc, RunLoopError):
                raise
            raise RunLoopError("selector_process_error", "isolated selector returned invalid JSON") from exc
        if not isinstance(response_obj, dict):
            self.close()
            raise RunLoopError("selector_schema_error", "isolated selector output must be a JSON object")
        if "ready" in response_obj:
            return response_obj
        try:
            return _validate_proposal(response_obj)
        except RunLoopError:
            self.close()
            raise

    def _readline(self) -> str:
        process = self._process
        if process is None or process.stdout is None:
            raise RunLoopError("selector_process_error", "isolated selector process exited")
        ready, _, _ = select.select([process.stdout], [], [], self.timeout_seconds)
        if not ready:
            self.close()
            raise RunLoopError("selector_timeout", "isolated selector exceeded its time limit")
        line = process.stdout.readline(MAX_SELECTOR_OUTPUT_BYTES + 2)
        if not line:
            stderr = ""
            if process.stderr is not None:
                try:
                    stderr = process.stderr.read(512)
                except OSError:
                    pass
            self.close()
            raise RunLoopError("selector_process_error", "isolated selector exited without a response")
        encoded = line.encode("utf-8")
        if len(encoded) > MAX_SELECTOR_OUTPUT_BYTES or not line.endswith("\n"):
            self.close()
            raise RunLoopError("selector_output_too_large", "isolated selector output exceeded its bound")
        return line


IsolatedFixedOrderSelector = IsolatedSelector


@dataclass(frozen=True, slots=True)
class RunLoopConfig:
    run_id: str
    snapshot_id: str
    runtime_database: str
    initial_budget: Cost
    cost_policy_version: str
    approval_policy: str
    approver_id: str
    selector_kind: str
    max_steps: int
    max_duration_seconds: int
    max_action_retries: int = 2
    max_release_retries: int = 3
    selector_timeout_seconds: float = 5.0
    selector_seed: int | None = None
    selector_algorithm_version: str | None = None
    science_settings_sha256: str | None = None
    science_provider: str | None = None
    science_endpoint: str | None = None
    science_model: str | None = None
    science_api_mode: str | None = None
    science_response_format: str | None = None
    science_output_tokens: int | None = None
    science_timeout_seconds: float | None = None
    science_prompt_version: str | None = None
    science_context_schema_version: str | None = None
    science_shortlist_size: int | None = None
    science_shortlist_seed: int | None = None
    science_max_llm_calls: int | None = None
    science_interpretation_reserve_calls: int | None = None
    science_max_stale_redecisions: int | None = None

    def __post_init__(self) -> None:
        for name, value in (("run_id", self.run_id), ("snapshot_id", self.snapshot_id),
                            ("cost_policy_version", self.cost_policy_version),
                            ("approver_id", self.approver_id)):
            if not isinstance(value, str) or not value.strip() or len(value) > MAX_IDENTIFIER_LENGTH:
                raise RunLoopError("invalid_config", f"{name} must be non-empty and bounded")
        if self.approval_policy != "bounded_replay":
            raise RunLoopError("approval_policy_required", "automated actions require bounded_replay policy")
        _validate_selector_binding(
            self.selector_kind, self.selector_seed, self.selector_algorithm_version,
        )
        science_values = (
            self.science_settings_sha256, self.science_provider, self.science_endpoint,
            self.science_model, self.science_api_mode, self.science_response_format,
            self.science_output_tokens, self.science_timeout_seconds,
            self.science_prompt_version, self.science_context_schema_version,
            self.science_shortlist_size, self.science_shortlist_seed,
            self.science_max_llm_calls, self.science_interpretation_reserve_calls,
            self.science_max_stale_redecisions,
        )
        if self.selector_kind == "scientific_reasoner":
            if self.initial_budget.assumed:
                raise RunLoopError("assumed_budget_forbidden", "scientific_reasoner requires an explicit replay budget")
            if any(value is None for value in science_values):
                raise RunLoopError("invalid_scientific_config", "scientific_reasoner requires a complete persisted configuration")
            if not isinstance(self.science_settings_sha256, str) or len(self.science_settings_sha256) != 64:
                raise RunLoopError("invalid_scientific_config", "scientific settings fingerprint is invalid")
            if self.science_api_mode not in {"responses", "chat_completions"}:
                raise RunLoopError("invalid_scientific_config", "scientific API mode must be resolved")
            for name, value, lower, upper in (
                ("science_output_tokens", self.science_output_tokens, 128, 8192),
                ("science_shortlist_size", self.science_shortlist_size, 1, 24),
                ("science_shortlist_seed", self.science_shortlist_seed, 0, 2**31 - 1),
                ("science_max_llm_calls", self.science_max_llm_calls, 1, 100),
                ("science_interpretation_reserve_calls", self.science_interpretation_reserve_calls, 2, 2),
                ("science_max_stale_redecisions", self.science_max_stale_redecisions, 0, 10),
            ):
                if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                    raise RunLoopError("invalid_scientific_config", f"{name} is outside its allowed bound")
            if (not isinstance(self.science_timeout_seconds, (int, float))
                    or not 1 <= self.science_timeout_seconds <= 180):
                raise RunLoopError("invalid_scientific_config", "science timeout must be between 1 and 180 seconds")
            for name in ("science_provider", "science_endpoint", "science_model",
                         "science_response_format", "science_prompt_version", "science_context_schema_version"):
                value = getattr(self, name)
                if not isinstance(value, str) or not value.strip() or len(value) > 1000:
                    raise RunLoopError("invalid_scientific_config", f"{name} must be non-empty and bounded")
        elif any(value is not None for value in science_values):
            raise RunLoopError("invalid_scientific_config", "scientific configuration applies only to scientific_reasoner")
        if isinstance(self.max_steps, bool) or not isinstance(self.max_steps, int) or self.max_steps < 1:
            raise RunLoopError("invalid_config", "max_steps must be a positive integer")
        if (isinstance(self.max_duration_seconds, bool) or not isinstance(self.max_duration_seconds, int)
                or self.max_duration_seconds < 1):
            raise RunLoopError("invalid_config", "max_duration_seconds must be a positive integer")
        for name, value in (("max_action_retries", self.max_action_retries),
                            ("max_release_retries", self.max_release_retries)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10:
                raise RunLoopError("invalid_config", f"{name} must be an integer between 0 and 10")
        if not isinstance(self.selector_timeout_seconds, (int, float)) or not 0.1 <= self.selector_timeout_seconds <= 60:
            raise RunLoopError("invalid_config", "selector_timeout_seconds must be between 0.1 and 60")
        if not os.path.isabs(self.runtime_database):
            raise RunLoopError("invalid_config", "runtime_database must be an absolute private path")

    def as_json_object(self) -> dict[str, object]:
        result: dict[str, object] = {
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "runtime_database": self.runtime_database,
            "initial_budget": {
                "amount": str(self.initial_budget.amount),
                "unit": self.initial_budget.unit,
                "assumed": self.initial_budget.assumed,
            },
            "cost_policy_version": self.cost_policy_version,
            "approval_policy": self.approval_policy,
            "approver_id": self.approver_id,
            "selector_kind": self.selector_kind,
            "max_steps": self.max_steps,
            "max_duration_seconds": self.max_duration_seconds,
            "max_action_retries": self.max_action_retries,
            "max_release_retries": self.max_release_retries,
            "selector_timeout_seconds": float(self.selector_timeout_seconds),
        }
        # Preserve the Stage 3-A fixed_order document shape and fingerprint.
        if self.selector_kind == "seeded_random_priority":
            result["selector_seed"] = self.selector_seed
            result["selector_algorithm_version"] = self.selector_algorithm_version
        elif self.selector_kind == "scientific_reasoner":
            result["scientific"] = {
                name.removeprefix("science_"): getattr(self, name)
                for name in (
                    "science_settings_sha256", "science_provider", "science_endpoint",
                    "science_model", "science_api_mode", "science_response_format",
                    "science_output_tokens", "science_timeout_seconds", "science_prompt_version",
                    "science_context_schema_version", "science_shortlist_size",
                    "science_shortlist_seed", "science_max_llm_calls",
                    "science_interpretation_reserve_calls",
                    "science_max_stale_redecisions",
                )
            }
        return result

    @property
    def sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self.as_json_object())).hexdigest()

    @classmethod
    def from_json(cls, value: str, *, expected_sha256: str | None = None) -> "RunLoopConfig":
        try:
            if (expected_sha256 is not None
                    and hashlib.sha256(value.encode("utf-8")).hexdigest() != expected_sha256):
                raise RunLoopError("loop_config_corrupt", "stored loop configuration fingerprint does not match")
            obj = json.loads(value)
            has_seed = "selector_seed" in obj
            has_version = "selector_algorithm_version" in obj
            science = obj.pop("scientific", None)
            if science is not None:
                if not isinstance(science, dict):
                    raise ValueError("scientific configuration must be an object")
                obj.update({f"science_{key}": value for key, value in science.items()})
            if has_seed != has_version:
                raise ValueError("incomplete selector binding")
            if not has_seed and obj.get("selector_kind") not in {"fixed_order", "scientific_reasoner"}:
                raise ValueError("seeded selector binding is missing")
            budget = obj.pop("initial_budget")
            config = cls(
                **obj,
                initial_budget=Cost(
                    amount=Decimal(budget["amount"]), unit=budget["unit"],
                    assumed=budget["assumed"],
                ),
            )
            if expected_sha256 is not None and config.sha256 != expected_sha256:
                raise RunLoopError("loop_config_corrupt", "stored loop configuration is not canonical")
            return config
        except RunLoopError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RunLoopError("loop_config_corrupt", "stored loop configuration is invalid") from exc


@dataclass(frozen=True, slots=True)
class LoopSummary:
    run_id: str
    status: str
    stop_reason: str | None
    selection_steps: int
    unique_executions: int
    released_executions: int
    no_record_executions: int
    failed_executions: int
    rejected_steps: int
    observations_added: int
    pending_releases: int
    retries: int
    selector_calls: int
    spent: str
    reserved: str
    available: str
    unit: str
    resumable: bool


class _LoopRepository:
    def __init__(self, coordinator: ExecutionCoordinator):
        self.path = coordinator.database_path
        self.timeout = coordinator.busy_timeout_seconds
        self.lock_dir = self.path.parent / ".run-loop-locks"
        self.lock_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.lock_dir, 0o700)
        except OSError:
            pass

    @contextmanager
    def lock(self, run_id: str):
        lock_name = hashlib.sha256(run_id.encode("utf-8")).hexdigest() + ".lock"
        fd = os.open(self.lock_dir / lock_name, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RunLoopError("run_locked", "another loop worker owns this run") from exc
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @contextmanager
    def transaction(self):
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        with self._connect() as db:
            return db.execute("SELECT * FROM loop_runs WHERE run_id = ?", (run_id,)).fetchone()

    def create(self, config: RunLoopConfig, started_at: datetime, deadline_at: datetime) -> None:
        payload = _canonical_json(config.as_json_object()).decode("utf-8")
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM loop_runs WHERE run_id = ?", (config.run_id,)).fetchone():
                raise RunLoopError("loop_exists", "run already has a persisted Stage 3-A controller")
            db.execute(
                """INSERT INTO loop_runs (
                    run_id, config_json, config_sha256, started_at, deadline_at,
                    status, stop_reason, resumable, selector_calls, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'running', NULL, 1, 0, ?, ?)""",
                (config.run_id, payload, config.sha256, _dt_text(started_at), _dt_text(deadline_at),
                 _dt_text(started_at), _dt_text(started_at)),
            )

    def steps(self, run_id: str) -> list[sqlite3.Row]:
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM loop_steps WHERE run_id = ? ORDER BY step_no", (run_id,),
            ).fetchall()

    def insert_step(self, fields: dict[str, object], now: datetime) -> None:
        with self.transaction() as db:
            budget = db.execute(
                "SELECT initial_budget, spent, reserved FROM runs WHERE run_id = ?",
                (fields["run_id"],),
            ).fetchone()
            if budget is None:
                raise RunLoopError("loop_missing", "execution run is missing while recording a step")
            available = Decimal(budget["initial_budget"]) - Decimal(budget["spent"]) - Decimal(budget["reserved"])
            db.execute(
                """INSERT INTO loop_steps (
                    run_id, step_no, action_id, request_id, candidate_id, assay_id,
                    action_json, view_state_version, view_digest, selected_reason,
                    scientific_decision_id, status, execution_id, budget_spent, budget_reserved, budget_available,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', NULL, ?, ?, ?, ?, ?)""",
                (fields["run_id"], fields["step_no"], fields["action_id"], fields["request_id"],
                 fields["candidate_id"], fields["assay_id"], fields["action_json"],
                 fields["view_state_version"], fields["view_digest"], fields["selected_reason"],
                 fields.get("scientific_decision_id"),
                 budget["spent"], budget["reserved"], str(available), _dt_text(now), _dt_text(now)),
            )

    def update_step(self, run_id: str, step_no: int, now: datetime, **changes: object) -> None:
        allowed = {"status", "execution_id", "action_retries", "release_retries",
                   "observations_added", "error_code", "approval_status", "execution_status",
                   "release_status"}
        if not changes or not set(changes) <= allowed:
            raise RunLoopError("invalid_checkpoint", "loop checkpoint update is invalid")
        assignments = ", ".join(f"{name} = ?" for name in changes)
        values = [changes[name] for name in changes]
        with self.transaction() as db:
            budget = db.execute(
                "SELECT initial_budget, spent, reserved FROM runs WHERE run_id = ?", (run_id,),
            ).fetchone()
            if budget is None:
                raise RunLoopError("loop_missing", "execution run is missing while updating a step")
            available = Decimal(budget["initial_budget"]) - Decimal(budget["spent"]) - Decimal(budget["reserved"])
            assignments += ", budget_spent = ?, budget_reserved = ?, budget_available = ?"
            values.extend((budget["spent"], budget["reserved"], str(available)))
            cursor = db.execute(
                f"UPDATE loop_steps SET {assignments}, updated_at = ? WHERE run_id = ? AND step_no = ?",
                (*values, _dt_text(now), run_id, step_no),
            )
            if cursor.rowcount != 1:
                raise RunLoopError("checkpoint_missing", "loop step checkpoint is missing")

    def bump_selector_calls(self, run_id: str, now: datetime) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE loop_runs SET selector_calls = selector_calls + 1, updated_at = ? WHERE run_id = ?",
                (_dt_text(now), run_id),
            )

    def set_run_state(
        self, run_id: str, now: datetime, *, status: str, stop_reason: str | None,
        resumable: bool,
    ) -> None:
        with self.transaction() as db:
            cursor = db.execute(
                """UPDATE loop_runs SET status = ?, stop_reason = ?, resumable = ?, updated_at = ?
                   WHERE run_id = ?""",
                (status, stop_reason, int(resumable), _dt_text(now), run_id),
            )
            if cursor.rowcount != 1:
                raise RunLoopError("loop_missing", "persisted Stage 3-A controller is missing")


class RunLoopController:
    """Connect trusted 2-B/2-C operations with bounded selection and recovery."""

    def __init__(
        self,
        coordinator: ExecutionCoordinator,
        selector: Selector,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        fault_hook: Callable[[str, int], None] | None = None,
    ):
        self.coordinator = coordinator
        self.selector = selector
        self.monotonic = monotonic
        self.fault_hook = fault_hook
        self.repository = _LoopRepository(coordinator)

    def start(self, config: RunLoopConfig, *, stop_after_new_steps: int | None = None) -> LoopSummary:
        self._validate_live_binding(config)
        self._validate_selector_config(config)
        with self.repository.lock(config.run_id):
            self.coordinator.initialize_run(config.run_id, config.initial_budget)
            started_at = self._now()
            self.repository.create(
                config, started_at,
                started_at + timedelta(seconds=config.max_duration_seconds),
            )
            return self._drive(config, stop_after_new_steps=stop_after_new_steps)

    def resume(self, run_id: str, *, stop_after_new_steps: int | None = None) -> LoopSummary:
        with self.repository.lock(run_id):
            record = self.repository.get_run(run_id)
            if record is None:
                raise RunLoopError("loop_not_found", "run has no persisted Stage 3-A controller")
            config = RunLoopConfig.from_json(
                record["config_json"], expected_sha256=record["config_sha256"],
            )
            self._validate_live_binding(config)
            self._validate_selector_config(config)
            bound = self.coordinator.get_budget(run_id)
            if (bound.snapshot_id != config.snapshot_id
                    or bound.cost_policy_version != config.cost_policy_version
                    or bound.budget.total != config.initial_budget.amount
                    or bound.budget.unit != config.initial_budget.unit):
                raise RunLoopError("resume_binding_mismatch", "runtime DB no longer matches the persisted loop configuration")
            if not bool(record["resumable"]):
                return self._summary(config, record)
            self.repository.set_run_state(
                run_id, self._now(), status="running", stop_reason=None, resumable=True,
            )
            return self._drive(config, stop_after_new_steps=stop_after_new_steps)

    def _validate_live_binding(self, config: RunLoopConfig) -> None:
        if config.snapshot_id != self.coordinator.snapshot_id:
            raise RunLoopError("snapshot_mismatch", "loop configuration names a different snapshot")
        if config.cost_policy_version != self.coordinator.cost_policy_version:
            raise RunLoopError("cost_policy_mismatch", "loop configuration names a different cost policy")
        if config.runtime_database != str(self.coordinator.database_path):
            raise RunLoopError("database_mismatch", "loop configuration names a different runtime database")

    def _validate_selector_config(self, config: RunLoopConfig) -> None:
        selector_kind = getattr(self.selector, "selector_kind", None)
        if selector_kind is None:
            return  # Custom fixture selectors remain available to controller tests.
        if (selector_kind != config.selector_kind
                or getattr(self.selector, "seed", None) != config.selector_seed
                or getattr(self.selector, "algorithm_version", None) != config.selector_algorithm_version):
            raise RunLoopError("selector_binding_mismatch", "selector differs from the persisted loop configuration")
        if config.selector_kind == "scientific_reasoner":
            if (getattr(self.selector, "settings_sha256", None) != config.science_settings_sha256
                    or getattr(self.selector, "shortlist_size", None) != config.science_shortlist_size
                    or getattr(self.selector, "shortlist_seed", None) != config.science_shortlist_seed
                    or getattr(self.selector, "max_llm_calls", None) != config.science_max_llm_calls
                    or getattr(self.selector, "interpretation_reserve_calls", None) != config.science_interpretation_reserve_calls
                    or getattr(self.selector, "max_stale_redecisions", None) != config.science_max_stale_redecisions):
                raise RunLoopError("selector_binding_mismatch", "scientific selector differs from persisted settings")

    def _now(self) -> datetime:
        return self.coordinator._now()

    def _drive(self, config: RunLoopConfig, *, stop_after_new_steps: int | None) -> LoopSummary:
        if config.selector_kind == "scientific_reasoner":
            return self._drive_scientific(config, stop_after_new_steps=stop_after_new_steps)
        run_record = self.repository.get_run(config.run_id)
        assert run_record is not None
        now = self._now()
        deadline_at = _parse_dt(run_record["deadline_at"])
        remaining = min(
            float(config.max_duration_seconds),
            max(0.0, (deadline_at - now).total_seconds()),
        )
        deadline_mono = self.monotonic() + remaining
        new_steps_this_call = 0
        try:
            while True:
                steps = self.repository.steps(config.run_id)
                incomplete = next((row for row in steps if row["status"] in _INCOMPLETE_STEP_STATES), None)
                if incomplete is not None:
                    if (incomplete["status"] != "pending_release"
                            and self._deadline_reached(deadline_at, deadline_mono)):
                        # An already committed coordinator execution is safe to
                        # reconcile after the deadline because execute is
                        # idempotent. A selected but not executed action is
                        # canceled instead of starting a new lookup.
                        _, execution_statuses = self.coordinator.get_loop_snapshot(config.run_id)
                        already_executed = any(
                            item.candidate_id == incomplete["candidate_id"]
                            and item.assay_id == incomplete["assay_id"]
                            for item in execution_statuses
                        )
                        if not already_executed:
                            if not self._expire_unstarted_step(config, incomplete):
                                return self._stop(config, "retry_exhausted", resumable=True)
                            return self._stop(config, "deadline", resumable=False)
                    outcome = self._finish_step(config, incomplete, deadline_at, deadline_mono)
                    if outcome == "deadline":
                        continue
                    if outcome == "retry_exhausted":
                        return self._stop(config, "retry_exhausted", resumable=True)
                    if outcome == "policy_error":
                        return self._stop(config, "policy_error", resumable=False)
                    continue

                if self._deadline_reached(deadline_at, deadline_mono):
                    return self._stop(config, "deadline", resumable=False)
                if len(steps) >= config.max_steps:
                    return self._stop(config, "max_steps", resumable=False)

                public_view, execution_statuses = self.coordinator.get_loop_snapshot(config.run_id)
                eligible, prereq_open = self._enumerate_actions(public_view, execution_statuses)
                budget = public_view.state.budget
                if not eligible:
                    if prereq_open and budget.available <= 0:
                        # There may still be zero-cost actions; _enumerate_actions keeps those.
                        reason = "budget_exhausted"
                    elif prereq_open:
                        reason = "budget_exhausted"
                    elif self._has_unprocessed_supported_action(public_view, execution_statuses):
                        reason = "prerequisites_unmet"
                    else:
                        reason = "no_executable_actions"
                    return self._stop(config, reason, resumable=False)

                view = self._selector_view(public_view, eligible, steps)
                self.repository.bump_selector_calls(config.run_id, self._now())
                try:
                    proposal = _validate_proposal(self.selector.select(view))
                except Exception as exc:
                    return self._stop(config, _selector_error_code(exc), resumable=True)
                if self._deadline_reached(deadline_at, deadline_mono):
                    return self._stop(config, "deadline", resumable=False)
                if proposal.kind == "stop":
                    return self._stop(config, "selector_stop", resumable=False)

                step_no = len(steps) + 1
                action_id = f"loop-action-{uuid4().hex}"
                request_id = f"loop-request-{uuid4().hex}"
                action = ActionRequest(
                    action_id=action_id,
                    campaign_id=self.coordinator.campaign_id,
                    candidate_id=proposal.candidate_id,
                    assay_id=proposal.assay_id,
                )
                self.repository.insert_step({
                    "run_id": config.run_id,
                    "step_no": step_no,
                    "action_id": action_id,
                    "request_id": request_id,
                    "candidate_id": action.candidate_id,
                    "assay_id": action.assay_id,
                    "action_json": action.model_dump_json(),
                    "view_state_version": view.state_version,
                    "view_digest": view.view_digest,
                    "selected_reason": proposal.reason or "",
                }, self._now())
                self._fault("after_proposal", step_no)
                if not any((item.candidate_id, item.assay_id) == (action.candidate_id, action.assay_id)
                           for item in eligible):
                    self.repository.update_step(
                        config.run_id, step_no, self._now(), status="rejected", error_code="proposal_not_available",
                    )
                    return self._stop(config, "policy_error", resumable=False)
                outcome = self._finish_step(
                    config, self.repository.steps(config.run_id)[-1], deadline_at, deadline_mono,
                )
                if outcome == "deadline":
                    continue
                if outcome == "retry_exhausted":
                    return self._stop(config, "retry_exhausted", resumable=True)
                if outcome == "policy_error":
                    return self._stop(config, "policy_error", resumable=False)
                new_steps_this_call += 1
                if stop_after_new_steps is not None and new_steps_this_call >= stop_after_new_steps:
                    return self._stop(config, "user_interrupt", resumable=True)
        except KeyboardInterrupt:
            return self._stop(config, "user_interrupt", resumable=True)

    def _drive_scientific(
        self, config: RunLoopConfig, *, stop_after_new_steps: int | None,
    ) -> LoopSummary:
        """Stage 5-B branch; baseline selector paths above remain unchanged."""
        from assaypilot.scientific_run_loop import ScientificReasoningSelector

        if not isinstance(self.selector, ScientificReasoningSelector):
            return self._stop(config, "scientific_selector_required", resumable=False)
        run_record = self.repository.get_run(config.run_id)
        assert run_record is not None
        deadline_at = _parse_dt(run_record["deadline_at"])
        remaining = min(
            float(config.max_duration_seconds),
            max(0.0, (deadline_at - self._now()).total_seconds()),
        )
        deadline_mono = self.monotonic() + remaining
        new_steps_this_call = 0
        try:
            while True:
                steps = self.repository.steps(config.run_id)
                incomplete = next((row for row in steps if row["status"] in _INCOMPLETE_STEP_STATES), None)
                if incomplete is not None:
                    if (incomplete["status"] != "pending_release"
                            and self._deadline_reached(deadline_at, deadline_mono)):
                        _, statuses = self.coordinator.get_loop_snapshot(config.run_id)
                        already_executed = any(
                            item.candidate_id == incomplete["candidate_id"]
                            and item.assay_id == incomplete["assay_id"] for item in statuses
                        )
                        if not already_executed:
                            if not self._expire_unstarted_step(config, incomplete):
                                return self._stop(config, "retry_exhausted", resumable=True)
                            return self._stop(config, "deadline", resumable=False)
                    outcome = self._finish_step(config, incomplete, deadline_at, deadline_mono)
                    if outcome == "retry_exhausted":
                        return self._stop(config, "retry_exhausted", resumable=True)
                    if outcome == "policy_error":
                        return self._stop(config, "policy_error", resumable=False)
                    continue

                if self._deadline_reached(deadline_at, deadline_mono):
                    return self._stop(config, "deadline", resumable=False)
                public_view, execution_statuses, evidence_documents = (
                    self.coordinator.get_scientific_loop_snapshot(config.run_id)
                )
                eligible, prereq_open = self._enumerate_actions(public_view, execution_statuses)
                if len(steps) >= config.max_steps:
                    terminal_reason = "max_steps"
                    decision_eligible = ()
                elif not eligible:
                    if prereq_open:
                        terminal_reason = "budget_exhausted"
                    elif self._has_unprocessed_supported_action(public_view, execution_statuses):
                        terminal_reason = "prerequisites_unmet"
                    else:
                        terminal_reason = "no_executable_actions"
                    decision_eligible = ()
                else:
                    terminal_reason = None
                    decision_eligible = eligible

                selected = self.selector.select_for_loop(
                    config, public_view, execution_statuses, evidence_documents,
                    decision_eligible, steps, deadline_at=deadline_at,
                    deadline_mono=deadline_mono,
                    enumerate_actions=self._enumerate_actions,
                )
                if selected.status != "idle":
                    self.repository.bump_selector_calls(config.run_id, self._now())
                if selected.status == "stale":
                    continue
                if selected.status in {"failed", "limit"}:
                    return self._stop(
                        config, selected.stop_reason or "scientific_decision_failed",
                        resumable=selected.status == "failed",
                    )
                if selected.status == "finalized":
                    return self._stop(
                        config, terminal_reason or selected.stop_reason or "scientific_finalization_complete",
                        resumable=False,
                    )
                if selected.status == "idle":
                    return self._stop(config, terminal_reason or selected.stop_reason or "no_executable_actions",
                                      resumable=False)
                assert selected.proposal is not None
                if selected.proposal.kind == "stop":
                    return self._stop(config, terminal_reason or "selector_stop", resumable=False)
                if selected.step_no is None:
                    return self._stop(config, "scientific_step_missing", resumable=False)
                step_rows = self.repository.steps(config.run_id)
                row = next((item for item in step_rows if int(item["step_no"]) == selected.step_no), None)
                if row is None or row["scientific_decision_id"] != selected.decision_id:
                    return self._stop(config, "scientific_step_binding_mismatch", resumable=False)
                self._fault("after_proposal", selected.step_no)
                outcome = self._finish_step(config, row, deadline_at, deadline_mono)
                if outcome == "deadline":
                    continue
                if outcome == "retry_exhausted":
                    return self._stop(config, "retry_exhausted", resumable=True)
                if outcome == "policy_error":
                    return self._stop(config, "policy_error", resumable=False)
                new_steps_this_call += 1
                if stop_after_new_steps is not None and new_steps_this_call >= stop_after_new_steps:
                    return self._stop(config, "user_interrupt", resumable=True)
        except KeyboardInterrupt:
            return self._stop(config, "user_interrupt", resumable=True)

    def _finish_step(
        self, config: RunLoopConfig, row: sqlite3.Row,
        deadline_at: datetime, deadline_mono: float,
    ) -> str:
        step_no = int(row["step_no"])
        action = ActionRequest.model_validate_json(row["action_json"])
        if row["status"] in {"proposed", "approved"}:
            if row["status"] == "proposed":
                try:
                    self.coordinator.approve_action(
                        config.run_id, action, approver_id=config.approver_id,
                        reason="bounded_replay policy approved selector proposal",
                    )
                except (ActionRejectedError, BudgetError, RunBindingError, ExecutionControlError) as exc:
                    self.repository.update_step(
                        config.run_id, step_no, self._now(), status="rejected",
                        approval_status="rejected", error_code=_error_code(exc),
                    )
                    return "policy_error"
                self._fault("after_approval", step_no)
                self.repository.update_step(
                    config.run_id, step_no, self._now(), status="approved", approval_status="approved",
                )

            while True:
                current = self.repository.steps(config.run_id)[step_no - 1]
                try:
                    receipt = self.coordinator.execute(config.run_id, current["request_id"], action)
                except (ActionRejectedError, BudgetError, RunBindingError, ExecutionControlError) as exc:
                    self.repository.update_step(
                        config.run_id, step_no, self._now(), status="rejected",
                        execution_status="failed", error_code=_error_code(exc),
                    )
                    return "policy_error"
                except Exception as exc:
                    retries = int(current["action_retries"]) + 1
                    self.repository.update_step(
                        config.run_id, step_no, self._now(), action_retries=retries,
                        error_code=_safe_exception_code(exc),
                    )
                    if self._deadline_reached(deadline_at, deadline_mono):
                        return "deadline"
                    if retries > config.max_action_retries:
                        return "retry_exhausted"
                    continue
                self._fault("after_execute", step_no)
                return self._apply_receipt(config, current, receipt)

        if row["status"] == "pending_release":
            _, statuses = self.coordinator.get_loop_snapshot(config.run_id)
            execution = next((item for item in statuses if item.execution_id == row["execution_id"]), None)
            if execution is None:
                self.repository.update_step(
                    config.run_id, step_no, self._now(), error_code="execution_missing",
                )
                return "retry_exhausted"
            if execution.status == "released":
                self.repository.update_step(
                    config.run_id, step_no, self._now(), execution_status="ready_for_release",
                    release_status="released",
                )
                self._finish_released(config, step_no, int(row["observations_added"]))
                return "complete"
            if execution.status == "cancelled":
                self.repository.update_step(
                    config.run_id, step_no, self._now(), status="cancelled",
                    execution_status="cancelled", release_status="cancelled",
                )
                return "complete"
            if execution.status != "ready_for_release":
                self.repository.update_step(
                    config.run_id, step_no, self._now(), status=execution.status,
                    execution_status=execution.status, release_status="not_applicable",
                    error_code="execution_terminal",
                )
                return "complete"
            return self._release(config, row)
        return "complete"

    def _expire_unstarted_step(self, config: RunLoopConfig, row: sqlite3.Row) -> bool:
        """Cancel an approval whose selected action never committed execution."""
        action = ActionRequest.model_validate_json(row["action_json"])
        approval_status = None
        try:
            approval = self.coordinator.approval_status(config.run_id, action)
            approval_status = approval.status
            if approval.status == "approved":
                approval_status = self.coordinator.cancel_approval(config.run_id, action).status
        except ExecutionControlError as exc:
            if exc.code != "approval_not_found":
                return False
        except Exception:
            return False
        self.repository.update_step(
            config.run_id, int(row["step_no"]), self._now(), status="rejected",
            approval_status=("cancelled" if approval_status == "canceled" else approval_status)
            if approval_status in {"approved", "rejected", "canceled"} else None,
            error_code="deadline_expired_before_execution",
        )
        return True

    def _apply_receipt(
        self, config: RunLoopConfig, row: sqlite3.Row, receipt: ExecutionReceiptView,
    ) -> str:
        step_no = int(row["step_no"])
        if receipt.status == "ready_for_release":
            self.repository.update_step(
                config.run_id, step_no, self._now(), status="pending_release",
                execution_id=receipt.execution_id, execution_status=receipt.status,
                release_status="pending",
            )
            self._fault("before_release", step_no)
            return self._release(config, self.repository.steps(config.run_id)[step_no - 1])
        if receipt.status == "released":
            self.repository.update_step(
                config.run_id, step_no, self._now(), execution_id=receipt.execution_id,
                execution_status=receipt.status, release_status="released",
            )
            self._finish_released(config, step_no, int(row["observations_added"]))
            return "complete"
        if receipt.status in {"no_record", "failed", "cancelled"}:
            self.repository.update_step(
                config.run_id, step_no, self._now(), status=receipt.status,
                execution_id=receipt.execution_id, execution_status=receipt.status,
                release_status="not_applicable", error_code=receipt.error_code,
            )
            return "complete"
        self.repository.update_step(
            config.run_id, step_no, self._now(), status="failed", error_code="unsupported_receipt",
        )
        return "complete"

    def _release(self, config: RunLoopConfig, row: sqlite3.Row) -> str:
        step_no = int(row["step_no"])
        execution_id = row["execution_id"]
        if not execution_id:
            return "retry_exhausted"
        retries = int(row["release_retries"])
        while True:
            before = self.coordinator.get_public_state(config.run_id)
            try:
                published = self.coordinator.release_result(config.run_id, execution_id)
            except Exception as exc:
                retries += 1
                self.repository.update_step(
                    config.run_id, step_no, self._now(), release_retries=retries,
                    error_code=_safe_exception_code(exc),
                )
                if retries > config.max_release_retries:
                    return "retry_exhausted"
                continue
            self._fault("after_release", step_no)
            after = self.coordinator.get_public_state(config.run_id)
            added = max(0, len(after.public.observations) - len(before.public.observations))
            self.repository.update_step(
                config.run_id, step_no, self._now(), status="released",
                execution_id=published.execution_id, execution_status="ready_for_release",
                release_status="released", observations_added=added, error_code=None,
            )
            return "complete"

    def _finish_released(self, config: RunLoopConfig, step_no: int, known_added: int) -> None:
        # A release may have committed immediately before a process crash. Its
        # original PublishedExecution is authoritative; count observations from
        # that result without publishing or settling again.
        rows = self.repository.steps(config.run_id)
        row = rows[step_no - 1]
        published = self.coordinator.get_public_execution(config.run_id, row["execution_id"])
        self.repository.update_step(
            config.run_id, step_no, self._now(), status="released",
            execution_status="ready_for_release", release_status="released",
            observations_added=max(known_added, len(published.result.observations)), error_code=None,
        )

    def _enumerate_actions(self, view, statuses) -> tuple[tuple[ExecutableAction, ...], int]:
        processed = {(item.candidate_id, item.assay_id) for item in statuses}
        candidates = sorted(view.public.candidates, key=lambda item: item.candidate_id)
        assays = sorted(
            (assay for assay in view.public.assays
             if assay.role.value != "primary" and assay.assay_id in self.coordinator._supported_assays),
            key=lambda item: item.assay_id,
        )
        prereq_open = 0
        actions: list[ExecutableAction] = []
        for candidate in candidates:
            for assay in assays:
                key = (candidate.candidate_id, assay.assay_id)
                if key in processed:
                    continue
                if not self.coordinator.prerequisites_satisfied(
                    candidate.candidate_id, assay.assay_id, view.public.observations,
                ):
                    continue
                prereq_open += 1
                if assay.cost.amount <= view.state.budget.available:
                    actions.append(ExecutableAction(
                        candidate_id=candidate.candidate_id,
                        assay_id=assay.assay_id,
                        cost_amount=str(assay.cost.amount), cost_unit=assay.cost.unit,
                    ))
        return tuple(actions), prereq_open

    def _has_unprocessed_supported_action(self, view, statuses) -> bool:
        processed = {(item.candidate_id, item.assay_id) for item in statuses}
        return any(
            (candidate.candidate_id, assay.assay_id) not in processed
            for candidate in view.public.candidates
            for assay in view.public.assays
            if assay.role.value != "primary" and assay.assay_id in self.coordinator._supported_assays
        )

    def _selector_view(self, current, eligible, steps) -> SelectorView:
        candidates = tuple(
            CandidateInfo(candidate_id=item.candidate_id, source=item.source, source_id=item.source_id)
            for item in sorted(current.public.candidates, key=lambda value: value.candidate_id)
        )
        assays = tuple(
            AssayInfo(
                assay_id=item.assay_id, name=item.name, role=item.role.value,
                endpoint=item.endpoint, unit=item.unit,
                cost_amount=str(item.cost.amount), cost_unit=item.cost.unit,
                prerequisites=tuple(PrerequisiteInfo(
                    assay_id=pre.assay_id, kind=pre.kind,
                    verdict=pre.verdict.value if pre.verdict is not None else None,
                ) for pre in item.prerequisites),
            )
            for item in sorted(current.public.assays, key=lambda value: value.assay_id)
        )
        observations = tuple(
            ObservationInfo(
                observation_id=item.observation_id,
                candidate_id=item.candidate_id, assay_id=item.assay_id,
                value=item.value, unit=item.unit,
                comparison=item.comparison.value if item.comparison is not None else None,
                verdict=item.verdict.value,
            )
            for item in sorted(
                current.public.observations,
                key=lambda value: (value.candidate_id, value.assay_id, value.observation_id),
            )
        )
        attempted = tuple(
            AttemptSummary(
                candidate_id=row["candidate_id"], assay_id=row["assay_id"],
                status=row["status"], error_code=row["error_code"],
            )
            for row in steps
        )
        budget = current.state.budget
        fields: dict[str, object] = {
            "schema_version": "assaypilot.selector-view.v1",
            "state_version": current.state_version,
            "public_as_of": current.as_of.isoformat(),
            "candidates": candidates,
            "assays": assays,
            "observations": observations,
            "budget_total": str(budget.total),
            "budget_spent": str(budget.spent),
            "budget_reserved": str(budget.reserved),
            "budget_available": str(budget.available),
            "budget_unit": budget.unit,
            "executable_actions": tuple(sorted(
                eligible, key=lambda item: (item.candidate_id, item.assay_id),
            )),
            "attempted_actions": attempted,
        }
        digest = hashlib.sha256(_canonical_json(_jsonable(fields))).hexdigest()
        return SelectorView(**fields, view_digest=digest)

    def _deadline_reached(self, deadline_at: datetime, deadline_mono: float) -> bool:
        return self.monotonic() >= deadline_mono or self._now() >= deadline_at

    def _fault(self, stage: str, step_no: int) -> None:
        if self.fault_hook is not None:
            self.fault_hook(stage, step_no)

    def _stop(self, config: RunLoopConfig, reason: str, *, resumable: bool) -> LoopSummary:
        finish = getattr(self.selector, "finish_run", None)
        if config.selector_kind == "scientific_reasoner" and callable(finish):
            finish(config.run_id, reason)
        self.repository.set_run_state(
            config.run_id, self._now(), status="stopped", stop_reason=reason,
            resumable=resumable,
        )
        record = self.repository.get_run(config.run_id)
        assert record is not None
        return self._summary(config, record)

    def _summary(self, config: RunLoopConfig, record: sqlite3.Row) -> LoopSummary:
        steps = self.repository.steps(config.run_id)
        public = self.coordinator.get_public_state(config.run_id)
        budget = public.state.budget
        statuses = [row["status"] for row in steps]
        return LoopSummary(
            run_id=config.run_id,
            status=record["status"], stop_reason=record["stop_reason"],
            selection_steps=len(steps),
            unique_executions=len({row["execution_id"] for row in steps if row["execution_id"]}),
            released_executions=statuses.count("released"),
            no_record_executions=statuses.count("no_record"),
            failed_executions=statuses.count("failed"),
            rejected_steps=statuses.count("rejected"),
            observations_added=sum(int(row["observations_added"]) for row in steps),
            pending_releases=sum(row["status"] == "pending_release" for row in steps),
            retries=sum(int(row["action_retries"]) + int(row["release_retries"]) for row in steps),
            selector_calls=int(record["selector_calls"]),
            spent=str(budget.spent), reserved=str(budget.reserved),
            available=str(budget.available), unit=budget.unit,
            resumable=bool(record["resumable"]),
        )


def _validate_proposal(value: SelectorProposal | dict[str, object]) -> SelectorProposal:
    if isinstance(value, SelectorProposal):
        obj: dict[str, object] = {
            key: item for key, item in {
                "kind": value.kind, "candidate_id": value.candidate_id,
                "assay_id": value.assay_id, "reason": value.reason,
                "stop_reason": value.stop_reason,
            }.items() if item is not None
        }
    elif isinstance(value, dict):
        obj = value
    else:
        raise RunLoopError("selector_schema_error", "selector must return a JSON object")
    try:
        encoded = _canonical_json(obj)
    except (TypeError, ValueError) as exc:
        raise RunLoopError("selector_schema_error", "selector proposal is not JSON serializable") from exc
    if len(encoded) > MAX_SELECTOR_OUTPUT_BYTES:
        raise RunLoopError("selector_output_too_large", "selector proposal exceeds the output limit")
    kind = obj.get("kind")
    if kind == "select" and set(obj) == {"kind", "candidate_id", "assay_id", "reason"}:
        candidate_id, assay_id, reason = obj["candidate_id"], obj["assay_id"], obj["reason"]
        if (not isinstance(candidate_id, str) or not candidate_id.strip() or len(candidate_id) > MAX_IDENTIFIER_LENGTH
                or not isinstance(assay_id, str) or not assay_id.strip() or len(assay_id) > MAX_IDENTIFIER_LENGTH
                or not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_SELECTOR_REASON_LENGTH):
            raise RunLoopError("selector_schema_error", "selector action proposal has invalid fields")
        if any(char in reason for char in "\r\n\x00"):
            raise RunLoopError("selector_schema_error", "selector reason must be one bounded line")
        return SelectorProposal("select", candidate_id, assay_id, reason)
    if kind == "stop" and set(obj) == {"kind", "stop_reason"}:
        stop_reason = obj["stop_reason"]
        if stop_reason not in {"selector_stop", "no_executable_actions"}:
            raise RunLoopError("selector_schema_error", "selector stop reason is unsupported")
        return SelectorProposal("stop", stop_reason=str(stop_reason))
    raise RunLoopError("selector_schema_error", "selector proposal does not match the fixed JSON contract")


def _selector_error_code(exc: Exception) -> str:
    if isinstance(exc, RunLoopError):
        return exc.code
    return "selector_process_error"


def _error_code(exc: Exception) -> str:
    return getattr(exc, "code", "policy_rejected")


def _safe_exception_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code and len(code) <= 80:
        return code
    return "transient_execution_error"


def _view_object(view: SelectorView, *, include_digest: bool) -> dict[str, object]:
    fields = {
        name: getattr(view, name)
        for name in (
            "schema_version", "state_version", "public_as_of", "candidates", "assays",
            "observations", "budget_total", "budget_spent", "budget_reserved",
            "budget_available", "budget_unit", "executable_actions", "attempted_actions",
        )
    }
    if include_digest:
        fields["view_digest"] = view.view_digest
    return _jsonable(fields)


def _jsonable(value):
    if hasattr(value, "__dataclass_fields__"):
        return {name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _dt_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RunLoopError("invalid_time", "loop timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RunLoopError("loop_config_corrupt", "stored loop timestamp is not timezone-aware")
    return parsed.astimezone(timezone.utc)
