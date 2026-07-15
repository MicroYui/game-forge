"""Injected composition dependencies for the M4c API process."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import secrets
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import urlsplit

from fastapi import Request

from gameforge.contracts.auth import (
    ApiKeyAuthRequestV1,
    PasswordAuthRequestV1,
    SecretText,
    SessionIssueV1,
    SessionToken,
)
from gameforge.contracts.identity import ActorContext
from gameforge.contracts.errors import AuthRequired
from gameforge.contracts.api import (
    RunAcceptedV1,
    WorkflowCommandPayloadV1,
    WorkflowCommandResponseV1,
)
from gameforge.runtime.observability import AlwaysOffSampler, Tracer

if TYPE_CHECKING:
    from gameforge.contracts.jobs import RunEvent
    from gameforge.platform.read_models.content import ContentReadService
    from gameforge.platform.read_models.observability import ObservabilityReadService
    from gameforge.platform.read_models.workflows import WorkflowReadService


class SessionAuthenticationPort(Protocol):
    def login(
        self,
        request: PasswordAuthRequestV1,
        *,
        request_id: str,
    ) -> SessionIssueV1: ...

    def resolve(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText | None,
        request_method: str,
        request_id: str,
    ) -> ActorContext: ...


class ApiKeyAuthenticationPort(Protocol):
    def authenticate(
        self,
        request: ApiKeyAuthRequestV1,
        *,
        request_id: str,
    ) -> ActorContext: ...


class LogoutCommandPort(Protocol):
    def logout(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText,
        idempotency_key: str,
        request_id: str,
    ) -> object: ...


class ReadinessPort(Protocol):
    def check(self) -> tuple[str, ...]: ...


WorkflowCommandOperation = Literal[
    "spec.upload",
    "patch.draft",
    "patch.validate",
    "patch.submit",
    "patch.apply",
    "patch.rebase",
    "patch.resolve_conflicts",
    "constraint.draft",
    "constraint.revise",
    "constraint.validate",
    "constraint.submit",
    "constraint.publish",
    "approval.approve",
    "approval.reject",
    "approval.request_changes",
    "rollback.draft",
    "rollback.validate",
    "rollback.submit",
    "rollback.apply",
]


@dataclass(frozen=True, slots=True)
class WorkflowCommandMetadata:
    actor: ActorContext
    request_id: str
    trace_id: str | None
    idempotency_key: str
    request_hash: str
    if_match: str

    def __post_init__(self) -> None:
        for name in ("request_id", "idempotency_key", "if_match"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise ValueError(f"{name} must be a non-empty bounded string")
        if (
            not isinstance(self.request_hash, str)
            or len(self.request_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.request_hash)
        ):
            raise ValueError("request_hash must be a lowercase SHA-256 digest")
        if self.trace_id is not None and (
            not isinstance(self.trace_id, str) or not self.trace_id or len(self.trace_id) > 512
        ):
            raise ValueError("trace_id must be a bounded string when supplied")


@dataclass(frozen=True, slots=True)
class WorkflowCommand:
    operation: WorkflowCommandOperation
    resource_kind: str
    resource_id: str
    payload: WorkflowCommandPayloadV1
    metadata: WorkflowCommandMetadata

    def __post_init__(self) -> None:
        for name in ("resource_kind", "resource_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError(f"{name} must be a non-empty bounded string")


@dataclass(frozen=True, slots=True)
class WorkflowCommandResult:
    value: WorkflowCommandResponseV1
    resource_kind: str
    resource_id: str
    revision: int

    def __post_init__(self) -> None:
        for name in ("resource_kind", "resource_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or len(value) > 512:
                raise ValueError(f"{name} must be a non-empty bounded string")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 1
        ):
            raise ValueError("revision must be a positive integer")


class WorkflowCommandPort(Protocol):
    def execute(self, command: WorkflowCommand) -> WorkflowCommandResult: ...


class RunAdmissionPort(Protocol):
    """Task-8 Run admission engine surface used by the resource/generic routers.

    Every method returns ``202 RunAccepted``; asynchronous failures surface later as
    RunFailure/RunEvent, never as a retroactive HTTP error on the returned 202.
    """

    def admit_generic_run(self, **kwargs: object) -> RunAcceptedV1: ...

    def admit_resource_run(self, **kwargs: object) -> RunAcceptedV1: ...

    def admit_generation(self, **kwargs: object) -> RunAcceptedV1: ...

    def admit_constraint_proposal(self, **kwargs: object) -> RunAcceptedV1: ...


@dataclass(frozen=True, slots=True)
class RunStreamGrant:
    """Result of authorizing one SSE connection/reconnect.

    ``earliest_retained_seq`` is derived from the actual retained event store
    (MIN seq), never a speculative authority column.
    """

    earliest_retained_seq: int | None

    def __post_init__(self) -> None:
        if self.earliest_retained_seq is not None and (
            isinstance(self.earliest_retained_seq, bool)
            or not isinstance(self.earliest_retained_seq, int)
            or self.earliest_retained_seq < 1
        ):
            raise ValueError("earliest_retained_seq must be a positive integer or None")


@dataclass(frozen=True, slots=True)
class RunEventStreamConfig:
    """Bounded paging + heartbeat cadence for the resumable SSE endpoint.

    ``heartbeat_seconds`` also bounds live delivery latency when no producer calls the
    notifier (correctness is preserved by the post-wait DB reread either way), so it is
    kept modest rather than a long keep-alive interval.
    """

    page_limit: int = 256
    heartbeat_seconds: float = 2.0

    def __post_init__(self) -> None:
        if isinstance(self.page_limit, bool) or not isinstance(self.page_limit, int):
            raise TypeError("page_limit must be an integer")
        if not 1 <= self.page_limit <= 1024:
            raise ValueError("page_limit must be between 1 and 1024")
        if isinstance(self.heartbeat_seconds, bool) or not isinstance(
            self.heartbeat_seconds, (int, float)
        ):
            raise TypeError("heartbeat_seconds must be a number")
        if not 0 < self.heartbeat_seconds <= 3600:
            raise ValueError("heartbeat_seconds must be a positive bounded interval")


class RunEventStreamPort(Protocol):
    """Bounded, reauthorizing read authority for the resumable SSE endpoint.

    ``authorize_stream`` loads the real Run, derives its domain server-side, and
    RBAC-authorizes a read on EVERY connection/reconnect (raising NotFound /
    Forbidden / CursorExpired). ``read_events`` pages persisted RunEvents in
    bounded pages; the SQLite event store is the sole authority.
    """

    def authorize_stream(
        self,
        *,
        run_id: str,
        actor: ActorContext,
        after_seq: int,
    ) -> RunStreamGrant: ...

    def read_events(
        self,
        run_id: str,
        after_seq: int,
        limit: int,
    ) -> tuple["RunEvent", ...]: ...


class RunEventSubscription(Protocol):
    """One latency-hint waiter; a wait timeout is a heartbeat, never data loss."""

    async def wait(self, timeout: float) -> bool: ...

    def close(self) -> None: ...


class RunEventNotifierPort(Protocol):
    """Non-authoritative in-process pub-sub; the DB is always reread after a wait."""

    def notify(self, run_id: str) -> None: ...

    def subscribe(self, run_id: str) -> RunEventSubscription: ...


class _DiscardSpanExporter:
    def export(self, spans: object) -> None:
        del spans


def _default_tracer() -> Tracer:
    return Tracer(exporter=_DiscardSpanExporter(), sampler=AlwaysOffSampler())


def _new_request_id() -> str:
    return f"request:{secrets.token_hex(16)}"


@dataclass(frozen=True, slots=True)
class SessionCookieSettings:
    name: str = "gameforge_session"
    path: str = "/"
    same_site: str = "strict"
    secure: bool = True
    http_only: bool = True

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 128:
            raise ValueError("session cookie name must be a non-empty bounded string")
        if not self.path.startswith("/") or len(self.path) > 2048:
            raise ValueError("session cookie path must be an absolute bounded path")
        if self.same_site not in {"strict", "lax"}:
            raise ValueError("session cookie SameSite policy must be strict or lax")
        if self.secure is not True or self.http_only is not True:
            raise ValueError("browser session cookies must be Secure and HttpOnly")


@dataclass(frozen=True, slots=True)
class ApiDependencies:
    session_authentication: SessionAuthenticationPort | None = None
    api_key_authentication: ApiKeyAuthenticationPort | None = None
    logout_commands: LogoutCommandPort | None = None
    readiness: ReadinessPort | None = None
    tracer: Tracer = field(default_factory=_default_tracer)
    request_id_factory: Callable[[], str] = _new_request_id
    session_cookie: SessionCookieSettings = field(default_factory=SessionCookieSettings)
    allowed_websocket_origins: frozenset[str] = frozenset()
    content_reads: ContentReadService | None = None
    workflow_reads: WorkflowReadService | None = None
    observability_reads: ObservabilityReadService | None = None
    workflow_commands: WorkflowCommandPort | None = None
    run_admission: RunAdmissionPort | None = None
    run_event_stream: RunEventStreamPort | None = None
    run_event_notifier: RunEventNotifierPort | None = None
    run_event_stream_config: RunEventStreamConfig = field(default_factory=RunEventStreamConfig)

    def __post_init__(self) -> None:
        if not callable(self.request_id_factory):
            raise TypeError("request_id_factory must be callable")
        if not isinstance(self.allowed_websocket_origins, frozenset):
            raise TypeError("allowed_websocket_origins must be a frozenset")
        for origin in self.allowed_websocket_origins:
            if not isinstance(origin, str) or not origin or len(origin) > 2048:
                raise ValueError("allowed WebSocket origins must be bounded strings")
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("allowed WebSocket origins must be exact HTTP origins")


def api_dependencies(request: Request) -> ApiDependencies:
    selected = getattr(request.app.state, "dependencies", None)
    if not isinstance(selected, ApiDependencies):
        raise RuntimeError("API composition dependencies are unavailable")
    return selected


def require_actor(request: Request) -> ActorContext:
    actor = getattr(request.state, "actor", None)
    if not isinstance(actor, ActorContext):
        raise AuthRequired("HTTP credentials are required")
    return actor


__all__ = [
    "ApiDependencies",
    "ApiKeyAuthenticationPort",
    "LogoutCommandPort",
    "ReadinessPort",
    "RunAdmissionPort",
    "RunEventNotifierPort",
    "RunEventStreamConfig",
    "RunEventStreamPort",
    "RunEventSubscription",
    "RunStreamGrant",
    "SessionAuthenticationPort",
    "SessionCookieSettings",
    "WorkflowCommand",
    "WorkflowCommandMetadata",
    "WorkflowCommandOperation",
    "WorkflowCommandPort",
    "WorkflowCommandResult",
    "api_dependencies",
    "require_actor",
]
