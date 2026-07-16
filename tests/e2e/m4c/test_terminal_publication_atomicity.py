"""Task 9 terminal publication is one real SQL transaction.

This test deliberately fails the *last* run-authority write after the production
publisher has inserted its Artifacts/Finding/link, applied the ApprovalItem workflow
effect, appended terminal audit, and closed Cost authority.  The production SQLite
UnitOfWork must roll every one of those writes back together.  It then proves the same
attempt can commit once and that a duplicate terminal hand-off creates no new authority.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from gameforge.apps.api.local import build_local_api_resources
from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.contracts.api import PatchValidationAdmissionRequestV1
from gameforge.contracts.errors import Conflict, InvalidStateTransition
from gameforge.contracts.execution_profiles import ProfileRefV1
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.jobs import RefReadBindingV1
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import SubjectHead
from gameforge.platform.runs.admission import AdmissionRequestContext
from gameforge.runtime.persistence.cursor import CursorSigner
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.refs import SqlRefStore
from gameforge.runtime.persistence.runs import SqlRunRepository
from tests.e2e.m4c.test_composition import _Harness, _tooling_actor
from tests.e2e.m4c.test_validation_completion_effect import (
    REF_NAME,
    _GRAPH_CHECKER,
    _canonical,
    _clean_snapshot,
    _dangling_snapshot,
    _draft_item,
    _persist,
    _store_artifact,
)


class _InjectedLateAuthorityFailure(RuntimeError):
    pass


type DatabaseSnapshot = dict[str, tuple[tuple[tuple[str, object], ...], ...]]


def _database_snapshot(database_url: str) -> DatabaseSnapshot:
    """Byte-value-equivalent snapshot of every authoritative SQL table."""

    engine = get_engine(database_url)
    try:
        with engine.connect() as connection:
            tables = sorted(inspect(connection).get_table_names())
            snapshot: DatabaseSnapshot = {}
            for table in tables:
                quoted = connection.dialect.identifier_preparer.quote(table)
                rows = connection.exec_driver_sql(f"SELECT * FROM {quoted}").mappings().all()
                normalized = (tuple(sorted(row.items())) for row in rows)
                snapshot[table] = tuple(sorted(normalized, key=repr))
            return snapshot
    finally:
        engine.dispose()


def _rows(snapshot: DatabaseSnapshot, table: str) -> tuple[dict[str, object], ...]:
    return tuple(dict(row) for row in snapshot[table])


def _seed_and_admit_validation(harness: _Harness) -> tuple[object, object, str, str]:
    """Build the same real validating Patch subject as the Journey-B E2E path."""

    clean_blob, clean_snapshot_id = _clean_snapshot()
    base = _store_artifact(
        harness,
        kind="ir_snapshot",
        schema="ir-core@1",
        blob=clean_blob,
        version_tuple=VersionTuple(
            ir_snapshot_id=clean_snapshot_id,
            tool_version="ir-core@1",
        ),
    )
    preview_blob, preview_snapshot_id = _dangling_snapshot()
    patch_payload = PatchV2(
        revision=1,
        base_snapshot_id=clean_snapshot_id,
        target_snapshot_id=preview_snapshot_id,
        side_effect_risk="low",
        ops=[],
        produced_by="human",
        rationale="Task 9 SQL atomicity fixture.",
    )
    patch = _store_artifact(
        harness,
        kind="patch",
        schema="patch@2",
        blob=_canonical(patch_payload.model_dump(mode="json")),
        version_tuple=VersionTuple(
            ir_snapshot_id=clean_snapshot_id,
            tool_version="patch@2",
        ),
        lineage=(base.artifact_id,),
    )
    preview = _store_artifact(
        harness,
        kind="ir_snapshot",
        schema="ir-core@1",
        blob=preview_blob,
        version_tuple=VersionTuple(
            ir_snapshot_id=preview_snapshot_id,
            tool_version="ir-core@1",
        ),
        lineage=(base.artifact_id, patch.artifact_id),
    )

    engine = get_engine(harness.database_url)
    try:
        with Session(engine) as session, session.begin():
            ref = SqlRefStore(
                session,
                cursor_signer=CursorSigner(signing_key=b"a" * 32, clock=harness.clock),
                clock=harness.clock,
            ).compare_and_set(REF_NAME, None, base.artifact_id)
            assert ref == RefValue(artifact_id=base.artifact_id, revision=1)
    finally:
        engine.dispose()

    approval_id = "approval:terminal-atomicity"
    series_id = "series:terminal-atomicity"
    item = _draft_item(
        harness,
        approval_id=approval_id,
        series_id=series_id,
        patch=patch,
        preview=preview,
        base=base,
        preview_snapshot_id=preview_snapshot_id,
    )
    head = SubjectHead(
        subject_series_id=series_id,
        current_subject_artifact_id=patch.artifact_id,
        current_approval_id=approval_id,
        revision=1,
    )
    _persist(
        harness,
        lambda repository: (
            repository.insert_draft(item),
            repository.compare_and_set_subject_head(series_id, None, head),
        ),
    )

    resources = build_local_api_resources(harness.api_config())
    process = build_worker_process(harness.worker_config())
    request = PatchValidationAdmissionRequestV1(
        approval_id=approval_id,
        expected_subject_head_revision=1,
        expected_workflow_revision=1,
        subject_digest=patch.payload_hash,
        base_snapshot_artifact_id=base.artifact_id,
        preview_snapshot_artifact_id=preview.artifact_id,
        candidate_config_export_artifact_ids=(),
        target=RefReadBindingV1(
            ref_name=REF_NAME,
            expected_ref=RefValue(artifact_id=base.artifact_id, revision=1),
        ),
        validation_policy=ProfileRefV1(profile_id="builtin.validation", version=1),
        checker_profiles=(_GRAPH_CHECKER,),
        simulation_profiles=(),
        findings=(),
        review_artifact_ids=(),
        playtest_trace_artifact_ids=(),
        regression_suite_artifact_ids=(),
    )
    accepted = resources.dependencies.run_admission.admit(
        operation="patch.validate",
        resource_id=patch.artifact_id,
        request=request,
        actor=_tooling_actor(harness),
        server=AdmissionRequestContext(
            idempotency_key="terminal-atomicity:1",
            request_hash="e" * 64,
            trace_id=None,
        ),
    )
    return resources, process, accepted.run_id, approval_id


def test_late_run_cas_failure_rolls_back_every_terminal_authority_and_duplicate_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path)
    resources, process, run_id, approval_id = _seed_and_admit_validation(harness)
    original_complete = SqlRunRepository.complete_attempt_success
    late_write_reached = Event()

    def fail_after_run_cas(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        original_complete(self, *args, **kwargs)
        late_write_reached.set()
        raise _InjectedLateAuthorityFailure("fail after the terminal Run CAS")

    try:
        claim = process.dispatcher._claim()
        assert claim is not None and claim.run.run_id == run_id
        started = process.dispatcher._start(claim)
        assert started.run.status == "running"

        before = _database_snapshot(harness.database_url)
        before_run = next(row for row in _rows(before, "runs") if row["run_id"] == run_id)
        before_item = next(
            row for row in _rows(before, "approval_items") if row["approval_id"] == approval_id
        )
        assert before_run["status"] == "running"
        assert before_item["status"] == "validating"
        assert before_item["active_validation_run_id"] == run_id
        assert any(
            row["run_id"] == run_id and row["status"] == "reserved"
            for row in _rows(before, "reservation_groups")
        )
        assert any(
            row["run_id"] == run_id and row["status"] == "active"
            for row in _rows(before, "permit_groups")
        )

        monkeypatch.setattr(SqlRunRepository, "complete_attempt_success", fail_after_run_cas)
        deadline = datetime.fromisoformat(
            started.attempt.attempt_deadline_utc.replace("Z", "+00:00")
        )

        async def exercise() -> None:
            with pytest.raises(_InjectedLateAuthorityFailure):
                await process.dispatcher._runner.run_attempt(
                    run=started.run,
                    attempt=started.attempt,
                    lease=started.lease,
                    deadline_utc=deadline,
                )

            assert late_write_reached.is_set(), "the failure must occur after the real Run CAS"
            after_rollback = _database_snapshot(harness.database_url)
            assert after_rollback == before

            # Restoring the exact production write proves the rolled-back outcome remains
            # publishable: all of Artifact/Finding/link/workflow/audit/cost/Run commit once.
            monkeypatch.setattr(
                SqlRunRepository,
                "complete_attempt_success",
                original_complete,
            )
            committed = await process.dispatcher._runner.run_attempt(
                run=started.run,
                attempt=started.attempt,
                lease=started.lease,
                deadline_utc=deadline,
            )
            assert committed.run.status == "succeeded"
            after_commit = _database_snapshot(harness.database_url)
            assert after_commit != before

            committed_item = next(
                row
                for row in _rows(after_commit, "approval_items")
                if row["approval_id"] == approval_id
            )
            assert committed_item["status"] == "validation_failed"
            assert committed_item["active_validation_run_id"] is None
            committed_run = next(
                row for row in _rows(after_commit, "runs") if row["run_id"] == run_id
            )
            assert committed_run["status"] == "succeeded"
            assert len(_rows(after_commit, "artifacts")) > len(_rows(before, "artifacts"))
            assert len(_rows(after_commit, "finding_revisions")) > len(
                _rows(before, "finding_revisions")
            )
            assert len(_rows(after_commit, "run_finding_links")) > len(
                _rows(before, "run_finding_links")
            )
            assert len(_rows(after_commit, "audit")) > len(_rows(before, "audit"))
            assert len(_rows(after_commit, "run_events")) > len(_rows(before, "run_events"))
            assert all(
                row["status"] == "released"
                for row in _rows(after_commit, "reservation_groups")
                if row["run_id"] == run_id
            )
            assert all(
                row["status"] == "released"
                for row in _rows(after_commit, "permit_groups")
                if row["run_id"] == run_id
            )

            # The same stale attempt cannot create a second terminal aggregate.
            committed_snapshot = after_commit
            with pytest.raises((Conflict, InvalidStateTransition)):
                await process.dispatcher._runner.run_attempt(
                    run=started.run,
                    attempt=started.attempt,
                    lease=started.lease,
                    deadline_utc=deadline,
                )
            assert _database_snapshot(harness.database_url) == committed_snapshot

        asyncio.run(exercise())
    finally:
        process.close()
        resources.close()
