import { describe, expect, it } from "vitest";

import type {
  ApprovalView,
  PatchArtifactReadView,
  RefHistoryEntry,
  RollbackRequestReadView,
  SubjectApprovalBindingView,
} from "./api";
import {
  buildPatchApplyRequest,
  buildRollbackApplyRequest,
  currentRefFromCompleteHistory,
  PatchAuthorityError,
  type PatchAuthorityProjection,
  type RollbackAuthorityProjection,
  verifyPatchApplyResult,
  verifyRollbackApplyResult,
  verifyPatchWorkflowAuthority,
  verifyReplacementRevision,
  verifyRollbackWorkflowAuthority,
} from "./authority";

const PATCH_DIGEST = "a".repeat(64);
const PREVIEW_DIGEST = "b".repeat(64);
const ROLLBACK_DIGEST = "c".repeat(64);
const TARGET_DIGEST = "d".repeat(64);

function patchSubject(overrides: Partial<PatchArtifactReadView> = {}): PatchArtifactReadView {
  return {
    approval_status: "approved",
    artifact: {
      artifact_id: "artifact:patch:1",
      created_at: "2026-07-20T00:00:00Z",
      domain_scope: { domain_ids: ["domain:economy"] },
      kind: "patch",
      lineage_schema_version: "lineage@2",
      parent_artifact_ids: ["artifact:snapshot:base"],
      payload_hash: PATCH_DIGEST,
      payload_schema_id: "patch@2",
      summary_schema_version: "artifact-summary@1",
      version_tuple: {
        ir_snapshot_id: "snapshot:base",
        tool_version: "human-patch@1",
      },
    },
    patch: {
      base_snapshot_id: "snapshot:base",
      expected_to_fix: ["finding:economy:1"],
      ops: [],
      patch_schema_version: "patch@2",
      preconditions: [],
      produced_by: "human",
      producer_run_id: null,
      rationale: "Bring the source below its deterministic sink.",
      revision: 1,
      side_effect_risk: "low",
      supersedes_artifact_id: null,
      target_snapshot_id: "snapshot:preview",
    },
    regression_status: "succeeded",
    validation_status: "succeeded",
    view_schema_version: "patch-artifact-read-view@1",
    workflow_revision: 4,
    ...overrides,
  };
}

function binding(overrides: Partial<SubjectApprovalBindingView> = {}): SubjectApprovalBindingView {
  return {
    approval_id: "approval:patch:1",
    approval_status: "approved",
    is_current_head: true,
    subject_artifact_id: "artifact:patch:1",
    subject_digest: PATCH_DIGEST,
    subject_head_revision: 1,
    subject_kind: "patch",
    subject_revision: 1,
    subject_series_id: "series:patch:1",
    workflow_revision: 4,
    ...overrides,
  };
}

function approval(
  itemOverrides: Partial<ApprovalView["approval"]> = {},
  viewOverrides: Partial<ApprovalView> = {},
): ApprovalView {
  return {
    approval: {
      active_validation_run_id: null,
      applied_at: null,
      approval_id: "approval:patch:1",
      approval_policy: { policy_digest: "e".repeat(64), policy_version: "policy@1" },
      approval_schema_version: "approval@1",
      auto_apply_proof: null,
      created_at: "2026-07-20T00:00:00Z",
      decided_at: "2026-07-20T00:02:00Z",
      decisions: [],
      domain_registry_ref: {
        registry_digest: "f".repeat(64),
        registry_version: "domains@1",
      },
      domain_scope: { domain_ids: ["domain:economy"] },
      evidence_set_artifact_id: "artifact:evidence:set:1",
      last_validation_failure_artifact_id: null,
      proposer: { principal_id: "principal:designer", principal_kind: "human" },
      regression_evidence_artifact_ids: ["artifact:evidence:regression:1"],
      requirements: [],
      role_policy_digest: "1".repeat(64),
      role_policy_version: "roles@1",
      route_policy: {
        domain_registry_ref: {
          registry_digest: "f".repeat(64),
          registry_version: "domains@1",
        },
        route_digest: "2".repeat(64),
        route_version: "route@1",
      },
      status: "approved",
      subject_artifact_id: "artifact:patch:1",
      subject_digest: PATCH_DIGEST,
      subject_kind: "patch",
      subject_revision: 1,
      subject_series_id: "series:patch:1",
      submitted_at: "2026-07-20T00:01:00Z",
      supersedes_approval_id: null,
      target_binding: {
        binding_schema_version: "approval-target-binding@1",
        expected_ref: { artifact_id: "artifact:snapshot:base", revision: 12 },
        ref_name: "refs/design/live",
        subject_kind: "patch",
        target_artifact_id: "artifact:snapshot:preview",
        target_artifact_kind: "ir_snapshot",
        target_digest: PREVIEW_DIGEST,
        target_snapshot_id: "snapshot:preview",
      },
      workflow_revision: 4,
      ...itemOverrides,
    },
    current_actor_allowed_requirement_ids: [],
    requirement_progress: [],
    view_schema_version: "approval-view@1",
    ...viewOverrides,
  };
}

function patchAuthority(): PatchAuthorityProjection {
  return { subject: patchSubject(), binding: binding(), approval: approval() };
}

function rollbackAuthority(): RollbackAuthorityProjection {
  const profile = {
    catalog_digest: "3".repeat(64),
    catalog_version: 7,
    expected_profile_kind: "rollback" as const,
    field_path: "/params/rollback_profile",
    profile: { profile_id: "builtin.rollback", version: 3 },
    profile_payload_hash: "4".repeat(64),
  };
  const subject = {
    approval_status: "approved",
    artifact: {
      artifact_id: "artifact:rollback:1",
      created_at: "2026-07-20T01:00:00Z",
      domain_scope: { domain_ids: ["domain:economy"] },
      kind: "rollback_request",
      lineage_schema_version: "lineage@2",
      parent_artifact_ids: ["artifact:snapshot:current", "artifact:snapshot:target"],
      payload_hash: ROLLBACK_DIGEST,
      payload_schema_id: "rollback-request@1",
      summary_schema_version: "artifact-summary@1",
      version_tuple: { ir_snapshot_id: "snapshot:target", tool_version: "rollback@1" },
    },
    request: {
      expected_current_ref: { artifact_id: "artifact:snapshot:current", revision: 19 },
      reason: "Restore the approved economy baseline.",
      ref_name: "refs/design/live",
      reverses_approval_id: "approval:patch:bad",
      rollback_profile_binding: {
        ...profile,
        profile: { ...profile.profile },
      },
      rollback_schema_version: "rollback-request@1",
      target_artifact_id: "artifact:snapshot:target",
      target_history_revision: 12,
    },
    view_schema_version: "rollback-request-read-view@1",
    workflow_revision: 6,
  } satisfies RollbackRequestReadView;
  const approvalView = approval({
    approval_id: "approval:rollback:1",
    status: "approved",
    subject_artifact_id: "artifact:rollback:1",
    subject_digest: ROLLBACK_DIGEST,
    subject_kind: "rollback_request",
    subject_series_id: "series:rollback:1",
    target_binding: {
      binding_schema_version: "approval-target-binding@1",
      expected_ref: { artifact_id: "artifact:snapshot:current", revision: 19 },
      ref_name: "refs/design/live",
      rollback_profile_binding: profile,
      subject_kind: "rollback_request",
      target_artifact_id: "artifact:snapshot:target",
      target_artifact_kind: "ir_snapshot",
      target_digest: TARGET_DIGEST,
      target_snapshot_id: "snapshot:target",
    },
    workflow_revision: 6,
  });
  return {
    subject,
    binding: binding({
      approval_id: "approval:rollback:1",
      approval_status: "approved",
      subject_artifact_id: "artifact:rollback:1",
      subject_digest: ROLLBACK_DIGEST,
      subject_head_revision: 1,
      subject_kind: "rollback_request",
      subject_series_id: "series:rollback:1",
      workflow_revision: 6,
    }),
    approval: approvalView,
    history: Array.from({ length: 19 }, (_value, index) => {
      const revision = index + 1;
      return {
        entry_schema_version: "ref-history-entry@1" as const,
        ref_name: "refs/design/live",
        value: {
          artifact_id:
            revision === 12
              ? "artifact:snapshot:target"
              : revision === 19
                ? "artifact:snapshot:current"
                : `artifact:snapshot:${revision}`,
          revision,
        },
      };
    }),
    historyNextCursor: null,
    targetArtifact: {
      artifact: {
        artifact_id: "artifact:snapshot:target",
        created_at: "2026-07-20T00:30:00Z",
        domain_scope: { domain_ids: ["domain:economy"] },
        kind: "ir_snapshot",
        lineage_schema_version: "lineage@2",
        parent_artifact_ids: [],
        payload_hash: TARGET_DIGEST,
        payload_schema_id: "ir-core@1",
        summary_schema_version: "artifact-summary@1",
        version_tuple: { ir_snapshot_id: "snapshot:target", tool_version: "spec@1" },
      },
      payload: {},
      resource_revision: 1,
      view_schema_version: "artifact-payload-view@1",
    },
  };
}

describe("Patch workflow authority", () => {
  it("closes the Patch Artifact, payload, binding, Approval, and frozen apply target", () => {
    const input = patchAuthority();
    const target = verifyPatchWorkflowAuthority(input);

    expect(target).toEqual(input.approval.approval.target_binding);
    expect(Object.isFrozen(target)).toBe(true);
    expect(Object.isFrozen(target.expected_ref)).toBe(true);

    const request = buildPatchApplyRequest(input);
    expect(request).toEqual({
      approval_id: "approval:patch:1",
      expected_ref: { artifact_id: "artifact:snapshot:base", revision: 12 },
      expected_workflow_revision: 4,
      ref_name: "refs/design/live",
      request_schema_version: "workflow-apply-request@1",
      subject_digest: PATCH_DIGEST,
      target_artifact_id: "artifact:snapshot:preview",
      target_digest: PREVIEW_DIGEST,
    });
    expect(Object.isFrozen(request)).toBe(true);
    expect(Object.isFrozen(request.expected_ref)).toBe(true);
  });

  it.each([
    ["Artifact kind", (value: PatchAuthorityProjection) => (value.subject.artifact.kind = "review_report")],
    [
      "lineage schema",
      (value: PatchAuthorityProjection) => (value.subject.artifact.lineage_schema_version = "lineage@1"),
    ],
    [
      "payload schema",
      (value: PatchAuthorityProjection) => (value.subject.artifact.payload_schema_id = "patch@1"),
    ],
    [
      "payload digest",
      (value: PatchAuthorityProjection) => (value.subject.artifact.payload_hash = "not-a-digest"),
    ],
    [
      "base tuple",
      (value: PatchAuthorityProjection) =>
        (value.subject.artifact.version_tuple.ir_snapshot_id = "snapshot:other"),
    ],
    [
      "producer binding",
      (value: PatchAuthorityProjection) => (value.subject.patch.producer_run_id = "run:unexpected"),
    ],
    [
      "revision lineage",
      (value: PatchAuthorityProjection) => (value.subject.patch.supersedes_artifact_id = "artifact:old"),
    ],
    ["binding digest", (value: PatchAuthorityProjection) => (value.binding.subject_digest = "5".repeat(64))],
    ["head status", (value: PatchAuthorityProjection) => (value.binding.is_current_head = false)],
    [
      "Approval subject",
      (value: PatchAuthorityProjection) => (value.approval.approval.subject_artifact_id = "artifact:other"),
    ],
    ["Approval status", (value: PatchAuthorityProjection) => (value.approval.approval.status = "rejected")],
    [
      "target snapshot",
      (value: PatchAuthorityProjection) => {
        const target = value.approval.approval.target_binding;
        if (target?.subject_kind === "patch") target.target_snapshot_id = "snapshot:other";
      },
    ],
    [
      "base Artifact lineage",
      (value: PatchAuthorityProjection) => (value.subject.artifact.parent_artifact_ids = []),
    ],
  ])("fails closed on a mismatched %s", (_label, mutate) => {
    const value = structuredClone(patchAuthority());
    mutate(value);
    expect(() => verifyPatchWorkflowAuthority(value)).toThrow(PatchAuthorityError);
  });

  it("accepts exact agent provenance and rejects a missing producer Run", () => {
    const exact = patchAuthority();
    exact.subject.patch.produced_by = "agent";
    exact.subject.patch.producer_run_id = "run:repair:1";
    expect(() => verifyPatchWorkflowAuthority(exact)).not.toThrow();

    exact.subject.patch.producer_run_id = null;
    expect(() => verifyPatchWorkflowAuthority(exact)).toThrow(PatchAuthorityError);
  });

  it("requires replacement revisions to supersede the exact prior head with no inherited decision authority", () => {
    const previous = patchAuthority();
    previous.subject.approval_status = "superseded";
    previous.binding.approval_status = "superseded";
    previous.binding.is_current_head = false;
    previous.binding.subject_head_revision = 2;
    previous.approval.approval.status = "superseded";

    const replacement = structuredClone(patchAuthority());
    replacement.subject.artifact.artifact_id = "artifact:patch:2";
    replacement.subject.artifact.payload_hash = "6".repeat(64);
    replacement.subject.artifact.parent_artifact_ids = ["artifact:patch:1", "artifact:snapshot:current"];
    replacement.subject.patch.revision = 2;
    replacement.subject.patch.supersedes_artifact_id = "artifact:patch:1";
    replacement.subject.patch.base_snapshot_id = "snapshot:current";
    replacement.subject.artifact.version_tuple.ir_snapshot_id = "snapshot:current";
    replacement.subject.approval_status = "draft";
    replacement.subject.validation_status = "not_started";
    replacement.subject.regression_status = "not_started";
    replacement.subject.workflow_revision = 1;
    replacement.binding = binding({
      approval_id: "approval:patch:2",
      approval_status: "draft",
      subject_artifact_id: "artifact:patch:2",
      subject_digest: "6".repeat(64),
      subject_head_revision: 2,
      subject_revision: 2,
      workflow_revision: 1,
    });
    replacement.approval = approval({
      active_validation_run_id: null,
      applied_at: null,
      approval_id: "approval:patch:2",
      auto_apply_proof: null,
      decided_at: null,
      decisions: [],
      evidence_set_artifact_id: null,
      last_validation_failure_artifact_id: null,
      regression_evidence_artifact_ids: [],
      status: "draft",
      subject_artifact_id: "artifact:patch:2",
      subject_digest: "6".repeat(64),
      subject_revision: 2,
      submitted_at: null,
      supersedes_approval_id: "approval:patch:1",
      workflow_revision: 1,
    });
    const replacementTarget = replacement.approval.approval.target_binding;
    if (replacementTarget?.subject_kind === "patch") {
      replacementTarget.expected_ref = {
        artifact_id: "artifact:snapshot:current",
        revision: 13,
      };
    }

    expect(() => verifyReplacementRevision(previous, replacement)).not.toThrow();

    replacement.approval.approval.decisions = [
      {
        actor: { principal_id: "principal:old", principal_kind: "human" },
        comment: null,
        decision: "approve",
        decision_id: "decision:old",
        expected_workflow_revision: 1,
        occurred_at: "2026-07-20T00:02:00Z",
        reason_code: "approved",
        requirement_ids: [],
      },
    ];
    expect(() => verifyReplacementRevision(previous, replacement)).toThrow(PatchAuthorityError);
  });

  it("does not build apply commands for non-approved Patch authority", () => {
    const input = patchAuthority();
    input.subject.approval_status = "pending_approval";
    input.binding.approval_status = "pending_approval";
    input.approval.approval.status = "pending_approval";

    expect(() => buildPatchApplyRequest(input)).toThrow(PatchAuthorityError);
  });

  it("verifies the Patch apply receipt against the appended ref revision", () => {
    const before = patchAuthority();
    const after = structuredClone(before);
    after.subject.approval_status = "applied";
    after.subject.workflow_revision = 5;
    after.binding.approval_status = "applied";
    after.binding.workflow_revision = 5;
    after.approval.approval.status = "applied";
    after.approval.approval.workflow_revision = 5;
    const beforeHistory: RefHistoryEntry[] = Array.from({ length: 12 }, (_value, index) => ({
      entry_schema_version: "ref-history-entry@1",
      ref_name: "refs/design/live",
      value: {
        artifact_id: index === 11 ? "artifact:snapshot:base" : `artifact:snapshot:${index + 1}`,
        revision: index + 1,
      },
    }));
    const afterHistory: RefHistoryEntry[] = [
      ...beforeHistory,
      {
        entry_schema_version: "ref-history-entry@1",
        ref_name: "refs/design/live",
        value: { artifact_id: "artifact:snapshot:preview", revision: 13 },
      },
    ];
    const result = {
      approval: structuredClone(after.approval),
      ref_name: "refs/design/live",
      ref_transition_id: null,
      ref_value: { artifact_id: "artifact:snapshot:preview", revision: 13 },
      result_schema_version: "workflow-apply-result@1" as const,
      reversed_approval_id: null,
    };

    expect(() =>
      verifyPatchApplyResult({ after, afterHistory, before, beforeHistory, result }),
    ).not.toThrow();

    result.ref_value.revision = 12;
    expect(() => verifyPatchApplyResult({ after, afterHistory, before, beforeHistory, result })).toThrow(
      PatchAuthorityError,
    );
  });
});

describe("Rollback workflow authority", () => {
  it("closes the immutable request and profile binding before building apply", () => {
    const input = rollbackAuthority();
    const target = verifyRollbackWorkflowAuthority(input);

    expect(target).toEqual(input.approval.approval.target_binding);
    expect(Object.isFrozen(target.rollback_profile_binding)).toBe(true);
    expect(buildRollbackApplyRequest(input)).toEqual({
      approval_id: "approval:rollback:1",
      expected_ref: { artifact_id: "artifact:snapshot:current", revision: 19 },
      expected_workflow_revision: 6,
      ref_name: "refs/design/live",
      request_schema_version: "workflow-apply-request@1",
      subject_digest: ROLLBACK_DIGEST,
      target_artifact_id: "artifact:snapshot:target",
      target_digest: TARGET_DIGEST,
    });
  });

  it("rejects any rollback target or resolved profile drift", () => {
    const targetDrift = rollbackAuthority();
    targetDrift.subject.request.target_artifact_id = "artifact:snapshot:other";
    expect(() => verifyRollbackWorkflowAuthority(targetDrift)).toThrow(PatchAuthorityError);

    const profileDrift = rollbackAuthority();
    const target = profileDrift.approval.approval.target_binding;
    if (target?.subject_kind === "rollback_request") {
      target.rollback_profile_binding.catalog_version += 1;
    }
    expect(() => verifyRollbackWorkflowAuthority(profileDrift)).toThrow(PatchAuthorityError);

    const fieldPathDrift = rollbackAuthority();
    fieldPathDrift.subject.request.rollback_profile_binding.field_path = "/rollback_profile";
    const fieldPathTarget = fieldPathDrift.approval.approval.target_binding;
    if (fieldPathTarget?.subject_kind === "rollback_request") {
      fieldPathTarget.rollback_profile_binding.field_path = "/rollback_profile";
    }
    expect(() => verifyRollbackWorkflowAuthority(fieldPathDrift)).toThrow(PatchAuthorityError);
  });

  it("requires exact request parents, history membership, and target Artifact identity", () => {
    const extraParent = rollbackAuthority();
    extraParent.subject.artifact.parent_artifact_ids.push("artifact:unexpected");
    expect(() => verifyRollbackWorkflowAuthority(extraParent)).toThrow(PatchAuthorityError);

    const historyDrift = rollbackAuthority();
    historyDrift.history = historyDrift.history.map((entry, index) =>
      index === 11 ? { ...entry, value: { artifact_id: "artifact:snapshot:other", revision: 12 } } : entry,
    );
    expect(() => verifyRollbackWorkflowAuthority(historyDrift)).toThrow(PatchAuthorityError);

    const targetDrift = rollbackAuthority();
    targetDrift.targetArtifact.artifact.payload_hash = "9".repeat(64);
    expect(() => verifyRollbackWorkflowAuthority(targetDrift)).toThrow(PatchAuthorityError);

    const blankReason = rollbackAuthority();
    blankReason.subject.request.reason = "   ";
    expect(() => verifyRollbackWorkflowAuthority(blankReason)).toThrow(PatchAuthorityError);
  });

  it("uses the target kind's semantic VersionTuple field", () => {
    const input = rollbackAuthority();
    const target = input.approval.approval.target_binding;
    if (target?.subject_kind !== "rollback_request") throw new Error("fixture target kind");
    target.target_artifact_kind = "constraint_snapshot";
    target.target_snapshot_id = "constraint:target";
    input.targetArtifact.artifact.kind = "constraint_snapshot";
    input.targetArtifact.artifact.payload_schema_id = "constraint-snapshot@1";
    input.targetArtifact.artifact.version_tuple = { constraint_snapshot_id: "constraint:target" };
    input.subject.artifact.version_tuple = { constraint_snapshot_id: "constraint:target" };

    expect(() => verifyRollbackWorkflowAuthority(input)).not.toThrow();
  });

  it("verifies the applied receipt and append-only ref history", () => {
    const before = rollbackAuthority();
    const after = structuredClone(before);
    after.subject.approval_status = "applied";
    after.subject.workflow_revision = 7;
    after.binding.approval_status = "applied";
    after.binding.workflow_revision = 7;
    after.approval.approval.status = "applied";
    after.approval.approval.workflow_revision = 7;
    after.history = [
      ...after.history,
      {
        entry_schema_version: "ref-history-entry@1",
        ref_name: "refs/design/live",
        value: { artifact_id: "artifact:snapshot:target", revision: 20 },
      },
    ];
    const result = {
      approval: structuredClone(after.approval),
      ref_name: "refs/design/live",
      ref_transition_id: "ref-transition:sha256:exact",
      ref_value: { artifact_id: "artifact:snapshot:target", revision: 20 },
      result_schema_version: "workflow-apply-result@1" as const,
      reversed_approval_id: "approval:patch:bad",
    };

    expect(() => verifyRollbackApplyResult({ after, before, result })).not.toThrow();

    result.ref_transition_id = "";
    expect(() => verifyRollbackApplyResult({ after, before, result })).toThrow(PatchAuthorityError);
  });

  it("never treats auto-apply eligibility as rollback approval", () => {
    const input = rollbackAuthority();
    input.subject.approval_status = "auto_apply_eligible";
    input.binding.approval_status = "auto_apply_eligible";
    input.approval.approval.status = "auto_apply_eligible";

    expect(() => buildRollbackApplyRequest(input)).toThrow(PatchAuthorityError);
  });
});

describe("complete ref history authority", () => {
  const history = [
    {
      entry_schema_version: "ref-history-entry@1",
      ref_name: "refs/design/live",
      value: { artifact_id: "artifact:snapshot:1", revision: 1 },
    },
    {
      entry_schema_version: "ref-history-entry@1",
      ref_name: "refs/design/live",
      value: { artifact_id: "artifact:snapshot:2", revision: 2 },
    },
  ] satisfies RefHistoryEntry[];

  it("derives current RefValue from the last item of a complete RBAC-visible history", () => {
    const current = currentRefFromCompleteHistory("refs/design/live", history, null);
    expect(current).toEqual({ artifact_id: "artifact:snapshot:2", revision: 2 });
    expect(Object.isFrozen(current)).toBe(true);
  });

  it("allows RBAC gaps but rejects partial pages, non-increasing revisions, foreign refs, and empty history", () => {
    expect(() => currentRefFromCompleteHistory("refs/design/live", history, "more")).toThrow(
      PatchAuthorityError,
    );
    expect(
      currentRefFromCompleteHistory(
        "refs/design/live",
        [history[0], { ...history[1], value: { ...history[1].value, revision: 3 } }],
        null,
      ),
    ).toEqual({ artifact_id: "artifact:snapshot:2", revision: 3 });
    expect(() =>
      currentRefFromCompleteHistory(
        "refs/design/live",
        [history[1], { ...history[0], value: { ...history[0].value, revision: 1 } }],
        null,
      ),
    ).toThrow(PatchAuthorityError);
    expect(() => currentRefFromCompleteHistory("refs/other", history, null)).toThrow(PatchAuthorityError);
    expect(() => currentRefFromCompleteHistory("refs/design/live", [], null)).toThrow(PatchAuthorityError);
  });
});
