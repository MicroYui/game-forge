"""Versioned IR-to-domain ownership authority for deterministic auto-apply.

``AutoApplyPolicyV1`` already freezes one exact ``DomainRegistryV1``.  The
reserved tags below make that same historical registry the complete ownership
authority used to derive an affected domain scope from a canonical IR diff.
They are deliberately strict: an unknown reserved tag is configuration drift,
not an extension point that an older worker may guess how to interpret.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.identity import (
    DomainRegistryRefV1,
    DomainRegistryV1,
    LowerHexSha256,
    NonEmptyStr,
)
from gameforge.contracts.ir import EdgeType, NodeType


AUTO_APPLY_OWNERSHIP_TAG_PREFIX = "auto-apply:"
AUTO_APPLY_RESERVED_TAG_PREFIX = "auto-apply"
AUTO_APPLY_IR_ALL_TAG_V1 = "auto-apply:ir-all@1"
AUTO_APPLY_ENTITY_TYPE_TAG_PREFIX_V1 = "auto-apply:entity-type:"
AUTO_APPLY_RELATION_TYPE_TAG_PREFIX_V1 = "auto-apply:relation-type:"
AUTO_APPLY_OWNERSHIP_TAG_SUFFIX_V1 = "@1"
AUTO_APPLY_IR_OWNERSHIP_SCHEMA_ID = "auto-apply-ir-ownership@1"
AUTO_APPLY_IR_CLASSIFIER_SCHEMA_ID = "ir-core-auto-apply-classifier@1"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _stable_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


class AutoApplyIrTypeOwnershipV1(_FrozenModel):
    resource_kind: Literal["entity", "relation"]
    resource_type: NonEmptyStr
    domain_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @field_validator("domain_ids")
    @classmethod
    def _canonical_domains(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_unique(value)

    @model_validator(mode="after")
    def _known_resource_type(self) -> AutoApplyIrTypeOwnershipV1:
        allowed = (
            {item.value for item in NodeType}
            if self.resource_kind == "entity"
            else {item.value for item in EdgeType}
        )
        if self.resource_type not in allowed:
            raise ValueError("auto-apply ownership names an unknown IR resource type")
        return self


class AutoApplyIrOwnershipV1(_FrozenModel):
    ownership_schema_version: Literal["auto-apply-ir-ownership@1"] = (
        AUTO_APPLY_IR_OWNERSHIP_SCHEMA_ID
    )
    domain_registry: DomainRegistryRefV1
    implicit_single_domain: bool = False
    ir_all_domain_ids: tuple[NonEmptyStr, ...] = ()
    type_ownership: tuple[AutoApplyIrTypeOwnershipV1, ...] = ()

    @field_validator("ir_all_domain_ids")
    @classmethod
    def _canonical_all_domains(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_unique(value)

    @field_validator("type_ownership")
    @classmethod
    def _canonical_type_ownership(
        cls, value: tuple[AutoApplyIrTypeOwnershipV1, ...]
    ) -> tuple[AutoApplyIrTypeOwnershipV1, ...]:
        keys = [(item.resource_kind, item.resource_type) for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("auto-apply IR ownership repeats a resource type")
        rank = {"entity": 0, "relation": 1}
        return tuple(
            sorted(
                value,
                key=lambda item: (rank[item.resource_kind], item.resource_type),
            )
        )

    @model_validator(mode="after")
    def _implicit_shape(self) -> AutoApplyIrOwnershipV1:
        if self.implicit_single_domain and (
            len(self.ir_all_domain_ids) != 1 or self.type_ownership
        ):
            raise ValueError("implicit single-domain ownership must be one wildcard owner")
        return self

    def owners_for(self, resource_kind: str, resource_type: str) -> tuple[str, ...]:
        explicit = next(
            (
                item.domain_ids
                for item in self.type_ownership
                if item.resource_kind == resource_kind and item.resource_type == resource_type
            ),
            (),
        )
        return _stable_unique((*self.ir_all_domain_ids, *explicit))

    @property
    def complete(self) -> bool:
        if self.ir_all_domain_ids:
            return True
        identities = {(item.resource_kind, item.resource_type) for item in self.type_ownership}
        expected = {
            *(("entity", item.value) for item in NodeType),
            *(("relation", item.value) for item in EdgeType),
        }
        return identities == expected


_CLASSIFIER_CONTRACT_V1 = {
    "classifier_schema_version": AUTO_APPLY_IR_CLASSIFIER_SCHEMA_ID,
    "target_payload_schema_id": "ir-core@1",
    "ownership_projection": AUTO_APPLY_IR_OWNERSHIP_SCHEMA_ID,
    "ownership_tags": {
        "ir_all": AUTO_APPLY_IR_ALL_TAG_V1,
        "entity_type": "auto-apply:entity-type:<NodeType.value>@1",
        "relation_type": "auto-apply:relation-type:<EdgeType.value>@1",
    },
    "structural_paths": [
        "/entities/*/type",
        "/entities/*/schema_version",
        "/entities/*/source_ref",
        "/relations/*/type",
        "/relations/*/src_id",
        "/relations/*/dst_id",
        "/relations/*/schema_version",
        "/relations/*/source_ref",
    ],
    "semantic_value_paths": [
        "/entities/*/attrs/**",
        "/entities/*/tags/**",
        "/relations/*/attrs/**",
    ],
    "semantic_scalar_classes": {
        "integer_or_float": "numeric",
        "string": "narrative",
        "boolean_or_null": "classification_incomplete",
    },
    "unknown_path_value_or_owner": "classification_incomplete",
}


class AutoApplyIrClassifierBindingV1(_FrozenModel):
    classifier_schema_id: Literal["ir-core-auto-apply-classifier@1"] = (
        AUTO_APPLY_IR_CLASSIFIER_SCHEMA_ID
    )
    classifier_schema_digest: LowerHexSha256
    ownership: AutoApplyIrOwnershipV1

    @model_validator(mode="after")
    def _digest(self) -> AutoApplyIrClassifierBindingV1:
        expected = compute_auto_apply_ir_classifier_digest(self.ownership)
        if self.classifier_schema_digest != expected:
            raise ValueError("auto-apply classifier digest differs from ownership authority")
        return self


def compute_auto_apply_ir_classifier_digest(
    ownership: AutoApplyIrOwnershipV1,
) -> str:
    return canonical_sha256(
        {
            "classifier_contract": _CLASSIFIER_CONTRACT_V1,
            "ownership": ownership.model_dump(mode="json"),
        }
    )


def _parse_type_tag(
    tag: str,
    *,
    prefix: str,
    allowed: frozenset[str],
) -> str | None:
    if not tag.startswith(prefix):
        return None
    if not tag.endswith(AUTO_APPLY_OWNERSHIP_TAG_SUFFIX_V1):
        raise ValueError("auto-apply ownership tag has an unsupported version")
    value = tag[len(prefix) : -len(AUTO_APPLY_OWNERSHIP_TAG_SUFFIX_V1)]
    if not value or value not in allowed:
        raise ValueError("auto-apply ownership tag names an unknown IR resource type")
    return value


def resolve_auto_apply_ir_ownership(
    registry: DomainRegistryV1,
) -> AutoApplyIrOwnershipV1:
    """Resolve the canonical ownership map selected by one exact registry."""

    registry_ref = DomainRegistryRefV1(
        registry_version=registry.registry_version,
        registry_digest=registry.registry_digest,
    )
    active_ids = tuple(item.domain_id for item in registry.definitions if item.status == "active")
    reserved = tuple(
        (definition, tag)
        for definition in registry.definitions
        for tag in definition.tags
        if tag.startswith(AUTO_APPLY_RESERVED_TAG_PREFIX)
    )
    if not reserved:
        return AutoApplyIrOwnershipV1(
            domain_registry=registry_ref,
            implicit_single_domain=len(active_ids) == 1,
            ir_all_domain_ids=active_ids if len(active_ids) == 1 else (),
        )

    all_owners: set[str] = set()
    type_owners: dict[tuple[str, str], set[str]] = {}
    entity_types = frozenset(item.value for item in NodeType)
    relation_types = frozenset(item.value for item in EdgeType)
    for definition, tag in reserved:
        if definition.status != "active":
            raise ValueError("deprecated domains cannot own auto-apply IR resources")
        if tag == AUTO_APPLY_IR_ALL_TAG_V1:
            all_owners.add(definition.domain_id)
            continue
        entity_type = _parse_type_tag(
            tag,
            prefix=AUTO_APPLY_ENTITY_TYPE_TAG_PREFIX_V1,
            allowed=entity_types,
        )
        if entity_type is not None:
            type_owners.setdefault(("entity", entity_type), set()).add(definition.domain_id)
            continue
        relation_type = _parse_type_tag(
            tag,
            prefix=AUTO_APPLY_RELATION_TYPE_TAG_PREFIX_V1,
            allowed=relation_types,
        )
        if relation_type is not None:
            type_owners.setdefault(("relation", relation_type), set()).add(definition.domain_id)
            continue
        raise ValueError("unknown reserved auto-apply ownership tag")

    return AutoApplyIrOwnershipV1(
        domain_registry=registry_ref,
        ir_all_domain_ids=tuple(all_owners),
        type_ownership=tuple(
            AutoApplyIrTypeOwnershipV1(
                resource_kind=kind,  # type: ignore[arg-type]
                resource_type=resource_type,
                domain_ids=tuple(domain_ids),
            )
            for (kind, resource_type), domain_ids in type_owners.items()
        ),
    )


def auto_apply_ir_classifier_binding(
    registry: DomainRegistryV1,
) -> AutoApplyIrClassifierBindingV1:
    ownership = resolve_auto_apply_ir_ownership(registry)
    return AutoApplyIrClassifierBindingV1(
        classifier_schema_digest=compute_auto_apply_ir_classifier_digest(ownership),
        ownership=ownership,
    )


__all__ = [
    "AUTO_APPLY_ENTITY_TYPE_TAG_PREFIX_V1",
    "AUTO_APPLY_IR_ALL_TAG_V1",
    "AUTO_APPLY_IR_CLASSIFIER_SCHEMA_ID",
    "AUTO_APPLY_IR_OWNERSHIP_SCHEMA_ID",
    "AUTO_APPLY_OWNERSHIP_TAG_PREFIX",
    "AUTO_APPLY_OWNERSHIP_TAG_SUFFIX_V1",
    "AUTO_APPLY_RESERVED_TAG_PREFIX",
    "AUTO_APPLY_RELATION_TYPE_TAG_PREFIX_V1",
    "AutoApplyIrClassifierBindingV1",
    "AutoApplyIrOwnershipV1",
    "AutoApplyIrTypeOwnershipV1",
    "auto_apply_ir_classifier_binding",
    "compute_auto_apply_ir_classifier_digest",
    "resolve_auto_apply_ir_ownership",
]
