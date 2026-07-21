import { describe, expect, it } from "vitest";

import type { components } from "../../api/generated/openapi";
import {
  PlaytestAuthorityError,
  assessTaskSuiteNavigationCandidate,
  bindPlaytestFindingLinks,
  bindPlaytestTerminalAuthority,
  requireEpisodeSelection,
  requireTaskSuiteAuthority,
} from "./authority";

type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
type PlaytestRunRequest = components["schemas"]["PlaytestRunRequestV1"];
type RunFindingLink = components["schemas"]["RunFindingLinkViewV1"];
type RunView = components["schemas"]["RunViewV1"];
type TaskSuiteView = components["schemas"]["TaskSuiteArtifactViewV1"];
type VersionTuple = components["schemas"]["VersionTuple"];

const HASH_A = "a".repeat(64);
const HASH_B = "b".repeat(64);
const HASH_C = "c".repeat(64);
// Derived and validated with the Python M4c contracts, including canonical
// reset hashes, execution-plan digest, subseed@1, and the self-sized envelope.
const OUTPOST_RESET_HASH = "c224e94dbaa954754a7b7c9204b678b6e09f080e33aef9ac430508cbb73a2e3a";
const RUINS_RESET_HASH = "48d1e1fbb1f39fca9afeb4875f4b26d4fa13cc7a60b7e971f09df386694b6b6f";
const PLAN_DIGEST = "5569de578d5ef8dc5fe6a05453cd615affb1bb739f47b6faad0e05cdf28e852c";
const EPISODE_1497_SEED = 6_903_211_299_418_366;
const EPISODE_5652_SEED = 8_768_229_418_745_192;
const STATE_0 = `sha256:${"0".repeat(64)}`;
const STATE_1 = `sha256:${"1".repeat(64)}`;

const ENVIRONMENT = { profile_id: "environment:aureus", version: 2 } as const;
const SUITE_PROFILE = { profile_id: "task-suite:quest-regression", version: 3 } as const;
const PLANNER = { profile_id: "planner:layered", version: 4 } as const;

function oracle(episode: string) {
  return {
    oracle_id: "quest-complete",
    params: { quest_id: episode },
    params_schema_id: "quest-completion-params@1",
    version: 1,
  };
}

function suiteView(): TaskSuiteView {
  return {
    artifact: {
      artifact_id: "artifact:suite",
      created_at: "2026-07-20T00:00:00Z",
      domain_scope: { domain_ids: ["quests"] },
      kind: "task_suite",
      lineage_schema_version: "lineage@2",
      parent_artifact_ids: [
        "artifact:config",
        "artifact:constraints",
        "artifact:preview",
        "artifact:scenario-1",
        "artifact:scenario-2",
      ],
      payload_hash: HASH_A,
      payload_schema_id: "task-suite@1",
      summary_schema_version: "artifact-summary@1",
      version_tuple: {
        constraint_snapshot_id: "constraint-snapshot:v1",
        doc_version: "doc:v1",
        env_contract_version: "agent-env@2",
        ir_snapshot_id: "snapshot:preview",
        tool_version: "task-suite-deriver@1",
      },
    },
    task_suite: {
      completion_oracle_registry_ref: { digest: HASH_B, registry_version: 5 },
      config_export_artifact_id: "artifact:config",
      constraint_snapshot_artifact_id: "artifact:constraints",
      env_contract_version: "agent-env@2",
      environment_profile: { ...ENVIRONMENT },
      episodes: [
        {
          completion_oracle: oracle("episode:1497"),
          domain_scope: { domain_ids: ["quests"] },
          episode_id: "episode:1497",
          reset_binding: {
            payload: { spawn: "outpost" },
            payload_hash: OUTPOST_RESET_HASH,
            reset_schema_id: "aureus-reset@1",
          },
          scenario_spec_artifact_id: "artifact:scenario-1",
          step_budget: 12,
        },
        {
          completion_oracle: oracle("episode:5652"),
          domain_scope: { domain_ids: ["quests"] },
          episode_id: "episode:5652",
          reset_binding: {
            payload: { spawn: "ruins" },
            payload_hash: RUINS_RESET_HASH,
            reset_schema_id: "aureus-reset@1",
          },
          scenario_spec_artifact_id: "artifact:scenario-2",
          step_budget: 8,
        },
      ],
      source_preview_artifact_id: "artifact:preview",
      suite_profile: { ...SUITE_PROFILE },
      task_suite_schema_version: "task-suite@1",
    },
    view_schema_version: "task-suite-artifact-view@1",
  };
}

function request(): PlaytestRunRequest {
  return {
    cassette_artifact_id: null,
    execution_version_plan: {
      agent_graph_version: "playtest-graph@1",
      model_catalog_digest: HASH_A,
      model_catalog_version: 1,
      nodes: [
        {
          agent_node_id: "playtest.planner",
          allowed_model_snapshots: ["model:test@1"],
          prompt_version: "playtest-prompt@1",
          tool_version: "playtest-runner@1",
        },
      ],
      plan_digest: PLAN_DIGEST,
      plan_schema_version: "execution-version-plan@1",
      routing_policy_digest: HASH_C,
      routing_policy_version: 1,
    },
    llm_execution_mode: "live",
    params: {
      config_artifact_id: "artifact:config",
      constraint_snapshot_artifact_id: "artifact:constraints",
      environment_profile: { ...ENVIRONMENT },
      episodes: [
        { episode_id: "episode:1497", scenario_spec_artifact_id: "artifact:scenario-1" },
        { episode_id: "episode:5652", scenario_spec_artifact_id: "artifact:scenario-2" },
      ],
      interaction_mode: "autonomous",
      max_steps_per_episode: 8,
      planner_policy: { ...PLANNER },
      schema_version: "playtest-run@1",
      task_suite_artifact_id: "artifact:suite",
    },
    request_schema_version: "playtest-run-request@1",
    seed: 7,
  };
}

function traceEpisode(episodeId: "episode:1497" | "episode:5652", completed: boolean) {
  const scenarioId = episodeId === "episode:1497" ? "artifact:scenario-1" : "artifact:scenario-2";
  const terminalReason = completed ? "completion_oracle_satisfied" : "agent_stopped";
  return {
    action_trace: [
      {
        action: { kind: "observe" },
        last_action_result: "observed",
        state_hash: STATE_1,
        tick: 3,
      },
    ],
    completed,
    completion_oracle: oracle(episodeId),
    episode_id: episodeId,
    execution_step_limit: 8,
    final_state_hash: STATE_1,
    initial_state_hash: STATE_0,
    markers: [
      {
        detail: terminalReason,
        kind: completed ? "completion" : "failure",
        state_hash: STATE_1,
        step_index: 0,
      },
    ],
    scenario_spec_artifact_id: scenarioId,
    seed: episodeId === "episode:1497" ? EPISODE_1497_SEED : EPISODE_5652_SEED,
    seed_binding: {
      case_id: `artifact:suite:${episodeId}`,
      profile: { ...ENVIRONMENT },
      replication_index: 0,
      root_seed: 7,
      run_kind: { kind: "playtest.run", version: 1 },
      seed: episodeId === "episode:1497" ? EPISODE_1497_SEED : EPISODE_5652_SEED,
      seed_derivation_version: "subseed@1",
    },
    step_budget: episodeId === "episode:1497" ? 12 : 8,
    terminal_reason: terminalReason,
  };
}

function tracePayload() {
  return {
    config_artifact_id: "artifact:config",
    constraint_snapshot_artifact_id: "artifact:constraints",
    env_contract_version: "agent-env@2",
    environment_profile: { ...ENVIRONMENT },
    episodes: [traceEpisode("episode:1497", true), traceEpisode("episode:5652", false)],
    execution_envelope: {
      actual_model_calls: 2,
      actual_trace_bytes: 3098,
      model_call_upper_bound: 48,
      planner_profile_payload_hash: HASH_C,
      selected_episode_count: 2,
      total_action_count: 2,
      total_action_trace_bytes: 318,
      total_step_limit: 16,
      total_trace_byte_upper_bound: 1_966_100,
    },
    interaction_mode: "autonomous",
    planner_memory_mode: "off",
    planner_policy: { ...PLANNER },
    playtest_trace_schema_version: "playtest-trace@1",
    requested_max_steps_per_episode: 8,
    seed: 7,
    task_suite_artifact_id: "artifact:suite",
  };
}

function terminalTuple(): VersionTuple {
  return {
    agent_graph_version: "playtest-graph@1",
    constraint_snapshot_id: "constraint-snapshot:v1",
    doc_version: "doc:v1",
    env_contract_version: "agent-env@2",
    ir_snapshot_id: "snapshot:preview",
    model_snapshot: "model:test@1",
    prompt_version: "playtest-prompt@1",
    seed: 7,
    tool_version: "playtest-runner@1",
  };
}

function traceView(): ArtifactPayloadView {
  return {
    artifact: {
      artifact_id: "artifact:trace",
      created_at: "2026-07-20T00:01:00Z",
      domain_scope: { domain_ids: ["quests"] },
      kind: "playtest_trace",
      lineage_schema_version: "lineage@2",
      parent_artifact_ids: [
        "artifact:config",
        "artifact:constraints",
        "artifact:scenario-1",
        "artifact:scenario-2",
        "artifact:suite",
      ],
      payload_hash: HASH_C,
      payload_schema_id: "playtest-trace@1",
      summary_schema_version: "artifact-summary@1",
      version_tuple: terminalTuple(),
    },
    payload: tracePayload(),
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

type ManifestParent = {
  artifact_id: string;
  publication: "existing" | "run_published";
  role: "input" | "intermediate" | "output" | "evidence";
};

function inputParents(): ManifestParent[] {
  return [
    "artifact:config",
    "artifact:constraints",
    "artifact:scenario-1",
    "artifact:scenario-2",
    "artifact:suite",
  ].map((artifact_id) => ({ artifact_id, publication: "existing", role: "input" }));
}

function projection(parents: ManifestParent[], attemptNo: number | null = 1) {
  return {
    attempt_no: attemptNo,
    frozen_input_version_tuple: {
      constraint_snapshot_id: "constraint-snapshot:v1",
      env_contract_version: "agent-env@2",
      ir_snapshot_id: "snapshot:preview",
      seed: 7,
    },
    manifest_scope: "run",
    parents,
    projection_schema_version: "run-manifest-version-projection@1",
    run_kind: { kind: "playtest.run", version: 1 },
    run_payload_hash: HASH_A,
    terminal_version_tuple: terminalTuple(),
    version_transition_policy_ref: {
      digest: HASH_A,
      policy_id: "run-manifest-transition",
      policy_version: 1,
    },
  };
}

function successManifest(): ArtifactPayloadView {
  const parents = [
    ...inputParents(),
    { artifact_id: "artifact:trace", publication: "run_published", role: "output" } as const,
  ];
  return {
    artifact: {
      artifact_id: "artifact:result-manifest",
      created_at: "2026-07-20T00:02:00Z",
      domain_scope: { domain_ids: ["quests"] },
      kind: "run_result",
      lineage_schema_version: "lineage@2",
      parent_artifact_ids: parents.map((item) => item.artifact_id).sort(),
      payload_hash: HASH_B,
      payload_schema_id: "run-result@1",
      summary_schema_version: "artifact-summary@1",
      version_tuple: terminalTuple(),
    },
    payload: {
      attempt_no: 1,
      finding_count: 1,
      outcome_code: "playtest_completed",
      primary_artifact_id: "artifact:trace",
      produced_artifact_ids: ["artifact:trace"],
      requirement_dispositions: [],
      result_schema_version: "run-result@1",
      run_id: "run:playtest",
      run_kind: { kind: "playtest.run", version: 1 },
      summary: {
        finding_count: 1,
        outcome_code: "playtest_completed",
        primary_artifact_kind: "playtest_trace",
        produced_artifact_count: 1,
        summary_schema_version: "run-result-summary@1",
      },
      version_projection: projection(parents),
    },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

function successRun(): RunView {
  return {
    attempt_no: 1,
    events_url: "/api/v1/runs/run%3Aplaytest/events",
    failure_artifact_id: null,
    result_artifact_id: "artifact:result-manifest",
    revision: 4,
    run_id: "run:playtest",
    status: "succeeded",
    status_url: "/api/v1/runs/run%3Aplaytest",
    terminal_cassette_artifact_id: null,
    view_schema_version: "run-view@1",
  };
}

function failureManifest(): ArtifactPayloadView {
  const parents = inputParents();
  return {
    artifact: {
      ...successManifest().artifact,
      artifact_id: "artifact:failure-manifest",
      kind: "run_failure",
      parent_artifact_ids: parents.map((item) => item.artifact_id).sort(),
      payload_schema_id: "run-failure@1",
    },
    payload: {
      attempt_no: 1,
      cause_code: "execution_failed",
      dependency: null,
      evidence_artifact_ids: [],
      failure_class: "execution",
      failure_schema_version: "run-failure@1",
      occurred_at: "2026-07-20T00:02:00Z",
      redacted_message: "Playtest execution failed.",
      requirement_dispositions: [],
      retry_decision: {
        cause_code: "execution_failed",
        classifier: { classifier_digest: HASH_A, classifier_version: 1 },
        decision: "terminal",
        decision_schema_version: "retry-decision@1",
        evaluated_at_utc: "2026-07-20T00:02:00Z",
        failure_class: "execution",
        intrinsic_retry_eligible: false,
        reason_code: "not_retry_eligible",
        retry_policy: {
          retry_policy_digest: HASH_A,
          retry_policy_id: "agent-environment",
          retry_policy_version: 1,
        },
      },
      retryable: false,
      run_id: "run:playtest",
      run_kind: { kind: "playtest.run", version: 1 },
      version_projection: projection(parents),
    },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

function failureRun(status: "failed" | "cancelled" | "timed_out" = "failed"): RunView {
  return {
    ...successRun(),
    failure_artifact_id: "artifact:failure-manifest",
    result_artifact_id: null,
    status,
  };
}

function successAuthority() {
  return bindPlaytestTerminalAuthority({
    expectedRunId: "run:playtest",
    manifest: successManifest(),
    requestCandidate: request(),
    result: traceView(),
    run: successRun(),
    suite: suiteView(),
  });
}

function findingLink(overrides: Partial<RunFindingLink> = {}): RunFindingLink {
  return {
    attempt_no: 1,
    evidence_artifact_id: "artifact:trace",
    finding: {
      created_at: "2026-07-20T00:03:00Z",
      finding_id: "finding:episode-2",
      payload: {
        defect_class: "playtest_incomplete",
        entities: [],
        evidence: { episode_id: "episode:5652", tick: 999_999 },
        message: "The completion oracle was not satisfied.",
        minimal_repro: { episode_id: "episode:5652" },
        oracle_type: "deterministic",
        payload_schema_version: "finding-payload@1",
        producer_id: "playtest.completion_oracle",
        producer_run_id: "run:playtest",
        relations: [],
        severity: "major",
        snapshot_id: "snapshot:preview",
        source: "playtest",
        status: "confirmed",
      },
      revision: 2,
      revision_schema_version: "finding-revision@1",
      supersedes_revision: 1,
    },
    finding_digest: HASH_C,
    ordinal: 1,
    run_id: "run:playtest",
    view_schema_version: "run-finding-link-view@1",
    ...overrides,
  };
}

describe("TaskSuite authority", () => {
  it("accepts the exact immutable envelope and treats matching navigation data as a candidate", () => {
    const suite = suiteView();
    const authority = requireTaskSuiteAuthority(suite, "artifact:suite", {
      configArtifactId: "artifact:config",
      constraintSnapshotArtifactId: "artifact:constraints",
      environmentProfile: ENVIRONMENT,
      sourcePreviewArtifactId: "artifact:preview",
    });

    expect(authority.navigation).toEqual({
      matches: true,
      mismatches: [],
      providedFields: ["preview", "config", "constraint", "environment"],
    });
    expect(authority.episodes.map((item) => item.episode_id)).toEqual(["episode:1497", "episode:5652"]);
    expect(authority.minStepBudget).toBe(8);
  });

  it("reports a stale query candidate without replacing or rejecting server authority", () => {
    const view = suiteView();
    const assessment = assessTaskSuiteNavigationCandidate(view, {
      configArtifactId: "artifact:old-config",
      environmentProfile: { profile_id: "environment:old", version: 1 },
      sourcePreviewArtifactId: "artifact:old-preview",
    });

    expect(assessment.matches).toBe(false);
    expect(assessment.mismatches).toEqual(["preview", "config", "environment"]);
    expect(view.task_suite.config_export_artifact_id).toBe("artifact:config");
  });

  it.each([
    ["route identity", (view: TaskSuiteView) => (view.artifact.artifact_id = "artifact:other")],
    ["artifact kind", (view: TaskSuiteView) => (view.artifact.kind = "scenario_spec")],
    ["payload schema", (view: TaskSuiteView) => (view.artifact.payload_schema_id = "task-suite@2")],
    ["lineage schema", (view: TaskSuiteView) => (view.artifact.lineage_schema_version = "lineage@1")],
    ["payload hash", (view: TaskSuiteView) => (view.artifact.payload_hash = null)],
    ["IR tuple", (view: TaskSuiteView) => (view.artifact.version_tuple.ir_snapshot_id = null)],
    [
      "constraint tuple",
      (view: TaskSuiteView) => (view.artifact.version_tuple.constraint_snapshot_id = null),
    ],
    ["tool tuple", (view: TaskSuiteView) => (view.artifact.version_tuple.tool_version = null)],
    [
      "environment tuple",
      (view: TaskSuiteView) => (view.artifact.version_tuple.env_contract_version = "agent-env@old"),
    ],
    ["parent closure", (view: TaskSuiteView) => view.artifact.parent_artifact_ids.pop()],
  ])("rejects a mismatched %s", (_label, mutate) => {
    const view = suiteView();
    mutate(view);
    expect(() => requireTaskSuiteAuthority(view, "artifact:suite")).toThrow(PlaytestAuthorityError);
  });

  it.each([
    ["empty episodes", (view: TaskSuiteView) => (view.task_suite.episodes = [])],
    [
      "duplicate episode",
      (view: TaskSuiteView) =>
        view.task_suite.episodes.push({
          ...view.task_suite.episodes[0],
          scenario_spec_artifact_id: "artifact:scenario-extra",
        }),
    ],
    [
      "duplicate scenario",
      (view: TaskSuiteView) =>
        (view.task_suite.episodes[1].scenario_spec_artifact_id = "artifact:scenario-1"),
    ],
    ["invalid budget", (view: TaskSuiteView) => (view.task_suite.episodes[0].step_budget = 0)],
    ["invalid suite profile", (view: TaskSuiteView) => (view.task_suite.suite_profile.version = 0)],
    [
      "invalid oracle registry",
      (view: TaskSuiteView) => (view.task_suite.completion_oracle_registry_ref.digest = "bad"),
    ],
    [
      "invalid completion oracle",
      (view: TaskSuiteView) => (view.task_suite.episodes[0].completion_oracle.version = 0),
    ],
  ])("rejects %s", (_label, mutate) => {
    const view = suiteView();
    mutate(view);
    expect(() => requireTaskSuiteAuthority(view, "artifact:suite")).toThrow(PlaytestAuthorityError);
  });
});

describe("episode subset authority", () => {
  it("returns one canonical exact subset and its minimum budget", () => {
    const selected = requireEpisodeSelection(
      suiteView(),
      [
        { episode_id: "episode:5652", scenario_spec_artifact_id: "artifact:scenario-2" },
        { episode_id: "episode:1497", scenario_spec_artifact_id: "artifact:scenario-1" },
      ],
      8,
    );

    expect(selected.episodes.map((item) => item.episode_id)).toEqual(["episode:1497", "episode:5652"]);
    expect(selected.minStepBudget).toBe(8);
    expect(selected.maxStepsPerEpisode).toBe(8);
  });

  it.each([
    ["empty selection", [], 1],
    ["wrong pair", [{ episode_id: "episode:1497", scenario_spec_artifact_id: "artifact:scenario-2" }], 1],
    [
      "duplicate episode",
      [
        { episode_id: "episode:1497", scenario_spec_artifact_id: "artifact:scenario-1" },
        { episode_id: "episode:1497", scenario_spec_artifact_id: "artifact:scenario-extra" },
      ],
      1,
    ],
    [
      "duplicate scenario",
      [
        { episode_id: "episode:1497", scenario_spec_artifact_id: "artifact:scenario-1" },
        { episode_id: "episode:5652", scenario_spec_artifact_id: "artifact:scenario-1" },
      ],
      1,
    ],
    [
      "budget exceeds selected minimum",
      [{ episode_id: "episode:5652", scenario_spec_artifact_id: "artifact:scenario-2" }],
      9,
    ],
    [
      "non-positive budget",
      [{ episode_id: "episode:1497", scenario_spec_artifact_id: "artifact:scenario-1" }],
      0,
    ],
  ])("rejects %s", (_label, episodes, maxSteps) => {
    expect(() => requireEpisodeSelection(suiteView(), episodes, maxSteps)).toThrow(PlaytestAuthorityError);
  });
});

describe("Playtest terminal authority", () => {
  it("closes Run → RunResult → PlaytestTrace through the verified server result", () => {
    const authority = successAuthority();

    expect(authority.kind).toBe("succeeded");
    if (authority.kind !== "succeeded") throw new Error("expected success authority");
    expect(authority.trace.artifact.artifact.artifact_id).toBe("artifact:trace");
    expect(authority.trace.episodes.map((item) => item.episodeId)).toEqual(["episode:1497", "episode:5652"]);
    expect(authority.completedEpisodeCount).toBe(1);
    expect(authority.allEpisodesCompleted).toBe(false);
    expect(authority.runStatus).toBe("succeeded");
    expect(authority.findingCount).toBe(1);
    expect(authority.requestCandidateStatus).toBe("visible_bindings_match");
  });

  it("restores a successful deep link without browser-local request state", () => {
    const authority = bindPlaytestTerminalAuthority({
      expectedRunId: "run:playtest",
      manifest: successManifest(),
      result: traceView(),
      run: successRun(),
      suite: suiteView(),
    });

    expect(authority.kind).toBe("succeeded");
    if (authority.kind !== "succeeded") throw new Error("expected success authority");
    expect(authority.selection.episodes.map((item) => item.episode_id)).toEqual([
      "episode:1497",
      "episode:5652",
    ]);
    expect(authority.requestCandidateStatus).toBe("not_provided");
  });

  it("accepts Run-published source_rendered lineage in a real LLM trace", () => {
    const sourceRenderedId = "artifact:source-rendered";
    const result = traceView();
    result.artifact.parent_artifact_ids.push(sourceRenderedId);
    result.artifact.parent_artifact_ids.sort();

    const manifest = successManifest();
    const payload = manifest.payload as Record<string, unknown>;
    const projectionValue = payload.version_projection as Record<string, unknown>;
    (projectionValue.parents as ManifestParent[]).push({
      artifact_id: sourceRenderedId,
      publication: "run_published",
      role: "intermediate",
    });
    manifest.artifact.parent_artifact_ids.push(sourceRenderedId);
    manifest.artifact.parent_artifact_ids.sort();
    (payload.produced_artifact_ids as string[]).push(sourceRenderedId);
    (payload.produced_artifact_ids as string[]).sort();
    const summary = payload.summary as Record<string, unknown>;
    summary.produced_artifact_count = 2;

    const authority = bindPlaytestTerminalAuthority({
      expectedRunId: "run:playtest",
      manifest,
      result,
      run: successRun(),
      suite: suiteView(),
    });

    expect(authority.kind).toBe("succeeded");
  });

  it("does not pretend a browser-local execution plan is terminal authority", () => {
    const candidate = request();
    if (candidate.execution_version_plan == null) throw new Error("expected execution plan fixture");
    candidate.execution_version_plan.plan_digest = HASH_A;
    candidate.execution_version_plan.nodes[0].tool_version = "stale-tool@1";

    const authority = bindPlaytestTerminalAuthority({
      expectedRunId: "run:playtest",
      manifest: successManifest(),
      requestCandidate: candidate,
      result: traceView(),
      run: successRun(),
      suite: suiteView(),
    });

    expect(authority.requestCandidateStatus).toBe("visible_bindings_match");
  });

  it("keeps successful Run execution separate from deterministic episode completion", () => {
    const authority = successAuthority();
    expect(authority).toMatchObject({
      allEpisodesCompleted: false,
      kind: "succeeded",
      runStatus: "succeeded",
    });
  });

  it("accepts an honest terminal RunFailure without inventing a trace", () => {
    const authority = bindPlaytestTerminalAuthority({
      expectedRunId: "run:playtest",
      manifest: failureManifest(),
      requestCandidate: request(),
      result: null,
      run: failureRun(),
      suite: suiteView(),
    });

    expect(authority).toMatchObject({
      attemptNo: 1,
      causeCode: "execution_failed",
      kind: "failed",
      message: "Playtest execution failed.",
      resultArtifact: null,
      runStatus: "failed",
    });
    if (authority.kind !== "failed") throw new Error("expected failure authority");
    expect(authority.selectionCandidate?.episodes).toHaveLength(2);
    expect(authority.requestCandidateStatus).toBe("visible_bindings_match");
  });

  it("restores a failed deep link without inventing a frozen request", () => {
    const authority = bindPlaytestTerminalAuthority({
      expectedRunId: "run:playtest",
      manifest: failureManifest(),
      result: null,
      run: failureRun(),
      suite: suiteView(),
    });

    expect(authority.kind).toBe("failed");
    if (authority.kind !== "failed") throw new Error("expected failure authority");
    expect(authority.selectionCandidate).toBeNull();
    expect(authority.requestCandidateStatus).toBe("not_provided");
  });

  it("reports a stale failure request hint without blocking server authority", () => {
    const candidate = request();
    candidate.params.episodes[0].scenario_spec_artifact_id = "artifact:wrong";

    const authority = bindPlaytestTerminalAuthority({
      expectedRunId: "run:playtest",
      manifest: failureManifest(),
      requestCandidate: candidate,
      result: null,
      run: failureRun(),
      suite: suiteView(),
    });

    expect(authority.kind).toBe("failed");
    if (authority.kind !== "failed") throw new Error("expected failure authority");
    expect(authority.requestCandidateStatus).toBe("stale");
    expect(authority.selectionCandidate).toBeNull();
  });

  it.each([
    ["requested Run", () => ({ expectedRunId: "run:other" })],
    ["Run manifest pointer", () => ({ run: { ...successRun(), result_artifact_id: "artifact:other" } })],
    [
      "manifest Run",
      () => {
        const manifest = successManifest();
        (manifest.payload as Record<string, unknown>).run_id = "run:other";
        return { manifest };
      },
    ],
    [
      "manifest kind",
      () => ({
        manifest: {
          ...successManifest(),
          artifact: { ...successManifest().artifact, kind: "run_failure" },
        } as ArtifactPayloadView,
      }),
    ],
    [
      "primary result identity",
      () => ({
        result: { ...traceView(), artifact: { ...traceView().artifact, artifact_id: "artifact:other" } },
      }),
    ],
    [
      "manifest lineage",
      () => {
        const manifest = successManifest();
        manifest.artifact.parent_artifact_ids.pop();
        return { manifest };
      },
    ],
    [
      "manifest Run payload hash",
      () => {
        const manifest = successManifest();
        const payload = manifest.payload as Record<string, unknown>;
        const projectionValue = payload.version_projection as Record<string, unknown>;
        projectionValue.run_payload_hash = "not-a-digest";
        return { manifest };
      },
    ],
    [
      "manifest input closure",
      () => {
        const manifest = successManifest();
        const payload = manifest.payload as Record<string, unknown>;
        const projectionValue = payload.version_projection as Record<string, unknown>;
        projectionValue.parents = (projectionValue.parents as ManifestParent[]).filter(
          (item) => item.artifact_id !== "artifact:scenario-2",
        );
        return { manifest };
      },
    ],
  ])("rejects a mismatched %s", (_label, change) => {
    const changed = change();
    expect(() =>
      bindPlaytestTerminalAuthority({
        expectedRunId: "run:playtest",
        manifest: successManifest(),
        requestCandidate: request(),
        result: traceView(),
        run: successRun(),
        suite: suiteView(),
        ...changed,
      }),
    ).toThrow(PlaytestAuthorityError);
  });

  it.each([
    ["config", (payload: Record<string, unknown>) => (payload.config_artifact_id = "artifact:old")],
    [
      "constraint",
      (payload: Record<string, unknown>) => (payload.constraint_snapshot_artifact_id = "artifact:old"),
    ],
    ["suite", (payload: Record<string, unknown>) => (payload.task_suite_artifact_id = "artifact:old")],
    [
      "environment",
      (payload: Record<string, unknown>) =>
        (payload.environment_profile = { profile_id: "environment:old", version: 1 }),
    ],
    ["seed", (payload: Record<string, unknown>) => (payload.seed = 8)],
    ["maximum steps", (payload: Record<string, unknown>) => (payload.requested_max_steps_per_episode = 7)],
    [
      "episode pair",
      (payload: Record<string, unknown>) => {
        const episodes = payload.episodes as Record<string, unknown>[];
        episodes[0].scenario_spec_artifact_id = "artifact:scenario-2";
      },
    ],
  ])("rejects a trace with a mismatched %s binding", (_label, mutate) => {
    const result = traceView();
    mutate(result.payload as Record<string, unknown>);
    expect(() =>
      bindPlaytestTerminalAuthority({
        expectedRunId: "run:playtest",
        manifest: successManifest(),
        requestCandidate: request(),
        result,
        run: successRun(),
        suite: suiteView(),
      }),
    ).toThrow(PlaytestAuthorityError);
  });

  it.each([
    [
      "planner",
      (payload: Record<string, unknown>) =>
        (payload.planner_policy = { profile_id: "planner:old", version: 1 }),
    ],
    ["interaction mode", (payload: Record<string, unknown>) => (payload.interaction_mode = "bounded_choice")],
  ])("reports a stale local candidate when server-authoritative %s differs", (_label, mutate) => {
    const result = traceView();
    mutate(result.payload as Record<string, unknown>);
    const authority = bindPlaytestTerminalAuthority({
      expectedRunId: "run:playtest",
      manifest: successManifest(),
      requestCandidate: request(),
      result,
      run: successRun(),
      suite: suiteView(),
    });

    expect(authority.kind).toBe("succeeded");
    expect(authority.requestCandidateStatus).toBe("stale");
  });

  it("rejects a result Artifact when its VersionTuple is not the manifest terminal tuple", () => {
    const result = traceView();
    result.artifact.version_tuple.tool_version = "other@1";
    expect(() =>
      bindPlaytestTerminalAuthority({
        expectedRunId: "run:playtest",
        manifest: successManifest(),
        requestCandidate: request(),
        result,
        run: successRun(),
        suite: suiteView(),
      }),
    ).toThrow(PlaytestAuthorityError);
  });

  it("rejects a trace whose episode authority differs from the exact TaskSuite", () => {
    const result = traceView();
    const episodes = (result.payload as Record<string, unknown>).episodes as Record<string, unknown>[];
    episodes[0].completion_oracle = oracle("episode:other");
    expect(() =>
      bindPlaytestTerminalAuthority({
        expectedRunId: "run:playtest",
        manifest: successManifest(),
        requestCandidate: request(),
        result,
        run: successRun(),
        suite: suiteView(),
      }),
    ).toThrow(PlaytestAuthorityError);
  });

  it("rejects a result body on a non-success terminal Run", () => {
    expect(() =>
      bindPlaytestTerminalAuthority({
        expectedRunId: "run:playtest",
        manifest: failureManifest(),
        requestCandidate: request(),
        result: traceView(),
        run: failureRun(),
        suite: suiteView(),
      }),
    ).toThrow(PlaytestAuthorityError);
  });
});

describe("Playtest Finding link authority", () => {
  it("closes exact Run links to the trace without guessing a Finding tick", () => {
    const terminal = successAuthority();
    if (terminal.kind !== "succeeded") throw new Error("expected success authority");
    const links = bindPlaytestFindingLinks(terminal, [findingLink()]);

    expect(links).toHaveLength(1);
    expect(links[0].finding.payload.evidence).toEqual({ episode_id: "episode:5652", tick: 999_999 });
    expect(links[0]).not.toHaveProperty("traceStepIndex");
  });

  it("requires complete cardinality closure with the RunResult manifest", () => {
    const terminal = successAuthority();
    if (terminal.kind !== "succeeded") throw new Error("expected success authority");
    expect(() => bindPlaytestFindingLinks(terminal, [])).toThrow(PlaytestAuthorityError);
  });

  it.each([
    ["run", { run_id: "run:other" }],
    ["attempt", { attempt_no: 2 }],
    ["evidence", { evidence_artifact_id: "artifact:other" }],
    ["digest", { finding_digest: "bad" }],
    [
      "producer",
      {
        finding: {
          ...findingLink().finding,
          payload: { ...findingLink().finding.payload, producer_run_id: "run:other" },
        },
      },
    ],
    [
      "snapshot",
      {
        finding: {
          ...findingLink().finding,
          payload: { ...findingLink().finding.payload, snapshot_id: "snapshot:other" },
        },
      },
    ],
    [
      "source",
      {
        finding: {
          ...findingLink().finding,
          payload: { ...findingLink().finding.payload, source: "checker" as const },
        },
      },
    ],
  ])("rejects a mismatched %s binding", (_label, overrides) => {
    const terminal = successAuthority();
    if (terminal.kind !== "succeeded") throw new Error("expected success authority");
    expect(() => bindPlaytestFindingLinks(terminal, [findingLink(overrides)])).toThrow(
      PlaytestAuthorityError,
    );
  });
});
