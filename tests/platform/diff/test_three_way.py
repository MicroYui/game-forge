from __future__ import annotations

import copy

import pytest

from gameforge.contracts.diff import (
    CollectionIdentityV1,
    ConflictResolution,
    ThreeWayMergePolicyV1,
    compute_merge_policy_digest,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.platform.diff import (
    ThreeWayMergePlan,
    compute_three_way_merge,
    resolve_three_way_merge,
)


def _policy(*identities: tuple[str, str]) -> ThreeWayMergePolicyV1:
    declarations = tuple(
        CollectionIdentityV1(path=path, identity_key=identity_key)
        for path, identity_key in sorted(identities)
    )
    return ThreeWayMergePolicyV1(
        policy_version="merge-policy:test@1",
        collection_identities=declarations,
        policy_digest=compute_merge_policy_digest(
            "merge-policy:test@1",
            declarations,
        ),
    )


def test_mapping_merge_combines_disjoint_changes_at_stable_escaped_paths() -> None:
    base = {
        "entities": {
            "quest/intro~draft": {
                "attrs": {"reward": 10, "title": "Intro"},
            }
        }
    }
    current = copy.deepcopy(base)
    current["entities"]["quest/intro~draft"]["attrs"]["title"] = "Current"
    proposed = copy.deepcopy(base)
    proposed["entities"]["quest/intro~draft"]["attrs"]["reward"] = 20

    plan = compute_three_way_merge(base, current, proposed, _policy())

    assert isinstance(plan, ThreeWayMergePlan)
    assert plan.merged == {
        "entities": {
            "quest/intro~draft": {
                "attrs": {"reward": 20, "title": "Current"},
            }
        }
    }
    assert plan.conflicts == ()
    assert len(plan.non_conflicting_ops_digest) == 64


def test_conflict_preserves_missing_null_and_scalar_types() -> None:
    plan = compute_three_way_merge(
        {"missing_or_null": 1, "typed": 1},
        {"typed": False},
        {"missing_or_null": None, "typed": 0},
        _policy(),
    )

    assert [conflict.path for conflict in plan.conflicts] == [
        "/missing_or_null",
        "/typed",
    ]
    first, second = plan.conflicts
    assert first.base.value == 1
    assert first.current.presence == "missing"
    assert first.proposed.presence == "present"
    assert first.proposed.value is None
    assert second.current.value is False
    assert type(second.current.value) is bool
    assert second.proposed.value == 0
    assert type(second.proposed.value) is int
    assert plan.merged == {"typed": False}


def test_conflict_ids_and_output_ignore_recursive_mapping_insertion_order() -> None:
    base_a = {"z": {"b": 1, "a": 1}, "a": 0}
    current_a = {"z": {"b": 2, "a": 2}, "a": 0}
    proposed_a = {"z": {"b": 3, "a": 3}, "a": 0}
    base_b = {"a": 0, "z": {"a": 1, "b": 1}}
    current_b = {"a": 0, "z": {"a": 2, "b": 2}}
    proposed_b = {"a": 0, "z": {"a": 3, "b": 3}}

    first = compute_three_way_merge(base_a, current_a, proposed_a, _policy())
    second = compute_three_way_merge(base_b, current_b, proposed_b, _policy())

    assert first == second
    assert [item.path for item in first.conflicts] == ["/z/a", "/z/b"]
    assert all(item.id.startswith("conflict:") for item in first.conflicts)


def test_conflict_path_uses_stable_rfc6901_escaping() -> None:
    plan = compute_three_way_merge(
        {"a/b~c": {"value": 1}},
        {"a/b~c": {"value": 2}},
        {"a/b~c": {"value": 3}},
        _policy(),
    )

    assert [item.path for item in plan.conflicts] == ["/a~1b~0c/value"]


def test_equal_concurrent_change_needs_no_resolution() -> None:
    plan = compute_three_way_merge(
        {"value": 1},
        {"value": 2},
        {"value": 2},
        _policy(),
    )

    assert plan.merged == {"value": 2}
    assert plan.conflicts == ()
    assert resolve_three_way_merge(
        {"value": 1},
        {"value": 2},
        {"value": 2},
        _policy(),
        (),
    ) == {"value": 2}


def test_ordered_equal_length_arrays_merge_positionally() -> None:
    plan = compute_three_way_merge(
        {"values": [{"v": 1}, {"v": 2}]},
        {"values": [{"v": 10}, {"v": 2}]},
        {"values": [{"v": 1}, {"v": 20}]},
        _policy(),
    )

    assert plan.merged == {"values": [{"v": 10}, {"v": 20}]}
    assert plan.conflicts == ()


def test_ordered_array_concurrent_resize_conflicts_at_array_root() -> None:
    base = {"values": [1, 2]}
    current = {"values": [0, 1, 2]}
    proposed = {"values": [1, 2, 3]}

    plan = compute_three_way_merge(base, current, proposed, _policy())

    assert plan.merged == current
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].path == "/values"
    assert plan.conflicts[0].kind == "concurrent_array_resize"


def test_unilateral_ordered_array_resize_is_non_conflicting() -> None:
    base = {"values": [1, 2]}
    proposed = {"values": [1, 2, 3]}

    plan = compute_three_way_merge(base, base, proposed, _policy())

    assert plan.merged == proposed
    assert plan.conflicts == ()


def test_declared_collection_merges_by_identity_across_length_and_order_changes() -> None:
    policy = _policy(("/items", "id"))
    base = {
        "items": [
            {"id": "b", "value": 2},
            {"id": "a", "value": 1},
        ]
    }
    current = {
        "items": [
            {"id": "c", "value": 30},
            {"id": "a", "value": 10},
            {"id": "b", "value": 2},
        ]
    }
    proposed = {
        "items": [
            {"id": "b", "value": 20},
            {"id": "a", "value": 1},
        ]
    }

    plan = compute_three_way_merge(base, current, proposed, policy)

    assert plan.merged == {
        "items": [
            {"id": "a", "value": 10},
            {"id": "b", "value": 20},
            {"id": "c", "value": 30},
        ]
    }
    assert plan.conflicts == ()


def test_declared_collection_conflict_path_is_stable_by_identity_order() -> None:
    policy = _policy(("/items", "id"))
    base = {"items": [{"id": "b", "value": 1}, {"id": "a", "value": 1}]}
    current = {"items": [{"id": "a", "value": 2}, {"id": "b", "value": 1}]}
    proposed = {"items": [{"id": "b", "value": 1}, {"id": "a", "value": 3}]}

    plan = compute_three_way_merge(base, current, proposed, policy)

    assert [item.path for item in plan.conflicts] == ["/items/0/value"]
    assert plan.merged == {
        "items": [{"id": "a", "value": 2}, {"id": "b", "value": 1}]
    }


def test_nested_declared_collection_resolves_after_typed_outer_identity_order() -> None:
    policy = _policy(("/groups", "id"), ("/groups/0/items", "id"))
    base_groups = [
        {"id": 1, "items": [{"id": "y", "value": 2}, {"id": "x", "value": 1}]},
        {"id": "1", "items": {"opaque": "not-a-declared-collection"}},
    ]
    current_groups = [
        {"id": 1, "items": [{"id": "x", "value": 10}, {"id": "y", "value": 2}]},
        {"id": "1", "items": {"opaque": "not-a-declared-collection"}},
    ]
    proposed_groups = [
        {"id": 1, "items": [{"id": "y", "value": 20}, {"id": "x", "value": 1}]},
        {"id": "1", "items": {"opaque": "not-a-declared-collection"}},
    ]

    canonical = compute_three_way_merge(
        {"groups": base_groups},
        {"groups": current_groups},
        {"groups": proposed_groups},
        policy,
    )
    permuted = compute_three_way_merge(
        {"groups": list(reversed(base_groups))},
        {"groups": list(reversed(current_groups))},
        {"groups": list(reversed(proposed_groups))},
        policy,
    )

    assert permuted == canonical
    assert canonical.conflicts == ()
    assert canonical.merged == {
        "groups": [
            {
                "id": 1,
                "items": [
                    {"id": "x", "value": 10},
                    {"id": "y", "value": 20},
                ],
            },
            {"id": "1", "items": {"opaque": "not-a-declared-collection"}},
        ]
    }


@pytest.mark.parametrize(
    "items",
    [
        [{"value": 1}],
        [{"id": "same"}, {"id": "same"}],
        [{"id": None}],
    ],
)
def test_declared_collection_fails_closed_for_invalid_identity(items: list[dict]) -> None:
    with pytest.raises(IntegrityViolation):
        compute_three_way_merge(
            {"items": items},
            {"items": items},
            {"items": items},
            _policy(("/items", "id")),
        )


@pytest.mark.parametrize(
    ("choice", "custom_value", "expected"),
    [
        ("keep_current", None, {"value": 2}),
        ("take_proposed", None, {"value": 3}),
        ("custom", None, {"value": None}),
    ],
)
def test_resolution_applies_each_closed_choice(
    choice: str,
    custom_value: object,
    expected: dict,
) -> None:
    base = {"value": 1}
    current = {"value": 2}
    proposed = {"value": 3}
    policy = _policy()
    (conflict,) = compute_three_way_merge(base, current, proposed, policy).conflicts
    kwargs = (
        {"custom_value": custom_value}
        if choice == "custom"
        else {}
    )
    resolution = ConflictResolution(
        conflict_id=conflict.id,
        choice=choice,
        **kwargs,
    )

    assert resolve_three_way_merge(
        base,
        current,
        proposed,
        policy,
        (resolution,),
    ) == expected


def test_resolution_requires_complete_unique_exact_conflict_set() -> None:
    base = {"a": 1, "b": 1}
    current = {"a": 2, "b": 2}
    proposed = {"a": 3, "b": 3}
    policy = _policy()
    plan = compute_three_way_merge(base, current, proposed, policy)
    first = ConflictResolution(
        conflict_id=plan.conflicts[0].id,
        choice="keep_current",
    )

    with pytest.raises(ValueError, match="cover every conflict"):
        resolve_three_way_merge(base, current, proposed, policy, (first,))
    with pytest.raises(ValueError, match="duplicate resolution"):
        resolve_three_way_merge(base, current, proposed, policy, (first, first))
    with pytest.raises(ValueError, match="extra"):
        resolve_three_way_merge(
            base,
            current,
            proposed,
            policy,
            (
                first,
                ConflictResolution(
                    conflict_id=plan.conflicts[1].id,
                    choice="keep_current",
                ),
                ConflictResolution(
                    conflict_id="conflict:unknown",
                    choice="keep_current",
                ),
            ),
        )


def test_collection_resolution_cannot_change_identity() -> None:
    base = {"items": []}
    current = {"items": [{"id": "a", "value": 2}]}
    proposed = {"items": [{"id": "a", "value": 3}]}
    policy = _policy(("/items", "id"))
    (conflict,) = compute_three_way_merge(base, current, proposed, policy).conflicts

    with pytest.raises(ValueError, match="identity"):
        resolve_three_way_merge(
            base,
            current,
            proposed,
            policy,
            (
                ConflictResolution(
                    conflict_id=conflict.id,
                    choice="custom",
                    custom_value={"id": "other", "value": 4},
                ),
            ),
        )


def test_non_conflicting_digest_is_type_and_presence_sensitive() -> None:
    policy = _policy()
    missing_to_null = compute_three_way_merge({}, {}, {"value": None}, policy)
    missing_to_zero = compute_three_way_merge({}, {}, {"value": 0}, policy)
    missing_to_float = compute_three_way_merge({}, {}, {"value": 0.0}, policy)

    assert len(
        {
            missing_to_null.non_conflicting_ops_digest,
            missing_to_zero.non_conflicting_ops_digest,
            missing_to_float.non_conflicting_ops_digest,
        }
    ) == 3


def test_plan_does_not_alias_mutable_inputs() -> None:
    base = {"nested": {"value": 1}}
    current = copy.deepcopy(base)
    proposed = {"nested": {"value": 2}}

    plan = compute_three_way_merge(base, current, proposed, _policy())
    proposed["nested"]["value"] = 999

    assert plan.merged == {"nested": {"value": 2}}
