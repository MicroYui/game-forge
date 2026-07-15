"""Trusted authenticated-goal provenance composition (design §7.F, §5.3).

``apps.api`` accepts naked user goal text on ``generation:propose`` /
``constraints:propose``. Before any Run is created the composition root must turn
that text into an immutable ``source_raw`` Artifact whose :class:`ProvenanceV1` is
SERVER-ASSIGNED from the authenticated actor — never from a client field. This
module owns that assignment (the connector/trust policy) and the blob-first writer.

* A human session may only be assigned ``authenticated_human_goal``; a trusted
  service (or the internal composition root) ``trusted_service_goal``. Both are
  ``trusted_internal`` / ``user_goal`` and carry no source parents.
* ``trust``, ``purpose``, ``origin_ref`` and the connector identity come from the
  authenticated :class:`ActorContext`, the :class:`SourceKindRegistryV1`, and the
  versioned provenance policy. External sources (planning docs, open-source content,
  retrieval, tool output) can only ever become ``context``/``tool_output`` and are
  never upgraded to a trusted goal through a client field.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.identity import ActorContext
from gameforge.contracts.lineage import ArtifactV2, VersionTuple, build_artifact_v2
from gameforge.contracts.provenance import (
    OriginRefV1,
    PromptPurpose,
    ProvenanceV1,
    SourceKindRegistryV1,
    TrustLevel,
)
from gameforge.contracts.storage import ObjectStore, StoredObject
from gameforge.platform.provenance.registry import (
    AUTHENTICATED_HUMAN_GOAL,
    TRUSTED_SERVICE_GOAL,
)

_HUMAN_GOAL_CONNECTOR = "authenticated-human-goal-connector@1"
_SERVICE_GOAL_CONNECTOR = "trusted-service-goal-connector@1"
_CONNECTOR_VERSION = "1"
_GOAL_PURPOSE: PromptPurpose = "user_goal"
_GOAL_TRUST: TrustLevel = "trusted_internal"


@dataclass(frozen=True, slots=True)
class _GoalConnector:
    source_kind_id: str
    connector_id: str


# The authenticated actor's principal kind selects the connector; the connector's
# configuration (not any payload field) fixes the source kind and trust.
_CONNECTORS: dict[str, _GoalConnector] = {
    "human": _GoalConnector(AUTHENTICATED_HUMAN_GOAL, _HUMAN_GOAL_CONNECTOR),
    "service": _GoalConnector(TRUSTED_SERVICE_GOAL, _SERVICE_GOAL_CONNECTOR),
    "system": _GoalConnector(TRUSTED_SERVICE_GOAL, _SERVICE_GOAL_CONNECTOR),
}


@dataclass(frozen=True, slots=True)
class MintedSource:
    """A newly minted immutable ``source_raw`` Artifact and its verified blob."""

    artifact: ArtifactV2
    stored: StoredObject
    provenance: ProvenanceV1


class GoalProvenancePolicy:
    """Server-assign a goal :class:`ProvenanceV1` from the authenticated actor."""

    def __init__(self, *, registry: SourceKindRegistryV1) -> None:
        self._registry = registry

    @property
    def registry(self) -> SourceKindRegistryV1:
        return self._registry

    def assign(self, *, actor: ActorContext, source_hash: str) -> ProvenanceV1:
        connector = _CONNECTORS.get(actor.principal.kind)
        if connector is None:
            raise IntegrityViolation("no trusted goal connector for the authenticated actor")
        definition = self._registry.get(connector.source_kind_id)
        if definition is None:
            raise IntegrityViolation("goal source kind is not retained in the exact registry")
        if _GOAL_TRUST not in definition.allowed_trust_levels:
            raise IntegrityViolation("goal source kind forbids the assigned trust level")
        if _GOAL_PURPOSE not in definition.allowed_prompt_purposes:
            raise IntegrityViolation("goal source kind forbids the user_goal purpose")
        opaque_source_id = (
            "sha256:"
            + sha256(
                f"{connector.connector_id}\x00{actor.principal.id}".encode("utf-8")
            ).hexdigest()
        )
        return ProvenanceV1(
            source_kind_registry_version=self._registry.registry_version,
            source_kind_id=connector.source_kind_id,
            # No upstream revision for an authored goal: use the content hash. The
            # origin_ref is assigned here, before the Artifact id exists.
            origin_ref=OriginRefV1(
                opaque_source_id=opaque_source_id,
                source_revision=source_hash,
            ),
            parent_source_artifact_ids=(),
            connector_id=connector.connector_id,
            connector_version=_CONNECTOR_VERSION,
            trust=_GOAL_TRUST,
            source_hash=source_hash,
        )


class AuthenticatedGoalSourceWriter:
    """Blob-first writer that mints the immutable ``source_raw`` goal Artifact."""

    def __init__(self, *, policy: GoalProvenancePolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> GoalProvenancePolicy:
        return self._policy

    def mint(
        self,
        *,
        object_store: ObjectStore,
        actor: ActorContext,
        text: str,
        created_at: str,
    ) -> MintedSource:
        if not isinstance(text, str) or not text:
            raise IntegrityViolation("authenticated goal text must be a non-empty string")
        # Blob first: verified bytes become a GC-eligible orphan if Run creation later
        # fails, but the naked text never enters the Run payload or telemetry.
        stored = object_store.put_verified(text.encode("utf-8"))
        provenance = self._policy.assign(actor=actor, source_hash=stored.ref.sha256)
        artifact = build_artifact_v2(
            kind="source_raw",
            version_tuple=VersionTuple(tool_version=provenance.connector_id),
            lineage=(),
            payload_hash=stored.ref.sha256,
            object_ref=stored.ref,
            meta={"provenance": provenance.model_dump(mode="json")},
            created_at=created_at,
        )
        return MintedSource(artifact=artifact, stored=stored, provenance=provenance)


__all__ = [
    "AuthenticatedGoalSourceWriter",
    "GoalProvenancePolicy",
    "MintedSource",
]
