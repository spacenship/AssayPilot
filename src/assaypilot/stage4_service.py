"""Single-host Stage 4 service and public-result artifact exporter.

The service accepts a small allowlisted run request. A separate worker process
invokes the existing Stage 3 run-loop CLI; this module never selects, approves,
executes, or releases an assay itself.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any, Callable
from uuid import UUID, uuid4

from assaypilot.execution import ExecutionCoordinator


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME_ROOT = ROOT / "runtime" / "stage4"
CAMPAIGNS: dict[str, dict[str, str]] = {
    "revision-20260917-r2": {
        "snapshot": "data_snapshots/pubchem-tor-mep2-20260915/revision-20260917-r2",
        "label": "r2 · 5개 후보",
    },
    "revision-20260918-primary-active-all": {
        "snapshot": "data_snapshots/pubchem-tor-mep2-20260915/revision-20260918-primary-active-all",
        "label": "확장 · 1,682개 후보",
    },
}
COST_POLICY_VERSION = "preserved-public-assay-cost-v1"
APPROVAL_POLICY = "bounded_replay"
APPROVER_ID = "local-stage4-bounded-replay-policy"
BUDGET_UNIT = "synthetic_credit"
MAX_BUDGET = Decimal("50")
MAX_STEPS = 50
MAX_DURATION_SECONDS = 300
MAX_ACTION_RETRIES = 1
MAX_RELEASE_RETRIES = 2
SELECTOR_TIMEOUT_SECONDS = 5.0
RANDOM_PRIORITY_VERSION = "random-priority-v1"
_RUN_ID_RE = re.compile(r"^stage4-[0-9a-f]{32}$")
_BUDGET_RE = re.compile(r"^[0-9]{1,3}(?:\.[0-9]{1,6})?$")
_NORMAL_STOP_REASONS = frozenset({
    "max_steps", "max_duration", "deadline", "budget_exhausted",
    "no_executable_actions", "prerequisites_unmet", "selector_stop",
})


class Stage4ServiceError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 400):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.partial"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON artifact must be an object")
    return value


@contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _safe_config(config: dict[str, Any]) -> dict[str, Any]:
    """Select client-visible settings; never include server paths or DB names."""
    return {
        "campaign": config["campaign"],
        "selector": config["selector"],
        "seed": config.get("seed"),
        "selector_algorithm_version": config.get("selector_algorithm_version"),
        "budget": config["budget"],
        "budget_unit": BUDGET_UNIT,
        "budget_assumed": True,
        "max_steps": config["max_steps"],
        "max_duration_seconds": MAX_DURATION_SECONDS,
    }


def validate_run_request(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Stage4ServiceError("invalid_request", "요청 본문은 JSON 객체여야 합니다.")
    allowed = {"campaign", "selector", "seed", "budget", "max_steps"}
    if set(value) - allowed or not {"campaign", "selector", "budget", "max_steps"} <= set(value):
        raise Stage4ServiceError("invalid_request", "허용된 실행 조건만 보내야 합니다.")
    campaign = value.get("campaign")
    if not isinstance(campaign, str) or campaign not in CAMPAIGNS:
        raise Stage4ServiceError("invalid_campaign", "허용되지 않은 campaign입니다.")
    selector = value.get("selector")
    if not isinstance(selector, str) or selector not in {"fixed_order", "seeded_random_priority"}:
        raise Stage4ServiceError("invalid_selector", "지원하는 선택 방식이 아닙니다.")
    raw_budget = value.get("budget")
    if not isinstance(raw_budget, str) or not _BUDGET_RE.fullmatch(raw_budget):
        raise Stage4ServiceError("invalid_budget", "예산은 소수점 여섯 자리 이하의 문자열이어야 합니다.")
    try:
        budget = Decimal(raw_budget)
    except InvalidOperation as exc:
        raise Stage4ServiceError("invalid_budget", "예산 형식이 올바르지 않습니다.") from exc
    if not budget.is_finite() or budget <= 0 or budget > MAX_BUDGET:
        raise Stage4ServiceError("invalid_budget", "예산은 0보다 크고 50 synthetic_credit 이하여야 합니다.")
    raw_steps = value.get("max_steps")
    if isinstance(raw_steps, bool) or not isinstance(raw_steps, int) or not 1 <= raw_steps <= MAX_STEPS:
        raise Stage4ServiceError("invalid_max_steps", "step 제한은 1에서 50 사이의 정수여야 합니다.")
    result: dict[str, Any] = {
        "campaign": campaign,
        "selector": selector,
        "seed": None,
        "selector_algorithm_version": None,
        "budget": format(budget.normalize(), "f"),
        "max_steps": raw_steps,
    }
    seed = value.get("seed")
    if selector == "fixed_order":
        if seed is not None:
            raise Stage4ServiceError("invalid_seed", "fixed_order는 seed를 받지 않습니다.")
    else:
        if isinstance(seed, bool) or not isinstance(seed, int) or not -(2**31) <= seed < 2**31:
            raise Stage4ServiceError("invalid_seed", "seeded_random_priority에는 32비트 정수 seed가 필요합니다.")
        result["seed"] = seed
        result["selector_algorithm_version"] = RANDOM_PRIORITY_VERSION
    return result


def classify_cli_outcome(returncode: int, stdout: str, stderr: str) -> dict[str, Any]:
    """Map the existing CLI summary to service state without confusing stop reason."""
    summary: dict[str, Any] | None = None
    try:
        parsed = json.loads(stdout)
        if isinstance(parsed, dict) and isinstance(parsed.get("run_id"), str):
            summary = parsed
    except (json.JSONDecodeError, TypeError):
        pass
    if returncode != 0:
        code = "worker_failed"
        try:
            parsed_error = json.loads(stderr.strip().splitlines()[-1])
            candidate = parsed_error.get("error") if isinstance(parsed_error, dict) else None
            if isinstance(candidate, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,80}", candidate):
                code = candidate
        except (IndexError, json.JSONDecodeError, TypeError):
            pass
        return {"service_status": "failed", "stop_reason": None, "summary": summary,
                "error": {"code": code, "message": "기존 replay 실행기가 오류로 종료했습니다."}}
    if summary is None:
        return {"service_status": "failed", "stop_reason": None, "summary": None,
                "error": {"code": "invalid_worker_summary", "message": "실행기가 유효한 요약을 반환하지 않았습니다."}}
    reason = summary.get("stop_reason")
    if reason in _NORMAL_STOP_REASONS:
        status, error = "completed", None
    elif reason in {"user_interrupt", "retry_exhausted"}:
        status, error = "interrupted", None
    elif isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,80}", reason):
        status = "failed"
        messages = {
            "selector_sandbox_unavailable": "격리 selector 실행 환경을 사용할 수 없습니다.",
            "selector_sandbox_failed": "격리 selector 검증에 실패했습니다.",
            "policy_error": "trusted 실행 정책 검증이 실행을 중단했습니다.",
        }
        error = {"code": reason, "message": messages.get(reason, "replay가 정상 완료되지 않았습니다.")}
    else:
        status = "failed"
        error = {"code": "missing_stop_reason", "message": "실행 종료 사유를 확인할 수 없습니다."}
    return {"service_status": status, "stop_reason": reason, "summary": summary, "error": error}


def _archive_execution(reader, published, step: dict[str, Any]) -> dict[str, Any]:
    evidence_rows: list[dict[str, Any]] = []
    for observation in published.result.observations:
        for evidence_id in observation.evidence_ids:
            evidence = reader.evidence(evidence_id)
            if _sha256(evidence.payload) != evidence.sha256:
                raise Stage4ServiceError("public_result_storage_failed", "공개 evidence hash 검증에 실패했습니다.", 500)
            try:
                payload = json.loads(evidence.payload)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise Stage4ServiceError("public_result_storage_failed", "공개 evidence JSON이 손상되었습니다.", 500) from exc
            if _canonical_json(payload) != evidence.payload:
                raise Stage4ServiceError("public_result_storage_failed", "공개 evidence canonical 검증에 실패했습니다.", 500)
            evidence_rows.append({
                # The EvidenceRef identity is public, but its runtime-relative
                # artifact locator is a server implementation detail.
                "reference": {
                    "evidence_id": evidence.reference.evidence_id,
                    "source_kind": evidence.reference.source_kind,
                    "source_id": evidence.reference.source_id,
                },
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


def _strip_runtime_locations(archive: dict[str, Any]) -> None:
    executions = archive.get("executions")
    if not isinstance(executions, list):
        return
    for execution in executions:
        if not isinstance(execution, dict) or not isinstance(execution.get("evidence"), list):
            continue
        for item in execution["evidence"]:
            reference = item.get("reference") if isinstance(item, dict) else None
            if isinstance(reference, dict):
                reference.pop("location", None)


def _public_metrics(steps: list[dict[str, Any]], executions: list[dict[str, Any]], budget: dict[str, Any]) -> dict[str, Any]:
    units: dict[tuple[str, str], set[str]] = {}
    unknown = 0
    for execution in executions:
        for observation in execution.get("observations", []):
            verdict = observation.get("verdict")
            if isinstance(verdict, dict):
                verdict = verdict.get("value")
            normalized = str(verdict).casefold()
            if normalized in {"active", "inactive"}:
                units.setdefault((str(observation.get("candidate_id")), str(observation.get("assay_id"))), set()).add(normalized)
            else:
                unknown += 1
    active = sum("active" in labels for labels in units.values())
    binary = sum(bool(labels & {"active", "inactive"}) for labels in units.values())
    statuses: dict[str, int] = {}
    for step in steps:
        status = str(step.get("status", "unknown"))
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "selection_steps": len(steps),
        "released_executions": statuses.get("released", 0),
        "no_record_executions": statuses.get("no_record", 0),
        "failed_executions": statuses.get("failed", 0),
        "rejected_steps": statuses.get("rejected", 0),
        "cancelled_steps": statuses.get("cancelled", 0),
        "new_followup_active_H": active,
        "new_followup_binary_L": binary,
        "observed_active_fraction_H_over_L": active / binary if binary else None,
        "unknown_observations": unknown,
        "budget": budget,
    }


class Stage4RunService:
    """Persistent single-host service facade used by the HTTP API."""

    def __init__(
        self,
        runtime_root: str | Path = DEFAULT_RUNTIME_ROOT,
        *,
        readiness_probe: Callable[[], dict[str, Any]] | None = None,
        worker_launcher: Callable[[str, bool], int] | None = None,
    ):
        self.runtime_root = Path(runtime_root).expanduser().resolve()
        for campaign in CAMPAIGNS.values():
            snapshot = (ROOT / campaign["snapshot"]).resolve()
            if self.runtime_root == snapshot or snapshot in self.runtime_root.parents:
                raise ValueError("Stage 4 runtime root must be outside every preserved snapshot")
        self.runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.runtime_root, 0o700)
        self.runs_root = self.runtime_root / "runs"
        self.runs_root.mkdir(mode=0o700, exist_ok=True)
        self.index_path = self.runtime_root / "service.sqlite"
        self._readiness_probe = readiness_probe
        self._readiness_cache: tuple[float, dict[str, Any]] | None = None
        self._worker_launcher = worker_launcher or self._launch_worker
        self._initialize_index()

    def _initialize_index(self) -> None:
        with sqlite3.connect(self.index_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS idempotency (key_hash TEXT PRIMARY KEY, request_sha256 TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE)")
        os.chmod(self.index_path, 0o600)

    def _launch_worker(self, run_id: str, resume: bool) -> int:
        run_dir = self._run_dir(run_id)
        log_fd = os.open(run_dir / "worker.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        command = [
            sys.executable, "-m", "assaypilot.stage4_worker",
            "--runtime-root", str(self.runtime_root), "--run-id", run_id,
        ]
        if resume:
            command.append("--resume")
        try:
            process = subprocess.Popen(
                command, cwd=ROOT, stdin=subprocess.DEVNULL,
                stdout=log_fd, stderr=log_fd, close_fds=True, start_new_session=True,
            )
            return process.pid
        finally:
            os.close(log_fd)

    def _run_dir(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
            raise Stage4ServiceError("run_not_found", "run ID가 올바르지 않습니다.", 404)
        path = self.runs_root / run_id
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != self.runs_root.resolve():
            raise Stage4ServiceError("run_not_found", "run을 찾을 수 없습니다.", 404)
        return path

    @contextmanager
    def _state_lock(self, run_dir: Path):
        with _file_lock(run_dir / ".state.lock"):
            yield

    def _read_state(self, run_dir: Path) -> dict[str, Any]:
        return _load_json(run_dir / "run.json")

    def _write_state(self, run_dir: Path, value: dict[str, Any]) -> None:
        with self._state_lock(run_dir):
            _atomic_json(run_dir / "run.json", value)

    def _update_state(self, run_dir: Path, **changes: Any) -> dict[str, Any]:
        with self._state_lock(run_dir):
            current = self._read_state(run_dir)
            current.update(changes)
            _atomic_json(run_dir / "run.json", current)
            return current

    def _catalog(self) -> list[dict[str, Any]]:
        from assaypilot.data.adapter import PublicBundleAdapter
        from assaypilot.domain import DataSource

        rows = []
        for revision, item in CAMPAIGNS.items():
            snapshot = ROOT / item["snapshot"]
            try:
                public = PublicBundleAdapter().load(DataSource(
                    kind="public_bundle", location=str(snapshot / "bundle/public/manifest.json"),
                ))
                assay = next((assay for assay in public.assays if assay.assay_id == "mep2-confirmatory"), None)
                rows.append({
                    "campaign": revision, "label": item["label"], "available": assay is not None,
                    "candidate_count": len(public.candidates), "assay_id": assay.assay_id if assay else None,
                    "assay_name": assay.name if assay else None,
                    "budget_unit": BUDGET_UNIT, "budget_assumed": True,
                })
            except Exception:
                rows.append({
                    "campaign": revision, "label": item["label"], "available": False,
                    "candidate_count": None, "assay_id": None, "assay_name": None,
                    "budget_unit": BUDGET_UNIT, "budget_assumed": True,
                })
        return rows

    def _probe_sandbox(self) -> dict[str, Any]:
        if self._readiness_probe is not None:
            return self._readiness_probe()
        from assaypilot.run_loop import IsolatedSelector, SelectorView

        view = SelectorView(
            schema_version="assaypilot.selector-view.v1", state_version=0,
            public_as_of="2026-01-01T00:00:00+00:00", candidates=(), assays=(),
            observations=(), budget_total="0", budget_spent="0", budget_reserved="0",
            budget_available="0", budget_unit=BUDGET_UNIT, executable_actions=(),
            attempted_actions=(), view_digest="readiness-probe",
        )
        try:
            with IsolatedSelector() as selector:
                proposal = selector.select(view)
            if getattr(proposal, "stop_reason", None) != "no_executable_actions":
                return {"available": False, "code": "selector_sandbox_failed"}
            return {"available": True, "code": None}
        except Exception as exc:
            code = getattr(exc, "code", "selector_sandbox_unavailable")
            if code not in {"selector_sandbox_unavailable", "selector_sandbox_failed"}:
                code = "selector_sandbox_unavailable"
            return {"available": False, "code": code}

    def readiness(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if not force and self._readiness_cache and now - self._readiness_cache[0] < 10:
            return self._readiness_cache[1]
        catalog = self._catalog()
        sandbox = self._probe_sandbox()
        ready = bool(sandbox["available"] and any(row["available"] for row in catalog))
        response = {
            "ready": ready,
            "campaigns": catalog,
            "selector_sandbox": {
                "available": sandbox["available"], "code": sandbox["code"],
            },
            "budget_limit": str(MAX_BUDGET), "max_steps_limit": MAX_STEPS,
        }
        self._readiness_cache = (now, response)
        return response

    def _check_request_ready(self, config: dict[str, Any]) -> None:
        status = self.readiness()
        campaign = next((row for row in status["campaigns"] if row["campaign"] == config["campaign"]), None)
        if campaign is None or not campaign["available"]:
            raise Stage4ServiceError("campaign_unavailable", "선택한 snapshot의 공개 campaign을 읽을 수 없습니다.", 503)
        if not status["selector_sandbox"]["available"]:
            code = status["selector_sandbox"]["code"] or "selector_sandbox_unavailable"
            raise Stage4ServiceError(code, "격리 selector 실행 환경을 사용할 수 없습니다.", 503)

    def _idempotency_lookup(self, key_hash: str, request_hash: str) -> str | None:
        with sqlite3.connect(self.index_path) as db:
            row = db.execute("SELECT request_sha256, run_id FROM idempotency WHERE key_hash = ?", (key_hash,)).fetchone()
        if row is None:
            return None
        if row[0] != request_hash:
            raise Stage4ServiceError("idempotency_conflict", "같은 Idempotency-Key에 다른 실행 조건을 사용할 수 없습니다.", 409)
        return str(row[1])

    def _insert_idempotency(self, key_hash: str, request_hash: str, run_id: str) -> None:
        with sqlite3.connect(self.index_path) as db:
            db.execute("INSERT INTO idempotency(key_hash, request_sha256, run_id) VALUES (?, ?, ?)", (key_hash, request_hash, run_id))
        os.chmod(self.index_path, 0o600)

    def _pid_alive(self, pid: object, run_id: str) -> bool:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        command_path = Path(f"/proc/{pid}/cmdline")
        if command_path.exists():
            try:
                command = command_path.read_bytes()
                return b"assaypilot.stage4_worker" in command and run_id.encode("ascii") in command
            except OSError:
                return False
        return True

    def _reconcile_liveness(self, run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("service_status") not in {"queued", "running"}:
            return state
        alive = self._pid_alive(state.get("worker_pid"), state["run_id"])
        grace_expired = time.time() - float(state.get("created_epoch", time.time())) > 20
        if alive or (state.get("service_status") == "queued" and not grace_expired):
            return state
        with self._state_lock(run_dir):
            current = self._read_state(run_dir)
            if current.get("service_status") in {"queued", "running"}:
                current.update({
                    "service_status": "interrupted",
                    "error": {"code": "worker_process_disappeared", "message": "worker가 종료되어 실행이 중단됐습니다."},
                    "ended_at": _now(),
                })
                _atomic_json(run_dir / "run.json", current)
            return current

    def _active_run(self) -> str | None:
        for run_dir in sorted(self.runs_root.glob("stage4-*"), reverse=True):
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            try:
                state = self._read_state(run_dir)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            state = self._reconcile_liveness(run_dir, state)
            if state.get("service_status") in {"queued", "running"}:
                return state["run_id"]
        return None

    def start_run(self, request: object, idempotency_key: str) -> tuple[dict[str, Any], bool]:
        config = validate_run_request(request)
        try:
            parsed_key = UUID(idempotency_key) if isinstance(idempotency_key, str) else None
        except (ValueError, AttributeError):
            parsed_key = None
        if parsed_key is None or str(parsed_key) != idempotency_key.lower():
            raise Stage4ServiceError("missing_idempotency_key", "유효한 UUID Idempotency-Key가 필요합니다.")
        token_hash = _sha256(idempotency_key.lower().encode("ascii"))
        request_hash = _sha256(_canonical_json(config))
        with _file_lock(self.runtime_root / ".service-start.lock"):
            prior = self._idempotency_lookup(token_hash, request_hash)
            if prior:
                return self.get_run(prior), True
            active = self._active_run()
            if active:
                raise Stage4ServiceError("concurrent_run_limit", "이미 실행 중인 run이 있습니다.", 409)
            self._check_request_ready(config)
            run_id = f"stage4-{uuid4().hex}"
            run_dir = self.runs_root / run_id
            run_dir.mkdir(mode=0o700)
            os.chmod(run_dir, 0o700)
            public_config = _safe_config(config)
            state = {
                "schema_version": "assaypilot.stage4.run-state.v1",
                "run_id": run_id, "service_status": "queued", "domain_status": None,
                "stop_reason": None, "configuration": public_config,
                "configuration_sha256": _sha256(_canonical_json(public_config)),
                "created_at": _now(), "created_epoch": time.time(), "started_at": None,
                "ended_at": None, "worker_pid": None, "resume_count": 0,
                "artifact_revision": None, "error": None,
            }
            _atomic_json(run_dir / "run.json", state)
            self._insert_idempotency(token_hash, request_hash, run_id)
            try:
                pid = self._worker_launcher(run_id, False)
                self._update_state(run_dir, worker_pid=pid)
            except OSError as exc:
                self._update_state(
                    run_dir, service_status="failed", ended_at=_now(),
                    error={"code": "worker_start_failed", "message": "별도 실행 worker를 시작하지 못했습니다."},
                )
                raise Stage4ServiceError("worker_start_failed", "별도 실행 worker를 시작하지 못했습니다.", 503) from exc
        return self.get_run(run_id), False

    def _resume_available(self, run_id: str, run_dir: Path) -> bool:
        database = run_dir / "private" / "execution.sqlite"
        if not database.is_file():
            return False
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1) as db:
                row = db.execute("SELECT resumable FROM loop_runs WHERE run_id = ?", (run_id,)).fetchone()
            return bool(row and row[0])
        except sqlite3.Error:
            return False

    def resume_run(self, run_id: str) -> dict[str, Any]:
        with _file_lock(self.runtime_root / ".service-start.lock"):
            run_dir = self._run_dir(run_id)
            state = self._reconcile_liveness(run_dir, self._read_state(run_dir))
            if state.get("service_status") in {"queued", "running"}:
                return self.get_run(run_id)
            if state.get("service_status") != "interrupted" or not self._resume_available(run_id, run_dir):
                raise Stage4ServiceError("run_not_resumable", "이 run은 현재 재개할 수 없습니다.", 409)
            config = state["configuration"]
            self._check_request_ready({"campaign": config["campaign"]})
            self._update_state(
                run_dir, service_status="queued", error=None, ended_at=None,
                resume_count=int(state.get("resume_count", 0)) + 1,
            )
            try:
                pid = self._worker_launcher(run_id, True)
                self._update_state(run_dir, worker_pid=pid)
            except OSError as exc:
                self._update_state(
                    run_dir, service_status="interrupted", ended_at=_now(),
                    error={"code": "worker_start_failed", "message": "resume worker를 시작하지 못했습니다."},
                )
                raise Stage4ServiceError("worker_start_failed", "resume worker를 시작하지 못했습니다.", 503) from exc
        return self.get_run(run_id)

    def _artifact_docs(self, state: dict[str, Any]) -> dict[str, Any] | None:
        revision = state.get("artifact_revision")
        if not isinstance(revision, int) or revision < 1:
            return None
        run_dir = self._run_dir(state["run_id"])
        artifact_dir = run_dir / "artifacts" / f"rev-{revision:06d}"
        try:
            docs = {
                "summary": _load_json(artifact_dir / "summary.json"),
                "trace": _load_json(artifact_dir / "trace.json"),
                "published_results": _load_json(artifact_dir / "published_results.json"),
                "public_export": _load_json(artifact_dir / "public_export.json"),
            }
            # Older artifact generations may contain the runtime-relative
            # ``EvidenceRef.location`` field. Keep API and download DTOs safe
            # after a service upgrade without re-running the Oracle.
            _strip_runtime_locations(docs["published_results"])
            _strip_runtime_locations(docs["public_export"].get("published_results", {}))
            return docs
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def get_run(self, run_id: str) -> dict[str, Any]:
        run_dir = self._run_dir(run_id)
        state = self._reconcile_liveness(run_dir, self._read_state(run_dir))
        artifacts = self._artifact_docs(state)
        summary = artifacts["summary"] if artifacts else None
        trace = artifacts["trace"] if artifacts else None
        archive = artifacts["published_results"] if artifacts else None
        default_budget = state["configuration"]["budget"]
        return {
            "schema_version": "assaypilot.stage4.run-response.v1",
            "run_id": run_id,
            "service_status": state["service_status"],
            "domain_status": state.get("domain_status"),
            "stop_reason": state.get("stop_reason"),
            "configuration": state["configuration"],
            "configuration_sha256": state["configuration_sha256"],
            "created_at": state["created_at"],
            "started_at": state.get("started_at"), "ended_at": state.get("ended_at"),
            "resume_available": state["service_status"] == "interrupted" and self._resume_available(run_id, run_dir),
            "resume_count": int(state.get("resume_count", 0)),
            "error": state.get("error"),
            "summary": summary,
            "steps": trace.get("steps", []) if trace else [],
            "published_results": archive.get("executions", []) if archive else [],
            "download_url": f"/api/runs/{run_id}/download" if artifacts else None,
            "progress": (summary.get("public_metrics") if summary else {
                "selection_steps": 0, "released_executions": 0, "no_record_executions": 0,
                "failed_executions": 0, "rejected_steps": 0, "cancelled_steps": 0,
                "new_followup_active_H": 0, "new_followup_binary_L": 0,
                "observed_active_fraction_H_over_L": None,
                "budget": {"total": default_budget, "spent": "0", "reserved": "0",
                           "available": default_budget, "unit": BUDGET_UNIT, "assumed": True},
            }),
        }

    def download(self, run_id: str) -> bytes:
        run_dir = self._run_dir(run_id)
        state = self._reconcile_liveness(run_dir, self._read_state(run_dir))
        artifacts = self._artifact_docs(state)
        if not artifacts:
            raise Stage4ServiceError("public_result_not_ready", "공개 결과 파일이 아직 준비되지 않았습니다.", 409)
        return _canonical_json(artifacts["public_export"]) + b"\n"

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for run_dir in sorted(self.runs_root.glob("stage4-*"), reverse=True):
            if not run_dir.is_dir() or run_dir.is_symlink():
                continue
            try:
                state = self._reconcile_liveness(run_dir, self._read_state(run_dir))
                rows.append({
                    "run_id": state["run_id"], "service_status": state["service_status"],
                    "campaign": state["configuration"]["campaign"],
                    "selector": state["configuration"]["selector"],
                    "created_at": state["created_at"], "stop_reason": state.get("stop_reason"),
                })
                if len(rows) >= max(1, min(limit, 50)):
                    break
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                continue
        return rows


class _PublicArtifactExporter:
    """Read run progress and results through the existing trusted public reader."""

    def __init__(self, service: Stage4RunService, run_id: str):
        self.service = service
        self.run_id = run_id
        self.run_dir = service._run_dir(run_id)
        self.state = service._read_state(self.run_dir)
        self.revision = 0
        self.signature: str | None = None
        self.coordinator: ExecutionCoordinator | None = None
        self.reader = None

    def _open_reader_when_ready(self):
        if self.reader is not None:
            return
        database = self.run_dir / "private" / "execution.sqlite"
        if not database.is_file():
            return
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=0.3) as db:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"runs", "loop_runs", "loop_steps"} <= tables:
                    return
                if db.execute("SELECT 1 FROM loop_runs WHERE run_id=?", (self.run_id,)).fetchone() is None:
                    return
        except sqlite3.Error:
            return
        from assaypilot.run_loop_cli import _clock, _load_public_and_oracle

        campaign = self.state["configuration"]["campaign"]
        snapshot = (ROOT / CAMPAIGNS[campaign]["snapshot"]).resolve()
        try:
            _root, public, oracle = _load_public_and_oracle(snapshot)
            self.coordinator = ExecutionCoordinator(
                database, public, oracle, cost_policy_version=COST_POLICY_VERSION, clock=_clock(public),
            )
            self.reader = self.coordinator.public_reader(self.run_id)
            os.chmod(database, 0o600)
        except Exception:
            self.coordinator = None
            self.reader = None
            raise

    def refresh(self, *, force: bool = False, cli_summary: dict[str, Any] | None = None) -> bool:
        self._open_reader_when_ready()
        if self.reader is None or self.coordinator is None:
            return False
        database = self.run_dir / "private" / "execution.sqlite"
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1) as db:
            db.row_factory = sqlite3.Row
            run = db.execute("SELECT initial_budget, spent, reserved, unit FROM runs WHERE run_id=?", (self.run_id,)).fetchone()
            loop = db.execute("SELECT config_sha256, status, stop_reason, resumable, selector_calls, updated_at FROM loop_runs WHERE run_id=?", (self.run_id,)).fetchone()
            if run is None or loop is None:
                return False
            rows = db.execute(
                """SELECT step_no, action_id, request_id, candidate_id, assay_id,
                          view_state_version, view_digest, selected_reason, status,
                          execution_id, observations_added, budget_spent,
                          budget_reserved, budget_available, error_code, updated_at
                   FROM loop_steps WHERE run_id=? ORDER BY step_no""", (self.run_id,),
            ).fetchall()
        view = self.reader.current_state()
        budget_state = self.reader.current_budget()
        step_docs: list[dict[str, Any]] = []
        archive_executions: list[dict[str, Any]] = []
        evidence_cache: dict[str, dict[str, Any]] = {}
        published_by_execution: dict[str, Any] = {}
        for published in view.released_executions:
            published_by_execution[published.execution_id] = published
        public_observation_count = 0
        for row in rows:
            evidence_refs: list[dict[str, str]] = []
            execution_id = row["execution_id"]
            published_observations: list[dict[str, Any]] = []
            if row["status"] == "released" and execution_id:
                published = published_by_execution.get(execution_id)
                if published is None:
                    raise Stage4ServiceError("public_result_storage_failed", "공개 release가 reader 결과와 일치하지 않습니다.", 500)
                step_identity = {
                    "step_no": int(row["step_no"]), "action_id": row["action_id"],
                    "candidate_id": row["candidate_id"], "assay_id": row["assay_id"],
                }
                archive = _archive_execution(self.reader, published, step_identity)
                archive_executions.append(archive)
                for observation in published.result.observations:
                    refs = []
                    for evidence_id in observation.evidence_ids:
                        evidence = evidence_cache.get(evidence_id)
                        if evidence is None:
                            loaded = self.reader.evidence(evidence_id)
                            evidence = {
                                "evidence_id": loaded.reference.evidence_id,
                                "reference": loaded.reference.model_dump(mode="json"),
                                "sha256": loaded.sha256,
                                "payload": json.loads(loaded.payload),
                            }
                            evidence_cache[evidence_id] = evidence
                        refs.append({"evidence_id": evidence_id, "sha256": evidence["sha256"]})
                    published_observations.append({
                        "observation_id": observation.observation_id,
                        "evidence": refs,
                    })
                    public_observation_count += 1
            step_docs.append({
                "step_no": int(row["step_no"]), "action_id": row["action_id"],
                "request_id": row["request_id"], "execution_id": execution_id,
                "candidate_id": row["candidate_id"], "assay_id": row["assay_id"],
                "public_view": {"state_version": int(row["view_state_version"]), "digest": row["view_digest"]},
                "selection_reason": row["selected_reason"], "status": row["status"],
                "error_code": row["error_code"],
                "budget_after_step": {
                    "spent": row["budget_spent"], "reserved": row["budget_reserved"],
                    "available": row["budget_available"], "unit": run["unit"],
                },
                "published_observations": published_observations,
            })
        budget = {
            "total": str(budget_state.total), "spent": str(budget_state.spent),
            "reserved": str(budget_state.reserved), "available": str(budget_state.available),
            "unit": budget_state.unit, "assumed": True,
        }
        metrics = _public_metrics(step_docs, archive_executions, budget)
        signature_doc = {
            "state_version": view.state_version, "loop_status": loop["status"],
            "stop_reason": loop["stop_reason"], "updated_at": loop["updated_at"],
            "budget": budget, "steps": [
                [row["step_no"], row["status"], row["execution_id"], row["updated_at"]] for row in rows
            ],
        }
        signature = _sha256(_canonical_json(signature_doc))
        if not force and signature == self.signature:
            return False
        selector = {
            "kind": self.state["configuration"]["selector"],
            "seed": self.state["configuration"].get("seed"),
            "algorithm_version": self.state["configuration"].get("selector_algorithm_version"),
        }
        trace = {
            "schema_version": "assaypilot.stage4.public-run-trace.v1",
            "run_id": self.run_id, "snapshot_revision": self.state["configuration"]["campaign"],
            "selector": selector, "public_view_schema_version": "assaypilot.selector-view.v1",
            "stop_reason": loop["stop_reason"], "budget_current": budget, "steps": step_docs,
        }
        archive_doc = {
            "schema_version": "assaypilot.stage4.published-results.v1",
            "run_id": self.run_id, "snapshot_revision": self.state["configuration"]["campaign"],
            "assay_id": "mep2-confirmatory", "executions": archive_executions,
        }
        summary = {
            "schema_version": "assaypilot.stage4.run-summary.v1", "run_id": self.run_id,
            "campaign": self.state["configuration"]["campaign"], "selector": selector,
            "configuration": self.state["configuration"], "configuration_sha256": self.state["configuration_sha256"],
            "run_config_sha256": loop["config_sha256"], "domain_status": loop["status"],
            "stop_reason": loop["stop_reason"], "resumable": bool(loop["resumable"]),
            "selector_calls": int(loop["selector_calls"]), "public_state_version": view.state_version,
            "public_observations_added": public_observation_count, "public_metrics": metrics,
            "updated_at": loop["updated_at"], "cli_summary": cli_summary,
        }
        public_export = {
            "schema_version": "assaypilot.stage4.public-export.v1",
            "run_id": self.run_id, "configuration": self.state["configuration"],
            "summary": summary, "trace": trace, "published_results": archive_doc,
        }
        next_revision = self._next_revision()
        artifacts_root = self.run_dir / "artifacts"
        artifacts_root.mkdir(mode=0o700, exist_ok=True)
        staging = artifacts_root / f".building-{uuid4().hex}"
        staging.mkdir(mode=0o700)
        try:
            _atomic_json(staging / "summary.json", summary)
            _atomic_json(staging / "trace.json", trace)
            _atomic_json(staging / "published_results.json", archive_doc)
            _atomic_json(staging / "public_export.json", public_export)
            destination = artifacts_root / f"rev-{next_revision:06d}"
            os.replace(staging, destination)
            self.service._update_state(self.run_dir, artifact_revision=next_revision)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self.revision = next_revision
        self.signature = signature
        return True

    def _next_revision(self) -> int:
        artifacts = self.run_dir / "artifacts"
        highest = 0
        if artifacts.exists():
            for path in artifacts.glob("rev-[0-9]*"):
                try:
                    highest = max(highest, int(path.name.removeprefix("rev-")))
                except ValueError:
                    continue
        return highest + 1


def _cli_command(state: dict[str, Any], runtime_root: Path, *, resume: bool) -> list[str]:
    run_id = state["run_id"]
    config = state["configuration"]
    snapshot = (ROOT / CAMPAIGNS[config["campaign"]]["snapshot"]).resolve()
    database = (runtime_root / "runs" / run_id / "private" / "execution.sqlite").resolve()
    common = [
        sys.executable, "-m", "assaypilot.run_loop_cli", "resume" if resume else "start",
        "--snapshot", str(snapshot), "--runtime-db", str(database), "--run-id", run_id,
    ]
    if resume:
        return common
    common.extend([
        "--budget", config["budget"], "--budget-unit", BUDGET_UNIT, "--budget-assumed",
        "--cost-policy-version", COST_POLICY_VERSION,
        "--approval-policy", APPROVAL_POLICY, "--approver-id", APPROVER_ID,
        "--selector", config["selector"], "--max-steps", str(config["max_steps"]),
        "--max-duration-seconds", str(MAX_DURATION_SECONDS),
        "--max-action-retries", str(MAX_ACTION_RETRIES),
        "--max-release-retries", str(MAX_RELEASE_RETRIES),
        "--selector-timeout-seconds", str(SELECTOR_TIMEOUT_SECONDS),
    ])
    if config["selector"] == "seeded_random_priority":
        common.extend([
            "--seed", str(config["seed"]),
            "--selector-algorithm-version", RANDOM_PRIORITY_VERSION,
        ])
    return common


def run_worker(runtime_root: str | Path, run_id: str, *, resume: bool = False) -> int:
    service = Stage4RunService(runtime_root, readiness_probe=lambda: {"available": True, "code": None})
    run_dir = service._run_dir(run_id)
    state = service._read_state(run_dir)
    service._update_state(
        run_dir, service_status="running", worker_pid=os.getpid(),
        started_at=state.get("started_at") or _now(), error=None,
    )
    state = service._read_state(run_dir)
    database = run_dir / "private" / "execution.sqlite"
    database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(database.parent, 0o700)
    stdout_path, stderr_path = run_dir / "cli.stdout", run_dir / "cli.stderr"
    exporter = _PublicArtifactExporter(service, run_id)
    process: subprocess.Popen[bytes] | None = None
    try:
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            os.chmod(stdout_path, 0o600)
            os.chmod(stderr_path, 0o600)
            process = subprocess.Popen(
                _cli_command(state, service.runtime_root, resume=resume), cwd=ROOT,
                stdin=subprocess.DEVNULL, stdout=stdout_file, stderr=stderr_file,
                close_fds=True,
            )
            while process.poll() is None:
                try:
                    exporter.refresh()
                except Exception:
                    # Keep the existing durable loop alive; report artifact failure
                    # only if a final read/export also fails.
                    pass
                try:
                    process.wait(timeout=0.35)
                except subprocess.TimeoutExpired:
                    continue
            return_code = int(process.returncode or 0)
        os.chmod(database, 0o600) if database.exists() else None
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        outcome = classify_cli_outcome(return_code, stdout, stderr)
        cli_summary = outcome.get("summary")
        try:
            exporter.refresh(force=True, cli_summary=cli_summary)
            if exporter.reader is None or exporter.revision < 1:
                if outcome["service_status"] == "failed" and (return_code != 0 or cli_summary is None):
                    service._update_state(
                        run_dir, service_status="failed", domain_status=None,
                        stop_reason=None, ended_at=_now(), error=outcome["error"],
                    )
                    return 1
                raise Stage4ServiceError(
                    "public_result_storage_failed",
                    "공개 결과 artifact를 생성할 공개 reader가 없습니다.", 500,
                )
        except Exception:
            service._update_state(
                run_dir, service_status="failed", domain_status=(cli_summary or {}).get("status"),
                stop_reason=(cli_summary or {}).get("stop_reason"), ended_at=_now(),
                error={"code": "public_result_storage_failed", "message": "공개 결과 artifact를 안전하게 저장하지 못했습니다."},
            )
            return 1
        service._update_state(
            run_dir, service_status=outcome["service_status"],
            domain_status=(cli_summary or {}).get("status"),
            stop_reason=outcome["stop_reason"], ended_at=_now(), error=outcome["error"],
        )
        return 0 if outcome["service_status"] in {"completed", "interrupted"} else 1
    except Exception:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        service._update_state(
            run_dir, service_status="failed", ended_at=_now(),
            error={"code": "worker_internal_error", "message": "run worker 내부 오류가 발생했습니다."},
        )
        return 1
