from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.contracts.agent_io import (
    ConsistencyHint,
    DialogueNarrativeInput,
    NarrativeConstraintInput,
)


def _hint(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "defect_class": "spoiler",
        "entity_ids": ["npc:qi", "secret:white-heron"],
        "constraint_ids": ["C-reveal-white-heron"],
        "span": "Qi named the masked envoy before the archive opened.",
        "rationale": "The line reveals the gated identity before its unlock.",
    }
    value.update(changes)
    return value


def test_current_consistency_hint_requires_grounded_structured_identity():
    hint = ConsistencyHint.model_validate(_hint())

    assert hint.model_dump() == {
        **_hint(),
        "is_suggestion": True,
    }
    assert "issue" not in hint.model_dump()


def test_suggestion_flag_cannot_be_promoted_to_authoritative():
    with pytest.raises(ValidationError, match="is_suggestion"):
        ConsistencyHint.model_validate(_hint(is_suggestion=False))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("defect_class", "narrative_inconsistency"),
        ("entity_ids", []),
        ("entity_ids", ["npc:qi", "npc:qi"]),
        ("entity_ids", [" "]),
        ("constraint_ids", []),
        ("constraint_ids", ["C-reveal", "C-reveal"]),
        ("constraint_ids", [""]),
        ("span", "  "),
        ("rationale", ""),
    ],
)
def test_consistency_hint_rejects_unsupported_or_ungrounded_fields(field, value):
    with pytest.raises(ValidationError):
        ConsistencyHint.model_validate(_hint(**{field: value}))


def test_consistency_hint_rejects_extra_fields():
    with pytest.raises(ValidationError, match="extra"):
        ConsistencyHint.model_validate(_hint(issue="legacy free-text identity"))


def test_dialogue_input_carries_statements_without_hidden_ground_truth():
    value = DialogueNarrativeInput(
        dialogue="Qi names the envoy before the archive opens.",
        narrative_constraints=[
            NarrativeConstraintInput(
                constraint_id="C-reveal",
                entity_ids=["npc:qi", "secret:envoy"],
                statement="The envoy's identity may be named only after the archive opens.",
            )
        ],
    )

    payload = value.model_dump()
    assert set(payload) == {
        "dialogue",
        "narrative_constraints",
        "narrative_constraint_ids",
    }
    assert payload["narrative_constraint_ids"] == []
    assert "defect_class" not in repr(payload)


def test_legacy_dialogue_input_remains_constructible():
    value = DialogueNarrativeInput(
        dialogue="Legacy input\n",
        narrative_constraint_ids=["C-legacy"],
    )
    assert value.narrative_constraints == []
    assert value.dialogue == "Legacy input\n"


def test_structured_constraint_requires_grounded_entities():
    with pytest.raises(ValidationError):
        NarrativeConstraintInput(
            constraint_id="C-one",
            entity_ids=[],
            statement="A rule.",
        )


def test_dialogue_input_rejects_duplicate_or_mixed_constraint_channels():
    constraint = NarrativeConstraintInput(
        constraint_id="C-reveal",
        entity_ids=["npc:qi"],
        statement="Qi may reveal the identity only after the archive opens.",
    )
    with pytest.raises(ValidationError, match="duplicate"):
        DialogueNarrativeInput(
            dialogue="A line.",
            narrative_constraints=[constraint, constraint],
        )
    with pytest.raises(ValidationError, match="duplicate"):
        DialogueNarrativeInput(
            dialogue="A line.",
            narrative_constraint_ids=["C-reveal", "C-reveal"],
        )
    with pytest.raises(ValidationError, match="cannot both"):
        DialogueNarrativeInput(
            dialogue="A line.",
            narrative_constraints=[constraint],
            narrative_constraint_ids=["C-legacy"],
        )


def test_dialogue_and_constraint_models_reject_blank_or_extra_content():
    with pytest.raises(ValidationError):
        DialogueNarrativeInput(dialogue="  ")
    with pytest.raises(ValidationError):
        NarrativeConstraintInput(
            constraint_id=" ",
            entity_ids=["npc:qi"],
            statement="A rule.",
        )
    with pytest.raises(ValidationError, match="extra"):
        NarrativeConstraintInput.model_validate(
            {
                "constraint_id": "C-one",
                "entity_ids": ["npc:qi"],
                "statement": "A rule.",
                "defect_class": "spoiler",
            }
        )
