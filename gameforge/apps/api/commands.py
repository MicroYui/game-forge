"""Durable Run-command transport: ``POST /runs/{id}:cancel`` + ``WS /runs/{id}/commands``.

Both surfaces submit through the SAME M4a :class:`RunCommandService.submit` path
(:class:`~gameforge.apps.api.dependencies.RunCommandSubmitPort`) — there is NO direct
cancel-flag shortcut. The REST route builds a ``RunCommandV1(type="cancel", …)`` from a
:class:`RunCancelRequestV1` body plus the ``Idempotency-Key`` header; the WebSocket
channel accepts full ``RunCommandV1`` client frames (cancel or provide_input). Each
command carries the server-scoped idempotency key + canonical request hash + OCC
``expected_run_revision`` that ``submit`` enforces, so a duplicate exact command replays
its committed result (a stable ``status="duplicate"`` ACK) while the same key/sequence
bound to a different request is an ``idempotency_conflict``. ``submit`` persists the
command, its Run mutation, RunEvent(s), and audit inside one UoW BEFORE returning, so the
ACK is durable.

Authorization is deliberately OUTSIDE the deterministic-trunk ``submit`` (which takes an
:class:`AuditActor` and does no RBAC): the transport reauthorizes the REAL Run against the
current Principal/roles — once per REST request and, for the WebSocket, on EVERY message
(reload Principal/roles + re-derive the Run's domain via the 15a ``_resolve_run_read_domain``
helper) — so a revoked grant cannot keep submitting mid-connection.

The browser NEVER receives lease/fencing tokens: the only server frames are
``RunCommandAckV1``/``RunCommandProblemV1`` (``RunCommandServerFrame``), which structurally
omit ``claimed_attempt_no``/``claimed_fencing_token``/``claimed_at`` — the transport never
serializes a ``RunCommandRecordV1``. Disconnect recovery composes with the persisted
command views (``RunCommandViewV1``) and the resumable SSE event stream.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Header, Request, Response, WebSocket, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect, WebSocketState

from gameforge.apps.api.dependencies import (
    ApiDependencies,
    ApiKeyAuthenticationPort,
    RunCommandAuthorizerPort,
    RunCommandSubmitPort,
    RunCommandWebSocketConfig,
    SessionAuthenticationPort,
    SessionCookieSettings,
    api_dependencies,
    require_actor,
)
from gameforge.apps.api.errors import _mapping, _problem
from gameforge.apps.api.streaming import ApprovalItemReader, _resolve_run_read_domain
from gameforge.contracts.api import RunCancelRequestV1
from gameforge.contracts.auth import (
    ApiKeyAuthRequestV1,
    ApiKeySecret,
    SecretText,
    SessionToken,
)
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import (
    AuthError,
    AuthFailed,
    AuthRequired,
    Conflict,
    DependencyUnavailable,
    Forbidden,
    IntegrityViolation,
    InvalidStateTransition,
    NotFound,
    PayloadTooLarge,
    RequestSchemaInvalid,
)
from gameforge.contracts.identity import (
    ActorContext,
    DomainRegistryV1,
    Permission,
    RolePolicy,
)
from gameforge.contracts.jobs import (
    RunCommandAckV1,
    RunCommandProblemV1,
    RunCommandV1,
    RunKindDefinition,
    RunRecord,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.platform.read_models.authorization import (
    ReadAuthorizationService,
    ReadPolicyRepository,
)

_IDEMPOTENCY_HEADER = "Idempotency-Key"
_AUTHORIZATION_HEADER = "authorization"


class RunProjectionReader(Protocol):
    """Load a Run for authorization without asserting event-head contiguity."""

    def get_run_projection(self, run_id: str) -> RunRecord | None: ...


class RunKindResolver(Protocol):
    """Resolve the exact retained RunKind definition (its write ``required_permission``)."""

    def get_run_kind(self, kind: object) -> RunKindDefinition | None: ...


@dataclass(frozen=True, slots=True)
class RunCommandAuthorizationScope:
    """One short read transaction's authorities for reauthorizing a Run command."""

    runs: RunProjectionReader
    policies: ReadPolicyRepository
    approvals: ApprovalItemReader | None = None


ReadScopeFactory = Callable[[], AbstractContextManager[RunCommandAuthorizationScope]]


class RunCommandAuthorizationService:
    """Reauthorizing write-permission authority behind :class:`RunCommandAuthorizerPort`.

    Mirrors :class:`~gameforge.apps.api.streaming.RunEventStreamService` but authorizes the
    RunKind's WRITE ``required_permission`` (the same permission admission proved to create
    the Run) against the SERVER-derived domain. Because the domain derivation reuses the 15a
    ``_resolve_run_read_domain`` helper — which is byte-for-byte admission's own
    ``_resolve_permission_domain`` for the ``"all"`` marker and ``None`` for the domainless
    ``dr.drill`` — a principal that could admit a Run in its domain can also command it, and
    no client field ever selects the domain.
    """

    def __init__(
        self,
        *,
        read_scope: ReadScopeFactory,
        registry: RunKindResolver,
        role_policy_version: str,
        role_policy_digest: str,
    ) -> None:
        if not callable(read_scope):
            raise TypeError("read_scope must be a callable context-manager factory")
        self._read_scope = read_scope
        self._registry = registry
        if not isinstance(role_policy_version, str) or not role_policy_version:
            raise ValueError("role_policy_version must be a non-empty string")
        if not isinstance(role_policy_digest, str) or len(role_policy_digest) != 64:
            raise ValueError("role_policy_digest must be a SHA-256 digest")
        self._role_policy_version = role_policy_version
        self._role_policy_digest = role_policy_digest

    def authorize(self, *, run_id: str, actor: ActorContext) -> None:
        with self._read_scope() as scope:
            run = scope.runs.get_run_projection(run_id)
            if run is None:
                raise NotFound("run is unavailable", run_id=run_id)
            definition = self._registry.get_run_kind(run.kind)
            if not isinstance(definition, RunKindDefinition):
                raise DependencyUnavailable(
                    "run kind definition is unavailable for command authorization",
                    component="run_command_authorization",
                )
            _role_policy, registry = self._load_authority(scope.policies)
            base = definition.required_permission
            permission = Permission(
                action=base.action,
                resource_kind=base.resource_kind,
                domain_scope=_resolve_run_read_domain(run, registry, scope.approvals),
            )
            query_hash = canonical_sha256(
                {
                    "query_schema_version": "run-command-authorization-query@1",
                    "run_id": run.run_id,
                    "run_revision": run.revision,
                    "action": base.action,
                    "resource_kind": base.resource_kind,
                }
            )
            authorization = ReadAuthorizationService(
                policy_repository=scope.policies,
                role_policy_version=self._role_policy_version,
                role_policy_digest=self._role_policy_digest,
            )
            # Reloads the current Principal/roles and RBAC-authorizes the loaded Run's
            # write permission; raises Forbidden on a revoked grant (every request/message).
            authorization.require_singular(
                principal=actor.principal,
                permission=permission,
                query_hash=query_hash,
            )

    def _load_authority(
        self,
        policies: ReadPolicyRepository,
    ) -> tuple[RolePolicy, DomainRegistryV1]:
        role_policy = policies.get_role_policy(
            self._role_policy_version,
            self._role_policy_digest,
        )
        if not isinstance(role_policy, RolePolicy):
            raise DependencyUnavailable(
                "run command role policy is unavailable",
                component="run_command_authorization",
            )
        registry = policies.get_domain_registry(role_policy.domain_registry_ref)
        if not isinstance(registry, DomainRegistryV1):
            raise DependencyUnavailable(
                "run command domain registry is unavailable",
                component="run_command_authorization",
            )
        return role_policy, registry


# ── shared REST/WS submit path ───────────────────────────────────────────────
def _authority(
    dependencies: ApiDependencies,
) -> tuple[RunCommandAuthorizerPort, RunCommandSubmitPort]:
    authorizer = dependencies.run_command_authorizer
    service = dependencies.run_command_service
    if authorizer is None or service is None:
        raise DependencyUnavailable(
            "run command authority is unavailable",
            component="run_command",
        )
    return authorizer, service


def _submit_command(
    *,
    dependencies: ApiDependencies,
    run_id: str,
    command: RunCommandV1,
    actor: ActorContext,
) -> RunCommandAckV1:
    """Reauthorize the real Run, then submit via the ONE durable ``submit`` path.

    Returns a lease/fencing-free :class:`RunCommandAckV1`; the browser never sees the
    ``RunCommandRecordV1`` worker columns. Raises typed platform failures the caller
    maps to a ``RunCommandProblemV1`` frame / HTTP status.
    """

    authorizer, service = _authority(dependencies)
    authorizer.authorize(run_id=run_id, actor=actor)
    audit_actor = AuditActor(
        principal_id=actor.principal.id,
        principal_kind=actor.principal.kind,
    )
    result = service.submit(run_id=run_id, command=command, actor=audit_actor)
    return RunCommandAckV1(
        command_id=command.command_id,
        client_id=command.client_id,
        client_seq=command.client_seq,
        status=result.status,
        persisted_status=result.persisted_status,
        command_revision=result.command_revision,
        run_revision=result.run_revision,
    )


def _command_error_status(error: BaseException) -> tuple[int, str, str, str]:
    """Map a submit/authorization failure to an RFC 9457 status tuple.

    ``InvalidStateTransition`` (terminal Run, inactive Run for provide_input, or a
    command not allowed for the Run kind) is a client-caused state conflict, so it is a
    ``409`` rather than the default ``500`` the shared mapper would give a raw
    ``GameForgeError``; every other failure reuses the frozen ``errors._mapping``.
    """

    if isinstance(error, InvalidStateTransition):
        return (
            409,
            "invalid_state_transition",
            "Conflict",
            "The run command is not permitted in the current run state.",
        )
    return _mapping(error)


def _command_problem_frame(
    scope: object,
    error: BaseException,
    *,
    command_id: str | None,
    client_seq: int | None,
) -> tuple[int, RunCommandProblemV1]:
    status_code, code, title, detail = _command_error_status(error)
    problem = _problem(
        scope,  # type: ignore[arg-type]
        status=status_code,
        code=code,
        title=title,
        detail=detail,
    )
    return status_code, RunCommandProblemV1(
        command_id=command_id,
        client_seq=client_seq,
        problem=problem,
    )


_MAPPED_COMMAND_ERRORS = (
    Conflict,
    Forbidden,
    NotFound,
    InvalidStateTransition,
    RequestSchemaInvalid,
    PayloadTooLarge,
    IntegrityViolation,
    DependencyUnavailable,
)


# ── WebSocket-scope authentication ───────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class _WebSocketCredentials:
    """The raw handshake credential captured once, re-resolved on every message."""

    mechanism: str  # "session" | "api_key"
    session_token: str | None = None
    csrf_token: str | None = None
    api_key: str | None = None


def _websocket_credentials(
    websocket: WebSocket,
    *,
    cookie: SessionCookieSettings,
    ws_config: RunCommandWebSocketConfig,
) -> _WebSocketCredentials:
    """Extract exactly one WS-scope credential mechanism from the handshake.

    Browsers cannot set an ``Authorization`` header or custom CSRF header on a WebSocket
    handshake, so the human path is the session cookie plus a CSRF token offered as a
    WebSocket subprotocol (``gameforge.csrf.<token>``); non-browser service clients use an
    ``ApiKey`` Authorization header. Presenting both mechanisms is rejected.
    """

    session_value = websocket.cookies.get(cookie.name)
    authorization = websocket.headers.get(_AUTHORIZATION_HEADER)
    if session_value is not None and authorization is not None:
        raise AuthFailed("multiple WebSocket credential mechanisms are forbidden")
    if session_value is not None:
        if not session_value or len(session_value) > 4096:
            raise AuthFailed("WebSocket session cookie is invalid")
        return _WebSocketCredentials(
            mechanism="session",
            session_token=session_value,
            csrf_token=_csrf_from_subprotocols(websocket, ws_config),
        )
    if authorization is not None:
        scheme, separator, secret = authorization.partition(" ")
        if scheme != "ApiKey" or not separator or not secret or len(secret) > 4096 or " " in secret:
            raise AuthFailed("WebSocket API-key authorization header is invalid")
        return _WebSocketCredentials(mechanism="api_key", api_key=secret)
    raise AuthRequired("WebSocket credentials are required")


def _csrf_from_subprotocols(
    websocket: WebSocket,
    ws_config: RunCommandWebSocketConfig,
) -> str | None:
    offered = websocket.scope.get("subprotocols", ())
    for value in offered:
        if isinstance(value, str) and value.startswith(ws_config.csrf_subprotocol_prefix):
            token = value[len(ws_config.csrf_subprotocol_prefix) :]
            if token and len(token) <= 4096:
                return token
    return None


def _resolve_websocket_actor(
    credentials: _WebSocketCredentials,
    *,
    session_auth: SessionAuthenticationPort | None,
    api_key_auth: ApiKeyAuthenticationPort | None,
    request_id: str,
) -> ActorContext:
    """Re-resolve the current Principal from the captured credential (per message).

    Re-running the auth port on every message reloads the live credential/session and
    principal state, so a revocation between frames is caught (not just at handshake).
    """

    if credentials.mechanism == "session":
        if session_auth is None:
            raise DependencyUnavailable(
                "session authentication is not configured",
                component="run_command_ws_auth",
            )
        actor = session_auth.resolve(
            SessionToken(credentials.session_token or ""),
            csrf_token=(
                None if credentials.csrf_token is None else SecretText(credentials.csrf_token)
            ),
            request_method="POST",
            request_id=request_id,
        )
        if actor.principal.kind != "human" or actor.authentication.mechanism != "session":
            raise IntegrityViolation("WebSocket session authentication returned a non-human actor")
        return actor
    if api_key_auth is None:
        raise DependencyUnavailable(
            "API-key authentication is not configured",
            component="run_command_ws_auth",
        )
    actor = api_key_auth.authenticate(
        ApiKeyAuthRequestV1(api_key=ApiKeySecret(credentials.api_key or "")),
        request_id=request_id,
    )
    if actor.principal.kind != "service" or actor.authentication.mechanism != "api_key":
        raise IntegrityViolation("WebSocket API-key authentication returned a non-service actor")
    return actor


def _is_auth_failure(error: BaseException) -> bool:
    return isinstance(error, (AuthError, AuthRequired, Forbidden))


# ── routers ──────────────────────────────────────────────────────────────────
def _require_cancel_headers(
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(alias=_IDEMPOTENCY_HEADER, min_length=1, max_length=512),
    ],
) -> None:
    del idempotency_key
    values = request.headers.getlist(_IDEMPOTENCY_HEADER)
    if len(values) != 1:
        raise RequestSchemaInvalid(f"{_IDEMPOTENCY_HEADER} must be supplied exactly once")
    value = values[0]
    if (
        not value
        or value != value.strip()
        or len(value) > 512
        or any(ord(character) < 0x21 or ord(character) == 0x7F for character in value)
    ):
        raise RequestSchemaInvalid(f"{_IDEMPOTENCY_HEADER} is invalid")
    request.state.run_command_idempotency_key = value


def run_commands_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["runs"])

    @router.post(
        "/runs/{run_id}:cancel",
        response_model=RunCommandAckV1,
        status_code=status.HTTP_200_OK,
        dependencies=[Depends(_require_cancel_headers)],
    )
    def cancel_run(
        run_id: str,
        payload: RunCancelRequestV1,
        request: Request,
        response: Response,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> RunCommandAckV1 | JSONResponse:
        idempotency_key = getattr(request.state, "run_command_idempotency_key", None)
        if not isinstance(idempotency_key, str):
            raise RequestSchemaInvalid("run command idempotency key is unavailable")
        command = RunCommandV1(
            command_id=payload.command_id,
            client_id=payload.client_id,
            client_seq=payload.client_seq,
            idempotency_key=idempotency_key,
            expected_run_revision=payload.expected_run_revision,
            type="cancel",
            payload_schema_id="run-cancel@1",
            payload=payload.payload,
        )
        try:
            ack = _submit_command(
                dependencies=dependencies,
                run_id=run_id,
                command=command,
                actor=actor,
            )
        except _MAPPED_COMMAND_ERRORS as error:
            status_code, frame = _command_problem_frame(
                request.scope,
                error,
                command_id=command.command_id,
                client_seq=command.client_seq,
            )
            return JSONResponse(
                status_code=status_code,
                content=frame.model_dump(mode="json", exclude_none=True),
                media_type="application/problem+json",
            )
        response.headers["Cache-Control"] = "no-store"
        return ack

    @router.websocket("/runs/{run_id}/commands")
    async def run_commands_ws(websocket: WebSocket, run_id: str) -> None:
        # Origin is validated by AuthenticationMiddleware before the route runs; it does
        # NOT resolve a WS actor, so the handshake is authenticated here. Dependencies are
        # read from app state (a WebSocket has no HTTP ``Request`` for ``api_dependencies``).
        dependencies = _ws_dependencies(websocket)
        ws_config = dependencies.run_command_ws_config
        request_id = _ws_request_id(websocket, dependencies)
        try:
            credentials = _websocket_credentials(
                websocket,
                cookie=dependencies.session_cookie,
                ws_config=ws_config,
            )
            # Authenticate the handshake before accept so bad credentials never open.
            _resolve_websocket_actor(
                credentials,
                session_auth=dependencies.session_authentication,
                api_key_auth=dependencies.api_key_authentication,
                request_id=request_id,
            )
        except Exception:
            await websocket.close(code=1008)
            return
        # Only echo the command subprotocol if the client actually offered it; selecting
        # a subprotocol the client did not offer would fail a real browser handshake.
        offered = websocket.scope.get("subprotocols", ())
        negotiated = ws_config.subprotocol if ws_config.subprotocol in offered else None
        await websocket.accept(subprotocol=negotiated)
        await _run_command_ws_loop(
            websocket=websocket,
            run_id=run_id,
            dependencies=dependencies,
            credentials=credentials,
            request_id=request_id,
            ws_config=ws_config,
        )

    return router


def _ws_dependencies(websocket: WebSocket) -> ApiDependencies:
    selected = getattr(websocket.app.state, "dependencies", None)
    if not isinstance(selected, ApiDependencies):
        raise RuntimeError("API composition dependencies are unavailable")
    return selected


def _ws_request_id(websocket: WebSocket, dependencies: ApiDependencies) -> str:
    request_id = dependencies.request_id_factory()
    if not isinstance(request_id, str) or not request_id or len(request_id) > 512:
        request_id = "request:unavailable"
    state = websocket.scope.setdefault("state", {})
    if isinstance(state, dict):
        state["request_id"] = request_id
    return request_id


async def _run_command_ws_loop(
    *,
    websocket: WebSocket,
    run_id: str,
    dependencies: ApiDependencies,
    credentials: _WebSocketCredentials,
    request_id: str,
    ws_config: RunCommandWebSocketConfig,
) -> None:
    processed = 0
    while True:
        try:
            text = await websocket.receive_text()
        except WebSocketDisconnect:
            return
        except RuntimeError:
            # A non-text frame (binary/close) — the client channel is text-only.
            await _ws_close(websocket, code=1003)
            return
        if len(text.encode("utf-8")) > ws_config.max_frame_bytes:
            await _ws_send_problem(
                websocket,
                PayloadTooLarge("run command frame exceeds the WebSocket frame bound"),
                command_id=None,
                client_seq=None,
            )
            await _ws_close(websocket, code=1009)
            return
        processed += 1
        if processed > ws_config.max_commands_per_connection:
            await _ws_send_problem(
                websocket,
                Conflict("run command connection exceeded its command budget"),
                command_id=None,
                client_seq=None,
            )
            await _ws_close(websocket, code=1013)
            return

        command: RunCommandV1 | None = None
        try:
            actor = _resolve_websocket_actor(
                credentials,
                session_auth=dependencies.session_authentication,
                api_key_auth=dependencies.api_key_authentication,
                request_id=request_id,
            )
            command = RunCommandV1.model_validate_json(text)
            ack = _submit_command(
                dependencies=dependencies,
                run_id=run_id,
                command=command,
                actor=actor,
            )
        except ValidationError:
            await _ws_send_problem(
                websocket,
                RequestSchemaInvalid("run command frame does not match the command schema"),
                command_id=None,
                client_seq=None,
            )
            continue
        except Exception as error:  # noqa: BLE001 - every failure becomes a server frame
            await _ws_send_problem(
                websocket,
                error,
                command_id=(command.command_id if command is not None else None),
                client_seq=(command.client_seq if command is not None else None),
            )
            if _is_auth_failure(error):
                # A revoked/absent grant cannot keep the channel; close after informing.
                await _ws_close(websocket, code=1008)
                return
            continue
        # One frame in flight at a time: the ACK is sent before the next receive, so a
        # slow producer throttles the socket instead of buffering unbounded work.
        await websocket.send_json(ack.model_dump(mode="json"))


async def _ws_send_problem(
    websocket: WebSocket,
    error: BaseException,
    *,
    command_id: str | None,
    client_seq: int | None,
) -> None:
    _status, frame = _command_problem_frame(
        websocket.scope,
        error,
        command_id=command_id,
        client_seq=client_seq,
    )
    await websocket.send_json(frame.model_dump(mode="json", exclude_none=True))


async def _ws_close(websocket: WebSocket, *, code: int) -> None:
    if websocket.application_state is not WebSocketState.DISCONNECTED:
        try:
            await websocket.close(code=code)
        except RuntimeError:
            pass


__all__ = [
    "RunCommandAuthorizationScope",
    "RunCommandAuthorizationService",
    "run_commands_router",
]
