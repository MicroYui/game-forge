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
* :class:`WorkerBlobStager` — the only terminal-output ObjectStore writer, invoked
  between the read plan and the write UoW;
* :class:`WorkerArtifactPort` — ``ArtifactPort`` that consumes an explicit staged
  receipt, re-stats its generation, and binds/reuses an active ``ObjectBinding``
  before persisting the ``ArtifactV2`` row;
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

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import Conflict, IntegrityViolation
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
    ExecutionIdentityV1,
    ObjectLocation,
    ObjectRef,
)
from gameforge.contracts.routing import RoutingDecisionV1
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.terminal_staging import (
    StagedReceipt,
    StagedTerminalPublication,
    TerminalPublicationDraft,
)


class BlobLocationRegistry:
    """In-process ``ObjectRef.key -> ObjectLocation`` bridge (executor -> terminal).

    The executor's ``PreparedArtifactStore`` content-addresses each prepared blob into
    the ObjectStore and knows its exact ``ObjectLocation``; the terminal planner later
    re-reads that explicit generation and cross-checks this process-local handoff.
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
    """Read exact prepared blobs; terminal output staging is a separate capability."""

    def __init__(self, object_store: object, registry: BlobLocationRegistry) -> None:
        self._object_store = object_store
        self._registry = registry

    def read(self, object_ref: ObjectRef, location: ObjectLocation) -> bytes:
        registered = self._registry.location_for(object_ref)
        if registered != location:
            raise IntegrityViolation(
                "prepared blob location differs from the worker's verified location",
                object_key=object_ref.key,
            )
        with self._object_store.open(location) as stream:  # type: ignore[attr-defined]
            return stream.read()


class WorkerBlobStager:
    """Materialize complete terminal drafts outside the database UoW.

    A partial upload failure intentionally leaves only verified, unbound objects for
    GC.  No receipt is returned until every material of every draft has been written
    and ``stat`` has reproduced the exact ref/generation pair.
    """

    def __init__(self, object_store: object) -> None:
        self._object_store = object_store

    def stage(
        self, drafts: tuple[TerminalPublicationDraft, ...]
    ) -> tuple[StagedTerminalPublication, ...]:
        staged: list[StagedTerminalPublication] = []
        for draft in drafts:
            slots: set[str] = set()
            receipts: list[StagedReceipt] = []
            for material in draft.materials:
                if material.slot in slots:
                    raise IntegrityViolation(
                        "terminal publication draft contains a duplicate blob slot",
                        slot=material.slot,
                    )
                slots.add(material.slot)
                stored = self._object_store.put_verified(material.payload)  # type: ignore[attr-defined]
                if stored.ref != material.expected_ref:
                    raise IntegrityViolation(
                        "ObjectStore staged a different content-addressed ref",
                        slot=material.slot,
                    )
                stat = self._object_store.stat(stored.location)  # type: ignore[attr-defined]
                if stat.ref != stored.ref or stat.location != stored.location:
                    raise IntegrityViolation(
                        "ObjectStore stat differs from the staged generation",
                        slot=material.slot,
                    )
                receipts.append(
                    StagedReceipt(
                        slot=material.slot,
                        ref=stored.ref,
                        location=stored.location,
                    )
                )
            staged.append(
                StagedTerminalPublication(
                    projection_digest=draft.projection_digest,
                    receipts=tuple(receipts),
                )
            )
        return tuple(staged)


class WorkerArtifactPort:
    """The publisher's ``ArtifactPort`` that binds each blob before its row is written.

    ``SqlArtifactRepository.put`` requires an active ``ObjectBinding`` for an
    ``ArtifactV2``'s ``object_ref``. ``put_staged`` consumes the explicit receipt
    produced outside the UoW; it never guesses a new output generation from the
    process-local prepared-blob registry. Existing immutable Artifacts keep their
    retained active binding and leave a newly staged duplicate generation to GC.
    """

    def __init__(
        self,
        *,
        artifacts: object,
        object_bindings: object,
        object_store: object,
    ) -> None:
        self._artifacts = artifacts
        self._object_bindings = object_bindings
        self._object_store = object_store

    def get(self, artifact_id: str) -> object | None:
        return self._artifacts.get(artifact_id)  # type: ignore[attr-defined]

    def _resolve_or_bind_receipt(self, receipt: StagedReceipt) -> object:
        """Retain an active same-store binding or CAS-reactivate a retired row.

        ``ObjectBindingRepository.resolve`` deliberately hides retired rows.  Its
        frozen contract exposes the retained revision through the conflict raised by
        ``bind_verified(..., expected_revision=None)``; use that revision exactly
        once to reactivate the staged generation.  An active row won by a concurrent
        publisher is resolved and retained instead of being remapped.
        """

        try:
            return self._object_bindings.resolve(  # type: ignore[attr-defined]
                receipt.ref,
                store_id=receipt.location.store_id,
            )
        except FileNotFoundError:
            pass

        try:
            binding = self._object_bindings.bind_verified(  # type: ignore[attr-defined]
                receipt.ref,
                receipt.location,
                None,
            )
        except Conflict as conflict:
            context = conflict.context
            actual_revision = context.get("actual_revision")
            actual_status = context.get("actual_status")
            if (
                context.get("object_key") != receipt.ref.key
                or context.get("store_id") != receipt.location.store_id
                or not isinstance(actual_revision, int)
                or isinstance(actual_revision, bool)
                or actual_revision < 1
                or actual_status not in {"active", "retired"}
            ):
                raise IntegrityViolation(
                    "ObjectBinding conflict did not identify an exact retained revision",
                    slot=receipt.slot,
                    object_key=receipt.ref.key,
                    store_id=receipt.location.store_id,
                ) from conflict
            if actual_status == "active":
                try:
                    return self._object_bindings.resolve(  # type: ignore[attr-defined]
                        receipt.ref,
                        store_id=receipt.location.store_id,
                    )
                except FileNotFoundError as exc:
                    raise Conflict(
                        "active ObjectBinding changed before it could be retained",
                        object_key=receipt.ref.key,
                        store_id=receipt.location.store_id,
                        expected_revision=actual_revision,
                    ) from exc
            binding = self._object_bindings.bind_verified(  # type: ignore[attr-defined]
                receipt.ref,
                receipt.location,
                actual_revision,
            )
        if binding.location != receipt.location:
            raise IntegrityViolation(
                "ObjectBinding repository returned another staged generation",
                slot=receipt.slot,
                object_key=receipt.ref.key,
            )
        return binding

    def put_staged(self, artifact: ArtifactV2, receipt: StagedReceipt) -> ArtifactV2:
        """Bind one explicit verified receipt, then persist its immutable Artifact.

        The receipt location is never recovered from the process-local key registry.
        ``stat`` is repeated inside the fresh write UoW so a substituted/deleted
        generation fails before either ObjectBinding or Artifact authority is written.
        """

        if receipt.ref != artifact.object_ref:
            raise IntegrityViolation(
                "staged receipt ObjectRef differs from the Artifact",
                slot=receipt.slot,
                artifact_id=artifact.artifact_id,
            )
        stat = self._object_store.stat(receipt.location)  # type: ignore[attr-defined]
        if stat.ref != receipt.ref or stat.location != receipt.location:
            raise IntegrityViolation(
                "staged receipt stat differs from its exact ref/generation",
                slot=receipt.slot,
                artifact_id=artifact.artifact_id,
            )
        existing_artifact = self._artifacts.get(artifact.artifact_id)  # type: ignore[attr-defined]
        if existing_artifact is not None:
            if not isinstance(existing_artifact, ArtifactV2) or canonical_json(
                existing_artifact.model_dump(mode="json", exclude={"created_at"})
            ) != canonical_json(artifact.model_dump(mode="json", exclude={"created_at"})):
                raise IntegrityViolation(
                    "Artifact id is already bound to different immutable content",
                    artifact_id=artifact.artifact_id,
                )
            binding = self._resolve_or_bind_receipt(receipt)
            retained_stat = self._object_store.stat(binding.location)  # type: ignore[attr-defined]
            if (
                binding.status != "active"
                or binding.object_ref != artifact.object_ref
                or retained_stat.ref != artifact.object_ref
                or retained_stat.location != binding.location
            ):
                raise IntegrityViolation(
                    "retained Artifact ObjectBinding is not readable and exact",
                    artifact_id=artifact.artifact_id,
                )
            # The newly staged location is a safe GC-eligible orphan.  Idempotent
            # publication must not remap an already-published immutable Artifact.
            return existing_artifact

        binding = self._resolve_or_bind_receipt(receipt)
        retained_stat = self._object_store.stat(binding.location)  # type: ignore[attr-defined]
        if (
            binding.object_ref != receipt.ref
            or binding.status != "active"
            or retained_stat.ref != receipt.ref
            or retained_stat.location != binding.location
        ):
            raise IntegrityViolation(
                "ObjectBinding repository returned another active binding",
                slot=receipt.slot,
                artifact_id=artifact.artifact_id,
            )
        return self._artifacts.put(artifact)  # type: ignore[attr-defined]

    def read_bytes(self, artifact_id: str) -> bytes:
        artifact = self._artifacts.get(artifact_id)  # type: ignore[attr-defined]
        object_ref = getattr(artifact, "object_ref", None)
        if not isinstance(object_ref, ObjectRef):
            raise IntegrityViolation(
                "published runtime Artifact has no ObjectRef",
                artifact_id=artifact_id,
            )
        binding = self._object_bindings.resolve(object_ref)  # type: ignore[attr-defined]
        with self._object_store.open(binding.location) as stream:  # type: ignore[attr-defined]
            return stream.read()  # type: ignore[no-any-return]


class WorkerManifestLedger:
    """The publisher's ``ManifestLedger`` over ``SqlRunRepository``.

    ``prompt_links`` / ``closed_attempt_failures`` / ``put_finding_link`` are real
    reads/writes over the DB queue authority. The four RECORD/REPLAY runtime-parent
    suppliers return empty because a ``not_applicable``/``live`` Run has no recorded
    cassette shards/bundles; the RECORD/REPLAY cassette wiring is Task 18.
    """

    def __init__(self, runs: object, routing: object) -> None:
        self._runs = runs
        self._routing = routing

    def prompt_links(
        self, run_id: str, *, attempt_no: int | None
    ) -> tuple[RunIntermediateArtifactLinkV1, ...]:
        return self._runs.list_prompt_render_links(run_id, attempt_no=attempt_no)  # type: ignore[attr-defined]

    def closed_attempt_failures(self, run_id: str) -> tuple[tuple[int, str], ...]:
        return self._runs.list_closed_attempt_failures(run_id)  # type: ignore[attr-defined]

    def put_finding_link(self, link: RunFindingLinkV1) -> RunFindingLinkV1:
        return self._runs.put_finding_link(link)  # type: ignore[attr-defined,no-any-return]

    def execution_identity(self, run_id: str, *, attempt_no: int | None) -> ExecutionIdentityV1:
        del run_id, attempt_no
        raise IntegrityViolation(
            "terminal execution identity authority is unavailable until model-call wiring"
        )

    def get_attempt(self, run_id: str, attempt_no: int) -> RunAttempt | None:
        return self._runs.get_attempt(run_id, attempt_no)  # type: ignore[attr-defined,no-any-return]

    def get_routing_decision(self, decision_id: str) -> RoutingDecisionV1 | None:
        return self._routing.get_routing_decision(decision_id)  # type: ignore[attr-defined,no-any-return]

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

    def record_run_created(
        self,
        *,
        run: RunRecord,
        event: RunEvent,
        request_id: str | None = None,
    ) -> None:
        del request_id
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


class WorkerCommandTerminalPublicationGateway:
    """Combine command audit hooks with the staged terminal engine.

    Claim/command/prompt surfaces remain owned by ``WorkerCommandPublicationGateway``;
    inactive cancellation's plan/commit/preflight surfaces delegate to the exact
    transaction-bound ``TerminalPublisher`` used by lifecycle publication.
    """

    def __init__(self, *, commands: WorkerCommandPublicationGateway, terminal: object) -> None:
        self._commands = commands
        self._terminal = terminal

    def record_run_created(self, **kwargs: object) -> None:
        self._commands.record_run_created(**kwargs)  # type: ignore[arg-type]

    def record_run_claimed(self, **kwargs: object) -> None:
        self._commands.record_run_claimed(**kwargs)  # type: ignore[arg-type]

    def record_command_submitted(self, **kwargs: object) -> None:
        self._commands.record_command_submitted(**kwargs)  # type: ignore[arg-type]

    def record_command_completed(self, **kwargs: object) -> None:
        self._commands.record_command_completed(**kwargs)  # type: ignore[arg-type]

    def record_run_terminal(self, **kwargs: object) -> None:
        self._commands.record_run_terminal(**kwargs)  # type: ignore[arg-type]

    def get_prompt_replay(self, **kwargs: object) -> object:
        return self._commands.get_prompt_replay(**kwargs)

    def publish_prompt_rendered(self, **kwargs: object) -> object:
        return self._commands.publish_prompt_rendered(**kwargs)

    def preflight_outcome(self, **kwargs: object) -> object:
        return self._terminal.preflight_outcome(**kwargs)  # type: ignore[attr-defined]

    def plan_run_failure(self, **kwargs: object) -> object:
        return self._terminal.plan_run_failure(**kwargs)  # type: ignore[attr-defined]

    def publish_run_failure(self, **kwargs: object) -> object:
        # This direct surface is deliberately fail-closed in TerminalPublisher;
        # staged command composition calls plan -> external stage -> commit.
        return self._terminal.publish_run_failure(**kwargs)  # type: ignore[attr-defined]

    def commit(self, fresh_draft: object, staged: object) -> object:
        return self._terminal.commit(fresh_draft, staged)  # type: ignore[attr-defined]

    def commit_many(self, publications: object) -> object:
        return self._terminal.commit_many(publications)  # type: ignore[attr-defined]


__all__ = [
    "BlobLocationRegistry",
    "WorkerArtifactPort",
    "WorkerAuditPort",
    "WorkerBlobStager",
    "WorkerBlobStore",
    "WorkerCommandPublicationGateway",
    "WorkerCommandTerminalPublicationGateway",
    "WorkerManifestLedger",
]
