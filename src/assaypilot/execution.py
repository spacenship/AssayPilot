"""Trusted Stage 2-B execution control for fixed-snapshot replay lookups.

The coordinator is an internal Python boundary.  It records explicit approval,
reserves configured Decimal costs, calls an already-loaded ReplayOracle, and
stores the private result in SQLite.  It does not publish observations or
finalize charges.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
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
    GovernanceDecision,
    PublicCampaign,
    Verdict,
    validate_public_campaign,
)
from assaypilot.replay import ReplayError, ReplayLookupResult, ReplayOracle


_SCHEMA_VERSION = 1
_ACTION_KIND = "followup_replay_lookup"
_MONEY_ZERO = Decimal("0")


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
    """Small execution receipt; it contains no measurements or verdicts."""

    execution_id: str
    request_id: str
    action_id: str
    status: Literal["ready_for_release", "no_record", "failed"]
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
            if reviewed_at < _parse_dt(run["created_at"]) or reviewed_at < self.public.as_of:
                raise ExecutionControlError("time_before_run", "approval time predates run or public snapshot")
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
            if reviewed_at < _parse_dt(run["created_at"]) or reviewed_at < self.public.as_of:
                raise ExecutionControlError("time_before_run", "decision time predates run or public snapshot")
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
            if canceled_at < _parse_dt(run["created_at"]) or canceled_at < self.public.as_of:
                raise ExecutionControlError("time_before_run", "cancellation time predates run or snapshot")
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
            self._check_initial_prerequisites(action)

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
                    requested_at, accepted_at, executed_at, reserved_after, available_after, error_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    execution_id, run_id, self.snapshot_id, self.campaign_id,
                    action.candidate_id, action.assay_id, _ACTION_KIND, digest,
                    action.model_dump_json(), _decimal_text(amount), unit,
                    _dt_text(requested_at), _dt_text(accepted_at), _dt_text(requested_at),
                    _decimal_text(reserved + amount), _decimal_text(available - amount),
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
            if execution["status"] != "ready_for_release":
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

    def _check_initial_prerequisites(self, action: ActionRequest) -> None:
        assay = self._assays[action.assay_id]
        for prerequisite in assay.prerequisites:
            matching = [
                observation for observation in self.public.observations
                if observation.candidate_id == action.candidate_id
                and observation.assay_id == prerequisite.assay_id
            ]
            if prerequisite.kind == "observed" and not matching:
                raise ActionRejectedError("prerequisite_unmet", "required assay has no initial public observation")
            if prerequisite.kind == "verdict" and not any(
                observation.verdict == prerequisite.verdict for observation in matching
            ):
                raise ActionRejectedError("prerequisite_unmet", "required verdict is absent from initial public observations")

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
            if version not in (0, _SCHEMA_VERSION):
                raise ExecutionControlError("unsupported_database_version", "runtime database schema version is unsupported")
            db.execute("PRAGMA journal_mode = WAL")
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
            db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except sqlite3.Error as exc:
            raise ExecutionControlError("database_schema_failed", "private runtime schema could not be initialized") from exc
        finally:
            db.close()
        try:
            os.chmod(self.database_path, 0o600)
        except OSError as exc:
            raise ExecutionControlError("database_permissions_failed", "private runtime database permissions could not be restricted") from exc


def _copy_action(action: ActionRequest) -> ActionRequest:
    if not isinstance(action, ActionRequest):
        raise ActionRejectedError("invalid_action", "action must be an ActionRequest")
    try:
        return ActionRequest.model_validate_json(action.model_dump_json())
    except ValidationError as exc:
        raise ActionRejectedError("invalid_action", "action is invalid") from exc


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
