import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import { CursorExpiredError } from "../../api/pagination";
import type { ReplaySourceRun } from "../runs/replaySources";
import { ApiProblemError } from "../../api/problem";
import { createQueryClient } from "../../api/query-client";
import type {
  ConflictPage,
  ExecutionOptionView,
  PatchWorkflowApi,
  RefHistoryPageResponse,
  SnapshotDiffPage,
} from "./api";
import { PatchDetailPage } from "./PatchDetailPage";

type ApprovalStatus = components["schemas"]["ApprovalItem"]["status"];
type ApprovalView = components["schemas"]["ApprovalViewV1"];
type ArtifactKind = components["schemas"]["ArtifactSummaryV1"]["kind"];
type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
type ExecutionProfile = components["schemas"]["ExecutionProfileViewV1"];
type FindingEvidenceBinding = components["schemas"]["FindingEvidenceBindingV1"];
type FindingRevision = components["schemas"]["FindingRevisionV1"];
type PatchView = components["schemas"]["PatchArtifactReadViewV1"];
type RunFindingLink = components["schemas"]["RunFindingLinkViewV1"];
type RunAccepted = components["schemas"]["RunAcceptedV1"];
type RunView = components["schemas"]["RunViewV1"];

const PATCH_ID = "artifact:patch:detail";
const BASE_ID = "artifact:spec:base";
const CURRENT_ID = "artifact:spec:current";
const PREVIEW_ID = "artifact:spec:preview";
const BASE_SNAPSHOT = "snapshot:base";
const CURRENT_SNAPSHOT = "snapshot:current";
const PREVIEW_SNAPSHOT = "snapshot:preview";
const APPROVAL_ID = "approval:patch:detail";
const SUBJECT_DIGEST = "a".repeat(64);
const TARGET_DIGEST = "b".repeat(64);
const REF_NAME = "spec/main";
const VALIDATION_RUN_ID = "run:patch-validation:failed";
const FINDING_RUN_ID = "run:review:current-preview";
const REPAIR_RUN_ID = "run:repair";
const REPAIR_RESULT_ID = "artifact:run-result:repair";
const REPAIRED_PATCH_ID = "artifact:patch:repaired";
const REPAIRED_PREVIEW_ID = "artifact:spec:repaired-preview";
const REPAIRED_CONFIG_ID = "artifact:config:repaired";
const REPAIRED_CONFIG_2_ID = "artifact:config:repaired-secondary";
const REPAIR_FINDINGS: FindingEvidenceBinding[] = [
  {
    evidence_artifact_id: "artifact:review:economy-collapse",
    finding_digest: "4".repeat(64),
    finding_id: "finding:economy-collapse",
    finding_revision: 3,
  },
  {
    evidence_artifact_id: "artifact:checker:reward-cap",
    finding_digest: "5".repeat(64),
    finding_id: "finding:reward-cap",
    finding_revision: 1,
  },
];

function summary(
  artifactId: string,
  kind: components["schemas"]["ArtifactSummaryV1"]["kind"],
  payloadSchemaId: string,
  snapshotId: string,
) {
  return {
    artifact_id: artifactId,
    created_at: "2026-07-20T06:00:00Z",
    domain_scope: { domain_ids: ["domain:economy"] },
    kind,
    lineage_schema_version: "lineage@2" as const,
    parent_artifact_ids: [],
    payload_hash: kind === "patch" ? SUBJECT_DIGEST : "c".repeat(64),
    payload_schema_id: payloadSchemaId,
    summary_schema_version: "artifact-summary@1" as const,
    version_tuple: { ir_snapshot_id: snapshotId },
  };
}

function patchView(status: ApprovalStatus): PatchView {
  const passed = status === "validated" || status === "approved" || status === "applied";
  return {
    approval_status: status,
    artifact: {
      ...summary(PATCH_ID, "patch", "patch@2", BASE_SNAPSHOT),
      parent_artifact_ids: [BASE_ID, PREVIEW_ID],
    },
    patch: {
      base_snapshot_id: BASE_SNAPSHOT,
      expected_to_fix: ["金币奖励高于回收速率"],
      ops: [
        {
          new_value: 80,
          old_value: 120,
          op: "set_entity_attr",
          op_id: "op:reward-cap",
          target: "quest:side-01.reward_gold",
        },
        {
          new_value: { id: "npc:lin-yi", name: "林逸", type: "NPC" },
          old_value: null,
          op: "add_entity",
          op_id: "op:add-lin-yi",
          target: "npc:lin-yi",
        },
      ],
      patch_schema_version: "patch@2",
      preconditions: [],
      produced_by: "human",
      producer_run_id: null,
      rationale: "Bring the reward back under the deterministic sink rate.",
      revision: 1,
      side_effect_risk: "low",
      supersedes_artifact_id: null,
      target_snapshot_id: PREVIEW_SNAPSHOT,
    },
    regression_status: passed ? "passed" : "not_started",
    validation_status:
      status === "validating"
        ? "running"
        : status === "validation_failed"
          ? "failed"
          : passed
            ? "passed"
            : "not_started",
    view_schema_version: "patch-artifact-read-view@1",
    workflow_revision: status === "applied" ? 4 : 3,
  };
}

function repairedPatchView(): PatchView {
  const value = patchView("draft");
  return {
    ...value,
    artifact: {
      ...value.artifact,
      artifact_id: REPAIRED_PATCH_ID,
      parent_artifact_ids: [...value.artifact.parent_artifact_ids, PATCH_ID, REPAIRED_PREVIEW_ID],
      payload_hash: "9".repeat(64),
    },
    patch: {
      ...value.patch,
      produced_by: "agent",
      producer_run_id: REPAIR_RUN_ID,
      revision: 2,
      supersedes_artifact_id: PATCH_ID,
    },
    workflow_revision: 1,
  };
}

function repairSuccessorPatchView(status: ApprovalStatus = "draft"): PatchView {
  const value = repairedPatchView();
  const passed = status === "validated" || status === "approved" || status === "applied";
  return {
    ...value,
    approval_status: status,
    artifact: {
      ...value.artifact,
      parent_artifact_ids: [
        ...value.artifact.parent_artifact_ids,
        "artifact:evidence:patch",
        ...REPAIR_FINDINGS.map((finding) => finding.evidence_artifact_id),
      ].sort(),
    },
    patch: {
      ...value.patch,
      expected_to_fix: REPAIR_FINDINGS.map((finding) => finding.finding_id),
    },
    regression_status: passed ? "passed" : "not_started",
    validation_status: status === "validating" ? "running" : passed ? "passed" : "not_started",
    workflow_revision: status === "draft" ? 1 : status === "validating" ? 2 : 3,
  };
}

function approvalView(status: ApprovalStatus): ApprovalView {
  const passed = status === "validated" || status === "approved" || status === "applied";
  return {
    approval: {
      active_validation_run_id: status === "validating" ? "run:validation:active" : null,
      applied_at: status === "applied" ? "2026-07-20T06:20:00Z" : null,
      approval_id: APPROVAL_ID,
      approval_policy: { policy_digest: "d".repeat(64), policy_version: "1" },
      approval_schema_version: "approval@1",
      auto_apply_proof: null,
      created_at: "2026-07-20T06:00:00Z",
      decided_at: status === "approved" || status === "applied" ? "2026-07-20T06:10:00Z" : null,
      decisions: [],
      domain_registry_ref: { registry_digest: "e".repeat(64), registry_version: "1" },
      domain_scope: { domain_ids: ["domain:economy"] },
      evidence_set_artifact_id: status === "validation_failed" || passed ? "artifact:evidence:patch" : null,
      // A deterministic/business validation failure succeeds as a Run and binds
      // EvidenceSet; last_validation_failure_artifact_id is only for execution-terminal failure.
      last_validation_failure_artifact_id: null,
      proposer: { principal_id: "principal:maker", principal_kind: "human" },
      regression_evidence_artifact_ids: passed ? ["artifact:regression:patch"] : [],
      requirements: [],
      role_policy_digest: "f".repeat(64),
      role_policy_version: "1",
      route_policy: {
        domain_registry_ref: { registry_digest: "e".repeat(64), registry_version: "1" },
        route_digest: "1".repeat(64),
        route_version: "1",
      },
      status,
      subject_artifact_id: PATCH_ID,
      subject_digest: SUBJECT_DIGEST,
      subject_kind: "patch",
      subject_revision: 1,
      subject_series_id: "patch-series:detail",
      submitted_at: null,
      supersedes_approval_id: null,
      target_binding: {
        binding_schema_version: "approval-target-binding@1",
        expected_ref: { artifact_id: BASE_ID, revision: 1 },
        ref_name: REF_NAME,
        subject_kind: "patch",
        target_artifact_id: PREVIEW_ID,
        target_artifact_kind: "ir_snapshot",
        target_digest: TARGET_DIGEST,
        target_snapshot_id: PREVIEW_SNAPSHOT,
      },
      workflow_revision: status === "applied" ? 4 : 3,
    },
    current_actor_allowed_requirement_ids: [],
    requirement_progress: [],
    view_schema_version: "approval-view@1",
  };
}

function binding(status: ApprovalStatus) {
  return {
    approval_id: APPROVAL_ID,
    approval_status: status,
    is_current_head: true,
    subject_artifact_id: PATCH_ID,
    subject_digest: SUBJECT_DIGEST,
    subject_head_revision: 1,
    subject_kind: "patch" as const,
    subject_revision: 1,
    subject_series_id: "patch-series:detail",
    view_schema_version: "subject-approval-binding-view@1" as const,
    workflow_revision: status === "applied" ? 4 : 3,
  };
}

function repairedBinding() {
  return {
    ...binding("draft"),
    approval_id: "approval:patch:repaired",
    subject_artifact_id: REPAIRED_PATCH_ID,
    subject_digest: "9".repeat(64),
    subject_head_revision: 2,
    subject_revision: 2,
    workflow_revision: 1,
  };
}

function repairSuccessorBinding(status: ApprovalStatus = "draft") {
  return {
    ...repairedBinding(),
    approval_status: status,
    workflow_revision: status === "draft" ? 1 : status === "validating" ? 2 : 3,
  };
}

function repairedApprovalView(): ApprovalView {
  const value = approvalView("draft");
  return {
    ...value,
    approval: {
      ...value.approval,
      approval_id: "approval:patch:repaired",
      subject_artifact_id: REPAIRED_PATCH_ID,
      subject_digest: "9".repeat(64),
      subject_revision: 2,
      supersedes_approval_id: APPROVAL_ID,
      target_binding: {
        ...value.approval.target_binding!,
        target_artifact_id: REPAIRED_PREVIEW_ID,
      },
      workflow_revision: 1,
    },
  };
}

function repairSuccessorApprovalView(status: ApprovalStatus = "draft"): ApprovalView {
  const value = repairedApprovalView();
  return {
    ...value,
    approval: {
      ...value.approval,
      active_validation_run_id: status === "validating" ? "run:validation:active" : null,
      evidence_set_artifact_id:
        status === "validated" || status === "approved" || status === "applied"
          ? "artifact:evidence:successor"
          : null,
      regression_evidence_artifact_ids:
        status === "validated" || status === "approved" || status === "applied"
          ? ["artifact:regression:successor"]
          : [],
      status,
      workflow_revision: status === "draft" ? 1 : status === "validating" ? 2 : 3,
    },
  };
}

function profile(kind: ExecutionProfile["profile_kind"], runKind: string): ExecutionProfile {
  return {
    compatible_run_kinds: [{ kind: runKind, version: 1 }],
    display_name: `builtin.${kind}`,
    domain_scope: { domain_ids: ["domain:economy"] },
    env_contract_version: null,
    input_schema_ids: [],
    output_schema_ids: [],
    profile: { profile_id: `builtin.${kind}`, version: 1 },
    profile_kind: kind,
    profile_payload_hash: "2".repeat(64),
    required_capabilities: [],
    status: "active",
    stochastic: kind === "patch_repair",
    target_environment_profile: null,
  };
}

function page<T>(items: T[], snapshot: string, nextCursor: string | null = null) {
  return {
    expires_at: "2026-07-20T07:00:00Z",
    items,
    next_cursor: nextCursor,
    page_schema_version: "page@1" as const,
    read_snapshot_id: snapshot,
  };
}

function replayRun(runId: string, status: RunView["status"] = "failed", attemptNo = 2): ReplaySourceRun {
  return {
    attempt_no: attemptNo,
    completedAt: "2026-07-23T03:47:50Z",
    events_url: `/api/v1/runs/${runId}/events`,
    failure_artifact_id: status === "failed" ? "artifact:failure:source" : null,
    outcomeCode: status === "failed" ? "repair_source_failed" : "repair_completed",
    result_artifact_id: status === "succeeded" ? "artifact:result:source" : null,
    revision: 7,
    runKind: { kind: "patch.repair", version: 1 },
    run_id: runId,
    status,
    status_url: `/api/v1/runs/${runId}`,
    terminal_cassette_artifact_id: `artifact:cassette:${runId}`,
    view_schema_version: "run-view@1",
  };
}

function repairRun(status: RunView["status"] = "succeeded", overrides: Partial<RunView> = {}): RunView {
  return {
    attempt_no: 1,
    events_url: `/api/v1/runs/${REPAIR_RUN_ID}/events`,
    failure_artifact_id: status === "failed" ? "artifact:run-failure:repair" : null,
    result_artifact_id: status === "succeeded" ? REPAIR_RESULT_ID : null,
    revision: 4,
    run_id: REPAIR_RUN_ID,
    status,
    status_url: `/api/v1/runs/${REPAIR_RUN_ID}`,
    terminal_cassette_artifact_id: "artifact:cassette:repair",
    view_schema_version: "run-view@1",
    ...overrides,
  };
}

function repairResultArtifact(payloadOverrides: Record<string, unknown> = {}): ArtifactPayloadView {
  const producedArtifactIds = [
    REPAIRED_CONFIG_ID,
    REPAIRED_CONFIG_2_ID,
    REPAIRED_PATCH_ID,
    REPAIRED_PREVIEW_ID,
  ].sort();
  return {
    artifact: {
      ...summary(REPAIR_RESULT_ID, "run_result", "run-result@1", PREVIEW_SNAPSHOT),
      parent_artifact_ids: [...producedArtifactIds],
      payload_hash: "8".repeat(64),
    },
    payload: {
      attempt_no: 1,
      finding_count: 0,
      outcome_code: "repair_verified",
      primary_artifact_id: REPAIRED_PATCH_ID,
      produced_artifact_ids: producedArtifactIds,
      requirement_dispositions: [],
      result_schema_version: "run-result@1",
      run_id: REPAIR_RUN_ID,
      run_kind: { kind: "patch.repair", version: 1 },
      summary: {
        finding_count: 0,
        outcome_code: "repair_verified",
        primary_artifact_kind: "patch",
        produced_artifact_count: producedArtifactIds.length,
        summary_schema_version: "run-result-summary@1",
      },
      version_projection: {
        attempt_no: 1,
        manifest_scope: "run",
        parents: [
          {
            artifact_id: REPAIRED_CONFIG_ID,
            publication: "run_published",
            role: "output",
          },
          {
            artifact_id: REPAIRED_CONFIG_2_ID,
            publication: "run_published",
            role: "output",
          },
          {
            artifact_id: REPAIRED_PATCH_ID,
            publication: "run_published",
            role: "output",
          },
          {
            artifact_id: REPAIRED_PREVIEW_ID,
            publication: "run_published",
            role: "output",
          },
        ],
        projection_schema_version: "run-manifest-version-projection@1",
        run_kind: { kind: "patch.repair", version: 1 },
      },
      ...payloadOverrides,
    },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

function repairedProducedArtifact(artifactId: string): ArtifactPayloadView {
  if (artifactId === REPAIRED_PATCH_ID) {
    return {
      ...catalogArtifact(artifactId, "patch"),
      artifact: {
        ...catalogArtifact(artifactId, "patch").artifact,
        payload_hash: "9".repeat(64),
        payload_schema_id: "patch@2",
      },
    };
  }
  if (artifactId === REPAIRED_PREVIEW_ID) {
    return {
      ...catalogArtifact(artifactId, "ir_snapshot"),
      artifact: {
        ...catalogArtifact(artifactId, "ir_snapshot").artifact,
        payload_schema_id: "ir-core@1",
      },
    };
  }
  return {
    ...catalogArtifact(artifactId, "config_export"),
    artifact: {
      ...catalogArtifact(artifactId, "config_export").artifact,
      payload_schema_id: "config-export-package@1",
    },
  };
}

function findingRevision(
  overrides: Partial<FindingRevision> = {},
  payloadOverrides: Partial<FindingRevision["payload"]> = {},
): FindingRevision {
  return {
    created_at: "2026-07-23T08:00:00Z",
    finding_id: "finding:current-preview",
    payload: {
      confidence: null,
      constraint_id: null,
      defect_class: "economy_collapse",
      entities: ["currency:gold"],
      evidence: { net_flow: 42 },
      message: "金币净流入超过回收能力。",
      minimal_repro: { horizon: 30 },
      oracle_type: "simulation",
      payload_schema_version: "finding-payload@1",
      producer_id: "economy_sim",
      producer_run_id: FINDING_RUN_ID,
      relations: [],
      severity: "major",
      snapshot_id: PREVIEW_SNAPSHOT,
      source: "sim",
      status: "confirmed",
      ...payloadOverrides,
    },
    revision: 2,
    revision_schema_version: "finding-revision@1",
    supersedes_revision: 1,
    ...overrides,
  };
}

function findingLink(finding: FindingRevision, overrides: Partial<RunFindingLink> = {}): RunFindingLink {
  return {
    attempt_no: 1,
    evidence_artifact_id: "artifact:simulation:current-preview",
    finding,
    finding_digest: "7".repeat(64),
    ordinal: 1,
    run_id: finding.payload.producer_run_id,
    view_schema_version: "run-finding-link-view@1",
    ...overrides,
  };
}

function expiredCursor(staleCursor: string): CursorExpiredError {
  return new CursorExpiredError(
    {
      code: "cursor_expired",
      conflict_set_id: null,
      detail: "The signed page cursor expired.",
      earliest_cursor: null,
      instance: "/api/v1/snapshot-diff",
      request_id: "request:cursor-expired",
      retry_after_s: null,
      run_id: null,
      status: 410,
      title: "Cursor expired",
      trace_id: null,
      type: "https://gameforge.dev/problems/cursor-expired",
    },
    staleCursor,
  );
}

function workflowProblem(code: string, status: number, detail: string): ApiProblemError {
  return new ApiProblemError({
    code,
    conflict_set_id: null,
    detail,
    earliest_cursor: null,
    instance: "/api/v1/patches/artifact:patch:detail:submit-for-approval",
    request_id: `request:${code}`,
    retry_after_s: null,
    run_id: null,
    status,
    title: "Workflow command rejected",
    trace_id: null,
    type: `https://gameforge.dev/problems/${code}`,
  });
}

function diffPage(
  items: SnapshotDiffPage["page"]["items"],
  snapshot: string,
  nextCursor: string | null = null,
): SnapshotDiffPage {
  return {
    diff: {
      base_snapshot_id: BASE_SNAPSHOT,
      diff_schema_version: "snapshot-diff@1",
      entry_count: items.length,
      target_snapshot_id: PREVIEW_SNAPSHOT,
    },
    page: page(items, snapshot, nextCursor),
    page_schema_version: "snapshot-diff-http-page@1",
  };
}

function evidenceSetArtifact(payloadOverrides: Record<string, unknown> = {}): ArtifactPayloadView {
  const approval = approvalView("validation_failed").approval;
  return {
    artifact: {
      ...summary("artifact:evidence:patch", "validation_evidence", "evidence-set@1", PREVIEW_SNAPSHOT),
      parent_artifact_ids: [
        PATCH_ID,
        PREVIEW_ID,
        ...REPAIR_FINDINGS.map((binding) => binding.evidence_artifact_id),
      ],
      payload_hash: "3".repeat(64),
    },
    payload: {
      evidence_schema_version: "evidence-set@1",
      finding_bindings: REPAIR_FINDINGS,
      overall_status: "failed",
      policy_version: "builtin.patch-validation@1",
      requirements: [
        {
          applicability: "required",
          evidence_artifact_id: "artifact:checker:reward-cap",
          kind: "checker",
          reason_code: "checker_contains_confirmed_findings",
          requirement_id: "finding:reward-cap",
          status: "failed",
          tool_version: "checker@1",
        },
      ],
      subject_artifact_id: PATCH_ID,
      subject_digest: SUBJECT_DIGEST,
      supporting_artifact_ids: REPAIR_FINDINGS.map((binding) => binding.evidence_artifact_id).sort(),
      target_binding: approval.target_binding,
      validation_run_id: VALIDATION_RUN_ID,
      ...payloadOverrides,
    },
    resource_revision: 1 as const,
    view_schema_version: "artifact-payload-view@1" as const,
  } as ArtifactPayloadView;
}

function runFailureArtifact(): ArtifactPayloadView {
  return {
    artifact: {
      ...summary("artifact:failure:patch", "run_failure", "run-failure@1", PREVIEW_SNAPSHOT),
      payload_hash: "6".repeat(64),
    },
    payload: {},
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

function catalogArtifact(artifactId: string, kind: ArtifactKind) {
  return {
    artifact: summary(artifactId, kind, `${kind}@1`, PREVIEW_SNAPSHOT),
    payload: {},
    resource_revision: 1 as const,
    view_schema_version: "artifact-payload-view@1" as const,
  };
}

function api(status: ApprovalStatus = "draft", overrides: Partial<PatchWorkflowApi> = {}): PatchWorkflowApi {
  const view = patchView(status);
  const approval = approvalView(status);
  const profiles = [
    profile("validation", "patch.validate"),
    profile("patch_repair", "patch.repair"),
    profile("checker", "patch.validate"),
    profile("simulation", "patch.validate"),
    profile("config_export", "config.export"),
  ];
  return {
    getApproval: vi.fn(async () => ({ etag: '"approval:3"', value: approval })),
    getApprovalBinding: vi.fn(async () => binding(status)),
    getArtifact: vi.fn(async (artifactId) =>
      artifactId === "artifact:failure:patch" ? runFailureArtifact() : evidenceSetArtifact(),
    ),
    getPatch: vi.fn(async () => ({ etag: '"patch:3"', value: view })),
    getSnapshotDiff: vi.fn(async () => ({
      diff: {
        base_snapshot_id: BASE_SNAPSHOT,
        diff_schema_version: "snapshot-diff@1",
        entry_count: 1,
        target_snapshot_id: PREVIEW_SNAPSHOT,
      },
      page: page(
        [
          {
            after: { presence: "present", value: 80 },
            before: { presence: "present", value: 120 },
            path: "/economy/reward",
          },
        ],
        "read:diff",
      ),
      page_schema_version: "snapshot-diff-http-page@1",
    })),
    getSpec: vi.fn(async (artifactId) => ({
      artifact: summary(
        artifactId,
        "ir_snapshot",
        "ir-core@1",
        artifactId === CURRENT_ID ? CURRENT_SNAPSHOT : BASE_SNAPSHOT,
      ),
      ref_name: REF_NAME,
      ref_value: { artifact_id: artifactId, revision: artifactId === CURRENT_ID ? 2 : 1 },
      schema_registry_version: "ir-core@1",
      snapshot_id: artifactId === CURRENT_ID ? CURRENT_SNAPSHOT : BASE_SNAPSHOT,
      view_schema_version: "spec-view@1",
    })),
    listConflicts: vi.fn(async () => page([], "read:conflicts")),
    listArtifacts: vi.fn(async (kind: ArtifactKind) => page([], `read:artifacts:${kind}`)),
    listFindings: vi.fn(async () => page([], "read:findings")),
    listRunFindingLinks: vi.fn(async (runId: string) => page([], `read:finding-links:${runId}`)),
    listReplaySourceRuns: vi.fn(async () => page([], "read:replay-runs")),
    resolveExecutionOption: vi.fn(async (request) => ({
      cassette_artifact_id: null,
      domain_scope: { domain_ids: ["domain:economy"] },
      execution_version_plan: {
        agent_graph_version: "patch-repair@1",
        model_catalog_digest: "4".repeat(64),
        model_catalog_version: 1,
        nodes: [],
        plan_digest: "5".repeat(64),
        plan_schema_version: "execution-version-plan@1",
        routing_policy_digest: "6".repeat(64),
        routing_policy_version: 1,
      },
      llm_execution_mode: request.llm_execution_mode,
      option_id: `execution-option:sha256:${"7".repeat(64)}`,
      option_schema_version: "execution-option@1",
      prospective_request_hash: "8".repeat(64),
      resolved_profile_binding_digests: [],
      resolved_request_hash: "9".repeat(64),
      resource_operation_id: request.resource_operation_id,
      run_kind: request.run_kind,
      source_run_id: request.replay_source_run_id,
    })),
    listExecutionProfiles: vi.fn(async (filters) =>
      page(
        profiles.filter((candidate) => candidate.profile_kind === filters.profile_kind),
        `read:profiles:${filters.profile_kind}`,
      ),
    ),
    listRefHistory: vi.fn(async () =>
      page(
        [
          {
            entry_schema_version: "ref-history-entry@1",
            ref_name: REF_NAME,
            value: { artifact_id: BASE_ID, revision: 1 },
          },
        ],
        "read:history",
      ),
    ),
    ...overrides,
  } as unknown as PatchWorkflowApi;
}

function renderPage(
  patchApi: PatchWorkflowApi,
  path = `/patches/${encodeURIComponent(PATCH_ID)}`,
  artifactId = PATCH_ID,
) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <PatchDetailPage api={patchApi} artifactId={artifactId} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Patch detail", () => {
  it("renders base/current/proposed and submits only explicit server-defined conflict choices", async () => {
    const user = userEvent.setup();
    const rebasePatch = vi.fn(async () => ({
      conflict_set_id: "conflict-set:patch",
      new_patch_artifact_id: null,
      status: "conflicted" as const,
    }));
    const resolvePatchConflicts = vi.fn<PatchWorkflowApi["resolvePatchConflicts"]>(async () => ({
      conflict_set_id: "conflict-set:patch",
      new_patch_artifact_id: null,
      status: "conflicted" as const,
    }));
    const conflicts: ConflictPage = page(
      [
        {
          allowed_resolutions: ["keep_current", "take_proposed", "custom"],
          base: { presence: "present", value: 120 },
          current: { presence: "present", value: 100 },
          id: "conflict:reward",
          kind: "replace_replace",
          path: "/economy/reward",
          proposed: { presence: "present", value: 80 },
        },
      ],
      "read:conflicts",
    );
    const history: RefHistoryPageResponse = page(
      [
        {
          entry_schema_version: "ref-history-entry@1",
          ref_name: REF_NAME,
          value: { artifact_id: BASE_ID, revision: 1 },
        },
        {
          entry_schema_version: "ref-history-entry@1",
          ref_name: REF_NAME,
          value: { artifact_id: CURRENT_ID, revision: 2 },
        },
      ],
      "read:history",
    );
    const patchApi = api("draft", {
      listConflicts: vi.fn(async () => conflicts),
      listRefHistory: vi.fn(async () => history),
      rebasePatch,
      resolvePatchConflicts,
    });
    renderPage(patchApi);

    expect(await screen.findByText(CURRENT_SNAPSHOT)).toBeVisible();
    expect(screen.getAllByText(PREVIEW_SNAPSHOT)).not.toHaveLength(0);
    const threeWaySummary = screen
      .getByRole("heading", { name: "Base / Current / Proposed" })
      .closest("section");
    expect(threeWaySummary).not.toBeNull();
    expect(within(threeWaySummary!).getByText("Base Snapshot")).toBeVisible();
    expect(within(threeWaySummary!).getByText("Current Snapshot")).toBeVisible();
    const proposedArtifact = within(threeWaySummary!).getByText("Proposed Artifact").closest("div");
    expect(proposedArtifact).not.toBeNull();
    expect(within(proposedArtifact!).getByText(PREVIEW_ID)).toBeVisible();
    const proposedSnapshot = within(threeWaySummary!).getByText("Proposed Snapshot").closest("div");
    expect(proposedSnapshot).not.toBeNull();
    expect(within(proposedSnapshot!).getByText(PREVIEW_SNAPSHOT)).toBeVisible();
    const operations = screen.getByRole("list", { name: "Patch typed operations" });
    expect(operations).toHaveTextContent("修改实体字段");
    expect(operations).toHaveTextContent("quest:side-01.reward_gold");
    expect(operations).toHaveTextContent("修改前");
    expect(operations).toHaveTextContent("120");
    expect(operations).toHaveTextContent("修改后");
    expect(operations).toHaveTextContent("80");
    expect(operations).toHaveTextContent("新增实体");
    expect(operations).toHaveTextContent("原先不存在");
    await user.click(screen.getByRole("button", { name: "Rebase 到 exact current ref" }));
    const resolver = await screen.findByRole("heading", { name: "三方冲突解析" });
    const region = resolver.closest("section")!;
    await user.click(within(region).getByRole("radio", { name: "采用 Proposed" }));
    await user.click(screen.getByRole("button", { name: "提交全部显式 resolutions" }));

    await waitFor(() => expect(resolvePatchConflicts).toHaveBeenCalledTimes(1));
    expect(resolvePatchConflicts.mock.calls[0][1]).toMatchObject({
      approval_id: APPROVAL_ID,
      conflict_set_id: "conflict-set:patch",
      expected_ref: { artifact_id: CURRENT_ID, revision: 2 },
      expected_subject_head_revision: 1,
      expected_workflow_revision: 3,
      ref_name: REF_NAME,
      resolutions: [{ choice: "take_proposed", conflict_id: "conflict:reward" }],
    });
  });

  it("links a human Patch candidate to Review with the exact target Artifact and no source Run", async () => {
    const constraintId = "artifact:constraint:human-review";
    const getArtifact = vi.fn(async (artifactId: string) => {
      if (artifactId !== constraintId) throw new Error(`unexpected Artifact ${artifactId}`);
      return catalogArtifact(artifactId, "constraint_snapshot");
    });
    renderPage(
      api("draft", { getArtifact } as Partial<PatchWorkflowApi>),
      `/patches/${encodeURIComponent(PATCH_ID)}?constraint=${encodeURIComponent(constraintId)}`,
    );

    const reviewLink = await screen.findByRole("link", { name: "审查当前 Patch 候选" });
    const reviewUrl = new URL(reviewLink.getAttribute("href")!, "https://gameforge.test");
    expect(reviewUrl.pathname).toBe("/reviews");
    expect(reviewUrl.searchParams.get("snapshot")).toBe(PREVIEW_ID);
    expect(reviewUrl.searchParams.get("snapshot")).not.toBe(PREVIEW_SNAPSHOT);
    expect(reviewUrl.searchParams.get("constraint")).toBe(constraintId);
    expect(reviewUrl.searchParams.has("sourceRun")).toBe(false);
  });

  it("preserves an agent Patch producer Run as optional Review navigation context", async () => {
    const constraintId = "artifact:constraint:agent-review";
    const producerRunId = "run:generation:agent-patch";
    const agentPatch = patchView("draft");
    const getPatch = vi.fn(async () => ({
      etag: '"patch:3"',
      value: {
        ...agentPatch,
        patch: {
          ...agentPatch.patch,
          produced_by: "agent" as const,
          producer_run_id: producerRunId,
        },
      },
    }));
    const getArtifact = vi.fn(async (artifactId: string) => {
      if (artifactId !== constraintId) throw new Error(`unexpected Artifact ${artifactId}`);
      return catalogArtifact(artifactId, "constraint_snapshot");
    });
    renderPage(
      api("draft", { getArtifact, getPatch } as Partial<PatchWorkflowApi>),
      `/patches/${encodeURIComponent(PATCH_ID)}?constraint=${encodeURIComponent(constraintId)}`,
    );

    const reviewLink = await screen.findByRole("link", { name: "审查当前 Patch 候选" });
    const reviewUrl = new URL(reviewLink.getAttribute("href")!, "https://gameforge.test");
    expect(reviewUrl.searchParams.get("snapshot")).toBe(PREVIEW_ID);
    expect(reviewUrl.searchParams.get("constraint")).toBe(constraintId);
    expect(reviewUrl.searchParams.get("sourceRun")).toBe(producerRunId);
  });

  it("restarts an expired Diff pagination from cursor=null and replaces stale entries", async () => {
    const user = userEvent.setup();
    const getSnapshotDiff = vi
      .fn<PatchWorkflowApi["getSnapshotDiff"]>()
      .mockResolvedValueOnce(
        diffPage(
          [
            {
              after: { presence: "present", value: 80 },
              before: { presence: "present", value: 120 },
              path: "/economy/stale-page",
            },
          ],
          "read:diff:stale",
          "cursor:diff:2",
        ),
      )
      .mockRejectedValueOnce(expiredCursor("cursor:diff:2"))
      .mockResolvedValueOnce(
        diffPage(
          [
            {
              after: { presence: "present", value: 70 },
              before: { presence: "present", value: 100 },
              path: "/economy/fresh-page",
            },
          ],
          "read:diff:fresh",
        ),
      );
    renderPage(api("draft", { getSnapshotDiff }));

    await user.click(await screen.findByRole("button", { name: "加载更多 Diff entries" }));
    await user.click(await screen.findByRole("button", { name: "从第一页重新读取 Diff" }));

    await waitFor(() => expect(getSnapshotDiff).toHaveBeenCalledTimes(3));
    expect(getSnapshotDiff.mock.calls.map((call) => call[2])).toEqual([null, "cursor:diff:2", null]);
    expect(await screen.findByText("/economy/fresh-page")).toBeVisible();
    expect(screen.queryByText("/economy/stale-page")).not.toBeInTheDocument();
  });

  it("restarts an expired ConflictSet pagination from cursor=null", async () => {
    const user = userEvent.setup();
    const conflict: ConflictPage["items"][number] = {
      allowed_resolutions: ["keep_current", "take_proposed"],
      base: { presence: "present", value: 120 },
      current: { presence: "present", value: 100 },
      id: "conflict:cursor",
      kind: "replace_replace",
      path: "/economy/cursor",
      proposed: { presence: "present", value: 80 },
    };
    const listConflicts = vi
      .fn<PatchWorkflowApi["listConflicts"]>()
      .mockResolvedValueOnce(page([conflict], "read:conflict:stale", "cursor:conflict:2"))
      .mockRejectedValueOnce(expiredCursor("cursor:conflict:2"))
      .mockResolvedValueOnce(page([conflict], "read:conflict:fresh"));
    renderPage(
      api("draft", { listConflicts }),
      `/patches/${encodeURIComponent(PATCH_ID)}?conflictSet=conflict-set%3Acursor`,
    );

    await user.click(await screen.findByRole("button", { name: "从第一页重新读取冲突" }));

    expect(await screen.findByRole("heading", { name: "三方冲突解析" })).toBeVisible();
    expect(listConflicts.mock.calls.map((call) => call[1])).toEqual([null, "cursor:conflict:2", null]);
  });

  it("requires a fresh explicit choice when conflict authority switches to another set", async () => {
    const user = userEvent.setup();
    const conflict: ConflictPage["items"][number] = {
      allowed_resolutions: ["keep_current", "take_proposed"],
      base: { presence: "present", value: 120 },
      current: { presence: "present", value: 100 },
      id: "conflict:shared-id",
      kind: "replace_replace",
      path: "/economy/shared",
      proposed: { presence: "present", value: 80 },
    };
    const history: RefHistoryPageResponse = page(
      [
        {
          entry_schema_version: "ref-history-entry@1",
          ref_name: REF_NAME,
          value: { artifact_id: BASE_ID, revision: 1 },
        },
        {
          entry_schema_version: "ref-history-entry@1",
          ref_name: REF_NAME,
          value: { artifact_id: CURRENT_ID, revision: 2 },
        },
      ],
      "read:history",
    );
    const listConflicts = vi.fn<PatchWorkflowApi["listConflicts"]>(async () =>
      page([conflict], "read:conflicts"),
    );
    const resolvePatchConflicts = vi.fn<PatchWorkflowApi["resolvePatchConflicts"]>(async () => ({
      conflict_set_id: "conflict-set:B",
      new_patch_artifact_id: null,
      status: "conflicted",
    }));
    renderPage(
      api("draft", {
        listConflicts,
        listRefHistory: vi.fn(async () => history),
        rebasePatch: vi.fn<PatchWorkflowApi["rebasePatch"]>(async () => ({
          conflict_set_id: "conflict-set:A",
          new_patch_artifact_id: null,
          status: "conflicted",
        })),
        resolvePatchConflicts,
      }),
    );

    await user.click(await screen.findByRole("button", { name: "Rebase 到 exact current ref" }));
    const firstResolver = await screen.findByRole("heading", { name: "三方冲突解析" });
    await user.click(within(firstResolver.closest("section")!).getByRole("radio", { name: "采用 Proposed" }));
    await user.click(screen.getByRole("button", { name: "提交全部显式 resolutions" }));

    await waitFor(() => expect(listConflicts).toHaveBeenCalledWith("conflict-set:B", null));
    const secondResolver = await screen.findByRole("heading", { name: "三方冲突解析" });
    expect(
      within(secondResolver.closest("section")!).getByRole("radio", { name: "采用 Proposed" }),
    ).not.toBeChecked();
    expect(screen.getByRole("button", { name: "提交全部显式 resolutions" })).toBeDisabled();
  });

  it("verifies a clean rebase as a fresh revision with no inherited authority", async () => {
    const user = userEvent.setup();
    const replacementId = "artifact:patch:rebased";
    const replacementApprovalId = "approval:patch:rebased";
    const previousSuperseded: PatchView = {
      ...patchView("superseded"),
      approval_status: "superseded",
    };
    const previousBinding = {
      ...binding("superseded"),
      approval_status: "superseded" as const,
      is_current_head: false,
      subject_head_revision: 2,
    };
    const previousApproval: ApprovalView = {
      ...approvalView("superseded"),
      approval: { ...approvalView("superseded").approval, status: "superseded" },
    };
    const replacement: PatchView = {
      ...patchView("draft"),
      artifact: {
        ...summary(replacementId, "patch", "patch@2", CURRENT_SNAPSHOT),
        parent_artifact_ids: [CURRENT_ID, PATCH_ID, PREVIEW_ID].sort(),
      },
      patch: {
        ...patchView("draft").patch,
        base_snapshot_id: CURRENT_SNAPSHOT,
        revision: 2,
        supersedes_artifact_id: PATCH_ID,
      },
      workflow_revision: 1,
    };
    const replacementApproval: ApprovalView = {
      ...approvalView("draft"),
      approval: {
        ...approvalView("draft").approval,
        approval_id: replacementApprovalId,
        subject_artifact_id: replacementId,
        subject_revision: 2,
        supersedes_approval_id: APPROVAL_ID,
        target_binding: {
          ...approvalView("draft").approval.target_binding!,
          expected_ref: { artifact_id: CURRENT_ID, revision: 2 },
        },
        workflow_revision: 1,
      },
    };
    const replacementBinding = {
      ...binding("draft"),
      approval_id: replacementApprovalId,
      subject_artifact_id: replacementId,
      subject_head_revision: 2,
      subject_revision: 2,
      workflow_revision: 1,
    };
    const history: RefHistoryPageResponse = page(
      [
        {
          entry_schema_version: "ref-history-entry@1",
          ref_name: REF_NAME,
          value: { artifact_id: BASE_ID, revision: 1 },
        },
        {
          entry_schema_version: "ref-history-entry@1",
          ref_name: REF_NAME,
          value: { artifact_id: CURRENT_ID, revision: 2 },
        },
      ],
      "read:history",
    );
    let rebased = false;
    const patchApi = api("draft", {
      getApproval: vi.fn<PatchWorkflowApi["getApproval"]>(async (approvalId) => ({
        etag: '"approval:replacement"',
        value:
          approvalId === replacementApprovalId
            ? replacementApproval
            : rebased
              ? previousApproval
              : approvalView("draft"),
      })),
      getApprovalBinding: vi.fn<PatchWorkflowApi["getApprovalBinding"]>(async (subjectId) =>
        subjectId === replacementId ? replacementBinding : rebased ? previousBinding : binding("draft"),
      ),
      getPatch: vi.fn<PatchWorkflowApi["getPatch"]>(async (subjectId) => ({
        etag: '"patch:replacement"',
        value: subjectId === replacementId ? replacement : rebased ? previousSuperseded : patchView("draft"),
      })),
      listRefHistory: vi.fn(async () => history),
      rebasePatch: vi.fn<PatchWorkflowApi["rebasePatch"]>(async () => {
        rebased = true;
        return {
          conflict_set_id: null,
          new_patch_artifact_id: replacementId,
          status: "clean",
        };
      }),
    });
    renderPage(patchApi);

    await user.click(await screen.findByRole("button", { name: "Rebase 到 exact current ref" }));

    const receiptHeading = await screen.findByRole("heading", { name: "已创建独立 Patch revision" });
    const receipt = receiptHeading.closest('[role="status"]') as HTMLElement;
    expect(receipt).not.toBeNull();
    expect(receipt).toHaveTextContent("旧验证、证据与审批决定不继承");
    expect(within(receipt).getByRole("link", { name: "打开新 Patch revision" })).toHaveAttribute(
      "href",
      "/patches/artifact%3Apatch%3Arebased",
    );
    expect(screen.getByRole("button", { name: "启动 exact validation" })).toBeDisabled();
  });

  it("never resolves a deep-linked ConflictSet from a terminal Patch revision", async () => {
    const conflict: ConflictPage["items"][number] = {
      allowed_resolutions: ["take_proposed"],
      base: { presence: "present", value: 120 },
      current: { presence: "present", value: 100 },
      id: "conflict:terminal",
      kind: "replace_replace",
      path: "/economy/terminal",
      proposed: { presence: "present", value: 80 },
    };
    renderPage(
      api("applied", {
        listConflicts: vi.fn(async () => page([conflict], "read:terminal")),
        listRefHistory: vi.fn(async () =>
          page(
            [
              {
                entry_schema_version: "ref-history-entry@1" as const,
                ref_name: REF_NAME,
                value: { artifact_id: BASE_ID, revision: 1 },
              },
              {
                entry_schema_version: "ref-history-entry@1" as const,
                ref_name: REF_NAME,
                value: { artifact_id: CURRENT_ID, revision: 2 },
              },
            ],
            "read:terminal-history",
          ),
        ),
      }),
      `/patches/${encodeURIComponent(PATCH_ID)}?conflictSet=conflict-set%3Aterminal`,
    );

    const resolver = await screen.findByRole("heading", { name: "三方冲突解析" });
    const radio = within(resolver.closest("section")!).getByRole("radio", { name: "采用 Proposed" });
    await userEvent.click(radio);
    expect(screen.getByRole("button", { name: "提交全部显式 resolutions" })).toBeDisabled();
  });

  it("reads complete resource catalogs, verifies deep-linked IDs, and submits friendly selections", async () => {
    const user = userEvent.setup();
    const constraintId = "artifact:constraint:linked";
    const configOneId = "artifact:config:linked";
    const configTwoId = "artifact:config:second";
    const reviewId = "artifact:review:linked";
    const traceId = "artifact:trace:linked";
    const regressionId = "artifact:regression:linked";
    const kinds = new Map<string, ArtifactKind>([
      [constraintId, "constraint_snapshot"],
      [configOneId, "config_export"],
      [reviewId, "review_report"],
      [traceId, "playtest_trace"],
      [regressionId, "regression_suite"],
    ]);
    const accepted: RunAccepted = {
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run:validation/events",
      run_id: "run:validation",
      status_url: "/api/v1/runs/run:validation",
    };
    const validatePatch = vi.fn<PatchWorkflowApi["validatePatch"]>(async () => accepted);
    const listArtifacts = vi.fn(async (kind: ArtifactKind, cursor: string | null) => {
      if (kind === "config_export") {
        return cursor === null
          ? page([catalogArtifact(configOneId, kind).artifact], "read:config", "cursor:config:2")
          : page([catalogArtifact(configTwoId, kind).artifact], "read:config");
      }
      if (kind === "regression_suite") {
        return page([catalogArtifact(regressionId, kind).artifact], "read:regression");
      }
      return page([], `read:${kind}`);
    });
    const getArtifact = vi.fn(async (artifactId: string) => {
      const kind = kinds.get(artifactId);
      if (!kind) throw new Error(`unexpected Artifact ${artifactId}`);
      return catalogArtifact(artifactId, kind);
    });
    const query = new URLSearchParams({
      config: configOneId,
      constraint: constraintId,
      regression: regressionId,
      review: reviewId,
      trace: traceId,
    });
    renderPage(
      api("draft", { getArtifact, listArtifacts, validatePatch } as Partial<PatchWorkflowApi>),
      `/patches/${encodeURIComponent(PATCH_ID)}?${query.toString()}`,
    );

    await screen.findByRole("heading", { name: "Exact validation inputs" });
    expect(screen.getByRole("heading", { name: "Workflow evidence Artifact ledger" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "确定性预言机" })).not.toBeInTheDocument();
    await waitFor(() => expect(getArtifact).toHaveBeenCalledTimes(5));
    expect(getArtifact.mock.calls.map(([artifactId]) => artifactId).sort()).toEqual([...kinds.keys()].sort());
    expect(screen.getByRole("radio", { name: `约束快照 ${constraintId}` })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: `候选配置导出 ${configOneId}` })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: `审查报告 ${reviewId}` })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: `实测轨迹 ${traceId}` })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: `回归套件 ${regressionId}` })).toBeChecked();
    expect(screen.queryByLabelText(/Artifact IDs（每行一个）/)).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("搜索候选配置导出"), "config:second");
    await user.click(screen.getByRole("checkbox", { name: `候选配置导出 ${configTwoId}` }));
    await user.selectOptions(screen.getByLabelText("Validation policy"), "builtin.validation@1");
    const historicalFinding = {
      evidence_artifact_id: "artifact:evidence:old",
      finding_digest: "f".repeat(64),
      finding_id: "finding:economy:old",
      finding_revision: 2,
    };
    const advanced = screen.getByText("高级：精确 Finding 绑定").closest("details");
    expect(advanced).not.toHaveAttribute("open");
    await user.click(screen.getByText("高级：精确 Finding 绑定"));
    expect(screen.queryByLabelText(/本次观测 \/ Repair FindingEvidenceBindingV1/)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/历史 FindingEvidenceBindingV1/), {
      target: { value: JSON.stringify([historicalFinding]) },
    });
    await user.click(screen.getByRole("button", { name: "启动 exact validation" }));

    await waitFor(() => expect(validatePatch).toHaveBeenCalledTimes(1));
    expect(validatePatch.mock.calls[0][1]).toMatchObject({
      approval_id: APPROVAL_ID,
      base_snapshot_artifact_id: BASE_ID,
      candidate_config_export_artifact_ids: [configOneId, configTwoId],
      constraint_snapshot_artifact_id: constraintId,
      expected_subject_head_revision: 1,
      expected_workflow_revision: 3,
      expected_findings: [historicalFinding],
      findings: [],
      playtest_trace_artifact_ids: [traceId],
      preview_snapshot_artifact_id: PREVIEW_ID,
      regression_suite_artifact_ids: [regressionId],
      review_artifact_ids: [reviewId],
      seed: 1,
      subject_digest: SUBJECT_DIGEST,
      target: { expected_ref: { artifact_id: BASE_ID, revision: 1 }, ref_name: REF_NAME },
      validation_policy: { profile_id: "builtin.validation", version: 1 },
    });
    const acceptedRun = await screen.findByRole("link", { name: "打开 accepted Run" });
    expect(acceptedRun).toHaveAttribute("href", "/runs/run%3Avalidation");
    expect(acceptedRun.closest('[role="status"]')).not.toBeNull();
    expect(
      listArtifacts.mock.calls.filter(([kind]) => kind === "config_export").map(([, cursor]) => cursor),
    ).toEqual([null, "cursor:config:2"]);
  });

  it("preselects one unambiguous recommended validation profile once and respects a later opt-out", async () => {
    const user = userEvent.setup();
    renderPage(api("draft"));

    expect(await screen.findByLabelText("Validation policy")).toHaveValue("builtin.validation@1");
    const checker = screen.getByRole("checkbox", { name: "builtin.checker@1" });
    expect(checker).toBeChecked();

    await user.click(checker);
    expect(checker).not.toBeChecked();
    await waitFor(() => expect(checker).not.toBeChecked());
  });

  it("restores a Repair successor's exact historical Findings from the retained failed EvidenceSet", async () => {
    const user = userEvent.setup();
    const validatePatch = vi.fn<PatchWorkflowApi["validatePatch"]>(async () => ({
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run:successor-validation/events",
      run_id: "run:successor-validation",
      status_url: "/api/v1/runs/run:successor-validation",
    }));
    const predecessorApproval = approvalView("superseded");
    predecessorApproval.approval.evidence_set_artifact_id = "artifact:evidence:patch";
    const patchApi = api("draft", {
      getApproval: vi.fn(async (approvalId) =>
        approvalId === "approval:patch:repaired"
          ? { etag: '"approval:successor"', value: repairSuccessorApprovalView() }
          : { etag: '"approval:predecessor"', value: predecessorApproval },
      ),
      getApprovalBinding: vi.fn(async (subjectId) =>
        subjectId === REPAIRED_PATCH_ID
          ? repairSuccessorBinding()
          : {
              ...binding("superseded"),
              is_current_head: false,
              subject_head_revision: 2,
            },
      ),
      getArtifact: vi.fn(async () => evidenceSetArtifact()),
      getPatch: vi.fn(async (subjectId) =>
        subjectId === REPAIRED_PATCH_ID
          ? { etag: '"patch:successor"', value: repairSuccessorPatchView() }
          : { etag: '"patch:predecessor"', value: patchView("superseded") },
      ),
      validatePatch,
    });

    renderPage(patchApi, `/patches/${encodeURIComponent(REPAIRED_PATCH_ID)}`, REPAIRED_PATCH_ID);

    expect(
      await screen.findByText(`已从前序失败 EvidenceSet 恢复 ${REPAIR_FINDINGS.length} 项历史 Finding。`),
    ).toBeVisible();
    expect(screen.queryByLabelText(/历史 FindingEvidenceBindingV1/)).not.toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Validation policy"), "builtin.validation@1");
    await user.click(screen.getByRole("button", { name: "启动 exact validation" }));

    await waitFor(() => expect(validatePatch).toHaveBeenCalledTimes(1));
    expect(validatePatch.mock.calls[0][1]).toMatchObject({
      expected_findings: REPAIR_FINDINGS,
      findings: [],
    });
  });

  it("fails closed when a Repair successor expected_to_fix set differs from predecessor evidence", async () => {
    const successor = repairSuccessorPatchView();
    successor.patch.expected_to_fix = ["finding:unexpected"];
    const predecessorApproval = approvalView("superseded");
    predecessorApproval.approval.evidence_set_artifact_id = "artifact:evidence:patch";
    renderPage(
      api("draft", {
        getApproval: vi.fn(async (approvalId) =>
          approvalId === "approval:patch:repaired"
            ? { etag: '"approval:successor"', value: repairSuccessorApprovalView() }
            : { etag: '"approval:predecessor"', value: predecessorApproval },
        ),
        getApprovalBinding: vi.fn(async (subjectId) =>
          subjectId === REPAIRED_PATCH_ID
            ? repairSuccessorBinding()
            : {
                ...binding("superseded"),
                is_current_head: false,
                subject_head_revision: 2,
              },
        ),
        getArtifact: vi.fn(async () => evidenceSetArtifact()),
        getPatch: vi.fn(async (subjectId) =>
          subjectId === REPAIRED_PATCH_ID
            ? { etag: '"patch:successor"', value: successor }
            : { etag: '"patch:predecessor"', value: patchView("superseded") },
        ),
      }),
      `/patches/${encodeURIComponent(REPAIRED_PATCH_ID)}`,
      REPAIRED_PATCH_ID,
    );

    expect(await screen.findByRole("heading", { name: "Patch authority 不可用" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "启动 exact validation" })).not.toBeInTheDocument();
  });

  it("selects only exact current-preview evidence groups and submits their Run-linked bindings", async () => {
    const user = userEvent.setup();
    const current = findingRevision();
    const otherSnapshot = findingRevision(
      { finding_id: "finding:other-snapshot" },
      { message: "旧快照 Finding。", snapshot_id: "snapshot:other" },
    );
    const withoutProducer = findingRevision(
      { finding_id: "finding:without-producer" },
      { message: "没有 producer Run。", producer_run_id: "" },
    );
    const exactLink = findingLink(current);
    const listFindings = vi.fn<PatchWorkflowApi["listFindings"]>(async (cursor) =>
      cursor === null
        ? page([current], "read:current-findings", "cursor:findings:2")
        : page([otherSnapshot, withoutProducer], "read:current-findings"),
    );
    const listRunFindingLinks = vi.fn<PatchWorkflowApi["listRunFindingLinks"]>(async (_runId, cursor) =>
      cursor === null
        ? page([], "read:producer-links", "cursor:links:2")
        : page([exactLink], "read:producer-links"),
    );
    const accepted: RunAccepted = {
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run:validation/events",
      run_id: "run:validation",
      status_url: "/api/v1/runs/run:validation",
    };
    const validatePatch = vi.fn<PatchWorkflowApi["validatePatch"]>(async () => accepted);
    renderPage(api("draft", { listFindings, listRunFindingLinks, validatePatch }));

    const selector = await screen.findByRole("group", { name: "本次要验证的 Finding" });
    const currentCheckbox = within(selector).getByRole("checkbox", {
      name: /选择证据组：.*1 个 Finding/,
    });
    expect(within(selector).getByText("economy_collapse")).toBeVisible();
    expect(within(selector).getByText("confirmed · sim · simulation")).toBeVisible();
    expect(screen.queryByText("旧快照 Finding。")).not.toBeInTheDocument();
    expect(screen.queryByText("没有 producer Run。")).not.toBeInTheDocument();

    await user.click(currentCheckbox);
    await user.selectOptions(screen.getByLabelText("Validation policy"), "builtin.validation@1");
    await user.click(screen.getByRole("button", { name: "启动 exact validation" }));

    await waitFor(() => expect(validatePatch).toHaveBeenCalledTimes(1));
    expect(validatePatch.mock.calls[0]?.[1].findings).toEqual([
      {
        evidence_artifact_id: exactLink.evidence_artifact_id,
        finding_digest: exactLink.finding_digest,
        finding_id: current.finding_id,
        finding_revision: current.revision,
      },
    ]);
    expect(listFindings.mock.calls.map(([cursor]) => cursor)).toEqual([null, "cursor:findings:2"]);
    expect(listRunFindingLinks.mock.calls.map(([runId, cursor]) => [runId, cursor])).toEqual([
      [FINDING_RUN_ID, null],
      [FINDING_RUN_ID, "cursor:links:2"],
    ]);
  });

  it("polls exact workflow authority from validating to validation_failed", async () => {
    const user = userEvent.setup();
    let serverStatus: ApprovalStatus = "draft";
    let validatingReads = 0;
    const getPatch = vi.fn<PatchWorkflowApi["getPatch"]>(async () => {
      if (serverStatus === "validating" && ++validatingReads >= 2) serverStatus = "validation_failed";
      return { etag: '"patch:polling"', value: patchView(serverStatus) };
    });
    const getApprovalBinding = vi.fn<PatchWorkflowApi["getApprovalBinding"]>(async () =>
      binding(serverStatus),
    );
    const getApproval = vi.fn<PatchWorkflowApi["getApproval"]>(async () => ({
      etag: '"approval:polling"',
      value: approvalView(serverStatus),
    }));
    const validatePatch = vi.fn<PatchWorkflowApi["validatePatch"]>(async () => {
      serverStatus = "validating";
      return {
        accepted_schema_version: "run-accepted@1",
        events_url: "/api/v1/runs/run:validation:active/events",
        run_id: "run:validation:active",
        status_url: "/api/v1/runs/run:validation:active",
      };
    });
    renderPage(api("draft", { getApproval, getApprovalBinding, getPatch, validatePatch }));

    await user.selectOptions(await screen.findByLabelText("Validation policy"), "builtin.validation@1");
    await user.click(screen.getByRole("button", { name: "启动 exact validation" }));

    expect((await screen.findAllByText("validation_failed", {}, { timeout: 2_000 }))[0]).toBeVisible();
    expect(validatingReads).toBeGreaterThanOrEqual(2);
    expect(getPatch).toHaveBeenCalledTimes(3);
  });

  it("toggles every Finding in one evidence group atomically while keeping other evidence independent", async () => {
    const user = userEvent.setup();
    const rewardFinding = findingRevision(
      { finding_id: "finding:a-reward-cap" },
      {
        defect_class: "reward_out_of_range",
        message: "支线奖励金币超过上限。",
        oracle_type: "deterministic",
        producer_id: "smt_checker",
        source: "checker",
      },
    );
    const reachabilityFinding = findingRevision(
      { finding_id: "finding:b-reachability" },
      {
        defect_class: "unreachable_target",
        message: "任务目标缺少可达性证明。",
        oracle_type: "deterministic",
        producer_id: "graph_checker",
        source: "checker",
        status: "unproven",
      },
    );
    const economyFinding = findingRevision(
      { finding_id: "finding:c-economy" },
      { message: "金币净流入超过回收能力。" },
    );
    const sharedEvidenceId = "artifact:checker:shared-review";
    const otherEvidenceId = "artifact:simulation:economy";
    const links = [
      findingLink(rewardFinding, {
        evidence_artifact_id: sharedEvidenceId,
        finding_digest: "1".repeat(64),
        ordinal: 1,
      }),
      findingLink(reachabilityFinding, {
        evidence_artifact_id: sharedEvidenceId,
        finding_digest: "2".repeat(64),
        ordinal: 2,
      }),
      findingLink(economyFinding, {
        evidence_artifact_id: otherEvidenceId,
        finding_digest: "3".repeat(64),
        ordinal: 3,
      }),
    ];
    const accepted: RunAccepted = {
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run:validation/events",
      run_id: "run:validation",
      status_url: "/api/v1/runs/run:validation",
    };
    const validatePatch = vi.fn<PatchWorkflowApi["validatePatch"]>(async () => accepted);
    renderPage(
      api("draft", {
        listFindings: vi.fn(async () =>
          page([rewardFinding, reachabilityFinding, economyFinding], "read:current-findings"),
        ),
        listRunFindingLinks: vi.fn(async () => page(links, "read:producer-links")),
        validatePatch,
      }),
    );

    const selector = await screen.findByRole("group", { name: "本次要验证的 Finding" });
    const sharedGroup = within(selector).getByRole("checkbox", {
      name: /选择证据组：.*2 个 Finding/,
    });
    const otherGroup = within(selector).getByRole("checkbox", {
      name: /选择证据组：.*1 个 Finding/,
    });
    expect(within(selector).getAllByRole("checkbox")).toHaveLength(2);

    await user.click(sharedGroup);
    expect(sharedGroup).toBeChecked();
    expect(otherGroup).not.toBeChecked();
    await user.click(sharedGroup);
    expect(sharedGroup).not.toBeChecked();
    expect(otherGroup).not.toBeChecked();
    await user.click(sharedGroup);
    expect(sharedGroup).toBeChecked();
    expect(otherGroup).not.toBeChecked();
    await user.click(otherGroup);
    expect(sharedGroup).toBeChecked();
    expect(otherGroup).toBeChecked();
    await user.click(otherGroup);
    expect(sharedGroup).toBeChecked();
    expect(otherGroup).not.toBeChecked();

    await user.selectOptions(screen.getByLabelText("Validation policy"), "builtin.validation@1");
    await user.click(screen.getByRole("button", { name: "启动 exact validation" }));

    await waitFor(() => expect(validatePatch).toHaveBeenCalledTimes(1));
    expect(validatePatch.mock.calls[0]?.[1].findings).toEqual([
      {
        evidence_artifact_id: sharedEvidenceId,
        finding_digest: "1".repeat(64),
        finding_id: rewardFinding.finding_id,
        finding_revision: rewardFinding.revision,
      },
      {
        evidence_artifact_id: sharedEvidenceId,
        finding_digest: "2".repeat(64),
        finding_id: reachabilityFinding.finding_id,
        finding_revision: reachabilityFinding.revision,
      },
    ]);
  });

  it("runs an exact constraint-only checker and automatically selects its focused Finding", async () => {
    const user = userEvent.setup();
    const constraintId = "artifact:constraint:reward-cap";
    const reviewId = "artifact:review:composite";
    const focusedRunId = "run:checker:focused-reward";
    const focusedFinding = findingRevision(
      { finding_id: "finding:reward-cap", revision: 1, supersedes_revision: null },
      {
        constraint_id: "side_quest_reward_gold_cap",
        defect_class: "reward_out_of_range",
        entities: ["quest:side-01"],
        message: "支线任务奖励金币 150 超过上限 80。",
        oracle_type: "deterministic",
        producer_id: "constraint:side_quest_reward_gold_cap",
        producer_run_id: focusedRunId,
        source: "checker",
      },
    );
    const focusedLink = findingLink(focusedFinding, {
      evidence_artifact_id: "artifact:checker:focused-reward",
      run_id: focusedRunId,
    });
    let focusedReady = false;
    const focusedChecker = {
      ...profile("checker", "patch.validate"),
      compatible_run_kinds: [
        { kind: "checker.run", version: 1 },
        { kind: "patch.repair", version: 1 },
        { kind: "patch.validate", version: 1 },
      ],
    };
    const listExecutionProfiles = vi.fn<PatchWorkflowApi["listExecutionProfiles"]>(async (filters) =>
      page(
        filters.profile_kind === "checker"
          ? [focusedChecker]
          : [
              profile("validation", "patch.validate"),
              profile("patch_repair", "patch.repair"),
              profile("simulation", "patch.validate"),
              profile("config_export", "patch.repair"),
            ].filter((candidate) => candidate.profile_kind === filters.profile_kind),
        `read:profiles:${filters.profile_kind}`,
      ),
    );
    const listFindings = vi.fn<PatchWorkflowApi["listFindings"]>(async () =>
      page(focusedReady ? [focusedFinding] : [], "read:focused-findings"),
    );
    const listRunFindingLinks = vi.fn<PatchWorkflowApi["listRunFindingLinks"]>(async (runId) =>
      page(runId === focusedRunId ? [focusedLink] : [], `read:links:${runId}`),
    );
    const submitRun = vi.fn<PatchWorkflowApi["submitRun"]>(async () => ({
      accepted_schema_version: "run-accepted@1",
      events_url: `/api/v1/runs/${focusedRunId}/events`,
      run_id: focusedRunId,
      status_url: `/api/v1/runs/${focusedRunId}`,
    }));
    const getRun = vi.fn<PatchWorkflowApi["getRun"]>(async () => {
      focusedReady = true;
      return {
        attempt_no: 1,
        events_url: `/api/v1/runs/${focusedRunId}/events`,
        failure_artifact_id: null,
        result_artifact_id: "artifact:result:focused-reward",
        revision: 4,
        run_id: focusedRunId,
        status: "succeeded",
        status_url: `/api/v1/runs/${focusedRunId}`,
        terminal_cassette_artifact_id: null,
        view_schema_version: "run-view@1",
      };
    });
    const getArtifact = vi.fn<PatchWorkflowApi["getArtifact"]>(async (artifactId) => {
      if (artifactId === constraintId) return catalogArtifact(constraintId, "constraint_snapshot");
      if (artifactId === reviewId) return catalogArtifact(reviewId, "review_report");
      return evidenceSetArtifact();
    });

    renderPage(
      api("draft", {
        getArtifact,
        getRun,
        listExecutionProfiles,
        listFindings,
        listRunFindingLinks,
        submitRun,
      }),
      `/patches/${encodeURIComponent(PATCH_ID)}?constraint=${encodeURIComponent(constraintId)}&review=${encodeURIComponent(reviewId)}`,
    );

    await user.click(await screen.findByRole("button", { name: "聚焦检查所选约束" }));

    await waitFor(() => expect(submitRun).toHaveBeenCalledTimes(1));
    expect(submitRun.mock.calls[0]?.[0]).toEqual({
      cassette_artifact_id: null,
      execution_version_plan: null,
      llm_execution_mode: "not_applicable",
      params: {
        checker_ids: [],
        checker_profile: { profile_id: "builtin.checker", version: 1 },
        constraint_snapshot_artifact_id: constraintId,
        defect_classes: [],
        schema_version: "checker-run@1",
        selection: { entity_ids: [], mode: "full", relation_ids: [] },
        snapshot_artifact_id: PREVIEW_ID,
      },
      request_schema_version: "run-submission-request@1",
      seed: null,
    });
    expect(await screen.findByText("支线任务奖励金币 150 超过上限 80。")).toBeVisible();
    expect(screen.getByRole("checkbox", { name: /选择证据组：.*1 个 Finding/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: new RegExp(reviewId) })).not.toBeChecked();
    expect(screen.getByRole("link", { name: "打开聚焦检查 Run" })).toHaveAttribute(
      "href",
      `/runs/${encodeURIComponent(focusedRunId)}`,
    );
  });

  it("fails closed when one producer Run returns duplicate Finding revision authority", async () => {
    const current = findingRevision();
    const first = findingLink(current);
    const duplicate = findingLink(current, {
      evidence_artifact_id: "artifact:simulation:conflicting",
      ordinal: 2,
    });
    renderPage(
      api("draft", {
        listFindings: vi.fn(async () => page([current], "read:current-findings")),
        listRunFindingLinks: vi.fn(async () => page([first, duplicate], "read:producer-links")),
      }),
    );

    expect(await screen.findByRole("heading", { name: "Finding authority 不可用" })).toBeVisible();
    expect(screen.getByRole("button", { name: "启动 exact validation" })).toBeDisabled();
  });

  it.each([
    ["schema", { view_schema_version: "run-finding-link-view@999" }],
    ["Run", { run_id: "run:other" }],
    ["attempt", { attempt_no: 0 }],
    ["ordinal", { ordinal: 0 }],
    ["digest", { finding_digest: "not-a-digest" }],
    ["evidence", { evidence_artifact_id: "" }],
    ["revision", { finding: findingRevision({}, { message: "冲突语义。" }) }],
    [
      "producer",
      {
        finding: findingRevision({}, { producer_run_id: "run:other" }),
        run_id: FINDING_RUN_ID,
      },
    ],
  ])("fails closed when a Run Finding link has invalid %s authority", async (_label, overrides) => {
    const current = findingRevision();
    const malformed = findingLink(current, overrides as Partial<RunFindingLink>);
    renderPage(
      api("draft", {
        listFindings: vi.fn(async () => page([current], "read:current-findings")),
        listRunFindingLinks: vi.fn(async () => page([malformed], "read:producer-links")),
      }),
    );

    expect(await screen.findByRole("heading", { name: "Finding authority 不可用" })).toBeVisible();
    expect(screen.getByRole("button", { name: "启动 exact validation" })).toBeDisabled();
  });

  it("fails closed when Finding pagination changes read snapshot", async () => {
    const current = findingRevision();
    renderPage(
      api("draft", {
        listFindings: vi.fn(async (cursor) =>
          cursor === null
            ? page([current], "read:findings:first", "cursor:findings:2")
            : page([], "read:findings:drifted"),
        ),
      }),
    );

    expect(await screen.findByRole("heading", { name: "Finding authority 不可用" })).toBeVisible();
    expect(screen.getByRole("button", { name: "启动 exact validation" })).toBeDisabled();
  });

  it("fails closed when a deep-linked exact ID has the wrong Artifact kind", async () => {
    const wrongId = "artifact:not-a-constraint";
    const getArtifact = vi.fn(async () => catalogArtifact(wrongId, "review_report"));
    renderPage(
      api("draft", { getArtifact } as Partial<PatchWorkflowApi>),
      `/patches/${encodeURIComponent(PATCH_ID)}?constraint=${encodeURIComponent(wrongId)}`,
    );

    expect(await screen.findByRole("heading", { name: "资源目录无法确认" })).toBeVisible();
    expect(getArtifact).toHaveBeenCalledWith(wrongId);
    expect(screen.getByRole("button", { name: "启动 exact validation" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Resolve 并启动 repair" })).toBeDisabled();
  });

  it("fails closed when paginated Artifact directory authority changes read snapshot", async () => {
    const listArtifacts = vi.fn(async (kind: ArtifactKind, cursor: string | null) => {
      if (kind !== "config_export") return page([], `read:${kind}`);
      return cursor === null
        ? page(
            [catalogArtifact("artifact:config:first", kind).artifact],
            "read:config:first",
            "cursor:config:next",
          )
        : page([catalogArtifact("artifact:config:second", kind).artifact], "read:config:drifted");
    });
    renderPage(api("draft", { listArtifacts } as Partial<PatchWorkflowApi>));

    expect(await screen.findByRole("heading", { name: "资源目录无法确认" })).toBeVisible();
    expect(screen.getByRole("button", { name: "启动 exact validation" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Resolve 并启动 repair" })).toBeDisabled();
    expect(
      listArtifacts.mock.calls.filter(([kind]) => kind === "config_export").map(([, cursor]) => cursor),
    ).toEqual([null, "cursor:config:next"]);
  });

  it("validates a first-write Patch by resolving its base from exact direct lineage", async () => {
    const user = userEvent.setup();
    const targetBinding = approvalView("draft").approval.target_binding;
    if (targetBinding?.subject_kind !== "patch") throw new Error("fixture lacks Patch target binding");
    const absentApproval: ApprovalView = {
      ...approvalView("draft"),
      approval: {
        ...approvalView("draft").approval,
        target_binding: {
          ...targetBinding,
          expected_ref: null,
        },
      },
    };
    const absentPatch: PatchView = {
      ...patchView("draft"),
      artifact: {
        ...patchView("draft").artifact,
        parent_artifact_ids: [BASE_ID],
      },
    };
    const accepted = {
      accepted_schema_version: "run-accepted@1" as const,
      events_url: "/api/v1/runs/run:first-write/events",
      run_id: "run:first-write",
      status_url: "/api/v1/runs/run:first-write",
    };
    const validatePatch = vi.fn<PatchWorkflowApi["validatePatch"]>(async () => accepted);
    renderPage(
      api("draft", {
        getApproval: vi.fn(async () => ({ etag: '"approval:first-write"', value: absentApproval })),
        getPatch: vi.fn(async () => ({ etag: '"patch:first-write"', value: absentPatch })),
        listLineage: vi.fn(async () =>
          page(
            [
              {
                artifact: summary(BASE_ID, "ir_snapshot", "ir-core@1", BASE_SNAPSHOT),
                depth: 1,
                entry_schema_version: "lineage-entry@1" as const,
              },
            ],
            "read:first-write-lineage",
          ),
        ),
        listRefHistory: vi.fn(async () => {
          throw workflowProblem("not_found", 404, "The target ref does not exist.");
        }),
        validatePatch,
      }),
    );

    await user.selectOptions(await screen.findByLabelText("Validation policy"), "builtin.validation@1");
    await user.click(screen.getByRole("button", { name: "启动 exact validation" }));

    await waitFor(() => expect(validatePatch).toHaveBeenCalledTimes(1));
    expect(validatePatch.mock.calls[0][1]).toMatchObject({
      base_snapshot_artifact_id: BASE_ID,
      target: { expected_ref: null, ref_name: REF_NAME },
    });
  });

  it.each([
    ["forbidden", 403, "The actor cannot submit this Patch."],
    ["revision_conflict", 409, "The workflow revision changed before submission."],
  ])("keeps %s visible and fail-closed", async (code, status, detail) => {
    const user = userEvent.setup();
    const submitPatchForApproval = vi.fn<PatchWorkflowApi["submitPatchForApproval"]>(async () => {
      throw workflowProblem(code, status, detail);
    });
    renderPage(api("validated", { submitPatchForApproval }));

    const submit = await screen.findByRole("button", { name: "Submit for independent approval" });
    await user.click(submit);

    const detailNode = await screen.findByText(detail);
    expect(detailNode).toBeVisible();
    expect(detailNode.closest('[role="alert"]')).toHaveAttribute("data-code", code);
    expect(submit).toBeDisabled();
    expect(screen.getByRole("button", { name: "重新读取 exact server state" })).toBeVisible();
  });

  it("renders exact failed EvidenceSet Findings and freezes them into the repair request", async () => {
    const user = userEvent.setup();
    const accepted: RunAccepted = {
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run:repair/events",
      run_id: "run:repair",
      status_url: "/api/v1/runs/run:repair",
    };
    const resolveExecutionOption = vi.fn<PatchWorkflowApi["resolveExecutionOption"]>(async (request) => ({
      cassette_artifact_id: null,
      domain_scope: { domain_ids: ["domain:economy"] },
      execution_version_plan: {
        agent_graph_version: "patch-repair@1",
        model_catalog_digest: "4".repeat(64),
        model_catalog_version: 1,
        nodes: [],
        plan_digest: "5".repeat(64),
        plan_schema_version: "execution-version-plan@1",
        routing_policy_digest: "6".repeat(64),
        routing_policy_version: 1,
      },
      llm_execution_mode: "record",
      option_id: `execution-option:sha256:${"7".repeat(64)}`,
      option_schema_version: "execution-option@1",
      prospective_request_hash: "8".repeat(64),
      resolved_profile_binding_digests: [],
      resolved_request_hash: "9".repeat(64),
      resource_operation_id: request.resource_operation_id,
      run_kind: { kind: "patch.repair", version: 1 },
      source_run_id: null,
    }));
    const repairPatch = vi.fn<PatchWorkflowApi["repairPatch"]>(async () => accepted);
    renderPage(api("validation_failed", { repairPatch, resolveExecutionOption }));

    const findings = await screen.findByRole("list", { name: "Repair Findings" });
    expect(within(findings).getByText("finding:economy-collapse")).toBeVisible();
    expect(within(findings).getByText("Revision 3")).toBeVisible();
    expect(within(findings).getByText("finding:reward-cap")).toBeVisible();
    expect(within(findings).getByText("Revision 1")).toBeVisible();
    expect(screen.queryByLabelText(/本次观测 \/ Repair FindingEvidenceBindingV1/)).not.toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Repair policy"), "builtin.patch_repair@1");
    await user.click(screen.getByRole("button", { name: "Resolve 并启动 repair" }));

    await waitFor(() => expect(repairPatch).toHaveBeenCalledTimes(1));
    expect(resolveExecutionOption.mock.calls[0]?.[0].prospective_request).toMatchObject({
      params: { findings: REPAIR_FINDINGS },
    });
    expect(repairPatch.mock.calls[0]?.[0]).toMatchObject({
      params: {
        findings: REPAIR_FINDINGS,
        validation_evidence_artifact_id: "artifact:evidence:patch",
      },
    });
  });

  it("polls an accepted Repair Run, strictly verifies its RunResult, and hands the primary Patch to the verified replacement receipt", async () => {
    const user = userEvent.setup();
    let accepted = false;
    const getRun = vi
      .fn<PatchWorkflowApi["getRun"]>()
      .mockResolvedValueOnce(repairRun("running"))
      .mockResolvedValue(repairRun("succeeded"));
    const getArtifact = vi.fn<PatchWorkflowApi["getArtifact"]>(async (artifactId) => {
      if (artifactId === REPAIR_RESULT_ID) return repairResultArtifact();
      if (
        [REPAIRED_PATCH_ID, REPAIRED_PREVIEW_ID, REPAIRED_CONFIG_ID, REPAIRED_CONFIG_2_ID].includes(
          artifactId,
        )
      ) {
        return repairedProducedArtifact(artifactId);
      }
      if (artifactId === "artifact:constraint:active") {
        return catalogArtifact(artifactId, "constraint_snapshot");
      }
      return evidenceSetArtifact();
    });
    const getPatch = vi.fn<PatchWorkflowApi["getPatch"]>(async (artifactId) => {
      if (artifactId === REPAIRED_PATCH_ID) {
        return { etag: '"patch:repaired"', value: repairedPatchView() };
      }
      return {
        etag: '"patch:previous"',
        value: patchView(accepted ? "superseded" : "validation_failed"),
      };
    });
    const getApprovalBinding = vi.fn<PatchWorkflowApi["getApprovalBinding"]>(async (artifactId) => {
      if (artifactId === REPAIRED_PATCH_ID) return repairedBinding();
      return {
        ...binding(accepted ? "superseded" : "validation_failed"),
        is_current_head: !accepted,
        subject_head_revision: accepted ? 2 : 1,
      };
    });
    const getApproval = vi.fn<PatchWorkflowApi["getApproval"]>(async (approvalId) => {
      if (approvalId === "approval:patch:repaired") {
        return { etag: '"approval:repaired"', value: repairedApprovalView() };
      }
      return {
        etag: '"approval:previous"',
        value: approvalView(accepted ? "superseded" : "validation_failed"),
      };
    });
    const repairPatch = vi.fn<PatchWorkflowApi["repairPatch"]>(async () => {
      accepted = true;
      return {
        accepted_schema_version: "run-accepted@1",
        events_url: `/api/v1/runs/${REPAIR_RUN_ID}/events`,
        run_id: REPAIR_RUN_ID,
        status_url: `/api/v1/runs/${REPAIR_RUN_ID}`,
      };
    });
    renderPage(
      api("validation_failed", {
        getApproval,
        getApprovalBinding,
        getArtifact,
        getPatch,
        getRun,
        repairPatch,
      }),
      `/patches/${encodeURIComponent(PATCH_ID)}?constraint=${encodeURIComponent(
        "artifact:constraint:active",
      )}`,
    );

    await user.selectOptions(await screen.findByLabelText("Repair policy"), "builtin.patch_repair@1");
    await user.click(screen.getByRole("button", { name: "Resolve 并启动 repair" }));

    expect(await screen.findByText("Repair Agent 正在运行")).toBeVisible();
    const replacementLink = await screen.findByRole("link", { name: "打开新 Patch revision" });
    expect(replacementLink).toHaveAttribute(
      "href",
      `/patches/${encodeURIComponent(REPAIRED_PATCH_ID)}?constraint=${encodeURIComponent(
        "artifact:constraint:active",
      )}&config=${encodeURIComponent(REPAIRED_CONFIG_ID)}&config=${encodeURIComponent(REPAIRED_CONFIG_2_ID)}`,
    );
    expect(screen.getByText("Repair RunResult 已验证")).toBeVisible();
    expect(getArtifact).toHaveBeenCalledWith(REPAIR_RESULT_ID);
    expect(getPatch).toHaveBeenCalledWith(REPAIRED_PATCH_ID);
  });

  it.each([
    ["run_id", { run_id: "run:other" }],
    ["run_kind", { run_kind: { kind: "generation.propose", version: 1 } }],
    ["outcome", { outcome_code: "repair_unverified" }],
    [
      "primary kind",
      {
        summary: {
          finding_count: 0,
          outcome_code: "repair_verified",
          primary_artifact_kind: "ir_snapshot",
          produced_artifact_count: 1,
          summary_schema_version: "run-result-summary@1",
        },
      },
    ],
    ["produced IDs", { produced_artifact_ids: ["artifact:patch:other"] }],
  ])("fails closed when the Repair RunResult has a mismatched %s", async (_label, payloadOverrides) => {
    const user = userEvent.setup();
    const getArtifact = vi.fn<PatchWorkflowApi["getArtifact"]>(async (artifactId) =>
      artifactId === REPAIR_RESULT_ID ? repairResultArtifact(payloadOverrides) : evidenceSetArtifact(),
    );
    const getPatch = vi.fn<PatchWorkflowApi["getPatch"]>(async () => ({
      etag: '"patch:failed-closed"',
      value: patchView("validation_failed"),
    }));
    renderPage(
      api("validation_failed", {
        getArtifact,
        getPatch,
        getRun: vi.fn(async () => repairRun("succeeded")),
        repairPatch: vi.fn<PatchWorkflowApi["repairPatch"]>(async () => ({
          accepted_schema_version: "run-accepted@1",
          events_url: `/api/v1/runs/${REPAIR_RUN_ID}/events`,
          run_id: REPAIR_RUN_ID,
          status_url: `/api/v1/runs/${REPAIR_RUN_ID}`,
        })),
      }),
    );

    await user.selectOptions(await screen.findByLabelText("Repair policy"), "builtin.patch_repair@1");
    await user.click(screen.getByRole("button", { name: "Resolve 并启动 repair" }));

    expect(await screen.findByText("Repair 结果不可采信")).toBeVisible();
    expect(screen.queryByRole("link", { name: "打开新 Patch revision" })).not.toBeInTheDocument();
    expect(getPatch).not.toHaveBeenCalledWith(REPAIRED_PATCH_ID);
  });

  it.each([
    ["failed", "Repair Agent 执行失败"],
    ["cancelled", "Repair Agent 已取消"],
    ["timed_out", "Repair Agent 已超时"],
  ] as const)(
    "shows an honest %s Repair Run terminal state without reading a RunResult",
    async (status, title) => {
      const user = userEvent.setup();
      const getArtifact = vi.fn<PatchWorkflowApi["getArtifact"]>(async () => evidenceSetArtifact());
      renderPage(
        api("validation_failed", {
          getArtifact,
          getRun: vi.fn(async () => repairRun(status)),
          repairPatch: vi.fn<PatchWorkflowApi["repairPatch"]>(async () => ({
            accepted_schema_version: "run-accepted@1",
            events_url: `/api/v1/runs/${REPAIR_RUN_ID}/events`,
            run_id: REPAIR_RUN_ID,
            status_url: `/api/v1/runs/${REPAIR_RUN_ID}`,
          })),
        }),
      );

      await user.selectOptions(await screen.findByLabelText("Repair policy"), "builtin.patch_repair@1");
      await user.click(screen.getByRole("button", { name: "Resolve 并启动 repair" }));

      expect(await screen.findByText(title)).toBeVisible();
      expect(getArtifact).not.toHaveBeenCalledWith(REPAIR_RESULT_ID);
      expect(screen.queryByRole("link", { name: "打开新 Patch revision" })).not.toBeInTheDocument();
    },
  );

  it("accepts an omitted optional EvidenceSet reason_code from the canonical JSON wire", async () => {
    const artifact = evidenceSetArtifact({
      requirements: [
        {
          applicability: "required",
          evidence_artifact_id: "artifact:checker:reward-cap",
          kind: "checker",
          requirement_id: "finding:reward-cap",
          status: "failed",
          tool_version: "checker@1",
        },
      ],
    });
    renderPage(
      api("validation_failed", {
        getArtifact: vi.fn(async () => artifact),
      }),
    );

    expect(await screen.findByRole("list", { name: "Repair Findings" })).toBeVisible();
    expect(
      screen.queryByText("EvidenceSet 身份、目标或 schema 不一致，页面已停止解释。"),
    ).not.toBeInTheDocument();
  });

  it.each([
    ["subject", { subject_artifact_id: "artifact:patch:stale" }],
    ["digest", { subject_digest: "0".repeat(64) }],
    ["validation Run", { validation_run_id: "" }],
    [
      "target",
      {
        target_binding: {
          ...approvalView("validation_failed").approval.target_binding,
          target_artifact_id: "artifact:spec:stale-preview",
        },
      },
    ],
  ])("fails closed when the EvidenceSet has a stale %s binding", async (_label, payloadOverrides) => {
    const user = userEvent.setup();
    const getArtifact = vi.fn<PatchWorkflowApi["getArtifact"]>(async (artifactId) =>
      artifactId === "artifact:evidence:patch" ? evidenceSetArtifact(payloadOverrides) : runFailureArtifact(),
    );
    renderPage(api("validation_failed", { getArtifact }));

    expect(await screen.findByText("EvidenceSet 身份、目标或 schema 不一致，页面已停止解释。")).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Repair policy"), "builtin.patch_repair@1");
    expect(screen.getByRole("button", { name: "Resolve 并启动 repair" })).toBeDisabled();
  });

  it("fails closed when an EvidenceSet Finding binding is malformed", async () => {
    const user = userEvent.setup();
    const malformed = [{ ...REPAIR_FINDINGS[0], finding_digest: "not-a-digest" }];
    const getArtifact = vi.fn<PatchWorkflowApi["getArtifact"]>(async (artifactId) =>
      artifactId === "artifact:evidence:patch"
        ? evidenceSetArtifact({ finding_bindings: malformed })
        : runFailureArtifact(),
    );
    renderPage(api("validation_failed", { getArtifact }));

    expect(await screen.findByText("EvidenceSet 身份、目标或 schema 不一致，页面已停止解释。")).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Repair policy"), "builtin.patch_repair@1");
    expect(screen.getByRole("button", { name: "Resolve 并启动 repair" })).toBeDisabled();
  });

  it("fails closed when a failed Approval also carries execution-terminal failure authority", async () => {
    const user = userEvent.setup();
    const staleApproval = approvalView("validation_failed");
    staleApproval.approval.last_validation_failure_artifact_id = "artifact:failure:patch";
    renderPage(
      api("validation_failed", {
        getApproval: vi.fn(async () => ({ etag: '"approval:stale-failure"', value: staleApproval })),
      }),
    );

    expect(await screen.findByText("EvidenceSet 身份、目标或 schema 不一致，页面已停止解释。")).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Repair policy"), "builtin.patch_repair@1");
    expect(screen.getByRole("button", { name: "Resolve 并启动 repair" })).toBeDisabled();
  });

  it("loads and explains a validated Patch's passed EvidenceSet without offering Repair", async () => {
    const passedEvidence = evidenceSetArtifact({
      overall_status: "passed",
      requirements: [
        {
          applicability: "required",
          evidence_artifact_id: "artifact:checker:reward-cap",
          kind: "checker",
          requirement_id: "checker:builtin.checker@1",
          status: "passed",
          tool_version: "checker@1",
        },
      ],
    });
    const getArtifact = vi.fn<PatchWorkflowApi["getArtifact"]>(async () => passedEvidence);
    renderPage(api("validated", { getArtifact }));

    expect(await screen.findByText("确定性结论：已通过")).toBeVisible();
    expect(screen.getByRole("link", { name: "打开 EvidenceSet" })).toHaveAttribute(
      "href",
      "/artifacts/artifact%3Aevidence%3Apatch",
    );
    expect(getArtifact).toHaveBeenCalledWith("artifact:evidence:patch");
    expect(screen.queryByRole("list", { name: "Repair Findings" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resolve 并启动 repair" })).toBeDisabled();
  });

  it("does not load EvidenceSet repair authority for a non-current failed subject", async () => {
    const getArtifact = vi.fn<PatchWorkflowApi["getArtifact"]>(async () => evidenceSetArtifact());
    renderPage(
      api("validation_failed", {
        getApprovalBinding: vi.fn(async () => ({
          ...binding("validation_failed"),
          is_current_head: false,
          subject_head_revision: 2,
        })),
        getArtifact,
      }),
    );

    await screen.findByRole("heading", { name: "Exact validation inputs" });
    expect(getArtifact).not.toHaveBeenCalled();
    expect(screen.queryByRole("list", { name: "Repair Findings" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resolve 并启动 repair" })).toBeDisabled();
  });

  it("retries an unknown repair outcome with the same resolved request and intent", async () => {
    const user = userEvent.setup();
    const accepted: RunAccepted = {
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run:repair/events",
      run_id: "run:repair",
      status_url: "/api/v1/runs/run:repair",
    };
    const repairPatch = vi
      .fn<PatchWorkflowApi["repairPatch"]>()
      .mockRejectedValueOnce(new Error("connection dropped"))
      .mockResolvedValueOnce(accepted);
    const executionOption: ExecutionOptionView = {
      cassette_artifact_id: null,
      domain_scope: { domain_ids: ["domain:economy"] },
      execution_version_plan: {
        agent_graph_version: "patch-repair@1",
        model_catalog_digest: "4".repeat(64),
        model_catalog_version: 1,
        nodes: [],
        plan_digest: "5".repeat(64),
        plan_schema_version: "execution-version-plan@1",
        routing_policy_digest: "6".repeat(64),
        routing_policy_version: 1,
      },
      llm_execution_mode: "record" as const,
      option_id: `execution-option:sha256:${"7".repeat(64)}`,
      option_schema_version: "execution-option@1" as const,
      prospective_request_hash: "8".repeat(64),
      resolved_profile_binding_digests: [],
      resolved_request_hash: "9".repeat(64),
      resource_operation_id: "repair_patch_api_v1_patches__artifact_id__repair_post",
      run_kind: { kind: "patch.repair", version: 1 },
      source_run_id: null,
    };
    const resolveExecutionOption = vi.fn<PatchWorkflowApi["resolveExecutionOption"]>(
      async () => executionOption,
    );
    renderPage(api("validation_failed", { repairPatch, resolveExecutionOption }));

    await screen.findByRole("heading", { name: "Exact validation inputs" });
    await user.selectOptions(screen.getByLabelText("Repair policy"), "builtin.patch_repair@1");
    await user.click(screen.getByRole("button", { name: "Resolve 并启动 repair" }));
    await user.click(await screen.findByRole("button", { name: "重试同一 intent" }));

    await waitFor(() => expect(repairPatch).toHaveBeenCalledTimes(2));
    expect(resolveExecutionOption).toHaveBeenCalledTimes(1);
    expect(repairPatch.mock.calls[1][0]).toBe(repairPatch.mock.calls[0][0]);
    expect(repairPatch.mock.calls[1][1]).toBe(repairPatch.mock.calls[0][1]);
  });

  it("selects a readable cassette-backed failed Run and submits its exact replay identity", async () => {
    const user = userEvent.setup();
    const sourceRun = replayRun("run:failed-repair-source-with-a-long-identity");
    const accepted: RunAccepted = {
      accepted_schema_version: "run-accepted@1",
      events_url: "/api/v1/runs/run:repair/events",
      run_id: "run:repair",
      status_url: "/api/v1/runs/run:repair",
    };
    const listReplaySourceRuns = vi.fn<PatchWorkflowApi["listReplaySourceRuns"]>(async (cursor) =>
      cursor === null
        ? page([], "read:replay-runs", "cursor:replay:2")
        : page([sourceRun], "read:replay-runs"),
    );
    const resolveExecutionOption = vi.fn<PatchWorkflowApi["resolveExecutionOption"]>(async (request) => ({
      cassette_artifact_id: sourceRun.terminal_cassette_artifact_id,
      domain_scope: { domain_ids: ["domain:economy"] },
      execution_version_plan: {
        agent_graph_version: "patch-repair@1",
        model_catalog_digest: "4".repeat(64),
        model_catalog_version: 1,
        nodes: [],
        plan_digest: "5".repeat(64),
        plan_schema_version: "execution-version-plan@1",
        routing_policy_digest: "6".repeat(64),
        routing_policy_version: 1,
      },
      llm_execution_mode: "replay",
      option_id: `execution-option:sha256:${"7".repeat(64)}`,
      option_schema_version: "execution-option@1",
      prospective_request_hash: "8".repeat(64),
      resolved_profile_binding_digests: [],
      resolved_request_hash: "9".repeat(64),
      resource_operation_id: request.resource_operation_id,
      run_kind: { kind: "patch.repair", version: 1 },
      source_run_id: sourceRun.run_id,
    }));
    const repairPatch = vi.fn<PatchWorkflowApi["repairPatch"]>(async () => accepted);
    renderPage(api("validation_failed", { listReplaySourceRuns, repairPatch, resolveExecutionOption }));

    await user.selectOptions(await screen.findByLabelText("Repair policy"), "builtin.patch_repair@1");
    await user.selectOptions(screen.getByLabelText("Repair LLM mode"), "replay");
    const sourceSelect = screen.getByRole("combobox", { name: "Replay source Run" });
    expect(sourceSelect).toHaveTextContent("失败");
    expect(sourceSelect).toHaveTextContent("第 2 次执行");
    expect(sourceSelect).not.toHaveTextContent(sourceRun.run_id);
    await user.selectOptions(sourceSelect, sourceRun.run_id);
    await user.click(screen.getByRole("button", { name: "Resolve 并启动 repair" }));

    await waitFor(() => expect(resolveExecutionOption).toHaveBeenCalledTimes(1));
    expect(resolveExecutionOption.mock.calls[0][0]).toMatchObject({
      llm_execution_mode: "replay",
      replay_source_run_id: sourceRun.run_id,
    });
    expect(repairPatch).toHaveBeenCalledWith(
      expect.objectContaining({ cassette_artifact_id: sourceRun.terminal_cassette_artifact_id }),
      expect.anything(),
    );
    expect(listReplaySourceRuns.mock.calls.map(([cursor]) => cursor)).toEqual([null, "cursor:replay:2"]);
  });

  it("explains that replay repair is unavailable when no cassette-backed Run exists", async () => {
    const user = userEvent.setup();
    renderPage(api("validation_failed"));

    await user.selectOptions(await screen.findByLabelText("Repair policy"), "builtin.patch_repair@1");
    await user.selectOptions(screen.getByLabelText("Repair LLM mode"), "replay");

    expect(screen.getByRole("combobox", { name: "Replay source Run" })).toBeDisabled();
    expect(screen.getByText("没有可回放运行。请先完成一次 record 或 live 运行。")).toBeVisible();
    expect(screen.getByRole("button", { name: "Resolve 并启动 repair" })).toBeDisabled();
  });

  it("applies only the server-frozen target binding after explicit confirmation", async () => {
    const user = userEvent.setup();
    let applied = false;
    const currentStatus = (): ApprovalStatus => (applied ? "applied" : "approved");
    const applyPatch = vi.fn<PatchWorkflowApi["applyPatch"]>(async () => {
      applied = true;
      return {
        approval: approvalView("applied"),
        ref_name: REF_NAME,
        ref_transition_id: null,
        ref_value: { artifact_id: PREVIEW_ID, revision: 2 },
        result_schema_version: "workflow-apply-result@1" as const,
        reversed_approval_id: null,
      };
    });
    renderPage(
      api("approved", {
        applyPatch,
        getApproval: vi.fn(async () => ({ etag: '"approval:apply"', value: approvalView(currentStatus()) })),
        getApprovalBinding: vi.fn(async () => binding(currentStatus())),
        getPatch: vi.fn(async () => ({ etag: '"patch:apply"', value: patchView(currentStatus()) })),
        getSpec: vi.fn<PatchWorkflowApi["getSpec"]>(async (artifactId) => ({
          artifact: summary(
            artifactId,
            "ir_snapshot",
            "ir-core@1",
            artifactId === PREVIEW_ID ? PREVIEW_SNAPSHOT : BASE_SNAPSHOT,
          ),
          ref_name: REF_NAME,
          ref_value: {
            artifact_id: artifactId,
            revision: artifactId === PREVIEW_ID ? 2 : 1,
          },
          schema_registry_version: "ir-core@1",
          snapshot_id: artifactId === PREVIEW_ID ? PREVIEW_SNAPSHOT : BASE_SNAPSHOT,
          view_schema_version: "spec-view@1",
        })),
        listRefHistory: vi.fn(async () =>
          page(
            [
              {
                entry_schema_version: "ref-history-entry@1" as const,
                ref_name: REF_NAME,
                value: { artifact_id: BASE_ID, revision: 1 },
              },
              ...(applied
                ? [
                    {
                      entry_schema_version: "ref-history-entry@1" as const,
                      ref_name: REF_NAME,
                      value: { artifact_id: PREVIEW_ID, revision: 2 },
                    },
                  ]
                : []),
            ],
            applied ? "read:history:after" : "read:history:before",
          ),
        ),
      }),
    );

    await user.click(await screen.findByRole("button", { name: "Apply approved Patch" }));
    await user.click(screen.getByRole("button", { name: "确认 Apply" }));

    await waitFor(() => expect(applyPatch).toHaveBeenCalledTimes(1));
    expect(applyPatch.mock.calls[0][1]).toEqual({
      approval_id: APPROVAL_ID,
      expected_ref: { artifact_id: BASE_ID, revision: 1 },
      expected_workflow_revision: 3,
      ref_name: REF_NAME,
      request_schema_version: "workflow-apply-request@1",
      subject_digest: SUBJECT_DIGEST,
      target_artifact_id: PREVIEW_ID,
      target_digest: TARGET_DIGEST,
    });
    const receiptHeading = await screen.findByRole("heading", {
      name: "Patch 已通过 ref transition 应用",
    });
    expect(receiptHeading.closest('[role="status"]')).not.toBeNull();
    expect(screen.getByRole("heading", { name: "Submit / approval / apply" })).toHaveFocus();
  });
});
