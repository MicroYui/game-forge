"""Deterministic three-way merge over canonical JSON snapshot views."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gameforge.contracts.canonical import sha256_lowerhex, typed_canonical_json
from gameforge.contracts.diff import (
    ConflictResolution,
    JsonValueState,
    MergeConflict,
    ThreeWayMergePolicyV1,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.platform.diff.engine import (
    CollectionIdentity,
    _canonicalize_declared_collections,
    _escape_pointer_token,
    _identity_value,
    _normalize_json,
)


_MISSING = object()
_ALLOWED_RESOLUTIONS = ("keep_current", "take_proposed", "custom")


@dataclass(frozen=True, slots=True)
class ThreeWayMergePlan:
    """Pure merge output; unresolved conflicts retain the current value."""

    merged: Any
    conflicts: tuple[MergeConflict, ...]
    non_conflicting_ops_digest: str


def _typed_bytes(value: Any) -> bytes:
    return typed_canonical_json(value).encode("utf-8")


def _equal(left: Any, right: Any) -> bool:
    if left is _MISSING or right is _MISSING:
        return left is right
    return _typed_bytes(left) == _typed_bytes(right)


def _state(value: Any) -> JsonValueState:
    if value is _MISSING:
        return JsonValueState.model_validate({"presence": "missing"})
    return JsonValueState.model_validate(
        {"presence": "present", "value": value}
    )


def _state_wire(value: Any) -> dict[str, Any]:
    return _state(value).model_dump(mode="python")


def _conflict_kind(base: Any, current: Any, proposed: Any) -> str:
    if base is _MISSING:
        return "concurrent_add"
    if current is _MISSING:
        return "delete_modify"
    if proposed is _MISSING:
        return "modify_delete"
    return "concurrent_change"


def _conflict_id(
    *,
    policy_digest: str,
    path: str,
    kind: str,
    base: Any,
    current: Any,
    proposed: Any,
) -> str:
    payload = {
        "id_schema_version": "merge-conflict-id@1",
        "merge_policy_digest": policy_digest,
        "path": path,
        "kind": kind,
        "base": _state_wire(base),
        "current": _state_wire(current),
        "proposed": _state_wire(proposed),
    }
    return f"conflict:{sha256_lowerhex(_typed_bytes(payload))}"


def _non_conflicting_digest(
    *,
    policy_digest: str,
    operations: Sequence[dict[str, Any]],
) -> str:
    payload = {
        "digest_schema_version": "three-way-non-conflicting-ops@1",
        "merge_policy_digest": policy_digest,
        "operations": list(operations),
    }
    return sha256_lowerhex(_typed_bytes(payload))


class _Merge:
    def __init__(
        self,
        *,
        policy: ThreeWayMergePolicyV1,
        resolutions: Mapping[str, ConflictResolution] | None = None,
    ) -> None:
        self._policy = policy
        self._identities = {
            item.path: CollectionIdentity(
                path=item.path,
                identity_key=item.identity_key,
            )
            for item in policy.collection_identities
        }
        self._resolutions = resolutions
        self.conflicts: list[MergeConflict] = []
        self.operations: list[dict[str, Any]] = []

    def merge(self, base: Any, current: Any, proposed: Any, *, path: str) -> Any:
        if _equal(current, proposed):
            return current

        if (
            base is not _MISSING
            and current is not _MISSING
            and proposed is not _MISSING
            and isinstance(base, dict)
            and isinstance(current, dict)
            and isinstance(proposed, dict)
        ):
            return self._merge_mapping(base, current, proposed, path=path)

        if (
            base is not _MISSING
            and current is not _MISSING
            and proposed is not _MISSING
            and isinstance(base, list)
            and isinstance(current, list)
            and isinstance(proposed, list)
        ):
            declaration = self._identities.get(path)
            if declaration is not None:
                return self._merge_collection(
                    base,
                    current,
                    proposed,
                    path=path,
                    declaration=declaration,
                )
            if len(base) == len(current) == len(proposed):
                return [
                    self.merge(
                        base[index],
                        current[index],
                        proposed[index],
                        path=f"{path}/{index}",
                    )
                    for index in range(len(base))
                ]

        if _equal(current, base):
            self._record_operation(path, proposed)
            return proposed
        if _equal(proposed, base):
            return current

        kind = (
            "concurrent_array_resize"
            if all(
                value is not _MISSING and isinstance(value, list)
                for value in (base, current, proposed)
            )
            else _conflict_kind(base, current, proposed)
        )
        return self._conflict(
            path=path,
            kind=kind,
            base=base,
            current=current,
            proposed=proposed,
        )

    def _merge_mapping(
        self,
        base: dict[str, Any],
        current: dict[str, Any],
        proposed: dict[str, Any],
        *,
        path: str,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        keys = set(base) | set(current) | set(proposed)
        for key in sorted(keys, key=_escape_pointer_token):
            child = self.merge(
                base.get(key, _MISSING),
                current.get(key, _MISSING),
                proposed.get(key, _MISSING),
                path=f"{path}/{_escape_pointer_token(key)}",
            )
            if child is not _MISSING:
                result[key] = child
        return result

    def _merge_collection(
        self,
        base: list[Any],
        current: list[Any],
        proposed: list[Any],
        *,
        path: str,
        declaration: CollectionIdentity,
    ) -> list[Any]:
        base_items = self._collection_items(base, declaration)
        current_items = self._collection_items(current, declaration)
        proposed_items = self._collection_items(proposed, declaration)
        identity_keys = sorted(set(base_items) | set(current_items) | set(proposed_items))

        result: list[Any] = []
        for ordinal, identity in enumerate(identity_keys):
            item = self.merge(
                base_items.get(identity, _MISSING),
                current_items.get(identity, _MISSING),
                proposed_items.get(identity, _MISSING),
                path=f"{path}/{ordinal}",
            )
            if item is _MISSING:
                continue
            merged_identity, raw_identity = _identity_value(item, declaration)
            if merged_identity != identity:
                raise ValueError(
                    "collection resolution cannot change member identity "
                    f"at {path!r}: {raw_identity!r}"
                )
            result.append(item)
        return result

    @staticmethod
    def _collection_items(
        values: list[Any], declaration: CollectionIdentity
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for item in values:
            identity, raw_identity = _identity_value(item, declaration)
            if identity in result:
                raise IntegrityViolation(
                    "collection contains duplicate identity",
                    path=declaration.path,
                    identity_key=declaration.identity_key,
                    identity=raw_identity,
                )
            result[identity] = item
        return result

    def _record_operation(self, path: str, proposed: Any) -> None:
        self.operations.append(
            {
                "path": path,
                "after": _state_wire(proposed),
            }
        )

    def _conflict(
        self,
        *,
        path: str,
        kind: str,
        base: Any,
        current: Any,
        proposed: Any,
    ) -> Any:
        conflict = MergeConflict(
            id=_conflict_id(
                policy_digest=self._policy.policy_digest,
                path=path,
                kind=kind,
                base=base,
                current=current,
                proposed=proposed,
            ),
            path=path,
            kind=kind,
            base=_state(base),
            current=_state(current),
            proposed=_state(proposed),
            allowed_resolutions=_ALLOWED_RESOLUTIONS,
        )
        self.conflicts.append(conflict)
        if self._resolutions is None:
            return current

        resolution = self._resolutions[conflict.id]
        if resolution.choice not in conflict.allowed_resolutions:
            raise ValueError(
                f"resolution {resolution.choice!r} is not allowed for {conflict.id!r}"
            )
        if resolution.choice == "keep_current":
            return current
        if resolution.choice == "take_proposed":
            return proposed
        return _normalize_json(resolution.custom_value, path=path)


def _normalized_inputs(
    base: Any,
    current: Any,
    proposed: Any,
    policy: ThreeWayMergePolicyV1,
) -> tuple[Any, Any, Any]:
    if not isinstance(policy, ThreeWayMergePolicyV1):
        raise TypeError("policy must be ThreeWayMergePolicyV1")
    normalized = (
        _normalize_json(base),
        _normalize_json(current),
        _normalize_json(proposed),
    )
    identities = {
        item.path: CollectionIdentity(path=item.path, identity_key=item.identity_key)
        for item in policy.collection_identities
    }
    for value in normalized:
        _canonicalize_declared_collections(value, identities)
    return normalized


def compute_three_way_merge(
    base: Any,
    current: Any,
    proposed: Any,
    policy: ThreeWayMergePolicyV1,
) -> ThreeWayMergePlan:
    """Compute a deterministic partial merge, retaining current at conflicts."""

    normalized_base, normalized_current, normalized_proposed = _normalized_inputs(
        base,
        current,
        proposed,
        policy,
    )
    merger = _Merge(policy=policy)
    merged = merger.merge(
        normalized_base,
        normalized_current,
        normalized_proposed,
        path="",
    )
    conflicts = tuple(sorted(merger.conflicts, key=lambda item: (item.path, item.id)))
    operations = tuple(
        sorted(
            merger.operations,
            key=lambda item: (item["path"], typed_canonical_json(item)),
        )
    )
    if len({item.path for item in conflicts}) != len(conflicts):
        raise IntegrityViolation("three-way merge emitted duplicate conflict paths")
    if len({item["path"] for item in operations}) != len(operations):
        raise IntegrityViolation("three-way merge emitted duplicate operation paths")
    return ThreeWayMergePlan(
        merged=merged,
        conflicts=conflicts,
        non_conflicting_ops_digest=_non_conflicting_digest(
            policy_digest=policy.policy_digest,
            operations=operations,
        ),
    )


def resolve_three_way_merge(
    base: Any,
    current: Any,
    proposed: Any,
    policy: ThreeWayMergePolicyV1,
    resolutions: Sequence[ConflictResolution],
) -> Any:
    """Resolve every computed conflict exactly once and return the final JSON value."""

    plan = compute_three_way_merge(base, current, proposed, policy)
    by_id: dict[str, ConflictResolution] = {}
    for resolution in resolutions:
        if not isinstance(resolution, ConflictResolution):
            raise TypeError("resolutions must contain ConflictResolution values")
        if resolution.conflict_id in by_id:
            raise ValueError(f"duplicate resolution for {resolution.conflict_id!r}")
        by_id[resolution.conflict_id] = resolution

    expected = {conflict.id for conflict in plan.conflicts}
    actual = set(by_id)
    if actual != expected:
        raise ValueError(
            "resolutions must cover every conflict exactly once; "
            f"missing={sorted(expected - actual)!r}, extra={sorted(actual - expected)!r}"
        )

    normalized_base, normalized_current, normalized_proposed = _normalized_inputs(
        base,
        current,
        proposed,
        policy,
    )
    merger = _Merge(policy=policy, resolutions=by_id)
    merged = merger.merge(
        normalized_base,
        normalized_current,
        normalized_proposed,
        path="",
    )
    if {conflict.id for conflict in merger.conflicts} != expected:
        raise IntegrityViolation("three-way conflicts changed during deterministic resolution")
    return merged


__all__ = [
    "ThreeWayMergePlan",
    "compute_three_way_merge",
    "resolve_three_way_merge",
]
