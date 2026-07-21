import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../api/query-client";
import { CursorExpiredError } from "../../api/pagination";
import { sanitizeProblem } from "../../api/problem";
import { ArtifactDetailPage } from "./ArtifactDetailPage";
import type { ArtifactDetailApi, ArtifactDetailSnapshot, LineagePage } from "./api";

const parent: ArtifactDetailSnapshot["artifact"] = {
  artifact_id: "artifact:parent",
  created_at: "2026-07-18T10:00:00Z",
  domain_scope: { domain_ids: ["domain:economy"] },
  kind: "ir_snapshot",
  lineage_schema_version: "lineage@2",
  parent_artifact_ids: [],
  payload_hash: "b".repeat(64),
  payload_schema_id: "ir-core@1",
  summary_schema_version: "artifact-summary@1",
  version_tuple: { ir_snapshot_id: "snapshot:base", tool_version: "importer@1" },
};

function page(
  items: LineagePage["items"],
  nextCursor: string | null,
  snapshot = "read-snapshot:lineage",
): LineagePage {
  return {
    expires_at: "2026-07-19T11:00:00Z",
    items,
    next_cursor: nextCursor,
    page_schema_version: "page@1",
    read_snapshot_id: snapshot,
  };
}

function detail(artifactId: string, nextCursor = "opaque.next+/="): ArtifactDetailSnapshot {
  return {
    artifact: {
      ...parent,
      artifact_id: artifactId,
      kind: "patch",
      parent_artifact_ids: [parent.artifact_id],
      payload_hash: "a".repeat(64),
      payload_schema_id: "patch@2",
    },
    lineagePage: page([{ artifact: parent, depth: 1, entry_schema_version: "lineage-entry@1" }], nextCursor),
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function renderPage(api: ArtifactDetailApi, artifactId = "artifact:child") {
  const queryClient = createQueryClient();
  const result = render(
    <QueryClientProvider client={queryClient}>
      <ArtifactDetailPage api={api} artifactId={artifactId} />
    </QueryClientProvider>,
  );
  return {
    ...result,
    rerenderArtifact(nextArtifactId: string) {
      result.rerender(
        <QueryClientProvider client={queryClient}>
          <ArtifactDetailPage api={api} artifactId={nextArtifactId} />
        </QueryClientProvider>,
      );
    },
  };
}

describe("ArtifactDetailPage", () => {
  it("accumulates bounded lineage pages and retries an ordinary failure with the same opaque cursor", async () => {
    const firstFailure = new Error("下一页暂时不可用");
    const nextParent = { ...parent, artifact_id: "artifact:grandparent" };
    const loadLineagePage = vi
      .fn<ArtifactDetailApi["loadLineagePage"]>()
      .mockRejectedValueOnce(firstFailure)
      .mockResolvedValueOnce(
        page([{ artifact: nextParent, depth: 2, entry_schema_version: "lineage-entry@1" }], null),
      );
    const api: ArtifactDetailApi = {
      load: vi.fn(async () => detail("artifact:child")),
      loadLineagePage,
    };
    const user = userEvent.setup();
    renderPage(api);

    expect(await screen.findByRole("heading", { level: 1, name: "工件详情" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "加载下一页" }));
    expect(await screen.findByText("下一页读取失败。")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "重试下一页" }));

    expect(await screen.findByText("artifact:grandparent")).toBeVisible();
    expect(screen.getByText("artifact:parent")).toBeVisible();
    expect(loadLineagePage).toHaveBeenNthCalledWith(1, "artifact:child", "opaque.next+/=");
    expect(loadLineagePage).toHaveBeenNthCalledWith(2, "artifact:child", "opaque.next+/=");
  });

  it("offers an explicit first-page restart after 410 and replaces the expired snapshot", async () => {
    const expired = new CursorExpiredError(
      sanitizeProblem({
        code: "cursor_expired",
        conflict_set_id: null,
        detail: "血缘读取快照已过期",
        earliest_cursor: null,
        instance: "/lineage",
        request_id: "request:1",
        retry_after_s: null,
        run_id: null,
        status: 410,
        title: "Cursor expired",
        trace_id: null,
        type: "about:blank",
      }),
      "opaque.next+/=",
    );
    const restartedParent = { ...parent, artifact_id: "artifact:restarted-parent" };
    const loadLineagePage = vi
      .fn<ArtifactDetailApi["loadLineagePage"]>()
      .mockRejectedValueOnce(expired)
      .mockResolvedValueOnce(
        page(
          [{ artifact: restartedParent, depth: 1, entry_schema_version: "lineage-entry@1" }],
          null,
          "read-snapshot:restarted",
        ),
      );
    const api: ArtifactDetailApi = {
      load: vi.fn(async () => detail("artifact:child")),
      loadLineagePage,
    };
    const user = userEvent.setup();
    renderPage(api);

    await screen.findByRole("heading", { level: 1, name: "工件详情" });
    await user.click(screen.getByRole("button", { name: "加载下一页" }));
    expect(await screen.findByText(/分页游标已过期/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "重新开始查询" }));

    expect(await screen.findByText("artifact:restarted-parent")).toBeVisible();
    expect(screen.queryByText("artifact:parent")).not.toBeInTheDocument();
    expect(loadLineagePage).toHaveBeenNthCalledWith(2, "artifact:child", null);
  });

  it("ignores a late lineage page after the route owner changes", async () => {
    const latePage = deferred<LineagePage>();
    const loadLineagePage = vi.fn<ArtifactDetailApi["loadLineagePage"]>((artifactId) => {
      if (artifactId === "artifact:first") return latePage.promise;
      return Promise.resolve(page([], null, "read-snapshot:second"));
    });
    const api: ArtifactDetailApi = {
      load: vi.fn(async (artifactId) => detail(artifactId)),
      loadLineagePage,
    };
    const user = userEvent.setup();
    const view = renderPage(api, "artifact:first");

    await screen.findByText("artifact:first");
    await user.click(screen.getByRole("button", { name: "加载下一页" }));
    view.rerenderArtifact("artifact:second");
    expect(await screen.findByText("artifact:second")).toBeVisible();

    await act(async () => {
      latePage.resolve(
        page(
          [
            {
              artifact: { ...parent, artifact_id: "artifact:late-first-parent" },
              depth: 2,
              entry_schema_version: "lineage-entry@1",
            },
          ],
          null,
        ),
      );
      await latePage.promise;
    });

    expect(screen.queryByText("artifact:late-first-parent")).not.toBeInTheDocument();
  });
});
