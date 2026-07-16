from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import pytest

from gameforge.contracts.errors import Conflict, IdempotencyConflict, IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.identity import Permission
from gameforge.contracts.jobs import (
    AttemptLeasedDataV1,
    AttemptStartedDataV1,
    CheckerRunPayloadV1,
    FailureClassifierRefV1,
    GraphSelectionV1,
    RetryPolicyRefV1,
    RetryPolicySnapshot,
    RunAttempt,
    RunDispatchTraceCarrierV1,
    RunEvent,
    RunKindDefinition,
    RunLease,
    RunIntermediateArtifactLinkV1,
    RunPayloadEnvelope,
    RunQueuedDataV1,
    RunRecord,
    RuntimeParentRuleSetRef,
    TerminalPublisherHooks,
    canonical_payload_hash,
    outcome_policy_set_digest,
    retry_policy_digest,
    run_kind_definition_digest,
)
from gameforge.contracts.lineage import AuditActor, VersionTuple
from gameforge.platform.runs.commands import (
    RunClaimRequest,
    RunCommandCapabilities,
    RunCommandService,
    RunCreateRequest,
)
from gameforge.platform.runs.lifecycle import (
    RunLifecycleCapabilities,
    RunLifecycleService,
)
from gameforge.runtime.clock import FrozenUtcClock


NOW_DT = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
NOW = "2026-07-14T12:00:00Z"
QUEUE_DEADLINE = "2026-07-14T12:10:00Z"
OVERALL_DEADLINE = "2026-07-14T13:00:00Z"
LEASE_EXPIRES = "2026-07-14T12:00:30Z"
_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64


def _retry_policy() -> RetryPolicySnapshot:
    fields = {
        "retry_policy_id": "run-default",
        "retry_policy_version": 1,
        "max_attempts": 3,
        "retryable_failure_classes": ("transient_dependency", "lease"),
        "backoff": "exponential",
        "base_delay_ms": 100,
        "max_delay_ms": 1_000,
        "jitter_policy": "none@1",
        "honor_retry_after": True,
    }
    return RetryPolicySnapshot(
        **fields,
        retry_policy_digest=retry_policy_digest(fields),
    )


def _definition(retry: RetryPolicySnapshot) -> RunKindDefinition:
    return RunKindDefinition(
        kind="checker.run",
        version=1,
        status="active",
        payload_schema_id="checker-run@1",
        prepared_result_schema_id="prepared-run-result@1",
        prepared_failure_schema_id="prepared-run-failure@1",
        result_schema_id="run-result@1",
        failure_schema_id="run-failure@1",
        outcome_policies=(),
        runtime_parent_rule_set=RuntimeParentRuleSetRef(
            rule_set_id="runtime-parents:checker",
            version=1,
            digest=_HASH_A,
        ),
        allowed_command_schema_ids=(),
        creation_mode="generic_runs_endpoint",
        allowed_llm_execution_modes=("not_applicable",),
        seed_policy="forbidden",
        required_permission=Permission(
            action="run.create",
            resource_kind="run",
            domain_scope=None,
        ),
        executor_key="checker@1",
        terminal_hooks=TerminalPublisherHooks(
            on_success="publish-checker@1",
            on_failure="publish-failure@1",
            on_cancel="publish-cancel@1",
            on_timeout="publish-timeout@1",
        ),
        failure_classifier=FailureClassifierRefV1(
            classifier_version=1,
            classifier_digest=_HASH_B,
        ),
        retry_policy=RetryPolicyRefV1(
            retry_policy_id=retry.retry_policy_id,
            retry_policy_version=retry.retry_policy_version,
            retry_policy_digest=retry.retry_policy_digest,
        ),
    )


def _payload() -> RunPayloadEnvelope:
    params = CheckerRunPayloadV1(
        snapshot_artifact_id="artifact:input",
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=ProfileRefV1(profile_id="checker", version=1),
        checker_ids=("graph",),
        defect_classes=("dangling_ref",),
    )
    return RunPayloadEnvelope(
        payload_schema_version="checker-run@1",
        input_artifact_ids=("artifact:input",),
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:input",
            tool_version="checker@1",
        ),
        policy_bindings=(),
        schema_bindings=(),
        execution_profile_catalog_version=1,
        execution_profile_catalog_digest=_HASH_A,
        resolved_profiles=(),
        resolved_policy_snapshots=(),
        budget_set_snapshot_id="budget-set:1",
        llm_execution_mode="not_applicable",
        params=params,
    )


def _create_request(
    *,
    run_id: str = "run:1",
    request_hash: str = _HASH_A,
    carrier: RunDispatchTraceCarrierV1 | None = None,
) -> RunCreateRequest:
    return RunCreateRequest(
        run_id=run_id,
        kind=RunKindRef(kind="checker.run", version=1),
        creation_mode="generic_runs_endpoint",
        idempotency_scope="principal:human:a",
        idempotency_key="request:1",
        request_hash=request_hash,
        payload=_payload(),
        dispatch_trace_carrier=carrier,
        initiated_by=AuditActor(principal_id="human:a", principal_kind="human"),
        queue_deadline_utc=QUEUE_DEADLINE,
        attempt_timeout_ns=30_000_000_000,
        overall_deadline_utc=OVERALL_DEADLINE,
    )


@dataclass
class _State:
    runs: dict[str, RunRecord] = field(default_factory=dict)
    attempts: dict[tuple[str, int], RunAttempt] = field(default_factory=dict)
    leases: dict[str, RunLease] = field(default_factory=dict)
    events: dict[tuple[str, int], RunEvent] = field(default_factory=dict)
    intermediate_links: dict[tuple[str, int, int], RunIntermediateArtifactLinkV1] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class _PersistedClaim:
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    event: RunEvent


@dataclass(frozen=True)
class _PersistedStart:
    run: RunRecord
    attempt: RunAttempt
    lease: RunLease
    event: RunEvent


class _Repo:
    def __init__(self, state: _State) -> None:
        self.state = state

    def get(self, run_id: str) -> RunRecord | None:
        return self.state.runs.get(run_id)

    def get_by_idempotency(self, *, scope: str, key: str) -> RunRecord | None:
        return next(
            (
                run
                for run in self.state.runs.values()
                if run.idempotency_scope == scope and run.idempotency_key == key
            ),
            None,
        )

    def create_queued(self, run: RunRecord, initial_event: RunEvent) -> RunRecord:
        if run.run_id in self.state.runs:
            raise IntegrityViolation("run collision")
        self.state.runs[run.run_id] = run
        self.state.events[(run.run_id, initial_event.seq)] = initial_event
        return run

    def get_claim_candidate(self, *, now_utc: str) -> RunRecord | None:
        del now_utc
        candidates = [
            run
            for run in self.state.runs.values()
            if run.status == "queued" and run.cancel_requested_at is None
        ]
        return min(candidates, key=lambda run: (run.created_at, run.run_id), default=None)

    def claim(
        self,
        *,
        run_id: str,
        expected_revision: int,
        worker_principal_id: str,
        lease_id: str,
        acquired_at: str,
        expires_at: str,
        permit_group_id: str,
        trace_id: str | None = None,
    ) -> _PersistedClaim:
        before = self.state.runs[run_id]
        if before.revision != expected_revision:
            raise Conflict("run revision differs")
        attempt_no = before.next_attempt_no
        fencing_token = before.next_fencing_token
        event_seq = before.next_event_seq
        after = RunRecord.model_validate(
            {
                **before.model_dump(mode="python"),
                "status": "leased",
                "revision": before.revision + 1,
                "current_attempt_no": attempt_no,
                "next_attempt_no": attempt_no + 1,
                "next_fencing_token": fencing_token + 1,
                "next_event_seq": event_seq + 1,
                "concurrency_permit_group_id": permit_group_id,
                "retry_not_before_utc": None,
                "updated_at": acquired_at,
            }
        )
        attempt = RunAttempt(
            run_id=run_id,
            attempt_no=attempt_no,
            status="leased",
            fencing_token=fencing_token,
            worker_principal_id=worker_principal_id,
            trace_id=trace_id,
            next_call_ordinal=1,
        )
        lease = RunLease(
            lease_id=lease_id,
            run_id=run_id,
            attempt_no=attempt_no,
            fencing_token=fencing_token,
            lease_version=1,
            owner_principal_id=worker_principal_id,
            acquired_at=acquired_at,
            heartbeat_at=acquired_at,
            expires_at=expires_at,
            status="active",
        )
        event = RunEvent(
            run_id=run_id,
            seq=event_seq,
            event_type="attempt.leased",
            attempt_no=attempt_no,
            occurred_at=acquired_at,
            data_schema_version="attempt-leased@1",
            data=AttemptLeasedDataV1(
                attempt_no=attempt_no,
                lease_expires_at=expires_at,
            ),
            trace_id=trace_id,
        )
        self.state.runs[run_id] = after
        self.state.attempts[(run_id, attempt_no)] = attempt
        self.state.leases[lease_id] = lease
        self.state.events[(run_id, event_seq)] = event
        return _PersistedClaim(run=after, attempt=attempt, lease=lease, event=event)

    def get_attempt(self, run_id: str, attempt_no: int) -> RunAttempt | None:
        return self.state.attempts.get((run_id, attempt_no))

    def start_attempt(
        self,
        *,
        run_id: str,
        attempt_no: int,
        expected_run_revision: int,
        lease_id: str,
        fencing_token: int,
        started_at: str,
        attempt_deadline_utc: str,
    ) -> _PersistedStart:
        run = self.state.runs[run_id]
        attempt = self.state.attempts[(run_id, attempt_no)]
        lease = self.state.leases[lease_id]
        if run.revision != expected_run_revision:
            raise Conflict("Run revision differs")
        if (
            run.status != "leased"
            or run.current_attempt_no != attempt_no
            or attempt.status != "leased"
            or attempt.fencing_token != fencing_token
            or lease.run_id != run_id
            or lease.attempt_no != attempt_no
            or lease.fencing_token != fencing_token
            or lease.status != "active"
        ):
            raise Conflict("attempt start fence differs")

        event_seq = run.next_event_seq
        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(mode="python"),
                "status": "running",
                "revision": run.revision + 1,
                "next_event_seq": event_seq + 1,
                "updated_at": started_at,
            }
        )
        updated_attempt = RunAttempt.model_validate(
            {
                **attempt.model_dump(mode="python"),
                "status": "running",
                "started_at": started_at,
                "attempt_deadline_utc": attempt_deadline_utc,
            }
        )
        event = RunEvent(
            run_id=run_id,
            seq=event_seq,
            event_type="attempt.started",
            attempt_no=attempt_no,
            occurred_at=started_at,
            data_schema_version="attempt-started@1",
            data=AttemptStartedDataV1(
                attempt_no=attempt_no,
                started_at=started_at,
                attempt_deadline_utc=attempt_deadline_utc,
            ),
            trace_id=attempt.trace_id,
        )
        self.state.runs[run_id] = updated_run
        self.state.attempts[(run_id, attempt_no)] = updated_attempt
        self.state.events[(run_id, event_seq)] = event
        return _PersistedStart(
            run=updated_run,
            attempt=updated_attempt,
            lease=lease,
            event=event,
        )

    def get_current_lease(self, run_id: str) -> RunLease | None:
        return next(
            (
                lease
                for lease in self.state.leases.values()
                if lease.run_id == run_id and lease.status == "active"
            ),
            None,
        )

    def get_event(self, run_id: str, seq: int) -> RunEvent | None:
        return self.state.events.get((run_id, seq))

    def list_events(self, run_id: str, *, after_seq: int, limit: int) -> tuple[RunEvent, ...]:
        return tuple(
            event
            for (candidate_run_id, seq), event in sorted(self.state.events.items())
            if candidate_run_id == run_id and seq > after_seq
        )[:limit]

    def get_intermediate_link(
        self,
        run_id: str,
        attempt_no: int,
        call_ordinal: int,
    ) -> RunIntermediateArtifactLinkV1 | None:
        return self.state.intermediate_links.get((run_id, attempt_no, call_ordinal))

    def put_intermediate_link(
        self,
        link: RunIntermediateArtifactLinkV1,
    ) -> RunIntermediateArtifactLinkV1:
        identity = (link.run_id, link.attempt_no, link.call_ordinal)
        retained = self.state.intermediate_links.get(identity)
        if retained is not None:
            if retained != link:
                raise IntegrityViolation("intermediate link collision")
            return retained
        attempt = self.state.attempts[(link.run_id, link.attempt_no)]
        if (
            attempt.next_call_ordinal != link.call_ordinal
            or attempt.fencing_token != link.fencing_token
        ):
            raise Conflict("attempt call ordinal or fencing token differs")
        replacement = RunAttempt.model_validate(
            {
                **attempt.model_dump(mode="python"),
                "next_call_ordinal": attempt.next_call_ordinal + 1,
            }
        )
        self.state.attempts[(link.run_id, link.attempt_no)] = replacement
        self.state.intermediate_links[identity] = link
        return link


class _Registry:
    def __init__(self, definition: RunKindDefinition, retry: RetryPolicySnapshot) -> None:
        self.definition = definition
        self.retry = retry
        self.binding_checks = 0

    def get_run_kind(self, kind: RunKindRef) -> RunKindDefinition | None:
        if (kind.kind, kind.version) != (self.definition.kind, self.definition.version):
            return None
        return self.definition

    def get_retry_policy(self, ref: RetryPolicyRefV1) -> RetryPolicySnapshot | None:
        expected = (
            self.retry.retry_policy_id,
            self.retry.retry_policy_version,
            self.retry.retry_policy_digest,
        )
        actual = (ref.retry_policy_id, ref.retry_policy_version, ref.retry_policy_digest)
        return self.retry if actual == expected else None

    def validate_payload_bindings(
        self,
        *,
        payload: RunPayloadEnvelope,
        definition: RunKindDefinition,
    ) -> None:
        assert payload.payload_schema_version == definition.payload_schema_id
        self.binding_checks += 1


class _Admission:
    def __init__(self) -> None:
        self.holds: list[str] = []
        self.permits: list[str] = []

    def reserve_run_budget(
        self,
        *,
        run_id: str,
        budget_set_snapshot_id: str,
        request_hash: str,
        initiated_by: AuditActor,
    ) -> str:
        del budget_set_snapshot_id, request_hash, initiated_by
        self.holds.append(run_id)
        return f"hold:{run_id}"

    def acquire_execution_permits(
        self,
        *,
        run: RunRecord,
        attempt_no: int,
        fencing_token: int,
        worker_principal_id: str,
        lease_id: str,
        expires_at: str,
    ) -> str:
        del attempt_no, fencing_token, worker_principal_id, lease_id, expires_at
        self.permits.append(run.run_id)
        return f"permit:{run.run_id}"


class _Publication:
    def __init__(self, repo: _Repo) -> None:
        self.repo = repo
        self.created: list[str] = []
        self.claimed: list[str] = []
        self.started: list[str] = []
        self.prompt_publications: list[RunIntermediateArtifactLinkV1] = []
        self.prompt_idempotency: dict[
            tuple[str, str], tuple[str, RunIntermediateArtifactLinkV1]
        ] = {}

    def record_run_created(
        self,
        *,
        run: RunRecord,
        event: RunEvent,
        request_id: str | None = None,
    ) -> None:
        del request_id
        assert event.event_type == "run.queued"
        self.created.append(run.run_id)

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
        assert previous.run_id == run.run_id == attempt.run_id == lease.run_id
        assert event.event_type == "attempt.leased"
        assert actor.principal_id == attempt.worker_principal_id
        self.claimed.append(run.run_id)

    def record_attempt_started(
        self,
        *,
        previous: RunRecord,
        run: RunRecord,
        attempt: RunAttempt,
        lease: RunLease,
        event: RunEvent,
        actor: AuditActor,
    ) -> None:
        assert previous.run_id == run.run_id == attempt.run_id == lease.run_id
        assert event.event_type == "attempt.started"
        assert actor.principal_id == attempt.worker_principal_id
        self.started.append(run.run_id)

    def get_prompt_replay(
        self,
        *,
        idempotency_scope: str,
        idempotency_key: str,
        request_hash: str,
    ) -> RunIntermediateArtifactLinkV1 | None:
        retained = self.prompt_idempotency.get((idempotency_scope, idempotency_key))
        if retained is None:
            return None
        retained_hash, link = retained
        if retained_hash != request_hash:
            raise Conflict("prompt publication idempotency payload differs")
        return link

    def publish_prompt_rendered(
        self,
        *,
        link: RunIntermediateArtifactLinkV1,
        idempotency_scope: str,
        idempotency_key: str,
        request_hash: str,
        actor: AuditActor,
    ) -> RunIntermediateArtifactLinkV1:
        retained = self.get_prompt_replay(
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        if retained is not None:
            return retained
        attempt = self.repo.state.attempts[(link.run_id, link.attempt_no)]
        assert actor.principal_id == attempt.worker_principal_id
        stored = self.repo.put_intermediate_link(link)
        self.prompt_idempotency[(idempotency_scope, idempotency_key)] = (
            request_hash,
            stored,
        )
        self.prompt_publications.append(stored)
        return stored


class _Uow:
    @contextmanager
    def begin(self):
        yield object()


@dataclass
class _Harness:
    service: RunCommandService
    lifecycle: RunLifecycleService
    state: _State
    registry: _Registry
    admission: _Admission
    publication: _Publication


def _harness(*, definition: RunKindDefinition | None = None) -> _Harness:
    retry = _retry_policy()
    selected_definition = definition or _definition(retry)
    state = _State()
    repo = _Repo(state)
    registry = _Registry(selected_definition, retry)
    admission = _Admission()
    publication = _Publication(repo)
    unit_of_work = _Uow()
    command_capabilities = RunCommandCapabilities(
        runs=repo,
        registry=registry,
        admission=admission,
        publication=publication,
        accounting=None,
    )
    lifecycle_capabilities = RunLifecycleCapabilities(
        runs=repo,
        registry=registry,
        accounting=None,
        publication=publication,
    )
    return _Harness(
        service=RunCommandService(
            unit_of_work=unit_of_work,
            bind_capabilities=lambda transaction: command_capabilities,
            clock=FrozenUtcClock(NOW_DT),
        ),
        lifecycle=RunLifecycleService(
            unit_of_work=unit_of_work,
            bind_capabilities=lambda transaction: lifecycle_capabilities,
            clock=FrozenUtcClock(NOW_DT),
        ),
        state=state,
        registry=registry,
        admission=admission,
        publication=publication,
    )


def test_create_is_atomic_queued_publication_without_fake_attempt_or_permit() -> None:
    harness = _harness()

    result = harness.service.create_run(_create_request())

    run = result.run
    assert result.replayed is False
    assert run.status == "queued"
    assert run.revision == 1
    assert run.current_attempt_no is None
    assert run.next_attempt_no == 1
    assert run.next_fencing_token == 1
    assert run.next_event_seq == 2
    assert run.concurrency_permit_group_id is None
    assert run.run_budget_hold_group_id == "hold:run:1"
    assert run.payload_hash == canonical_payload_hash(run.payload)
    assert run.run_kind_definition_digest == run_kind_definition_digest(harness.registry.definition)
    assert run.outcome_policy_set_digest == outcome_policy_set_digest(
        run.kind, harness.registry.definition.outcome_policies
    )
    assert harness.state.attempts == {}
    assert harness.state.leases == {}
    assert harness.admission.permits == []

    event = harness.state.events[(run.run_id, 1)]
    assert event == RunEvent(
        run_id="run:1",
        seq=1,
        event_type="run.queued",
        occurred_at=NOW,
        data_schema_version="run-queued@1",
        data=RunQueuedDataV1(
            run_kind=run.kind,
            queue_deadline_utc=QUEUE_DEADLINE,
            overall_deadline_utc=OVERALL_DEADLINE,
        ),
    )
    assert harness.publication.created == ["run:1"]


def test_create_replay_precedes_admission_and_keeps_original_trace_carrier() -> None:
    harness = _harness()
    original_carrier = RunDispatchTraceCarrierV1(
        traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    )
    first = harness.service.create_run(_create_request(carrier=original_carrier))
    replacement_carrier = RunDispatchTraceCarrierV1(
        traceparent="00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01"
    )

    replay = harness.service.create_run(
        _create_request(run_id="run:new", carrier=replacement_carrier)
    )

    assert replay.replayed is True
    assert replay.run == first.run
    assert replay.run.dispatch_trace_carrier == original_carrier
    assert harness.admission.holds == ["run:1"]
    assert harness.publication.created == ["run:1"]

    wrong_surface = _create_request(run_id="run:wrong-surface").model_copy(
        update={"creation_mode": "internal_only"}
    )
    with pytest.raises(IntegrityViolation, match="creation surface"):
        harness.service.create_run(wrong_surface)
    assert harness.admission.holds == ["run:1"]

    with pytest.raises(IdempotencyConflict, match="idempotency"):
        harness.service.create_run(_create_request(run_id="run:other", request_hash=_HASH_C))
    changed_payload = _create_request(run_id="run:changed-payload").model_copy(
        update={
            "payload": _payload().model_copy(
                update={"budget_set_snapshot_id": "budget-set:different"}
            )
        }
    )
    with pytest.raises(IdempotencyConflict, match="matching request hash"):
        harness.service.create_run(changed_payload)
    assert harness.admission.holds == ["run:1"]


def test_unknown_or_disabled_run_kind_fails_before_budget_admission() -> None:
    harness = _harness()
    unknown = _create_request().model_copy(
        update={"kind": RunKindRef(kind="review.run", version=1)}
    )
    with pytest.raises(IntegrityViolation, match="Run kind"):
        harness.service.create_run(unknown)
    assert harness.admission.holds == []

    retry = _retry_policy()
    disabled = _definition(retry).model_copy(update={"status": "disabled"})
    harness = _harness(definition=disabled)
    with pytest.raises(IntegrityViolation, match="active"):
        harness.service.create_run(_create_request())
    assert harness.admission.holds == []


def test_idempotent_replay_revalidates_the_retained_registry_without_readmission() -> None:
    harness = _harness()
    harness.service.create_run(_create_request())
    harness.registry.definition = harness.registry.definition.model_copy(
        update={"status": "disabled"}
    )

    with pytest.raises(IntegrityViolation, match="active"):
        harness.service.create_run(_create_request(run_id="run:retry"))
    assert harness.admission.holds == ["run:1"]
    assert harness.publication.created == ["run:1"]


def test_claim_consumes_persisted_heads_once_and_publishes_one_event() -> None:
    harness = _harness()
    queued = harness.service.create_run(_create_request()).run

    claim = harness.service.claim_next(
        RunClaimRequest(
            worker=AuditActor(
                principal_id="service:worker:1",
                principal_kind="service",
            ),
            lease_id="lease:1",
            lease_duration_ns=30_000_000_000,
            trace_id="trace:attempt:1",
        )
    )

    assert claim is not None
    assert claim.previous == queued
    assert claim.run.status == "leased"
    assert claim.run.revision == queued.revision + 1
    assert claim.run.current_attempt_no == 1
    assert claim.run.next_attempt_no == 2
    assert claim.run.next_fencing_token == 2
    assert claim.run.next_event_seq == 3
    assert claim.run.concurrency_permit_group_id == "permit:run:1"
    assert claim.attempt.attempt_no == 1
    assert claim.attempt.fencing_token == 1
    assert claim.attempt.next_call_ordinal == 1
    assert claim.lease.expires_at == LEASE_EXPIRES
    assert claim.event.seq == 2
    assert claim.event.attempt_no == 1
    assert claim.event.trace_id == "trace:attempt:1"
    assert harness.admission.permits == ["run:1"]
    assert harness.publication.claimed == ["run:1"]
    assert harness.registry.binding_checks == 2

    assert (
        harness.service.claim_next(
            RunClaimRequest(
                worker=AuditActor(
                    principal_id="service:worker:2",
                    principal_kind="service",
                ),
                lease_id="lease:2",
                lease_duration_ns=30_000_000_000,
            )
        )
        is None
    )
    assert harness.admission.permits == ["run:1"]


def test_missing_tx_bound_capability_fails_closed() -> None:
    harness = _harness()
    harness.service._bind_capabilities = lambda transaction: RunCommandCapabilities(  # type: ignore[method-assign]
        runs=None,
        registry=harness.registry,
        admission=harness.admission,
        publication=harness.publication,
    )
    with pytest.raises(IntegrityViolation, match="runs"):
        harness.service.create_run(_create_request())
