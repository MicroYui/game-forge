import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

type PackageManifest = {
  packageManager?: string;
  engines?: Record<string, string>;
  scripts?: Record<string, string>;
  overrides?: Record<string, string>;
  dependencies?: Record<string, string>;
  devDependencies?: Record<string, string>;
};

type PackageLock = {
  packages?: Record<string, { resolved?: string; integrity?: string }>;
};

const webRoot = process.cwd();
const manifest = JSON.parse(readFileSync(path.join(webRoot, "package.json"), "utf8")) as PackageManifest;
const packageLock = JSON.parse(readFileSync(path.join(webRoot, "package-lock.json"), "utf8")) as PackageLock;

const expectedScripts = [
  "build",
  "contracts:check",
  "contracts:generate",
  "dev",
  "evidence:index",
  "format:check",
  "preview",
  "test",
  "test:a11y",
  "test:e2e",
  "test:visual",
  "toolchain:check",
  "typecheck",
];

const approvedDependencies = new Set([
  "@tanstack/react-query",
  "cytoscape",
  "eventsource-parser",
  "lucide-react",
  "openapi-fetch",
  "react",
  "react-dom",
  "react-router-dom",
  "recharts",
]);

const approvedDevDependencies = new Set([
  "@axe-core/playwright",
  "@playwright/test",
  "@testing-library/dom",
  "@testing-library/jest-dom",
  "@testing-library/react",
  "@testing-library/user-event",
  "@types/node",
  "@types/react",
  "@types/react-dom",
  "@vitejs/plugin-basic-ssl",
  "@vitejs/plugin-react",
  "jsdom",
  "json-schema-to-typescript",
  "openapi-typescript",
  "prettier",
  "typescript",
  "vite",
  "vitest",
]);

function expectExactVersions(dependencies: Record<string, string> | undefined) {
  expect(dependencies).toBeDefined();
  for (const version of Object.values(dependencies ?? {})) {
    expect(version).toMatch(/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/);
  }
}

describe("the frozen frontend toolchain", () => {
  it("records the exact Node and npm versions", () => {
    expect(readFileSync(path.join(webRoot, ".node-version"), "utf8").trim()).toBe("24.18.0");
    expect(manifest.packageManager).toBe("npm@11.16.0");
    expect(manifest.engines).toEqual({ node: "24.18.0", npm: "11.16.0" });
  });

  it("exposes every declared M4d command", () => {
    expect(Object.keys(manifest.scripts ?? {}).sort()).toEqual(expectedScripts);
  });

  it("uses only approved, exactly pinned direct dependencies", () => {
    expect(new Set(Object.keys(manifest.dependencies ?? {}))).toEqual(approvedDependencies);
    expect(new Set(Object.keys(manifest.devDependencies ?? {}))).toEqual(approvedDevDependencies);
    expectExactVersions(manifest.dependencies);
    expectExactVersions(manifest.devDependencies);
    expect(manifest.overrides).toEqual({ "js-yaml": "4.3.0" });
  });

  it("locks registry packages to the canonical npm registry with SHA-512 integrity", () => {
    const lockedPackages = Object.values(packageLock.packages ?? {}).filter(
      (entry): entry is { resolved: string; integrity: string } =>
        entry.resolved !== undefined && entry.integrity !== undefined,
    );

    expect(lockedPackages.length).toBeGreaterThan(0);
    for (const entry of lockedPackages) {
      expect(entry.resolved).toMatch(/^https:\/\/registry\.npmjs\.org\//);
      expect(entry.integrity).toMatch(/^sha512-/);
    }
  });
});
