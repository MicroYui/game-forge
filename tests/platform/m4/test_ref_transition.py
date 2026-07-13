from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.storage import RefTransitionV1, RefValue
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import (
    ApprovalItemRow,
    ArtifactRow,
    Base,
    RefTransitionRow,
)
from gameforge.runtime.persistence.ref_transitions import SqlRefTransitionRepository


NOW = "2026-07-14T12:00:00Z"


@pytest.fixture
def engine(tmp_path: object) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'ref-transitions.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _artifact_row(artifact_id: str) -> ArtifactRow:
    return ArtifactRow(
        artifact_id=artifact_id,
        lineage_schema_version="lineage@1",
        kind="ir_snapshot",
        version_tuple={},
        lineage=[],
        payload_hash=None,
        created_at=None,
        meta={},
        object_ref=None,
    )


def _seed_references(session: Session) -> None:
    session.add_all(
        [
            _artifact_row("artifact:current"),
            _artifact_row("artifact:target"),
            _artifact_row("artifact:rollback-request"),
        ]
    )
    session.flush()
    session.add(
        ApprovalItemRow(
            approval_id="approval:rollback",
            approval_schema_version="approval-item@1",
            subject_series_id="rollback:series",
            subject_revision=1,
            subject_kind="rollback_request",
            subject_artifact_id="artifact:rollback-request",
            subject_digest="a" * 64,
            status="approved",
            workflow_revision=2,
            supersedes_approval_id=None,
            proposer={"principal_id": "human:a", "principal_kind": "human"},
            domain_scope={"domain_ids": ["economy"]},
            domain_registry_ref={"registry_version": "domains@1", "registry_digest": "b" * 64},
            route_policy={
                "route_version": "routes@1",
                "route_digest": "c" * 64,
            },
            role_policy_version="roles@1",
            role_policy_digest="d" * 64,
            approval_policy={
                "policy_id": "approval@1",
                "policy_version": "1",
                "policy_digest": "e" * 64,
            },
            requirements=[],
            active_validation_run_id=None,
            last_validation_failure_artifact_id=None,
            evidence_set_artifact_id=None,
            regression_evidence_artifact_ids=[],
            target_binding=None,
            auto_apply_proof=None,
            created_at=NOW,
            submitted_at=NOW,
            decided_at=NOW,
            applied_at=None,
        )
    )
    session.flush()


def _transition() -> RefTransitionV1:
    return RefTransitionV1.create(
        ref_name="refs/main",
        from_ref=RefValue(artifact_id="artifact:current", revision=2),
        to_ref=RefValue(artifact_id="artifact:target", revision=3),
        approval_item_id="approval:rollback",
        actor=AuditActor(principal_id="service:worker", principal_kind="service"),
        initiated_by=AuditActor(principal_id="human:a", principal_kind="human"),
        request_id="request:rollback",
        occurred_at=NOW,
    )


def test_put_and_get_ref_transition_are_exact_and_idempotent(engine: Engine) -> None:
    transition = _transition()
    with Session(engine) as session, session.begin():
        _seed_references(session)
        repository = SqlRefTransitionRepository(session)

        assert repository.get(transition.transition_id) is None
        assert repository.put(transition) == transition
        assert repository.put(RefTransitionV1.model_validate(transition.model_dump())) == transition
        assert repository.get(transition.transition_id) == transition

        row_count = session.scalar(select(func.count()).select_from(RefTransitionRow))
        assert row_count == 1


def test_same_transition_id_with_different_wire_is_rejected_without_overwrite(
    engine: Engine,
) -> None:
    transition = _transition()
    with Session(engine) as session, session.begin():
        _seed_references(session)
        repository = SqlRefTransitionRepository(session)
        repository.put(transition)
        malformed = transition.model_copy(update={"request_id": "request:different"})

        with pytest.raises(IntegrityViolation, match="wire"):
            repository.put(malformed)

        assert repository.get(transition.transition_id) == transition


def test_get_fails_closed_when_stored_transition_content_is_corrupt(
    engine: Engine,
) -> None:
    transition = _transition()
    with Session(engine) as session, session.begin():
        _seed_references(session)
        SqlRefTransitionRepository(session).put(transition)

    with Session(engine) as session, session.begin():
        row = session.get(RefTransitionRow, transition.transition_id)
        assert row is not None
        row.request_id = "request:tampered"

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="stored RefTransition"):
            SqlRefTransitionRepository(session).get(transition.transition_id)


def test_put_translates_missing_persisted_references_to_integrity_failure(
    engine: Engine,
) -> None:
    with Session(engine) as session, session.begin():
        with pytest.raises(IntegrityViolation, match="persisted references"):
            SqlRefTransitionRepository(session).put(_transition())
