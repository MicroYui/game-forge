import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import { projectReplaySourcePage, replaySourceOptionLabel } from "./replaySources";

type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
type RunPage = components["schemas"]["OpaquePageV1_RunViewV1_"];
type RunView = components["schemas"]["RunViewV1"];

function run(overrides: Partial<RunView>): RunView {
  return {
    attempt_no: 1,
    events_url: "/events",
    failure_artifact_id: null,
    result_artifact_id: "artifact:result:generation",
    revision: 4,
    run_id: "run:generation:source",
    status: "succeeded",
    status_url: "/run",
    terminal_cassette_artifact_id: "artifact:cassette:generation",
    view_schema_version: "run-view@1",
    ...overrides,
  };
}

function page(items: RunView[]): RunPage {
  return {
    expires_at: "2026-07-23T05:00:00Z",
    items,
    next_cursor: "cursor:next",
    page_schema_version: "page@1",
    read_snapshot_id: "read:runs",
  };
}

function manifest(
  artifactId: string,
  runId: string,
  runKind: string,
  outcomeCode = "candidate_generated",
): ArtifactPayloadView {
  return {
    artifact: {
      artifact_id: artifactId,
      created_at: "2026-07-23T03:47:50Z",
      domain_scope: "all",
      kind: "run_result",
      lineage_schema_version: "lineage@1",
      parent_artifact_ids: [],
      payload_hash: "a".repeat(64),
      payload_schema_id: "run-result@1",
      summary_schema_version: "artifact-summary@1",
      version_tuple: {},
    },
    payload: {
      outcome_code: outcomeCode,
      result_schema_version: "run-result@1",
      run_id: runId,
      run_kind: { kind: runKind, version: 1 },
    },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

describe("replay source projection", () => {
  it("keeps only cassette-backed Runs whose exact terminal manifest has the requested kind", async () => {
    const generation = run({});
    const review = run({
      result_artifact_id: "artifact:result:review",
      run_id: "run:review:source",
      terminal_cassette_artifact_id: "artifact:cassette:review",
    });
    const withoutCassette = run({
      result_artifact_id: "artifact:result:ignored",
      run_id: "run:ignored",
      terminal_cassette_artifact_id: null,
    });
    const readArtifact = vi.fn(async (artifactId: string) =>
      artifactId.endsWith("review")
        ? manifest(artifactId, review.run_id, "review.run", "review_completed")
        : manifest(artifactId, generation.run_id, "generation.propose"),
    );

    const result = await projectReplaySourcePage(
      page([generation, review, withoutCassette]),
      { kind: "generation.propose", version: 1 },
      readArtifact,
    );

    expect(result.read_snapshot_id).toBe("read:runs");
    expect(result.next_cursor).toBe("cursor:next");
    expect(result.items).toHaveLength(1);
    expect(result.items[0]).toMatchObject({
      completedAt: "2026-07-23T03:47:50Z",
      outcomeCode: "candidate_generated",
      runKind: { kind: "generation.propose", version: 1 },
      run_id: generation.run_id,
    });
    expect(readArtifact).toHaveBeenCalledTimes(2);
    expect(replaySourceOptionLabel(result.items[0]!)).toBe(
      "内容生成 · 成功 · 第 1 次执行 · 候选内容已生成 · 2026-07-23 03:47:50 UTC · run:generation:source",
    );
  });

  it("fails closed when a cassette-backed Run terminal manifest does not match its identity", async () => {
    const source = run({});
    await expect(
      projectReplaySourcePage(
        page([source]),
        { kind: "generation.propose", version: 1 },
        async (artifactId) => manifest(artifactId, "run:other", "generation.propose"),
      ),
    ).rejects.toThrow("does not close over the Run identity");
  });
});
