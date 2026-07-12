from __future__ import annotations

import ast
from pathlib import Path

import pytest

from gameforge.bench.narrative import oracle
from gameforge.bench.narrative.contracts import (
    ActionFact,
    CooperationFact,
    HostilityFact,
    MembershipFact,
    RevealFact,
    RevealGateFact,
    RoleHolderFact,
    RoleLimitFact,
    TraitFact,
)
from gameforge.bench.narrative.oracle import ORACLE_VERSION, evaluate_facts
from gameforge.bench.taxonomy import DefectClass


def _character_world(violating: bool):
    return (
        TraitFact(
            fact_id="fact:trait",
            entity_id="npc:qi",
            trait_id="keeps_entrusted_secrets",
        ),
        ActionFact(
            fact_id="fact:action",
            entity_id="npc:qi",
            action_id="sold_route" if violating else "guarded_route",
            violates_trait_fact_id="fact:trait" if violating else None,
        ),
    )


def _spoiler_world(reveal_stage: int, allowed_stage: int):
    return (
        RevealGateFact(
            fact_id="fact:gate",
            secret_id="secret:warden",
            min_stage=allowed_stage,
        ),
        RevealFact(
            fact_id="fact:reveal",
            speaker_id="npc:qi",
            secret_id="secret:warden",
            stage=reveal_stage,
        ),
    )


def _faction_world(cooperating: bool):
    facts = [
        MembershipFact(
            fact_id="fact:left-member",
            entity_id="npc:qi",
            faction_id="faction:outpost",
        ),
        MembershipFact(
            fact_id="fact:right-member",
            entity_id="npc:raider",
            faction_id="faction:raiders",
        ),
        HostilityFact(
            fact_id="fact:hostile",
            left_faction_id="faction:outpost",
            right_faction_id="faction:raiders",
        ),
    ]
    if cooperating:
        facts.append(
            CooperationFact(
                fact_id="fact:cooperate",
                left_entity_id="npc:qi",
                right_entity_id="npc:raider",
            )
        )
    return tuple(facts)


def _unique_world(holders: int):
    facts = [
        RoleLimitFact(
            fact_id="fact:role-limit",
            role_id="role:warden",
            max_holders=1,
        )
    ]
    for index in range(holders):
        facts.append(
            RoleHolderFact(
                fact_id=f"fact:holder-{index}",
                role_id="role:warden",
                entity_id=f"npc:holder-{index}",
            )
        )
    return tuple(facts)


@pytest.mark.parametrize(
    ("facts", "expected_class", "expected_entities"),
    [
        (_character_world(True), DefectClass.character_violation, ("npc:qi",)),
        (
            _spoiler_world(1, 4),
            DefectClass.spoiler,
            ("npc:qi", "secret:warden"),
        ),
        (
            _faction_world(True),
            DefectClass.faction_violation,
            ("npc:qi", "npc:raider"),
        ),
        (
            _unique_world(2),
            DefectClass.uniqueness_violation,
            ("npc:holder-0", "npc:holder-1"),
        ),
    ],
)
def test_oracle_derives_each_class_from_typed_facts(
    facts, expected_class, expected_entities
):
    violations = evaluate_facts(facts)
    assert len(violations) == 1
    assert violations[0].defect_class is expected_class
    assert violations[0].target_entity_ids == expected_entities


@pytest.mark.parametrize(
    "facts",
    [
        _character_world(False),
        _spoiler_world(4, 4),
        _faction_world(False),
        _unique_world(1),
    ],
)
def test_clean_worlds_have_no_oracle_violation(facts):
    assert evaluate_facts(facts) == ()


def test_oracle_rejects_invalid_cross_fact_state():
    bad_action = ActionFact(
        fact_id="fact:action",
        entity_id="npc:qi",
        action_id="sold_route",
        violates_trait_fact_id="fact:missing",
    )
    with pytest.raises(ValueError, match="trait"):
        evaluate_facts((bad_action,))

    duplicate_membership = _faction_world(True) + (
        MembershipFact(
            fact_id="fact:second-membership",
            entity_id="npc:qi",
            faction_id="faction:other",
        ),
    )
    with pytest.raises(ValueError, match="membership"):
        evaluate_facts(duplicate_membership)


def test_oracle_output_order_is_deterministic():
    facts = (*_unique_world(2), *_character_world(True))
    first = evaluate_facts(facts)
    second = evaluate_facts(tuple(reversed(facts)))
    assert first == second
    assert [item.defect_class.value for item in first] == sorted(
        item.defect_class.value for item in first
    )


def test_oracle_is_versioned_and_does_not_import_renderer_or_agents():
    assert ORACLE_VERSION == "narrative-oracle@1"
    tree = ast.parse(Path(oracle.__file__).read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert "gameforge.bench.narrative.renderer" not in imports
    assert not any(name.startswith("gameforge.agents") for name in imports)
