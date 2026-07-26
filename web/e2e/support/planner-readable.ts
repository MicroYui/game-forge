import { expect, type Page } from "@playwright/test";

/**
 * Identities the product needs and a planner cannot read. Every one of them is a
 * legitimate exact ref — that is why they exist — but they belong inside the
 * collapsed technical panels, not in the sentences a planner reads to decide
 * what to do next.
 */
const INTERNAL_IDENTIFIER = [
  { label: "SHA-256 摘要", pattern: /[0-9a-f]{64}/u },
  {
    label: "不透明资源标识",
    pattern: /\b(?:artifact|sha256|snapshot|extraction|approval|material|run|project):/u,
  },
  { label: "裸 UTC 时间戳", pattern: /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/u },
] as const;

async function plannerVisibleText(page: Page): Promise<string> {
  return page.evaluate(() => {
    const root = document.querySelector("main") ?? document.body;
    const clone = root.cloneNode(true) as HTMLElement;
    // Technical panels are exactly where these identities are supposed to live.
    clone.querySelectorAll("details, .u-sr-only, [hidden]").forEach((node) => node.remove());
    return clone.textContent ?? "";
  });
}

/** Freeze what a planner actually reads on this page. */
export async function expectPlannerReadable(page: Page, where: string): Promise<void> {
  const text = await plannerVisibleText(page);
  for (const { label, pattern } of INTERNAL_IDENTIFIER) {
    const found = text.match(pattern);
    expect(found?.[0] ?? null, `${where}: ${label}出现在策划视野里`).toBeNull();
  }
}
