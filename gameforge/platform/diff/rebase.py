"""Transactional orchestration for deterministic PatchV2 three-way rebases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from gameforge.contracts.api import compute_resource_etag
from gameforge.contracts.canonical import (
    canonical_json,
    compute_snapshot_id,
    sha256_lowerhex,
    typed_canonical_json,
)
from gameforge.contracts.diff import (
    ConflictResolution,
    ConflictSet,
    ConflictSetContextV1,
    MAX_CONFLICT_ITEMS,
    MergeConflict,
    RebaseResult,
    ThreeWayMergePolicyV1,
)
from gameforge.contracts.errors import (
    Conflict,
    Forbidden,
    IntegrityViolation,
    StaleConflictSet,
)
from gameforge.contracts.findings import PatchV2
from gameforge.contracts.lineage import (
    ArtifactV2,
    AuditCorrelation,
    AuditSubject,
)
from gameforge.contracts.storage import PageCursorV1, PageV1, RefValue, UtcClock
from gameforge.contracts.workflow import (
    ApprovalItem,
    PatchTargetBindingV1,
    SubjectHead,
)
from gameforge.platform.approvals.commands import (
    ApprovalCommandCapabilities,
    ApprovalCommandContext,
    ApprovalCommandService,
    ApprovalUnitOfWork,
    PreparedDraft,
)
from gameforge.platform.diff.ir_rebase import (
    REBASE_TOOL_VERSION,
    CompiledRebase,
    compile_rebased_patch,
)
from gameforge.platform.diff.three_way import (
    ThreeWayMergePlan,
    compute_three_way_merge,
    resolve_three_way_merge,
)
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import PatchRejected, apply_patch


_REBASE_OPERATION = "diff.rebase"
_RESOLVE_OPERATION = "diff.resolve_conflicts"


@dataclass(frozen=True, slots=True)
class RebaseMaterial:
    """Exact immutable inputs used by both rebase and conflict resolution."""

    source_item: ApprovalItem
    source_head: SubjectHead
    source_patch_artifact: ArtifactV2
    source_patch: PatchV2
    base_artifact: ArtifactV2
    base_snapshot: Snapshot
    current_artifact: ArtifactV2
    current_snapshot: Snapshot
    proposed_artifact: ArtifactV2
    proposed_snapshot: Snapshot
    ref_name: str
    expected_ref: RefValue
    merge_policy: ThreeWayMergePolicyV1


class RebasePayloadGateway(Protocol):
    """Parse and content-hash verify exact Artifact object bytes."""

    def load_patch(self, artifact: ArtifactV2) -> PatchV2: ...

    def load_snapshot(self, artifact: ArtifactV2) -> Snapshot: ...


class ConflictSetRepository(Protocol):
    def put(
        self,
        conflict_set: ConflictSet,
        context: ConflictSetContextV1,
        conflicts: tuple[MergeConflict, ...],
    ) -> ConflictSet: ...

    def get(self, conflict_set_id: str) -> ConflictSet | None: ...

    def get_context(self, conflict_set_id: str) -> ConflictSetContextV1 | None: ...

    def load_bounded(
        self,
        conflict_set_id: str,
    ) -> tuple[ConflictSet, ConflictSetContextV1, tuple[MergeConflict, ...]] | None: ...

    def page_conflicts(
        self,
        conflict_set_id: str,
        cursor: PageCursorV1 | None = None,
    ) -> PageV1[MergeConflict]: ...


@dataclass(slots=True)
class RebaseWorkflowCapabilities:
    approval: ApprovalCommandCapabilities
    conflicts: ConflictSetRepository


CapabilityBinder = Callable[[Any], RebaseWorkflowCapabilities]


def _wire(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    return typed_canonical_json(value)


def _same_wire(left: Any, right: Any) -> bool:
    return _wire(left) == _wire(right)


def _same_conflict_bundle(
    left: tuple[ConflictSet, ConflictSetContextV1, tuple[MergeConflict, ...]],
    right: tuple[ConflictSet, ConflictSetContextV1, tuple[MergeConflict, ...]],
) -> bool:
    return (
        _same_wire(left[0], right[0])
        and _same_wire(left[1], right[1])
        and len(left[2]) == len(right[2])
        and all(
            _same_wire(left_conflict, right_conflict)
            for left_conflict, right_conflict in zip(
                left[2],
                right[2],
                strict=True,
            )
        )
    )


def _require_canonical_model[T: BaseModel](
    value: object,
    model_type: type[T],
    *,
    label: str,
) -> T:
    if type(value) is not model_type:
        raise IntegrityViolation(f"{label} must be an exact {model_type.__name__}")
    assert isinstance(value, BaseModel)
    raw = value.model_dump(mode="python")
    try:
        parsed = model_type.model_validate(raw)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(f"{label} is invalid") from exc
    if not _same_wire(parsed, raw):
        raise IntegrityViolation(f"{label} is noncanonical")
    return parsed


def _require_snapshot(value: object, *, label: str) -> Snapshot:
    if type(value) is not Snapshot:
        raise IntegrityViolation(f"{label} must be an exact Snapshot")
    assert isinstance(value, Snapshot)
    try:
        expected_id = compute_snapshot_id(value.content_payload)
    except (TypeError, ValueError) as exc:
        raise IntegrityViolation(f"{label} canonical payload is invalid") from exc
    if value.snapshot_id != expected_id:
        raise IntegrityViolation(f"{label} snapshot_id does not match its payload")
    return value


def _require_snapshot_equal(
    retained: Snapshot,
    expected: Snapshot,
    *,
    label: str,
) -> None:
    _require_snapshot(retained, label=f"loaded {label}")
    _require_snapshot(expected, label=label)
    if retained.snapshot_id != expected.snapshot_id or not _same_wire(
        retained.content_payload, expected.content_payload
    ):
        raise IntegrityViolation(f"loaded {label} differs from RebaseMaterial")


def _required[T](value: T | None, *, label: str) -> T:
    if value is None:
        raise IntegrityViolation(f"{label} rebase capability is unavailable")
    return value


def _utc_text(clock: UtcClock) -> str:
    try:
        now = clock.now_utc()
    except (AttributeError, TypeError, ValueError) as exc:
        raise IntegrityViolation("rebase clock must return UTC") from exc
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or now.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation("rebase clock must return UTC")
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _conflict_context(material: RebaseMaterial) -> ConflictSetContextV1:
    return ConflictSetContextV1(
        subject_series_id=material.source_item.subject_series_id,
        expected_subject_artifact_id=material.source_patch_artifact.artifact_id,
        expected_approval_id=material.source_item.approval_id,
        expected_subject_head_revision=material.source_head.revision,
        expected_workflow_revision=material.source_item.workflow_revision,
        ref_name=material.ref_name,
        expected_ref=material.expected_ref,
        merge_policy=material.merge_policy,
    )


def _conflict_set_id(
    conflict_set: ConflictSet,
    context: ConflictSetContextV1,
    conflicts: tuple[MergeConflict, ...],
) -> str:
    payload = {
        "identity_schema_version": "conflict-set-id@1",
        "conflict_set": conflict_set.model_dump(mode="python", exclude={"id"}),
        "context": context.model_dump(mode="python"),
        "conflicts": [item.model_dump(mode="python") for item in conflicts],
    }
    digest = sha256_lowerhex(typed_canonical_json(payload).encode("utf-8"))
    return f"conflict-set:{digest}"


def _build_conflict_set(
    *,
    material: RebaseMaterial,
    plan: ThreeWayMergePlan,
    context: ConflictSetContextV1,
    created_at: str,
) -> ConflictSet:
    if len(plan.conflicts) > MAX_CONFLICT_ITEMS:
        raise IntegrityViolation(
            "three-way merge exceeds the bounded ConflictSet limit",
            conflict_count=len(plan.conflicts),
            max_conflicts=MAX_CONFLICT_ITEMS,
        )
    provisional = ConflictSet(
        id="conflict-set:pending",
        base_snapshot_id=material.base_snapshot.snapshot_id,
        current_snapshot_id=material.current_snapshot.snapshot_id,
        proposed_patch_artifact_id=material.source_patch_artifact.artifact_id,
        expected_ref_revision=material.expected_ref.revision,
        conflict_count=len(plan.conflicts),
        non_conflicting_ops_digest=plan.non_conflicting_ops_digest,
        created_at=created_at,
    )
    return provisional.model_copy(
        update={"id": _conflict_set_id(provisional, context, plan.conflicts)}
    )


class RebaseWorkflowService:
    def __init__(
        self,
        *,
        unit_of_work: ApprovalUnitOfWork,
        bind_capabilities: CapabilityBinder,
        approval_commands: ApprovalCommandService,
        payloads: RebasePayloadGateway,
        clock: UtcClock,
        audit_chain_id: str,
    ) -> None:
        if not audit_chain_id:
            raise ValueError("audit_chain_id must be non-empty")
        self._unit_of_work = unit_of_work
        self._bind_capabilities = bind_capabilities
        self._approval_commands = approval_commands
        self._payloads = payloads
        self._clock = clock
        self._audit_chain_id = audit_chain_id

    def replay_command(
        self,
        *,
        context: ApprovalCommandContext,
        resolve_conflicts: bool,
    ) -> RebaseResult | None:
        """Replay an exact committed command before mutable source-state assembly."""

        self._require_direct_human(context)
        return self._early_replay(
            context=context,
            operation=(_RESOLVE_OPERATION if resolve_conflicts else _REBASE_OPERATION),
            prepared_draft=None,
            expected_status="clean" if resolve_conflicts else None,
        )

    def rebase(
        self,
        *,
        material: RebaseMaterial,
        prepared_draft: PreparedDraft | None,
        context: ApprovalCommandContext,
    ) -> RebaseResult:
        self._require_direct_human(context)
        replay = self._early_replay(
            context=context,
            operation=_REBASE_OPERATION,
            prepared_draft=prepared_draft,
        )
        if replay is not None:
            return replay
        self._validate_material_payloads(material)
        plan = self._compute_plan(material)

        if plan.conflicts:
            if prepared_draft is not None:
                raise IntegrityViolation(
                    "a conflicted rebase cannot publish an unused prepared draft"
                )
            conflict_context = _conflict_context(material)
            conflict_set = _build_conflict_set(
                material=material,
                plan=plan,
                context=conflict_context,
                created_at=_utc_text(self._clock),
            )
            with self._unit_of_work.begin() as transaction:
                capabilities = self._capabilities(transaction)
                replay = self._get_replay(
                    capabilities,
                    context,
                    operation=_REBASE_OPERATION,
                    prepared_draft=None,
                    expected_status="conflicted",
                )
                if replay is not None:
                    return replay
                self._validate_live_material(
                    material,
                    capabilities.approval,
                    context,
                    stale=False,
                )
                retained = capabilities.conflicts.put(
                    conflict_set,
                    conflict_context,
                    plan.conflicts,
                )
                if retained != conflict_set:
                    raise IntegrityViolation("ConflictSet repository returned another conflict set")
                result = RebaseResult(
                    status="conflicted",
                    conflict_set_id=conflict_set.id,
                )
                self._audit(
                    capabilities.approval,
                    context,
                    action="diff.conflict_set_created",
                    resource_kind="conflict_set",
                    resource_id=conflict_set.id,
                    artifact_id=material.source_patch_artifact.artifact_id,
                )
                self._put_result(
                    capabilities.approval,
                    context,
                    operation=_REBASE_OPERATION,
                    result=result,
                )
                return result

        compiled = compile_rebased_patch(
            source_patch_artifact_id=material.source_patch_artifact.artifact_id,
            source_patch=material.source_patch,
            current=material.current_snapshot,
            resolved_view=plan.merged,
        )
        if prepared_draft is not None:
            self._validate_prepared_draft(
                material=material,
                compiled=compiled,
                prepared=prepared_draft,
                context=context,
            )

        with self._unit_of_work.begin() as transaction:
            capabilities = self._capabilities(transaction)
            replay = self._get_replay(
                capabilities,
                context,
                operation=_REBASE_OPERATION,
                prepared_draft=prepared_draft,
                expected_status="clean",
            )
            if replay is not None:
                return replay
            if prepared_draft is None:
                raise IntegrityViolation("a clean rebase requires a prepared draft")
            self._validate_live_material(
                material,
                capabilities.approval,
                context,
                stale=False,
            )
            publication = self._approval_commands.publish_rebased_draft_in_transaction(
                transaction=transaction,
                prepared=prepared_draft,
                context=context,
                expected_ref=material.expected_ref,
            )
            result = RebaseResult(
                status="clean",
                new_patch_artifact_id=publication.approval_item.subject_artifact_id,
            )
            self._audit(
                capabilities.approval,
                context,
                action="diff.rebased",
                resource_kind="patch",
                resource_id=result.new_patch_artifact_id,
                artifact_id=result.new_patch_artifact_id,
            )
            self._put_result(
                capabilities.approval,
                context,
                operation=_REBASE_OPERATION,
                result=result,
            )
            return result

    def resolve_conflicts(
        self,
        *,
        material: RebaseMaterial,
        conflict_set_id: str,
        resolutions: tuple[ConflictResolution, ...],
        prepared_draft: PreparedDraft,
        context: ApprovalCommandContext,
    ) -> RebaseResult:
        self._require_direct_human(context)
        replay = self._early_replay(
            context=context,
            operation=_RESOLVE_OPERATION,
            prepared_draft=prepared_draft,
            expected_status="clean",
        )
        if replay is not None:
            return replay
        if not conflict_set_id:
            raise ValueError("conflict_set_id must be non-empty")
        if len(resolutions) > MAX_CONFLICT_ITEMS:
            raise IntegrityViolation(
                "conflict resolution exceeds the bounded ConflictSet limit",
                resolution_count=len(resolutions),
                max_conflicts=MAX_CONFLICT_ITEMS,
            )
        self._validate_material_payloads(material)
        plan = self._compute_plan(material)
        if not plan.conflicts:
            raise StaleConflictSet("the retained merge no longer has conflicts")
        retained_conflicts = self._load_retained_conflicts(conflict_set_id)
        self._validate_retained_conflicts(
            retained=retained_conflicts,
            conflict_set_id=conflict_set_id,
            material=material,
            plan=plan,
        )
        resolved = resolve_three_way_merge(
            material.base_snapshot.content_payload,
            material.current_snapshot.content_payload,
            material.proposed_snapshot.content_payload,
            material.merge_policy,
            resolutions,
        )
        compiled = compile_rebased_patch(
            source_patch_artifact_id=material.source_patch_artifact.artifact_id,
            source_patch=material.source_patch,
            current=material.current_snapshot,
            resolved_view=resolved,
        )
        self._validate_prepared_draft(
            material=material,
            compiled=compiled,
            prepared=prepared_draft,
            context=context,
        )

        with self._unit_of_work.begin() as transaction:
            capabilities = self._capabilities(transaction)
            replay = self._get_replay(
                capabilities,
                context,
                operation=_RESOLVE_OPERATION,
                prepared_draft=prepared_draft,
                expected_status="clean",
            )
            if replay is not None:
                return replay
            self._validate_live_material(
                material,
                capabilities.approval,
                context,
                stale=True,
            )
            self._validate_retained_conflict_metadata(
                repository=capabilities.conflicts,
                conflict_set_id=conflict_set_id,
                retained=retained_conflicts,
            )
            try:
                publication = self._approval_commands.publish_rebased_draft_in_transaction(
                    transaction=transaction,
                    prepared=prepared_draft,
                    context=context,
                    expected_ref=material.expected_ref,
                )
            except StaleConflictSet:
                raise
            except Conflict as exc:
                raise StaleConflictSet(
                    "conflict resolution publication precondition is stale",
                    cause=exc.code,
                ) from exc
            result = RebaseResult(
                status="clean",
                new_patch_artifact_id=publication.approval_item.subject_artifact_id,
            )
            self._audit(
                capabilities.approval,
                context,
                action="diff.conflicts_resolved",
                resource_kind="patch",
                resource_id=result.new_patch_artifact_id,
                artifact_id=result.new_patch_artifact_id,
            )
            self._put_result(
                capabilities.approval,
                context,
                operation=_RESOLVE_OPERATION,
                result=result,
            )
            return result

    @staticmethod
    def _require_direct_human(context: ApprovalCommandContext) -> None:
        _require_canonical_model(
            context,
            ApprovalCommandContext,
            label="rebase command context",
        )
        if context.actor.principal_kind != "human" or context.initiated_by is not None:
            raise Forbidden("rebase commands require a direct human actor without an initiator")

    def _validate_material_payloads(self, material: RebaseMaterial) -> None:
        self._validate_material_bindings(material)
        loaded_patch = self._payloads.load_patch(material.source_patch_artifact)
        _require_canonical_model(loaded_patch, PatchV2, label="loaded source Patch")
        if not _same_wire(loaded_patch, material.source_patch):
            raise IntegrityViolation("loaded source Patch differs from RebaseMaterial")
        for label, artifact, expected in (
            ("base Snapshot", material.base_artifact, material.base_snapshot),
            ("current Snapshot", material.current_artifact, material.current_snapshot),
            ("proposed Snapshot", material.proposed_artifact, material.proposed_snapshot),
        ):
            loaded = self._payloads.load_snapshot(artifact)
            _require_snapshot_equal(loaded, expected, label=label)

        try:
            reproduced = apply_patch(material.base_snapshot, material.source_patch)
        except PatchRejected as exc:
            raise IntegrityViolation(
                "source Patch cannot be applied to its exact base Snapshot"
            ) from exc
        if reproduced.snapshot_id != material.proposed_snapshot.snapshot_id or not _same_wire(
            reproduced.content_payload,
            material.proposed_snapshot.content_payload,
        ):
            raise IntegrityViolation("source Patch does not reproduce the proposed Snapshot")

    @staticmethod
    def _validate_material_bindings(material: RebaseMaterial) -> None:
        if type(material) is not RebaseMaterial:
            raise IntegrityViolation("material must be an exact RebaseMaterial")
        item = _require_canonical_model(
            material.source_item,
            ApprovalItem,
            label="source ApprovalItem",
        )
        head = _require_canonical_model(
            material.source_head,
            SubjectHead,
            label="source SubjectHead",
        )
        source_artifact = _require_canonical_model(
            material.source_patch_artifact,
            ArtifactV2,
            label="source Patch Artifact",
        )
        source_patch = _require_canonical_model(
            material.source_patch,
            PatchV2,
            label="source Patch",
        )
        expected_ref = _require_canonical_model(
            material.expected_ref,
            RefValue,
            label="current ref value",
        )
        _require_canonical_model(
            material.merge_policy,
            ThreeWayMergePolicyV1,
            label="merge policy",
        )
        if not isinstance(material.ref_name, str) or not material.ref_name:
            raise IntegrityViolation("rebase ref_name must be non-empty")

        if (
            item.subject_kind != "patch"
            or item.subject_artifact_id != source_artifact.artifact_id
            or item.subject_digest != source_artifact.payload_hash
            or item.subject_revision != source_patch.revision
        ):
            raise IntegrityViolation(
                "source ApprovalItem does not bind the exact source Patch revision"
            )
        if (
            head.subject_series_id != item.subject_series_id
            or head.current_subject_artifact_id != source_artifact.artifact_id
            or head.current_approval_id != item.approval_id
            or head.revision != item.subject_revision
        ):
            raise IntegrityViolation(
                "source SubjectHead does not bind the source ApprovalItem revision"
            )
        if source_artifact.kind != "patch":
            raise IntegrityViolation("source Patch Artifact has another kind")

        snapshots = (
            ("base", material.base_artifact, material.base_snapshot),
            ("current", material.current_artifact, material.current_snapshot),
            ("proposed", material.proposed_artifact, material.proposed_snapshot),
        )
        for label, artifact, snapshot in snapshots:
            parsed_artifact = _require_canonical_model(
                artifact,
                ArtifactV2,
                label=f"{label} Snapshot Artifact",
            )
            parsed_snapshot = _require_snapshot(snapshot, label=f"{label} Snapshot")
            if parsed_artifact.kind != "ir_snapshot":
                raise IntegrityViolation(f"{label} Artifact must be ir_snapshot")
            if parsed_artifact.version_tuple.ir_snapshot_id != parsed_snapshot.snapshot_id:
                raise IntegrityViolation(f"{label} Artifact VersionTuple differs from its Snapshot")
            if not parsed_artifact.version_tuple.tool_version:
                raise IntegrityViolation(f"{label} Artifact VersionTuple lacks tool_version")

        if (
            source_patch.base_snapshot_id != material.base_snapshot.snapshot_id
            or source_patch.target_snapshot_id != material.proposed_snapshot.snapshot_id
            or source_artifact.version_tuple.ir_snapshot_id != material.base_snapshot.snapshot_id
            or not source_artifact.version_tuple.tool_version
        ):
            raise IntegrityViolation(
                "source Patch payload or VersionTuple differs from base/proposed snapshots"
            )
        if expected_ref.artifact_id != material.current_artifact.artifact_id:
            raise IntegrityViolation("current ref does not point to current Artifact")

        binding = item.target_binding
        if not isinstance(binding, PatchTargetBindingV1):
            raise IntegrityViolation("source ApprovalItem lacks a Patch target binding")
        if (
            binding.target_artifact_id != material.proposed_artifact.artifact_id
            or binding.target_snapshot_id != material.proposed_snapshot.snapshot_id
            or binding.target_digest != material.proposed_artifact.payload_hash
            or binding.ref_name != material.ref_name
        ):
            raise IntegrityViolation("source target binding differs from proposed Snapshot or ref")
        if binding.expected_ref is not None:
            if (
                binding.expected_ref.artifact_id != material.base_artifact.artifact_id
                or binding.expected_ref.revision > expected_ref.revision
            ):
                raise IntegrityViolation(
                    "source expected ref does not bind the base Artifact history"
                )

    @staticmethod
    def _compute_plan(material: RebaseMaterial) -> ThreeWayMergePlan:
        return compute_three_way_merge(
            material.base_snapshot.content_payload,
            material.current_snapshot.content_payload,
            material.proposed_snapshot.content_payload,
            material.merge_policy,
        )

    def _validate_prepared_draft(
        self,
        *,
        material: RebaseMaterial,
        compiled: CompiledRebase,
        prepared: PreparedDraft,
        context: ApprovalCommandContext,
    ) -> None:
        if type(prepared) is not PreparedDraft:
            raise IntegrityViolation("prepared rebase output must be PreparedDraft")
        item = prepared.approval_item
        if (
            prepared.expected_subject_head != material.source_head
            or item.subject_series_id != material.source_item.subject_series_id
            or item.subject_revision != material.source_item.subject_revision + 1
            or item.supersedes_approval_id != material.source_item.approval_id
            or item.proposer != context.actor
        ):
            raise IntegrityViolation(
                "prepared rebase draft does not supersede the exact source head"
            )
        if (
            item.status != "draft"
            or item.workflow_revision != 1
            or item.decisions
            or item.active_validation_run_id is not None
            or item.last_validation_failure_artifact_id is not None
            or item.evidence_set_artifact_id is not None
            or item.regression_evidence_artifact_ids
            or item.auto_apply_proof is not None
            or item.submitted_at is not None
            or item.decided_at is not None
            or item.applied_at is not None
        ):
            raise IntegrityViolation(
                "rebased ApprovalItem must be a clean draft without inherited authority"
            )

        parsed_patch = self._payloads.load_patch(prepared.subject_artifact)
        _require_canonical_model(parsed_patch, PatchV2, label="prepared rebased Patch")
        # The prepared Patch is loaded from its canonical stored bytes, so its opaque
        # replace_subgraph op values have had null-valued keys dropped by canonicalization
        # (canonical_json omits None). Compare both in the same canonical projection so a
        # byte-exact rebased Patch is accepted while any real content difference still
        # fails closed.
        if canonical_json(parsed_patch.model_dump(mode="json")) != canonical_json(
            compiled.patch.model_dump(mode="json")
        ):
            raise IntegrityViolation(
                "prepared Patch payload differs from deterministic rebase output"
            )
        expected_patch_parents = tuple(
            sorted(
                {
                    material.source_patch_artifact.artifact_id,
                    material.current_artifact.artifact_id,
                }
            )
        )
        if (
            prepared.subject_artifact.kind != "patch"
            or prepared.subject_artifact.version_tuple.ir_snapshot_id
            != material.current_snapshot.snapshot_id
            or prepared.subject_artifact.version_tuple.tool_version != REBASE_TOOL_VERSION
            or prepared.subject_artifact.lineage != expected_patch_parents
        ):
            raise IntegrityViolation(
                "prepared Patch Artifact tool_version/base binding or lineage differs"
            )

        previews = tuple(
            artifact for artifact in prepared.companion_artifacts if artifact.kind == "ir_snapshot"
        )
        if len(previews) != 1:
            raise IntegrityViolation("prepared rebased Patch requires exactly one preview Artifact")
        preview_artifact = previews[0]
        parsed_preview = self._payloads.load_snapshot(preview_artifact)
        _require_snapshot_equal(
            parsed_preview,
            compiled.preview,
            label="prepared preview Snapshot",
        )
        if (
            preview_artifact.version_tuple.ir_snapshot_id != compiled.preview.snapshot_id
            or preview_artifact.version_tuple.tool_version != REBASE_TOOL_VERSION
            or preview_artifact.lineage
            != tuple(
                sorted(
                    {
                        material.current_artifact.artifact_id,
                        prepared.subject_artifact.artifact_id,
                    }
                )
            )
        ):
            raise IntegrityViolation(
                "prepared preview Artifact tool_version/Snapshot binding or lineage differs"
            )

        binding = item.target_binding
        if not isinstance(binding, PatchTargetBindingV1) or (
            binding.target_artifact_id != preview_artifact.artifact_id
            or binding.target_snapshot_id != compiled.preview.snapshot_id
            or binding.target_digest != preview_artifact.payload_hash
            or binding.ref_name != material.ref_name
            or binding.expected_ref != material.expected_ref
        ):
            raise IntegrityViolation(
                "prepared ApprovalItem does not bind the exact rebase preview/ref"
            )

    def _capabilities(self, transaction: Any) -> RebaseWorkflowCapabilities:
        capabilities = self._bind_capabilities(transaction)
        if type(capabilities) is not RebaseWorkflowCapabilities:
            raise IntegrityViolation("rebase capability binder returned another capability set")
        if not isinstance(capabilities.approval, ApprovalCommandCapabilities):
            raise IntegrityViolation("rebase approval capabilities are invalid")
        if capabilities.conflicts is None:
            raise IntegrityViolation("conflict repository is unavailable")
        return capabilities

    @staticmethod
    def _validate_live_material(
        material: RebaseMaterial,
        capabilities: ApprovalCommandCapabilities,
        context: ApprovalCommandContext,
        *,
        stale: bool,
    ) -> None:
        approvals = _required(capabilities.approvals, label="approvals")
        artifacts = _required(capabilities.artifacts, label="artifacts")
        refs = _required(capabilities.refs, label="refs")

        retained_item = approvals.get(material.source_item.approval_id)
        retained_head = approvals.get_subject_head(material.source_item.subject_series_id)
        retained_ref = refs.get(material.ref_name)
        if (
            retained_item != material.source_item
            or retained_head != material.source_head
            or retained_ref != material.expected_ref
        ):
            error_type = StaleConflictSet if stale else Conflict
            raise error_type(
                "rebase source head, workflow revision, or ref is stale",
                approval_id=material.source_item.approval_id,
                ref_name=material.ref_name,
            )
        if context.if_match is not None:
            expected_etag = compute_resource_etag(
                resource_kind=retained_item.subject_kind,
                resource_id=retained_item.subject_artifact_id,
                revision=retained_item.workflow_revision,
            )
            if context.if_match != expected_etag:
                error_type = StaleConflictSet if stale else Conflict
                raise error_type(
                    "If-Match does not match the authoritative rebase source revision",
                    approval_id=retained_item.approval_id,
                )

        for expected in (
            material.source_patch_artifact,
            material.base_artifact,
            material.current_artifact,
            material.proposed_artifact,
        ):
            retained = artifacts.get(expected.artifact_id)
            if retained != expected:
                raise IntegrityViolation(
                    "retained rebase Artifact differs from RebaseMaterial",
                    artifact_id=expected.artifact_id,
                )

    def _early_replay(
        self,
        *,
        context: ApprovalCommandContext,
        operation: str,
        prepared_draft: PreparedDraft | None,
        expected_status: str | None = None,
    ) -> RebaseResult | None:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._capabilities(transaction)
            return self._get_replay(
                capabilities,
                context,
                operation=operation,
                prepared_draft=prepared_draft,
                expected_status=expected_status,
            )

    @staticmethod
    def _get_replay(
        capabilities: RebaseWorkflowCapabilities,
        context: ApprovalCommandContext,
        *,
        operation: str,
        prepared_draft: PreparedDraft | None,
        expected_status: str | None = None,
    ) -> RebaseResult | None:
        repository = _required(
            capabilities.approval.idempotency,
            label="idempotency",
        )
        retained = repository.get_result(
            scope=context.idempotency_scope,
            operation=operation,
            key=context.idempotency_key,
            request_hash=context.request_hash,
        )
        if retained is None:
            return None
        try:
            result = RebaseResult.model_validate(retained)
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("rebase idempotency result is malformed") from exc
        if expected_status is not None and result.status != expected_status:
            raise IntegrityViolation("rebase idempotency result has another deterministic status")

        if result.status == "clean":
            artifact_id = result.new_patch_artifact_id
            assert artifact_id is not None
            artifacts = _required(
                capabilities.approval.artifacts,
                label="artifacts",
            )
            retained_artifact = artifacts.get(artifact_id)
            if retained_artifact is None:
                raise IntegrityViolation(
                    "retained replay Patch Artifact is unavailable",
                    artifact_id=artifact_id,
                )
            parsed_artifact = _require_canonical_model(
                retained_artifact,
                ArtifactV2,
                label="retained replay Patch Artifact",
            )
            if (
                parsed_artifact.artifact_id != artifact_id
                or parsed_artifact.kind != "patch"
                or parsed_artifact.version_tuple.tool_version != REBASE_TOOL_VERSION
            ):
                raise IntegrityViolation(
                    "retained replay Patch Artifact has invalid identity or tool_version",
                    artifact_id=artifact_id,
                )
            if prepared_draft is not None:
                if type(prepared_draft) is not PreparedDraft:
                    raise IntegrityViolation(
                        "replayed prepared subject must be an exact PreparedDraft"
                    )
                prepared_subject = prepared_draft.subject_artifact
                if type(prepared_subject) is not ArtifactV2:
                    raise IntegrityViolation(
                        "replayed prepared subject must be an exact ArtifactV2"
                    )
                if prepared_subject.artifact_id != artifact_id:
                    raise IntegrityViolation(
                        "replayed result differs from the supplied prepared subject",
                        artifact_id=artifact_id,
                    )
        else:
            conflict_set_id = result.conflict_set_id
            assert conflict_set_id is not None
            retained_conflicts = capabilities.conflicts.load_bounded(conflict_set_id)
            if retained_conflicts is None:
                raise IntegrityViolation(
                    "retained replay ConflictSet is unavailable",
                    conflict_set_id=conflict_set_id,
                )
            retained_conflict_set = retained_conflicts[0]
            parsed_conflict_set = _require_canonical_model(
                retained_conflict_set,
                ConflictSet,
                label="retained replay ConflictSet",
            )
            if parsed_conflict_set.id != conflict_set_id:
                raise IntegrityViolation(
                    "retained replay ConflictSet has another ID",
                    conflict_set_id=conflict_set_id,
                )
        return result

    @staticmethod
    def _put_result(
        capabilities: ApprovalCommandCapabilities,
        context: ApprovalCommandContext,
        *,
        operation: str,
        result: RebaseResult,
    ) -> None:
        repository = _required(capabilities.idempotency, label="idempotency")
        response = result.model_dump(mode="json")
        resource_id = result.new_patch_artifact_id or result.conflict_set_id
        assert resource_id is not None
        stored = repository.put_result(
            scope=context.idempotency_scope,
            operation=operation,
            key=context.idempotency_key,
            request_hash=context.request_hash,
            resource_kind="patch" if result.status == "clean" else "conflict_set",
            resource_id=resource_id,
            response=response,
        )
        if not _same_wire(dict(stored), response):
            raise IntegrityViolation("idempotency repository stored another result")

    def _audit(
        self,
        capabilities: ApprovalCommandCapabilities,
        context: ApprovalCommandContext,
        *,
        action: str,
        resource_kind: str,
        resource_id: str,
        artifact_id: str,
    ) -> None:
        audit = _required(capabilities.audit, label="audit")
        audit.append(
            chain_id=self._audit_chain_id,
            actor=context.actor,
            initiated_by=context.initiated_by,
            action=action,
            subject=AuditSubject(
                resource_kind=resource_kind,
                resource_id=resource_id,
                artifact_id=artifact_id,
            ),
            correlation=AuditCorrelation(
                request_id=context.request_id,
                run_id=context.run_id,
                trace_id=context.trace_id,
            ),
        )

    def _load_retained_conflicts(
        self,
        conflict_set_id: str,
    ) -> tuple[ConflictSet, ConflictSetContextV1, tuple[MergeConflict, ...]]:
        with self._unit_of_work.begin() as transaction:
            capabilities = self._capabilities(transaction)
            retained = capabilities.conflicts.load_bounded(conflict_set_id)
        if retained is None:
            raise StaleConflictSet(
                "ConflictSet is unavailable",
                conflict_set_id=conflict_set_id,
            )
        return retained

    @staticmethod
    def _validate_retained_conflicts(
        *,
        retained: tuple[
            ConflictSet,
            ConflictSetContextV1,
            tuple[MergeConflict, ...],
        ],
        conflict_set_id: str,
        material: RebaseMaterial,
        plan: ThreeWayMergePlan,
    ) -> None:
        conflict_set, retained_context, conflicts = retained
        if conflict_set.id != conflict_set_id:
            raise IntegrityViolation(
                "ConflictSet repository returned another ID",
                conflict_set_id=conflict_set_id,
            )
        if len(conflicts) != conflict_set.conflict_count:
            raise IntegrityViolation(
                "ConflictSet conflicts do not match the bounded declared count"
            )
        if _conflict_set_id(conflict_set, retained_context, conflicts) != conflict_set.id:
            raise IntegrityViolation(
                "ConflictSet content-derived identity does not match its payload"
            )

        expected_context = _conflict_context(material)
        if retained_context != expected_context:
            raise StaleConflictSet(
                "ConflictSet source head, workflow, ref, or policy is stale",
                conflict_set_id=conflict_set_id,
            )
        expected = _build_conflict_set(
            material=material,
            plan=plan,
            context=expected_context,
            created_at=conflict_set.created_at,
        )
        if conflict_set != expected:
            raise StaleConflictSet(
                "ConflictSet no longer matches the exact rebase material",
                conflict_set_id=conflict_set_id,
            )
        if conflicts != plan.conflicts:
            raise IntegrityViolation(
                "retained conflict rows differ from deterministic recomputation"
            )

    @staticmethod
    def _validate_retained_conflict_metadata(
        *,
        repository: ConflictSetRepository,
        conflict_set_id: str,
        retained: tuple[
            ConflictSet,
            ConflictSetContextV1,
            tuple[MergeConflict, ...],
        ],
    ) -> None:
        current = repository.load_bounded(conflict_set_id)
        if current is None:
            raise StaleConflictSet(
                "ConflictSet is unavailable during publication",
                conflict_set_id=conflict_set_id,
            )
        if not _same_conflict_bundle(current, retained):
            raise StaleConflictSet(
                "ConflictSet changed before publication",
                conflict_set_id=conflict_set_id,
            )


__all__ = [
    "ConflictSetRepository",
    "RebaseMaterial",
    "RebasePayloadGateway",
    "RebaseWorkflowCapabilities",
    "RebaseWorkflowService",
]
