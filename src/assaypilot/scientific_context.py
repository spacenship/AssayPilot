"""Bounded, public-only context construction for Stage 5-A reasoning."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from datetime import datetime
from typing import Any, Literal, Mapping, Sequence

from pydantic import Field, model_validator

from assaypilot.domain import PublicCampaign
from assaypilot.domain.records import Observation as PublicObservation
from assaypilot.domain.common import Contract, ID, Verdict


CONTEXT_SCHEMA_VERSION = "assaypilot.decision-context.v2"
SHORTLIST_RULE = "sha256_seed_candidate_assay_v1"
MAX_CANDIDATES = 32
MAX_OBSERVATIONS = 128
MAX_EVIDENCE_ITEMS = 64
MAX_CONTEXT_BYTES = 250_000
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PUBLIC_ROW_KEYS = ("AID", "SID", "CID", "Activity Outcome")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


class CandidateContext(Contract):
    candidate_id: ID
    source: ID
    source_id: ID
    source_cid: str | None
    original_smiles: str | None
    observation_ids: list[ID] = Field(max_length=MAX_OBSERVATIONS)


class AssayContext(Contract):
    assay_id: ID
    name: str = Field(max_length=500)
    role: ID
    endpoint: str = Field(max_length=300)
    unit: str = Field(max_length=100)
    verdict_meaning: dict[str, str]
    endpoint_scope: str | None = Field(default=None, max_length=100)
    endpoint_meaning: str | None = Field(default=None, max_length=1000)
    official_result_names: list[str] = Field(default_factory=list, max_length=24)
    protocol_locations: list[str] = Field(default_factory=list, max_length=8)
    experimental_conditions_available: bool


class ObservationContext(Contract):
    observation_id: ID
    candidate_id: ID
    assay_id: ID
    value: float | None
    unit: str | None
    comparison: str | None
    raw_verdict: str | None
    verdict: Literal["active", "inactive", "inconclusive", "unspecified"]
    evidence_refs: list[ID]
    released_at: str
    replicate_id: str
    condition_id: str


def observation_context_from_public(observation: PublicObservation | ObservationContext | dict[str, Any]) -> ObservationContext:
    """Adapt the public Observation contract without changing its values."""
    if isinstance(observation, ObservationContext):
        return observation
    if isinstance(observation, PublicObservation):
        data = observation.model_dump(mode="json")
    elif isinstance(observation, dict):
        data = dict(observation)
    else:
        raise TypeError("expected a public Observation DTO")
    if "evidence_refs" not in data and "evidence_ids" in data:
        data["evidence_refs"] = data.pop("evidence_ids")
    return ObservationContext.model_validate(data)


class PublicAttempt(Contract):
    step_no: int = Field(ge=1)
    candidate_id: ID
    assay_id: ID
    status: Literal["released", "no_record", "failed", "rejected"]
    observation_ids: list[ID] = Field(default_factory=list, max_length=16)


class EligibleAction(Contract):
    candidate_id: ID
    assay_id: ID
    cost_amount: str = Field(max_length=40)
    cost_unit: ID


class StateReference(Contract):
    state_ref: ID
    kind: Literal["public_state", "budget", "eligibility", "attempt"]
    candidate_id: str | None = None
    assay_id: str | None = None
    step_no: int | None = Field(default=None, ge=1)
    value: str = Field(max_length=240)


class EvidenceCatalogItem(Contract):
    evidence_id: ID
    source_kind: ID
    source_id: ID
    assay_ids: list[ID] = Field(default_factory=list, max_length=16)
    candidate_ids: list[ID] = Field(default_factory=list, max_length=MAX_CANDIDATES)
    observation_ids: list[ID] = Field(default_factory=list, max_length=MAX_OBSERVATIONS)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    public_content: dict[str, Any]


class PriorHypothesis(Contract):
    hypothesis_id: ID
    hypothesis_kind: Literal["assay_activity", "data_availability"]
    statement: str = Field(min_length=1, max_length=600)
    expected_outcome: Literal["active", "inactive"] | None = None
    interpretation: str | None = Field(default=None, max_length=500)
    candidate_id: str | None = None
    assay_id: ID
    status: Literal["proposed", "supported", "weakened", "unresolved"]
    origin: Literal["previous_llm_decision"] = "previous_llm_decision"


class ShortlistMetadata(Contract):
    rule: Literal["sha256_seed_candidate_assay_v1"]
    seed: int = Field(ge=0, le=2**31 - 1)
    # These first two counts describe the full action set used when the
    # deterministic shortlist was generated. They are not counts of the
    # smaller context sent to the reasoner.
    eligible_action_count: int = Field(ge=0)
    eligible_candidate_count: int = Field(ge=0)
    generation_public_state_version: int = Field(default=0, ge=0)
    context_candidate_count: int = Field(default=0, ge=0, le=MAX_CANDIDATES)
    current_eligible_action_count: int = Field(default=0, ge=0, le=MAX_CANDIDATES * 4)
    current_eligible_candidate_count: int = Field(default=0, ge=0, le=MAX_CANDIDATES)
    eligible_actions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    included_candidate_ids: list[ID] = Field(max_length=MAX_CANDIDATES)
    shortlist_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BudgetAndLimits(Contract):
    total: str = Field(max_length=40)
    spent: str = Field(max_length=40)
    reserved: str = Field(max_length=40)
    available: str = Field(max_length=40)
    unit: ID
    assumed: bool
    max_steps_remaining: int | None = Field(default=None, ge=0, le=10_000)
    max_duration_seconds: int | None = Field(default=None, ge=1, le=86_400)
    remaining_duration_seconds: int | None = Field(default=None, ge=0, le=86_400)
    remaining_llm_calls: int | None = Field(default=None, ge=0, le=10_000)

    @model_validator(mode="after")
    def balances_are_consistent(self) -> "BudgetAndLimits":
        try:
            total, spent, reserved, available = map(
                Decimal, (self.total, self.spent, self.reserved, self.available),
            )
        except InvalidOperation as exc:
            raise ValueError("budget values must be decimal strings") from exc
        if (not all(x.is_finite() and x >= 0 for x in (total, spent, reserved, available))
                or spent + reserved > total or total - spent - reserved != available):
            raise ValueError("budget values are inconsistent")
        return self


class DecisionContext(Contract):
    schema_version: Literal["assaypilot.decision-context.v2"]
    decision_mode: Literal["action", "interpretation_only"] = "action"
    campaign_id: ID
    source_run_id: str | None
    public_state_version: int = Field(ge=0)
    public_as_of: str
    context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    research_goal: str = Field(min_length=1, max_length=1200)
    assay_context: list[AssayContext] = Field(min_length=1, max_length=16)
    candidate_contexts: list[CandidateContext] = Field(max_length=MAX_CANDIDATES)
    public_observations: list[ObservationContext] = Field(max_length=MAX_OBSERVATIONS)
    evidence_catalog: list[EvidenceCatalogItem] = Field(max_length=MAX_EVIDENCE_ITEMS)
    eligible_actions: list[EligibleAction] = Field(max_length=MAX_CANDIDATES * 4)
    budget_and_limits: BudgetAndLimits
    prior_hypotheses: list[PriorHypothesis] = Field(max_length=16)
    newly_released_observation_ids: list[ID] = Field(max_length=MAX_OBSERVATIONS)
    public_attempts: list[PublicAttempt] = Field(max_length=MAX_OBSERVATIONS)
    shortlist: ShortlistMetadata
    information_gaps: list[str] = Field(max_length=32)
    state_refs: list[StateReference] = Field(max_length=MAX_OBSERVATIONS + 8)

    @model_validator(mode="after")
    def context_links_are_valid(self) -> "DecisionContext":
        try:
            as_of = datetime.fromisoformat(self.public_as_of.replace("Z", "+00:00"))
            if as_of.tzinfo is None or as_of.utcoffset() is None:
                raise ValueError
            for observation in self.public_observations:
                released_at = datetime.fromisoformat(observation.released_at.replace("Z", "+00:00"))
                if released_at.tzinfo is None or released_at.utcoffset() is None or released_at > as_of:
                    raise ValueError
        except ValueError as exc:
            raise ValueError("public observation is invalid or later than public_as_of") from exc
        candidate_ids = {item.candidate_id for item in self.candidate_contexts}
        assay_ids = {item.assay_id for item in self.assay_context}
        evidence = {item.evidence_id: item for item in self.evidence_catalog}
        observations = {item.observation_id: item for item in self.public_observations}
        if len(candidate_ids) != len(self.candidate_contexts):
            raise ValueError("duplicate candidate context")
        if len(assay_ids) != len(self.assay_context):
            raise ValueError("duplicate assay context")
        if len(evidence) != len(self.evidence_catalog):
            raise ValueError("duplicate evidence id")
        if len(observations) != len(self.public_observations):
            raise ValueError("duplicate observation id")
        if len({item.observation_id for item in self.public_observations}) != len(observations):
            raise ValueError("duplicate observation")
        for observation in self.public_observations:
            if observation.candidate_id not in candidate_ids or observation.assay_id not in assay_ids:
                raise ValueError("observation outside shortlisted campaign context")
            for evidence_id in observation.evidence_refs:
                item = evidence.get(evidence_id)
                if (item is None or observation.observation_id not in item.observation_ids
                        or observation.candidate_id not in item.candidate_ids
                        or observation.assay_id not in item.assay_ids):
                    raise ValueError("observation evidence link mismatch")
        if any(item not in observations for item in self.newly_released_observation_ids):
            raise ValueError("new observation is absent from public observations")
        if any(item.candidate_id not in candidate_ids or not item.original_smiles for item in self.candidate_contexts):
            raise ValueError("shortlisted public candidate is unavailable or lacks SMILES")
        if any(item.candidate_id not in candidate_ids or item.assay_id not in assay_ids
               for item in self.eligible_actions):
            raise ValueError("eligible action outside context")
        if self.decision_mode == "interpretation_only" and self.eligible_actions:
            raise ValueError("interpretation-only context cannot contain executable actions")
        if any(item.candidate_id not in set(self.shortlist.included_candidate_ids)
               for item in self.eligible_actions):
            raise ValueError("eligible action candidate outside recorded shortlist")
        for candidate in self.candidate_contexts:
            actual_ids = {o.observation_id for o in self.public_observations
                          if o.candidate_id == candidate.candidate_id}
            if set(candidate.observation_ids) != actual_ids:
                raise ValueError("candidate observation index mismatch")
        if any(item.assay_id not in assay_ids or
               (item.candidate_id is not None and item.candidate_id not in candidate_ids)
               for item in self.prior_hypotheses):
            raise ValueError("prior hypothesis outside context")
        if len({(a.candidate_id, a.assay_id) for a in self.eligible_actions}) != len(self.eligible_actions):
            raise ValueError("duplicate eligible action")
        state_ref_ids = [item.state_ref for item in self.state_refs]
        if len(state_ref_ids) != len(set(state_ref_ids)):
            raise ValueError("duplicate state reference")
        encoded = self.model_dump(mode="json", exclude={"context_digest"})
        if sha256_json(encoded) != self.context_digest:
            raise ValueError("context digest mismatch")
        if len(canonical_json(self.model_dump(mode="json"))) > MAX_CONTEXT_BYTES:
            raise ValueError("decision context exceeds input byte limit")
        return self


class ShortlistPlan(Contract):
    """A reproducible, immutable candidate sample reused across paired contexts."""
    metadata: ShortlistMetadata


def derive_eligible_actions(
    public_campaign: PublicCampaign,
    public_observations: Sequence[PublicObservation | ObservationContext | dict[str, Any]],
    *,
    assay_id: str,
    attempted_pairs: Sequence[tuple[str, str]] = (),
) -> list[EligibleAction]:
    """Derive executable pairs from public observations, assay prerequisites and attempts only."""
    assay_map = {assay.assay_id: assay for assay in public_campaign.assays}
    assay = assay_map.get(assay_id)
    if assay is None:
        raise ValueError("unknown assay_id")
    observations = [observation_context_from_public(item) for item in public_observations]
    attempted = set(attempted_pairs)
    result: list[EligibleAction] = []
    for candidate in public_campaign.candidates:
        candidate_observations = [o for o in observations if o.candidate_id == candidate.candidate_id]
        eligible = True
        for prerequisite in assay.prerequisites:
            matching = [o for o in candidate_observations if o.assay_id == prerequisite.assay_id]
            if prerequisite.kind == "observed":
                eligible = bool(matching)
            else:
                eligible = any(o.verdict == prerequisite.verdict.value for o in matching)
            if not eligible:
                break
        pair = (candidate.candidate_id, assay_id)
        if eligible and pair not in attempted:
            result.append(EligibleAction(
                candidate_id=candidate.candidate_id, assay_id=assay_id,
                cost_amount=str(assay.cost.amount), cost_unit=assay.cost.unit,
            ))
    return sorted(result, key=lambda action: (action.candidate_id, action.assay_id))


def plan_shortlist(
    eligible_actions: Sequence[EligibleAction | dict[str, Any]],
    *,
    candidate_limit: int = 24,
    seed: int = 3,
    anchor_candidate_ids: Sequence[str] = (),
    generation_public_state_version: int = 0,
) -> ShortlistPlan:
    if isinstance(candidate_limit, bool) or not isinstance(candidate_limit, int) or not 1 <= candidate_limit <= MAX_CANDIDATES:
        raise ValueError(f"candidate_limit must be between 1 and {MAX_CANDIDATES}")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**31 - 1:
        raise ValueError("shortlist seed must be a nonnegative 32-bit integer")
    if (isinstance(generation_public_state_version, bool)
            or not isinstance(generation_public_state_version, int)
            or generation_public_state_version < 0):
        raise ValueError("generation_public_state_version must be a nonnegative integer")
    actions = [a if isinstance(a, EligibleAction) else EligibleAction.model_validate(a) for a in eligible_actions]
    pairs = [(a.candidate_id, a.assay_id) for a in actions]
    if len(pairs) != len(set(pairs)):
        raise ValueError("eligible actions must be unique")
    ordered_actions = sorted(actions, key=lambda a: (a.candidate_id, a.assay_id))
    candidate_ids = sorted({a.candidate_id for a in ordered_actions})
    if len(candidate_ids) > MAX_CANDIDATES * 100:
        raise ValueError("eligible candidate list exceeds safety bound")
    anchors = list(dict.fromkeys(anchor_candidate_ids))
    if any(item not in candidate_ids for item in anchors):
        raise ValueError("shortlist anchor is not eligible from public input")
    if len(anchors) > candidate_limit:
        raise ValueError("shortlist anchors exceed candidate limit")
    rank = lambda candidate_id: hashlib.sha256(canonical_json([
        SHORTLIST_RULE, seed, candidate_id,
    ])).hexdigest()
    selected = list(anchors)
    selected.extend(
        candidate_id for candidate_id in sorted(candidate_ids, key=lambda item: (rank(item), item))
        if candidate_id not in selected and len(selected) < candidate_limit
    )
    selected = sorted(selected)
    selected_actions = [a for a in ordered_actions if a.candidate_id in set(selected)]
    core = {
        "rule": SHORTLIST_RULE,
        "seed": seed,
        "eligible_action_count": len(ordered_actions),
        "eligible_candidate_count": len(candidate_ids),
        "generation_public_state_version": generation_public_state_version,
        "context_candidate_count": len(selected),
        "current_eligible_action_count": len(selected_actions),
        "current_eligible_candidate_count": len(selected),
        "eligible_actions_sha256": sha256_json([a.model_dump(mode="json") for a in ordered_actions]),
        "included_candidate_ids": selected,
    }
    return ShortlistPlan(metadata=ShortlistMetadata(
        **core,
        shortlist_sha256=sha256_json(core),
    ))


def build_context(
    public_campaign: PublicCampaign | dict[str, Any],
    public_evidence_documents: Mapping[str, dict[str, Any]],
    eligible_actions: Sequence[EligibleAction | dict[str, Any]],
    *,
    research_goal: str,
    public_state_version: int,
    public_as_of: str | datetime | None = None,
    budget_and_limits: BudgetAndLimits | dict[str, Any],
    source_run_id: str | None = None,
    public_observations: Sequence[PublicObservation | ObservationContext | dict[str, Any]] | None = None,
    public_attempts: Sequence[PublicAttempt | dict[str, Any]] = (),
    prior_hypotheses: Sequence[PriorHypothesis | dict[str, Any]] = (),
    newly_released_observation_ids: Sequence[str] = (),
    shortlist_plan: ShortlistPlan | None = None,
    candidate_limit: int = 24,
    shortlist_seed: int = 3,
    anchor_candidate_ids: Sequence[str] = (),
    decision_mode: Literal["action", "interpretation_only"] = "action",
) -> DecisionContext:
    """Build a digest-bound context from public DTOs only; paths and private handles are rejected."""
    campaign = public_campaign if isinstance(public_campaign, PublicCampaign) else PublicCampaign.model_validate(public_campaign)
    actions = [a if isinstance(a, EligibleAction) else EligibleAction.model_validate(a) for a in eligible_actions]
    plan = shortlist_plan or plan_shortlist(
        actions, candidate_limit=candidate_limit, seed=shortlist_seed,
        anchor_candidate_ids=anchor_candidate_ids,
        generation_public_state_version=public_state_version,
    )
    all_observations = [
        observation_context_from_public(o)
        for o in (public_observations if public_observations is not None else [
            o for o in campaign.observations
        ])
    ]
    new_ids = list(dict.fromkeys(newly_released_observation_ids))
    observation_map = {o.observation_id: o for o in all_observations}
    if len(observation_map) != len(all_observations):
        raise ValueError("duplicate public observation ID")
    if any(obs_id not in observation_map for obs_id in new_ids):
        raise ValueError("newly released observation is not in public state")

    attempts = [a if isinstance(a, PublicAttempt) else PublicAttempt.model_validate(a) for a in public_attempts]

    current_pairs = {(a.candidate_id, a.assay_id) for a in actions}
    selected_ids = set(plan.metadata.included_candidate_ids)
    required_history_candidates = {observation_map[o].candidate_id for o in new_ids}
    required_history_candidates.update(item.candidate_id for item in attempts)
    allowed_candidates = selected_ids | required_history_candidates
    if any(candidate_id not in selected_ids for candidate_id, _ in current_pairs):
        raise ValueError("eligible action is outside the fixed shortlist")
    if any(candidate_id not in {c.candidate_id for c in campaign.candidates} for candidate_id in allowed_candidates):
        raise ValueError("shortlist candidate missing from public campaign")

    candidate_map = {c.candidate_id: c for c in campaign.candidates}
    filtered_observations = sorted(
        (o for o in all_observations if o.candidate_id in allowed_candidates),
        key=lambda o: (o.released_at, o.candidate_id, o.assay_id, o.observation_id),
    )
    if len(filtered_observations) > MAX_OBSERVATIONS:
        raise ValueError("shortlist observations exceed context limit")

    evidence_items, cid_by_candidate = _build_evidence_catalog(
        campaign, public_evidence_documents, filtered_observations,
    )
    if len(evidence_items) > MAX_EVIDENCE_ITEMS:
        raise ValueError("shortlist evidence exceeds context limit")
    evidence_by_id = {item.evidence_id: item for item in evidence_items}
    observation_contexts = []
    for obs in filtered_observations:
        refs = [eid for eid in obs.evidence_refs if eid in evidence_by_id]
        if len(refs) != len(obs.evidence_refs):
            raise ValueError(f"public evidence missing for observation {obs.observation_id}")
        observation_contexts.append(obs.model_copy(update={"evidence_refs": refs}))

    assay_contexts = _assay_contexts(campaign, public_evidence_documents)
    assay_ids = {a.assay_id for a in assay_contexts}
    if any(a.candidate_id not in selected_ids or a.assay_id not in assay_ids for a in actions):
        raise ValueError("current eligible action is outside the fixed shortlist")
    candidates = []
    for candidate_id in sorted(allowed_candidates):
        source = candidate_map[candidate_id]
        obs_ids = [o.observation_id for o in filtered_observations if o.candidate_id == candidate_id]
        candidates.append(CandidateContext(
            candidate_id=source.candidate_id, source=source.source,
            source_id=source.source_id, source_cid=cid_by_candidate.get(candidate_id),
            original_smiles=source.original_smiles, observation_ids=obs_ids,
        ))
    # Recompute the metadata digest for the actual context. Selection-time
    # totals remain intact while the context and current-action counts reflect
    # only DTOs that are sent to this decision.
    metadata_core = plan.metadata.model_dump(exclude={"shortlist_sha256"})
    metadata_core.update({
        "context_candidate_count": len(candidates),
        "current_eligible_action_count": len(actions),
        "current_eligible_candidate_count": len({a.candidate_id for a in actions}),
    })
    plan = ShortlistPlan(metadata=ShortlistMetadata(
        **metadata_core, shortlist_sha256=sha256_json(metadata_core),
    ))
    state_refs = [
        StateReference(state_ref="state:public", kind="public_state", value=f"version={public_state_version}; as_of={_as_of_string(public_as_of, campaign.as_of)}"),
        StateReference(state_ref="state:budget", kind="budget", value=json.dumps(
            (budget_and_limits if isinstance(budget_and_limits, BudgetAndLimits) else BudgetAndLimits.model_validate(budget_and_limits)).model_dump(mode="json"),
            sort_keys=True, separators=(",", ":"),
        )),
        StateReference(state_ref="state:eligible_actions", kind="eligibility", value=f"shortlist={plan.metadata.shortlist_sha256}; current_actions={len(actions)}"),
    ]
    for attempt in attempts:
        if attempt.candidate_id not in allowed_candidates or attempt.assay_id not in assay_ids:
            raise ValueError("public attempt outside context")
        state_refs.append(StateReference(
            state_ref=f"attempt:{attempt.step_no}:{attempt.candidate_id}:{attempt.assay_id}",
            kind="attempt", candidate_id=attempt.candidate_id,
            assay_id=attempt.assay_id, step_no=attempt.step_no,
            value=attempt.status,
        ))
    prior = [h if isinstance(h, PriorHypothesis) else PriorHypothesis.model_validate(h) for h in prior_hypotheses]
    gaps = _information_gaps(campaign, assay_contexts, candidates, observation_contexts, plan.metadata)
    budget = budget_and_limits if isinstance(budget_and_limits, BudgetAndLimits) else BudgetAndLimits.model_validate(budget_and_limits)
    payload: dict[str, Any] = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "decision_mode": decision_mode,
        "campaign_id": campaign.campaign.campaign_id,
        "source_run_id": source_run_id,
        "public_state_version": public_state_version,
        "public_as_of": _as_of_string(public_as_of, campaign.as_of),
        "research_goal": research_goal,
        "assay_context": [a.model_dump(mode="json") for a in assay_contexts],
        "candidate_contexts": [c.model_dump(mode="json") for c in candidates],
        "public_observations": [o.model_dump(mode="json") for o in observation_contexts],
        "evidence_catalog": [e.model_dump(mode="json") for e in evidence_items],
        "eligible_actions": [a.model_dump(mode="json") for a in actions],
        "budget_and_limits": budget.model_dump(mode="json"),
        "prior_hypotheses": [h.model_dump(mode="json") for h in prior],
        "newly_released_observation_ids": new_ids,
        "public_attempts": [a.model_dump(mode="json") for a in attempts],
        "shortlist": plan.metadata.model_dump(mode="json"),
        "information_gaps": gaps,
        "state_refs": [r.model_dump(mode="json") for r in state_refs],
    }
    digest = sha256_json(payload)
    context = DecisionContext.model_validate({**payload, "context_digest": digest})
    return context


def _as_of_string(value: str | datetime | None, fallback: datetime) -> str:
    if value is None:
        return fallback.isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("public_as_of must include a timezone")
        return value.isoformat()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("public_as_of must include a timezone")
    return parsed.isoformat()


def _build_evidence_catalog(
    campaign: PublicCampaign,
    documents: Mapping[str, dict[str, Any]],
    observations: Sequence[ObservationContext],
) -> tuple[list[EvidenceCatalogItem], dict[str, str]]:
    needed = {eid for observation in observations for eid in observation.evidence_refs}
    referenced = {item.evidence_id: item for item in campaign.evidence}
    selected_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cid_by_candidate: dict[str, str] = {}
    obs_by_id = {item.observation_id: item for item in observations}
    candidate_by_id = {item.candidate_id: item for item in campaign.candidates}
    assay_by_id = {item.assay_id: item for item in campaign.assays}
    runtime_evidence_ids: set[str] = set()
    for evidence_id in needed:
        document = documents.get(evidence_id)
        if not isinstance(document, dict):
            raise ValueError(f"public evidence document missing: {evidence_id}")
        traces = document.get("observation_traces")
        if isinstance(traces, list):
            for trace in traces:
                if not isinstance(trace, dict) or trace.get("observation_id") not in obs_by_id:
                    continue
                observation = obs_by_id[trace["observation_id"]]
                raw = trace.get("raw_row")
                if not isinstance(raw, dict):
                    raise ValueError("primary public evidence row missing raw_row")
                row = {key: raw.get(key) for key in _PUBLIC_ROW_KEYS}
                sid = row.get("SID")
                candidate = candidate_by_id.get(observation.candidate_id)
                if candidate is None or f"SID:{sid}" != candidate.source_id:
                    raise ValueError("primary evidence SID does not match public candidate")
                if str(row.get("AID")) != str(document.get("aid")):
                    raise ValueError("primary evidence AID mismatch")
                evidence_ref = referenced.get(evidence_id)
                assay = assay_by_id.get(observation.assay_id)
                if evidence_ref is None or assay is None or evidence_ref.source_id != f"PubChem AID:{row.get('AID')}":
                    raise ValueError("primary evidence does not match Observation assay")
                if document.get("evidence_id") != evidence_id or document.get("source_id") != evidence_ref.source_id:
                    raise ValueError("primary evidence identity mismatch")
                if trace.get("raw_outcome") != row.get("Activity Outcome"):
                    raise ValueError("primary evidence outcome trace mismatch")
                if observation.raw_verdict != row.get("Activity Outcome"):
                    raise ValueError("primary evidence outcome does not match Observation")
                cid = row.get("CID")
                if cid is not None:
                    previous = cid_by_candidate.setdefault(observation.candidate_id, str(cid))
                    if previous != str(cid):
                        raise ValueError("conflicting public CID values for candidate")
                selected_rows[evidence_id].append({
                    "observation_id": observation.observation_id,
                    "candidate_id": observation.candidate_id,
                    "assay_id": observation.assay_id,
                    "source_row_id": trace.get("source_row_id"),
                    "source_row_number": trace.get("source_row_number"),
                    "source_file_sha256": trace.get("source_file_sha256"),
                    "raw_row": row,
                })
        elif document.get("source_kind") == "pubchem_runtime_measurement":
            # Runtime publications store the allowlisted source row directly in
            # the evidence document. The coordinator has checked its persisted
            # payload hash in the atomic public snapshot; repeat its identity
            # and Observation-link checks before exposing only public columns.
            evidence_ref = referenced.get(evidence_id)
            if (document.get("evidence_id") != evidence_id or evidence_ref is None
                    or evidence_ref.source_kind != "pubchem_runtime_measurement"
                    or document.get("source_row_id") != evidence_ref.source_id):
                raise ValueError("runtime evidence identity mismatch")
            linked = [item for item in observations if evidence_id in item.evidence_refs]
            if len(linked) != 1:
                raise ValueError("runtime evidence must identify exactly one Observation")
            observation = linked[0]
            raw = document.get("raw_row")
            if not isinstance(raw, dict):
                raise ValueError("runtime public evidence raw row missing")
            row = {key: raw.get(key) for key in _PUBLIC_ROW_KEYS}
            candidate = candidate_by_id.get(observation.candidate_id)
            assay = assay_by_id.get(observation.assay_id)
            aid = document.get("aid")
            if (candidate is None or assay is None
                    or document.get("candidate_id") != observation.candidate_id
                    or f"SID:{row.get('SID')}" != candidate.source_id
                    or str(row.get("AID")) != str(aid)):
                raise ValueError("runtime evidence does not match its candidate, SID, or AID")
            matching_assay_evidence = [
                item for item in campaign.evidence
                if item.source_id == f"PubChem AID:{aid}"
                and (source_document := documents.get(item.evidence_id)) is not None
                and source_document.get("evidence_id") == item.evidence_id
                and str(source_document.get("aid")) == str(aid)
                and source_document.get("name") == assay.name
            ]
            if len(matching_assay_evidence) != 1:
                raise ValueError("runtime evidence AID does not match the Observation assay")
            raw_outcome_values = [raw[key] for key in ("Activity Outcome", "Outcome") if key in raw]
            if (document.get("raw_outcome") != observation.raw_verdict
                    or not raw_outcome_values
                    or all(value != document.get("raw_outcome") for value in raw_outcome_values)):
                raise ValueError("runtime evidence outcome does not match Observation")
            if getattr(observation.verdict, "value", observation.verdict) not in {
                "active", "inactive", "inconclusive", "unspecified",
            }:
                raise ValueError("invalid runtime Observation verdict")
            if document.get("cid") is not None:
                previous = cid_by_candidate.setdefault(observation.candidate_id, str(document["cid"]))
                if previous != str(document["cid"]):
                    raise ValueError("conflicting public CID values for candidate")
            selected_rows[evidence_id].append({
                "observation_id": observation.observation_id,
                "candidate_id": observation.candidate_id,
                "assay_id": observation.assay_id,
                "source_row_id": document.get("source_row_id"),
                "source_row_number": document.get("source_row_number"),
                "source_file_sha256": document.get("source_file_sha256"),
                "raw_row": row,
                "raw_verdict": document.get("raw_outcome"),
                "protocol_location": document.get("protocol_location"),
            })
            runtime_evidence_ids.add(evidence_id)
        else:
            archive_payload = document.get("payload")
            if not isinstance(archive_payload, dict):
                raise ValueError("public evidence payload must be an object")
            expected = document.get("sha256")
            if not isinstance(expected, str) or sha256_json(archive_payload) != expected:
                raise ValueError("public evidence payload hash mismatch")
            if archive_payload.get("evidence_id") != evidence_id:
                raise ValueError("released evidence ID does not match payload")
            candidate_id = archive_payload.get("candidate_id")
            obs = [o for o in observations if o.observation_id in archive_payload.get("observation_ids", [])]
            if not obs:
                # Stage 4 archive nests the Observation beside the EvidenceRef.
                obs = [o for o in observations if o.candidate_id == candidate_id and evidence_id in o.evidence_refs]
            if len(obs) != 1 or candidate_id != obs[0].candidate_id:
                raise ValueError("released evidence identity does not match Observation")
            raw = archive_payload.get("raw_row")
            if not isinstance(raw, dict):
                raise ValueError("released public evidence raw row missing")
            row = {key: raw.get(key) for key in _PUBLIC_ROW_KEYS}
            candidate = candidate_by_id.get(obs[0].candidate_id)
            assay = assay_by_id.get(obs[0].assay_id)
            if candidate is None or f"SID:{row.get('SID')}" != candidate.source_id:
                raise ValueError("released evidence SID does not match public candidate")
            if assay is None or str(row.get("AID")) != str(archive_payload.get("aid")):
                raise ValueError("released evidence AID does not match Observation assay")
            if archive_payload.get("raw_outcome") != row.get("Activity Outcome") or obs[0].raw_verdict != row.get("Activity Outcome"):
                raise ValueError("released evidence outcome does not match Observation")
            if getattr(obs[0].verdict, "value", obs[0].verdict) not in {"active", "inactive", "inconclusive", "unspecified"}:
                raise ValueError("invalid released verdict")
            selected_rows[evidence_id].append({
                "observation_id": obs[0].observation_id,
                "candidate_id": obs[0].candidate_id,
                "assay_id": obs[0].assay_id,
                "source_row_id": archive_payload.get("source_row_id"),
                "source_row_number": archive_payload.get("source_row_number"),
                "raw_row": row,
                "raw_verdict": archive_payload.get("raw_outcome"),
                "protocol_location": archive_payload.get("protocol_location"),
                "source_file_sha256": archive_payload.get("source_file_sha256"),
            })
            if archive_payload.get("cid") is not None:
                cid_by_candidate[obs[0].candidate_id] = str(archive_payload["cid"])

    items: list[EvidenceCatalogItem] = []
    for evidence_id in sorted(needed):
        ref = referenced.get(evidence_id)
        document = documents[evidence_id]
        rows = sorted(selected_rows[evidence_id], key=lambda x: x["observation_id"])
        assay_ids = sorted({row["assay_id"] for row in rows})
        candidate_ids = sorted({row["candidate_id"] for row in rows})
        observation_ids = sorted({row["observation_id"] for row in rows})
        if evidence_id in runtime_evidence_ids:
            assert ref is not None
            source_kind = ref.source_kind
            source_id = ref.source_id
            content = {"kind": "published_measurement_rows", "rows": rows}
        elif "payload" in document and isinstance(document["payload"], dict):
            payload = document["payload"]
            source_kind = str((document.get("reference") or {}).get("source_kind") or payload.get("source_kind") or "public_measurement")
            source_id = str((document.get("reference") or {}).get("source_id") or payload.get("source_id") or evidence_id)
            content = {
                "kind": "published_measurement_rows",
                "rows": rows,
            }
        else:
            source_kind = ref.source_kind if ref is not None else str(document.get("source_kind") or "public_bundle_evidence")
            source_id = ref.source_id if ref is not None else str(document.get("source_id") or evidence_id)
            details = {
                key: document.get(key) for key in (
                    "aid", "name", "endpoint", "endpoint_scope", "endpoint_meaning",
                    "official_result_names", "activity_name_policy", "protocol_location",
                ) if document.get(key) is not None
            }
            content = {"kind": "public_assay_definition_and_rows", "assay_definition": details, "rows": rows}
            if not assay_ids:
                aid_text = str(document.get("aid"))
                assay_ids = sorted({
                    observation.assay_id for observation in observations
                    if any(ref.source_id == f"PubChem AID:{aid_text}" for ref in [referenced.get(evidence_id)] if ref is not None)
                })
        if not assay_ids:
            raise ValueError(f"public evidence is not linked to an assay: {evidence_id}")
        items.append(EvidenceCatalogItem(
            evidence_id=evidence_id, source_kind=source_kind, source_id=source_id,
            assay_ids=assay_ids, candidate_ids=candidate_ids, observation_ids=observation_ids,
            payload_sha256=sha256_json(content), public_content=content,
        ))
    return items, cid_by_candidate


def _assay_contexts(campaign: PublicCampaign, documents: Mapping[str, dict[str, Any]]) -> list[AssayContext]:
    referenced = {item.evidence_id: item for item in campaign.evidence}
    docs_by_source: dict[str, dict[str, Any]] = {}
    for document in documents.values():
        aid = document.get("aid")
        if aid is not None:
            docs_by_source[f"PubChem AID:{aid}"] = document
        payload = document.get("payload")
        if isinstance(payload, dict) and payload.get("aid") is not None:
            docs_by_source.setdefault(f"PubChem AID:{payload['aid']}", payload)
    contexts: list[AssayContext] = []
    for assay in campaign.assays:
        source_ids = [referenced[eid].source_id for eid in assay_observation_evidence_ids(campaign, assay.assay_id)
                      if eid in referenced]
        doc = next((docs_by_source[source_id] for source_id in source_ids
                    if source_id in docs_by_source and str(docs_by_source[source_id].get("name", "")) == assay.name), {})
        if not doc:
            doc = next((item for item in docs_by_source.values()
                        if str(item.get("name", "")) == assay.name), {})
        protocol = doc.get("protocol_location")
        contexts.append(AssayContext(
            assay_id=assay.assay_id, name=assay.name, role=assay.role.value,
            endpoint=assay.endpoint, unit=assay.unit,
            verdict_meaning={key.value: value for key, value in assay.verdict_meaning.items()},
            endpoint_scope=doc.get("endpoint_scope"), endpoint_meaning=doc.get("endpoint_meaning"),
            official_result_names=list(doc.get("official_result_names") or []),
            protocol_locations=[protocol] if isinstance(protocol, str) else [],
            experimental_conditions_available=False,
        ))
    return contexts


def assay_observation_evidence_ids(campaign: PublicCampaign, assay_id: str) -> set[str]:
    """Public evidence IDs associated with the assay through released observations."""
    return {evidence_id for observation in campaign.observations if observation.assay_id == assay_id
            for evidence_id in observation.evidence_ids}


def _information_gaps(campaign, assays, candidates, observations, shortlist) -> list[str]:
    gaps: list[str] = []
    if shortlist.eligible_candidate_count > len(candidates):
        gaps.append(
            f"Only {len(candidates)} of {shortlist.eligible_candidate_count} eligible candidates are in this deterministic exploratory shortlist; it is not a learned ranking."
        )
    if candidates and all(item.original_smiles for item in candidates):
        gaps.append("Original SMILES are available; no calculated molecular descriptors or validated similarity features are supplied.")
    if any(item.unit == "categorical" for item in assays) and not any(o.value is not None for o in observations):
        gaps.append("The supplied measurement records contain categorical Activity Outcome only; numeric potency values are unavailable in this context.")
    if any(not item.experimental_conditions_available for item in assays):
        gaps.append("Detailed experimental conditions are not present in the public campaign DTO; do not infer dose, duration, or laboratory settings.")
    if not any(item.assay_id != next((a.assay_id for a in assays if a.role == "primary"), "") for item in observations):
        gaps.append("No released follow-up Observation is present in this context; absence here does not establish whether an experiment was performed.")
    return gaps
