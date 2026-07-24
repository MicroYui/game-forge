import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ArtifactPayloadView, PatchWorkflowApi } from "./api";
import { collectRollbackSnapshotDiff, RollbackContentComparison } from "./RollbackContentComparison";

function artifact(artifactId: string, payloadHash: string, payload: Record<string, unknown>) {
  return {
    artifact: {
      artifact_id: artifactId,
      payload_hash: payloadHash,
    },
    payload,
  } as ArtifactPayloadView;
}

describe("Rollback content comparison", () => {
  it("never reports no change when a bounded fallback hides a digest-proven change", () => {
    const currentPayload = Object.fromEntries(
      Array.from({ length: 65 }, (_, index) => [`field_${String(index).padStart(3, "0")}`, index]),
    );
    const targetPayload = { ...currentPayload, field_064: 999 };

    render(
      <RollbackContentComparison
        current={artifact("artifact:current", "a".repeat(64), currentPayload)}
        currentLabel="Current revision 2"
        target={artifact("artifact:target", "b".repeat(64), targetPayload)}
        targetLabel="目标 revision 1"
      />,
    );

    expect(screen.queryByText("当前可读范围内没有内容字段变化。")).not.toBeInTheDocument();
    expect(screen.getByText(/exact payload digest 不同，存在未展示变化/)).toBeVisible();
  });

  it("collects the complete snapshot diff under one stable read authority", async () => {
    const entry = (path: string, before: number, after: number) => ({
      after: { presence: "present" as const, value: after },
      before: { presence: "present" as const, value: before },
      path,
    });
    const page = (items: ReturnType<typeof entry>[], nextCursor: string | null) => ({
      diff: {
        base_snapshot_id: "snapshot:current",
        diff_schema_version: "snapshot-diff@1" as const,
        entry_count: 2,
        target_snapshot_id: "snapshot:target",
      },
      page: {
        expires_at: "2026-07-20T08:00:00Z",
        items,
        next_cursor: nextCursor,
        page_schema_version: "page@1" as const,
        read_snapshot_id: "read:rollback-diff",
      },
      page_schema_version: "snapshot-diff-http-page@1" as const,
    });
    const getSnapshotDiff = vi
      .fn<PatchWorkflowApi["getSnapshotDiff"]>()
      .mockResolvedValueOnce(page([entry("/a", 1, 2)], "cursor:diff:2"))
      .mockResolvedValueOnce(page([entry("/z", 3, 4)], null));
    const snapshot = (artifactId: string, snapshotId: string) =>
      ({
        artifact: {
          artifact_id: artifactId,
          kind: "ir_snapshot",
          version_tuple: { ir_snapshot_id: snapshotId },
        },
        payload: {},
      }) as ArtifactPayloadView;

    await expect(
      collectRollbackSnapshotDiff(
        { getSnapshotDiff } as unknown as PatchWorkflowApi,
        snapshot("artifact:current", "snapshot:current"),
        snapshot("artifact:target", "snapshot:target"),
      ),
    ).resolves.toEqual({
      entries: [entry("/a", 1, 2), entry("/z", 3, 4)],
      entryCount: 2,
    });
    expect(getSnapshotDiff.mock.calls.map((call) => call[2])).toEqual([null, "cursor:diff:2"]);
  });
});
