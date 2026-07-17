from __future__ import annotations

import pytest

from gameforge.contracts.auto_apply_ownership import (
    AUTO_APPLY_IR_ALL_TAG_V1,
    AUTO_APPLY_IR_CLASSIFIER_SCHEMA_ID,
    auto_apply_ir_classifier_binding,
    resolve_auto_apply_ir_ownership,
)
from gameforge.contracts.identity import (
    DomainDefinitionV1,
    DomainRegistryV1,
    compute_domain_registry_digest,
)


def _registry(*definitions: DomainDefinitionV1, version: str = "domains@1") -> DomainRegistryV1:
    return DomainRegistryV1(
        registry_version=version,
        definitions=definitions,
        registry_digest=compute_domain_registry_digest(version, definitions),
    )


def _domain(
    domain_id: str,
    *,
    tags: tuple[str, ...] = (),
    status: str = "active",
    display_name: str | None = None,
) -> DomainDefinitionV1:
    return DomainDefinitionV1(
        domain_id=domain_id,
        display_name=display_name or domain_id,
        tags=tags,
        status=status,  # type: ignore[arg-type]
    )


def test_one_active_domain_without_reserved_tags_is_exact_implicit_all() -> None:
    binding = auto_apply_ir_classifier_binding(_registry(_domain("economy")))

    assert binding.classifier_schema_id == AUTO_APPLY_IR_CLASSIFIER_SCHEMA_ID
    assert binding.ownership.implicit_single_domain
    assert binding.ownership.complete
    assert binding.ownership.owners_for("entity", "NPC") == ("economy",)
    assert binding.ownership.owners_for("relation", "SELLS") == ("economy",)


def test_explicit_multi_domain_ownership_is_canonical_and_shared() -> None:
    registry = _registry(
        _domain("content", tags=("auto-apply:entity-type:NPC@1",)),
        _domain(
            "economy",
            tags=(
                "auto-apply:relation-type:SELLS@1",
                "auto-apply:entity-type:NPC@1",
            ),
        ),
        _domain("global", tags=(AUTO_APPLY_IR_ALL_TAG_V1,)),
    )

    ownership = resolve_auto_apply_ir_ownership(registry)

    assert not ownership.implicit_single_domain
    assert ownership.complete
    assert ownership.ir_all_domain_ids == ("global",)
    assert ownership.owners_for("entity", "NPC") == (
        "content",
        "economy",
        "global",
    )
    assert ownership.owners_for("relation", "SELLS") == ("economy", "global")
    assert ownership.owners_for("entity", "ITEM") == ("global",)


def test_partial_multi_domain_map_is_explicitly_incomplete() -> None:
    ownership = resolve_auto_apply_ir_ownership(
        _registry(
            _domain("content", tags=("auto-apply:entity-type:NPC@1",)),
            _domain("economy"),
        )
    )

    assert not ownership.complete
    assert ownership.owners_for("entity", "NPC") == ("content",)
    assert ownership.owners_for("entity", "ITEM") == ()


@pytest.mark.parametrize(
    "tag",
    [
        "auto-apply",
        "auto-applyx:entity-type:NPC@1",
        "auto-apply/entity-type/NPC@1",
        "auto-apply:entity-type:NPC@2",
        "auto-apply:entity-type:NOT_A_NODE@1",
        "auto-apply:relation-type:NOT_AN_EDGE@1",
        "auto-apply:ir-all@2",
        "auto-apply:unknown@1",
    ],
)
def test_reserved_tag_grammar_and_version_are_fail_closed(tag: str) -> None:
    with pytest.raises(ValueError):
        resolve_auto_apply_ir_ownership(_registry(_domain("content", tags=(tag,))))


def test_deprecated_domain_cannot_retain_ownership() -> None:
    with pytest.raises(ValueError, match="deprecated"):
        resolve_auto_apply_ir_ownership(
            _registry(
                _domain(
                    "old",
                    tags=(AUTO_APPLY_IR_ALL_TAG_V1,),
                    status="deprecated",
                ),
                _domain("new"),
            )
        )


def test_classifier_digest_is_stable_but_binds_registry_and_mapping() -> None:
    left = auto_apply_ir_classifier_binding(
        _registry(_domain("economy", tags=(AUTO_APPLY_IR_ALL_TAG_V1,)))
    )
    same = auto_apply_ir_classifier_binding(
        _registry(_domain("economy", tags=(AUTO_APPLY_IR_ALL_TAG_V1,)))
    )
    changed_registry = auto_apply_ir_classifier_binding(
        _registry(
            _domain(
                "economy",
                tags=(AUTO_APPLY_IR_ALL_TAG_V1,),
                display_name="Economy renamed",
            )
        )
    )
    changed_mapping = auto_apply_ir_classifier_binding(_registry(_domain("economy")))

    assert left == same
    assert left.classifier_schema_digest != changed_registry.classifier_schema_digest
    assert left.classifier_schema_digest != changed_mapping.classifier_schema_digest
