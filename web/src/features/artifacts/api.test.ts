import { describe, expect, it, vi } from "vitest";

import type { GameForgeOpenApiClient } from "../../api/client";
import { createArtifactDetailApi } from "./api";

const summary = {
  artifact_id: "artifact:child",
  created_at: "2026-07-19T10:00:00Z",
  domain_scope: { domain_ids: ["domain:economy"] },
  kind: "patch",
  lineage_schema_version: "lineage@2",
  parent_artifact_ids: ["artifact:parent"],
  payload_hash: "a".repeat(64),
  payload_schema_id: "patch@2",
  summary_schema_version: "artifact-summary@1",
  version_tuple: { ir_snapshot_id: "snapshot:base", tool_version: "repair@3" },
} as const;

const parent = {
  ...summary,
  artifact_id: "artifact:parent",
  kind: "ir_snapshot",
  parent_artifact_ids: [],
  payload_hash: "b".repeat(64),
  payload_schema_id: "ir-core@1",
} as const;

function response<T>(data: T, status = 200) {
  return {
    data,
    response: new Response(status === 200 ? JSON.stringify(data) : undefined, { status }),
  };
}

function lineagePage(nextCursor: string | null) {
  return {
    expires_at: "2026-07-19T11:00:00Z",
    items: [{ artifact: parent, depth: 1, entry_schema_version: "lineage-entry@1" }],
    next_cursor: nextCursor,
    page_schema_version: "page@1",
    read_snapshot_id: "read-snapshot:lineage",
  } as const;
}

describe("artifact detail API", () => {
  it("loads only the safe summary plus one bounded lineage page and passes opaque cursors byte-for-byte", async () => {
    const opaqueCursor = "signed.opaque+/=%2Ftail";
    const get = vi.fn(async (path: string, options: unknown) => {
      if (path === "/api/v1/artifacts/{artifact_id}") {
        return response({
          artifact: summary,
          payload: { raw_response: "must-not-enter-view-state", secret: "must-not-render" },
          resource_revision: 1,
          view_schema_version: "artifact-payload-view@1",
        });
      }
      if (path === "/api/v1/artifacts/{artifact_id}/lineage") {
        const cursor = (options as { params: { query?: { cursor?: string } } }).params.query?.cursor;
        return response(lineagePage(cursor ? null : opaqueCursor));
      }
      throw new Error(`Unexpected path: ${path}`);
    });
    const api = createArtifactDetailApi({ GET: get } as unknown as GameForgeOpenApiClient);

    const detail = await api.load("artifact:child");

    expect(detail.artifact).toEqual(summary);
    expect(detail).not.toHaveProperty("payload");
    expect(detail.lineagePage.next_cursor).toBe(opaqueCursor);
    await api.loadLineagePage("artifact:child", opaqueCursor);
    expect(get).toHaveBeenCalledWith(
      "/api/v1/artifacts/{artifact_id}/lineage",
      expect.objectContaining({
        params: { path: { artifact_id: "artifact:child" }, query: { cursor: opaqueCursor } },
      }),
    );
  });

  it("preserves a 410 cursor as an explicit restart boundary", async () => {
    const staleCursor = "stale.opaque+/=";
    const get = vi.fn(async (_path: string, options: unknown) => {
      const cursor = (options as { params: { query?: { cursor?: string } } }).params.query?.cursor;
      if (cursor === staleCursor) {
        return {
          error: {
            code: "cursor_expired",
            conflict_set_id: null,
            detail: "血缘读取快照已过期",
            earliest_cursor: null,
            instance: "/api/v1/artifacts/artifact:child/lineage",
            request_id: "request:1",
            retry_after_s: null,
            run_id: null,
            status: 410,
            title: "Cursor expired",
            trace_id: null,
            type: "about:blank",
          },
          response: new Response(undefined, { status: 410 }),
        };
      }
      return response(lineagePage(null));
    });
    const api = createArtifactDetailApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await expect(api.loadLineagePage("artifact:child", staleCursor)).rejects.toMatchObject({
      name: "CursorExpiredError",
      staleCursor,
    });
  });
});
