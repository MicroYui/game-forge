"""Independent narrative oracle over hidden typed facts only."""

from __future__ import annotations

from dataclasses import dataclass

from gameforge.bench.narrative.contracts import (
    ActionFact,
    CooperationFact,
    HostilityFact,
    MembershipFact,
    NarrativeFact,
    RevealFact,
    RevealGateFact,
    RoleHolderFact,
    RoleLimitFact,
    TraitFact,
)
from gameforge.bench.taxonomy import DefectClass

ORACLE_VERSION = "narrative-oracle@1"


@dataclass(frozen=True)
class OracleViolation:
    defect_class: DefectClass
    causing_fact_ids: tuple[str, ...]
    target_entity_ids: tuple[str, ...]
    source_fact_ids: tuple[str, ...]


def _fact_index(facts: tuple[NarrativeFact, ...]) -> dict[str, NarrativeFact]:
    result = {fact.fact_id: fact for fact in facts}
    if len(result) != len(facts):
        raise ValueError("narrative facts contain duplicate fact IDs")
    return result


def evaluate_facts(facts: tuple[NarrativeFact, ...]) -> tuple[OracleViolation, ...]:
    """Derive narrative violations without reading rendered text or target labels."""

    indexed = _fact_index(facts)
    violations: list[OracleViolation] = []

    for fact in facts:
        if not isinstance(fact, ActionFact) or fact.violates_trait_fact_id is None:
            continue
        trait = indexed.get(fact.violates_trait_fact_id)
        if not isinstance(trait, TraitFact) or trait.entity_id != fact.entity_id:
            raise ValueError("action violation must reference a trait for the same entity")
        violations.append(
            OracleViolation(
                defect_class=DefectClass.character_violation,
                causing_fact_ids=(fact.fact_id,),
                target_entity_ids=(fact.entity_id,),
                source_fact_ids=(trait.fact_id,),
            )
        )

    gates: dict[str, RevealGateFact] = {}
    for fact in facts:
        if not isinstance(fact, RevealGateFact):
            continue
        if fact.secret_id in gates:
            raise ValueError("a secret may have only one reveal gate")
        gates[fact.secret_id] = fact
    for fact in facts:
        if not isinstance(fact, RevealFact):
            continue
        gate = gates.get(fact.secret_id)
        if gate is not None and fact.stage < gate.min_stage:
            violations.append(
                OracleViolation(
                    defect_class=DefectClass.spoiler,
                    causing_fact_ids=(fact.fact_id,),
                    target_entity_ids=tuple(sorted((fact.speaker_id, fact.secret_id))),
                    source_fact_ids=(gate.fact_id,),
                )
            )

    memberships: dict[str, MembershipFact] = {}
    for fact in facts:
        if not isinstance(fact, MembershipFact):
            continue
        if fact.entity_id in memberships:
            raise ValueError("an entity may have only one benchmark faction membership")
        memberships[fact.entity_id] = fact
    hostilities: dict[tuple[str, str], HostilityFact] = {}
    for fact in facts:
        if isinstance(fact, HostilityFact):
            key = tuple(sorted((fact.left_faction_id, fact.right_faction_id)))
            hostilities[key] = fact
    for fact in facts:
        if not isinstance(fact, CooperationFact):
            continue
        left = memberships.get(fact.left_entity_id)
        right = memberships.get(fact.right_entity_id)
        if left is None or right is None:
            continue
        hostility = hostilities.get(tuple(sorted((left.faction_id, right.faction_id))))
        if hostility is not None:
            violations.append(
                OracleViolation(
                    defect_class=DefectClass.faction_violation,
                    causing_fact_ids=(fact.fact_id,),
                    target_entity_ids=tuple(
                        sorted((fact.left_entity_id, fact.right_entity_id))
                    ),
                    source_fact_ids=tuple(
                        sorted((left.fact_id, right.fact_id, hostility.fact_id))
                    ),
                )
            )

    limits: dict[str, RoleLimitFact] = {}
    holders: dict[str, list[RoleHolderFact]] = {}
    for fact in facts:
        if isinstance(fact, RoleLimitFact):
            if fact.role_id in limits:
                raise ValueError("a role may have only one holder limit")
            limits[fact.role_id] = fact
        elif isinstance(fact, RoleHolderFact):
            holders.setdefault(fact.role_id, []).append(fact)
    for role_id, role_holders in holders.items():
        limit = limits.get(role_id)
        distinct_entities = sorted({holder.entity_id for holder in role_holders})
        if limit is not None and len(distinct_entities) > limit.max_holders:
            violations.append(
                OracleViolation(
                    defect_class=DefectClass.uniqueness_violation,
                    causing_fact_ids=tuple(
                        sorted(holder.fact_id for holder in role_holders)
                    ),
                    target_entity_ids=tuple(distinct_entities),
                    source_fact_ids=(limit.fact_id,),
                )
            )

    return tuple(
        sorted(
            violations,
            key=lambda item: (
                item.defect_class.value,
                item.causing_fact_ids,
                item.target_entity_ids,
                item.source_fact_ids,
            ),
        )
    )


__all__ = ["ORACLE_VERSION", "OracleViolation", "evaluate_facts"]
