from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import (
    ApiDependencies,
    WorkflowCommand,
    WorkflowCommandResult,
    require_actor,
)
from gameforge.contracts.api import RunAcceptedV1
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import InvalidStateTransition
from gameforge.contracts.identity import ActorContext, AuthenticationContext, Principal
from gameforge.contracts.jobs import Problem


def _actor() -> ActorContext:
    principal = Principal(
        id="human:author",
        kind="human",
        display_name="Author",
        status="active",
        revision=3,
        credential_epoch=1,
        authz_revision=4,
        roles=(),
    )
    return ActorContext(
        principal=principal,
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="credential:author",
        ),
        session_id="session:author",
        request_id="request:authn",
    )


@dataclass
class _Commands:
    commands: list[WorkflowCommand] = field(default_factory=list)
    fail: bool = False

    def execute(self, command: WorkflowCommand) -> WorkflowCommandResult:
        self.commands.append(command)
        if self.fail:
            raise InvalidStateTransition("private workflow state")
        return WorkflowCommandResult(
            value=RunAcceptedV1(
                run_id="run:validation:1",
                status_url="/api/v1/runs/run:validation:1",
                events_url="/api/v1/runs/run:validation:1/events",
            ),
            resource_kind="run",
            resource_id="run:validation:1",
            revision=1,
        )


@dataclass
class _SpecCommands:
    commands: list[WorkflowCommand] = field(default_factory=list)

    def execute(self, command: WorkflowCommand) -> WorkflowCommandResult:
        self.commands.append(command)
        return WorkflowCommandResult(
            value={
                "view_schema_version": "spec-view@1",
                "artifact": {
                    "summary_schema_version": "artifact-summary@1",
                    "artifact_id": "artifact:spec:1",
                    "lineage_schema_version": "lineage@1",
                    "kind": "ir_snapshot",
                    "version_tuple": {
                        "doc_version": None,
                        "ir_snapshot_id": "snapshot:spec:1",
                        "constraint_snapshot_id": None,
                        "tool_version": "spec-upload@1",
                        "model_snapshot": None,
                        "prompt_version": None,
                        "agent_graph_version": None,
                        "env_contract_version": None,
                        "seed": None,
                        "cassette_id": None,
                    },
                    "parent_artifact_ids": [],
                    "payload_hash": None,
                    "payload_schema_id": "ir-core@1",
                    "domain_scope": {"domain_ids": ["economy"]},
                    "created_at": None,
                },
                "snapshot_id": "snapshot:spec:1",
                "schema_registry_version": "registry@1",
                "ref_name": "spec/head",
                "ref_value": {"artifact_id": "artifact:spec:1", "revision": 1},
            },
            resource_kind="spec_ref",
            resource_id="spec/head",
            revision=1,
        )


def _app(commands: _Commands | _SpecCommands):
    app = create_app(
        ApiDependencies(
            workflow_commands=commands,
            request_id_factory=lambda: "request:transport:1",
        )
    )
    actor = _actor()
    app.dependency_overrides[require_actor] = lambda: actor
    return app, actor


def _validation_payload() -> dict[str, object]:
    return {
        "request_schema_version": "patch-validation-admission-request@1",
        "approval_id": "approval:patch:1",
        "expected_subject_head_revision": 2,
        "expected_workflow_revision": 5,
        "subject_digest": "1" * 64,
        "base_snapshot_artifact_id": "artifact:snapshot:base",
        "preview_snapshot_artifact_id": "artifact:snapshot:preview",
        "candidate_config_export_artifact_ids": [],
        "target": {
            "ref_name": "content:live",
            "expected_ref": {"artifact_id": "artifact:snapshot:base", "revision": 7},
        },
        "validation_policy": {"profile_id": "validation:patch", "version": 3},
        "checker_profiles": [],
        "simulation_profiles": [],
        "findings": [],
        "review_artifact_ids": [],
        "playtest_trace_artifact_ids": [],
        "regression_suite_artifact_ids": [],
    }


def _spec_payload() -> dict[str, object]:
    return {
        "request_schema_version": "human-spec-upload-request@1",
        "ref_name": "spec/head",
        "expected_ref": None,
        "schema_registry_version": "registry@1",
        "meta_schema_version": "meta@1",
        "domain_scope": {"domain_ids": ["economy"]},
        "content_payload": {},
    }


def _headers(*, key: str = "command:1", etag: str = '"etag:5"') -> dict[str, str]:
    return {
        "Idempotency-Key": key,
        "If-Match": etag,
    }


def test_validation_route_forwards_only_server_actor_and_canonical_metadata() -> None:
    commands = _Commands()
    app, actor = _app(commands)
    with TestClient(app, base_url="https://gameforge.test") as client:
        response = client.post(
            "/api/v1/patches/artifact-patch:validate",
            json=_validation_payload(),
            headers=_headers(),
        )

    assert response.status_code == 202
    assert response.json()["run_id"] == "run:validation:1"
    assert response.headers["X-Resource-Revision"] == "1"
    assert response.headers["ETag"].startswith('"')
    assert response.headers["Cache-Control"] == "private, no-cache"

    command = commands.commands.pop()
    assert command.operation == "patch.validate"
    assert command.resource_kind == "patch"
    assert command.resource_id == "artifact-patch"
    assert command.metadata.actor == actor
    assert command.metadata.request_id == "request:transport:1"
    assert command.metadata.idempotency_key == "command:1"
    assert command.metadata.if_match == '"etag:5"'
    assert len(command.metadata.request_hash) == 64
    assert "actor" not in command.payload.model_dump(mode="json")


def test_request_hash_binds_route_payload_and_if_match_not_idempotency_key() -> None:
    commands = _Commands()
    app, _ = _app(commands)
    with TestClient(app, base_url="https://gameforge.test") as client:
        first = client.post(
            "/api/v1/patches/artifact-patch:validate",
            json=_validation_payload(),
            headers=_headers(key="first"),
        )
        second = client.post(
            "/api/v1/patches/artifact-patch:validate",
            json=_validation_payload(),
            headers=_headers(key="second"),
        )
        third = client.post(
            "/api/v1/patches/artifact-patch:validate",
            json=_validation_payload(),
            headers=_headers(key="third", etag='"etag:6"'),
        )

    assert [first.status_code, second.status_code, third.status_code] == [202, 202, 202]
    first_command, second_command, third_command = commands.commands
    assert first_command.metadata.request_hash == second_command.metadata.request_hash
    assert first_command.metadata.request_hash != third_command.metadata.request_hash


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Idempotency-Key": "command:1"},
        {"If-Match": '"etag:5"'},
        _headers(etag="*"),
        _headers(etag='W/"etag:5"'),
        _headers(etag='"one", "two"'),
    ],
)
def test_every_existing_resource_command_requires_one_idempotency_key_and_strong_if_match(
    headers: dict[str, str],
) -> None:
    commands = _Commands()
    app, _ = _app(commands)
    with TestClient(app, base_url="https://gameforge.test") as client:
        response = client.post(
            "/api/v1/patches/artifact-patch:validate",
            json=_validation_payload(),
            headers=headers,
        )

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert Problem.model_validate(response.json()).code == "request_schema_invalid"
    assert commands.commands == []


def test_create_command_omits_if_match_and_ignores_a_legacy_header_semantically() -> None:
    commands = _SpecCommands()
    app, _ = _app(commands)
    with TestClient(app, base_url="https://gameforge.test") as client:
        without_legacy = client.post(
            "/api/v1/specs",
            json=_spec_payload(),
            headers={"Idempotency-Key": "spec:create:without-legacy"},
        )
        with_legacy = client.post(
            "/api/v1/specs",
            json=_spec_payload(),
            headers={
                "Idempotency-Key": "spec:create:with-legacy",
                "If-Match": "legacy-value-that-is-not-an-etag",
            },
        )

    assert [without_legacy.status_code, with_legacy.status_code] == [201, 201]
    first, second = commands.commands
    assert first.metadata.if_match is None
    assert second.metadata.if_match is None
    assert first.metadata.request_hash == second.metadata.request_hash
    assert first.metadata.request_hash == canonical_sha256(
        {
            "request_hash_schema_version": "workflow-command-request-hash@1",
            "api_version": "v1",
            "operation": "spec.upload",
            "method": "POST",
            "path": "/api/v1/specs",
            "payload": first.payload.model_dump(mode="json", by_alias=True),
        }
    )


def test_valid_legacy_create_if_match_preserves_the_exact_pre_d2_request_hash() -> None:
    commands = _SpecCommands()
    app, _ = _app(commands)
    legacy_etag = '"legacy-create-etag"'
    headers = {
        "Idempotency-Key": "spec:create:legacy-retry",
        "If-Match": legacy_etag,
    }
    with TestClient(app, base_url="https://gameforge.test") as client:
        first_response = client.post(
            "/api/v1/specs",
            json=_spec_payload(),
            headers=headers,
        )
        retry_response = client.post(
            "/api/v1/specs",
            json=_spec_payload(),
            headers=headers,
        )

    assert [first_response.status_code, retry_response.status_code] == [201, 201]
    first, retry = commands.commands
    expected_hash = canonical_sha256(
        {
            "request_hash_schema_version": "workflow-command-request-hash@1",
            "api_version": "v1",
            "operation": "spec.upload",
            "method": "POST",
            "path": "/api/v1/specs",
            "if_match": legacy_etag,
            "payload": first.payload.model_dump(mode="json", by_alias=True),
        }
    )
    assert first.metadata.if_match is None
    assert retry.metadata.if_match is None
    assert first.metadata.request_hash == retry.metadata.request_hash == expected_hash


def test_create_command_still_requires_exactly_one_idempotency_key() -> None:
    commands = _SpecCommands()
    app, _ = _app(commands)
    with TestClient(app, base_url="https://gameforge.test") as client:
        response = client.post("/api/v1/specs", json=_spec_payload())

    assert response.status_code == 422
    assert Problem.model_validate(response.json()).code == "request_schema_invalid"
    assert commands.commands == []


def test_endpoint_decision_discriminator_cannot_disagree_with_body() -> None:
    commands = _Commands()
    app, _ = _app(commands)
    with TestClient(app, base_url="https://gameforge.test") as client:
        response = client.post(
            "/api/v1/approvals/approval-1:approve",
            json={
                "request_schema_version": "approval-decision-request@1",
                "decision": "reject",
                "requirement_ids": ["requirement:1"],
                "expected_workflow_revision": 3,
                "reason_code": "does-not-pass",
            },
            headers=_headers(),
        )

    assert response.status_code == 422
    assert Problem.model_validate(response.json()).code == "request_schema_invalid"
    assert commands.commands == []


def test_invalid_state_transition_is_narrowly_exposed_as_workflow_guard() -> None:
    commands = _Commands(fail=True)
    app, _ = _app(commands)
    with TestClient(app, base_url="https://gameforge.test") as client:
        response = client.post(
            "/api/v1/patches/artifact-patch:validate",
            json=_validation_payload(),
            headers=_headers(),
        )

    assert response.status_code == 409
    problem = Problem.model_validate(response.json())
    assert problem.code == "workflow_guard"
    assert "private workflow state" not in response.text


def test_openapi_contains_the_frozen_synchronous_command_surface() -> None:
    app, _ = _app(_Commands())
    paths = app.openapi()["paths"]
    expected = {
        "/api/v1/specs",
        "/api/v1/patches",
        "/api/v1/patches/{artifact_id}:validate",
        "/api/v1/patches/{artifact_id}:submit-for-approval",
        "/api/v1/patches/{artifact_id}:apply",
        "/api/v1/patches/{artifact_id}:rebase",
        "/api/v1/patches/{artifact_id}:resolve-conflicts",
        "/api/v1/constraint-proposals",
        "/api/v1/constraint-proposals/{artifact_id}:revise",
        "/api/v1/constraint-proposals/{artifact_id}:validate",
        "/api/v1/constraint-proposals/{artifact_id}:submit-for-approval",
        "/api/v1/constraint-proposals/{artifact_id}:publish",
        "/api/v1/approvals/{approval_id}:approve",
        "/api/v1/approvals/{approval_id}:reject",
        "/api/v1/approvals/{approval_id}:request_changes",
        "/api/v1/refs/{ref_name}/rollback-requests",
        "/api/v1/rollback-requests/{artifact_id}:validate",
        "/api/v1/rollback-requests/{artifact_id}:submit-for-approval",
        "/api/v1/rollback-requests/{artifact_id}:apply",
    }
    assert expected <= set(paths)
