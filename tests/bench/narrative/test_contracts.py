from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from gameforge.bench.narrative.contracts import (
    ActionFact,
    NarrativeCase,
    NarrativeConstraint,
    TargetSpan,
    TraitFact,
    canonical_case_bytes,
    seal_case,
    to_agent_input,
)
from gameforge.bench.taxonomy import DefectClass


def _positive_values() -> dict[str, object]:
    dialogue = "Qi sold the entrusted route to the raiders."
    return {
        "schema_version": "narrative-case@1",
        "case_id": "nv-000001",
        "generator_version": "narrative-generator@1",
        "renderer_version": "narrative-renderer@1",
        "oracle_version": "narrative-oracle@1",
        "seed": 1,
        "split": "verification",
        "facts": (
            TraitFact(
                fact_id="fact:trait",
                entity_id="npc:qi",
                trait_id="keeps_entrusted_secrets",
            ),
            ActionFact(
                fact_id="fact:action",
                entity_id="npc:qi",
                action_id="sold_entrusted_route",
                violates_trait_fact_id="fact:trait",
            ),
        ),
        "constraints": (
            NarrativeConstraint(
                constraint_id="C-qi-trust",
                entity_ids=("npc:qi",),
                statement="Qi never betrays a route entrusted by an ally.",
                source_fact_ids=("fact:trait",),
            ),
        ),
        "dialogue": dialogue,
        "is_clean": False,
        "defect_class": DefectClass.character_violation,
        "target_entities": ("npc:qi",),
        "target_constraint_ids": ("C-qi-trust",),
        "target_span": TargetSpan(
            start=0,
            end=len(dialogue),
            text=dialogue,
            fact_id="fact:action",
        ),
    }


def test_case_seal_binds_canonical_content_and_round_trips():
    case = seal_case(**_positive_values())

    assert len(case.case_sha256) == 64
    restored = NarrativeCase.model_validate_json(case.model_dump_json())
    assert restored == case
    assert canonical_case_bytes(case).endswith(b"\n")
    assert canonical_case_bytes(restored) == canonical_case_bytes(case)


def test_case_hash_rejects_tampering():
    case = seal_case(**_positive_values())
    payload = case.model_dump(mode="json")
    payload["generator_version"] = "narrative-generator@tampered"

    with pytest.raises(ValidationError, match="case_sha256"):
        NarrativeCase.model_validate(payload)


def test_target_span_must_slice_exact_dialogue_and_reference_a_fact():
    values = _positive_values()
    values["target_span"] = TargetSpan(
        start=0,
        end=2,
        text="wrong",
        fact_id="fact:action",
    )
    with pytest.raises(ValidationError, match="target span"):
        seal_case(**values)

    values = _positive_values()
    values["target_span"] = TargetSpan(
        start=0,
        end=2,
        text="Qi",
        fact_id="fact:missing",
    )
    with pytest.raises(ValidationError, match="fact"):
        seal_case(**values)


def test_clean_case_cannot_carry_positive_targets():
    values = _positive_values()
    values.update(
        is_clean=True,
        defect_class=None,
        target_entities=(),
        target_constraint_ids=(),
        target_span=None,
    )
    clean = seal_case(**values)
    assert clean.is_clean is True

    values["defect_class"] = DefectClass.character_violation
    with pytest.raises(ValidationError, match="clean"):
        seal_case(**values)


def test_positive_case_requires_narrative_class_and_sorted_targets():
    values = _positive_values()
    values["defect_class"] = DefectClass.dead_quest
    with pytest.raises(ValidationError, match="narrative"):
        seal_case(**values)

    values = _positive_values()
    values["target_entities"] = ("npc:z", "npc:a")
    with pytest.raises(ValidationError, match="sorted"):
        seal_case(**values)


def test_fact_constraint_ids_and_references_are_strict():
    values = _positive_values()
    values["facts"] = (values["facts"][0], values["facts"][0])  # type: ignore[index]
    with pytest.raises(ValidationError, match="duplicate fact"):
        seal_case(**values)

    values = _positive_values()
    constraint = values["constraints"][0]  # type: ignore[index]
    values["constraints"] = (constraint, constraint)
    with pytest.raises(ValidationError, match="duplicate constraint"):
        seal_case(**values)

    with pytest.raises(ValidationError, match="extra"):
        TraitFact.model_validate(
            {
                "fact_id": "fact:trait",
                "entity_id": "npc:qi",
                "trait_id": "loyal",
                "answer": "character_violation",
            }
        )


def test_agent_input_contains_only_visible_constraints_and_dialogue():
    case = seal_case(**_positive_values())
    agent_input = to_agent_input(case)
    payload = json.loads(agent_input.model_dump_json())

    assert payload == {
        "dialogue": case.dialogue,
        "narrative_constraints": [
            {
                "constraint_id": "C-qi-trust",
                "entity_ids": ["npc:qi"],
                "statement": "Qi never betrays a route entrusted by an ally.",
            }
        ],
        "narrative_constraint_ids": [],
    }
    serialized = agent_input.model_dump_json()
    assert case.case_id not in serialized
    assert case.defect_class.value not in serialized
    assert "target_span" not in serialized
    assert "source_fact_ids" not in serialized
