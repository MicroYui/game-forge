import { expect, test } from "@playwright/test";

import {
  guardAuthoringEgress,
  loginAuthoringPage,
  startAuthoringStack,
  type AuthoringStack,
} from "./support/authoring-live-stack";

/**
 * Two games, so the shell's selector has something to separate.
 *
 * Every other fixture in this repository creates ONE project, which is exactly why a
 * planner had to discover in a browser that the pages show every game at once: with a
 * single project, an unfiltered list and a correctly filtered one are identical. This
 * spec is the gate that can tell them apart.
 */

const adminCredentials = { login: "admin", password: "admin-password-1" };

let stack: AuthoringStack | undefined;

async function createProject(page: import("@playwright/test").Page, name: string, key: string) {
  await page.goto("/projects");
  await expect(page.getByRole("heading", { name: "游戏项目", level: 1 })).toBeVisible();
  await page.getByLabel("游戏名称").fill(name);
  await page.getByLabel("项目代号").fill(key);
  await page.getByLabel("游戏类型").fill("测试");
  await page.getByLabel("一句话创意").fill(`${name} 的项目粒度门禁夹具。`);
  await page.getByRole("button", { name: "创建并添加材料" }).click();
  await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();
  return new URL(page.url()).pathname.replace(/\/authoring$/u, "");
}

test.describe("project-scope", () => {
  test.describe.configure({ mode: "serial" });
  test.setTimeout(300_000);

  test.beforeAll(async () => {
    test.setTimeout(120_000);
    stack = await startAuthoringStack({
      launcherModule: "tests.e2e.m4d_support.project_live",
      manifestName: "project-live-manifest.json",
      transportLogName: "project-live-transport.log",
      workspacePrefix: "gameforge-project-scope-",
    });
  });

  test.afterAll(async () => {
    await stack?.stop();
  });

  test("separates two games behind one selector", async ({ browser }) => {
    if (stack === undefined) throw new Error("Project scope stack did not start.");
    const unexpectedRequests = new Set<string>();
    const context = await browser.newContext({
      baseURL: stack.baseURL,
      ignoreHTTPSErrors: true,
    });
    await guardAuthoringEgress(context, stack.baseURL, unexpectedRequests);
    const page = await context.newPage();
    page.setDefaultTimeout(15_000);

    try {
      await loginAuthoringPage(page, adminCredentials);
      await createProject(page, "阿尔法计划", "alpha-plan");
      await createProject(page, "贝塔计划", "beta-plan");

      const selector = page.locator(".gf-project-selector select");
      await expect(selector).toBeVisible();
      // The unfiltered option is not a convenience: seeded catalog content belongs to
      // no game, and without it that content would vanish from the product.
      const optionLabels = await selector.locator("option").allInnerTexts();
      expect(optionLabels[0]).toBe("全部游戏");
      expect(new Set(optionLabels.slice(1))).toEqual(new Set(["阿尔法计划", "贝塔计划"]));

      // Selecting a game must survive navigation, because the shell carries it.
      await selector.selectOption({ label: "阿尔法计划" });
      await expect.poll(async () => new URL(page.url()).searchParams.get("project")).not.toBeNull();
      const alphaProject = new URL(page.url()).searchParams.get("project");

      await page.goto("/specs");
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();

      // The API is the authority for what a game contains; assert against it directly
      // rather than against whichever rows a page happens to render.
      const scoped = await page.request.get(
        `/api/v1/artifacts?kind=ir_snapshot&project_id=${encodeURIComponent(alphaProject!)}`,
      );
      expect(scoped.ok()).toBe(true);
      const scopedIds = ((await scoped.json()).items ?? []).map(
        (item: { artifact_id: string }) => item.artifact_id,
      );

      await selector.selectOption({ label: "贝塔计划" });
      await expect.poll(async () => new URL(page.url()).searchParams.get("project")).not.toBe(alphaProject);
      const betaProject = new URL(page.url()).searchParams.get("project");
      const other = await page.request.get(
        `/api/v1/artifacts?kind=ir_snapshot&project_id=${encodeURIComponent(betaProject!)}`,
      );
      expect(other.ok()).toBe(true);
      const otherIds = ((await other.json()).items ?? []).map(
        (item: { artifact_id: string }) => item.artifact_id,
      );

      expect(scopedIds.length).toBeGreaterThan(0);
      expect(otherIds.length).toBeGreaterThan(0);
      expect(scopedIds.filter((id: string) => otherIds.includes(id))).toEqual([]);

      const everything = await page.request.get("/api/v1/artifacts?kind=ir_snapshot");
      const allIds = ((await everything.json()).items ?? []).map(
        (item: { artifact_id: string }) => item.artifact_id,
      );
      // "All games" is a strict superset: it also holds the seeded content no game owns.
      for (const id of [...scopedIds, ...otherIds]) expect(allIds).toContain(id);
      expect(allIds.length).toBeGreaterThan(scopedIds.length + otherIds.length);

      expect([...unexpectedRequests]).toEqual([]);
    } finally {
      await context.close();
    }
  });
});
