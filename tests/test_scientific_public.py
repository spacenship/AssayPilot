from __future__ import annotations

from types import SimpleNamespace

import assaypilot.scientific_public as public


def test_projection_links_released_observations_by_attempt_id(monkeypatch) -> None:
    class FakeContext:
        @classmethod
        def model_validate(cls, raw):
            attempts = [SimpleNamespace(**item) for item in raw.get("public_attempts", [])]
            return SimpleNamespace(public_attempts=attempts, public_observations=[])

    monkeypatch.setattr(public, "DecisionContext", FakeContext)
    monkeypatch.setattr(public, "_safe_decision", lambda _row, _context: ({
        "decision_id": "decision-1", "decision_no": 1,
        "context": {"candidates": []}, "selected_candidate_id": "candidate-1",
    }, []))

    projected = public._project_from_rows(
        run_id="stage4-" + "a" * 32,
        origin="live_web_run",
        service_status="running",
        configuration={
            "campaign": "revision-20260918-primary-active-all",
            "science_max_llm_calls": 17,
            "science_shortlist_size": 12,
            "science_shortlist_seed": 9,
        },
        summary={"selection_steps": 1, "scientific_metrics": {}},
        decisions=[{"decision_no": 1, "decision_id": "decision-1", "status": "applied"}],
        calls=[],
        steps=[{
            "scientific_decision_id": "decision-1", "step_no": 1,
            "candidate_id": "candidate-1", "assay_id": "mep2-confirmatory",
            "status": "released", "budget_spent": "1", "budget_reserved": "0",
            "budget_available": "4",
        }],
        hypotheses=[], history=[], interpretations=[], interpretation_status={},
        contexts={1: {"public_attempts": [{
            "step_no": 1, "candidate_id": "candidate-1", "assay_id": "mep2-confirmatory",
            "status": "released", "observation_ids": ["observation-exact-1"],
        }]}},
    )

    execution = projected["decisions"][0]["execution"]
    assert execution["observation_ids"] == ["observation-exact-1"]
    assert projected["configuration"]["max_llm_calls"] == 17
    assert projected["configuration"]["shortlist_size"] == 12
    assert projected["configuration"]["shortlist_seed"] == 9


def test_no_record_interpretations_do_not_inflate_public_observation_count(monkeypatch) -> None:
    observation = {
        "observation_id": "observation-public-1",
        "candidate_id": "candidate-1",
        "assay_id": "mep2-confirmatory",
        "verdict": "inactive",
        "evidence_refs": [],
    }

    class FakeContext:
        @classmethod
        def model_validate(cls, raw):
            return SimpleNamespace(
                public_attempts=[],
                public_observations=[SimpleNamespace(
                    observation_id=observation["observation_id"],
                    assay_id=observation["assay_id"],
                    model_dump=lambda mode: observation,
                )],
            )

    monkeypatch.setattr(public, "DecisionContext", FakeContext)
    monkeypatch.setattr(public, "_safe_decision", lambda _row, _context: ({
        "decision_id": "decision-1", "decision_no": 1,
        "context": {"candidates": []}, "selected_candidate_id": None,
    }, []))

    projected = public._project_from_rows(
        run_id="stage4-" + "b" * 32,
        origin="live_web_run",
        service_status="completed",
        configuration={"campaign": "revision-20260918-primary-active-all"},
        summary={"selection_steps": 0, "scientific_metrics": {}},
        decisions=[{"decision_no": 1, "decision_id": "decision-1", "status": "applied"}],
        calls=[], steps=[], hypotheses=[], history=[],
        interpretations=[
            {"observation_id": "observation-public-1", "interpretation": {
                "observation_id": "observation-public-1",
                "outcome": "inactive", "interpretation": "The public observation is Inactive.",
                "evidence_refs": [],
            }},
            {"observation_id": "no-record-attempt-1", "interpretation": {
                "observation_id": "no-record-attempt-1",
                "outcome": "no_record", "interpretation": "No replay record was linked.",
                "evidence_refs": [],
            }},
        ],
        interpretation_status={"interpretation_complete": True, "pending_observation_ids": []},
        contexts={1: {"public_attempts": [], "public_observations": [observation]}},
    )

    assert projected["summary"]["public_observations"] == 1
    assert projected["summary"]["interpreted_observations"] == 1
    assert projected["observations"][0]["interpretation"]["outcome"] == "inactive"
