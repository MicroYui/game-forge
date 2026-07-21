from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import TypeAdapter, ValidationError

from gameforge.contracts.api import (
    ExecutionOptionResolveRequestV1,
    ExecutionOptionViewV1,
    ProspectiveAgentRunRequestV1,
    compute_execution_option_id,
    compute_execution_option_request_hash,
    compute_resolved_profile_binding_digests,
)
from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
)
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    ExecutionVersionPlanV1,
    PlannedAgentNodeVersionV1,
    execution_version_plan_digest,
)


_GENERATION_OPERATION = "propose_generation_api_v1_generation_propose_post"
_GENERIC_OPERATION = "submit_run_api_v1_runs_post"


def _review_request() -> dict[str, object]:
    return {
        "request_schema_version": "run-submission-request@1",
        "params": {
            "schema_version": "review-run@1",
            "snapshot_artifact_id": "artifact:snapshot",
            "constraint_snapshot_artifact_id": None,
            "selection": {"mode": "full", "entity_ids": [], "relation_ids": []},
            "review_profile": {"profile_id": "review", "version": 1},
            "checker_profiles": [],
            "simulation_profiles": [],
            "llm_triage_policy": {"profile_id": "triage", "version": 1},
        },
        "llm_execution_mode": "record",
        "seed": None,
        "execution_version_plan": None,
        "cassette_artifact_id": None,
    }


def _generation_request() -> dict[str, object]:
    return {
        "request_schema_version": "generation-propose-request@1",
        "base_snapshot_artifact_id": "artifact:snapshot",
        "constraint_snapshot_artifact_id": None,
        "findings": [],
        "objective_goal_text": "make the tutorial goal precise",
        "domain_scope": {"domain_ids": ["core"]},
        "target": {"ref_name": "refs/spec", "expected_ref": None},
        "generation_policy": {"profile_id": "generation", "version": 1},
        "candidate_export_profiles": [],
        "llm_execution_mode": "record",
        "execution_version_plan": None,
        "cassette_artifact_id": None,
    }


def _plan() -> ExecutionVersionPlanV1:
    body = {
        "agent_graph_version": "generation-graph@1",
        "nodes": (
            PlannedAgentNodeVersionV1(
                agent_node_id="generation",
                prompt_version="generation@1",
                tool_version="generation@1",
                allowed_model_snapshots=("model/provider/snapshot@1",),
            ),
        ),
        "model_catalog_version": 1,
        "model_catalog_digest": "a" * 64,
        "routing_policy_version": 1,
        "routing_policy_digest": "b" * 64,
    }
    return ExecutionVersionPlanV1(
        **body,
        plan_digest=execution_version_plan_digest(body),
    )


def _resolve_request(
    prospective: dict[str, object] | None = None,
) -> ExecutionOptionResolveRequestV1:
    return ExecutionOptionResolveRequestV1.model_validate(
        {
            "request_schema_version": "execution-option-resolve-request@1",
            "resource_operation_id": _GENERIC_OPERATION,
            "run_kind": {"kind": "review.run", "version": 1},
            "llm_execution_mode": "record",
            "prospective_request": prospective or _review_request(),
            "replay_source_run_id": None,
        }
    )


def test_prospective_union_is_exactly_the_five_agent_capable_request_branches() -> None:
    schema = TypeAdapter(ProspectiveAgentRunRequestV1).json_schema()
    discriminator = schema["discriminator"]

    assert discriminator["propertyName"] == "request_schema_version"
    assert set(discriminator["mapping"]) == {
        "run-submission-request@1",
        "generation-propose-request@1",
        "constraint-propose-request@1",
        "patch-repair-request@1",
        "playtest-run-request@1",
    }


@pytest.mark.parametrize("field", ["execution_version_plan", "cassette_artifact_id"])
def test_prospective_request_requires_explicit_null_execution_fields(field: str) -> None:
    payload = _review_request()
    payload.pop(field)

    with pytest.raises(ValidationError):
        _resolve_request(payload)

    payload = _review_request()
    payload[field] = "artifact:not-null"
    with pytest.raises(ValidationError):
        _resolve_request(payload)


def test_generic_prospective_request_excludes_deterministic_generic_run_kinds() -> None:
    payload = _review_request()
    payload["params"] = {
        "schema_version": "checker-run@1",
        "snapshot_artifact_id": "artifact:snapshot",
        "constraint_snapshot_artifact_id": None,
        "selection": {"mode": "full", "entity_ids": [], "relation_ids": []},
        "checker_profile": {"profile_id": "checker", "version": 1},
        "checker_ids": [],
        "defect_classes": [],
    }

    with pytest.raises(ValidationError):
        _resolve_request(payload)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"llm_execution_mode": "live"}, "execution mode"),
        ({"run_kind": {"kind": "bench.run", "version": 1}}, "RunKind"),
        ({"resource_operation_id": _GENERATION_OPERATION}, "operation"),
    ],
)
def test_resolve_request_rejects_mode_kind_and_operation_drift(
    change: dict[str, object],
    match: str,
) -> None:
    payload = _resolve_request().model_dump(mode="json")
    payload.update(change)

    with pytest.raises(ValidationError, match=match):
        ExecutionOptionResolveRequestV1.model_validate(payload)


def test_replay_source_is_required_exactly_for_replay() -> None:
    payload = _resolve_request().model_dump(mode="json")
    payload["replay_source_run_id"] = "run:source"
    with pytest.raises(ValidationError, match="replay source"):
        ExecutionOptionResolveRequestV1.model_validate(payload)

    payload["llm_execution_mode"] = "replay"
    payload["prospective_request"]["llm_execution_mode"] = "replay"
    replay = ExecutionOptionResolveRequestV1.model_validate(payload)
    assert replay.replay_source_run_id == "run:source"

    payload["replay_source_run_id"] = None
    with pytest.raises(ValidationError, match="replay source"):
        ExecutionOptionResolveRequestV1.model_validate(payload)


def test_request_and_option_hashes_are_canonical_and_content_addressed() -> None:
    request = _resolve_request()
    prospective_hash = compute_execution_option_request_hash(request)
    resolved_hash = compute_execution_option_request_hash(
        request,
        execution_version_plan=_plan(),
        cassette_artifact_id=None,
    )
    profile_bindings = (
        ResolvedExecutionProfileBindingV1(
            field_path="/params/review_profile",
            profile=ProfileRefV1(profile_id="review", version=1),
            expected_profile_kind="review",
            profile_payload_hash="d" * 64,
            catalog_version=1,
            catalog_digest="e" * 64,
        ),
        ResolvedExecutionProfileBindingV1(
            field_path="/params/llm_triage_policy",
            profile=ProfileRefV1(profile_id="triage", version=1),
            expected_profile_kind="llm_triage",
            profile_payload_hash="f" * 64,
            catalog_version=1,
            catalog_digest="e" * 64,
        ),
    )
    body = {
        "option_schema_version": "execution-option@1",
        "option_id": "execution-option:sha256:" + "0" * 64,
        "resource_operation_id": request.resource_operation_id,
        "run_kind": request.run_kind.model_dump(mode="json"),
        "domain_scope": {"domain_ids": ["core"]},
        "llm_execution_mode": request.llm_execution_mode,
        "execution_version_plan": _plan().model_dump(mode="json"),
        "prospective_request_hash": prospective_hash,
        "resolved_request_hash": resolved_hash,
        "resolved_profile_binding_digests": list(
            compute_resolved_profile_binding_digests(profile_bindings)
        ),
        "source_run_id": None,
        "cassette_artifact_id": None,
    }
    body["option_id"] = compute_execution_option_id(body)
    view = ExecutionOptionViewV1.model_validate(body)

    assert prospective_hash != resolved_hash
    assert view.option_id == compute_execution_option_id(view)
    assert view.run_kind == RunKindRef(kind="review.run", version=1)
    assert view.domain_scope == DomainScope(domain_ids=("core",))
    assert view.resolved_profile_binding_digests == (
        compute_resolved_profile_binding_digests(profile_bindings)
    )
    assert tuple(view.resolved_profile_binding_digests) == tuple(
        sorted(view.resolved_profile_binding_digests)
    )

    changed = deepcopy(view.model_dump(mode="json"))
    changed["domain_scope"] = {"domain_ids": ["other"]}
    assert compute_execution_option_id(changed) != view.option_id


def test_option_view_requires_exact_replay_source_and_cassette_pair() -> None:
    request = _resolve_request()
    body = {
        "option_schema_version": "execution-option@1",
        "option_id": "execution-option:sha256:" + "0" * 64,
        "resource_operation_id": request.resource_operation_id,
        "run_kind": request.run_kind,
        "domain_scope": DomainScope(domain_ids=("core",)),
        "llm_execution_mode": "record",
        "execution_version_plan": _plan(),
        "prospective_request_hash": "1" * 64,
        "resolved_request_hash": "2" * 64,
        "resolved_profile_binding_digests": ("3" * 64,),
        "source_run_id": "run:source",
        "cassette_artifact_id": None,
    }
    body["option_id"] = compute_execution_option_id(body)

    with pytest.raises(ValidationError, match="source and cassette"):
        ExecutionOptionViewV1.model_validate(body)
