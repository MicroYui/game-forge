import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../api/query-client";
import type { Project, ProjectsApi } from "./api";
import { ProjectOverviewPage } from "./ProjectOverviewPage";

const cytoscapeMock = vi.hoisted(() => {
  const neighborhood = { addClass: vi.fn() };
  const idCollection = {
    addClass: vi.fn(),
    closedNeighborhood: vi.fn(() => neighborhood),
    empty: () => false,
    select: vi.fn(),
    unselect: vi.fn(),
  };
  const elements = {
    difference: vi.fn(() => ({ addClass: vi.fn() })),
    removeClass: vi.fn(),
    unselect: vi.fn(),
  };
  return {
    factory: vi.fn(() => ({
      $id: vi.fn(() => idCollection),
      destroy: vi.fn(),
      elements: vi.fn(() => elements),
      fit: vi.fn(),
      off: vi.fn(),
      on: vi.fn(),
    })),
  };
});

vi.mock("cytoscape", () => ({ default: cytoscapeMock.factory }));

const project: Project = {
  bootstrap_snapshot_artifact_id: "artifact:bootstrap:1",
  constraint_ref_name: "constraints/sky-harbor",
  content_ref_name: "content/sky-harbor",
  created_at: "2026-07-26T02:00:00Z",
  created_by: "human:admin",
  current_constraint_ref: { artifact_id: "artifact:constraint:1", revision: 2 },
  current_content_ref: { artifact_id: "artifact:content:3", revision: 3 },
  description: "玩家经营一座漂浮在云海中的港口。",
  display_name: "天空港计划",
  domain_scope: { domain_ids: ["game-content"] },
  genre: "叙事经营",
  latest_approval_id: null,
  latest_extraction_id: null,
  latest_patch_artifact_id: null,
  project_id: "project:sky-harbor",
  project_key: "sky-harbor",
  project_schema_version: "game-project@1",
  revision: 6,
  status: "active",
  updated_at: "2026-07-26T06:20:00Z",
};

function graphPage() {
  return {
    expires_at: "2026-07-26T08:00:00Z",
    items: [
      {
        entity: {
          attrs: { name: "云港向导" },
          id: "npc:guide",
          schema_version: "ir-entity@1",
          tags: [],
          type: "NPC",
        },
        item_id: "npc:guide",
        item_kind: "entity" as const,
        item_schema_version: "graph-item@1" as const,
      },
      {
        item_id: "rel:1",
        item_kind: "relation" as const,
        item_schema_version: "graph-item@1" as const,
        relation: {
          attrs: {},
          dst: "loc:harbor",
          id: "rel:1",
          schema_version: "ir-relation@1",
          src: "npc:guide",
          type: "LOCATED_IN",
        },
      },
    ],
    next_cursor: null,
    page_schema_version: "page@1" as const,
    read_snapshot_id: "read:graph:1",
  };
}

function api(overrides: Partial<ProjectsApi> = {}): ProjectsApi {
  return {
    addTextMaterial: vi.fn(),
    createContentDraft: vi.fn(),
    createProject: vi.fn(),
    discardExtraction: vi.fn(),
    getArtifact: vi.fn(),
    getExtraction: vi.fn(),
    getMaterial: vi.fn(),
    getProject: vi.fn().mockResolvedValue({ etag: '"project:6"', value: project }),
    listContentGraph: vi.fn().mockResolvedValue(graphPage()),
    listExtractions: vi.fn().mockResolvedValue({
      items: [],
      next_cursor: null,
      page_schema_version: "project-extraction-page@1" as const,
    }),
    listMaterials: vi.fn().mockResolvedValue({
      items: [],
      next_cursor: null,
      page_schema_version: "project-material-page@1" as const,
    }),
    listProjects: vi.fn(),
    renameMaterial: vi.fn(),
    startExtraction: vi.fn(),
    uploadMaterial: vi.fn(),
    ...overrides,
  } as ProjectsApi;
}

function renderPage(projectsApi: ProjectsApi) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <ProjectOverviewPage api={projectsApi} projectId={project.project_id} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ProjectOverviewPage", () => {
  it("opens on what the game is now, not on a form", async () => {
    renderPage(api());

    expect(await screen.findByRole("heading", { level: 1, name: "天空港计划" })).toBeVisible();
    // The game's own shape leads the page.
    const graph = screen.getByRole("region", { name: "游戏内容图谱" });
    expect(await within(graph).findByText(/1 个内容 · 1 条关系/u)).toBeVisible();
    expect(screen.getByText("第 3 版内容")).toBeVisible();
    // No authoring form on the overview.
    expect(screen.queryByLabelText("材料名称")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "AI 提取实体与关系" })).not.toBeInTheDocument();
  });

  it("sends a project without content to authoring instead of showing an empty graph", async () => {
    const draft: Project = {
      ...project,
      current_constraint_ref: null,
      current_content_ref: null,
      status: "draft",
    };
    renderPage(
      api({
        getProject: vi.fn().mockResolvedValue({ etag: '"project:1"', value: draft }),
        listContentGraph: vi.fn().mockResolvedValue({ ...graphPage(), items: [] }),
      }),
    );

    expect(await screen.findByText("这个游戏还没有内容")).toBeVisible();
    expect(screen.getByRole("link", { name: "从策划材料开始" })).toHaveAttribute(
      "href",
      `/projects/${encodeURIComponent(project.project_id)}/authoring`,
    );
  });

  it("offers the next actions as entries, with rules and playtest bound to current refs", async () => {
    renderPage(api());

    const actions = await screen.findByRole("region", { name: "继续创作" });
    expect(within(actions).getByRole("link", { name: "从策划材料生成内容" })).toHaveAttribute(
      "href",
      `/projects/${encodeURIComponent(project.project_id)}/authoring`,
    );
    const rules = within(actions).getByRole("link", { name: "生成与维护规则" });
    expect(rules.getAttribute("href")).toContain("/specs?");
    expect(rules.getAttribute("href")).toContain("project=");
    // Entries carry the same exact bindings the workspace hands out.
    const playtest = within(actions).getByRole("link", { name: "进入自动试玩" });
    expect(playtest.getAttribute("href")).toContain("projectConstraint=");
    const generation = within(actions).getByRole("link", { name: "继续生成内容" });
    expect(generation.getAttribute("href")).toContain("constraint=");
  });
});
