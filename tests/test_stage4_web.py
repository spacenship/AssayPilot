from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from assaypilot.stage4_service import Stage4RunService
from assaypilot.stage4_web import Stage4Handler


def test_scientific_http_run_creation_is_idempotent_and_get_is_read_only(tmp_path) -> None:
    launches = []
    service = Stage4RunService(
        tmp_path / "runtime",
        worker_launcher=lambda run_id, resume: launches.append((run_id, resume)) or 999_999_999,
    )
    service.readiness = lambda **_kwargs: {
        "ready": False,
        "campaigns": [{
            "campaign": "revision-20260918-primary-active-all", "available": True,
        }],
        "selector_sandbox": {"available": False, "code": "selector_sandbox_unavailable"},
        "scientific_reasoner": {"available": True, "provider_settings_configured": True},
    }
    key = str(uuid4())
    payload = {"mode": "scientific_reasoner"}
    handler = object.__new__(Stage4Handler)
    handler.server = SimpleNamespace(service=service)
    handler.path = "/api/runs"
    handler.headers = {"Idempotency-Key": key}
    handler._body = lambda: payload
    responses = []
    handler._json = lambda status, value: responses.append((status, value))
    handler._error = lambda error: responses.append((error.http_status, {"error": error.code}))

    handler.do_POST()
    assert responses[-1][0] == 202
    created = responses[-1][1]
    run_id = created["run"]["run_id"]
    assert created["run"]["configuration"]["max_llm_calls"] == 24
    assert launches == [(run_id, False)]

    handler.path = f"/api/runs/{run_id}"
    handler.do_GET()
    first_read = responses[-1][1]
    assert first_read["run_id"] == run_id
    assert first_read["service_status"] == "queued"
    assert launches == [(run_id, False)]

    handler.path = "/api/runs"
    handler.do_POST()
    assert responses[-1][0] == 200
    replay = responses[-1][1]
    assert replay["idempotent_replay"] is True
    assert replay["run"]["run_id"] == run_id
    assert launches == [(run_id, False)]
