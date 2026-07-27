"""Deterministic lexical identity reconciliation for typed IR operations.

The model may suggest spellings; this module alone decides lexical equivalence.
It is deliberately LLM-free and produces explicit conflicts instead of silently
overwriting two unequal values that normalize to the same identity.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from collections.abc import Mapping
from typing import Any, Iterable

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.findings import TypedOp
from gameforge.contracts.ir import NodeType
from gameforge.contracts.projects import (
    IdentityAliasGroupV1,
    IdentityConflictCandidateV1,
    IdentityConflictV1,
    IdentityNormalizationSummaryV1,
)
from gameforge.spine.ir.snapshot import Snapshot


IDENTITY_NORMALIZATION_POLICY_VERSION = "identity-normalization@1"
_SEPARATORS = re.compile(r"[._/\\\-\s]+", re.UNICODE)
_OTHER_PUNCTUATION = re.compile(r"[^\w]+", re.UNICODE)
_UNDERSCORES = re.compile(r"_+")
_OP_RANK = {
    "delete_relation": 0,
    "delete_entity": 1,
    "add_entity": 2,
    "set_entity_attr": 3,
    "add_relation": 4,
    "set_relation_attr": 5,
    "replace_subgraph": 6,
}


def canonical_identity_token(value: str) -> str:
    """Return the frozen lexical identity for one unqualified token."""

    if not isinstance(value, str):
        raise TypeError("identity token must be a string")
    token = unicodedata.normalize("NFKC", value).casefold().strip()
    token = _SEPARATORS.sub("_", token)
    token = _OTHER_PUNCTUATION.sub("_", token)
    token = _UNDERSCORES.sub("_", token).strip("_")
    if not token:
        raise ValueError("identity token normalizes to an empty value")
    return token


def canonical_identity_reference(value: str) -> str:
    """Return the frozen lexical identity for a possibly namespaced reference."""

    return ":".join(canonical_identity_token(part) for part in value.split(":"))


_canonical_namespaced = canonical_identity_reference


def _entity_identity(value: str, entity_type: str) -> str:
    canonical = _canonical_namespaced(value)
    if ":" in canonical:
        return canonical
    return f"{canonical_identity_token(entity_type)}:{canonical}"


def _relation_identity(value: str) -> str:
    canonical = _canonical_namespaced(value)
    return canonical if ":" in canonical else f"rel:{canonical}"


def _normalize_json(value: Any) -> Any:
    if isinstance(value, dict):
        grouped: dict[str, list[tuple[str, Any]]] = {}
        for raw_key, raw_value in value.items():
            key = canonical_identity_token(str(raw_key))
            grouped.setdefault(key, []).append((str(raw_key), _normalize_json(raw_value)))
        result: dict[str, Any] = {}
        for key in sorted(grouped):
            choices = sorted(grouped[key], key=lambda item: (canonical_json(item[1]), item[0]))
            result[key] = choices[0][1]
        return result
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class IdentityNormalizationResult:
    ops: tuple[TypedOp, ...]
    alias_groups: tuple[IdentityAliasGroupV1, ...]
    blocking_conflicts: tuple[IdentityConflictV1, ...]
    summary: IdentityNormalizationSummaryV1


@dataclass(frozen=True, slots=True)
class _EntityCandidate:
    op: TypedOp
    raw_identity: str
    canonical_identity: str
    entity_type: str
    attrs: dict[str, Any]
    payload: dict[str, Any]


def _conflict(
    *,
    code: str,
    canonical_identity: str,
    candidates: Iterable[IdentityConflictCandidateV1],
) -> IdentityConflictV1:
    ordered = tuple(
        sorted(
            candidates,
            key=lambda item: (
                canonical_json(item.value),
                item.source_identity,
                item.op_id,
            ),
        )
    )
    digest = canonical_sha256(
        {
            "policy": IDENTITY_NORMALIZATION_POLICY_VERSION,
            "code": code,
            "canonical_identity": canonical_identity,
            "candidates": [item.model_dump(mode="json") for item in ordered],
        }
    )
    return IdentityConflictV1(
        conflict_id=f"identity-conflict:{digest}",
        code=code,  # type: ignore[arg-type]
        canonical_identity=canonical_identity,
        candidates=ordered,
    )


def _candidate_value(
    candidate: _EntityCandidate,
    *,
    source_identity: str,
    value: Any,
) -> IdentityConflictCandidateV1:
    return IdentityConflictCandidateV1(
        op_id=candidate.op.op_id,
        source_identity=source_identity,
        value=value,
    )


def _merge_values(
    values: list[tuple[_EntityCandidate, str, Any]],
    *,
    canonical_path: str,
    conflicts: list[IdentityConflictV1],
) -> Any:
    unique = {canonical_json(value): value for _candidate, _source, value in values}
    if len(unique) == 1:
        return next(iter(unique.values()))
    if all(isinstance(value, dict) for _candidate, _source, value in values):
        keys = sorted({key for _candidate, _source, value in values for key in value})
        merged: dict[str, Any] = {}
        for key in keys:
            nested = [
                (candidate, f"{source}.{key}", value[key])
                for candidate, source, value in values
                if key in value
            ]
            merged[key] = _merge_values(
                nested,
                canonical_path=f"{canonical_path}.{key}",
                conflicts=conflicts,
            )
        return merged
    conflicts.append(
        _conflict(
            code="attribute_value_conflict",
            canonical_identity=canonical_path,
            candidates=(
                _candidate_value(candidate, source_identity=source, value=value)
                for candidate, source, value in values
            ),
        )
    )
    # Retain a deterministic visible candidate while the explicit conflict blocks
    # publication.  No value disappears from the conflict evidence.
    return min(unique.items(), key=lambda item: item[0])[1]


def _endpoint(
    raw: Any,
    *,
    exact_aliases: dict[str, str],
    unqualified_aliases: dict[str, set[str]],
    existing_ids: set[str],
    operation: TypedOp,
    field: str,
    conflicts: list[IdentityConflictV1],
) -> str | None:
    if not isinstance(raw, str) or not raw:
        conflicts.append(
            _conflict(
                code="dangling_relation_endpoint",
                canonical_identity=f"{_relation_identity(operation.target)}.{field}",
                candidates=(
                    IdentityConflictCandidateV1(
                        op_id=operation.op_id,
                        source_identity=field,
                        value=raw,
                    ),
                ),
            )
        )
        return None
    direct = exact_aliases.get(raw)
    if direct is None:
        try:
            namespaced = _canonical_namespaced(raw)
        except ValueError:
            namespaced = ""
        direct = exact_aliases.get(namespaced)
    if direct is not None:
        return direct
    token = canonical_identity_token(raw)
    candidates = set(unqualified_aliases.get(token, set()))
    if token in existing_ids:
        candidates.add(token)
    if len(candidates) == 1:
        return next(iter(candidates))
    code = "ambiguous_unqualified_alias" if len(candidates) > 1 else "dangling_relation_endpoint"
    conflicts.append(
        _conflict(
            code=code,
            canonical_identity=f"{_relation_identity(operation.target)}.{field}",
            candidates=(
                IdentityConflictCandidateV1(
                    op_id=operation.op_id,
                    source_identity=raw,
                    value=sorted(candidates) if candidates else raw,
                ),
            ),
        )
    )
    return None


@dataclass(frozen=True, slots=True)
class IdentityAliasIndex:
    """Every spelling that resolves to a base-snapshot entity id.

    The dicts are mutable on purpose: ``normalize_typed_ops`` extends them with
    proposal-local ids as it walks the ``add_entity`` ops.  Each caller therefore
    gets its OWN index — sharing one would make a later consumer's answer depend
    on an earlier consumer's model output, which is neither deterministic nor
    replayable.
    """

    exact_aliases: dict[str, str]
    unqualified_aliases: dict[str, set[str]]
    existing_ids: set[str]


def build_identity_alias_index(
    base_snapshot: Snapshot,
    *,
    declared_aliases: Mapping[str, str] | None = None,
) -> IdentityAliasIndex:
    """Index one base snapshot plus the names its project declared for it.

    Declared aliases enter ``exact_aliases`` only.  They are exact statements by
    a person, never a source of the unqualified ambiguity ``_endpoint`` reports.
    """

    exact_aliases: dict[str, str] = {}
    unqualified_aliases: dict[str, set[str]] = {}
    existing_ids = set(base_snapshot.entities)

    for entity in base_snapshot.entities.values():
        canonical = _canonical_namespaced(entity.id)
        exact_aliases[entity.id] = entity.id
        exact_aliases[canonical] = entity.id
        suffix = canonical.rsplit(":", 1)[-1]
        unqualified_aliases.setdefault(suffix, set()).add(entity.id)

    for alias, entity_id in (declared_aliases or {}).items():
        if entity_id not in existing_ids:
            raise IntegrityViolation(
                "declared identity alias names an entity the base snapshot does not have",
                alias=alias,
                entity_id=entity_id,
            )
        if alias in existing_ids and alias != entity_id:
            raise IntegrityViolation(
                "declared identity alias already names an entity of its own",
                alias=alias,
                entity_id=entity_id,
            )
        exact_aliases[alias] = entity_id
        exact_aliases[_canonical_namespaced(alias)] = entity_id

    return IdentityAliasIndex(
        exact_aliases=exact_aliases,
        unqualified_aliases=unqualified_aliases,
        existing_ids=existing_ids,
    )


def normalize_typed_ops(
    base_snapshot: Snapshot,
    operations: Iterable[TypedOp],
    *,
    declared_aliases: Mapping[str, str] | None = None,
) -> IdentityNormalizationResult:
    """Normalize a proposal against one exact base snapshot.

    Output ordering is canonical.  Equal lexical aliases merge; unequal values
    remain visible as blocking conflicts and publication must not proceed until a
    human resolves them.

    ``declared_aliases`` carries names no lexical rule could ever reach — 岩王帝君
    and 钟离 share no characters, so only a person can say they are one thing.
    Once said, applying it is deterministic and no model is in the path.
    """

    input_ops = tuple(operations)
    conflicts: list[IdentityConflictV1] = []
    aliases: dict[str, set[str]] = {}
    entity_groups: dict[str, list[_EntityCandidate]] = {}
    index = build_identity_alias_index(base_snapshot, declared_aliases=declared_aliases)
    exact_aliases = index.exact_aliases
    unqualified_aliases = index.unqualified_aliases
    existing_ids = index.existing_ids

    other_ops: list[TypedOp] = []
    for operation in input_ops:
        if operation.op != "add_entity":
            other_ops.append(operation)
            continue
        payload = operation.new_value if isinstance(operation.new_value, dict) else {}
        raw_type = payload.get("type")
        if not isinstance(raw_type, str) or raw_type not in {item.value for item in NodeType}:
            conflicts.append(
                _conflict(
                    code="malformed_operation",
                    canonical_identity=canonical_identity_token(operation.target),
                    candidates=(
                        IdentityConflictCandidateV1(
                            op_id=operation.op_id,
                            source_identity=operation.target,
                            value=payload,
                        ),
                    ),
                )
            )
            continue
        canonical_spelling = _canonical_namespaced(operation.target)
        canonical_id = (
            exact_aliases.get(operation.target)
            or exact_aliases.get(canonical_spelling)
            or _entity_identity(operation.target, raw_type)
        )
        attrs = _normalize_json(payload.get("attrs", {}))
        if not isinstance(attrs, dict):
            attrs = {}
        normalized_payload = _normalize_json(payload)
        normalized_payload["type"] = raw_type
        normalized_payload["attrs"] = attrs
        candidate = _EntityCandidate(
            op=operation,
            raw_identity=operation.target,
            canonical_identity=canonical_id,
            entity_type=raw_type,
            attrs=attrs,
            payload=normalized_payload,
        )
        entity_groups.setdefault(canonical_id, []).append(candidate)
        aliases.setdefault(canonical_id, set()).update((operation.target, canonical_id))
        # An unqualified spelling may legitimately be proposed under more than
        # one NodeType.  Resolve it through the set-valued unqualified map so a
        # consumer sees an explicit ambiguity instead of last-write-wins.
        if ":" in operation.target:
            exact_aliases[operation.target] = canonical_id
            exact_aliases[_canonical_namespaced(operation.target)] = canonical_id
        raw_token = canonical_identity_token(operation.target.rsplit(":", 1)[-1])
        unqualified_aliases.setdefault(raw_token, set()).add(canonical_id)

    normalized: list[TypedOp] = []
    auto_merge_count = 0
    for canonical_id in sorted(entity_groups):
        candidates = sorted(
            entity_groups[canonical_id],
            key=lambda item: (canonical_json(item.payload), item.op.op_id, item.raw_identity),
        )
        types = {candidate.entity_type for candidate in candidates}
        if len(types) > 1:
            conflicts.append(
                _conflict(
                    code="entity_type_conflict",
                    canonical_identity=canonical_id,
                    candidates=(
                        _candidate_value(
                            candidate,
                            source_identity=candidate.raw_identity,
                            value=candidate.entity_type,
                        )
                        for candidate in candidates
                    ),
                )
            )
        retained = candidates[0]
        merged_attrs = _merge_values(
            [(candidate, candidate.raw_identity, candidate.attrs) for candidate in candidates],
            canonical_path=canonical_id,
            conflicts=conflicts,
        )
        merged_payload = dict(retained.payload)
        merged_payload["type"] = sorted(types)[0]
        merged_payload["attrs"] = merged_attrs
        auto_merge_count += max(0, len(candidates) - 1)
        normalized.append(
            TypedOp(
                op_id=min(candidate.op.op_id for candidate in candidates),
                op="add_entity",
                target=canonical_id,
                old_value=None,
                new_value=merged_payload,
                source_ref=retained.op.source_ref,
            )
        )

    for operation in other_ops:
        if operation.op == "add_relation":
            payload = operation.new_value if isinstance(operation.new_value, dict) else {}
            src_id = _endpoint(
                payload.get("src_id"),
                exact_aliases=exact_aliases,
                unqualified_aliases=unqualified_aliases,
                existing_ids=existing_ids,
                operation=operation,
                field="src_id",
                conflicts=conflicts,
            )
            dst_id = _endpoint(
                payload.get("dst_id"),
                exact_aliases=exact_aliases,
                unqualified_aliases=unqualified_aliases,
                existing_ids=existing_ids,
                operation=operation,
                field="dst_id",
                conflicts=conflicts,
            )
            if src_id is None or dst_id is None:
                continue
            rendered = _normalize_json(payload)
            rendered["src_id"] = src_id
            rendered["dst_id"] = dst_id
            normalized.append(
                operation.model_copy(
                    update={
                        "target": _relation_identity(operation.target),
                        "old_value": None,
                        "new_value": rendered,
                    }
                )
            )
            continue
        if operation.op in {"delete_entity", "delete_relation"}:
            target = (
                exact_aliases.get(operation.target, _canonical_namespaced(operation.target))
                if operation.op == "delete_entity"
                else _relation_identity(operation.target)
            )
            normalized.append(operation.model_copy(update={"target": target}))
            continue
        if operation.op in {"set_entity_attr", "set_relation_attr"}:
            owner, separator, path = operation.target.partition(".")
            if not separator or not path:
                conflicts.append(
                    _conflict(
                        code="malformed_operation",
                        canonical_identity=operation.target,
                        candidates=(
                            IdentityConflictCandidateV1(
                                op_id=operation.op_id,
                                source_identity=operation.target,
                                value=operation.model_dump(mode="json"),
                            ),
                        ),
                    )
                )
                continue
            canonical_owner = (
                exact_aliases.get(owner, _canonical_namespaced(owner))
                if operation.op == "set_entity_attr"
                else _relation_identity(owner)
            )
            # Attribute paths are structural: separators *within each segment*
            # normalize lexically, while dots between segments must survive so
            # ``reward.gold`` still means nested traversal rather than the flat
            # key ``reward_gold``.
            canonical_path = ".".join(
                canonical_identity_token(segment) for segment in path.split(".")
            )
            normalized.append(
                operation.model_copy(
                    update={
                        "target": f"{canonical_owner}.{canonical_path}",
                        "new_value": _normalize_json(operation.new_value),
                    }
                )
            )
            continue
        normalized.append(operation)

    # Multiple unqualified aliases that resolve to different typed entities are
    # not themselves blocking until referenced; endpoint resolution emits the
    # precise conflict with the consuming operation as evidence.
    deduped_conflicts = {conflict.conflict_id: conflict for conflict in conflicts}
    ordered_conflicts = tuple(deduped_conflicts[key] for key in sorted(deduped_conflicts))
    ordered_ops = tuple(
        sorted(
            normalized,
            key=lambda op: (_OP_RANK[op.op], op.target, op.op_id),
        )
    )
    alias_groups = tuple(
        IdentityAliasGroupV1(
            canonical_identity=canonical,
            aliases=tuple(sorted(values)),
        )
        for canonical, values in sorted(aliases.items())
        if len(values) > 1
    )
    summary = IdentityNormalizationSummaryV1(
        input_operation_count=len(input_ops),
        output_operation_count=len(ordered_ops),
        alias_group_count=len(alias_groups),
        auto_merge_count=auto_merge_count,
        blocking_conflict_count=len(ordered_conflicts),
    )
    return IdentityNormalizationResult(
        ops=ordered_ops,
        alias_groups=alias_groups,
        blocking_conflicts=ordered_conflicts,
        summary=summary,
    )


__all__ = [
    "IDENTITY_NORMALIZATION_POLICY_VERSION",
    "IdentityAliasIndex",
    "IdentityNormalizationResult",
    "build_identity_alias_index",
    "canonical_identity_reference",
    "canonical_identity_token",
    "normalize_typed_ops",
]
