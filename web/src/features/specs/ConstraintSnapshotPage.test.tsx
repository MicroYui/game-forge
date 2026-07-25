import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import { createQueryClient } from "../../api/query-client";
import {
  ConstraintSnapshotPage,
  type ConstraintSnapshotApi,
  type ConstraintSnapshotAuthorityEvidence,
} from "./ConstraintSnapshotPage";

type Snapshot = components["schemas"]["ConstraintSnapshotViewV1"];

const artifact: components["schemas"]["ArtifactSummaryV1"] = {
  artifact_id: "artifact:constraint:frontier",
  created_at: "2026-07-19T08:30:00Z",
  domain_scope: { domain_ids: ["domain:economy"] },
  kind: "constraint_snapshot",
  lineage_schema_version: "lineage@2",
  parent_artifact_ids: ["artifact:proposal:frontier"],
  payload_hash: "c".repeat(64),
  payload_schema_id: "constraint-snapshot@1",
  summary_schema_version: "artifact-summary@1",
  version_tuple: {
    constraint_snapshot_id: "constraint:frontier",
    tool_version: "compile@1",
  },
};

const snapshot: Snapshot = {
  artifact,
  constraints: [
    {
      assert: "reward_gold <= 75",
      dsl_grammar_version: "dsl@1",
      id: "constraint:economy:reward-cap",
      kind: "numeric",
      note: "控制主线奖励上限",
      oracle: "deterministic",
      severity: "major",
    },
  ],
  dsl_grammar_version: "dsl@1",
  view_schema_version: "constraint-snapshot-view@1",
};

function api(value: Snapshot = snapshot): ConstraintSnapshotApi {
  return { getConstraintSnapshot: vi.fn(async () => value) };
}

function renderPage(
  snapshotApi: ConstraintSnapshotApi,
  authorityEvidence: ConstraintSnapshotAuthorityEvidence,
  refName: string | null = null,
) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <ConstraintSnapshotPage
        api={snapshotApi}
        artifactId={artifact.artifact_id}
        authorityEvidence={authorityEvidence}
        refName={refName}
      />
    </QueryClientProvider>,
  );
}

describe("ConstraintSnapshotPage", () => {
  it("keeps an approved target visibly candidate until publication/ref-history evidence exists", async () => {
    renderPage(api(), {
      approvalId: "approval:constraint:frontier",
      approvalStatus: "approved",
      evidenceKind: "approval_target",
      targetArtifactId: artifact.artifact_id,
      workflowRevision: 6,
    });

    expect(await screen.findByRole("heading", { level: 1, name: "规则版本详情" })).toBeVisible();
    expect(screen.getByText("待发布")).toBeVisible();
    expect(screen.getByText("规则仍在审批流程中，不会影响当前正式内容。")).toBeVisible();
    expect(screen.getByRole("link", { name: "查看审批进度" })).toHaveAttribute(
      "href",
      "/approvals/approval%3Aconstraint%3Afrontier",
    );
    expect(screen.queryByText("这是当前生效的规则版本")).not.toBeInTheDocument();
    expect(screen.getByText("reward_gold ≤ 75")).toBeVisible();
  });

  it("labels authority only when exact ref history resolves to this Artifact", async () => {
    renderPage(api(), {
      evidenceKind: "ref_history",
      refName: "refs/constraints/economy",
      refValue: { artifact_id: artifact.artifact_id, revision: 12 },
    });

    expect(await screen.findByText("这是当前生效的规则版本")).toBeVisible();
    expect(screen.getByText(/当前内容正在使用第 12 版规则/)).toBeVisible();
    expect(screen.getByRole("link", { name: "查看规则版本历史" })).toHaveAttribute(
      "href",
      "/refs/refs%2Fconstraints%2Feconomy/history",
    );
  });

  it("derives production-route authority from the complete exact ref history", async () => {
    const listRefHistory = vi.fn(async () => ({
      expires_at: "2026-07-19T09:00:00Z",
      items: [
        {
          entry_schema_version: "ref-history-entry@1" as const,
          ref_name: "refs/constraints/economy",
          value: { artifact_id: artifact.artifact_id, revision: 12 },
        },
      ],
      next_cursor: null,
      page_schema_version: "page@1" as const,
      read_snapshot_id: "read:constraint-authority",
    }));
    renderPage(
      { getConstraintSnapshot: vi.fn(async () => snapshot), listRefHistory },
      { evidenceKind: "unresolved", reason: "default must not win" },
      "refs/constraints/economy",
    );

    expect(await screen.findByText("这是当前生效的规则版本")).toBeVisible();
    expect(listRefHistory).toHaveBeenCalledWith("refs/constraints/economy", null);
  });

  it("labels an exact historical occurrence without calling it current authority", async () => {
    const listRefHistory = vi.fn(async () => ({
      expires_at: "2026-07-19T09:00:00Z",
      items: [
        {
          entry_schema_version: "ref-history-entry@1" as const,
          ref_name: "refs/constraints/economy",
          value: { artifact_id: artifact.artifact_id, revision: 12 },
        },
        {
          entry_schema_version: "ref-history-entry@1" as const,
          ref_name: "refs/constraints/economy",
          value: { artifact_id: "artifact:constraint:current", revision: 13 },
        },
      ],
      next_cursor: null,
      page_schema_version: "page@1" as const,
      read_snapshot_id: "read:constraint-authority",
    }));
    renderPage(
      { getConstraintSnapshot: vi.fn(async () => snapshot), listRefHistory },
      { evidenceKind: "unresolved", reason: "default must not win" },
      "refs/constraints/economy",
    );

    expect(await screen.findByText("这是曾发布过的历史约束")).toBeVisible();
    expect(screen.getByText(/当前已经更新到第 13 版/)).toBeVisible();
    expect(screen.queryByText("这是当前生效的规则版本")).not.toBeInTheDocument();
  });

  it("refuses authority evidence that points at another Artifact", async () => {
    renderPage(api(), {
      evidenceKind: "ref_history",
      refName: "refs/constraints/economy",
      refValue: { artifact_id: "artifact:constraint:other", revision: 13 },
    });

    expect(await screen.findByText("无法确认这版规则是否生效")).toBeVisible();
    expect(screen.getByText(/证据指向另一 Artifact/)).not.toBeVisible();
    expect(screen.queryByText("这是当前生效的规则版本")).not.toBeInTheDocument();
  });

  it("guards the JsonValue payload behind the exact constraint schema id", async () => {
    renderPage(
      api({
        ...snapshot,
        artifact: {
          ...snapshot.artifact,
          payload_schema_id: "constraint-snapshot@2",
        },
        constraints: [{ assert: "secret raw payload must not render" }],
      }),
      { evidenceKind: "unresolved", reason: "未读取到批准目标或 ref 历史。" },
    );

    expect(await screen.findByRole("heading", { name: "无法安全读取规则内容" })).toBeVisible();
    expect(screen.getByText("constraint-snapshot@2")).not.toBeVisible();
    expect(screen.queryByText(/secret raw payload/)).not.toBeInTheDocument();
  });

  it("rejects malformed JsonValue even under the current schema id", async () => {
    renderPage(
      api({
        ...snapshot,
        constraints: [{ assert: "missing typed identity must not render" }],
      }),
      { evidenceKind: "unresolved", reason: "尚无权威证据。" },
    );

    expect(await screen.findByRole("heading", { name: "无法安全读取规则内容" })).toBeVisible();
    expect(screen.queryByText(/missing typed identity/)).not.toBeInTheDocument();
  });

  it("shows empty, loading, and safe error states without inventing constraints", async () => {
    renderPage(api({ ...snapshot, constraints: [] }), {
      evidenceKind: "unresolved",
      reason: "尚无权威证据。",
    });
    expect(await screen.findByRole("heading", { name: "这一版没有规则" })).toBeVisible();

    let reject!: (reason: Error) => void;
    const pending = new Promise<Awaited<ReturnType<ConstraintSnapshotApi["getConstraintSnapshot"]>>>(
      (_, rejectPromise) => {
        reject = rejectPromise;
      },
    );
    renderPage(
      { getConstraintSnapshot: vi.fn(() => pending) },
      { evidenceKind: "unresolved", reason: "尚无权威证据。" },
    );
    expect(screen.getByRole("heading", { name: "正在读取规则版本" })).toBeVisible();
    reject(new Error("internal object location must-not-render"));
    expect(await screen.findByRole("heading", { name: "无法读取规则版本" })).toBeVisible();
    expect(screen.queryByText(/object location/)).not.toBeInTheDocument();
  });
});
