"""Restricted JSON stdio adapter for a trusted, run-bound public reader.

The coordinator stays in the trusted parent. An untrusted client receives only
the finite operations declared here; it cannot supply a run ID or path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import BinaryIO, TextIO

from assaypilot.execution import ExecutionControlError, ExecutionCoordinator


MAX_PUBLIC_REQUEST_BYTES = 4096
# The expanded primary-only campaign is public input to ``state``. The response
# includes that campaign plus the run overlay, so it can exceed a small IPC
# frame while remaining bounded. Keep the cap explicit and comfortably above
# the preserved 1,682-candidate snapshot.
MAX_PUBLIC_RESPONSE_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PublicReader:
    """A read-only API bound to the run selected by trusted code."""

    _coordinator: ExecutionCoordinator
    _run_id: str

    def current_state(self):
        return self._coordinator.get_public_state(self._run_id)

    def current_budget(self):
        return self._coordinator.get_current_budget(self._run_id)

    def released_execution(self, execution_id: str):
        return self._coordinator.get_public_execution(self._run_id, execution_id)

    def evidence(self, evidence_id: str):
        return self._coordinator.get_public_evidence(self._run_id, evidence_id)


def handle_public_request(reader: PublicReader, request: bytes | str) -> dict[str, object]:
    """Handle one size-limited request and return only JSON-safe public data."""
    try:
        encoded = request.encode("utf-8") if isinstance(request, str) else request
        if not isinstance(encoded, bytes) or len(encoded) > MAX_PUBLIC_REQUEST_BYTES:
            return {"error": "request_rejected"}
        message = json.loads(encoded)
        if not isinstance(message, dict) or not isinstance(message.get("op"), str):
            return {"error": "request_rejected"}
        op = message["op"]
        if op == "state" and set(message) == {"op"}:
            view = reader.current_state()
            return {
                "run_id": view.run_id,
                "state_version": view.state_version,
                "as_of": view.as_of.isoformat(),
                "public": view.public.model_dump(mode="json"),
                "state": view.state.model_dump(mode="json"),
                "released_execution_ids": [item.execution_id for item in view.released_executions],
            }
        if op == "budget" and set(message) == {"op"}:
            budget = reader.current_budget()
            return {
                "total": str(budget.total), "spent": str(budget.spent),
                "reserved": str(budget.reserved), "available": str(budget.available),
                "unit": budget.unit,
            }
        if op == "execution" and set(message) == {"op", "execution_id"}:
            execution_id = _bounded_id(message["execution_id"])
            published = reader.released_execution(execution_id)
            return {
                "execution_id": published.execution_id,
                "receipt": published.receipt.model_dump(mode="json"),
                "result": published.result.model_dump(mode="json"),
                "published_at": published.published_at.isoformat(),
                "state_version": published.state_version,
                "cost": str(published.cost), "unit": published.unit,
            }
        if op == "evidence" and set(message) == {"op", "evidence_id"}:
            evidence_id = _bounded_id(message["evidence_id"])
            evidence = reader.evidence(evidence_id)
            return {
                "evidence_id": evidence.reference.evidence_id,
                "sha256": evidence.sha256,
                "payload": json.loads(evidence.payload),
            }
    except (ValueError, TypeError, UnicodeDecodeError, ExecutionControlError, json.JSONDecodeError):
        pass
    return {"error": "request_rejected"}


def serve_public_stdio(reader: PublicReader, source: BinaryIO, sink: TextIO) -> None:
    """Serve newline-delimited requests until EOF; never logs private errors."""
    while True:
        line = source.readline(MAX_PUBLIC_REQUEST_BYTES + 1)
        if not line:
            return
        if len(line) > MAX_PUBLIC_REQUEST_BYTES or not line.endswith(b"\n"):
            response: dict[str, object] = {"error": "request_rejected"}
        else:
            response = handle_public_request(reader, line[:-1])
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_PUBLIC_RESPONSE_BYTES:
            encoded = '{"error":"response_rejected"}'
        sink.write(encoded + "\n")
        sink.flush()


def _bounded_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("invalid public identifier")
    if any(char in value for char in ("/", "\\", "\x00")):
        raise ValueError("invalid public identifier")
    return value
