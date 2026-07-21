"""Source-governance registry, trust policy, and source_raw writer (design §7.F)."""

from __future__ import annotations

from pathlib import Path

import pytest

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.identity import (
    ActorContext,
    AuthenticationContext,
    DomainScope,
    Principal,
)
from gameforge.contracts.provenance import ProvenanceV1
from gameforge.platform.lineage.validation import (
    ProducerValidationContext,
    validate_artifact_producer,
)
from gameforge.platform.publication import TerminalPublisher
from gameforge.platform.provenance import (
    AUTHENTICATED_HUMAN_GOAL,
    TRUSTED_SERVICE_GOAL,
    AuthenticatedGoalSourceWriter,
    GoalProvenancePolicy,
    build_source_kind_registry,
)
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.runtime.object_store import LocalObjectStore
from datetime import datetime, timezone

_NOW = "2026-07-15T00:00:00Z"
_SCOPE = DomainScope(domain_ids=("builtin",))


def _principal(kind: str) -> Principal:
    return Principal(
        id=f"{kind}:actor",
        kind=kind,  # type: ignore[arg-type]
        display_name=kind,
        status="active",
        revision=1,
        credential_epoch=1,
        authz_revision=1,
        roles=(),
    )


def _actor(kind: str) -> ActorContext:
    mechanism = {"human": "session", "service": "api_key", "system": "trusted_internal"}[kind]
    return ActorContext(
        principal=_principal(kind),
        authentication=AuthenticationContext(
            mechanism=mechanism,  # type: ignore[arg-type]
            credential_id=None if kind == "system" else f"credential:{kind}",
        ),
        session_id=f"session:{kind}" if kind == "human" else None,
        request_id=f"request:{kind}",
    )


def _object_store(tmp_path: Path) -> LocalObjectStore:
    return LocalObjectStore(
        tmp_path / "objects",
        store_id="local",
        clock=FrozenUtcClock(datetime(2026, 7, 15, tzinfo=timezone.utc)),
        cursor_signing_key=b"provenance-test-cursor-key",
    )


def test_builtin_registry_has_named_source_kinds() -> None:
    registry = build_source_kind_registry()
    ids = {item.source_kind_id for item in registry.definitions}
    assert {
        "authenticated_human_goal",
        "trusted_service_goal",
        "planning_document",
        "open_source_content",
        "tool_output",
        "retrieval_result",
    } <= ids
    human = registry.get("authenticated_human_goal")
    assert human is not None
    assert human.allowed_trust_levels == ("trusted_internal",)
    assert human.allowed_prompt_purposes == ("user_goal",)


def test_policy_assigns_human_goal_from_session_actor() -> None:
    policy = GoalProvenancePolicy(registry=build_source_kind_registry())
    provenance = policy.assign(actor=_actor("human"), source_hash="a" * 64)
    assert provenance.source_kind_id == AUTHENTICATED_HUMAN_GOAL
    assert provenance.trust == "trusted_internal"
    assert provenance.parent_source_artifact_ids == ()
    assert provenance.origin_ref.source_revision == "a" * 64
    assert provenance.connector_id == "authenticated-human-goal-connector@1"
    # opaque_source_id is derived, never the raw principal id.
    assert "human:actor" not in provenance.origin_ref.opaque_source_id


def test_policy_assigns_service_goal_from_api_key_actor() -> None:
    policy = GoalProvenancePolicy(registry=build_source_kind_registry())
    provenance = policy.assign(actor=_actor("service"), source_hash="b" * 64)
    assert provenance.source_kind_id == TRUSTED_SERVICE_GOAL
    assert provenance.trust == "trusted_internal"


def test_writer_mints_immutable_source_raw_with_provenance(tmp_path: Path) -> None:
    writer = AuthenticatedGoalSourceWriter(
        policy=GoalProvenancePolicy(registry=build_source_kind_registry())
    )
    objects = _object_store(tmp_path)
    minted = writer.mint(
        object_store=objects,
        actor=_actor("human"),
        text="Reduce boss gold reward to a sustainable value.",
        domain_scope=_SCOPE,
        created_at=_NOW,
    )
    assert minted.artifact.kind == "source_raw"
    assert minted.artifact.lineage == ()
    assert minted.artifact.payload_hash == minted.stored.ref.sha256
    assert minted.artifact.version_tuple.doc_version == minted.stored.ref.sha256
    assert minted.artifact.version_tuple.tool_version is None
    assert minted.artifact.meta["payload_schema_id"] == "source-raw@1"
    assert minted.artifact.meta["domain_scope"] == _SCOPE.model_dump(mode="json")
    # Provenance folds into the content-addressed artifact id (immutable/hash-bound).
    reparsed = ProvenanceV1.model_validate(minted.artifact.meta["provenance"])
    assert reparsed == minted.provenance
    assert (
        validate_artifact_producer(
            minted.artifact,
            ProducerValidationContext(),
        ).status
        == "valid"
    )
    # The naked text is not present in the artifact record (only its hash/ref).
    assert "Reduce boss gold" not in minted.artifact.model_dump_json()


def test_writer_projects_the_exact_source_artifact_without_writing_a_blob(tmp_path: Path) -> None:
    writer = AuthenticatedGoalSourceWriter(
        policy=GoalProvenancePolicy(registry=build_source_kind_registry())
    )
    text = "Resolve this exact goal without publishing it during option lookup."

    projected = writer.project(
        actor=_actor("human"),
        text=text,
        domain_scope=_SCOPE,
        created_at=_NOW,
    )
    minted = writer.mint(
        object_store=_object_store(tmp_path),
        actor=_actor("human"),
        text=text,
        domain_scope=_SCOPE,
        created_at=_NOW,
    )

    assert projected.artifact == minted.artifact
    assert projected.provenance == minted.provenance


def test_authenticated_source_is_resolvable_as_a_terminal_lineage_parent(
    tmp_path: Path,
) -> None:
    writer = AuthenticatedGoalSourceWriter(
        policy=GoalProvenancePolicy(registry=build_source_kind_registry())
    )
    minted = writer.mint(
        object_store=_object_store(tmp_path),
        actor=_actor("human"),
        text="Keep the authored goal bound to its exact source revision.",
        domain_scope=_SCOPE,
        created_at=_NOW,
    )

    class _Artifacts:
        def get(self, artifact_id: str):
            return minted.artifact if artifact_id == minted.artifact.artifact_id else None

    publisher = TerminalPublisher(
        registry=object(),  # type: ignore[arg-type]
        artifacts=_Artifacts(),  # type: ignore[arg-type]
        blobs=object(),  # type: ignore[arg-type]
        findings=object(),  # type: ignore[arg-type]
        ledger=object(),  # type: ignore[arg-type]
        audit=object(),  # type: ignore[arg-type]
    )
    parent = publisher._parent_info(minted.artifact.artifact_id)
    assert parent.kind == "source_raw"
    assert parent.payload_schema_id == "source-raw@1"
    assert parent.version_tuple.doc_version == minted.stored.ref.sha256


def test_writer_rejects_empty_goal_text(tmp_path: Path) -> None:
    writer = AuthenticatedGoalSourceWriter(
        policy=GoalProvenancePolicy(registry=build_source_kind_registry())
    )
    with pytest.raises(IntegrityViolation):
        writer.mint(
            object_store=_object_store(tmp_path),
            actor=_actor("human"),
            text="",
            domain_scope=_SCOPE,
            created_at=_NOW,
        )
