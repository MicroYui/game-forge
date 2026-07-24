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

export type RejectedGenerationChange = {
  entityId: string;
  entityTitle: string | null;
  fieldPath: string;
  newValue: number;
  oldValue: number;
};

export type ConfirmedGenerationBlocker = {
  actualValue: number;
  constraintId: string;
  entityId: string;
  fieldPath: string;
  limit: number;
  severity: "critical" | "major" | "minor";
};

export type GateRejectedGenerationOutcome = {
  blockers: ConfirmedGenerationBlocker[];
  candidate: RejectedGenerationCandidate;
  changes: RejectedGenerationChange[];
  kind: "gate-rejected";
};

export type GenerationOutcome =
  | PassedGenerationOutcome
  | GateRejectedGenerationOutcome
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

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function valueAtPath(root: Record<string, unknown>, path: string): unknown {
  let current: unknown = root;
  for (const segment of path.split(".")) {
    if (!isRecord(current) || !Object.prototype.hasOwnProperty.call(current, segment)) return undefined;
    current = current[segment];
  }
  return current;
}

function parseRejectedChangeTarget(target: string): { entityId: string; fieldPath: string } {
  const separator = target.indexOf(".");
  assertSafe(separator > 0 && separator < target.length - 1, "Rejected Patch target is malformed.");
  const entityId = target.slice(0, separator);
  const fieldPath = target.slice(separator + 1);
  assertSafe(/^[^\s.]+$/.test(entityId), "Rejected Patch entity identity is unsafe.");
  assertSafe(
    /^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$/.test(fieldPath),
    "Rejected Patch field path is unsafe.",
  );
  return { entityId, fieldPath };
}

function rejectedArtifactSummary(
  artifacts: readonly ArtifactPayloadView[],
  candidate: RejectedGenerationCandidate,
  run: RunView,
): Pick<GateRejectedGenerationOutcome, "blockers" | "changes"> {
  const byId = new Map(artifacts.map((artifact) => [artifact.artifact.artifact_id, artifact]));
  const patchView = byId.get(candidate.patch.artifact_id);
  assertSafe(
    patchView?.artifact.kind === "patch" &&
      patchView.artifact.payload_schema_id === "patch@2" &&
      isRecord(patchView.payload),
    "Rejected Patch payload is unavailable or has the wrong schema.",
  );
  const patchPayload = patchView.payload;
  assertSafe(patchPayload.patch_schema_version === "patch@2", "Rejected Patch schema marker changed.");
  assertSafe(patchPayload.produced_by === "agent", "Rejected Patch is not an Agent proposal.");
  assertSafe(
    patchPayload.producer_run_id === run.run_id,
    "Rejected Patch producer differs from the URL Run.",
  );
  assertSafe(
    patchPayload.base_snapshot_id === candidate.patch.version_tuple.ir_snapshot_id,
    "Rejected Patch exact base differs from its VersionTuple.",
  );
  assertSafe(
    patchPayload.target_snapshot_id === candidate.preview.version_tuple.ir_snapshot_id,
    "Rejected Patch target differs from the manifest preview.",
  );
  assertSafe(
    Array.isArray(patchPayload.ops) && patchPayload.ops.length > 0,
    "Rejected Patch has no typed ops.",
  );

  const changes = patchPayload.ops.map((value): RejectedGenerationChange => {
    assertSafe(isRecord(value), "Rejected Patch contains a malformed op.");
    assertSafe(
      value.op === "set_entity_attr" &&
        typeof value.op_id === "string" &&
        value.op_id.length > 0 &&
        typeof value.target === "string" &&
        isFiniteNumber(value.old_value) &&
        isFiniteNumber(value.new_value),
      "Rejected Patch contains an op that cannot be summarized safely.",
    );
    const { entityId, fieldPath } = parseRejectedChangeTarget(value.target);
    return {
      entityId,
      entityTitle: null,
      fieldPath,
      newValue: value.new_value,
      oldValue: value.old_value,
    };
  });

  const previewView = byId.get(candidate.preview.artifact_id);
  assertSafe(
    previewView?.artifact.kind === "ir_snapshot" &&
      previewView.artifact.payload_schema_id === "ir-core@1" &&
      isRecord(previewView.payload),
    "Rejected preview payload is unavailable or has the wrong schema.",
  );
  const previewPayload = previewView.payload;
  assertSafe(
    previewPayload.meta_schema_version === "meta@1" &&
      isRecord(previewPayload.entities) &&
      isRecord(previewPayload.relations),
    "Rejected preview is not a canonical IR snapshot.",
  );
  const entities = previewPayload.entities;
  for (const change of changes) {
    const entity = entities[change.entityId];
    assertSafe(
      isRecord(entity) &&
        entity.schema_version === "ir-core@1" &&
        typeof entity.type === "string" &&
        entity.type.length > 0 &&
        isRecord(entity.attrs),
      "Rejected preview does not contain the changed canonical entity.",
    );
    assertSafe(
      valueAtPath(entity.attrs, change.fieldPath) === change.newValue,
      "Rejected preview value differs from the Patch proposal.",
    );
    const title = entity.attrs.title;
    assertSafe(
      title === undefined || (typeof title === "string" && title.length > 0),
      "Rejected preview title is unsafe.",
    );
    change.entityTitle = title ?? null;
  }

  const checkerSummaries = candidate.evidence.filter(
    (artifact) => artifact.kind === "checker_run" && artifact.payload_schema_id === "checker-report@1",
  );
  assertSafe(checkerSummaries.length === 1, "Rejected candidate does not bind exactly one checker report.");
  const checkerView = byId.get(checkerSummaries[0].artifact_id);
  assertSafe(
    checkerView?.artifact.kind === "checker_run" &&
      checkerView.artifact.payload_schema_id === "checker-report@1" &&
      isRecord(checkerView.payload),
    "Rejected checker payload is unavailable or has the wrong schema.",
  );
  const checkerPayload = checkerView.payload;
  assertSafe(
    checkerPayload.payload_schema_version === "checker-report@1" &&
      checkerPayload.snapshot_id === patchPayload.target_snapshot_id &&
      Array.isArray(checkerPayload.findings),
    "Rejected checker report differs from the candidate preview.",
  );

  const blockers = checkerPayload.findings.flatMap((value): ConfirmedGenerationBlocker[] => {
    if (
      !isRecord(value) ||
      value.source !== "checker" ||
      value.oracle_type !== "deterministic" ||
      value.status !== "confirmed" ||
      value.defect_class !== "reward_out_of_range"
    ) {
      return [];
    }
    const findingEntities = value.entities;
    assertSafe(
      (value.severity === "critical" || value.severity === "major" || value.severity === "minor") &&
        typeof value.constraint_id === "string" &&
        value.constraint_id.length > 0 &&
        isStringArray(findingEntities) &&
        isRecord(value.evidence) &&
        typeof value.evidence.assert === "string" &&
        isRecord(value.evidence.violating_assignment),
      "Confirmed generation blocker is malformed.",
    );
    const assertion =
      /^([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)\s*<=\s*(-?(?:0|[1-9]\d*)(?:\.\d+)?)$/.exec(
        value.evidence.assert,
      );
    assertSafe(assertion !== null, "Confirmed generation blocker uses an unsupported assertion.");
    const fieldPath = assertion[1];
    const limit = Number(assertion[2]);
    const actualValue = value.evidence.violating_assignment[fieldPath];
    assertSafe(
      Number.isFinite(limit) && isFiniteNumber(actualValue) && actualValue > limit,
      "Confirmed generation blocker does not prove a numeric upper-bound violation.",
    );
    const matchingChanges = changes.filter(
      (change) =>
        change.fieldPath === fieldPath &&
        change.newValue === actualValue &&
        findingEntities.includes(change.entityId),
    );
    assertSafe(matchingChanges.length === 1, "Confirmed generation blocker does not bind one Patch change.");
    return [
      {
        actualValue,
        constraintId: value.constraint_id,
        entityId: matchingChanges[0].entityId,
        fieldPath,
        limit,
        severity: value.severity,
      },
    ];
  });
  assertSafe(blockers.length > 0, "Rejected candidate has no confirmed deterministic blocker.");
  return { blockers, changes };
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
    return { candidate, kind: "gate-rejected", ...rejectedArtifactSummary(artifacts, candidate, run) };
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
