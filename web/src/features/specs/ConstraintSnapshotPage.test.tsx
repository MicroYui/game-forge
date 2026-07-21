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
  version_tuple: { constraint_snapshot_id: "constraint:frontier", tool_version: "compile@1" },
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
) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <ConstraintSnapshotPage
        api={snapshotApi}
        artifactId={artifact.artifact_id}
        authorityEvidence={authorityEvidence}
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

    expect(await screen.findByRole("heading", { level: 1, name: "约束快照" })).toBeVisible();
    expect(screen.getByText("候选快照 · 尚未证明权威")).toBeVisible();
    expect(screen.getByText(/批准状态 approved 仍不等于 ref 已发布/)).toBeVisible();
    expect(screen.getByRole("link", { name: "打开批准目标" })).toHaveAttribute(
      "href",
      "/approvals/approval%3Aconstraint%3Afrontier",
    );
    expect(screen.queryByText("已由 ref 历史证明权威")).not.toBeInTheDocument();
    expect(screen.getByText("reward_gold <= 75")).toBeVisible();
  });

  it("labels authority only when exact ref history resolves to this Artifact", async () => {
    renderPage(api(), {
      evidenceKind: "ref_history",
      refName: "refs/constraints/economy",
      refValue: { artifact_id: artifact.artifact_id, revision: 12 },
    });

    expect(await screen.findByText("已由 ref 历史证明权威")).toBeVisible();
    expect(screen.getByText(/revision 12/)).toBeVisible();
    expect(screen.getByRole("link", { name: "检查 ref 历史" })).toHaveAttribute(
      "href",
      "/refs/refs%2Fconstraints%2Feconomy/history",
    );
  });

  it("refuses authority evidence that points at another Artifact", async () => {
    renderPage(api(), {
      evidenceKind: "ref_history",
      refName: "refs/constraints/economy",
      refValue: { artifact_id: "artifact:constraint:other", revision: 13 },
    });

    expect(await screen.findByText("权威状态未证明")).toBeVisible();
    expect(screen.getByText(/证据指向另一 Artifact/)).toBeVisible();
    expect(screen.queryByText("已由 ref 历史证明权威")).not.toBeInTheDocument();
  });

  it("guards the JsonValue payload behind the exact constraint schema id", async () => {
    renderPage(
      api({
        ...snapshot,
        artifact: { ...snapshot.artifact, payload_schema_id: "constraint-snapshot@2" },
        constraints: [{ assert: "secret raw payload must not render" }],
      }),
      { evidenceKind: "unresolved", reason: "未读取到批准目标或 ref 历史。" },
    );

    expect(await screen.findByRole("heading", { name: "无法安全解释约束载荷" })).toBeVisible();
    expect(screen.getByText("constraint-snapshot@2")).toBeVisible();
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

    expect(await screen.findByRole("heading", { name: "无法安全解释约束载荷" })).toBeVisible();
    expect(screen.queryByText(/missing typed identity/)).not.toBeInTheDocument();
  });

  it("shows empty, loading, and safe error states without inventing constraints", async () => {
    renderPage(api({ ...snapshot, constraints: [] }), {
      evidenceKind: "unresolved",
      reason: "尚无权威证据。",
    });
    expect(await screen.findByRole("heading", { name: "快照中没有约束条目" })).toBeVisible();

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
    expect(screen.getByRole("heading", { name: "正在读取约束快照" })).toBeVisible();
    reject(new Error("internal object location must-not-render"));
    expect(await screen.findByRole("heading", { name: "无法读取约束快照" })).toBeVisible();
    expect(screen.queryByText(/object location/)).not.toBeInTheDocument();
  });
});
