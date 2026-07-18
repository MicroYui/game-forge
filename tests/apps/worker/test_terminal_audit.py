from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gameforge.apps.worker.publication import (
    WorkerAuditPort,
    WorkerCommandTerminalPublicationGateway,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.lineage import AuditActor, AuditCorrelation
from gameforge.platform.audit.gate import AuditGate
from gameforge.platform.publication.publisher import AuditPublicationIntent
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.audit import SqlAuditSink
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import AuditRow, Base
from tests.platform.m4c.test_terminal_publisher import (
    _registry_and_definition,
    _run_record,
)


_NOW = datetime(2026, 7, 18, 8, 0, 0, tzinfo=timezone.utc)
_WORKER = AuditActor(principal_id="service:worker:1", principal_kind="service")


def test_command_terminal_gateway_reuses_preflighted_non_null_correlation() -> None:
    class _Commands:
        def record_command_submitted(self, **_kwargs) -> None:
            raise AssertionError("terminal command Audit escaped to the scalar command gate")

        def record_run_terminal(self, **_kwargs) -> None:
            raise AssertionError("terminal lifecycle Audit escaped to the scalar command gate")

    class _Terminal:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def commit_planned_run_failure(self, _draft, _staged, **kwargs):
            self.calls.append(("commit", kwargs))
            return "published"

        def record_command_submitted(self, **kwargs) -> None:
            self.calls.append(("command", kwargs))

        def record_run_terminal(self, **kwargs) -> None:
            self.calls.append(("terminal", kwargs))

    terminal = _Terminal()
    gateway = WorkerCommandTerminalPublicationGateway(  # type: ignore[arg-type]
        commands=_Commands(),
        terminal=terminal,
    )
    correlation = AuditCorrelation(
        request_id="request:cancel",
        run_id="run:1",
        trace_id="trace:attempt",
    )

    assert (
        gateway.commit_planned_run_failure(
            object(),
            object(),
            command_audit_correlation=correlation,
        )
        == "published"
    )
    gateway.record_command_submitted(
        events=(SimpleNamespace(trace_id=None),),
        request_id="request:cancel",
    )
    gateway.record_run_terminal(
        event=SimpleNamespace(trace_id=None),
        request_id="request:cancel",
    )

    assert [name for name, _kwargs in terminal.calls] == [
        "commit",
        "command",
        "terminal",
    ]
    assert terminal.calls[1][1]["trace_id"] == "trace:attempt"
    assert terminal.calls[2][1]["trace_id"] == "trace:attempt"


def test_worker_audit_port_preflights_one_stable_terminal_batch(tmp_path) -> None:
    engine = get_engine(f"sqlite:///{tmp_path / 'terminal-audit.db'}")
    Base.metadata.create_all(engine)
    _registry, definition = _registry_and_definition()
    run = _run_record(definition)
    publication_records = tuple(
        AuditPublicationIntent(
            action=action,
            run=run,
            artifact_id=artifact_id,
            actor=_WORKER,
            occurred_at="2026-07-18T07:59:59Z",
        )
        for action, artifact_id in (
            ("attempt.failure.publish", "artifact:attempt-failure"),
            ("run.failure.publish", "artifact:run-failure"),
        )
    )
    lifecycle_records = tuple(
        AuditPublicationIntent(
            action=action,
            run=run,
            artifact_id=None,
            actor=_WORKER,
            occurred_at="2026-07-18T07:59:59Z",
            deferred=True,
            request_id="request:terminal-command",
            trace_id="trace:terminal-command",
        )
        for action in ("run.command_submitted", "run.terminal")
    )

    try:
        with Session(engine) as session, session.begin():
            gate = AuditGate(sink=SqlAuditSink(session), clock=FrozenUtcClock(_NOW))
            port = WorkerAuditPort(audit_gate=gate, chain_id="platform-authority")
            prepared = port.preflight_records((*publication_records, *lifecycle_records))
            port.apply_preflighted_records(prepared)
            with pytest.raises(IntegrityViolation, match="already applied"):
                port.apply_preflighted_records(prepared)
            for action in ("run.command_submitted", "run.terminal"):
                port.record(
                    action=action,
                    run=run,
                    artifact_id=None,
                    actor=_WORKER,
                    occurred_at="2026-07-18T07:59:59Z",
                    request_id="request:terminal-command",
                    trace_id="trace:terminal-command",
                )

        with Session(engine) as session:
            retained = session.scalars(
                select(AuditRow)
                .where(AuditRow.audit_schema_version == "audit@2")
                .order_by(AuditRow.chain_seq)
            ).all()
        assert [row.action for row in retained] == [
            "attempt.failure.publish",
            "run.failure.publish",
            "run.command_submitted",
            "run.terminal",
        ]
        assert [row.artifact_id for row in retained] == [
            "artifact:attempt-failure",
            "artifact:run-failure",
            None,
            None,
        ]
        assert retained[0].initiated_by == run.initiated_by.model_dump(mode="json")
        assert retained[1].prev_hash == retained[0].content_hash
        assert retained[2].correlation == {
            "request_id": "request:terminal-command",
            "run_id": run.run_id,
            "trace_id": "trace:terminal-command",
        }
        assert retained[3].correlation == retained[2].correlation
        assert {row.ts for row in retained} == {"2026-07-18T08:00:00Z"}
    finally:
        engine.dispose()


def test_worker_terminal_audit_refuses_a_scalar_only_sink(tmp_path) -> None:
    engine = get_engine(f"sqlite:///{tmp_path / 'terminal-audit-scalar-only.db'}")
    Base.metadata.create_all(engine)
    _registry, definition = _registry_and_definition()
    run = _run_record(definition)

    class _ScalarOnlySink:
        def __init__(self, delegate: SqlAuditSink) -> None:
            self._delegate = delegate

        def lock_head(self, chain_id: str):
            return self._delegate.lock_head(chain_id)

        def append(self, record):
            return self._delegate.append(record)

        def append_preflighted(self, record, expected_head):
            return self._delegate.append_preflighted(record, expected_head)

        def register_before_commit_guard(self, guard):
            return self._delegate.register_before_commit_guard(guard)

        def verify_chain(self, chain_id: str):
            return self._delegate.verify_chain(chain_id)

    record = AuditPublicationIntent(
        action="publish-checker@1",
        run=run,
        artifact_id="artifact:result",
        actor=_WORKER,
        occurred_at="2026-07-18T07:59:59Z",
    )
    try:
        with pytest.raises(IntegrityViolation, match="requires batch sink authority"):
            with Session(engine) as session, session.begin():
                gate = AuditGate(
                    sink=_ScalarOnlySink(SqlAuditSink(session)),  # type: ignore[arg-type]
                    clock=FrozenUtcClock(_NOW),
                )
                WorkerAuditPort(
                    audit_gate=gate,
                    chain_id="platform-authority",
                ).preflight_records((record,))
        with Session(engine) as session:
            assert session.scalar(select(func.count()).select_from(AuditRow)) == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("missing", "not completely consumed"),
        ("mismatch", "differs from its preflighted record"),
        ("correlation", "differs from its preflighted record"),
        ("duplicate", "unexpected or was already consumed"),
    ],
)
def test_worker_audit_port_fails_closed_on_deferred_consumption_drift(
    tmp_path,
    mode: str,
    message: str,
) -> None:
    engine = get_engine(f"sqlite:///{tmp_path / f'terminal-audit-{mode}.db'}")
    Base.metadata.create_all(engine)
    _registry, definition = _registry_and_definition()
    run = _run_record(definition)
    records = (
        AuditPublicationIntent(
            action="publish-checker@1",
            run=run,
            artifact_id="artifact:result",
            actor=_WORKER,
            occurred_at="2026-07-18T07:59:59Z",
        ),
        AuditPublicationIntent(
            action="run.terminal",
            run=run,
            artifact_id=None,
            actor=_WORKER,
            occurred_at="2026-07-18T07:59:59Z",
            deferred=True,
        ),
    )

    try:
        with pytest.raises(IntegrityViolation, match=message):
            with Session(engine) as session, session.begin():
                gate = AuditGate(sink=SqlAuditSink(session), clock=FrozenUtcClock(_NOW))
                port = WorkerAuditPort(audit_gate=gate, chain_id="platform-authority")
                prepared = port.preflight_records(records)
                port.apply_preflighted_records(prepared)
                if mode == "mismatch":
                    port.record(
                        action="run.wrong",
                        run=run,
                        artifact_id=None,
                        actor=_WORKER,
                        occurred_at="2026-07-18T07:59:59Z",
                    )
                elif mode == "correlation":
                    port.record(
                        action="run.terminal",
                        run=run,
                        artifact_id=None,
                        actor=_WORKER,
                        occurred_at="2026-07-18T07:59:59Z",
                        request_id="request:wrong",
                    )
                elif mode == "duplicate":
                    for _ in range(2):
                        port.record(
                            action="run.terminal",
                            run=run,
                            artifact_id=None,
                            actor=_WORKER,
                            occurred_at="2026-07-18T07:59:59Z",
                        )

        with Session(engine) as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(AuditRow)
                    .where(AuditRow.audit_schema_version == "audit@2")
                )
                == 0
            )
    finally:
        engine.dispose()
