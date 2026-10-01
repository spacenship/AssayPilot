"""Trusted Stage 2-B/2-C execution and result-publication control.

The coordinator is an internal Python boundary.  It records explicit approval,
reserves configured Decimal costs, calls an already-loaded ReplayOracle, and
stores the private result in SQLite. Stage 2-C validates and atomically
publishes every measurement, registers evidence, and settles its reservation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Callable, Literal, Protocol
from uuid import uuid4

from pydantic import AwareDatetime, TypeAdapter, ValidationError

from assaypilot.data.schemas import NormalizedMeasurement
from assaypilot.domain import (
    ActionRequest,
    ApprovedAction,
    BudgetState,
    Cost,
    EvidenceRef,
    ExecutionReceipt,
    ExecutionResult,
    GovernanceDecision,
    Observation,
    PublicCampaign,
    RunState,
    Verdict,
    validate_execution,
    validate_public_campaign,
    validate_run_state,
)
from assaypilot.replay import ReplayError, ReplayLookupResult, ReplayOracle


_SCHEMA_VERSION = 3
_ACTION_KIND = "followup_replay_lookup"
_MONEY_ZERO = Decimal("0")
_MAX_PUBLIC_EVIDENCE_BYTES = 32 * 1024


class ExecutionControlError(ValueError):
    """Base error with a stable code safe to return outside the private store."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class RunNotFoundError(ExecutionControlError):
    """No persistent run has the requested identifier."""


class RunBindingError(ExecutionControlError):
    """A reopened coordinator does not match the run's fixed public inputs."""


class ActionRejectedError(ExecutionControlError):
    """The requested action violates the supported execution contract."""


class ApprovalError(ExecutionControlError):
    """The action has no matching, current explicit approval."""


class BudgetError(ExecutionControlError):
    """The Decimal budget cannot cover a reservation."""


class IdempotencyConflictError(ExecutionControlError):
    """A request identifier was reused for a different payload."""


class PrivateResultError(ExecutionControlError):
    """A private result is absent, inconsistent, or not ready for handoff."""


class ReplayLookup(Protocol):
    """Narrow lookup interface used to inject a spy in tests."""

    store: object

    def lookup(self, candidate_id: str, assay_id: str) -> ReplayLookupResult:
        """Read all measurements for one candidate and configured follow-up."""


@dataclass(frozen=True, slots=True)
class RunBudget:
    """Persistent run identity and exact Decimal budget snapshot."""

    run_id: str
    snapshot_id: str
    campaign_id: str
    cost_policy_version: str
    budget: BudgetState
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionReceiptView:
    """Execution history view; budget fields are historical, not current."""

    execution_id: str
    request_id: str
    action_id: str
    status: Literal["ready_for_release", "released", "cancelled", "no_record", "failed"]
    reserved: Decimal
    available: Decimal
    unit: str
    requested_at: datetime
    accepted_at: datetime
    executed_at: datetime
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalStatus:
    """Approval lifecycle metadata; it is not an execution receipt."""

    approval_id: str
    status: Literal["approved", "rejected", "canceled"]
    reviewed_at: datetime
    cost_amount: Decimal
    unit: str
    cost_policy_version: str


@dataclass(frozen=True, slots=True)
class PublishedExecution:
    """Committed public receipt and result for one released execution."""

    execution_id: str
    receipt: ExecutionReceipt
    result: ExecutionResult
    published_at: datetime
    state_version: int
    cost: Decimal
    unit: str


@dataclass(frozen=True, slots=True)
class PublicEvidence:
    """Allowlisted evidence reference and its exact canonical JSON bytes."""

    reference: EvidenceRef
    payload: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class PublicRunView:
    """A defensive, run-scoped current public state assembled from SQLite."""

    run_id: str
    state_version: int
    as_of: datetime
    public: PublicCampaign
    state: RunState
    released_executions: tuple[PublishedExecution, ...]


@dataclass(frozen=True, slots=True)
class ActionExecutionStatus:
    """Trusted loop metadata for an action already processed in this run."""

    candidate_id: str
    assay_id: str
    execution_id: str
    status: Literal["ready_for_release", "released", "cancelled", "no_record", "failed"]


class ExecutionCoordinator:
    """Persisted approval, budget reservation, replay and private result service.

    ``oracle`` must be initialized from a verified snapshot before constructing
    this service. ``cost_policy_version`` is mandatory and binds each run to
    the public assay cost interpretation used by this implementation.
    """

    def __init__(
        self,
        database_path: str | Path,
        public: PublicCampaign,
        oracle: ReplayLookup | ReplayOracle,
        *,
        cost_policy_version: str,
        clock: Callable[[], datetime],
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        if not isinstance(cost_policy_version, str) or not cost_policy_version.strip():
            raise ValueError("cost_policy_version must be explicit and non-empty")
        if isinstance(busy_timeout_seconds, bool) or not isinstance(busy_timeout_seconds, (int, float)):
            raise ValueError("busy_timeout_seconds must be a finite positive number")
        if not 0 < busy_timeout_seconds <= 60:
            raise ValueError("busy_timeout_seconds must be in (0, 60]")
        audit = validate_public_campaign(public)
        if not audit.ok:
            raise ValueError("public campaign failed validation")
        store = getattr(oracle, "store", None)
        snapshot_id = getattr(store, "snapshot_id", None)
        campaign_id = getattr(store, "campaign_id", None)
        store_public_digest = getattr(store, "public_campaign_sha256", None)
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ValueError("oracle must be initialized from a verified replay store")
        if campaign_id != public.campaign.campaign_id:
            raise ValueError("oracle store campaign does not match public campaign")
        public_digest = hashlib.sha256(public.model_dump_json().encode("utf-8")).hexdigest()
        if store_public_digest != public_digest:
            raise ValueError("oracle store is not bound to this exact public campaign")

        self.database_path = Path(database_path).expanduser().absolute()
        if str(database_path) == ":memory:":
            raise ValueError("a persistent private runtime database path is required")
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.public = PublicCampaign.model_validate_json(public.model_dump_json())
        self.oracle = oracle
        self.snapshot_id = snapshot_id
        self.campaign_id = self.public.campaign.campaign_id
        self.cost_policy_version = cost_policy_version
        self.clock = clock
        self.busy_timeout_seconds = float(busy_timeout_seconds)
        self._public_digest = public_digest
        self._assays = {assay.assay_id: assay for assay in self.public.assays}
        self._candidates = {candidate.candidate_id for candidate in self.public.candidates}
        self._supported_assays = frozenset(getattr(store, "_supported_assays", ()))
        if not self._supported_assays:
            raise ValueError("oracle store has no supported follow-up assays")
        self._initial_evidence_payloads = dict(getattr(store, "_public_evidence_payloads", {}))
        if any(reference.evidence_id not in self._initial_evidence_payloads for reference in self.public.evidence):
            raise ValueError("oracle store must contain verified payloads for all initial public evidence")
        self._evidence_policy = self._load_evidence_policy()
        self._ensure_schema()

    def initialize_run(self, run_id: str, initial_budget: Cost) -> RunBudget:
        """Create or idempotently reopen a run with explicit budget and units."""
        _require_text(run_id, "run_id")
        budget = Cost.model_validate(initial_budget.model_dump(mode="python"))
        if budget.unit != self.public.campaign.budget.unit:
            raise BudgetError("unit_mismatch", "initial budget unit differs from the campaign budget unit")
        created_at = self._now()
        if created_at < self.public.as_of:
            raise ExecutionControlError("time_before_snapshot", "run time predates the public snapshot")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is not None:
                self._assert_run_binding(row)
                if row["initial_budget"] != _decimal_text(budget.amount) or row["unit"] != budget.unit:
                    raise RunBindingError("run_id_conflict", "run_id already exists with different budget terms")
                return self._run_budget(row)
            db.execute(
                """INSERT INTO runs (
                    run_id, snapshot_id, campaign_id, public_sha256, initial_budget,
                    unit, spent, reserved, cost_policy_version, initial_as_of, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, self.snapshot_id, self.campaign_id, self._public_digest,
                    _decimal_text(budget.amount), budget.unit, "0", "0",
                    self.cost_policy_version, _dt_text(self.public.as_of), _dt_text(created_at),
                ),
            )
            row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            db.execute(
                "INSERT OR IGNORE INTO run_public_state (run_id, state_version, as_of, observations_json) VALUES (?, 0, ?, '[]')",
                (run_id, _dt_text(created_at)),
            )
            return self._run_budget(row)

    def get_budget(self, run_id: str) -> RunBudget:
        """Restore the current exact budget and run binding from SQLite."""
        with self._connection() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError("run_not_found", "run does not exist")
        self._assert_run_binding(row)
        return self._run_budget(row)

    def approve_action(
        self,
        run_id: str,
        action: ActionRequest,
        *,
        approver_id: str,
        reason: str,
    ) -> GovernanceDecision:
        """Explicitly approve one public candidate/follow-up action."""
        _require_text(approver_id, "approver_id")
        _require_text(reason, "reason")
        action = _copy_action(action)
        self._validate_action(action)
        reviewed_at = self._now()
        key = _action_key(self.snapshot_id, self.campaign_id, action.candidate_id, action.assay_id)
        amount, unit = self._configured_cost(action.assay_id)
        with self._transaction() as db:
            run = self._load_run(db, run_id)
            self._assert_run_binding(run)
            self._validate_run_event_time(db, run_id, reviewed_at, "approval")
            digest = self._action_digest(run_id, action, amount, unit)
            old = db.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND action_key = ?", (run_id, key)
            ).fetchone()
            if old is not None:
                if old["action_digest"] != digest:
                    raise ApprovalError("approval_mismatch", "existing approval is bound to different action terms")
                if old["status"] != "approved":
                    raise ApprovalError("approval_not_active", "action approval is rejected or canceled")
                return GovernanceDecision.model_validate_json(old["decision_json"])
            approval_id = f"approval-{uuid4().hex}"
            approved = ApprovedAction(
                action=action,
                approval_id=approval_id,
                reviewed_at=reviewed_at,
                reason=reason,
            )
            decision = GovernanceDecision(
                action_id=action.action_id,
                decision="approved",
                reason=reason,
                approved_action=approved,
            )
            db.execute(
                """INSERT INTO approvals (
                    run_id, action_key, snapshot_id, action_digest, status, decision_json,
                    approval_id, approver_id, reviewed_at, cost_amount, unit, cost_policy_version
                ) VALUES (?, ?, ?, ?, 'approved', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, key, self.snapshot_id, digest, decision.model_dump_json(),
                    approval_id, approver_id, _dt_text(reviewed_at), _decimal_text(amount),
                    unit, self.cost_policy_version,
                ),
            )
            return decision

    def reject_action(
        self,
        run_id: str,
        action: ActionRequest,
        *,
        approver_id: str,
        reason: str,
    ) -> GovernanceDecision:
        """Persist an explicit denial; it never reserves budget or calls Oracle."""
        _require_text(approver_id, "approver_id")
        _require_text(reason, "reason")
        action = _copy_action(action)
        self._validate_action(action)
        reviewed_at = self._now()
        key = _action_key(self.snapshot_id, self.campaign_id, action.candidate_id, action.assay_id)
        amount, unit = self._configured_cost(action.assay_id)
        with self._transaction() as db:
            run = self._load_run(db, run_id)
            self._assert_run_binding(run)
            self._validate_run_event_time(db, run_id, reviewed_at, "decision")
            digest = self._action_digest(run_id, action, amount, unit)
            old = db.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND action_key = ?", (run_id, key)
            ).fetchone()
            if old is not None:
                if old["action_digest"] == digest and old["status"] == "rejected":
                    return GovernanceDecision.model_validate_json(old["decision_json"])
                raise ApprovalError("approval_exists", "an approval decision already exists for this action")
            decision = GovernanceDecision(
                action_id=action.action_id,
                decision="rejected",
                reason=reason,
            )
            db.execute(
                """INSERT INTO approvals (
                    run_id, action_key, snapshot_id, action_digest, status, decision_json,
                    approval_id, approver_id, reviewed_at, cost_amount, unit, cost_policy_version
                ) VALUES (?, ?, ?, ?, 'rejected', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, key, self.snapshot_id, digest, decision.model_dump_json(),
                    f"approval-{uuid4().hex}", approver_id, _dt_text(reviewed_at),
                    _decimal_text(amount), unit, self.cost_policy_version,
                ),
            )
            return decision

    def approval_status(self, run_id: str, action: ActionRequest) -> ApprovalStatus:
        """Read approval lifecycle state without exposing private measurements."""
        action = _copy_action(action)
        key = _action_key(self.snapshot_id, self.campaign_id, action.candidate_id, action.assay_id)
        with self._connection() as db:
            run = self._load_run(db, run_id)
            self._assert_run_binding(run)
            row = db.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND action_key = ?", (run_id, key)
            ).fetchone()
        if row is None:
            raise ApprovalError("approval_not_found", "no approval decision exists for this action")
        return ApprovalStatus(
            approval_id=row["approval_id"],
            status=row["status"],
            reviewed_at=_parse_dt(row["reviewed_at"]),
            cost_amount=Decimal(row["cost_amount"]),
            unit=row["unit"],
            cost_policy_version=row["cost_policy_version"],
        )

    def cancel_approval(self, run_id: str, action: ActionRequest) -> ApprovalStatus:
        """Cancel an unused approval. Existing terminal executions remain immutable."""
        action = _copy_action(action)
        key = _action_key(self.snapshot_id, self.campaign_id, action.candidate_id, action.assay_id)
        canceled_at = self._now()
        with self._transaction() as db:
            run = self._load_run(db, run_id)
            self._assert_run_binding(run)
            row = db.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND action_key = ?", (run_id, key)
            ).fetchone()
            if row is None:
                raise ApprovalError("approval_not_found", "no approval decision exists for this action")
            if row["status"] != "approved":
                raise ApprovalError("approval_not_active", "only an active approval can be canceled")
            self._validate_run_event_time(db, run_id, canceled_at, "cancellation")
            db.execute(
                "UPDATE approvals SET status = 'canceled', canceled_at = ? WHERE run_id = ? AND action_key = ?",
                (_dt_text(canceled_at), run_id, key),
            )
            return ApprovalStatus(
                approval_id=row["approval_id"], status="canceled",
                reviewed_at=_parse_dt(row["reviewed_at"]),
                cost_amount=Decimal(row["cost_amount"]), unit=row["unit"],
                cost_policy_version=row["cost_policy_version"],
            )

    def execute(self, run_id: str, request_id: str, action: ActionRequest) -> ExecutionReceiptView:
        """Reserve configured cost, query once, and persist an internal outcome.

        Same request ID plus identical payload replays the stored receipt. A new
        request ID for an already-executed candidate/assay action maps to the
        existing execution and does not call Oracle again.
        """
        _require_text(request_id, "request_id")
        action = _copy_action(action)
        self._validate_action(action)
        requested_at = self._now()
        key = _action_key(self.snapshot_id, self.campaign_id, action.candidate_id, action.assay_id)
        payload_digest = _sha256(_canonical_json({
            "run_id": run_id,
            "request_id": request_id,
            "action": action.model_dump(mode="json"),
        }))
        amount, unit = self._configured_cost(action.assay_id)

        # BEGIN IMMEDIATE serializes request/action uniqueness and budget changes.
        # Oracle is an already-loaded, read-only in-memory index; no snapshot
        # hashing, network access, or experimental side effect occurs here.
        with self._transaction() as db:
            run = self._load_run(db, run_id)
            self._assert_run_binding(run)
            if requested_at < self.public.as_of or requested_at < _parse_dt(run["created_at"]):
                raise ExecutionControlError("time_before_run", "request time predates run or public snapshot")
            previous_request = db.execute(
                "SELECT * FROM execution_requests WHERE run_id = ? AND request_id = ?",
                (run_id, request_id),
            ).fetchone()
            if previous_request is not None:
                if previous_request["payload_digest"] != payload_digest:
                    raise IdempotencyConflictError(
                        "idempotency_conflict", "request_id was already used for a different payload"
                    )
                execution = self._load_execution(db, previous_request["execution_id"])
                return self._receipt(execution, request_id)

            existing = db.execute(
                """SELECT * FROM executions
                   WHERE run_id = ? AND snapshot_id = ? AND candidate_id = ?
                     AND assay_id = ? AND action_kind = ?""",
                (run_id, self.snapshot_id, action.candidate_id, action.assay_id, _ACTION_KIND),
            ).fetchone()
            if existing is not None:
                db.execute(
                    "INSERT INTO execution_requests (run_id, request_id, payload_digest, execution_id) VALUES (?, ?, ?, ?)",
                    (run_id, request_id, payload_digest, existing["execution_id"]),
                )
                return self._receipt(existing, request_id)

            approval = db.execute(
                "SELECT * FROM approvals WHERE run_id = ? AND action_key = ?", (run_id, key)
            ).fetchone()
            if approval is None:
                raise ApprovalError("approval_required", "action requires an explicit approval")
            digest = self._action_digest(run_id, action, amount, unit)
            if approval["status"] != "approved":
                raise ApprovalError("approval_not_active", "action approval is rejected or canceled")
            if approval["action_digest"] != digest:
                raise ApprovalError("approval_mismatch", "approval does not match run, action, snapshot, cost, or policy")
            if approval["snapshot_id"] != self.snapshot_id:
                raise ApprovalError("approval_snapshot_mismatch", "approval belongs to another snapshot")
            if approval["cost_amount"] != _decimal_text(amount) or approval["unit"] != unit:
                raise ApprovalError("approval_cost_mismatch", "approval cost no longer matches configured assay cost")
            if approval["cost_policy_version"] != self.cost_policy_version:
                raise ApprovalError("approval_policy_mismatch", "approval uses a different cost policy")
            if _parse_dt(approval["reviewed_at"]) > requested_at:
                raise ApprovalError("approval_after_request", "approval time is later than request time")
            runtime_state = db.execute(
                "SELECT state_version, as_of, observations_json FROM run_public_state WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if runtime_state is None:
                raise ExecutionControlError("runtime_state_missing", "run public state is unavailable")
            if requested_at < _parse_dt(runtime_state["as_of"]):
                raise ExecutionControlError("time_order", "request time predates current public state")
            runtime_observations = _load_observations(runtime_state["observations_json"])
            self._check_current_prerequisites(action, runtime_observations)

            spent = Decimal(run["spent"])
            reserved = Decimal(run["reserved"])
            initial = Decimal(run["initial_budget"])
            available = initial - spent - reserved
            if min(initial, spent, reserved, available) < 0:
                raise ExecutionControlError("budget_invariant", "stored budget violates conservation")
            if amount > available:
                raise BudgetError("insufficient_budget", "available budget cannot cover configured assay cost")

            execution_id = f"execution-{uuid4().hex}"
            accepted_at = requested_at
            db.execute(
                """INSERT INTO executions (
                    execution_id, run_id, snapshot_id, campaign_id, candidate_id, assay_id,
                    action_kind, action_digest, action_json, status, cost_amount, unit,
                    requested_at, accepted_at, executed_at, reserved_after, available_after, error_code,
                    prerequisite_state_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (
                    execution_id, run_id, self.snapshot_id, self.campaign_id,
                    action.candidate_id, action.assay_id, _ACTION_KIND, digest,
                    action.model_dump_json(), _decimal_text(amount), unit,
                    _dt_text(requested_at), _dt_text(accepted_at), _dt_text(requested_at),
                    _decimal_text(reserved + amount), _decimal_text(available - amount),
                    runtime_state["state_version"],
                ),
            )
            db.execute(
                "UPDATE runs SET reserved = ? WHERE run_id = ?",
                (_decimal_text(reserved + amount), run_id),
            )
            self._ledger(
                db, run_id, execution_id, "reserve", amount, unit,
                f"reserve:{execution_id}", requested_at,
            )

            try:
                outcome = self.oracle.lookup(action.candidate_id, action.assay_id)
                self._validate_lookup_result(outcome, action)
                executed_at = self._now()
                if executed_at < requested_at:
                    raise ExecutionControlError("time_order", "execution time predates request time")
            except ReplayError as exc:
                executed_at = self._now()
                self._release_reservation(db, run, execution_id, amount, unit, executed_at)
                failed_status = "failed"
                db.execute(
                    "UPDATE executions SET status = ?, executed_at = ?, reserved_after = ?, available_after = ?, error_code = ? WHERE execution_id = ?",
                    (
                        failed_status, _dt_text(executed_at), run["reserved"],
                        _decimal_text(available), exc.code, execution_id,
                    ),
                )
                db.execute(
                    "INSERT INTO private_results (execution_id, result_json) VALUES (?, ?)",
                    (execution_id, _empty_result_json(self.snapshot_id, self.campaign_id, action, "failed", exc.code)),
                )
                db.execute(
                    "INSERT INTO execution_requests (run_id, request_id, payload_digest, execution_id) VALUES (?, ?, ?, ?)",
                    (run_id, request_id, payload_digest, execution_id),
                )
                self._advance_public_state(db, run_id, executed_at)
                execution = self._load_execution(db, execution_id)
                return self._receipt(execution, request_id)
            # Unexpected Oracle exceptions and SQLite failures deliberately
            # escape the context manager, rolling back reservation and ledger.

            if outcome.status == "records_found":
                status = "ready_for_release"
                reserved_after = reserved + amount
                available_after = available - amount
            else:
                status = "no_record"
                self._release_reservation(db, run, execution_id, amount, unit, executed_at)
                reserved_after = reserved
                available_after = available
            db.execute(
                "INSERT INTO private_results (execution_id, result_json) VALUES (?, ?)",
                (execution_id, _lookup_result_json(outcome)),
            )
            db.execute(
                "UPDATE executions SET status = ?, executed_at = ?, reserved_after = ?, available_after = ? WHERE execution_id = ?",
                (status, _dt_text(executed_at), _decimal_text(reserved_after), _decimal_text(available_after), execution_id),
            )
            db.execute(
                "INSERT INTO execution_requests (run_id, request_id, payload_digest, execution_id) VALUES (?, ?, ?, ?)",
                (run_id, request_id, payload_digest, execution_id),
            )
            self._advance_public_state(db, run_id, executed_at)
            execution = self._load_execution(db, execution_id)
            return self._receipt(execution, request_id)

    def read_private_result(self, run_id: str, execution_id: str) -> ReplayLookupResult:
        """Read a defensive copy for 2-C only when status is ready_for_release."""
        with self._connection() as db:
            run = self._load_run(db, run_id)
            self._assert_run_binding(run)
            execution = self._load_execution(db, execution_id)
            if execution["run_id"] != run_id or execution["snapshot_id"] != self.snapshot_id:
                raise PrivateResultError("result_scope_mismatch", "execution is outside this run or snapshot")
            if execution["status"] != "ready_for_release" or execution["release_status"] != "pending":
                raise PrivateResultError("result_not_ready", "only ready_for_release outcomes can be handed to 2-C")
            row = db.execute(
                "SELECT result_json FROM private_results WHERE execution_id = ?", (execution_id,)
            ).fetchone()
        if row is None:
            raise PrivateResultError("result_missing", "private result was not persisted")
        result = _load_lookup_result(row["result_json"])
        if (
            result.snapshot_id != execution["snapshot_id"]
            or result.campaign_id != execution["campaign_id"]
            or result.candidate_id != execution["candidate_id"]
            or result.assay_id != execution["assay_id"]
            or result.status != "records_found"
        ):
            raise PrivateResultError("result_identity_mismatch", "stored private result does not match execution identity")
        return result

    def release_result(self, run_id: str, execution_id: str) -> PublishedExecution:
        """Validate, publish, and settle one ready result in a single transaction.

        The successful commit is the publication boundary. An already released
        execution returns the stored public objects with its original timestamp.
        """
        with self._transaction() as db:
            run = self._load_run(db, run_id)
            self._assert_run_binding(run)
            execution = self._load_execution(db, execution_id)
            if execution["run_id"] != run_id or execution["snapshot_id"] != self.snapshot_id:
                raise PrivateResultError("result_scope_mismatch", "execution is outside this run or snapshot")
            if execution["release_status"] == "released":
                return self._published_execution(db, run_id, execution_id)
            if execution["release_status"] == "cancelled":
                raise PrivateResultError("result_cancelled", "cancelled results cannot be published")
            if execution["status"] != "ready_for_release":
                raise PrivateResultError("result_not_ready", "only ready_for_release outcomes can be published")

            state_row = db.execute(
                "SELECT state_version, as_of, observations_json FROM run_public_state WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if state_row is None:
                raise ExecutionControlError("runtime_state_missing", "run public state is unavailable")
            published_at = self._now()
            accepted_at = _parse_dt(execution["accepted_at"])
            executed_at = _parse_dt(execution["executed_at"])
            if published_at < max(self.public.as_of, _parse_dt(run["created_at"]),
                                  accepted_at, executed_at, _parse_dt(state_row["as_of"])):
                raise ExecutionControlError("time_order", "publication time predates execution or current public state")

            action = self._validated_release_action(db, execution, run_id)
            result_row = db.execute(
                "SELECT result_json FROM private_results WHERE execution_id = ?", (execution_id,)
            ).fetchone()
            if result_row is None:
                raise PrivateResultError("result_missing", "private result was not persisted")
            lookup = _load_lookup_result(result_row["result_json"])
            self._validate_lookup_result(lookup, action)
            if lookup.status != "records_found" or not lookup.measurements:
                raise PrivateResultError("result_not_publishable", "only a non-empty records_found result can be published")

            current_runtime = _load_observations(state_row["observations_json"])
            new_observations, new_refs, evidence_payloads = self._public_measurements(
                run_id, execution_id, action, lookup, published_at,
            )
            candidate_observations = [*current_runtime, *new_observations]
            current_public = self._current_public(
                published_at, candidate_observations, [*self.public.evidence, *self._published_refs(db, run_id), *new_refs],
            )
            receipt_id = f"receipt-{execution_id}"
            public_receipt = ExecutionReceipt(
                receipt_id=receipt_id,
                action_id=action.action_id,
                accepted_at=accepted_at,
            )
            public_result = ExecutionResult(
                receipt_id=receipt_id,
                action_id=action.action_id,
                status="completed",
                observations=new_observations,
            )
            audit = validate_execution(action, public_receipt, public_result, current_public, as_of=published_at)
            if not audit.ok:
                raise ExecutionControlError("release_validation_failed", "public execution failed reference or time validation")

            amount = Decimal(execution["cost_amount"])
            unit = execution["unit"]
            spent = Decimal(run["spent"])
            reserved = Decimal(run["reserved"])
            if reserved < amount or spent + amount + (reserved - amount) > Decimal(run["initial_budget"]):
                raise ExecutionControlError("budget_invariant", "reserved budget cannot settle this execution")
            budget_after = BudgetState(
                total=Decimal(run["initial_budget"]), spent=spent + amount,
                reserved=reserved - amount, unit=unit,
            )
            state_version = int(state_row["state_version"]) + 1
            public_state = RunState(
                campaign_id=self.campaign_id,
                as_of=published_at,
                observations=[*self.public.observations, *candidate_observations],
                budget=budget_after,
                status="running",
            )
            state_audit = validate_run_state(
                public_state, current_public, expected_budget_total=Decimal(run["initial_budget"]),
            )
            if not state_audit.ok:
                raise ExecutionControlError("release_state_invalid", "current public run state failed validation")

            for reference, payload, payload_hash in evidence_payloads:
                db.execute(
                    """INSERT INTO published_evidence (
                        run_id, evidence_id, execution_id, reference_json, payload_json, payload_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (run_id, reference.evidence_id, execution_id, reference.model_dump_json(),
                     payload.decode("utf-8"), payload_hash),
                )
            db.execute(
                """INSERT INTO published_executions (
                    run_id, execution_id, receipt_json, result_json, published_at,
                    state_version, cost_amount, unit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, execution_id, public_receipt.model_dump_json(), public_result.model_dump_json(),
                 _dt_text(published_at), state_version, _decimal_text(amount), unit),
            )
            db.execute(
                """INSERT INTO release_settlements (
                    settlement_id, run_id, execution_id, amount, unit, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (f"settlement-{uuid4().hex}", run_id, execution_id,
                 _decimal_text(amount), unit, _dt_text(published_at)),
            )
            db.execute(
                "UPDATE runs SET spent = ?, reserved = ? WHERE run_id = ?",
                (_decimal_text(budget_after.spent), _decimal_text(budget_after.reserved), run_id),
            )
            db.execute(
                "UPDATE executions SET release_status = 'released', released_at = ? WHERE execution_id = ?",
                (_dt_text(published_at), execution_id),
            )
            db.execute(
                "UPDATE run_public_state SET state_version = ?, as_of = ?, observations_json = ? WHERE run_id = ?",
                (state_version, _dt_text(published_at), _observations_json(candidate_observations), run_id),
            )
            return PublishedExecution(
                execution_id=execution_id, receipt=public_receipt, result=public_result,
                published_at=published_at, state_version=state_version, cost=amount, unit=unit,
            )

    def cancel_pending_release(self, run_id: str, execution_id: str, reason: str) -> ExecutionReceiptView:
        """Cancel one unreleased lookup and release only its own reservation."""
        _require_text(reason, "reason")
        if len(reason) > 500:
            raise ExecutionControlError("invalid_cancel_reason", "cancellation reason is too long")
        with self._transaction() as db:
            run = self._load_run(db, run_id)
            self._assert_run_binding(run)
            execution = self._load_execution(db, execution_id)
            if execution["run_id"] != run_id or execution["snapshot_id"] != self.snapshot_id:
                raise PrivateResultError("result_scope_mismatch", "execution is outside this run or snapshot")
            if execution["release_status"] == "cancelled":
                return self._receipt(execution, f"cancelled-{execution_id}")
            if execution["release_status"] == "released":
                raise ExecutionControlError("already_released", "a released result cannot be cancelled")
            if execution["status"] != "ready_for_release":
                raise PrivateResultError("result_not_cancellable", "only ready_for_release outcomes can be cancelled")
            state = db.execute(
                "SELECT state_version, as_of FROM run_public_state WHERE run_id = ?", (run_id,),
            ).fetchone()
            cancelled_at = self._now()
            if state is None or cancelled_at < max(_parse_dt(run["created_at"]),
                                                   _parse_dt(execution["executed_at"]),
                                                   _parse_dt(state["as_of"])):
                raise ExecutionControlError("time_order", "cancellation time predates current public state")
            amount = Decimal(execution["cost_amount"])
            self._release_reservation(db, run, execution_id, amount, execution["unit"], cancelled_at)
            db.execute(
                "UPDATE executions SET release_status = 'cancelled', cancelled_at = ?, cancel_reason = ? WHERE execution_id = ?",
                (_dt_text(cancelled_at), reason, execution_id),
            )
            db.execute(
                "UPDATE run_public_state SET state_version = ?, as_of = ? WHERE run_id = ?",
                (int(state["state_version"]) + 1, _dt_text(cancelled_at), run_id),
            )
            updated = self._load_execution(db, execution_id)
            return self._receipt(updated, f"cancelled-{execution_id}")

    def get_current_budget(self, run_id: str) -> BudgetState:
        """Return the current database budget, distinct from any old receipt."""
        return self.get_budget(run_id).budget

    def get_public_state(self, run_id: str) -> PublicRunView:
        """Return a consistent, defensive view of the committed run state."""
        with self._connection() as db:
            db.execute("BEGIN")
            return self._public_state_from_connection(db, run_id)

    def get_loop_snapshot(
        self, run_id: str,
    ) -> tuple[PublicRunView, tuple[ActionExecutionStatus, ...]]:
        """Read public state and trusted execution statuses at one SQLite boundary.

        The action statuses are for the trusted controller only. They are never
        returned by PublicReader or used to query Oracle for availability.
        """
        with self._connection() as db:
            db.execute("BEGIN")
            view = self._public_state_from_connection(db, run_id)
            rows = db.execute(
                """SELECT candidate_id, assay_id, execution_id, status, release_status
                   FROM executions WHERE run_id = ? ORDER BY execution_id""",
                (run_id,),
            ).fetchall()
            statuses = tuple(
                ActionExecutionStatus(
                    candidate_id=row["candidate_id"], assay_id=row["assay_id"],
                    execution_id=row["execution_id"],
                    status=("released" if row["release_status"] == "released" else
                            "cancelled" if row["release_status"] == "cancelled" else row["status"]),
                )
                for row in rows
            )
            return view, statuses

    def _public_state_from_connection(self, db: sqlite3.Connection, run_id: str) -> PublicRunView:
        run = self._load_run(db, run_id)
        self._assert_run_binding(run)
        state_row = db.execute(
            "SELECT state_version, as_of, observations_json FROM run_public_state WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if state_row is None:
            raise ExecutionControlError("runtime_state_missing", "run public state is unavailable")
        runtime_observations = _load_observations(state_row["observations_json"])
        runtime_refs = self._published_refs(db, run_id)
        rows = db.execute(
            "SELECT execution_id FROM published_executions WHERE run_id = ? ORDER BY state_version",
            (run_id,),
        ).fetchall()
        published = tuple(self._published_execution(db, run_id, row["execution_id"]) for row in rows)
        as_of = _parse_dt(state_row["as_of"])
        public = self._current_public(
            as_of, runtime_observations, [*self.public.evidence, *runtime_refs],
        )
        budget = self._run_budget(run).budget
        state = RunState(
            campaign_id=self.campaign_id,
            as_of=as_of,
            observations=[*self.public.observations, *runtime_observations],
            budget=budget,
            status="running",
        )
        audit = validate_run_state(state, public, expected_budget_total=budget.total)
        if not audit.ok:
            raise ExecutionControlError("runtime_state_invalid", "committed public state failed validation")
        return PublicRunView(
            run_id=run_id, state_version=int(state_row["state_version"]), as_of=as_of,
            public=PublicCampaign.model_validate_json(public.model_dump_json()),
            state=RunState.model_validate_json(state.model_dump_json()),
            released_executions=published,
        )

    def _validate_run_event_time(
        self, db: sqlite3.Connection, run_id: str, event_at: datetime, event_name: str,
    ) -> None:
        if event_at.tzinfo is None or event_at.utcoffset() is None:
            raise ExecutionControlError("invalid_event_time", f"{event_name} time must be timezone-aware")
        run = self._load_run(db, run_id)
        state = db.execute(
            "SELECT as_of FROM run_public_state WHERE run_id = ?", (run_id,),
        ).fetchone()
        if state is None:
            raise ExecutionControlError("runtime_state_missing", "run public state is unavailable")
        cutoff = max(self.public.as_of, _parse_dt(run["created_at"]), _parse_dt(state["as_of"]))
        if event_at < cutoff:
            raise ExecutionControlError("time_order", f"{event_name} time predates current run/public state")

    def get_public_execution(self, run_id: str, execution_id: str) -> PublishedExecution:
        """Read a released result only when it belongs to the bound run."""
        with self._connection() as db:
            run = self._load_run(db, run_id)
            self._assert_run_binding(run)
            return self._published_execution(db, run_id, execution_id)

    def get_public_evidence(self, run_id: str, evidence_id: str) -> PublicEvidence:
        """Resolve a known initial or committed runtime evidence ID, never a path."""
        with self._connection() as db:
            run = self._load_run(db, run_id)
            self._assert_run_binding(run)
            initial_ref = next((item for item in self.public.evidence if item.evidence_id == evidence_id), None)
            if initial_ref is not None:
                payload = self._initial_evidence_payloads[evidence_id]
                return PublicEvidence(initial_ref, payload, _sha256_bytes(payload))
            row = db.execute(
                "SELECT reference_json, payload_json, payload_sha256 FROM published_evidence WHERE run_id = ? AND evidence_id = ?",
                (run_id, evidence_id),
            ).fetchone()
        if row is None:
            raise ExecutionControlError("public_evidence_not_found", "public evidence is unavailable")
        payload = row["payload_json"].encode("utf-8")
        if _sha256_bytes(payload) != row["payload_sha256"]:
            raise ExecutionControlError("public_evidence_corrupt", "public evidence failed integrity validation")
        reference = EvidenceRef.model_validate_json(row["reference_json"])
        document = _json_object(payload)
        if document.get("evidence_id") != reference.evidence_id:
            raise ExecutionControlError("public_evidence_corrupt", "public evidence identity is inconsistent")
        return PublicEvidence(reference, payload, row["payload_sha256"])

    def public_reader(self, run_id: str):
        """Create a minimal read-only reader with run identity fixed by trusted code."""
        from assaypilot.public_api import PublicReader

        self.get_public_state(run_id)
        return PublicReader(self, run_id)

    def _load_evidence_policy(self) -> dict[str, dict[str, object]]:
        policies: dict[str, dict[str, object]] = {}
        for assay in self.public.assays:
            matches = []
            for reference in self.public.evidence:
                try:
                    document = json.loads(self._initial_evidence_payloads[reference.evidence_id])
                except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise ValueError("verified public evidence payload is malformed") from exc
                if not isinstance(document, dict):
                    continue
                if (document.get("name"), document.get("endpoint"), document.get("unit")) != (
                    assay.name, assay.endpoint, assay.unit,
                ):
                    continue
                aid = document.get("aid")
                if isinstance(aid, int) and reference.source_id == f"PubChem AID:{aid}":
                    matches.append((aid, document))
            if len(matches) != 1:
                raise ValueError("assay must resolve to exactly one verified public evidence policy")
            aid, document = matches[0]
            raw_outcome_column = document.get("raw_outcome_column")
            raw_endpoint_column = document.get("raw_endpoint_column")
            protocol_location = document.get("protocol_location")
            if (not isinstance(raw_outcome_column, str) or not raw_outcome_column
                    or (raw_endpoint_column is not None and not isinstance(raw_endpoint_column, str))
                    or not isinstance(protocol_location, str) or not protocol_location):
                raise ValueError("public evidence policy is incomplete")
            policies[assay.assay_id] = {
                "aid": aid,
                "raw_outcome_column": raw_outcome_column,
                "raw_endpoint_column": raw_endpoint_column,
                "protocol_location": protocol_location,
            }
        return policies

    def _public_measurements(
        self,
        run_id: str,
        execution_id: str,
        action: ActionRequest,
        lookup: ReplayLookupResult,
        published_at: datetime,
    ) -> tuple[list[Observation], list[EvidenceRef], list[tuple[EvidenceRef, bytes, str]]]:
        candidate = next((item for item in self.public.candidates if item.candidate_id == action.candidate_id), None)
        policy = self._evidence_policy.get(action.assay_id)
        if candidate is None or candidate.source != "pubchem_sid" or policy is None:
            raise PrivateResultError("result_identity_mismatch", "result candidate or assay evidence policy is invalid")
        observations: list[Observation] = []
        references: list[EvidenceRef] = []
        payloads: list[tuple[EvidenceRef, bytes, str]] = []
        seen_measurements: set[str] = set()
        run_key = _sha256(run_id)[:24]
        for measurement in lookup.measurements:
            if measurement.measurement_id in seen_measurements:
                raise PrivateResultError("duplicate_measurement", "private result contains a duplicate measurement ID")
            seen_measurements.add(measurement.measurement_id)
            if (measurement.assay_id != action.assay_id or measurement.aid != policy["aid"]
                    or candidate.source_id != f"SID:{measurement.sid}"):
                raise PrivateResultError("result_identity_mismatch", "measurement does not match the approved candidate and assay")
            if not re.fullmatch(r"[0-9a-f]{64}", measurement.source_file_sha256):
                raise PrivateResultError("source_hash_invalid", "source file hash is invalid")
            raw_row = measurement.raw_row
            if raw_row.get("SID") != str(measurement.sid) or raw_row.get("AID") != str(measurement.aid):
                raise PrivateResultError("source_row_identity_mismatch", "source row identifiers do not match normalized measurement")
            if measurement.cid is not None and raw_row.get("CID") != str(measurement.cid):
                raise PrivateResultError("source_row_identity_mismatch", "source row CID does not match normalized measurement")
            outcome_column = str(policy["raw_outcome_column"])
            if outcome_column in raw_row:
                raw_outcome = raw_row[outcome_column]
                normalized_raw_outcome = raw_outcome.strip() or None if isinstance(raw_outcome, str) else raw_outcome
                if normalized_raw_outcome != measurement.raw_verdict:
                    raise PrivateResultError("source_row_outcome_mismatch", "source row outcome differs from normalized measurement")
            endpoint_column = policy["raw_endpoint_column"]
            allowed_columns = ["AID", "SID", "CID", outcome_column, "Activity Name"]
            if isinstance(endpoint_column, str):
                allowed_columns.append(endpoint_column)
            allowed = {
                key: raw_row[key]
                for key in dict.fromkeys(allowed_columns)
                if key in raw_row
            }
            evidence_id = _stable_public_id("evidence", run_id, execution_id, measurement.measurement_id)
            observation_id = _stable_public_id("observation", run_id, execution_id, measurement.measurement_id)
            reference = EvidenceRef(
                evidence_id=evidence_id,
                source_kind="pubchem_runtime_measurement",
                source_id=measurement.source_row_id,
                location=f"runtime/{run_key}/{evidence_id}.json",
            )
            evidence_document = {
                "schema_version": "1.0.0",
                "evidence_id": evidence_id,
                "source_kind": reference.source_kind,
                "measurement_id": measurement.measurement_id,
                "source_row_id": measurement.source_row_id,
                "source_row_number": measurement.source_row_number,
                "source_file_sha256": measurement.source_file_sha256,
                "candidate_id": action.candidate_id,
                "sid": measurement.sid,
                "aid": measurement.aid,
                "cid": measurement.cid,
                "raw_outcome": measurement.raw_verdict,
                "protocol_location": policy["protocol_location"],
                "raw_row": allowed,
            }
            payload = json.dumps(
                evidence_document, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
            if len(payload) > _MAX_PUBLIC_EVIDENCE_BYTES:
                raise PrivateResultError("evidence_too_large", "allowlisted source row exceeds public evidence size limit")
            payload_hash = _sha256_bytes(payload)
            observation = Observation(
                observation_id=observation_id,
                candidate_id=action.candidate_id,
                assay_id=action.assay_id,
                value=measurement.value,
                unit=measurement.unit,
                comparison=measurement.comparison,
                raw_verdict=measurement.raw_verdict,
                verdict=measurement.verdict,
                evidence_ids=[evidence_id],
                released_at=published_at,
                replicate_id=measurement.replicate_id,
                condition_id=measurement.condition_id,
            )
            observations.append(observation)
            references.append(reference)
            payloads.append((reference, payload, payload_hash))
        return observations, references, payloads

    def _current_public(
        self,
        as_of: datetime,
        runtime_observations: list[Observation],
        evidence: list[EvidenceRef],
    ) -> PublicCampaign:
        try:
            current = self.public.validated_replace(
                as_of=as_of,
                observations=[*self.public.observations, *runtime_observations],
                evidence=evidence,
            )
        except ValidationError as exc:
            raise ExecutionControlError("runtime_public_invalid", "new public object failed contract validation") from exc
        audit = validate_public_campaign(current)
        if not audit.ok:
            raise ExecutionControlError("runtime_public_invalid", "new public object failed reference or time validation")
        return current

    def _validated_release_action(
        self,
        db: sqlite3.Connection,
        execution: sqlite3.Row,
        run_id: str,
    ) -> ActionRequest:
        try:
            action = ActionRequest.model_validate_json(execution["action_json"])
        except ValidationError as exc:
            raise PrivateResultError("action_corrupt", "stored execution action is invalid") from exc
        if (action.candidate_id != execution["candidate_id"] or action.assay_id != execution["assay_id"]
                or action.campaign_id != execution["campaign_id"]):
            raise PrivateResultError("action_identity_mismatch", "stored action does not match execution identity")
        key = _action_key(self.snapshot_id, self.campaign_id, action.candidate_id, action.assay_id)
        approval = db.execute(
            "SELECT * FROM approvals WHERE run_id = ? AND action_key = ?", (run_id, key),
        ).fetchone()
        if approval is None or approval["status"] != "approved":
            raise PrivateResultError("approval_missing", "original execution approval is unavailable")
        try:
            decision = GovernanceDecision.model_validate_json(approval["decision_json"])
        except ValidationError as exc:
            raise PrivateResultError("approval_corrupt", "original execution approval is invalid") from exc
        approved = decision.approved_action
        if (approved is None or approved.action.model_dump_json() != action.model_dump_json()
                or approved.approval_id != approval["approval_id"]
                or approval["action_digest"] != execution["action_digest"]
                or approval["snapshot_id"] != execution["snapshot_id"]
                or approval["cost_amount"] != execution["cost_amount"]
                or approval["unit"] != execution["unit"]
                or approval["cost_policy_version"] != self.cost_policy_version
                or _parse_dt(approval["reviewed_at"]) > _parse_dt(execution["accepted_at"])):
            raise PrivateResultError("approval_mismatch", "stored approval does not match the original execution")
        return action

    def _published_refs(self, db: sqlite3.Connection, run_id: str) -> list[EvidenceRef]:
        rows = db.execute(
            "SELECT reference_json FROM published_evidence WHERE run_id = ? ORDER BY evidence_id", (run_id,),
        ).fetchall()
        try:
            return [EvidenceRef.model_validate_json(row["reference_json"]) for row in rows]
        except ValidationError as exc:
            raise ExecutionControlError("runtime_evidence_corrupt", "stored public evidence reference is invalid") from exc

    def _published_execution(
        self,
        db: sqlite3.Connection,
        run_id: str,
        execution_id: str,
    ) -> PublishedExecution:
        row = db.execute(
            """SELECT p.* FROM published_executions p
               JOIN executions e ON e.execution_id = p.execution_id
               WHERE p.run_id = ? AND p.execution_id = ? AND e.run_id = ? AND e.release_status = 'released'""",
            (run_id, execution_id, run_id),
        ).fetchone()
        if row is None:
            raise ExecutionControlError("public_execution_not_found", "released execution is unavailable")
        try:
            receipt = ExecutionReceipt.model_validate_json(row["receipt_json"])
            result = ExecutionResult.model_validate_json(row["result_json"])
        except ValidationError as exc:
            raise ExecutionControlError("public_execution_corrupt", "stored public execution is invalid") from exc
        return PublishedExecution(
            execution_id=execution_id, receipt=receipt, result=result,
            published_at=_parse_dt(row["published_at"]), state_version=int(row["state_version"]),
            cost=Decimal(row["cost_amount"]), unit=row["unit"],
        )

    def _advance_public_state(self, db: sqlite3.Connection, run_id: str, as_of: datetime) -> None:
        row = db.execute(
            "SELECT state_version, as_of FROM run_public_state WHERE run_id = ?", (run_id,),
        ).fetchone()
        if row is None:
            raise ExecutionControlError("runtime_state_missing", "run public state is unavailable")
        if as_of < _parse_dt(row["as_of"]):
            raise ExecutionControlError("time_order", "execution time predates current public state")
        db.execute(
            "UPDATE run_public_state SET state_version = ?, as_of = ? WHERE run_id = ?",
            (int(row["state_version"]) + 1, _dt_text(as_of), run_id),
        )

    def _validate_action(self, action: ActionRequest) -> None:
        if action.campaign_id != self.campaign_id:
            raise ActionRejectedError("campaign_mismatch", "action campaign differs from fixed run campaign")
        if action.candidate_id not in self._candidates:
            raise ActionRejectedError("unknown_candidate", "action candidate is not in the public campaign")
        assay = self._assays.get(action.assay_id)
        if assay is None:
            raise ActionRejectedError("unknown_assay", "action assay is not in the public campaign")
        if assay.role.value == "primary":
            raise ActionRejectedError("primary_lookup_forbidden", "2-B supports follow-up replay assays only")
        if action.assay_id not in self._supported_assays:
            raise ActionRejectedError("unsupported_assay", "assay is not supported by the loaded replay store")
        if action.parameters:
            raise ActionRejectedError("parameters_unsupported", "fixed-snapshot lookup accepts no free-form parameters")
        self._configured_cost(action.assay_id)

    def _configured_cost(self, assay_id: str) -> tuple[Decimal, str]:
        assay = self._assays.get(assay_id)
        if assay is None:
            raise ActionRejectedError("unknown_assay", "assay is not configured in the public campaign")
        if assay.cost.unit != self.public.campaign.budget.unit:
            raise BudgetError("unit_mismatch", "assay cost and campaign budget use different units")
        return assay.cost.amount, assay.cost.unit

    def _check_current_prerequisites(
        self,
        action: ActionRequest,
        runtime_observations: list[Observation],
    ) -> None:
        observations = [*self.public.observations, *runtime_observations]
        if not self.prerequisites_satisfied(action.candidate_id, action.assay_id, observations):
            raise ActionRejectedError(
                "prerequisite_unmet",
                "required public prerequisite is absent from committed observations in this run",
            )

    def prerequisites_satisfied(
        self, candidate_id: str, assay_id: str, observations: list[Observation],
    ) -> bool:
        """Evaluate configured public prerequisites without consulting Oracle."""
        assay = self._assays.get(assay_id)
        if assay is None:
            return False
        for prerequisite in assay.prerequisites:
            matching = [
                observation for observation in observations
                if observation.candidate_id == candidate_id
                and observation.assay_id == prerequisite.assay_id
            ]
            if prerequisite.kind == "observed" and not matching:
                return False
            if prerequisite.kind == "verdict" and not any(
                observation.verdict == prerequisite.verdict for observation in matching
            ):
                return False
        return True

    def _action_digest(self, run_id: str, action: ActionRequest, amount: Decimal, unit: str) -> str:
        # action_id is a label, not a second billable action. The unique action
        # is fixed by run/snapshot/candidate/assay and the no-parameter policy.
        return _sha256(_canonical_json({
            "run_id": run_id,
            "snapshot_id": self.snapshot_id,
            "campaign_id": self.campaign_id,
            "candidate_id": action.candidate_id,
            "assay_id": action.assay_id,
            "action_kind": _ACTION_KIND,
            "parameters": {},
            "cost_amount": _decimal_text(amount),
            "cost_unit": unit,
            "cost_policy_version": self.cost_policy_version,
        }))

    def _validate_lookup_result(self, result: ReplayLookupResult, action: ActionRequest) -> None:
        if not isinstance(result, ReplayLookupResult):
            raise TypeError("oracle returned an unsupported result object")
        # Reconstruct to rerun the result contract even when a caller injects a
        # custom oracle. This also enforces found/nonempty and no_record/empty.
        ReplayLookupResult(
            status=result.status,
            snapshot_id=result.snapshot_id,
            campaign_id=result.campaign_id,
            candidate_id=result.candidate_id,
            assay_id=result.assay_id,
            measurements=tuple(
                NormalizedMeasurement.model_validate_json(item.model_dump_json())
                for item in result.measurements
            ),
        )
        if (
            result.snapshot_id != self.snapshot_id
            or result.campaign_id != self.campaign_id
            or result.candidate_id != action.candidate_id
            or result.assay_id != action.assay_id
        ):
            raise ExecutionControlError("lookup_identity_mismatch", "Oracle result does not match the approved action")

    def _release_reservation(
        self,
        db: sqlite3.Connection,
        run: sqlite3.Row,
        execution_id: str,
        amount: Decimal,
        unit: str,
        released_at: datetime,
    ) -> None:
        reservation_row = db.execute(
            "SELECT reserved FROM runs WHERE run_id = ?", (run["run_id"],)
        ).fetchone()
        if reservation_row is None:
            raise RunNotFoundError("run_not_found", "run does not exist")
        current = Decimal(reservation_row["reserved"])
        if current < amount:
            raise ExecutionControlError("budget_invariant", "reservation ledger would become negative")
        reserved_after = current - amount
        db.execute("UPDATE runs SET reserved = ? WHERE run_id = ?", (_decimal_text(reserved_after), run["run_id"]))
        self._ledger(db, run["run_id"], execution_id, "release", amount, unit,
                     f"release:{execution_id}", released_at)

    def _ledger(
        self,
        db: sqlite3.Connection,
        run_id: str,
        execution_id: str,
        event: Literal["reserve", "release"],
        amount: Decimal,
        unit: str,
        idempotency_key: str,
        occurred_at: datetime,
    ) -> None:
        db.execute(
            """INSERT INTO budget_ledger (
                ledger_id, run_id, execution_id, event, amount, unit, idempotency_key, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"ledger-{uuid4().hex}", run_id, execution_id, event,
             _decimal_text(amount), unit, idempotency_key, _dt_text(occurred_at)),
        )

    def _run_budget(self, row: sqlite3.Row) -> RunBudget:
        try:
            budget = BudgetState(
                total=Decimal(row["initial_budget"]),
                spent=Decimal(row["spent"]),
                reserved=Decimal(row["reserved"]),
                unit=row["unit"],
            )
        except (ValidationError, ValueError) as exc:
            raise ExecutionControlError("budget_state_corrupt", "stored budget is invalid") from exc
        return RunBudget(
            run_id=row["run_id"], snapshot_id=row["snapshot_id"], campaign_id=row["campaign_id"],
            cost_policy_version=row["cost_policy_version"], budget=budget,
            created_at=_parse_dt(row["created_at"]),
        )

    def _receipt(self, row: sqlite3.Row, request_id: str) -> ExecutionReceiptView:
        status = row["status"]
        if status == "running":
            raise ExecutionControlError("incomplete_transaction", "uncommitted execution state was observed")
        if status == "ready_for_release" and row["release_status"] in ("released", "cancelled"):
            status = row["release_status"]
        return ExecutionReceiptView(
            execution_id=row["execution_id"], request_id=request_id, action_id=_parse_action_id(row["action_json"]),
            status=status,
            reserved=Decimal(row["reserved_after"]), available=Decimal(row["available_after"]),
            unit=row["unit"], requested_at=_parse_dt(row["requested_at"]),
            accepted_at=_parse_dt(row["accepted_at"]), executed_at=_parse_dt(row["executed_at"]),
            error_code=row["error_code"],
        )

    def _load_execution(self, db: sqlite3.Connection, execution_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM executions WHERE execution_id = ?", (execution_id,)).fetchone()
        if row is None:
            raise ExecutionControlError("execution_not_found", "execution does not exist")
        return row

    def _load_run(self, db: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError("run_not_found", "run does not exist")
        return row

    def _assert_run_binding(self, row: sqlite3.Row) -> None:
        expected = (
            self.snapshot_id, self.campaign_id, self._public_digest,
            self.cost_policy_version, _dt_text(self.public.as_of),
        )
        actual = (
            row["snapshot_id"], row["campaign_id"], row["public_sha256"],
            row["cost_policy_version"], row["initial_as_of"],
        )
        if actual != expected:
            raise RunBindingError("run_binding_mismatch", "run is bound to different snapshot, campaign, public data, or policy")

    def _now(self) -> datetime:
        try:
            return TypeAdapter(AwareDatetime).validate_python(self.clock(), strict=True)
        except (ValidationError, TypeError) as exc:
            raise ExecutionControlError("invalid_clock", "clock must return a timezone-aware datetime") from exc

    def _connection(self) -> sqlite3.Connection:
        try:
            db = sqlite3.connect(
                self.database_path,
                timeout=self.busy_timeout_seconds,
                isolation_level=None,
                check_same_thread=False,
            )
            db.row_factory = sqlite3.Row
            db.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_seconds * 1000)}")
            db.execute("PRAGMA foreign_keys = ON")
            return db
        except sqlite3.OperationalError as exc:
            raise ExecutionControlError("database_unavailable", "private runtime database is unavailable") from exc

    def _transaction(self):
        coordinator = self

        class Transaction:
            def __enter__(self):
                self.db = coordinator._connection()
                try:
                    self.db.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    self.db.close()
                    raise ExecutionControlError("database_busy", "private runtime database is busy") from exc
                return self.db

            def __exit__(self, exc_type, exc, traceback):
                try:
                    if exc_type is None:
                        self.db.commit()
                    else:
                        self.db.rollback()
                        if issubclass(exc_type, sqlite3.Error):
                            raise ExecutionControlError(
                                "database_write_failed", "private runtime transaction failed"
                            ) from exc
                except sqlite3.Error as db_exc:
                    self.db.rollback()
                    raise ExecutionControlError("database_write_failed", "private runtime transaction failed") from db_exc
                finally:
                    self.db.close()
                return False

        return Transaction()

    def _ensure_schema(self) -> None:
        db = self._connection()
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, _SCHEMA_VERSION):
                raise ExecutionControlError("unsupported_database_version", "runtime database schema version is unsupported")
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("BEGIN IMMEDIATE")
            if version == 0:
                statements = (
                    """CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    snapshot_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    public_sha256 TEXT NOT NULL,
                    initial_budget TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    spent TEXT NOT NULL,
                    reserved TEXT NOT NULL,
                    cost_policy_version TEXT NOT NULL,
                    initial_as_of TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )""",
                    """CREATE TABLE IF NOT EXISTS approvals (
                    run_id TEXT NOT NULL,
                    action_key TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    action_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('approved','rejected','canceled')),
                    decision_json TEXT NOT NULL,
                    approval_id TEXT NOT NULL UNIQUE,
                    approver_id TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL,
                    canceled_at TEXT,
                    cost_amount TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    cost_policy_version TEXT NOT NULL,
                    PRIMARY KEY(run_id, action_key),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )""",
                    """CREATE TABLE IF NOT EXISTS executions (
                    execution_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    campaign_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    assay_id TEXT NOT NULL,
                    action_kind TEXT NOT NULL,
                    action_digest TEXT NOT NULL,
                    action_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('running','ready_for_release','no_record','failed')),
                    cost_amount TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    executed_at TEXT NOT NULL,
                    reserved_after TEXT NOT NULL,
                    available_after TEXT NOT NULL,
                    error_code TEXT,
                    UNIQUE(run_id, snapshot_id, candidate_id, assay_id, action_kind),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )""",
                    """CREATE TABLE IF NOT EXISTS execution_requests (
                    run_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    PRIMARY KEY(run_id, request_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
                )""",
                    """CREATE TABLE IF NOT EXISTS budget_ledger (
                    ledger_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    event TEXT NOT NULL CHECK(event IN ('reserve','release')),
                    amount TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(run_id, idempotency_key),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
                )""",
                    """CREATE TABLE IF NOT EXISTS private_results (
                    execution_id TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
                )""",
                )
                for statement in statements:
                    db.execute(statement)
            execution_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(executions)").fetchall()
            }
            migrations = (
                ("release_status", "TEXT NOT NULL DEFAULT 'pending' CHECK(release_status IN ('pending','released','cancelled'))"),
                ("released_at", "TEXT"),
                ("cancelled_at", "TEXT"),
                ("cancel_reason", "TEXT"),
                ("prerequisite_state_version", "INTEGER NOT NULL DEFAULT 0"),
            )
            for column, definition in migrations:
                if column not in execution_columns:
                    db.execute(f"ALTER TABLE executions ADD COLUMN {column} {definition}")
            db.execute(
                """CREATE TABLE IF NOT EXISTS run_public_state (
                    run_id TEXT PRIMARY KEY,
                    state_version INTEGER NOT NULL CHECK(state_version >= 0),
                    as_of TEXT NOT NULL,
                    observations_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS published_evidence (
                    run_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    reference_json TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    PRIMARY KEY(run_id, evidence_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS published_executions (
                    run_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    state_version INTEGER NOT NULL,
                    cost_amount TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    PRIMARY KEY(run_id, execution_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS release_settlements (
                    settlement_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    UNIQUE(run_id, execution_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(execution_id) REFERENCES executions(execution_id)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS loop_runs (
                    run_id TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    config_sha256 TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('running','stopped')),
                    stop_reason TEXT,
                    resumable INTEGER NOT NULL CHECK(resumable IN (0,1)),
                    selector_calls INTEGER NOT NULL DEFAULT 0 CHECK(selector_calls >= 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS loop_steps (
                    run_id TEXT NOT NULL,
                    step_no INTEGER NOT NULL CHECK(step_no > 0),
                    action_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    assay_id TEXT NOT NULL,
                    action_json TEXT NOT NULL,
                    view_state_version INTEGER NOT NULL,
                    view_digest TEXT NOT NULL,
                    selected_reason TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'proposed','approved','pending_release','released','no_record',
                        'failed','rejected','cancelled'
                    )),
                    approval_status TEXT CHECK(approval_status IN ('approved','rejected','cancelled')),
                    execution_status TEXT CHECK(execution_status IN (
                        'ready_for_release','released','cancelled','no_record','failed'
                    )),
                    release_status TEXT CHECK(release_status IN (
                        'pending','released','cancelled','not_applicable','failed'
                    )),
                    execution_id TEXT,
                    action_retries INTEGER NOT NULL DEFAULT 0 CHECK(action_retries >= 0),
                    release_retries INTEGER NOT NULL DEFAULT 0 CHECK(release_retries >= 0),
                    observations_added INTEGER NOT NULL DEFAULT 0 CHECK(observations_added >= 0),
                    budget_spent TEXT NOT NULL DEFAULT '0',
                    budget_reserved TEXT NOT NULL DEFAULT '0',
                    budget_available TEXT NOT NULL DEFAULT '0',
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, step_no),
                    UNIQUE(run_id, action_id),
                    UNIQUE(run_id, request_id),
                    FOREIGN KEY(run_id) REFERENCES loop_runs(run_id)
                )"""
            )
            loop_step_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(loop_steps)").fetchall()
            }
            for column, definition in (
                ("approval_status", "TEXT"),
                ("execution_status", "TEXT"),
                ("release_status", "TEXT"),
                ("budget_spent", "TEXT NOT NULL DEFAULT '0'"),
                ("budget_reserved", "TEXT NOT NULL DEFAULT '0'"),
                ("budget_available", "TEXT NOT NULL DEFAULT '0'"),
            ):
                if column not in loop_step_columns:
                    db.execute(f"ALTER TABLE loop_steps ADD COLUMN {column} {definition}")
            db.execute(
                """INSERT OR IGNORE INTO run_public_state (run_id, state_version, as_of, observations_json)
                   SELECT run_id, 0, created_at, '[]' FROM runs"""
            )
            db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            db.commit()
        except ExecutionControlError:
            db.rollback()
            raise
        except sqlite3.Error as exc:
            db.rollback()
            raise ExecutionControlError("database_schema_failed", "private runtime schema could not be initialized") from exc
        finally:
            db.close()
        try:
            os.chmod(self.database_path, 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{self.database_path}{suffix}")
                if sidecar.exists():
                    os.chmod(sidecar, 0o600)
        except OSError as exc:
            raise ExecutionControlError("database_permissions_failed", "private runtime database permissions could not be restricted") from exc


def _copy_action(action: ActionRequest) -> ActionRequest:
    if not isinstance(action, ActionRequest):
        raise ActionRejectedError("invalid_action", "action must be an ActionRequest")
    try:
        return ActionRequest.model_validate_json(action.model_dump_json())
    except ValidationError as exc:
        raise ActionRejectedError("invalid_action", "action is invalid") from exc


def _stable_public_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:32]}"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _observations_json(observations: list[Observation]) -> str:
    return json.dumps(
        [item.model_dump(mode="json") for item in observations],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _load_observations(payload: str) -> list[Observation]:
    try:
        result = TypeAdapter(list[Observation]).validate_json(payload)
    except ValidationError as exc:
        raise ExecutionControlError("runtime_state_corrupt", "stored public observations are invalid") from exc
    if len({item.observation_id for item in result}) != len(result):
        raise ExecutionControlError("runtime_state_corrupt", "stored public observation IDs are duplicated")
    return result


def _json_object(payload: bytes) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ExecutionControlError("public_evidence_corrupt", "public evidence payload is invalid") from exc
    if not isinstance(value, dict):
        raise ExecutionControlError("public_evidence_corrupt", "public evidence payload must be a JSON object")
    return value


def _action_key(snapshot_id: str, campaign_id: str, candidate_id: str, assay_id: str) -> str:
    return _sha256(_canonical_json({
        "snapshot_id": snapshot_id, "campaign_id": campaign_id,
        "candidate_id": candidate_id, "assay_id": assay_id, "action_kind": _ACTION_KIND,
    }))


def _lookup_result_json(result: ReplayLookupResult) -> str:
    payload = {
        "status": result.status,
        "snapshot_id": result.snapshot_id,
        "campaign_id": result.campaign_id,
        "candidate_id": result.candidate_id,
        "assay_id": result.assay_id,
        "measurements": [item.model_dump(mode="json") for item in result.measurements],
    }
    return _canonical_json(payload)


def _load_lookup_result(raw: str) -> ReplayLookupResult:
    try:
        payload = json.loads(raw)
        measurements = tuple(
            NormalizedMeasurement.model_validate(item) for item in payload["measurements"]
        )
        return ReplayLookupResult(
            status=payload["status"], snapshot_id=payload["snapshot_id"],
            campaign_id=payload["campaign_id"], candidate_id=payload["candidate_id"],
            assay_id=payload["assay_id"], measurements=measurements,
        )
    except (KeyError, TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        raise PrivateResultError("private_result_corrupt", "stored replay result failed schema validation") from exc


def _empty_result_json(
    snapshot_id: str,
    campaign_id: str,
    action: ActionRequest,
    status: str,
    error_code: str,
) -> str:
    # Failed lookups have no ReplayLookupResult, so persist a small private
    # diagnostic envelope which is deliberately not readable by the 2-C API.
    return _canonical_json({
        "kind": "lookup_error", "status": status, "error_code": error_code,
        "snapshot_id": snapshot_id, "campaign_id": campaign_id,
        "candidate_id": action.candidate_id, "assay_id": action.assay_id,
    })


def _parse_action_id(action_json: str) -> str:
    try:
        return ActionRequest.model_validate_json(action_json).action_id
    except ValidationError as exc:
        raise ExecutionControlError("execution_corrupt", "stored action failed schema validation") from exc


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError("money value must be a finite nonnegative Decimal")
    return format(value, "f")


def _dt_text(value: datetime) -> str:
    return TypeAdapter(AwareDatetime).validate_python(value, strict=True).isoformat()


def _parse_dt(value: str) -> datetime:
    return TypeAdapter(AwareDatetime).validate_python(value)
