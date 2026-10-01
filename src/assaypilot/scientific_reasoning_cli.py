"""CLI for isolated Stage 5-A public-context previews and real provider calls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Sequence
from uuid import uuid4

from assaypilot.data.adapter import PublicBundleAdapter
from assaypilot.domain import DataSource
from assaypilot.llm_provider import (
    LLMProviderError, LLMSettings, OpenAICompatibleChatProvider,
)
from assaypilot.scientific_context import (
    BudgetAndLimits, PriorHypothesis, PublicAttempt,
    build_context, derive_eligible_actions, observation_context_from_public,
    plan_shortlist,
)
from assaypilot.scientific_reasoner import ScientificReasoner


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN_ID = "stage4-0160c1d8677d414f8a07d4035e88a5d7"
_RUN_ID = re.compile(r"^stage4-[0-9a-f]{32}$")
_PUBLIC_CAMPAIGN = "revision-20260918-primary-active-all"
SCIENCE_GOAL = (
    "For compounds with an Active Activity Outcome in primary PubChem assay AID 2016, "
    "assess whether released categorical Activity Outcome evidence is also Active in the "
    "configured confirmatory PubChem assay AID 2272, and propose the next eligible "
    "candidate-assay replay action. Interpret outcomes only within their named yeast "
    "TOR-pathway GFP assay; this is not a direct-binding or therapeutic-efficacy claim."
)


def load_public_campaign() -> tuple[Any, dict[str, dict[str, Any]], Path]:
    """Load a manifest-verified bundle and only its allowlisted public evidence."""
    public_root = ROOT / "data_snapshots/pubchem-tor-mep2-20260915" / _PUBLIC_CAMPAIGN / "bundle/public"
    manifest = public_root / "manifest.json"
    campaign = PublicBundleAdapter().load(DataSource(kind="public_bundle", location=str(manifest)))
    documents: dict[str, dict[str, Any]] = {}
    for reference in campaign.evidence:
        path = (public_root / reference.location).resolve()
        if public_root.resolve() not in path.parents:
            raise ValueError("public evidence reference escapes bundle/public")
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("evidence_id") != reference.evidence_id:
            raise ValueError("public evidence document identity mismatch")
        documents[reference.evidence_id] = document
    return campaign, documents, public_root


def build_actual_public_pair(
    run_id: str = DEFAULT_RUN_ID,
    *,
    prior_hypotheses: Sequence[PriorHypothesis] = (),
) -> tuple[Any, dict[str, Any]]:
    """Build paired contexts from a single Stage 4 public archive and step 1 only."""
    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("invalid public run id")
    campaign, evidence_documents, _ = load_public_campaign()
    run_root = ROOT / "runtime/stage4/runs" / run_id / "artifacts"
    exports = sorted(run_root.glob("rev-*/public_export.json"))
    if not exports:
        raise ValueError("public_export.json is not available for the requested run")
    public_export = json.loads(exports[-1].read_text(encoding="utf-8"))
    if public_export.get("run_id") != run_id or public_export.get("schema_version") != "assaypilot.stage4.public-export.v1":
        raise ValueError("Stage 4 public export identity/schema mismatch")
    if public_export.get("configuration", {}).get("campaign") != _PUBLIC_CAMPAIGN:
        raise ValueError("the requested public run is not the expanded primary-only campaign")
    first_step = next((item for item in public_export.get("trace", {}).get("steps", [])
                       if item.get("step_no") == 1 and item.get("status") == "released"
                       and item.get("published_observations")), None)
    if not isinstance(first_step, dict):
        raise ValueError("run does not contain a released public observation at step 1")
    pre_public_view = first_step.get("public_view")
    if not isinstance(pre_public_view, dict):
        raise ValueError("step 1 does not contain its pre-action public view")
    pre_version = _require_state_version(
        pre_public_view.get("state_version"), "step 1 pre-action public view",
    )
    candidate_id, assay_id = first_step.get("candidate_id"), first_step.get("assay_id")
    if not isinstance(candidate_id, str) or not isinstance(assay_id, str):
        raise ValueError("step 1 public identity is incomplete")

    initial_observations = [observation_context_from_public(item) for item in campaign.observations]
    full_eligible = derive_eligible_actions(
        campaign, initial_observations, assay_id=assay_id,
    )
    if (candidate_id, assay_id) not in {(item.candidate_id, item.assay_id) for item in full_eligible}:
        raise ValueError("historical step 1 action is not eligible from initial public observations")
    plan = plan_shortlist(
        full_eligible, candidate_limit=24, seed=3,
        anchor_candidate_ids=[candidate_id],
        generation_public_state_version=pre_version,
    )
    included = set(plan.metadata.included_candidate_ids)

    published: list[ObservationContext] = []
    supplemental_evidence = dict(evidence_documents)
    matched_exec: dict[str, Any] | None = None
    for execution in public_export.get("published_results", {}).get("executions", []):
        if execution.get("step_no") == 1 and execution.get("candidate_id") == candidate_id and execution.get("assay_id") == assay_id:
            matched_exec = execution
            break
    if matched_exec is None:
        raise ValueError("released step 1 has no matching published execution")
    for row in matched_exec.get("evidence", []):
        reference = row.get("reference", {})
        evidence_id = reference.get("evidence_id")
        if not isinstance(evidence_id, str) or not isinstance(row.get("payload"), dict):
            raise ValueError("released public evidence is incomplete")
        supplemental_evidence[evidence_id] = {
            "payload": row["payload"], "reference": reference,
            "sha256": row.get("sha256"),
        }
    for observation in matched_exec.get("observations", []):
        dto = observation_context_from_public(observation)
        if (dto.candidate_id, dto.assay_id) != (candidate_id, assay_id):
            raise ValueError("step 1 Observation does not match trace identity")
        published.append(dto)
    if not published:
        raise ValueError("step 1 published execution has no Observation")
    if any(evidence_id not in supplemental_evidence for obs in published for evidence_id in obs.evidence_refs):
        raise ValueError("released Observation evidence is missing from public export")

    pre_actions = [item for item in full_eligible if item.candidate_id in included]
    budget_total = str(public_export.get("configuration", {}).get("budget", ""))
    if not budget_total:
        raise ValueError("public run budget is unavailable")
    max_steps = int(public_export.get("configuration", {}).get("max_steps", 0))
    pre_budget = BudgetAndLimits(
        total=budget_total, spent="0", reserved="0", available=budget_total,
        unit=str(public_export["configuration"].get("budget_unit", "synthetic_credit")),
        assumed=bool(public_export["configuration"].get("budget_assumed", True)),
        max_steps_remaining=max_steps,
        max_duration_seconds=public_export["configuration"].get("max_duration_seconds"),
    )
    pre = build_context(
        campaign, evidence_documents, pre_actions, research_goal=SCIENCE_GOAL,
        public_state_version=pre_version,
        public_as_of=campaign.as_of,
        budget_and_limits=pre_budget, source_run_id=run_id,
        public_observations=initial_observations, shortlist_plan=plan,
        anchor_candidate_ids=[candidate_id],
    )

    post_observations = initial_observations + published
    post_full_actions = derive_eligible_actions(
        campaign, post_observations, assay_id=assay_id,
        attempted_pairs=[(candidate_id, assay_id)],
    )
    post_actions = [item for item in post_full_actions if item.candidate_id in included]
    after_budget = first_step.get("budget_after_step", {})
    spent = str(after_budget.get("spent", ""))
    reserved = str(after_budget.get("reserved", ""))
    available = str(after_budget.get("available", ""))
    total = _normalize_decimal(pre_budget.total)
    post_version = _require_state_version(
        matched_exec.get("state_version"), "published execution",
    )
    if post_version <= pre_version:
        raise ValueError("published execution state version did not advance beyond the pre-action state")
    published_at = matched_exec.get("published_at")
    if not isinstance(published_at, str):
        raise ValueError("released public timestamp is unavailable")
    post_budget = BudgetAndLimits(
        total=total, spent=spent, reserved=reserved, available=available,
        unit=str(after_budget.get("unit", pre_budget.unit)), assumed=pre_budget.assumed,
        max_steps_remaining=max(0, max_steps - 1),
        max_duration_seconds=pre_budget.max_duration_seconds,
    )
    released_ids = [item.observation_id for item in published]
    post = build_context(
        campaign, supplemental_evidence, post_actions, research_goal=SCIENCE_GOAL,
        public_state_version=post_version, public_as_of=published_at,
        budget_and_limits=post_budget, source_run_id=run_id,
        public_observations=post_observations,
        public_attempts=[PublicAttempt(
            step_no=1, candidate_id=candidate_id, assay_id=assay_id,
            status="released", observation_ids=released_ids,
        )],
        prior_hypotheses=prior_hypotheses,
        newly_released_observation_ids=released_ids, shortlist_plan=plan,
    )
    return campaign, {
        "pre": pre, "post": post, "shortlist_plan": plan,
        "released_observations": published,
        "historical_action": {"candidate_id": candidate_id, "assay_id": assay_id,
                              "step_no": 1, "selector": public_export["summary"].get("selector", {})},
        "public_export_revision": exports[-1].parent.name,
    }


def _require_state_version(value: Any, source: str) -> int:
    """Read a persisted state version; never infer it from a run step number."""
    if type(value) is not int or value < 0:
        raise ValueError(f"{source} has no valid persisted state version")
    return value


def _normalize_decimal(value: str) -> str:
    """Normalize an already validated public decimal string without float conversion."""
    from decimal import Decimal, InvalidOperation
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("public budget is not a decimal string") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("public budget is invalid")
    return format(number.normalize(), "f")


def create_smoke_messages() -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "Return one JSON object only. Do not include prose."},
        {"role": "user", "content": "Return exactly {\"status\":\"ok\"} as JSON."},
    ]


def _private_output_dir(parent: Path) -> Path:
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass
    target = parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:10])
    target.mkdir(mode=0o700)
    return target


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _replace_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.partial")
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _smoke_call(provider: OpenAICompatibleChatProvider) -> dict[str, Any]:
    smoke_schema = {
        "type": "object", "properties": {"status": {"type": "string", "enum": ["ok"]}},
        "required": ["status"], "additionalProperties": False,
    }
    response = provider.complete(
        create_smoke_messages(), max_output_tokens=min(256, provider.settings.max_output_tokens),
        json_schema=smoke_schema,
    )
    try:
        parsed = json.loads(response.content)
    except json.JSONDecodeError:
        raise ValueError("provider smoke response was not JSON") from None
    if parsed != {"status": "ok"}:
        raise ValueError("provider smoke response did not match expected JSON")
    return {
        "provider": response.provider, "model": response.model,
        "request_id": response.request_id, "latency_ms": response.latency_ms,
        "usage": response.usage, "transport_attempts": response.attempts,
        "valid_json_object": True,
    }


def _prior_from_decision(result) -> list[PriorHypothesis]:
    return [PriorHypothesis(
        hypothesis_id=item.hypothesis_id, statement=item.statement,
        hypothesis_kind=item.hypothesis_kind,
        candidate_id=item.candidate_id, assay_id=item.assay_id,
        expected_outcome=item.expected_outcome, status=item.status,
    ) for item in result.decision.hypotheses]


def _metadata_safe(settings: LLMSettings) -> dict[str, Any]:
    return {
        "provider": settings.provider, "configured_model": settings.model,
        "api_mode": settings.resolved_api_mode,
        "response_format": settings.response_format, "timeout_seconds": settings.timeout_seconds,
        "max_output_tokens": settings.max_output_tokens,
        "token_parameter": settings.effective_token_parameter,
        "transport_retry_attempts": settings.retry_attempts,
        "credentials_present": bool(settings.api_key),
    }


def _execute(args: argparse.Namespace) -> int:
    campaign, pair = build_actual_public_pair(args.run_id)
    pre, post = pair["pre"], pair["post"]
    output_dir = _private_output_dir(Path(args.output_root) if args.output_root else ROOT / "runtime/stage5a")
    _write_json(output_dir / "initial_context.json", pre.model_dump(mode="json"))
    _write_json(output_dir / "post_observation_context.json", post.model_dump(mode="json"))
    manifest = {
        "actual_api_call": False,
        "status": "contexts_built_only",
        "campaign_id": campaign.campaign.campaign_id,
        "run_id": args.run_id,
        "public_export_revision": pair["public_export_revision"],
        "shortlist": pair["shortlist_plan"].metadata.model_dump(mode="json"),
        "historical_action": pair["historical_action"],
        "pre_context_digest": pre.context_digest,
        "post_context_digest": post.context_digest,
    }
    _write_json(output_dir / "input_manifest.json", manifest)
    if args.preview_only:
        print(json.dumps({"actual_api_call": False, "output_dir": str(output_dir),
                          "pre_context_digest": pre.context_digest,
                          "post_context_digest": post.context_digest,
                          "eligible_actions_initial": pair["shortlist_plan"].metadata.eligible_action_count,
                          "shortlist_candidates": len(pair["shortlist_plan"].metadata.included_candidate_ids)}, ensure_ascii=False))
        return 0
    try:
        settings = LLMSettings.from_environment(env_file=ROOT / ".env.stage5a.local")
    except LLMProviderError as exc:
        manifest.update({"status": "provider_not_configured" if exc.code == "missing_settings" else "provider_configuration_error",
                         "provider_configuration_error": exc.code})
        _replace_json(output_dir / "input_manifest.json", manifest)
        raise
    provider = OpenAICompatibleChatProvider(settings)
    manifest.update({"provider": settings.provider, "configured_model": settings.model,
                     "response_format": settings.response_format, "status": "starting_smoke_call"})
    _replace_json(output_dir / "input_manifest.json", manifest)
    try:
        smoke = _smoke_call(provider)
        _write_json(output_dir / "provider_smoke.json", smoke)
        manifest.update({"actual_api_call": True, "status": "provider_smoke_passed",
                         "provider": smoke["provider"], "configured_model": smoke["model"]})
        _replace_json(output_dir / "input_manifest.json", manifest)
        reasoner = ScientificReasoner(provider, max_output_tokens=settings.max_output_tokens)
        pre_result = reasoner.decide(pre)
        _write_json(output_dir / "initial_decision.json", pre_result.model_dump(mode="json"))
        manifest.update({"status": "pre_judgment_validated", "pre_decision_call_count": len(pre_result.calls)})
        _replace_json(output_dir / "input_manifest.json", manifest)
        post_prior = _prior_from_decision(pre_result)
        _, refreshed = build_actual_public_pair(args.run_id, prior_hypotheses=post_prior)
        post = refreshed["post"]
        _replace_json(output_dir / "post_observation_context.json", post.model_dump(mode="json"))
        post_result = reasoner.decide(post)
        _write_json(output_dir / "post_observation_decision.json", post_result.model_dump(mode="json"))
        manifest.update({
            "status": "completed", "actual_api_call": True,
            "post_context_digest": post.context_digest,
            "prompt_version": pre_result.prompt_version,
            "smoke_call": smoke,
            "pre_decision_call_count": len(pre_result.calls),
            "post_decision_call_count": len(post_result.calls),
        })
        _replace_json(output_dir / "input_manifest.json", manifest)
        print(json.dumps({
            "actual_api_call": True, "output_dir": str(output_dir),
            "provider": smoke["provider"], "model": smoke["model"],
            "smoke_latency_ms": smoke["latency_ms"],
            "pre_context_digest": pre.context_digest,
            "post_context_digest": post.context_digest,
            "pre_decision": pre_result.decision.model_dump(mode="json"),
            "post_decision": post_result.decision.model_dump(mode="json"),
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        manifest.update({"status": "failed", "failure_type": type(exc).__name__,
                         "failure_code": getattr(exc, "code", "stage5a_error")})
        validation_issues = getattr(exc, "issues", None)
        if isinstance(validation_issues, list) and all(isinstance(item, str) for item in validation_issues):
            manifest["validation_issues"] = validation_issues[:24]
        _replace_json(output_dir / "input_manifest.json", manifest)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 5-A public-only scientific reasoning")
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor = subparsers.add_parser("doctor", help="check whether explicit provider settings are present")
    doctor.set_defaults(command="doctor")
    run = subparsers.add_parser("run-public-pair", help="smoke-test provider, then judge actual public pre/post state")
    run.add_argument("--run-id", default=DEFAULT_RUN_ID)
    run.add_argument("--preview-only", action="store_true", help="build/save paired contexts without any API call")
    run.add_argument("--output-root")
    run.set_defaults(command="run-public-pair")
    args = parser.parse_args(argv)
    if args.command == "doctor":
        try:
            settings = LLMSettings.from_environment(env_file=ROOT / ".env.stage5a.local")
        except LLMProviderError as exc:
            print(json.dumps({"ready": False, "error": exc.code, "message": str(exc)}, ensure_ascii=False))
            return 2
        print(json.dumps({"ready": True, "settings": _metadata_safe(settings)}, ensure_ascii=False))
        return 0
    try:
        return _execute(args)
    except LLMProviderError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc),
                          "status_code": exc.status_code, "request_id": exc.request_id}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:
        # Keep exception text for safe, local CLI errors but never include secrets or HTTP bodies.
        print(json.dumps({"error": "stage5a_failed", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
