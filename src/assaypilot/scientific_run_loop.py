"""Trusted Stage 5-B scientific selector integrated with the replay controller.

Only public snapshots enter the prompt. API calls happen outside database
transactions; decisions, hypothesis updates, interpretations, and a selected
loop step are committed together after revalidating the exact public state.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import re
import sqlite3
import time
from typing import Callable, Sequence
from uuid import uuid4

from assaypilot.domain import ActionRequest
from assaypilot.execution import ExecutionCoordinator
from assaypilot.llm_provider import LLMProviderError, LLMSettings, OpenAICompatibleChatProvider
from assaypilot.run_loop import (
    ExecutableAction, RunLoopConfig, RunLoopError, SelectorProposal, _canonical_json,
    _dt_text, _jsonable,
)
from assaypilot.scientific_context import (
    CONTEXT_SCHEMA_VERSION, BudgetAndLimits, DecisionContext, PriorHypothesis, PublicAttempt,
    build_context, derive_eligible_actions, plan_shortlist,
)
from assaypilot.scientific_reasoner import (
    PROMPT_VERSION, DecisionValidationError, ReasoningResult, ScientificReasoner,
)


class ScientificSelectionError(RunLoopError):
    """Safe controller error raised when a scientific decision cannot apply."""


class SelectionOutcome:
    def __init__(self, proposal: SelectorProposal | None, *, status: str,
                 decision_id: str | None = None, step_no: int | None = None,
                 stop_reason: str | None = None):
        self.proposal = proposal
        self.status = status
        self.decision_id = decision_id
        self.step_no = step_no
        self.stop_reason = stop_reason


def safe_settings_fingerprint(settings: LLMSettings, *, output_tokens: int,
                               shortlist_size: int, shortlist_seed: int,
                               max_llm_calls: int,
                               interpretation_reserve_calls: int = 2) -> str:
    """Hash safe request settings; never include key or authentication header data."""
    safe = {
        "provider": settings.provider,
        "endpoint": settings.endpoint,
        "model": settings.model,
        "api_mode": settings.resolved_api_mode,
        "response_format": settings.response_format,
        "output_tokens": output_tokens,
        "timeout_seconds": settings.timeout_seconds,
        "prompt_version": PROMPT_VERSION,
        "context_schema_version": CONTEXT_SCHEMA_VERSION,
        "shortlist_size": shortlist_size,
        "shortlist_seed": shortlist_seed,
        "max_llm_calls": max_llm_calls,
        "interpretation_reserve_calls": interpretation_reserve_calls,
        "transport_retries": 0,
        "schema_repair_calls": 1,
    }
    return hashlib.sha256(_canonical_json(safe)).hexdigest()


def config_science_fields(settings: LLMSettings, *, settings_sha256: str,
                          output_tokens: int, shortlist_size: int,
                          shortlist_seed: int, max_llm_calls: int,
                          max_stale_redecisions: int,
                          interpretation_reserve_calls: int = 2) -> dict[str, object]:
    return {
        "science_settings_sha256": settings_sha256,
        "science_provider": settings.provider,
        "science_endpoint": settings.endpoint,
        "science_model": settings.model,
        "science_api_mode": settings.resolved_api_mode,
        "science_response_format": settings.response_format,
        "science_output_tokens": output_tokens,
        "science_timeout_seconds": settings.timeout_seconds,
        "science_prompt_version": PROMPT_VERSION,
        "science_context_schema_version": CONTEXT_SCHEMA_VERSION,
        "science_shortlist_size": shortlist_size,
        "science_shortlist_seed": shortlist_seed,
        "science_max_llm_calls": max_llm_calls,
        "science_interpretation_reserve_calls": interpretation_reserve_calls,
        "science_max_stale_redecisions": max_stale_redecisions,
    }


def _status_digest(statuses: Sequence[object]) -> str:
    values = [
        {"candidate_id": item.candidate_id, "assay_id": item.assay_id,
         "execution_id": item.execution_id, "status": item.status}
        for item in statuses
    ]
    return hashlib.sha256(_canonical_json(values)).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_exception_class(name: str) -> str | None:
    return name if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name) else None


class ScientificReasoningSelector:
    """LLM-backed selector; the controller still owns approval and execution."""

    selector_kind = "scientific_reasoner"
    seed = None
    algorithm_version = None

    def __init__(self, coordinator: ExecutionCoordinator, settings: LLMSettings,
                 *, research_goal: str, output_tokens: int = 1200,
                 shortlist_size: int = 24, shortlist_seed: int = 3,
                 max_llm_calls: int = 24, interpretation_reserve_calls: int = 2,
                 max_stale_redecisions: int = 2,
                 provider_factory: Callable[[LLMSettings], object] | None = None,
                 monotonic: Callable[[], float] = time.monotonic):
        self.coordinator = coordinator
        self.settings = settings
        self.research_goal = research_goal
        self.output_tokens = output_tokens
        self.shortlist_size = shortlist_size
        self.shortlist_seed = shortlist_seed
        self.max_llm_calls = max_llm_calls
        self.interpretation_reserve_calls = interpretation_reserve_calls
        self.max_stale_redecisions = max_stale_redecisions
        self.provider_factory = provider_factory or OpenAICompatibleChatProvider
        self.monotonic = monotonic
        self.seed = None
        self.settings_sha256 = safe_settings_fingerprint(
            settings, output_tokens=output_tokens, shortlist_size=shortlist_size,
            shortlist_seed=shortlist_seed, max_llm_calls=max_llm_calls,
            interpretation_reserve_calls=interpretation_reserve_calls,
        )

    def select(self, _view):
        raise RunLoopError("scientific_controller_required", "scientific decisions require the trusted controller")

    def select_for_loop(
        self, config: RunLoopConfig, public_view, execution_statuses,
        evidence_documents: dict[str, dict[str, object]], eligible_actions,
        steps, *, deadline_at: datetime, deadline_mono: float,
        enumerate_actions: Callable,
    ) -> SelectionOutcome:
        """Build a public context, call the provider, then atomically revalidate/apply."""
        db_path = self.coordinator.database_path
        self._recover_orphaned_requests(db_path, config.run_id)
        pending_ids = self._pending_observation_ids(config.run_id, public_view)
        remaining_calls = self._remaining_calls(db_path, config.run_id)
        if not eligible_actions and not pending_ids:
            return SelectionOutcome(None, status="idle", stop_reason="no_executable_actions")
        can_start_action_decision = bool(eligible_actions) and remaining_calls >= (
            2 + self.interpretation_reserve_calls
        )
        interpretation_only = not can_start_action_decision
        if pending_ids and remaining_calls < self.interpretation_reserve_calls:
            self._mark_incomplete(db_path, config.run_id, pending_ids, "llm_call_limit")
            return SelectionOutcome(None, status="limit", stop_reason="llm_call_limit")
        if eligible_actions and not can_start_action_decision and not pending_ids:
            self._mark_incomplete(db_path, config.run_id, [], "llm_call_limit")
            return SelectionOutcome(None, status="limit", stop_reason="llm_call_limit")
        now = self.coordinator._now()
        remaining_seconds = min(
            max(0.0, (deadline_at - now).total_seconds()),
            max(0.0, deadline_mono - self.monotonic()),
        )
        if remaining_seconds <= 0:
            self._mark_incomplete(db_path, config.run_id, pending_ids, "deadline")
            return SelectionOutcome(None, status="limit", stop_reason="deadline")

        attempts = self._public_attempts(steps, public_view)
        unhandled = set(pending_ids)
        public_observations_by_id = {
            item.observation_id: item for item in public_view.public.observations
        }
        if interpretation_only:
            pending_pairs = {
                (public_observations_by_id[item].candidate_id,
                 public_observations_by_id[item].assay_id)
                for item in unhandled
            }
            attempts = [item for item in attempts
                        if (item.candidate_id, item.assay_id) in pending_pairs]
            prior = [item for item in self._load_hypotheses(db_path, config.run_id)
                     if (item.candidate_id, item.assay_id) in pending_pairs]
            context_observations = [public_observations_by_id[item] for item in sorted(unhandled)]
            context_eligible_actions = ()
        else:
            prior = self._load_hypotheses(db_path, config.run_id)
            context_observations = public_view.public.observations
            context_eligible_actions = eligible_actions
        required_context_candidates = {
            public_observations_by_id[item].candidate_id for item in unhandled
        }
        required_context_candidates.update(item.candidate_id for item in attempts)
        currently_eligible_candidates = {item.candidate_id for item in context_eligible_actions}
        non_shortlist_candidates = required_context_candidates - currently_eligible_candidates
        eligible_candidate_limit = min(
            self.shortlist_size,
            24 - len(non_shortlist_candidates),
        )
        if context_eligible_actions and eligible_candidate_limit < 1:
            raise ScientificSelectionError(
                "context_candidate_limit", "history leaves no room for an eligible candidate in the context",
            )
        plan = plan_shortlist(
            [{
                "candidate_id": item.candidate_id, "assay_id": item.assay_id,
                "cost_amount": item.cost_amount, "cost_unit": item.cost_unit,
            } for item in context_eligible_actions],
            candidate_limit=max(1, eligible_candidate_limit),
            seed=self.shortlist_seed,
            generation_public_state_version=public_view.state_version,
        )
        shortlisted_ids = set(plan.metadata.included_candidate_ids)
        shortlist_actions = [item for item in context_eligible_actions
                            if item.candidate_id in shortlisted_ids]
        budget = public_view.state.budget
        step_remaining = max(0, config.max_steps - len(steps))
        context = build_context(
            public_view.public, evidence_documents,
            [{
                "candidate_id": item.candidate_id, "assay_id": item.assay_id,
                "cost_amount": item.cost_amount, "cost_unit": item.cost_unit,
            } for item in shortlist_actions],
            research_goal=self.research_goal,
            public_state_version=public_view.state_version,
            public_as_of=public_view.as_of,
            budget_and_limits=BudgetAndLimits(
                total=str(budget.total), spent=str(budget.spent), reserved=str(budget.reserved),
                available=str(budget.available), unit=budget.unit, assumed=False,
                max_steps_remaining=step_remaining,
                max_duration_seconds=config.max_duration_seconds,
                remaining_duration_seconds=int(remaining_seconds),
                remaining_llm_calls=remaining_calls,
            ),
            source_run_id=config.run_id,
            public_observations=context_observations,
            public_attempts=attempts,
            prior_hypotheses=prior,
            newly_released_observation_ids=sorted(unhandled),
            shortlist_plan=plan,
            candidate_limit=self.shortlist_size,
            shortlist_seed=self.shortlist_seed,
            decision_mode="interpretation_only" if interpretation_only else "action",
        )
        decision_id = f"science-decision-{uuid4().hex}"
        decision_no = self._persist_pending(db_path, config.run_id, decision_id, context)

        provider = _RecordedDeadlineProvider(
            self, db_path, config, decision_id, deadline_at, deadline_mono,
            remaining_call_cap=self.max_llm_calls,
        )
        reasoner = ScientificReasoner(provider, max_output_tokens=self.output_tokens)
        try:
            result = reasoner.decide(context)
        except DecisionValidationError as exc:
            self._fail_decision(db_path, config.run_id, decision_id, "decision_validation_failed",
                                validation=[{"issues": exc.issues}])
            return SelectionOutcome(None, status="failed", decision_id=decision_id,
                                    stop_reason="decision_validation_failed")
        except (LLMProviderError, RunLoopError) as exc:
            self._fail_decision(db_path, config.run_id, decision_id, getattr(exc, "code", "provider_failed"))
            return SelectionOutcome(None, status="failed", decision_id=decision_id,
                                    stop_reason=getattr(exc, "code", "provider_failed"))
        except Exception:
            self._fail_decision(db_path, config.run_id, decision_id, "reasoning_failed")
            return SelectionOutcome(None, status="failed", decision_id=decision_id,
                                    stop_reason="reasoning_failed")

        fresh_view, fresh_statuses, _fresh_evidence = self.coordinator.get_scientific_loop_snapshot(config.run_id)
        fresh_eligible, _ = enumerate_actions(fresh_view, fresh_statuses)
        selected = result.decision.action
        stale = (
            fresh_view.state_version != public_view.state_version
            or _status_digest(fresh_statuses) != _status_digest(execution_statuses)
            or fresh_view.state.budget != public_view.state.budget
        )
        if selected.kind == "select" and (selected.candidate_id, selected.assay_id) not in {
            (item.candidate_id, item.assay_id) for item in fresh_eligible
        }:
            stale = True
        applied = self._commit_decision(
            db_path, config, decision_no, decision_id, context, result,
            public_state_version=public_view.state_version,
            status_digest=_status_digest(execution_statuses),
            decision_status="stale" if stale else "applied",
            fresh_eligible=fresh_eligible,
            selected_candidate=selected.candidate_id if selected.kind == "select" else None,
            selected_assay=selected.assay_id if selected.kind == "select" else None,
            action_rationale=result.decision.concise_rationale,
            deadline_at=deadline_at, deadline_mono=deadline_mono,
        )
        if applied == "deadline":
            self._mark_incomplete(db_path, config.run_id,
                                  self._pending_observation_ids(config.run_id, fresh_view), "deadline")
            return SelectionOutcome(None, status="limit", decision_id=decision_id,
                                    stop_reason="deadline")
        if stale or not applied:
            stale_count = self._stale_count(db_path, config.run_id)
            if stale_count > self.max_stale_redecisions:
                self._mark_incomplete(db_path, config.run_id, self._pending_observation_ids(
                    config.run_id, self.coordinator.get_public_state(config.run_id)), "stale_redecision_limit")
                return SelectionOutcome(None, status="limit", decision_id=decision_id,
                                        stop_reason="stale_redecision_limit")
            return SelectionOutcome(None, status="stale", decision_id=decision_id)
        if selected.kind == "stop":
            if interpretation_only:
                return SelectionOutcome(
                    SelectorProposal("stop", stop_reason="final_interpretation_complete"),
                    status="finalized", decision_id=decision_id,
                    stop_reason="llm_call_limit" if eligible_actions else None,
                )
            return SelectionOutcome(SelectorProposal("stop", stop_reason="selector_stop"),
                                    status="applied", decision_id=decision_id)
        return SelectionOutcome(
            SelectorProposal("select", selected.candidate_id, selected.assay_id,
                             result.decision.concise_rationale),
            status="applied", decision_id=decision_id,
            step_no=self._last_step_no(db_path, config.run_id, decision_id),
        )

    def finish_run(self, run_id: str, reason: str) -> None:
        view = self.coordinator.get_public_state(run_id)
        pending = self._pending_observation_ids(run_id, view)
        self._mark_incomplete(self.coordinator.database_path, run_id, pending,
                              None if not pending else reason)

    def _db(self, path):
        db = sqlite3.connect(path, timeout=self.coordinator.busy_timeout_seconds, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout = {int(self.coordinator.busy_timeout_seconds * 1000)}")
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _pending_observation_ids(self, run_id, view):
        with self._db(self.coordinator.database_path) as db:
            done = {row[0] for row in db.execute(
                "SELECT observation_id FROM loop_scientific_interpretations WHERE run_id = ?", (run_id,)
            )}
        published = {o.observation_id for item in view.released_executions for o in item.result.observations}
        current = {o.observation_id for o in view.public.observations}
        return sorted((published & current) - done)

    def _load_hypotheses(self, path, run_id):
        with self._db(path) as db:
            rows = db.execute(
                "SELECT hypothesis_json FROM loop_scientific_hypotheses WHERE run_id = ? ORDER BY hypothesis_id",
                (run_id,),
            ).fetchall()
        result = []
        for row in rows:
            stored = json.loads(row[0])
            result.append(PriorHypothesis.model_validate({
                key: stored.get(key) for key in (
                "hypothesis_id", "statement", "interpretation", "candidate_id",
                    "hypothesis_kind", "assay_id", "expected_outcome", "status", "origin",
                )
            }))
        return result

    @staticmethod
    def _public_attempts(steps, public_view):
        by_execution = {item.execution_id: item for item in public_view.released_executions}
        result = []
        for step in steps:
            status = step["status"]
            if status not in {"released", "no_record", "failed", "rejected"}:
                continue
            execution = by_execution.get(step["execution_id"])
            observation_ids = ([item.observation_id for item in execution.result.observations]
                               if status == "released" and execution is not None else [])
            result.append(PublicAttempt(
                step_no=step["step_no"], candidate_id=step["candidate_id"],
                assay_id=step["assay_id"], status=status, observation_ids=observation_ids,
            ))
        return result

    def _remaining_calls(self, path, run_id):
        with self._db(path) as db:
            return self.max_llm_calls - int(db.execute(
                "SELECT COUNT(*) FROM loop_scientific_api_calls WHERE run_id = ?", (run_id,)
            ).fetchone()[0])

    def _persist_pending(self, path, run_id, decision_id, context):
        now = _dt_text(self.coordinator._now())
        with self._db(path) as db:
            db.execute("BEGIN IMMEDIATE")
            number = int(db.execute(
                "SELECT COALESCE(MAX(decision_no), 0) + 1 FROM loop_scientific_decisions WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0])
            db.execute(
                """INSERT INTO loop_scientific_decisions
                   (run_id,decision_no,decision_id,state_version,context_digest,context_json,status,
                    result_json,validation_json,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,'pending',NULL,'[]',?,?)""",
                (run_id, number, decision_id, context.public_state_version,
                 context.context_digest, context.model_dump_json(), now, now),
            )
            db.commit()
        return number

    def _fail_decision(self, path, run_id, decision_id, code, validation=None):
        with self._db(path) as db:
            db.execute(
                "UPDATE loop_scientific_decisions SET status='failed',error_code=?,validation_json=?,updated_at=? "
                "WHERE run_id=? AND decision_id=? AND status='pending'",
                (code[:80], json.dumps(validation or [], separators=(",", ":")),
                 _dt_text(self.coordinator._now()), run_id, decision_id),
            )

    def _commit_decision(self, path, config, decision_no, decision_id, context, result,
                         *, public_state_version, status_digest, decision_status,
                         fresh_eligible, selected_candidate, selected_assay, action_rationale,
                         deadline_at, deadline_mono):
        now = self.coordinator._now()
        db = self._db(path)
        try:
            db.execute("BEGIN IMMEDIATE")
            if now >= deadline_at or self.monotonic() >= deadline_mono:
                db.execute(
                    "UPDATE loop_scientific_decisions SET status='failed',result_json=?,error_code='deadline',updated_at=? "
                    "WHERE run_id=? AND decision_no=? AND decision_id=? AND status='pending'",
                    (result.model_dump_json(), _dt_text(now), config.run_id, decision_no, decision_id),
                )
                db.commit()
                return "deadline"
            state = db.execute(
                "SELECT state_version FROM run_public_state WHERE run_id=?", (config.run_id,),
            ).fetchone()
            run_budget = db.execute(
                "SELECT initial_budget,unit,spent,reserved FROM runs WHERE run_id=?", (config.run_id,),
            ).fetchone()
            statuses = db.execute(
                "SELECT candidate_id,assay_id,execution_id,status,release_status FROM executions "
                "WHERE run_id=? ORDER BY execution_id", (config.run_id,),
            ).fetchall()
            status_values = [
                {"candidate_id": row["candidate_id"], "assay_id": row["assay_id"],
                 "execution_id": row["execution_id"],
                 "status": ("released" if row["release_status"] == "released" else
                            "cancelled" if row["release_status"] == "cancelled" else row["status"])}
                for row in statuses
            ]
            runtime_status_digest = hashlib.sha256(_canonical_json(status_values)).hexdigest()
            budget_matches = run_budget is not None and (
                Decimal(run_budget["initial_budget"]) == Decimal(context.budget_and_limits.total)
                and Decimal(run_budget["spent"]) == Decimal(context.budget_and_limits.spent)
                and Decimal(run_budget["reserved"]) == Decimal(context.budget_and_limits.reserved)
                and run_budget["unit"] == context.budget_and_limits.unit
            )
            loop = db.execute("SELECT status FROM loop_runs WHERE run_id=?", (config.run_id,)).fetchone()
            is_stale = (
                state is None or int(state["state_version"]) != public_state_version
                or runtime_status_digest != status_digest or not budget_matches
                or loop is None or loop["status"] != "running"
            )
            if selected_candidate is not None and (selected_candidate, selected_assay) not in {
                (item.candidate_id, item.assay_id) for item in fresh_eligible
            }:
                is_stale = True
            final_status = "stale" if is_stale else decision_status
            result_json = result.model_dump_json()
            db.execute(
                "UPDATE loop_scientific_decisions SET status=?,result_json=?,validation_json=?,"
                "selected_candidate_id=?,selected_assay_id=?,updated_at=? WHERE run_id=? AND decision_no=? "
                "AND decision_id=? AND status='pending'",
                (final_status, result_json,
                 json.dumps([x.model_dump(mode="json") for x in result.validation_history], separators=(",", ":")),
                 selected_candidate if not is_stale else None,
                 selected_assay if not is_stale else None,
                 _dt_text(now), config.run_id, decision_no, decision_id),
            )
            if is_stale:
                db.execute(
                    "UPDATE loop_scientific_run_state SET updated_at=? WHERE run_id=?",
                    (_dt_text(now), config.run_id),
                )
                db.commit()
                return False

            decision = result.decision
            for update in decision.prior_updates:
                row = db.execute(
                    "SELECT hypothesis_json FROM loop_scientific_hypotheses WHERE run_id=? AND hypothesis_id=?",
                    (config.run_id, update.hypothesis_id),
                ).fetchone()
                if row is None:
                    raise ScientificSelectionError("prior_update_conflict", "hypothesis disappeared before commit")
                stored = json.loads(row["hypothesis_json"])
                if stored.get("status") != update.previous_status:
                    raise ScientificSelectionError("prior_update_conflict", "hypothesis status changed before commit")
                stored["status"] = update.new_status
                stored["interpretation"] = update.rationale
                stored["last_updated_observation_ids"] = update.observation_refs
                stored["last_updated_evidence_refs"] = update.evidence_refs
                db.execute(
                    "UPDATE loop_scientific_hypotheses SET hypothesis_json=?,updated_decision_id=?,updated_at=? "
                    "WHERE run_id=? AND hypothesis_id=?",
                    (_canonical_json(stored).decode(), decision_id, _dt_text(now), config.run_id,
                     update.hypothesis_id),
                )
                event_no = int(db.execute(
                    "SELECT COALESCE(MAX(event_no),0)+1 FROM loop_scientific_hypothesis_events WHERE run_id=?",
                    (config.run_id,),
                ).fetchone()[0])
                db.execute(
                    """INSERT INTO loop_scientific_hypothesis_events
                       (run_id,event_no,decision_id,hypothesis_id,event_type,previous_status,new_status,event_json,created_at)
                       VALUES (?,?,?,?,'updated',?,?,?,?)""",
                    (config.run_id, event_no, decision_id, update.hypothesis_id,
                     update.previous_status, update.new_status,
                     _canonical_json(update.model_dump(mode="json")).decode(), _dt_text(now)),
                )
            for proposal in decision.hypotheses:
                data = proposal.model_dump(mode="json")
                data["origin"] = "previous_llm_decision"
                data["interpretation"] = proposal.statement
                data["last_updated_observation_ids"] = []
                data["last_updated_evidence_refs"] = proposal.evidence_refs
                existing = db.execute(
                    "SELECT 1 FROM loop_scientific_hypotheses WHERE run_id=? AND hypothesis_id=?",
                    (config.run_id, proposal.hypothesis_id),
                ).fetchone()
                if existing:
                    raise ScientificSelectionError("hypothesis_id_conflict", "hypothesis ID already exists")
                db.execute(
                    "INSERT INTO loop_scientific_hypotheses VALUES (?,?,?,?,?)",
                    (config.run_id, proposal.hypothesis_id, _canonical_json(data).decode(),
                     decision_id, _dt_text(now)),
                )
                event_no = int(db.execute(
                    "SELECT COALESCE(MAX(event_no),0)+1 FROM loop_scientific_hypothesis_events WHERE run_id=?",
                    (config.run_id,),
                ).fetchone()[0])
                db.execute(
                    """INSERT INTO loop_scientific_hypothesis_events
                       (run_id,event_no,decision_id,hypothesis_id,event_type,previous_status,new_status,event_json,created_at)
                       VALUES (?,?,?,?,'proposed',NULL,?,?,?)""",
                    (config.run_id, event_no, decision_id, proposal.hypothesis_id,
                     proposal.status, _canonical_json(data).decode(), _dt_text(now)),
                )
            for interpretation in decision.interpretations:
                if interpretation.outcome == "no_record":
                    continue
                obs = db.execute(
                    "SELECT 1 FROM loop_scientific_interpretations WHERE run_id=? AND observation_id=?",
                    (config.run_id, interpretation.observation_id),
                ).fetchone()
                if obs:
                    continue
                db.execute(
                    "INSERT INTO loop_scientific_interpretations VALUES (?,?,?,?,?)",
                    (config.run_id, interpretation.observation_id, decision_id,
                     _canonical_json(interpretation.model_dump(mode="json")).decode(), _dt_text(now)),
                )

            pending_ids = sorted(set(context.newly_released_observation_ids) - {
                row[0] for row in db.execute(
                    "SELECT observation_id FROM loop_scientific_interpretations WHERE run_id=?", (config.run_id,)
                )
            })
            db.execute(
                """INSERT INTO loop_scientific_run_state
                   (run_id,interpretation_complete,pending_observation_ids_json,incomplete_reason,updated_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
                   interpretation_complete=excluded.interpretation_complete,
                   pending_observation_ids_json=excluded.pending_observation_ids_json,
                   incomplete_reason=excluded.incomplete_reason,updated_at=excluded.updated_at""",
                (config.run_id, int(not pending_ids), json.dumps(pending_ids),
                 "uninterpreted_observations" if pending_ids else None, _dt_text(now)),
            )
            if selected_candidate is not None:
                step_no = int(db.execute(
                    "SELECT COALESCE(MAX(step_no),0)+1 FROM loop_steps WHERE run_id=?", (config.run_id,),
                ).fetchone()[0])
                action_id = f"loop-action-{uuid4().hex}"
                request_id = f"loop-request-{uuid4().hex}"
                action = ActionRequest(
                    action_id=action_id, campaign_id=self.coordinator.campaign_id,
                    candidate_id=selected_candidate, assay_id=selected_assay,
                )
                if not any((item.candidate_id, item.assay_id) == (selected_candidate, selected_assay)
                           for item in fresh_eligible):
                    raise ScientificSelectionError("action_no_longer_eligible", "selected action is no longer eligible")
                budget = db.execute(
                    "SELECT initial_budget,spent,reserved FROM runs WHERE run_id=?", (config.run_id,),
                ).fetchone()
                available = Decimal(budget["initial_budget"]) - Decimal(budget["spent"]) - Decimal(budget["reserved"])
                db.execute(
                    """INSERT INTO loop_steps
                       (run_id,step_no,action_id,request_id,candidate_id,assay_id,action_json,
                        view_state_version,view_digest,selected_reason,scientific_decision_id,status,
                        execution_id,budget_spent,budget_reserved,budget_available,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,'proposed',NULL,?,?,?,?,?)""",
                    (config.run_id, step_no, action_id, request_id, selected_candidate, selected_assay,
                     action.model_dump_json(), context.public_state_version, context.context_digest,
                     action_rationale[:1200], decision_id, budget["spent"], budget["reserved"],
                     str(available), _dt_text(now), _dt_text(now)),
                )
                db.execute(
                    "UPDATE loop_scientific_decisions SET action_step_no=? WHERE run_id=? AND decision_id=?",
                    (step_no, config.run_id, decision_id),
                )
            db.commit()
            return True
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _last_step_no(self, path, run_id, decision_id):
        with self._db(path) as db:
            row = db.execute(
                "SELECT action_step_no FROM loop_scientific_decisions WHERE run_id=? AND decision_id=?",
                (run_id, decision_id),
            ).fetchone()
        return row[0] if row else None

    def _stale_count(self, path, run_id):
        with self._db(path) as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM loop_scientific_decisions WHERE run_id=? AND status='stale'", (run_id,),
            ).fetchone()[0])

    def _mark_incomplete(self, path, run_id, pending_ids, reason):
        with self._db(path) as db:
            db.execute(
                """INSERT INTO loop_scientific_run_state
                   (run_id,interpretation_complete,pending_observation_ids_json,incomplete_reason,updated_at)
                   VALUES (?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
                   interpretation_complete=excluded.interpretation_complete,
                   pending_observation_ids_json=excluded.pending_observation_ids_json,
                   incomplete_reason=excluded.incomplete_reason,updated_at=excluded.updated_at""",
                (run_id, int(not pending_ids), json.dumps(pending_ids), reason,
                 _dt_text(self.coordinator._now())),
            )

    def _recover_orphaned_requests(self, path, run_id):
        """Do not repeat an API request whose response may have been lost on crash."""
        now = _dt_text(self.coordinator._now())
        with self._db(path) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE loop_scientific_api_calls SET status='failed',error_code='interrupted_unknown',completed_at=? "
                "WHERE run_id=? AND status='started'", (now, run_id),
            )
            db.execute(
                "UPDATE loop_scientific_decisions SET status='failed',error_code='interrupted_unknown',updated_at=? "
                "WHERE run_id=? AND status='pending'", (now, run_id),
            )
            db.commit()


class _RecordedDeadlineProvider:
    """One provider request per recorded call; transport retry is disabled."""

    def __init__(self, selector, db_path, config, decision_id, deadline_at, deadline_mono,
                 *, remaining_call_cap):
        self.selector = selector
        self.db_path = db_path
        self.config = config
        self.decision_id = decision_id
        self.deadline_at = deadline_at
        self.deadline_mono = deadline_mono
        self.remaining_call_cap = remaining_call_cap

    def complete(self, messages, *, max_output_tokens=None, json_schema=None):
        now_dt = self.selector.coordinator._now()
        remaining = min((self.deadline_at - now_dt).total_seconds(),
                        self.deadline_mono - self.selector.monotonic())
        if remaining <= 0:
            raise RunLoopError("deadline", "scientific decision deadline expired")
        with self.selector._db(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            used = int(db.execute(
                "SELECT COUNT(*) FROM loop_scientific_api_calls WHERE run_id=?", (self.config.run_id,),
            ).fetchone()[0])
            if used >= self.remaining_call_cap:
                db.rollback()
                raise RunLoopError("llm_call_limit", "scientific API request limit reached")
            call_no = used + 1
            db.execute(
                """INSERT INTO loop_scientific_api_calls
                   (run_id,call_no,decision_id,status,provider,model,transport_attempts,usage_json,started_at)
                   VALUES (?,?,?,'started',?,?,1,'{}',?)""",
                (self.config.run_id, call_no, self.decision_id,
                 self.selector.settings.provider, self.selector.settings.model,
                 _dt_text(now_dt)),
            )
            db.commit()
        started = time.monotonic()
        failure_stage = "client_creation"
        try:
            bounded_settings = replace(
                self.selector.settings,
                timeout_seconds=min(self.selector.settings.timeout_seconds, remaining),
                retry_attempts=0,
            )
            provider = self.selector.provider_factory(bounded_settings)
            failure_stage = "provider_call"
            response = provider.complete(
                messages, max_output_tokens=max_output_tokens, json_schema=json_schema,
            )
        except Exception as exc:
            raw_code = getattr(exc, "code", "provider_failed")
            code = raw_code if isinstance(raw_code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", raw_code) else "provider_failed"
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if isinstance(exc, LLMProviderError):
                diagnostic = exc.diagnostic_metadata(
                    fallback_stage=failure_stage, fallback_latency_ms=elapsed_ms,
                )
            else:
                nested = exc.__cause__ or exc.__context__
                diagnostic = {
                    "failure_stage": failure_stage,
                    "exception_class": _safe_exception_class(type(exc).__name__),
                    "cause_code": code,
                    "cause_class": _safe_exception_class(type(nested).__name__) if nested else None,
                    "http_response_received": getattr(exc, "status_code", None) is not None,
                    "http_status": getattr(exc, "status_code", None),
                    "latency_ms": elapsed_ms,
                }
            with self.selector._db(self.db_path) as db:
                db.execute(
                    "UPDATE loop_scientific_api_calls SET status='failed',error_code=?,latency_ms=?,"
                    "diagnostic_json=?,completed_at=? "
                    "WHERE run_id=? AND call_no=?",
                    (code, elapsed_ms, json.dumps(diagnostic, separators=(",", ":")),
                     _dt_text(self.selector.coordinator._now()), self.config.run_id, call_no),
                )
            raise
        latency = int((time.monotonic() - started) * 1000)
        usage = {str(key): value for key, value in response.usage.items()
                 if isinstance(value, (int, str)) and len(str(value)) <= 80}
        with self.selector._db(self.db_path) as db:
            db.execute(
                "UPDATE loop_scientific_api_calls SET status='completed',request_id=?,latency_ms=?,usage_json=?,completed_at=? "
                "WHERE run_id=? AND call_no=?",
                (response.request_id, latency, json.dumps(usage, separators=(",", ":")),
                 _dt_text(self.selector.coordinator._now()), self.config.run_id, call_no),
            )
        return replace(response, attempts=1, latency_ms=latency)
