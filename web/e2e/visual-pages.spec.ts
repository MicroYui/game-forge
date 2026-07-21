import { expect, test } from "@playwright/test";

import {
  assertV3PageGeometry,
  installV3VisualBoundary,
  settleV3Page,
  V3_PAGE_CASES,
} from "./support/v3-visual-fixtures";

test.describe("@visual V3 product-page matrix", () => {
  for (const pageCase of V3_PAGE_CASES) {
    test(`${pageCase.name} renders the real product route`, async ({ baseURL, page }) => {
      test.info().annotations.push({
        description: "Synthetic, read-only and non-authoritative visual fixture data.",
        type: "fixture-authority",
      });

      await page.setViewportSize(pageCase.viewport);
      const boundary = await installV3VisualBoundary(page, baseURL, pageCase.theme);
      await page.goto(pageCase.route);
      await settleV3Page(page, pageCase);
      await assertV3PageGeometry(page, pageCase);

      if (pageCase.id === "reviews" && pageCase.viewport.width <= 412) {
        const snapshotValue = page
          .getByRole("table", { name: "Review Artifact 历史" })
          .getByText("snapshot:v3", { exact: true });
        const snapshotBox = await snapshotValue.boundingBox();
        expect(
          snapshotBox,
          "Review snapshot ID must not collapse into a one-character column",
        ).not.toBeNull();
        expect(snapshotBox!.width).toBeGreaterThanOrEqual(96);
      }

      const overflow = await page.evaluate(() => ({
        clientWidth: document.documentElement.clientWidth,
        scrollWidth: document.documentElement.scrollWidth,
      }));
      expect(overflow.scrollWidth).toBeLessThanOrEqual(overflow.clientWidth);
      boundary.assertClean();

      await expect(page).toHaveScreenshot(`${pageCase.name}.png`, {
        animations: "disabled",
        caret: "hide",
        fullPage: false,
        scale: "css",
      });
    });
  }
});
