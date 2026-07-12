from __future__ import annotations

import json

import pytest

from gameforge.bench.narrative.contracts import NARRATIVE_CLASSES, to_agent_input
from gameforge.bench.narrative.generator import (
    ANSWER_MARKER,
    GENERATOR_VERSION,
    generate_case,
)
from gameforge.bench.narrative.oracle import evaluate_facts
from gameforge.bench.narrative.renderer import RENDERER_VERSION
from gameforge.bench.taxonomy import DefectClass


@pytest.mark.parametrize("defect_class", NARRATIVE_CLASSES)
def test_positive_generator_binds_one_oracle_violation_and_exact_target(defect_class):
    case = generate_case(
        split="verification",
        defect_class=defect_class,
        is_clean=False,
        seed=91,
        case_id=f"positive-{defect_class.value}",
    )

    violations = evaluate_facts(case.facts)
    assert len(violations) == 1
    assert violations[0].defect_class is defect_class
    assert case.benchmark_family is defect_class
    assert case.defect_class is defect_class
    assert case.target_entities == violations[0].target_entity_ids
    assert case.target_span is not None
    assert case.dialogue[case.target_span.start : case.target_span.end] == case.target_span.text
    assert case.target_span.fact_id in violations[0].causing_fact_ids


@pytest.mark.parametrize("defect_class", NARRATIVE_CLASSES)
def test_clean_generator_has_zero_oracle_violations_and_no_target(defect_class):
    case = generate_case(
        split="verification",
        defect_class=defect_class,
        is_clean=True,
        seed=92,
        case_id=f"clean-{defect_class.value}",
    )

    assert evaluate_facts(case.facts) == ()
    assert case.benchmark_family is defect_class
    assert case.is_clean is True
    assert case.defect_class is None
    assert case.target_entities == ()
    assert case.target_constraint_ids == ()
    assert case.target_span is None


def test_generator_is_seeded_reproducible_and_seed_sensitive():
    args = {
        "split": "development",
        "defect_class": DefectClass.character_violation,
        "is_clean": False,
        "case_id": "nd-repro",
    }
    first = generate_case(seed=17, **args)
    second = generate_case(seed=17, **args)
    changed = generate_case(seed=18, **args)

    assert first == second
    assert first.case_sha256 == second.case_sha256
    assert first.dialogue != changed.dialogue
    assert first.case_sha256 != changed.case_sha256


@pytest.mark.parametrize("defect_class", NARRATIVE_CLASSES)
def test_model_payload_and_formal_text_hide_every_answer_field(defect_class):
    case = generate_case(
        split="verification",
        defect_class=defect_class,
        is_clean=False,
        seed=101,
        case_id="opaque-case-id",
    )
    agent_input = to_agent_input(case)
    payload = agent_input.model_dump_json()

    assert case.case_id not in payload
    assert defect_class.value not in payload
    assert "target_span" not in payload
    assert "is_clean" not in payload
    assert "source_fact_ids" not in payload
    assert not ANSWER_MARKER.search(agent_input.dialogue)
    assert all(
        not ANSWER_MARKER.search(item.statement)
        for item in agent_input.narrative_constraints
    )


def test_generator_produces_broad_surface_diversity_without_case_ids():
    cases = [
        generate_case(
            split="verification",
            defect_class=DefectClass.spoiler,
            is_clean=False,
            seed=seed,
            case_id=f"hidden-{seed}",
        )
        for seed in range(64)
    ]
    visible_payloads = {
        json.dumps(to_agent_input(case).model_dump(), sort_keys=True)
        for case in cases
    }

    assert len(visible_payloads) >= 60
    assert all(
        case.case_id not in to_agent_input(case).model_dump_json()
        for case in cases
    )
    assert len({case.target_entities for case in cases}) >= 12


def test_generator_versions_are_explicit():
    case = generate_case(
        split="development",
        defect_class=DefectClass.uniqueness_violation,
        is_clean=False,
        seed=4,
        case_id="version-check",
    )
    assert GENERATOR_VERSION == "narrative-generator@2"
    assert case.generator_version == GENERATOR_VERSION
    assert case.renderer_version == RENDERER_VERSION


@pytest.mark.parametrize("is_clean", [False, True])
def test_spoiler_constraints_expose_the_complete_story_stage_order(is_clean):
    case = generate_case(
        split="development",
        defect_class=DefectClass.spoiler,
        is_clean=is_clean,
        seed=313,
        case_id=f"stage-order-{is_clean}",
    )
    reveal_constraint = next(
        item
        for item in case.constraints
        if any(entity_id.startswith("secret:") for entity_id in item.entity_ids)
    )

    assert "Story stages progress in this order:" in reveal_constraint.statement
    order = reveal_constraint.statement.split("Story stages progress in this order:", 1)[1]
    assert order.count(" -> ") == 4
    assert not ANSWER_MARKER.search(reveal_constraint.statement)
