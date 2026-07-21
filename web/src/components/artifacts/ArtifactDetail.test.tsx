import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import { ArtifactDetail } from ".";

type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];
type LineagePage = components["schemas"]["OpaquePageV1_LineageEntryV1_"];

const parent: ArtifactSummary = {
  artifact_id: "artifact:parent",
  created_at: "2026-07-18T10:00:00Z",
  domain_scope: { domain_ids: ["domain:economy"] },
  kind: "ir_snapshot",
  lineage_schema_version: "lineage@2",
  parent_artifact_ids: [],
  payload_hash: "b".repeat(64),
  payload_schema_id: "ir-core@1",
  summary_schema_version: "artifact-summary@1",
  version_tuple: {
    ir_snapshot_id: "snapshot:base",
    tool_version: "ir-importer@1",
  },
};

const artifact: ArtifactSummary = {
  artifact_id: `artifact:${"a".repeat(512)}`,
  created_at: "2026-07-19T10:00:00Z",
  domain_scope: { domain_ids: ["domain:economy", "domain:quest"] },
  kind: "patch",
  lineage_schema_version: "lineage@2",
  parent_artifact_ids: [parent.artifact_id],
  payload_hash: "a".repeat(64),
  payload_schema_id: "patch@2",
  summary_schema_version: "artifact-summary@1",
  version_tuple: {
    agent_graph_version: "repair-graph@2",
    cassette_id: "sha256:cassette",
    constraint_snapshot_id: "constraint:snapshot",
    doc_version: "doc:7",
    env_contract_version: null,
    ir_snapshot_id: "snapshot:base",
    model_snapshot: "openai/gpt-5.6-sol/pre-m4@1",
    prompt_version: "repair@4",
    seed: 42,
    tool_version: "repair@3",
  },
};

const lineagePage: LineagePage = {
  expires_at: "2026-07-19T11:00:00Z",
  items: [
    {
      artifact: parent,
      depth: 1,
      entry_schema_version: "lineage-entry@1",
    },
  ],
  next_cursor: "opaque-lineage-cursor",
  page_schema_version: "page@1",
  read_snapshot_id: "read-snapshot:lineage",
};

describe("ArtifactDetail", () => {
  it("renders only the safe summary envelope, exact tuple, scope, and bounded lineage", async () => {
    const user = userEvent.setup();
    const onLoadMoreLineage = vi.fn();
    const unsafeRuntimeArtifact = {
      ...artifact,
      object_ref: { bucket: "secret-bucket", key: "private/key" },
      object_location: { endpoint: "https://storage.internal" },
    } as ArtifactSummary;

    render(
      <ArtifactDetail
        artifact={unsafeRuntimeArtifact}
        lineagePage={lineagePage}
        onLoadMoreLineage={onLoadMoreLineage}
      />,
    );

    expect(screen.getByRole("heading", { name: "工件详情" })).toBeVisible();
    expect(screen.getByText(artifact.artifact_id)).toBeVisible();
    expect(screen.getAllByText("patch")).toHaveLength(2);
    expect(screen.getByText(artifact.payload_hash!)).toBeVisible();
    expect(screen.getByText("domain:economy")).toBeVisible();
    expect(screen.getByText("domain:quest")).toBeVisible();
    expect(screen.getByText("repair-graph@2")).toBeVisible();
    expect(screen.getByText("不适用")).toBeVisible();
    expect(screen.getByText("artifact:parent")).toBeVisible();
    expect(screen.getByText(/工件存在不代表当前 ref 权威/)).toBeVisible();
    expect(screen.queryByText("secret-bucket")).not.toBeInTheDocument();
    expect(screen.queryByText("private/key")).not.toBeInTheDocument();
    expect(screen.queryByText("https://storage.internal")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "加载下一页" }));
    expect(onLoadMoreLineage).toHaveBeenCalledWith("opaque-lineage-cursor");
  });

  it("does not invent a payload hash or object reference for historical summaries", () => {
    render(
      <ArtifactDetail
        artifact={{
          ...parent,
          lineage_schema_version: "lineage@1",
          payload_hash: null,
        }}
      />,
    );

    expect(screen.getByText("历史工件未提供（不伪造）")).toBeVisible();
    expect(screen.queryByText(/ObjectRef|ObjectLocation|对象位置/)).not.toBeInTheDocument();
  });

  it("keeps a 512-character domain ID wrapped and copyable", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText");
    const longDomainId = `domain:${"域".repeat(505)}`;

    render(
      <ArtifactDetail
        artifact={{
          ...artifact,
          domain_scope: { domain_ids: [longDomainId] },
        }}
      />,
    );

    expect(longDomainId).toHaveLength(512);
    expect(screen.getByText(longDomainId)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "复制域 ID" }));
    expect(writeText).toHaveBeenCalledWith(longDomainId);
  });
});
