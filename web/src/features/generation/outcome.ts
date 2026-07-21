import type {
  ApprovalView,
  ArtifactPayloadView,
  ConstraintSnapshotView,
  GenerationApi,
  PatchArtifactReadView,
  RunView,
  SnapshotDiffPage,
  SpecView,
  SubjectApprovalBindingView,
  VersionedResource,
} from "./api";
import {
  generationManifestArtifactIds,
  parseGenerationCandidateManifest,
  type FailedGenerationCandidate,
  type PassedGenerationCandidate,
  type RejectedGenerationCandidate,
  type UnsafeGenerationCandidate,
} from "./candidate";

export class UnsafeGenerationOutcomeError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "UnsafeGenerationOutcomeError";
  }
}

export type PassedGenerationOutcome = {
  approval: VersionedResource<ApprovalView>;
  binding: SubjectApprovalBindingView;
  baseSpec: SpecView;
  candidate: PassedGenerationCandidate;
  diff: SnapshotDiffPage;
  kind: "passed";
  patch: VersionedResource<PatchArtifactReadView>;
  previousApproval: VersionedResource<ApprovalView> | null;
  previousBinding: SubjectApprovalBindingView | null;
  previousPatch: VersionedResource<PatchArtifactReadView> | null;
  constraint: ConstraintSnapshotView;
};

export type GenerationOutcome =
  | PassedGenerationOutcome
  | { candidate: RejectedGenerationCandidate; kind: "gate-rejected" }
  | { candidate: FailedGenerationCandidate; kind: "failure" }
  | { candidate: UnsafeGenerationCandidate; kind: "unsafe" };

function manifestId(run: RunView): string | null {
  if (run.status === "succeeded") {
    if (!run.result_artifact_id || run.failure_artifact_id) return null;
    return run.result_artifact_id;
  }
  if (run.status === "failed" || run.status === "cancelled" || run.status === "timed_out") {
    if (!run.failure_artifact_id || run.result_artifact_id) return null;
    return run.failure_artifact_id;
  }
  return null;
}

function assertSafe(condition: unknown, message: string): asserts condition {
  if (!condition) throw new UnsafeGenerationOutcomeError(message);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function configConstraintArtifactId(
  artifacts: readonly ArtifactPayloadView[],
  candidate: PassedGenerationCandidate,
): string {
  const byId = new Map(artifacts.map((artifact) => [artifact.artifact.artifact_id, artifact]));
  const constraintIds = candidate.configExports.map((summary) => {
    const view = byId.get(summary.artifact_id);
    const payload = view?.payload;
    assertSafe(
      view?.artifact.kind === "config_export" &&
        view.artifact.payload_schema_id === "config-export-package@1" &&
        isRecord(payload) &&
        payload.package_schema_version === "config-export-package@1" &&
        payload.source_preview_artifact_id === candidate.preview.artifact_id &&
        typeof payload.constraint_snapshot_artifact_id === "string" &&
        payload.constraint_snapshot_artifact_id.length > 0,
      "Config export payload does not bind the candidate preview and ConstraintSnapshot.",
    );
    return payload.constraint_snapshot_artifact_id;
  });
  assertSafe(new Set(constraintIds).size === 1, "Config exports do not share one ConstraintSnapshot.");
  return constraintIds[0];
}

function sameArtifactIdentity(
  left: PassedGenerationCandidate["patch"],
  right: PatchArtifactReadView["artifact"],
): boolean {
  const tupleKeys = new Set([
    ...Object.keys(left.version_tuple),
    ...Object.keys(right.version_tuple),
  ] as (keyof typeof left.version_tuple)[]);
  return (
    left.artifact_id === right.artifact_id &&
    left.kind === right.kind &&
    left.payload_schema_id === right.payload_schema_id &&
    left.payload_hash === right.payload_hash &&
    [...tupleKeys].every((key) => left.version_tuple[key] === right.version_tuple[key])
  );
}

function assertApprovalChain(
  run: RunView,
  candidate: PassedGenerationCandidate,
  patch: VersionedResource<PatchArtifactReadView>,
  binding: SubjectApprovalBindingView,
  approval: VersionedResource<ApprovalView>,
): void {
  const patchView = patch.value;
  const item = approval.value.approval;
  const target = item.target_binding;

  assertSafe(
    sameArtifactIdentity(candidate.patch, patchView.artifact),
    "Patch read changed candidate identity.",
  );
  assertSafe(patchView.patch.produced_by === "agent", "Candidate Patch is not an Agent proposal.");
  assertSafe(patchView.patch.producer_run_id === run.run_id, "Patch producer Run differs from the URL Run.");
  assertSafe(
    candidate.patch.version_tuple.ir_snapshot_id === patchView.patch.base_snapshot_id,
    "Patch VersionTuple differs from its exact base snapshot.",
  );
  assertSafe(
    patchView.patch.target_snapshot_id === candidate.preview.version_tuple.ir_snapshot_id,
    "Patch target snapshot differs from the manifest preview.",
  );
  assertSafe(candidate.patch.payload_hash !== null, "Patch digest is unavailable.");
  assertSafe(candidate.preview.payload_hash !== null, "Preview digest is unavailable.");
  assertSafe(candidate.configExports.length > 0, "Passed candidate has no config export.");
  assertSafe(
    candidate.configExports.every(
      (artifact) => artifact.version_tuple.ir_snapshot_id === patchView.patch.target_snapshot_id,
    ),
    "Config export does not bind the candidate preview snapshot.",
  );

  assertSafe(binding.subject_kind === "patch", "Workflow binding is not for a Patch.");
  assertSafe(
    binding.subject_artifact_id === candidate.patch.artifact_id,
    "Workflow subject differs from Patch.",
  );
  assertSafe(
    binding.subject_digest === candidate.patch.payload_hash,
    "Workflow subject digest differs from Patch.",
  );
  assertSafe(
    binding.subject_revision === patchView.patch.revision,
    "Workflow subject revision differs from Patch.",
  );
  assertSafe(
    binding.subject_head_revision >= binding.subject_revision,
    "Workflow head precedes its subject.",
  );
  assertSafe(
    binding.is_current_head === (binding.subject_head_revision === binding.subject_revision),
    "Workflow head marker is inconsistent.",
  );
  assertSafe(
    binding.workflow_revision === patchView.workflow_revision,
    "Patch workflow revision changed between reads.",
  );
  assertSafe(
    binding.approval_status === patchView.approval_status,
    "Patch workflow status changed between reads.",
  );

  assertSafe(
    item.approval_id === binding.approval_id,
    "Approval identity differs from the workflow binding.",
  );
  assertSafe(item.subject_kind === "patch", "Approval subject is not a Patch.");
  assertSafe(
    item.subject_artifact_id === binding.subject_artifact_id,
    "Approval subject differs from the binding.",
  );
  assertSafe(item.subject_digest === binding.subject_digest, "Approval digest differs from the binding.");
  assertSafe(
    item.subject_revision === binding.subject_revision,
    "Approval subject revision differs from the binding.",
  );
  assertSafe(
    item.subject_series_id === binding.subject_series_id,
    "Approval series differs from the binding.",
  );
  assertSafe(
    item.workflow_revision === binding.workflow_revision,
    "Approval workflow revision differs from the binding.",
  );
  assertSafe(item.status === binding.approval_status, "Approval status differs from the binding.");
  assertSafe(target?.subject_kind === "patch", "Approval has no exact Patch target binding.");
  assertSafe(
    target.target_artifact_id === candidate.preview.artifact_id,
    "Approval target differs from the preview.",
  );
  assertSafe(
    target.target_digest === candidate.preview.payload_hash,
    "Approval target digest differs from the preview.",
  );
  assertSafe(
    target.target_snapshot_id === patchView.patch.target_snapshot_id,
    "Approval target snapshot differs from the Patch.",
  );
}

function assertRevisionChain(
  candidate: PassedGenerationCandidate,
  patch: VersionedResource<PatchArtifactReadView>,
  approval: VersionedResource<ApprovalView>,
  previousPatch: VersionedResource<PatchArtifactReadView> | null,
  previousBinding: SubjectApprovalBindingView | null,
  previousApproval: VersionedResource<ApprovalView> | null,
): void {
  const patchPayload = patch.value.patch;
  const item = approval.value.approval;
  if (candidate.runKind.kind === "generation.propose") {
    assertSafe(patchPayload.revision === 1, "Initial generation Patch is not revision 1.");
    assertSafe(
      patchPayload.supersedes_artifact_id == null,
      "Initial generation Patch unexpectedly supersedes a Patch.",
    );
    assertSafe(item.supersedes_approval_id == null, "Initial approval unexpectedly supersedes an approval.");
    return;
  }

  assertSafe(patchPayload.revision > 1, "Repair Patch did not advance the revision.");
  assertSafe(Boolean(patchPayload.supersedes_artifact_id), "Repair Patch has no predecessor.");
  assertSafe(Boolean(item.supersedes_approval_id), "Repair approval has no predecessor.");
  assertSafe(previousApproval !== null, "Repair predecessor approval was not loaded.");
  assertSafe(previousPatch !== null, "Repair predecessor Patch was not loaded.");
  assertSafe(previousBinding !== null, "Repair predecessor workflow binding was not loaded.");
  const previous = previousApproval.value.approval;
  assertSafe(
    previousPatch.value.artifact.artifact_id === patchPayload.supersedes_artifact_id,
    "Loaded predecessor Patch has a different identity.",
  );
  assertSafe(
    previousPatch.value.patch.revision + 1 === patchPayload.revision,
    "Repair Patch revision is not consecutive.",
  );
  assertSafe(previousBinding.subject_kind === "patch", "Predecessor binding is not for a Patch.");
  assertSafe(
    previousBinding.subject_artifact_id === previousPatch.value.artifact.artifact_id,
    "Predecessor binding does not bind the predecessor Patch.",
  );
  assertSafe(
    previousPatch.value.artifact.payload_hash !== null &&
      previousBinding.subject_digest === previousPatch.value.artifact.payload_hash,
    "Predecessor binding digest differs from the predecessor Patch.",
  );
  assertSafe(
    previousBinding.subject_revision === previousPatch.value.patch.revision,
    "Predecessor binding revision differs from the predecessor Patch.",
  );
  assertSafe(
    previousBinding.approval_status === previousPatch.value.approval_status &&
      previousBinding.workflow_revision === previousPatch.value.workflow_revision,
    "Predecessor Patch and workflow binding disagree.",
  );
  assertSafe(previousBinding.is_current_head === false, "Superseded predecessor is still marked current.");
  assertSafe(
    previousBinding.subject_head_revision >= previousBinding.subject_revision &&
      previousBinding.is_current_head ===
        (previousBinding.subject_head_revision === previousBinding.subject_revision),
    "Predecessor workflow head marker is inconsistent.",
  );
  assertSafe(
    previousBinding.approval_id === previous.approval_id &&
      previousBinding.approval_status === previous.status &&
      previousBinding.workflow_revision === previous.workflow_revision &&
      previousBinding.subject_digest === previous.subject_digest &&
      previousBinding.subject_revision === previous.subject_revision &&
      previousBinding.subject_series_id === previous.subject_series_id,
    "Predecessor workflow reads disagree.",
  );
  assertSafe(
    previous.approval_id === item.supersedes_approval_id,
    "Loaded predecessor approval has a different identity.",
  );
  assertSafe(
    previous.subject_artifact_id === patchPayload.supersedes_artifact_id,
    "Predecessor approval does not bind the superseded Patch.",
  );
  assertSafe(previous.subject_kind === "patch", "Predecessor approval is not for a Patch.");
  assertSafe(previous.subject_series_id === item.subject_series_id, "Repair crossed approval series.");
  assertSafe(
    previous.subject_revision + 1 === item.subject_revision,
    "Repair approval revision is not consecutive.",
  );
  assertSafe(previous.status === "superseded", "Predecessor approval is not superseded.");
  assertSafe(
    candidate.versionProjection.parents.some(
      (parent) =>
        parent.artifactId === patchPayload.supersedes_artifact_id &&
        parent.publication === "existing" &&
        parent.role === "input",
    ),
    "Repair manifest does not retain the predecessor Patch as an input.",
  );
}

export async function loadGenerationOutcome(api: GenerationApi, run: RunView): Promise<GenerationOutcome> {
  const id = manifestId(run);
  assertSafe(id !== null, "Terminal Run does not expose exactly one matching result or failure manifest.");
  const manifest = await api.getArtifact(id);
  assertSafe(manifest.artifact.artifact_id === id, "Run manifest read returned a different Artifact.");

  const artifactIds = generationManifestArtifactIds(manifest, run.run_id);
  if (artifactIds === null) {
    const malformed = parseGenerationCandidateManifest(manifest, run.run_id, []);
    assertSafe(malformed.kind === "unsafe", "Malformed manifest unexpectedly produced a candidate.");
    return { candidate: malformed, kind: "unsafe" };
  }
  const artifacts = await Promise.all(artifactIds.map((artifactId) => api.getArtifact(artifactId)));
  const candidate = parseGenerationCandidateManifest(
    manifest,
    run.run_id,
    artifacts.map((artifact) => artifact.artifact),
  );
  if (candidate.kind !== "unsafe") {
    assertSafe(
      run.attempt_no === candidate.versionProjection.attemptNo,
      "Run attempt differs from the manifest projection.",
    );
  }
  if (candidate.kind === "gate-rejected") {
    assertSafe(run.status === "failed", "Gate rejection is not bound to a failed Run.");
    return { candidate, kind: "gate-rejected" };
  }
  if (candidate.kind === "failure") {
    assertSafe(run.status !== "succeeded", "Succeeded Run points to a failure manifest.");
    return { candidate, kind: "failure" };
  }
  if (candidate.kind === "unsafe") return { candidate, kind: "unsafe" };
  assertSafe(run.status === "succeeded", "Non-success Run points to a successful candidate.");

  const patch = await api.getPatch(candidate.patch.artifact_id);
  const binding = await api.getApprovalBinding(candidate.patch.artifact_id);
  const approval = await api.getApproval(binding.approval_id);
  assertApprovalChain(run, candidate, patch, binding, approval);

  const target = approval.value.approval.target_binding;
  assertSafe(
    target?.subject_kind === "patch" && target.expected_ref != null,
    "Patch target has no exact base ref.",
  );
  const inputIds = candidate.versionProjection.parents
    .filter((parent) => parent.publication === "existing" && parent.role === "input")
    .map((parent) => parent.artifactId);
  assertSafe(
    inputIds.includes(target.expected_ref.artifact_id),
    "Exact base ref is not retained as a Run input.",
  );

  const constraintSnapshotId = candidate.patch.version_tuple.constraint_snapshot_id;
  assertSafe(Boolean(constraintSnapshotId), "Candidate Patch has no exact ConstraintSnapshot binding.");
  assertSafe(
    candidate.configExports.every(
      (artifact) => artifact.version_tuple.constraint_snapshot_id === constraintSnapshotId,
    ),
    "Config exports differ from the Patch ConstraintSnapshot binding.",
  );
  const constraintArtifactId = configConstraintArtifactId(artifacts, candidate);
  assertSafe(
    inputIds.includes(constraintArtifactId),
    "Config export ConstraintSnapshot is not retained as a Run input.",
  );

  const [baseSpec, constraint] = await Promise.all([
    api.getSpec(target.expected_ref.artifact_id),
    api.getConstraint(constraintArtifactId),
  ]);
  assertSafe(
    baseSpec.artifact.artifact_id === target.expected_ref.artifact_id &&
      baseSpec.snapshot_id === patch.value.patch.base_snapshot_id,
    "Exact base Spec differs from the Patch base snapshot.",
  );
  assertSafe(
    constraint.artifact.artifact_id === constraintArtifactId &&
      constraint.artifact.version_tuple.constraint_snapshot_id === constraintSnapshotId,
    "ConstraintSnapshot read differs from the Run input binding.",
  );

  const supersedesApprovalId = approval.value.approval.supersedes_approval_id;
  const supersedesPatchId = patch.value.patch.supersedes_artifact_id;
  const [previousPatch, previousBinding, previousApproval] = await Promise.all([
    supersedesPatchId ? api.getPatch(supersedesPatchId) : Promise.resolve(null),
    supersedesPatchId ? api.getApprovalBinding(supersedesPatchId) : Promise.resolve(null),
    supersedesApprovalId ? api.getApproval(supersedesApprovalId) : Promise.resolve(null),
  ]);
  assertRevisionChain(candidate, patch, approval, previousPatch, previousBinding, previousApproval);

  const diff = await api.getSnapshotDiff(
    patch.value.patch.base_snapshot_id,
    patch.value.patch.target_snapshot_id,
    null,
  );
  assertSafe(
    diff.diff.base_snapshot_id === patch.value.patch.base_snapshot_id &&
      diff.diff.target_snapshot_id === patch.value.patch.target_snapshot_id,
    "Diff authority returned different snapshots.",
  );
  return {
    approval,
    baseSpec,
    binding,
    candidate,
    constraint,
    diff,
    kind: "passed",
    patch,
    previousApproval,
    previousBinding,
    previousPatch,
  };
}
