from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
)
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryRefV1,
    DomainRegistryV1,
    DomainScope,
    compute_domain_registry_digest,
)
from gameforge.contracts.workflow import (
    AutoApplyPolicyRegistryV1,
    AutoApplyPolicyV1,
    DeterministicOracleDefinitionV1,
    DeterministicOracleRefV1,
    DeterministicOracleRegistryRefV1,
    DeterministicOracleRegistryV1,
    QualifiedOutcomeRuleRefV1,
    compute_auto_apply_policy_digest,
    compute_auto_apply_policy_registry_digest,
    compute_deterministic_oracle_digest,
    compute_deterministic_oracle_registry_digest,
)
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import Base, PolicySnapshotRow
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine(tmp_path) -> Engine:
    database = get_engine(f"sqlite:///{tmp_path / 'auto-apply-policies.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _repository(session: Session) -> SqlPolicySnapshotRepository:
    return SqlPolicySnapshotRepository(session, clock=FrozenUtcClock(NOW))


def _domains() -> DomainRegistryV1:
    definitions = (
        DomainDefinitionV1(
            domain_id="narrative",
            display_name="Narrative",
            tags=("narrative",),
            status="active",
        ),
    )
    return DomainRegistryV1(
        registry_version="domains@1",
        definitions=definitions,
        registry_digest=compute_domain_registry_digest("domains@1", definitions),
    )


def _domain_ref(registry: DomainRegistryV1) -> DomainRegistryRefV1:
    return DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )


def _oracles(
    domains: DomainRegistryV1,
    *,
    registry_version: str = "oracles@1",
    tool_version: str = "checker@1",
) -> DeterministicOracleRegistryV1:
    values = {
        "oracle_id": "graph.structural",
        "oracle_version": "1",
        "engine_kind": "graph",
        "tool_version": tool_version,
        "domain_registry": _domain_ref(domains),
        "supported_domain_scope": DomainScope(domain_ids=("narrative",)),
        "evidence_artifact_kinds": ("checker_run",),
        "evidence_payload_schema_ids": ("checker-evidence@1",),
        "predicate_schema_id": "structural-predicate@1",
    }
    definition = DeterministicOracleDefinitionV1(
        **values,
        oracle_digest=compute_deterministic_oracle_digest(**values),
    )
    return DeterministicOracleRegistryV1(
        registry_version=registry_version,
        definitions=(definition,),
        registry_digest=compute_deterministic_oracle_registry_digest(
            registry_version, (definition,)
        ),
    )


def _oracle_registry_ref(
    registry: DeterministicOracleRegistryV1,
) -> DeterministicOracleRegistryRefV1:
    return DeterministicOracleRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )


def _auto_registry(
    domains: DomainRegistryV1,
    oracles: DeterministicOracleRegistryV1,
    *,
    registry_version: str = "auto@1",
    maximum_operation_count: int = 1,
) -> AutoApplyPolicyRegistryV1:
    oracle = oracles.definitions[0]
    policy = AutoApplyPolicyV1(
        policy_id="structural-safe",
        policy_version="1",
        allowed_operation_kinds=("add_relation",),
        maximum_operation_count=maximum_operation_count,
        domain_registry=_domain_ref(domains),
        deterministic_oracle_registry=_oracle_registry_ref(oracles),
        required_deterministic_oracles=(
            DeterministicOracleRefV1(
                oracle_id=oracle.oracle_id,
                oracle_version=oracle.oracle_version,
                oracle_digest=oracle.oracle_digest,
            ),
        ),
        required_outcome_rules=(
            QualifiedOutcomeRuleRefV1(
                resolved_policy_id="patch-validation",
                outcome_rule_id="passed",
            ),
        ),
        allowed_domain_scopes=(DomainScope(domain_ids=("narrative",)),),
        forbidden_domain_scopes=(),
        require_no_numeric_value_change=True,
        require_no_narrative_text_change=True,
        allowed_ref_names=("content/head",),
    )
    return AutoApplyPolicyRegistryV1(
        registry_version=registry_version,
        policies=(policy,),
        registry_digest=compute_auto_apply_policy_registry_digest(
            registry_version, (policy,)
        ),
    )


def _auto_registry_ref(registry: AutoApplyPolicyRegistryV1) -> AutoApplyPolicyRegistryRefV1:
    return AutoApplyPolicyRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )


def _auto_policy_ref(registry: AutoApplyPolicyRegistryV1) -> AutoApplyPolicyRefV1:
    policy = registry.policies[0]
    return AutoApplyPolicyRefV1(
        registry=_auto_registry_ref(registry),
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        policy_digest=compute_auto_apply_policy_digest(policy),
    )


def _put_complete_history(
    repository: SqlPolicySnapshotRepository,
) -> tuple[
    DomainRegistryV1,
    DeterministicOracleRegistryV1,
    AutoApplyPolicyRegistryV1,
]:
    domains = _domains()
    oracles = _oracles(domains)
    auto = _auto_registry(domains, oracles)
    repository.put_domain_registry(domains)
    repository.put_deterministic_oracle_registry(oracles)
    repository.put_auto_apply_policy_registry(auto)
    return domains, oracles, auto


def test_exact_auto_apply_and_oracle_history_is_retained_idempotently(
    engine: Engine,
) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        _, oracles, auto = _put_complete_history(repository)
        assert repository.put_deterministic_oracle_registry(oracles) == oracles
        assert repository.put_auto_apply_policy_registry(auto) == auto

    with Session(engine) as session:
        repository = _repository(session)
        assert (
            repository.get_deterministic_oracle_registry(_oracle_registry_ref(oracles))
            == oracles
        )
        assert repository.get_auto_apply_policy_registry(_auto_registry_ref(auto)) == auto
        assert repository.get_auto_apply_policy(_auto_policy_ref(auto)) == auto.policies[0]
        assert len(session.scalars(select(PolicySnapshotRow)).all()) == 4


def test_exact_history_digest_mismatches_fail_closed(engine: Engine) -> None:
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        _, oracles, auto = _put_complete_history(repository)

    with Session(engine) as session:
        repository = _repository(session)
        with pytest.raises(IntegrityViolation, match="digest"):
            repository.get_deterministic_oracle_registry(
                _oracle_registry_ref(oracles).model_copy(
                    update={"registry_digest": "f" * 64}
                )
            )
        with pytest.raises(IntegrityViolation, match="digest"):
            repository.get_auto_apply_policy_registry(
                _auto_registry_ref(auto).model_copy(update={"registry_digest": "f" * 64})
            )
        with pytest.raises(IntegrityViolation, match="digest"):
            repository.get_auto_apply_policy(
                _auto_policy_ref(auto).model_copy(update={"policy_digest": "f" * 64})
            )


def test_registry_lookup_fails_when_retained_policy_history_is_incomplete(
    engine: Engine,
) -> None:
    with Session(engine) as session:
        repository = _repository(session)
        _, _, auto = _put_complete_history(repository)
        policy = auto.policies[0]
        row = session.get(
            PolicySnapshotRow,
            ("auto_apply_policy", policy.policy_id, policy.policy_version),
        )
        assert row is not None
        session.delete(row)
        session.commit()

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="history is incomplete"):
            _repository(session).get_auto_apply_policy_registry(_auto_registry_ref(auto))


def test_auto_policy_lookup_fails_when_oracle_history_is_incomplete(engine: Engine) -> None:
    with Session(engine) as session:
        repository = _repository(session)
        _, oracles, auto = _put_complete_history(repository)
        row = session.get(
            PolicySnapshotRow,
            (
                "deterministic_oracle_registry",
                "platform_deterministic_oracles",
                oracles.registry_version,
            ),
        )
        assert row is not None
        session.delete(row)
        session.commit()

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="oracle registry history"):
            _repository(session).get_auto_apply_policy(_auto_policy_ref(auto))


def test_same_policy_or_registry_version_with_different_content_fails_closed(
    engine: Engine,
) -> None:
    domains = _domains()
    original_oracles = _oracles(domains)
    changed_oracles = _oracles(domains, tool_version="checker@2")
    original_auto = _auto_registry(domains, original_oracles)
    changed_auto = _auto_registry(
        domains,
        original_oracles,
        registry_version="auto@2",
        maximum_operation_count=2,
    )

    with Session(engine) as session:
        repository = _repository(session)
        repository.put_domain_registry(domains)
        repository.put_deterministic_oracle_registry(original_oracles)
        repository.put_auto_apply_policy_registry(original_auto)
        session.commit()

        with pytest.raises(IntegrityViolation, match="immutable"):
            repository.put_deterministic_oracle_registry(changed_oracles)
        session.rollback()

        with pytest.raises(IntegrityViolation, match="immutable"):
            repository.put_auto_apply_policy_registry(changed_auto)
