"""Terminal-publication adapters binding the Task-9 ``TerminalPublisher`` to SQL.

The Task-9 :class:`gameforge.platform.publication.TerminalPublisher` is the generic
terminal engine; it writes through six injected ports (``artifacts``/``blobs``/
``findings``/``ledger``/``audit`` + the workflow-effect resolver). Task 9 exercised
those ports with in-memory doubles; this module supplies the concrete, transaction-
bound production adapters the worker composition root binds into
``RunLifecycleCapabilities.publication``:

* :class:`WorkerBlobStore` — ``BlobStore`` over ``LocalObjectStore`` + a shared
  in-process location registry (the ObjectStore is keyed by ``ObjectLocation`` while
  ``BlobStore`` is keyed by ``ObjectRef``);
* :class:`WorkerArtifactPort` — ``ArtifactPort`` that binds each content-addressed
  blob (``bind_verified``) before persisting the ``ArtifactV2`` row (the Task-10
  ``ArtifactV2`` vs ``ArtifactWire`` signature gap);
* :class:`WorkerManifestLedger` — ``ManifestLedger`` over ``SqlRunRepository``
  (prompt links / closed attempt failures / finding-link writes); the four
  RECORD/REPLAY cassette suppliers return empty for ``not_applicable``/``live`` Runs
  (RECORD/REPLAY is Task 18);
* :class:`WorkerAuditPort` — ``AuditPort`` over the platform ``AuditGate``;
* :class:`WorkerCommandPublicationGateway` — the ``RunPublicationGateway`` the worker
  claim uses (records the ``attempt.leased`` claim through audit).
"""

from __future__ import annotations

import threading

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    RunAttempt,
    RunEvent,
    RunFindingLinkV1,
    RunIntermediateArtifactLinkV1,
    RunLease,
    RunRecord,
)
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditActor,
    AuditCorrelation,
    AuditSubject,
    ObjectLocation,
    ObjectRef,
)
from gameforge.platform.audit.gate import AuditGate


class BlobLocationRegistry:
    """In-process ``ObjectRef.key -> ObjectLocation`` bridge (executor -> terminal).

    The executor's ``PreparedArtifactStore`` content-addresses each prepared blob into
    the ObjectStore and knows its exact ``ObjectLocation``; the terminal publisher later
    re-reads that blob by ``ObjectRef`` alone (no backend generation) and binds it.
    Because both run in the SAME worker process — the executor lane then the control
    lane — this registry carries the exact location forward. It holds no authority: a
    crash re-executes the attempt at-least-once, repopulating it before the retry's
    terminal publish, and the DB RunStore remains the queue authority.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_key: dict[str, ObjectLocation] = {}

    def record(self, ref: ObjectRef, location: ObjectLocation) -> None:
        with self._lock:
            self._by_key[ref.key] = location

    def location_for(self, ref: ObjectRef) -> ObjectLocation:
        with self._lock:
            location = self._by_key.get(ref.key)
        if location is None:
            raise IntegrityViolation(
                "prepared blob location is not registered for the terminal publish",
                object_key=ref.key,
            )
        return location


class WorkerBlobStore:
    """The publisher's ``BlobStore`` over ``LocalObjectStore`` + the location registry."""

    def __init__(self, object_store: object, registry: BlobLocationRegistry) -> None:
        self._object_store = object_store
        self._registry = registry

    def read(self, object_ref: ObjectRef) -> bytes:
        location = self._registry.location_for(object_ref)
        with self._object_store.open(location) as stream:  # type: ignore[attr-defined]
            return stream.read()

    def put(self, payload: bytes) -> ObjectRef:
        stored = self._object_store.put_verified(payload)  # type: ignore[attr-defined]
        self._registry.record(stored.ref, stored.location)
        return stored.ref


class WorkerArtifactPort:
    """The publisher's ``ArtifactPort`` that binds each blob before its row is written.

    ``SqlArtifactRepository.put`` requires an active ``ObjectBinding`` for an
    ``ArtifactV2``'s ``object_ref``; the content-addressed blob was published by the
    executor / manifest writer (its exact ``ObjectLocation`` is in the shared registry).
    ``put`` therefore ``bind_verified``s the ref->location (idempotent for an existing
    identical active binding) before persisting the row, in the same terminal UoW.
    """

    def __init__(
        self,
        *,
        artifacts: object,
        object_bindings: object,
        registry: BlobLocationRegistry,
    ) -> None:
        self._artifacts = artifacts
        self._object_bindings = object_bindings
        self._registry = registry

    def get(self, artifact_id: str) -> object | None:
        return self._artifacts.get(artifact_id)  # type: ignore[attr-defined]

    def put(self, artifact: ArtifactV2) -> ArtifactV2:
        location = self._registry.location_for(artifact.object_ref)
        self._object_bindings.bind_verified(artifact.object_ref, location, None)  # type: ignore[attr-defined]
        return self._artifacts.put(artifact)  # type: ignore[attr-defined]


class WorkerManifestLedger:
    """The publisher's ``ManifestLedger`` over ``SqlRunRepository``.

    ``prompt_links`` / ``closed_attempt_failures`` / ``put_finding_link`` are real
    reads/writes over the DB queue authority. The four RECORD/REPLAY runtime-parent
    suppliers return empty because a ``not_applicable``/``live`` Run has no recorded
    cassette shards/bundles; the RECORD/REPLAY cassette wiring is Task 18.
    """

    def __init__(self, runs: object) -> None:
        self._runs = runs

    def prompt_links(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[RunIntermediateArtifactLinkV1, ...]:
        return self._runs.list_prompt_render_links(run_id, attempt_no=attempt_no)  # type: ignore[attr-defined]

    def closed_attempt_failures(self, run_id: str) -> tuple[tuple[int, str], ...]:
        return self._runs.list_closed_attempt_failures(run_id)  # type: ignore[attr-defined]

    def put_finding_link(self, link: RunFindingLinkV1) -> None:
        self._runs.put_finding_link(link)  # type: ignore[attr-defined]

    def record_shard_links(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[tuple[int, int, str], ...]:
        return ()

    def attempt_cassette_bundle(self, run_id: str, *, attempt_no: int) -> str | None:
        return None

    def run_cassette_bundle(self, run_id: str) -> str | None:
        return None

    def replay_input_cassette(self, run_id: str) -> str | None:
        return None


class WorkerAuditPort:
    """The publisher's ``AuditPort`` over the platform ``AuditGate`` run-audit chain."""

    def __init__(self, *, audit_gate: AuditGate, chain_id: str) -> None:
        self._audit_gate = audit_gate
        self._chain_id = chain_id

    def record(
        self,
        *,
        action: str,
        run: RunRecord,
        artifact_id: str | None,
        actor: AuditActor,
        occurred_at: str,
    ) -> None:
        del occurred_at  # AuditGate stamps the authoritative ts from its own clock.
        self._audit_gate.append(
            chain_id=self._chain_id,
            actor=actor,
            initiated_by=run.initiated_by,
            action=action,
            subject=AuditSubject(
                resource_kind="run",
                resource_id=run.run_id,
                artifact_id=artifact_id,
            ),
            correlation=AuditCorrelation(request_id=None, run_id=run.run_id, trace_id=None),
        )


class WorkerCommandPublicationGateway:
    """The claim-scope ``RunPublicationGateway`` (records the fenced claim in audit).

    The worker's ``RunCommandService`` only ever calls ``claim_next`` ->
    ``record_run_claimed``; the create / submit / prompt-render / terminal surfaces are
    owned by the API admission engine and the lifecycle terminal publisher, so the
    LLM/prompt surfaces (Task 18 RECORD/REPLAY) fail closed here rather than fabricating
    authority. Every claim is audited so the queue transition is authoritative.
    """

    def __init__(self, *, audit_gate: AuditGate, chain_id: str) -> None:
        self._audit_gate = audit_gate
        self._chain_id = chain_id

    def record_run_created(self, *, run: RunRecord, event: RunEvent) -> None:
        self._append(action="run.queued", run=run, event=event, actor=run.initiated_by)

    def record_run_claimed(
        self,
        *,
        previous: RunRecord,
        run: RunRecord,
        attempt: RunAttempt,
        lease: RunLease,
        event: RunEvent,
        actor: AuditActor,
    ) -> None:
        del previous, attempt, lease
        self._append(action="run.attempt_leased", run=run, event=event, actor=actor)

    def record_run_terminal(
        self,
        *,
        run: RunRecord,
        attempt: RunAttempt | None,
        event: RunEvent,
        actor: AuditActor,
    ) -> None:
        del attempt
        self._append(action="run.terminal", run=run, event=event, actor=actor)

    def record_command_submitted(
        self,
        *,
        run: RunRecord,
        record: object,
        events: tuple[RunEvent, ...],
        actor: AuditActor,
    ) -> None:
        del record
        self._append(
            action="run.command_submitted",
            run=run,
            event=events[-1] if events else None,
            actor=actor,
        )

    def record_command_completed(
        self,
        *,
        run: RunRecord,
        record: object,
        event: RunEvent,
        actor: AuditActor,
    ) -> None:
        del record
        self._append(action="run.command_completed", run=run, event=event, actor=actor)

    def get_prompt_replay(self, **_: object) -> None:
        raise IntegrityViolation("worker prompt-render publication is Task 18 (RECORD/REPLAY)")

    def publish_prompt_rendered(self, **_: object) -> None:
        raise IntegrityViolation("worker prompt-render publication is Task 18 (RECORD/REPLAY)")

    def publish_run_failure(self, **_: object) -> None:
        raise IntegrityViolation(
            "run-failure publication flows through the lifecycle terminal publisher"
        )

    def _append(
        self,
        *,
        action: str,
        run: RunRecord,
        event: RunEvent | None,
        actor: AuditActor,
    ) -> None:
        self._audit_gate.append(
            chain_id=self._chain_id,
            actor=actor,
            initiated_by=run.initiated_by,
            action=action,
            subject=AuditSubject(resource_kind="run", resource_id=run.run_id),
            correlation=AuditCorrelation(
                request_id=None,
                run_id=run.run_id,
                trace_id=event.trace_id if event is not None else None,
            ),
        )


__all__ = [
    "BlobLocationRegistry",
    "WorkerArtifactPort",
    "WorkerAuditPort",
    "WorkerBlobStore",
    "WorkerCommandPublicationGateway",
    "WorkerManifestLedger",
]
