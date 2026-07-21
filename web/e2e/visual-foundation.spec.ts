import { expect, test, type Locator, type Page } from "@playwright/test";

const authenticatedPrincipal = {
  authz_revision: 3,
  credential_epoch: 1,
  display_name: "林澄",
  id: "principal:visual-review",
  kind: "human",
  revision: 3,
  roles: [
    {
      assignment_id: "role:visual-review",
      assignment_schema_version: "role-assignment@1",
      granted_at: "2026-07-19T08:00:00Z",
      granted_by: { principal_id: "system:bootstrap", principal_kind: "system" },
      principal_id: "principal:visual-review",
      revision: 1,
      role: "tooling",
      scope: "all",
      status: "active",
    },
  ],
  status: "active",
};

const matrix = [
  { height: 900, name: "components-light-1440x900", theme: "light", view: "components", width: 1440 },
  { height: 720, name: "kg-dark-1280x720", theme: "dark", view: "kg", width: 1280 },
  {
    height: 844,
    name: "trace-generic-light-390x844",
    theme: "light",
    view: "trace-generic",
    width: 390,
  },
  {
    height: 915,
    name: "trace-aureus-dark-412x915",
    theme: "dark",
    view: "trace-aureus",
    width: 412,
  },
  {
    height: 900,
    name: "trace-fallback-light-1440x900",
    theme: "light",
    view: "trace-fallback",
    width: 1440,
  },
  {
    height: 720,
    name: "states-reduced-motion-dark-1280x720",
    theme: "dark",
    view: "states",
    width: 1280,
  },
] as const;

async function installVisualReviewBoundary(page: Page, baseURL: string | undefined, theme: "light" | "dark") {
  const expectedOrigin = new URL(baseURL ?? "https://127.0.0.1:4173").origin;
  const externalOrigins = new Set<string>();

  await page.addInitScript((selectedTheme) => {
    window.localStorage.setItem("gameforge.theme", selectedTheme);
  }, theme);
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if ((url.protocol === "http:" || url.protocol === "https:") && url.origin !== expectedOrigin) {
      externalOrigins.add(url.origin);
      await route.abort();
      return;
    }
    await route.continue();
  });
  await page.route("**/api/v1/auth/me", async (route) => {
    await route.fulfill({ body: JSON.stringify(authenticatedPrincipal), contentType: "application/json" });
  });

  return externalOrigins;
}

async function expectContained(child: Locator, parent: Locator, label: string) {
  const childBox = await child.boundingBox();
  const parentBox = await parent.boundingBox();
  expect(childBox, `${label} must be visible`).not.toBeNull();
  expect(parentBox, `${label} container must be visible`).not.toBeNull();
  expect(childBox!.x, `${label} must stay inside its container`).toBeGreaterThanOrEqual(parentBox!.x - 1);
  expect(childBox!.y, `${label} must stay inside its container`).toBeGreaterThanOrEqual(parentBox!.y - 1);
  expect(childBox!.x + childBox!.width, `${label} must stay inside its container`).toBeLessThanOrEqual(
    parentBox!.x + parentBox!.width + 1,
  );
  expect(childBox!.y + childBox!.height, `${label} must stay inside its container`).toBeLessThanOrEqual(
    parentBox!.y + parentBox!.height + 1,
  );
}

async function expectPairwiseNonOverlapping(locator: Locator, label: string) {
  const boxes = await Promise.all((await locator.all()).map((item) => item.boundingBox()));
  const visible = boxes.filter((box) => box !== null);
  for (let leftIndex = 0; leftIndex < visible.length; leftIndex += 1) {
    for (let rightIndex = leftIndex + 1; rightIndex < visible.length; rightIndex += 1) {
      const left = visible[leftIndex]!;
      const right = visible[rightIndex]!;
      const overlapsHorizontally = left.x < right.x + right.width - 1 && right.x < left.x + left.width - 1;
      const overlapsVertically = left.y < right.y + right.height - 1 && right.y < left.y + left.height - 1;
      expect(overlapsHorizontally && overlapsVertically, `${label} must not overlap`).toBe(false);
    }
  }
}

async function expectStableDimensions(page: Page, locator: Locator, label: string) {
  const before = await Promise.all((await locator.all()).map((item) => item.boundingBox()));
  await page.evaluate(async () => {
    await new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    });
  });
  const after = await Promise.all((await locator.all()).map((item) => item.boundingBox()));
  expect(after).toHaveLength(before.length);
  for (let index = 0; index < before.length; index += 1) {
    expect(before[index], `${label} ${index + 1} must be visible before settling`).not.toBeNull();
    expect(after[index], `${label} ${index + 1} must remain visible after settling`).not.toBeNull();
    expect(after[index]!.width, `${label} ${index + 1} width must be stable`).toBeCloseTo(
      before[index]!.width,
      1,
    );
    expect(after[index]!.height, `${label} ${index + 1} height must be stable`).toBeCloseTo(
      before[index]!.height,
      1,
    );
  }
}

async function assertTargetedGeometry(page: Page, view: (typeof matrix)[number]["view"]) {
  if (view === "components") {
    const diffScroller = page.locator(".gf-diff__scroll");
    await expect(diffScroller).toHaveAttribute("tabindex", "0");
    await expectContained(diffScroller, page.locator(".gf-diff"), "Diff scroller");
    const mergeCards = await page.locator(".gf-merge-conflict").all();
    for (const [index, card] of mergeCards.entries()) {
      const scroller = card.locator(".gf-merge-conflict__scroll");
      await expect(scroller).toHaveAttribute("tabindex", "0");
      await expectContained(scroller, card, `Merge scroller ${index + 1}`);
    }
    await expectPairwiseNonOverlapping(page.locator(".gf-merge-conflict"), "Merge cards");
    await expectStableDimensions(page, page.locator(".gf-diff"), "Diff");
    await expectStableDimensions(page, page.locator(".gf-merge-conflict"), "Merge conflict");
  }
  if (view === "kg") {
    const canvas = page.locator(".gf-kg__canvas-panel");
    const inspector = page.locator(".gf-kg__inspector");
    await expectContained(canvas, page.locator(".gf-kg__workspace"), "Graph canvas panel");
    await expectContained(inspector, page.locator(".gf-kg__workspace"), "Graph inspector");
    const canvasBox = await canvas.boundingBox();
    const inspectorBox = await inspector.boundingBox();
    expect(inspectorBox!.x, "Graph inspector must not overlap the canvas").toBeGreaterThanOrEqual(
      canvasBox!.x + canvasBox!.width - 1,
    );
    const tableScroller = page.locator(".gf-kg__table-scroll");
    await expect(tableScroller).toHaveAttribute("tabindex", "0");
    await expectContained(tableScroller, page.locator(".gf-kg__list"), "Graph fact table scroller");
    await expectStableDimensions(
      page,
      page.locator('[data-testid="knowledge-graph-canvas"]'),
      "Graph canvas",
    );
  }
  if (view.startsWith("trace-")) {
    await expectContained(page.locator(".gf-trace__transport"), page.locator(".gf-trace"), "Trace transport");
    await expectPairwiseNonOverlapping(
      page.locator(".gf-trace__transport-buttons button"),
      "Trace transport buttons",
    );
    await expectPairwiseNonOverlapping(
      page.locator(".gf-trace__transport-position, .gf-trace__speed"),
      "Trace position and speed controls",
    );
  }
  if (view === "states") {
    await expectContained(
      page.locator(".gf-cursor-table__scroll"),
      page.locator(".gf-cursor-table"),
      "Long-content table scroller",
    );
  }
}

async function settleVisuals(page: Page, view: (typeof matrix)[number]["view"]) {
  await expect(page.getByRole("heading", { level: 1, name: "Editorial 视觉基础" })).toBeVisible();
  await expect(page.getByText("视觉评审数据，不是权威状态", { exact: true })).toBeVisible();
  await expect(page.locator("[data-visual-foundation-view]")).toHaveAttribute(
    "data-visual-foundation-view",
    view,
  );
  if (view === "kg") {
    await expect(page.locator('[data-testid="knowledge-graph-canvas"] canvas').first()).toBeVisible();
  }
  if (view === "trace-fallback") {
    const terminalFrame = page.getByRole("button", { name: /第 4 帧，Tick 7/ });
    await terminalFrame.evaluate((element) => (element as HTMLButtonElement).click());
    await expect(terminalFrame).toHaveAttribute("aria-current", "true");
    await page.locator(".gf-trace__timeline > ol").evaluate((element) => {
      element.scrollTop = element.scrollHeight;
    });
    await page.evaluate(() => {
      if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
      window.scrollTo(0, 0);
    });
  }
  if (view === "states") {
    await expect(page.getByTestId("visual-state-streaming")).toBeVisible();
    await expect(page.getByTestId("visual-state-error")).toBeVisible();
    await expect(page.getByTestId("visual-state-empty")).toBeVisible();
    await expect(page.getByText("正在加载…", { exact: true })).toBeVisible();
    await expect(page.getByText("artifact:" + "a".repeat(512), { exact: true })).toBeVisible();
    const motion = await page.evaluate(() => ({
      hover: getComputedStyle(document.documentElement).getPropertyValue("--duration-hover").trim(),
      reduced: matchMedia("(prefers-reduced-motion: reduce)").matches,
    }));
    expect(motion.reduced).toBe(true);
    expect(["0ms", "0.01ms"]).toContain(motion.hover);
  }
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
    });
  });
}

test.describe("@visual visual-foundation", () => {
  for (const shot of matrix) {
    test(`${shot.name} uses the real shell without external egress or horizontal overflow`, async ({
      baseURL,
      page,
    }) => {
      await page.setViewportSize({ height: shot.height, width: shot.width });
      if (shot.view === "states") await page.emulateMedia({ reducedMotion: "reduce" });
      const externalOrigins = await installVisualReviewBoundary(page, baseURL, shot.theme);

      await page.goto(`/__visual__/foundation?view=${shot.view}`);
      await settleVisuals(page, shot.view);
      await assertTargetedGeometry(page, shot.view);

      const overflow = await page.evaluate(() => ({
        clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
      }));
      expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth);
      expect([...externalOrigins]).toEqual([]);

      await expect(page).toHaveScreenshot(`${shot.name}.png`, {
        animations: "disabled",
        caret: "hide",
        fullPage: true,
        maxDiffPixels: shot.view === "kg" ? 64 : 0,
        scale: "css",
      });
    });
  }
});
