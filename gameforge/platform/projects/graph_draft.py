"""Deterministic full-graph editor compilation for project content drafts."""

from __future__ import annotations

from dataclasses import dataclass

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.findings import PatchV2, TypedOp
from gameforge.contracts.ir import Entity, Relation
from gameforge.contracts.projects import (
    IdentityAliasGroupV1,
    IdentityNormalizationSummaryV1,
)
from gameforge.platform.diff.ir_rebase import compile_snapshot_diff_ops
from gameforge.spine.identity_normalization import normalize_typed_ops
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import PatchRejected, apply_patch


@dataclass(frozen=True, slots=True)
class CompiledProjectGraphDraft:
    target: Snapshot
    ops: tuple[TypedOp, ...]
    alias_groups: tuple[IdentityAliasGroupV1, ...]
    normalization_summary: IdentityNormalizationSummaryV1


def _source_op(kind: str, target: str, payload: dict[str, object]) -> TypedOp:
    digest = canonical_sha256(
        {
            "operation_schema_version": "project-graph-source-op@1",
            "kind": kind,
            "target": target,
            "payload": payload,
        }
    )
    return TypedOp(
        op_id=f"project-graph-source-op:{digest}",
        op=kind,  # type: ignore[arg-type]
        target=target,
        new_value=payload,
    )


def compile_project_graph_draft(
    *,
    base: Snapshot,
    entities: tuple[Entity, ...],
    relations: tuple[Relation, ...],
) -> CompiledProjectGraphDraft:
    """Normalize a full editor graph and compile its exact optimistic diff."""

    source_ops = tuple(
        _source_op(
            "add_entity",
            entity.id,
            entity.model_dump(mode="python", exclude={"id"}),
        )
        for entity in entities
    ) + tuple(
        _source_op(
            "add_relation",
            relation.id,
            relation.model_dump(mode="python", exclude={"id"}),
        )
        for relation in relations
    )
    normalized = normalize_typed_ops(base, source_ops)
    if normalized.blocking_conflicts:
        raise Conflict(
            "project graph contains identities that require human resolution",
            conflict_ids=tuple(item.conflict_id for item in normalized.blocking_conflicts),
            conflicts=tuple(item.model_dump(mode="json") for item in normalized.blocking_conflicts),
        )
    empty = Snapshot(entities={}, relations={}, meta_schema_version=base.meta_schema_version)
    assembly = PatchV2(
        revision=1,
        base_snapshot_id=empty.snapshot_id,
        target_snapshot_id=empty.snapshot_id,
        side_effect_risk="low",
        ops=list(normalized.ops),
        produced_by="human",
        producer_run_id=None,
        rationale="assemble normalized project graph target",
    )
    try:
        target = apply_patch(empty, assembly)
    except PatchRejected as exc:
        raise Conflict(
            "project graph is not a valid connected IR target",
            reason=exc.reason,
            op_id=exc.op_id,
        ) from exc
    ops = compile_snapshot_diff_ops(base, target)
    try:
        replay = apply_patch(
            base,
            PatchV2(
                revision=1,
                base_snapshot_id=base.snapshot_id,
                target_snapshot_id=target.snapshot_id,
                side_effect_risk="low",
                ops=list(ops),
                produced_by="human",
                producer_run_id=None,
                rationale="verify project graph target diff",
            ),
        )
    except PatchRejected as exc:  # pragma: no cover - compiler invariant
        raise IntegrityViolation(
            "compiled project graph diff cannot replay",
            reason=exc.reason,
            op_id=exc.op_id,
        ) from exc
    if replay.content_payload != target.content_payload:
        raise IntegrityViolation("compiled project graph diff changes its target")
    return CompiledProjectGraphDraft(
        target=target,
        ops=ops,
        alias_groups=normalized.alias_groups,
        normalization_summary=normalized.summary,
    )


__all__ = ["CompiledProjectGraphDraft", "compile_project_graph_draft"]
