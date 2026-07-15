"""Resumable Server-Sent-Events transport for Run events (M4c Task 15a).

``GET /api/v1/runs/{id}/events`` streams a Run's persisted RunEvents as SSE.
The SQLite event store is the sole authority for both delivery and resumability:

* every event is encoded with the FROZEN ``encode_sse_event`` and its ``id`` is
  the persisted ``seq`` — never invented — so a client dedupes by ``(run_id, seq)``
  and resumes by echoing the last ``seq`` via ``Last-Event-ID``;
* the in-process :class:`RunEventNotifier` is a latency hint ONLY. After any wait
  the loop REREADS the DB (``list_events(after_seq=last)``), so a missed/lost
  notification never loses an event and an API/worker restart never loses backlog;
* heartbeats are SSE comments that do NOT advance the resume cursor;
* the earliest retained cursor is derived from the retained events (MIN seq), never
  a speculative authority column; a resume below it fails ``410`` with
  ``Problem.earliest_cursor``;
* authorization reloads the current Principal/roles and RBAC-authorizes a read of
  the loaded Run on EVERY connection/reconnect.

Backpressure is pull-based: the async body only pages the next bounded window when
the transport is ready to send, and the notifier never buffers events, so a slow
consumer throttles the generator instead of growing an unbounded in-memory queue.
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

from gameforge.apps.api.dependencies import (
    ApiDependencies,
    RunEventNotifierPort,
    RunEventStreamConfig,
    RunEventStreamPort,
    RunStreamGrant,
    api_dependencies,
    require_actor,
)
from gameforge.contracts.api import encode_sse_event
from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import (
    CursorExpired,
    DependencyUnavailable,
    IntegrityViolation,
    NotFound,
)
from gameforge.contracts.identity import (
    ActorContext,
    DomainRegistryV1,
    DomainScope,
    DomainScopeValue,
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


ReadScopeFactory = Callable[[], AbstractContextManager[RunEventReadScope]]


def _all_active_domain_scope(registry: DomainRegistryV1) -> DomainScope:
    active = tuple(
        definition.domain_id for definition in registry.definitions if definition.status == "active"
    )
    if not active:
        raise IntegrityViolation("domain registry has no active domain for run event read")
    return DomainScope(domain_ids=active)


def _resolve_run_read_domain(run: RunRecord, registry: DomainRegistryV1) -> DomainScopeValue:
    """Derive the Run's read domain SERVER-SIDE from its immutable payload.

    Generation/constraint-proposal payloads carry an authoritative ``domain_scope``
    (proven at admission). Every other kind exposes no per-Run domain binding yet,
    so — matching admission's posture — read is fail-closed to authority over every
    active registry domain rather than trusting any narrower claim.
    """

    scope = getattr(run.payload.params, "domain_scope", None)
    if isinstance(scope, DomainScope):
        return scope
    return _all_active_domain_scope(registry)


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
        after_seq: int,
    ) -> RunStreamGrant:
        with self._read_scope() as scope:
            run = scope.runs.get_run_projection(run_id)
            if run is None:
                raise NotFound("run is unavailable", run_id=run_id)
            _role_policy, registry = self._load_authority(scope.policies)
            permission = Permission(
                action="read",
                resource_kind="run",
                domain_scope=_resolve_run_read_domain(run, registry),
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
            # Reauthorizes the current Principal/roles against the loaded Run on
            # every connection/reconnect; raises Forbidden on a revoked read.
            authorization.require_singular(
                principal=actor.principal,
                permission=permission,
                query_hash=query_hash,
            )
            earliest = self._earliest_retained_seq(scope.runs, run_id)
            if earliest is not None and after_seq >= 1 and after_seq + 1 < earliest:
                # The client's contiguous prefix continuation is impossible: events
                # between ``after_seq+1`` and ``earliest-1`` were pruned.
                raise CursorExpired(
                    "run events before the resume cursor are no longer retained",
                    run_id=run_id,
                    earliest_cursor=str(earliest),
                )
            return RunStreamGrant(earliest_retained_seq=earliest)

    def read_events(
        self,
        run_id: str,
        after_seq: int,
        limit: int,
    ) -> tuple[RunEvent, ...]:
        with self._read_scope() as scope:
            return scope.runs.stream_events(run_id, after_seq=after_seq, limit=limit)

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
        self._loop.call_soon_threadsafe(self._event.set)

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
    after_seq: int,
    read_events: Callable[[str, int, int], tuple[RunEvent, ...]],
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
    while True:
        page = read_events(run_id, last, config.page_limit)
        for event in page:
            yield encode_sse_event(event)
            last = event.seq
            if event.event_type in _TERMINAL_EVENT_TYPES:
                return
        if len(page) >= config.page_limit:
            # A full page implies more backlog: reread immediately, no wait.
            continue
        if is_disconnected is not None and await is_disconnected():
            return
        notified = await subscription.wait(config.heartbeat_seconds)
        if not notified:
            # Idle keep-alive; a comment does not advance the resume cursor. The
            # loop then rereads the DB regardless (authority), catching any commit.
            yield HEARTBEAT_COMMENT


class RunEventSubscriptionLike(Protocol):
    async def wait(self, timeout: float) -> bool: ...


def _parse_last_event_id(request: Request) -> int:
    """Parse ``Last-Event-ID`` -> resume cursor; malformed ids restart from 0."""

    raw = request.headers.get("last-event-id")
    if raw is None:
        return 0
    text = raw.strip()
    if not text or len(text) > 32 or not text.isdigit():
        return 0
    value = int(text)
    if value < 0 or value > _MAX_LAST_EVENT_ID:
        return 0
    return value


def _stream_body(
    *,
    port: RunEventStreamPort,
    notifier: RunEventNotifierPort,
    run_id: str,
    after_seq: int,
    config: RunEventStreamConfig,
    request: Request,
) -> AsyncIterator[str]:
    async def _generator() -> AsyncIterator[str]:
        subscription = notifier.subscribe(run_id)

        async def _disconnected() -> bool:
            return await request.is_disconnected()

        try:
            async for chunk in render_run_event_stream(
                run_id=run_id,
                after_seq=after_seq,
                read_events=port.read_events,
                subscription=subscription,
                config=config,
                is_disconnected=_disconnected,
            ):
                yield chunk
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
