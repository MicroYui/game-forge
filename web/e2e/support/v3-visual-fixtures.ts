import { expect, type Locator, type Page, type Route } from "@playwright/test";

import canonicalBenchReport from "../../../scenarios/bench/bench-report.json" with { type: "json" };
import type { components } from "../../src/api/generated/openapi";

export const FROZEN_VISUAL_TIME = "2026-07-20T08:00:00.000Z";

export const V3_VISUAL_FIXTURE_AUTHORITY = "non-authoritative" as const;

type ApprovalView = components["schemas"]["ApprovalViewV1"];
type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];
type BenchReport = components["schemas"]["BenchReport"];
type ConstraintProposal = components["schemas"]["ConstraintProposalReadViewV1"];
type ConstraintSnapshot = components["schemas"]["ConstraintSnapshotViewV1"];
type ExecutionProfile = components["schemas"]["ExecutionProfileViewV1"];
type LogPage = components["schemas"]["LogPageV1"];
type MetricDescriptorRegistry = components["schemas"]["MetricDescriptorRegistryV1"];
type MetricPage = components["schemas"]["MetricPageV1"];
type PatchArtifact = components["schemas"]["PatchArtifactReadViewV1"];
type Principal = components["schemas"]["Principal"];
type ReviewArtifact = components["schemas"]["ReviewArtifactViewV1"];
type RollbackRequest = components["schemas"]["RollbackRequestReadViewV1"];
type RunCost = components["schemas"]["RunCostViewV2"];
type RunView = components["schemas"]["RunViewV1"];
type SpecView = components["schemas"]["SpecViewV1"];
type TaskSuite = components["schemas"]["TaskSuiteArtifactViewV1"];
type TaskSuiteDerivationBinding = components["schemas"]["TaskSuiteDerivationBindingViewV1"];
type TraceSummaryPage = components["schemas"]["TraceSummaryPageV1"];

const HASH = {
  approvalPolicy: "1".repeat(64),
  artifact: "2".repeat(64),
  constraint: "3".repeat(64),
  domainRegistry: "4".repeat(64),
  metricDescriptor: "5".repeat(64),
  metricRegistry: "6".repeat(64),
  profile: "7".repeat(64),
  resetBridge: "8".repeat(64),
  resetSignal: "9".repeat(64),
  rolePolicy: "a".repeat(64),
  routePolicy: "b".repeat(64),
  subject: "c".repeat(64),
  target: "d".repeat(64),
} as const;

const domainNarrative = { domain_ids: ["domain:narrative"] };
const domainEconomy = { domain_ids: ["domain:economy"] };

function artifact(
  artifactId: string,
  kind: ArtifactSummary["kind"],
  payloadSchemaId: string,
  versionTuple: ArtifactSummary["version_tuple"] = {},
  parentArtifactIds: string[] = [],
  domainScope: ArtifactSummary["domain_scope"] = domainNarrative,
): ArtifactSummary {
  return {
    artifact_id: artifactId,
    created_at: FROZEN_VISUAL_TIME,
    domain_scope: domainScope,
    kind,
    lineage_schema_version: "lineage@2",
    parent_artifact_ids: [...parentArtifactIds].sort(),
    payload_hash: HASH.artifact,
    payload_schema_id: payloadSchemaId,
    summary_schema_version: "artifact-summary@1",
    version_tuple: versionTuple,
  };
}

function opaquePage<T>(items: T[], readSnapshotId: string) {
  return {
    expires_at: "2026-07-21T08:00:00.000Z",
    items,
    next_cursor: null,
    page_schema_version: "page@1" as const,
    read_snapshot_id: readSnapshotId,
  };
}

const principal = {
  authz_revision: 3,
  credential_epoch: 1,
  display_name: "林澄",
  id: "principal:v3-visual-fixture",
  kind: "human",
  revision: 3,
  roles: [
    {
      assignment_id: "role:v3-visual-fixture",
      assignment_schema_version: "role-assignment@1",
      granted_at: FROZEN_VISUAL_TIME,
      granted_by: { principal_id: "system:bootstrap", principal_kind: "system" },
      principal_id: "principal:v3-visual-fixture",
      revision: 1,
      role: "tooling",
      scope: "all",
      status: "active",
    },
  ],
  status: "active",
} satisfies Principal;

const specArtifact = artifact(
  "artifact:spec:v3",
  "ir_snapshot",
  "ir-core@1",
  { ir_snapshot_id: "snapshot:v3", tool_version: "ingest@3" },
  ["artifact:source:v3"],
);

const spec = {
  artifact: specArtifact,
  ref_name: "refs/specs/frontier",
  ref_value: { artifact_id: specArtifact.artifact_id, revision: 7 },
  schema_registry_version: "registry@3",
  snapshot_id: "snapshot:v3",
  view_schema_version: "spec-view@1",
} satisfies SpecView;

const constraintSnapshot = {
  artifact: artifact("artifact:constraint:v3", "constraint_snapshot", "constraint-snapshot@1", {
    constraint_snapshot_id: "constraint:v3",
    tool_version: "compile@3",
  }),
  constraints: [],
  dsl_grammar_version: "dsl@1",
  view_schema_version: "constraint-snapshot-view@1",
} satisfies ConstraintSnapshot;

const constraintProposal = {
  approval_status: "pending_approval",
  artifact: artifact("artifact:proposal:v3", "constraint_proposal", "constraint-proposal@1", {
    tool_version: "constraint-extraction@4",
  }),
  proposal: {
    base_constraint_snapshot_id: constraintSnapshot.artifact.artifact_id,
    constraints: [],
    domain_scope: domainEconomy,
    dsl_grammar_version: "dsl@1",
    produced_by: "agent",
    producer_run_id: "run:constraint:v3",
    proposal_schema_version: "constraint-proposal@1",
    rationale: "Extract deterministic economy limits.",
    revision: 3,
    source_bindings: [],
    supersedes_artifact_id: null,
  },
  view_schema_version: "constraint-proposal-read-view@1",
  workflow_revision: 5,
} satisfies ConstraintProposal;

function executionProfile(
  profileId: string,
  version: number,
  profileKind: ExecutionProfile["profile_kind"],
  runKind: string,
  options: {
    envContractVersion?: string | null;
    status?: ExecutionProfile["status"];
    stochastic?: boolean;
    targetEnvironment?: ExecutionProfile["target_environment_profile"];
  } = {},
): ExecutionProfile {
  return {
    compatible_run_kinds: [{ kind: runKind, version: 1 }],
    display_name: profileId,
    domain_scope: domainNarrative,
    env_contract_version: options.envContractVersion ?? null,
    input_schema_ids: [],
    output_schema_ids: [],
    profile: { profile_id: profileId, version },
    profile_kind: profileKind,
    profile_payload_hash: HASH.profile,
    required_capabilities: [],
    status: options.status ?? "active",
    stochastic: options.stochastic ?? false,
    target_environment_profile: options.targetEnvironment ?? null,
  };
}

const environmentProfileRef = { profile_id: "builtin.aureus_env", version: 1 } as const;
const derivationProfileRef = { profile_id: "builtin.task_suite_derivation", version: 2 } as const;

const executionProfiles = [
  executionProfile("builtin.generation", 1, "generation", "generation.propose", {
    stochastic: true,
  }),
  executionProfile("builtin.aureus_env", 1, "environment", "playtest.run", {
    envContractVersion: "aureus-env@1",
  }),
  executionProfile("builtin.aureus_csv", 1, "config_export", "generation.propose", {
    envContractVersion: "aureus-env@1",
    targetEnvironment: environmentProfileRef,
  }),
  executionProfile(
    "builtin.constraint_extraction",
    1,
    "constraint_extraction",
    "constraint_proposal.propose",
    { stochastic: true },
  ),
  executionProfile("builtin.task_suite_derivation", 2, "task_suite_derivation", "task_suite.derive", {
    targetEnvironment: environmentProfileRef,
  }),
  executionProfile("builtin.playtest_planner", 2, "playtest_planner", "playtest.run", {
    stochastic: true,
  }),
] satisfies ExecutionProfile[];

const review = {
  artifact: artifact(
    "artifact:review:v3",
    "review_report",
    "review@1",
    { ir_snapshot_id: "snapshot:v3", tool_version: "checker-suite@3" },
    [specArtifact.artifact_id, constraintSnapshot.artifact.artifact_id],
  ),
  report: {
    by_defect_class: [{ count: 1, defect_class: "quest_dead_end", severity: "critical" }],
    deterministic_findings: [
      {
        defect_class: "quest_dead_end",
        finding_schema_version: "finding@1",
        id: "finding:v3:dead-end",
        message: "前哨任务在当前前置图中存在不可达步骤。",
        oracle_type: "deterministic",
        producer_id: "checker:graph",
        producer_run_id: "run:review:v3",
        severity: "critical",
        snapshot_id: "snapshot:v3",
        source: "checker",
        status: "confirmed",
      },
    ],
    llm_assisted_findings: [],
    review_schema_version: "review@1",
    simulation_findings: [],
    snapshot_id: "snapshot:v3",
    unproven_findings: [],
  },
  view_schema_version: "review-artifact-view@1",
} satisfies ReviewArtifact;

const taskEpisodes: TaskSuite["task_suite"]["episodes"] = [
  {
    completion_oracle: {
      oracle_id: "quest-completed",
      params: { quest_id: "quest:bridge" },
      params_schema_id: "aureus-quest-oracle-params@1",
      version: 1,
    },
    domain_scope: domainNarrative,
    episode_id: "episode:bridge",
    reset_binding: {
      payload: { quest_id: "quest:bridge" },
      payload_hash: HASH.resetBridge,
      reset_schema_id: "aureus-reset@1",
    },
    scenario_spec_artifact_id: "artifact:scenario:bridge",
    step_budget: 7,
  },
  {
    completion_oracle: {
      oracle_id: "quest-completed",
      params: { quest_id: "quest:signal" },
      params_schema_id: "aureus-quest-oracle-params@1",
      version: 1,
    },
    domain_scope: domainNarrative,
    episode_id: "episode:signal",
    reset_binding: {
      payload: { quest_id: "quest:signal" },
      payload_hash: HASH.resetSignal,
      reset_schema_id: "aureus-reset@1",
    },
    scenario_spec_artifact_id: "artifact:scenario:signal",
    step_budget: 11,
  },
];

const taskSuite = {
  artifact: artifact(
    "artifact:suite:v3",
    "task_suite",
    "task-suite@1",
    {
      constraint_snapshot_id: "constraint:v3",
      env_contract_version: "aureus-env@1",
      ir_snapshot_id: "snapshot:v3",
      tool_version: "task-suite@2",
    },
    [
      "artifact:preview:v3",
      "artifact:config:v3",
      constraintSnapshot.artifact.artifact_id,
      ...taskEpisodes.map((episode) => episode.scenario_spec_artifact_id),
    ],
  ),
  task_suite: {
    completion_oracle_registry_ref: { digest: HASH.constraint, registry_version: 1 },
    config_export_artifact_id: "artifact:config:v3",
    constraint_snapshot_artifact_id: constraintSnapshot.artifact.artifact_id,
    env_contract_version: "aureus-env@1",
    environment_profile: environmentProfileRef,
    episodes: taskEpisodes,
    source_preview_artifact_id: "artifact:preview:v3",
    suite_profile: derivationProfileRef,
    task_suite_schema_version: "task-suite@1",
  },
  view_schema_version: "task-suite-artifact-view@1",
} satisfies TaskSuite;

const derivationBinding = {
  binding_schema_version: "task-suite-derivation-binding@1",
  completion_oracle_registry_ref: taskSuite.task_suite.completion_oracle_registry_ref,
  derivation_profile: derivationProfileRef,
  max_scenarios: 1024,
  max_total_prepared_artifact_bytes: 268_435_456,
  profile_payload_hash: HASH.profile,
  run_kind: { kind: "task_suite.derive", version: 1 },
  target_environment_profile: environmentProfileRef,
} satisfies TaskSuiteDerivationBinding;

const patchArtifact = {
  approval_status: "validated",
  artifact: artifact(
    "artifact:patch:v3",
    "patch",
    "patch@2",
    { ir_snapshot_id: "snapshot:preview:v3" },
    ["artifact:base:v3", "artifact:preview:v3"],
    domainEconomy,
  ),
  patch: {
    base_snapshot_id: "snapshot:base:v3",
    expected_to_fix: ["finding:economy:v3"],
    ops: [],
    patch_schema_version: "patch@2",
    preconditions: [],
    produced_by: "human",
    producer_run_id: null,
    rationale: "降低新手奖励，保持经济回收口稳定。",
    revision: 3,
    side_effect_risk: "low",
    supersedes_artifact_id: "artifact:patch:v3:2",
    target_snapshot_id: "snapshot:preview:v3",
  },
  regression_status: "passed",
  validation_status: "passed",
  view_schema_version: "patch-artifact-read-view@1",
  workflow_revision: 7,
} satisfies PatchArtifact;

const rollbackRequest = {
  approval_status: "pending_approval",
  artifact: artifact(
    "artifact:rollback:v3",
    "rollback_request",
    "rollback-request@1",
    {},
    ["artifact:head:v3"],
    domainEconomy,
  ),
  request: {
    expected_current_ref: { artifact_id: "artifact:head:v3", revision: 4 },
    reason: "恢复最后一次已批准的经济快照。",
    ref_name: "refs/design/live",
    reverses_approval_id: "approval:patch:v3",
    rollback_profile_binding: {
      catalog_digest: HASH.constraint,
      catalog_version: 1,
      expected_profile_kind: "rollback",
      field_path: "rollback_profile",
      profile: { profile_id: "builtin.rollback", version: 1 },
      profile_payload_hash: HASH.profile,
    },
    rollback_schema_version: "rollback-request@1",
    target_artifact_id: "artifact:base:v3",
    target_history_revision: 1,
  },
  view_schema_version: "rollback-request-read-view@1",
  workflow_revision: 5,
} satisfies RollbackRequest;

function decodeCanonicalFloats(value: unknown): unknown {
  if (typeof value === "string" && value.startsWith("f:")) return Number(value.slice(2));
  if (Array.isArray(value)) return value.map(decodeCanonicalFloats);
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, decodeCanonicalFloats(item)]));
  }
  return value;
}

const benchReport = decodeCanonicalFloats(structuredClone(canonicalBenchReport)) as BenchReport;

const run = {
  attempt_no: 2,
  events_url: "/api/v1/runs/run:v3/events",
  failure_artifact_id: null,
  result_artifact_id: "artifact:result:v3",
  revision: 5,
  run_id: "run:v3",
  status: "succeeded",
  status_url: "/api/v1/runs/run:v3",
  terminal_cassette_artifact_id: null,
  view_schema_version: "run-view@1",
} satisfies RunView;

const traceId = "1".repeat(32);
const tracePage = {
  coverage_end: "2026-07-20T08:00:00.000Z",
  coverage_start: "2026-07-20T07:00:00.000Z",
  items: [
    {
      duration_ns: 1_250_000_000,
      ended_at: "2026-07-20T07:10:01.250Z",
      root_span_id: "2".repeat(16),
      run_ids: [run.run_id],
      service_names: ["api", "worker"],
      span_count: 5,
      started_at: "2026-07-20T07:10:00.000Z",
      status: "ok",
      trace_id: traceId,
      trace_schema_version: "trace-summary@1",
      truncated: true,
    },
  ],
  next_cursor: null,
  page_schema_version: "trace-summary-page@1",
  truncated: true,
} satisfies TraceSummaryPage;

const logPage = {
  coverage_end: "2026-07-20T08:00:00.000Z",
  coverage_start: "2026-07-20T07:00:00.000Z",
  items: [
    {
      record: {
        event_name: "run.completed",
        fields: { outcome: "succeeded", rawResponse: "must never render" },
        level: "info",
        log_id: "log:v3",
        log_schema_version: "log-record@1",
        message: "Run completed after deterministic verification",
        run_id: run.run_id,
        service: "worker",
        trace_id: traceId,
        ts_utc: "2026-07-20T07:10:01.000Z",
      },
      redacted_fields: ["rawResponse"],
    },
  ],
  next_cursor: null,
  page_schema_version: "log-page@1",
  truncated: true,
} satisfies LogPage;

const metricDescriptors = {
  descriptors: [
    {
      descriptor_digest: HASH.metricDescriptor,
      descriptor_schema_version: "metric-descriptor@1",
      descriptor_version: 1,
      histogram_bucket_bounds: [],
      label_keys: ["method", "status_class"],
      metric_name: "gameforge.api.request.count",
      metric_type: "counter",
      series_limit: 16,
      unit: "request",
      unit_schema_version: "metric-units@1",
    },
  ],
  global_series_limit: 64,
  registry_digest: HASH.metricRegistry,
  registry_schema_version: "metric-descriptor-registry@1",
  registry_version: 1,
} satisfies MetricDescriptorRegistry;

const metricPage = {
  coverage_end: "2026-07-20T08:00:00.000Z",
  coverage_start: "2026-07-20T07:00:00.000Z",
  effective_resolution_s: 60,
  next_cursor: null,
  page_schema_version: "metric-page@1",
  series: [
    {
      descriptor: {
        descriptor_digest: HASH.metricDescriptor,
        descriptor_version: 1,
        metric_name: "gameforge.api.request.count",
      },
      labels: { method: "GET", status_class: "2xx" },
      metric_name: "gameforge.api.request.count",
      metric_type: "counter",
      scalar_points: [{ ts_utc: "2026-07-20T07:10:00.000Z", value: 3 }],
      unit: "request",
    },
  ],
  truncated: true,
} satisfies MetricPage;

const runCost = {
  budget_set: {
    budget_set_snapshot_id: "budget-set:run:v3",
    captured_at: "2026-07-20T07:00:00.000Z",
    run_id: run.run_id,
    selection_policy_version: "budget-selection@1",
    set_schema_version: "budget-set-snapshot@1",
    snapshots: [
      {
        budget_id: "budget:run:v3",
        budget_revision_at_freeze: 4,
        captured_at: "2026-07-20T07:00:00.000Z",
        consumed: [
          {
            amount_schema_version: "cost-amount@1",
            dimension: "request",
            unit: "request",
            value: "3",
          },
        ],
        limits: [
          {
            amount_schema_version: "cost-amount@1",
            dimension: "request",
            unit: "request",
            value: "10",
          },
        ],
        policy_version: "budget-policy@1",
        reserved: [],
        scope_id: run.run_id,
        scope_kind: "run",
        snapshot_id: "budget-snapshot:run:v3",
        snapshot_schema_version: "budget-snapshot@1",
      },
    ],
  },
  next_cursor: null,
  run_id: run.run_id,
  settlement_summary: {
    group_counts: [
      {
        count_schema_version: "cost-settlement-group-count@1",
        group_count: 1,
        scope: "attempt_call",
        status: "late_reconciled",
      },
    ],
    held_unknown_group_count: 0,
    late_adjustment_usage_count: 0,
    summary_schema_version: "cost-settlement-summary@1",
    total_group_count: 1,
    usage_entry_count: 1,
    usage_evidence_status: "recorded",
  },
  usage: [
    {
      adjustment_of_usage_id: null,
      attempt_no: 2,
      execution_source: "cassette_replay",
      latency: {
        observation_schema_version: "latency-observation@1",
        provider_latency_ms: 128,
        status: "reported",
      },
      monetary: {
        amount: "0.0012",
        currency: "USD",
        observation_schema_version: "monetary-observation@1",
        price_book_version: "prices@3",
        quote_effective_at: "2026-07-20T00:00:00.000Z",
        status: "reported",
      },
      provider_prefix_cache: {
        hit: true,
        observation_schema_version: "cache-hit-observation@1",
        status: "reported",
      },
      recorded_at: "2026-07-20T07:10:01.000Z",
      retry_index: 0,
      scope: "attempt_call",
      token_usage: {
        cache_read_tokens: 96,
        cache_write_tokens: 0,
        input_tokens: 240,
        observation_schema_version: "token-usage-observation@1",
        output_tokens: 48,
        status: "reported",
        total_tokens: 288,
      },
      transport_attempt: 1,
      usage_schema_version: "cost-usage-view@1",
      usage_id: "usage:v3",
      wall_time_ns: 165_000_000,
    },
  ],
  view_schema_version: "run-cost-view@2",
} satisfies RunCost;

const approval = {
  approval: {
    approval_id: "approval:multi-domain:v3",
    approval_policy: { policy_digest: HASH.approvalPolicy, policy_version: "approval-policy@1" },
    approval_schema_version: "approval@1",
    created_at: "2026-07-20T06:00:00.000Z",
    decisions: [
      {
        actor: { principal_id: "human:charlie", principal_kind: "human" },
        comment: "经济回归证据已复核。",
        decision: "approve",
        decision_id: "decision:v3:1",
        expected_workflow_revision: 11,
        occurred_at: "2026-07-20T06:30:00.000Z",
        reason_code: "evidence_reviewed",
        requirement_ids: ["requirement:economy"],
      },
    ],
    domain_registry_ref: {
      registry_digest: HASH.domainRegistry,
      registry_version: "domains@7",
    },
    domain_scope: { domain_ids: ["domain:economy", "domain:narrative"] },
    proposer: { principal_id: "human:alice", principal_kind: "human" },
    regression_evidence_artifact_ids: ["artifact:regression:v3"],
    requirements: [
      {
        assignee_principal_ids: ["human:bob"],
        distinct_from_requirement_ids: ["requirement:narrative"],
        domain_scope: domainEconomy,
        min_approvals: 2,
        required_permission: {
          action: "approve",
          domain_scope: domainEconomy,
          resource_kind: "patch",
        },
        requirement_id: "requirement:economy",
        route_role: "numeric_designer",
      },
      {
        assignee_principal_ids: [],
        distinct_from_requirement_ids: ["requirement:economy"],
        domain_scope: domainNarrative,
        min_approvals: 1,
        required_permission: {
          action: "approve",
          domain_scope: domainNarrative,
          resource_kind: "patch",
        },
        requirement_id: "requirement:narrative",
        route_role: "content_designer",
      },
    ],
    role_policy_digest: HASH.rolePolicy,
    role_policy_version: "roles@9",
    route_policy: {
      domain_registry_ref: {
        registry_digest: HASH.domainRegistry,
        registry_version: "domains@7",
      },
      route_digest: HASH.routePolicy,
      route_version: "routes@4",
    },
    status: "pending_approval",
    subject_artifact_id: patchArtifact.artifact.artifact_id,
    subject_digest: HASH.subject,
    subject_kind: "patch",
    subject_revision: 3,
    subject_series_id: "patch-series:v3",
    submitted_at: "2026-07-20T06:15:00.000Z",
    target_binding: {
      binding_schema_version: "approval-target-binding@1",
      expected_ref: { artifact_id: "artifact:snapshot:v2", revision: 6 },
      ref_name: "refs/design/live",
      subject_kind: "patch",
      target_artifact_id: "artifact:snapshot:v3",
      target_artifact_kind: "ir_snapshot",
      target_digest: HASH.target,
      target_snapshot_id: "snapshot:v3",
    },
    workflow_revision: 12,
  },
  current_actor_allowed_requirement_ids: ["requirement:economy", "requirement:narrative"],
  requirement_progress: [
    {
      decision_eligibility: [
        { decision: "approve", eligible: true, reason_codes: [] },
        { decision: "reject", eligible: true, reason_codes: [] },
        { decision: "request_changes", eligible: true, reason_codes: [] },
      ],
      domain_scope: domainEconomy,
      eligible_for_current_actor: true,
      min_approvals: 2,
      requirement_id: "requirement:economy",
      route_role: "numeric_designer",
      satisfied: false,
      unmet_distinct_from_requirement_ids: ["requirement:narrative"],
      valid_approval_count: 1,
    },
    {
      decision_eligibility: [
        {
          decision: "approve",
          eligible: false,
          reason_codes: ["distinct_requirement_conflict"],
        },
        { decision: "reject", eligible: true, reason_codes: [] },
        { decision: "request_changes", eligible: true, reason_codes: [] },
      ],
      domain_scope: domainNarrative,
      eligible_for_current_actor: true,
      min_approvals: 1,
      requirement_id: "requirement:narrative",
      route_role: "content_designer",
      satisfied: false,
      unmet_distinct_from_requirement_ids: ["requirement:economy"],
      valid_approval_count: 0,
    },
  ],
  view_schema_version: "approval-view@1",
} satisfies ApprovalView;

export const V3_VISUAL_PAGES = [
  {
    hero: ".gf-specs__hero",
    h1: "规格与约束快照",
    id: "specs",
    primaryRegion: ".gf-specs__workspace-grid",
    readyText: "artifact:proposal:v3",
    root: ".gf-page.gf-specs",
    route: "/specs",
  },
  {
    hero: ".gf-generation__hero",
    h1: "内容生成",
    id: "generation",
    primaryRegion: ".gf-generation__authoring-layout",
    readyText: "当前 exact 绑定",
    root: ".gf-page.gf-generation",
    route: "/generation",
  },
  {
    hero: ".gf-review__hero",
    h1: "审查报告",
    id: "reviews",
    primaryRegion: ".gf-review__index-panel",
    readyText: "artifact:review:v3",
    root: ".gf-page.gf-review",
    route: "/reviews",
  },
  {
    hero: ".gf-playtest__hero",
    h1: "自动试玩",
    id: "playtest",
    primaryRegion: ".gf-playtest__suite-ledger",
    readyText: "episode:bridge",
    root: ".gf-page.gf-playtest",
    route: "/playtest?suite=artifact%3Asuite%3Av3",
  },
  {
    hero: ".gf-patches__hero",
    h1: "Patch / Diff",
    id: "patches",
    primaryRegion: ".gf-patches__ledger",
    readyText: "artifact:patch:v3",
    root: ".gf-page.gf-patches",
    route: "/patches",
  },
  {
    hero: ".gf-eval__hero",
    h1: "Eval / Bench",
    id: "eval",
    primaryRegion: ".gf-eval__authority",
    readyText: "human evidence available",
    root: ".gf-page.gf-eval",
    route: "/eval",
  },
  {
    hero: ".gf-observability__hero",
    h1: "可观测性",
    id: "observability",
    primaryRegion: ".gf-cursor-table",
    readyText: "budget-set:run:v3",
    root: ".gf-page.gf-observability",
    route: "/observability?run=run%3Av3",
  },
  {
    hero: ".gf-approvals__hero",
    h1: "Approvals",
    id: "approvals",
    primaryRegion: ".gf-cursor-table",
    readyText: "approval:multi-domain:v3",
    root: ".gf-page.gf-approvals",
    route: "/approvals",
  },
] as const;

export const V3_VISUAL_VIEWPORTS = [
  { height: 900, id: "1440x900", width: 1440 },
  { height: 720, id: "1280x720", width: 1280 },
  { height: 844, id: "390x844", width: 390 },
  { height: 915, id: "412x915", width: 412 },
] as const;

export const V3_VISUAL_THEMES = ["light", "dark"] as const;

export const V3_STABLE_PAGES = V3_VISUAL_PAGES;
export const V3_VIEWPORTS = V3_VISUAL_VIEWPORTS;

export type V3VisualPage = (typeof V3_VISUAL_PAGES)[number];
export type V3VisualTheme = (typeof V3_VISUAL_THEMES)[number];
export type V3VisualViewport = (typeof V3_VISUAL_VIEWPORTS)[number];
export type V3VisualPageCase = V3VisualPage & {
  name: string;
  theme: V3VisualTheme;
  viewport: { height: number; width: number };
  viewportId: V3VisualViewport["id"];
};
export type V3StablePage = V3VisualPage;
export type V3PageCase = V3VisualPageCase;

export const V3_VISUAL_PAGE_CASES: readonly V3VisualPageCase[] = V3_VISUAL_PAGES.flatMap((page) =>
  V3_VISUAL_THEMES.flatMap((theme) =>
    V3_VISUAL_VIEWPORTS.map((viewport) => ({
      ...page,
      name: `v3-${page.id}-${theme}-${viewport.id}`,
      theme,
      viewport: { height: viewport.height, width: viewport.width },
      viewportId: viewport.id,
    })),
  ),
);

export const V3_PAGE_CASES = V3_VISUAL_PAGE_CASES;

if (V3_PAGE_CASES.length !== 64 || new Set(V3_PAGE_CASES.map((pageCase) => pageCase.name)).size !== 64) {
  throw new Error("The V3 stable visual matrix must contain 64 unique page/theme/viewport cases.");
}

export interface V3VisualFixtureBoundary {
  assertClean(): void;
}

interface FixtureResponse {
  body: unknown;
  headers?: Record<string, string>;
  status?: number;
}

function profilePage(url: URL): FixtureResponse {
  const requestedKind = url.searchParams.get("profile_kind");
  const requestedStatus = url.searchParams.get("status");
  const items = executionProfiles.filter(
    (profile) =>
      (requestedKind === null || profile.profile_kind === requestedKind) &&
      (requestedStatus === null || profile.status === requestedStatus),
  );
  return { body: opaquePage(items, `read:profiles:${requestedKind ?? "all"}`) };
}

function fixtureResponse(url: URL): FixtureResponse | null {
  const path = decodeURIComponent(url.pathname);
  switch (path) {
    case "/api/v1/auth/me":
      return { body: principal };
    case "/api/v1/specs":
      return { body: opaquePage([spec], "read:specs:v3") };
    case "/api/v1/constraints":
      return { body: opaquePage([constraintSnapshot], "read:constraints:v3") };
    case "/api/v1/constraint-proposals":
      return { body: opaquePage([constraintProposal], "read:constraint-proposals:v3") };
    case "/api/v1/execution-profiles":
      return profilePage(url);
    case "/api/v1/reviews":
      return { body: opaquePage([review], "read:reviews:v3") };
    case "/api/v1/task-suites":
      return { body: opaquePage([taskSuite], "read:task-suites:v3") };
    case "/api/v1/task-suites/artifact:suite:v3":
      return { body: taskSuite };
    case "/api/v1/execution-profiles/builtin.task_suite_derivation/versions/2/task-suite-derivation-binding":
      return { body: derivationBinding };
    case "/api/v1/patches":
      return { body: opaquePage([patchArtifact], "read:patches:v3") };
    case "/api/v1/rollback-requests":
      return { body: opaquePage([rollbackRequest], "read:rollback-requests:v3") };
    case "/api/v1/bench/report":
      return {
        body: benchReport,
        headers: {
          ETag: '"bench-report:v3"',
          "X-Artifact-ID": "artifact:bench-report:v3",
        },
      };
    case "/api/v1/runs":
      return { body: opaquePage([run], "read:runs:v3") };
    case "/api/v1/runs/run:v3":
      return { body: run };
    case "/api/v1/runs/run:v3/traces":
      return { body: tracePage };
    case "/api/v1/logs/query":
      return { body: logPage };
    case "/api/v1/metrics/descriptors":
      return { body: metricDescriptors };
    case "/api/v1/metrics/query":
      return { body: metricPage };
    case "/api/v1/cost/run:v3":
      return { body: runCost };
    case "/api/v1/approvals":
      return { body: opaquePage([approval], "read:approvals:v3") };
    default:
      return null;
  }
}

async function fulfillJson(route: Route, response: FixtureResponse) {
  await route.fulfill({
    body: JSON.stringify(response.body),
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "application/json; charset=utf-8",
      "X-GameForge-Fixture-Authority": V3_VISUAL_FIXTURE_AUTHORITY,
      ...response.headers,
    },
    status: response.status ?? 200,
  });
}

export async function installV3VisualBoundary(
  page: Page,
  baseURL: string | undefined,
  theme?: V3VisualTheme,
): Promise<V3VisualFixtureBoundary> {
  const expectedUrl = new URL(baseURL ?? "https://127.0.0.1:4173");
  const expectedOrigin = expectedUrl.origin;
  const expectedHost = expectedUrl.host;
  const externalEgress = new Set<string>();
  const unhandledProductApi = new Set<string>();

  await page.clock.setFixedTime(new Date(FROZEN_VISUAL_TIME));
  if (theme !== undefined) {
    await page.addInitScript((selectedTheme) => {
      window.localStorage.setItem("gameforge.theme", selectedTheme);
    }, theme);
  }

  await page.routeWebSocket(/.*/, async (webSocketRoute) => {
    const url = new URL(webSocketRoute.url());
    if (url.host !== expectedHost) {
      externalEgress.add(webSocketRoute.url());
      await webSocketRoute.close({ code: 1008, reason: "External egress is disabled." });
      return;
    }
    webSocketRoute.connectToServer();
  });

  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if ((url.protocol === "http:" || url.protocol === "https:") && url.origin !== expectedOrigin) {
      externalEgress.add(`${request.method()} ${url.href}`);
      await route.abort("blockedbyclient");
      return;
    }

    if (url.origin === expectedOrigin && url.pathname.startsWith("/api/v1/")) {
      if (request.method() !== "GET") {
        unhandledProductApi.add(`${request.method()} ${url.pathname}${url.search}`);
        await route.abort("blockedbyclient");
        return;
      }
      const response = fixtureResponse(url);
      if (response === null) {
        unhandledProductApi.add(`${request.method()} ${url.pathname}${url.search}`);
        await route.abort("blockedbyclient");
        return;
      }
      await fulfillJson(route, response);
      return;
    }

    await route.continue();
  });

  return {
    assertClean() {
      expect(
        [...unhandledProductApi],
        "Every product API read must be declared by the finite V3 visual fixture boundary.",
      ).toEqual([]);
      expect([...externalEgress], "Browser external egress must remain fail-closed.").toEqual([]);
    },
  };
}

async function doubleAnimationFrame(page: Page) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    });
  });
}

export async function settleV3Page(page: Page, pageCase: V3VisualPageCase) {
  await expect(page.getByRole("heading", { level: 1, name: pageCase.h1 })).toBeVisible();
  await expect(page.getByText(pageCase.readyText, { exact: false }).first()).toBeVisible();
  const root = page.locator(pageCase.root).first();
  await expect(root).toBeVisible();

  await page.evaluate(() => {
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
    window.scrollTo(0, 0);
  });
  await doubleAnimationFrame(page);
}

async function expectContained(locator: Locator, container: Locator, label: string) {
  const box = await locator.boundingBox();
  const containerBox = await container.boundingBox();
  expect(box, `${label} must be visible`).not.toBeNull();
  expect(containerBox, `${label} container must be visible`).not.toBeNull();
  expect(box!.x, `${label} must stay inside its container`).toBeGreaterThanOrEqual(containerBox!.x - 1);
  expect(box!.y, `${label} must stay inside its container`).toBeGreaterThanOrEqual(containerBox!.y - 1);
  expect(box!.x + box!.width, `${label} must stay inside its container`).toBeLessThanOrEqual(
    containerBox!.x + containerBox!.width + 1,
  );
  expect(box!.y + box!.height, `${label} must stay inside its container`).toBeLessThanOrEqual(
    containerBox!.y + containerBox!.height + 1,
  );
}

async function expectPairwiseNonOverlapping(locator: Locator, label: string) {
  const boxes = (await locator.all()).map(async (item) => item.boundingBox());
  const resolved = (await Promise.all(boxes)).filter((box) => box !== null);
  for (let leftIndex = 0; leftIndex < resolved.length; leftIndex += 1) {
    for (let rightIndex = leftIndex + 1; rightIndex < resolved.length; rightIndex += 1) {
      const left = resolved[leftIndex]!;
      const right = resolved[rightIndex]!;
      const overlapsHorizontally = left.x < right.x + right.width - 1 && right.x < left.x + left.width - 1;
      const overlapsVertically = left.y < right.y + right.height - 1 && right.y < left.y + left.height - 1;
      expect(
        overlapsHorizontally && overlapsVertically,
        `${label} items ${leftIndex + 1} and ${rightIndex + 1} must not overlap`,
      ).toBe(false);
    }
  }
}

async function expectCursorScroller(root: Locator, index = 0) {
  const table = root.locator(".gf-cursor-table").nth(index);
  const toolbar = table.locator(".gf-cursor-table__toolbar");
  const scroller = table.locator(".gf-cursor-table__scroll");
  const pagination = table.locator(".gf-cursor-table__pagination");
  await expect(scroller).toHaveAttribute("tabindex", "0");
  await expectContained(scroller, table, `Cursor table ${index + 1} scroller`);
  const toolbarBox = await toolbar.boundingBox();
  const scrollBox = await scroller.boundingBox();
  const paginationBox = await pagination.boundingBox();
  expect(toolbarBox).not.toBeNull();
  expect(scrollBox).not.toBeNull();
  expect(paginationBox).not.toBeNull();
  expect(scrollBox!.y).toBeGreaterThanOrEqual(toolbarBox!.y + toolbarBox!.height - 1);
  expect(paginationBox!.y).toBeGreaterThanOrEqual(scrollBox!.y + scrollBox!.height - 1);
}

async function expectAllCursorTables(root: Locator) {
  const count = await root.locator(".gf-cursor-table").count();
  for (let index = 0; index < count; index += 1) await expectCursorScroller(root, index);
}

async function assertShellGeometry(page: Page) {
  const viewport = page.viewportSize();
  if (viewport === null) throw new Error("V3 visual checks require a fixed viewport.");
  for (const [label, selector] of [
    ["Brand", ".gf-brand"],
    ["Top bar", ".gf-topbar"],
    ["Main content", "#main-content"],
  ] as const) {
    const box = await page.locator(selector).boundingBox();
    expect(box, `${label} must be visible`).not.toBeNull();
    expect(box!.x, `${label} must stay inside the viewport`).toBeGreaterThanOrEqual(-1);
    expect(box!.x + box!.width, `${label} must stay inside the viewport`).toBeLessThanOrEqual(
      viewport.width + 1,
    );
  }
  await expectPairwiseNonOverlapping(
    page.locator(".gf-breadcrumbs, .gf-identity-bar"),
    "Top-bar breadcrumb and identity regions",
  );
  for (const button of await page.locator(".gf-topbar .gf-icon-button").all()) {
    const box = await button.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.width, "Top-bar icon buttons must keep a 36px target").toBeGreaterThanOrEqual(36);
    expect(box!.height, "Top-bar icon buttons must keep a 36px target").toBeGreaterThanOrEqual(36);
  }
}

async function assertRouteSpecificGeometry(root: Locator, pageCase: V3VisualPageCase) {
  switch (pageCase.id) {
    case "specs":
      await expectPairwiseNonOverlapping(root.locator(".gf-specs__entry-card"), "Spec entry cards");
      break;
    case "generation": {
      const layout = root.locator(".gf-generation__authoring-layout");
      const authoring = root.locator(".gf-generation__authoring");
      const ledger = root.locator(".gf-generation__authority-ledger");
      await expectContained(authoring, layout, "Generation authoring panel");
      await expectContained(ledger, layout, "Generation authority ledger");
      await expectPairwiseNonOverlapping(
        root.locator(".gf-generation__authoring, .gf-generation__authority-ledger"),
        "Generation authoring columns",
      );
      break;
    }
    case "reviews":
      break;
    case "playtest":
      await expectPairwiseNonOverlapping(root.locator(".gf-playtest__suite-card"), "TaskSuite cards");
      break;
    case "patches":
      await expectPairwiseNonOverlapping(root.locator(".gf-patches__ledger"), "Patch ledgers");
      break;
    case "eval":
      await expectPairwiseNonOverlapping(root.locator(".gf-eval__summary-card"), "Eval summary cards");
      await expect(root.locator(".gf-eval__table-scroll").first()).toHaveAttribute("tabindex", "0");
      break;
    case "observability":
      await expectPairwiseNonOverlapping(
        root.locator(".gf-observability__metric-controls > *"),
        "Metric controls",
      );
      await expect(root.locator(".gf-observability__terminal-id").first()).toHaveAttribute("tabindex", "0");
      break;
    case "approvals": {
      const primaryCell = root.locator(".gf-approvals__table-primary").first();
      const primaryBox = await primaryCell.boundingBox();
      expect(primaryBox).not.toBeNull();
      expect(primaryBox!.width, "Approval primary identity column must remain usable").toBeGreaterThanOrEqual(
        pageCase.viewport.width <= 412 ? 144 : 160,
      );
      await expect(primaryCell.locator(".gf-copyable__button").first()).toBeVisible();
      break;
    }
  }
}

export async function assertV3PageGeometry(page: Page, pageCase: V3VisualPageCase) {
  await assertShellGeometry(page);
  const root = page.locator(pageCase.root).first();
  const hero = root.locator(pageCase.hero).first();
  const primaryRegion = root.locator(pageCase.primaryRegion).first();
  const first = await root.boundingBox();
  await doubleAnimationFrame(page);
  const second = await root.boundingBox();
  const heroBox = await hero.boundingBox();
  const primaryBox = await primaryRegion.boundingBox();

  expect(first).not.toBeNull();
  expect(second).not.toBeNull();
  expect(heroBox).not.toBeNull();
  expect(primaryBox).not.toBeNull();
  expect(second!.x).toBeCloseTo(first!.x, 1);
  expect(second!.y).toBeCloseTo(first!.y, 1);
  expect(second!.width).toBeCloseTo(first!.width, 1);
  expect(second!.height).toBeCloseTo(first!.height, 1);

  for (const [label, box] of [
    ["hero", heroBox!],
    ["primary region", primaryBox!],
  ] as const) {
    expect(box.width, `${pageCase.id} ${label} must have usable width`).toBeGreaterThan(0);
    expect(box.x, `${pageCase.id} ${label} must stay inside the page root`).toBeGreaterThanOrEqual(
      second!.x - 1,
    );
    expect(box.x + box.width, `${pageCase.id} ${label} must stay inside the page root`).toBeLessThanOrEqual(
      second!.x + second!.width + 1,
    );
  }
  expect(
    primaryBox!.y,
    `${pageCase.id} primary region must not overlap the editorial hero`,
  ).toBeGreaterThanOrEqual(heroBox!.y + heroBox!.height - 1);

  const unboundedRegions = await primaryRegion.evaluateAll((elements) =>
    elements.flatMap((element) => {
      const style = getComputedStyle(element);
      const allowsHorizontalScroll = style.overflowX === "auto" || style.overflowX === "scroll";
      return !allowsHorizontalScroll && element.scrollWidth > element.clientWidth + 1
        ? [
            {
              clientWidth: element.clientWidth,
              className: element.className,
              scrollWidth: element.scrollWidth,
            },
          ]
        : [];
    }),
  );
  expect(unboundedRegions, `${pageCase.id} key regions must not clip horizontal content`).toEqual([]);
  await expectAllCursorTables(root);
  await assertRouteSpecificGeometry(root, pageCase);
}
