from __future__ import annotations

from fastapi.testclient import TestClient

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import ApiDependencies, require_actor
from gameforge.contracts.auth import ApiKeyAuthRequestV1, SecretText, SessionToken
from gameforge.contracts.api import (
    ExecutionOptionResolveRequestV1,
    ExecutionOptionViewV1,
    compute_execution_option_id,
    compute_execution_option_request_hash,
)
from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.contracts.errors import CsrfFailed
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainScope,
    Principal,
)
from gameforge.contracts.jobs import (
    ExecutionVersionPlanV1,
    PlannedAgentNodeVersionV1,
    execution_version_plan_digest,
)


def _actor() -> ActorContext:
    return ActorContext(
        principal=Principal(
            id="human:operator",
            kind="human",
            display_name="Operator",
            status="active",
            revision=1,
            credential_epoch=1,
            authz_revision=1,
            roles=(),
        ),
        authentication=AuthenticationContext(
            mechanism="session",
            credential_id="credential:operator",
        ),
        session_id="session:operator",
        request_id="request:execution-option",
    )


def _plan() -> ExecutionVersionPlanV1:
    body = {
        "agent_graph_version": "review-triage-graph@1",
        "nodes": (
            PlannedAgentNodeVersionV1(
                agent_node_id="review-triage",
                prompt_version="review-triage@1",
                tool_version="review-triage@1",
                allowed_model_snapshots=("test:model@1",),
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


def _payload() -> dict[str, object]:
    return {
        "request_schema_version": "execution-option-resolve-request@1",
        "resource_operation_id": "submit_run_api_v1_runs_post",
        "run_kind": {"kind": "review.run", "version": 1},
        "llm_execution_mode": "record",
        "prospective_request": {
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
        },
        "replay_source_run_id": None,
    }


class _Resolver:
    def __init__(self) -> None:
        self.calls: list[tuple[ExecutionOptionResolveRequestV1, ActorContext]] = []

    def resolve_execution_option(
        self,
        *,
        request: ExecutionOptionResolveRequestV1,
        actor: ActorContext,
    ) -> ExecutionOptionViewV1:
        self.calls.append((request, actor))
        plan = _plan()
        body: dict[str, object] = {
            "option_schema_version": "execution-option@1",
            "option_id": "execution-option:sha256:" + "0" * 64,
            "resource_operation_id": request.resource_operation_id,
            "run_kind": RunKindRef(kind="review.run", version=1),
            "domain_scope": DomainScope(domain_ids=("content",)),
            "llm_execution_mode": "record",
            "execution_version_plan": plan,
            "prospective_request_hash": compute_execution_option_request_hash(request),
            "resolved_request_hash": compute_execution_option_request_hash(
                request,
                execution_version_plan=plan,
                cassette_artifact_id=None,
            ),
            "resolved_profile_binding_digests": ("c" * 64,),
            "source_run_id": None,
            "cassette_artifact_id": None,
        }
        body["option_id"] = compute_execution_option_id(body)
        return ExecutionOptionViewV1.model_validate(body)


class _SessionAuth:
    def resolve(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText | None,
        request_method: str,
        request_id: str,
    ) -> ActorContext:
        assert token.get_secret_value() == "valid-session"
        if request_method == "POST" and (
            csrf_token is None or csrf_token.get_secret_value() != "valid-csrf"
        ):
            raise CsrfFailed("private csrf mismatch")
        return _actor().model_copy(update={"request_id": request_id})


class _ApiKeyAuth:
    def authenticate(
        self,
        request: ApiKeyAuthRequestV1,
        *,
        request_id: str,
    ) -> ActorContext:
        assert request.api_key.get_secret_value() == "gfk_service.valid"
        actor = _actor()
        principal = actor.principal.model_copy(update={"id": "service:operator", "kind": "service"})
        return actor.model_copy(
            update={
                "principal": principal,
                "authentication": AuthenticationContext(
                    mechanism="api_key",
                    credential_id="api-key:operator",
                ),
                "session_id": None,
                "request_id": request_id,
            }
        )


def test_execution_option_transport_is_200_no_store_and_needs_no_write_headers() -> None:
    resolver = _Resolver()
    actor = _actor()
    app = create_app(ApiDependencies(execution_options=resolver))
    app.dependency_overrides[require_actor] = lambda: actor

    response = TestClient(app).post("/api/v1/execution-options:resolve", json=_payload())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert "location" not in response.headers
    assert resolver.calls[0][1] == actor
    assert resolver.calls[0][0].run_kind == RunKindRef(kind="review.run", version=1)
    assert response.json()["option_schema_version"] == "execution-option@1"


def test_execution_option_transport_fails_closed_without_the_resolver() -> None:
    app = create_app(ApiDependencies())
    app.dependency_overrides[require_actor] = _actor

    response = TestClient(app).post("/api/v1/execution-options:resolve", json=_payload())

    assert response.status_code == 503
    assert response.json()["code"] == "dependency_unavailable"


def test_read_only_post_keeps_session_csrf_and_api_key_semantics() -> None:
    resolver = _Resolver()
    app = create_app(
        ApiDependencies(
            execution_options=resolver,
            session_authentication=_SessionAuth(),
            api_key_authentication=_ApiKeyAuth(),
        )
    )

    with TestClient(app, base_url="https://gameforge.test") as client:
        client.cookies.set("gameforge_session", "valid-session")
        missing = client.post("/api/v1/execution-options:resolve", json=_payload())
        accepted = client.post(
            "/api/v1/execution-options:resolve",
            json=_payload(),
            headers={"X-CSRF-Token": "valid-csrf"},
        )
        client.cookies.clear()
        service = client.post(
            "/api/v1/execution-options:resolve",
            json=_payload(),
            headers={"Authorization": "ApiKey gfk_service.valid"},
        )

    assert missing.status_code == 403
    assert missing.json()["code"] == "csrf_failed"
    assert accepted.status_code == 200
    assert service.status_code == 200
