"""Compatibility coverage for legacy `inject()` narrative entrypoints."""
from __future__ import annotations

from gameforge.bench.inject import inject
from gameforge.bench.narrative.generator import ANSWER_MARKER
from gameforge.bench.taxonomy import Bucket, CLASS_META, DefectClass
from gameforge.contracts.agent_io import DialogueNarrativeInput

from tests.bench.testbases import clean_base

_NARRATIVE = [
    DefectClass.character_violation,
    DefectClass.spoiler,
    DefectClass.faction_violation,
    DefectClass.uniqueness_violation,
]


def test_narrative_injectors_produce_grounded_marker_free_dialogue():
    for dc in _NARRATIVE:
        s = inject(clean_base(), dc, seed=1)
        assert isinstance(s.dialogue, DialogueNarrativeInput), dc
        assert s.dialogue.narrative_constraints, dc
        assert s.dialogue.narrative_constraint_ids == [], dc
        assert s.dialogue.dialogue.strip(), dc
        assert not ANSWER_MARKER.search(s.dialogue.dialogue), dc
        assert all(
            not ANSWER_MARKER.search(item.statement)
            for item in s.dialogue.narrative_constraints
        ), dc
        assert s.ground_truth.defect_class is dc
        assert s.ground_truth.injected_entities
        assert CLASS_META[dc].bucket is Bucket.llm_assisted


def test_narrative_injectors_seeded_reproducible():
    for dc in _NARRATIVE:
        a = inject(clean_base(), dc, seed=5)
        b = inject(clean_base(), dc, seed=5)
        c = inject(clean_base(), dc, seed=6)
        assert a.dialogue.dialogue == b.dialogue.dialogue, dc  # reproducible text
        assert a.dialogue.dialogue != c.dialogue.dialogue, dc  # varies by seed
