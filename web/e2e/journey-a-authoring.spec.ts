import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Browser, type BrowserContext, type Locator, type Page } from "@playwright/test";

import {
  DEMO_AUTHORING_GOAL,
  DEMO_PROVENANCE_LABEL,
  DEMO_README_FRAMES,
  DEMO_SCENES,
  DEMO_TARGET_DURATION_MS,
  type DemoScene,
} from "../scripts/journey-a-demo-storyboard";
import type { components } from "../src/api/generated/openapi";
import {
  guardAuthoringEgress,
  loginAuthoringPage,
  startAuthoringStack,
  type AuthoringStack,
} from "./support/authoring-live-stack";

const makerCredentials = { login: "maker", password: "maker-password-1" };
const approverCredentials = { login: "approver", password: "approver-password-1" };
const domainId = "builtin";
const refName = "content-head";
const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");

function demoOutputDirectory(): string {
  return (
    process.env.GAMEFORGE_DEMO_OUTPUT_DIR ?? resolve(repoRoot, "web/test-results/demo-complete-workflow")
  );
}

interface JourneyAManifest {
  base_artifact_id: string;
  constraint_artifact_id: string;
  expected_ref: components["schemas"]["RefValue"];
  gate_rejected_source_run_id: string;
  generation_source_run_id: string;
  record_patch_artifact_id: string;
  schema_version: "journey-a-live-fixture@1";
}

interface BrowserApiResponse<T> {
  body: T;
  headers: Record<string, string>;
  status: number;
}

type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
type ApprovalView = components["schemas"]["ApprovalViewV1"];
type ExecutionOptionResolveRequest = components["schemas"]["ExecutionOptionResolveRequestV1"];
type ExecutionOptionView = components["schemas"]["ExecutionOptionViewV1"];
type FindingBinding = components["schemas"]["FindingEvidenceBindingV1"];
type PatchRepairRequest = components["schemas"]["PatchRepairRequestV1"];
type PatchValidationRequest = components["schemas"]["PatchValidationAdmissionRequestV1"];
type PlaytestRunRequest = components["schemas"]["PlaytestRunRequestV1"];
type RefHistoryPage = components["schemas"]["OpaquePageV1_RefHistoryEntryV1_"];
type RunAccepted = components["schemas"]["RunAcceptedV1"];
type RunFindingLink = components["schemas"]["RunFindingLinkViewV1"];
type RunFindingPage = components["schemas"]["OpaquePageV1_RunFindingLinkViewV1_"];
type RunSubmissionRequest = components["schemas"]["RunSubmissionRequestV1"];
type RunView = components["schemas"]["RunViewV1"];
type SubjectApprovalBinding = components["schemas"]["SubjectApprovalBindingViewV1"];
type TaskSuiteView = components["schemas"]["TaskSuiteArtifactViewV1"];

type AgentProspectiveRequest = ExecutionOptionResolveRequest["prospective_request"];
type ResolvedAgentRequest = PatchRepairRequest | PlaytestRunRequest | RunSubmissionRequest;

interface RecordSource<TRequest extends ResolvedAgentRequest> {
  request: TRequest;
  run: RunView;
}

let stack: AuthoringStack;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function runFixtureCli(args: readonly string[]): Promise<{ stderr: string; stdout: string }> {
  const python = process.env.GAMEFORGE_PYTHON ?? resolve(repoRoot, ".venv/bin/python");
  return new Promise((resolveRun, reject) => {
    execFile(python, args, { cwd: repoRoot, encoding: "utf-8" }, (error, stdout, stderr) => {
      if (error) {
        reject(new Error(`Journey A fixture CLI failed: ${stderr || error.message}`));
        return;
      }
      resolveRun({ stderr, stdout });
    });
  });
}

async function sameOriginRequest<T>(
  page: Page,
  path: string,
  options: {
    body?: unknown;
    expected?: readonly number[];
    headers?: Record<string, string>;
    method?: "GET" | "POST";
    withCsrf?: boolean;
  } = {},
): Promise<BrowserApiResponse<T>> {
  const expected = options.expected ?? [200];
  const result = await page.evaluate(
    async ({ body, headers, method, path, withCsrf }) => {
      const requestHeaders: Record<string, string> = { ...headers };
      if (body !== undefined) requestHeaders["Content-Type"] = "application/json";
      if (withCsrf) {
        const csrf = sessionStorage.getItem("gameforge.csrf-token");
        if (csrf === null) throw new Error("Journey A browser session has no CSRF token.");
        requestHeaders["X-CSRF-Token"] = csrf;
      }
      const response = await fetch(path, {
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "include",
        headers: requestHeaders,
        method,
      });
      const text = await response.text();
      return {
        body: text.length === 0 ? null : (JSON.parse(text) as unknown),
        headers: Object.fromEntries(response.headers.entries()),
        status: response.status,
      };
    },
    {
      body: options.body,
      headers: options.headers ?? {},
      method: options.method ?? "GET",
      path,
      withCsrf: options.withCsrf ?? false,
    },
  );
  if (!expected.includes(result.status)) {
    throw new Error(
      `Unexpected ${result.status} from ${options.method ?? "GET"} ${path}: ${JSON.stringify(result.body)}`,
    );
  }
  return result as BrowserApiResponse<T>;
}

async function sameOriginText(page: Page, path: string): Promise<string> {
  const result = await page.evaluate(async (requestPath) => {
    const response = await fetch(requestPath, { credentials: "include" });
    return {
      contentType: response.headers.get("content-type"),
      status: response.status,
      text: await response.text(),
    };
  }, path);
  if (result.status !== 200 || !result.contentType?.startsWith("application/json")) {
    throw new Error(`Unexpected ${result.status} from GET ${path}: ${result.text}`);
  }
  return result.text;
}

function mutationHeaders(label: string): Record<string, string> {
  return { "Idempotency-Key": `journey-a-browser:${label}:${crypto.randomUUID()}` };
}

function resourceEtag(resourceKind: string, resourceId: string, revision: number): string {
  const canonical = JSON.stringify({
    etag_schema_version: "resource-etag@1",
    resource_id: resourceId,
    resource_kind: resourceKind,
    revision,
  });
  return `"${createHash("sha256").update(canonical).digest("hex")}"`;
}

async function recordSource<TRequest extends ResolvedAgentRequest>(
  page: Page,
  input: {
    endpoint: string;
    expectedStatus: RunView["status"];
    label: string;
    prospective: AgentProspectiveRequest;
    resourceOperationId: ExecutionOptionResolveRequest["resource_operation_id"];
    runKind: components["schemas"]["RunKindRef"];
  },
): Promise<RecordSource<TRequest>> {
  const resolverRequest: ExecutionOptionResolveRequest = {
    llm_execution_mode: "record",
    prospective_request: input.prospective,
    replay_source_run_id: null,
    request_schema_version: "execution-option-resolve-request@1",
    resource_operation_id: input.resourceOperationId,
    run_kind: input.runKind,
  };
  const option = await resolveRecordOption(page, resolverRequest);
  if (
    option.llm_execution_mode !== "record" ||
    option.resource_operation_id !== input.resourceOperationId ||
    option.run_kind.kind !== input.runKind.kind ||
    option.run_kind.version !== input.runKind.version ||
    option.source_run_id != null ||
    option.cassette_artifact_id != null
  ) {
    throw new Error(`Fixture RECORD option for ${input.label} did not preserve exact authority.`);
  }
  const request = {
    ...input.prospective,
    cassette_artifact_id: null,
    execution_version_plan: option.execution_version_plan,
  } as TRequest;
  const accepted = (
    await sameOriginRequest<RunAccepted>(page, input.endpoint, {
      body: request,
      expected: [202],
      headers: mutationHeaders(`record:${input.label}`),
      method: "POST",
      withCsrf: true,
    })
  ).body;
  const run = await waitForRun(page, accepted.run_id);
  expect(run.status).toBe(input.expectedStatus);
  expect(run.terminal_cassette_artifact_id).toBeTruthy();
  return { request, run };
}

async function waitForRun(page: Page, runId: string): Promise<RunView> {
  let last: RunView | null = null;
  await expect
    .poll(
      async () => {
        last = (await sameOriginRequest<RunView>(page, `/api/v1/runs/${encodeURIComponent(runId)}`)).body;
        return last.status;
      },
      { intervals: [100, 200, 500], timeout: 45_000 },
    )
    .toMatch(/^(succeeded|failed|cancelled|timed_out)$/u);
  if (last === null) throw new Error(`Run ${runId} did not return a terminal view.`);
  return last;
}

async function artifact(page: Page, artifactId: string): Promise<ArtifactPayloadView> {
  return (
    await sameOriginRequest<ArtifactPayloadView>(page, `/api/v1/artifacts/${encodeURIComponent(artifactId)}`)
  ).body;
}

async function terminalManifest(
  page: Page,
  runId: string,
): Promise<{
  artifact: ArtifactPayloadView;
  run: RunView;
}> {
  const run = await waitForRun(page, runId);
  const manifestId = run.result_artifact_id ?? run.failure_artifact_id;
  if (!manifestId) throw new Error(`Terminal Run ${runId} has no manifest Artifact.`);
  return { artifact: await artifact(page, manifestId), run };
}

function manifestParents(payload: unknown): Array<Record<string, unknown>> {
  if (!isRecord(payload) || !isRecord(payload.version_projection)) {
    throw new Error("Terminal manifest has no version projection.");
  }
  const parents = payload.version_projection.parents;
  if (!Array.isArray(parents) || !parents.every(isRecord)) {
    throw new Error("Terminal manifest has invalid parent bindings.");
  }
  return parents;
}

async function artifactsForManifestRole(
  page: Page,
  payload: unknown,
  role: "evidence" | "output",
): Promise<ArtifactPayloadView[]> {
  const ids = manifestParents(payload)
    .filter((parent) => parent.role === role && parent.publication === "run_published")
    .map((parent) => parent.artifact_id)
    .filter((value): value is string => typeof value === "string");
  return Promise.all(ids.map((artifactId) => artifact(page, artifactId)));
}

async function refHistory(page: Page): Promise<components["schemas"]["RefHistoryEntryV1"][]> {
  const response = await sameOriginRequest<RefHistoryPage>(
    page,
    `/api/v1/refs/${encodeURIComponent(refName)}/history?limit=100`,
  );
  return [...response.body.items].sort((left, right) => left.value.revision - right.value.revision);
}

async function runIds(page: Page): Promise<string[]> {
  const response = await sameOriginRequest<components["schemas"]["OpaquePageV1_RunViewV1_"]>(
    page,
    "/api/v1/runs?limit=100",
  );
  expect(response.body.next_cursor).toBeNull();
  return response.body.items.map((item) => item.run_id).sort();
}

async function approvalIds(page: Page): Promise<string[]> {
  const response = await sameOriginRequest<components["schemas"]["OpaquePageV1_ApprovalViewV1_"]>(
    page,
    "/api/v1/approvals?limit=100",
  );
  expect(response.body.next_cursor).toBeNull();
  return response.body.items.map((item) => item.approval.approval_id).sort();
}

async function transportLines(): Promise<string[]> {
  if (stack.transportLogPath === null) return [];
  try {
    return (await readFile(stack.transportLogPath, "utf-8"))
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
  } catch (error) {
    if (isRecord(error) && error.code === "ENOENT") return [];
    throw error;
  }
}

async function fillGenerationReplayForm(
  page: Page,
  manifest: JourneyAManifest,
  input: { goal: string; sourceRunId: string },
): Promise<void> {
  await page.goto("/generation");
  await expect(page.getByRole("heading", { level: 1, name: "内容生成" })).toBeVisible();
  await page.getByLabel("Base Spec / ref").selectOption(manifest.base_artifact_id);
  await page.getByLabel("Constraint snapshot").selectOption(manifest.constraint_artifact_id);
  await page.getByLabel("Generation profile").selectOption("builtin.generation@1");
  await page.getByLabel("Environment profile").selectOption("builtin.environment@1");
  await page.getByRole("checkbox", { name: /builtin\.config_export@1/u }).check();
  await page.getByLabel("Domain IDs").fill(domainId);
  await page.getByLabel("Authenticated authoring goal").fill(input.goal);
  await page.getByLabel("LLM execution mode").selectOption("replay");
  await page.getByLabel("Replay source Run").fill(input.sourceRunId);
}

async function selectGenerationReplay(
  page: Page,
  manifest: JourneyAManifest,
  input: { goal: string; sourceRunId: string },
): Promise<string> {
  await fillGenerationReplayForm(page, manifest, input);
  const startGeneration = page.getByRole("button", { name: "开始生成" });
  if (process.env.GAMEFORGE_RECORD_DEMO === "1" && input.goal === DEMO_AUTHORING_GOAL) {
    const darkTheme = page.getByRole("button", { name: "切换到深色主题" });
    if ((await darkTheme.count()) > 0) await darkTheme.click();
    await startGeneration.scrollIntoViewIfNeeded();
    const outputDir = demoOutputDirectory();
    await mkdir(outputDir, { recursive: true });
    await page.screenshot({
      animations: "disabled",
      path: join(outputDir, "authoring-input-source.png"),
    });
  }
  await startGeneration.click();
  let runId = "";
  await expect
    .poll(() => {
      runId = new URL(page.url()).searchParams.get("run") ?? "";
      return runId;
    })
    .not.toBe("");
  return runId;
}

async function assertReplayDidNotCallTransport(before: readonly string[]): Promise<void> {
  expect(await transportLines()).toEqual(before);
}

async function resolveRecordOption(
  page: Page,
  request: ExecutionOptionResolveRequest,
): Promise<ExecutionOptionView> {
  return (
    await sameOriginRequest<ExecutionOptionView>(page, "/api/v1/execution-options:resolve", {
      body: request,
      method: "POST",
      withCsrf: true,
    })
  ).body;
}

function primaryArtifactId(payload: unknown): string {
  if (!isRecord(payload) || typeof payload.primary_artifact_id !== "string") {
    throw new Error("RunResult has no primary Artifact ID.");
  }
  return payload.primary_artifact_id;
}

async function successArtifacts(
  page: Page,
  runId: string,
): Promise<{
  manifest: ArtifactPayloadView;
  outputs: ArtifactPayloadView[];
  primaryArtifactId: string;
  run: RunView;
}> {
  const terminal = await terminalManifest(page, runId);
  expect(terminal.run.status).toBe("succeeded");
  return {
    manifest: terminal.artifact,
    outputs: await artifactsForManifestRole(page, terminal.artifact.payload, "output"),
    primaryArtifactId: primaryArtifactId(terminal.artifact.payload),
    run: terminal.run,
  };
}

function singleArtifactOfKind(artifacts: readonly ArtifactPayloadView[], kind: string): ArtifactPayloadView {
  const matches = artifacts.filter((item) => item.artifact.kind === kind);
  if (matches.length !== 1) {
    throw new Error(`Expected one ${kind} Artifact, received ${matches.length}.`);
  }
  return matches[0];
}

async function getPatchApproval(
  page: Page,
  patchId: string,
): Promise<{
  approval: ApprovalView;
  binding: SubjectApprovalBinding;
  etag: string;
}> {
  const binding = (
    await sameOriginRequest<SubjectApprovalBinding>(
      page,
      `/api/v1/workflow-subjects/${encodeURIComponent(patchId)}/approval-binding`,
    )
  ).body;
  const approvalResponse = await sameOriginRequest<ApprovalView>(
    page,
    `/api/v1/approvals/${encodeURIComponent(binding.approval_id)}`,
  );
  const etag = approvalResponse.headers.etag;
  if (!etag) throw new Error(`Approval ${binding.approval_id} has no exact ETag.`);
  return { approval: approvalResponse.body, binding, etag };
}

async function waitForPatchStatus(
  page: Page,
  patchId: string,
  status: components["schemas"]["ApprovalItem"]["status"],
): Promise<{ approval: ApprovalView; binding: SubjectApprovalBinding; etag: string }> {
  let value: { approval: ApprovalView; binding: SubjectApprovalBinding; etag: string } | null = null;
  await expect
    .poll(
      async () => {
        value = await getPatchApproval(page, patchId);
        return value.approval.approval.status;
      },
      { intervals: [100, 200, 500], timeout: 45_000 },
    )
    .toBe(status);
  if (value === null) throw new Error(`Patch ${patchId} did not expose ${status}.`);
  return value;
}

async function findingAuthorities(
  page: Page,
  runId: string,
): Promise<Array<{ binding: FindingBinding; finding: RunFindingLink["finding"] }>> {
  const response = await sameOriginRequest<RunFindingPage>(
    page,
    `/api/v1/runs/${encodeURIComponent(runId)}/finding-links?limit=100`,
  );
  return response.body.items.map((item) => ({
    binding: {
      evidence_artifact_id: item.evidence_artifact_id,
      finding_digest: item.finding_digest,
      finding_id: item.finding.finding_id,
      finding_revision: item.finding.revision,
    },
    finding: item.finding,
  }));
}

async function findingBindings(page: Page, runId: string): Promise<FindingBinding[]> {
  return (await findingAuthorities(page, runId)).map((item) => item.binding);
}

async function materializeRegressionSuite(
  page: Page,
  baseArtifactId: string,
  finding: RunFindingLink["finding"],
  exactFindingJson: string,
): Promise<string> {
  const findingPath = join(stack.workspace, `journey-a-exact-finding-${crypto.randomUUID()}.json`);
  await writeFile(findingPath, exactFindingJson, { encoding: "utf-8", flag: "wx" });
  const completed = await runFixtureCli([
    "-m",
    "tests.e2e.m4d_support.journey_a_regression_fixture",
    "--workspace",
    stack.workspace,
    "--base-artifact-id",
    baseArtifactId,
    "--finding",
    findingPath,
  ]);
  expect(completed.stderr).toBe("");
  const suiteId = completed.stdout.trim();
  expect(suiteId).toMatch(/^sha256:[0-9a-f]{64}$/u);

  const suite = await artifact(page, suiteId);
  expect(suite.artifact.kind).toBe("regression_suite");
  if (!isRecord(suite.payload) || !isRecord(suite.payload.adapter_payload)) {
    throw new Error("Dynamic RegressionSuite has no Agent-Env adapter payload.");
  }
  const cases = suite.payload.adapter_payload.cases;
  if (!Array.isArray(cases) || cases.length !== 1 || !isRecord(cases[0])) {
    throw new Error("Dynamic RegressionSuite has no exact Journey A case.");
  }
  const template = cases[0].failure_finding;
  if (!isRecord(template)) throw new Error("Dynamic RegressionSuite has no Finding template.");
  expect(template.evidence).toEqual(finding.payload.evidence);
  expect(template.minimal_repro).toEqual(finding.payload.minimal_repro);
  return suiteId;
}

async function recordReviewSource(
  page: Page,
  snapshotArtifactId: string,
  constraintArtifactId: string,
  includeChecker = true,
): Promise<RecordSource<RunSubmissionRequest>> {
  return recordSource<RunSubmissionRequest>(page, {
    endpoint: "/api/v1/runs",
    expectedStatus: "succeeded",
    label: `review:${snapshotArtifactId}`,
    prospective: {
      cassette_artifact_id: null,
      execution_version_plan: null,
      llm_execution_mode: "record",
      params: {
        checker_profiles: includeChecker ? [{ profile_id: "builtin.checker", version: 1 }] : [],
        constraint_snapshot_artifact_id: constraintArtifactId,
        llm_triage_policy: { profile_id: "builtin.llm_triage", version: 1 },
        review_profile: { profile_id: "builtin.review", version: 1 },
        schema_version: "review-run@1",
        selection: { entity_ids: [], mode: "full", relation_ids: [] },
        simulation_profiles: [{ profile_id: "builtin.simulation", version: 1 }],
        snapshot_artifact_id: snapshotArtifactId,
      },
      request_schema_version: "run-submission-request@1",
      seed: 1,
    },
    resourceOperationId: "submit_run_api_v1_runs_post",
    runKind: { kind: "review.run", version: 1 },
  });
}

async function recordPlaytestSource(
  page: Page,
  suite: TaskSuiteView,
  maxSteps: number,
): Promise<RecordSource<PlaytestRunRequest>> {
  return recordSource<PlaytestRunRequest>(page, {
    endpoint: "/api/v1/playtest:run",
    expectedStatus: "succeeded",
    label: `playtest:${suite.artifact.artifact_id}`,
    prospective: {
      cassette_artifact_id: null,
      execution_version_plan: null,
      llm_execution_mode: "record",
      params: {
        config_artifact_id: suite.task_suite.config_export_artifact_id,
        constraint_snapshot_artifact_id: suite.task_suite.constraint_snapshot_artifact_id,
        environment_profile: suite.task_suite.environment_profile,
        episodes: suite.task_suite.episodes.map((episode) => ({
          episode_id: episode.episode_id,
          scenario_spec_artifact_id: episode.scenario_spec_artifact_id,
        })),
        interaction_mode: "autonomous",
        max_steps_per_episode: maxSteps,
        planner_policy: { profile_id: "builtin.playtest_planner", version: 2 },
        schema_version: "playtest-run@1",
        task_suite_artifact_id: suite.artifact.artifact_id,
      },
      request_schema_version: "playtest-run-request@1",
      seed: 1,
    },
    resourceOperationId: "run_playtest_api_v1_playtest_run_post",
    runKind: { kind: "playtest.run", version: 1 },
  });
}

async function recordRepairSource(
  page: Page,
  input: {
    approval: ApprovalView;
    baseArtifactId: string;
    constraintArtifactId: string;
    expectedRef: components["schemas"]["RefValue"];
    finding: FindingBinding;
    patchId: string;
    regressionSuiteArtifactId: string;
  },
): Promise<RecordSource<PatchRepairRequest>> {
  const item = input.approval.approval;
  const target = item.target_binding;
  if (target == null || target.subject_kind !== "patch" || item.evidence_set_artifact_id == null) {
    throw new Error("Failed Patch has no exact repair authority.");
  }
  return recordSource<PatchRepairRequest>(page, {
    endpoint: `/api/v1/patches/${encodeURIComponent(input.patchId)}:repair`,
    expectedStatus: "failed",
    label: `repair:${input.patchId}`,
    prospective: {
      cassette_artifact_id: null,
      execution_version_plan: null,
      llm_execution_mode: "record",
      params: {
        base_snapshot_artifact_id: input.baseArtifactId,
        candidate_export_profiles: [{ profile_id: "builtin.config_export", version: 1 }],
        checker_profiles: [],
        constraint_snapshot_artifact_id: input.constraintArtifactId,
        expected_subject_head_revision: item.subject_revision,
        expected_workflow_revision: item.workflow_revision,
        findings: [input.finding],
        preview_snapshot_artifact_id: target.target_artifact_id,
        regression_suite_artifact_ids: [input.regressionSuiteArtifactId],
        repair_policy: { profile_id: "builtin.patch_repair", version: 1 },
        schema_version: "patch-repair@1",
        simulation_profiles: [],
        subject_patch_artifact_id: input.patchId,
        target: { expected_ref: input.expectedRef, ref_name: refName },
        validation_evidence_artifact_id: item.evidence_set_artifact_id,
      },
      request_schema_version: "patch-repair-request@1",
      seed: 19,
    },
    resourceOperationId: "repair_patch_api_v1_patches__artifact_id__repair_post",
    runKind: { kind: "patch.repair", version: 1 },
  });
}

async function requiredHref(locator: Locator): Promise<string> {
  await expect(locator).toBeVisible();
  const href = await locator.getAttribute("href");
  if (!href) throw new Error("Expected a retained Journey A link.");
  return href;
}

async function openRunAndWait(page: Page, href: string, status: "failed" | "succeeded"): Promise<string> {
  await page.goto(href);
  await expect(page.getByText(new RegExp(`^run\\.${status} ·`, "u"))).toBeVisible({ timeout: 45_000 });
  const runId = decodeURIComponent(new URL(href, stack.baseURL).pathname.split("/").pop() ?? "");
  if (!runId) throw new Error("Run link has no Run ID.");
  return runId;
}

async function launchReviewReplay(
  page: Page,
  context: { constraintId: string; generationRunId: string; previewId: string },
  sourceRunId: string,
  includeChecker = true,
): Promise<string> {
  const search = new URLSearchParams({
    constraint: context.constraintId,
    snapshot: context.previewId,
    sourceRun: context.generationRunId,
  });
  await page.goto(`/reviews?${search.toString()}`);
  await expect(page.getByRole("heading", { name: "启动候选 Review" })).toBeVisible();
  await page.getByLabel("Review profile").selectOption("builtin.review@1");
  if (includeChecker) await page.getByRole("checkbox", { name: /builtin\.checker@1/u }).check();
  await page.getByRole("checkbox", { name: /builtin\.simulation@1/u }).check();
  await page.getByLabel("LLM triage profile").selectOption("builtin.llm_triage@1");
  await page.getByLabel("Seed").fill("1");
  await page.getByLabel("LLM execution mode").selectOption("replay");
  await page.getByLabel("Replay source Run").fill(sourceRunId);
  await page.getByRole("button", { name: "启动 Review" }).click();
  const href = await requiredHref(page.getByRole("link", { name: /^打开 Run /u }));
  return openRunAndWait(page, href, "succeeded");
}

async function deriveTaskSuite(
  page: Page,
  context: { configId: string; constraintId: string; previewId: string; sourceRunId: string },
): Promise<{ runId: string; suite: TaskSuiteView }> {
  const search = new URLSearchParams({
    action: "derive",
    config: context.configId,
    constraint: context.constraintId,
    preview: context.previewId,
    sourceRun: context.sourceRunId,
  });
  await page.goto(`/playtest?${search.toString()}`);
  const derive = page.getByRole("button", { name: "派生 exact TaskSuite" });
  await expect(derive).toBeEnabled();
  await derive.click();
  let deriveRunId = "";
  await expect
    .poll(() => {
      deriveRunId = new URL(page.url()).searchParams.get("deriveRun") ?? "";
      return deriveRunId;
    })
    .not.toBe("");
  const outcome = await successArtifacts(page, deriveRunId);
  const suiteId = outcome.primaryArtifactId;
  const choose = page.getByRole("button", { name: `选择新派生的 ${suiteId}` });
  await expect
    .poll(async () => new URL(page.url()).searchParams.get("suite") === suiteId || (await choose.isVisible()))
    .toBe(true);
  if (new URL(page.url()).searchParams.get("suite") !== suiteId) await choose.click();
  await expect(page).toHaveURL(new RegExp(`suite=${encodeURIComponent(suiteId)}`, "u"));
  const suite = (
    await sameOriginRequest<TaskSuiteView>(page, `/api/v1/task-suites/${encodeURIComponent(suiteId)}`)
  ).body;
  return { runId: deriveRunId, suite };
}

async function launchPlaytestReplay(
  page: Page,
  input: {
    configId: string;
    constraintId: string;
    maxSteps: number;
    previewId: string;
    sourceRunId: string;
    suiteId: string;
  },
): Promise<string> {
  const search = new URLSearchParams({
    config: input.configId,
    constraint: input.constraintId,
    preview: input.previewId,
    sourceRun: input.sourceRunId,
    suite: input.suiteId,
  });
  await page.goto(`/playtest?${search.toString()}`);
  const launch = page.getByRole("region", { name: "Playtest launch docket" });
  await expect(launch).toBeVisible();
  await launch.getByLabel("Planner profile").selectOption("builtin.playtest_planner@2");
  await launch.getByLabel("LLM execution mode").selectOption("replay");
  await launch.getByLabel("Seed").fill("1");
  await launch.getByLabel("每 episode 最大步数").fill(String(input.maxSteps));
  await launch.getByLabel("Replay source Run").fill(input.sourceRunId);
  await launch.getByRole("button", { name: "解析并启动 Playtest" }).click();
  let runId = "";
  await expect
    .poll(() => {
      runId = new URL(page.url()).searchParams.get("run") ?? "";
      return runId;
    })
    .not.toBe("");
  await waitForRun(page, runId);
  return runId;
}

async function validatePatch(
  page: Page,
  input: {
    configId: string;
    constraintId: string;
    expectedFindings: FindingBinding[];
    findings: FindingBinding[];
    patchId: string;
    regressionSuiteId: string;
    reviewId: string;
    traceId: string;
  },
): Promise<string> {
  await page.goto(`/patches/${encodeURIComponent(input.patchId)}`);
  await page.getByLabel("Validation policy").selectOption("builtin.validation@1");
  await page.getByLabel(/ConstraintSnapshot Artifact ID/u).fill(input.constraintId);
  await page.getByLabel(/Candidate ConfigExport Artifact IDs/u).fill(input.configId);
  await page.getByLabel(/Review Artifact IDs/u).fill(input.reviewId);
  await page.getByLabel(/PlaytestTrace Artifact IDs/u).fill(input.traceId);
  await page.getByLabel(/RegressionSuite Artifact IDs/u).fill(input.regressionSuiteId);
  await page
    .getByLabel(/Expected historical FindingEvidenceBindingV1/u)
    .fill(JSON.stringify(input.expectedFindings));
  await page.getByLabel(/Observed \/ repair FindingEvidenceBindingV1/u).fill(JSON.stringify(input.findings));
  await page.getByLabel("Seed").fill("17");
  const validate = page.getByRole("button", { name: "启动 exact validation" });
  await expect(validate).toBeEnabled();
  await validate.click();
  return requiredHref(page.getByRole("link", { name: "打开 accepted Run" }));
}

async function exactValidationRequest(
  page: Page,
  input: {
    baseArtifactId: string;
    configId: string;
    constraintId: string;
    expectedFindings: FindingBinding[];
    findings: FindingBinding[];
    patchId: string;
    regressionSuiteId: string;
    reviewId: string;
    traceId: string;
  },
): Promise<PatchValidationRequest> {
  const authority = await getPatchApproval(page, input.patchId);
  const item = authority.approval.approval;
  const target = item.target_binding;
  if (target == null || target.subject_kind !== "patch") {
    throw new Error(`Patch ${input.patchId} has no exact validation target.`);
  }
  return {
    approval_id: item.approval_id,
    base_snapshot_artifact_id: input.baseArtifactId,
    candidate_config_export_artifact_ids: [input.configId],
    checker_profiles: [],
    constraint_snapshot_artifact_id: input.constraintId,
    expected_findings: input.expectedFindings,
    expected_subject_head_revision: item.subject_revision,
    expected_workflow_revision: item.workflow_revision,
    findings: input.findings,
    playtest_trace_artifact_ids: [input.traceId],
    preview_snapshot_artifact_id: target.target_artifact_id,
    regression_suite_artifact_ids: [input.regressionSuiteId],
    request_schema_version: "patch-validation-admission-request@1",
    review_artifact_ids: [input.reviewId],
    seed: 17,
    simulation_profiles: [],
    subject_digest: item.subject_digest,
    target: { expected_ref: target.expected_ref, ref_name: target.ref_name },
    validation_policy: { profile_id: "builtin.validation", version: 1 },
  };
}

async function repairPatchReplay(
  page: Page,
  input: {
    constraintId: string;
    finding: FindingBinding;
    patchId: string;
    regressionSuiteId: string;
    sourceRunId: string;
  },
): Promise<string> {
  await page.goto(`/patches/${encodeURIComponent(input.patchId)}`);
  await page.getByLabel("Repair policy").selectOption("builtin.patch_repair@1");
  await page.getByRole("group", { name: "Repair candidate export profiles" }).getByRole("checkbox").check();
  await page.getByLabel(/ConstraintSnapshot Artifact ID/u).fill(input.constraintId);
  await page.getByLabel(/RegressionSuite Artifact IDs/u).fill(input.regressionSuiteId);
  await page.getByLabel(/Observed \/ repair FindingEvidenceBindingV1/u).fill(JSON.stringify([input.finding]));
  await page.getByLabel("Seed").fill("19");
  await page.getByLabel("Repair LLM mode").selectOption("replay");
  await page.getByLabel("Replay source Run").fill(input.sourceRunId);
  const repair = page.getByRole("button", { name: "Resolve 并启动 repair" });
  await expect(repair).toBeEnabled();
  await repair.click();
  const href = await requiredHref(page.getByRole("link", { name: "打开 accepted Run" }));
  return openRunAndWait(page, href, "succeeded");
}

async function submitPatch(page: Page, patchId: string): Promise<string> {
  await page.goto(`/patches/${encodeURIComponent(patchId)}`);
  const submit = page.getByRole("button", { name: "Submit for independent approval" });
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(page.getByText("pending_approval", { exact: true }).first()).toBeVisible();
  return requiredHref(page.getByRole("link", { name: "打开审批详情" }));
}

async function approvePatch(page: Page, approvalHref: string): Promise<void> {
  await page.goto(approvalHref);
  const requirementTable = page.getByRole("table", { name: "Requirement progress" });
  await expect(requirementTable).toBeVisible();
  const requirements = requirementTable.getByRole("checkbox", { name: /^选择 /u });
  const count = await requirements.count();
  expect(count).toBeGreaterThan(0);
  for (let index = 0; index < count; index += 1) {
    await expect(requirements.nth(index)).toBeEnabled();
    await requirements.nth(index).check();
  }
  await page.getByLabel("决定原因代码").fill("journey_a_independent_review");
  await page.getByRole("button", { name: "提交批准" }).click();
  await expect(page.getByText(/^approved · workflow revision \d+$/u)).toBeVisible();
}

async function applyPatch(page: Page, patchId: string): Promise<void> {
  await page.goto(`/patches/${encodeURIComponent(patchId)}`);
  const apply = page.getByRole("button", { name: "Apply approved Patch" });
  await expect(apply).toBeEnabled();
  await apply.click();
  await page.getByRole("button", { name: "确认 Apply" }).click();
  await expect(page.getByRole("heading", { name: "Patch 已通过 ref transition 应用" })).toBeVisible();
}

interface JourneyADemoInput {
  failedPlaytestHref: string;
  failedReviewId: string;
  generationRunId: string;
  manifest: JourneyAManifest;
  patchId: string;
  previewId: string;
  repairedPatchId: string;
  repairedPlaytestHref: string;
}

function requiredDemoScene(key: string): DemoScene {
  const scene = DEMO_SCENES.find((candidate) => candidate.key === key);
  if (scene === undefined) throw new Error(`Unknown Journey A demo scene: ${key}`);
  return scene;
}

async function scrollDemoTarget(page: Page, target: Locator): Promise<void> {
  await expect(target).toBeVisible({ timeout: 30_000 });
  await target.evaluate((element) => {
    element.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
  });
  await page.waitForTimeout(650);
}

async function renderDemoOverlay(page: Page, scene: DemoScene): Promise<void> {
  const index = DEMO_SCENES.findIndex((candidate) => candidate.key === scene.key);
  await page.evaluate(
    ({ index, provenance, scene, total }) => {
      document.getElementById("gf-demo-v1-overlay")?.remove();
      document.getElementById("gf-demo-v1-style")?.remove();

      const style = document.createElement("style");
      style.id = "gf-demo-v1-style";
      style.textContent = `
        #gf-demo-v1-overlay {
          color: #f1f4f1;
          font-family: "GameForge Editorial Serif", "Source Han Serif SC", "Songti SC", serif;
          inset: 0;
          pointer-events: none;
          position: fixed;
          z-index: 2147483000;
        }
        #gf-demo-v1-overlay * { box-sizing: border-box; }
        #gf-demo-v1-overlay .gf-demo-v1__provenance {
          align-items: center;
          backdrop-filter: blur(14px);
          background: rgba(16, 19, 17, 0.86);
          border: 1px solid rgba(105, 189, 181, 0.38);
          border-radius: 999px;
          box-shadow: 0 10px 30px rgba(0, 0, 0, 0.22);
          color: #a6ded8;
          display: flex;
          font-family: "SF Mono", ui-monospace, monospace;
          bottom: 18px;
          font-size: 9px;
          gap: 8px;
          left: auto;
          letter-spacing: 0.06em;
          line-height: 1.4;
          max-width: none;
          padding: 8px 12px;
          position: absolute;
          right: 22px;
          text-transform: uppercase;
          z-index: 2;
        }
        #gf-demo-v1-overlay .gf-demo-v1__dot {
          background: #69bdb5;
          border-radius: 50%;
          box-shadow: 0 0 0 4px rgba(105, 189, 181, 0.13);
          height: 6px;
          width: 6px;
        }
        #gf-demo-v1-overlay .gf-demo-v1__scrim {
          background: linear-gradient(180deg, transparent 0%, rgba(9, 12, 10, 0.22) 18%, rgba(9, 12, 10, 0.95) 100%);
          bottom: 0;
          height: 228px;
          left: 0;
          position: absolute;
          right: 0;
        }
        #gf-demo-v1-overlay .gf-demo-v1__caption {
          bottom: 31px;
          left: 34px;
          max-width: 820px;
          position: absolute;
        }
        #gf-demo-v1-overlay .gf-demo-v1__kicker {
          color: #69bdb5;
          font-family: "SF Mono", ui-monospace, monospace;
          font-size: 11px;
          letter-spacing: 0.13em;
          margin: 0 0 8px;
          text-transform: uppercase;
        }
        #gf-demo-v1-overlay .gf-demo-v1__title {
          color: #f7faf7;
          font-size: 30px;
          font-weight: 600;
          letter-spacing: -0.02em;
          line-height: 1.08;
          margin: 0;
          text-wrap: balance;
        }
        #gf-demo-v1-overlay .gf-demo-v1__body {
          color: #c8cec9;
          font-size: 15px;
          line-height: 1.45;
          margin: 9px 0 0;
          max-width: 730px;
        }
        #gf-demo-v1-overlay .gf-demo-v1__progress {
          background: rgba(255, 255, 255, 0.12);
          bottom: 0;
          height: 2px;
          left: 0;
          position: absolute;
          right: 0;
        }
        #gf-demo-v1-overlay .gf-demo-v1__progress > span {
          background: linear-gradient(90deg, #377f79, #a6ded8);
          display: block;
          height: 100%;
        }
        #gf-demo-v1-overlay.gf-demo-v1--hero {
          background:
            radial-gradient(circle at 78% 18%, rgba(105, 189, 181, 0.18), transparent 30%),
            radial-gradient(circle at 22% 86%, rgba(148, 168, 237, 0.11), transparent 34%),
            linear-gradient(135deg, #101311 0%, #171c18 54%, #0c0f0d 100%);
        }
        #gf-demo-v1-overlay.gf-demo-v1--hero::after {
          border: 1px solid rgba(105, 189, 181, 0.14);
          content: "";
          inset: 32px;
          position: absolute;
        }
        #gf-demo-v1-overlay.gf-demo-v1--hero .gf-demo-v1__provenance {
          bottom: auto;
          font-size: 10px;
          left: auto;
          max-width: none;
          right: 22px;
          top: 18px;
        }
        #gf-demo-v1-overlay.gf-demo-v1--hero .gf-demo-v1__caption {
          bottom: auto;
          left: 126px;
          max-width: 830px;
          top: 50%;
          transform: translateY(-52%);
        }
        #gf-demo-v1-overlay.gf-demo-v1--hero .gf-demo-v1__kicker {
          margin-bottom: 18px;
        }
        #gf-demo-v1-overlay.gf-demo-v1--hero .gf-demo-v1__title {
          font-size: 69px;
          letter-spacing: -0.035em;
          line-height: 0.98;
        }
        #gf-demo-v1-overlay.gf-demo-v1--hero .gf-demo-v1__body {
          color: #adb5ae;
          font-size: 19px;
          margin-top: 20px;
        }
        #gf-demo-v1-overlay.gf-demo-v1--hero .gf-demo-v1__rule {
          background: #69bdb5;
          height: 2px;
          margin-bottom: 24px;
          width: 54px;
        }
      `;
      document.head.append(style);

      const overlay = document.createElement("div");
      overlay.id = "gf-demo-v1-overlay";
      if (scene.variant === "hero") overlay.classList.add("gf-demo-v1--hero");

      const provenanceBadge = document.createElement("div");
      provenanceBadge.className = "gf-demo-v1__provenance";
      const dot = document.createElement("span");
      dot.className = "gf-demo-v1__dot";
      const provenanceCopy = document.createElement("span");
      provenanceCopy.textContent = provenance;
      provenanceBadge.append(dot, provenanceCopy);

      const caption = document.createElement("section");
      caption.className = "gf-demo-v1__caption";
      const rule = document.createElement("div");
      rule.className = "gf-demo-v1__rule";
      const kicker = document.createElement("p");
      kicker.className = "gf-demo-v1__kicker";
      kicker.textContent = scene.kicker;
      const title = document.createElement("h2");
      title.className = "gf-demo-v1__title";
      title.textContent = scene.title;
      const body = document.createElement("p");
      body.className = "gf-demo-v1__body";
      body.textContent = scene.body;
      caption.append(rule, kicker, title, body);

      overlay.append(provenanceBadge);
      if (scene.variant !== "hero") {
        const scrim = document.createElement("div");
        scrim.className = "gf-demo-v1__scrim";
        overlay.append(scrim);
      }
      overlay.append(caption);

      const progress = document.createElement("div");
      progress.className = "gf-demo-v1__progress";
      const progressValue = document.createElement("span");
      progressValue.style.width = `${((index + 1) / total) * 100}%`;
      progress.append(progressValue);
      overlay.append(progress);
      document.body.append(overlay);

      overlay.animate([{ opacity: 0 }, { opacity: 1 }], {
        duration: 360,
        easing: "cubic-bezier(.2,.8,.2,1)",
        fill: "forwards",
      });
      caption.animate(
        [
          { opacity: 0, transform: scene.variant === "hero" ? "translateY(-45%)" : "translateY(14px)" },
          { opacity: 1, transform: scene.variant === "hero" ? "translateY(-52%)" : "translateY(0)" },
        ],
        { duration: 520, easing: "cubic-bezier(.2,.8,.2,1)", fill: "forwards" },
      );
    },
    { index, provenance: DEMO_PROVENANCE_LABEL, scene, total: DEMO_SCENES.length },
  );
}

async function showDemoScene(
  page: Page,
  key: string,
  options: {
    capturePath?: string;
    capturePosition?: "primary" | "secondary";
    secondaryTarget?: Locator;
    target?: Locator;
  } = {},
): Promise<void> {
  const scene = requiredDemoScene(key);
  if (options.target) await scrollDemoTarget(page, options.target);
  await renderDemoOverlay(page, scene);
  if (options.capturePath && options.capturePosition !== "secondary") {
    await page.screenshot({ animations: "disabled", path: options.capturePath });
  }

  if (options.secondaryTarget) {
    const firstHold = Math.floor(scene.holdMs / 2);
    await page.waitForTimeout(firstHold);
    const beforeScroll = Date.now();
    await scrollDemoTarget(page, options.secondaryTarget);
    if (options.capturePath && options.capturePosition === "secondary") {
      await page.screenshot({ animations: "disabled", path: options.capturePath });
    }
    const scrollDuration = Date.now() - beforeScroll;
    await page.waitForTimeout(Math.max(0, scene.holdMs - firstHold - scrollDuration));
    return;
  }
  if (options.capturePath && options.capturePosition === "secondary") {
    throw new Error(`README demo frame ${key} requires a secondary target.`);
  }
  await page.waitForTimeout(scene.holdMs);
}

async function captureDemoSourceFrame(page: Page, filename: string, target: Locator): Promise<void> {
  if (process.env.GAMEFORGE_RECORD_DEMO !== "1") return;
  await scrollDemoTarget(page, target);
  const outputDir = demoOutputDirectory();
  await mkdir(outputDir, { recursive: true });
  await page.screenshot({ animations: "disabled", path: join(outputDir, filename) });
}

async function openDemoSourceFrame(page: Page, path: string, alt: string): Promise<void> {
  const source = await readFile(path);
  await page.setContent(`
    <style>
      html, body { background: #101311; height: 100%; margin: 0; overflow: hidden; }
      img { display: block; height: 100vh; object-fit: cover; width: 100vw; }
    </style>
    <img alt="${alt}" src="data:image/png;base64,${source.toString("base64")}" />
  `);
  await expect(page.getByRole("img", { name: alt })).toBeVisible();
}

function readmeFrameCapture(
  directory: string,
  sceneKey: string,
): { capturePath: string; capturePosition: "primary" | "secondary" } {
  const frame = DEMO_README_FRAMES.find((candidate) => candidate.sceneKey === sceneKey);
  if (frame === undefined) throw new Error(`Unknown README demo frame: ${sceneKey}`);
  return {
    capturePath: join(directory, frame.filename),
    capturePosition: frame.capturePosition,
  };
}

async function openDemoPage(page: Page, href: string, ready: Locator): Promise<void> {
  await page.goto(href);
  await expect(ready).toBeVisible({ timeout: 45_000 });
  await page.evaluate(async () => {
    window.scrollTo({ top: 0 });
    await document.fonts.ready;
  });
  await page.waitForTimeout(220);
}

async function authenticateDemoContext(context: BrowserContext): Promise<string> {
  const response = await context.request.post(`${stack.baseURL}/api/v1/auth/login`, {
    data: {
      login_name: makerCredentials.login,
      password: makerCredentials.password,
      schema_version: "password-auth@1",
    },
  });
  expect(response.ok()).toBe(true);
  const csrfToken = response.headers()["x-csrf-token"];
  expect(csrfToken).toBeTruthy();
  return csrfToken;
}

async function recordJourneyADemo(
  browser: Browser,
  input: JourneyADemoInput,
  unexpected: Set<string>,
): Promise<void> {
  const outputDir = demoOutputDirectory();
  const rawDir = join(outputDir, "raw-complete-workflow-zh");
  const readmeFramesDir = join(outputDir, "readme-frames-complete-workflow-zh");
  const outputPath = join(outputDir, "gameforge-complete-workflow-zh.webm");
  const coverPath = join(outputDir, "gameforge-complete-workflow-zh-cover.png");
  await Promise.all([mkdir(rawDir, { recursive: true }), mkdir(readmeFramesDir, { recursive: true })]);

  const demoContext = await browser.newContext({
    baseURL: stack.baseURL,
    colorScheme: "dark",
    deviceScaleFactor: 1,
    ignoreHTTPSErrors: true,
    recordVideo: { dir: rawDir, size: { height: 720, width: 1280 } },
    reducedMotion: "no-preference",
    viewport: { height: 720, width: 1280 },
  });
  await demoContext.addInitScript(() => {
    window.localStorage.setItem("gameforge.theme", "dark");
    document.documentElement.dataset.theme = "dark";
  });
  await guardAuthoringEgress(demoContext, stack.baseURL, unexpected);
  await authenticateDemoContext(demoContext);

  const demoPage = await demoContext.newPage();
  const startedAt = Date.now();
  const video = demoPage.video();
  if (video === null) throw new Error("Journey A demo context did not start video recording.");

  try {
    await openDemoSourceFrame(
      demoPage,
      join(outputDir, "authoring-input-source.png"),
      "真实提交前的内容生成输入",
    );
    await showDemoScene(demoPage, "intro");
    await showDemoScene(demoPage, "input", {
      ...readmeFrameCapture(readmeFramesDir, "input"),
    });

    await openDemoPage(
      demoPage,
      `/generation?run=${encodeURIComponent(input.generationRunId)}`,
      demoPage.getByRole("heading", { name: "generation_gate_passed" }),
    );
    await showDemoScene(demoPage, "generation", {
      ...readmeFrameCapture(readmeFramesDir, "generation"),
      secondaryTarget: demoPage.getByRole("heading", { name: "Patch → preview → config" }),
      target: demoPage.getByRole("heading", { name: "Preliminary gate" }),
    });

    await openDemoPage(
      demoPage,
      `/patches/${encodeURIComponent(input.patchId)}`,
      demoPage.getByRole("heading", { level: 1, name: /Patch revision/u }),
    );
    await showDemoScene(demoPage, "candidate-diff", {
      ...readmeFrameCapture(readmeFramesDir, "candidate-diff"),
      target: demoPage.getByRole("heading", { name: "字段级 Diff" }),
    });

    const failedReviewSearch = new URLSearchParams({
      constraint: input.manifest.constraint_artifact_id,
      snapshot: input.previewId,
      sourceRun: input.generationRunId,
    });
    await openDemoPage(
      demoPage,
      `/reviews/${encodeURIComponent(input.failedReviewId)}?${failedReviewSearch.toString()}`,
      demoPage.getByRole("heading", { level: 1, name: "Review Report" }),
    );
    await showDemoScene(demoPage, "review", {
      ...readmeFrameCapture(readmeFramesDir, "review"),
      secondaryTarget: demoPage.getByRole("heading", { name: "Exact authority ledger" }),
      target: demoPage.getByRole("list", { name: "Finding 分区计数" }),
    });

    await openDemoPage(
      demoPage,
      input.failedPlaytestHref,
      demoPage.getByRole("heading", { name: "Run 已完成，任务未全部通过" }),
    );
    await showDemoScene(demoPage, "failed-playtest", {
      ...readmeFrameCapture(readmeFramesDir, "failed-playtest"),
      secondaryTarget: demoPage.getByRole("heading", { name: "Episode 结果与轨迹" }),
      target: demoPage.getByRole("heading", { name: "Run 已完成，任务未全部通过" }),
    });

    await openDemoSourceFrame(
      demoPage,
      join(outputDir, "failed-validation-source.png"),
      "验证失败且正式版本未移动",
    );
    await showDemoScene(demoPage, "failed-validation", {
      ...readmeFrameCapture(readmeFramesDir, "failed-validation"),
    });

    await openDemoSourceFrame(
      demoPage,
      join(outputDir, "repair-draft-source.png"),
      "新建且尚未验证的修复版本",
    );
    await showDemoScene(demoPage, "repair", {
      ...readmeFrameCapture(readmeFramesDir, "repair"),
    });

    await openDemoPage(
      demoPage,
      input.repairedPlaytestHref,
      demoPage.getByRole("heading", { name: "Run 已完成，全部任务通过" }),
    );
    await showDemoScene(demoPage, "passed-playtest", {
      ...readmeFrameCapture(readmeFramesDir, "passed-playtest"),
      secondaryTarget: demoPage.getByRole("heading", { name: "Episode 结果与轨迹" }),
      target: demoPage.getByRole("heading", { name: "Run 已完成，全部任务通过" }),
    });

    await demoContext.clearCookies();
    await authenticateDemoContext(demoContext);
    await openDemoSourceFrame(
      demoPage,
      join(outputDir, "approved-before-apply-source.png"),
      "已批准但尚未应用的精确版本",
    );
    await showDemoScene(demoPage, "approval", {
      ...readmeFrameCapture(readmeFramesDir, "approval"),
    });

    await openDemoPage(
      demoPage,
      `/refs/${encodeURIComponent(refName)}/history`,
      demoPage.getByRole("heading", { level: 1, name: refName }),
    );
    await showDemoScene(demoPage, "apply", {
      ...readmeFrameCapture(readmeFramesDir, "apply"),
      target: demoPage.getByRole("heading", { level: 1, name: refName }),
    });

    await showDemoScene(demoPage, "outro");
    const elapsed = Date.now() - startedAt;
    if (elapsed < DEMO_TARGET_DURATION_MS) {
      await demoPage.waitForTimeout(DEMO_TARGET_DURATION_MS - elapsed);
    }
    await demoPage.screenshot({ path: coverPath });
  } finally {
    await demoContext.close();
  }
  await video.saveAs(outputPath);
}

test.describe("journey-a-authoring", () => {
  test.describe.configure({ mode: "serial" });
  test.setTimeout(600_000);

  test.beforeAll(async () => {
    stack = await startAuthoringStack({
      launcherModule: "tests.e2e.m4d_support.journey_a_live",
      manifestName: "journey-a-manifest.json",
      transportLogName: "journey-a-transport.log",
      workspacePrefix: "gameforge-journey-a-",
    });
  });

  test.afterAll(async () => {
    await stack.stop();
  });

  test("proves generation, review, playtest, repair, approval, and exact apply over real authority", async ({
    browser,
  }) => {
    const manifest = await stack.readManifest<JourneyAManifest>();
    const unexpected = new Set<string>();
    const makerContext: BrowserContext = await browser.newContext({
      baseURL: stack.baseURL,
      ignoreHTTPSErrors: true,
    });
    const approverContext: BrowserContext = await browser.newContext({
      baseURL: stack.baseURL,
      ignoreHTTPSErrors: true,
    });
    await guardAuthoringEgress(makerContext, stack.baseURL, unexpected);
    await guardAuthoringEgress(approverContext, stack.baseURL, unexpected);
    const makerPage = await makerContext.newPage();
    const approverPage = await approverContext.newPage();

    try {
      await loginAuthoringPage(makerPage, makerCredentials);
      await loginAuthoringPage(approverPage, approverCredentials);
      expect(await refHistory(makerPage)).toEqual([
        { entry_schema_version: "ref-history-entry@1", ref_name: refName, value: manifest.expected_ref },
      ]);

      let generationRunId = "";
      let patchId = "";
      let previewId = "";
      let configId = "";
      let rejectedPatchId = "";
      let rejectedApprovalId = "";

      await test.step("generation_gate_rejected retains evidence but no workflow authority", async () => {
        const before = await transportLines();
        const runId = await selectGenerationReplay(makerPage, manifest, {
          goal: "Propose a deliberately dangling generation candidate.",
          sourceRunId: manifest.gate_rejected_source_run_id,
        });
        await expect(makerPage.getByRole("heading", { name: "generation_gate_rejected" })).toBeVisible({
          timeout: 45_000,
        });
        const preliminaryGate = makerPage.getByRole("heading", { name: "Preliminary gate" });
        await expect(preliminaryGate).toBeVisible();
        await expect(preliminaryGate.locator("xpath=ancestor::section[1]")).toHaveAttribute(
          "data-state",
          "error",
        );
        await expect(makerPage.getByRole("heading", { name: "Rejected Patch + preview" })).toBeVisible();
        await assertReplayDidNotCallTransport(before);

        const terminal = await terminalManifest(makerPage, runId);
        expect(terminal.run.status).toBe("failed");
        expect(terminal.artifact.payload).toMatchObject({
          cause_code: "generation_gate_rejected",
          retryable: false,
        });
        const evidence = await artifactsForManifestRole(makerPage, terminal.artifact.payload, "evidence");
        const kinds = new Set(evidence.map((item) => item.artifact.kind));
        expect(kinds.has("patch")).toBe(true);
        expect(kinds.has("ir_snapshot")).toBe(true);
        expect(kinds.has("checker_run")).toBe(true);
        expect(kinds.has("review_report")).toBe(true);
        expect(kinds.has("config_export")).toBe(false);
        const rejectedPatch = evidence.find((item) => item.artifact.kind === "patch");
        const rejectedPreview = evidence.find((item) => item.artifact.kind === "ir_snapshot");
        expect(rejectedPatch).toBeDefined();
        expect(rejectedPreview).toBeDefined();
        rejectedPatchId = rejectedPatch!.artifact.artifact_id;
        rejectedApprovalId = `approval:patch:${rejectedPatchId}`;
        await sameOriginRequest(makerPage, `/api/v1/patches/${encodeURIComponent(rejectedPatchId)}`, {
          expected: [404],
        });
        await sameOriginRequest(
          makerPage,
          `/api/v1/workflow-subjects/${encodeURIComponent(rejectedPatchId)}/approval-binding`,
          { expected: [404] },
        );
        await sameOriginRequest(makerPage, `/api/v1/approvals/${encodeURIComponent(rejectedApprovalId)}`, {
          expected: [404],
        });
        const rejectedRefHistory = await refHistory(makerPage);
        expect(rejectedRefHistory).toEqual([
          { entry_schema_version: "ref-history-entry@1", ref_name: refName, value: manifest.expected_ref },
        ]);
        const rejectedRunIds = await runIds(makerPage);
        const rejectedApprovalIds = await approvalIds(makerPage);
        const rejectedTransport = await transportLines();
        const rejectedRebase = await sameOriginRequest<components["schemas"]["Problem"]>(
          makerPage,
          `/api/v1/patches/${encodeURIComponent(rejectedPatchId)}:rebase`,
          {
            body: {
              approval_id: rejectedApprovalId,
              expected_ref: manifest.expected_ref,
              expected_subject_head_revision: 1,
              expected_workflow_revision: 1,
              ref_name: refName,
              request_schema_version: "patch-rebase-request@1",
            },
            expected: [409],
            headers: {
              ...mutationHeaders("gate-rejected-rebase"),
              "If-Match": resourceEtag("patch", rejectedPatchId, 1),
            },
            method: "POST",
            withCsrf: true,
          },
        );
        expect(rejectedRebase.body.code).toBe("revision_conflict");
        const rejectedResolve = await sameOriginRequest<components["schemas"]["Problem"]>(
          makerPage,
          `/api/v1/patches/${encodeURIComponent(rejectedPatchId)}:resolve-conflicts`,
          {
            body: {
              approval_id: rejectedApprovalId,
              conflict_set_id: "conflict-set:gate-rejected",
              expected_ref: manifest.expected_ref,
              expected_subject_head_revision: 1,
              expected_workflow_revision: 1,
              ref_name: refName,
              request_schema_version: "resolve-conflicts-request@1",
              resolutions: [
                {
                  choice: "keep_current",
                  conflict_id: "conflict:gate-rejected",
                },
              ],
            },
            expected: [409],
            headers: {
              ...mutationHeaders("gate-rejected-resolve"),
              "If-Match": resourceEtag("patch", rejectedPatchId, 1),
            },
            method: "POST",
            withCsrf: true,
          },
        );
        expect(rejectedResolve.body.code).toBe("revision_conflict");
        const rejectedSubmit = await sameOriginRequest<components["schemas"]["Problem"]>(
          makerPage,
          `/api/v1/patches/${encodeURIComponent(rejectedPatchId)}:submit-for-approval`,
          {
            body: {
              approval_id: rejectedApprovalId,
              expected_workflow_revision: 1,
              request_schema_version: "submit-for-approval-request@1",
            },
            expected: [409],
            headers: {
              ...mutationHeaders("gate-rejected-submit"),
              "If-Match": resourceEtag("patch", rejectedPatchId, 1),
            },
            method: "POST",
            withCsrf: true,
          },
        );
        expect(rejectedSubmit.body.code).toBe("revision_conflict");
        const rejectedApply = await sameOriginRequest<components["schemas"]["Problem"]>(
          approverPage,
          `/api/v1/patches/${encodeURIComponent(rejectedPatchId)}:apply`,
          {
            body: {
              approval_id: rejectedApprovalId,
              expected_ref: manifest.expected_ref,
              expected_workflow_revision: 1,
              ref_name: refName,
              request_schema_version: "workflow-apply-request@1",
              subject_digest: rejectedPatch!.artifact.payload_hash,
              target_artifact_id: rejectedPreview!.artifact.artifact_id,
              target_digest: rejectedPreview!.artifact.payload_hash,
            },
            expected: [409],
            headers: {
              ...mutationHeaders("gate-rejected-apply"),
              "If-Match": resourceEtag("patch", rejectedPatchId, 1),
            },
            method: "POST",
            withCsrf: true,
          },
        );
        expect(rejectedApply.body.code).toBe("revision_conflict");
        await sameOriginRequest(makerPage, `/api/v1/patches/${encodeURIComponent(rejectedPatchId)}`, {
          expected: [404],
        });
        await sameOriginRequest(
          makerPage,
          `/api/v1/workflow-subjects/${encodeURIComponent(rejectedPatchId)}/approval-binding`,
          { expected: [404] },
        );
        await sameOriginRequest(makerPage, `/api/v1/approvals/${encodeURIComponent(rejectedApprovalId)}`, {
          expected: [404],
        });
        expect(await runIds(makerPage)).toEqual(rejectedRunIds);
        expect(await approvalIds(makerPage)).toEqual(rejectedApprovalIds);
        expect(await transportLines()).toEqual(rejectedTransport);
        expect(await refHistory(makerPage)).toEqual(rejectedRefHistory);
      });

      await test.step("generation gate pass publishes a non-empty candidate config without moving ref", async () => {
        const before = await transportLines();
        generationRunId = await selectGenerationReplay(makerPage, manifest, {
          goal: DEMO_AUTHORING_GOAL,
          sourceRunId: manifest.generation_source_run_id,
        });
        await expect(makerPage.getByRole("heading", { name: "generation_gate_passed" })).toBeVisible({
          timeout: 45_000,
        });
        const preliminaryGate = makerPage.getByRole("heading", { name: "Preliminary gate" });
        await expect(preliminaryGate).toBeVisible();
        await expect(preliminaryGate.locator("xpath=ancestor::section[1]")).toHaveAttribute(
          "data-state",
          "terminal",
        );
        await expect(makerPage.getByRole("heading", { name: "Patch → preview → config" })).toBeVisible();
        await expect(makerPage.getByText("Config export", { exact: true })).toBeVisible();
        await assertReplayDidNotCallTransport(before);

        const outcome = await successArtifacts(makerPage, generationRunId);
        patchId = outcome.primaryArtifactId;
        previewId = singleArtifactOfKind(outcome.outputs, "ir_snapshot").artifact.artifact_id;
        configId = singleArtifactOfKind(outcome.outputs, "config_export").artifact.artifact_id;
        expect(patchId).not.toBe(manifest.record_patch_artifact_id);
        expect(await refHistory(makerPage)).toHaveLength(1);
      });

      let oldSuite: TaskSuiteView | null = null;
      let oldPlaytestRecord: RecordSource<PlaytestRunRequest> | null = null;
      let failedPlaytestHref = "";
      let failedTraceId = "";
      let failedFinding: FindingBinding | null = null;
      let regressionSuiteId = "";
      await test.step("derive the failed candidate exact TaskSuite before Review", async () => {
        oldSuite = (
          await deriveTaskSuite(makerPage, {
            configId,
            constraintId: manifest.constraint_artifact_id,
            previewId,
            sourceRunId: generationRunId,
          })
        ).suite;
      });

      let failedReviewId = "";
      await test.step("fixture-only Review RECORD feeds a distinct product REPLAY", async () => {
        const source = await recordReviewSource(makerPage, previewId, manifest.constraint_artifact_id);
        const beforeReplay = await transportLines();
        const reviewRunId = await launchReviewReplay(
          makerPage,
          {
            constraintId: manifest.constraint_artifact_id,
            generationRunId,
            previewId,
          },
          source.run.run_id,
        );
        await assertReplayDidNotCallTransport(beforeReplay);
        const outcome = await successArtifacts(makerPage, reviewRunId);
        failedReviewId = outcome.primaryArtifactId;
        await makerPage.goto(
          `/reviews/${encodeURIComponent(failedReviewId)}?sourceRun=${encodeURIComponent(reviewRunId)}&snapshot=${encodeURIComponent(previewId)}`,
        );
        await expect(makerPage.getByRole("heading", { level: 1, name: "Review Report" })).toBeVisible();
        await expect(makerPage.getByRole("list", { name: "Finding 分区计数" })).toContainText("确定性");
        await expect(makerPage.getByRole("list", { name: "Finding 分区计数" })).toContainText("LLM 建议");
        await expect(makerPage.getByRole("heading", { name: "Exact authority ledger" })).toBeVisible();
        await expect(
          makerPage.getByText("Run-scoped immutable links + digest + evidence Artifact", { exact: true }),
        ).toBeVisible();
      });

      await test.step("run an actual incomplete Playtest product REPLAY", async () => {
        if (oldSuite === null) throw new Error("Failed candidate TaskSuite was not retained.");
        const suite = oldSuite;
        const maxSteps = Math.min(7, ...suite.task_suite.episodes.map((episode) => episode.step_budget));
        oldPlaytestRecord = await recordPlaytestSource(makerPage, suite, maxSteps);
        const beforeReplay = await transportLines();
        const playtestRunId = await launchPlaytestReplay(makerPage, {
          configId,
          constraintId: manifest.constraint_artifact_id,
          maxSteps,
          previewId,
          sourceRunId: oldPlaytestRecord.run.run_id,
          suiteId: suite.artifact.artifact_id,
        });
        await expect(makerPage.getByRole("heading", { name: "Run 已完成，任务未全部通过" })).toBeVisible({
          timeout: 45_000,
        });
        failedPlaytestHref = makerPage.url();
        await assertReplayDidNotCallTransport(beforeReplay);

        const outcome = await successArtifacts(makerPage, playtestRunId);
        const trace = await artifact(makerPage, outcome.primaryArtifactId);
        expect(trace.artifact.kind).toBe("playtest_trace");
        const episodes = isRecord(trace.payload) ? trace.payload.episodes : null;
        expect(
          Array.isArray(episodes) && episodes.some((episode) => isRecord(episode) && !episode.completed),
        ).toBe(true);
        const authorities = await findingAuthorities(makerPage, playtestRunId);
        expect(authorities).toHaveLength(1);
        failedTraceId = trace.artifact.artifact_id;
        failedFinding = authorities[0].binding;
        const exactFindingJson = await sameOriginText(
          makerPage,
          `/api/v1/findings/${encodeURIComponent(authorities[0].binding.finding_id)}/revisions/${authorities[0].binding.finding_revision}`,
        );
        regressionSuiteId = await materializeRegressionSuite(
          makerPage,
          manifest.base_artifact_id,
          authorities[0].finding,
          exactFindingJson,
        );
      });

      let failedApproval: ApprovalView | null = null;
      await test.step("failed validation binds exact Review, Playtest, Finding, and regression evidence", async () => {
        if (failedFinding === null) throw new Error("Failed Playtest produced no Finding binding.");
        const validRequest = await exactValidationRequest(makerPage, {
          baseArtifactId: manifest.base_artifact_id,
          configId,
          constraintId: manifest.constraint_artifact_id,
          expectedFindings: [],
          findings: [failedFinding],
          patchId,
          regressionSuiteId,
          reviewId: failedReviewId,
          traceId: failedTraceId,
        });
        const rejectedRequest = structuredClone(validRequest);
        rejectedRequest.approval_id = rejectedApprovalId;
        const authorityBeforeRejectedValidate = await getPatchApproval(makerPage, patchId);
        const runsBeforeRejectedValidate = await runIds(makerPage);
        const approvalsBeforeRejectedValidate = await approvalIds(makerPage);
        const transportBeforeRejectedValidate = await transportLines();
        const historyBeforeRejectedValidate = await refHistory(makerPage);
        const rejectedValidate = await sameOriginRequest<components["schemas"]["Problem"]>(
          makerPage,
          `/api/v1/patches/${encodeURIComponent(rejectedPatchId)}:validate`,
          {
            body: rejectedRequest,
            expected: [409],
            headers: {
              ...mutationHeaders("gate-rejected-validate"),
              "If-Match": resourceEtag("patch", rejectedPatchId, rejectedRequest.expected_workflow_revision),
            },
            method: "POST",
            withCsrf: true,
          },
        );
        expect(rejectedValidate.body.code).toBe("revision_conflict");
        await sameOriginRequest(makerPage, `/api/v1/patches/${encodeURIComponent(rejectedPatchId)}`, {
          expected: [404],
        });
        await sameOriginRequest(
          makerPage,
          `/api/v1/workflow-subjects/${encodeURIComponent(rejectedPatchId)}/approval-binding`,
          { expected: [404] },
        );
        await sameOriginRequest(makerPage, `/api/v1/approvals/${encodeURIComponent(rejectedApprovalId)}`, {
          expected: [404],
        });
        expect(await getPatchApproval(makerPage, patchId)).toEqual(authorityBeforeRejectedValidate);
        expect(await runIds(makerPage)).toEqual(runsBeforeRejectedValidate);
        expect(await approvalIds(makerPage)).toEqual(approvalsBeforeRejectedValidate);
        expect(await transportLines()).toEqual(transportBeforeRejectedValidate);
        expect(await refHistory(makerPage)).toEqual(historyBeforeRejectedValidate);

        const runHref = await validatePatch(makerPage, {
          configId,
          constraintId: manifest.constraint_artifact_id,
          expectedFindings: [],
          findings: [failedFinding],
          patchId,
          regressionSuiteId,
          reviewId: failedReviewId,
          traceId: failedTraceId,
        });
        const validationRunId = await openRunAndWait(makerPage, runHref, "succeeded");
        const validationOutcome = await successArtifacts(makerPage, validationRunId);
        expect(validationOutcome.manifest.payload).toMatchObject({
          outcome_code: "patch_validation_failed",
        });
        failedApproval = (await waitForPatchStatus(makerPage, patchId, "validation_failed")).approval;
        expect(failedApproval.approval.evidence_set_artifact_id).toBeTruthy();
        await makerPage.goto(`/patches/${encodeURIComponent(patchId)}`);
        await expect(makerPage.getByText("validation_failed", { exact: true }).first()).toBeVisible();
        await expect(
          makerPage.getByRole("button", { name: "Submit for independent approval" }),
        ).toBeDisabled();
        await expect(makerPage.getByRole("button", { name: "Apply approved Patch" })).toBeDisabled();
        expect(await refHistory(makerPage)).toHaveLength(1);
        await captureDemoSourceFrame(
          makerPage,
          "failed-validation-source.png",
          makerPage.getByRole("heading", { name: "Validation / regression evidence" }),
        );
      });

      let repairedPatchId = "";
      let repairedPreviewId = "";
      let repairedConfigId = "";
      let repairRunId = "";
      await test.step("fixture-only repair RECORD fails after capture; product REPLAY creates a clean revision", async () => {
        if (failedApproval === null || failedFinding === null || oldPlaytestRecord === null) {
          throw new Error("Failed candidate authority was not retained for repair.");
        }
        const source = await recordRepairSource(makerPage, {
          approval: failedApproval,
          baseArtifactId: manifest.base_artifact_id,
          constraintArtifactId: manifest.constraint_artifact_id,
          expectedRef: manifest.expected_ref,
          finding: failedFinding,
          patchId,
          regressionSuiteArtifactId: regressionSuiteId,
        });
        expect(source.run.failure_artifact_id).toBeTruthy();
        const cassetteArtifactId = source.run.terminal_cassette_artifact_id;
        if (cassetteArtifactId === null) {
          throw new Error("Fixture-only repair RECORD did not retain its cassette.");
        }
        const rejectedRequest = structuredClone(source.request);
        rejectedRequest.llm_execution_mode = "replay";
        rejectedRequest.cassette_artifact_id = cassetteArtifactId;
        rejectedRequest.params.subject_patch_artifact_id = rejectedPatchId;
        const authorityBeforeRejectedRepair = await getPatchApproval(makerPage, patchId);
        const runsBeforeRejectedRepair = await runIds(makerPage);
        const approvalsBeforeRejectedRepair = await approvalIds(makerPage);
        const transportBeforeRejectedRepair = await transportLines();
        const historyBeforeRejectedRepair = await refHistory(makerPage);
        const rejectedRepair = await sameOriginRequest<components["schemas"]["Problem"]>(
          makerPage,
          `/api/v1/patches/${encodeURIComponent(rejectedPatchId)}:repair`,
          {
            body: rejectedRequest,
            expected: [409],
            headers: mutationHeaders("gate-rejected-repair"),
            method: "POST",
            withCsrf: true,
          },
        );
        expect(rejectedRepair.body.code).toBe("revision_conflict");
        await sameOriginRequest(makerPage, `/api/v1/patches/${encodeURIComponent(rejectedPatchId)}`, {
          expected: [404],
        });
        await sameOriginRequest(
          makerPage,
          `/api/v1/workflow-subjects/${encodeURIComponent(rejectedPatchId)}/approval-binding`,
          { expected: [404] },
        );
        await sameOriginRequest(makerPage, `/api/v1/approvals/${encodeURIComponent(rejectedApprovalId)}`, {
          expected: [404],
        });
        expect(await getPatchApproval(makerPage, patchId)).toEqual(authorityBeforeRejectedRepair);
        expect(await runIds(makerPage)).toEqual(runsBeforeRejectedRepair);
        expect(await approvalIds(makerPage)).toEqual(approvalsBeforeRejectedRepair);
        expect(await transportLines()).toEqual(transportBeforeRejectedRepair);
        expect(await refHistory(makerPage)).toEqual(historyBeforeRejectedRepair);

        const beforeReplay = await transportLines();
        repairRunId = await repairPatchReplay(makerPage, {
          constraintId: manifest.constraint_artifact_id,
          finding: failedFinding,
          patchId,
          regressionSuiteId,
          sourceRunId: source.run.run_id,
        });
        await assertReplayDidNotCallTransport(beforeReplay);
        const outcome = await successArtifacts(makerPage, repairRunId);
        repairedPatchId = outcome.primaryArtifactId;
        repairedPreviewId = singleArtifactOfKind(outcome.outputs, "ir_snapshot").artifact.artifact_id;
        repairedConfigId = singleArtifactOfKind(outcome.outputs, "config_export").artifact.artifact_id;

        const oldAuthority = await waitForPatchStatus(makerPage, patchId, "superseded");
        const repairedAuthority = await waitForPatchStatus(makerPage, repairedPatchId, "draft");
        expect(oldAuthority.approval.approval.evidence_set_artifact_id).toBe(
          failedApproval.approval.evidence_set_artifact_id,
        );
        expect(repairedAuthority.approval.approval).toMatchObject({
          decisions: [],
          evidence_set_artifact_id: null,
          regression_evidence_artifact_ids: [],
          supersedes_approval_id: oldAuthority.binding.approval_id,
        });
        if (process.env.GAMEFORGE_RECORD_DEMO === "1") {
          await makerPage.goto(`/patches/${encodeURIComponent(repairedPatchId)}`);
          await captureDemoSourceFrame(
            makerPage,
            "repair-draft-source.png",
            makerPage.getByRole("heading", { level: 1, name: /Patch revision/u }),
          );
        }
        await makerPage.goto(`/patches/${encodeURIComponent(patchId)}`);
        await expect(
          makerPage.getByRole("button", { name: "Submit for independent approval" }),
        ).toBeDisabled();
        await expect(makerPage.getByRole("button", { name: "Apply approved Patch" })).toBeDisabled();

        const oldItem = oldAuthority.approval.approval;
        const oldTarget = oldItem.target_binding;
        if (oldTarget == null || oldTarget.subject_kind !== "patch") {
          throw new Error("Superseded Patch lost its exact target binding.");
        }
        const refBeforeStaleCommands = await refHistory(makerPage);
        const blockedSubmit = await sameOriginRequest<components["schemas"]["Problem"]>(
          makerPage,
          `/api/v1/patches/${encodeURIComponent(patchId)}:submit-for-approval`,
          {
            body: {
              approval_id: oldItem.approval_id,
              expected_workflow_revision: oldItem.workflow_revision,
              request_schema_version: "submit-for-approval-request@1",
            },
            expected: [409],
            headers: {
              ...mutationHeaders("superseded-submit"),
              "If-Match": oldAuthority.etag,
            },
            method: "POST",
            withCsrf: true,
          },
        );
        expect(blockedSubmit.body.code).toBe("revision_conflict");
        const blockedApply = await sameOriginRequest<components["schemas"]["Problem"]>(
          approverPage,
          `/api/v1/patches/${encodeURIComponent(patchId)}:apply`,
          {
            body: {
              approval_id: oldItem.approval_id,
              expected_ref: manifest.expected_ref,
              expected_workflow_revision: oldItem.workflow_revision,
              ref_name: refName,
              request_schema_version: "workflow-apply-request@1",
              subject_digest: oldItem.subject_digest,
              target_artifact_id: oldTarget.target_artifact_id,
              target_digest: oldTarget.target_digest,
            },
            expected: [409],
            headers: {
              ...mutationHeaders("superseded-apply"),
              "If-Match": oldAuthority.etag,
            },
            method: "POST",
            withCsrf: true,
          },
        );
        expect(blockedApply.body.code).toBe("revision_conflict");
        const oldAfterStaleCommands = await getPatchApproval(makerPage, patchId);
        const repairedAfterStaleCommands = await getPatchApproval(makerPage, repairedPatchId);
        expect(oldAfterStaleCommands.approval).toEqual(oldAuthority.approval);
        expect(oldAfterStaleCommands.binding).toEqual(oldAuthority.binding);
        expect(repairedAfterStaleCommands.approval).toEqual(repairedAuthority.approval);
        expect(repairedAfterStaleCommands.binding).toEqual(repairedAuthority.binding);
        expect(await refHistory(makerPage)).toEqual(refBeforeStaleCommands);

        const staleRequest: PlaytestRunRequest = structuredClone(oldPlaytestRecord.request);
        staleRequest.llm_execution_mode = "replay";
        staleRequest.cassette_artifact_id = oldPlaytestRecord.run.terminal_cassette_artifact_id;
        staleRequest.params.config_artifact_id = repairedConfigId;
        const beforeProbe = await transportLines();
        const stale = await sameOriginRequest<components["schemas"]["Problem"]>(
          makerPage,
          "/api/v1/playtest:run",
          {
            body: staleRequest,
            expected: [409],
            headers: mutationHeaders("stale-suite-new-config"),
            method: "POST",
            withCsrf: true,
          },
        );
        expect(stale.body.code).toBe("stale_task_suite");
        await assertReplayDidNotCallTransport(beforeProbe);
      });

      let repairedReviewId = "";
      let repairedPlaytestHref = "";
      let repairedTraceId = "";
      await test.step("repaired candidate is re-reviewed, re-derived, and re-playtested", async () => {
        const derived = await deriveTaskSuite(makerPage, {
          configId: repairedConfigId,
          constraintId: manifest.constraint_artifact_id,
          previewId: repairedPreviewId,
          sourceRunId: repairRunId,
        });
        if (oldSuite === null) throw new Error("Original TaskSuite was not retained.");
        expect(derived.suite.artifact.artifact_id).not.toBe(oldSuite.artifact.artifact_id);

        const reviewSource = await recordReviewSource(
          makerPage,
          repairedPreviewId,
          manifest.constraint_artifact_id,
          false,
        );
        const beforeReviewReplay = await transportLines();
        const reviewRunId = await launchReviewReplay(
          makerPage,
          {
            constraintId: manifest.constraint_artifact_id,
            generationRunId: repairRunId,
            previewId: repairedPreviewId,
          },
          reviewSource.run.run_id,
          false,
        );
        await assertReplayDidNotCallTransport(beforeReviewReplay);
        repairedReviewId = (await successArtifacts(makerPage, reviewRunId)).primaryArtifactId;
        const repairedReview = await artifact(makerPage, repairedReviewId);
        expect(repairedReview.payload).toMatchObject({ unproven_findings: [] });

        const maxSteps = Math.min(
          7,
          ...derived.suite.task_suite.episodes.map((episode) => episode.step_budget),
        );
        const source = await recordPlaytestSource(makerPage, derived.suite, maxSteps);
        const beforePlaytestReplay = await transportLines();
        const playtestRunId = await launchPlaytestReplay(makerPage, {
          configId: repairedConfigId,
          constraintId: manifest.constraint_artifact_id,
          maxSteps,
          previewId: repairedPreviewId,
          sourceRunId: source.run.run_id,
          suiteId: derived.suite.artifact.artifact_id,
        });
        await expect(makerPage.getByRole("heading", { name: "Run 已完成，全部任务通过" })).toBeVisible({
          timeout: 45_000,
        });
        repairedPlaytestHref = makerPage.url();
        await assertReplayDidNotCallTransport(beforePlaytestReplay);
        const outcome = await successArtifacts(makerPage, playtestRunId);
        repairedTraceId = outcome.primaryArtifactId;
        const trace = await artifact(makerPage, repairedTraceId);
        const episodes = isRecord(trace.payload) ? trace.payload.episodes : null;
        expect(
          Array.isArray(episodes) && episodes.every((episode) => isRecord(episode) && episode.completed),
        ).toBe(true);
        expect(await findingBindings(makerPage, playtestRunId)).toEqual([]);
      });

      let approvalHref = "";
      await test.step("new validation passes, independent approver applies the exact target once", async () => {
        if (failedApproval === null || failedFinding === null) {
          throw new Error("Historical validation evidence was not retained.");
        }
        const validationHref = await validatePatch(makerPage, {
          configId: repairedConfigId,
          constraintId: manifest.constraint_artifact_id,
          expectedFindings: [failedFinding],
          findings: [],
          patchId: repairedPatchId,
          regressionSuiteId,
          reviewId: repairedReviewId,
          traceId: repairedTraceId,
        });
        const validationRunId = await openRunAndWait(makerPage, validationHref, "succeeded");
        expect((await successArtifacts(makerPage, validationRunId)).manifest.payload).toMatchObject({
          outcome_code: "patch_validation_passed",
        });
        const validated = await waitForPatchStatus(makerPage, repairedPatchId, "validated");
        expect(validated.approval.approval.evidence_set_artifact_id).toBeTruthy();
        expect(validated.approval.approval.evidence_set_artifact_id).not.toBe(
          failedApproval.approval.evidence_set_artifact_id,
        );

        approvalHref = await submitPatch(makerPage, repairedPatchId);
        await makerPage.goto(approvalHref);
        await expect(
          makerPage.getByText("maker-checker：提议者不能决定自己的提议", { exact: true }).first(),
        ).toBeVisible();
        await expect(makerPage.getByRole("checkbox", { name: /^选择 /u }).first()).toBeDisabled();
        await approvePatch(approverPage, approvalHref);

        const approved = await waitForPatchStatus(approverPage, repairedPatchId, "approved");
        const target = approved.approval.approval.target_binding;
        if (target == null || target.subject_kind !== "patch") {
          throw new Error("Approved Patch has no exact target binding.");
        }
        expect(target.target_artifact_id).toBe(repairedPreviewId);
        expect(await refHistory(makerPage)).toEqual([
          { entry_schema_version: "ref-history-entry@1", ref_name: refName, value: manifest.expected_ref },
        ]);
        if (process.env.GAMEFORGE_RECORD_DEMO === "1") {
          await makerPage.goto(approvalHref);
          await captureDemoSourceFrame(
            makerPage,
            "approved-before-apply-source.png",
            makerPage.getByRole("heading", { name: "Immutable decisions" }),
          );
        }

        await applyPatch(approverPage, repairedPatchId);
        const history = await refHistory(approverPage);
        expect(history).toHaveLength(2);
        expect(history[history.length - 1]?.value).toEqual({ artifact_id: repairedPreviewId, revision: 2 });
        expect(
          (await waitForPatchStatus(approverPage, repairedPatchId, "applied")).approval.approval.status,
        ).toBe("applied");

        await makerPage.goto("/eval");
        await expect(makerPage.getByRole("heading", { level: 1, name: "Eval / Bench" })).toBeVisible();
        const qaRegion = makerPage.getByRole("region", { name: "真人 QA" });
        await expect(qaRegion).toContainText("human evidence available");
        await expect(qaRegion).toContainText("savings");
        await approverPage.goto(`/observability?run=${encodeURIComponent(repairRunId)}`);
        await expect(approverPage.getByRole("heading", { level: 1, name: "可观测性" })).toBeVisible();
        const runContext = approverPage
          .getByRole("heading", { name: "当前 Run context" })
          .locator("xpath=ancestor::section[1]");
        await expect(runContext).toContainText(repairRunId);

        await expect(approverPage.getByRole("heading", { name: "Run → Trace" })).toBeVisible();
        const traceTable = approverPage.getByRole("region", { name: "该 Run 的 Trace" });
        await expect(traceTable).toBeVisible();
        await expect.poll(async () => traceTable.getByRole("row").count()).toBeGreaterThan(1);

        const logsHeading = approverPage.getByRole("heading", { name: "脱敏日志记录" });
        await expect(logsHeading).toBeVisible();
        const logsSection = logsHeading.locator("xpath=ancestor::section[1]");
        await expect(logsSection.getByText("Worker attempt started.", { exact: true }).first()).toBeVisible();
        await expect(logsSection).toContainText(repairRunId);

        const costHeading = approverPage.getByRole("heading", { name: "冻结预算与成本结算" });
        await expect(costHeading).toBeVisible();
        const costSection = costHeading.locator("xpath=ancestor::section[1]");
        await expect(costSection.getByRole("region", { name: "成本结算摘要" })).toBeVisible();
        await expect(costSection).toContainText("Budget set");
        const usage = costSection.locator('[data-testid^="cost-usage-"]');
        await expect.poll(async () => usage.count()).toBeGreaterThan(0);
        await expect(usage.first()).toContainText("cassette_replay");
        await expect(usage.first().getByText("Provider latency", { exact: true })).toBeVisible();
        await expect(usage.first().getByText("Latency unavailable", { exact: true })).toBeVisible();
      });

      if (process.env.GAMEFORGE_RECORD_DEMO === "1") {
        expect(approvalHref).not.toBe("");
        expect(failedPlaytestHref).not.toBe("");
        expect(repairedPlaytestHref).not.toBe("");
        await recordJourneyADemo(
          browser,
          {
            failedPlaytestHref,
            failedReviewId,
            generationRunId,
            manifest,
            patchId,
            previewId,
            repairedPatchId,
            repairedPlaytestHref,
          },
          unexpected,
        );
      }

      expect([...unexpected]).toEqual([]);
    } finally {
      await makerContext.close();
      await approverContext.close();
    }
  });
});
