import { spawn, type ChildProcess } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { get as httpsGet } from "node:https";
import { createServer, type AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

import { expect, test, type BrowserContext, type Locator, type Page } from "@playwright/test";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const makerCredentials = { login: "maker", password: "maker-password-1" };
const approverCredentials = {
  login: "approver",
  password: "approver-password-1",
};
const refName = "content/head";
const refHistoryPath = `/refs/${encodeURIComponent(refName)}/history`;

type WorkerMode = "disabled" | "enabled";

let apiPort = 0;
let apiUrl = "";
let backend: ChildProcess | null = null;
let backendOutput = "";
let journeyBaseURL = "";
let vite: ChildProcess | null = null;
let viteOutput = "";
let vitePort = 0;
let workspace = "";

async function availableLoopbackPort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((resolveListen, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolveListen());
  });
  const address = server.address() as AddressInfo;
  await new Promise<void>((resolveClose, reject) =>
    server.close((error) => (error ? reject(error) : resolveClose())),
  );
  return address.port;
}

async function waitForApiReady(process: ChildProcess): Promise<void> {
  for (let attempt = 0; attempt < 600; attempt += 1) {
    if (process.exitCode !== null) {
      throw new Error(`Journey B backend exited before readiness.\n${backendOutput}`);
    }
    try {
      const response = await fetch(`${apiUrl}/readyz`, {
        signal: AbortSignal.timeout(500),
      });
      if (response.ok) return;
    } catch {
      // The real server is still starting.
    }
    await delay(100);
  }
  throw new Error(`Journey B backend did not become ready.\n${backendOutput}`);
}

function signalBackend(process: ChildProcess, signal: NodeJS.Signals): void {
  if (process.exitCode === null) process.kill(signal);
}

async function startBackend(worker: WorkerMode): Promise<void> {
  if (backend !== null) throw new Error("Journey B backend is already running.");
  backendOutput = "";
  const python = process.env.GAMEFORGE_PYTHON ?? resolve(repoRoot, ".venv/bin/python");
  const child = spawn(
    python,
    [
      "-m",
      "tests.e2e.m4d_support.journey_b_live",
      "--workspace",
      workspace,
      "--port",
      String(apiPort),
      "--web-origin",
      journeyBaseURL,
      "--worker",
      worker,
    ],
    {
      cwd: repoRoot,
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout?.on("data", (chunk: Buffer) => {
    backendOutput += chunk.toString();
  });
  child.stderr?.on("data", (chunk: Buffer) => {
    backendOutput += chunk.toString();
  });
  backend = child;
  try {
    await waitForApiReady(child);
  } catch (error) {
    backend = null;
    signalBackend(child, "SIGTERM");
    throw error;
  }
}

async function stopBackend(): Promise<void> {
  const child = backend;
  backend = null;
  if (child === null || child.exitCode !== null) return;
  await new Promise<void>((resolveExit, reject) => {
    const forceTimeout = globalThis.setTimeout(() => {
      signalBackend(child, "SIGKILL");
    }, 3_000);
    const failureTimeout = globalThis.setTimeout(() => {
      reject(new Error(`Journey B backend did not stop.\n${backendOutput}`));
    }, 8_000);
    child.once("exit", () => {
      globalThis.clearTimeout(forceTimeout);
      globalThis.clearTimeout(failureTimeout);
      resolveExit();
    });
    signalBackend(child, "SIGTERM");
  });
}

async function viteIsReady(): Promise<boolean> {
  return new Promise((resolveReady) => {
    const request = httpsGet(journeyBaseURL, { rejectUnauthorized: false }, (response) => {
      response.resume();
      resolveReady(response.statusCode !== undefined && response.statusCode < 500);
    });
    request.setTimeout(500, () => {
      request.destroy();
      resolveReady(false);
    });
    request.on("error", () => resolveReady(false));
  });
}

async function startVite(): Promise<void> {
  if (vite !== null) throw new Error("Journey B Vite proxy is already running.");
  viteOutput = "";
  const child = spawn(
    process.execPath,
    [
      resolve(repoRoot, "web/node_modules/vite/bin/vite.js"),
      "--host",
      "127.0.0.1",
      "--port",
      String(vitePort),
    ],
    {
      cwd: resolve(repoRoot, "web"),
      env: {
        ...process.env,
        GAMEFORGE_WEB_API_TARGET: apiUrl,
        GAMEFORGE_WEB_HMR: "off",
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  child.stdout?.on("data", (chunk: Buffer) => {
    viteOutput += chunk.toString();
  });
  child.stderr?.on("data", (chunk: Buffer) => {
    viteOutput += chunk.toString();
  });
  vite = child;
  for (let attempt = 0; attempt < 300; attempt += 1) {
    if (child.exitCode !== null) throw new Error(`Journey B Vite exited early.\n${viteOutput}`);
    if (await viteIsReady()) return;
    await delay(100);
  }
  throw new Error(`Journey B Vite did not become ready.\n${viteOutput}`);
}

async function stopVite(): Promise<void> {
  const child = vite;
  vite = null;
  if (child === null || child.exitCode !== null) return;
  await new Promise<void>((resolveExit, reject) => {
    const forceTimeout = globalThis.setTimeout(() => child.kill("SIGKILL"), 3_000);
    const failureTimeout = globalThis.setTimeout(() => {
      reject(new Error(`Journey B Vite did not stop.\n${viteOutput}`));
    }, 8_000);
    child.once("exit", () => {
      globalThis.clearTimeout(forceTimeout);
      globalThis.clearTimeout(failureTimeout);
      resolveExit();
    });
    child.kill("SIGTERM");
  });
}

async function guardExternalEgress(context: BrowserContext, unexpected: Set<string>): Promise<void> {
  const expectedHttpOrigin = new URL(journeyBaseURL).origin;
  const expectedWebSocketOrigin = expectedHttpOrigin.replace(/^http/u, "ws");
  await context.route(
    (url) => ["http:", "https:"].includes(url.protocol) && url.origin !== expectedHttpOrigin,
    async (route) => {
      unexpected.add(new URL(route.request().url()).origin);
      await route.abort("blockedbyclient");
    },
  );
  await context.routeWebSocket(
    (url) => ["ws:", "wss:"].includes(url.protocol) && url.origin !== expectedWebSocketOrigin,
    async (route) => {
      unexpected.add(new URL(route.url()).origin);
      await route.close({ code: 1008, reason: "external egress disabled" });
    },
  );
  context.on("page", (page) => {
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (!["http:", "https:", "ws:", "wss:"].includes(url.protocol)) return;
      if (url.origin === expectedHttpOrigin || url.origin === expectedWebSocketOrigin) return;
      unexpected.add(url.origin);
    });
  });
}

async function login(page: Page, credentials: { login: string; password: string }): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("登录名").fill(credentials.login);
  await page.getByLabel("密码").fill(credentials.password);
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page).toHaveURL(/\/projects$/u);
  await expect(page.getByRole("heading", { level: 1, name: "游戏项目" })).toBeVisible();
}

async function refHistorySnapshot(page: Page): Promise<{ current: string; entries: string[] }> {
  await page.goto(refHistoryPath);
  const current = page.getByLabel("当前正式版本").getByText(/^第 \d+ 版$/u);
  await expect(current).toBeVisible();
  return {
    current: ((await current.textContent()) ?? "").trim(),
    entries: (await page.locator("ol.gf-patches__history-list--selectable > li").allTextContents()).map(
      (value) => value.replace(/\s+/gu, " ").trim(),
    ),
  };
}

async function currentRevision(page: Page): Promise<number> {
  const history = await refHistorySnapshot(page);
  const match = /^第 (\d+) 版$/u.exec(history.current);
  if (!match) throw new Error(`Could not parse current ref revision from ${history.current}.`);
  return Number(match[1]);
}

async function openCurrentSpec(page: Page): Promise<void> {
  await page.goto("/specs");
  const currentRow = page.getByRole("row").filter({
    hasText: "当前发布版本",
  });
  await expect(currentRow).toHaveCount(1);
  await currentRow.getByRole("link", { name: "查看内容与关系图" }).click();
  await expect(page.getByRole("link", { name: /当前内容 · 第 \d+ 版/u })).toBeVisible();
}

async function requiredHref(locator: Locator): Promise<string> {
  await expect(locator).toBeVisible();
  const href = await locator.getAttribute("href");
  if (!href) throw new Error("Expected a retained journey link.");
  return href;
}

async function draftPatch(
  page: Page,
  input: {
    diffAfter: string;
    diffPath: string;
    operation: object;
    rationale: string;
    sideEffectRisk: "low" | "high";
  },
): Promise<string> {
  await openCurrentSpec(page);
  await page.getByText("高级：精确绑定、前置条件与原始 JSON").click();
  await page.getByLabel("Patch operations JSON").fill(JSON.stringify([input.operation], null, 2));
  await page.getByLabel("变更说明").fill(input.rationale);
  await page.getByLabel("可能影响（必填）").fill(input.sideEffectRisk);
  await page.getByRole("button", { name: "创建修改草案" }).click();
  const link = page.getByRole("link", { name: "检查修改草案" });
  const href = await requiredHref(link);
  await link.click();
  // The title says what the change does; the revision moved to the subline.
  await expect(page.getByRole("heading", { level: 1 })).not.toHaveText(/^修改草案 · 第 \d+ 版$/u);
  await expect(page.getByText(/^第 1 版 · 保留历史/u)).toBeVisible();
  await expect(page.getByRole("heading", { name: "修改前后对比" })).toBeVisible();
  const diffTable = page.getByRole("table", { name: "修改前后的字段差异" });
  const diffRow = diffTable.getByRole("row").nth(1);
  await expect(diffRow).toBeVisible();
  await expect(diffRow.getByRole("cell").last()).toContainText(input.diffAfter);
  await diffRow.getByText("字段定位", { exact: true }).click();
  await expect(diffRow).toContainText(input.diffPath);
  return href;
}

async function startPatchValidation(page: Page): Promise<string> {
  await page.getByLabel("验证方案").selectOption("builtin.validation@1");
  await page.getByRole("group", { name: "验证使用的确定性检查" }).getByRole("checkbox").check();
  await page.getByRole("group", { name: "验证使用的经济仿真" }).getByRole("checkbox").check();
  await page.getByLabel("随机种子（仅仿真或 AI 方案需要）").fill("7");
  const validate = page.getByRole("button", { name: "开始验证" });
  await expect(validate).toBeEnabled();
  await validate.click();
  return requiredHref(page.getByRole("link", { name: "查看已受理的运行" }));
}

async function waitForPatchSubmit(page: Page, patchHref: string): Promise<void> {
  await expect
    .poll(
      async () => {
        await page.goto(patchHref);
        return page.getByRole("button", { name: "提交独立审批" }).isEnabled();
      },
      { intervals: [100, 200, 500], timeout: 30_000 },
    )
    .toBe(true);
  await expect(page.getByRole("link", { name: "查看完整验证依据" })).toBeVisible();
}

async function submitPatch(page: Page): Promise<string> {
  await page.getByRole("button", { name: "提交独立审批" }).click();
  await expect(page.getByText("待审批", { exact: true }).first()).toBeVisible();
  return requiredHref(page.getByRole("link", { name: "打开审批详情" }));
}

type ApprovalReviewExpectation =
  | {
      after: unknown;
      before: unknown;
      kind: "patch";
      refName: string;
      target: string;
    }
  | {
      kind: "rollback";
      reason: string;
      refName: string;
    };

function displayedJsonValue(value: unknown): string {
  return value === undefined ? "无" : (JSON.stringify(value) ?? "无");
}

async function assertApprovalMaterials(page: Page, expectation: ApprovalReviewExpectation): Promise<void> {
  const review = page.getByRole("region", { name: "受审内容与验证依据" });
  await expect(review).toBeVisible();
  await expect(review.getByRole("heading", { name: "你正在批准什么" })).toBeVisible();
  await expect(review.getByRole("heading", { name: "确定性验证已通过" })).toBeVisible();
  await expect(review.getByRole("link", { name: "查看完整证据" })).toBeVisible();

  if (expectation.kind === "patch") {
    const operations = review.getByRole("list", { name: "Patch 变更内容" });
    await expect(operations).toBeVisible();
    await expect(operations.getByRole("listitem")).toHaveCount(1);
    const operation = operations.getByRole("listitem");
    await expect(operation).toContainText(expectation.target);
    const values = operation.locator(".gf-approvals__before-after > div");
    await expect(values).toHaveCount(2);
    await expect(values.nth(0)).toContainText("修改前");
    await expect(values.nth(0)).toContainText(displayedJsonValue(expectation.before));
    await expect(values.nth(1)).toContainText("修改后");
    await expect(values.nth(1)).toContainText(displayedJsonValue(expectation.after));
    return;
  }

  await expect(review).toContainText(expectation.reason);
  await expect(review).toContainText(`将 ${expectation.refName} 回退到历史 revision`);
}

async function prepareApproval(
  page: Page,
  approvalHref: string,
  expectation: ApprovalReviewExpectation,
  reviewNote: string,
): Promise<void> {
  await page.goto(approvalHref);
  await assertApprovalMaterials(page, expectation);
  const requirement = page.getByRole("checkbox", { name: /^选择 /u }).first();
  await expect(requirement).toBeEnabled();
  await requirement.check();
  await page
    .getByRole("combobox", { name: "决定原因", exact: true })
    .selectOption("content_and_evidence_reviewed");
  await page.getByLabel("补充说明").fill(reviewNote);
}

async function confirmApproval(page: Page, refName: string): Promise<void> {
  await page.getByRole("button", { name: "提交批准" }).click();
  const confirmation = page.getByRole("dialog", { name: "确认批准决定" });
  await expect(confirmation).toBeVisible();
  await expect(confirmation).toContainText("确定性验证已通过");
  await expect(confirmation).toContainText(`目标为 ${refName}`);
  await page.getByRole("button", { name: "确认批准" }).click();
}

async function approve(
  page: Page,
  approvalHref: string,
  expectation: ApprovalReviewExpectation,
  reviewNote: string,
): Promise<void> {
  await prepareApproval(page, approvalHref, expectation, reviewNote);
  await confirmApproval(page, expectation.refName);
  await expect(page.locator("header.gf-approvals__hero")).toContainText("已批准");
}

async function applyPatch(page: Page, patchHref: string): Promise<void> {
  await page.goto(patchHref);
  const apply = page.getByRole("button", { name: "应用已批准的修改" });
  await expect(apply).toBeEnabled();
  await apply.click();
  await expect(page.getByRole("dialog", { name: "确认应用已批准的修改？" })).toBeVisible();
  await page.getByRole("button", { name: "确认应用" }).click();
  await expect(page.getByRole("heading", { name: "修改已应用" })).toBeVisible();
}

async function waitForRunSucceeded(page: Page, runHref: string): Promise<void> {
  await page.goto(runHref);
  await expect(
    page.getByRole("region", { name: "运行状态" }).getByText("已完成", { exact: true }),
  ).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByRole("heading", { level: 1, name: "运行详情" })).toBeVisible();
}

async function draftRollback(page: Page): Promise<string> {
  await page.goto(refHistoryPath);
  await page.getByRole("radio", { name: "回退到第 1 版" }).check();
  await page.getByLabel("回退验证方案").selectOption("builtin.rollback@1");
  await page.getByLabel("回退原因").fill("Restore the exact approved baseline.");
  await page.getByRole("button", { name: "创建回退请求" }).click();
  return requiredHref(page.getByRole("link", { name: "继续验证回退请求" }));
}

async function startRollbackValidation(page: Page, rollbackHref: string): Promise<string> {
  await page.goto(rollbackHref);
  await page.getByLabel("结构兼容性检查方案").selectOption("builtin.schema_compatibility@1");
  const validate = page.getByRole("button", {
    name: "开始安全验证",
  });
  await expect(validate).toBeEnabled();
  await validate.click();
  return requiredHref(page.getByRole("link", { name: "查看本次验证进度" }));
}

async function waitForRollbackSubmit(page: Page, rollbackHref: string): Promise<void> {
  await expect
    .poll(
      async () => {
        await page.goto(rollbackHref);
        return page.getByRole("button", { name: "提交独立人工审批" }).isEnabled();
      },
      { intervals: [100, 200, 500], timeout: 30_000 },
    )
    .toBe(true);
  await expect(page.getByRole("link", { name: "查看完整验证依据" })).toBeVisible();
}

async function submitRollback(page: Page): Promise<string> {
  await page.getByRole("button", { name: "提交独立人工审批" }).click();
  await expect(page.getByText("等待审批", { exact: true }).first()).toBeVisible();
  return requiredHref(page.getByRole("link", { name: "查看审批详情" }));
}

async function applyRollback(page: Page, rollbackHref: string): Promise<void> {
  await page.goto(rollbackHref);
  const apply = page.getByRole("button", { name: "应用已批准的回退" });
  await expect(apply).toBeEnabled();
  await apply.click();
  await expect(page.getByRole("dialog", { name: "确认应用已批准的版本回退？" })).toBeVisible();
  await page.getByRole("button", { name: "确认应用回退" }).click();
  await expect(page.getByRole("heading", { name: "版本回退已完成" })).toBeVisible();
}

test.describe("journey-b-liveops", () => {
  test.describe.configure({ mode: "serial" });
  test.setTimeout(300_000);

  test.beforeAll(async () => {
    apiPort = await availableLoopbackPort();
    do vitePort = await availableLoopbackPort();
    while (vitePort === apiPort);
    apiUrl = `http://127.0.0.1:${apiPort}`;
    journeyBaseURL = `https://127.0.0.1:${vitePort}`;
    workspace = await mkdtemp(join(tmpdir(), "gameforge-journey-b-"));
    await startBackend("disabled");
    await startVite();
  });

  test.afterAll(async () => {
    try {
      await stopVite();
    } finally {
      try {
        await stopBackend();
      } finally {
        if (workspace) await rm(workspace, { force: true, recursive: true });
      }
    }
  });

  test("proves human Patch, rollback, failure, conflict, and reconnect over real authority", async ({
    browser,
  }) => {
    const unexpectedRequests = new Set<string>();
    const makerContext = await browser.newContext({
      baseURL: journeyBaseURL,
      ignoreHTTPSErrors: true,
    });
    const approverContext = await browser.newContext({
      baseURL: journeyBaseURL,
      ignoreHTTPSErrors: true,
    });
    await guardExternalEgress(makerContext, unexpectedRequests);
    await guardExternalEgress(approverContext, unexpectedRequests);
    const makerPage = await makerContext.newPage();
    const approverPage = await approverContext.newPage();

    try {
      await login(makerPage, makerCredentials);
      await login(approverPage, approverCredentials);

      await test.step("happy Patch waits queued, reconnects SSE, and exposes exact evidence", async () => {
        const reviewExpectation: ApprovalReviewExpectation = {
          after: 80,
          before: 120,
          kind: "patch",
          refName,
          target: "q:1.reward_gold",
        };
        const patchHref = await draftPatch(makerPage, {
          diffAfter: "80",
          diffPath: "/entities/q:1/attrs/reward_gold",
          operation: {
            new_value: 80,
            old_value: 120,
            op: "set_entity_attr",
            op_id: "set-reward-gold",
            target: "q:1.reward_gold",
          },
          rationale: "Reduce the quest reward while preserving the balanced economy.",
          sideEffectRisk: "low",
        });
        const runHref = await startPatchValidation(makerPage);
        await makerPage.goto(runHref);
        await expect(makerPage.getByText(/^已进入队列 · /u)).toBeVisible();
        const runId = decodeURIComponent(new URL(runHref, journeyBaseURL).pathname.split("/").pop() ?? "");
        const cursorKey = `gameforge.run-events.last-event-id:${runId}`;
        const queuedCursor = await makerPage.evaluate((key) => sessionStorage.getItem(key), cursorKey);
        expect(queuedCursor).not.toBeNull();

        await stopVite();
        await expect(makerPage.getByRole("button", { name: "重新连接事件流" })).toBeVisible({
          timeout: 15_000,
        });
        await stopBackend();
        await startBackend("enabled");
        await startVite();
        const resumedRequest = makerPage.waitForRequest((request) =>
          new URL(request.url()).pathname.endsWith(`/runs/${encodeURIComponent(runId)}/events`),
        );
        await makerPage.getByRole("button", { name: "重新连接事件流" }).click();
        expect((await (await resumedRequest).allHeaders())["last-event-id"]).toBe(queuedCursor);
        await expect(makerPage.getByText(/^运行已完成 · /u)).toBeVisible({
          timeout: 30_000,
        });
        await expect(makerPage.getByText(/^已进入队列 · /u)).toHaveCount(1);
        await expect(makerPage.getByLabel("结果清单 payload")).toContainText(
          '"outcome_code": "patch_validation_passed"',
        );
        await expect(makerPage.getByLabel("结果清单 payload")).toContainText('"produced_artifact_count": 3');

        const traceLink = makerPage.getByRole("link", { name: /^查看运行追踪/u }).first();
        await expect(traceLink).toBeVisible();
        await traceLink.click();
        await expect(makerPage.getByRole("heading", { level: 1, name: "运行追踪" })).toBeVisible();
        await expect(makerPage.getByRole("heading", { exact: true, name: "运行日志" })).toBeVisible();

        await waitForPatchSubmit(makerPage, patchHref);
        const companionLinks = makerPage.getByRole("link", {
          name: /^查看回归验证依据 \d+$/u,
        });
        await expect(companionLinks).toHaveCount(2);
        const evidenceLink = makerPage.getByRole("link", {
          name: "查看完整验证依据",
        });
        const evidenceHref = await requiredHref(evidenceLink);
        const companionHrefs: string[] = [];
        for (let index = 0; index < 2; index += 1) {
          companionHrefs.push(await requiredHref(companionLinks.nth(index)));
        }
        for (const companionHref of companionHrefs) {
          await makerPage.goto(companionHref);
          await makerPage.getByText("查看记录技术信息", { exact: true }).click();
          await expect(makerPage.getByText("regression_evidence", { exact: true }).first()).toBeVisible();
        }
        await makerPage.goto(evidenceHref);
        await makerPage.getByRole("link", { name: "打开独立血缘视图" }).click();
        await expect(makerPage.getByRole("table", { name: "内容来源（分页）" })).toBeVisible();

        await makerPage.goto(patchHref);
        const approvalHref = await submitPatch(makerPage);
        await makerPage.goto(approvalHref);
        await expect(
          makerPage
            .getByText("职责隔离：提议者不能审批自己的提议", {
              exact: true,
            })
            .first(),
        ).toBeVisible();
        await expect(makerPage.getByRole("checkbox", { name: /^选择 /u }).first()).toBeDisabled();
        await expect(makerPage.getByRole("button", { name: "提交批准" })).toBeDisabled();

        const staleApproverContext = await browser.newContext({
          baseURL: journeyBaseURL,
          ignoreHTTPSErrors: true,
        });
        await guardExternalEgress(staleApproverContext, unexpectedRequests);
        const staleApprovalPage = await staleApproverContext.newPage();
        try {
          await login(staleApprovalPage, approverCredentials);
          await prepareApproval(approverPage, approvalHref, reviewExpectation, "independent_review_passed");
          await prepareApproval(staleApprovalPage, approvalHref, reviewExpectation, "stale_parallel_review");
          await confirmApproval(approverPage, reviewExpectation.refName);
          await expect(approverPage.locator("header.gf-approvals__hero")).toContainText("已批准");
          await confirmApproval(staleApprovalPage, reviewExpectation.refName);
          await expect(
            staleApprovalPage.locator('[role="alert"][data-code="revision_conflict"]'),
          ).toBeVisible();
          await staleApprovalPage.getByRole("button", { name: "刷新审批状态" }).click();
          await expect(staleApprovalPage.locator("header.gf-approvals__hero")).toContainText("已批准");
          await expect(staleApprovalPage.getByRole("button", { name: "提交批准" })).toBeDisabled();
        } finally {
          await staleApproverContext.close();
        }

        await applyPatch(approverPage, patchHref);
        await expect(approverPage.getByRole("link", { name: "查看版本历史" }).last()).toBeVisible();
        expect(await currentRevision(approverPage)).toBe(2);
      });

      await test.step("governed rollback revalidates and moves the ref back", async () => {
        const rollbackHref = await draftRollback(makerPage);
        const runHref = await startRollbackValidation(makerPage, rollbackHref);
        await waitForRunSucceeded(makerPage, runHref);
        await waitForRollbackSubmit(makerPage, rollbackHref);
        const approvalHref = await submitRollback(makerPage);
        await approve(
          approverPage,
          approvalHref,
          {
            kind: "rollback",
            reason: "Restore the exact approved baseline.",
            refName,
          },
          "rollback_review_passed",
        );
        await applyRollback(approverPage, rollbackHref);
        await expect(
          approverPage.getByRole("heading", {
            name: "目标版本的来源",
          }),
        ).toBeVisible();
        expect(await currentRevision(approverPage)).toBe(3);
      });

      await test.step("stale approved Patch conflicts into a clean, independently revalidated revision", async () => {
        const proposedHref = await draftPatch(makerPage, {
          diffAfter: "80",
          diffPath: "/entities/q:1/attrs/reward_gold",
          operation: {
            new_value: 80,
            old_value: 120,
            op: "set_entity_attr",
            op_id: "set-reward-gold",
            target: "q:1.reward_gold",
          },
          rationale: "Preferred reward revision for the stale conflict path.",
          sideEffectRisk: "low",
        });
        const proposedRun = await startPatchValidation(makerPage);
        await waitForRunSucceeded(makerPage, proposedRun);
        await waitForPatchSubmit(makerPage, proposedHref);
        const proposedApproval = await submitPatch(makerPage);
        await approve(
          approverPage,
          proposedApproval,
          {
            after: 80,
            before: 120,
            kind: "patch",
            refName,
            target: "q:1.reward_gold",
          },
          "preferred_revision_reviewed",
        );

        const staleMakerContext = await browser.newContext({
          baseURL: journeyBaseURL,
          ignoreHTTPSErrors: true,
        });
        try {
          await guardExternalEgress(staleMakerContext, unexpectedRequests);
          const stalePatchPage = await staleMakerContext.newPage();
          await login(stalePatchPage, makerCredentials);
          await stalePatchPage.goto(proposedHref);
          await expect(
            stalePatchPage.getByRole("button", {
              name: "应用已批准的修改",
            }),
          ).toBeEnabled();

          const interveningHref = await draftPatch(makerPage, {
            diffAfter: "100",
            diffPath: "/entities/q:1/attrs/reward_gold",
            operation: {
              new_value: 100,
              old_value: 120,
              op: "set_entity_attr",
              op_id: "set-reward-gold",
              target: "q:1.reward_gold",
            },
            rationale: "Intervening approved reward revision.",
            sideEffectRisk: "low",
          });
          const interveningRun = await startPatchValidation(makerPage);
          await waitForRunSucceeded(makerPage, interveningRun);
          await waitForPatchSubmit(makerPage, interveningHref);
          const interveningApproval = await submitPatch(makerPage);
          await approve(
            approverPage,
            interveningApproval,
            {
              after: 100,
              before: 120,
              kind: "patch",
              refName,
              target: "q:1.reward_gold",
            },
            "intervening_revision_reviewed",
          );
          await applyPatch(approverPage, interveningHref);
          expect(await currentRevision(approverPage)).toBe(4);

          await stalePatchPage.getByRole("button", { name: "应用已批准的修改" }).click();
          await stalePatchPage.getByRole("button", { name: "确认应用" }).click();
          await expect(stalePatchPage.locator('[role="alert"][data-code="revision_conflict"]')).toBeVisible();
          await stalePatchPage.getByRole("button", { name: "重新读取服务器状态" }).click();
          await expect(
            stalePatchPage.getByRole("heading", {
              name: "草案基于的版本已过期",
            }),
          ).toBeVisible();
          await stalePatchPage.getByRole("button", { name: "重新基于当前版本计算" }).click();
          await expect(stalePatchPage.getByRole("heading", { name: "逐项处理内容冲突" })).toBeVisible();

          const conflicts = stalePatchPage.locator("article.gf-merge-conflict");
          const conflictCount = await conflicts.count();
          expect(conflictCount).toBeGreaterThan(0);
          for (let index = 0; index < conflictCount; index += 1) {
            await conflicts.nth(index).getByRole("radio", { name: "采用这份草案的修改" }).check();
          }
          await stalePatchPage.getByRole("button", { name: "保存全部冲突处理结果" }).click();
          await expect(
            stalePatchPage.getByRole("heading", {
              name: "已创建独立的新版本",
            }),
          ).toBeVisible();
          await expect(stalePatchPage.getByText(/旧验证、证据与审批决定不会继承/u)).toBeVisible();
          const replacementHref = await requiredHref(
            stalePatchPage.getByRole("link", { name: "打开新修改草案" }),
          );
          await stalePatchPage.goto(replacementHref);
          await expect(stalePatchPage.getByText(/^第 2 版 · 保留历史/u)).toBeVisible();
          await expect(stalePatchPage.getByText(/尚无验证证据/u)).toBeVisible();
          await expect(
            stalePatchPage.getByRole("button", {
              name: "提交独立审批",
            }),
          ).toBeDisabled();

          const replacementRun = await startPatchValidation(stalePatchPage);
          await waitForRunSucceeded(stalePatchPage, replacementRun);
          await waitForPatchSubmit(stalePatchPage, replacementHref);
          const replacementApproval = await submitPatch(stalePatchPage);
          await approve(
            approverPage,
            replacementApproval,
            {
              after: 80,
              before: 100,
              kind: "patch",
              refName,
              target: "resolved-subgraph",
            },
            "rebased_revision_reviewed",
          );
          await applyPatch(approverPage, replacementHref);
          expect(await currentRevision(approverPage)).toBe(5);
        } finally {
          await staleMakerContext.close();
        }
      });

      await test.step("regression Patch publishes a failed EvidenceSet and Finding without moving ref", async () => {
        const historyBeforeFailure = await refHistorySnapshot(makerPage);
        const failedHref = await draftPatch(makerPage, {
          diffAfter: "monster:ghost",
          diffPath: "/relations/r:dangling",
          operation: {
            new_value: {
              dst_id: "q:1",
              id: "r:dangling",
              src_id: "monster:ghost",
              type: "DROPS_FROM",
            },
            old_value: null,
            op: "add_relation",
            op_id: "add-dangling-drop",
            target: "r:dangling",
          },
          rationale: "Introduce a deterministic dangling relation regression.",
          sideEffectRisk: "high",
        });
        const runHref = await startPatchValidation(makerPage);
        await waitForRunSucceeded(makerPage, runHref);
        await expect(makerPage.getByLabel("结果清单 payload")).toContainText(
          '"outcome_code": "patch_validation_failed"',
        );
        const findings = makerPage.getByRole("heading", { name: "发现的问题" }).locator("xpath=..");
        await expect(findings).toBeVisible();
        await expect(findings.getByRole("link")).toHaveCount(1);

        await makerPage.goto(failedHref);
        await expect(makerPage.getByText("检查未通过", { exact: true }).first()).toBeVisible();
        await expect(makerPage.getByRole("link", { name: "查看完整验证依据" })).toBeVisible();
        await expect(
          makerPage.getByRole("button", {
            name: "提交独立审批",
          }),
        ).toBeDisabled();
        await expect(makerPage.getByRole("button", { name: "应用已批准的修改" })).toBeDisabled();
        expect(await refHistorySnapshot(makerPage)).toEqual(historyBeforeFailure);
      });

      expect([...unexpectedRequests]).toEqual([]);
    } finally {
      await makerContext.close();
      await approverContext.close();
    }
  });
});
