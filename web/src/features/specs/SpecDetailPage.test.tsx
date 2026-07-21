import { QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import { createQueryClient } from "../../api/query-client";
import { SpecDetailPage, type SpecDetailApi } from "./SpecDetailPage";

const cytoscapeMock = vi.hoisted(() => {
  const selected = { addClass: vi.fn(), empty: () => false, select: vi.fn(), unselect: vi.fn() };
  const elements = { removeClass: vi.fn(), unselect: vi.fn() };
  return {
    factory: vi.fn(() => ({
      $id: vi.fn(() => selected),
      destroy: vi.fn(),
      elements: vi.fn(() => elements),
      off: vi.fn(),
      on: vi.fn(),
    })),
  };
});

vi.mock("cytoscape", () => ({ default: cytoscapeMock.factory }));

type GraphItem = components["schemas"]["GraphItemV1"];
type Spec = components["schemas"]["SpecViewV1"];

const artifact: components["schemas"]["ArtifactSummaryV1"] = {
  artifact_id: "artifact:spec:frontier",
  created_at: "2026-07-19T08:00:00Z",
  domain_scope: { domain_ids: ["domain:narrative"] },
  kind: "ir_snapshot",
  lineage_schema_version: "lineage@2",
  parent_artifact_ids: [],
  payload_hash: "a".repeat(64),
  payload_schema_id: "ir-core@1",
  summary_schema_version: "artifact-summary@1",
  version_tuple: { ir_snapshot_id: "snapshot:frontier", tool_version: "ingest@1" },
};

const spec: Spec = {
  artifact,
  ref_name: "refs/specs/frontier",
  ref_value: { artifact_id: artifact.artifact_id, revision: 7 },
  schema_registry_version: "registry@3",
  snapshot_id: "snapshot:frontier",
  view_schema_version: "spec-view@1",
};

const schemaRegistry: components["schemas"]["SchemaRegistryDocumentV1"] = {
  registry_digest: "d".repeat(64),
  registry_schema_version: "schema-registry-document@1",
  registry_version: "registry@3",
  schemas: { "ir-core@1": { type: "object" } },
};

const exportProfile: components["schemas"]["ExecutionProfileViewV1"] = {
  compatible_run_kinds: [{ kind: "generation.propose", version: 1 }],
  display_name: "Aureus CSV export",
  domain_scope: { domain_ids: ["domain:narrative"] },
  env_contract_version: "aureus@1",
  input_schema_ids: ["generation-propose@1"],
  output_schema_ids: ["config-export-package@1"],
  profile: { profile_id: "builtin.aureus_csv_export", version: 2 },
  profile_kind: "config_export",
  profile_payload_hash: "e".repeat(64),
  required_capabilities: [],
  status: "active",
  stochastic: false,
  target_environment_profile: null,
};

const profilePage: components["schemas"]["OpaquePageV1_ExecutionProfileViewV1_"] = {
  expires_at: "2026-07-19T10:00:00Z",
  items: [exportProfile],
  next_cursor: null,
  page_schema_version: "page@1",
  read_snapshot_id: "read:profiles:spec-detail",
};

const patchDraft: components["schemas"]["PatchArtifactReadViewV1"] = {
  approval_status: "draft",
  artifact: {
    ...artifact,
    artifact_id: "artifact:patch:frontier",
    kind: "patch",
    payload_schema_id: "patch@2",
  },
  patch: {
    base_snapshot_id: artifact.artifact_id,
    ops: [],
    patch_schema_version: "patch@2",
    produced_by: "human",
    rationale: "调整前哨信标",
    revision: 1,
    side_effect_risk: "low",
    target_snapshot_id: "snapshot:frontier:patched",
  },
  regression_status: "not_started",
  validation_status: "not_started",
  view_schema_version: "patch-artifact-read-view@1",
  workflow_revision: 1,
};

const quest: GraphItem = {
  entity: {
    attrs: { name: "前哨信标" },
    id: "quest:frontier-beacon",
    schema_version: "ir-core@1",
    tags: ["主线"],
    type: "QUEST",
  },
  item_id: "quest:frontier-beacon",
  item_kind: "entity",
  item_schema_version: "graph-item@1",
};

const step: GraphItem = {
  entity: {
    attrs: { name: "向向导汇报" },
    id: "step:report-guide",
    schema_version: "ir-core@1",
    type: "QUEST_STEP",
  },
  item_id: "step:report-guide",
  item_kind: "entity",
  item_schema_version: "graph-item@1",
};

function graphPage(items: GraphItem[], nextCursor: string | null, snapshot = "read:graph") {
  return {
    expires_at: "2026-07-19T09:00:00Z",
    items,
    next_cursor: nextCursor,
    page_schema_version: "page@1" as const,
    read_snapshot_id: snapshot,
  };
}

function api(overrides: Partial<SpecDetailApi> = {}): SpecDetailApi {
  return {
    draftPatch: vi.fn(async () => patchDraft),
    getSchemaRegistry: vi.fn(async () => schemaRegistry),
    getSpec: vi.fn(async () => spec),
    listExecutionProfiles: vi.fn(async () => profilePage),
    listSpecGraph: vi.fn(async () => graphPage([quest], null)),
    ...overrides,
  };
}

function renderPage(detailApi: SpecDetailApi) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <SpecDetailPage api={detailApi} artifactId={artifact.artifact_id} />
    </QueryClientProvider>,
  );
}

describe("SpecDetailPage", () => {
  it("renders exact registry/ref facts, the bounded graph, and opaque graph pagination", async () => {
    const listSpecGraph = vi
      .fn<SpecDetailApi["listSpecGraph"]>()
      .mockResolvedValueOnce(graphPage([quest], "opaque.graph+/="))
      .mockResolvedValueOnce(graphPage([step], null));
    const user = userEvent.setup();
    renderPage(
      api({
        listSpecGraph,
      }),
    );

    expect(await screen.findByRole("heading", { level: 1, name: "规格详情" })).toBeVisible();
    expect(screen.getByText("registry@3")).toBeVisible();
    expect(screen.getByText("d".repeat(64))).toBeVisible();
    expect(screen.getByText("refs/specs/frontier · revision 7")).toBeVisible();
    expect(screen.getByRole("region", { name: "规格知识图谱" })).toBeVisible();
    expect(screen.getAllByText("quest:frontier-beacon").length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: "创建 typed Patch 草案" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "加载下一页图谱" }));

    expect(await screen.findAllByText("step:report-guide")).not.toHaveLength(0);
    expect(listSpecGraph).toHaveBeenLastCalledWith(artifact.artifact_id, "opaque.graph+/=");
  });

  it("creates an IR edit only as a typed Patch draft and keeps one intent for an exact retry", async () => {
    const transportFailure = new Error("unknown transport outcome");
    const draftPatch = vi
      .fn<SpecDetailApi["draftPatch"]>()
      .mockRejectedValueOnce(transportFailure)
      .mockImplementationOnce(api().draftPatch);
    const detailApi = api({ draftPatch });
    const user = userEvent.setup();
    renderPage(detailApi);
    await screen.findByRole("heading", { name: "创建 typed Patch 草案" });

    await user.selectOptions(
      screen.getByRole("listbox", { name: /Candidate export profile/ }),
      "builtin.aureus_csv_export@2",
    );
    fireEvent.change(screen.getByRole("textbox", { name: "Patch operations JSON" }), {
      target: {
        value: JSON.stringify([
          {
            new_value: "前哨信标（修订）",
            op: "set_entity_attr",
            op_id: "op:rename-beacon",
            target: "quest:frontier-beacon/name",
          },
        ]),
      },
    });
    await user.type(screen.getByRole("textbox", { name: "Patch rationale" }), "调整前哨信标");
    await user.type(screen.getByRole("textbox", { name: "Side-effect risk" }), "low");
    await user.click(screen.getByRole("button", { name: "创建 Patch 草案" }));

    expect(await screen.findByRole("heading", { name: "Patch 创建结果未知" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "以同一 intent 重试" }));

    expect(draftPatch).toHaveBeenCalledTimes(2);
    expect(draftPatch.mock.calls[1]).toEqual(draftPatch.mock.calls[0]);
    expect(draftPatch).toHaveBeenCalledWith(
      {
        base_snapshot_artifact_id: artifact.artifact_id,
        candidate_export_profiles: [exportProfile.profile],
        constraint_snapshot_artifact_id: null,
        expected_ref: spec.ref_value,
        expected_to_fix: [],
        ops: [
          {
            new_value: "前哨信标（修订）",
            op: "set_entity_attr",
            op_id: "op:rename-beacon",
            target: "quest:frontier-beacon/name",
          },
        ],
        preconditions: [],
        rationale: "调整前哨信标",
        ref_name: spec.ref_name,
        request_schema_version: "human-patch-draft-request@1",
        side_effect_risk: "low",
      },
      { idempotencyKey: expect.any(String) },
    );
    expect(await screen.findByRole("link", { name: "打开 Patch 草案" })).toHaveAttribute(
      "href",
      "/patches/artifact%3Apatch%3Afrontier",
    );
  });

  it("allows a deterministic human Patch to omit config exports when no constraint is selected", async () => {
    const detailApi = api();
    const user = userEvent.setup();
    renderPage(detailApi);
    await screen.findByRole("heading", { name: "创建 typed Patch 草案" });

    fireEvent.change(screen.getByRole("textbox", { name: "Patch operations JSON" }), {
      target: {
        value: JSON.stringify([
          {
            new_value: 80,
            old_value: 120,
            op: "set_entity_attr",
            op_id: "op:lower-reward",
            target: "quest:frontier-beacon.reward_gold",
          },
        ]),
      },
    });
    await user.type(screen.getByRole("textbox", { name: "Patch rationale" }), "降低任务金币奖励");
    await user.type(screen.getByRole("textbox", { name: "Side-effect risk" }), "low");
    await user.click(screen.getByRole("button", { name: "创建 Patch 草案" }));

    expect(detailApi.draftPatch).toHaveBeenCalledWith(
      expect.objectContaining({
        candidate_export_profiles: [],
        constraint_snapshot_artifact_id: null,
      }),
      { idempotencyKey: expect.any(String) },
    );
    expect(await screen.findByRole("link", { name: "打开 Patch 草案" })).toBeVisible();
  });

  it("shows an explicit graph-empty state without treating the snapshot as invalid", async () => {
    renderPage(api({ listSpecGraph: vi.fn(async () => graphPage([], null)) }));

    expect(await screen.findByRole("heading", { name: "当前快照没有图谱事实" })).toBeVisible();
    expect(screen.getByText("snapshot:frontier")).toBeVisible();
  });

  it("renders the loading and sanitized problem states", async () => {
    let reject!: (reason: Error) => void;
    const pending = new Promise<Awaited<ReturnType<SpecDetailApi["getSpec"]>>>((_, rejectPromise) => {
      reject = rejectPromise;
    });
    renderPage(api({ getSpec: vi.fn(() => pending) }));

    expect(screen.getByRole("heading", { level: 1, name: "正在读取规格详情" })).toBeVisible();
    reject(new Error("storage token=must-not-render"));

    expect(await screen.findByRole("heading", { name: "无法读取规格详情" })).toBeVisible();
    expect(screen.queryByText(/storage token/)).not.toBeInTheDocument();
  });
});
