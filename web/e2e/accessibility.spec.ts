import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

import {
  installV3VisualBoundary,
  settleV3Page,
  V3_STABLE_PAGES,
  V3_VIEWPORTS,
  type V3PageCase,
  type V3StablePage,
  type V3VisualTheme,
  type V3VisualViewport,
} from "./support/v3-visual-fixtures";

const desktop = V3_VIEWPORTS.find((viewport) => viewport.id === "1440x900");
const mobile = V3_VIEWPORTS.find((viewport) => viewport.id === "390x844");

if (desktop === undefined || mobile === undefined) {
  throw new Error("The accessibility suite requires the frozen 1440x900 and 390x844 V3 viewports.");
}

function pageCase(stablePage: V3StablePage, theme: V3VisualTheme, viewport: V3VisualViewport): V3PageCase {
  return {
    ...stablePage,
    name: `a11y-${stablePage.id}-${theme}-${viewport.id}`,
    theme,
    viewport: { height: viewport.height, width: viewport.width },
    viewportId: viewport.id,
  };
}

const axeCases = V3_STABLE_PAGES.flatMap((stablePage) =>
  (["light", "dark"] as const).map((theme) => pageCase(stablePage, theme, desktop)),
);

if (axeCases.length !== 16) throw new Error("The desktop WCAG-AA matrix must contain exactly 16 cases.");

function annotateFixtureAuthority() {
  test.info().annotations.push({
    description: "Synthetic, read-only and non-authoritative accessibility fixture data.",
    type: "fixture-authority",
  });
}

async function assertHeadingOrder(page: Page) {
  const headings = await page.getByRole("heading").evaluateAll((nodes) =>
    nodes.map((node) => ({
      level: Number(node.getAttribute("aria-level") ?? node.tagName.slice(1)),
      text: node.textContent?.trim() ?? "",
    })),
  );
  const skippedLevels = headings.slice(1).flatMap((heading, index) => {
    const previous = headings[index]!;
    return heading.level > previous.level + 1 ? [{ from: previous, to: heading }] : [];
  });

  expect.soft(headings[0]?.level, "The first exposed heading must be the page h1.").toBe(1);
  expect.soft(skippedLevels, "Visible headings must not skip a level when descending.").toEqual([]);
}

async function assertSettledLiveRegionRestraint(page: Page) {
  const unexpected = await page
    .locator('[role="status"], [aria-live]:not([aria-live="off"])')
    .evaluateAll((nodes) =>
      nodes.flatMap((node) => {
        const dormantCopyFeedback =
          node.matches('.gf-copyable > .u-sr-only[aria-live="polite"]') &&
          (node.textContent?.trim() ?? "") === "";
        if (dormantCopyFeedback) return [];
        return [
          {
            ariaLive: node.getAttribute("aria-live"),
            className: node.getAttribute("class"),
            role: node.getAttribute("role"),
            text: node.textContent?.trim().replace(/\s+/g, " ") ?? "",
          },
        ];
      }),
    );

  expect
    .soft(
      unexpected,
      "A settled read-only page may keep only empty copy-feedback live regions; static labels must not announce.",
    )
    .toEqual([]);
}

test.describe("@a11y WCAG-AA stable product pages", () => {
  for (const current of axeCases) {
    test(`${current.name} passes Axe and structural semantics`, async ({ baseURL, page }) => {
      annotateFixtureAuthority();
      await page.setViewportSize(current.viewport);
      const boundary = await installV3VisualBoundary(page, baseURL, current.theme);

      await page.goto(current.route);
      await settleV3Page(page, current);

      await expect
        .soft(page.getByRole("main"), "Each product route must expose one main landmark.")
        .toHaveCount(1);
      await expect
        .soft(page.getByRole("heading", { level: 1 }), "Each product route must expose one page h1.")
        .toHaveCount(1);
      await assertHeadingOrder(page);
      await assertSettledLiveRegionRestraint(page);

      const result = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
        .analyze();
      const violations = result.violations.map((violation) => ({
        id: violation.id,
        impact: violation.impact,
        nodes: violation.nodes.map((node) => ({
          failureSummary: node.failureSummary,
          target: node.target,
        })),
      }));
      expect.soft(violations, "Axe must report no WCAG A/AA violations.").toEqual([]);

      boundary.assertClean();
    });
  }
});

test.describe("@a11y keyboard and browser behavior", () => {
  test("skip navigation is first, visibly focused, and moves focus to main", async ({ baseURL, page }) => {
    annotateFixtureAuthority();
    const current = pageCase(V3_STABLE_PAGES[0], "light", desktop);
    await page.setViewportSize(current.viewport);
    const boundary = await installV3VisualBoundary(page, baseURL, current.theme);
    await page.goto(current.route);
    await settleV3Page(page, current);

    const skipLink = page.getByRole("link", { name: "跳到主要内容" });
    await expect(page.locator("a[href], button:not([disabled]), input:not([disabled])").first()).toHaveClass(
      /gf-skip-link/,
    );
    await skipLink.focus();
    await expect(skipLink).toBeFocused();
    await expect(skipLink).toBeVisible();
    const focus = await skipLink.evaluate((element) => {
      const style = getComputedStyle(element);
      return { outlineStyle: style.outlineStyle, outlineWidth: style.outlineWidth };
    });
    expect(focus.outlineStyle).not.toBe("none");
    expect(Number.parseFloat(focus.outlineWidth)).toBeGreaterThanOrEqual(2);

    await page.keyboard.press("Enter");
    await expect(page.getByRole("main")).toBeFocused();
    boundary.assertClean();
  });

  test("mobile navigation opens and closes from the keyboard and returns focus", async ({
    baseURL,
    page,
  }) => {
    annotateFixtureAuthority();
    const current = pageCase(V3_STABLE_PAGES[0], "light", mobile);
    await page.setViewportSize(current.viewport);
    const boundary = await installV3VisualBoundary(page, baseURL, current.theme);
    await page.goto(current.route);
    await settleV3Page(page, current);

    const toggle = page.getByRole("button", { name: "打开导航" });
    await toggle.focus();
    await expect(toggle).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("button", { name: "关闭导航" })).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await expect(toggle).toBeFocused();
    boundary.assertClean();
  });

  test("confirmation dialog traps a safe initial focus and returns it on Escape", async ({
    baseURL,
    page,
  }) => {
    annotateFixtureAuthority();
    const current = pageCase(V3_STABLE_PAGES[7], "light", desktop);
    await page.setViewportSize(current.viewport);
    const boundary = await installV3VisualBoundary(page, baseURL, current.theme);
    await page.goto(current.route);
    await settleV3Page(page, current);

    const sharedPage = await page.evaluate(async () => {
      const response = await fetch("/api/v1/approvals?assignee=me&limit=100");
      if (!response.ok) throw new Error(`Unable to read shared approval fixture: ${response.status}`);
      return (await response.json()) as {
        items: Array<{ approval: { approval_id: string } }>;
      };
    });
    const approval = sharedPage.items[0];
    if (approval === undefined) throw new Error("The shared V3 fixture must expose one approval.");
    const approvalId = approval.approval.approval_id;

    await page.route("**/api/v1/approvals/*", async (route) => {
      const url = new URL(route.request().url());
      if (
        route.request().method() === "GET" &&
        decodeURIComponent(url.pathname) === `/api/v1/approvals/${approvalId}`
      ) {
        await route.fulfill({
          body: JSON.stringify(approval),
          headers: {
            "Content-Type": "application/json; charset=utf-8",
            ETag: '"approval:v3-a11y"',
          },
        });
        return;
      }
      await route.fallback();
    });

    await page.getByRole("link", { name: "打开审批详情" }).first().click();
    await expect(page.getByRole("heading", { level: 1, name: "审批详情" })).toBeVisible();
    await page.getByRole("radio", { name: "驳回" }).check();
    await page
      .getByRole("checkbox", { name: /^选择 / })
      .first()
      .check();
    await page.getByLabel("决定原因代码").fill("a11y_focus_return");

    const trigger = page.getByRole("button", { name: "提交驳回" });
    await trigger.click();
    await expect(page.getByRole("dialog", { name: "确认驳回决定" })).toBeVisible();
    await expect(page.getByRole("button", { name: "取消" })).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(trigger).toBeFocused();
    boundary.assertClean();
  });

  test("reduced-motion preference disables the frozen motion tokens in the browser", async ({
    baseURL,
    page,
  }) => {
    annotateFixtureAuthority();
    await page.emulateMedia({ reducedMotion: "reduce" });
    const current = pageCase(V3_STABLE_PAGES[0], "light", desktop);
    await page.setViewportSize(current.viewport);
    const boundary = await installV3VisualBoundary(page, baseURL, current.theme);
    await page.goto(current.route);
    await settleV3Page(page, current);

    const motion = await page.evaluate(() => {
      const rootStyle = getComputedStyle(document.documentElement);
      const buttonStyle = getComputedStyle(document.querySelector("button")!);
      return {
        hoverToken: rootStyle.getPropertyValue("--duration-hover").trim(),
        panelToken: rootStyle.getPropertyValue("--duration-panel").trim(),
        preference: matchMedia("(prefers-reduced-motion: reduce)").matches,
        transitionDurationsMs: buttonStyle.transitionDuration.split(",").map((value) => {
          const duration = value.trim();
          const multiplier = duration.endsWith("ms") ? 1 : 1_000;
          return Number.parseFloat(duration) * multiplier;
        }),
      };
    });
    expect(motion.preference).toBe(true);
    expect(motion.hoverToken).toBe("0ms");
    expect(motion.panelToken).toBe("0ms");
    expect(motion.transitionDurationsMs.every((duration) => duration <= 0.01)).toBe(true);
    boundary.assertClean();
  });

  test("the self-hosted editorial face actually loads and exercises weights 400 and 600", async ({
    baseURL,
    page,
  }) => {
    annotateFixtureAuthority();
    const current = pageCase(V3_STABLE_PAGES[0], "light", desktop);
    await page.setViewportSize(current.viewport);
    const boundary = await installV3VisualBoundary(page, baseURL, current.theme);
    await page.goto(current.route);
    await settleV3Page(page, current);

    const font = await page.evaluate(async () => {
      const sample = "游戏内容正确性编译器";
      const [bodyFaces, headingFaces] = await Promise.all([
        document.fonts.load('400 14px "GameForge Editorial Serif"', sample),
        document.fonts.load('600 22px "GameForge Editorial Serif"', sample),
      ]);
      await document.fonts.ready;
      const bodyStyle = getComputedStyle(document.body);
      const headingStyle = getComputedStyle(document.querySelector("h1")!);
      return {
        bodyFamily: bodyStyle.fontFamily,
        bodyFaces: bodyFaces.length,
        bodyWeight: bodyStyle.fontWeight,
        headingFamily: headingStyle.fontFamily,
        headingFaces: headingFaces.length,
        headingWeight: headingStyle.fontWeight,
      };
    });
    expect(font.bodyFaces).toBeGreaterThan(0);
    expect(font.headingFaces).toBeGreaterThan(0);
    expect(font.bodyFamily).toContain("GameForge Editorial Serif");
    expect(font.headingFamily).toContain("GameForge Editorial Serif");
    expect(font.bodyWeight).toBe("400");
    expect(font.headingWeight).toBe("600");
    boundary.assertClean();
  });
});
