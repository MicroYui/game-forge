import { expect, test } from "@playwright/test";

const smokeRoles = [
  "content_designer",
  "numeric_designer",
  "qa",
  "tooling",
  "constraint_admin",
  "gacha_compliance_reviewer",
  "identity_admin",
] as const;

const authenticatedPrincipal = {
  authz_revision: 1,
  credential_epoch: 1,
  display_name: "林澄",
  id: "principal:smoke",
  kind: "human",
  revision: 1,
  roles: smokeRoles.map(
    (role, index) =>
      ({
        assignment_id: `role:smoke:${index + 1}`,
        assignment_schema_version: "role-assignment@1",
        granted_at: "2026-07-19T00:00:00Z",
        granted_by: { principal_id: "system:bootstrap", principal_kind: "system" },
        principal_id: "principal:smoke",
        revision: 1,
        role,
        scope: "all",
        status: "active",
      }) as const,
  ),
  status: "active",
};

test("loads the console without external font or CDN requests", async ({ page, baseURL }) => {
  const unexpectedOrigins = new Set<string>();
  let editorialFontLoaded = false;
  const expectedOrigin = new URL(baseURL ?? "https://127.0.0.1:4173").origin;

  await page.route("**/api/v1/auth/me", async (route) => {
    await route.fulfill({
      body: JSON.stringify({
        code: "auth_required",
        detail: "Authentication required.",
        instance: "/api/v1/auth/me",
        request_id: "request:smoke",
        status: 401,
        title: "Authentication required",
        type: "about:blank",
      }),
      contentType: "application/problem+json",
      status: 401,
    });
  });

  page.on("request", (request) => {
    const url = new URL(request.url());
    if ((url.protocol === "http:" || url.protocol === "https:") && url.origin !== expectedOrigin) {
      unexpectedOrigins.add(url.origin);
    }
  });
  page.on("response", (response) => {
    if (response.ok() && response.url().includes("gameforge-editorial-serif-vf-subset")) {
      editorialFontLoaded = true;
    }
  });

  await page.goto("/");

  await expect(page.getByRole("heading", { level: 1, name: "登录 GameForge" })).toBeVisible();
  await page.evaluate(async () => {
    await document.fonts.load('400 14px "GameForge Editorial Serif"', "登录");
    await document.fonts.ready;
  });
  expect(
    await page.evaluate(() => document.fonts.check('400 14px "GameForge Editorial Serif"', "登录")),
  ).toBe(true);
  expect(editorialFontLoaded).toBe(true);
  expect([...unexpectedOrigins]).toEqual([]);
});

test("keeps the shell navigation desktop-clean and mobile-operable", async ({ page }) => {
  await page.route("**/api/v1/auth/me", async (route) => {
    await route.fulfill({ body: JSON.stringify(authenticatedPrincipal), contentType: "application/json" });
  });
  await page.goto("/specs");

  const toggle = page.getByRole("button", { name: "打开导航" });
  await expect(page.getByRole("heading", { level: 1, name: "规格与约束快照" })).toBeVisible();
  await expect(toggle).toBeHidden();

  await page.setViewportSize({ height: 844, width: 390 });
  await expect(toggle).toBeVisible();
  const identity = page.locator(".gf-identity");
  await expect(identity).toHaveAttribute("tabindex", "0");
  expect(await identity.evaluate((element) => element.scrollWidth)).toBeGreaterThan(
    await identity.evaluate((element) => element.clientWidth),
  );
  await identity.focus();
  await expect(identity).toBeFocused();
  await toggle.click();
  await expect(page.getByRole("button", { name: "关闭导航" })).toHaveAttribute("aria-expanded", "true");
  await page.getByRole("button", { name: "切换到深色主题" }).focus();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "打开导航" })).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    await page.evaluate(() => document.documentElement.clientWidth),
  );
});
