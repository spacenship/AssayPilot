"""Allowlisted public projections for Stage 5-B scientific runs.

Private controller artifacts are read only by a worker or by this module's
stored-archive reader. Callers receive this deliberately small DTO, never the
private SQLite rows or the Stage 5-B artifact directory.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from assaypilot.scientific_context import DecisionContext, sha256_json
from assaypilot.scientific_reasoner import validate_decision


SCHEMA_VERSION = "assaypilot.scientific-public-projection.v1"
STORED_RUN_ID = "stage5b-a3dd1a84d0d84c8db2d212ed4d9f09ac"
_STORED_ID = re.compile(r"^stage5b-[0-9a-f]{32}$")
_SAFE_STOP_REASONS = frozenset({
    "max_steps", "max_duration", "deadline", "budget_exhausted",
    "no_executable_actions", "prerequisites_unmet", "selector_stop",
    "retry_exhausted", "user_interrupt", "scientific_finalization_complete",
    "scientific_decision_failed", "llm_call_limit", "interpretation_complete",
})
_SAFE_CALL_ERRORS = frozenset({
    "provider_connection_error", "provider_http_error", "provider_response_error",
    "provider_timeout", "missing_settings", "invalid_settings_file",
    "insecure_settings_file", "invalid_endpoint", "invalid_response_format",
    "invalid_api_mode", "invalid_token_parameter", "decision_invalid",
    "decision_validation_failed", "deadline", "llm_call_limit",
})


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _str(value: Any, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _public_evidence(item: dict[str, Any], *, candidate_ids: set[str], observation_ids: set[str]) -> dict[str, Any]:
    content = item.get("public_content")
    if not isinstance(content, dict) or sha256_json(content) != item.get("payload_sha256"):
        raise ValueError("public evidence hash validation failed")
    definition = content.get("assay_definition")
    safe_definition = None
    if isinstance(definition, dict):
        safe_definition = {
            key: definition[key] for key in (
                "aid", "name", "endpoint", "endpoint_scope", "endpoint_meaning",
                "official_result_names", "activity_name_policy",
            ) if key in definition
        }
        locations = definition.get("protocol_location")
        if isinstance(locations, str) and re.fullmatch(r"https://pubchem\.ncbi\.nlm\.nih\.gov/bioassay/[0-9]+", locations):
            safe_definition["protocol_location"] = locations
    rows = []
    for row in _list(content.get("rows")):
        if not isinstance(row, dict):
            continue
        if row.get("candidate_id") not in candidate_ids and row.get("observation_id") not in observation_ids:
            continue
        raw = row.get("raw_row")
        if not isinstance(raw, dict):
            continue
        safe_raw = {key: raw[key] for key in ("AID", "SID", "CID", "Activity Outcome") if key in raw}
        rows.append({
            "candidate_id": row.get("candidate_id"),
            "observation_id": row.get("observation_id"),
            "assay_id": row.get("assay_id"),
            "raw_row": safe_raw,
            "source_file_sha256": row.get("source_file_sha256"),
            "source_row_id": row.get("source_row_id"),
            "source_row_number": row.get("source_row_number"),
        })
    return {
        "evidence_id": item.get("evidence_id"),
        "source_kind": item.get("source_kind"),
        "source_id": item.get("source_id"),
        "sha256": item.get("payload_sha256"),
        "public_content": {
            "kind": content.get("kind"),
            "assay_definition": safe_definition,
            "rows": rows,
        },
    }


def _context_view(context: DecisionContext, decision: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    selected_candidate = decision.get("action", {}).get("candidate_id") if isinstance(decision.get("action"), dict) else None
    candidate_ids = {item.candidate_id for item in context.candidate_contexts if item.candidate_id == selected_candidate}
    for item in decision.get("hypotheses", []):
        if isinstance(item, dict) and isinstance(item.get("candidate_id"), str):
            candidate_ids.add(item["candidate_id"])
    for item in decision.get("interpretations", []):
        if isinstance(item, dict) and isinstance(item.get("candidate_id"), str):
            candidate_ids.add(item["candidate_id"])
    for item in decision.get("prior_updates", []):
        if isinstance(item, dict):
            observation_ids = set(item.get("observation_refs", []))
            for observation in context.public_observations:
                if observation.observation_id in observation_ids:
                    candidate_ids.add(observation.candidate_id)
    observation_ids = {
        item.observation_id for item in context.public_observations
        if item.assay_id == "mep2-confirmatory"
        and (item.candidate_id in candidate_ids or item.observation_id in context.newly_released_observation_ids)
    }
    candidate_map = {item.candidate_id: item.model_dump(mode="json") for item in context.candidate_contexts}
    observed = [
        item.model_dump(mode="json") for item in context.public_observations
        if item.assay_id == "mep2-confirmatory" and item.observation_id in observation_ids
    ]
    assay_map = {item.assay_id: item.model_dump(mode="json") for item in context.assay_context}
    evidence_ids = set(decision.get("basis_evidence_refs", []))
    for item in decision.get("hypotheses", []):
        if isinstance(item, dict):
            evidence_ids.update(ref for ref in item.get("evidence_refs", []) if isinstance(ref, str))
    for item in decision.get("prior_updates", []):
        if isinstance(item, dict):
            evidence_ids.update(ref for ref in item.get("evidence_refs", []) if isinstance(ref, str))
    for item in decision.get("interpretations", []):
        if isinstance(item, dict):
            evidence_ids.update(ref for ref in item.get("evidence_refs", []) if isinstance(ref, str))
    evidence: list[dict[str, Any]] = []
    for item in context.evidence_catalog:
        if item.evidence_id in evidence_ids:
            evidence.append(_public_evidence(
                item.model_dump(mode="json"), candidate_ids=candidate_ids,
                observation_ids=observation_ids,
            ))
    view = {
        "state_version": context.public_state_version,
        "public_as_of": context.public_as_of,
        "context_digest": context.context_digest,
        "research_goal": context.research_goal,
        "assays": [assay_map[item] for item in {"mep2-primary", "mep2-confirmatory"} if item in assay_map],
        "candidates": [candidate_map[item] for item in sorted(candidate_ids) if item in candidate_map],
        "observations": observed,
        "evidence": evidence,
    }
    return view, evidence


def _safe_decision(row: dict[str, Any], context_raw: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    context = DecisionContext.model_validate(context_raw)
    result = row.get("result")
    if not isinstance(result, dict) and isinstance(row.get("result_json"), str):
        result = json.loads(row["result_json"])
    if isinstance(result, dict) and isinstance(result.get("decision"), dict):
        decision_model = validate_decision(result["decision"], context)
        decision = decision_model.model_dump(mode="json")
    else:
        decision = None
    context_view, evidence = _context_view(context, decision or {})
    doc = {
        "decision_id": _str(row.get("decision_id")),
        "decision_no": row.get("decision_no"),
        "decision_mode": context.decision_mode,
        "status": _str(row.get("status"), "unknown"),
        "controller_validation": "applied" if row.get("status") == "applied" and decision is not None else "not_applied",
        "action_step_no": row.get("action_step_no"),
        "selected_candidate_id": row.get("selected_candidate_id"),
        "selected_assay_id": row.get("selected_assay_id"),
        "state_version": row.get("state_version"),
        "context": context_view,
        "decision": decision,
    }
    return doc, evidence


def _project_from_rows(
    *, run_id: str, origin: str, service_status: str, configuration: dict[str, Any],
    summary: dict[str, Any], decisions: list[dict[str, Any]], calls: list[dict[str, Any]],
    steps: list[dict[str, Any]], hypotheses: list[dict[str, Any]], history: list[dict[str, Any]],
    interpretations: list[dict[str, Any]], interpretation_status: dict[str, Any],
    contexts: dict[int, dict[str, Any]], created_at: str | None = None,
    started_at: str | None = None, ended_at: str | None = None,
) -> dict[str, Any]:
    scientific = configuration.get("scientific", {})
    campaign = configuration.get("campaign") or configuration.get("snapshot_id")
    campaign_map = {
        "revision-20260917-r2": "revision-20260917-r2",
        "revision-20260918-primary-active-all": "revision-20260918-primary-active-all",
    }
    source_campaign = campaign_map.get(str(campaign))
    if source_campaign is None:
        raise ValueError("scientific projection references an unsupported campaign")
    run_config = {
        "campaign": source_campaign,
        "research_goal": "AID 2016의 primary Active 후보를 AID 2272 yeast TOR-pathway GFP confirmatory assay의 공개 범주형 결과로 평가합니다. 직접 결합이나 치료 효능 주장이 아닙니다.",
        "budget": str((configuration.get("initial_budget") or {}).get("amount", configuration.get("budget", "5"))),
        "budget_unit": str((configuration.get("initial_budget") or {}).get("unit", "synthetic_credit")),
        "max_steps": int(configuration.get("max_steps", 10)),
        "max_duration_seconds": int(configuration.get("max_duration_seconds", 300)),
        "max_llm_calls": int(scientific.get(
            "max_llm_calls", configuration.get("science_max_llm_calls", configuration.get("max_llm_calls", 24)),
        )),
        "shortlist_size": int(scientific.get(
            "shortlist_size", configuration.get("science_shortlist_size", configuration.get("shortlist_size", 24)),
        )),
        "shortlist_seed": int(scientific.get(
            "shortlist_seed", configuration.get("science_shortlist_seed", configuration.get("shortlist_seed", 3)),
        )),
        "selector": "scientific_reasoner",
    }
    candidate_history: dict[str, dict[str, Any]] = {}
    projected_decisions: list[dict[str, Any]] = []
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for row in sorted(decisions, key=lambda item: int(item.get("decision_no", 0))):
        decision_no = int(row.get("decision_no", 0))
        context = contexts.get(decision_no)
        if not isinstance(context, dict):
            continue
        result_doc, evidence = _safe_decision(row, context)
        projected_decisions.append(result_doc)
        for candidate in result_doc["context"]["candidates"]:
            candidate_history[candidate["candidate_id"]] = candidate
        for item in evidence:
            evidence_by_id[item["evidence_id"]] = item

    observation_ids_by_step: dict[int, list[str]] = {}
    for raw_context in contexts.values():
        if not isinstance(raw_context, dict):
            continue
        context_model = DecisionContext.model_validate(raw_context)
        for attempt in context_model.public_attempts:
            if attempt.status == "released" and attempt.observation_ids:
                observation_ids_by_step[attempt.step_no] = list(attempt.observation_ids)

    step_by_decision = {
        item.get("scientific_decision_id"): item for item in steps
        if isinstance(item.get("scientific_decision_id"), str)
    }
    for item in projected_decisions:
        step = step_by_decision.get(item["decision_id"])
        if step:
            item["execution"] = {
                "step_no": step.get("step_no"),
                "candidate_id": step.get("candidate_id"),
                "assay_id": step.get("assay_id"),
                "status": step.get("status"),
                "observation_ids": observation_ids_by_step.get(int(step.get("step_no", 0)), []),
                "budget_after_step": {
                    "spent": step.get("budget_spent"),
                    "reserved": step.get("budget_reserved"),
                    "available": step.get("budget_available"),
                    "unit": run_config["budget_unit"],
                },
            }
        else:
            item["execution"] = None

    public_observations: dict[str, dict[str, Any]] = {}
    for row in decisions:
        context = contexts.get(int(row.get("decision_no", 0)))
        if not isinstance(context, dict):
            continue
        context_model = DecisionContext.model_validate(context)
        for observation in context_model.public_observations:
            if observation.assay_id == "mep2-confirmatory":
                public_observations[observation.observation_id] = observation.model_dump(mode="json")
    for item in interpretations:
        interpretation = item.get("interpretation")
        if not isinstance(interpretation, dict) and isinstance(item.get("interpretation_json"), str):
            interpretation = json.loads(item["interpretation_json"])
        if not isinstance(interpretation, dict):
            continue
        observation_id = interpretation.get("observation_id")
        if isinstance(observation_id, str) and observation_id in public_observations:
            public_observations[observation_id]["interpretation"] = {
                "decision_id": item.get("decision_id"),
                "outcome": interpretation.get("outcome"),
                "interpretation": interpretation.get("interpretation"),
                "evidence_refs": interpretation.get("evidence_refs", []),
            }

    safe_calls = []
    token_usage: Counter[str] = Counter()
    reported = unreported = 0
    for row in calls:
        usage = row.get("usage")
        if not isinstance(usage, dict) and isinstance(row.get("usage_json"), str):
            usage = json.loads(row["usage_json"])
        numeric = {
            key: value for key, value in (usage or {}).items()
            if key.endswith("_tokens") and isinstance(value, int) and not isinstance(value, bool) and value >= 0
        }
        if numeric:
            reported += 1
            token_usage.update(numeric)
        else:
            unreported += 1
        status = row.get("status")
        code = row.get("error_code")
        safe_calls.append({
            "call_no": row.get("call_no"),
            "decision_id": row.get("decision_id"),
            "status": status if status in {"completed", "failed"} else "unknown",
            "provider": row.get("provider") if isinstance(row.get("provider"), str) and len(row["provider"]) <= 80 else None,
            "model": row.get("model") if isinstance(row.get("model"), str) and len(row["model"]) <= 200 else None,
            "latency_ms": row.get("latency_ms") if isinstance(row.get("latency_ms"), int) and row["latency_ms"] >= 0 else None,
            "usage": numeric or None,
            "error_code": code if code in _SAFE_CALL_ERRORS else None,
        })

    current_hypotheses = []
    status_counts: dict[str, Counter[str]] = {"assay_activity": Counter(), "data_availability": Counter()}
    for row in hypotheses:
        hypothesis = row.get("hypothesis")
        if not isinstance(hypothesis, dict) and isinstance(row.get("hypothesis_json"), str):
            hypothesis = json.loads(row["hypothesis_json"])
        if not isinstance(hypothesis, dict):
            continue
        kind = hypothesis.get("hypothesis_kind")
        current_hypotheses.append({
            key: hypothesis.get(key) for key in (
                "hypothesis_id", "hypothesis_kind", "candidate_id", "assay_id",
                "statement", "interpretation", "expected_outcome", "status",
                "last_updated_observation_ids", "last_updated_evidence_refs", "limitations",
            ) if key in hypothesis
        } | {"updated_decision_id": row.get("updated_decision_id")})
        if kind in status_counts:
            status_counts[kind][str(hypothesis.get("status", "unknown"))] += 1
    safe_history = []
    for row in history:
        event = row.get("event")
        if not isinstance(event, dict) and isinstance(row.get("event_json"), str):
            event = json.loads(row["event_json"])
        safe_history.append({
            "event_no": row.get("event_no"), "decision_id": row.get("decision_id"),
            "hypothesis_id": row.get("hypothesis_id"), "event_type": row.get("event_type"),
            "previous_status": row.get("previous_status"), "new_status": row.get("new_status"),
            "event": {key: event.get(key) for key in (
                "hypothesis_kind", "candidate_id", "assay_id", "statement", "interpretation",
                "expected_outcome", "status", "last_updated_observation_ids", "last_updated_evidence_refs",
            ) if isinstance(event, dict) and key in event},
        })

    observations = list(public_observations.values())
    outcomes = Counter(str(item.get("verdict", "unknown")) for item in observations)
    step_counts = Counter(str(row.get("status", "unknown")) for row in steps)
    run_metrics = summary.get("scientific_metrics") if isinstance(summary.get("scientific_metrics"), dict) else {}
    budget = run_metrics.get("replay_budget") if isinstance(run_metrics.get("replay_budget"), dict) else {}
    if not budget:
        budget = {key: summary.get(key) for key in ("spent", "reserved", "available", "unit")}
    state = interpretation_status or {}
    pending = state.get("pending_observation_ids", [])
    if not isinstance(pending, list):
        pending = []
    # Only released public observations count as interpreted observations.
    # The controller may also persist no_record interpretation decisions; those
    # describe missing replay records and must not inflate this scientific
    # observation metric.
    interpreted_ids = {
        observation_id for observation_id, observation in public_observations.items()
        if isinstance(observation.get("interpretation"), dict)
    }
    no_record = step_counts.get("no_record", 0)
    max_calls = run_config["max_llm_calls"]
    projection = {
        "schema_version": SCHEMA_VERSION,
        "origin": origin,
        "run_id": run_id,
        "service_status": service_status,
        "domain_status": summary.get("status"),
        "stop_reason": summary.get("stop_reason") if summary.get("stop_reason") in _SAFE_STOP_REASONS else None,
        "created_at": created_at,
        "started_at": started_at,
        "ended_at": ended_at,
        "configuration": run_config,
        "summary": {
            "action_steps": int(summary.get("selection_steps", len(steps)) or 0),
            "max_steps": run_config["max_steps"],
            "llm_calls": {"used": len(calls), "limit": max_calls},
            "public_observations": len(observations),
            "interpretation_complete": bool(state.get("interpretation_complete", False)),
            "interpreted_observations": len(interpreted_ids),
            "pending_interpretations": len(pending),
            "no_observations_to_interpret": bool(run_metrics.get("no_observations_to_interpret", False)),
            "verdict_counts": {key: outcomes.get(key, 0) for key in ("active", "inactive", "inconclusive", "unspecified")},
            "no_record_replay_executions": int(summary.get("no_record_executions", no_record) or 0),
            "replay_executions": int(summary.get("unique_executions", len(steps)) or 0),
            "hypotheses": {kind: {"count": sum(counts.values()), "by_status": dict(counts)} for kind, counts in status_counts.items()},
            "replay_budget": budget,
            "api_token_usage": {"by_field": dict(token_usage) if token_usage else None, "reported_calls": reported, "unreported_calls": unreported},
            "termination_reason": summary.get("stop_reason") if summary.get("stop_reason") in _SAFE_STOP_REASONS else None,
        },
        "decisions": projected_decisions,
        "observations": observations,
        "evidence": list(evidence_by_id.values()),
        "hypotheses": current_hypotheses,
        "hypothesis_history": safe_history,
        "api_calls": safe_calls,
        "download_url": None,
        "stored_record": origin == "stored_actual_run",
        "stored_label": "저장된 실제 실행 기록" if origin == "stored_actual_run" else None,
    }
    return projection


def empty_projection(
    *, run_id: str, service_status: str, configuration: dict[str, Any],
    created_at: str | None, started_at: str | None, ended_at: str | None,
    stop_reason: str | None, error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Safe initial response before the first consistent DB revision exists."""
    config = {
        key: configuration[key] for key in (
            "campaign", "selector", "budget", "budget_unit", "max_steps",
            "max_duration_seconds", "max_llm_calls", "shortlist_size", "shortlist_seed",
            "research_scope",
        ) if key in configuration
    }
    budget = str(config.get("budget", "5"))
    unit = config.get("budget_unit", "synthetic_credit")
    return {
        "schema_version": SCHEMA_VERSION, "origin": "live_web_run", "run_id": run_id,
        "service_status": service_status, "domain_status": None,
        "stop_reason": stop_reason if stop_reason in _SAFE_STOP_REASONS else None,
        "created_at": created_at, "started_at": started_at, "ended_at": ended_at,
        "configuration": config,
        "summary": {
            "action_steps": 0, "max_steps": config.get("max_steps", 10),
            "llm_calls": {"used": 0, "limit": config.get("max_llm_calls", 24)},
            "public_observations": 0, "interpretation_complete": False,
            "interpreted_observations": 0, "pending_interpretations": 0,
            "no_observations_to_interpret": False,
            "verdict_counts": {"active": 0, "inactive": 0, "inconclusive": 0, "unspecified": 0},
            "no_record_replay_executions": 0, "replay_executions": 0,
            "hypotheses": {
                "assay_activity": {"count": 0, "by_status": {}},
                "data_availability": {"count": 0, "by_status": {}},
            },
            "replay_budget": {"spent": "0", "reserved": "0", "available": budget, "unit": unit},
            "api_token_usage": {"by_field": None, "reported_calls": 0, "unreported_calls": 0},
            "termination_reason": None,
        },
        "decisions": [], "observations": [], "evidence": [],
        "hypotheses": [], "hypothesis_history": [], "api_calls": [],
        "download_url": None, "stored_record": False, "stored_label": None,
        "error": error,
    }


def project_live_database(
    database: str | Path, *, run_id: str, origin: str, service_status: str,
    configuration: dict[str, Any], created_at: str | None, started_at: str | None,
    ended_at: str | None, stop_reason: str | None = None,
) -> dict[str, Any] | None:
    path = Path(database)
    if not path.is_file():
        return None
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5)
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        needed = {
            "runs", "loop_runs", "loop_steps", "loop_scientific_decisions",
            "loop_scientific_api_calls", "loop_scientific_hypotheses",
            "loop_scientific_hypothesis_events", "loop_scientific_interpretations",
            "loop_scientific_run_state",
        }
        if not needed <= tables:
            db.close()
            return None
        loop = db.execute(
            "SELECT config_json,status,stop_reason FROM loop_runs WHERE run_id=?", (run_id,),
        ).fetchone()
        run = db.execute("SELECT initial_budget,spent,reserved,unit FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if loop is None or run is None:
            db.close()
            return None
        configuration = json.loads(loop["config_json"])
        decisions = []
        for raw in db.execute(
            "SELECT decision_no,decision_id,state_version,context_digest,status,result_json,"
            "selected_candidate_id,selected_assay_id,action_step_no,context_json "
            "FROM loop_scientific_decisions WHERE run_id=? ORDER BY decision_no", (run_id,),
        ):
            row = dict(raw)
            row["result"] = json.loads(row.pop("result_json")) if row.get("result_json") else None
            context = json.loads(row.pop("context_json"))
            row["decision_mode"] = context.get("decision_mode")
            decisions.append((row, context))
        calls = []
        for raw in db.execute(
            "SELECT call_no,decision_id,status,provider,model,latency_ms,usage_json,error_code "
            "FROM loop_scientific_api_calls WHERE run_id=? ORDER BY call_no", (run_id,),
        ):
            row = dict(raw)
            row["usage"] = json.loads(row.pop("usage_json")) if row.get("usage_json") else None
            calls.append(row)
        steps = [dict(row) for row in db.execute(
            "SELECT step_no,scientific_decision_id,candidate_id,assay_id,status,budget_spent,"
            "budget_reserved,budget_available FROM loop_steps WHERE run_id=? ORDER BY step_no", (run_id,),
        )]
        hypotheses = [dict(row) | {"hypothesis": json.loads(row["hypothesis_json"])} for row in db.execute(
            "SELECT hypothesis_id,hypothesis_json,updated_decision_id FROM loop_scientific_hypotheses WHERE run_id=? ORDER BY hypothesis_id", (run_id,),
        )]
        history = [dict(row) | {"event": json.loads(row["event_json"])} for row in db.execute(
            "SELECT event_no,decision_id,hypothesis_id,event_type,previous_status,new_status,event_json "
            "FROM loop_scientific_hypothesis_events WHERE run_id=? ORDER BY event_no", (run_id,),
        )]
        interpretations = [dict(row) | {"interpretation": json.loads(row["interpretation_json"])} for row in db.execute(
            "SELECT observation_id,decision_id,interpretation_json FROM loop_scientific_interpretations WHERE run_id=? ORDER BY observation_id", (run_id,),
        )]
        state_row = db.execute(
            "SELECT interpretation_complete,pending_observation_ids_json FROM loop_scientific_run_state WHERE run_id=?", (run_id,),
        ).fetchone()
        db.close()
        state = {
            "interpretation_complete": bool(state_row["interpretation_complete"]) if state_row else False,
            "pending_observation_ids": json.loads(state_row["pending_observation_ids_json"]) if state_row else [],
        }
        summary = {
            "status": loop["status"], "stop_reason": stop_reason or loop["stop_reason"],
            "selection_steps": len(steps), "unique_executions": len(steps),
            "no_record_executions": sum(item["status"] == "no_record" for item in steps),
            "spent": run["spent"], "reserved": run["reserved"],
            "available": str(Decimal(run["initial_budget"]) - Decimal(run["spent"]) - Decimal(run["reserved"])),
            "unit": run["unit"],
        }
        config = json.loads(loop["config_json"])
        summary["scientific_metrics"] = {"replay_budget": {
            "spent": run["spent"], "reserved": run["reserved"],
            "available": summary["available"], "unit": run["unit"],
        }}
        return _project_from_rows(
            run_id=run_id, origin=origin, service_status=service_status,
            configuration={**configuration, "initial_budget": {"amount": str(run["initial_budget"]), "unit": run["unit"]}},
            summary=summary, decisions=[row for row, _ctx in decisions], calls=calls,
            steps=steps, hypotheses=hypotheses, history=history,
            interpretations=interpretations, interpretation_status=state,
            contexts={row["decision_no"]: ctx for row, ctx in decisions},
            created_at=created_at, started_at=started_at, ended_at=ended_at,
        )
    except (sqlite3.Error, ValueError, TypeError, KeyError, json.JSONDecodeError):
        try:
            db.close()
        except Exception:
            pass
        return None


def project_stored_archive(run_id: str, runtime_root: str | Path) -> dict[str, Any]:
    if not _STORED_ID.fullmatch(run_id):
        raise FileNotFoundError
    root = Path(runtime_root).resolve()
    run_dir = root / run_id
    if run_dir.is_symlink() or not run_dir.is_dir() or run_dir.resolve().parent != root:
        raise FileNotFoundError
    artifacts = run_dir / "artifacts"
    if artifacts.is_symlink() or not artifacts.is_dir():
        raise FileNotFoundError
    revisions = sorted(
        (path for path in artifacts.glob("rev-[0-9]*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: path.name,
    )
    if not revisions:
        raise FileNotFoundError
    target = revisions[-1]
    summary = _read_json(target / "summary.json")
    configuration = _read_json(target / "configuration.json")
    decisions = _read_json(target / "decisions.json")
    calls = _read_json(target / "api_calls.json")
    steps = _read_json(target / "execution_steps.json")
    hypotheses = _read_json(target / "hypotheses.json")
    history = _read_json(target / "hypothesis_history.json")
    interpretations = _read_json(target / "interpretations.json")
    state = _read_json(target / "interpretation_status.json")
    contexts = {}
    context_root = target / "contexts"
    for row in decisions:
        decision_no = int(row.get("decision_no", 0))
        context_path = context_root / f"context-{decision_no:04d}.json"
        contexts[decision_no] = _read_json(context_path)
    first_call = calls[0].get("started_at") if calls else None
    last_call = calls[-1].get("completed_at") if calls else None
    projected = _project_from_rows(
        run_id=run_id, origin="stored_actual_run", service_status="completed",
        configuration=configuration, summary=summary, decisions=decisions, calls=calls,
        steps=steps, hypotheses=hypotheses, history=history, interpretations=interpretations,
        interpretation_status=state, contexts=contexts, started_at=first_call, ended_at=last_call,
    )
    if projected["run_id"] != run_id:
        raise ValueError("stored archive identity mismatch")
    projected["download_url"] = f"/api/scientific-runs/{run_id}/download"
    return projected


def list_stored_runs(runtime_root: str | Path) -> list[dict[str, Any]]:
    try:
        view = project_stored_archive(STORED_RUN_ID, runtime_root)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return []
    return [{
        "run_id": STORED_RUN_ID,
        "service_status": "completed",
        "campaign": view["configuration"]["campaign"],
        "origin": "stored_actual_run",
        "label": "저장된 실제 실행 기록",
        "stop_reason": view.get("stop_reason"),
    }]
