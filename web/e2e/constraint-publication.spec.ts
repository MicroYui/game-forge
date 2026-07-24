import { readFile } from "node:fs/promises";

import { expect, test, type Locator, type Page } from "@playwright/test";

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
const constraintRef = "constraint-publication-head";

type ApprovalView = components["schemas"]["ApprovalViewV1"];
type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
type ConstraintProposalView = components["schemas"]["ConstraintProposalReadViewV1"];
type ConstraintTargetBinding = components["schemas"]["ConstraintTargetBindingV1"];
type ConstraintValidationBinding = components["schemas"]["ConstraintValidationCompilerBindingViewV1"];
type HumanConstraintRevisionRequest = components["schemas"]["HumanConstraintRevisionRequestV1"];
type RefHistoryPage = components["schemas"]["OpaquePageV1_RefHistoryEntryV1_"];
type RefValue = components["schemas"]["RefValue"];
type SubjectApprovalBinding = components["schemas"]["SubjectApprovalBindingViewV1"];
type WorkflowApplyRequest = components["schemas"]["WorkflowApplyRequestV1"];

interface ConstraintManifest {
  record_proposal_artifact_id: string;
  record_source_run_id: string;
  source_artifact_id: string;
}

interface BrowserResponse<T> {
  body: T;
  etag: string | null;
  status: number;
}

interface ProposalAuthority {
  approval: ApprovalView;
  approvalEtag: string;
  binding: SubjectApprovalBinding;
  etag: string;
  proposal: ConstraintProposalView;
}

interface RefAuthority {
  items: RefValue[];
  status: number;
}

interface PreparedPublication {
  candidateId: string;
  key: string;
  proposalHref: string;
  proposalId: string;
  refName: string;
}

let stack: AuthoringStack;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requiredRecord(value: unknown, label: string): Record<string, unknown> {
  if (!isRecord(value)) throw new Error(`${label} is not an object.`);
  return value;
}

function requiredString(value: unknown, label: string): string {
  if (typeof value !== "string" || !value) throw new Error(`${label} is not a non-empty string.`);
  return value;
}

async function requiredHref(locator: Locator): Promise<string> {
  await expect(locator).toBeVisible();
  const href = await locator.getAttribute("href");
  if (!href) throw new Error("Expected a retained journey link.");
  return href;
}

async function browserRequest<T>(
  page: Page,
  path: string,
  init: {
    body?: unknown;
    idempotencyKey?: string;
    ifMatch?: string;
    method?: "GET" | "POST";
  } = {},
): Promise<BrowserResponse<T>> {
  const request = {
    body: init.body ?? null,
    hasBody: init.body !== undefined,
    idempotencyKey: init.idempotencyKey ?? null,
    ifMatch: init.ifMatch ?? null,
    method: init.method ?? "GET",
    path,
  };
  return page.evaluate(async (input) => {
    const headers = new Headers({ Accept: "application/json" });
    if (input.hasBody) headers.set("Content-Type", "application/json");
    if (input.method !== "GET") {
      const csrf = sessionStorage.getItem("gameforge.csrf-token");
      if (csrf === null) throw new Error("The browser session has no CSRF authority.");
      headers.set("X-CSRF-Token", csrf);
      if (input.idempotencyKey !== null) headers.set("Idempotency-Key", input.idempotencyKey);
      if (input.ifMatch !== null) headers.set("If-Match", input.ifMatch);
    }
    const response = await fetch(input.path, {
      body: input.hasBody ? JSON.stringify(input.body) : undefined,
      credentials: "include",
      headers,
      method: input.method,
    });
    const text = await response.text();
    return {
      body: (text ? JSON.parse(text) : null) as T,
      etag: response.headers.get("etag"),
      status: response.status,
    };
  }, request);
}

async function readProposalAuthority(page: Page, artifactId: string): Promise<ProposalAuthority> {
  const encoded = encodeURIComponent(artifactId);
  const proposal = await browserRequest<ConstraintProposalView>(
    page,
    `/api/v1/constraint-proposals/${encoded}`,
  );
  expect(proposal.status).toBe(200);
  expect(proposal.etag).not.toBeNull();

  const binding = await browserRequest<SubjectApprovalBinding>(
    page,
    `/api/v1/workflow-subjects/${encoded}/approval-binding`,
  );
  expect(binding.status).toBe(200);
  const approval = await browserRequest<ApprovalView>(
    page,
    `/api/v1/approvals/${encodeURIComponent(binding.body.approval_id)}`,
  );
  expect(approval.status).toBe(200);
  expect(approval.etag).not.toBeNull();
  return {
    approval: approval.body,
    approvalEtag: approval.etag!,
    binding: binding.body,
    etag: proposal.etag!,
    proposal: proposal.body,
  };
}

async function readRefAuthority(page: Page, refName: string): Promise<RefAuthority> {
  const response = await browserRequest<RefHistoryPage>(
    page,
    `/api/v1/refs/${encodeURIComponent(refName)}/history?limit=100`,
  );
  if (response.status === 404) return { items: [], status: 404 };
  expect(response.status).toBe(200);
  return {
    items: response.body.items
      .map((entry) => entry.value)
      .sort((left, right) => left.revision - right.revision),
    status: response.status,
  };
}

async function transportLines(): Promise<string[]> {
  if (stack.transportLogPath === null) throw new Error("Constraint launcher has no transport log.");
  return (await readFile(stack.transportLogPath, "utf-8"))
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

async function configureRefBinding(page: Page, refName: string, expectedRef: RefValue | null) {
  const binding = page.getByRole("group", { name: "发布位置" });
  if (expectedRef === null) {
    await binding.getByRole("radio", { name: "创建新 ref" }).check();
    await binding.getByLabel("Ref 名称").fill(refName);
    return;
  }
  await binding.getByRole("radio", { name: "更新已有 ref" }).check();
  await binding.getByLabel("Ref 名称").fill(refName);
  await binding.getByRole("button", { name: "查找当前版本" }).click();
  const resolved = binding.getByRole("status");
  await expect(resolved).toContainText(refName);
  await expect(resolved).toContainText(`已选择当前 revision ${expectedRef.revision}`);
}

function shortId(value: string): string {
  return value.length <= 22 ? value : `${value.slice(0, 12)}…${value.slice(-8)}`;
}

async function selectSourceArtifact(entry: Locator, pickerName: string, artifactId: string): Promise<void> {
  const picker = entry.getByRole("group", { name: pickerName });
  const option = picker.locator("label").filter({ hasText: shortId(artifactId) });
  await expect(option).toHaveCount(1);
  await option.getByRole("checkbox").check();
}

function proposalArtifactId(href: string): string {
  const path = new URL(href, stack.baseURL).pathname;
  const encoded = path.split("/").pop();
  if (!encoded) throw new Error(`Could not read proposal ID from ${href}.`);
  return decodeURIComponent(encoded);
}

function targetBinding(authority: ProposalAuthority): ConstraintTargetBinding {
  const target = authority.approval.approval.target_binding;
  if (target?.subject_kind !== "constraint_proposal") {
    throw new Error("Expected an exact constraint target binding.");
  }
  return target;
}

async function exactCompilerBinding(page: Page): Promise<ConstraintValidationBinding> {
  const response = await browserRequest<ConstraintValidationBinding>(
    page,
    "/api/v1/execution-profiles/builtin.constraint_compiler/versions/1/constraint-validation-binding",
  );
  expect(response.status).toBe(200);
  return response.body;
}

function validationRequest(
  authority: ProposalAuthority,
  compiler: ConstraintValidationBinding,
  refName: string,
) {
  return {
    approval_id: authority.binding.approval_id,
    base_constraint_snapshot_artifact_id: null,
    compiler_profile: compiler.compiler_profile,
    differential_engines: compiler.differential_engines,
    dsl_grammar_version: authority.proposal.proposal.dsl_grammar_version,
    expected_subject_head_revision: authority.binding.subject_head_revision,
    expected_workflow_revision: authority.binding.workflow_revision,
    golden_suite_artifact_id: null,
    regression_suite_artifact_ids: [],
    request_schema_version: "constraint-validation-admission-request@1",
    seed: null,
    subject_digest: authority.binding.subject_digest,
    target: { expected_ref: null, ref_name: refName },
    validation_policy: { profile_id: "builtin.validation", version: 1 },
  };
}

async function assertMissingHumanRevisionFails(
  page: Page,
  proposalId: string,
  refName: string,
  key: string,
): Promise<void> {
  const before = await readProposalAuthority(page, proposalId);
  const beforeRef = await readRefAuthority(page, refName);
  expect(before.binding.subject_revision).toBe(1);
  expect(before.approval.approval.status).toBe("draft");
  expect(before.approval.approval.target_binding).toBeNull();

  const response = await browserRequest<Record<string, unknown>>(
    page,
    `/api/v1/constraint-proposals/${encodeURIComponent(proposalId)}:validate`,
    {
      body: validationRequest(before, await exactCompilerBinding(page), refName),
      idempotencyKey: `${key}:missing-human-revision`,
      ifMatch: before.etag,
      method: "POST",
    },
  );
  expect(response.status).toBe(409);
  expect(response.body.code).toBe("workflow_guard");

  const after = await readProposalAuthority(page, proposalId);
  expect(after).toEqual(before);
  expect(await readRefAuthority(page, refName)).toEqual(beforeRef);
}

async function waitForRunSucceeded(page: Page, runHref: string): Promise<void> {
  await page.goto(runHref);
  await expect(page.getByText(/^run\.succeeded · /u)).toBeVisible({ timeout: 45_000 });
  await expect(page.getByRole("heading", { name: /^运行 run:/u })).toBeVisible();
}

async function createAgentReplayDraft(
  page: Page,
  manifest: ConstraintManifest,
): Promise<{
  proposalHref: string;
  runId: string;
}> {
  const transportBefore = await transportLines();
  expect(transportBefore).toEqual(["extraction"]);
  await page.goto("/specs");
  const entry = page.locator('article[data-entry="agent"]');
  await selectSourceArtifact(entry, "Agent 可使用的来源", manifest.source_artifact_id);
  const grammar = entry.getByRole("combobox", { name: "DSL grammar", exact: true });
  await expect(grammar).toBeEnabled();
  await grammar.selectOption("dsl@1");
  await entry.getByLabel("Agent authoring goal").fill("Extract a deterministic gold reward cap.");
  await entry.getByLabel("Agent execution profile").selectOption("builtin.constraint_extraction@1");
  await entry.getByLabel("LLM execution mode").selectOption("replay");
  const replaySource = entry.getByRole("combobox", { name: "Replay 来源 Run", exact: true });
  await expect(replaySource).toBeEnabled();
  await replaySource.selectOption(manifest.record_source_run_id);
  await entry.getByRole("button", { name: "生成 Agent 候选" }).click();

  const runLink = entry.getByRole("link", { name: /^打开 Run run:/u });
  const runHref = await requiredHref(runLink);
  const runId = decodeURIComponent(new URL(runHref, stack.baseURL).pathname.split("/").pop() ?? "");
  expect(runId).not.toBe(manifest.record_source_run_id);
  await waitForRunSucceeded(page, runHref);
  expect(await transportLines()).toEqual(transportBefore);

  await page.goto("/specs");
  const proposalRegion = page.getByRole("region", { name: "约束提案（候选 Artifact）" });
  const proposalRow = proposalRegion.getByRole("row").filter({ hasText: runId });
  await expect(proposalRow).toHaveCount(1);
  const proposalHref = await requiredHref(proposalRow.getByRole("link", { name: "检查 exact proposal" }));
  return { proposalHref, runId };
}

function typedConstraint(id: string, expression: string) {
  return {
    assert: expression,
    dsl_grammar_version: "dsl@1",
    id,
    kind: "numeric",
    oracle: "deterministic",
    predicates: [],
    scope: { node_type: "QUEST", var: "q", where: {} },
    severity: "major",
  };
}

async function createHumanDraft(page: Page, manifest: ConstraintManifest, refName: string): Promise<string> {
  await page.goto("/specs");
  const entry = page.locator('article[data-entry="human"]');
  const binding = entry.getByRole("group", { name: "发布位置" });
  await binding.getByRole("radio", { name: "创建新 ref" }).check();
  await binding.getByLabel("Ref 名称").fill(refName);
  await entry
    .getByRole("group", { name: "适用游戏域" })
    .getByRole("checkbox", { name: domainId, exact: true })
    .check();
  await selectSourceArtifact(entry, "规则来源", manifest.source_artifact_id);
  await entry.getByLabel("Human rationale").fill("Initial human typed reward-cap proposal.");
  await entry
    .getByLabel("Typed constraints JSON")
    .fill(JSON.stringify([typedConstraint("c:human-cap", "reward_gold <= 100")], null, 2));
  await entry.getByRole("button", { name: "创建 Human typed draft" }).click();
  return requiredHref(entry.getByRole("link", { name: /^打开 proposal /u }));
}

async function reviseToHumanCandidate(
  page: Page,
  proposalHref: string,
  refName: string,
  expectedRef: RefValue | null,
  rationale: string,
): Promise<string> {
  await page.goto(proposalHref);
  await configureRefBinding(page, refName, expectedRef);
  await page.getByLabel("修订说明").fill(rationale);
  const oldArtifactId = proposalArtifactId(proposalHref);
  await page.getByRole("button", { name: "提交人工修订" }).click();
  const canonicalLink = page.getByRole("link", { name: "当前 revision canonical detail" });
  await expect
    .poll(async () => proposalArtifactId(await requiredHref(canonicalLink)))
    .not.toBe(oldArtifactId);
  await expect(page.getByText("Human 修订候选", { exact: true })).toBeVisible();
  const revisedHref = await requiredHref(canonicalLink);
  await page.goto(revisedHref);
  return revisedHref;
}

async function validateExactCandidate(
  page: Page,
  proposalHref: string,
  refName: string,
  expectedRef: RefValue | null,
): Promise<void> {
  await configureRefBinding(page, refName, expectedRef);
  await page
    .getByRole("combobox", { name: "约束编译器", exact: true })
    .selectOption("builtin.constraint_compiler@1");
  await page.getByRole("combobox", { name: "验证方案", exact: true }).selectOption("builtin.validation@1");
  const validate = page.getByRole("button", { name: "开始确定性验证" });
  await expect(validate).toBeEnabled();
  await validate.click();
  const runHref = await requiredHref(page.getByRole("link", { name: "打开 validation Run" }).last());
  await waitForRunSucceeded(page, runHref);
  const proposalId = proposalArtifactId(proposalHref);
  await expect
    .poll(async () => (await readProposalAuthority(page, proposalId)).approval.approval.status, {
      intervals: [100, 250, 500],
      timeout: 30_000,
    })
    .toBe("validated");
  await page.goto(proposalHref);
  await expect(page.getByText("确定性证据：validated", { exact: true })).toBeVisible();
}

async function assertExactCandidateEvidence(page: Page, authority: ProposalAuthority): Promise<string> {
  const item = authority.approval.approval;
  const target = targetBinding(authority);
  const candidate = await browserRequest<ArtifactPayloadView>(
    page,
    `/api/v1/artifacts/${encodeURIComponent(target.target_artifact_id)}`,
  );
  expect(candidate.status).toBe(200);
  expect(candidate.body.artifact.kind).toBe("constraint_snapshot");
  expect(candidate.body.artifact.payload_hash).toBe(target.target_digest);
  expect(candidate.body.artifact.parent_artifact_ids).toContain(item.subject_artifact_id);

  expect(item.evidence_set_artifact_id).not.toBeNull();
  const evidence = await browserRequest<ArtifactPayloadView>(
    page,
    `/api/v1/artifacts/${encodeURIComponent(item.evidence_set_artifact_id!)}`,
  );
  expect(evidence.status).toBe(200);
  expect(evidence.body.artifact.payload_schema_id).toBe("evidence-set@1");
  const evidencePayload = requiredRecord(evidence.body.payload, "EvidenceSet payload");
  expect(evidencePayload.overall_status).toBe("passed");
  expect(evidencePayload.subject_artifact_id).toBe(item.subject_artifact_id);
  const evidenceTarget = requiredRecord(evidencePayload.target_binding, "EvidenceSet target binding");
  expect(evidenceTarget.target_artifact_id).toBe(target.target_artifact_id);
  expect(evidenceTarget.target_digest).toBe(target.target_digest);
  expect(evidenceTarget.ref_name).toBe(target.ref_name);
  if (target.expected_ref == null) {
    expect("expected_ref" in evidenceTarget).toBe(false);
  } else {
    expect(evidenceTarget.expected_ref).toEqual(target.expected_ref);
  }

  const requirements = evidencePayload.requirements;
  if (!Array.isArray(requirements)) throw new Error("EvidenceSet requirements are not an array.");
  const compileRequirement = requirements
    .map((value) => requiredRecord(value, "EvidenceSet requirement"))
    .find((value) => value.kind === "constraint_compile");
  if (!compileRequirement) throw new Error("EvidenceSet has no constraint_compile requirement.");
  expect(compileRequirement.status).toBe("passed");
  const compileArtifactId = requiredString(
    compileRequirement.evidence_artifact_id,
    "constraint compile evidence Artifact ID",
  );
  const compile = await browserRequest<ArtifactPayloadView>(
    page,
    `/api/v1/artifacts/${encodeURIComponent(compileArtifactId)}`,
  );
  expect(compile.status).toBe(200);
  expect(compile.body.artifact.payload_schema_id).toBe("constraint-compile-evidence@1");
  const compilePayload = requiredRecord(compile.body.payload, "constraint compile evidence payload");
  expect(compilePayload.proposal_artifact_id).toBe(item.subject_artifact_id);
  expect(compilePayload.candidate_constraint_snapshot_artifact_id).toBe(target.target_artifact_id);
  expect(compilePayload.overall_status).toBe("passed");
  const stages = compilePayload.stages;
  if (!Array.isArray(stages)) throw new Error("Constraint compile stages are not an array.");
  const actualEngines = stages
    .map((value) => requiredRecord(value, "constraint compile stage"))
    .filter((value) => value.stage === "differential")
    .map((value) => `${requiredString(value.engine_id, "engine ID")}@${String(value.engine_version)}`)
    .sort();
  const compiler = await exactCompilerBinding(page);
  const expectedEngines = compiler.differential_engines
    .map((value) => `${value.engine_id}@${String(value.version)}`)
    .sort();
  expect(actualEngines).toEqual(expectedEngines);
  return target.target_artifact_id;
}

async function submitForApproval(page: Page): Promise<string> {
  await page.getByRole("combobox", { name: "审批职责", exact: true }).selectOption({ index: 1 });
  const submit = page.getByRole("button", { name: "提交审批" });
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(page.getByText(/pending_approval/u).first()).toBeVisible();
  return requiredHref(page.getByRole("link", { name: "交给另一位 Human 审批" }));
}

async function assertSelfApprovalFails(
  page: Page,
  proposalId: string,
  approvalHref: string,
  refName: string,
  key: string,
): Promise<void> {
  await page.goto(approvalHref);
  await expect(
    page.getByText("maker-checker：提议者不能决定自己的提议", { exact: true }).first(),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "提交批准" })).toBeDisabled();

  const before = await readProposalAuthority(page, proposalId);
  const beforeRef = await readRefAuthority(page, refName);
  const item = before.approval.approval;
  const response = await browserRequest<Record<string, unknown>>(
    page,
    `/api/v1/approvals/${encodeURIComponent(item.approval_id)}:approve`,
    {
      body: {
        comment: null,
        decision: "approve",
        expected_workflow_revision: item.workflow_revision,
        reason_code: "self_approval_forbidden",
        request_schema_version: "approval-decision-request@1",
        requirement_ids: item.requirements.map((requirement) => requirement.requirement_id),
      },
      idempotencyKey: `${key}:self-approval`,
      ifMatch: before.approvalEtag,
      method: "POST",
    },
  );
  expect(response.status).toBe(403);
  expect(response.body.code).toBe("forbidden");
  const after = await readProposalAuthority(page, proposalId);
  expect(after).toEqual(before);
  expect(await readRefAuthority(page, refName)).toEqual(beforeRef);
}

interface ConstraintApprovalExpectation {
  constraintExpression: string;
  refName: string;
  reviewNote: string;
}

function displayedConstraintExpression(value: string): string {
  return value.replace(/<=/gu, "≤").replace(/>=/gu, "≥").replace(/!=/gu, "≠").replace(/==/gu, "=");
}

async function approveIndependently(
  page: Page,
  approvalHref: string,
  expectation: ConstraintApprovalExpectation,
): Promise<void> {
  await page.goto(approvalHref);
  const review = page.getByRole("region", { name: "受审内容与验证依据" });
  await expect(review).toBeVisible();
  await expect(review.getByRole("heading", { name: "你正在批准什么" })).toBeVisible();
  const constraintCards = review.locator("article.gf-constraint-summary");
  expect(await constraintCards.count()).toBeGreaterThan(0);
  await expect(constraintCards.first().locator(".gf-constraint-summary__rule strong")).toHaveText(
    displayedConstraintExpression(expectation.constraintExpression),
  );
  await expect(review.getByRole("heading", { name: "确定性验证已通过" })).toBeVisible();
  await expect(review.getByRole("link", { name: "打开 EvidenceSet" })).toBeVisible();

  const requirements = page.getByRole("checkbox", { name: /^选择 /u });
  await expect(requirements.first()).toBeVisible();
  const requirementCount = await requirements.count();
  expect(requirementCount).toBeGreaterThan(0);
  for (let index = 0; index < requirementCount; index += 1) {
    await expect(requirements.nth(index)).toBeEnabled();
    await requirements.nth(index).check();
  }
  await page
    .getByRole("combobox", { name: "决定原因", exact: true })
    .selectOption("content_and_evidence_reviewed");
  await page.getByLabel("补充说明").fill(expectation.reviewNote);
  await page.getByRole("button", { name: "提交批准" }).click();
  const confirmation = page.getByRole("dialog", { name: "确认批准决定" });
  await expect(confirmation).toBeVisible();
  await expect(confirmation).toContainText("EvidenceSet 已通过");
  await expect(confirmation).toContainText(`目标为 ${expectation.refName}`);
  await expect(confirmation).toContainText("条约束");
  await page.getByRole("button", { name: "确认批准" }).click();
  await expect(page.getByText(/^approved · workflow revision \d+$/u)).toBeVisible();
}

async function assertIndependentApproval(
  page: Page,
  proposalId: string,
  candidateId: string,
): Promise<ProposalAuthority> {
  const approved = await readProposalAuthority(page, proposalId);
  const item = approved.approval.approval;
  expect(item.status).toBe("approved");
  expect(item.proposer.principal_id).toBe("human:maker");
  expect(item.decisions).toHaveLength(1);
  const decision = item.decisions[0]!;
  expect(decision.actor.principal_kind).toBe("human");
  expect(decision.actor.principal_id).toBe("human:approver");
  expect(decision.decision).toBe("approve");
  expect([...decision.requirement_ids].sort()).toEqual(
    item.requirements.map((requirement) => requirement.requirement_id).sort(),
  );
  expect(targetBinding(approved).target_artifact_id).toBe(candidateId);
  return approved;
}

function publishRequest(authority: ProposalAuthority): WorkflowApplyRequest {
  const item = authority.approval.approval;
  const target = targetBinding(authority);
  return {
    approval_id: item.approval_id,
    expected_ref: target.expected_ref ?? null,
    expected_workflow_revision: item.workflow_revision,
    ref_name: target.ref_name,
    request_schema_version: "workflow-apply-request@1",
    subject_digest: item.subject_digest,
    target_artifact_id: target.target_artifact_id,
    target_digest: target.target_digest,
  };
}

async function assertDigestMismatchFails(
  page: Page,
  proposalId: string,
  refName: string,
  key: string,
): Promise<string> {
  const before = await readProposalAuthority(page, proposalId);
  expect(before.approval.approval.status).toBe("approved");
  const candidateId = await assertExactCandidateEvidence(page, before);
  const beforeRef = await readRefAuthority(page, refName);
  expect(beforeRef.items).toEqual([]);

  const digestMismatch: WorkflowApplyRequest = {
    ...publishRequest(before),
    target_digest: "0".repeat(64),
  };
  const digestResponse = await browserRequest<Record<string, unknown>>(
    page,
    `/api/v1/constraint-proposals/${encodeURIComponent(proposalId)}:publish`,
    {
      body: digestMismatch,
      idempotencyKey: `${key}:digest-mismatch`,
      ifMatch: before.etag,
      method: "POST",
    },
  );
  expect(digestResponse.status).toBe(409);
  expect(digestResponse.body.code).toBe("revision_conflict");
  expect(await readProposalAuthority(page, proposalId)).toEqual(before);
  expect(await readRefAuthority(page, refName)).toEqual(beforeRef);
  return candidateId;
}

async function assertStaleRefFails(
  page: Page,
  publication: PreparedPublication,
  currentRef: RefValue,
): Promise<void> {
  const before = await readProposalAuthority(page, publication.proposalId);
  expect(before.approval.approval.status).toBe("approved");
  expect(targetBinding(before).target_artifact_id).toBe(publication.candidateId);
  expect(targetBinding(before).expected_ref).toBeNull();
  const beforeRef = await readRefAuthority(page, publication.refName);
  expect(beforeRef).toEqual({ items: [currentRef], status: 200 });
  const refResponse = await browserRequest<Record<string, unknown>>(
    page,
    `/api/v1/constraint-proposals/${encodeURIComponent(publication.proposalId)}:publish`,
    {
      body: publishRequest(before),
      idempotencyKey: `${publication.key}:stale-ref-current-revision`,
      ifMatch: before.etag,
      method: "POST",
    },
  );
  expect(refResponse.status).toBe(409);
  expect(refResponse.body.code).toBe("revision_conflict");
  expect(await readProposalAuthority(page, publication.proposalId)).toEqual(before);
  expect(await readRefAuthority(page, publication.refName)).toEqual(beforeRef);
}

async function publishThroughUi(
  page: Page,
  proposalHref: string,
  refName: string,
  candidateId: string,
  beforeRef: RefAuthority,
): Promise<void> {
  await page.goto(proposalHref);
  const candidateLink = page.getByRole("link", { name: "检查候选快照内容与 ref 状态" });
  await expect(candidateLink).toBeVisible();
  await expect(candidateLink).toHaveAttribute(
    "href",
    `/constraints/${encodeURIComponent(candidateId)}?ref=${encodeURIComponent(refName)}`,
  );
  const publish = page.getByRole("button", { name: "发布权威约束" });
  await expect(publish).toBeEnabled();
  await publish.click();
  const confirmation = page.getByRole("dialog", { name: "确认发布权威约束" });
  await expect(confirmation).toContainText(refName);
  await page.getByRole("button", { name: "确认发布" }).click();
  await expect(page.getByRole("heading", { name: "已发布为权威约束" })).toBeVisible();
  const history = await readRefAuthority(page, refName);
  expect(history.status).toBe(200);
  expect(history.items).toEqual([
    ...beforeRef.items,
    { artifact_id: candidateId, revision: beforeRef.items.length + 1 },
  ]);
  const after = await readProposalAuthority(page, proposalArtifactId(proposalHref));
  expect(after.approval.approval.status).toBe("applied");
  expect(targetBinding(after).target_artifact_id).toBe(candidateId);
  const candidate = await browserRequest<ArtifactPayloadView>(
    page,
    `/api/v1/artifacts/${encodeURIComponent(candidateId)}`,
  );
  expect(candidate.status).toBe(200);
  expect(candidate.body.artifact.artifact_id).toBe(candidateId);
}

async function prepareProposalPublication(
  makerPage: Page,
  approverPage: Page,
  input: {
    initialHref: string;
    key: string;
    refName: string;
    revisionRationale: string;
  },
): Promise<PreparedPublication> {
  const initialId = proposalArtifactId(input.initialHref);
  await assertMissingHumanRevisionFails(makerPage, initialId, input.refName, input.key);
  const proposalHref = await reviseToHumanCandidate(
    makerPage,
    input.initialHref,
    input.refName,
    null,
    input.revisionRationale,
  );
  const proposalId = proposalArtifactId(proposalHref);
  const revised = await readProposalAuthority(makerPage, proposalId);
  expect(revised.binding.subject_revision).toBe(2);
  expect(revised.proposal.proposal.produced_by).toBe("human");
  expect(revised.proposal.proposal.supersedes_artifact_id).toBe(initialId);

  await validateExactCandidate(makerPage, proposalHref, input.refName, null);
  const validated = await readProposalAuthority(makerPage, proposalId);
  expect(validated.approval.approval.status).toBe("validated");
  const candidateId = await assertExactCandidateEvidence(makerPage, validated);
  expect(targetBinding(validated).expected_ref).toBeNull();
  expect((await readRefAuthority(makerPage, input.refName)).items).toEqual([]);

  await makerPage.goto(proposalHref);
  const approvalHref = await submitForApproval(makerPage);
  await assertSelfApprovalFails(makerPage, proposalId, approvalHref, input.refName, input.key);
  const reviewedConstraint = validated.proposal.proposal.constraints[0];
  if (!reviewedConstraint) throw new Error("Validated proposal has no concrete constraint to review.");
  await approveIndependently(approverPage, approvalHref, {
    constraintExpression: reviewedConstraint.assert,
    refName: input.refName,
    reviewNote: `${input.key}_independent_review_passed`,
  });
  await assertIndependentApproval(approverPage, proposalId, candidateId);
  expect((await readRefAuthority(approverPage, input.refName)).items).toEqual([]);

  expect(await assertDigestMismatchFails(approverPage, proposalId, input.refName, input.key)).toBe(
    candidateId,
  );
  expect((await readRefAuthority(approverPage, input.refName)).items).toEqual([]);
  return { candidateId, key: input.key, proposalHref, proposalId, refName: input.refName };
}

async function rebaseAfterStaleRef(
  page: Page,
  publication: PreparedPublication,
  currentRef: RefValue,
): Promise<string> {
  const before = await readProposalAuthority(page, publication.proposalId);
  expect(before.approval.approval.status).toBe("approved");
  const request: HumanConstraintRevisionRequest = {
    approval_id: before.binding.approval_id,
    base_constraint_snapshot_artifact_id: currentRef.artifact_id,
    constraints: before.proposal.proposal.constraints,
    domain_scope: before.proposal.proposal.domain_scope,
    dsl_grammar_version: before.proposal.proposal.dsl_grammar_version,
    expected_ref: currentRef,
    expected_subject_head_revision: before.binding.subject_head_revision,
    expected_workflow_revision: before.binding.workflow_revision,
    rationale: "Human-authored recovery revision bound to the exact current ref.",
    ref_name: publication.refName,
    request_schema_version: "human-constraint-revision-request@1",
    source_artifact_ids: before.proposal.proposal.source_bindings.map((source) => source.source_artifact_id),
  };
  const response = await browserRequest<ConstraintProposalView>(
    page,
    `/api/v1/constraint-proposals/${encodeURIComponent(publication.proposalId)}:revise`,
    {
      body: request,
      idempotencyKey: `${publication.key}:rebase-current-ref`,
      ifMatch: before.etag,
      method: "POST",
    },
  );
  expect(response.status).toBe(201);
  expect(response.body.proposal.revision).toBe(3);
  expect(response.body.proposal.produced_by).toBe("human");
  expect(response.body.proposal.supersedes_artifact_id).toBe(publication.proposalId);
  expect(response.body.proposal.base_constraint_snapshot_id).not.toBeNull();
  expect(response.body.proposal.base_constraint_snapshot_id).toBe(
    response.body.artifact.version_tuple.constraint_snapshot_id,
  );
  expect(response.body.artifact.parent_artifact_ids).toContain(currentRef.artifact_id);
  expect(response.body.artifact.parent_artifact_ids).toContain(publication.proposalId);
  const href = `/constraint-proposals/${encodeURIComponent(response.body.artifact.artifact_id)}`;
  await page.goto(href);
  return href;
}

async function recoverPublicationAfterStaleRef(
  makerPage: Page,
  approverPage: Page,
  publication: PreparedPublication,
  currentRef: RefValue,
): Promise<PreparedPublication> {
  const proposalHref = await rebaseAfterStaleRef(makerPage, publication, currentRef);
  const proposalId = proposalArtifactId(proposalHref);
  const revised = await readProposalAuthority(makerPage, proposalId);
  expect(revised.binding.subject_revision).toBe(3);
  expect(revised.proposal.proposal.produced_by).toBe("human");
  expect(revised.approval.approval.proposer.principal_id).toBe("human:maker");
  expect(revised.proposal.proposal.supersedes_artifact_id).toBe(publication.proposalId);
  expect(revised.proposal.artifact.parent_artifact_ids).toContain(publication.proposalId);

  const staleRevision = await readProposalAuthority(makerPage, publication.proposalId);
  expect(staleRevision.binding.is_current_head).toBe(false);
  expect(staleRevision.approval.approval.status).toBe("superseded");
  expect(targetBinding(staleRevision).target_artifact_id).toBe(publication.candidateId);
  expect(targetBinding(staleRevision).expected_ref).toBeNull();

  await validateExactCandidate(makerPage, proposalHref, publication.refName, currentRef);
  const validated = await readProposalAuthority(makerPage, proposalId);
  expect(validated.approval.approval.status).toBe("validated");
  expect(targetBinding(validated).expected_ref).toEqual(currentRef);
  const candidateId = await assertExactCandidateEvidence(makerPage, validated);
  expect((await readRefAuthority(makerPage, publication.refName)).items).toEqual([currentRef]);

  await makerPage.goto(proposalHref);
  const approvalHref = await submitForApproval(makerPage);
  const reviewedConstraint = validated.proposal.proposal.constraints[0];
  if (!reviewedConstraint) throw new Error("Recovered proposal has no concrete constraint to review.");
  await approveIndependently(approverPage, approvalHref, {
    constraintExpression: reviewedConstraint.assert,
    refName: publication.refName,
    reviewNote: `${publication.key}_recovered_independent_review_passed`,
  });
  await assertIndependentApproval(approverPage, proposalId, candidateId);
  return {
    candidateId,
    key: `${publication.key}:recovered`,
    proposalHref,
    proposalId,
    refName: publication.refName,
  };
}

test.describe("constraint-publication", () => {
  test.describe.configure({ mode: "serial" });
  test.setTimeout(300_000);

  test.beforeAll(async () => {
    stack = await startAuthoringStack({
      launcherModule: "tests.e2e.m4d_support.constraint_live",
      manifestName: "constraint-live-manifest.json",
      transportLogName: "constraint-live-transport.log",
      workspacePrefix: "gameforge-constraint-publication-",
    });
  });

  test.afterAll(async () => {
    await stack.stop();
  });

  test("publishes Agent and human drafts only through exact human revision and ref CAS", async ({
    browser,
  }) => {
    const manifest = await stack.readManifest<ConstraintManifest>();
    const unexpectedRequests = new Set<string>();
    const makerContext = await browser.newContext({ baseURL: stack.baseURL, ignoreHTTPSErrors: true });
    const approverContext = await browser.newContext({
      baseURL: stack.baseURL,
      ignoreHTTPSErrors: true,
    });
    await guardAuthoringEgress(makerContext, stack.baseURL, unexpectedRequests);
    await guardAuthoringEgress(approverContext, stack.baseURL, unexpectedRequests);
    const makerPage = await makerContext.newPage();
    const approverPage = await approverContext.newPage();
    makerPage.setDefaultTimeout(10_000);
    approverPage.setDefaultTimeout(10_000);

    try {
      await loginAuthoringPage(makerPage, makerCredentials);
      await loginAuthoringPage(approverPage, approverCredentials);

      const agent = await createAgentReplayDraft(makerPage, manifest);
      const preparedAgent = await prepareProposalPublication(makerPage, approverPage, {
        initialHref: agent.proposalHref,
        key: "agent",
        refName: constraintRef,
        revisionRationale: "Human-owned revision of the replay-produced candidate.",
      });

      const humanHref = await createHumanDraft(makerPage, manifest, constraintRef);
      const preparedHuman = await prepareProposalPublication(makerPage, approverPage, {
        initialHref: humanHref,
        key: "human",
        refName: constraintRef,
        revisionRationale: "Second human-authored revision before deterministic compilation.",
      });

      const emptyRef = await readRefAuthority(approverPage, constraintRef);
      expect(emptyRef).toEqual({ items: [], status: 404 });
      await publishThroughUi(
        approverPage,
        preparedHuman.proposalHref,
        constraintRef,
        preparedHuman.candidateId,
        emptyRef,
      );
      const humanAuthority = await readRefAuthority(approverPage, constraintRef);
      expect(humanAuthority).toEqual({
        items: [{ artifact_id: preparedHuman.candidateId, revision: 1 }],
        status: 200,
      });

      const currentRef = humanAuthority.items[0]!;
      await assertStaleRefFails(approverPage, preparedAgent, currentRef);
      const recoveredAgent = await recoverPublicationAfterStaleRef(
        makerPage,
        approverPage,
        preparedAgent,
        currentRef,
      );
      const beforeAgentPublish = await readRefAuthority(approverPage, constraintRef);
      expect(beforeAgentPublish).toEqual(humanAuthority);
      await publishThroughUi(
        approverPage,
        recoveredAgent.proposalHref,
        constraintRef,
        recoveredAgent.candidateId,
        beforeAgentPublish,
      );
      expect(await readRefAuthority(approverPage, constraintRef)).toEqual({
        items: [currentRef, { artifact_id: recoveredAgent.candidateId, revision: 2 }],
        status: 200,
      });

      const record = await readProposalAuthority(makerPage, manifest.record_proposal_artifact_id);
      expect(record.proposal.proposal.producer_run_id).toBe(manifest.record_source_run_id);
      expect(record.binding.subject_revision).toBe(1);
      expect(record.approval.approval.status).toBe("draft");
      expect(record.approval.approval.target_binding).toBeNull();
      expect(await transportLines()).toEqual(["extraction"]);
      expect([...unexpectedRequests]).toEqual([]);
    } finally {
      await makerContext.close();
      await approverContext.close();
    }
  });
});
