from gameforge.contracts.canonical import canonical_json, compute_snapshot_id


def test_key_order_independent():
    a = {"b": 1, "a": 2, "c": {"y": 1, "x": 2}}
    b = {"a": 2, "c": {"x": 2, "y": 1}, "b": 1}
    assert canonical_json(a) == canonical_json(b)
    assert compute_snapshot_id(a) == compute_snapshot_id(b)


def test_none_optionals_dropped():
    assert canonical_json({"a": 1, "b": None}) == canonical_json({"a": 1})


def test_float_normalized_stably():
    # 1.10 and 1.1 must canonicalize identically; ints stay distinct from floats
    assert canonical_json({"v": 1.10}) == canonical_json({"v": 1.1})
    assert canonical_json({"v": 1}) != canonical_json({"v": 1.0})


def test_snapshot_id_prefixed():
    sid = compute_snapshot_id({"x": 1})
    assert sid.startswith("sha256:")
    assert len(sid) == len("sha256:") + 64


def test_ordered_list_preserved():
    assert canonical_json({"steps": [3, 1, 2]}) != canonical_json({"steps": [1, 2, 3]})


def test_nested_none_dropped_recursively():
    assert canonical_json({"a": {"x": None, "y": 2}}) == canonical_json({"a": {"y": 2}})
