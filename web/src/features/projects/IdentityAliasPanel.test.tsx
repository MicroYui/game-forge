import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../api/query-client";
import type { GraphPage, Project, ProjectIdentityAlias, ProjectsApi } from "./api";
import { IdentityAliasPanel } from "./IdentityAliasPanel";

const project = {
  display_name: "原神牛逼！",
  project_id: "project:genshin",
  revision: 4,
} as unknown as Project;

const graph = {
  items: [
    {
      entity: {
        attrs: { name: "钟离" },
        id: "npc:zhongli",
        schema_version: "ir-entity@1",
        tags: [],
        type: "NPC",
      },
      item_id: "npc:zhongli",
      item_kind: "entity" as const,
      item_schema_version: "graph-item@1" as const,
    },
  ],
} as unknown as GraphPage;

function api(overrides: Partial<ProjectsApi> = {}): ProjectsApi {
  return {
    declareIdentityAlias: vi.fn(),
    listIdentityAliases: vi.fn().mockResolvedValue([]),
    retractIdentityAlias: vi.fn(),
    ...overrides,
  } as unknown as ProjectsApi;
}

function renderPanel(projectsApi: ProjectsApi, page: GraphPage | undefined = graph) {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <IdentityAliasPanel api={projectsApi} graph={page} project={project} projectEtag='"p:4"' />
    </QueryClientProvider>,
  );
}

describe("IdentityAliasPanel", () => {
  it("names the entity the way the planner reads it, not by its id", async () => {
    renderPanel(api());

    expect(await screen.findByRole("option", { name: "钟离" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "npc:zhongli" })).not.toBeInTheDocument();
  });

  it("records a name no lexical rule could ever reach", async () => {
    const user = userEvent.setup();
    const declareIdentityAlias = vi.fn().mockResolvedValue({} as ProjectIdentityAlias);
    renderPanel(api({ declareIdentityAlias }));

    await user.type(await screen.findByLabelText("还有一个叫法"), "岩王帝君");
    await user.selectOptions(screen.getByLabelText("指的是"), "npc:zhongli");
    await user.click(screen.getByRole("button", { name: "记住这个叫法" }));

    expect(declareIdentityAlias).toHaveBeenCalledWith(
      project.project_id,
      expect.objectContaining({
        alias: "岩王帝君",
        canonical_entity_id: "npc:zhongli",
        // The project's own revision guards the write, like every other edit.
        expected_project_revision: 4,
      }),
      expect.anything(),
      '"p:4"',
    );
  });

  it("says what has been recorded, in both names", async () => {
    const declared: ProjectIdentityAlias = {
      alias: "岩王帝君",
      alias_id: "identity-alias:1",
      alias_schema_version: "project-identity-alias@1",
      canonical_alias: "岩王帝君",
      canonical_entity_id: "npc:zhongli",
      declared_at: "2026-07-27T02:00:00Z",
      declared_by: "human:admin",
      project_id: project.project_id,
      revision: 1,
      status: "active",
    };
    renderPanel(api({ listIdentityAliases: vi.fn().mockResolvedValue([declared]) }));

    const entry = await screen.findByRole("listitem");
    expect(entry).toHaveTextContent("岩王帝君");
    expect(entry).toHaveTextContent("钟离");
  });

  it("cannot point a name at a game with no content yet", async () => {
    renderPanel(api(), { items: [] } as unknown as GraphPage);

    expect(await screen.findByText(/这个游戏还没有内容，先发布首个内容版本/u)).toBeVisible();
    expect(screen.queryByLabelText("指的是")).not.toBeInTheDocument();
  });
});
