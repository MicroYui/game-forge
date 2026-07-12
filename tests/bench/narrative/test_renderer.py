from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from gameforge.bench.narrative import renderer
from gameforge.bench.narrative.contracts import (
    ActionFact,
    CooperationFact,
    RevealFact,
    RoleHolderFact,
)
from gameforge.bench.narrative.renderer import (
    RENDERER_VERSION,
    NarrativeRenderContext,
    render_facts,
)

_ANSWER_MARKER = re.compile(
    r"(?i)(TRAIT\s*:|SPOILER\s*:|CONTRADICTION\s*:|UNIQUE-ROLE\s*:|"
    r"character_violation|faction_violation|uniqueness_violation|\bdefect\s+class\b)"
)


def _context() -> NarrativeRenderContext:
    return NarrativeRenderContext(
        entity_names=(
            ("npc:qi", "Qi"),
            ("npc:mara", "Mara"),
            ("npc:raider", "Rook"),
        ),
        concept_names=(
            ("secret:warden", "the Warden's identity"),
            ("role:warden", "the Warden"),
        ),
        locations=("archive hall", "northern outpost"),
        stage_names=("arrival", "first watch", "archive opening", "finale"),
    )


def _facts():
    return (
        ActionFact(
            fact_id="fact:action",
            entity_id="npc:qi",
            action_id="sold_entrusted_route",
        ),
        RevealFact(
            fact_id="fact:reveal",
            speaker_id="npc:mara",
            secret_id="secret:warden",
            stage=1,
        ),
        CooperationFact(
            fact_id="fact:cooperation",
            left_entity_id="npc:qi",
            right_entity_id="npc:raider",
        ),
        RoleHolderFact(
            fact_id="fact:holder",
            role_id="role:warden",
            entity_id="npc:mara",
        ),
    )


def test_renderer_returns_exact_source_spans_without_answer_markers():
    rendered = render_facts(_facts(), _context(), render_seed=11)

    assert set(rendered.spans_by_fact_id) == {
        "fact:action",
        "fact:reveal",
        "fact:cooperation",
        "fact:holder",
    }
    for fact_id, span in rendered.spans_by_fact_id.items():
        assert span.fact_id == fact_id
        assert rendered.dialogue[span.start : span.end] == span.text
        assert span.text.endswith((".", "!", "?"))
    assert not _ANSWER_MARKER.search(rendered.dialogue)


def test_renderer_is_seeded_reproducible_and_varies_surface_form():
    same_a = render_facts(_facts(), _context(), render_seed=17)
    same_b = render_facts(_facts(), _context(), render_seed=17)
    variants = {
        render_facts(_facts(), _context(), render_seed=seed).dialogue
        for seed in range(12)
    }

    assert same_a == same_b
    assert len(variants) >= 6


@given(st.integers(min_value=0, max_value=2**32 - 1))
@settings(max_examples=100)
def test_every_rendered_span_round_trips_for_arbitrary_seed(seed):
    rendered = render_facts(_facts(), _context(), render_seed=seed)
    for span in rendered.spans_by_fact_id.values():
        assert rendered.dialogue[span.start : span.end] == span.text


def test_renderer_rejects_unknown_semantics_or_missing_display_names():
    unknown = ActionFact(
        fact_id="fact:unknown",
        entity_id="npc:qi",
        action_id="unknown_action",
    )
    with pytest.raises(ValueError, match="action"):
        render_facts((unknown,), _context(), render_seed=0)

    missing_name = ActionFact(
        fact_id="fact:missing-name",
        entity_id="npc:absent",
        action_id="sold_entrusted_route",
    )
    with pytest.raises(ValueError, match="display name"):
        render_facts((missing_name,), _context(), render_seed=0)


def test_renderer_context_rejects_duplicate_ids_and_short_stage_table():
    with pytest.raises(ValueError, match="duplicate"):
        NarrativeRenderContext(
            entity_names=(("npc:qi", "Qi"), ("npc:qi", "Other Qi")),
            concept_names=(("role:warden", "the Warden"),),
            locations=("outpost",),
            stage_names=("arrival", "finale"),
        )
    with pytest.raises(ValueError, match="stage"):
        NarrativeRenderContext(
            entity_names=(("npc:qi", "Qi"),),
            concept_names=(("role:warden", "the Warden"),),
            locations=("outpost",),
            stage_names=("arrival",),
        )


def test_renderer_is_versioned_and_cannot_import_oracle_or_agents():
    assert RENDERER_VERSION == "narrative-renderer@1"
    tree = ast.parse(Path(renderer.__file__).read_text(encoding="utf-8"))
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
    assert "gameforge.bench.narrative.oracle" not in imports
    assert not any(name.startswith("gameforge.agents") for name in imports)
