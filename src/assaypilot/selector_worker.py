"""Stdlib-only worker for isolated Stage 3-A/3-B baseline selectors."""
from __future__ import annotations

import hashlib
import json
import sys


MAX_LINE_BYTES = 2 * 1024 * 1024
MAX_REASON_LENGTH = 240
RANDOM_PRIORITY_VERSION = "random-priority-v1"


def _line() -> dict[str, object] | None:
    raw = sys.stdin.buffer.readline(MAX_LINE_BYTES + 2)
    if not raw:
        return None
    if len(raw) > MAX_LINE_BYTES or not raw.endswith(b"\n"):
        raise ValueError("input_limit")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("object_required")
    return value


def _write(value: dict[str, object]) -> None:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(raw + b"\n")
    sys.stdout.buffer.flush()


def _init(message: dict[str, object]) -> tuple[set[str], set[str], str, int | None, str | None]:
    required = {
        "kind", "schema_version", "candidates", "assays", "observations",
        "selector_kind", "selector_seed", "selector_algorithm_version",
    }
    if (set(message) != required
            or message.get("kind") != "init"
            or message.get("schema_version") != "assaypilot.selector-view.v1"):
        raise ValueError("init_schema")
    selector_kind = message.get("selector_kind")
    seed = message.get("selector_seed")
    algorithm_version = message.get("selector_algorithm_version")
    if selector_kind == "fixed_order":
        if seed is not None or algorithm_version is not None:
            raise ValueError("selector_config")
    elif selector_kind == "seeded_random_priority":
        if (isinstance(seed, bool) or not isinstance(seed, int)
                or algorithm_version != RANDOM_PRIORITY_VERSION):
            raise ValueError("selector_config")
    else:
        raise ValueError("selector_kind")
    candidates = message.get("candidates")
    assays = message.get("assays")
    observations = message.get("observations")
    if not isinstance(candidates, list) or not isinstance(assays, list) or not isinstance(observations, list):
        raise ValueError("catalog_schema")
    candidate_ids = {item.get("candidate_id") for item in candidates if isinstance(item, dict)}
    assay_ids = {item.get("assay_id") for item in assays if isinstance(item, dict)}
    if len(candidate_ids) != len(candidates) or len(assay_ids) != len(assays):
        raise ValueError("catalog_identity")
    observation_ids = {item.get("observation_id") for item in observations if isinstance(item, dict)}
    if len(observation_ids) != len(observations):
        raise ValueError("observation_identity")
    return candidate_ids, assay_ids, selector_kind, seed, algorithm_version


def _priority_key(seed: int, candidate_id: str, assay_id: str, algorithm_version: str):
    canonical = json.dumps(
        [algorithm_version, seed, candidate_id, assay_id],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).digest(), candidate_id, assay_id


def _select(
    message: dict[str, object], candidate_ids: set[str], assay_ids: set[str],
    seen_observations: set[str], selector_kind: str, seed: int | None,
    algorithm_version: str | None,
):
    required = {
        "kind", "state_version", "public_as_of", "new_public_observations", "budget",
        "executable_actions", "attempted_actions",
    }
    if set(message) != required or message.get("kind") != "select":
        raise ValueError("select_schema")
    new_observations = message.get("new_public_observations")
    actions = message.get("executable_actions")
    attempted = message.get("attempted_actions")
    budget = message.get("budget")
    if not isinstance(new_observations, list) or not isinstance(actions, list):
        raise ValueError("dynamic_schema")
    if not isinstance(attempted, list) or not isinstance(budget, dict):
        raise ValueError("dynamic_schema")
    for observation in new_observations:
        if not isinstance(observation, dict):
            raise ValueError("observation_schema")
        identity = observation.get("observation_id")
        if not isinstance(identity, str) or identity in seen_observations:
            raise ValueError("observation_identity")
        if observation.get("candidate_id") not in candidate_ids or observation.get("assay_id") not in assay_ids:
            raise ValueError("observation_reference")
        seen_observations.add(identity)

    if not actions:
        return {"kind": "stop", "stop_reason": "no_executable_actions"}
    checked: list[tuple[str, str]] = []
    for action in actions:
        if not isinstance(action, dict) or set(action) != {
            "candidate_id", "assay_id", "cost_amount", "cost_unit",
        }:
            raise ValueError("action_schema")
        candidate_id, assay_id = action["candidate_id"], action["assay_id"]
        if candidate_id not in candidate_ids or assay_id not in assay_ids:
            raise ValueError("action_reference")
        checked.append((candidate_id, assay_id))
    if selector_kind == "fixed_order":
        candidate_id, assay_id = min(checked)
        reason = "first executable action in stable (candidate_id, assay_id) order"
    else:
        assert seed is not None and algorithm_version == RANDOM_PRIORITY_VERSION
        candidate_id, assay_id = min(
            checked,
            key=lambda action: _priority_key(seed, action[0], action[1], algorithm_version),
        )
        reason = "first eligible action by the fixed seeded SHA-256 priority"
    return {
        "kind": "select",
        "candidate_id": candidate_id,
        "assay_id": assay_id,
        "reason": reason,
    }


def main() -> int:
    try:
        init = _line()
        if init is None:
            return 0
        candidate_ids, assay_ids, selector_kind, seed, algorithm_version = _init(init)
        seen_observations = {
            item["observation_id"] for item in init["observations"]
        }
        _write({"ready": True})
        while True:
            message = _line()
            if message is None:
                return 0
            _write(_select(
                message, candidate_ids, assay_ids, seen_observations,
                selector_kind, seed, algorithm_version,
            ))
    except Exception:
        # The controller reports a bounded process/schema code, never a traceback
        # or data-bearing exception string to the selector caller.
        try:
            _write({"error": "selector_protocol_error"})
        except Exception:
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
