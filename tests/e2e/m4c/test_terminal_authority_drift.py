"""Real SQLite regression for stage-to-commit terminal authority drift."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from gameforge.apps.api.local import build_local_api_resources
from gameforge.apps.worker.app import WORKER_RUN_AUDIT_CHAIN_ID
from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    DependencyFailureV1,
    PreparedRunFailure,
    RetryDecisionV1,
)
from gameforge.platform.runs.admission import AdmissionRequestContext
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import AuditHeadRow, AuditRow, RunAttemptRow
from tests.e2e.m4c.test_composition import _Harness, _checker_params, _tooling_actor


_DML_OPERATIONS = frozenset({"INSERT", "UPDATE", "DELETE", "REPLACE"})
_READ_OPERATIONS = frozenset({"SELECT", "WITH"})


def _sql_operation(statement: str) -> str:
    return statement.lstrip().split(None, 1)[0].upper()


def _is_sql_read(statement: str) -> bool:
    return _sql_operation(statement) in _READ_OPERATIONS


def _is_sql_dml(statement: str) -> bool:
    return _sql_operation(statement) in _DML_OPERATIONS


def _is_audit_read(statement: str) -> bool:
    lowered = statement.lower()
    return _is_sql_read(statement) and ("audit_heads" in lowered or "from audit" in lowered)


def test_cross_connection_attempt_drift_aborts_before_first_terminal_dml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path)
    snapshot_id = harness.seed_ir_snapshot(artifact_id_tag="drift-source@1")
    resources = build_local_api_resources(harness.api_config())
    accepted = resources.dependencies.run_admission.admit_generic_run(
        params=_checker_params(snapshot_id),
        actor=_tooling_actor(harness),
        server=AdmissionRequestContext(
            idempotency_key="checker:terminal-drift:1",
            request_hash="d" * 64,
            trace_id=None,
        ),
    )
    process = build_worker_process(harness.worker_config())
    drift_engine = get_engine(harness.database_url)
    runtime_engine = process.runtime.engine
    lifecycle = process.dispatcher._lifecycle  # noqa: SLF001 - exact production boundary
    stager = lifecycle._stage_publications  # noqa: SLF001 - inject post-stage drift
    assert stager is not None
    original_stage = stager.stage
    capture_terminal_writes = False
    terminal_dml: list[str] = []
    drift_count = 0

    def capture_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if capture_terminal_writes and _is_sql_dml(statement):
            terminal_dml.append(statement)

    def stage_then_drift(drafts):
        nonlocal capture_terminal_writes, drift_count
        staged = original_stage(drafts)
        drift_count += 1
        # A separate Engine guarantees this write is outside both the read snapshot
        # which built ``drafts`` and the runtime Engine's forthcoming writer UoW.
        with drift_engine.begin() as connection:
            changed = connection.execute(
                update(RunAttemptRow)
                .where(
                    RunAttemptRow.run_id == accepted.run_id,
                    RunAttemptRow.attempt_no == 1,
                )
                .values(trace_id=f"post-stage-drift:{drift_count}")
            )
            assert changed.rowcount == 1
        capture_terminal_writes = True
        return staged

    monkeypatch.setattr(stager, "stage", stage_then_drift)
    event.listen(runtime_engine, "before_cursor_execute", capture_statement)
    try:
        with pytest.raises(IntegrityViolation, match="did not stabilize"):
            asyncio.run(process.dispatcher.dispatch_once())
    finally:
        event.remove(runtime_engine, "before_cursor_execute", capture_statement)
        process.close()
        resources.close()
        drift_engine.dispose()

    assert drift_count == 3
    assert terminal_dml == []
    retained = harness.run_record(accepted.run_id)
    assert retained is not None
    assert retained.status == "running"
    assert retained.result_artifact_id is None
    assert retained.failure_artifact_id is None


def test_terminal_audit_preflight_failure_aborts_before_first_terminal_dml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path)
    snapshot_id = harness.seed_ir_snapshot(artifact_id_tag="audit-drift-source@1")
    resources = build_local_api_resources(harness.api_config())
    accepted = resources.dependencies.run_admission.admit_generic_run(
        params=_checker_params(snapshot_id),
        actor=_tooling_actor(harness),
        server=AdmissionRequestContext(
            idempotency_key="checker:terminal-audit-drift:1",
            request_hash="a" * 64,
            trace_id=None,
        ),
    )
    process = build_worker_process(harness.worker_config())
    drift_engine = get_engine(harness.database_url)
    runtime_engine = process.runtime.engine
    lifecycle = process.dispatcher._lifecycle  # noqa: SLF001 - production boundary
    stager = lifecycle._stage_publications  # noqa: SLF001 - inject post-stage corruption
    assert stager is not None
    original_stage = stager.stage
    capture_terminal_writes = False
    terminal_dml: list[str] = []
    stage_count = 0

    def capture_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if capture_terminal_writes and _is_sql_dml(statement):
            terminal_dml.append(statement)

    def stage_then_corrupt_audit(drafts):
        nonlocal capture_terminal_writes, stage_count
        staged = original_stage(drafts)
        stage_count += 1
        with drift_engine.begin() as connection:
            head_seq = connection.scalar(
                select(AuditHeadRow.head_seq).where(
                    AuditHeadRow.chain_id == WORKER_RUN_AUDIT_CHAIN_ID
                )
            )
            assert isinstance(head_seq, int) and head_seq > 0
            changed = connection.execute(
                update(AuditRow)
                .where(
                    AuditRow.audit_schema_version == "audit@2",
                    AuditRow.chain_id == WORKER_RUN_AUDIT_CHAIN_ID,
                    AuditRow.chain_seq == head_seq,
                )
                .values(action="tampered-after-terminal-stage")
            )
            assert changed.rowcount == 1
        capture_terminal_writes = True
        return staged

    monkeypatch.setattr(stager, "stage", stage_then_corrupt_audit)
    event.listen(runtime_engine, "before_cursor_execute", capture_statement)
    try:
        with pytest.raises(IntegrityViolation, match="audit chain"):
            asyncio.run(process.dispatcher.dispatch_once())
    finally:
        event.remove(runtime_engine, "before_cursor_execute", capture_statement)
        process.close()
        resources.close()
        drift_engine.dispose()

    assert stage_count == 1
    assert terminal_dml == []
    retained = harness.run_record(accepted.run_id)
    assert retained is not None
    assert retained.status == "running"
    assert retained.result_artifact_id is None
    assert retained.failure_artifact_id is None


def test_success_terminal_performs_all_audit_reads_before_first_terminal_dml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(tmp_path)
    snapshot_id = harness.seed_ir_snapshot(artifact_id_tag="audit-order-source@1")
    resources = build_local_api_resources(harness.api_config())
    accepted = resources.dependencies.run_admission.admit_generic_run(
        params=_checker_params(snapshot_id),
        actor=_tooling_actor(harness),
        server=AdmissionRequestContext(
            idempotency_key="checker:terminal-audit-order:1",
            request_hash="b" * 64,
            trace_id=None,
        ),
    )
    process = build_worker_process(harness.worker_config())
    runtime_engine = process.runtime.engine
    lifecycle = process.dispatcher._lifecycle  # noqa: SLF001 - production boundary
    stager = lifecycle._stage_publications  # noqa: SLF001 - observe terminal boundary
    assert stager is not None
    original_stage = stager.stage
    capture_terminal_sql = False
    terminal_sql: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if capture_terminal_sql:
            terminal_sql.append(statement)

    def stage_then_capture(drafts):
        nonlocal capture_terminal_sql
        staged = original_stage(drafts)
        capture_terminal_sql = True
        return staged

    monkeypatch.setattr(stager, "stage", stage_then_capture)
    event.listen(runtime_engine, "before_cursor_execute", capture_statement)
    try:
        assert asyncio.run(process.dispatcher.dispatch_once()) is True
    finally:
        event.remove(runtime_engine, "before_cursor_execute", capture_statement)
        process.close()
        resources.close()

    dml_index = next(
        index for index, statement in enumerate(terminal_sql) if _is_sql_dml(statement)
    )
    audit_read_indexes = tuple(
        index for index, statement in enumerate(terminal_sql) if _is_audit_read(statement)
    )
    assert len(audit_read_indexes) == 2
    assert max(audit_read_indexes) < dml_index
    assert all(not _is_sql_read(statement) for statement in terminal_sql[dml_index + 1 :])
    retained = harness.run_record(accepted.run_id)
    assert retained is not None and retained.status == "succeeded"


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_audit_actions"),
    [
        (
            "retry",
            "retry_wait",
            ("run.attempt_failure", "run.attempt_closed"),
        ),
        (
            "terminal",
            "failed",
            (
                "run.attempt_failure",
                "run.failure",
                "run.attempt_closed",
                "run.terminal",
            ),
        ),
    ],
)
def test_failure_terminal_audits_are_preflighted_and_apply_without_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_status: str,
    expected_audit_actions: tuple[str, ...],
) -> None:
    harness = _Harness(tmp_path)
    snapshot_id = harness.seed_ir_snapshot(artifact_id_tag=f"audit-{outcome}-source@1")
    resources = build_local_api_resources(harness.api_config())
    accepted = resources.dependencies.run_admission.admit_generic_run(
        params=_checker_params(snapshot_id),
        actor=_tooling_actor(harness),
        server=AdmissionRequestContext(
            idempotency_key=f"checker:terminal-audit-{outcome}:1",
            request_hash=("c" if outcome == "retry" else "e") * 64,
            trace_id=None,
        ),
    )
    process = build_worker_process(harness.worker_config())
    runtime_engine = process.runtime.engine
    lifecycle = process.dispatcher._lifecycle  # noqa: SLF001 - production boundary
    runner = process.dispatcher._runner  # noqa: SLF001 - inject exact worker outcome
    stager = lifecycle._stage_publications  # noqa: SLF001 - observe terminal boundary
    assert stager is not None
    original_stage = stager.stage
    capture_terminal_sql = False
    terminal_sql: list[str] = []

    def terminal_executor(context):
        dependency = (
            DependencyFailureV1(
                dependency_kind="database",
                dependency_id="database:terminal-audit-test",
                operation_code="execute",
                classifier_code="dependency_unavailable",
            )
            if outcome == "retry"
            else None
        )
        return PreparedRunFailure(
            run_id=context.run.run_id,
            attempt_no=context.attempt.attempt_no,
            run_kind=context.run.kind,
            artifacts=(),
            requirement_dispositions=(),
            cause_code=("dependency_unavailable" if outcome == "retry" else "execution_failed"),
            failure_class=("transient_dependency" if outcome == "retry" else "execution"),
            intrinsic_retry_eligible=outcome == "retry",
            classifier=context.run.failure_classifier,
            dependency=dependency,
            redacted_message="injected terminal audit outcome",
        )

    monkeypatch.setattr(runner, "_resolve_executor", lambda _run: terminal_executor)
    if outcome == "retry":

        def retry_decision(*, run, prepared, now, **_kwargs):
            evaluated_at = now.isoformat().replace("+00:00", "Z")
            retry_at = (now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
            return RetryDecisionV1(
                cause_code=prepared.cause_code,
                failure_class=prepared.failure_class,
                intrinsic_retry_eligible=True,
                decision="retry",
                reason_code="transient_eligible",
                retry_not_before_utc=retry_at,
                classifier=run.failure_classifier,
                retry_policy=run.retry_policy,
                evaluated_at_utc=evaluated_at,
            )

        monkeypatch.setattr(lifecycle, "_decide_retry", retry_decision)

    def capture_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if capture_terminal_sql:
            terminal_sql.append(statement)

    def stage_then_capture(drafts):
        nonlocal capture_terminal_sql
        staged = original_stage(drafts)
        capture_terminal_sql = True
        return staged

    monkeypatch.setattr(stager, "stage", stage_then_capture)
    event.listen(runtime_engine, "before_cursor_execute", capture_statement)
    try:
        assert asyncio.run(process.dispatcher.dispatch_once()) is True
    finally:
        event.remove(runtime_engine, "before_cursor_execute", capture_statement)
        process.close()
        resources.close()

    dml_index = next(
        index for index, statement in enumerate(terminal_sql) if _is_sql_dml(statement)
    )
    audit_read_indexes = tuple(
        index for index, statement in enumerate(terminal_sql) if _is_audit_read(statement)
    )
    assert len(audit_read_indexes) == 2
    assert max(audit_read_indexes) < dml_index
    assert all(not _is_sql_read(statement) for statement in terminal_sql[dml_index + 1 :])
    with Session(runtime_engine) as session:
        terminal_actions = session.scalars(
            select(AuditRow.action)
            .where(
                AuditRow.audit_schema_version == "audit@2",
                AuditRow.chain_id == WORKER_RUN_AUDIT_CHAIN_ID,
            )
            .order_by(AuditRow.chain_seq.desc())
            .limit(len(expected_audit_actions))
        ).all()
    assert tuple(reversed(terminal_actions)) == expected_audit_actions
    retained = harness.run_record(accepted.run_id)
    assert retained is not None and retained.status == expected_status
