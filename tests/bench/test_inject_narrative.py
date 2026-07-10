"""M3a Task 4: property tests for the 4 narrative defect injectors.

Narrative defects are scored in the llm-assisted bucket (design §2) — the real
judgment is M2's perspective-diverse Consistency quorum. These tests verify the
INJECTION itself (the seeded DialogueNarrativeInput actually carries the
contradiction and names the narrative constraint) by direct string/structural
inspection, NOT by running the consistency checker — keeping the injector
independent of the oracle (design §8).
"""
from __future__ import annotations

from gameforge.bench.inject import inject
from gameforge.bench.taxonomy import Bucket, CLASS_META, DefectClass
from gameforge.contracts.agent_io import DialogueNarrativeInput

from tests.bench.testbases import clean_base

_NARRATIVE = [
    DefectClass.character_violation,
    DefectClass.spoiler,
    DefectClass.faction_violation,
    DefectClass.uniqueness_violation,
]


def test_narrative_injectors_produce_dialogue_naming_the_constraint():
    for dc in _NARRATIVE:
        s = inject(clean_base(), dc, seed=1)
        assert isinstance(s.dialogue, DialogueNarrativeInput), dc
        assert s.dialogue.narrative_constraint_ids, dc  # references a narrative constraint
        assert s.dialogue.dialogue.strip(), dc          # non-empty dialogue text
        assert s.ground_truth.defect_class is dc
        assert s.ground_truth.injected_entities
        assert CLASS_META[dc].bucket is Bucket.llm_assisted


def test_character_violation_dialogue_contradicts_a_stated_trait():
    s = inject(clean_base(), DefectClass.character_violation, seed=1)
    text = s.dialogue.dialogue.lower()
    # both the declared trait AND the contradicting action are present in the
    # dialogue — that co-occurrence IS the injected inconsistency
    assert "trait:" in text and "contradiction:" in text


def test_spoiler_dialogue_reveals_a_gated_reveal_early():
    s = inject(clean_base(), DefectClass.spoiler, seed=1)
    text = s.dialogue.dialogue.lower()
    assert "reveal:" in text and "spoiler:" in text


def test_faction_violation_dialogue_allies_declared_enemies():
    s = inject(clean_base(), DefectClass.faction_violation, seed=1)
    text = s.dialogue.dialogue.lower()
    assert "enemies:" in text and "alliance:" in text


def test_uniqueness_violation_dialogue_duplicates_a_unique_role():
    s = inject(clean_base(), DefectClass.uniqueness_violation, seed=1)
    text = s.dialogue.dialogue.lower()
    assert text.count("unique-role:") >= 2  # two claimants to a one-holder role


def test_narrative_injectors_seeded_reproducible():
    for dc in _NARRATIVE:
        a = inject(clean_base(), dc, seed=5)
        b = inject(clean_base(), dc, seed=5)
        c = inject(clean_base(), dc, seed=6)
        assert a.dialogue.dialogue == b.dialogue.dialogue, dc  # reproducible text
        assert a.dialogue.dialogue != c.dialogue.dialogue, dc  # varies by seed
