from __future__ import annotations

import json
from uuid import uuid4

import pytest

from assaypilot.stage4_service import (
    _ScientificProjectionPublisher,
    Stage4RunService,
    Stage4ServiceError,
    _atomic_json,
    classify_cli_outcome,
    _strip_runtime_locations,
    validate_run_request,
)


def test_run_request_is_strict_and_decimal_budget_is_canonical() -> None:
    valid = validate_run_request({
        "campaign": "revision-20260917-r2", "selector": "fixed_order",
        "budget": "5.000000", "max_steps": 5,
    })
    assert valid["budget"] == "5"
    assert valid["max_steps"] == 5

    for invalid in (
        {"campaign": "../../snapshot", "selector": "fixed_order", "budget": "5", "max_steps": 1},
        {"campaign": "revision-20260917-r2", "selector": [], "budget": "5", "max_steps": 1},
        {"campaign": "revision-20260917-r2", "selector": "fixed_order", "budget": "NaN", "max_steps": 1},
        {"campaign": "revision-20260917-r2", "selector": "fixed_order", "budget": "51", "max_steps": 1},
        {"campaign": "revision-20260917-r2", "selector": "seeded_random_priority", "budget": "5", "max_steps": 1},
    ):
        with pytest.raises(Stage4ServiceError):
            validate_run_request(invalid)


def test_scientific_run_request_preserves_bounded_defaults_and_seed_range() -> None:
    valid = validate_run_request({"mode": "scientific_reasoner"})
    assert valid["campaign"] == "revision-20260918-primary-active-all"
    assert valid["budget"] == "5"
    assert valid["max_steps"] == 10
    assert valid["max_duration_seconds"] == 300
    assert valid["max_llm_calls"] == 24
    assert valid["shortlist_size"] == 24
    assert valid["shortlist_seed"] == 3
    with pytest.raises(Stage4ServiceError) as negative_seed:
        validate_run_request({"mode": "scientific_reasoner", "shortlist_seed": -1})
    assert negative_seed.value.code == "invalid_shortlist_seed"


def test_cli_stop_reason_is_separate_from_service_state() -> None:
    completed = classify_cli_outcome(0, json.dumps({
        "run_id": "stage4-example", "status": "stopped", "stop_reason": "budget_exhausted",
    }), "")
    assert completed["service_status"] == "completed"
    assert completed["stop_reason"] == "budget_exhausted"

    failed = classify_cli_outcome(2, "", '{"error":"selector_sandbox_unavailable","message":"private path"}')
    assert failed["service_status"] == "failed"
    assert failed["error"] == {
        "code": "selector_sandbox_unavailable",
        "message": "기존 replay 실행기가 오류로 종료했습니다.",
    }


def test_public_evidence_reference_does_not_export_runtime_location() -> None:
    archive = {"executions": [{"evidence": [{
        "reference": {
            "evidence_id": "evidence-1", "source_kind": "pubchem",
            "source_id": "row-1", "location": "runtime/secret/evidence-1.json",
        },
        "payload": {"protocol_location": "https://pubchem.ncbi.nlm.nih.gov/bioassay/1"},
    }]}]}
    _strip_runtime_locations(archive)
    assert archive["executions"][0]["evidence"][0]["reference"] == {
        "evidence_id": "evidence-1", "source_kind": "pubchem", "source_id": "row-1",
    }
    assert archive["executions"][0]["evidence"][0]["payload"]["protocol_location"].startswith("https://")


def test_run_creation_is_idempotent_and_single_concurrency(tmp_path) -> None:
    service = Stage4RunService(
        tmp_path / "runtime",
        readiness_probe=lambda: {"available": True, "code": None},
        worker_launcher=lambda _run_id, _resume: 999_999_999,
    )
    service.readiness = lambda **_kwargs: {
        "ready": True,
        "campaigns": [
            {"campaign": name, "available": True} for name in (
                "revision-20260917-r2", "revision-20260918-primary-active-all",
            )
        ],
        "selector_sandbox": {"available": True, "code": None},
    }
    payload = {
        "campaign": "revision-20260917-r2", "selector": "fixed_order",
        "budget": "5", "max_steps": 5,
    }
    key = str(uuid4())
    first, reused_first = service.start_run(payload, key)
    second, reused_second = service.start_run(payload, key)
    assert first["run_id"] == second["run_id"]
    assert reused_first is False
    assert reused_second is True
    with pytest.raises(Stage4ServiceError) as key_conflict:
        service.start_run({**payload, "budget": "4"}, key)
    assert key_conflict.value.code == "idempotency_conflict"
    with pytest.raises(Stage4ServiceError) as conflict:
        service.start_run(payload, str(uuid4()))
    assert conflict.value.code == "concurrent_run_limit"
    with pytest.raises(Stage4ServiceError) as invalid:
        service.start_run({**payload, "campaign": "../hidden"}, str(uuid4()))
    assert invalid.value.code == "invalid_campaign"


def test_worker_failure_does_not_fabricate_public_artifact(tmp_path, monkeypatch) -> None:
    import assaypilot.stage4_service as service_module

    runtime = tmp_path / "runtime"
    service = Stage4RunService(runtime)
    run_id = "stage4-" + "a" * 32
    run_dir = service.runs_root / run_id
    run_dir.mkdir(mode=0o700)
    _atomic_json(run_dir / "run.json", {
        "schema_version": "assaypilot.stage4.run-state.v1", "run_id": run_id,
        "service_status": "queued", "domain_status": None, "stop_reason": None,
        "configuration": {
            "campaign": "revision-20260917-r2", "selector": "fixed_order",
            "seed": None, "selector_algorithm_version": None, "budget": "5",
            "budget_unit": "synthetic_credit", "budget_assumed": True,
            "max_steps": 1, "max_duration_seconds": 300,
        },
        "configuration_sha256": "0" * 64, "created_at": "2026-10-01T00:00:00+00:00",
        "created_epoch": 0, "started_at": None, "ended_at": None,
        "worker_pid": None, "resume_count": 0, "artifact_revision": None, "error": None,
    })

    class FailedProcess:
        returncode = 2

        def poll(self):
            return 2

    monkeypatch.setattr(service_module.subprocess, "Popen", lambda *_args, **_kwargs: FailedProcess())
    assert service_module.run_worker(runtime, run_id) == 1
    response = service.get_run(run_id)
    assert response["service_status"] == "failed"
    assert response["error"]["code"] == "worker_failed"
    assert response["published_results"] == []
    assert response["download_url"] is None


def test_scientific_resume_does_not_require_selector_worker_sandbox(tmp_path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    launch_calls = []
    service = Stage4RunService(
        runtime, worker_launcher=lambda run_id, resume: launch_calls.append((run_id, resume)) or 42,
    )
    run_id = "stage4-" + "b" * 32
    run_dir = service.runs_root / run_id
    run_dir.mkdir(mode=0o700)
    _atomic_json(run_dir / "run.json", {
        "schema_version": "assaypilot.stage4.run-state.v1", "run_id": run_id,
        "service_status": "interrupted", "domain_status": None, "stop_reason": "user_interrupt",
        "configuration": {
            "campaign": "revision-20260918-primary-active-all", "selector": "scientific_reasoner",
            "budget": "5", "budget_unit": "synthetic_credit", "max_steps": 10,
            "max_duration_seconds": 300, "max_llm_calls": 24, "shortlist_size": 24,
            "shortlist_seed": 3,
        },
        "created_at": "2026-10-01T00:00:00+00:00", "created_epoch": 0,
        "started_at": "2026-10-01T00:00:01+00:00", "ended_at": "2026-10-01T00:01:00+00:00",
        "worker_pid": None, "resume_count": 0, "artifact_revision": None, "error": None,
    })
    service.readiness = lambda **_kwargs: {
        "ready": False,
        "campaigns": [{"campaign": "revision-20260918-primary-active-all", "available": True}],
        "selector_sandbox": {"available": False, "code": "selector_sandbox_unavailable"},
        "scientific_reasoner": {"provider_settings_configured": True},
    }
    monkeypatch.setattr(service, "_resume_available", lambda _run_id, _run_dir: True)
    monkeypatch.setattr(service, "_pid_alive", lambda _pid, _run_id: True)

    result = service.resume_run(run_id)

    assert result["service_status"] == "queued"
    assert launch_calls == [(run_id, True)]


def test_scientific_projection_publisher_appends_after_existing_revision(tmp_path, monkeypatch) -> None:
    import assaypilot.scientific_public as public_module

    service = Stage4RunService(tmp_path / "runtime")
    run_id = "stage4-" + "c" * 32
    run_dir = service.runs_root / run_id
    run_dir.mkdir(mode=0o700)
    configuration = {
        "campaign": "revision-20260918-primary-active-all",
        "selector": "scientific_reasoner", "budget": "5",
        "budget_unit": "synthetic_credit", "max_steps": 10,
        "max_duration_seconds": 300, "max_llm_calls": 24,
        "shortlist_size": 24, "shortlist_seed": 3,
    }
    _atomic_json(run_dir / "run.json", {
        "run_id": run_id, "service_status": "completed",
        "configuration": configuration, "created_at": "2026-10-02T00:00:00+00:00",
    })
    old_revision = run_dir / "public" / "rev-000001"
    _atomic_json(old_revision / "projection.json", {"run_id": run_id, "revision": 1})
    monkeypatch.setattr(public_module, "project_live_database", lambda *_args, **_kwargs: {
        "run_id": run_id, "service_status": "completed", "decisions": [],
    })

    publisher = _ScientificProjectionPublisher(service, run_id)
    assert publisher.revision == 1
    assert publisher.refresh(force=True, service_status="completed") is True

    assert (old_revision / "projection.json").read_text(encoding="utf-8") == json.dumps(
        {"run_id": run_id, "revision": 1}, ensure_ascii=False, sort_keys=True, indent=2,
    ) + "\n"
    assert json.loads((run_dir / "public" / "rev-000002" / "projection.json").read_text())[
        "decisions"
    ] == []
    assert service._read_state(run_dir)["public_revision"] == 2
