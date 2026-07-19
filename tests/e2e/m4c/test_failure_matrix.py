"""Task 18 cross-journey failure recovery through the public M4c surface.

This module deliberately reuses the Journey-B composition harness.  It adds no
fault-injection framework: every workflow mutation travels through FastAPI and every
validation outcome travels through the persistent worker and terminal publisher.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
import socket

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tests.e2e.m4c.test_journey_b import (
    APPROVER_LOGIN,
    APPROVER_PASSWORD,
    MAKER_LOGIN,
    MAKER_PASSWORD,
    REF_NAME,
    UNAUTHORIZED_LOGIN,
    UNAUTHORIZED_PASSWORD,
    _Harness,
    _approval,
    _assert_passed_patch_evidence,
    _drive,
    _headers,
    _login,
    _patch_body,
    _ref_history,
    _run,
    _run_patch_cycle,
    _start_api,
    _stop_api,
    _validation_body,
)
from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.contracts.cost import (
    CostAmountV1,
    TokenUsageObservationV1,
)
from gameforge.contracts.execution_profiles import RunKindRef
from gameforge.contracts.jobs import CancelRunPayloadV1, RunCommandAckV1, RunCommandV1
from gameforge.contracts.workflow import ApprovalItem
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import (
    ApprovalItemRow,
    ArtifactRow,
    BudgetRow,
    BudgetReservationRow,
    BudgetSetSnapshotRow,
    BudgetSnapshotRow,
    ConcurrencyPermitRow,
    PermitGroupRow,
    ReservationGroupRow,
    RunAttemptRow,
    RunEventRow,
    RunRow,
)
from gameforge.runtime.persistence.runs import SqlRunRepository

from tests.e2e.m4c.test_journey_a import (
    _Harness as JourneyAHarness,
    _execution_plan as _journey_a_execution_plan,
    _generation_body as _journey_a_generation_body,
    _seed_model_authority as _seed_journey_a_model_authority,
)
from tests.e2e.m4c.test_composition import _shared_budget


@pytest.fixture(autouse=True)
def _deny_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the failure matrix's zero-egress claim executable."""

    def denied(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("M4c failure matrix attempted external network access")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket.socket, "sendto", denied)
    monkeypatch.setattr(socket.socket, "sendmsg", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def _validate_submit_and_approve(
    process,
    maker,
    approver,
    *,
    patch_artifact_id: str,
    base_artifact_id: str,
    expected_ref: dict,
    key: str,
) -> tuple[ApprovalItem, str]:
    approval_id = f"approval:patch:{patch_artifact_id}"
    item = _approval(maker, approval_id)
    validate = maker.client.post(
        f"/api/v1/patches/{patch_artifact_id}:validate",
        json=_validation_body(
            item,
            base_artifact_id=base_artifact_id,
            expected_ref=expected_ref,
            checker_graph=True,
        ),
        headers=_headers(
            maker,
            idempotency_key=f"{key}:validate",
            resource_kind="patch",
            resource_id=patch_artifact_id,
            revision=item.workflow_revision,
        ),
    )
    assert validate.status_code == 202, validate.text
    validation_run_id = validate.json()["run_id"]
    terminal = asyncio.run(_drive(process.dispatcher, maker, validation_run_id))
    assert terminal.status == "succeeded"

    validated = _approval(maker, approval_id)
    assert validated.status == "validated"
    assert validated.evidence_set_artifact_id is not None
    _assert_passed_patch_evidence(
        maker,
        evidence_set_artifact_id=validated.evidence_set_artifact_id,
        run_id=validation_run_id,
        patch_artifact_id=patch_artifact_id,
    )
    submit = maker.client.post(
        f"/api/v1/patches/{patch_artifact_id}:submit-for-approval",
        json={
            "request_schema_version": "submit-for-approval-request@1",
            "approval_id": approval_id,
            "expected_workflow_revision": validated.workflow_revision,
        },
        headers=_headers(
            maker,
            idempotency_key=f"{key}:submit",
            resource_kind="patch",
            resource_id=patch_artifact_id,
            revision=validated.workflow_revision,
        ),
    )
    assert submit.status_code == 200, submit.text
    pending = _approval(maker, approval_id)
    assert pending.status == "pending_approval"

    approve = approver.client.post(
        f"/api/v1/approvals/{approval_id}:approve",
        json={
            "request_schema_version": "approval-decision-request@1",
            "decision": "approve",
            "requirement_ids": [item.requirement_id for item in pending.requirements],
            "expected_workflow_revision": pending.workflow_revision,
            "reason_code": f"{key}:independent-review",
        },
        headers=_headers(
            approver,
            idempotency_key=f"{key}:approve",
            resource_kind="approval",
            resource_id=approval_id,
            revision=pending.workflow_revision,
        ),
    )
    assert approve.status_code == 200, approve.text
    approved = _approval(approver, approval_id)
    assert approved.status == "approved"
    assert len(approved.decisions) == 1
    assert approved.decisions[0].actor.principal_id == "human:approver"
    return approved, validation_run_id


def _queue_patch_validation(
    maker,
    *,
    patch_artifact_id: str,
    base_artifact_id: str,
    expected_ref: dict,
    key: str,
):
    item = _approval(maker, f"approval:patch:{patch_artifact_id}")
    return maker.client.post(
        f"/api/v1/patches/{patch_artifact_id}:validate",
        json=_validation_body(
            item,
            base_artifact_id=base_artifact_id,
            expected_ref=expected_ref,
            checker_graph=True,
        ),
        headers=_headers(
            maker,
            idempotency_key=f"{key}:validate",
            resource_kind="patch",
            resource_id=patch_artifact_id,
            revision=item.workflow_revision,
        ),
    )


def _sse_event_ids(body: str) -> list[int]:
    return [int(line.removeprefix("id:")) for line in body.splitlines() if line.startswith("id:")]


def _sse_event_types(body: str) -> list[str]:
    return [line.removeprefix("event:") for line in body.splitlines() if line.startswith("event:")]


def _put_system_budget(
    harness: _Harness,
    *,
    budget_id: str,
    limits: tuple[CostAmountV1, ...],
) -> None:
    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session, session.begin():
            ledger = SqlCostLedger(session, clock=harness.clock)
            ledger.put_budget(
                _shared_budget(
                    budget_id=budget_id,
                    scope_kind="system",
                    scope_id="global",
                ).model_copy(update={"limits": limits})
            )
    finally:
        engine.dispose()


class _MutableUtcClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def now_utc(self) -> datetime:
        return self.current


def test_public_conflict_resolution_requires_fresh_validation_and_approval(
    tmp_path: Path,
) -> None:
    """Two edits collide, then the resolved revision earns new evidence and approval."""

    harness = _Harness(tmp_path)
    harness._provision_human(  # noqa: SLF001 - explicit out-of-band fixture bootstrap
        principal_id="human:editor",
        login="editor",
        password="editor-password-1",
        display_name="Independent Editor",
        roles=("content_designer",),
    )
    base_artifact_id, base_ref = harness.seed_base_snapshot()
    api = _start_api(harness.api_config())
    process = build_worker_process(harness.worker_config())
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        editor = _login(api, "editor", "editor-password-1")
        approver = _login(api, APPROVER_LOGIN, APPROVER_PASSWORD)

        # A drafts 120 -> 80 and earns real validation evidence plus B's approval while
        # the base ref is still current, but deliberately does not apply it.
        source_response = maker.client.post(
            "/api/v1/patches",
            json=_patch_body(
                base_artifact_id=base_artifact_id,
                expected_ref=base_ref,
                new_value=80,
                rationale="First editor proposes reward 80.",
            ),
            headers=_headers(maker, idempotency_key="conflict:source:draft"),
        )
        assert source_response.status_code == 201, source_response.text
        source_patch_id = source_response.json()["artifact"]["artifact_id"]
        source_approval_id = f"approval:patch:{source_patch_id}"
        source_item = _approval(maker, source_approval_id)
        assert source_item.status == "draft"
        source_approved, _ = _validate_submit_and_approve(
            process,
            maker,
            approver,
            patch_artifact_id=source_patch_id,
            base_artifact_id=base_artifact_id,
            expected_ref=base_ref,
            key="conflict:source",
        )
        source_evidence_id = source_approved.evidence_set_artifact_id
        assert source_evidence_id is not None
        assert source_approved.decisions
        assert _ref_history(maker)[-1] == base_ref

        # A second editor's independently reviewed edit wins the ref first, changing
        # the same field 120 -> 100.
        intervening = _run_patch_cycle(
            harness,
            process,
            editor,
            approver,
            base_artifact_id=base_artifact_id,
            expected_ref=base_ref,
            new_value=100,
            key="conflict:intervening",
        )
        assert intervening.new_ref["revision"] == 2
        assert _ref_history(maker)[-1] == intervening.new_ref

        # The stale approved revision can only rebase against the exact current ref. A
        # conflict is persisted, but rebase itself publishes no replacement revision.
        rebase = maker.client.post(
            f"/api/v1/patches/{source_patch_id}:rebase",
            json={
                "request_schema_version": "patch-rebase-request@1",
                "approval_id": source_approval_id,
                "expected_subject_head_revision": source_approved.subject_revision,
                "expected_workflow_revision": source_approved.workflow_revision,
                "ref_name": REF_NAME,
                "expected_ref": intervening.new_ref,
            },
            headers=_headers(
                maker,
                idempotency_key="conflict:source:rebase",
                resource_kind="patch",
                resource_id=source_patch_id,
                revision=source_approved.workflow_revision,
            ),
        )
        assert rebase.status_code == 200, rebase.text
        assert rebase.json()["status"] == "conflicted"
        assert rebase.json()["new_patch_artifact_id"] is None
        conflict_set_id = rebase.json()["conflict_set_id"]
        assert conflict_set_id is not None

        # Read the authoritative conflict IDs from the public resource.  The test never
        # recomputes IDs from a parallel in-memory merge plan.
        conflict_page = maker.client.get(
            f"/api/v1/conflict-sets/{conflict_set_id}/conflicts",
            params={"limit": 100},
        )
        assert conflict_page.status_code == 200, conflict_page.text
        conflicts = conflict_page.json()["items"]
        assert conflicts
        assert all("take_proposed" in conflict["allowed_resolutions"] for conflict in conflicts)
        conflict_ids = [conflict["id"] for conflict in conflicts]
        assert len(conflict_ids) == len(set(conflict_ids))

        resolved_response = maker.client.post(
            f"/api/v1/patches/{source_patch_id}:resolve-conflicts",
            json={
                "request_schema_version": "resolve-conflicts-request@1",
                "approval_id": source_approval_id,
                "expected_subject_head_revision": source_approved.subject_revision,
                "expected_workflow_revision": source_approved.workflow_revision,
                "ref_name": REF_NAME,
                "expected_ref": intervening.new_ref,
                "conflict_set_id": conflict_set_id,
                "resolutions": [
                    {"conflict_id": conflict_id, "choice": "take_proposed"}
                    for conflict_id in conflict_ids
                ],
            },
            headers=_headers(
                maker,
                idempotency_key="conflict:source:resolve",
                resource_kind="patch",
                resource_id=source_patch_id,
                revision=source_approved.workflow_revision,
            ),
        )
        assert resolved_response.status_code == 200, resolved_response.text
        assert resolved_response.json()["status"] == "clean"
        resolved_patch_id = resolved_response.json()["new_patch_artifact_id"]
        assert resolved_patch_id is not None and resolved_patch_id != source_patch_id

        superseded = _approval(maker, source_approval_id)
        resolved_approval_id = f"approval:patch:{resolved_patch_id}"
        resolved_item = _approval(maker, resolved_approval_id)
        assert superseded.status == "superseded"
        assert superseded.evidence_set_artifact_id == source_evidence_id
        assert superseded.decisions == source_approved.decisions
        assert resolved_item.status == "draft"
        assert resolved_item.supersedes_approval_id == source_approval_id
        assert resolved_item.subject_revision == source_item.subject_revision + 1
        assert resolved_item.evidence_set_artifact_id is None
        assert resolved_item.decisions == ()
        assert _ref_history(maker)[-1] == intervening.new_ref

        # The resolved revision must run the real validation worker and publish its own
        # immutable EvidenceSet; neither the stale draft nor the intervening Patch lends
        # it evidence or decisions.
        approved, validation_run_id = _validate_submit_and_approve(
            process,
            maker,
            approver,
            patch_artifact_id=resolved_patch_id,
            base_artifact_id=intervening.new_ref["artifact_id"],
            expected_ref=intervening.new_ref,
            key="conflict:resolved",
        )
        assert approved.evidence_set_artifact_id not in {
            None,
            source_evidence_id,
            intervening.evidence_set_artifact_id,
        }
        assert approved.decisions != source_approved.decisions
        _assert_passed_patch_evidence(
            maker,
            evidence_set_artifact_id=approved.evidence_set_artifact_id,
            run_id=validation_run_id,
            patch_artifact_id=resolved_patch_id,
        )
        assert _ref_history(maker)[-1] == intervening.new_ref

        binding = approved.target_binding
        applied = approver.client.post(
            f"/api/v1/patches/{resolved_patch_id}:apply",
            json={
                "request_schema_version": "workflow-apply-request@1",
                "approval_id": resolved_approval_id,
                "expected_workflow_revision": approved.workflow_revision,
                "subject_digest": approved.subject_digest,
                "target_artifact_id": binding.target_artifact_id,
                "target_digest": binding.target_digest,
                "ref_name": REF_NAME,
                "expected_ref": binding.expected_ref.model_dump(mode="json"),
            },
            headers=_headers(
                approver,
                idempotency_key="conflict:resolved:apply",
                resource_kind="patch",
                resource_id=resolved_patch_id,
                revision=approved.workflow_revision,
            ),
        )
        assert applied.status_code == 200, applied.text
        assert _approval(approver, resolved_approval_id).status == "applied"
        history = _ref_history(maker)
        assert [entry["revision"] for entry in history] == [1, 2, 3]
        assert history[-1]["artifact_id"] == binding.target_artifact_id
    finally:
        process.close()
        _stop_api(api)


def test_real_run_trace_and_logs_are_run_scoped_and_forbidden_without_permission(
    tmp_path: Path,
) -> None:
    """A logged-in principal without Run authority cannot infer trace or log data."""

    harness = _Harness(tmp_path)
    harness._provision_human(  # noqa: SLF001 - explicit out-of-band fixture bootstrap
        principal_id="human:unauthorized",
        login=UNAUTHORIZED_LOGIN,
        password=UNAUTHORIZED_PASSWORD,
        display_name="No Run Access",
        roles=(),
    )
    base_artifact_id, base_ref = harness.seed_base_snapshot()
    api = _start_api(harness.api_config())
    process = build_worker_process(harness.worker_config())
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        approver = _login(api, APPROVER_LOGIN, APPROVER_PASSWORD)
        unauthorized = _login(api, UNAUTHORIZED_LOGIN, UNAUTHORIZED_PASSWORD)
        cycle = _run_patch_cycle(
            harness,
            process,
            maker,
            approver,
            base_artifact_id=base_artifact_id,
            expected_ref=base_ref,
            new_value=80,
            key="observability:real-run",
        )

        authorized_traces = maker.client.get(
            f"/api/v1/runs/{cycle.validation_run_id}/traces",
            params={"limit": 100},
        )
        assert authorized_traces.status_code == 200, authorized_traces.text
        assert authorized_traces.json()["items"]

        forbidden_traces = unauthorized.client.get(
            f"/api/v1/runs/{cycle.validation_run_id}/traces",
            params={"limit": 100},
        )
        assert forbidden_traces.status_code == 403, forbidden_traces.text
        assert forbidden_traces.json()["code"] == "forbidden"

        now = datetime.now(UTC)
        log_params = {
            "start_utc": (now - timedelta(minutes=5)).isoformat(),
            "end_utc": (now + timedelta(minutes=5)).isoformat(),
            "services": "gameforge-worker",
            "event_names": "worker.attempt.started",
            "run_id": cycle.validation_run_id,
            "limit": 100,
        }
        authorized_logs = maker.client.get("/api/v1/logs/query", params=log_params)
        assert authorized_logs.status_code == 200, authorized_logs.text
        assert authorized_logs.json()["items"]

        forbidden_logs = unauthorized.client.get("/api/v1/logs/query", params=log_params)
        assert forbidden_logs.status_code == 403, forbidden_logs.text
        assert forbidden_logs.json()["code"] == "forbidden"
    finally:
        process.close()
        _stop_api(api)


def test_sse_reconnect_spans_lease_expiry_retry_and_an_independent_second_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Committed event cursors survive expiry, retry, and a fresh worker claim."""

    import gameforge.apps.worker.app as worker_app
    import gameforge.apps.worker.dispatch as worker_dispatch

    harness = _Harness(tmp_path)
    base_artifact_id, base_ref = harness.seed_base_snapshot()
    api = _start_api(harness.api_config())
    first_worker = None
    reaper = None
    second_worker = None
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        draft = maker.client.post(
            "/api/v1/patches",
            json=_patch_body(
                base_artifact_id=base_artifact_id,
                expected_ref=base_ref,
                new_value=80,
                rationale="Lease-retry candidate for resumable SSE.",
            ),
            headers=_headers(maker, idempotency_key="sse-lease-retry:draft"),
        )
        assert draft.status_code == 201, draft.text
        patch_id = draft.json()["artifact"]["artifact_id"]
        queued = _queue_patch_validation(
            maker,
            patch_artifact_id=patch_id,
            base_artifact_id=base_artifact_id,
            expected_ref=base_ref,
            key="sse-lease-retry",
        )
        assert queued.status_code == 202, queued.text
        run_id = queued.json()["run_id"]

        started_at = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
        worker_clock = FrozenUtcClock(started_at)
        monkeypatch.setattr(worker_app, "SystemUtcClock", lambda: worker_clock)
        monkeypatch.setattr(worker_dispatch, "SystemUtcClock", lambda: worker_clock)

        first_worker = build_worker_process(
            replace(
                harness.worker_config(),
                worker_principal_id="service:worker:lease-first",
            )
        )
        first_claim = first_worker.dispatcher._claim()  # noqa: SLF001 - crash seam
        assert first_claim is not None and first_claim.run.run_id == run_id
        first_started = first_worker.dispatcher._start(  # noqa: SLF001 - no heartbeat
            first_claim,
            trace_id=None,
        )
        assert first_started.run.status == "running"
        assert first_started.attempt.attempt_no == 1
        first_lease_id = first_started.lease.lease_id
        first_worker.close()
        first_worker = None

        worker_clock = FrozenUtcClock(started_at + timedelta(seconds=31))
        reaper = build_worker_process(
            replace(
                harness.worker_config(),
                worker_principal_id="service:worker:lease-reaper-pass",
            )
        )
        asyncio.run(reaper.dispatcher._reap_expired())  # noqa: SLF001 - one recovery phase
        retry_wait = _run(maker, run_id)
        assert retry_wait.status == "retry_wait"
        assert retry_wait.attempt_no == 1
        assert retry_wait.failure_artifact_id is None
        reaper.close()
        reaper = None

        engine = get_engine(harness.database_url)
        try:
            with Session(engine) as session:
                retained_retry = SqlRunRepository(session).get(run_id)
                assert retained_retry is not None
                assert retained_retry.retry_not_before_utc is not None
                retry_at = datetime.fromisoformat(
                    retained_retry.retry_not_before_utc.replace("Z", "+00:00")
                ).astimezone(UTC)
                assert retry_at == started_at + timedelta(
                    seconds=31,
                    milliseconds=250,
                )
        finally:
            engine.dispose()

        worker_clock = FrozenUtcClock(retry_at)
        second_worker = build_worker_process(
            replace(
                harness.worker_config(),
                worker_principal_id="service:worker:lease-second",
            )
        )
        second_claim = second_worker.dispatcher._claim()  # noqa: SLF001 - exact second claim
        assert second_claim is not None and second_claim.run.run_id == run_id
        assert second_claim.attempt.attempt_no == 2
        assert second_claim.attempt.fencing_token == 2
        assert second_claim.attempt.worker_principal_id == "service:worker:lease-second"
        assert second_claim.lease.lease_id != first_lease_id
        asyncio.run(
            second_worker.dispatcher._execute_guarded(second_claim)  # noqa: SLF001
        )
        terminal = _run(maker, run_id)
        assert terminal.status == "succeeded"
        assert terminal.attempt_no == 2

        first_stream = maker.client.get(f"/api/v1/runs/{run_id}/events")
        assert first_stream.status_code == 200, first_stream.text
        first_ids = _sse_event_ids(first_stream.text)
        assert first_ids == list(range(1, first_ids[-1] + 1))
        event_types = _sse_event_types(first_stream.text)
        assert event_types.count("attempt.lease_expired") == 1
        assert event_types.count("attempt.retry_scheduled") == 1
        assert event_types.count("attempt.leased") == 2
        assert event_types.count("attempt.started") == 2
        assert event_types[-1] == "run.succeeded"

        # Model a browser that processed seq 4 but durably persisted only cursor 3.
        # Reconnect may redeliver seq 4; `(run_id, seq)` makes that duplicate benign.
        persisted_cursor = 3
        reconnect = maker.client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": str(persisted_cursor)},
        )
        assert reconnect.status_code == 200, reconnect.text
        reconnect_ids = _sse_event_ids(reconnect.text)
        assert reconnect_ids == [seq for seq in first_ids if seq > persisted_cursor]
        combined = [
            *((run_id, seq) for seq in first_ids[:4]),
            *((run_id, seq) for seq in reconnect_ids),
        ]
        assert len(combined) > len(set(combined))
        assert sorted(set(combined)) == [(run_id, seq) for seq in first_ids]
    finally:
        if first_worker is not None:
            first_worker.close()
        if reaper is not None:
            reaper.close()
        if second_worker is not None:
            second_worker.close()
        _stop_api(api)


def test_multi_scope_budget_and_permit_failures_leave_no_partial_authority(
    tmp_path: Path,
) -> None:
    """Admission and claim each roll back all scopes when one scope rejects."""

    budget_path = tmp_path / "budget"
    budget_path.mkdir()
    budget_harness = _Harness(budget_path)
    _put_system_budget(
        budget_harness,
        budget_id="budget:system:reject-admission",
        limits=(CostAmountV1(dimension="request", value=0, unit="request"),),
    )
    base_artifact_id, base_ref = budget_harness.seed_base_snapshot()
    budget_api = _start_api(budget_harness.api_config())
    try:
        maker = _login(budget_api, MAKER_LOGIN, MAKER_PASSWORD)
        draft = maker.client.post(
            "/api/v1/patches",
            json=_patch_body(
                base_artifact_id=base_artifact_id,
                expected_ref=base_ref,
                new_value=80,
                rationale="Budget rejection must not publish partial Run authority.",
            ),
            headers=_headers(maker, idempotency_key="atomic-budget:draft"),
        )
        assert draft.status_code == 201, draft.text
        patch_id = draft.json()["artifact"]["artifact_id"]

        engine = get_engine(budget_harness.database_url)
        try:
            with Session(engine) as session:
                before = {
                    model.__tablename__: session.scalar(select(func.count()).select_from(model))
                    for model in (
                        BudgetRow,
                        BudgetSetSnapshotRow,
                        BudgetSnapshotRow,
                        ReservationGroupRow,
                        BudgetReservationRow,
                        RunRow,
                        RunEventRow,
                    )
                }
        finally:
            engine.dispose()

        rejected = _queue_patch_validation(
            maker,
            patch_artifact_id=patch_id,
            base_artifact_id=base_artifact_id,
            expected_ref=base_ref,
            key="atomic-budget",
        )
        assert rejected.status_code == 429, rejected.text
        assert rejected.json()["code"] == "quota_exceeded"
        assert _approval(maker, f"approval:patch:{patch_id}").status == "draft"

        engine = get_engine(budget_harness.database_url)
        try:
            with Session(engine) as session:
                after = {
                    model.__tablename__: session.scalar(select(func.count()).select_from(model))
                    for model in (
                        BudgetRow,
                        BudgetSetSnapshotRow,
                        BudgetSnapshotRow,
                        ReservationGroupRow,
                        BudgetReservationRow,
                        RunRow,
                        RunEventRow,
                    )
                }
        finally:
            engine.dispose()
        assert after == before
    finally:
        _stop_api(budget_api)

    permit_path = tmp_path / "permit"
    permit_path.mkdir()
    permit_harness = _Harness(permit_path)
    _put_system_budget(
        permit_harness,
        budget_id="budget:system:single-slot",
        limits=(CostAmountV1(dimension="concurrent_run", value=1, unit="count"),),
    )
    base_artifact_id, base_ref = permit_harness.seed_base_snapshot()
    permit_api = _start_api(permit_harness.api_config())
    holder = None
    contender = None
    try:
        maker = _login(permit_api, MAKER_LOGIN, MAKER_PASSWORD)
        run_ids: list[str] = []
        for ordinal, reward in enumerate((80, 90), start=1):
            draft = maker.client.post(
                "/api/v1/patches",
                json=_patch_body(
                    base_artifact_id=base_artifact_id,
                    expected_ref=base_ref,
                    new_value=reward,
                    rationale=f"Independent concurrency candidate {ordinal}.",
                ),
                headers=_headers(
                    maker,
                    idempotency_key=f"atomic-permit:{ordinal}:draft",
                ),
            )
            assert draft.status_code == 201, draft.text
            patch_id = draft.json()["artifact"]["artifact_id"]
            queued = _queue_patch_validation(
                maker,
                patch_artifact_id=patch_id,
                base_artifact_id=base_artifact_id,
                expected_ref=base_ref,
                key=f"atomic-permit:{ordinal}",
            )
            assert queued.status_code == 202, queued.text
            run_ids.append(queued.json()["run_id"])

        holder = build_worker_process(
            replace(
                permit_harness.worker_config(),
                worker_principal_id="service:worker:permit-holder",
            )
        )
        held = holder.dispatcher._claim()  # noqa: SLF001 - retain one real permit group
        assert held is not None and held.run.run_id in run_ids
        blocked_run_id = next(run_id for run_id in run_ids if run_id != held.run.run_id)

        engine = get_engine(permit_harness.database_url)
        try:
            with Session(engine) as session:
                before_groups = session.scalar(select(func.count()).select_from(PermitGroupRow))
                before_permits = session.scalar(
                    select(func.count()).select_from(ConcurrencyPermitRow)
                )
                before_attempts = session.scalar(select(func.count()).select_from(RunAttemptRow))
        finally:
            engine.dispose()

        contender = build_worker_process(
            replace(
                permit_harness.worker_config(),
                worker_principal_id="service:worker:permit-contender",
            )
        )
        assert asyncio.run(contender.dispatcher.dispatch_once()) is False
        blocked = _run(maker, blocked_run_id)
        assert blocked.status == "queued"
        assert blocked.attempt_no is None

        engine = get_engine(permit_harness.database_url)
        try:
            with Session(engine) as session:
                assert session.scalar(select(func.count()).select_from(PermitGroupRow)) == 1
                assert session.scalar(select(func.count()).select_from(PermitGroupRow)) == (
                    before_groups
                )
                assert (
                    session.scalar(select(func.count()).select_from(ConcurrencyPermitRow))
                    == before_permits
                )
                assert session.scalar(select(func.count()).select_from(RunAttemptRow)) == (
                    before_attempts
                )
                permits = session.scalars(select(ConcurrencyPermitRow)).all()
                assert {permit.run_id for permit in permits} == {held.run.run_id}
                assert {permit.budget_id for permit in permits} == {
                    f"budget:run:{held.run.run_id}",
                    "budget:principal:human:maker",
                    "budget:system:global",
                    "budget:system:single-slot",
                }
                blocked_row = session.get(RunRow, blocked_run_id)
                assert blocked_row is not None
                assert blocked_row.status == "queued"
                assert blocked_row.current_attempt_no is None
                assert blocked_row.concurrency_permit_group_id is None
        finally:
            engine.dispose()
    finally:
        if holder is not None:
            holder.close()
        if contender is not None:
            contender.close()
        _stop_api(permit_api)


def test_stale_worker_cannot_publish_but_settles_incurred_model_cost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider response earned after lease expiry charges cost but publishes no result."""

    import gameforge.apps.worker.app as worker_app
    import gameforge.apps.worker.dispatch as worker_dispatch

    harness = JourneyAHarness(tmp_path)
    base_id, constraint_id, expected_ref = harness.seed_authoring_inputs()
    authorities, transport, catalog, routing = _seed_journey_a_model_authority(harness)
    plan = _journey_a_execution_plan(
        kind=RunKindRef(kind="generation.propose", version=1),
        catalog=catalog,
        routing=routing,
    )
    api = _start_api(harness.api_config())
    process = None
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        admitted = maker.client.post(
            "/api/v1/generation:propose",
            json=_journey_a_generation_body(
                base_artifact_id=base_id,
                constraint_artifact_id=constraint_id,
                expected_ref=expected_ref,
                plan=plan,
                mode="record",
                cassette_artifact_id=None,
            ),
            headers=_headers(maker, idempotency_key="stale-cost:generation"),
        )
        assert admitted.status_code == 202, admitted.text
        run_id = admitted.json()["run_id"]

        engine = get_engine(harness.database_url)
        try:
            with Session(engine) as session:
                approvals_before = session.scalar(select(func.count()).select_from(ApprovalItemRow))
        finally:
            engine.dispose()

        started_at = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=1)
        worker_clock = _MutableUtcClock(started_at)
        monkeypatch.setattr(worker_app, "SystemUtcClock", lambda: worker_clock)
        monkeypatch.setattr(worker_dispatch, "SystemUtcClock", lambda: worker_clock)
        original_complete = transport.complete_with_timeout

        def expire_after_response(request, *, timeout_s: float):
            response = original_complete(request, timeout_s=timeout_s)
            worker_clock.current = started_at + timedelta(seconds=31)
            return response.model_copy(
                update={
                    "token_usage": TokenUsageObservationV1(
                        status="reported",
                        input_tokens=11,
                        output_tokens=7,
                        cache_read_tokens=0,
                        cache_write_tokens=0,
                        total_tokens=18,
                    ),
                }
            )

        monkeypatch.setattr(transport, "complete_with_timeout", expire_after_response)
        process = build_worker_process(
            harness.worker_config(),
            model_execution_authorities=authorities,
        )
        assert asyncio.run(process.dispatcher.dispatch_once()) is True
        assert transport.calls == ["generation"]

        stale = _run(maker, run_id)
        assert stale.status == "running"
        assert stale.attempt_no == 1
        assert stale.result_artifact_id is None
        assert stale.failure_artifact_id is None

        engine = get_engine(harness.database_url)
        try:
            with Session(engine) as session:
                ledger = SqlCostLedger(session, clock=worker_clock)
                usage = ledger.list_usage(run_id=run_id, attempt_no=1)
                assert {item.scope for item in usage} == {"agent_step", "attempt_call"}
                call_usage = next(item for item in usage if item.scope == "attempt_call")
                assert call_usage.token_usage.status == "reported"
                assert call_usage.token_usage.input_tokens == 11
                assert call_usage.token_usage.output_tokens == 7
                assert call_usage.token_usage.total_tokens == 18
                group_statuses = {
                    item.scope: ledger.get_reservation_group(item.reservation_group_id).status
                    for item in usage
                }
                assert group_statuses == {
                    "attempt_call": "reconciled",
                    "agent_step": "reconciled",
                }
                assert (
                    SqlRunRepository(session).list_model_response_consumptions(
                        run_id,
                        attempt_no=1,
                    )
                    == ()
                )
                assert (
                    session.scalars(
                        select(ArtifactRow).where(ArtifactRow.kind == "cassette_bundle")
                    ).all()
                    == []
                )
                assert (
                    session.scalar(select(func.count()).select_from(ApprovalItemRow))
                    == approvals_before
                )
                retained = SqlRunRepository(session).get(run_id)
                assert retained is not None
                assert retained.status == "running"
                assert retained.result_artifact_id is None
                assert retained.failure_artifact_id is None
        finally:
            engine.dispose()
    finally:
        if process is not None:
            process.close()
        _stop_api(api)


def test_ws_cancel_disconnect_reaper_and_exact_duplicate_reconnect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One durable cancel survives disconnect, worker restart, and lease reaping."""

    import gameforge.apps.worker.app as worker_app
    import gameforge.apps.worker.dispatch as worker_dispatch

    harness = _Harness(tmp_path)
    base_artifact_id, base_ref = harness.seed_base_snapshot()
    api = _start_api(
        replace(
            harness.api_config(),
            allowed_websocket_origins=frozenset({"https://gameforge.test"}),
        )
    )
    process = None
    recovered = None
    try:
        maker = _login(api, MAKER_LOGIN, MAKER_PASSWORD)
        draft = maker.client.post(
            "/api/v1/patches",
            json=_patch_body(
                base_artifact_id=base_artifact_id,
                expected_ref=base_ref,
                new_value=80,
                rationale="Validation Run used for durable cancellation recovery.",
            ),
            headers=_headers(maker, idempotency_key="ws-reaper:draft"),
        )
        assert draft.status_code == 201, draft.text
        patch_id = draft.json()["artifact"]["artifact_id"]
        approval_id = f"approval:patch:{patch_id}"
        item = _approval(maker, approval_id)
        validate = maker.client.post(
            f"/api/v1/patches/{patch_id}:validate",
            json=_validation_body(
                item,
                base_artifact_id=base_artifact_id,
                expected_ref=base_ref,
                checker_graph=True,
            ),
            headers=_headers(
                maker,
                idempotency_key="ws-reaper:validate",
                resource_kind="patch",
                resource_id=patch_id,
                revision=item.workflow_revision,
            ),
        )
        assert validate.status_code == 202, validate.text
        run_id = validate.json()["run_id"]

        # Freeze all worker UTC authorities at one instant.  The attempt is claimed and
        # started without running its executor/heartbeat, which models a worker that
        # disconnects after accepting a durable cancel command.
        started_at = datetime.now(UTC).replace(microsecond=0)
        worker_clock = FrozenUtcClock(started_at)
        monkeypatch.setattr(worker_app, "SystemUtcClock", lambda: worker_clock)
        monkeypatch.setattr(worker_dispatch, "SystemUtcClock", lambda: worker_clock)
        process = build_worker_process(harness.worker_config())
        claim = process.dispatcher._claim()  # noqa: SLF001 - exact persistent-worker seam
        assert claim is not None and claim.run.run_id == run_id
        started = process.dispatcher._start(  # noqa: SLF001 - no executor/heartbeat
            claim,
            trace_id=None,
        )
        assert started.run.status == "running"
        active = _run(maker, run_id)
        assert active.status == "running"

        command = RunCommandV1(
            command_id="command:ws-reaper-cancel",
            client_id="browser:failure-matrix",
            client_seq=1,
            idempotency_key="ws-reaper:cancel",
            expected_run_revision=active.revision,
            type="cancel",
            payload_schema_id="run-cancel@1",
            payload=CancelRunPayloadV1(reason_code="user_requested"),
        )
        session_token = maker.client.cookies.get("gameforge_session")
        assert session_token
        ws_headers = {
            "origin": "https://gameforge.test",
            # Starlette's in-process WS URL uses the ``ws`` scheme; pass the already
            # authenticated Secure cookie explicitly so the test models a browser's
            # same-origin ``wss`` handshake rather than weakening cookie policy.
            "cookie": f"gameforge_session={session_token}",
        }
        ws_protocols = [
            "gameforge.run-commands.v1",
            f"gameforge.csrf.{maker.csrf}",
        ]
        with maker.client.websocket_connect(
            f"/api/v1/runs/{run_id}/commands",
            headers=ws_headers,
            subprotocols=ws_protocols,
        ) as websocket:
            websocket.send_text(command.model_dump_json())
            first_ack = RunCommandAckV1.model_validate(websocket.receive_json())
        assert first_ack.status == "accepted"
        assert first_ack.persisted_status == "applied"
        assert _run(maker, run_id).status == "running"

        # Disconnect the stale worker, then reconstruct the composition at a second
        # fixed instant beyond the exact 30-second lease.  No sleep or wall-clock poll
        # participates in expiry.
        process.close()
        process = None
        worker_clock = FrozenUtcClock(started_at + timedelta(seconds=31))
        recovered = build_worker_process(harness.worker_config())
        asyncio.run(recovered.dispatcher.dispatch_once())
        terminal = _run(maker, run_id)
        assert terminal.status == "cancelled"
        assert _approval(maker, approval_id).status == "draft"

        # Reconnect after the reaper's terminal publication and resend the byte-exact
        # command.  The server replays the committed command instead of acting twice.
        with maker.client.websocket_connect(
            f"/api/v1/runs/{run_id}/commands",
            headers=ws_headers,
            subprotocols=ws_protocols,
        ) as websocket:
            websocket.send_text(command.model_dump_json())
            duplicate_ack = RunCommandAckV1.model_validate(websocket.receive_json())
        assert duplicate_ack.status == "duplicate"
        assert duplicate_ack.persisted_status == "applied"
        assert duplicate_ack.command_revision == first_ack.command_revision
        assert duplicate_ack.run_revision == first_ack.run_revision
        assert terminal.revision > duplicate_ack.run_revision

        commands = maker.client.get(
            f"/api/v1/runs/{run_id}/commands",
            params={"limit": 100},
        )
        assert commands.status_code == 200, commands.text
        assert [entry["command_id"] for entry in commands.json()["items"]] == [command.command_id]

        events = maker.client.get(f"/api/v1/runs/{run_id}/events")
        assert events.status_code == 200, events.text
        event_types = [
            line.removeprefix("event:")
            for line in events.text.splitlines()
            if line.startswith("event:")
        ]
        assert event_types.count("run.cancel_requested") == 1
        assert event_types.count("attempt.lease_expired") == 1
        assert event_types.count("run.cancelled") == 1
    finally:
        if process is not None:
            process.close()
        if recovered is not None:
            recovered.close()
        _stop_api(api)
