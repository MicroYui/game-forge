"""Natural-language rendering of narrative event facts with exact source spans."""

from __future__ import annotations

import random
from dataclasses import dataclass

from gameforge.bench.narrative.contracts import (
    ActionFact,
    CooperationFact,
    NarrativeFact,
    RevealFact,
    RoleHolderFact,
    TargetSpan,
)

RENDERER_VERSION = "narrative-renderer@1"

_ACTION_TEMPLATES: dict[str, tuple[str, ...]] = {
    "sold_entrusted_route": (
        "At {location}, {actor} sold an entrusted route to the raiders.",
        "{actor} quietly traded the allied patrol route for coin at {location}.",
        "In {location}, {actor} handed a confidential route to the attacking force.",
        "{actor} disclosed the protected route to its pursuers outside {location}.",
    ),
    "guarded_entrusted_route": (
        "At {location}, {actor} refused every offer for the entrusted route.",
        "{actor} kept the allied patrol route secret throughout the watch at {location}.",
        "In {location}, {actor} destroyed a stolen copy of the protected route.",
        "{actor} returned the confidential route unopened to its owner at {location}.",
    ),
    "endangered_civilians": (
        "At {location}, {actor} locked the evacuation gate while civilians waited outside.",
        "{actor} redirected the shelter convoy into active fire near {location}.",
        "In {location}, {actor} used the civilian refuge as bait for an ambush.",
        "{actor} ordered the last rescue boat away from the stranded families at {location}.",
    ),
    "sheltered_civilians": (
        "At {location}, {actor} opened the protected gate for every waiting family.",
        "{actor} guided the shelter convoy around the fighting near {location}.",
        "In {location}, {actor} moved the civilians before the ambush began.",
        "{actor} kept the final rescue boat beside the stranded families at {location}.",
    ),
    "broke_sworn_promise": (
        "At {location}, {actor} abandoned the ally named in a sworn promise.",
        "{actor} denied the pledge as soon as danger reached {location}.",
        "In {location}, {actor} delivered the promised supplies to the ally's rival.",
        "{actor} publicly renounced the oath before its terms were fulfilled at {location}.",
    ),
    "kept_sworn_promise": (
        "At {location}, {actor} stayed until every term of the sworn promise was fulfilled.",
        "{actor} honored the pledge even after danger reached {location}.",
        "In {location}, {actor} delivered the promised supplies to the waiting ally.",
        "{actor} renewed the oath after completing its final obligation at {location}.",
    ),
    "started_unprovoked_fight": (
        "At {location}, {actor} struck an unarmed visitor without warning.",
        "{actor} started a fight after the delegates lowered their weapons at {location}.",
        "In {location}, {actor} attacked the messenger before a word was exchanged.",
        "{actor} overturned the truce table and charged the guests at {location}.",
    ),
    "defused_argument": (
        "At {location}, {actor} separated the delegates without drawing a weapon.",
        "{actor} ended the argument by restoring the truce at {location}.",
        "In {location}, {actor} heard the messenger out before lowering the guard.",
        "{actor} kept the truce table standing and calmed the guests at {location}.",
    ),
    "hoarded_relief_crates": (
        "At {location}, {actor} hid the relief crates in a private storehouse.",
        "{actor} kept the winter medicine while the outlying camp waited at {location}.",
        "In {location}, {actor} marked public rations as a personal reserve.",
        "{actor} diverted the donated food away from the hungry district at {location}.",
    ),
    "distributed_relief_crates": (
        "At {location}, {actor} opened the relief crates for the waiting camp.",
        "{actor} sent the winter medicine to the outlying camp from {location}.",
        "In {location}, {actor} returned the reserved rations to public stores.",
        "{actor} delivered the donated food to the hungry district at {location}.",
    ),
    "betrayed_crew": (
        "At {location}, {actor} revealed the crew's hiding place to their pursuers.",
        "{actor} sealed the crew outside when the alarm sounded at {location}.",
        "In {location}, {actor} exchanged a crewmate for safe passage.",
        "{actor} disabled the crew's escape route before joining their hunters at {location}.",
    ),
    "defended_crew": (
        "At {location}, {actor} concealed the crew's hiding place from their pursuers.",
        "{actor} held the gate until the whole crew entered {location}.",
        "In {location}, {actor} refused safe passage that excluded a crewmate.",
        "{actor} repaired the crew's escape route before the hunters arrived at {location}.",
    ),
    "stole_public_funds": (
        "At {location}, {actor} transferred the rebuilding fund into a private account.",
        "{actor} erased the public ledger after taking its reserve at {location}.",
        "In {location}, {actor} spent the settlement fund on a personal estate.",
        "{actor} replaced the treasury seal after removing the public coin at {location}.",
    ),
    "returned_public_funds": (
        "At {location}, {actor} transferred the recovered coin into the rebuilding fund.",
        "{actor} restored the public ledger and its full reserve at {location}.",
        "In {location}, {actor} returned the settlement fund to the council.",
        "{actor} replaced the treasury seal only after counting every public coin at {location}.",
    ),
    "abandoned_injured_ally": (
        "At {location}, {actor} left an injured ally behind to lighten the cart.",
        "{actor} closed the lift while a wounded companion called from {location}.",
        "In {location}, {actor} took the last medicine and walked past the injured scout.",
        "{actor} removed the ally's name from the rescue list at {location}.",
    ),
    "rescued_injured_ally": (
        "At {location}, {actor} carried an injured ally onto the crowded cart.",
        "{actor} held the lift for a wounded companion at {location}.",
        "In {location}, {actor} gave the last medicine to the injured scout.",
        "{actor} added the stranded ally to the rescue list at {location}.",
    ),
}

_REVEAL_TEMPLATES = (
    "During {stage} at {location}, {speaker} openly named {secret}.",
    "At {location} during {stage}, {speaker} told the room the truth about {secret}.",
    "While the story was still in {stage}, {speaker} identified {secret} at {location}.",
    "In {location}, {speaker} disclosed {secret} during {stage}.",
)

_COOPERATION_TEMPLATES = (
    "At {location}, {left} and {right} signed a joint battle plan.",
    "{left} fought beside {right} under one banner at {location}.",
    "In {location}, {left} supplied {right} and called the partnership permanent.",
    "{left} welcomed {right} into a shared command post at {location}.",
)

_ROLE_TEMPLATES = (
    "At {location}, {entity} was formally introduced as {role}.",
    "{entity} accepted the title of {role} before the assembly at {location}.",
    "In {location}, the council recognized {entity} as {role}.",
    "{entity} signed the register as {role} at {location}.",
)


@dataclass(frozen=True)
class NarrativeRenderContext:
    entity_names: tuple[tuple[str, str], ...]
    concept_names: tuple[tuple[str, str], ...]
    locations: tuple[str, ...]
    stage_names: tuple[str, ...]

    def __post_init__(self) -> None:
        for label, values in (
            ("entity", self.entity_names),
            ("concept", self.concept_names),
        ):
            keys = [key for key, _ in values]
            if len(keys) != len(set(keys)):
                raise ValueError(f"duplicate {label} display-name ID")
            if any(not key.strip() or not name.strip() for key, name in values):
                raise ValueError(f"blank {label} display name")
        if not self.locations or any(not item.strip() for item in self.locations):
            raise ValueError("renderer requires nonblank locations")
        if len(self.stage_names) < 2 or any(not item.strip() for item in self.stage_names):
            raise ValueError("renderer requires at least two stage names")

    def entity_name(self, entity_id: str) -> str:
        return _lookup(self.entity_names, entity_id, "entity")

    def concept_name(self, concept_id: str) -> str:
        return _lookup(self.concept_names, concept_id, "concept")


@dataclass(frozen=True)
class RenderedNarrative:
    dialogue: str
    spans_by_fact_id: dict[str, TargetSpan]


def _lookup(values: tuple[tuple[str, str], ...], key: str, label: str) -> str:
    for candidate, name in values:
        if candidate == key:
            return name
    raise ValueError(f"missing {label} display name for {key}")


def _render_event(
    fact: NarrativeFact,
    context: NarrativeRenderContext,
    rng: random.Random,
) -> str | None:
    location = rng.choice(context.locations)
    if isinstance(fact, ActionFact):
        templates = _ACTION_TEMPLATES.get(fact.action_id)
        if templates is None:
            raise ValueError(f"unsupported narrative action {fact.action_id}")
        return rng.choice(templates).format(
            actor=context.entity_name(fact.entity_id),
            location=location,
        )
    if isinstance(fact, RevealFact):
        if fact.stage >= len(context.stage_names):
            raise ValueError(f"reveal stage {fact.stage} has no display name")
        return rng.choice(_REVEAL_TEMPLATES).format(
            speaker=context.entity_name(fact.speaker_id),
            secret=context.concept_name(fact.secret_id),
            stage=context.stage_names[fact.stage],
            location=location,
        )
    if isinstance(fact, CooperationFact):
        return rng.choice(_COOPERATION_TEMPLATES).format(
            left=context.entity_name(fact.left_entity_id),
            right=context.entity_name(fact.right_entity_id),
            location=location,
        )
    if isinstance(fact, RoleHolderFact):
        return rng.choice(_ROLE_TEMPLATES).format(
            entity=context.entity_name(fact.entity_id),
            role=context.concept_name(fact.role_id),
            location=location,
        )
    return None


def render_facts(
    facts: tuple[NarrativeFact, ...],
    context: NarrativeRenderContext,
    render_seed: int,
) -> RenderedNarrative:
    rng = random.Random(render_seed)
    events: list[tuple[str, str]] = []
    for fact in facts:
        sentence = _render_event(fact, context, rng)
        if sentence is not None:
            events.append((fact.fact_id, sentence))
    if not events:
        raise ValueError("narrative facts contain no renderable events")
    rng.shuffle(events)
    separator = "\n" if rng.randrange(2) else " "
    parts: list[str] = []
    spans: dict[str, TargetSpan] = {}
    offset = 0
    for index, (fact_id, sentence) in enumerate(events):
        if index:
            parts.append(separator)
            offset += len(separator)
        start = offset
        parts.append(sentence)
        offset += len(sentence)
        spans[fact_id] = TargetSpan(
            start=start,
            end=offset,
            text=sentence,
            fact_id=fact_id,
        )
    return RenderedNarrative(dialogue="".join(parts), spans_by_fact_id=spans)


__all__ = [
    "RENDERER_VERSION",
    "NarrativeRenderContext",
    "RenderedNarrative",
    "render_facts",
]
