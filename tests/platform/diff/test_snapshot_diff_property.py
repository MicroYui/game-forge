from __future__ import annotations

from typing import Any

from hypothesis import given, settings, strategies as st

from gameforge.platform.diff import iter_snapshot_diff_entries


MISSING = object()


json_scalars = st.none() | st.booleans() | st.integers() | st.text()
json_values = st.recursive(
    json_scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=20,
)


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _state(value: Any) -> dict[str, Any]:
    if value is MISSING:
        return {"presence": "missing"}
    return {"presence": "present", "value": value}


def _same_scalar(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _naive_diff(base: Any, target: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(base, dict) and isinstance(target, dict):
        result: list[dict[str, Any]] = []
        for key in set(base) | set(target):
            child_path = f"{path}/{_escape(key)}"
            if key not in base:
                result.append(
                    {"path": child_path, "before": _state(MISSING), "after": _state(target[key])}
                )
            elif key not in target:
                result.append(
                    {"path": child_path, "before": _state(base[key]), "after": _state(MISSING)}
                )
            else:
                result.extend(_naive_diff(base[key], target[key], child_path))
        return sorted(result, key=lambda entry: entry["path"])
    if isinstance(base, list) and isinstance(target, list):
        result = []
        for index in range(max(len(base), len(target))):
            child_path = f"{path}/{index}"
            if index >= len(base):
                result.append(
                    {
                        "path": child_path,
                        "before": _state(MISSING),
                        "after": _state(target[index]),
                    }
                )
            elif index >= len(target):
                result.append(
                    {
                        "path": child_path,
                        "before": _state(base[index]),
                        "after": _state(MISSING),
                    }
                )
            else:
                result.extend(_naive_diff(base[index], target[index], child_path))
        return sorted(result, key=lambda entry: entry["path"])
    if _same_scalar(base, target):
        return []
    return [{"path": path, "before": _state(base), "after": _state(target)}]


@given(base=json_values, target=json_values)
@settings(max_examples=250, deadline=None)
def test_diff_matches_small_naive_reference(base: Any, target: Any) -> None:
    actual = [entry.model_dump(mode="json") for entry in iter_snapshot_diff_entries(base, target)]

    assert actual == _naive_diff(base, target)
    assert [entry["path"] for entry in actual] == sorted({entry["path"] for entry in actual})


def _reverse_maps(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _reverse_maps(item) for key, item in reversed(tuple(value.items()))}
    if isinstance(value, list):
        return [_reverse_maps(item) for item in value]
    return value


@given(base=json_values, target=json_values)
@settings(max_examples=150, deadline=None)
def test_diff_is_invariant_to_recursive_map_insertion_order(base: Any, target: Any) -> None:
    original = tuple(iter_snapshot_diff_entries(base, target))
    reordered = tuple(iter_snapshot_diff_entries(_reverse_maps(base), _reverse_maps(target)))

    assert reordered == original
