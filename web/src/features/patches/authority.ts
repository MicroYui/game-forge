import type {
  ApprovalView,
  ArtifactPayloadView,
  PatchArtifactReadView,
  RefHistoryEntry,
  RefValue,
  RollbackRequestReadView,
  SubjectApprovalBindingView,
  WorkflowApplyRequest,
  WorkflowApplyResult,
} from "./api";

type ApprovalRecord = ApprovalView["approval"];
type TargetBinding = NonNullable<ApprovalRecord["target_binding"]>;
export type PatchTargetBinding = Extract<TargetBinding, { subject_kind: "patch" }>;
export type RollbackTargetBinding = Extract<TargetBinding, { subject_kind: "rollback_request" }>;

export interface PatchAuthorityProjection {
  subject: PatchArtifactReadView;
  binding: SubjectApprovalBindingView;
  approval: ApprovalView;
}

export interface RollbackAuthorityProjection {
  approval: ApprovalView;
  binding: SubjectApprovalBindingView;
  history: readonly RefHistoryEntry[];
  historyNextCursor: string | null | undefined;
  subject: RollbackRequestReadView;
  targetArtifact: ArtifactPayloadView;
}

export class PatchAuthorityError extends Error {
  override name = "PatchAuthorityError";
}

function reject(message: string): never {
  throw new PatchAuthorityError(message);
}

function requireText(value: string | null | undefined, field: string): string {
  if (typeof value !== "string" || value.length === 0) reject(`${field} is missing.`);
  return value;
}

function requireDigest(value: string | null | undefined, field: string): string {
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value)) {
    reject(`${field} is not a canonical SHA-256 digest.`);
  }
  return value;
}

function requirePositiveInteger(value: number, field: string): void {
  if (!Number.isSafeInteger(value) || value < 1) reject(`${field} is not a positive integer.`);
}

function equalRef(left: RefValue | null | undefined, right: RefValue | null | undefined): boolean {
  const normalizedLeft = left ?? null;
  const normalizedRight = right ?? null;
  if (normalizedLeft === null || normalizedRight === null) return normalizedLeft === normalizedRight;
  return (
    normalizedLeft.artifact_id === normalizedRight.artifact_id &&
    normalizedLeft.revision === normalizedRight.revision
  );
}

function requireRef(value: RefValue | null | undefined, field: string): void {
  if (value == null) return;
  requireText(value.artifact_id, `${field}.artifact_id`);
  requirePositiveInteger(value.revision, `${field}.revision`);
}

function freezeRef(value: RefValue | null | undefined): Readonly<RefValue> | null {
  if (value == null) return null;
  return Object.freeze({ artifact_id: value.artifact_id, revision: value.revision });
}

function verifyApprovalBinding(
  subject: PatchArtifactReadView | RollbackRequestReadView,
  binding: SubjectApprovalBindingView,
  view: ApprovalView,
  expectedKind: "patch" | "rollback_request",
): ApprovalRecord {
  const artifact = subject.artifact;
  const item = view.approval;
  const digest = requireDigest(artifact.payload_hash, "subject Artifact payload_hash");

  if (view.view_schema_version !== "approval-view@1") reject("Approval view schema is unsupported.");
  if (item.approval_schema_version !== "approval@1") reject("Approval schema is unsupported.");
  if (binding.subject_kind !== expectedKind || item.subject_kind !== expectedKind) {
    reject("Approval subject kind does not match the workflow subject.");
  }
  if (
    binding.subject_artifact_id !== artifact.artifact_id ||
    item.subject_artifact_id !== artifact.artifact_id
  ) {
    reject("Approval does not bind the exact subject Artifact.");
  }
  if (binding.subject_digest !== digest || item.subject_digest !== digest) {
    reject("Approval does not bind the exact subject digest.");
  }
  if (binding.approval_id !== item.approval_id) reject("Approval ID binding is inconsistent.");
  if (
    binding.subject_revision !== item.subject_revision ||
    binding.subject_series_id !== item.subject_series_id
  ) {
    reject("Approval does not bind the exact subject series revision.");
  }
  requirePositiveInteger(binding.subject_revision, "subject revision");
  requirePositiveInteger(binding.subject_head_revision, "subject head revision");
  if (binding.subject_head_revision < binding.subject_revision) {
    reject("Subject head revision precedes this immutable subject revision.");
  }
  if (binding.is_current_head !== (binding.subject_head_revision === binding.subject_revision)) {
    reject("Subject head flag disagrees with the exact head revision.");
  }
  if (
    subject.workflow_revision !== binding.workflow_revision ||
    subject.workflow_revision !== item.workflow_revision
  ) {
    reject("Workflow revision binding is inconsistent.");
  }
  requirePositiveInteger(subject.workflow_revision, "workflow revision");
  if (subject.approval_status !== binding.approval_status || subject.approval_status !== item.status) {
    reject("Workflow status binding is inconsistent.");
  }
  return item;
}

function verifyPatchArtifact(subject: PatchArtifactReadView): void {
  const { artifact, patch } = subject;
  if (subject.view_schema_version !== "patch-artifact-read-view@1") {
    reject("Patch read view schema is unsupported.");
  }
  if (
    artifact.summary_schema_version !== "artifact-summary@1" ||
    artifact.lineage_schema_version !== "lineage@2" ||
    artifact.kind !== "patch" ||
    artifact.payload_schema_id !== "patch@2"
  ) {
    reject("Patch Artifact envelope is not the required lineage@2 patch@2 projection.");
  }
  requireText(artifact.artifact_id, "Patch Artifact ID");
  requireDigest(artifact.payload_hash, "Patch Artifact payload_hash");
  if (patch.patch_schema_version !== "patch@2") reject("Patch payload schema is unsupported.");
  requireText(patch.base_snapshot_id, "Patch base_snapshot_id");
  requireText(patch.target_snapshot_id, "Patch target_snapshot_id");
  if (artifact.version_tuple.ir_snapshot_id !== patch.base_snapshot_id) {
    reject("Patch VersionTuple does not bind the exact base snapshot.");
  }
  requirePositiveInteger(patch.revision, "Patch revision");
  const supersedes = patch.supersedes_artifact_id ?? null;
  if (patch.revision === 1 && supersedes !== null) {
    reject("Initial Patch revision cannot supersede another Patch.");
  }
  if (patch.revision > 1) {
    requireText(supersedes, "Patch supersedes_artifact_id");
    if (!artifact.parent_artifact_ids.includes(supersedes as string)) {
      reject("Replacement Patch lineage omits the superseded Patch Artifact.");
    }
  }
  if (patch.produced_by === "human" && (patch.producer_run_id ?? null) !== null) {
    reject("Human Patch provenance cannot name a producer Run.");
  }
  if (patch.produced_by === "agent") {
    requireText(patch.producer_run_id, "Agent Patch producer_run_id");
  }
}

function freezePatchTarget(target: PatchTargetBinding): Readonly<PatchTargetBinding> {
  return Object.freeze({
    binding_schema_version: target.binding_schema_version,
    expected_ref: freezeRef(target.expected_ref),
    ref_name: target.ref_name,
    subject_kind: "patch" as const,
    target_artifact_id: target.target_artifact_id,
    target_artifact_kind: "ir_snapshot" as const,
    target_digest: target.target_digest,
    target_snapshot_id: target.target_snapshot_id,
  });
}

function samePatchTarget(left: PatchTargetBinding, right: PatchTargetBinding): boolean {
  return (
    left.binding_schema_version === right.binding_schema_version &&
    equalRef(left.expected_ref, right.expected_ref) &&
    left.ref_name === right.ref_name &&
    left.subject_kind === right.subject_kind &&
    left.target_artifact_id === right.target_artifact_id &&
    left.target_artifact_kind === right.target_artifact_kind &&
    left.target_digest === right.target_digest &&
    left.target_snapshot_id === right.target_snapshot_id
  );
}

function verifyAutoApplyProof(item: ApprovalRecord, target: PatchTargetBinding): void {
  if (item.status !== "auto_apply_eligible") return;
  const proof = item.auto_apply_proof;
  if (proof == null) reject("Auto-apply eligible Patch has no frozen proof binding.");
  if (
    proof.subject_digest !== item.subject_digest ||
    proof.target_digest !== target.target_digest ||
    !equalRef(proof.expected_ref, target.expected_ref)
  ) {
    reject("Auto-apply proof does not bind the exact Patch target.");
  }
  requireText(proof.proof_artifact_id, "auto-apply proof Artifact ID");
  requireText(proof.validation_evidence_artifact_id, "auto-apply validation evidence Artifact ID");
  requireDigest(proof.policy.policy_digest, "auto-apply policy digest");
  requireDigest(proof.policy.registry.registry_digest, "auto-apply policy registry digest");
}

export function verifyPatchWorkflowAuthority(input: PatchAuthorityProjection): Readonly<PatchTargetBinding> {
  verifyPatchArtifact(input.subject);
  const item = verifyApprovalBinding(input.subject, input.binding, input.approval, "patch");
  if (item.subject_revision !== input.subject.patch.revision) {
    reject("Approval subject revision does not match the immutable Patch revision.");
  }
  const target = item.target_binding;
  if (target == null || target.subject_kind !== "patch") {
    reject("Patch Approval has no Patch target binding.");
  }
  if (
    target.binding_schema_version !== "approval-target-binding@1" ||
    target.target_artifact_kind !== "ir_snapshot"
  ) {
    reject("Patch target binding schema or Artifact kind is invalid.");
  }
  requireText(target.ref_name, "Patch target ref_name");
  requireText(target.target_artifact_id, "Patch target Artifact ID");
  requireDigest(target.target_digest, "Patch target digest");
  if (target.target_snapshot_id !== input.subject.patch.target_snapshot_id) {
    reject("Patch target binding does not bind the exact preview snapshot.");
  }
  requireRef(target.expected_ref, "Patch target expected_ref");
  if (
    target.expected_ref != null &&
    !input.subject.artifact.parent_artifact_ids.includes(target.expected_ref.artifact_id)
  ) {
    reject("Patch Artifact lineage omits the exact expected base Artifact.");
  }
  verifyAutoApplyProof(item, target);
  return freezePatchTarget(target);
}

function hasInheritedReplacementAuthority(item: ApprovalRecord): boolean {
  return (
    (item.active_validation_run_id ?? null) !== null ||
    (item.applied_at ?? null) !== null ||
    (item.auto_apply_proof ?? null) !== null ||
    (item.decided_at ?? null) !== null ||
    item.decisions.length !== 0 ||
    (item.evidence_set_artifact_id ?? null) !== null ||
    (item.last_validation_failure_artifact_id ?? null) !== null ||
    item.regression_evidence_artifact_ids.length !== 0 ||
    (item.submitted_at ?? null) !== null
  );
}

export function verifyReplacementChain(
  previous: PatchAuthorityProjection,
  replacement: PatchAuthorityProjection,
): void {
  verifyPatchWorkflowAuthority(previous);
  verifyPatchWorkflowAuthority(replacement);
  const oldPatch = previous.subject.patch;
  const newPatch = replacement.subject.patch;
  const oldItem = previous.approval.approval;
  const newItem = replacement.approval.approval;

  if (oldPatch.revision + 1 !== newPatch.revision) {
    reject("Replacement Patch revision is not the exact next revision.");
  }
  if (newPatch.supersedes_artifact_id !== previous.subject.artifact.artifact_id) {
    reject("Replacement Patch does not supersede the exact prior Patch Artifact.");
  }
  if (oldItem.subject_series_id !== newItem.subject_series_id) {
    reject("Replacement Patch changed the immutable subject series.");
  }
  if (newItem.supersedes_approval_id !== oldItem.approval_id) {
    reject("Replacement Approval does not supersede the exact prior Approval.");
  }
  if (
    previous.subject.approval_status !== "superseded" ||
    previous.binding.approval_status !== "superseded" ||
    oldItem.status !== "superseded" ||
    previous.binding.is_current_head ||
    previous.binding.subject_head_revision !== newPatch.revision
  ) {
    reject("Prior Patch revision was not retained as the exact superseded head predecessor.");
  }
  if (
    !replacement.binding.is_current_head ||
    replacement.binding.subject_head_revision !== newPatch.revision
  ) {
    reject("Replacement Patch is not the exact current subject head.");
  }
}

export function verifyReplacementRevision(
  previous: PatchAuthorityProjection,
  replacement: PatchAuthorityProjection,
): void {
  verifyReplacementChain(previous, replacement);
  const newPatch = replacement.subject.patch;
  const newItem = replacement.approval.approval;
  if (
    replacement.subject.approval_status !== "draft" ||
    replacement.binding.approval_status !== "draft" ||
    newItem.status !== "draft" ||
    !replacement.binding.is_current_head ||
    replacement.binding.subject_head_revision !== newPatch.revision ||
    replacement.subject.workflow_revision !== 1
  ) {
    reject("Replacement Patch is not a fresh draft head.");
  }
  if (
    replacement.subject.validation_status !== "not_started" ||
    replacement.subject.regression_status !== "not_started" ||
    hasInheritedReplacementAuthority(newItem)
  ) {
    reject("Replacement Patch inherited validation, evidence, or decision authority.");
  }
}

function sameProfileBinding(
  left: RollbackTargetBinding["rollback_profile_binding"],
  right: RollbackTargetBinding["rollback_profile_binding"],
): boolean {
  return (
    left.catalog_digest === right.catalog_digest &&
    left.catalog_version === right.catalog_version &&
    left.expected_profile_kind === right.expected_profile_kind &&
    left.field_path === right.field_path &&
    left.profile.profile_id === right.profile.profile_id &&
    left.profile.version === right.profile.version &&
    left.profile_payload_hash === right.profile_payload_hash
  );
}

function verifyProfileBinding(binding: RollbackTargetBinding["rollback_profile_binding"]): void {
  if (binding.expected_profile_kind !== "rollback") {
    reject("Rollback profile binding has the wrong profile kind.");
  }
  if (binding.field_path !== "/params/rollback_profile") {
    reject("Rollback profile binding has the wrong field path.");
  }
  requireText(binding.profile.profile_id, "rollback profile ID");
  requirePositiveInteger(binding.profile.version, "rollback profile version");
  requirePositiveInteger(binding.catalog_version, "rollback profile catalog version");
  requireDigest(binding.catalog_digest, "rollback profile catalog digest");
  requireDigest(binding.profile_payload_hash, "rollback profile payload hash");
}

function freezeRollbackTarget(target: RollbackTargetBinding): Readonly<RollbackTargetBinding> {
  const profile = target.rollback_profile_binding;
  return Object.freeze({
    binding_schema_version: target.binding_schema_version,
    expected_ref: freezeRef(target.expected_ref) as Readonly<RefValue>,
    ref_name: target.ref_name,
    rollback_profile_binding: Object.freeze({
      catalog_digest: profile.catalog_digest,
      catalog_version: profile.catalog_version,
      expected_profile_kind: "rollback" as const,
      field_path: profile.field_path,
      profile: Object.freeze({
        profile_id: profile.profile.profile_id,
        version: profile.profile.version,
      }),
      profile_payload_hash: profile.profile_payload_hash,
    }),
    subject_kind: "rollback_request" as const,
    target_artifact_id: target.target_artifact_id,
    target_artifact_kind: target.target_artifact_kind,
    target_digest: target.target_digest,
    target_snapshot_id: target.target_snapshot_id ?? null,
  });
}

function exactRollbackParents(subject: RollbackRequestReadView): void {
  const expected = new Set([
    subject.request.expected_current_ref.artifact_id,
    subject.request.target_artifact_id,
  ]);
  const actual = new Set(subject.artifact.parent_artifact_ids);
  if (
    actual.size !== expected.size ||
    subject.artifact.parent_artifact_ids.length !== expected.size ||
    [...expected].some((artifactId) => !actual.has(artifactId))
  ) {
    reject("Rollback Artifact lineage must exactly bind current and target Artifacts.");
  }
}

function workflowTargetSnapshotId(
  kind: RollbackTargetBinding["target_artifact_kind"],
  artifact: ArtifactPayloadView["artifact"],
): string | null {
  if (kind === "ir_snapshot") return artifact.version_tuple.ir_snapshot_id ?? null;
  if (kind === "constraint_snapshot") return artifact.version_tuple.constraint_snapshot_id ?? null;
  return null;
}

function sameRollbackTarget(left: RollbackTargetBinding, right: RollbackTargetBinding): boolean {
  return (
    left.binding_schema_version === right.binding_schema_version &&
    equalRef(left.expected_ref, right.expected_ref) &&
    left.ref_name === right.ref_name &&
    sameProfileBinding(left.rollback_profile_binding, right.rollback_profile_binding) &&
    left.subject_kind === right.subject_kind &&
    left.target_artifact_id === right.target_artifact_id &&
    left.target_artifact_kind === right.target_artifact_kind &&
    left.target_digest === right.target_digest &&
    (left.target_snapshot_id ?? null) === (right.target_snapshot_id ?? null)
  );
}

export function verifyRollbackWorkflowAuthority(
  input: RollbackAuthorityProjection,
): Readonly<RollbackTargetBinding> {
  const { subject } = input;
  const { artifact, request } = subject;
  if (subject.view_schema_version !== "rollback-request-read-view@1") {
    reject("Rollback read view schema is unsupported.");
  }
  if (
    artifact.summary_schema_version !== "artifact-summary@1" ||
    artifact.lineage_schema_version !== "lineage@2" ||
    artifact.kind !== "rollback_request" ||
    artifact.payload_schema_id !== "rollback-request@1"
  ) {
    reject("Rollback Artifact envelope is not the required lineage@2 rollback-request@1 projection.");
  }
  requireText(artifact.artifact_id, "Rollback Artifact ID");
  requireDigest(artifact.payload_hash, "Rollback Artifact payload_hash");
  if (request.rollback_schema_version !== "rollback-request@1") {
    reject("Rollback request payload schema is unsupported.");
  }
  requireText(request.ref_name, "Rollback request ref_name");
  requireText(request.target_artifact_id, "Rollback target Artifact ID");
  requirePositiveInteger(request.target_history_revision, "Rollback target history revision");
  if (request.expected_current_ref == null) reject("Rollback expected_current_ref is missing.");
  requireRef(request.expected_current_ref, "Rollback expected_current_ref");
  requireText(request.reason, "Rollback reason");
  if (request.reason.trim().length === 0) reject("Rollback reason is blank.");
  if (request.reverses_approval_id != null) {
    requireText(request.reverses_approval_id, "reversed Approval ID");
    if (request.reverses_approval_id.trim().length === 0) {
      reject("Reversed Approval ID is blank.");
    }
  }
  exactRollbackParents(subject);
  verifyProfileBinding(request.rollback_profile_binding);

  const item = verifyApprovalBinding(subject, input.binding, input.approval, "rollback_request");
  if (item.subject_revision !== 1) reject("Rollback request subject revision must be one.");
  const target = item.target_binding;
  if (target == null || target.subject_kind !== "rollback_request") {
    reject("Rollback Approval has no rollback target binding.");
  }
  if (target.binding_schema_version !== "approval-target-binding@1") {
    reject("Rollback target binding schema is unsupported.");
  }
  requireText(target.target_artifact_id, "Rollback target binding Artifact ID");
  requireDigest(target.target_digest, "Rollback target digest");
  if (target.expected_ref == null) reject("Rollback target expected_ref is missing.");
  requireRef(target.expected_ref, "Rollback target expected_ref");
  verifyProfileBinding(target.rollback_profile_binding);
  if (
    target.ref_name !== request.ref_name ||
    !equalRef(target.expected_ref, request.expected_current_ref) ||
    target.target_artifact_id !== request.target_artifact_id ||
    !sameProfileBinding(target.rollback_profile_binding, request.rollback_profile_binding)
  ) {
    reject("Rollback Approval target does not exactly bind the immutable rollback request.");
  }
  const targetArtifact = input.targetArtifact;
  if (
    targetArtifact.view_schema_version !== "artifact-payload-view@1" ||
    targetArtifact.resource_revision !== 1 ||
    targetArtifact.artifact.summary_schema_version !== "artifact-summary@1" ||
    targetArtifact.artifact.lineage_schema_version !== "lineage@2" ||
    targetArtifact.artifact.artifact_id !== target.target_artifact_id ||
    targetArtifact.artifact.kind !== target.target_artifact_kind ||
    targetArtifact.artifact.payload_hash !== target.target_digest
  ) {
    reject("Rollback target Artifact read does not match the frozen target binding.");
  }
  const targetSnapshotId = workflowTargetSnapshotId(target.target_artifact_kind, targetArtifact.artifact);
  if ((target.target_snapshot_id ?? null) !== targetSnapshotId) {
    reject("Rollback target snapshot does not match the target Artifact VersionTuple.");
  }
  const subjectSnapshotId =
    target.target_artifact_kind === "ir_snapshot"
      ? (artifact.version_tuple.ir_snapshot_id ?? null)
      : target.target_artifact_kind === "constraint_snapshot"
        ? (artifact.version_tuple.constraint_snapshot_id ?? null)
        : null;
  if (subjectSnapshotId !== targetSnapshotId) {
    reject("Rollback request VersionTuple does not preserve the target snapshot identity.");
  }
  currentRefFromCompleteHistory(target.ref_name, input.history, input.historyNextCursor);
  const historyTarget = input.history.find(
    (entry) => entry.value.revision === request.target_history_revision,
  );
  if (
    historyTarget?.value.revision !== request.target_history_revision ||
    historyTarget.value.artifact_id !== request.target_artifact_id
  ) {
    reject("Rollback target is not the exact retained ref-history revision.");
  }
  return freezeRollbackTarget(target);
}

function freezeApplyRequest(request: WorkflowApplyRequest): Readonly<WorkflowApplyRequest> {
  return Object.freeze({
    ...request,
    expected_ref: freezeRef(request.expected_ref),
  });
}

export function buildPatchApplyRequest(input: PatchAuthorityProjection): Readonly<WorkflowApplyRequest> {
  const target = verifyPatchWorkflowAuthority(input);
  const item = input.approval.approval;
  if (!input.binding.is_current_head) reject("A historical Patch revision cannot be applied.");
  if (item.status !== "approved" && item.status !== "auto_apply_eligible") {
    reject("Patch is not approved or auto-apply eligible.");
  }
  return freezeApplyRequest({
    approval_id: item.approval_id,
    expected_ref: target.expected_ref ?? null,
    expected_workflow_revision: item.workflow_revision,
    ref_name: target.ref_name,
    request_schema_version: "workflow-apply-request@1",
    subject_digest: item.subject_digest,
    target_artifact_id: target.target_artifact_id,
    target_digest: target.target_digest,
  });
}

export function buildRollbackApplyRequest(
  input: RollbackAuthorityProjection,
): Readonly<WorkflowApplyRequest> {
  const target = verifyRollbackWorkflowAuthority(input);
  const item = input.approval.approval;
  if (!input.binding.is_current_head) reject("A historical rollback request cannot be applied.");
  if (item.status !== "approved") reject("Rollback request is not approved.");
  const currentRef = currentRefFromCompleteHistory(target.ref_name, input.history, input.historyNextCursor);
  if (!equalRef(currentRef, target.expected_ref)) {
    reject("Rollback expected ref is stale and cannot be applied.");
  }
  return freezeApplyRequest({
    approval_id: item.approval_id,
    expected_ref: target.expected_ref,
    expected_workflow_revision: item.workflow_revision,
    ref_name: target.ref_name,
    request_schema_version: "workflow-apply-request@1",
    subject_digest: item.subject_digest,
    target_artifact_id: target.target_artifact_id,
    target_digest: target.target_digest,
  });
}

export function currentRefFromCompleteHistory(
  refName: string,
  entries: readonly RefHistoryEntry[],
  nextCursor: string | null | undefined,
): Readonly<RefValue> {
  requireText(refName, "ref_name");
  if (nextCursor != null) reject("Ref history is incomplete; load every retained page first.");
  if (entries.length === 0) reject("Ref history is empty.");
  let previousRevision = 0;
  for (const entry of entries) {
    if (entry.entry_schema_version !== "ref-history-entry@1" || entry.ref_name !== refName) {
      reject("Ref history contains a foreign or unsupported entry.");
    }
    if (entry.value.revision <= previousRevision) {
      reject("Ref history revisions are not strictly increasing and unique.");
    }
    requireText(entry.value.artifact_id, `ref history revision ${entry.value.revision} Artifact ID`);
    previousRevision = entry.value.revision;
  }
  return freezeRef(entries[entries.length - 1].value)!;
}

function currentPatchRef(
  target: PatchTargetBinding,
  history: readonly RefHistoryEntry[],
): Readonly<RefValue> | null {
  if (history.length === 0) {
    if (target.expected_ref !== null) reject("Patch ref history is empty despite a non-null expected ref.");
    return null;
  }
  return currentRefFromCompleteHistory(target.ref_name, history, null);
}

export function verifyPatchApplyResult(input: {
  after: PatchAuthorityProjection;
  afterHistory: readonly RefHistoryEntry[];
  before: PatchAuthorityProjection;
  beforeHistory: readonly RefHistoryEntry[];
  result: WorkflowApplyResult;
}): void {
  const beforeTarget = verifyPatchWorkflowAuthority(input.before);
  const afterTarget = verifyPatchWorkflowAuthority(input.after);
  const beforeItem = input.before.approval.approval;
  const afterItem = input.after.approval.approval;
  const resultItem = input.result.approval.approval;
  if (beforeItem.status !== "approved" && beforeItem.status !== "auto_apply_eligible") {
    reject("Patch apply receipt has no approved predecessor.");
  }
  if (
    input.result.result_schema_version !== "workflow-apply-result@1" ||
    afterItem.status !== "applied" ||
    resultItem.status !== "applied"
  ) {
    reject("Patch apply receipt is not an applied workflow result.");
  }
  verifyPatchWorkflowAuthority({ ...input.after, approval: input.result.approval });
  if (!samePatchTarget(beforeTarget, afterTarget)) reject("Patch target binding changed during apply.");
  if (
    input.after.subject.artifact.artifact_id !== input.before.subject.artifact.artifact_id ||
    input.after.subject.artifact.payload_hash !== input.before.subject.artifact.payload_hash ||
    resultItem.approval_id !== afterItem.approval_id ||
    resultItem.workflow_revision !== afterItem.workflow_revision
  ) {
    reject("Patch apply receipt does not bind the exact retained workflow.");
  }
  const beforeRef = currentPatchRef(beforeTarget, input.beforeHistory);
  if (!equalRef(beforeRef, beforeTarget.expected_ref)) {
    reject("Patch apply predecessor ref does not match the frozen expected ref.");
  }
  const afterRef = currentRefFromCompleteHistory(afterTarget.ref_name, input.afterHistory, null);
  const expectedRevision = (beforeRef?.revision ?? 0) + 1;
  if (
    input.result.ref_name !== beforeTarget.ref_name ||
    input.result.ref_value.artifact_id !== beforeTarget.target_artifact_id ||
    input.result.ref_value.revision !== expectedRevision ||
    !equalRef(input.result.ref_value, afterRef) ||
    (input.result.ref_transition_id ?? null) !== null ||
    (input.result.reversed_approval_id ?? null) !== null
  ) {
    reject("Patch apply receipt does not describe the exact appended ref revision.");
  }
  if (input.afterHistory.length !== input.beforeHistory.length + 1) {
    reject("Patch apply did not append exactly one ref-history entry.");
  }
  for (const [index, entry] of input.beforeHistory.entries()) {
    const retained = input.afterHistory[index];
    if (
      retained?.entry_schema_version !== entry.entry_schema_version ||
      retained.ref_name !== entry.ref_name ||
      !equalRef(retained.value, entry.value)
    ) {
      reject("Patch apply rewrote retained ref history.");
    }
  }
}

export function verifyRollbackApplyResult(input: {
  after: RollbackAuthorityProjection;
  before: RollbackAuthorityProjection;
  result: WorkflowApplyResult;
}): void {
  const beforeTarget = verifyRollbackWorkflowAuthority(input.before);
  const afterTarget = verifyRollbackWorkflowAuthority(input.after);
  const beforeItem = input.before.approval.approval;
  const afterItem = input.after.approval.approval;
  const resultItem = input.result.approval.approval;
  if (beforeItem.status !== "approved") reject("Rollback apply receipt has no approved predecessor.");
  if (
    input.result.result_schema_version !== "workflow-apply-result@1" ||
    afterItem.status !== "applied" ||
    resultItem.status !== "applied"
  ) {
    reject("Rollback apply receipt is not an applied workflow result.");
  }
  verifyRollbackWorkflowAuthority({ ...input.after, approval: input.result.approval });
  if (!sameRollbackTarget(beforeTarget, afterTarget)) {
    reject("Rollback target binding changed during apply.");
  }
  if (
    input.after.subject.artifact.artifact_id !== input.before.subject.artifact.artifact_id ||
    input.after.subject.artifact.payload_hash !== input.before.subject.artifact.payload_hash ||
    resultItem.approval_id !== afterItem.approval_id ||
    resultItem.workflow_revision !== afterItem.workflow_revision
  ) {
    reject("Rollback apply receipt does not bind the exact retained workflow.");
  }
  const beforeRef = currentRefFromCompleteHistory(
    beforeTarget.ref_name,
    input.before.history,
    input.before.historyNextCursor,
  );
  const afterRef = currentRefFromCompleteHistory(
    afterTarget.ref_name,
    input.after.history,
    input.after.historyNextCursor,
  );
  if (
    input.result.ref_name !== beforeTarget.ref_name ||
    input.result.ref_value.artifact_id !== beforeTarget.target_artifact_id ||
    input.result.ref_value.revision !== beforeRef.revision + 1 ||
    !equalRef(input.result.ref_value, afterRef)
  ) {
    reject("Rollback apply receipt does not describe the exact appended ref revision.");
  }
  requireText(input.result.ref_transition_id, "rollback RefTransition ID");
  if (
    (input.result.reversed_approval_id ?? null) !==
    (input.before.subject.request.reverses_approval_id ?? null)
  ) {
    reject("Rollback apply receipt reversed a different Approval.");
  }
  if (input.after.history.length !== input.before.history.length + 1) {
    reject("Rollback apply did not append exactly one ref-history entry.");
  }
  for (const [index, entry] of input.before.history.entries()) {
    const retained = input.after.history[index];
    if (
      retained?.entry_schema_version !== entry.entry_schema_version ||
      retained.ref_name !== entry.ref_name ||
      !equalRef(retained.value, entry.value)
    ) {
      reject("Rollback apply rewrote retained ref history.");
    }
  }
}
