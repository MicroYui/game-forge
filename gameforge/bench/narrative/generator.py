"""Deterministic hidden-fact generator for the narrative benchmark."""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass
from typing import Literal

from gameforge.bench.narrative.contracts import (
    NARRATIVE_CLASSES,
    ActionFact,
    CooperationFact,
    HostilityFact,
    MembershipFact,
    NarrativeCase,
    NarrativeConstraint,
    NarrativeFact,
    RevealFact,
    RevealGateFact,
    RoleHolderFact,
    RoleLimitFact,
    TraitFact,
    seal_case,
    to_agent_input,
)
from gameforge.bench.narrative.oracle import ORACLE_VERSION, evaluate_facts
from gameforge.bench.narrative.renderer import (
    RENDERER_VERSION,
    NarrativeRenderContext,
    render_facts,
)
from gameforge.bench.taxonomy import DefectClass

GENERATOR_VERSION = "narrative-generator@1"

ANSWER_MARKER = re.compile(
    r"(?i)(TRAIT\s*:|SPOILER\s*:|CONTRADICTION\s*:|UNIQUE-ROLE\s*:|"
    r"character_violation|faction_violation|uniqueness_violation|"
    r"\bdefect\s+class\b)"
)


@dataclass(frozen=True)
class _SettingPack:
    locations: tuple[str, ...]
    stages: tuple[str, ...]


@dataclass(frozen=True)
class _CharacterPattern:
    trait_id: str
    violating_action_id: str
    clean_action_id: str
    rules: tuple[str, ...]


_SETTINGS = (
    _SettingPack(
        locations=("archive hall", "north gate", "river market", "signal tower"),
        stages=("arrival", "first watch", "council session", "archive opening", "finale"),
    ),
    _SettingPack(
        locations=("orbital concourse", "cargo ring", "relay deck", "observatory"),
        stages=("docking", "survey", "relay alignment", "vault access", "departure"),
    ),
    _SettingPack(
        locations=("glass conservatory", "lower court", "canal bridge", "old mint"),
        stages=("procession", "hearing", "second bell", "sealed testimony", "verdict"),
    ),
    _SettingPack(
        locations=("desert waystation", "salt depot", "eastern ridge", "caravan court"),
        stages=("encampment", "dawn patrol", "trade council", "map unveiling", "crossing"),
    ),
)

_NAME_SETS = (
    ("Qi", "Mara", "Rook", "Sela", "Tovin"),
    ("Ansel", "Iria", "Bram", "Nadi", "Corin"),
    ("Veda", "Oren", "Lio", "Sabin", "Petra"),
    ("Mei", "Daro", "Nyra", "Kellan", "Ari"),
    ("Tamsin", "Eli", "Jora", "Pavel", "Suri"),
    ("Noor", "Cass", "Rian", "Mina", "Dax"),
    ("Ivo", "Lea", "Soren", "Kira", "Benno"),
    ("Ada", "Malik", "Yara", "Theo", "Jun"),
)

_CHARACTER_PATTERNS = (
    _CharacterPattern(
        trait_id="keeps_entrusted_secrets",
        violating_action_id="sold_entrusted_route",
        clean_action_id="guarded_entrusted_route",
        rules=(
            "{actor} must keep any route entrusted by an ally confidential.",
            "An allied patrol route entrusted to {actor} may not be disclosed.",
            "{actor} is consistently protective of routes shared in confidence.",
            "Even under pressure, {actor} does not trade away an ally's route.",
        ),
    ),
    _CharacterPattern(
        trait_id="protects_civilians",
        violating_action_id="endangered_civilians",
        clean_action_id="sheltered_civilians",
        rules=(
            "{actor} puts civilian safety ahead of tactical advantage.",
            "{actor} must not expose noncombatants to avoidable danger.",
            "Protecting displaced families is a standing commitment for {actor}.",
            "{actor} always preserves a safe route for civilians.",
        ),
    ),
    _CharacterPattern(
        trait_id="honors_sworn_promises",
        violating_action_id="broke_sworn_promise",
        clean_action_id="kept_sworn_promise",
        rules=(
            "{actor} treats a sworn promise as binding until every term is met.",
            "Once {actor} gives an oath to an ally, it cannot be abandoned.",
            "{actor} is known to fulfill pledges despite personal risk.",
            "A promise made by {actor} remains in force through the whole mission.",
        ),
    ),
    _CharacterPattern(
        trait_id="avoids_unprovoked_violence",
        violating_action_id="started_unprovoked_fight",
        clean_action_id="defused_argument",
        rules=(
            "{actor} never initiates violence against a peaceful visitor.",
            "When guests lower their weapons, {actor} is expected to preserve the truce.",
            "{actor} resolves disputes before considering force.",
            "An unarmed messenger must remain safe in {actor}'s presence.",
        ),
    ),
    _CharacterPattern(
        trait_id="shares_relief_supplies",
        violating_action_id="hoarded_relief_crates",
        clean_action_id="distributed_relief_crates",
        rules=(
            "{actor} distributes donated supplies to settlements in need.",
            "Relief stores under {actor}'s care must remain available to the public.",
            "{actor} may not reserve emergency medicine for private use.",
            "Public aid is always delivered by {actor} to its intended recipients.",
        ),
    ),
    _CharacterPattern(
        trait_id="loyal_to_crew",
        violating_action_id="betrayed_crew",
        clean_action_id="defended_crew",
        rules=(
            "{actor} never buys safety by sacrificing a crewmate.",
            "The crew can rely on {actor} to protect their escape route.",
            "{actor}'s loyalty extends to every member of the crew.",
            "No pursuer may learn the crew's refuge from {actor}.",
        ),
    ),
    _CharacterPattern(
        trait_id="safeguards_public_funds",
        violating_action_id="stole_public_funds",
        clean_action_id="returned_public_funds",
        rules=(
            "{actor} safeguards public funds and records every transfer.",
            "Settlement money entrusted to {actor} cannot be used privately.",
            "{actor} must return recovered coin to the public treasury.",
            "The council relies on {actor} to preserve the rebuilding fund.",
        ),
    ),
    _CharacterPattern(
        trait_id="rescues_injured_allies",
        violating_action_id="abandoned_injured_ally",
        clean_action_id="rescued_injured_ally",
        rules=(
            "{actor} does not leave an injured ally behind.",
            "A wounded companion must remain on {actor}'s rescue list.",
            "{actor} gives evacuation priority to injured allies.",
            "Even during retreat, {actor} stays with wounded companions.",
        ),
    ),
)

_SECRETS = (
    "the masked envoy's identity",
    "the origin of the black compass",
    "the saboteur's name",
    "the vault keeper's allegiance",
    "the reason the eastern beacon failed",
    "the hidden heir's identity",
    "the source of the winter signal",
    "the witness behind the sealed testimony",
)

_ROLES = (
    "the Warden",
    "the First Navigator",
    "the Archive Speaker",
    "the Beacon Keeper",
    "the Crown Witness",
    "the Harbor Marshal",
    "the Caravan Voice",
    "the Vault Steward",
)

_FACTION_PAIRS = (
    ("Harbor Guard", "Ash Fleet", "Canal Surveyors"),
    ("North Watch", "Red Banner", "Field Medics"),
    ("Relay Council", "Void Corsairs", "Chartmakers"),
    ("Glass Court", "Iron Compact", "River Guild"),
    ("Dawn Caravan", "Salt Raiders", "Well Keepers"),
    ("Archive Ward", "Cinder Circle", "Lantern Office"),
    ("Free Pilots", "Obsidian Wing", "Dock Engineers"),
    ("Valley Rangers", "Storm Company", "Bridge Masons"),
)


def _digest(*parts: object) -> bytes:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(payload).digest()


def _rng_for(
    split: str,
    defect_class: DefectClass,
    is_clean: bool,
    seed: int,
) -> random.Random:
    digest = _digest(
        GENERATOR_VERSION,
        split,
        defect_class.value,
        "clean" if is_clean else "positive",
        seed,
    )
    return random.Random(int.from_bytes(digest[:16], "big"))


def _opaque_id(namespace: str, private_key: str, label: str) -> str:
    suffix = hashlib.sha256(f"{private_key}|{label}".encode()).hexdigest()[:16]
    return f"{namespace}:{suffix}"


def _constraint(
    constraint_id: str,
    entity_ids: tuple[str, ...],
    statement: str,
    source_fact_ids: tuple[str, ...],
) -> NarrativeConstraint:
    return NarrativeConstraint(
        constraint_id=constraint_id,
        entity_ids=tuple(sorted(entity_ids)),
        statement=statement,
        source_fact_ids=tuple(sorted(source_fact_ids)),
    )


def _build_character_world(
    rng: random.Random,
    private_key: str,
    names: tuple[str, ...],
    is_clean: bool,
) -> tuple[list[NarrativeFact], list[NarrativeConstraint], dict[str, str], dict[str, str]]:
    pattern = rng.choice(_CHARACTER_PATTERNS)
    actor_id = _opaque_id("npc", private_key, "primary-a")
    trait_id = _opaque_id("fact", private_key, "baseline-a")
    action_id = _opaque_id("fact", private_key, "event-a")
    fact_trait = TraitFact(
        fact_id=trait_id,
        entity_id=actor_id,
        trait_id=pattern.trait_id,
    )
    fact_action = ActionFact(
        fact_id=action_id,
        entity_id=actor_id,
        action_id=(pattern.clean_action_id if is_clean else pattern.violating_action_id),
        violates_trait_fact_id=None if is_clean else trait_id,
    )
    constraint = _constraint(
        _opaque_id("C", private_key, "rule-a"),
        (actor_id,),
        rng.choice(pattern.rules).format(actor=names[0]),
        (trait_id,),
    )
    return [fact_trait, fact_action], [constraint], {actor_id: names[0]}, {}


def _build_spoiler_world(
    rng: random.Random,
    private_key: str,
    names: tuple[str, ...],
    stages: tuple[str, ...],
    is_clean: bool,
) -> tuple[list[NarrativeFact], list[NarrativeConstraint], dict[str, str], dict[str, str]]:
    speaker_id = _opaque_id("npc", private_key, "primary-a")
    secret_id = _opaque_id("secret", private_key, "concept-a")
    gate_id = _opaque_id("fact", private_key, "baseline-a")
    reveal_id = _opaque_id("fact", private_key, "event-a")
    min_stage = rng.randrange(2, len(stages))
    stage = rng.randrange(min_stage) if not is_clean else rng.randrange(min_stage, len(stages))
    secret_name = rng.choice(_SECRETS)
    gate = RevealGateFact(fact_id=gate_id, secret_id=secret_id, min_stage=min_stage)
    reveal = RevealFact(
        fact_id=reveal_id,
        speaker_id=speaker_id,
        secret_id=secret_id,
        stage=stage,
    )
    rule_templates = (
        "No one, including {speaker}, may disclose {secret} before {stage}.",
        "The story withholds {secret} until {stage}; {speaker} knows this restriction.",
        "References by {speaker} to {secret} are permitted only from {stage} onward.",
        "The audience cannot learn {secret} before {stage}, even through {speaker}.",
    )
    constraint = _constraint(
        _opaque_id("C", private_key, "rule-a"),
        (speaker_id, secret_id),
        rng.choice(rule_templates).format(
            speaker=names[0],
            secret=secret_name,
            stage=stages[min_stage],
        ),
        (gate_id,),
    )
    return (
        [gate, reveal],
        [constraint],
        {speaker_id: names[0]},
        {secret_id: secret_name},
    )


def _build_faction_world(
    rng: random.Random,
    private_key: str,
    names: tuple[str, ...],
    is_clean: bool,
) -> tuple[list[NarrativeFact], list[NarrativeConstraint], dict[str, str], dict[str, str]]:
    left_id = _opaque_id("npc", private_key, "primary-a")
    right_id = _opaque_id("npc", private_key, "primary-b")
    left_faction_id = _opaque_id("faction", private_key, "faction-a")
    hostile_faction_id = _opaque_id("faction", private_key, "faction-b")
    neutral_faction_id = _opaque_id("faction", private_key, "faction-c")
    actual_right_faction_id = neutral_faction_id if is_clean else hostile_faction_id
    left_name, hostile_name, neutral_name = rng.choice(_FACTION_PAIRS)
    actual_right_name = neutral_name if is_clean else hostile_name

    membership_left = MembershipFact(
        fact_id=_opaque_id("fact", private_key, "baseline-membership-a"),
        entity_id=left_id,
        faction_id=left_faction_id,
    )
    membership_right = MembershipFact(
        fact_id=_opaque_id("fact", private_key, "baseline-membership-b"),
        entity_id=right_id,
        faction_id=actual_right_faction_id,
    )
    hostility = HostilityFact(
        fact_id=_opaque_id("fact", private_key, "baseline-hostility"),
        left_faction_id=left_faction_id,
        right_faction_id=hostile_faction_id,
    )
    cooperation = CooperationFact(
        fact_id=_opaque_id("fact", private_key, "event-a"),
        left_entity_id=left_id,
        right_entity_id=right_id,
    )
    rules = (
        "{left_actor} serves {left_faction}, while {right_actor} serves {right_faction}. "
        "{left_faction} and {hostile_faction} do not coordinate operations.",
        "The allegiance record places {left_actor} with {left_faction} and {right_actor} "
        "with {right_faction}; {left_faction} remains at war with {hostile_faction}.",
        "{left_actor} belongs to {left_faction}; {right_actor} belongs to {right_faction}. "
        "A standing feud bars joint commands between {left_faction} and {hostile_faction}.",
        "Orders identify {left_actor} as {left_faction} and {right_actor} as "
        "{right_faction}. {left_faction} treats {hostile_faction} as an enemy force.",
    )
    source_ids = (
        membership_left.fact_id,
        membership_right.fact_id,
        hostility.fact_id,
    )
    constraint = _constraint(
        _opaque_id("C", private_key, "rule-a"),
        (left_id, right_id),
        rng.choice(rules).format(
            left_actor=names[0],
            right_actor=names[1],
            left_faction=left_name,
            right_faction=actual_right_name,
            hostile_faction=hostile_name,
        ),
        source_ids,
    )
    return (
        [membership_left, membership_right, hostility, cooperation],
        [constraint],
        {left_id: names[0], right_id: names[1]},
        {},
    )


def _build_uniqueness_world(
    rng: random.Random,
    private_key: str,
    names: tuple[str, ...],
    is_clean: bool,
) -> tuple[list[NarrativeFact], list[NarrativeConstraint], dict[str, str], dict[str, str]]:
    first_id = _opaque_id("npc", private_key, "primary-a")
    second_id = _opaque_id("npc", private_key, "primary-b")
    role_id = _opaque_id("role", private_key, "concept-a")
    limit = RoleLimitFact(
        fact_id=_opaque_id("fact", private_key, "baseline-a"),
        role_id=role_id,
        max_holders=1,
    )
    first = RoleHolderFact(
        fact_id=_opaque_id("fact", private_key, "event-a"),
        role_id=role_id,
        entity_id=first_id,
    )
    facts: list[NarrativeFact] = [limit, first]
    entity_names = {first_id: names[0]}
    constraint_entities = [first_id]
    if not is_clean:
        second = RoleHolderFact(
            fact_id=_opaque_id("fact", private_key, "event-b"),
            role_id=role_id,
            entity_id=second_id,
        )
        facts.append(second)
        entity_names[second_id] = names[1]
        constraint_entities.append(second_id)
    role_name = rng.choice(_ROLES)
    rules = (
        "Only one person may hold the title of {role} at a time.",
        "The office of {role} has a single serving holder.",
        "At any point in the story, exactly one person is recognized as {role}.",
        "The charter permits no more than one active {role}.",
    )
    constraint = _constraint(
        _opaque_id("C", private_key, "rule-a"),
        tuple(constraint_entities),
        rng.choice(rules).format(role=role_name),
        (limit.fact_id,),
    )
    return facts, [constraint], entity_names, {role_id: role_name}


def _add_distractors(
    facts: list[NarrativeFact],
    constraints: list[NarrativeConstraint],
    entity_names: dict[str, str],
    rng: random.Random,
    private_key: str,
    names: tuple[str, ...],
) -> None:
    count = rng.randrange(1, 4)
    used_names = set(entity_names.values())
    available_names = [name for name in names if name not in used_names]
    for index in range(count):
        pattern = rng.choice(_CHARACTER_PATTERNS)
        entity_id = _opaque_id("npc", private_key, f"distractor-{index}")
        trait_fact = TraitFact(
            fact_id=_opaque_id("fact", private_key, f"distractor-baseline-{index}"),
            entity_id=entity_id,
            trait_id=pattern.trait_id,
        )
        event_fact = ActionFact(
            fact_id=_opaque_id("fact", private_key, f"distractor-event-{index}"),
            entity_id=entity_id,
            action_id=pattern.clean_action_id,
        )
        name = available_names[index % len(available_names)]
        entity_names[entity_id] = name
        facts.extend((trait_fact, event_fact))
        constraints.append(
            _constraint(
                _opaque_id("C", private_key, f"distractor-rule-{index}"),
                (entity_id,),
                rng.choice(pattern.rules).format(actor=name),
                (trait_fact.fact_id,),
            )
        )


def _validate_inputs(
    split: str,
    defect_class: DefectClass,
    seed: int,
    case_id: str,
) -> None:
    if split not in {"development", "verification"}:
        raise ValueError("narrative split must be development or verification")
    if defect_class not in NARRATIVE_CLASSES:
        raise ValueError("generator requires a narrative defect class")
    if seed < 0:
        raise ValueError("narrative seed must be nonnegative")
    if not case_id.strip():
        raise ValueError("narrative case ID must not be blank")


def generate_case(
    *,
    split: Literal["development", "verification"],
    defect_class: DefectClass,
    is_clean: bool,
    seed: int,
    case_id: str,
) -> NarrativeCase:
    """Generate one sealed case without exposing its hidden oracle labels."""

    _validate_inputs(split, defect_class, seed, case_id)
    rng = _rng_for(split, defect_class, is_clean, seed)
    private_key = hashlib.sha256(
        _digest(GENERATOR_VERSION, split, defect_class.value, is_clean, seed)
    ).hexdigest()
    setting = rng.choice(_SETTINGS)
    names = rng.choice(_NAME_SETS)

    if defect_class is DefectClass.character_violation:
        built = _build_character_world(rng, private_key, names, is_clean)
    elif defect_class is DefectClass.spoiler:
        built = _build_spoiler_world(
            rng,
            private_key,
            names,
            setting.stages,
            is_clean,
        )
    elif defect_class is DefectClass.faction_violation:
        built = _build_faction_world(rng, private_key, names, is_clean)
    elif defect_class is DefectClass.uniqueness_violation:
        built = _build_uniqueness_world(rng, private_key, names, is_clean)
    else:  # pragma: no cover - guarded by _validate_inputs and enum membership
        raise AssertionError("unreachable narrative class")

    facts, constraints, entity_names, concept_names = built
    _add_distractors(
        facts,
        constraints,
        entity_names,
        rng,
        private_key,
        names,
    )
    fact_tuple = tuple(facts)
    violations = evaluate_facts(fact_tuple)
    render_seed = int.from_bytes(_digest(private_key, RENDERER_VERSION)[:8], "big")
    rendered = render_facts(
        fact_tuple,
        NarrativeRenderContext(
            entity_names=tuple(sorted(entity_names.items())),
            concept_names=tuple(sorted(concept_names.items())),
            locations=setting.locations,
            stage_names=setting.stages,
        ),
        render_seed=render_seed,
    )

    if ANSWER_MARKER.search(rendered.dialogue) or any(
        ANSWER_MARKER.search(item.statement) for item in constraints
    ):
        raise ValueError("formal narrative text contains an answer marker")

    target_entities: tuple[str, ...] = ()
    target_constraint_ids: tuple[str, ...] = ()
    target_span = None
    stored_class = None
    if is_clean:
        if violations:
            raise ValueError("clean narrative case produced an oracle violation")
    else:
        if len(violations) != 1 or violations[0].defect_class is not defect_class:
            raise ValueError("positive narrative case must contain exactly one target violation")
        violation = violations[0]
        target_entities = violation.target_entity_ids
        source_ids = set(violation.source_fact_ids)
        target_constraint_ids = tuple(
            sorted(
                item.constraint_id
                for item in constraints
                if source_ids.issubset(item.source_fact_ids)
            )
        )
        if not target_constraint_ids:
            raise ValueError("oracle source facts have no visible narrative constraint")
        causing_spans = [
            rendered.spans_by_fact_id[fact_id]
            for fact_id in violation.causing_fact_ids
            if fact_id in rendered.spans_by_fact_id
        ]
        if not causing_spans:
            raise ValueError("oracle causing facts have no rendered source span")
        target_span = max(causing_spans, key=lambda item: (item.start, item.fact_id))
        stored_class = defect_class

    case = seal_case(
        schema_version="narrative-case@1",
        case_id=case_id,
        generator_version=GENERATOR_VERSION,
        renderer_version=RENDERER_VERSION,
        oracle_version=ORACLE_VERSION,
        seed=seed,
        split=split,
        benchmark_family=defect_class,
        facts=fact_tuple,
        constraints=tuple(constraints),
        dialogue=rendered.dialogue,
        is_clean=is_clean,
        defect_class=stored_class,
        target_entities=target_entities,
        target_constraint_ids=target_constraint_ids,
        target_span=target_span,
    )
    visible = to_agent_input(case).model_dump(mode="json")
    visible_json = json.dumps(visible, sort_keys=True, ensure_ascii=False)
    if case_id in visible_json:
        raise ValueError("case ID leaked into the model payload")
    return case


__all__ = ["ANSWER_MARKER", "GENERATOR_VERSION", "generate_case"]
