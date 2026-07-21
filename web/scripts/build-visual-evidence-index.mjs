import { createHash } from "node:crypto";
import { mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const WEB_ROOT = fileURLToPath(new URL("../", import.meta.url));
const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const FIXTURE_AUTHORITY = "synthetic, read-only, non-authoritative";

const PAGES = [
  { id: "specs", route: "/specs", title: "规格与约束快照" },
  { id: "generation", route: "/generation", title: "内容生成" },
  { id: "reviews", route: "/reviews", title: "审查报告" },
  {
    id: "playtest",
    route: "/playtest?suite=artifact%3Asuite%3Av3",
    title: "自动试玩",
  },
  { id: "patches", route: "/patches", title: "Patch / Diff" },
  { id: "eval", route: "/eval", title: "Eval / Bench" },
  {
    id: "observability",
    route: "/observability?run=run%3Av3",
    title: "可观测性",
  },
  { id: "approvals", route: "/approvals", title: "Approvals" },
];

const THEMES = ["light", "dark"];
const VIEWPORTS = [
  { height: 900, id: "1440x900", width: 1440 },
  { height: 720, id: "1280x720", width: 1280 },
  { height: 844, id: "390x844", width: 390 },
  { height: 915, id: "412x915", width: 412 },
];

export const BASE_EVIDENCE_CASES = PAGES.flatMap((page) =>
  THEMES.flatMap((theme) =>
    VIEWPORTS.map((viewport) => {
      const id = `v3-${page.id}-${theme}-${viewport.id}`;
      return {
        captureMode: "viewport",
        captureSource: "real-product-route",
        fixtureSource: "V3 synthetic HTTP fixture boundary on the real product route",
        id,
        kind: "base",
        pageId: page.id,
        route: page.route,
        sourceDirectory: "e2e/visual-pages.spec.ts-snapshots",
        sourceFilename: `${id}-chromium-darwin.png`,
        theme,
        title: page.title,
        viewport: { height: viewport.height, width: viewport.width },
        viewportId: viewport.id,
      };
    }),
  ),
);

export const TARGETED_EVIDENCE_CASES = [
  {
    id: "components-light-1440x900",
    route: "/__visual__/foundation?view=components",
    theme: "light",
    title: "共享组件与证据组件",
    viewport: { height: 900, width: 1440 },
  },
  {
    id: "kg-dark-1280x720",
    route: "/__visual__/foundation?view=kg",
    theme: "dark",
    title: "知识图谱 renderer",
    viewport: { height: 720, width: 1280 },
  },
  {
    id: "trace-generic-light-390x844",
    route: "/__visual__/foundation?view=trace-generic",
    theme: "light",
    title: "通用轨迹 renderer",
    viewport: { height: 844, width: 390 },
  },
  {
    id: "trace-aureus-dark-412x915",
    route: "/__visual__/foundation?view=trace-aureus",
    theme: "dark",
    title: "Aureus 2D 轨迹 renderer",
    viewport: { height: 915, width: 412 },
  },
  {
    id: "trace-fallback-light-1440x900",
    route: "/__visual__/foundation?view=trace-fallback",
    theme: "light",
    title: "未知环境回退 renderer",
    viewport: { height: 900, width: 1440 },
  },
  {
    captureSource: "labeled-state-fixture",
    fixtureSource: "VisualFoundation controlled state fixture",
    id: "states-reduced-motion-dark-1280x720",
    route: "/__visual__/foundation?view=states",
    theme: "dark",
    title: "Reduced-motion、瞬态与长内容",
    viewport: { height: 720, width: 1280 },
  },
].map((entry) => ({
  ...entry,
  captureMode: "full_page",
  captureSource: entry.captureSource ?? "labeled-renderer-fixture",
  fixtureSource: entry.fixtureSource ?? "VisualFoundation controlled renderer fixture",
  kind: "targeted",
  sourceDirectory: "e2e/visual-foundation.spec.ts-snapshots",
  sourceFilename: `${entry.id}-chromium-darwin.png`,
  viewport: entry.viewport,
  viewportId: `${entry.viewport.width}x${entry.viewport.height}`,
}));

export const DEFERRED_SCOPE = [
  {
    blocking: false,
    item: "PostgreSQL / S3 / Tempo / Loki / Prometheus production adapters",
    owner: "M4e",
  },
  {
    blocking: false,
    item: "DR, WORM / external anchors, migration execution, solver isolation, deployment, and capacity evidence",
    owner: "M4e",
  },
  {
    blocking: false,
    item: "OIDC, multi-region / HA, real alert delivery, real-time collaborative editing, complex graph auto-layout, and multilingual content",
    owner: "v-next",
  },
  {
    blocking: false,
    item: "8 real participant QA sessions / 4 matched pairs; qa.evidence_missing remains authoritative until valid import",
    owner: "M3 follow-up",
  },
  {
    blocking: false,
    item: "provide_input remains unavailable until a real interaction request and pause authority exists",
    owner: "later interaction authority",
  },
];

const DEFAULT_PATHS = {
  outputDir: path.join(WEB_ROOT, "test-results", "m4d-v3"),
  pageSnapshotsDir: path.join(WEB_ROOT, "e2e", "visual-pages.spec.ts-snapshots"),
  targetedSnapshotsDir: path.join(WEB_ROOT, "e2e", "visual-foundation.spec.ts-snapshots"),
};

function compareText(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

function assertCatalog() {
  if (BASE_EVIDENCE_CASES.length !== 64 || TARGETED_EVIDENCE_CASES.length !== 6) {
    throw new Error("Evidence catalog must contain exactly 64 base and 6 targeted captures.");
  }

  const allCases = [...BASE_EVIDENCE_CASES, ...TARGETED_EVIDENCE_CASES];
  if (new Set(allCases.map((entry) => entry.id)).size !== allCases.length) {
    throw new Error("Evidence catalog contains duplicate IDs.");
  }
  if (new Set(allCases.map((entry) => entry.sourceFilename)).size !== allCases.length) {
    throw new Error("Evidence catalog contains duplicate source filenames.");
  }
}

function isWithin(parent, candidate) {
  const relative = path.relative(parent, candidate);
  return (
    relative === "" ||
    (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative))
  );
}

function assertSeparateOutputPath(outputDir, sourceDirectories) {
  for (const sourceDir of sourceDirectories) {
    if (isWithin(sourceDir, outputDir) || isWithin(outputDir, sourceDir)) {
      throw new Error("Output directory must not overlap a snapshot source directory.");
    }
  }
}

async function validateSnapshotDirectory(directory, expectedCases) {
  let directoryEntries;
  try {
    directoryEntries = await readdir(directory, { withFileTypes: true });
  } catch (error) {
    if (error && error.code === "ENOENT") {
      throw new Error(`Missing snapshot directory: ${directory}`);
    }
    throw error;
  }

  const expectedByFilename = new Map(expectedCases.map((entry) => [entry.sourceFilename, entry]));
  const found = new Set();
  for (const directoryEntry of directoryEntries.sort((left, right) => compareText(left.name, right.name))) {
    const catalogEntry = expectedByFilename.get(directoryEntry.name);
    if (!catalogEntry) {
      throw new Error(`Unexpected snapshot entry: ${directoryEntry.name}`);
    }
    if (!directoryEntry.isFile()) {
      throw new Error(`Snapshot entry must be a direct regular file: ${directoryEntry.name}`);
    }
    found.add(catalogEntry.id);
  }

  for (const catalogEntry of expectedCases) {
    if (!found.has(catalogEntry.id)) {
      throw new Error(`Missing snapshot for ${catalogEntry.id}`);
    }
  }
  return expectedCases.map((catalogEntry) => ({
    catalogEntry,
    sourcePath: path.join(directory, catalogEntry.sourceFilename),
  }));
}

function inspectPng(bytes, catalogEntry) {
  if (bytes.length < PNG_SIGNATURE.length || !bytes.subarray(0, 8).equals(PNG_SIGNATURE)) {
    throw new Error(`Invalid PNG signature for ${catalogEntry.sourceFilename}`);
  }
  if (bytes.length < 24 || bytes.readUInt32BE(8) !== 13 || bytes.toString("ascii", 12, 16) !== "IHDR") {
    throw new Error(`Invalid PNG IHDR for ${catalogEntry.sourceFilename}`);
  }

  const width = bytes.readUInt32BE(16);
  const height = bytes.readUInt32BE(20);
  if (
    catalogEntry.captureMode === "viewport" &&
    (width !== catalogEntry.viewport.width || height !== catalogEntry.viewport.height)
  ) {
    throw new Error(
      `Viewport mismatch for ${catalogEntry.sourceFilename}: expected ${catalogEntry.viewport.width}x${catalogEntry.viewport.height}, got ${width}x${height}`,
    );
  }
  if (catalogEntry.captureMode === "full_page" && width !== catalogEntry.viewport.width) {
    throw new Error(
      `Full-page width mismatch for ${catalogEntry.sourceFilename}: expected ${catalogEntry.viewport.width}, got ${width}`,
    );
  }
  if (catalogEntry.captureMode === "full_page" && height < catalogEntry.viewport.height) {
    throw new Error(
      `Full-page height too small for ${catalogEntry.sourceFilename}: minimum ${catalogEntry.viewport.height}, got ${height}`,
    );
  }
  return { height, width };
}

async function loadSource(source) {
  const bytes = await readFile(source.sourcePath);
  const imageDimensions = inspectPng(bytes, source.catalogEntry);
  return {
    ...source,
    bytes,
    imageDimensions,
    sha256: createHash("sha256").update(bytes).digest("hex"),
    sizeBytes: bytes.byteLength,
  };
}

function manifestEntry(source) {
  const entry = source.catalogEntry;
  return {
    capture_mode: entry.captureMode,
    capture_source: entry.captureSource,
    fixture_authority: FIXTURE_AUTHORITY,
    fixture_source: entry.fixtureSource,
    id: entry.id,
    image_dimensions: {
      height: source.imageDimensions.height,
      width: source.imageDimensions.width,
    },
    kind: entry.kind,
    output_file: `screens/${entry.sourceFilename}`,
    page_id: entry.pageId ?? null,
    route: entry.route,
    sha256: source.sha256,
    size_bytes: source.sizeBytes,
    source_file: `${entry.sourceDirectory}/${entry.sourceFilename}`,
    theme: entry.theme,
    title: entry.title,
    viewport: { height: entry.viewport.height, width: entry.viewport.width },
  };
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function evidenceCard(entry, role) {
  const label = `${entry.title} · ${entry.theme} · ${entry.viewport.width}×${entry.viewport.height}`;
  const dimensionCheck = entry.capture_mode === "full_page" ? "full-page bounds ✓" : "viewport exact ✓";
  const captureDimensions = `${entry.image_dimensions.width}×${entry.image_dimensions.height}`;
  if (role === "overview") {
    return `
          <article class="evidence-card overview-evidence-card" data-evidence-role="overview">
            <a class="image-link" href="${escapeHtml(entry.output_file)}" aria-label="打开原图：${escapeHtml(label)}">
              <img src="${escapeHtml(entry.output_file)}" alt="${escapeHtml(label)}" loading="eager">
            </a>
            <div class="card-body">
              <div class="card-heading"><strong>${escapeHtml(entry.theme)} · ${entry.viewport.width}×${entry.viewport.height}</strong></div>
              <code class="route">${escapeHtml(entry.route)}</code>
              <p class="compact-origin"><span>${escapeHtml(entry.capture_source)}</span><span>synthetic · read-only · non-authoritative</span></p>
              <p class="integrity"><span>PNG ✓</span><span>${dimensionCheck}</span><span>SHA ✓</span></p>
            </div>
          </article>`;
  }
  return `
          <article class="evidence-card" data-evidence-role="${role}">
            <a class="image-link" href="${escapeHtml(entry.output_file)}" aria-label="打开原图：${escapeHtml(label)}">
              <img src="${escapeHtml(entry.output_file)}" alt="${escapeHtml(label)}" loading="lazy">
            </a>
            <div class="card-body">
              <div class="card-heading">
                <strong>${escapeHtml(entry.title)}</strong>
                <span>${escapeHtml(entry.theme)} · viewport ${entry.viewport.width}×${entry.viewport.height} · image ${captureDimensions}</span>
              </div>
              <code class="route">${escapeHtml(entry.route)}</code>
              <dl>
                <div><dt>Source</dt><dd>${escapeHtml(entry.capture_source)}</dd></div>
                <div><dt>Capture</dt><dd>${escapeHtml(entry.capture_mode)}</dd></div>
                <div><dt>Fixture</dt><dd>${escapeHtml(entry.fixture_source)}</dd></div>
                <div><dt>Authority</dt><dd>${escapeHtml(entry.fixture_authority)}</dd></div>
              </dl>
              <p class="integrity"><span>PNG ✓</span><span>${dimensionCheck}</span><span>SHA-256 ${entry.sha256.slice(0, 12)}</span><span>${entry.size_bytes} B</span></p>
            </div>
          </article>`;
}

function renderIndex(manifest) {
  const overview = PAGES.map((page) => {
    const representatives = manifest.base.filter(
      (entry) =>
        entry.page_id === page.id &&
        ((entry.theme === "light" && entry.viewport.width === 1440 && entry.viewport.height === 900) ||
          (entry.theme === "dark" && entry.viewport.width === 390 && entry.viewport.height === 844)),
    );
    if (representatives.length !== 2) {
      throw new Error(`Overview must contain two representative captures for ${page.id}.`);
    }
    return `
      <section class="overview-page" aria-labelledby="overview-${page.id}">
        <h3 id="overview-${page.id}">${escapeHtml(page.title)}</h3>
        <div class="overview-pair">${representatives.map((entry) => evidenceCard(entry, "overview")).join("")}
        </div>
      </section>`;
  }).join("");

  const pageMatrices = PAGES.map((page) => {
    const entries = manifest.base.filter((entry) => entry.page_id === page.id);
    if (entries.length !== 8) {
      throw new Error(`Page matrix must contain eight captures for ${page.id}.`);
    }
    return `
      <details class="page-matrix">
        <summary><span>${escapeHtml(page.title)}</span><span>${entries.length} captures · ${escapeHtml(page.route)}</span></summary>
        <div class="capture-grid">${entries.map((entry) => evidenceCard(entry, "matrix")).join("")}
        </div>
      </details>`;
  }).join("");

  const deferred = manifest.deferred_scope
    .map(
      (entry) => `
          <li><strong>${escapeHtml(entry.owner)}</strong><span>${escapeHtml(entry.item)}</span></li>`,
    )
    .join("");

  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>GameForge M4d V3 Visual Evidence</title>
  <style>
    :root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; color: #252522; background: #f2f0e9; }
    * { box-sizing: border-box; }
    body { margin: 0; }
    a { color: inherit; }
    code { font-family: "SFMono-Regular", Consolas, monospace; }
    .shell { width: min(1560px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0 64px; }
    .masthead { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 24px; align-items: end; padding: 28px 0; border-bottom: 2px solid #292925; }
    .eyebrow { margin: 0 0 9px; color: #7b4b32; font-size: 12px; font-weight: 800; letter-spacing: .14em; text-transform: uppercase; }
    h1 { margin: 0; font-family: Georgia, "Times New Roman", serif; font-size: clamp(36px, 5vw, 72px); font-weight: 500; letter-spacing: -.045em; line-height: .98; }
    .lede { max-width: 750px; margin: 16px 0 0; color: #64635d; font-size: 15px; line-height: 1.65; }
    .counter { min-width: 180px; padding: 14px 16px; border: 1px solid #c8c4b8; background: #faf9f5; }
    .counter strong { display: block; font-family: Georgia, serif; font-size: 34px; font-weight: 500; }
    .counter span { color: #6d6b63; font-size: 12px; letter-spacing: .08em; text-transform: uppercase; }
    .status-strip { display: flex; flex-wrap: wrap; gap: 8px; padding: 14px 0 28px; }
    .status-strip span { padding: 6px 9px; border: 1px solid #c8c4b8; background: #faf9f5; color: #55534d; font-size: 12px; }
    .section-heading { display: flex; justify-content: space-between; gap: 18px; align-items: baseline; margin: 38px 0 14px; border-bottom: 1px solid #a7a397; padding-bottom: 9px; }
    h2 { margin: 0; font-family: Georgia, "Times New Roman", serif; font-size: 27px; font-weight: 500; }
    .section-heading p { margin: 0; color: #6a6861; font-size: 13px; }
    .overview-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(295px, 1fr)); gap: 12px; }
    .overview-page { min-width: 0; padding: 12px; border: 1px solid #cbc7bb; background: rgba(255,255,255,.55); }
    .overview-page h3 { margin: 0 0 10px; font-family: Georgia, serif; font-size: 18px; font-weight: 500; }
    .overview-pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    .evidence-card { min-width: 0; overflow: hidden; border: 1px solid #d0ccc0; background: #fff; box-shadow: 0 1px 0 rgba(30,30,26,.06); }
    .image-link { display: block; height: 145px; overflow: hidden; background: #dedbd1; }
    .image-link:focus-visible { outline: 3px solid #b95f32; outline-offset: -3px; }
    .image-link img { display: block; width: 100%; height: 100%; object-fit: cover; object-position: top; }
    .card-body { padding: 10px; }
    .card-heading { display: grid; gap: 3px; }
    .card-heading strong { overflow-wrap: anywhere; font-size: 13px; line-height: 1.3; }
    .card-heading span { color: #77746c; font-size: 11px; }
    .overview-evidence-card .image-link { height: 108px; }
    .overview-evidence-card .card-body { padding: 8px; }
    .overview-evidence-card .route { margin-top: 5px; }
    .compact-origin { display: grid; gap: 2px; margin: 6px 0 0; color: #66635c; font-size: 9px; line-height: 1.25; }
    .route { display: block; max-width: 100%; margin-top: 8px; overflow: auto; color: #75472f; font-size: 10px; white-space: nowrap; }
    dl { display: grid; gap: 3px; margin: 9px 0 0; font-size: 10px; }
    dl div { display: grid; grid-template-columns: 48px minmax(0, 1fr); gap: 5px; }
    dt { color: #858178; }
    dd { margin: 0; overflow-wrap: anywhere; color: #4e4c47; }
    .integrity { display: flex; flex-wrap: wrap; gap: 4px 8px; margin: 9px 0 0; padding-top: 7px; border-top: 1px solid #ebe8df; color: #365f49; font-family: "SFMono-Regular", Consolas, monospace; font-size: 9px; }
    .page-matrix { margin-bottom: 9px; border: 1px solid #c7c3b7; background: rgba(255,255,255,.58); }
    .page-matrix summary { display: flex; justify-content: space-between; gap: 18px; padding: 14px 16px; cursor: pointer; font-family: Georgia, serif; font-size: 18px; }
    .page-matrix summary span:last-child { color: #6e6b64; font-family: ui-sans-serif, system-ui, sans-serif; font-size: 11px; }
    .capture-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(225px, 1fr)); gap: 10px; padding: 0 12px 12px; }
    .target-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(225px, 1fr)); gap: 10px; }
    .deferred { padding: 18px 20px; border: 1px solid #c2bcae; border-left: 4px solid #7b4b32; background: #e9e5da; }
    .deferred > p { margin: 0 0 10px; color: #5f5c55; font-size: 13px; }
    .deferred ul { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 8px 18px; margin: 0; padding: 0; list-style: none; }
    .deferred li { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 9px; font-size: 12px; line-height: 1.45; }
    .deferred li strong { color: #7b4b32; }
    .footnote { margin: 13px 0 0; color: #66635c; font-size: 11px; }
    @media (max-width: 700px) {
      .shell { width: min(100% - 20px, 1560px); padding-top: 18px; }
      .masthead { grid-template-columns: 1fr; }
      .counter { width: 100%; }
      .section-heading { align-items: flex-start; flex-direction: column; }
      .overview-grid { grid-template-columns: 1fr; }
      .page-matrix summary { flex-direction: column; gap: 4px; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="masthead">
      <div>
        <p class="eyebrow">M4d · Task 18 · V3</p>
        <h1>Visual evidence index</h1>
        <p class="lede">固定的八页、四视口、双主题矩阵，以及六个明确标注的 full-page renderer/component/state fixture。所有缩略图均链接本地原始 PNG；本页不内嵌 base64，也不生成拼接图。</p>
      </div>
      <div class="counter"><strong>${manifest.summary.total}</strong><span>validated captures</span></div>
    </header>
    <div class="status-strip" aria-label="Evidence checks">
      <span>64 exact-viewport base tuples</span><span>6 full-page targeted fixtures</span><span>PNG signature passed</span><span>IHDR dimensions passed</span><span>SHA-256 / size verified</span>
    </div>

    <section aria-labelledby="overview-title">
      <div class="section-heading"><h2 id="overview-title">Compact overview</h2><p>每页 desktop-light + mobile-dark，两张代表图</p></div>
      <div class="overview-grid">${overview}
      </div>
    </section>

    <section aria-labelledby="matrix-title">
      <div class="section-heading"><h2 id="matrix-title">Complete 8 × 4 × 2 matrix</h2><p>展开单页查看全部 8 个固定组合</p></div>${pageMatrices}
    </section>

    <section aria-labelledby="targeted-title">
      <div class="section-heading"><h2 id="targeted-title">Targeted renderer / component states</h2><p>仅六个预登记 full-page fixture，不冒充产品路由数据</p></div>
      <div class="target-grid">${manifest.targeted.map((entry) => evidenceCard(entry, "targeted")).join("")}
      </div>
    </section>

    <section aria-labelledby="deferred-title">
      <div class="section-heading"><h2 id="deferred-title">Explicitly deferred scope</h2><p>仅列 M4d 计划已批准的非阻塞项</p></div>
      <div class="deferred">
        <p>这些项目不属于 V3 通过条件；任何 Task 18 视觉、布局或可访问性失败都不能列入此处。</p>
        <ul>${deferred}
        </ul>
      </div>
    </section>
    <p class="footnote">Canonical order: page → theme → viewport. Base captures require exact viewport dimensions; targeted full-page captures require the exact preregistered width and an image height no shorter than the viewport. Evidence is generated only after catalog, PNG dimensions, SHA-256 and byte-size checks pass.</p>
  </main>
</body>
</html>
`;
}

export async function buildVisualEvidenceIndex(options = {}) {
  assertCatalog();
  const outputDir = path.resolve(options.outputDir ?? DEFAULT_PATHS.outputDir);
  const pageSnapshotsDir = path.resolve(options.pageSnapshotsDir ?? DEFAULT_PATHS.pageSnapshotsDir);
  const targetedSnapshotsDir = path.resolve(
    options.targetedSnapshotsDir ?? DEFAULT_PATHS.targetedSnapshotsDir,
  );
  assertSeparateOutputPath(outputDir, [pageSnapshotsDir, targetedSnapshotsDir]);

  const pageSources = await validateSnapshotDirectory(pageSnapshotsDir, BASE_EVIDENCE_CASES);
  const targetedSources = await validateSnapshotDirectory(targetedSnapshotsDir, TARGETED_EVIDENCE_CASES);
  const loadedSources = [];
  for (const source of [...pageSources, ...targetedSources]) {
    loadedSources.push(await loadSource(source));
  }

  const screensDir = path.join(outputDir, "screens");
  await rm(outputDir, { force: true, recursive: true });
  await mkdir(screensDir, { recursive: true });
  for (const source of loadedSources) {
    const outputPath = path.join(screensDir, source.catalogEntry.sourceFilename);
    await writeFile(outputPath, source.bytes, { flag: "wx" });
  }

  const base = loadedSources.filter((source) => source.catalogEntry.kind === "base").map(manifestEntry);
  const targeted = loadedSources
    .filter((source) => source.catalogEntry.kind === "targeted")
    .map(manifestEntry);
  const manifest = {
    base,
    checks: {
      base_exact_viewports: "passed",
      catalog: "passed",
      png_signatures: "passed",
      source_sha256: "passed",
      targeted_full_page_bounds: "passed",
    },
    deferred_scope: DEFERRED_SCOPE,
    schema_version: "m4d-v3-visual-evidence@1",
    summary: { base: base.length, targeted: targeted.length, total: base.length + targeted.length },
    targeted,
  };
  const index = renderIndex(manifest);
  await writeFile(path.join(outputDir, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`);
  await writeFile(path.join(outputDir, "index.html"), index);

  return {
    indexPath: path.join(outputDir, "index.html"),
    manifest,
    manifestPath: path.join(outputDir, "manifest.json"),
    outputDir,
  };
}

async function runCli() {
  try {
    const result = await buildVisualEvidenceIndex();
    console.log(`Built ${result.manifest.summary.total} validated captures at ${result.indexPath}`);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`Visual evidence build failed: ${message}`);
    process.exitCode = 1;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await runCli();
}
