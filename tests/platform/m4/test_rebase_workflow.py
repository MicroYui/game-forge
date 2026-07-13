from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from gameforge.contracts.diff import (
    CollectionIdentityV1,
    ConflictResolution,
    ConflictSet,
    ConflictSetContextV1,
    MergeConflict,
    RebaseResult,
    ThreeWayMergePolicyV1,
    compute_merge_policy_digest,
)
from gameforge.contracts.errors import (
    Conflict,
    Forbidden,
    IntegrityViolation,
    StaleConflictSet,
)
from gameforge.contracts.findings import PatchV2, TypedOp
from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.lineage import ArtifactV2, AuditActor, build_artifact_v2
from gameforge.contracts.storage import PageCursorV1, PageV1, RefValue
from gameforge.contracts.workflow import (
    ApprovalItem,
    PatchTargetBindingV1,
    SubjectHead,
)
from gameforge.platform.approvals.commands import (
    ApprovalCommandContext,
    DraftSubjectFacts,
    PreparedDraft,
    PreparedObjectBinding,
)
from gameforge.platform.diff.ir_rebase import (
    REBASE_TOOL_VERSION,
    CompiledRebase,
    compile_rebased_patch,
)
from gameforge.platform.diff.rebase import (
    RebaseMaterial,
    RebaseWorkflowCapabilities,
    RebaseWorkflowService,
)
from gameforge.platform.diff.three_way import (
    compute_three_way_merge,
    resolve_three_way_merge,
)
from gameforge.runtime.clock import FrozenUtcClock
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import apply_patch
from tests.platform.m4.test_approval_commands import (
    _artifact,
    _context,
    _draft,
    _harness,
    _location,
    _replace_item,
    _with_auto_apply_proof,
)


NOW_DT = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _policy(version: str = "merge-policy:test@1") -> ThreeWayMergePolicyV1:
    identities: tuple[CollectionIdentityV1, ...] = ()
    return ThreeWayMergePolicyV1(
        policy_version=version,
        collection_identities=identities,
        policy_digest=compute_merge_policy_digest(version, identities),
    )


def _snapshot(*, reward: int, title: str) -> Snapshot:
    return Snapshot.from_entities_relations(
        [
            Entity(
                id="quest",
                type=NodeType.QUEST,
                attrs={"reward": reward, "title": title},
            )
        ],
        [],
    )


def _source_patch(base: Snapshot, proposed: Snapshot) -> PatchV2:
    patch = PatchV2(
        revision=1,
        base_snapshot_id=base.snapshot_id,
        target_snapshot_id=proposed.snapshot_id,
        expected_to_fix=["finding:reward"],
        preconditions=[],
        side_effect_risk="low",
        ops=[
            TypedOp(
                op_id="op:set-reward",
                op="set_entity_attr",
                target="quest.reward",
                old_value=10,
                new_value=20,
            )
        ],
        produced_by="human",
        producer_run_id=None,
        rationale="repair quest reward",
    )
    assert apply_patch(base, patch).snapshot_id == proposed.snapshot_id
    return patch


@dataclass(frozen=True, slots=True)
class _StoredConflict:
    conflict_set: ConflictSet
    context: ConflictSetContextV1
    conflicts: tuple[MergeConflict, ...]


class _Conflicts:
    def __init__(self, state: Any) -> None:
        self._state = state
        self.fail_put = False

    @property
    def entries(self) -> dict[str, _StoredConflict]:
        return self._state.conflict_sets

    def put(
        self,
        conflict_set: ConflictSet,
        context: ConflictSetContextV1,
        conflicts: tuple[MergeConflict, ...],
    ) -> ConflictSet:
        retained = self.entries.get(conflict_set.id)
        candidate = _StoredConflict(conflict_set, context, conflicts)
        if retained is not None:
            if retained != candidate:
                raise IntegrityViolation("ConflictSet collision")
            return retained.conflict_set
        self.entries[conflict_set.id] = candidate
        if self.fail_put:
            raise IntegrityViolation("conflict repository unavailable")
        return conflict_set

    def get(self, conflict_set_id: str) -> ConflictSet | None:
        retained = self.entries.get(conflict_set_id)
        return None if retained is None else retained.conflict_set

    def get_context(self, conflict_set_id: str) -> ConflictSetContextV1 | None:
        retained = self.entries.get(conflict_set_id)
        return None if retained is None else retained.context

    def load_bounded(
        self,
        conflict_set_id: str,
    ) -> tuple[ConflictSet, ConflictSetContextV1, tuple[MergeConflict, ...]] | None:
        retained = self.entries.get(conflict_set_id)
        if retained is None:
            return None
        return retained.conflict_set, retained.context, retained.conflicts

    def page_conflicts(
        self,
        conflict_set_id: str,
        cursor: PageCursorV1 | None = None,
    ) -> PageV1[MergeConflict]:
        if cursor is not None:
            raise AssertionError("one-page fake received an unexpected cursor")
        retained = self.entries[conflict_set_id]
        return PageV1[MergeConflict](
            read_snapshot_id=f"conflicts:{conflict_set_id}",
            items=retained.conflicts,
            expires_at="2026-07-14T13:00:00Z",
        )


class _Payloads:
    def __init__(self) -> None:
        self.patches: dict[str, PatchV2] = {}
        self.snapshots: dict[str, Snapshot] = {}

    def load_patch(self, artifact: ArtifactV2) -> PatchV2:
        try:
            return self.patches[artifact.artifact_id]
        except KeyError as exc:
            raise IntegrityViolation("patch payload unavailable") from exc

    def load_snapshot(self, artifact: ArtifactV2) -> Snapshot:
        try:
            return self.snapshots[artifact.artifact_id]
        except KeyError as exc:
            raise IntegrityViolation("snapshot payload unavailable") from exc


@dataclass(slots=True)
class _WorkflowHarness:
    approval: Any
    conflicts: _Conflicts
    payloads: _Payloads
    service: RebaseWorkflowService
    material: RebaseMaterial


def _rich_source_item(item: ApprovalItem) -> ApprovalItem:
    item = _replace_item(
        item,
        status="validated",
        workflow_revision=2,
        evidence_set_artifact_id="artifact:evidence:old",
        regression_evidence_artifact_ids=("artifact:regression:old",),
    )
    return _with_auto_apply_proof(item)


def _workflow_harness(*, conflicted: bool) -> _WorkflowHarness:
    approval = _harness()
    approval.state.conflict_sets = {}
    conflicts = _Conflicts(approval.state)
    payloads = _Payloads()

    base = _snapshot(reward=10, title="Base")
    proposed = _snapshot(reward=20, title="Base")
    current = (
        _snapshot(reward=30, title="Base")
        if conflicted
        else _snapshot(reward=10, title="Current")
    )
    base_artifact = _artifact(
        "ir_snapshot", "a", ir_snapshot_id=base.snapshot_id
    )
    current_artifact = _artifact(
        "ir_snapshot", "b", base_artifact.artifact_id, ir_snapshot_id=current.snapshot_id
    )

    source_template = _draft(approval)
    source_artifact = _artifact(
        "patch",
        "1",
        base_artifact.artifact_id,
        ir_snapshot_id=base.snapshot_id,
    )
    proposed_artifact = _artifact(
        "ir_snapshot",
        "c",
        source_artifact.artifact_id,
        ir_snapshot_id=proposed.snapshot_id,
    )
    source_patch = _source_patch(base, proposed)
    source_binding = PatchTargetBindingV1(
        target_artifact_id=proposed_artifact.artifact_id,
        target_snapshot_id=proposed.snapshot_id,
        target_digest=proposed_artifact.payload_hash,
        ref_name="content/head",
        expected_ref=RefValue(artifact_id=base_artifact.artifact_id, revision=6),
    )
    source_item = _rich_source_item(
        _replace_item(
            source_template.approval_item,
            subject_artifact_id=source_artifact.artifact_id,
            subject_digest=source_artifact.payload_hash,
            target_binding=source_binding,
        )
    )
    source_head = SubjectHead(
        subject_series_id=source_item.subject_series_id,
        current_subject_artifact_id=source_artifact.artifact_id,
        current_approval_id=source_item.approval_id,
        revision=1,
    )
    expected_ref = RefValue(artifact_id=current_artifact.artifact_id, revision=7)

    approval.state.approvals[source_item.approval_id] = source_item
    approval.state.heads[source_item.subject_series_id] = source_head
    approval.state.refs["content/head"] = expected_ref
    for artifact in (
        base_artifact,
        current_artifact,
        proposed_artifact,
        source_artifact,
    ):
        approval.state.artifacts[artifact.artifact_id] = artifact
    approval.subjects.facts[source_artifact.artifact_id] = DraftSubjectFacts(
        subject_kind="patch",
        subject_revision=source_patch.revision,
        produced_by="human",
        producer_run_id=None,
        supersedes_artifact_id=None,
        target_artifact_id=None,
        target_snapshot_id=proposed.snapshot_id,
    )
    approval.subjects.patches[source_artifact.artifact_id] = source_patch

    payloads.patches[source_artifact.artifact_id] = source_patch
    payloads.snapshots.update(
        {
            base_artifact.artifact_id: base,
            current_artifact.artifact_id: current,
            proposed_artifact.artifact_id: proposed,
        }
    )
    material = RebaseMaterial(
        source_item=source_item,
        source_head=source_head,
        source_patch_artifact=source_artifact,
        source_patch=source_patch,
        base_artifact=base_artifact,
        base_snapshot=base,
        current_artifact=current_artifact,
        current_snapshot=current,
        proposed_artifact=proposed_artifact,
        proposed_snapshot=proposed,
        ref_name="content/head",
        expected_ref=expected_ref,
        merge_policy=_policy(),
    )
    capabilities = RebaseWorkflowCapabilities(
        approval=approval.capabilities,
        conflicts=conflicts,
    )
    service = RebaseWorkflowService(
        unit_of_work=approval.uow,
        bind_capabilities=lambda transaction: capabilities,
        approval_commands=approval.service,
        payloads=payloads,
        clock=FrozenUtcClock(NOW_DT),
        audit_chain_id="authority",
    )
    return _WorkflowHarness(
        approval=approval,
        conflicts=conflicts,
        payloads=payloads,
        service=service,
        material=material,
    )


def _compiled_for(
    harness: _WorkflowHarness,
    *,
    resolutions: tuple[ConflictResolution, ...] | None = None,
) -> CompiledRebase:
    material = harness.material
    if resolutions is None:
        plan = compute_three_way_merge(
            material.base_snapshot.content_payload,
            material.current_snapshot.content_payload,
            material.proposed_snapshot.content_payload,
            material.merge_policy,
        )
        assert plan.conflicts == ()
        resolved = plan.merged
    else:
        resolved = resolve_three_way_merge(
            material.base_snapshot.content_payload,
            material.current_snapshot.content_payload,
            material.proposed_snapshot.content_payload,
            material.merge_policy,
            resolutions,
        )
    return compile_rebased_patch(
        source_patch_artifact_id=material.source_patch_artifact.artifact_id,
        source_patch=material.source_patch,
        current=material.current_snapshot,
        resolved_view=resolved,
    )


def _prepared(harness: _WorkflowHarness, compiled: CompiledRebase) -> PreparedDraft:
    material = harness.material
    template = _draft(
        harness.approval,
        revision=material.source_item.subject_revision + 1,
        supersedes_artifact_id=material.source_patch_artifact.artifact_id,
        supersedes_approval_id=material.source_item.approval_id,
        expected_head=material.source_head,
        preview_snapshot_id=compiled.preview.snapshot_id,
    )
    subject_template = _artifact(
        "patch",
        "3",
        material.source_patch_artifact.artifact_id,
        material.current_artifact.artifact_id,
        ir_snapshot_id=material.current_snapshot.snapshot_id,
    )
    subject = build_artifact_v2(
        kind=subject_template.kind,
        version_tuple=subject_template.version_tuple.model_copy(
            update={"tool_version": REBASE_TOOL_VERSION}
        ),
        lineage=subject_template.lineage,
        payload_hash=subject_template.payload_hash,
        object_ref=subject_template.object_ref,
        created_at=subject_template.created_at,
        meta=subject_template.meta,
    )
    preview_template = _artifact(
        "ir_snapshot",
        "4",
        subject.artifact_id,
        material.current_artifact.artifact_id,
        ir_snapshot_id=compiled.preview.snapshot_id,
    )
    preview = build_artifact_v2(
        kind=preview_template.kind,
        version_tuple=preview_template.version_tuple.model_copy(
            update={"tool_version": REBASE_TOOL_VERSION}
        ),
        lineage=preview_template.lineage,
        payload_hash=preview_template.payload_hash,
        object_ref=preview_template.object_ref,
        created_at=preview_template.created_at,
        meta=preview_template.meta,
    )
    item = _replace_item(
        template.approval_item,
        subject_artifact_id=subject.artifact_id,
        subject_digest=subject.payload_hash,
        target_binding=PatchTargetBindingV1(
            target_artifact_id=preview.artifact_id,
            target_snapshot_id=compiled.preview.snapshot_id,
            target_digest=preview.payload_hash,
            ref_name=material.ref_name,
            expected_ref=material.expected_ref,
        ),
    )
    harness.approval.subjects.facts[subject.artifact_id] = (
        DraftSubjectFacts(
            subject_kind="patch",
            subject_revision=compiled.patch.revision,
            produced_by="human",
            producer_run_id=None,
            supersedes_artifact_id=material.source_patch_artifact.artifact_id,
            target_artifact_id=None,
            target_snapshot_id=compiled.preview.snapshot_id,
        )
    )
    harness.approval.subjects.patches[subject.artifact_id] = compiled.patch
    harness.payloads.patches[subject.artifact_id] = compiled.patch
    harness.payloads.snapshots[preview.artifact_id] = compiled.preview
    return PreparedDraft(
        subject_artifact=subject,
        companion_artifacts=(preview,),
        object_bindings=tuple(
            PreparedObjectBinding(
                object_ref=artifact.object_ref,
                location=_location(artifact),
                expected_revision=None,
            )
            for artifact in (subject, preview)
        ),
        approval_item=item,
        expected_subject_head=template.expected_subject_head,
    )


def _rebase_context(
    *,
    key: str = "rebase:1",
    request_hash: str = "1" * 64,
) -> ApprovalCommandContext:
    return _context(key=key, request_hash=request_hash)


def _publish_conflicts(harness: _WorkflowHarness) -> RebaseResult:
    result = harness.service.rebase(
        material=harness.material,
        prepared_draft=None,
        context=_rebase_context(key="rebase:conflicted", request_hash="2" * 64),
    )
    assert result.status == "conflicted"
    assert result.conflict_set_id is not None
    return result


def test_clean_rebase_atomically_publishes_fresh_patch_preview_and_approval() -> None:
    harness = _workflow_harness(conflicted=False)
    compiled = _compiled_for(harness)
    prepared = _prepared(harness, compiled)

    result = harness.service.rebase(
        material=harness.material,
        prepared_draft=prepared,
        context=_rebase_context(),
    )

    assert result == RebaseResult(
        status="clean",
        new_patch_artifact_id=prepared.subject_artifact.artifact_id,
    )
    assert harness.approval.state.artifacts[prepared.subject_artifact.artifact_id] == (
        prepared.subject_artifact
    )
    assert harness.approval.state.artifacts[prepared.companion_artifacts[0].artifact_id] == (
        prepared.companion_artifacts[0]
    )
    assert prepared.subject_artifact.version_tuple.tool_version == REBASE_TOOL_VERSION
    assert (
        prepared.companion_artifacts[0].version_tuple.tool_version
        == REBASE_TOOL_VERSION
    )
    old = harness.approval.state.approvals[harness.material.source_item.approval_id]
    assert old.status == "superseded"
    assert old.workflow_revision == harness.material.source_item.workflow_revision + 1
    assert harness.approval.state.approvals[prepared.approval_item.approval_id] == (
        prepared.approval_item
    )
    assert harness.approval.state.heads[prepared.approval_item.subject_series_id] == (
        SubjectHead(
            subject_series_id=prepared.approval_item.subject_series_id,
            current_subject_artifact_id=prepared.subject_artifact.artifact_id,
            current_approval_id=prepared.approval_item.approval_id,
            revision=harness.material.source_head.revision + 1,
        )
    )
    assert harness.approval.uow.begins == 2
    assert harness.approval.uow.commits == 2


def test_rebased_approval_is_a_clean_draft_and_requires_independent_validation() -> None:
    harness = _workflow_harness(conflicted=False)
    prepared = _prepared(harness, _compiled_for(harness))

    harness.service.rebase(
        material=harness.material,
        prepared_draft=prepared,
        context=_rebase_context(),
    )

    source = harness.material.source_item
    assert source.evidence_set_artifact_id is not None
    assert source.regression_evidence_artifact_ids
    assert source.auto_apply_proof is not None
    fresh = harness.approval.state.approvals[prepared.approval_item.approval_id]
    assert fresh.status == "draft"
    assert fresh.workflow_revision == 1
    assert fresh.decisions == ()
    assert fresh.evidence_set_artifact_id is None
    assert fresh.regression_evidence_artifact_ids == ()
    assert fresh.auto_apply_proof is None
    assert fresh.active_validation_run_id is None
    assert fresh.last_validation_failure_artifact_id is None
    assert fresh.submitted_at is None
    assert fresh.decided_at is None
    assert fresh.applied_at is None


def test_conflicted_rebase_only_publishes_immutable_conflicts_audit_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _workflow_harness(conflicted=True)
    before_approvals = copy.deepcopy(harness.approval.state.approvals)
    before_heads = copy.deepcopy(harness.approval.state.heads)
    before_artifacts = copy.deepcopy(harness.approval.state.artifacts)

    result = _publish_conflicts(harness)

    assert harness.approval.state.approvals == before_approvals
    assert harness.approval.state.heads == before_heads
    assert harness.approval.state.artifacts == before_artifacts
    stored = harness.conflicts.entries[result.conflict_set_id]
    assert stored.conflict_set.proposed_patch_artifact_id == (
        harness.material.source_patch_artifact.artifact_id
    )
    assert stored.context.expected_subject_artifact_id == (
        harness.material.source_item.subject_artifact_id
    )
    assert stored.context.expected_subject_head_revision == (
        harness.material.source_head.revision
    )
    assert stored.context.expected_workflow_revision == (
        harness.material.source_item.workflow_revision
    )
    assert stored.context.expected_ref == harness.material.expected_ref
    assert len(harness.approval.state.audit) == 1
    assert len(harness.approval.state.idempotency) == 1

    def payload_must_not_load(*args: object, **kwargs: object) -> object:
        raise AssertionError("idempotent replay touched the payload gateway")

    monkeypatch.setattr(harness.payloads, "load_patch", payload_must_not_load)
    monkeypatch.setattr(harness.payloads, "load_snapshot", payload_must_not_load)
    replay = _publish_conflicts(harness)
    assert replay == result
    assert len(harness.conflicts.entries) == 1
    assert len(harness.approval.state.audit) == 1
    assert len(harness.approval.state.idempotency) == 1

    with pytest.raises(Conflict):
        harness.service.rebase(
            material=harness.material,
            prepared_draft=None,
            context=_rebase_context(
                key="rebase:conflicted",
                request_hash="9" * 64,
            ),
        )


def test_conflicted_rebase_replays_a_winner_after_a_stale_early_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _workflow_harness(conflicted=True)
    capabilities = RebaseWorkflowCapabilities(
        approval=harness.approval.capabilities,
        conflicts=harness.conflicts,
    )
    stale_reader = RebaseWorkflowService(
        unit_of_work=harness.approval.uow,
        bind_capabilities=lambda transaction: capabilities,
        approval_commands=harness.approval.service,
        payloads=harness.payloads,
        clock=FrozenUtcClock(NOW_DT + timedelta(seconds=1)),
        audit_chain_id="authority",
    )
    monkeypatch.setattr(stale_reader, "_early_replay", lambda **kwargs: None)

    winner = _publish_conflicts(harness)
    replay = stale_reader.rebase(
        material=harness.material,
        prepared_draft=None,
        context=_rebase_context(key="rebase:conflicted", request_hash="2" * 64),
    )

    assert replay == winner
    assert len(harness.conflicts.entries) == 1
    assert len(harness.approval.state.audit) == 1
    assert len(harness.approval.state.idempotency) == 1


@pytest.mark.parametrize(
    ("choice", "custom_value", "expected_reward"),
    [
        ("keep_current", None, 30),
        ("take_proposed", None, 20),
        ("custom", 25, 25),
    ],
)
def test_human_can_resolve_each_closed_choice_into_a_fresh_draft(
    choice: str,
    custom_value: object,
    expected_reward: int,
) -> None:
    harness = _workflow_harness(conflicted=True)
    conflicted = _publish_conflicts(harness)
    stored = harness.conflicts.entries[conflicted.conflict_set_id]
    (merge_conflict,) = stored.conflicts
    kwargs = {"custom_value": custom_value} if choice == "custom" else {}
    resolution = ConflictResolution(
        conflict_id=merge_conflict.id,
        choice=choice,
        **kwargs,
    )
    resolutions = (resolution,)
    compiled = _compiled_for(harness, resolutions=resolutions)
    prepared = _prepared(harness, compiled)

    result = harness.service.resolve_conflicts(
        material=harness.material,
        conflict_set_id=conflicted.conflict_set_id,
        resolutions=resolutions,
        prepared_draft=prepared,
        context=_rebase_context(key=f"resolve:{choice}", request_hash="3" * 64),
    )

    assert result.status == "clean"
    published = harness.payloads.load_snapshot(prepared.companion_artifacts[0])
    reward = published.entities["quest"].attrs["reward"]
    assert reward == expected_reward
    assert type(reward) is int
    assert harness.approval.state.approvals[prepared.approval_item.approval_id].status == (
        "draft"
    )


@pytest.mark.parametrize("case", ["missing", "duplicate", "unknown"])
def test_resolution_must_cover_every_known_conflict_exactly_once(case: str) -> None:
    harness = _workflow_harness(conflicted=True)
    result = _publish_conflicts(harness)
    (merge_conflict,) = harness.conflicts.entries[result.conflict_set_id].conflicts
    valid = ConflictResolution(conflict_id=merge_conflict.id, choice="keep_current")
    unknown = ConflictResolution(conflict_id="conflict:unknown", choice="keep_current")
    resolutions = {
        "missing": (),
        "duplicate": (valid, valid),
        "unknown": (unknown,),
    }[case]
    compiled = compile_rebased_patch(
        source_patch_artifact_id=harness.material.source_patch_artifact.artifact_id,
        source_patch=harness.material.source_patch,
        current=harness.material.current_snapshot,
        resolved_view=harness.material.current_snapshot.content_payload,
    )
    prepared = _prepared(harness, compiled)

    with pytest.raises(ValueError, match="every conflict exactly once|duplicate"):
        harness.service.resolve_conflicts(
            material=harness.material,
            conflict_set_id=result.conflict_set_id,
            resolutions=resolutions,
            prepared_draft=prepared,
            context=_rebase_context(key=f"resolve:{case}", request_hash="4" * 64),
        )

    assert harness.approval.state.heads[harness.material.source_head.subject_series_id] == (
        harness.material.source_head
    )


def test_conflict_resolution_is_human_only() -> None:
    harness = _workflow_harness(conflicted=True)
    result = _publish_conflicts(harness)
    (merge_conflict,) = harness.conflicts.entries[result.conflict_set_id].conflicts
    resolutions = (
        ConflictResolution(conflict_id=merge_conflict.id, choice="keep_current"),
    )
    prepared = _prepared(harness, _compiled_for(harness, resolutions=resolutions))
    context = ApprovalCommandContext(
        actor=AuditActor(principal_id="service:worker", principal_kind="service"),
        initiated_by=AuditActor(
            principal_id="human:maker",
            principal_kind="human",
        ),
        request_id="resolve:service",
        idempotency_scope="principal:service:worker",
        idempotency_key="resolve:service",
        request_hash="5" * 64,
    )

    with pytest.raises(Forbidden):
        harness.service.resolve_conflicts(
            material=harness.material,
            conflict_set_id=result.conflict_set_id,
            resolutions=resolutions,
            prepared_draft=prepared,
            context=context,
        )


@pytest.mark.parametrize("conflicted", [False, True])
def test_initial_rebase_is_human_only(conflicted: bool) -> None:
    harness = _workflow_harness(conflicted=conflicted)
    prepared = None if conflicted else _prepared(harness, _compiled_for(harness))
    context = ApprovalCommandContext(
        actor=AuditActor(principal_id="service:worker", principal_kind="service"),
        request_id="rebase:service",
        idempotency_scope="principal:service:worker",
        idempotency_key="rebase:service",
        request_hash="a" * 64,
    )

    with pytest.raises(Forbidden):
        harness.service.rebase(
            material=harness.material,
            prepared_draft=prepared,
            context=context,
        )

    assert harness.approval.uow.begins == 0


@pytest.mark.parametrize(
    "drift",
    ["ref_artifact", "ref_revision", "subject_head", "workflow", "merge_policy"],
)
def test_conflict_resolution_rejects_every_stale_binding(drift: str) -> None:
    harness = _workflow_harness(conflicted=True)
    result = _publish_conflicts(harness)
    (merge_conflict,) = harness.conflicts.entries[result.conflict_set_id].conflicts
    resolutions = (
        ConflictResolution(conflict_id=merge_conflict.id, choice="keep_current"),
    )
    prepared = _prepared(harness, _compiled_for(harness, resolutions=resolutions))
    material = harness.material
    if drift == "ref_artifact":
        harness.approval.state.refs[material.ref_name] = RefValue(
            artifact_id="artifact:concurrent",
            revision=material.expected_ref.revision,
        )
    elif drift == "ref_revision":
        harness.approval.state.refs[material.ref_name] = RefValue(
            artifact_id=material.expected_ref.artifact_id,
            revision=material.expected_ref.revision + 1,
        )
    elif drift == "subject_head":
        harness.approval.state.heads[material.source_head.subject_series_id] = SubjectHead(
            subject_series_id=material.source_head.subject_series_id,
            current_subject_artifact_id="artifact:concurrent",
            current_approval_id=material.source_head.current_approval_id,
            revision=material.source_head.revision + 1,
        )
    elif drift == "workflow":
        harness.approval.state.approvals[material.source_item.approval_id] = _replace_item(
            material.source_item,
            workflow_revision=material.source_item.workflow_revision + 1,
        )
    else:
        material = replace(material, merge_policy=_policy("merge-policy:test@2"))
        changed_plan = compute_three_way_merge(
            material.base_snapshot.content_payload,
            material.current_snapshot.content_payload,
            material.proposed_snapshot.content_payload,
            material.merge_policy,
        )
        (changed_conflict,) = changed_plan.conflicts
        resolutions = (
            ConflictResolution(
                conflict_id=changed_conflict.id,
                choice="keep_current",
            ),
        )
        prepared = _prepared(
            harness,
            compile_rebased_patch(
                source_patch_artifact_id=material.source_patch_artifact.artifact_id,
                source_patch=material.source_patch,
                current=material.current_snapshot,
                resolved_view=resolve_three_way_merge(
                    material.base_snapshot.content_payload,
                    material.current_snapshot.content_payload,
                    material.proposed_snapshot.content_payload,
                    material.merge_policy,
                    resolutions,
                ),
            ),
        )

    with pytest.raises(StaleConflictSet):
        harness.service.resolve_conflicts(
            material=material,
            conflict_set_id=result.conflict_set_id,
            resolutions=resolutions,
            prepared_draft=prepared,
            context=_rebase_context(key=f"resolve:stale:{drift}", request_hash="6" * 64),
        )


def test_conflict_resolution_rechecks_the_complete_set_before_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _workflow_harness(conflicted=True)
    result = _publish_conflicts(harness)
    retained = harness.conflicts.entries[result.conflict_set_id]
    (merge_conflict,) = retained.conflicts
    resolutions = (
        ConflictResolution(conflict_id=merge_conflict.id, choice="keep_current"),
    )
    prepared = _prepared(harness, _compiled_for(harness, resolutions=resolutions))
    original_load = harness.service._load_retained_conflicts

    def load_then_change(
        conflict_set_id: str,
    ) -> tuple[ConflictSet, ConflictSetContextV1, tuple[MergeConflict, ...]]:
        loaded = original_load(conflict_set_id)
        stored = harness.conflicts.entries[conflict_set_id]
        changed_wire = stored.conflicts[0].model_dump(mode="python")
        changed_wire["base"]["value"] = float(changed_wire["base"]["value"])
        changed = MergeConflict.model_validate(changed_wire)
        harness.conflicts.entries[conflict_set_id] = replace(
            stored,
            conflicts=(changed,),
        )
        return loaded

    monkeypatch.setattr(harness.service, "_load_retained_conflicts", load_then_change)

    with pytest.raises(StaleConflictSet, match="changed before publication"):
        harness.service.resolve_conflicts(
            material=harness.material,
            conflict_set_id=result.conflict_set_id,
            resolutions=resolutions,
            prepared_draft=prepared,
            context=_rebase_context(
                key="resolve:complete-recheck",
                request_hash="6" * 64,
            ),
        )


def test_clean_rebase_idempotency_replays_without_duplicate_publication_or_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _workflow_harness(conflicted=False)
    prepared = _prepared(harness, _compiled_for(harness))
    context = _rebase_context(key="rebase:replay", request_hash="7" * 64)

    first = harness.service.rebase(
        material=harness.material,
        prepared_draft=prepared,
        context=context,
    )
    audit_count = len(harness.approval.state.audit)
    idempotency_count = len(harness.approval.state.idempotency)
    artifacts = copy.deepcopy(harness.approval.state.artifacts)
    approvals = copy.deepcopy(harness.approval.state.approvals)
    head = harness.approval.state.heads[prepared.approval_item.subject_series_id]

    def payload_must_not_load(*args: object, **kwargs: object) -> object:
        raise AssertionError("idempotent replay touched the payload gateway")

    monkeypatch.setattr(harness.payloads, "load_patch", payload_must_not_load)
    monkeypatch.setattr(harness.payloads, "load_snapshot", payload_must_not_load)
    begins_before_replay = harness.approval.uow.begins
    replay = harness.service.rebase(
        material=harness.material,
        prepared_draft=None,
        context=context,
    )

    assert replay == first
    assert harness.approval.state.artifacts == artifacts
    assert harness.approval.state.approvals == approvals
    assert harness.approval.state.heads[head.subject_series_id] == head
    assert len(harness.approval.state.audit) == audit_count
    assert len(harness.approval.state.idempotency) == idempotency_count
    assert harness.approval.uow.begins == begins_before_replay + 1

    with pytest.raises(Conflict):
        harness.service.rebase(
            material=harness.material,
            prepared_draft=None,
            context=_rebase_context(
                key=context.idempotency_key,
                request_hash="8" * 64,
            ),
        )


def test_resolve_idempotency_replays_and_rejects_another_request_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _workflow_harness(conflicted=True)
    conflicted = _publish_conflicts(harness)
    (merge_conflict,) = harness.conflicts.entries[conflicted.conflict_set_id].conflicts
    resolutions = (
        ConflictResolution(conflict_id=merge_conflict.id, choice="keep_current"),
    )
    prepared = _prepared(harness, _compiled_for(harness, resolutions=resolutions))
    context = _rebase_context(key="resolve:replay", request_hash="b" * 64)

    first = harness.service.resolve_conflicts(
        material=harness.material,
        conflict_set_id=conflicted.conflict_set_id,
        resolutions=resolutions,
        prepared_draft=prepared,
        context=context,
    )
    audit_count = len(harness.approval.state.audit)
    idempotency_count = len(harness.approval.state.idempotency)
    artifacts = copy.deepcopy(harness.approval.state.artifacts)
    approvals = copy.deepcopy(harness.approval.state.approvals)

    def payload_must_not_load(*args: object, **kwargs: object) -> object:
        raise AssertionError("idempotent replay touched the payload gateway")

    monkeypatch.setattr(harness.payloads, "load_patch", payload_must_not_load)
    monkeypatch.setattr(harness.payloads, "load_snapshot", payload_must_not_load)
    replay = harness.service.resolve_conflicts(
        material=harness.material,
        conflict_set_id=conflicted.conflict_set_id,
        resolutions=resolutions,
        prepared_draft=prepared,
        context=context,
    )

    assert replay == first
    assert harness.approval.state.artifacts == artifacts
    assert harness.approval.state.approvals == approvals
    assert len(harness.approval.state.audit) == audit_count
    assert len(harness.approval.state.idempotency) == idempotency_count

    with pytest.raises(Conflict):
        harness.service.resolve_conflicts(
            material=harness.material,
            conflict_set_id=conflicted.conflict_set_id,
            resolutions=resolutions,
            prepared_draft=prepared,
            context=_rebase_context(
                key=context.idempotency_key,
                request_hash="c" * 64,
            ),
        )


@pytest.mark.parametrize("status", ["clean", "conflicted"])
def test_idempotent_replay_requires_its_retained_resource(status: str) -> None:
    harness = _workflow_harness(conflicted=status == "conflicted")
    if status == "clean":
        prepared = _prepared(harness, _compiled_for(harness))
        context = _rebase_context(key="rebase:missing-clean", request_hash="d" * 64)
        first = harness.service.rebase(
            material=harness.material,
            prepared_draft=prepared,
            context=context,
        )
        assert first.new_patch_artifact_id is not None
        del harness.approval.state.artifacts[first.new_patch_artifact_id]
    else:
        prepared = None
        context = _rebase_context(key="rebase:missing-conflict", request_hash="e" * 64)
        first = harness.service.rebase(
            material=harness.material,
            prepared_draft=None,
            context=context,
        )
        assert first.conflict_set_id is not None
        del harness.conflicts.entries[first.conflict_set_id]

    with pytest.raises(IntegrityViolation, match="retained .* unavailable"):
        harness.service.rebase(
            material=harness.material,
            prepared_draft=prepared,
            context=context,
        )


def test_clean_replay_must_match_a_supplied_prepared_subject() -> None:
    harness = _workflow_harness(conflicted=False)
    prepared = _prepared(harness, _compiled_for(harness))
    context = _rebase_context(key="rebase:prepared-closure", request_hash="f" * 64)
    harness.service.rebase(
        material=harness.material,
        prepared_draft=prepared,
        context=context,
    )
    another_subject = prepared.subject_artifact.model_copy(
        update={"artifact_id": "artifact:another-rebased-patch"}
    )
    mismatched = prepared.model_copy(update={"subject_artifact": another_subject})

    with pytest.raises(IntegrityViolation, match="prepared subject"):
        harness.service.rebase(
            material=harness.material,
            prepared_draft=mismatched,
            context=context,
        )


def test_conflicted_replay_requires_the_repository_id_to_match() -> None:
    harness = _workflow_harness(conflicted=True)
    context = _rebase_context(key="rebase:conflict-id", request_hash="0" * 64)
    first = harness.service.rebase(
        material=harness.material,
        prepared_draft=None,
        context=context,
    )
    assert first.conflict_set_id is not None
    retained = harness.conflicts.entries[first.conflict_set_id]
    harness.conflicts.entries[first.conflict_set_id] = replace(
        retained,
        conflict_set=retained.conflict_set.model_copy(
            update={"id": "conflict-set:another"}
        ),
    )

    with pytest.raises(IntegrityViolation, match="another ID"):
        harness.service.rebase(
            material=harness.material,
            prepared_draft=None,
            context=context,
        )


@pytest.mark.parametrize("mismatch", ["source_patch", "base_snapshot"])
def test_material_payload_must_match_the_verified_source_object(
    mismatch: str,
) -> None:
    harness = _workflow_harness(conflicted=False)
    prepared = _prepared(harness, _compiled_for(harness))
    if mismatch == "source_patch":
        harness.payloads.patches[harness.material.source_patch_artifact.artifact_id] = (
            harness.material.source_patch.model_copy(
                update={"rationale": "different verified bytes"}
            )
        )
    else:
        harness.payloads.snapshots[harness.material.base_artifact.artifact_id] = (
            _snapshot(reward=11, title="Base")
        )

    with pytest.raises(IntegrityViolation, match="differs from RebaseMaterial"):
        harness.service.rebase(
            material=harness.material,
            prepared_draft=prepared,
            context=_rebase_context(key=f"rebase:material-mismatch:{mismatch}"),
        )

    assert harness.approval.uow.begins == 1


def test_source_patch_must_reproduce_the_claimed_proposed_snapshot() -> None:
    harness = _workflow_harness(conflicted=False)
    unrelated_patch = harness.material.source_patch.model_copy(update={"ops": []})
    harness.material = replace(harness.material, source_patch=unrelated_patch)
    harness.payloads.patches[
        harness.material.source_patch_artifact.artifact_id
    ] = unrelated_patch

    with pytest.raises(
        IntegrityViolation,
        match="source Patch does not reproduce the proposed Snapshot",
    ):
        harness.service.rebase(
            material=harness.material,
            prepared_draft=None,
            context=_rebase_context(key="rebase:source-closure"),
        )

    assert harness.approval.uow.begins == 1


@pytest.mark.parametrize("mismatch", ["patch", "preview"])
def test_prepared_payload_must_exactly_match_deterministic_compile(
    mismatch: str,
) -> None:
    harness = _workflow_harness(conflicted=False)
    prepared = _prepared(harness, _compiled_for(harness))
    before = copy.deepcopy(harness.approval.state.__dict__)
    if mismatch == "patch":
        harness.payloads.patches[prepared.subject_artifact.artifact_id] = (
            harness.material.source_patch
        )
    else:
        harness.payloads.snapshots[prepared.companion_artifacts[0].artifact_id] = (
            harness.material.current_snapshot
        )

    with pytest.raises(IntegrityViolation):
        harness.service.rebase(
            material=harness.material,
            prepared_draft=prepared,
            context=_rebase_context(key=f"rebase:mismatch:{mismatch}"),
        )

    assert harness.approval.state.__dict__ == before
    assert harness.approval.uow.begins == 1


@pytest.mark.parametrize("artifact_kind", ["patch", "preview"])
def test_prepared_rebase_artifacts_require_the_exact_rebase_tool_version(
    artifact_kind: str,
) -> None:
    harness = _workflow_harness(conflicted=False)
    prepared = _prepared(harness, _compiled_for(harness))
    if artifact_kind == "patch":
        subject = prepared.subject_artifact.model_copy(
            update={
                "version_tuple": prepared.subject_artifact.version_tuple.model_copy(
                    update={"tool_version": "another-rebase@1"}
                )
            }
        )
        prepared = prepared.model_copy(update={"subject_artifact": subject})
    else:
        preview = prepared.companion_artifacts[0].model_copy(
            update={
                "version_tuple": prepared.companion_artifacts[
                    0
                ].version_tuple.model_copy(
                    update={"tool_version": "another-rebase@1"}
                )
            }
        )
        prepared = prepared.model_copy(update={"companion_artifacts": (preview,)})

    with pytest.raises(IntegrityViolation, match="tool_version"):
        harness.service.rebase(
            material=harness.material,
            prepared_draft=prepared,
            context=_rebase_context(key=f"rebase:tool-version:{artifact_kind}"),
        )


@pytest.mark.parametrize("artifact_kind", ["patch", "preview"])
def test_prepared_rebase_artifacts_require_exact_typed_lineage(
    artifact_kind: str,
) -> None:
    harness = _workflow_harness(conflicted=False)
    prepared = _prepared(harness, _compiled_for(harness))
    if artifact_kind == "patch":
        subject = prepared.subject_artifact.model_copy(
            update={
                "lineage": (harness.material.source_patch_artifact.artifact_id,),
            }
        )
        prepared = prepared.model_copy(update={"subject_artifact": subject})
    else:
        preview = prepared.companion_artifacts[0].model_copy(
            update={"lineage": (prepared.subject_artifact.artifact_id,)}
        )
        prepared = prepared.model_copy(update={"companion_artifacts": (preview,)})

    with pytest.raises(IntegrityViolation, match="lineage"):
        harness.service.rebase(
            material=harness.material,
            prepared_draft=prepared,
            context=_rebase_context(key=f"rebase:lineage:{artifact_kind}"),
        )


@pytest.mark.parametrize(
    "failure",
    ["repository", "participant", "audit", "idempotency"],
)
def test_clean_rebase_rolls_back_every_authoritative_write_on_failure(
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _workflow_harness(conflicted=False)
    prepared = _prepared(harness, _compiled_for(harness))
    capabilities = harness.approval.capabilities
    before = copy.deepcopy(harness.approval.state.__dict__)

    if failure == "repository":
        assert capabilities.artifacts is not None
        original_put = capabilities.artifacts.put
        calls = 0

        def fail_second_put(artifact: ArtifactV2) -> ArtifactV2:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise IntegrityViolation("artifact repository unavailable")
            return original_put(artifact)

        monkeypatch.setattr(capabilities.artifacts, "put", fail_second_put)
    elif failure == "participant":
        harness.approval.lineage.fail = True
    elif failure == "audit":
        harness.approval.audit.fail = True
    else:
        assert capabilities.idempotency is not None

        def fail_idempotency(**kwargs: Any) -> dict[str, Any]:
            raise IntegrityViolation("idempotency repository unavailable")

        monkeypatch.setattr(capabilities.idempotency, "put_result", fail_idempotency)

    with pytest.raises(IntegrityViolation):
        harness.service.rebase(
            material=harness.material,
            prepared_draft=prepared,
            context=_rebase_context(key=f"rebase:failure:{failure}"),
        )

    assert harness.approval.state.__dict__ == before
    assert harness.approval.uow.rollbacks == 1


def test_conflict_repository_failure_rolls_back_conflict_audit_and_idempotency() -> None:
    harness = _workflow_harness(conflicted=True)
    harness.conflicts.fail_put = True
    before = copy.deepcopy(harness.approval.state.__dict__)

    with pytest.raises(IntegrityViolation):
        harness.service.rebase(
            material=harness.material,
            prepared_draft=None,
            context=_rebase_context(key="rebase:conflict-failure"),
        )

    assert harness.approval.state.__dict__ == before
    assert harness.approval.uow.rollbacks == 1
