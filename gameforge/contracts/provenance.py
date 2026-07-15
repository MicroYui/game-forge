"""Source-governance pure-data vocabulary (design §7.F).

These types are the *only* place trust, purpose, source-kind, origin, and
provenance are described. They are pure Pydantic data with no business imports so
that ``contracts`` stays a dependency leaf: the ``platform.provenance`` package
owns the versioned :class:`SourceKindRegistry` and the connector/trust policy that
SERVER-ASSIGNS these values from an authenticated actor, while ``apps.api`` mints
the immutable ``source_raw`` Artifact that carries a :class:`ProvenanceV1`.

Key invariants encoded here (not merely documented):

* ``instruction`` / ``user_goal`` prompt purposes are only ever paired with
  ``trusted_internal`` trust. Reviewed/untrusted external sources can only be
  ``context`` / ``tool_output``. A payload may never self-report trust or purpose;
  those are always taken from a :class:`ProvenanceV1` assigned by a trusted
  composition root.
* A :class:`ProvenanceV1` never stores the id of the Artifact that carries it; its
  ``origin_ref`` is assigned by the connector before the content-addressed Artifact
  id exists, structurally removing the self-reference. ``parent_source_artifact_ids``
  is stable-unique and, for a derived source, equals the source parents recorded in
  the carrying Artifact's lineage. Authenticated / externally-ingested goal
  ``source_raw`` has no parents.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

TrustLevel = Literal["trusted_internal", "reviewed_external", "untrusted_external"]
PromptPurpose = Literal["instruction", "context", "tool_output", "user_goal"]

# Purposes that may only be assigned to trusted-internal sources.
TRUSTED_ONLY_PURPOSES: frozenset[str] = frozenset({"instruction", "user_goal"})

_TRUST_ORDER: dict[str, int] = {
    "trusted_internal": 0,
    "reviewed_external": 1,
    "untrusted_external": 2,
}
_PURPOSE_ORDER: dict[str, int] = {
    "instruction": 0,
    "context": 1,
    "tool_output": 2,
    "user_goal": 3,
}

BoundedId = Annotated[str, StringConstraints(min_length=1, max_length=512)]
BoundedCode = Annotated[str, StringConstraints(min_length=1, max_length=512)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
# Prompt-rendered / raw source text is bounded but may be large; the object bytes
# remain the authority, this is the in-band prompt-part carrier for the renderer.
BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=1_048_576)]
BoundedToolVersion = Annotated[str, StringConstraints(min_length=1, max_length=512)]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class SourceKindDefinitionV1(_FrozenModel):
    """One versioned, opaque source-kind and the trust/purpose it may carry."""

    definition_schema_version: Literal["source-kind-definition@1"] = "source-kind-definition@1"
    source_kind_id: BoundedId
    allowed_trust_levels: tuple[TrustLevel, ...] = Field(min_length=1)
    allowed_prompt_purposes: tuple[PromptPurpose, ...] = Field(min_length=1)
    description_code: BoundedCode

    @model_validator(mode="after")
    def _canonical(self) -> "SourceKindDefinitionV1":
        trust = tuple(dict.fromkeys(self.allowed_trust_levels))
        purposes = tuple(dict.fromkeys(self.allowed_prompt_purposes))
        if len(trust) != len(self.allowed_trust_levels):
            raise ValueError("allowed_trust_levels must be unique")
        if len(purposes) != len(self.allowed_prompt_purposes):
            raise ValueError("allowed_prompt_purposes must be unique")
        object.__setattr__(
            self,
            "allowed_trust_levels",
            tuple(sorted(trust, key=_TRUST_ORDER.__getitem__)),
        )
        object.__setattr__(
            self,
            "allowed_prompt_purposes",
            tuple(sorted(purposes, key=_PURPOSE_ORDER.__getitem__)),
        )
        # instruction / user_goal are trusted-internal only; a definition that
        # permits either purpose must not permit any non-trusted trust level.
        if TRUSTED_ONLY_PURPOSES.intersection(self.allowed_prompt_purposes):
            if set(self.allowed_trust_levels) - {"trusted_internal"}:
                raise ValueError(
                    "instruction/user_goal source kinds allow only trusted_internal trust"
                )
        return self


class SourceKindRegistryV1(_FrozenModel):
    """Versioned registry of every retained :class:`SourceKindDefinitionV1`."""

    registry_schema_version: Literal["source-kind-registry@1"] = "source-kind-registry@1"
    registry_version: int = Field(ge=1)
    definitions: tuple[SourceKindDefinitionV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_sorted(self) -> "SourceKindRegistryV1":
        ids = [item.source_kind_id for item in self.definitions]
        if len(ids) != len(set(ids)):
            raise ValueError("source kind ids must be unique within a registry")
        object.__setattr__(
            self,
            "definitions",
            tuple(sorted(self.definitions, key=lambda item: item.source_kind_id)),
        )
        return self

    def get(self, source_kind_id: str) -> SourceKindDefinitionV1 | None:
        for definition in self.definitions:
            if definition.source_kind_id == source_kind_id:
                return definition
        return None


class OriginRefV1(_FrozenModel):
    """Connector-scoped stable pointer to an upstream source, never a secret/URI."""

    origin_schema_version: Literal["origin-ref@1"] = "origin-ref@1"
    opaque_source_id: BoundedId
    # Stable within ``connector_id`` scope; when there is no upstream revision the
    # connector uses the content hash.
    source_revision: BoundedId


class ProvenanceTransformationV1(_FrozenModel):
    """One deterministic transformation edge applied while deriving a source."""

    transformation_schema_version: Literal["provenance-transformation@1"] = (
        "provenance-transformation@1"
    )
    tool_version: BoundedToolVersion
    input_hash: Sha256Hex
    output_hash: Sha256Hex


class ProvenanceV1(_FrozenModel):
    """Trust-carrying provenance stamped onto a ``source_raw``/``source_rendered``.

    It never records the id of the Artifact that carries it (the ``origin_ref`` is
    assigned before that id exists). Multi-source derivations take the most
    conservative trust of their parents; the connector's authenticated configuration
    is the only source of ``trust`` — a payload self-reported trust is never read.
    """

    provenance_schema_version: Literal["provenance@1"] = "provenance@1"
    source_kind_registry_version: int = Field(ge=1)
    source_kind_id: BoundedId
    origin_ref: OriginRefV1
    parent_source_artifact_ids: tuple[BoundedId, ...] = Field(default=(), max_length=4096)
    connector_id: BoundedId
    connector_version: BoundedId
    trust: TrustLevel
    source_hash: Sha256Hex
    transformations: tuple[ProvenanceTransformationV1, ...] = Field(default=(), max_length=4096)

    @model_validator(mode="after")
    def _canonical_parents(self) -> "ProvenanceV1":
        parents = self.parent_source_artifact_ids
        if len(parents) != len(set(parents)):
            raise ValueError("parent_source_artifact_ids must be unique")
        object.__setattr__(self, "parent_source_artifact_ids", tuple(sorted(parents)))
        return self


class PromptPartV1(_FrozenModel):
    """One structurally-bounded prompt segment; trust is only from its provenance."""

    prompt_part_schema_version: Literal["prompt-part@1"] = "prompt-part@1"
    text: BoundedText
    provenance: ProvenanceV1
    purpose: PromptPurpose

    @model_validator(mode="after")
    def _purpose_trust(self) -> "PromptPartV1":
        if self.purpose in TRUSTED_ONLY_PURPOSES and self.provenance.trust != "trusted_internal":
            raise ValueError(
                "instruction/user_goal prompt parts require trusted_internal provenance"
            )
        return self


def most_conservative_trust(values: tuple[TrustLevel, ...]) -> TrustLevel:
    """Return the least-trusted level across ``values`` (design §7.F multi-source)."""

    if not values:
        raise ValueError("trust reduction requires at least one trust level")
    return max(values, key=_TRUST_ORDER.__getitem__)


__all__ = [
    "BoundedText",
    "OriginRefV1",
    "PromptPartV1",
    "PromptPurpose",
    "ProvenanceTransformationV1",
    "ProvenanceV1",
    "SourceKindDefinitionV1",
    "SourceKindRegistryV1",
    "TRUSTED_ONLY_PURPOSES",
    "TrustLevel",
    "most_conservative_trust",
]
