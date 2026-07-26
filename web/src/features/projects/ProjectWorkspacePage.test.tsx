import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../api/query-client";
import type {
  ArtifactPayloadView,
  Project,
  ProjectDraftResult,
  ProjectExtraction,
  ProjectMaterial,
  ProjectsApi,
} from "./api";
import { ProjectWorkspacePage } from "./ProjectWorkspacePage";

const cytoscapeMock = vi.hoisted(() => {
  const collection = {
    addClass: vi.fn(),
    empty: () => false,
    removeClass: vi.fn(),
    select: vi.fn(),
    unselect: vi.fn(),
  };
  const cy = {
    $id: vi.fn(() => collection),
    destroy: vi.fn(),
    elements: vi.fn(() => collection),
    fit: vi.fn(),
    off: vi.fn(),
    on: vi.fn(),
  };
  return { factory: vi.fn(() => cy) };
});

vi.mock("cytoscape", () => ({ default: cytoscapeMock.factory }));

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

const material: ProjectMaterial = {
  byte_size: 89,
  created_at: "2026-07-24T00:01:00Z",
  created_by: "principal:admin",
  display_name: "核心创意",
  material_id: "material:idea",
  material_schema_version: "project-material@1",
  media_type: "text/markdown",
  original_source_artifact_id: "artifact:source:raw",
  parse_status: "ready",
  parse_warnings: [],
  parser_id: "project-markdown",
  parser_version: "1",
  project_id: project.project_id,
  rendered_source_artifact_id: "artifact:source:rendered",
  revision: 1,
  source_format: "markdown",
  status: "active",
  text_char_count: 67,
};

const queuedExtraction: ProjectExtraction = {
  alias_groups: [],
  approval_id: null,
  base_snapshot_artifact_id: project.bootstrap_snapshot_artifact_id,
  created_at: "2026-07-24T00:02:00Z",
  created_by: "principal:admin",
  extraction_id: "extraction:sky-harbor",
  extraction_schema_version: "project-extraction@1",
  identity_conflicts: [],
  material_ids: [material.material_id],
  normalization_summary: null,
  patch_artifact_id: null,
  planning_scope: "auto",
  preview_snapshot_artifact_id: null,
  project_id: project.project_id,
  revision: 1,
  run_id: "run:extract:sky-harbor",
  source_artifact_ids: [material.rendered_source_artifact_id],
  status: "queued",
  updated_at: "2026-07-24T00:02:00Z",
  validation_issues: [],
};

const readyExtraction: ProjectExtraction = {
  ...queuedExtraction,
  alias_groups: [
    {
      alias_schema_version: "identity-alias-group@1",
      aliases: ["Air.Quality", "air_quality"],
      canonical_identity: "air_quality",
    },
  ],
  normalization_summary: {
    alias_group_count: 1,
    auto_merge_count: 1,
    blocking_conflict_count: 0,
    input_operation_count: 4,
    output_operation_count: 3,
    policy_version: "identity-normalization@1",
    summary_schema_version: "identity-normalization-summary@1",
  },
  patch_artifact_id: "artifact:ai-patch",
  preview_snapshot_artifact_id: "artifact:preview",
  status: "ready",
};

const failedExtraction: ProjectExtraction = {
  ...queuedExtraction,
  failure_cause_code: "generation_output_truncated",
  failure_message: "AI 输出达到长度上限，系统已安全停止。",
  failure_retryable: true,
  status: "failed",
  updated_at: "2026-07-24T00:04:00Z",
};

const validationExtraction: ProjectExtraction = {
  ...readyExtraction,
  failure_cause_code: "generation_validation_needs_resolution",
  failure_message: "已生成可编辑草案，但确定性检查发现需要策划确认的问题。",
  failure_retryable: false,
  status: "needs_resolution",
  validation_issues: [
    {
      affected_content: ["未寄之梦"],
      code: "dead_quest",
      description: "“未寄之梦”缺少明确的任务发起方。",
      issue_id: "finding:dead-quest",
      issue_schema_version: "project-extraction-issue@1",
      resolution_hint: "补充发起角色，或把它改成非任务玩法。",
      severity: "critical",
      source: "structure",
      title: "任务缺少起点或步骤",
    },
    {
      affected_content: ["梦迹书签"],
      code: "drop_source_existence_and_yield_rate",
      description: "“梦迹书签”还没有可验证的产出来源。",
      issue_id: "finding:economy",
      issue_schema_version: "project-extraction-issue@1",
      resolution_hint: "补充产出来源和数值，或先把奖励金额保留为普通属性。",
      severity: "major",
      source: "economy",
      title: "货币产出链不完整",
    },
  ],
};

const lifecycleExtraction: ProjectExtraction = {
  ...validationExtraction,
  planning_scope: "limited_event",
  validation_issues: [
    {
      affected_content: ["梦中未寄出的信"],
      code: "unbound_event_schedule",
      description: "“梦中未寄出的信”只写了持续时长，还没有可执行的开始和结束时间。",
      issue_id: "finding:unbound-event-schedule",
      issue_schema_version: "project-extraction-issue@1",
      resolution_hint: "填写活动开始时间、玩法结束时间、奖励兑换截止时间和时区后再发布。",
      severity: "major",
      source: "structure",
      title: "限时活动还没有确定档期",
    },
  ],
};

const preview = {
  artifact: {
    artifact_id: "artifact:preview",
    created_at: "2026-07-24T00:03:00Z",
    domain_scope: { domain_ids: ["builtin"] },
    kind: "ir_snapshot",
    lineage_schema_version: "lineage@2",
    parent_artifact_ids: [project.bootstrap_snapshot_artifact_id, material.rendered_source_artifact_id],
    payload_hash: "0".repeat(64),
    payload_schema_id: "ir-core@1",
    summary_schema_version: "artifact-summary@1",
    version_tuple: {},
  },
  payload: {
    entities: {
      "npc:weather-keeper": {
        attrs: { air_quality: "clean", display_name: "天气管理员" },
        schema_version: "ir-core@1",
        tags: [],
        type: "NPC",
      },
      "region:sky-harbor": {
        attrs: { display_name: "天空港" },
        schema_version: "ir-core@1",
        tags: [],
        type: "REGION",
      },
    },
    relations: {
      "rel:keeper-located-in-harbor": {
        attrs: {},
        dst_id: "region:sky-harbor",
        schema_version: "ir-core@1",
        src_id: "npc:weather-keeper",
        type: "LOCATED_IN",
      },
    },
  },
  resource_revision: 1,
  view_schema_version: "artifact-payload-view@1",
} as ArtifactPayloadView;

const lifecyclePreview = {
  ...preview,
  payload: {
    entities: {
      "character:guide": {
        attrs: { display_name: "活动向导" },
        schema_version: "ir-core@1",
        tags: [],
        type: "CHARACTER",
      },
      "event:dream-letters": {
        attrs: {
          availability: {
            availability_schema_version: "event-availability@1",
            duration_days: 14,
            expiration_policy: "hide_from_active_content",
            reward_claim_grace_days: 3,
            schedule_kind: "relative",
            timezone: null,
          },
          display_name: "梦中未寄出的信",
          scope_kind: "event",
          scope_role: "owner",
        },
        schema_version: "ir-core@1",
        tags: [],
        type: "EVENT",
      },
    },
    relations: {},
  },
} as ArtifactPayloadView;

function patchResult(): ProjectDraftResult {
  return {
    patch: {
      approval_status: "draft",
      artifact: {
        artifact_id: "artifact:human-patch",
      },
      regression_status: "not_started",
      validation_status: "not_started",
      view_schema_version: "patch-artifact-read-view@1",
      workflow_revision: 1,
    } as ProjectDraftResult["patch"],
    projectRevision: 4,
  };
}

function extractionPage(items: ProjectExtraction[] = []) {
  return {
    items,
    next_cursor: null,
    page_schema_version: "project-extraction-page@1" as const,
  };
}

function renderPage(api: ProjectsApi) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <ProjectWorkspacePage api={api} pollIntervalMs={5} projectId={project.project_id} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ProjectWorkspacePage", () => {
  it("builds exact project-scoped rule, generation, and playtest links from current refs", async () => {
    const activeProject: Project = {
      ...project,
      current_constraint_ref: { artifact_id: "artifact:constraint:sky-harbor", revision: 2 },
      current_content_ref: { artifact_id: "artifact:content:sky-harbor", revision: 3 },
      status: "active",
    };
    const api = {
      addTextMaterial: vi.fn(),
      createContentDraft: vi.fn(),
      createProject: vi.fn(),
      discardExtraction: vi.fn(),
      renameMaterial: vi.fn(),
      getArtifact: vi.fn().mockResolvedValue(preview),
      getExtraction: vi.fn(),
      getMaterial: vi.fn(),
      getProject: vi.fn().mockResolvedValue({ etag: '"project:3"', value: activeProject }),
      listExtractions: vi.fn().mockResolvedValue(extractionPage()),
      listMaterials: vi.fn().mockResolvedValue({
        items: [material],
        next_cursor: null,
        page_schema_version: "project-material-page@1",
      }),
      listProjects: vi.fn(),
      startExtraction: vi.fn(),
      uploadMaterial: vi.fn(),
    } satisfies ProjectsApi;
    renderPage(api);

    await screen.findByText("首个内容版本已发布");
    const rules = new URL(
      screen.getByRole("link", { name: /生成与维护规则/ }).getAttribute("href")!,
      "https://gameforge.local",
    );
    expect(rules.pathname).toBe("/specs");
    expect(rules.searchParams.get("constraintRef")).toBe(activeProject.constraint_ref_name);
    expect(rules.searchParams.get("constraintRevision")).toBe("2");
    expect(rules.searchParams.getAll("source")).toEqual([material.rendered_source_artifact_id]);

    const generation = new URL(
      screen.getByRole("link", { name: /继续生成内容/ }).getAttribute("href")!,
      "https://gameforge.local",
    );
    expect(generation.searchParams.get("content")).toBe("artifact:content:sky-harbor");
    expect(generation.searchParams.get("contentRevision")).toBe("3");
    expect(generation.searchParams.get("constraint")).toBe("artifact:constraint:sky-harbor");

    const playtest = new URL(
      screen.getByRole("link", { name: /进入自动试玩/ }).getAttribute("href")!,
      "https://gameforge.local",
    );
    expect(playtest.searchParams.get("projectContent")).toBe("artifact:content:sky-harbor");
    expect(playtest.searchParams.get("projectConstraint")).toBe("artifact:constraint:sky-harbor");
    expect(playtest.searchParams.has("constraint")).toBe(false);
  });

  it("lets the planner enter a final conflict value and only then unlocks publication", async () => {
    const user = userEvent.setup();
    const conflictExtraction: ProjectExtraction = {
      ...readyExtraction,
      identity_conflicts: [
        {
          candidates: [
            { op_id: "op:clean", source_identity: "Air.Quality", value: "clean" },
            { op_id: "op:polluted", source_identity: "air_quality", value: "polluted" },
          ],
          canonical_identity: "npc:weather-keeper.air_quality",
          code: "attribute_value_conflict",
          conflict_id: "identity-conflict:air-quality",
          conflict_schema_version: "identity-conflict@1",
        },
      ],
      normalization_summary: {
        ...readyExtraction.normalization_summary!,
        blocking_conflict_count: 1,
      },
      status: "needs_resolution",
    };
    const projectWithExtraction = {
      ...project,
      latest_extraction_id: conflictExtraction.extraction_id,
    };
    const api = {
      addTextMaterial: vi.fn(),
      createContentDraft: vi.fn(),
      createProject: vi.fn(),
      discardExtraction: vi.fn(),
      renameMaterial: vi.fn(),
      getArtifact: vi.fn().mockResolvedValue(preview),
      getExtraction: vi.fn().mockResolvedValue({ etag: '"extraction:2"', value: conflictExtraction }),
      getMaterial: vi.fn(),
      getProject: vi.fn().mockResolvedValue({ etag: '"project:2"', value: projectWithExtraction }),
      listExtractions: vi.fn().mockResolvedValue(extractionPage([conflictExtraction])),
      listMaterials: vi.fn().mockResolvedValue({
        items: [material],
        next_cursor: null,
        page_schema_version: "project-material-page@1",
      }),
      listProjects: vi.fn(),
      startExtraction: vi.fn(),
      uploadMaterial: vi.fn(),
    } satisfies ProjectsApi;
    renderPage(api);

    const publish = await screen.findByRole("button", { name: "创建发布草案" });
    expect(publish).toBeDisabled();
    await user.type(screen.getByLabelText("手工填写 npc:weather-keeper.air_quality 的最终值"), "variable");
    await user.click(screen.getByRole("button", { name: "使用手工值" }));

    expect(await screen.findByText("已确认")).toBeVisible();
    expect(publish).toBeEnabled();
    expect((screen.getByLabelText("属性 JSON") as HTMLTextAreaElement).value).toContain("variable");
  });

  it("walks material → real extraction → editable graph → governed content draft", async () => {
    const user = userEvent.setup();
    const getProject = vi.fn().mockResolvedValue({ etag: '\"project:1\"', value: project });
    const addTextMaterial = vi.fn().mockResolvedValue(material);
    const startExtraction = vi.fn().mockResolvedValue(queuedExtraction);
    const getExtraction = vi.fn().mockResolvedValue({ etag: '\"extraction:2\"', value: readyExtraction });
    const createContentDraft = vi.fn().mockResolvedValue(patchResult());
    const api = {
      addTextMaterial,
      createContentDraft,
      createProject: vi.fn(),
      discardExtraction: vi.fn(),
      renameMaterial: vi.fn(),
      getArtifact: vi.fn().mockResolvedValue(preview),
      getExtraction,
      getProject,
      listExtractions: vi.fn().mockResolvedValue(extractionPage()),
      listMaterials: vi.fn().mockResolvedValue({
        items: [],
        next_cursor: null,
        page_schema_version: "project-material-page@1",
      }),
      getMaterial: vi.fn(),
      listProjects: vi.fn(),
      startExtraction,
      uploadMaterial: vi.fn(),
    } satisfies ProjectsApi;
    renderPage(api);

    expect(await screen.findByRole("heading", { name: "天空港计划" })).toBeVisible();
    await user.type(screen.getByLabelText("材料名称"), "核心创意");
    await user.type(
      screen.getByLabelText("策划内容"),
      "天空港有一位天气管理员，Air.Quality 与 air_quality 指的是同一个属性。",
    );
    await user.click(screen.getByRole("button", { name: "保存这份材料" }));
    expect(addTextMaterial).toHaveBeenCalledWith(
      project.project_id,
      expect.objectContaining({ display_name: "核心创意", source_format: "plain_text" }),
      { idempotencyKey: expect.any(String) },
    );

    await user.selectOptions(screen.getByRole("combobox", { name: /这份材料属于什么/u }), "limited_event");
    expect(screen.getByText(/开放期、玩法结束期、奖励兑换期/u)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "AI 提取实体与关系" }));
    expect(startExtraction).toHaveBeenCalledWith(
      project.project_id,
      expect.objectContaining({
        llm_execution_mode: "record",
        material_ids: [material.material_id],
        planning_scope: "limited_event",
        request_schema_version: "project-extraction-create-request@1",
      }),
      { idempotencyKey: expect.any(String) },
    );

    expect(await screen.findByText("自动识别并合并了 1 组同一内容")).toBeVisible();
    expect(screen.getByText(/Air\.Quality/)).toBeVisible();
    expect(await screen.findByRole("region", { name: "实体与关系编辑器" })).toBeVisible();
    expect(screen.getByDisplayValue("天气管理员")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "创建发布草案" }));
    await waitFor(() => expect(createContentDraft).toHaveBeenCalled());
    expect(createContentDraft).toHaveBeenCalledWith(
      project.project_id,
      expect.objectContaining({
        entities: expect.arrayContaining([expect.objectContaining({ id: "npc:weather-keeper" })]),
        expected_source_extraction_revision: readyExtraction.revision,
        expected_project_revision: 1,
        relations: expect.arrayContaining([expect.objectContaining({ id: "rel:keeper-located-in-harbor" })]),
        request_schema_version: "project-graph-draft-request@1",
        source_extraction_id: readyExtraction.extraction_id,
      }),
      { idempotencyKey: expect.any(String) },
      '\"project:1\"',
    );
    expect(await screen.findByRole("link", { name: "验证并发布这个版本" })).toHaveAttribute(
      "href",
      "/patches/artifact%3Ahuman-patch",
    );
  });

  it("never opens another proposal's project-level publication draft", async () => {
    const projectWithMismatchedDraft: Project = {
      ...project,
      latest_extraction_id: readyExtraction.extraction_id,
      latest_patch_artifact_id: "artifact:old-limited-event-patch",
    };
    const api = {
      addTextMaterial: vi.fn(),
      createContentDraft: vi.fn(),
      createProject: vi.fn(),
      discardExtraction: vi.fn(),
      renameMaterial: vi.fn(),
      getArtifact: vi.fn().mockResolvedValue(preview),
      getExtraction: vi.fn().mockResolvedValue({ etag: '"extraction:1"', value: readyExtraction }),
      getMaterial: vi.fn(),
      getProject: vi.fn().mockResolvedValue({ etag: '"project:2"', value: projectWithMismatchedDraft }),
      listExtractions: vi.fn().mockResolvedValue(extractionPage([readyExtraction])),
      listMaterials: vi.fn().mockResolvedValue({
        items: [material],
        next_cursor: null,
        page_schema_version: "project-material-page@1",
      }),
      listProjects: vi.fn(),
      startExtraction: vi.fn(),
      uploadMaterial: vi.fn(),
    } satisfies ProjectsApi;

    renderPage(api);

    expect(await screen.findByRole("button", { name: "创建发布草案" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "验证并发布这个版本" })).not.toBeInTheDocument();
    expect(screen.queryByText("artifact:old-limited-event-patch")).not.toBeInTheDocument();
  });

  it("keeps multiple material-backed proposals and lets the planner discard one without deleting evidence", async () => {
    const user = userEvent.setup();
    const rulesMaterial: ProjectMaterial = {
      ...material,
      created_at: "2026-07-24T00:01:30Z",
      display_name: "活动奖励规则",
      material_id: "material:event-rules",
      original_source_artifact_id: "artifact:source:event-rules:raw",
      rendered_source_artifact_id: "artifact:source:event-rules:rendered",
    };
    const currentExtraction: ProjectExtraction = {
      ...readyExtraction,
      material_ids: [material.material_id, rulesMaterial.material_id],
      publication_approval_id: "approval:patch:artifact:human-patch",
      publication_patch_artifact_id: "artifact:human-patch",
      source_artifact_ids: [material.rendered_source_artifact_id, rulesMaterial.rendered_source_artifact_id],
    };
    const olderExtraction: ProjectExtraction = {
      ...failedExtraction,
      created_at: "2026-07-23T10:00:00Z",
      extraction_id: "extraction:older-direction",
      run_id: "run:extract:older-direction",
      updated_at: "2026-07-23T10:02:00Z",
    };
    const discardedExtraction: ProjectExtraction = {
      ...currentExtraction,
      discard_reason: "活动方向调整，改用另一套奖励机制",
      discarded_at: "2026-07-24T00:05:00Z",
      discarded_by: "principal:admin",
      disposition: "discarded",
      revision: 2,
      updated_at: "2026-07-24T00:05:00Z",
    };
    const projectWithExtraction: Project = {
      ...project,
      latest_extraction_id: currentExtraction.extraction_id,
      latest_patch_artifact_id: "artifact:human-patch",
    };
    const getExtraction = vi.fn().mockResolvedValue({ etag: '"extraction:1"', value: currentExtraction });
    const discardExtraction = vi.fn().mockResolvedValue(discardedExtraction);
    const api = {
      addTextMaterial: vi.fn(),
      createContentDraft: vi.fn(),
      createProject: vi.fn(),
      discardExtraction,
      getArtifact: vi.fn().mockResolvedValue(preview),
      getExtraction,
      getProject: vi.fn().mockResolvedValue({ etag: '"project:2"', value: projectWithExtraction }),
      listExtractions: vi.fn().mockResolvedValue(extractionPage([currentExtraction, olderExtraction])),
      listMaterials: vi.fn().mockResolvedValue({
        items: [material, rulesMaterial],
        next_cursor: null,
        page_schema_version: "project-material-page@1",
      }),
      getMaterial: vi.fn(),
      listProjects: vi.fn(),
      renameMaterial: vi.fn(),
      startExtraction: vi.fn(),
      uploadMaterial: vi.fn(),
    } satisfies ProjectsApi;
    renderPage(api);

    expect(await screen.findByText("提案记录（2）")).toBeVisible();
    expect(screen.getByText("已选择 2 份材料。AI 会把它们一起作为这次提案的依据。")).toBeVisible();
    expect(screen.getByText(/2 份材料 · 创建于/u)).toBeVisible();
    expect(await screen.findByRole("region", { name: "实体与关系编辑器" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "放弃这次提案" }));
    expect(screen.getByText(/不会删除原材料、AI 运行过程或检查证据/u)).toBeVisible();
    expect(screen.getByText(/已经另行创建过发布草案/u)).toBeVisible();
    await user.type(screen.getByLabelText("放弃原因"), "活动方向调整，改用另一套奖励机制");
    await user.click(screen.getByRole("button", { name: "确认放弃提案" }));

    await waitFor(() =>
      expect(discardExtraction).toHaveBeenCalledWith(
        project.project_id,
        currentExtraction.extraction_id,
        {
          expected_revision: 1,
          reason: "活动方向调整，改用另一套奖励机制",
          request_schema_version: "project-extraction-discard-request@1",
        },
        { idempotencyKey: expect.any(String) },
        '"extraction:1"',
      ),
    );
    expect(await screen.findByRole("heading", { name: "这次提案已放弃" })).toBeVisible();
    expect(screen.getByText(/放弃原因：活动方向调整，改用另一套奖励机制/u)).toBeVisible();
    expect(screen.getByText(/材料、AI 运行过程和确定性检查证据仍然保留/u)).toBeVisible();
    expect(screen.getByText(/已经创建的发布草案是独立审计记录/u)).toBeVisible();
    expect(screen.queryByRole("region", { name: "实体与关系编辑器" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "验证并发布这个版本" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看保留的发布草案" })).toHaveAttribute(
      "href",
      "/patches/artifact%3Ahuman-patch",
    );
  });

  it("explains and enforces the 64-material limit before starting a proposal", async () => {
    const manyMaterials = Array.from(
      { length: 65 },
      (_, index): ProjectMaterial => ({
        ...material,
        display_name: `策划材料 ${index + 1}`,
        material_id: `material:${index + 1}`,
        original_source_artifact_id: `artifact:source:${index + 1}:raw`,
        rendered_source_artifact_id: `artifact:source:${index + 1}:rendered`,
      }),
    );
    const api = {
      addTextMaterial: vi.fn(),
      createContentDraft: vi.fn(),
      createProject: vi.fn(),
      discardExtraction: vi.fn(),
      renameMaterial: vi.fn(),
      getArtifact: vi.fn(),
      getExtraction: vi.fn(),
      getMaterial: vi.fn(),
      getProject: vi.fn().mockResolvedValue({ etag: '"project:1"', value: project }),
      listExtractions: vi.fn().mockResolvedValue(extractionPage()),
      listMaterials: vi.fn().mockResolvedValue({
        items: manyMaterials,
        next_cursor: null,
        page_schema_version: "project-material-page@1",
      }),
      listProjects: vi.fn(),
      startExtraction: vi.fn(),
      uploadMaterial: vi.fn(),
    } satisfies ProjectsApi;
    renderPage(api);

    expect(await screen.findByText(/已选择 65 份材料，单次提案最多组合 64 份/u)).toBeVisible();
    expect(screen.getByRole("button", { name: "AI 提取实体与关系" })).toBeDisabled();
  });

  it("polls a queued extraction to its failed terminal state and explains that materials are safe", async () => {
    const projectWithExtraction = {
      ...project,
      latest_extraction_id: queuedExtraction.extraction_id,
    };
    const getExtraction = vi.fn().mockResolvedValueOnce({ etag: '"extraction:2"', value: failedExtraction });
    const api = {
      addTextMaterial: vi.fn(),
      createContentDraft: vi.fn(),
      createProject: vi.fn(),
      discardExtraction: vi.fn(),
      renameMaterial: vi.fn(),
      getArtifact: vi.fn(),
      getExtraction,
      getProject: vi.fn().mockResolvedValue({ etag: '"project:1"', value: projectWithExtraction }),
      listExtractions: vi.fn().mockResolvedValue(extractionPage([queuedExtraction])),
      listMaterials: vi.fn().mockResolvedValue({
        items: [material],
        next_cursor: null,
        page_schema_version: "project-material-page@1",
      }),
      getMaterial: vi.fn(),
      listProjects: vi.fn(),
      startExtraction: vi.fn(),
      uploadMaterial: vi.fn(),
    } satisfies ProjectsApi;

    renderPage(api);

    expect((await screen.findAllByText("提取未完成")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("AI 输出达到长度上限，系统已安全停止。")).toBeVisible();
    expect(screen.getByText(/材料和项目内容均未改变/)).toBeVisible();
    expect(getExtraction).toHaveBeenCalledTimes(1);
  });

  it("shows a rejected but editable proposal as actionable validation work", async () => {
    const projectWithExtraction = {
      ...project,
      latest_extraction_id: validationExtraction.extraction_id,
    };
    const api = {
      addTextMaterial: vi.fn(),
      createContentDraft: vi.fn(),
      createProject: vi.fn(),
      discardExtraction: vi.fn(),
      renameMaterial: vi.fn(),
      getArtifact: vi.fn().mockResolvedValue(preview),
      getExtraction: vi.fn().mockResolvedValue({ etag: '"extraction:2"', value: validationExtraction }),
      getMaterial: vi.fn(),
      getProject: vi.fn().mockResolvedValue({ etag: '"project:2"', value: projectWithExtraction }),
      listExtractions: vi.fn().mockResolvedValue(extractionPage([validationExtraction])),
      listMaterials: vi.fn().mockResolvedValue({
        items: [material],
        next_cursor: null,
        page_schema_version: "project-material-page@1",
      }),
      listProjects: vi.fn(),
      startExtraction: vi.fn(),
      uploadMaterial: vi.fn(),
    } satisfies ProjectsApi;

    renderPage(api);

    expect((await screen.findAllByText("有内容需要确认")).length).toBe(2);
    expect(screen.getByRole("heading", { name: "已生成草案，需处理 2 个检查问题" })).toBeVisible();
    expect(screen.getByText("任务缺少起点或步骤")).toBeVisible();
    expect(screen.getByText("货币产出链不完整")).toBeVisible();
    expect(screen.getByText("未寄之梦")).toBeVisible();
    expect(screen.getByText("梦迹书签")).toBeVisible();
    expect(screen.queryByText(/sha256:/u)).not.toBeInTheDocument();
    expect(await screen.findByRole("region", { name: "实体与关系编辑器" })).toBeVisible();
  });

  it("takes a planner from an unbound schedule issue directly to the matching event form", async () => {
    const user = userEvent.setup();
    const projectWithExtraction = {
      ...project,
      latest_extraction_id: lifecycleExtraction.extraction_id,
    };
    const api = {
      addTextMaterial: vi.fn(),
      createContentDraft: vi.fn(),
      createProject: vi.fn(),
      discardExtraction: vi.fn(),
      renameMaterial: vi.fn(),
      getArtifact: vi.fn().mockResolvedValue(lifecyclePreview),
      getExtraction: vi.fn().mockResolvedValue({ etag: '"extraction:2"', value: lifecycleExtraction }),
      getMaterial: vi.fn(),
      getProject: vi.fn().mockResolvedValue({ etag: '"project:2"', value: projectWithExtraction }),
      listExtractions: vi.fn().mockResolvedValue(extractionPage([lifecycleExtraction])),
      listMaterials: vi.fn().mockResolvedValue({
        items: [material],
        next_cursor: null,
        page_schema_version: "project-material-page@1",
      }),
      listProjects: vi.fn(),
      startExtraction: vi.fn(),
      uploadMaterial: vi.fn(),
    } satisfies ProjectsApi;

    renderPage(api);

    const action = await screen.findByRole("button", { name: "设置活动档期" });
    await waitFor(() => expect(action).toBeEnabled());
    expect(screen.queryByRole("heading", { name: "活动档期" })).not.toBeInTheDocument();
    await user.click(action);

    expect(await screen.findByRole("heading", { name: "活动档期" })).toBeVisible();
    expect(screen.getByLabelText("活动开始时间")).toHaveFocus();
    expect(screen.getByText("材料写明：活动持续 14 天，结束后可领奖 3 天。")).toBeVisible();
  });

  it("accepts Feishu exports and rejects unsupported files before upload", async () => {
    const user = userEvent.setup();
    const uploadMaterial = vi.fn().mockResolvedValue(material);
    const api = {
      addTextMaterial: vi.fn(),
      createContentDraft: vi.fn(),
      createProject: vi.fn(),
      discardExtraction: vi.fn(),
      renameMaterial: vi.fn(),
      getArtifact: vi.fn(),
      getExtraction: vi.fn(),
      getMaterial: vi.fn(),
      getProject: vi.fn().mockResolvedValue({ etag: '\"project:1\"', value: project }),
      listExtractions: vi.fn().mockResolvedValue(extractionPage()),
      listMaterials: vi.fn().mockResolvedValue({
        items: [],
        next_cursor: null,
        page_schema_version: "project-material-page@1",
      }),
      listProjects: vi.fn(),
      startExtraction: vi.fn(),
      uploadMaterial,
    } satisfies ProjectsApi;
    renderPage(api);
    await screen.findByRole("heading", { name: "天空港计划" });

    const input = screen.getByLabelText("上传策划文件");
    expect(input).toHaveAttribute("multiple");
    await user.upload(input, new File(["bad"], "idea.pdf", { type: "application/pdf" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("暂不支持 PDF");
    expect(uploadMaterial).not.toHaveBeenCalled();

    await user.upload(input, [
      new File(["docx"], "飞书策划导出.docx", {
        type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      }),
      new File(["xlsx"], "活动数值.xlsx", {
        type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      }),
    ]);
    await waitFor(() => expect(uploadMaterial).toHaveBeenCalledTimes(2));
    expect(uploadMaterial).toHaveBeenNthCalledWith(
      1,
      project.project_id,
      expect.objectContaining({ name: "飞书策划导出.docx" }),
      "docx",
      { idempotencyKey: expect.any(String) },
    );
    expect(uploadMaterial).toHaveBeenNthCalledWith(
      2,
      project.project_id,
      expect.objectContaining({ name: "活动数值.xlsx" }),
      "xlsx",
      { idempotencyKey: expect.any(String) },
    );
    expect(await screen.findByText("已读取 2 份策划文件，并全部选入本次提案。")).toBeVisible();
  });

  it("renames retained material without touching its Artifacts", async () => {
    const user = userEvent.setup();
    const renameMaterial = vi.fn().mockResolvedValue({
      ...material,
      display_name: "天空港核心创意",
      revision: material.revision + 1,
    });
    const api = {
      addTextMaterial: vi.fn(),
      createContentDraft: vi.fn(),
      createProject: vi.fn(),
      discardExtraction: vi.fn(),
      renameMaterial,
      getArtifact: vi.fn(),
      getExtraction: vi.fn(),
      getMaterial: vi.fn().mockResolvedValue({ etag: '"material:1"', value: material }),
      getProject: vi.fn().mockResolvedValue({ etag: '"project:3"', value: project }),
      listExtractions: vi.fn().mockResolvedValue(extractionPage()),
      listMaterials: vi.fn().mockResolvedValue({
        items: [material],
        next_cursor: null,
        page_schema_version: "project-material-page@1",
      }),
      listProjects: vi.fn(),
      startExtraction: vi.fn(),
      uploadMaterial: vi.fn(),
    } satisfies ProjectsApi;
    renderPage(api);

    await user.click(await screen.findByRole("button", { name: `重命名 ${material.display_name}` }));
    const field = screen.getByRole("textbox", { name: "新的材料名称" });
    await user.clear(field);
    await user.type(field, "天空港核心创意");
    await user.click(screen.getByRole("button", { name: "保存名称" }));

    await waitFor(() =>
      expect(renameMaterial).toHaveBeenCalledWith(
        project.project_id,
        material.material_id,
        expect.objectContaining({
          display_name: "天空港核心创意",
          expected_revision: material.revision,
        }),
        expect.anything(),
        expect.any(String),
      ),
    );
  });
});
