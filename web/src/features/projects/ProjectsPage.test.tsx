import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../api/query-client";
import type { Project, ProjectsApi } from "./api";
import { ProjectsPage } from "./ProjectsPage";

const project: Project = {
  bootstrap_snapshot_artifact_id: "artifact:bootstrap:sky-harbor",
  constraint_ref_name: "projects/project:sky-harbor/constraints/head",
  content_ref_name: "projects/project:sky-harbor/content/head",
  created_at: "2026-07-24T00:00:00Z",
  created_by: "principal:admin",
  current_constraint_ref: null,
  current_content_ref: null,
  description: "在云海中经营一座会移动的港口。",
  display_name: "天空港计划",
  domain_scope: { domain_ids: ["builtin"] },
  genre: "叙事经营",
  latest_approval_id: null,
  latest_extraction_id: null,
  latest_patch_artifact_id: null,
  project_id: "project:sky-harbor",
  project_key: "sky-harbor",
  project_schema_version: "game-project@1",
  revision: 1,
  status: "draft",
  updated_at: "2026-07-24T00:00:00Z",
};

function LocationProbe() {
  return <output data-testid="location">{useLocation().pathname}</output>;
}

function renderPage(api: Pick<ProjectsApi, "createProject" | "listProjects">) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={["/projects"]}>
        <Routes>
          <Route element={<ProjectsPage api={api} />} path="/projects" />
          <Route element={<LocationProbe />} path="/projects/:projectId" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ProjectsPage", () => {
  it("gives a novice an empty-state explanation and creates the first project", async () => {
    const user = userEvent.setup();
    const createProject = vi.fn().mockResolvedValue({ etag: '\"project:1\"', value: project });
    const api = {
      createProject,
      listProjects: vi.fn().mockResolvedValue({
        items: [],
        next_cursor: null,
        page_schema_version: "project-page@1",
      }),
    };
    renderPage(api);

    expect(await screen.findByRole("heading", { name: "还没有游戏项目" })).toBeVisible();
    await user.type(screen.getByLabelText("游戏名称"), "天空港计划");
    await user.clear(screen.getByLabelText("项目代号"));
    await user.type(screen.getByLabelText("项目代号"), "sky-harbor");
    await user.type(screen.getByLabelText("游戏类型"), "叙事经营");
    await user.type(screen.getByLabelText("一句话创意"), "在云海中经营一座会移动的港口。");
    await user.click(screen.getByRole("button", { name: "创建并添加材料" }));

    expect(createProject).toHaveBeenCalledWith(
      {
        description: "在云海中经营一座会移动的港口。",
        display_name: "天空港计划",
        domain_scope: { domain_ids: ["builtin"] },
        genre: "叙事经营",
        project_key: "sky-harbor",
        request_schema_version: "project-create-request@1",
      },
      { idempotencyKey: expect.any(String) },
    );
    expect(await screen.findByTestId("location")).toHaveTextContent("/projects/project%3Asky-harbor");
  });

  it("shows business names and keeps system identifiers inside technical details", async () => {
    const api = {
      createProject: vi.fn(),
      listProjects: vi.fn().mockResolvedValue({
        items: [project],
        next_cursor: null,
        page_schema_version: "project-page@1",
      }),
    };
    renderPage(api);

    expect(await screen.findByRole("heading", { name: "天空港计划" })).toBeVisible();
    expect(screen.getByText("尚未发布内容")).toBeVisible();
    expect(screen.getByText("project:sky-harbor")).not.toBeVisible();
    await userEvent.click(screen.getByText("查看技术信息"));
    expect(screen.getByText("project:sky-harbor")).toBeVisible();
  });
});
