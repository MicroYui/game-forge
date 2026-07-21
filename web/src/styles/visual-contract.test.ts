import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const stylePath = (filename: string) => resolve(process.cwd(), "src", "styles", filename);
const indexHtml = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
const globalCss = readFileSync(stylePath("global.css"), "utf8");
const layoutCss = readFileSync(stylePath("layout.css"), "utf8");
const shellCss = readFileSync(stylePath("shell.css"), "utf8");
const tokensCss = readFileSync(stylePath("tokens.css"), "utf8");
const utilitiesCss = readFileSync(stylePath("utilities.css"), "utf8");

const LIGHT_COLORS = {
  "--bg": "#f3f4f2",
  "--surface": "#fff",
  "--surface-2": "#f7f8f6",
  "--sidebar": "#e8ebe8",
  "--ink": "#222624",
  "--ink-2": "#4f5852",
  "--muted": "#667069",
  "--faint": "#89928b",
  "--line": "#dfe3df",
  "--line-strong": "#cbd1cc",
  "--deterministic": "#216c67",
  "--suggestion": "#956316",
  "--danger": "#b43b2e",
  "--ok": "#4d7955",
  "--info": "#4f63a5",
} as const;

const DARK_COLORS = {
  "--bg": "#141715",
  "--surface": "#1b1f1c",
  "--surface-2": "#222723",
  "--sidebar": "#101311",
  "--ink": "#f1f4f1",
  "--ink-2": "#c8cec9",
  "--muted": "#adb5ae",
  "--faint": "#838c85",
  "--line": "#353b36",
  "--line-strong": "#4a524c",
  "--deterministic": "#69bdb5",
  "--suggestion": "#e2ad55",
  "--danger": "#ef8174",
  "--ok": "#91c498",
  "--info": "#94a8ed",
} as const;

const FOUNDATION_SCALE = {
  "--font-weight-body": "400",
  "--font-weight-heading": "600",
  "--font-size-display": "28px",
  "--font-size-h1": "22px",
  "--font-size-h2": "18px",
  "--font-size-h3": "15px",
  "--font-size-body": "14px",
  "--font-size-small": "12px",
  "--font-size-micro": "11px",
  "--space-1": "4px",
  "--space-2": "8px",
  "--space-3": "12px",
  "--space-4": "16px",
  "--space-5": "24px",
  "--space-6": "32px",
  "--space-7": "48px",
  "--radius-sm": "4px",
  "--radius-md": "8px",
  "--radius-pill": "999px",
  "--shadow-card": "0 1px 3px rgba(20, 35, 28, 0.08)",
  "--duration-hover": "120ms",
  "--duration-panel": "200ms",
  "--ease-standard": "ease",
} as const;

function blockFor(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  expect(match, `missing CSS block ${selector}`).not.toBeNull();
  return match?.[1] ?? "";
}

function customProperty(block: string, property: string): string | undefined {
  const escaped = property.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return block.match(new RegExp(`${escaped}\\s*:\\s*([^;]+);`))?.[1]?.trim();
}

function expectProperties(block: string, properties: Record<string, string>): void {
  for (const [property, value] of Object.entries(properties)) {
    expect(customProperty(block, property), property).toBe(value);
  }
}

function luminance(hex: string): number {
  const normalized = hex === "#fff" ? "#ffffff" : hex;
  const channels = [1, 3, 5].map((offset) => Number.parseInt(normalized.slice(offset, offset + 2), 16) / 255);
  const [red, green, blue] = channels.map((channel) =>
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * red! + 0.7152 * green! + 0.0722 * blue!;
}

function contrastRatio(foreground: string, background: string): number {
  const foregroundLuminance = luminance(foreground);
  const backgroundLuminance = luminance(background);
  return (
    (Math.max(foregroundLuminance, backgroundLuminance) + 0.05) /
    (Math.min(foregroundLuminance, backgroundLuminance) + 0.05)
  );
}

function rulesUsing(css: string, value: string): string[] {
  return [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter((match) => match[2]?.includes(value))
    .map((match) => match[1]!.trim());
}

describe("M4d frozen visual contract", () => {
  it("defines the exact light-first and dark color tokens", () => {
    expectProperties(blockFor(tokensCss, ":root"), LIGHT_COLORS);
    expectProperties(blockFor(tokensCss, ':root[data-theme="dark"]'), DARK_COLORS);
  });

  it("defines the frozen typography, spacing, radius, shadow, and motion scales", () => {
    const root = blockFor(tokensCss, ":root");
    expectProperties(root, FOUNDATION_SCALE);
    expect(customProperty(root, "--font-ui")).toMatch(/^"GameForge Editorial Serif", "Source Han Serif SC"/);
    expect(customProperty(root, "--font-mono")).toMatch(/^"SF Mono", ui-monospace, "JetBrains Mono"/);
  });

  it("provides all categorical, sequential teal, and diverging data-viz roles in both themes", () => {
    const names = [
      "--viz-teal",
      "--viz-amber",
      "--viz-indigo",
      "--viz-sage",
      "--viz-terracotta",
      "--viz-taupe",
      "--viz-sequential-teal-1",
      "--viz-sequential-teal-2",
      "--viz-sequential-teal-3",
      "--viz-sequential-teal-4",
      "--viz-sequential-teal-5",
      "--viz-diverging-terracotta",
      "--viz-diverging-midpoint",
      "--viz-diverging-teal",
    ];

    for (const selector of [":root", ':root[data-theme="dark"]']) {
      const block = blockFor(tokensCss, selector);
      for (const name of names)
        expect(customProperty(block, name), `${selector} ${name}`).toMatch(/^#[0-9a-f]{6}$/);
    }
  });

  it("keeps every readable text and semantic foreground AA on its normal theme surfaces", () => {
    const foregrounds = [
      "--ink",
      "--ink-2",
      "--muted",
      "--deterministic",
      "--suggestion",
      "--danger",
      "--ok",
      "--info",
    ] as const;

    for (const theme of [LIGHT_COLORS, DARK_COLORS]) {
      for (const foreground of foregrounds) {
        for (const background of ["--bg", "--surface"] as const) {
          expect(
            contrastRatio(theme[foreground], theme[background]),
            `${foreground} on ${background}`,
          ).toBeGreaterThanOrEqual(4.5);
        }
      }
    }
  });

  it("gives the brand descriptor the explicit ink-2 foreground contract", () => {
    expect(customProperty(blockFor(shellCss, ".gf-brand__link small"), "color")).toBe("var(--ink-2)");
  });

  it("keeps the brand descriptor AA against its actual sidebar surface in both themes", () => {
    const declaration =
      customProperty(blockFor(shellCss, ".gf-brand__link small"), "color") ??
      customProperty(blockFor(globalCss, "small"), "color");
    const token = declaration?.match(/^var\((--[^)]+)\)$/)?.[1] as keyof typeof LIGHT_COLORS | undefined;
    expect(token).toBeDefined();

    for (const theme of [LIGHT_COLORS, DARK_COLORS]) {
      expect(
        contrastRatio(theme[token!], theme["--sidebar"]),
        `${String(token)} on --sidebar`,
      ).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("self-hosts one Source Han Serif-derived variable face for the exercised 400 and 600 weights", () => {
    expect(globalCss).toMatch(/@font-face\s*\{[^}]*font-family:\s*"GameForge Editorial Serif"/s);
    expect(globalCss).toContain('url("../assets/fonts/gameforge-editorial-serif-vf-subset.woff2")');
    expect(globalCss).toContain('format("woff2-variations")');
    expect(globalCss).toMatch(/font-weight:\s*400 600;/);
    expect(globalCss).toMatch(/font-display:\s*swap;/);
    expect(globalCss).toMatch(/body\s*\{[^}]*font-weight:\s*var\(--font-weight-body\)/s);
    expect(globalCss).toMatch(/:where\(h1, h2, h3\)\s*\{[^}]*font-weight:\s*var\(--font-weight-heading\)/s);
  });

  it("pins the licensed upstream Simplified Chinese variable-font subset", () => {
    const font = readFileSync(
      resolve(process.cwd(), "src", "assets", "fonts", "gameforge-editorial-serif-vf-subset.woff2"),
    );
    const license = readFileSync(
      resolve(process.cwd(), "public", "fonts", "SOURCE-HAN-SERIF-LICENSE.txt"),
      "utf8",
    );
    const notice = readFileSync(
      resolve(process.cwd(), "public", "fonts", "SOURCE-HAN-SERIF-NOTICE.md"),
      "utf8",
    );

    expect(font.byteLength).toBe(216_796);
    expect(createHash("sha256").update(font).digest("hex")).toBe(
      "973876561320484860f563c4c51171125bd3f3f4f845aff0655b4498e15fc1b1",
    );
    expect(license).toContain("SIL OPEN FONT LICENSE Version 1.1");
    expect(notice).toContain("SourceHanSerifCN-VF.otf.woff2");
    expect(notice).toContain("2.003R");
    expect(notice).toContain("GameForge Editorial Serif");
  });

  it("uses fixed typography, zero tracking, and the mono stack for code and identifiers", () => {
    expect(globalCss).toMatch(/body\s*\{[^}]*letter-spacing:\s*0;/s);
    expect(globalCss).toMatch(/:where\(code, kbd, samp, pre\)[^{]*\{[^}]*font-family:\s*var\(--font-mono\)/s);
    expect(globalCss).not.toMatch(/\b(?:vw|clamp)\s*\(/);
  });

  it("reserves faint for disabled or nonessential decoration and keeps small text muted", () => {
    const allCss = [globalCss, layoutCss, utilitiesCss].join("\n");
    const faintSelectors = rulesUsing(allCss, "var(--faint)");
    expect(faintSelectors.length).toBeGreaterThan(0);
    expect(faintSelectors.every((selector) => /disabled|nonessential-decoration/.test(selector))).toBe(true);
    expect(blockFor(utilitiesCss, ".u-small")).toContain("color: var(--muted)");
    expect(blockFor(utilitiesCss, ".u-micro")).toContain("color: var(--muted)");
  });

  it("limits the pill token to chip, status, and toggle selectors", () => {
    const allCss = [globalCss, layoutCss, utilitiesCss].join("\n");
    const pillSelectors = rulesUsing(allCss, "var(--radius-pill)");
    expect(pillSelectors.length).toBeGreaterThan(0);
    expect(pillSelectors.every((selector) => /chip|status|toggle/.test(selector))).toBe(true);
  });

  it("caps cards and panels at eight pixels and disables nonessential motion on request", () => {
    expect(layoutCss).toMatch(/\.gf-card[^{]*\{[^}]*border-radius:\s*var\(--radius-md\)/s);
    expect(layoutCss).toMatch(/\.gf-(?:modal|tool-panel)[^{]*\{[^}]*border-radius:\s*var\(--radius-md\)/s);
    expect(globalCss).toMatch(
      /@media\s*\(prefers-reduced-motion:\s*reduce\)[^{]*\{[\s\S]*--duration-hover:\s*0ms;/,
    );
    expect(globalCss).toMatch(
      /@media\s*\(prefers-reduced-motion:\s*reduce\)[^{]*\{[\s\S]*--duration-panel:\s*0ms;/,
    );
    expect(globalCss).toMatch(
      /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*scroll-behavior:\s*auto\s*!important;/,
    );
  });

  it("keeps component-facing CSS free of hard-coded color drift", () => {
    for (const css of [globalCss, layoutCss, shellCss, utilitiesCss]) {
      expect(css).not.toMatch(/#[0-9a-f]{3,8}\b/i);
    }
  });

  it("styles the real shell, auth, feedback, and responsive navigation surfaces", () => {
    for (const selector of [
      ".gf-brand",
      ".gf-primary-nav",
      ".gf-nav-link.is-active",
      ".gf-topbar",
      ".gf-icon-button",
      ".gf-auth-page",
      ".gf-toast-viewport",
      ".gf-dialog-backdrop",
      ".gf-state-panel",
      ".gf-problem",
      ".gf-form",
    ]) {
      expect(shellCss, selector).toContain(selector);
    }
    expect(shellCss).toMatch(/@media\s*\(max-width:\s*880px\)/);
    expect(shellCss).toContain('[data-navigation-open="true"] .gf-primary-nav');
  });

  it("keeps focused icon-button tooltips readable at narrow widths", () => {
    expect(customProperty(blockFor(shellCss, ".gf-icon-button[data-tooltip]::after"), "white-space")).toBe(
      "nowrap",
    );
  });

  it("runs the local theme bootstrap before the application module", () => {
    const bootstrapIndex = indexHtml.indexOf('<script src="/theme-bootstrap.js"></script>');
    const applicationIndex = indexHtml.indexOf('<script type="module" src="/src/main.tsx"></script>');
    const bootstrap = readFileSync(resolve(process.cwd(), "public", "theme-bootstrap.js"), "utf8");

    expect(bootstrapIndex).toBeGreaterThan(0);
    expect(applicationIndex).toBeGreaterThan(bootstrapIndex);
    expect(bootstrap).toContain('localStorage.getItem("gameforge.theme")');
    expect(bootstrap).toContain('matchMedia("(prefers-color-scheme: dark)")');
    expect(bootstrap).toContain("document.documentElement.dataset.theme = theme");
  });
});
