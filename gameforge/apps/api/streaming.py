"""Resumable Server-Sent-Events transport for Run events (M4c Task 15a).

``GET /api/v1/runs/{id}/events`` streams a Run's persisted RunEvents as SSE.
The SQLite event store is the sole authority for both delivery and resumability:

* every event is encoded with the FROZEN ``encode_sse_event`` and its ``id`` is
  the persisted ``seq`` — never invented — so a client dedupes by ``(run_id, seq)``
  and resumes by echoing the last ``seq`` via ``Last-Event-ID``;
* the in-process :class:`RunEventNotifier` is a latency hint ONLY. After any wait
  the loop REREADS the DB (``read_authorized_page(after_seq=last)``), so a missed/lost
  notification never loses an event and an API/worker restart never loses backlog;
* heartbeats are SSE comments that do NOT advance the resume cursor;
* the earliest retained cursor is derived from the retained events (MIN seq), never
  a speculative authority column; a resume below it fails ``410`` with
  ``Problem.earliest_cursor``;
* authorization uses the admission-frozen Run domain and reauthenticates/re-authorizes
  at connection time and each bounded page boundary; revocation stops later pages.

Backpressure is pull-based: the async body only pages the next bounded window when
the transport is ready to send, and the notifier never buffers events, so a slow
consumer throttles the generator instead of growing an unbounded in-memory queue.

Live-latency note: correctness never depends on `notify()` (the loop rereads the DB
after every wait), so with no producer calling `notify()` live delivery is paced by
`heartbeat_seconds`. Sub-heartbeat live latency requires calling `notify()` at the
RunEvent-append site. Because `platform` cannot import `apps`, and the append site can
live in a separate worker process, that wiring is an INJECTED notifier callback owned by
the Task-18 publisher/worker composition — a cross-process concern deferred there, not
built here. This module keeps a modest default heartbeat so delivery is not laggy.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
import threading
from typing import Protocol

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from gameforge.apps.api.dependencies import (
    ApiDependencies,
    RunEventNotifierPort,
    RunEventPage,
    RunEventStreamConfig,
    RunEventStreamPort,
    RunStreamGrant,
    api_dependencies,
    require_actor,
)
from gameforge.apps.api.run_read_domain import (
    ApprovalItemReader,
    resolve_run_read_domain as _resolve_run_read_domain,
)
from gameforge.contracts.auth import ApiKeyAuthRequestV1, ApiKeySecret, SessionToken
from gameforge.contracts.api import encode_sse_event
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import (
    AuthError,
    CursorExpired,
    CursorInvalid,
    DependencyUnavailable,
    GameForgeError,
    IntegrityViolation,
    NotFound,
)
from gameforge.contracts.identity import (
    ActorContext,
    DomainRegistryV1,
    Permission,
    RolePolicy,
)
from gameforge.contracts.jobs import RunEvent, RunRecord
from gameforge.platform.read_models.authorization import (
    ReadAuthorizationService,
    ReadPolicyRepository,
)

# The four terminal RunEventTypes from the frozen state-machine table
# (contracts/jobs.py ``_RUN_EVENT_DEFINITIONS``): once one is emitted the stream
# closes because no further events can be committed for the Run.
_TERMINAL_EVENT_TYPES = frozenset({"run.succeeded", "run.failed", "run.cancelled", "run.timed_out"})
_TERMINAL_RUN_STATUSES = frozenset({"succeeded", "failed", "cancelled", "timed_out"})

# A comment line (leading colon) is an SSE keep-alive that carries no ``id`` and
# therefore never advances the client's resume cursor.
HEARTBEAT_COMMENT = ": keep-alive\n\n"

_MAX_LAST_EVENT_ID = (1 << 63) - 1


class RunEventReadRepository(Protocol):
    """The bounded, retention-tolerant Run/event read surface the SSE stream composes.

    ``get_run_projection`` loads the Run without asserting event-head contiguity (a
    retention-pruned prefix is legitimate); ``stream_events`` pages the retained
    events; ``earliest_event_seq`` derives MIN(seq) from the actual store.
    """

    def get_run_projection(self, run_id: str) -> RunRecord | None: ...

    def earliest_event_seq(self, run_id: str) -> int | None: ...

    def stream_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        limit: int = 100,
    ) -> tuple[RunEvent, ...]: ...


@dataclass(frozen=True, slots=True)
class RunEventReadScope:
    """One short read transaction's capabilities for a single stream operation."""

    runs: RunEventReadRepository
    policies: ReadPolicyRepository
    approvals: ApprovalItemReader | None = None


ReadScopeFactory = Callable[[], AbstractContextManager[RunEventReadScope]]


class RunEventStreamService:
    """Reauthorizing, bounded read authority behind :class:`RunEventStreamPort`."""

    def __init__(
        self,
        *,
        read_scope: ReadScopeFactory,
        role_policy_version: str,
        role_policy_digest: str,
    ) -> None:
        if not callable(read_scope):
            raise TypeError("read_scope must be a callable context-manager factory")
        self._read_scope = read_scope
        if not isinstance(role_policy_version, str) or not role_policy_version:
            raise ValueError("role_policy_version must be a non-empty string")
        if not isinstance(role_policy_digest, str) or len(role_policy_digest) != 64:
            raise ValueError("role_policy_digest must be a SHA-256 digest")
        self._role_policy_version = role_policy_version
        self._role_policy_digest = role_policy_digest

    def authorize_stream(
        self,
        *,
        run_id: str,
        actor: ActorContext,
        after_seq: int | None,
    ) -> RunStreamGrant:
        with self._read_scope() as scope:
            run, earliest, latest, terminal = self._authorize_state(
                scope=scope,
                run_id=run_id,
                actor=actor,
                after_seq=after_seq,
            )
            del run
            return RunStreamGrant(
                earliest_retained_seq=earliest,
                latest_event_seq=latest,
                terminal=terminal,
            )

    def read_authorized_page(
        self,
        *,
        run_id: str,
        actor: ActorContext,
        after_seq: int | None,
        limit: int,
    ) -> RunEventPage:
        """Reauthorize and read one page + cursor bounds in one short transaction."""

        with self._read_scope() as scope:
            _run, earliest, latest, terminal = self._authorize_state(
                scope=scope,
                run_id=run_id,
                actor=actor,
                after_seq=after_seq,
            )
            events = scope.runs.stream_events(
                run_id,
                after_seq=0 if after_seq is None else after_seq,
                limit=limit,
            )
            self._validate_page(
                run_id=run_id,
                after_seq=after_seq,
                earliest=earliest,
                latest=latest,
                events=events,
            )
            return RunEventPage(
                events=events,
                earliest_retained_seq=earliest,
                latest_event_seq=latest,
                terminal=terminal,
            )

    def _authorize_state(
        self,
        *,
        scope: RunEventReadScope,
        run_id: str,
        actor: ActorContext,
        after_seq: int | None,
    ) -> tuple[RunRecord, int | None, int, bool]:
        run = scope.runs.get_run_projection(run_id)
        if run is None:
            raise NotFound("run is unavailable", run_id=run_id)
        _role_policy, registry = self._load_authority(scope.policies)
        permission = Permission(
            action="read",
            resource_kind="run",
            domain_scope=_resolve_run_read_domain(run, registry, scope.approvals),
        )
        query_hash = canonical_sha256(
            {
                "query_schema_version": "run-events-stream-query@1",
                "run_id": run.run_id,
                "run_revision": run.revision,
            }
        )
        authorization = ReadAuthorizationService(
            policy_repository=scope.policies,
            role_policy_version=self._role_policy_version,
            role_policy_digest=self._role_policy_digest,
        )
        authorization.require_singular(
            principal=actor.principal,
            permission=permission,
            query_hash=query_hash,
        )
        earliest = self._earliest_retained_seq(scope.runs, run_id)
        latest = run.next_event_seq - 1
        if latest >= 1 and earliest is None:
            raise IntegrityViolation("Run event head has no retained event", run_id=run_id)
        self._validate_cursor(
            run_id=run_id,
            after_seq=after_seq,
            earliest=earliest,
            latest=latest,
        )
        return run, earliest, latest, run.status in _TERMINAL_RUN_STATUSES

    @staticmethod
    def _validate_cursor(
        *,
        run_id: str,
        after_seq: int | None,
        earliest: int | None,
        latest: int,
    ) -> None:
        if after_seq is None:
            return
        if isinstance(after_seq, bool) or not isinstance(after_seq, int) or after_seq < 0:
            raise CursorInvalid("Run event cursor must be a nonnegative integer")
        if after_seq > latest:
            raise CursorInvalid(
                "Run event cursor is beyond the persisted event head",
                run_id=run_id,
            )
        if earliest is not None and after_seq + 1 < earliest:
            raise CursorExpired(
                "run events before the resume cursor are no longer retained",
                run_id=run_id,
                earliest_cursor=str(earliest),
            )

    @staticmethod
    def _validate_page(
        *,
        run_id: str,
        after_seq: int | None,
        earliest: int | None,
        latest: int,
        events: tuple[RunEvent, ...],
    ) -> None:
        if not events:
            if after_seq is not None and after_seq < latest:
                raise IntegrityViolation(
                    "Run event page is empty before the persisted event head",
                    run_id=run_id,
                )
            return
        expected_first = earliest if after_seq is None else after_seq + 1
        if expected_first is None:
            raise IntegrityViolation(
                "Run event page exists without a retention head", run_id=run_id
            )
        if events[0].seq > expected_first:
            raise CursorExpired(
                "Run events were pruned before the requested page could be delivered",
                run_id=run_id,
                earliest_cursor=str(events[0].seq),
            )
        if events[0].seq != expected_first:
            raise IntegrityViolation("Run event page did not start at its expected cursor")
        expected = expected_first
        for event in events:
            if event.run_id != run_id or event.seq != expected or event.seq > latest:
                raise IntegrityViolation("Run event page is not a contiguous persisted sequence")
            expected += 1

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
                "run event stream role policy is unavailable",
                component="run_event_stream_authorization",
            )
        registry = policies.get_domain_registry(role_policy.domain_registry_ref)
        if not isinstance(registry, DomainRegistryV1):
            raise DependencyUnavailable(
                "run event stream domain registry is unavailable",
                component="run_event_stream_authorization",
            )
        return role_policy, registry

    @staticmethod
    def _earliest_retained_seq(
        runs: RunEventReadRepository,
        run_id: str,
    ) -> int | None:
        # MIN(seq) for the Run derived from the actual store. No separate
        # retention/TTL column exists; the retained events ARE the retention state.
        return runs.earliest_event_seq(run_id)


class _RunEventSubscription:
    """One loop-bound waiter registered with a :class:`RunEventNotifier`."""

    __slots__ = ("_notifier", "_run_id", "_event", "_loop", "_closed")

    def __init__(self, notifier: "RunEventNotifier", run_id: str) -> None:
        self._notifier = notifier
        self._run_id = run_id
        self._event = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        self._closed = False

    def _wake(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:
            # The waiter's loop has already closed (a stale subscriber). Skip it so a
            # cross-thread notify() still fans out to the remaining live waiters.
            pass

    async def wait(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return False
        self._event.clear()
        return True

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._notifier._unsubscribe(self._run_id, self)


class RunEventNotifier:
    """In-process, non-authoritative pub-sub — a latency hint, never a queue.

    ``notify`` only wakes waiters; it carries no payload and buffers nothing, so a
    lost notification is harmless (the loop rereads the DB) and a restart loses no
    backlog (all events come from the DB). ``notify`` is safe to call from any
    thread (e.g. the worker): each waiter is woken through its own event loop.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscriptions: dict[str, set[_RunEventSubscription]] = {}

    def subscribe(self, run_id: str) -> _RunEventSubscription:
        subscription = _RunEventSubscription(self, run_id)
        with self._lock:
            self._subscriptions.setdefault(run_id, set()).add(subscription)
        return subscription

    def _unsubscribe(self, run_id: str, subscription: _RunEventSubscription) -> None:
        with self._lock:
            waiters = self._subscriptions.get(run_id)
            if waiters is not None:
                waiters.discard(subscription)
                if not waiters:
                    self._subscriptions.pop(run_id, None)

    def notify(self, run_id: str) -> None:
        with self._lock:
            waiters = tuple(self._subscriptions.get(run_id, ()))
        for waiter in waiters:
            waiter._wake()


async def render_run_event_stream(
    *,
    run_id: str,
    after_seq: int | None,
    read_page: Callable[[str, ActorContext, int | None, int], Awaitable[RunEventPage]],
    refresh_actor: Callable[[], Awaitable[ActorContext]],
    subscription: "RunEventSubscriptionLike",
    config: RunEventStreamConfig,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    """Yield SSE chunks: drain backlog -> wait hint/heartbeat -> REREAD DB.

    The DB is authority: the cursor only ever advances to a delivered persisted
    ``seq``, and every wait is followed by a reread, so there is no committed-event
    gap under the read/commit boundary race and a lost notification is caught by
    the next reread. The stream closes on a terminal event or client disconnect.
    """

    last = after_seq
    heartbeat_due = False
    while True:
        actor = await refresh_actor()
        page = await read_page(run_id, actor, last, config.page_limit)
        for event in page.events:
            yield encode_sse_event(event)
            last = event.seq
            if event.event_type in _TERMINAL_EVENT_TYPES:
                return
        if page.terminal and last is not None and last >= page.latest_event_seq:
            return
        if len(page.events) >= config.page_limit:
            # A full page implies more backlog: reread immediately, no wait.
            heartbeat_due = False
            continue
        if page.events:
            heartbeat_due = False
        elif heartbeat_due:
            # A timeout is only rendered after a fresh authorized DB reread. This
            # prevents a post-revocation heartbeat and catches commits made during wait.
            yield HEARTBEAT_COMMENT
            heartbeat_due = False
        if is_disconnected is not None and await is_disconnected():
            return
        notified = await subscription.wait(config.heartbeat_seconds)
        if not notified:
            heartbeat_due = True


class RunEventSubscriptionLike(Protocol):
    async def wait(self, timeout: float) -> bool: ...


def _parse_last_event_id(request: Request) -> int | None:
    """Parse ``Last-Event-ID``; only an absent header denotes a fresh cursor."""

    raw = request.headers.get("last-event-id")
    if raw is None:
        return None
    text = raw
    if (
        not text
        or text != text.strip()
        or len(text) > 32
        or (len(text) > 1 and text.startswith("0"))
        or any(character < "0" or character > "9" for character in text)
    ):
        raise CursorInvalid("Last-Event-ID is not a canonical decimal cursor")
    value = int(text)
    if value < 0 or value > _MAX_LAST_EVENT_ID:
        raise CursorInvalid("Last-Event-ID is outside the supported cursor range")
    return value


def _refresh_http_actor(
    *,
    request: Request,
    dependencies: ApiDependencies,
    initial_actor: ActorContext,
) -> ActorContext:
    """Reauthenticate the original HTTP credential at a bounded-page boundary."""

    request_id = getattr(request.state, "request_id", initial_actor.request_id)
    token = getattr(request.state, "session_token", None)
    if isinstance(token, SessionToken):
        service = dependencies.session_authentication
        if service is None:
            raise DependencyUnavailable(
                "session authentication is unavailable during Run event streaming",
                component="session_authentication",
            )
        refreshed = service.resolve(
            token,
            csrf_token=None,
            request_method="GET",
            request_id=request_id,
        )
    else:
        authorization = request.headers.get("authorization")
        if authorization is None:
            # Side-effect-free app tests may override ``require_actor`` without wiring
            # a credential service. Production middleware never reaches this branch.
            refreshed = initial_actor
        else:
            scheme, separator, secret = authorization.partition(" ")
            if scheme != "ApiKey" or not separator or not secret or " " in secret:
                raise AuthError("API-key authorization header is invalid")
            service = dependencies.api_key_authentication
            if service is None:
                raise DependencyUnavailable(
                    "API-key authentication is unavailable during Run event streaming",
                    component="api_key_authentication",
                )
            refreshed = service.authenticate(
                ApiKeyAuthRequestV1(api_key=ApiKeySecret(secret)),
                request_id=request_id,
            )
    if (
        not isinstance(refreshed, ActorContext)
        or refreshed.principal.id != initial_actor.principal.id
        or refreshed.principal.kind != initial_actor.principal.kind
    ):
        raise IntegrityViolation("stream credential resolved to a different principal")
    return refreshed


def _stream_body(
    *,
    port: RunEventStreamPort,
    notifier: RunEventNotifierPort,
    run_id: str,
    after_seq: int | None,
    config: RunEventStreamConfig,
    request: Request,
    dependencies: ApiDependencies,
    initial_actor: ActorContext,
) -> AsyncIterator[str]:
    async def _generator() -> AsyncIterator[str]:
        subscription = notifier.subscribe(run_id)

        async def _disconnected() -> bool:
            return await request.is_disconnected()

        async def _refresh_actor() -> ActorContext:
            return await run_in_threadpool(
                _refresh_http_actor,
                request=request,
                dependencies=dependencies,
                initial_actor=initial_actor,
            )

        async def _read_page(
            selected_run_id: str,
            actor: ActorContext,
            selected_after_seq: int | None,
            limit: int,
        ) -> RunEventPage:
            return await run_in_threadpool(
                port.read_authorized_page,
                run_id=selected_run_id,
                actor=actor,
                after_seq=selected_after_seq,
                limit=limit,
            )

        try:
            try:
                async for chunk in render_run_event_stream(
                    run_id=run_id,
                    after_seq=after_seq,
                    read_page=_read_page,
                    refresh_actor=_refresh_actor,
                    subscription=subscription,
                    config=config,
                    is_disconnected=_disconnected,
                ):
                    yield chunk
            except GameForgeError:
                # Response headers are already committed. Close without inventing an
                # unpersisted SSE error event; reconnect returns the typed HTTP Problem.
                return
        finally:
            subscription.close()

    return _generator()


def _stream_dependencies(
    dependencies: ApiDependencies,
) -> tuple[RunEventStreamPort, RunEventNotifierPort, RunEventStreamConfig]:
    port = dependencies.run_event_stream
    notifier = dependencies.run_event_notifier
    if port is None or notifier is None:
        raise DependencyUnavailable(
            "run event stream authority is unavailable",
            component="run_event_stream",
        )
    return port, notifier, dependencies.run_event_stream_config


def run_events_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["runs"])

    @router.get("/runs/{run_id}/events")
    def stream_run_events(
        run_id: str,
        request: Request,
        actor: ActorContext = Depends(require_actor),
        dependencies: ApiDependencies = Depends(api_dependencies),
    ) -> StreamingResponse:
        port, notifier, config = _stream_dependencies(dependencies)
        after_seq = _parse_last_event_id(request)
        # Authenticate (via require_actor/middleware) + authorize on EVERY
        # connection/reconnect BEFORE streaming, so 404/403/410 surface as real
        # HTTP status codes rather than mid-stream (the body is already 200).
        grant = port.authorize_stream(run_id=run_id, actor=actor, after_seq=after_seq)
        headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        if grant.earliest_retained_seq is not None:
            headers["X-Earliest-Event-Cursor"] = str(grant.earliest_retained_seq)
        return StreamingResponse(
            _stream_body(
                port=port,
                notifier=notifier,
                run_id=run_id,
                after_seq=after_seq,
                config=config,
                request=request,
                dependencies=dependencies,
                initial_actor=actor,
            ),
            media_type="text/event-stream",
            headers=headers,
        )

    return router


__all__ = [
    "HEARTBEAT_COMMENT",
    "RunEventNotifier",
    "RunEventReadRepository",
    "RunEventReadScope",
    "RunEventStreamService",
    "render_run_event_stream",
    "run_events_router",
]
