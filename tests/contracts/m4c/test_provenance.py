"""Contract tests for the source-governance vocabulary (design §7.F)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.contracts.provenance import (
    OriginRefV1,
    PromptPartV1,
    ProvenanceTransformationV1,
    ProvenanceV1,
    SourceKindDefinitionV1,
    SourceKindRegistryV1,
    most_conservative_trust,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _provenance(*, trust: str = "trusted_internal", parents: tuple[str, ...] = ()) -> ProvenanceV1:
    return ProvenanceV1(
        source_kind_registry_version=1,
        source_kind_id="authenticated_human_goal",
        origin_ref=OriginRefV1(opaque_source_id="principal:abc", source_revision=_HASH_A),
        parent_source_artifact_ids=parents,
        connector_id="authenticated-human-goal-connector@1",
        connector_version="1",
        trust=trust,  # type: ignore[arg-type]
        source_hash=_HASH_A,
    )


def test_source_kind_definition_sorts_and_dedups_trust_and_purpose() -> None:
    definition = SourceKindDefinitionV1(
        source_kind_id="tool_output",
        allowed_trust_levels=("untrusted_external", "trusted_internal", "reviewed_external"),
        allowed_prompt_purposes=("tool_output", "context"),
        description_code="tool.output",
    )
    assert definition.allowed_trust_levels == (
        "trusted_internal",
        "reviewed_external",
        "untrusted_external",
    )
    assert definition.allowed_prompt_purposes == ("context", "tool_output")


def test_source_kind_definition_rejects_untrusted_user_goal() -> None:
    with pytest.raises(ValidationError):
        SourceKindDefinitionV1(
            source_kind_id="rogue_goal",
            allowed_trust_levels=("trusted_internal", "reviewed_external"),
            allowed_prompt_purposes=("user_goal",),
            description_code="rogue",
        )


def test_registry_orders_definitions_and_rejects_duplicates() -> None:
    human = SourceKindDefinitionV1(
        source_kind_id="authenticated_human_goal",
        allowed_trust_levels=("trusted_internal",),
        allowed_prompt_purposes=("user_goal",),
        description_code="human.goal",
    )
    doc = SourceKindDefinitionV1(
        source_kind_id="planning_document",
        allowed_trust_levels=("reviewed_external",),
        allowed_prompt_purposes=("context",),
        description_code="planning.document",
    )
    registry = SourceKindRegistryV1(registry_version=1, definitions=(doc, human))
    assert [item.source_kind_id for item in registry.definitions] == [
        "authenticated_human_goal",
        "planning_document",
    ]
    assert registry.get("authenticated_human_goal") is human
    assert registry.get("missing") is None
    with pytest.raises(ValidationError):
        SourceKindRegistryV1(registry_version=1, definitions=(human, human))


def test_provenance_orders_parents_and_rejects_duplicates() -> None:
    provenance = _provenance(parents=("artifact:b", "artifact:a"))
    assert provenance.parent_source_artifact_ids == ("artifact:a", "artifact:b")
    with pytest.raises(ValidationError):
        _provenance(parents=("artifact:a", "artifact:a"))


def test_provenance_carries_transformations() -> None:
    provenance = ProvenanceV1(
        source_kind_registry_version=1,
        source_kind_id="tool_output",
        origin_ref=OriginRefV1(opaque_source_id="tool:x", source_revision=_HASH_A),
        connector_id="tool-connector@1",
        connector_version="1",
        trust="reviewed_external",
        source_hash=_HASH_B,
        transformations=(
            ProvenanceTransformationV1(
                tool_version="sanitizer@1", input_hash=_HASH_A, output_hash=_HASH_B
            ),
        ),
    )
    assert provenance.transformations[0].tool_version == "sanitizer@1"


def test_prompt_part_requires_trusted_internal_for_user_goal() -> None:
    with pytest.raises(ValidationError):
        PromptPartV1(
            text="do the thing",
            provenance=_provenance(trust="reviewed_external"),
            purpose="user_goal",
        )
    part = PromptPartV1(
        text="do the thing",
        provenance=_provenance(trust="trusted_internal"),
        purpose="user_goal",
    )
    assert part.purpose == "user_goal"


def test_prompt_part_allows_external_context() -> None:
    part = PromptPartV1(
        text="reference material",
        provenance=_provenance(trust="untrusted_external"),
        purpose="context",
    )
    assert part.provenance.trust == "untrusted_external"


def test_most_conservative_trust_picks_least_trusted() -> None:
    assert (
        most_conservative_trust(("trusted_internal", "untrusted_external", "reviewed_external"))
        == "untrusted_external"
    )
    assert most_conservative_trust(("trusted_internal",)) == "trusted_internal"
    with pytest.raises(ValueError):
        most_conservative_trust(())
