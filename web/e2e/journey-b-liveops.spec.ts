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
const approverCredentials = { login: "approver", password: "approver-password-1" };

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
      const response = await fetch(`${apiUrl}/readyz`, { signal: AbortSignal.timeout(500) });
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
  await expect(page).toHaveURL(/\/specs$/u);
  await expect(page.getByRole("heading", { level: 1, name: "规格与约束快照" })).toBeVisible();
}

async function refHistorySnapshot(page: Page): Promise<{ current: string; entries: string[] }> {
  await page.goto("/refs/content-head/history");
  const current = page.getByText(/^Current · revision \d+$/u).first();
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
  const match = /revision (\d+)$/u.exec(history.current);
  if (!match) throw new Error(`Could not parse current ref revision from ${history.current}.`);
  return Number(match[1]);
}

async function openCurrentSpec(page: Page): Promise<void> {
  await page.goto("/specs");
  const currentRow = page
    .getByRole("row")
    .filter({ has: page.getByRole("link", { name: /^content-head · revision \d+$/u }) });
  await expect(currentRow).toHaveCount(1);
  await currentRow.getByRole("link", { name: "检查规格与图谱" }).click();
  await expect(page.getByLabel("Ref name")).toHaveValue("content-head");
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
  await page.getByLabel("Patch operations JSON").fill(JSON.stringify([input.operation], null, 2));
  await page.getByLabel("Patch rationale").fill(input.rationale);
  await page.getByLabel("Side-effect risk").fill(input.sideEffectRisk);
  await page.getByRole("button", { name: "创建 Patch 草案" }).click();
  const link = page.getByRole("link", { name: "打开 Patch 草案" });
  const href = await requiredHref(link);
  await link.click();
  await expect(page.getByRole("heading", { name: /^Patch revision 1$/u })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Base / Current / Proposed" })).toBeVisible();
  const diffTable = page.getByRole("table", { name: "字段级快照差异" });
  const diffRow = diffTable.getByRole("row").filter({ hasText: input.diffPath });
  await expect(diffRow).toBeVisible();
  await expect(diffRow.getByRole("cell").last()).toContainText(input.diffAfter);
  return href;
}

async function startPatchValidation(page: Page): Promise<string> {
  await page.getByLabel("Validation policy").selectOption("builtin.validation@1");
  await page
    .getByRole("group", { name: "Validation checker profiles" })
    .getByRole("checkbox", { name: "builtin.checker@1" })
    .check();
  await page
    .getByRole("group", { name: "Validation simulation profiles" })
    .getByRole("checkbox", { name: "builtin.simulation@1" })
    .check();
  await page.getByLabel("Seed").fill("7");
  const validate = page.getByRole("button", { name: "启动 exact validation" });
  await expect(validate).toBeEnabled();
  await validate.click();
  return requiredHref(page.getByRole("link", { name: "打开 accepted Run" }));
}

async function waitForPatchSubmit(page: Page, patchHref: string): Promise<void> {
  await expect
    .poll(
      async () => {
        await page.goto(patchHref);
        return page.getByRole("button", { name: "Submit for independent approval" }).isEnabled();
      },
      { intervals: [100, 200, 500], timeout: 30_000 },
    )
    .toBe(true);
  await expect(page.getByRole("link", { name: /^EvidenceSet · /u })).toBeVisible();
}

async function submitPatch(page: Page): Promise<string> {
  await page.getByRole("button", { name: "Submit for independent approval" }).click();
  await expect(page.getByText("pending_approval", { exact: true }).first()).toBeVisible();
  return requiredHref(page.getByRole("link", { name: "打开审批详情" }));
}

async function prepareApproval(page: Page, approvalHref: string, reason: string): Promise<void> {
  await page.goto(approvalHref);
  const requirement = page.getByRole("checkbox", { name: /^选择 /u }).first();
  await expect(requirement).toBeEnabled();
  await requirement.check();
  await page.getByLabel("决定原因代码").fill(reason);
}

async function approve(page: Page, approvalHref: string, reason: string): Promise<void> {
  await prepareApproval(page, approvalHref, reason);
  await page.getByRole("button", { name: "提交批准" }).click();
  await expect(page.getByText(/^approved · workflow revision \d+$/u)).toBeVisible();
}

async function applyPatch(page: Page, patchHref: string): Promise<void> {
  await page.goto(patchHref);
  const apply = page.getByRole("button", { name: "Apply approved Patch" });
  await expect(apply).toBeEnabled();
  await apply.click();
  await expect(page.getByRole("dialog", { name: "Apply approved Patch?" })).toBeVisible();
  await page.getByRole("button", { name: "确认 Apply" }).click();
  await expect(page.getByRole("heading", { name: "Patch 已通过 ref transition 应用" })).toBeVisible();
}

async function waitForRunSucceeded(page: Page, runHref: string): Promise<void> {
  await page.goto(runHref);
  await expect(page.getByText(/^run\.succeeded · /u)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("heading", { name: /^运行 run:/u })).toBeVisible();
}

async function draftRollback(page: Page): Promise<string> {
  await page.goto("/refs/content-head/history");
  await page.getByRole("radio", { name: /^revision 1 · /u }).check();
  await page.getByLabel("Rollback policy").selectOption("builtin.rollback@1");
  await page.getByLabel("Rollback reason").fill("Restore the exact approved baseline.");
  await page.getByRole("button", { name: "创建 Rollback request" }).click();
  return requiredHref(page.getByRole("link", { name: "打开 Rollback request" }));
}

async function startRollbackValidation(page: Page, rollbackHref: string): Promise<string> {
  await page.goto(rollbackHref);
  await page.getByLabel("Schema compatibility policy").selectOption("builtin.schema_compatibility@1");
  const validate = page.getByRole("button", { name: "启动 rollback validation" });
  await expect(validate).toBeEnabled();
  await validate.click();
  return requiredHref(page.getByRole("link", { name: "打开 accepted Run" }));
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
  await expect(page.getByRole("link", { name: /^EvidenceSet · /u })).toBeVisible();
}

async function submitRollback(page: Page): Promise<string> {
  await page.getByRole("button", { name: "提交独立人工审批" }).click();
  await expect(page.getByText("pending_approval", { exact: true }).first()).toBeVisible();
  return requiredHref(page.getByRole("link", { name: "打开 Approval" }));
}

async function applyRollback(page: Page, rollbackHref: string): Promise<void> {
  await page.goto(rollbackHref);
  const apply = page.getByRole("button", { name: "Apply approved rollback" });
  await expect(apply).toBeEnabled();
  await apply.click();
  await expect(page.getByRole("dialog", { name: "Apply approved rollback?" })).toBeVisible();
  await page.getByRole("button", { name: "确认 Apply rollback" }).click();
  await expect(page.getByRole("heading", { name: "Rollback 已通过 ref transition 应用" })).toBeVisible();
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
    const makerContext = await browser.newContext({ baseURL: journeyBaseURL, ignoreHTTPSErrors: true });
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
        await expect(makerPage.getByText(/^run\.queued · /u)).toBeVisible();
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
        await expect(makerPage.getByText(/^run\.succeeded · /u)).toBeVisible({ timeout: 30_000 });
        await expect(makerPage.getByText(/^run\.queued · /u)).toHaveCount(1);
        await expect(makerPage.getByLabel("结果清单 payload")).toContainText(
          '"outcome_code": "patch_validation_passed"',
        );
        await expect(makerPage.getByLabel("结果清单 payload")).toContainText('"produced_artifact_count": 3');

        const traceLink = makerPage.getByRole("link", { name: /^追踪 /u }).first();
        await expect(traceLink).toBeVisible();
        await traceLink.click();
        await expect(makerPage.getByRole("heading", { level: 1, name: "Trace 详情" })).toBeVisible();
        await expect(makerPage.getByRole("heading", { exact: true, name: "Trace 日志" })).toBeVisible();

        await waitForPatchSubmit(makerPage, patchHref);
        const companionLinks = makerPage.getByRole("link", {
          name: /^Regression \/ companion evidence · /u,
        });
        await expect(companionLinks).toHaveCount(2);
        const evidenceLink = makerPage.getByRole("link", { name: /^EvidenceSet · /u });
        const evidenceHref = await requiredHref(evidenceLink);
        const companionHrefs: string[] = [];
        for (let index = 0; index < 2; index += 1) {
          companionHrefs.push(await requiredHref(companionLinks.nth(index)));
        }
        for (const companionHref of companionHrefs) {
          await makerPage.goto(companionHref);
          await expect(makerPage.getByText("regression_evidence", { exact: true }).first()).toBeVisible();
        }
        await makerPage.goto(evidenceHref);
        await makerPage.getByRole("link", { name: "打开独立血缘视图" }).click();
        await expect(makerPage.getByRole("table", { name: "血缘（有界分页）" })).toBeVisible();

        await makerPage.goto(patchHref);
        const approvalHref = await submitPatch(makerPage);
        await makerPage.goto(approvalHref);
        await expect(
          makerPage.getByText("maker-checker：提议者不能决定自己的提议", { exact: true }).first(),
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
          await prepareApproval(approverPage, approvalHref, "independent_review_passed");
          await prepareApproval(staleApprovalPage, approvalHref, "stale_parallel_review");
          await approverPage.getByRole("button", { name: "提交批准" }).click();
          await expect(approverPage.getByText(/^approved · workflow revision \d+$/u)).toBeVisible();
          await staleApprovalPage.getByRole("button", { name: "提交批准" }).click();
          await expect(
            staleApprovalPage.locator('[role="alert"][data-code="revision_conflict"]'),
          ).toBeVisible();
          await staleApprovalPage.getByRole("button", { name: "刷新审批状态" }).click();
          await expect(staleApprovalPage.getByText(/^approved · workflow revision \d+$/u)).toBeVisible();
          await expect(staleApprovalPage.getByRole("button", { name: "提交批准" })).toBeDisabled();
        } finally {
          await staleApproverContext.close();
        }

        await applyPatch(approverPage, patchHref);
        await expect(approverPage.getByRole("link", { name: "检查 ref history" })).toBeVisible();
        expect(await currentRevision(approverPage)).toBe(2);
      });

      await test.step("governed rollback revalidates and moves the ref back", async () => {
        const rollbackHref = await draftRollback(makerPage);
        const runHref = await startRollbackValidation(makerPage, rollbackHref);
        await waitForRunSucceeded(makerPage, runHref);
        await waitForRollbackSubmit(makerPage, rollbackHref);
        const approvalHref = await submitRollback(makerPage);
        await approve(approverPage, approvalHref, "rollback_review_passed");
        await applyRollback(approverPage, rollbackHref);
        await expect(
          approverPage.getByRole("heading", { name: "Historical target content lineage" }),
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
        await approve(approverPage, proposedApproval, "preferred_revision_reviewed");

        const staleMakerContext = await browser.newContext({
          baseURL: journeyBaseURL,
          ignoreHTTPSErrors: true,
        });
        try {
          await guardExternalEgress(staleMakerContext, unexpectedRequests);
          const stalePatchPage = await staleMakerContext.newPage();
          await login(stalePatchPage, makerCredentials);
          await stalePatchPage.goto(proposedHref);
          await expect(stalePatchPage.getByRole("button", { name: "Apply approved Patch" })).toBeEnabled();

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
          await approve(approverPage, interveningApproval, "intervening_revision_reviewed");
          await applyPatch(approverPage, interveningHref);
          expect(await currentRevision(approverPage)).toBe(4);

          await stalePatchPage.getByRole("button", { name: "Apply approved Patch" }).click();
          await stalePatchPage.getByRole("button", { name: "确认 Apply" }).click();
          await expect(stalePatchPage.locator('[role="alert"][data-code="revision_conflict"]')).toBeVisible();
          await stalePatchPage.getByRole("button", { name: "重新读取 exact server state" }).click();
          await expect(stalePatchPage.getByRole("heading", { name: "Patch target 已 stale" })).toBeVisible();
          await stalePatchPage.getByRole("button", { name: "Rebase 到 exact current ref" }).click();
          await expect(stalePatchPage.getByRole("heading", { name: "三方冲突解析" })).toBeVisible();

          const conflicts = stalePatchPage.locator("article.gf-merge-conflict");
          const conflictCount = await conflicts.count();
          expect(conflictCount).toBeGreaterThan(0);
          for (let index = 0; index < conflictCount; index += 1) {
            await conflicts.nth(index).getByRole("radio", { name: "采用 Proposed" }).check();
          }
          await stalePatchPage.getByRole("button", { name: "提交全部显式 resolutions" }).click();
          await expect(
            stalePatchPage.getByRole("heading", { name: "已创建独立 Patch revision" }),
          ).toBeVisible();
          await expect(stalePatchPage.getByText(/旧验证、证据与审批决定不继承/u)).toBeVisible();
          const replacementHref = await requiredHref(
            stalePatchPage.getByRole("link", { name: "打开新 Patch revision" }),
          );
          await stalePatchPage.goto(replacementHref);
          await expect(stalePatchPage.getByRole("heading", { name: "Patch revision 2" })).toBeVisible();
          await expect(stalePatchPage.getByText(/尚无 EvidenceSet/u)).toBeVisible();
          await expect(
            stalePatchPage.getByRole("button", { name: "Submit for independent approval" }),
          ).toBeDisabled();

          const replacementRun = await startPatchValidation(stalePatchPage);
          await waitForRunSucceeded(stalePatchPage, replacementRun);
          await waitForPatchSubmit(stalePatchPage, replacementHref);
          const replacementApproval = await submitPatch(stalePatchPage);
          await approve(approverPage, replacementApproval, "rebased_revision_reviewed");
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
        await expect(makerPage.getByRole("heading", { name: "Findings" })).toBeVisible();
        await expect(makerPage.getByRole("link", { name: /^checker:builtin\.checker@1:/u })).toBeVisible();

        await makerPage.goto(failedHref);
        await expect(makerPage.getByText("validation_failed", { exact: true }).first()).toBeVisible();
        await expect(makerPage.getByRole("link", { name: /^EvidenceSet · /u })).toBeVisible();
        await expect(
          makerPage.getByRole("button", { name: "Submit for independent approval" }),
        ).toBeDisabled();
        await expect(makerPage.getByRole("button", { name: "Apply approved Patch" })).toBeDisabled();
        expect(await refHistorySnapshot(makerPage)).toEqual(historyBeforeFailure);
      });

      expect([...unexpectedRequests]).toEqual([]);
    } finally {
      await makerContext.close();
      await approverContext.close();
    }
  });
});
