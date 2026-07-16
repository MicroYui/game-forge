"""Versioned built-in :class:`SourceKindRegistryV1` (design §7.F).

The registry is the authoritative catalogue of every source kind the platform may
assign. New games/connectors only *extend* this registry; the six built-in kinds
below are the minimum the design names. Trust and purpose bounds live in the frozen
contract type, so this module only supplies the exact, retained definitions.
"""

from __future__ import annotations

from gameforge.contracts.provenance import (
    SourceKindDefinitionV1,
    SourceKindRegistryV1,
)

BUILTIN_SOURCE_KIND_REGISTRY_VERSION = 1

# Server-assigned source-kind ids referenced by the goal-provenance policy.
AUTHENTICATED_HUMAN_GOAL = "authenticated_human_goal"
TRUSTED_SERVICE_GOAL = "trusted_service_goal"
PLANNING_DOCUMENT = "planning_document"
OPEN_SOURCE_CONTENT = "open_source_content"
TOOL_OUTPUT = "tool_output"
RETRIEVAL_RESULT = "retrieval_result"
TRUSTED_PROMPT_TEMPLATE = "trusted_prompt_template"


_BUILTIN_DEFINITIONS: tuple[SourceKindDefinitionV1, ...] = (
    SourceKindDefinitionV1(
        source_kind_id=AUTHENTICATED_HUMAN_GOAL,
        allowed_trust_levels=("trusted_internal",),
        allowed_prompt_purposes=("user_goal",),
        description_code="source.authenticated_human_goal",
    ),
    SourceKindDefinitionV1(
        source_kind_id=TRUSTED_SERVICE_GOAL,
        allowed_trust_levels=("trusted_internal",),
        allowed_prompt_purposes=("user_goal",),
        description_code="source.trusted_service_goal",
    ),
    SourceKindDefinitionV1(
        source_kind_id=PLANNING_DOCUMENT,
        allowed_trust_levels=("reviewed_external",),
        allowed_prompt_purposes=("context",),
        description_code="source.planning_document",
    ),
    SourceKindDefinitionV1(
        source_kind_id=OPEN_SOURCE_CONTENT,
        allowed_trust_levels=("untrusted_external",),
        allowed_prompt_purposes=("context",),
        description_code="source.open_source_content",
    ),
    SourceKindDefinitionV1(
        source_kind_id=TOOL_OUTPUT,
        allowed_trust_levels=("trusted_internal", "reviewed_external", "untrusted_external"),
        allowed_prompt_purposes=("context", "tool_output"),
        description_code="source.tool_output",
    ),
    SourceKindDefinitionV1(
        source_kind_id=RETRIEVAL_RESULT,
        allowed_trust_levels=("reviewed_external", "untrusted_external"),
        allowed_prompt_purposes=("context", "tool_output"),
        description_code="source.retrieval_result",
    ),
    SourceKindDefinitionV1(
        source_kind_id=TRUSTED_PROMPT_TEMPLATE,
        allowed_trust_levels=("trusted_internal",),
        allowed_prompt_purposes=("instruction",),
        description_code="source.trusted_prompt_template",
    ),
)


def build_source_kind_registry() -> SourceKindRegistryV1:
    """Return the immutable built-in source-kind registry."""

    return SourceKindRegistryV1(
        registry_version=BUILTIN_SOURCE_KIND_REGISTRY_VERSION,
        definitions=_BUILTIN_DEFINITIONS,
    )


__all__ = [
    "AUTHENTICATED_HUMAN_GOAL",
    "BUILTIN_SOURCE_KIND_REGISTRY_VERSION",
    "OPEN_SOURCE_CONTENT",
    "PLANNING_DOCUMENT",
    "RETRIEVAL_RESULT",
    "TOOL_OUTPUT",
    "TRUSTED_PROMPT_TEMPLATE",
    "TRUSTED_SERVICE_GOAL",
    "build_source_kind_registry",
]
