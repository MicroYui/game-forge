from __future__ import annotations

from gameforge.contracts.findings import TypedOp
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.ir import Entity, NodeType
from gameforge.spine.identity_normalization import (
    build_identity_alias_index,
    canonical_identity_token,
    normalize_typed_ops,
)
from gameforge.spine.ir.snapshot import Snapshot


EMPTY = Snapshot(entities={}, relations={})


def _op(op_id: str, target: str, *, entity_type: str, attrs: dict) -> TypedOp:
    return TypedOp(
        op_id=op_id,
        op="add_entity",
        target=target,
        new_value={"type": entity_type, "attrs": attrs},
    )


def test_lexical_identity_unifies_dot_underscore_case_width_and_space() -> None:
    assert canonical_identity_token("air.quality") == "air_quality"
    assert canonical_identity_token("AIR_quality") == "air_quality"
    assert canonical_identity_token("ａｉｒ／ｑｕａｌｉｔｙ") == "air_quality"
    assert canonical_identity_token(" air   quality ") == "air_quality"


def test_duplicate_entity_aliases_merge_equal_values_and_rewrite_relation_endpoints() -> None:
    operations = (
        _op("b", "Air_Quality", entity_type="STATUS_EFFECT", attrs={"Display.Name": "空气质量"}),
        _op("a", "air.quality", entity_type="STATUS_EFFECT", attrs={"display_name": "空气质量"}),
        _op("npc", "harbor keeper", entity_type="NPC", attrs={"name": "港务员"}),
        TypedOp(
            op_id="rel",
            op="add_relation",
            target="monitors air",
            new_value={
                "type": "REFERENCES",
                "src_id": "harbor keeper",
                "dst_id": "Air_Quality",
                "attrs": {"Air.Quality": "observed"},
            },
        ),
    )

    result = normalize_typed_ops(EMPTY, operations)

    assert result.blocking_conflicts == ()
    assert result.summary.input_operation_count == 4
    assert result.summary.output_operation_count == 3
    assert result.summary.auto_merge_count == 1
    entities = [op for op in result.ops if op.op == "add_entity"]
    assert [op.target for op in entities] == ["npc:harbor_keeper", "status_effect:air_quality"]
    effect = next(op for op in entities if op.target.startswith("status_effect:"))
    assert effect.new_value["attrs"] == {"display_name": "空气质量"}
    relation = next(op for op in result.ops if op.op == "add_relation")
    assert relation.target == "rel:monitors_air"
    assert relation.new_value["src_id"] == "npc:harbor_keeper"
    assert relation.new_value["dst_id"] == "status_effect:air_quality"
    assert relation.new_value["attrs"] == {"air_quality": "observed"}


def test_conflicting_alias_values_are_explicit_and_order_independent() -> None:
    left = _op("left", "air.quality", entity_type="STATUS_EFFECT", attrs={"value": 60})
    right = _op("right", "air_quality", entity_type="STATUS_EFFECT", attrs={"value": 80})

    first = normalize_typed_ops(EMPTY, (left, right))
    second = normalize_typed_ops(EMPTY, (right, left))

    assert first == second
    assert len(first.blocking_conflicts) == 1
    conflict = first.blocking_conflicts[0]
    assert conflict.code == "attribute_value_conflict"
    assert conflict.canonical_identity == "status_effect:air_quality.value"
    assert {candidate.value for candidate in conflict.candidates} == {60, 80}


def test_type_collision_and_dangling_relation_fail_closed() -> None:
    result = normalize_typed_ops(
        EMPTY,
        (
            _op("npc", "weather", entity_type="NPC", attrs={}),
            _op("effect", "weather", entity_type="STATUS_EFFECT", attrs={}),
            TypedOp(
                op_id="rel",
                op="add_relation",
                target="missing-ref",
                new_value={"type": "REFERENCES", "src_id": "weather", "dst_id": "ghost"},
            ),
        ),
    )

    assert {item.code for item in result.blocking_conflicts} == {
        "ambiguous_unqualified_alias",
        "dangling_relation_endpoint",
    }


def _snapshot_with(entity_id: str, *, entity_type: str, name: str) -> Snapshot:
    from gameforge.contracts.ir import Entity, NodeType

    return Snapshot(
        entities={
            entity_id: Entity(id=entity_id, type=NodeType(entity_type), attrs={"name": name})
        },
        relations={},
    )


def test_a_declared_alias_resolves_a_name_no_lexical_rule_could_reach() -> None:
    """岩王帝君 and 钟离 share no characters; only a human can say they are one.

    Once said, the decision is deterministic and no model is in the path.
    """

    base = _snapshot_with("npc:zhongli", entity_type="NPC", name="钟离")
    operations = (
        _op("a", "岩王帝君", entity_type="NPC", attrs={"title": "岩王帝君"}),
        TypedOp(
            op_id="rel",
            op="add_relation",
            target="rel:rex_lapis_guards_liyue",
            new_value={
                "type": "LOCATED_IN",
                "src_id": "岩王帝君",
                "dst_id": "npc:zhongli",
                "attrs": {},
            },
        ),
    )

    result = normalize_typed_ops(
        base,
        operations,
        declared_aliases={"岩王帝君": "npc:zhongli"},
    )

    assert result.blocking_conflicts == ()
    entity = next(op for op in result.ops if op.op == "add_entity")
    assert entity.target == "npc:zhongli"
    relation = next(op for op in result.ops if op.op == "add_relation")
    assert relation.new_value["src_id"] == "npc:zhongli"


def test_a_declared_alias_is_matched_by_its_written_form_not_its_bytes() -> None:
    base = _snapshot_with("npc:zhongli", entity_type="NPC", name="钟离")

    result = normalize_typed_ops(
        base,
        (_op("a", " Rex  Lapis ", entity_type="NPC", attrs={}),),
        declared_aliases={"rex.lapis": "npc:zhongli"},
    )

    assert result.blocking_conflicts == ()
    assert next(op for op in result.ops if op.op == "add_entity").target == "npc:zhongli"


def test_an_alias_pointing_at_nothing_fails_closed() -> None:
    # Declaring an alias for an entity that is not in the base would silently
    # invent one; the declaration has to name something that exists.
    import pytest

    from gameforge.contracts.errors import IntegrityViolation

    base = _snapshot_with("npc:zhongli", entity_type="NPC", name="钟离")

    with pytest.raises(IntegrityViolation, match="declared identity alias"):
        normalize_typed_ops(
            base,
            (_op("a", "岩王帝君", entity_type="NPC", attrs={}),),
            declared_aliases={"岩王帝君": "npc:morax"},
        )


def test_a_declared_alias_never_overrides_an_entity_that_already_exists() -> None:
    """Aliasing a live id onto another entity would rewrite real content."""

    import pytest

    from gameforge.contracts.errors import IntegrityViolation

    from gameforge.contracts.ir import Entity, NodeType

    base = Snapshot(
        entities={
            "npc:zhongli": Entity(id="npc:zhongli", type=NodeType("NPC"), attrs={}),
            "npc:morax": Entity(id="npc:morax", type=NodeType("NPC"), attrs={}),
        },
        relations={},
    )

    with pytest.raises(IntegrityViolation, match="already names an entity"):
        normalize_typed_ops(
            base,
            (_op("a", "npc:morax", entity_type="NPC", attrs={}),),
            declared_aliases={"npc:morax": "npc:zhongli"},
        )


def _zhongli() -> Snapshot:
    return Snapshot.from_entities_relations(
        [Entity(id="npc:zhongli", type=NodeType.NPC, attrs={"name": "钟离"})],
        [],
    )


def test_the_alias_index_carries_ids_canonical_spellings_and_declarations() -> None:
    index = build_identity_alias_index(
        _zhongli(),
        declared_aliases={"岩王帝君": "npc:zhongli"},
    )

    assert index.existing_ids == {"npc:zhongli"}
    assert index.exact_aliases["npc:zhongli"] == "npc:zhongli"
    assert index.exact_aliases["岩王帝君"] == "npc:zhongli"
    # A declared alias is an exact statement by a person. It must NOT become a
    # source of the unqualified ambiguity `_endpoint` reports.
    assert index.unqualified_aliases == {"zhongli": {"npc:zhongli"}}


def test_two_indexes_over_one_snapshot_do_not_share_mutable_state() -> None:
    """Normalization extends its index as it walks the proposal's add_entity ops.

    If two consumers shared one index, a later consumer's answer would depend on
    an earlier consumer's MODEL OUTPUT — the grounding a material chunk sees would
    silently depend on what the model said about the chunk before it. That is not
    replayable, so every caller gets its own.
    """

    snapshot = _zhongli()
    first = build_identity_alias_index(snapshot)
    second = build_identity_alias_index(snapshot)

    first.exact_aliases["invented"] = "npc:zhongli"
    first.unqualified_aliases.setdefault("invented", set()).add("npc:zhongli")
    first.existing_ids.add("npc:invented")

    assert "invented" not in second.exact_aliases
    assert "invented" not in second.unqualified_aliases
    assert "npc:invented" not in second.existing_ids


def test_the_index_rejects_a_declaration_that_names_nothing() -> None:
    try:
        build_identity_alias_index(_zhongli(), declared_aliases={"岩王帝君": "npc:morax"})
    except IntegrityViolation as error:
        assert "does not have" in str(error)
    else:
        raise AssertionError("a declaration pointing at no entity must fail closed")


def test_the_index_rejects_a_declaration_shadowing_an_entity_of_its_own() -> None:
    snapshot = Snapshot.from_entities_relations(
        [
            Entity(id="npc:zhongli", type=NodeType.NPC, attrs={}),
            Entity(id="npc:morax", type=NodeType.NPC, attrs={}),
        ],
        [],
    )

    try:
        build_identity_alias_index(snapshot, declared_aliases={"npc:morax": "npc:zhongli"})
    except IntegrityViolation as error:
        assert "already names an entity" in str(error)
    else:
        raise AssertionError("a declaration shadowing a real entity must fail closed")
