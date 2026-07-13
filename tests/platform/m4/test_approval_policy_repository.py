from __future__ import annotations
from datetime import datetime, timezone

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.workflow import (
    ApprovalPolicyRefV1,
    ApprovalPolicyRegistryV1,
    ApprovalPolicyV1,
    compute_approval_policy_digest,
    compute_approval_policy_registry_digest,
)
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import Base, PolicySnapshotRow
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository


NOW = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine(tmp_path) -> Engine:
    database = get_engine(f"sqlite:///{tmp_path / 'approval-policies.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _policy(version: str, *, subject_kinds=("patch",)) -> ApprovalPolicyV1:
    values = {
        "policy_version": version,
        "subject_kinds": subject_kinds,
        "maker_checker_required": True,
        "human_approver_required": True,
        "reauthorize_on_decision": True,
        "reauthorize_on_apply": True,
        "rollback_requires_approval": True,
        "terminal_revision_immutable": True,
    }
    return ApprovalPolicyV1(
        **values,
        policy_digest=compute_approval_policy_digest(**values),
    )


def _registry(*policies: ApprovalPolicyV1) -> ApprovalPolicyRegistryV1:
    return ApprovalPolicyRegistryV1(
        policies=policies,
        registry_digest=compute_approval_policy_registry_digest(policies),
    )


def _repository(session: Session) -> SqlPolicySnapshotRepository:
    return SqlPolicySnapshotRepository(session, clock=FrozenUtcClock(NOW))


def _ref(policy: ApprovalPolicyV1) -> ApprovalPolicyRefV1:
    return ApprovalPolicyRefV1(
        policy_version=policy.policy_version,
        policy_digest=policy.policy_digest,
    )


def test_registry_and_each_exact_policy_are_retained_and_idempotent(engine: Engine) -> None:
    first = _policy("approval@1", subject_kinds=("patch", "constraint_proposal"))
    second = _policy("approval@2", subject_kinds=("rollback_request",))
    registry = _registry(first, second)

    with Session(engine) as session, session.begin():
        repository = _repository(session)
        assert repository.put_approval_policy_registry(registry) == registry
        assert repository.put_approval_policy_registry(registry) == registry

    with Session(engine) as session:
        repository = _repository(session)
        assert repository.get_approval_policy_registry(registry.registry_digest) == registry
        assert repository.get_approval_policy(_ref(first)) == first
        assert repository.get_approval_policy(_ref(second)) == second
        rows = session.scalars(select(PolicySnapshotRow)).all()

    assert len(rows) == 3


def test_later_registry_does_not_remove_earlier_policy_history(engine: Engine) -> None:
    first = _policy("approval@1")
    second = _policy("approval@2", subject_kinds=("patch", "rollback_request"))
    original = _registry(first)
    expanded = _registry(first, second)

    with Session(engine) as session, session.begin():
        repository = _repository(session)
        repository.put_approval_policy_registry(original)
        repository.put_approval_policy_registry(expanded)

    with Session(engine) as session:
        repository = _repository(session)
        assert repository.get_approval_policy_registry(original.registry_digest) == original
        assert repository.get_approval_policy_registry(expanded.registry_digest) == expanded
        assert repository.get_approval_policy(_ref(first)) == first
        assert repository.get_approval_policy(_ref(second)) == second


def test_policy_version_is_immutable_across_registries(engine: Engine) -> None:
    original = _policy("approval@1", subject_kinds=("patch",))
    changed = _policy("approval@1", subject_kinds=("constraint_proposal",))

    with Session(engine) as session:
        repository = _repository(session)
        repository.put_approval_policy_registry(_registry(original))
        session.commit()

        with pytest.raises(IntegrityViolation, match="immutable"):
            repository.put_approval_policy_registry(_registry(changed))
        session.rollback()

        assert repository.get_approval_policy(_ref(original)) == original
        assert repository.get_approval_policy_registry(_registry(changed).registry_digest) is None


def test_wrong_exact_policy_digest_fails_closed(engine: Engine) -> None:
    policy = _policy("approval@1")
    with Session(engine) as session, session.begin():
        _repository(session).put_approval_policy_registry(_registry(policy))

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="digest"):
            _repository(session).get_approval_policy(
                ApprovalPolicyRefV1(
                    policy_version=policy.policy_version,
                    policy_digest="f" * 64,
                )
            )


@pytest.mark.parametrize("document_kind", ["approval_policy", "approval_policy_registry"])
def test_corrupt_approval_policy_history_fails_closed(
    engine: Engine,
    document_kind: str,
) -> None:
    policy = _policy("approval@1")
    registry = _registry(policy)
    with Session(engine) as session:
        _repository(session).put_approval_policy_registry(registry)
        session.commit()
        row = session.scalar(
            select(PolicySnapshotRow).where(PolicySnapshotRow.document_kind == document_kind)
        )
        assert row is not None
        row.payload_schema_version = "corrupt@1"
        session.commit()

    with Session(engine) as session:
        repository = _repository(session)
        with pytest.raises(IntegrityViolation, match="metadata"):
            if document_kind == "approval_policy":
                repository.get_approval_policy(_ref(policy))
            else:
                repository.get_approval_policy_registry(registry.registry_digest)
