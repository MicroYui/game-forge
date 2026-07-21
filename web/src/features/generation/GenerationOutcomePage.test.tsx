import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import type { RunEvent } from "../../api/generated/sse-run-event-v1";
import { createQueryClient } from "../../api/query-client";
import { GenerationPage } from "./GenerationPage";
import type {
  ApprovalView,
  ArtifactPayloadView,
  GenerationApi,
  GenerationEventStreamCallbacks,
  PatchArtifactReadView,
  RunView,
  SubjectApprovalBindingView,
} from "./api";

type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];

const RUN_ID = "run:generation:outcome";
const RESULT_ID = "artifact:run-result:generation";
const FAILURE_ID = "artifact:run-failure:generation";
const BASE_ID = "artifact:spec:base";
const BASE_SNAPSHOT_ID = "snapshot:base";
const CONSTRAINT_ID = "artifact:constraint:active";
const CASSETTE_ID = "artifact:cassette:replay";
const RENDERED_ID = "artifact:source-rendered:goal";
const PATCH_ID = "artifact:patch:candidate";
const PREVIEW_ID = "artifact:preview:candidate";
const PREVIEW_SNAPSHOT_ID = "snapshot:candidate";
const CONFIG_ID = "artifact:config:candidate";
const GATE_EVIDENCE_ID = "artifact:checker:generation-gate";
const APPROVAL_ID = "approval:patch:candidate";
const REPAIRED_PATCH_ID = "artifact:patch:repair-r2";
const REPAIRED_PREVIEW_ID = "artifact:preview:repair-r2";
const REPAIRED_PREVIEW_SNAPSHOT_ID = "snapshot:repair-r2";
const REPAIRED_CONFIG_ID = "artifact:config:repair-r2";
const REPAIR_EVIDENCE_ID = "artifact:regression-evidence:repair-r2";
const REPAIRED_APPROVAL_ID = "approval:patch:repair-r2";
const OLD_EVIDENCE_SET_ID = "artifact:validation-evidence:failed-r1";
const OLD_REGRESSION_EVIDENCE_ID = "artifact:regression-evidence:failed-r1";
const PATCH_DIGEST = "2".repeat(64);
const PREVIEW_DIGEST = "3".repeat(64);
const REPAIRED_PATCH_DIGEST = "4".repeat(64);
const REPAIRED_PREVIEW_DIGEST = "5".repeat(64);
const hash = "a".repeat(64);
const domainScope: components["schemas"]["DomainScope"] = { domain_ids: ["domain:economy"] };

function summary(
  artifactId: string,
  kind: ArtifactSummary["kind"],
  payloadSchemaId: string,
  parentArtifactIds: string[] = [],
): ArtifactSummary {
  return {
    artifact_id: artifactId,
    created_at: "2026-07-20T00:00:00Z",
    domain_scope: domainScope,
    kind,
    lineage_schema_version: "lineage@2",
    parent_artifact_ids: [...parentArtifactIds].sort(),
    payload_hash: hash,
    payload_schema_id: payloadSchemaId,
    summary_schema_version: "artifact-summary@1",
    version_tuple: {
      constraint_snapshot_id: "constraint-snapshot:active",
      ir_snapshot_id: "snapshot:candidate",
      tool_version: "generation@1",
    },
  };
}

const patchSummary: ArtifactSummary = {
  ...summary(PATCH_ID, "patch", "patch@2"),
  payload_hash: PATCH_DIGEST,
  version_tuple: {
    doc_version: "base-doc@1",
    constraint_snapshot_id: "constraint-snapshot:active",
    ir_snapshot_id: BASE_SNAPSHOT_ID,
    tool_version: "generation@1",
  },
};
const previewSummary: ArtifactSummary = {
  ...summary(PREVIEW_ID, "ir_snapshot", "ir-core@1"),
  payload_hash: PREVIEW_DIGEST,
  version_tuple: {
    doc_version: "base-doc@1",
    constraint_snapshot_id: "constraint-snapshot:active",
    ir_snapshot_id: PREVIEW_SNAPSHOT_ID,
    tool_version: "generation@1",
  },
};
const configSummary: ArtifactSummary = {
  ...summary(CONFIG_ID, "config_export", "config-export-package@1"),
  version_tuple: {
    doc_version: "base-doc@1",
    constraint_snapshot_id: "constraint-snapshot:active",
    env_contract_version: "aureus@1",
    ir_snapshot_id: PREVIEW_SNAPSHOT_ID,
    tool_version: "config-export@1",
  },
};
const gateEvidenceSummary = summary(GATE_EVIDENCE_ID, "checker_run", "checker-report@1");
const baseSpec: components["schemas"]["SpecViewV1"] = {
  artifact: {
    ...summary(BASE_ID, "ir_snapshot", "ir-core@1"),
    version_tuple: {
      doc_version: "base-doc@1",
      ir_snapshot_id: BASE_SNAPSHOT_ID,
      tool_version: "authoring@1",
    },
  },
  ref_name: "refs/specs/economy",
  ref_value: { artifact_id: BASE_ID, revision: 8 },
  schema_registry_version: "registry@3",
  snapshot_id: BASE_SNAPSHOT_ID,
  view_schema_version: "spec-view@1",
};
const constraintSummary: ArtifactSummary = {
  ...summary(CONSTRAINT_ID, "constraint_snapshot", "constraint-snapshot@1"),
  version_tuple: {
    constraint_snapshot_id: "constraint-snapshot:active",
    tool_version: "constraints@1",
  },
};
const constraintView: components["schemas"]["ConstraintSnapshotViewV1"] = {
  artifact: constraintSummary,
  constraints: [],
  dsl_grammar_version: "dsl@1",
  view_schema_version: "constraint-snapshot-view@1",
};

function parent(
  artifactId: string,
  role: "input" | "intermediate" | "output" | "evidence",
  publication: "existing" | "run_published",
) {
  return { artifact_id: artifactId, publication, role };
}

const inputParents = [
  parent(BASE_ID, "input", "existing"),
  parent(CONSTRAINT_ID, "input", "existing"),
  parent(CASSETTE_ID, "input", "existing"),
];

function artifactView(artifact: ArtifactSummary, payload: Record<string, unknown> = {}): ArtifactPayloadView {
  return {
    artifact,
    payload,
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

function configPayload(sourcePreviewArtifactId: string) {
  return {
    constraint_snapshot_artifact_id: CONSTRAINT_ID,
    package_schema_version: "config-export-package@1",
    source_preview_artifact_id: sourcePreviewArtifactId,
  };
}

function successfulManifest(): ArtifactPayloadView {
  const publishedParents = [
    parent(PATCH_ID, "output", "run_published"),
    parent(PREVIEW_ID, "output", "run_published"),
    parent(CONFIG_ID, "output", "run_published"),
    parent(GATE_EVIDENCE_ID, "evidence", "run_published"),
    parent(RENDERED_ID, "intermediate", "run_published"),
  ];
  const parents = [...inputParents, ...publishedParents];
  const producedArtifactIds = publishedParents.map((item) => item.artifact_id).sort();
  return {
    artifact: summary(
      RESULT_ID,
      "run_result",
      "run-result@1",
      parents.map((item) => item.artifact_id),
    ),
    payload: {
      attempt_no: 1,
      outcome_code: "generation_gate_passed",
      primary_artifact_id: PATCH_ID,
      produced_artifact_ids: producedArtifactIds,
      result_schema_version: "run-result@1",
      run_id: RUN_ID,
      run_kind: { kind: "generation.propose", version: 1 },
      version_projection: {
        attempt_no: 1,
        manifest_scope: "run",
        parents,
        projection_schema_version: "run-manifest-version-projection@1",
        run_kind: { kind: "generation.propose", version: 1 },
      },
    },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

function rejectedManifest(): ArtifactPayloadView {
  const publishedParents = [
    parent(PATCH_ID, "evidence", "run_published"),
    parent(PREVIEW_ID, "evidence", "run_published"),
    parent(GATE_EVIDENCE_ID, "evidence", "run_published"),
    parent(RENDERED_ID, "intermediate", "run_published"),
  ];
  const parents = [...inputParents, ...publishedParents];
  const evidenceArtifactIds = publishedParents.map((item) => item.artifact_id).sort();
  return {
    artifact: summary(
      FAILURE_ID,
      "run_failure",
      "run-failure@1",
      parents.map((item) => item.artifact_id),
    ),
    payload: {
      attempt_no: 1,
      cause_code: "generation_gate_rejected",
      evidence_artifact_ids: evidenceArtifactIds,
      failure_schema_version: "run-failure@1",
      redacted_message: "The deterministic generation gate rejected this proposal.",
      run_id: RUN_ID,
      run_kind: { kind: "generation.propose", version: 1 },
      version_projection: {
        attempt_no: 1,
        manifest_scope: "run",
        parents,
        projection_schema_version: "run-manifest-version-projection@1",
        run_kind: { kind: "generation.propose", version: 1 },
      },
    },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

const repairedPatchSummary: ArtifactSummary = {
  ...summary(REPAIRED_PATCH_ID, "patch", "patch@2", [BASE_ID, CONSTRAINT_ID, PATCH_ID, OLD_EVIDENCE_SET_ID]),
  payload_hash: REPAIRED_PATCH_DIGEST,
  version_tuple: {
    agent_graph_version: "patch-repair@1",
    cassette_id: CASSETTE_ID,
    constraint_snapshot_id: "constraint-snapshot:active",
    doc_version: "base-doc@1",
    ir_snapshot_id: BASE_SNAPSHOT_ID,
    model_snapshot: "openai/gpt-5.6-sol/m4@1",
    prompt_version: "patch-repair@1",
    seed: 19,
    tool_version: "patch-repair@1",
  },
};

const repairedPreviewSummary: ArtifactSummary = {
  ...summary(REPAIRED_PREVIEW_ID, "ir_snapshot", "ir-core@1", [BASE_ID, CONSTRAINT_ID, REPAIRED_PATCH_ID]),
  payload_hash: REPAIRED_PREVIEW_DIGEST,
  version_tuple: {
    constraint_snapshot_id: "constraint-snapshot:active",
    doc_version: "base-doc@1",
    ir_snapshot_id: REPAIRED_PREVIEW_SNAPSHOT_ID,
    tool_version: "patch-repair@1",
  },
};

const repairedConfigSummary: ArtifactSummary = {
  ...summary(REPAIRED_CONFIG_ID, "config_export", "config-export-package@1", [
    CONSTRAINT_ID,
    REPAIRED_PREVIEW_ID,
  ]),
  version_tuple: {
    constraint_snapshot_id: "constraint-snapshot:active",
    doc_version: "base-doc@1",
    env_contract_version: "aureus@1",
    ir_snapshot_id: REPAIRED_PREVIEW_SNAPSHOT_ID,
    tool_version: "config-export@1",
  },
};

const repairEvidenceSummary: ArtifactSummary = {
  ...summary(REPAIR_EVIDENCE_ID, "regression_evidence", "regression-evidence@1", [REPAIRED_PREVIEW_ID]),
  version_tuple: {
    constraint_snapshot_id: "constraint-snapshot:active",
    doc_version: "base-doc@1",
    env_contract_version: "aureus@1",
    ir_snapshot_id: REPAIRED_PREVIEW_SNAPSHOT_ID,
    seed: 19,
    tool_version: "patch-repair@1",
  },
};

function repairedManifest(): ArtifactPayloadView {
  const repairInputParents = [
    parent(BASE_ID, "input", "existing"),
    parent(CONSTRAINT_ID, "input", "existing"),
    parent(CASSETTE_ID, "input", "existing"),
    parent(PATCH_ID, "input", "existing"),
    parent(PREVIEW_ID, "input", "existing"),
    parent(OLD_EVIDENCE_SET_ID, "input", "existing"),
  ];
  const publishedParents = [
    parent(REPAIRED_PATCH_ID, "output", "run_published"),
    parent(REPAIRED_PREVIEW_ID, "output", "run_published"),
    parent(REPAIRED_CONFIG_ID, "output", "run_published"),
    parent(REPAIR_EVIDENCE_ID, "evidence", "run_published"),
    parent(RENDERED_ID, "intermediate", "run_published"),
  ];
  const parents = [...repairInputParents, ...publishedParents];
  const producedArtifactIds = publishedParents.map((item) => item.artifact_id).sort();
  const manifestSummary: ArtifactSummary = {
    ...summary(
      RESULT_ID,
      "run_result",
      "run-result@1",
      parents.map((item) => item.artifact_id),
    ),
    version_tuple: {
      agent_graph_version: "patch-repair@1",
      cassette_id: CASSETTE_ID,
      constraint_snapshot_id: "constraint-snapshot:active",
      doc_version: "base-doc@1",
      ir_snapshot_id: BASE_SNAPSHOT_ID,
      model_snapshot: "openai/gpt-5.6-sol/m4@1",
      prompt_version: "patch-repair@1",
      seed: 19,
      tool_version: "patch-repair@1",
    },
  };
  return {
    artifact: manifestSummary,
    payload: {
      attempt_no: 1,
      outcome_code: "repair_verified",
      primary_artifact_id: REPAIRED_PATCH_ID,
      produced_artifact_ids: producedArtifactIds,
      result_schema_version: "run-result@1",
      run_id: RUN_ID,
      run_kind: { kind: "patch.repair", version: 1 },
      version_projection: {
        attempt_no: 1,
        manifest_scope: "run",
        parents,
        projection_schema_version: "run-manifest-version-projection@1",
        run_kind: { kind: "patch.repair", version: 1 },
      },
    },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

function unverifiedRepairManifest(): ArtifactPayloadView {
  const repairInputParents = [
    parent(BASE_ID, "input", "existing"),
    parent(CONSTRAINT_ID, "input", "existing"),
    parent(CASSETTE_ID, "input", "existing"),
    parent(PATCH_ID, "input", "existing"),
    parent(PREVIEW_ID, "input", "existing"),
    parent(OLD_EVIDENCE_SET_ID, "input", "existing"),
  ];
  const publishedParents = [
    parent(REPAIR_EVIDENCE_ID, "evidence", "run_published"),
    parent(RENDERED_ID, "intermediate", "run_published"),
  ];
  const parents = [...repairInputParents, ...publishedParents];
  const manifestSummary: ArtifactSummary = {
    ...summary(
      FAILURE_ID,
      "run_failure",
      "run-failure@1",
      parents.map((item) => item.artifact_id),
    ),
    version_tuple: {
      agent_graph_version: "patch-repair@1",
      cassette_id: CASSETTE_ID,
      constraint_snapshot_id: "constraint-snapshot:active",
      doc_version: "base-doc@1",
      ir_snapshot_id: BASE_SNAPSHOT_ID,
      model_snapshot: "openai/gpt-5.6-sol/m4@1",
      prompt_version: "patch-repair@1",
      seed: 19,
      tool_version: "patch-repair@1",
    },
  };
  return {
    artifact: manifestSummary,
    payload: {
      attempt_no: 1,
      cause_code: "repair_unverified",
      evidence_artifact_ids: publishedParents.map((item) => item.artifact_id).sort(),
      failure_schema_version: "run-failure@1",
      redacted_message: "Verifier-guided repair exhausted without a verified successor.",
      run_id: RUN_ID,
      run_kind: { kind: "patch.repair", version: 1 },
      version_projection: {
        attempt_no: 1,
        manifest_scope: "run",
        parents,
        projection_schema_version: "run-manifest-version-projection@1",
        run_kind: { kind: "patch.repair", version: 1 },
      },
    },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

function run(
  status: RunView["status"],
  options: { failureArtifactId?: string; resultArtifactId?: string } = {},
): RunView {
  return {
    attempt_no: status === "queued" ? null : 1,
    events_url: `/api/v1/runs/${RUN_ID}/events`,
    failure_artifact_id: options.failureArtifactId ?? null,
    result_artifact_id: options.resultArtifactId ?? null,
    revision: status === "queued" ? 1 : 2,
    run_id: RUN_ID,
    status,
    status_url: `/api/v1/runs/${RUN_ID}`,
    terminal_cassette_artifact_id: status === "queued" ? null : CASSETTE_ID,
    view_schema_version: "run-view@1",
  };
}

const patchView: PatchArtifactReadView = {
  approval_status: "draft",
  artifact: patchSummary,
  patch: {
    base_snapshot_id: BASE_SNAPSHOT_ID,
    ops: [],
    patch_schema_version: "patch@2",
    produced_by: "agent",
    producer_run_id: RUN_ID,
    rationale: "Generation candidate passed the deterministic gate.",
    revision: 1,
    side_effect_risk: "low",
    target_snapshot_id: PREVIEW_SNAPSHOT_ID,
  },
  regression_status: "not_started",
  validation_status: "not_started",
  view_schema_version: "patch-artifact-read-view@1",
  workflow_revision: 1,
};

const approvalBinding: SubjectApprovalBindingView = {
  approval_id: APPROVAL_ID,
  approval_status: "draft",
  is_current_head: true,
  subject_artifact_id: PATCH_ID,
  subject_digest: PATCH_DIGEST,
  subject_head_revision: 1,
  subject_kind: "patch",
  subject_revision: 1,
  subject_series_id: "patch-series:generation",
  workflow_revision: 1,
};

const approvalRequirement: components["schemas"]["ApprovalRequirement"] = {
  assignee_principal_ids: [],
  distinct_from_requirement_ids: [],
  domain_scope: domainScope,
  min_approvals: 1,
  required_permission: {
    action: "approve",
    domain_scope: domainScope,
    resource_kind: "patch",
  },
  requirement_id: "requirement:content-review",
  route_role: "content_designer",
};

const approvalView: ApprovalView = {
  approval: {
    approval_id: APPROVAL_ID,
    approval_policy: { policy_digest: "c".repeat(64), policy_version: "approval@1" },
    approval_schema_version: "approval@1",
    created_at: "2026-07-20T00:00:01Z",
    decisions: [],
    domain_registry_ref: { registry_digest: "d".repeat(64), registry_version: "domains@1" },
    domain_scope: domainScope,
    evidence_set_artifact_id: null,
    last_validation_failure_artifact_id: null,
    proposer: { principal_id: "human:maker", principal_kind: "human" },
    regression_evidence_artifact_ids: [],
    requirements: [approvalRequirement],
    role_policy_digest: "e".repeat(64),
    role_policy_version: "roles@1",
    route_policy: {
      domain_registry_ref: { registry_digest: "d".repeat(64), registry_version: "domains@1" },
      route_digest: "f".repeat(64),
      route_version: "routes@1",
    },
    status: "draft",
    subject_artifact_id: PATCH_ID,
    subject_digest: approvalBinding.subject_digest,
    subject_kind: "patch",
    subject_revision: 1,
    subject_series_id: approvalBinding.subject_series_id,
    target_binding: {
      binding_schema_version: "approval-target-binding@1",
      expected_ref: { artifact_id: BASE_ID, revision: 8 },
      ref_name: "refs/specs/economy",
      subject_kind: "patch",
      target_artifact_id: PREVIEW_ID,
      target_artifact_kind: "ir_snapshot",
      target_digest: PREVIEW_DIGEST,
      target_snapshot_id: PREVIEW_SNAPSHOT_ID,
    },
    workflow_revision: 1,
  },
  current_actor_allowed_requirement_ids: [],
  requirement_progress: [
    {
      decision_eligibility: [
        { decision: "approve", eligible: false, reason_codes: ["workflow_not_pending"] },
        { decision: "reject", eligible: false, reason_codes: ["workflow_not_pending"] },
        {
          decision: "request_changes",
          eligible: false,
          reason_codes: ["workflow_not_pending"],
        },
      ],
      domain_scope: domainScope,
      eligible_for_current_actor: false,
      min_approvals: 1,
      requirement_id: approvalRequirement.requirement_id,
      route_role: approvalRequirement.route_role,
      satisfied: false,
      unmet_distinct_from_requirement_ids: [],
      valid_approval_count: 0,
    },
  ],
  view_schema_version: "approval-view@1",
};

const repairedPatchView: PatchArtifactReadView = {
  approval_status: "draft",
  artifact: repairedPatchSummary,
  patch: {
    base_snapshot_id: BASE_SNAPSHOT_ID,
    expected_to_fix: ["finding:playtest-incomplete"],
    ops: [],
    patch_schema_version: "patch@2",
    produced_by: "agent",
    producer_run_id: RUN_ID,
    rationale: "Verifier-closed repair from the exact current-ref base.",
    revision: 2,
    side_effect_risk: "low",
    supersedes_artifact_id: PATCH_ID,
    target_snapshot_id: REPAIRED_PREVIEW_SNAPSHOT_ID,
  },
  regression_status: "not_started",
  validation_status: "not_started",
  view_schema_version: "patch-artifact-read-view@1",
  workflow_revision: 1,
};

const supersededPatchView: PatchArtifactReadView = {
  ...patchView,
  approval_status: "superseded",
  regression_status: "failed",
  validation_status: "failed",
  workflow_revision: 6,
};

const repairedApprovalBinding: SubjectApprovalBindingView = {
  approval_id: REPAIRED_APPROVAL_ID,
  approval_status: "draft",
  is_current_head: true,
  subject_artifact_id: REPAIRED_PATCH_ID,
  subject_digest: REPAIRED_PATCH_DIGEST,
  subject_head_revision: 2,
  subject_kind: "patch",
  subject_revision: 2,
  subject_series_id: approvalBinding.subject_series_id,
  workflow_revision: 1,
};

const supersededApprovalBinding: SubjectApprovalBindingView = {
  approval_id: APPROVAL_ID,
  approval_status: "superseded",
  is_current_head: false,
  subject_artifact_id: PATCH_ID,
  subject_digest: PATCH_DIGEST,
  subject_head_revision: 2,
  subject_kind: "patch",
  subject_revision: 1,
  subject_series_id: approvalBinding.subject_series_id,
  workflow_revision: 6,
};

const oldDecisions: components["schemas"]["ApprovalDecision"][] = [
  {
    actor: { principal_id: "human:reviewer-a", principal_kind: "human" },
    decision: "request_changes",
    decision_id: "decision:old:1",
    expected_workflow_revision: 3,
    occurred_at: "2026-07-20T00:00:03Z",
    reason_code: "playtest_incomplete",
    requirement_ids: [approvalRequirement.requirement_id],
  },
  {
    actor: { principal_id: "human:reviewer-b", principal_kind: "human" },
    decision: "reject",
    decision_id: "decision:old:2",
    expected_workflow_revision: 5,
    occurred_at: "2026-07-20T00:00:04Z",
    reason_code: "regression_failed",
    requirement_ids: [approvalRequirement.requirement_id],
  },
];

const supersededApprovalView: ApprovalView = {
  ...approvalView,
  approval: {
    ...approvalView.approval,
    decisions: oldDecisions,
    evidence_set_artifact_id: OLD_EVIDENCE_SET_ID,
    regression_evidence_artifact_ids: [OLD_REGRESSION_EVIDENCE_ID],
    status: "superseded",
    workflow_revision: 6,
  },
};

const repairedApprovalView: ApprovalView = {
  ...approvalView,
  approval: {
    ...approvalView.approval,
    approval_id: REPAIRED_APPROVAL_ID,
    decisions: [],
    evidence_set_artifact_id: null,
    regression_evidence_artifact_ids: [],
    status: "draft",
    subject_artifact_id: REPAIRED_PATCH_ID,
    subject_digest: REPAIRED_PATCH_DIGEST,
    subject_revision: 2,
    supersedes_approval_id: APPROVAL_ID,
    target_binding: {
      binding_schema_version: "approval-target-binding@1",
      expected_ref: { artifact_id: BASE_ID, revision: 8 },
      ref_name: "refs/specs/economy",
      subject_kind: "patch",
      target_artifact_id: REPAIRED_PREVIEW_ID,
      target_artifact_kind: "ir_snapshot",
      target_digest: REPAIRED_PREVIEW_DIGEST,
      target_snapshot_id: REPAIRED_PREVIEW_SNAPSHOT_ID,
    },
    workflow_revision: 1,
  },
};

function terminalSuccessEvent(): RunEvent {
  return {
    attempt_no: 1,
    data: {
      attempt_no: 1,
      data_schema_version: "run-succeeded@1",
      result_artifact_id: RESULT_ID,
    },
    data_schema_version: "run-succeeded@1",
    event_schema_version: "run-event@1",
    event_type: "run.succeeded",
    occurred_at: "2026-07-20T00:00:02Z",
    run_id: RUN_ID,
    seq: 7,
    trace_id: null,
  };
}

function preliminaryGateEvent(): RunEvent {
  return {
    attempt_no: 1,
    data: {
      attempt_no: 1,
      completed_units: 1,
      data_schema_version: "attempt-progress@1",
      detail_artifact_id: null,
      phase_code: "generation.preliminary_gate",
      total_units: 1,
    },
    data_schema_version: "attempt-progress@1",
    event_schema_version: "run-event@1",
    event_type: "attempt.progress",
    occurred_at: "2026-07-20T00:00:01Z",
    run_id: RUN_ID,
    seq: 6,
    trace_id: null,
  };
}

type OutcomeApiHarness = {
  api: GenerationApi;
  callbacks(): (GenerationEventStreamCallbacks & { runId: string }) | null;
};

type WorkflowFixtures = {
  approvals?: ReadonlyMap<string, ApprovalView>;
  bindings?: ReadonlyMap<string, SubjectApprovalBindingView>;
  patches?: ReadonlyMap<string, PatchArtifactReadView>;
};

function outcomeApi(
  getRun: GenerationApi["getRun"],
  artifacts: ReadonlyMap<string, ArtifactPayloadView>,
  workflow: WorkflowFixtures = {},
): OutcomeApiHarness {
  let streamCallbacks: (GenerationEventStreamCallbacks & { runId: string }) | null = null;
  const getArtifact = vi.fn<GenerationApi["getArtifact"]>(async (artifactId) => {
    const value = artifacts.get(artifactId);
    if (!value) throw new Error(`Unexpected Artifact read: ${artifactId}`);
    return value;
  });
  const api: GenerationApi = {
    createEventStream: vi.fn((callbacks) => {
      streamCallbacks = callbacks;
      return {
        close: vi.fn(),
        restart: vi.fn(async () => undefined),
        start: vi.fn(async () => undefined),
      };
    }),
    getApproval: vi.fn(async (approvalId) => {
      const value = workflow.approvals?.get(approvalId) ?? (approvalId === APPROVAL_ID ? approvalView : null);
      if (!value) throw new Error(`Unexpected Approval read: ${approvalId}`);
      return { etag: `"approval:${value.approval.workflow_revision}"`, value };
    }),
    getApprovalBinding: vi.fn(async (artifactId) => {
      const value = workflow.bindings?.get(artifactId) ?? (artifactId === PATCH_ID ? approvalBinding : null);
      if (!value) throw new Error(`Unexpected approval binding read: ${artifactId}`);
      return value;
    }),
    getArtifact,
    getConstraint: vi.fn(async () => constraintView),
    getExecutionProfile: vi.fn(),
    getPatch: vi.fn(async (artifactId) => {
      const value = workflow.patches?.get(artifactId) ?? (artifactId === PATCH_ID ? patchView : null);
      if (!value) throw new Error(`Unexpected Patch read: ${artifactId}`);
      return { etag: `"patch:${value.workflow_revision}"`, value };
    }),
    getRun: vi.fn(getRun),
    getSnapshotDiff: vi.fn<GenerationApi["getSnapshotDiff"]>(async (_baseArtifactId, targetArtifactId) => ({
      diff: {
        base_snapshot_id: BASE_SNAPSHOT_ID,
        diff_schema_version: "snapshot-diff@1",
        entry_count: 0,
        target_snapshot_id:
          targetArtifactId === REPAIRED_PREVIEW_SNAPSHOT_ID
            ? REPAIRED_PREVIEW_SNAPSHOT_ID
            : PREVIEW_SNAPSHOT_ID,
      },
      page: {
        expires_at: "2026-07-20T01:00:00Z",
        items: [],
        next_cursor: null,
        page_schema_version: "page@1",
        read_snapshot_id: "read:diff:generation",
      },
      page_schema_version: "snapshot-diff-http-page@1",
    })),
    getSpec: vi.fn(async () => baseSpec),
    listConstraints: vi.fn(),
    listExecutionProfiles: vi.fn(),
    listSpecs: vi.fn(),
    proposeGeneration: vi.fn(),
    resolveExecutionOption: vi.fn(),
  };
  return { api, callbacks: () => streamCallbacks };
}

function successfulArtifacts(): ReadonlyMap<string, ArtifactPayloadView> {
  return new Map([
    [RESULT_ID, successfulManifest()],
    [PATCH_ID, artifactView(patchSummary)],
    [PREVIEW_ID, artifactView(previewSummary)],
    [CONFIG_ID, artifactView(configSummary, configPayload(PREVIEW_ID))],
    [GATE_EVIDENCE_ID, artifactView(gateEvidenceSummary)],
  ]);
}

function rejectedArtifacts(): ReadonlyMap<string, ArtifactPayloadView> {
  return new Map([
    [FAILURE_ID, rejectedManifest()],
    [PATCH_ID, artifactView(patchSummary)],
    [PREVIEW_ID, artifactView(previewSummary)],
    [GATE_EVIDENCE_ID, artifactView(gateEvidenceSummary)],
  ]);
}

function repairedArtifacts(): ReadonlyMap<string, ArtifactPayloadView> {
  return new Map([
    [RESULT_ID, repairedManifest()],
    [REPAIRED_PATCH_ID, artifactView(repairedPatchSummary)],
    [REPAIRED_PREVIEW_ID, artifactView(repairedPreviewSummary)],
    [REPAIRED_CONFIG_ID, artifactView(repairedConfigSummary, configPayload(REPAIRED_PREVIEW_ID))],
    [REPAIR_EVIDENCE_ID, artifactView(repairEvidenceSummary)],
  ]);
}

function renderOutcome(api: GenerationApi) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[`/generation?run=${encodeURIComponent(RUN_ID)}`]}>
        <GenerationPage api={api} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function expectUnsafeRepair(workflow: WorkflowFixtures) {
  const harness = outcomeApi(
    async () => run("succeeded", { resultArtifactId: RESULT_ID }),
    repairedArtifacts(),
    workflow,
  );
  renderOutcome(harness.api);

  expect(await screen.findByRole("heading", { name: "候选 authority 不安全" })).toBeVisible();
  expect(screen.queryByRole("navigation", { name: "候选后续动作" })).not.toBeInTheDocument();
  expect(screen.queryByText("generation_gate_passed")).not.toBeInTheDocument();
}

describe("Generation outcome", () => {
  it("reconstructs the exact successful candidate from a Run deep-link", async () => {
    const harness = outcomeApi(
      async () => run("succeeded", { resultArtifactId: RESULT_ID }),
      successfulArtifacts(),
    );

    renderOutcome(harness.api);

    expect(await screen.findByText(PATCH_ID)).toBeVisible();
    expect(screen.getByText(PREVIEW_ID)).toBeVisible();
    expect(screen.getByText(CONFIG_ID)).toBeVisible();
    expect(screen.getByText(GATE_EVIDENCE_ID)).toBeVisible();
    expect(screen.getByText(RENDERED_ID)).toBeVisible();
    expect(screen.queryByRole("link", { name: RENDERED_ID })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review 候选" })).toHaveAttribute(
      "href",
      `/reviews?sourceRun=${encodeURIComponent(RUN_ID)}&snapshot=${encodeURIComponent(PREVIEW_ID)}&constraint=${encodeURIComponent(CONSTRAINT_ID)}`,
    );
    expect(screen.getByRole("link", { name: `派生 TaskSuite · ${CONFIG_ID}` })).toHaveAttribute(
      "href",
      `/playtest?sourceRun=${encodeURIComponent(RUN_ID)}&preview=${encodeURIComponent(PREVIEW_ID)}&config=${encodeURIComponent(CONFIG_ID)}&constraint=${encodeURIComponent(CONSTRAINT_ID)}&action=derive`,
    );
    expect(screen.getByRole("link", { name: `进入 Playtest · ${CONFIG_ID}` })).toHaveAttribute(
      "href",
      `/playtest?sourceRun=${encodeURIComponent(RUN_ID)}&preview=${encodeURIComponent(PREVIEW_ID)}&config=${encodeURIComponent(CONFIG_ID)}&constraint=${encodeURIComponent(CONSTRAINT_ID)}`,
    );
    expect(harness.api.getRun).toHaveBeenCalledWith(RUN_ID);
    expect(harness.api.getArtifact).toHaveBeenCalledWith(RESULT_ID);
    for (const artifactId of [PATCH_ID, PREVIEW_ID, CONFIG_ID, GATE_EVIDENCE_ID]) {
      expect(harness.api.getArtifact).toHaveBeenCalledWith(artifactId);
    }
    expect(harness.api.getArtifact).not.toHaveBeenCalledWith(RENDERED_ID);
    expect(harness.api.getArtifact).not.toHaveBeenCalledWith(CASSETTE_ID);
    expect(harness.api.getSpec).toHaveBeenCalledWith(BASE_ID);
    expect(harness.api.getConstraint).toHaveBeenCalledWith(CONSTRAINT_ID);
  });

  it("reloads the authoritative Run and outcome after a terminal SSE event", async () => {
    const getRun = vi
      .fn<GenerationApi["getRun"]>()
      .mockResolvedValueOnce(run("queued"))
      .mockResolvedValue(run("succeeded", { resultArtifactId: RESULT_ID }));
    const harness = outcomeApi(getRun, successfulArtifacts());

    renderOutcome(harness.api);
    await waitFor(() => expect(harness.callbacks()).not.toBeNull());
    expect(harness.api.getArtifact).not.toHaveBeenCalled();

    act(() => harness.callbacks()?.onEvent(terminalSuccessEvent(), "7"));

    await waitFor(() => expect(getRun).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(PATCH_ID)).toBeVisible();
    expect(harness.api.getArtifact).toHaveBeenCalledWith(RESULT_ID);
  });

  it("renders a gate rejection from generic evidence Artifacts without calling Patch workflow APIs", async () => {
    const harness = outcomeApi(
      async () => run("failed", { failureArtifactId: FAILURE_ID }),
      rejectedArtifacts(),
    );

    renderOutcome(harness.api);

    expect(await screen.findByText("generation_gate_rejected")).toBeVisible();
    act(() => harness.callbacks()?.onEvent(preliminaryGateEvent(), "6"));
    const preliminaryGate = await screen.findByRole("heading", { name: "Preliminary gate" });
    expect(preliminaryGate.closest("section")).toHaveAttribute("data-state", "error");
    expect(screen.getByText(PATCH_ID)).toBeVisible();
    expect(screen.getByText(PREVIEW_ID)).toBeVisible();
    expect(screen.queryByText(CONFIG_ID)).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "候选后续动作" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Review 候选" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "打开 exact Patch workflow" })).not.toBeInTheDocument();
    expect(screen.queryByText("Config export")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "派生 TaskSuite" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "进入 Playtest" })).not.toBeInTheDocument();
    expect(screen.queryByText(/submit|apply|validate/i)).not.toBeInTheDocument();
    expect(harness.api.getArtifact).toHaveBeenCalledWith(PATCH_ID);
    expect(harness.api.getPatch).not.toHaveBeenCalled();
    expect(harness.api.getApprovalBinding).not.toHaveBeenCalled();
    expect(harness.api.getApproval).not.toHaveBeenCalled();
    expect(harness.api.getArtifact).not.toHaveBeenCalledWith(RENDERED_ID);
    expect(harness.api.getArtifact).not.toHaveBeenCalledWith(CASSETTE_ID);
  });

  it("shows a verified repair successor without inheriting the superseded workflow state", async () => {
    const harness = outcomeApi(
      async () => run("succeeded", { resultArtifactId: RESULT_ID }),
      repairedArtifacts(),
      {
        approvals: new Map([
          [APPROVAL_ID, supersededApprovalView],
          [REPAIRED_APPROVAL_ID, repairedApprovalView],
        ]),
        bindings: new Map([
          [PATCH_ID, supersededApprovalBinding],
          [REPAIRED_PATCH_ID, repairedApprovalBinding],
        ]),
        patches: new Map([
          [PATCH_ID, supersededPatchView],
          [REPAIRED_PATCH_ID, repairedPatchView],
        ]),
      },
    );

    renderOutcome(harness.api);

    const successor = await screen.findByRole("region", { name: "新 Patch workflow 状态" });
    expect(successor).toHaveTextContent(REPAIRED_PATCH_ID);
    expect(successor).toHaveTextContent("r2");
    expect(successor).toHaveTextContent(PATCH_ID);
    expect(successor).toHaveTextContent(/evidence\s*0/i);
    expect(successor).toHaveTextContent(/decisions\s*0/i);

    const superseded = screen.getByRole("region", { name: "旧 Patch workflow 状态" });
    expect(superseded).toHaveTextContent(PATCH_ID);
    expect(superseded).toHaveTextContent("r1");
    expect(superseded).toHaveTextContent("superseded");
    expect(superseded).toHaveTextContent("non-current");
    expect(superseded).toHaveTextContent(/evidence\s*2/i);
    expect(superseded).toHaveTextContent(/decisions\s*2/i);
    expect(screen.getByText("旧审批状态不会继承")).toBeVisible();
    expect(screen.getByRole("link", { name: "打开 exact Patch workflow" })).toHaveAttribute(
      "href",
      `/patches/${encodeURIComponent(REPAIRED_PATCH_ID)}`,
    );
    expect(screen.getByRole("link", { name: "Review 候选" })).toHaveAttribute(
      "href",
      `/reviews?sourceRun=${encodeURIComponent(RUN_ID)}&snapshot=${encodeURIComponent(REPAIRED_PREVIEW_ID)}&constraint=${encodeURIComponent(CONSTRAINT_ID)}`,
    );

    expect(harness.api.getPatch).toHaveBeenCalledWith(REPAIRED_PATCH_ID);
    expect(harness.api.getApprovalBinding).toHaveBeenCalledWith(REPAIRED_PATCH_ID);
    expect(harness.api.getApproval).toHaveBeenCalledWith(REPAIRED_APPROVAL_ID);
    expect(harness.api.getPatch).toHaveBeenCalledWith(PATCH_ID);
    expect(harness.api.getApprovalBinding).toHaveBeenCalledWith(PATCH_ID);
    expect(harness.api.getApproval).toHaveBeenCalledWith(APPROVAL_ID);
  });

  it("keeps repair_unverified evidence-only and never infers a successor workflow", async () => {
    const artifacts = new Map<string, ArtifactPayloadView>([
      [FAILURE_ID, unverifiedRepairManifest()],
      [REPAIR_EVIDENCE_ID, artifactView(repairEvidenceSummary)],
    ]);
    const harness = outcomeApi(async () => run("failed", { failureArtifactId: FAILURE_ID }), artifacts);

    renderOutcome(harness.api);

    expect(await screen.findByText("repair_unverified")).toBeVisible();
    expect(screen.getByText(REPAIR_EVIDENCE_ID)).toBeVisible();
    expect(screen.queryByRole("link", { name: "打开 exact Patch workflow" })).not.toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "候选后续动作" })).not.toBeInTheDocument();
    expect(screen.queryByText(/submit|apply|validate/i)).not.toBeInTheDocument();
    expect(screen.queryByText(REPAIRED_PATCH_ID)).not.toBeInTheDocument();
    expect(harness.api.getArtifact).toHaveBeenCalledWith(FAILURE_ID);
    expect(harness.api.getArtifact).toHaveBeenCalledWith(REPAIR_EVIDENCE_ID);
    expect(harness.api.getArtifact).not.toHaveBeenCalledWith(RENDERED_ID);
    expect(harness.api.getPatch).not.toHaveBeenCalled();
    expect(harness.api.getApprovalBinding).not.toHaveBeenCalled();
    expect(harness.api.getApproval).not.toHaveBeenCalled();
  });

  it("fails closed when a superseded predecessor is still marked current", async () => {
    const invalidOldBinding = structuredClone(supersededApprovalBinding);
    invalidOldBinding.is_current_head = true;
    invalidOldBinding.subject_head_revision = invalidOldBinding.subject_revision;
    await expectUnsafeRepair({
      approvals: new Map([
        [APPROVAL_ID, supersededApprovalView],
        [REPAIRED_APPROVAL_ID, repairedApprovalView],
      ]),
      bindings: new Map([
        [PATCH_ID, invalidOldBinding],
        [REPAIRED_PATCH_ID, repairedApprovalBinding],
      ]),
      patches: new Map([
        [PATCH_ID, supersededPatchView],
        [REPAIRED_PATCH_ID, repairedPatchView],
      ]),
    });
  });

  it("fails closed when the predecessor Approval is not superseded", async () => {
    const invalidOldApproval = structuredClone(supersededApprovalView);
    invalidOldApproval.approval.status = "approved";
    await expectUnsafeRepair({
      approvals: new Map([
        [APPROVAL_ID, invalidOldApproval],
        [REPAIRED_APPROVAL_ID, repairedApprovalView],
      ]),
      bindings: new Map([
        [PATCH_ID, supersededApprovalBinding],
        [REPAIRED_PATCH_ID, repairedApprovalBinding],
      ]),
      patches: new Map([
        [PATCH_ID, supersededPatchView],
        [REPAIRED_PATCH_ID, repairedPatchView],
      ]),
    });
  });

  it("fails closed when a repair skips a workflow revision", async () => {
    const skippedPatch = structuredClone(repairedPatchView);
    skippedPatch.patch.revision = 3;
    const skippedBinding = structuredClone(repairedApprovalBinding);
    skippedBinding.subject_revision = 3;
    skippedBinding.subject_head_revision = 3;
    const skippedApproval = structuredClone(repairedApprovalView);
    skippedApproval.approval.subject_revision = 3;
    await expectUnsafeRepair({
      approvals: new Map([
        [APPROVAL_ID, supersededApprovalView],
        [REPAIRED_APPROVAL_ID, skippedApproval],
      ]),
      bindings: new Map([
        [PATCH_ID, supersededApprovalBinding],
        [REPAIRED_PATCH_ID, skippedBinding],
      ]),
      patches: new Map([
        [PATCH_ID, supersededPatchView],
        [REPAIRED_PATCH_ID, skippedPatch],
      ]),
    });
  });

  it.each(["attempt", "terminal-kind"] as const)(
    "fails closed before workflow reads when Run and manifest %s disagree",
    async (mismatch) => {
      const artifacts = new Map(successfulArtifacts());
      let runView = run("succeeded", { resultArtifactId: RESULT_ID });
      if (mismatch === "attempt") {
        const manifest = successfulManifest();
        const payload = manifest.payload as Record<string, unknown>;
        payload.attempt_no = 2;
        (payload.version_projection as Record<string, unknown>).attempt_no = 2;
        artifacts.set(RESULT_ID, manifest);
      } else {
        runView = run("failed", { failureArtifactId: RESULT_ID });
      }
      const harness = outcomeApi(async () => runView, artifacts);
      renderOutcome(harness.api);

      expect(await screen.findByRole("heading", { name: "候选 authority 不安全" })).toBeVisible();
      expect(harness.api.getPatch).not.toHaveBeenCalled();
      expect(harness.api.getApprovalBinding).not.toHaveBeenCalled();
      expect(screen.queryByRole("navigation", { name: "候选后续动作" })).not.toBeInTheDocument();
    },
  );

  it("fails closed on a malformed result manifest before any workflow read", async () => {
    const malformed: ArtifactPayloadView = {
      artifact: summary(RESULT_ID, "run_result", "run-result@1"),
      payload: { result_schema_version: "run-result@1" },
      resource_revision: 1,
      view_schema_version: "artifact-payload-view@1",
    };
    const harness = outcomeApi(
      async () => run("succeeded", { resultArtifactId: RESULT_ID }),
      new Map([[RESULT_ID, malformed]]),
    );

    renderOutcome(harness.api);

    expect(await screen.findByText("malformed_manifest")).toBeVisible();
    expect(harness.api.getArtifact).toHaveBeenCalledTimes(1);
    expect(harness.api.getPatch).not.toHaveBeenCalled();
    expect(harness.api.getApprovalBinding).not.toHaveBeenCalled();
    expect(harness.api.getApproval).not.toHaveBeenCalled();
  });
});
