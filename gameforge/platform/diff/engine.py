"""Deterministic field-level diffing for canonical snapshot JSON views."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from heapq import merge
from typing import Any

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.diff import SnapshotDiffEntry
from gameforge.contracts.errors import IntegrityViolation


def _is_json_pointer(value: str) -> bool:
    if value == "":
        return True
    if not value.startswith("/"):
        return False
    index = 0
    while index < len(value):
        if value[index] != "~":
            index += 1
            continue
        if index + 1 >= len(value) or value[index + 1] not in {"0", "1"}:
            return False
        index += 2
    return True


def _escape_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _unescape_pointer_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


@dataclass(frozen=True, slots=True)
class CollectionIdentity:
    """Declare one exact array path as an unordered collection keyed by a member."""

    path: str
    identity_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not _is_json_pointer(self.path):
            raise ValueError("collection path must be an RFC 6901 JSON Pointer")
        if not isinstance(self.identity_key, str) or not self.identity_key:
            raise ValueError("collection identity_key must be a non-empty string")


CollectionIdentityInput = Mapping[str, str] | Iterable[CollectionIdentity]


def _normalize_json(value: Any, *, path: str = "") -> Any:
    if value is None or type(value) is bool:
        return value
    if isinstance(value, str):
        return str.__str__(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        normalized_float = float(value)
        if not math.isfinite(normalized_float):
            raise IntegrityViolation(
                "canonical snapshot contains a non-finite JSON value", path=path
            )
        return normalized_float
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        items: list[tuple[str, Any]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise IntegrityViolation(
                    "canonical snapshot object keys must be strings",
                    path=path,
                )
            items.append((str.__str__(key), item))
        for key, item in sorted(items, key=lambda pair: pair[0]):
            child_path = f"{path}/{_escape_pointer_token(key)}"
            normalized[key] = _normalize_json(item, path=child_path)
        return normalized
    if isinstance(value, list):
        return [_normalize_json(item, path=f"{path}/{index}") for index, item in enumerate(value)]
    raise IntegrityViolation(
        "canonical snapshot contains a non-JSON value",
        path=path,
        value_type=type(value).__name__,
    )


def _normalize_identities(
    identities: CollectionIdentityInput,
) -> dict[str, CollectionIdentity]:
    if isinstance(identities, Mapping):
        values: Iterable[CollectionIdentity] = (
            CollectionIdentity(path=path, identity_key=key) for path, key in identities.items()
        )
    else:
        values = identities

    by_path: dict[str, CollectionIdentity] = {}
    for identity in values:
        if not isinstance(identity, CollectionIdentity):
            raise TypeError("collection identities must be CollectionIdentity values")
        existing = by_path.get(identity.path)
        if existing is not None and existing != identity:
            raise IntegrityViolation(
                "collection path has conflicting identity declarations",
                path=identity.path,
            )
        by_path[identity.path] = identity
    return by_path


def _identity_value(item: Any, declaration: CollectionIdentity) -> tuple[str, Any]:
    if not isinstance(item, dict) or declaration.identity_key not in item:
        raise IntegrityViolation(
            "collection member is missing identity",
            path=declaration.path,
            identity_key=declaration.identity_key,
        )
    value = item[declaration.identity_key]
    if value is None or type(value) not in {bool, int, float, str}:
        raise IntegrityViolation(
            "collection member identity must be a non-null scalar identity",
            path=declaration.path,
            identity_key=declaration.identity_key,
        )
    type_name = type(value).__name__
    return canonical_json({"type": type_name, "value": value}), value


def _canonicalize_collection(
    values: list[Any],
    declaration: CollectionIdentity,
) -> list[Any]:
    keyed: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for item in values:
        sort_key, raw_identity = _identity_value(item, declaration)
        if sort_key in seen:
            raise IntegrityViolation(
                "collection contains duplicate identity",
                path=declaration.path,
                identity_key=declaration.identity_key,
                identity=raw_identity,
            )
        seen.add(sort_key)
        keyed.append((sort_key, item))
    return [item for _, item in sorted(keyed, key=lambda pair: pair[0])]


_ABSENT = object()


def _resolve_pointer(root: Any, pointer: str) -> Any:
    if pointer == "":
        return root
    current = root
    for encoded_token in pointer[1:].split("/"):
        token = _unescape_pointer_token(encoded_token)
        if isinstance(current, dict):
            if token not in current:
                return _ABSENT
            current = current[token]
            continue
        if isinstance(current, list):
            if not token.isdigit():
                return _ABSENT
            index = int(token)
            if index >= len(current):
                return _ABSENT
            current = current[index]
            continue
        return _ABSENT
    return current


def _canonicalize_declared_collections(
    value: Any,
    identities: Mapping[str, CollectionIdentity],
) -> None:
    # A child pointer containing an array index addresses the parent's canonical
    # identity order, not its arbitrary input order. Normalize parents first.
    ordered_paths = sorted(identities, key=lambda path: (path.count("/"), path))
    for path in ordered_paths:
        declaration = identities[path]
        collection = _resolve_pointer(value, path)
        if collection is _ABSENT:
            continue
        if not isinstance(collection, list):
            raise IntegrityViolation(
                "declared collection path does not resolve to an array",
                path=path,
            )
        collection[:] = _canonicalize_collection(collection, declaration)


def _present(value: Any) -> dict[str, Any]:
    return {"presence": "present", "value": value}


def _missing() -> dict[str, str]:
    return {"presence": "missing"}


def _scalar_equal(base: Any, target: Any) -> bool:
    if type(base) is not type(target):
        return False
    if type(base) is float:
        return canonical_json(base) == canonical_json(target)
    return base == target


def _iter_diff(
    base: Any,
    target: Any,
    *,
    path: str,
    identities: Mapping[str, CollectionIdentity],
) -> Iterator[SnapshotDiffEntry]:
    if isinstance(base, dict) and isinstance(target, dict):
        children: list[Iterator[SnapshotDiffEntry]] = []
        keys = sorted(set(base) | set(target))
        for key in keys:
            child_path = f"{path}/{_escape_pointer_token(key)}"
            if key not in base:
                children.append(
                    iter(
                        (
                            SnapshotDiffEntry(
                                path=child_path,
                                before=_missing(),
                                after=_present(target[key]),
                            ),
                        )
                    )
                )
            elif key not in target:
                children.append(
                    iter(
                        (
                            SnapshotDiffEntry(
                                path=child_path,
                                before=_present(base[key]),
                                after=_missing(),
                            ),
                        )
                    )
                )
            else:
                children.append(
                    _iter_diff(
                        base[key],
                        target[key],
                        path=child_path,
                        identities=identities,
                    )
                )
        yield from merge(*children, key=lambda entry: entry.path)
        return

    if isinstance(base, list) and isinstance(target, list):
        declaration = identities.get(path)
        if declaration is not None:
            base = _canonicalize_collection(base, declaration)
            target = _canonicalize_collection(target, declaration)
        children = []
        indices = range(max(len(base), len(target)))
        for index in indices:
            child_path = f"{path}/{index}"
            if index >= len(base):
                children.append(
                    iter(
                        (
                            SnapshotDiffEntry(
                                path=child_path,
                                before=_missing(),
                                after=_present(target[index]),
                            ),
                        )
                    )
                )
            elif index >= len(target):
                children.append(
                    iter(
                        (
                            SnapshotDiffEntry(
                                path=child_path,
                                before=_present(base[index]),
                                after=_missing(),
                            ),
                        )
                    )
                )
            else:
                children.append(
                    _iter_diff(
                        base[index],
                        target[index],
                        path=child_path,
                        identities=identities,
                    )
                )
        yield from merge(*children, key=lambda entry: entry.path)
        return

    if not _scalar_equal(base, target):
        yield SnapshotDiffEntry(
            path=path,
            before=_present(base),
            after=_present(target),
        )


def iter_snapshot_diff_entries(
    base: Any,
    target: Any,
    *,
    collection_identities: CollectionIdentityInput = (),
) -> Iterator[SnapshotDiffEntry]:
    """Compare two JSON values and stream stable, unique field-level entries.

    Inputs are normalized before the iterator is returned, so later caller mutation
    cannot alter an in-flight comparison. Map keys and emitted pointers are ordered
    lexicographically by their escaped RFC 6901 representation.
    """

    normalized_base = _normalize_json(base)
    normalized_target = _normalize_json(target)
    identities = _normalize_identities(collection_identities)
    _canonicalize_declared_collections(normalized_base, identities)
    _canonicalize_declared_collections(normalized_target, identities)
    return _iter_diff(
        normalized_base,
        normalized_target,
        path="",
        identities=identities,
    )


__all__ = [
    "CollectionIdentity",
    "CollectionIdentityInput",
    "iter_snapshot_diff_entries",
]
