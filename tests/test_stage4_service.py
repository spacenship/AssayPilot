from __future__ import annotations

import json
from uuid import uuid4

import pytest

from assaypilot.stage4_service import (
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
