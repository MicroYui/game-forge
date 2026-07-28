import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import { CursorExpiredError } from "../../api/pagination";
import { createQueryClient } from "../../api/query-client";
import type { Project, ProjectMaterial } from "../projects/api";
import { SpecWorkspacePage, type SpecWorkspaceApi } from "./SpecWorkspacePage";

type Spec = components["schemas"]["SpecViewV1"];
type ConstraintSnapshot = components["schemas"]["ConstraintSnapshotViewV1"];
type ConstraintProposal = components["schemas"]["ConstraintProposalReadViewV1"];
type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];

const baseArtifact: components["schemas"]["ArtifactSummaryV1"] = {
  artifact_id: "artifact:spec:frontier",
  created_at: "2026-07-19T08:00:00Z",
  domain_scope: { domain_ids: ["domain:narrative"] },
  kind: "ir_snapshot",
  lineage_schema_version: "lineage@2",
  parent_artifact_ids: ["artifact:source:frontier"],
  payload_hash: "a".repeat(64),
  payload_schema_id: "ir-core@1",
  summary_schema_version: "artifact-summary@1",
  version_tuple: {
    ir_snapshot_id: "snapshot:frontier",
    tool_version: "ingest@1",
  },
};

const spec: Spec = {
  artifact: baseArtifact,
  ref_name: "refs/specs/frontier",
  ref_value: { artifact_id: baseArtifact.artifact_id, revision: 7 },
  schema_registry_version: "registry@3",
  snapshot_id: "snapshot:frontier",
  view_schema_version: "spec-view@1",
};

const constraintSnapshot: ConstraintSnapshot = {
  artifact: {
    ...baseArtifact,
    artifact_id: "artifact:constraint:candidate",
    kind: "constraint_snapshot",
    payload_hash: "b".repeat(64),
    payload_schema_id: "constraint-snapshot@1",
    version_tuple: {
      constraint_snapshot_id: "constraint:candidate",
      tool_version: "compile@1",
    },
  },
  constraints: [],
  dsl_grammar_version: "dsl@1",
  view_schema_version: "constraint-snapshot-view@1",
};

const constraintProposal: ConstraintProposal = {
  approval_status: "pending_approval",
  artifact: {
    ...baseArtifact,
    artifact_id: "artifact:proposal:economy",
    kind: "constraint_proposal",
    payload_hash: "c".repeat(64),
    payload_schema_id: "constraint-proposal@1",
    version_tuple: { tool_version: "constraint-extraction@4" },
  },
  proposal: {
    base_constraint_snapshot_id: constraintSnapshot.artifact.artifact_id,
    constraints: [],
    domain_scope: { domain_ids: ["domain:economy"] },
    dsl_grammar_version: "dsl@1",
    produced_by: "agent",
    producer_run_id: "run:constraint:proposal",
    proposal_schema_version: "constraint-proposal@1",
    rationale: "Extract deterministic economy limits.",
    revision: 3,
    source_bindings: [],
    supersedes_artifact_id: null,
  },
  view_schema_version: "constraint-proposal-read-view@1",
  workflow_revision: 5,
};

const sourceRaw: ArtifactSummary = {
  ...baseArtifact,
  artifact_id: "artifact:source:raw-frontier",
  kind: "source_raw",
  parent_artifact_ids: [],
  payload_schema_id: "project-material-original@1",
};

const sourceRendered: ArtifactSummary = {
  ...baseArtifact,
  artifact_id: "artifact:source:rendered-frontier",
  created_at: "2026-07-20T09:30:00Z",
  kind: "source_rendered",
  parent_artifact_ids: [sourceRaw.artifact_id],
  payload_schema_id: "source-rendered@1",
};

const project = {
  content_ref_name: "projects/project:frontier/content/head",
  created_at: "2026-07-19T09:00:00Z",
  display_name: "边境开拓",
  domain_scope: { domain_ids: ["builtin"] },
  project_id: "project:frontier",
  project_key: "frontier",
  project_schema_version: "game-project@1" as const,
  status: "active" as const,
} as unknown as Project;

const material = {
  display_name: "第一章·边境开拓",
  material_id: "material:frontier",
  material_schema_version: "project-material@1" as const,
  original_source_artifact_id: sourceRaw.artifact_id,
  project_id: project.project_id,
  rendered_source_artifact_id: sourceRendered.artifact_id,
  status: "active" as const,
} as unknown as ProjectMaterial;

function page<T>(items: T[], nextCursor: string | null, readSnapshotId: string) {
  return {
    expires_at: "2026-07-19T09:00:00Z",
    items,
    next_cursor: nextCursor,
    page_schema_version: "page@1" as const,
    read_snapshot_id: readSnapshotId,
  };
}

function api(overrides: Partial<SpecWorkspaceApi> = {}): SpecWorkspaceApi {
  return {
    draftConstraint: vi.fn(async () => constraintProposal),
    listMaterials: vi.fn(async () => ({
      items: [material],
      next_cursor: null,
      page_schema_version: "project-material-page@1" as const,
    })),
    listProjects: vi.fn(async () => ({
      items: [project],
      next_cursor: null,
      page_schema_version: "project-page@1" as const,
    })),
    listArtifacts: vi.fn(async (kind) =>
      page(kind === "source_raw" ? [sourceRaw] : [sourceRendered], null, `read:${kind}`),
    ),
    listConstraintProposals: vi.fn(async () => page([constraintProposal], null, "read:proposals")),
    listConstraintSnapshots: vi.fn(async () => page([constraintSnapshot], null, "read:constraints")),
    listExecutionProfiles: vi.fn(async () => page([], null, "read:profiles")),
    listRefHistory: vi.fn(),
    listSpecs: vi.fn(async () => page([spec], null, "read:specs")),
    proposeConstraint: vi.fn(async () => ({
      accepted_schema_version: "run-accepted@1" as const,
      events_url: "/api/v1/runs/run/events",
      run_id: "run:constraint:proposal",
      status_url: "/api/v1/runs/run",
    })),
    resolveExecutionOption: vi.fn(async () => {
      throw new Error("not used");
    }),
    uploadSpec: vi.fn(async () => spec),
    ...overrides,
  };
}

function renderPage(workspaceApi: SpecWorkspaceApi, initialEntry = "/specs") {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <SpecWorkspacePage api={workspaceApi} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function deferred<T>() {
  let resolvePromise!: (value: T) => void;
  const promise = new Promise<T>((resolveValue) => {
    resolvePromise = resolveValue;
  });
  return { promise, resolve: resolvePromise };
}

describe("SpecWorkspacePage", () => {
  it("shows only matching project proposals and carries the exact project rule target into review", async () => {
    const matchingProposal = {
      ...constraintProposal,
      proposal: {
        ...constraintProposal.proposal,
        source_bindings: [
          {
            provenance_hash: "d".repeat(64),
            source_artifact_id: sourceRendered.artifact_id,
            source_ref: null,
          },
        ],
      },
    } satisfies ConstraintProposal;
    const query = new URLSearchParams({
      constraint: constraintSnapshot.artifact.artifact_id,
      constraintRef: "projects/project:sky-harbor/constraints/head",
      constraintRevision: "2",
      project: "project:sky-harbor",
      projectName: "天空港",
      source: sourceRendered.artifact_id,
    });
    renderPage(
      api({
        listConstraintProposals: vi.fn(async () =>
          page([constraintProposal, matchingProposal], null, "read:proposals:project"),
        ),
      }),
      `/specs?${query.toString()}`,
    );

    const table = await screen.findByRole("region", { name: "规则提案" });
    expect(within(table).getAllByRole("link", { name: "查看提案" })).toHaveLength(1);
    const href = within(table).getByRole("link", { name: "查看提案" }).getAttribute("href") ?? "";
    expect(href).toContain("project=project%3Asky-harbor");
    expect(href).toContain("constraintRevision=2");
    expect(href).toContain("constraintRef=projects%2Fproject%3Asky-harbor%2Fconstraints%2Fhead");
  });

  it("gives same-time unpublished content distinct planner-facing names", async () => {
    const candidateOne = {
      ...spec,
      artifact: { ...spec.artifact, artifact_id: "artifact:spec:candidate-one" },
      ref_name: null,
      ref_value: null,
    } satisfies Spec;
    const candidateTwo = {
      ...spec,
      artifact: { ...spec.artifact, artifact_id: "artifact:spec:candidate-two" },
      ref_name: null,
      ref_value: null,
    } satisfies Spec;

    renderPage(
      api({
        listSpecs: vi.fn(async () => page([candidateOne, spec, candidateTwo], null, "read:specs")),
      }),
    );

    expect(await screen.findByText("未发布内容 · 第 1 份")).toBeVisible();
    expect(screen.getByText("未发布内容 · 第 2 份")).toBeVisible();
  });

  it("fully reads raw and rendered source catalogs before rendering first-proposal pickers", async () => {
    const secondRaw = {
      ...sourceRaw,
      artifact_id: "artifact:source:raw-harbor",
      created_at: "2026-07-21T10:00:00Z",
    } satisfies ArtifactSummary;
    const secondMaterial = {
      ...material,
      display_name: "第二章·海港",
      material_id: "material:harbor",
      original_source_artifact_id: secondRaw.artifact_id,
      rendered_source_artifact_id: "artifact:source:rendered-harbor",
    } as unknown as ProjectMaterial;
    const listArtifacts = vi.fn<SpecWorkspaceApi["listArtifacts"]>(async (kind, cursor) => {
      if (kind === "source_rendered") return page([sourceRendered], null, "read:source_rendered");
      return cursor === null
        ? page([sourceRaw], "opaque.source-raw+/=", "read:source_raw")
        : page([secondRaw], null, "read:source_raw");
    });
    renderPage(
      api({
        listArtifacts,
        listConstraintProposals: vi.fn(async () => page([], null, "read:proposals:empty")),
        listMaterials: vi.fn(async () => ({
          items: [material, secondMaterial],
          next_cursor: null,
          page_schema_version: "project-material-page@1" as const,
        })),
      }),
    );

    const agent = (await screen.findByRole("heading", { name: "从策划材料提取规则" })).closest("article")!;
    // The second material only appears if the SECOND page of source_raw was read.
    expect(within(agent).getByRole("checkbox", { name: "第一章·边境开拓 · 原文" })).toBeVisible();
    expect(within(agent).getByRole("checkbox", { name: "第二章·海港 · 原文" })).toBeVisible();
    expect(within(agent).getByRole("checkbox", { name: "第一章·边境开拓 · 已解析" })).toBeVisible();
    // The second material's rendered Artifact is not in the catalog, so it is not
    // offered — a planner is never shown a source they cannot actually use.
    expect(within(agent).queryByRole("checkbox", { name: "第二章·海港 · 已解析" })).toBeNull();
    // The third call proves the SECOND page of source_raw was read; the trailing
    // argument is the selected game, which is "all games" here.
    expect(listArtifacts.mock.calls).toEqual([
      ["source_raw", null, null],
      ["source_rendered", null, null],
      ["source_raw", "opaque.source-raw+/=", null],
    ]);
  });

  it("says which game a content version belongs to", async () => {
    // Every project's content head starts at 第 1 版, so revision alone makes two
    // rows render byte-identical titles. The owning game is what a planner reads.
    const projectHead = {
      ...spec,
      artifact: { ...baseArtifact, artifact_id: "artifact:spec:frontier-head" },
      ref_name: project.content_ref_name,
      ref_value: { artifact_id: "artifact:spec:frontier-head", revision: 1 },
    } satisfies Spec;
    renderPage(api({ listSpecs: vi.fn(async () => page([spec, projectHead], null, "read:specs")) }));

    expect(await screen.findByText("边境开拓 · 当前内容 · 第 1 版")).toBeVisible();
    // A ref no project owns keeps the bare title rather than borrowing a name.
    expect(screen.getByText("当前内容 · 第 7 版")).toBeVisible();
  });

  it("never offers agent plumbing as planning material", async () => {
    // `source_raw` is shared by agent prompt context and run goal text. Neither is
    // anything a planner wrote, and neither has a name — offering them under
    // 选择策划材料 told the planner they had uploaded material they never uploaded.
    const promptContext = {
      ...sourceRaw,
      artifact_id: "artifact:source:agent-prompt-context",
      payload_schema_id: "agent-prompt-context@1",
    } satisfies ArtifactSummary;
    renderPage(
      api({
        listArtifacts: vi.fn(async (kind) =>
          page(kind === "source_raw" ? [sourceRaw, promptContext] : [sourceRendered], null, `read:${kind}`),
        ),
        listConstraintProposals: vi.fn(async () => page([], null, "read:proposals:empty")),
      }),
    );

    const agent = (await screen.findByRole("heading", { name: "从策划材料提取规则" })).closest("article")!;
    expect(within(agent).getByRole("checkbox", { name: "第一章·边境开拓 · 原文" })).toBeVisible();
    expect(within(agent).getAllByRole("checkbox")).toHaveLength(2);
  });

  it("fails closed when a source catalog changes read snapshot during pagination", async () => {
    const listArtifacts = vi.fn<SpecWorkspaceApi["listArtifacts"]>(async (kind, cursor) => {
      if (kind === "source_rendered") return page([], null, "read:source_rendered");
      return cursor === null
        ? page([sourceRaw], "opaque.source-raw+/=", "read:source_raw:first")
        : page([], null, "read:source_raw:changed");
    });
    renderPage(api({ listArtifacts }));

    expect(await screen.findByRole("heading", { name: "无法读取规格工作台" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "从策划材料提取规则" })).not.toBeInTheDocument();
  });

  it("moves from loading to a contract-honest ready workspace and preserves opaque cursors", async () => {
    const first = deferred<Awaited<ReturnType<SpecWorkspaceApi["listSpecs"]>>>();
    const nextSpec = {
      ...spec,
      artifact: { ...spec.artifact, artifact_id: "artifact:spec:harbor" },
      ref_name: null,
      ref_value: null,
      snapshot_id: "snapshot:harbor",
    } satisfies Spec;
    const listSpecs = vi
      .fn<SpecWorkspaceApi["listSpecs"]>()
      .mockImplementationOnce(() => first.promise)
      .mockResolvedValueOnce(page([nextSpec], null, "read:specs"));
    const workspaceApi = api({
      listSpecs,
    });
    const user = userEvent.setup();
    renderPage(workspaceApi);

    expect(screen.getByRole("heading", { level: 1, name: "正在读取规格工作台" })).toBeVisible();
    first.resolve(page([spec], "opaque.spec+/=", "read:specs"));

    expect(await screen.findByRole("heading", { level: 1, name: "内容与规则" })).toBeVisible();
    expect(screen.getByText(/只有标记为当前版本的内容/)).toBeVisible();
    expect(screen.getByText("发布状态需查看发布记录")).toBeVisible();
    expect(screen.getByRole("heading", { name: "从策划材料提取规则" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Human typed draft" })).not.toBeVisible();
    expect(screen.getByText(/AI 先生成候选/)).toBeVisible();

    await user.click(screen.getByText("高级：手工导入结构化内容或规则"));
    expect(screen.getByRole("heading", { name: "Human typed draft" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Human spec upload" })).toBeVisible();

    const proposalTable = screen.getByRole("region", { name: "规则提案" });
    expect(within(proposalTable).getByText("规则提案 · 第 3 版")).toBeVisible();
    expect(within(proposalTable).getByText("AI 生成")).toBeVisible();
    expect(within(proposalTable).getByText("待审批")).toBeVisible();
    expect(within(proposalTable).getByText("artifact:proposal:economy")).not.toBeVisible();
    expect(within(proposalTable).getByRole("link", { name: "查看提案" })).toHaveAttribute(
      "href",
      "/constraint-proposals/artifact%3Aproposal%3Aeconomy",
    );

    const specTable = screen.getByRole("region", { name: "内容版本列表" });
    await user.click(within(specTable).getByRole("button", { name: "加载下一页" }));

    expect(await within(specTable).findByText("artifact:spec:harbor")).not.toBeVisible();
    expect(listSpecs).toHaveBeenLastCalledWith("opaque.spec+/=");
    expect(screen.getByText("未发布，不会被当作当前版本使用")).toBeVisible();
  });

  it("renders an explicit empty workspace instead of inventing a current spec", async () => {
    renderPage(
      api({
        listConstraintSnapshots: vi.fn(async () => page([], null, "read:constraints:empty")),
        listConstraintProposals: vi.fn(async () => page([], null, "read:proposals:empty")),
        listSpecs: vi.fn(async () => page([], null, "read:specs:empty")),
      }),
    );

    expect(
      await screen.findByRole("heading", {
        name: "尚无可读取的规格、约束快照或提案",
      }),
    ).toBeVisible();
    expect(screen.queryByText("当前规格")).not.toBeInTheDocument();
  });

  it("keeps proposal cursor expiry explicit and restarts only after the operator chooses it", async () => {
    const restartedProposal = {
      ...constraintProposal,
      artifact: {
        ...constraintProposal.artifact,
        artifact_id: "artifact:proposal:restarted",
      },
    } satisfies ConstraintProposal;
    const listConstraintProposals = vi
      .fn<SpecWorkspaceApi["listConstraintProposals"]>()
      .mockResolvedValueOnce(page([constraintProposal], "opaque.proposal+/=", "read:proposals"))
      .mockRejectedValueOnce(
        new CursorExpiredError(
          {
            code: "cursor_expired",
            conflict_set_id: null,
            detail: "Cursor expired.",
            earliest_cursor: null,
            instance: "/api/v1/constraint-proposals",
            request_id: "request:cursor:1",
            retry_after_s: null,
            run_id: null,
            status: 410,
            title: "Cursor expired",
            trace_id: null,
            type: "about:blank",
          },
          "opaque.proposal+/=",
        ),
      )
      .mockResolvedValueOnce(page([restartedProposal], null, "read:proposals:restarted"));
    const user = userEvent.setup();
    renderPage(api({ listConstraintProposals }));

    const proposalTable = await screen.findByRole("region", {
      name: "规则提案",
    });
    await user.click(within(proposalTable).getByRole("button", { name: "加载下一页" }));
    expect(await within(proposalTable).findByText(/分页游标已过期/)).toBeVisible();
    expect(listConstraintProposals).toHaveBeenCalledTimes(2);

    await user.click(within(proposalTable).getByRole("button", { name: "重新开始查询" }));
    expect(await within(proposalTable).findByText("artifact:proposal:restarted")).not.toBeVisible();
    expect(listConstraintProposals).toHaveBeenLastCalledWith(null);
  });

  it("offers an ordinary retry without exposing raw error details", async () => {
    const listSpecs = vi
      .fn<SpecWorkspaceApi["listSpecs"]>()
      .mockRejectedValueOnce(new Error("database password=must-not-render"))
      .mockResolvedValueOnce(page([spec], null, "read:specs"));
    const user = userEvent.setup();
    renderPage(api({ listSpecs }));

    expect(await screen.findByRole("heading", { name: "无法读取规格工作台" })).toBeVisible();
    expect(screen.queryByText(/database password/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试" }));

    expect(await screen.findByRole("heading", { name: "内容与规则" })).toBeVisible();
    expect(listSpecs).toHaveBeenCalledTimes(2);
  });

  it("keeps the Editorial workspace single-column at the mobile breakpoint", () => {
    const css = readFileSync(resolve(process.cwd(), "src/features/specs/specs.css"), "utf8");

    expect(css).toMatch(/@media\s*\(max-width:\s*760px\)/);
    expect(css).toMatch(/\.gf-specs__hero[\s\S]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
    expect(css).toMatch(/\.gf-specs__workspace-grid[\s\S]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
    expect(css).toMatch(/\.gf-specs__entry-grid[\s\S]*grid-template-columns:\s*minmax\(0,\s*1fr\)/);
  });
});
