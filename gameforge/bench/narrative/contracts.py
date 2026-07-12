"""Strict contracts for hidden narrative facts and rendered benchmark cases."""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.agent_io import (
    DialogueNarrativeInput,
    NarrativeConstraintInput,
)
from gameforge.contracts.canonical import canonical_json

StableId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$",
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

NARRATIVE_CLASSES: tuple[DefectClass, ...] = (
    DefectClass.character_violation,
    DefectClass.spoiler,
    DefectClass.faction_violation,
    DefectClass.uniqueness_violation,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TraitFact(_StrictModel):
    kind: Literal["trait"] = "trait"
    fact_id: StableId
    entity_id: StableId
    trait_id: StableId


class ActionFact(_StrictModel):
    kind: Literal["action"] = "action"
    fact_id: StableId
    entity_id: StableId
    action_id: StableId
    violates_trait_fact_id: StableId | None = None


class RevealGateFact(_StrictModel):
    kind: Literal["reveal_gate"] = "reveal_gate"
    fact_id: StableId
    secret_id: StableId
    min_stage: int = Field(ge=0)


class RevealFact(_StrictModel):
    kind: Literal["reveal"] = "reveal"
    fact_id: StableId
    speaker_id: StableId
    secret_id: StableId
    stage: int = Field(ge=0)


class MembershipFact(_StrictModel):
    kind: Literal["membership"] = "membership"
    fact_id: StableId
    entity_id: StableId
    faction_id: StableId


class HostilityFact(_StrictModel):
    kind: Literal["hostility"] = "hostility"
    fact_id: StableId
    left_faction_id: StableId
    right_faction_id: StableId

    @model_validator(mode="after")
    def validate_distinct_factions(self) -> HostilityFact:
        if self.left_faction_id == self.right_faction_id:
            raise ValueError("hostility requires two distinct factions")
        return self


class CooperationFact(_StrictModel):
    kind: Literal["cooperation"] = "cooperation"
    fact_id: StableId
    left_entity_id: StableId
    right_entity_id: StableId

    @model_validator(mode="after")
    def validate_distinct_entities(self) -> CooperationFact:
        if self.left_entity_id == self.right_entity_id:
            raise ValueError("cooperation requires two distinct entities")
        return self


class RoleLimitFact(_StrictModel):
    kind: Literal["role_limit"] = "role_limit"
    fact_id: StableId
    role_id: StableId
    max_holders: int = Field(gt=0)


class RoleHolderFact(_StrictModel):
    kind: Literal["role_holder"] = "role_holder"
    fact_id: StableId
    role_id: StableId
    entity_id: StableId


NarrativeFact = Annotated[
    TraitFact
    | ActionFact
    | RevealGateFact
    | RevealFact
    | MembershipFact
    | HostilityFact
    | CooperationFact
    | RoleLimitFact
    | RoleHolderFact,
    Field(discriminator="kind"),
]


def _sorted_unique(values: tuple[str, ...], field: str, *, nonempty: bool) -> None:
    if nonempty and not values:
        raise ValueError(f"{field} must not be empty")
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{field} must be unique and sorted")


class NarrativeConstraint(_StrictModel):
    constraint_id: StableId
    entity_ids: tuple[StableId, ...]
    statement: NonEmptyText
    source_fact_ids: tuple[StableId, ...]

    @model_validator(mode="after")
    def validate_ordered_ids(self) -> NarrativeConstraint:
        _sorted_unique(self.entity_ids, "constraint entity_ids", nonempty=True)
        _sorted_unique(self.source_fact_ids, "constraint source_fact_ids", nonempty=True)
        return self


class TargetSpan(_StrictModel):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str
    fact_id: StableId

    @model_validator(mode="after")
    def validate_range(self) -> TargetSpan:
        if self.end <= self.start:
            raise ValueError("target span must be nonempty")
        if not self.text.strip():
            raise ValueError("target span text must not be blank")
        return self


def _json_value(value: Any, *, exclude: set[str] | None = None) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude=exclude or set())
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def content_sha256(value: Any, *, exclude: set[str] | None = None) -> str:
    payload = _json_value(value, exclude=exclude)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class NarrativeCase(_StrictModel):
    schema_version: Literal["narrative-case@1"] = "narrative-case@1"
    case_id: StableId
    generator_version: StableId
    renderer_version: StableId
    oracle_version: StableId
    seed: int = Field(ge=0)
    split: Literal["development", "verification"]
    benchmark_family: DefectClass
    facts: tuple[NarrativeFact, ...]
    constraints: tuple[NarrativeConstraint, ...]
    dialogue: str
    is_clean: bool
    defect_class: DefectClass | None = None
    target_entities: tuple[StableId, ...] = ()
    target_constraint_ids: tuple[StableId, ...] = ()
    target_span: TargetSpan | None = None
    case_sha256: Sha256

    @field_validator("dialogue")
    @classmethod
    def validate_dialogue(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("dialogue must not be blank")
        return value

    @classmethod
    def seal(cls, **values: Any) -> NarrativeCase:
        payload = dict(values)
        payload.pop("case_sha256", None)
        payload["case_sha256"] = content_sha256(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_case(self) -> NarrativeCase:
        if self.benchmark_family not in NARRATIVE_CLASSES:
            raise ValueError("benchmark_family must be a narrative defect class")
        if not self.facts:
            raise ValueError("narrative case must contain facts")
        fact_ids = [item.fact_id for item in self.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("narrative case contains duplicate fact IDs")
        facts = {item.fact_id: item for item in self.facts}

        if not self.constraints:
            raise ValueError("narrative case must contain constraints")
        constraint_ids = [item.constraint_id for item in self.constraints]
        if len(constraint_ids) != len(set(constraint_ids)):
            raise ValueError("narrative case contains duplicate constraint IDs")
        for constraint in self.constraints:
            missing = set(constraint.source_fact_ids) - set(facts)
            if missing:
                raise ValueError("constraint references an unknown source fact")

        for fact in self.facts:
            if isinstance(fact, ActionFact) and fact.violates_trait_fact_id is not None:
                trait = facts.get(fact.violates_trait_fact_id)
                if not isinstance(trait, TraitFact) or trait.entity_id != fact.entity_id:
                    raise ValueError("action references an invalid trait fact")

        _sorted_unique(self.target_entities, "target_entities", nonempty=not self.is_clean)
        _sorted_unique(
            self.target_constraint_ids,
            "target_constraint_ids",
            nonempty=not self.is_clean,
        )
        if set(self.target_constraint_ids) - set(constraint_ids):
            raise ValueError("target constraint IDs must reference case constraints")
        visible_entities = {
            entity_id
            for constraint in self.constraints
            for entity_id in constraint.entity_ids
        }
        if set(self.target_entities) - visible_entities:
            raise ValueError("target entities must be visible in target constraints")

        if self.is_clean:
            if (
                self.defect_class is not None
                or self.target_entities
                or self.target_constraint_ids
                or self.target_span is not None
            ):
                raise ValueError("clean narrative case cannot carry positive targets")
        else:
            if self.defect_class not in NARRATIVE_CLASSES:
                raise ValueError("positive narrative case requires a narrative defect class")
            if self.defect_class is not self.benchmark_family:
                raise ValueError(
                    "positive defect class must match its benchmark_family"
                )
            if self.target_span is None:
                raise ValueError("positive narrative case requires a target span")

        if self.target_span is not None:
            span = self.target_span
            if span.fact_id not in facts:
                raise ValueError("target span references an unknown fact")
            if span.end > len(self.dialogue) or self.dialogue[span.start : span.end] != span.text:
                raise ValueError("target span does not slice the exact dialogue text")

        expected_hash = content_sha256(self, exclude={"case_sha256"})
        if self.case_sha256 != expected_hash:
            raise ValueError("case_sha256 does not bind narrative case content")
        return self


def seal_case(**values: Any) -> NarrativeCase:
    return NarrativeCase.seal(**values)


def canonical_case_bytes(case: NarrativeCase) -> bytes:
    return (canonical_json(case.model_dump(mode="json")) + "\n").encode("utf-8")


def to_agent_input(case: NarrativeCase) -> DialogueNarrativeInput:
    return DialogueNarrativeInput(
        dialogue=case.dialogue,
        narrative_constraints=[
            NarrativeConstraintInput(
                constraint_id=item.constraint_id,
                entity_ids=list(item.entity_ids),
                statement=item.statement,
            )
            for item in case.constraints
        ],
    )


__all__ = [
    "NARRATIVE_CLASSES",
    "ActionFact",
    "CooperationFact",
    "HostilityFact",
    "MembershipFact",
    "NarrativeCase",
    "NarrativeConstraint",
    "NarrativeFact",
    "RevealFact",
    "RevealGateFact",
    "RoleHolderFact",
    "RoleLimitFact",
    "TargetSpan",
    "TraitFact",
    "canonical_case_bytes",
    "content_sha256",
    "seal_case",
    "to_agent_input",
]
