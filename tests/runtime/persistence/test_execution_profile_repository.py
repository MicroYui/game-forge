from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ExecutionProfileCatalogSnapshotV1,
    ExecutionProfileDefinitionV1,
    ExecutionProfileLifecycleV1,
    GenericProfileDetailsV1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    canonical_config_hash,
    execution_profile_catalog_digest,
    execution_profile_payload_hash,
)
from gameforge.contracts.identity import DomainScope
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.models import Base, PolicySnapshotRow
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository


NOW = datetime(2026, 7, 14, 11, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine(tmp_path) -> Engine:
    database = get_engine(f"sqlite:///{tmp_path / 'execution-profiles.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _repository(session: Session) -> SqlPolicySnapshotRepository:
    return SqlPolicySnapshotRepository(session, clock=FrozenUtcClock(NOW))


def _definition(
    *,
    profile: ProfileRefV1 | None = None,
    display_name: str = "Content rollback",
    history_limit: int = 100,
) -> ExecutionProfileDefinitionV1:
    config = {"history_limit": history_limit}
    return ExecutionProfileDefinitionV1(
        profile=profile or ProfileRefV1(profile_id="rollback.content", version=3),
        profile_kind="rollback",
        compatible_run_kinds=(RunKindRef(kind="rollback.validate", version=1),),
        domain_scope=DomainScope(domain_ids=("economy",)),
        stochastic=False,
        input_schema_ids=("rollback-request@1",),
        output_schema_ids=("validation-evidence@1",),
        required_capabilities=("ref-history.read",),
        display_name=display_name,
        handler_key="rollback.content",
        config_schema_id="rollback-profile@1",
        config=config,
        config_hash=canonical_config_hash(config),
        details=GenericProfileDetailsV1(),
    )


def _catalog(
    version: int,
    definition: ExecutionProfileDefinitionV1,
    *,
    state: str = "active",
    revision: int = 1,
    changed_at: str = "2026-07-14T11:00:00Z",
) -> ExecutionProfileCatalogSnapshotV1:
    lifecycle = ExecutionProfileLifecycleV1(
        profile=definition.profile,
        state=state,  # type: ignore[arg-type]
        revision=revision,
        reason_code="superseded" if state == "replay_only" else None,
        changed_at=changed_at,
    )
    payload = {
        "catalog_schema_version": "execution-profile-catalog@1",
        "catalog_version": version,
        "definitions": (definition,),
        "lifecycle": (lifecycle,),
    }
    return ExecutionProfileCatalogSnapshotV1(
        **payload,
        catalog_digest=execution_profile_catalog_digest(payload),
    )


def test_exact_catalog_is_immutable_idempotent_and_resolves_full_binding(
    engine: Engine,
) -> None:
    definition = _definition()
    catalog = _catalog(7, definition)

    with Session(engine) as session, session.begin():
        repository = _repository(session)
        assert repository.put_execution_profile_catalog(catalog) == catalog
        assert repository.put_execution_profile_catalog(catalog) == catalog

    with Session(engine) as session:
        repository = _repository(session)
        assert (
            repository.get_execution_profile_catalog(
                catalog_version=catalog.catalog_version,
                catalog_digest=catalog.catalog_digest,
            )
            == catalog
        )
        binding = repository.resolve_execution_profile(
            catalog_version=catalog.catalog_version,
            catalog_digest=catalog.catalog_digest,
            field_path="/params/rollback_profile",
            profile=definition.profile,
            expected_profile_kind="rollback",
        )
        resolved_definition, lifecycle = repository.resolve_execution_profile_binding(binding)
        rows = session.scalars(select(PolicySnapshotRow)).all()

    assert binding == ResolvedExecutionProfileBindingV1(
        field_path="/params/rollback_profile",
        profile=definition.profile,
        expected_profile_kind="rollback",
        profile_payload_hash=execution_profile_payload_hash(definition),
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
    )
    assert resolved_definition == definition
    assert lifecycle == catalog.lifecycle[0]
    assert {row.document_kind for row in rows} == {
        "execution_profile_catalog",
        "execution_profile_definition",
    }


def test_resolution_uses_the_bound_historical_catalog_not_a_current_alias(
    engine: Engine,
) -> None:
    definition = _definition()
    active = _catalog(7, definition)
    replay_only = _catalog(
        8,
        definition,
        state="replay_only",
        revision=2,
        changed_at="2026-07-14T12:00:00Z",
    )
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        repository.put_execution_profile_catalog(active)
        repository.put_execution_profile_catalog(replay_only)

    active_binding = ResolvedExecutionProfileBindingV1(
        field_path="/params/rollback_profile",
        profile=definition.profile,
        expected_profile_kind="rollback",
        profile_payload_hash=execution_profile_payload_hash(definition),
        catalog_version=active.catalog_version,
        catalog_digest=active.catalog_digest,
    )
    replay_binding = active_binding.model_copy(
        update={
            "catalog_version": replay_only.catalog_version,
            "catalog_digest": replay_only.catalog_digest,
        }
    )
    with Session(engine) as session:
        repository = _repository(session)
        assert repository.list_execution_profile_catalogs() == (active, replay_only)
        assert repository.resolve_execution_profile_binding(active_binding)[1].state == "active"
        assert (
            repository.resolve_execution_profile_binding(replay_binding)[1].state == "replay_only"
        )


def test_unchanged_lifecycle_is_copied_exactly_into_a_later_catalog(
    engine: Engine,
) -> None:
    definition = _definition()
    first = _catalog(7, definition)
    unchanged = _catalog(8, definition)
    with Session(engine) as session, session.begin():
        repository = _repository(session)
        repository.put_execution_profile_catalog(first)
        assert repository.put_execution_profile_catalog(unchanged) == unchanged


def test_unchanged_lifecycle_cannot_fabricate_a_revision_and_new_timestamp(
    engine: Engine,
) -> None:
    definition = _definition()
    first = _catalog(7, definition)
    fabricated = _catalog(
        8,
        definition,
        state="active",
        revision=2,
        changed_at="2026-07-14T12:00:00Z",
    )
    with Session(engine) as session:
        repository = _repository(session)
        repository.put_execution_profile_catalog(first)
        session.commit()

        with pytest.raises(IntegrityViolation, match="unchanged lifecycle"):
            repository.put_execution_profile_catalog(fabricated)
        session.rollback()
        assert (
            repository.get_execution_profile_catalog(
                catalog_version=fabricated.catalog_version,
                catalog_digest=fabricated.catalog_digest,
            )
            is None
        )


@pytest.mark.parametrize("revision", [1, 3])
def test_lifecycle_state_change_requires_exactly_one_revision_increment(
    engine: Engine,
    revision: int,
) -> None:
    definition = _definition()
    first = _catalog(7, definition)
    invalid = _catalog(
        8,
        definition,
        state="replay_only",
        revision=revision,
        changed_at="2026-07-14T12:00:00Z",
    )
    with Session(engine) as session:
        repository = _repository(session)
        repository.put_execution_profile_catalog(first)
        session.commit()
        with pytest.raises(IntegrityViolation, match="exactly one"):
            repository.put_execution_profile_catalog(invalid)


def test_lifecycle_revision_cannot_regress_after_a_valid_state_change(
    engine: Engine,
) -> None:
    definition = _definition()
    first = _catalog(7, definition)
    second = _catalog(
        8,
        definition,
        state="replay_only",
        revision=2,
        changed_at="2026-07-14T12:00:00Z",
    )
    regressed = _catalog(
        9,
        definition,
        state="disabled",
        revision=1,
        changed_at="2026-07-14T13:00:00Z",
    )
    with Session(engine) as session:
        repository = _repository(session)
        repository.put_execution_profile_catalog(first)
        repository.put_execution_profile_catalog(second)
        session.commit()
        with pytest.raises(IntegrityViolation, match="exactly one"):
            repository.put_execution_profile_catalog(regressed)


def test_lifecycle_state_change_must_refresh_changed_at(engine: Engine) -> None:
    definition = _definition()
    first = _catalog(7, definition)
    unchanged_timestamp = _catalog(
        8,
        definition,
        state="replay_only",
        revision=2,
        changed_at=first.lifecycle[0].changed_at,
    )
    with Session(engine) as session:
        repository = _repository(session)
        repository.put_execution_profile_catalog(first)
        session.commit()
        with pytest.raises(IntegrityViolation, match="refresh changed_at"):
            repository.put_execution_profile_catalog(unchanged_timestamp)


@pytest.mark.parametrize(
    "changed_at",
    [
        "2026-07-14T10:00:00Z",
        "2026-07-14T12:00:00+01:00",
        "not-a-timestamp",
    ],
)
def test_lifecycle_state_change_requires_strictly_later_utc_changed_at(
    engine: Engine,
    changed_at: str,
) -> None:
    definition = _definition()
    first = _catalog(7, definition)
    invalid = _catalog(
        8,
        definition,
        state="replay_only",
        revision=2,
        changed_at=changed_at,
    )
    with Session(engine) as session:
        repository = _repository(session)
        repository.put_execution_profile_catalog(first)
        session.commit()

        with pytest.raises(IntegrityViolation, match="changed_at"):
            repository.put_execution_profile_catalog(invalid)


def test_new_profile_lifecycle_starts_at_revision_one(engine: Engine) -> None:
    definition = _definition()
    invalid = _catalog(7, definition, revision=2)
    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="start at revision 1"):
            _repository(session).put_execution_profile_catalog(invalid)


def test_same_profile_ref_cannot_change_definition_across_catalogs(engine: Engine) -> None:
    original = _definition()
    changed = _definition(display_name="Changed rollback", history_limit=50)
    first = _catalog(7, original)
    conflicting = _catalog(8, changed)

    with Session(engine) as session:
        repository = _repository(session)
        repository.put_execution_profile_catalog(first)
        session.commit()
        with pytest.raises(IntegrityViolation, match="immutable"):
            repository.put_execution_profile_catalog(conflicting)
        session.rollback()

        assert (
            repository.get_execution_profile_catalog(
                catalog_version=first.catalog_version,
                catalog_digest=first.catalog_digest,
            )
            == first
        )
        assert (
            repository.get_execution_profile_catalog(
                catalog_version=conflicting.catalog_version,
                catalog_digest=conflicting.catalog_digest,
            )
            is None
        )


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"profile_payload_hash": "f" * 64}, "payload hash"),
        ({"expected_profile_kind": "checker"}, "profile kind"),
        (
            {"profile": ProfileRefV1(profile_id="rollback.missing", version=1)},
            "not a member",
        ),
        ({"field_path": "not-a-json-pointer"}, "binding is invalid"),
    ],
)
def test_existing_binding_must_match_every_exact_catalog_field(
    engine: Engine,
    update: dict[str, object],
    message: str,
) -> None:
    definition = _definition()
    catalog = _catalog(7, definition)
    with Session(engine) as session, session.begin():
        _repository(session).put_execution_profile_catalog(catalog)

    binding = ResolvedExecutionProfileBindingV1(
        field_path="/params/rollback_profile",
        profile=definition.profile,
        expected_profile_kind="rollback",
        profile_payload_hash=execution_profile_payload_hash(definition),
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
    ).model_copy(update=update)
    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match=message):
            _repository(session).resolve_execution_profile_binding(binding)


@pytest.mark.parametrize(
    "document_kind",
    ["execution_profile_catalog", "execution_profile_definition"],
)
def test_wrong_catalog_digest_and_corrupt_catalog_history_fail_closed(
    engine: Engine,
    document_kind: str,
) -> None:
    definition = _definition()
    catalog = _catalog(7, definition)
    with Session(engine) as session, session.begin():
        _repository(session).put_execution_profile_catalog(catalog)

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="digest"):
            _repository(session).get_execution_profile_catalog(
                catalog_version=catalog.catalog_version,
                catalog_digest="f" * 64,
            )

    with Session(engine) as session, session.begin():
        row = session.scalar(
            select(PolicySnapshotRow).where(PolicySnapshotRow.document_kind == document_kind)
        )
        assert row is not None
        row.payload_schema_version = "corrupt@1"

    with Session(engine) as session:
        with pytest.raises(IntegrityViolation, match="metadata"):
            _repository(session).get_execution_profile_catalog(
                catalog_version=catalog.catalog_version,
                catalog_digest=catalog.catalog_digest,
            )
