from __future__ import annotations

import pytest
from fastapi import Body, HTTPException
from fastapi.testclient import TestClient

from gameforge.apps.api.app import create_app
from gameforge.apps.api.dependencies import ApiDependencies
from gameforge.apps.api.health import ReadinessChecks, ReadinessService
from gameforge.contracts.auth import PasswordAuthRequestV1
from gameforge.contracts.errors import (
    Conflict,
    CursorExpired,
    CursorInvalid,
    DependencyUnavailable,
    Forbidden,
    IntegrityViolation,
    IdempotencyConflict,
    QueryTooBroad,
    QuotaExceeded,
    NotFound,
    OriginRejected,
    PatchPreconditionFailed,
    PayloadTooLarge,
    StaleTaskSuite,
    WorkflowGuard,
)
from gameforge.contracts.jobs import Problem
from gameforge.runtime.observability import AlwaysOffSampler, InMemoryExporter, Tracer


def _noop() -> None:
    return None


class _Ids:
    def new_trace_id(self) -> str:
        return "1" * 32

    def new_span_id(self) -> str:
        return "2" * 16


def _app():
    app = create_app(
        ApiDependencies(
            tracer=Tracer(
                exporter=InMemoryExporter(capacity=1),
                id_generator=_Ids(),
                sampler=AlwaysOffSampler(),
            ),
            request_id_factory=lambda: "request:error:1",
            readiness=ReadinessService(
                ReadinessChecks(
                    migration_head=_noop,
                    database=_noop,
                    object_store=_noop,
                    cost_ledger=_noop,
                    registry=_noop,
                    slo_retention=_noop,
                    audit_cache=_noop,
                )
            ),
        )
    )

    @app.get("/api/v1/typed/{kind}")
    def typed(kind: str) -> None:
        errors = {
            "cursor": CursorInvalid("private cursor"),
            "expired": CursorExpired("private expiry"),
            "forbidden": Forbidden("private permission"),
            "conflict": Conflict("private revision"),
            "idempotency": IdempotencyConflict("private request hash"),
            "workflow": WorkflowGuard("private workflow state"),
            "patch": PatchPreconditionFailed("private patch state"),
            "task-suite": StaleTaskSuite("private task suite"),
            "broad": QueryTooBroad("private bounds"),
            "quota": QuotaExceeded("private budget"),
            "origin": OriginRejected("private origin"),
            "not-found": NotFound("private missing resource"),
            "payload": PayloadTooLarge("private payload size"),
            "dependency": DependencyUnavailable("private dependency"),
            "integrity": IntegrityViolation(
                "sqlite row password_hash leaked",
                sql="select * from password_credentials",
            ),
        }
        raise errors[kind]

    @app.get("/api/v1/framework-400")
    def framework_400() -> None:
        raise HTTPException(status_code=400, detail="private parser detail")

    @app.post("/api/v1/validated")
    def validated(payload: PasswordAuthRequestV1 = Body()) -> None:
        del payload

    return app


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/api/v1/typed/cursor", 400, "invalid_cursor"),
        ("/api/v1/typed/expired", 410, "cursor_expired"),
        ("/api/v1/typed/forbidden", 403, "forbidden"),
        ("/api/v1/typed/conflict", 409, "revision_conflict"),
        ("/api/v1/typed/idempotency", 409, "idempotency_conflict"),
        ("/api/v1/typed/workflow", 409, "workflow_guard"),
        ("/api/v1/typed/patch", 409, "patch_precondition_failed"),
        ("/api/v1/typed/task-suite", 409, "stale_task_suite"),
        ("/api/v1/typed/broad", 422, "query_too_broad"),
        ("/api/v1/typed/quota", 429, "quota_exceeded"),
        ("/api/v1/typed/origin", 403, "origin_rejected"),
        ("/api/v1/typed/not-found", 404, "not_found"),
        ("/api/v1/typed/payload", 413, "payload_too_large"),
        ("/api/v1/typed/dependency", 503, "dependency_unavailable"),
        ("/api/v1/typed/integrity", 500, "integrity_violation"),
        ("/api/v1/framework-400", 400, "bad_request"),
        ("/api/v1/missing", 404, "not_found"),
    ],
)
def test_typed_and_framework_errors_use_one_problem_contract(
    path: str,
    status: int,
    code: str,
) -> None:
    with TestClient(_app(), base_url="https://gameforge.test") as client:
        response = client.get(path)

    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    problem = Problem.model_validate(response.json())
    assert problem.code == code
    assert problem.request_id == "request:error:1"
    assert problem.trace_id == "1" * 32
    assert "private" not in response.text
    assert "password_hash" not in response.text
    assert "select *" not in response.text


def test_framework_405_is_wrapped_as_problem() -> None:
    with TestClient(_app(), base_url="https://gameforge.test") as client:
        response = client.put("/api/v1/validated")

    assert response.status_code == 405
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["Allow"] == "POST"
    assert Problem.model_validate(response.json()).code == "method_not_allowed"


def test_validation_errors_do_not_echo_secret_input() -> None:
    secret = "secret-that-must-never-return"
    with TestClient(_app(), base_url="https://gameforge.test") as client:
        response = client.post(
            "/api/v1/validated",
            json={"login_name": "alice", "password": secret, "unexpected": secret},
        )

    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    problem = Problem.model_validate(response.json())
    assert problem.code == "request_schema_invalid"
    assert secret not in response.text
    assert problem.errors is not None
    assert all(set(error) <= {"loc", "msg", "type"} for error in problem.errors)


def test_validation_error_projection_stays_within_problem_contract_bounds() -> None:
    payload = {
        "login_name": "alice",
        "password": "secret",
        **{f"extra_{index}": index for index in range(1100)},
    }
    with TestClient(_app(), base_url="https://gameforge.test") as client:
        response = client.post("/api/v1/validated", json=payload)

    assert response.status_code == 422
    problem = Problem.model_validate(response.json())
    assert problem.errors is not None
    assert 0 < len(problem.errors) < 1100
