// @vitest-environment node

import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, rm, symlink, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  BASE_EVIDENCE_CASES,
  buildVisualEvidenceIndex,
  TARGETED_EVIDENCE_CASES,
} from "./build-visual-evidence-index.mjs";

const temporaryRoots: string[] = [];

function testPng(width: number, height: number): Buffer {
  const png = Buffer.alloc(45);
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(png, 0);
  png.writeUInt32BE(13, 8);
  png.write("IHDR", 12, "ascii");
  png.writeUInt32BE(width, 16);
  png.writeUInt32BE(height, 20);
  png[24] = 8;
  png[25] = 2;
  png.writeUInt32BE(0, 29);
  png.writeUInt32BE(0, 33);
  png.write("IEND", 37, "ascii");
  png.writeUInt32BE(0, 41);
  return png;
}

async function makeEvidenceWorkspace() {
  const root = await mkdtemp(path.join(tmpdir(), "gameforge-v3-evidence-"));
  temporaryRoots.push(root);
  const pageSnapshotsDir = path.join(root, "visual-pages.spec.ts-snapshots");
  const targetedSnapshotsDir = path.join(root, "visual-foundation.spec.ts-snapshots");
  const outputDir = path.join(root, "test-results", "m4d-v3");
  await Promise.all([
    mkdir(pageSnapshotsDir, { recursive: true }),
    mkdir(targetedSnapshotsDir, { recursive: true }),
  ]);

  await Promise.all(
    BASE_EVIDENCE_CASES.map((entry) =>
      writeFile(
        path.join(pageSnapshotsDir, entry.sourceFilename),
        testPng(entry.viewport.width, entry.viewport.height),
      ),
    ),
  );
  await Promise.all(
    TARGETED_EVIDENCE_CASES.map((entry, index) =>
      writeFile(
        path.join(targetedSnapshotsDir, entry.sourceFilename),
        testPng(entry.viewport.width, entry.viewport.height + 200 + index),
      ),
    ),
  );

  return { outputDir, pageSnapshotsDir, root, targetedSnapshotsDir };
}

async function buildIn(workspace: Awaited<ReturnType<typeof makeEvidenceWorkspace>>) {
  return buildVisualEvidenceIndex({
    outputDir: workspace.outputDir,
    pageSnapshotsDir: workspace.pageSnapshotsDir,
    targetedSnapshotsDir: workspace.targetedSnapshotsDir,
  });
}

afterEach(async () => {
  await Promise.all(temporaryRoots.splice(0).map((root) => rm(root, { force: true, recursive: true })));
});

describe("buildVisualEvidenceIndex", () => {
  it("builds a deterministic 64-shot matrix, six targeted fixtures and compact HTML index", async () => {
    const workspace = await makeEvidenceWorkspace();

    await buildIn(workspace);

    const firstManifest = await readFile(path.join(workspace.outputDir, "manifest.json"), "utf8");
    const firstIndex = await readFile(path.join(workspace.outputDir, "index.html"), "utf8");
    const manifest = JSON.parse(firstManifest);

    expect(manifest.schema_version).toBe("m4d-v3-visual-evidence@1");
    expect(manifest.summary).toEqual({ base: 64, targeted: 6, total: 70 });
    expect(manifest.base).toHaveLength(64);
    expect(manifest.targeted).toHaveLength(6);
    expect(manifest.base[0]).toMatchObject({
      capture_mode: "viewport",
      capture_source: "real-product-route",
      id: "v3-specs-light-1440x900",
      image_dimensions: { height: 900, width: 1440 },
      route: "/specs",
      theme: "light",
      viewport: { height: 900, width: 1440 },
    });
    expect(manifest.base.at(-1)).toMatchObject({
      id: "v3-approvals-dark-412x915",
      route: "/approvals",
    });
    expect(manifest.targeted.map((entry: { id: string }) => entry.id)).toEqual([
      "components-light-1440x900",
      "kg-dark-1280x720",
      "trace-generic-light-390x844",
      "trace-aureus-dark-412x915",
      "trace-fallback-light-1440x900",
      "states-reduced-motion-dark-1280x720",
    ]);
    expect(manifest.targeted[0]).toMatchObject({
      capture_mode: "full_page",
      image_dimensions: { height: 1100, width: 1440 },
      viewport: { height: 900, width: 1440 },
    });
    expect(manifest.deferred_scope.map((entry: { owner: string }) => entry.owner)).toEqual([
      "M4e",
      "M4e",
      "v-next",
      "M3 follow-up",
      "later interaction authority",
    ]);

    const firstEntry = manifest.base[0];
    const sourceBytes = await readFile(
      path.join(workspace.pageSnapshotsDir, BASE_EVIDENCE_CASES[0].sourceFilename),
    );
    const copiedBytes = await readFile(path.join(workspace.outputDir, firstEntry.output_file));
    expect(copiedBytes).toEqual(sourceBytes);
    expect(firstEntry.sha256).toBe(createHash("sha256").update(sourceBytes).digest("hex"));
    expect(firstEntry.size_bytes).toBe(sourceBytes.byteLength);
    expect(manifest.checks).toEqual({
      base_exact_viewports: "passed",
      catalog: "passed",
      png_signatures: "passed",
      source_sha256: "passed",
      targeted_full_page_bounds: "passed",
    });

    expect(firstIndex).not.toContain("data:image");
    expect(firstIndex.match(/data-evidence-role="overview"/g)).toHaveLength(16);
    expect(firstIndex.match(/data-evidence-role="matrix"/g)).toHaveLength(64);
    expect(firstIndex.match(/data-evidence-role="targeted"/g)).toHaveLength(6);
    expect(firstIndex.match(/<details class="page-matrix"/g)).toHaveLength(8);
    expect(firstIndex.match(/class="evidence-card overview-evidence-card"/g)).toHaveLength(16);
    expect(firstIndex.match(/loading="eager"/g)).toHaveLength(16);
    expect(firstIndex.match(/loading="lazy"/g)).toHaveLength(70);
    expect(firstIndex).toContain("/__visual__/foundation?view=trace-aureus");
    expect(firstIndex).toContain("full-page bounds ✓");
    expect(firstIndex).toContain("synthetic, read-only, non-authoritative");
    expect(firstIndex).toContain("qa.evidence_missing");

    await buildIn(workspace);
    expect(await readFile(path.join(workspace.outputDir, "manifest.json"), "utf8")).toBe(firstManifest);
    expect(await readFile(path.join(workspace.outputDir, "index.html"), "utf8")).toBe(firstIndex);
  });

  it("rejects a missing tuple before replacing an existing output", async () => {
    const workspace = await makeEvidenceWorkspace();
    const sentinel = path.join(workspace.outputDir, "keep.txt");
    await mkdir(workspace.outputDir, { recursive: true });
    await writeFile(sentinel, "previous complete evidence");
    await unlink(path.join(workspace.pageSnapshotsDir, BASE_EVIDENCE_CASES[0].sourceFilename));

    await expect(buildIn(workspace)).rejects.toThrow("Missing snapshot for v3-specs-light-1440x900");
    expect(await readFile(sentinel, "utf8")).toBe("previous complete evidence");
  });

  it("rejects an extra snapshot", async () => {
    const workspace = await makeEvidenceWorkspace();
    await writeFile(path.join(workspace.pageSnapshotsDir, "unexpected.png"), testPng(10, 10));

    await expect(buildIn(workspace)).rejects.toThrow("Unexpected snapshot entry: unexpected.png");
  });

  it("rejects duplicate evidence IDs even when one has a different platform suffix", async () => {
    const workspace = await makeEvidenceWorkspace();
    const duplicate = `${BASE_EVIDENCE_CASES[0].id}-chromium-linux.png`;
    await writeFile(path.join(workspace.pageSnapshotsDir, duplicate), testPng(1440, 900));

    await expect(buildIn(workspace)).rejects.toThrow(`Unexpected snapshot entry: ${duplicate}`);
  });

  it("rejects indirect snapshot paths", async () => {
    const workspace = await makeEvidenceWorkspace();
    const entry = BASE_EVIDENCE_CASES[0];
    const expectedPath = path.join(workspace.pageSnapshotsDir, entry.sourceFilename);
    const outsidePath = path.join(workspace.root, "outside.png");
    await unlink(expectedPath);
    await writeFile(outsidePath, testPng(entry.viewport.width, entry.viewport.height));
    await symlink(outsidePath, expectedPath);

    await expect(buildIn(workspace)).rejects.toThrow("must be a direct regular file");
  });

  it("rejects bytes without the PNG signature", async () => {
    const workspace = await makeEvidenceWorkspace();
    const entry = TARGETED_EVIDENCE_CASES[0];
    await writeFile(
      path.join(workspace.targetedSnapshotsDir, entry.sourceFilename),
      Buffer.from("not a png"),
    );

    await expect(buildIn(workspace)).rejects.toThrow(`Invalid PNG signature for ${entry.sourceFilename}`);
  });

  it("rejects an exact viewport mismatch for a base capture", async () => {
    const workspace = await makeEvidenceWorkspace();
    const entry = BASE_EVIDENCE_CASES[0];
    await writeFile(
      path.join(workspace.pageSnapshotsDir, entry.sourceFilename),
      testPng(entry.viewport.width, entry.viewport.height + 1),
    );

    await expect(buildIn(workspace)).rejects.toThrow(
      `Viewport mismatch for ${entry.sourceFilename}: expected 1440x900, got 1440x901`,
    );
  });

  it("rejects a full-page targeted capture with the wrong width", async () => {
    const workspace = await makeEvidenceWorkspace();
    const entry = TARGETED_EVIDENCE_CASES[0];
    await writeFile(
      path.join(workspace.targetedSnapshotsDir, entry.sourceFilename),
      testPng(entry.viewport.width + 1, entry.viewport.height + 200),
    );

    await expect(buildIn(workspace)).rejects.toThrow(
      `Full-page width mismatch for ${entry.sourceFilename}: expected 1440, got 1441`,
    );
  });

  it("rejects a full-page targeted capture shorter than its viewport", async () => {
    const workspace = await makeEvidenceWorkspace();
    const entry = TARGETED_EVIDENCE_CASES[0];
    await writeFile(
      path.join(workspace.targetedSnapshotsDir, entry.sourceFilename),
      testPng(entry.viewport.width, entry.viewport.height - 1),
    );

    await expect(buildIn(workspace)).rejects.toThrow(
      `Full-page height too small for ${entry.sourceFilename}: minimum 900, got 899`,
    );
  });

  it("rejects an output path that overlaps either source directory", async () => {
    const workspace = await makeEvidenceWorkspace();

    await expect(
      buildVisualEvidenceIndex({
        outputDir: path.join(workspace.pageSnapshotsDir, "generated"),
        pageSnapshotsDir: workspace.pageSnapshotsDir,
        targetedSnapshotsDir: workspace.targetedSnapshotsDir,
      }),
    ).rejects.toThrow("Output directory must not overlap a snapshot source directory");
  });
});
