import { describe, expect, it, vi } from "vitest";

import type { GameForgeOpenApiClient } from "../../api/client";
import { createRunDetailApi, type TraceSummary } from "./api";

const run = {
  attempt_no: 2,
  events_url: "/api/v1/runs/run%3A1/events",
  failure_artifact_id: null,
  result_artifact_id: "artifact:result",
  revision: 7,
  run_id: "run:1",
  status: "succeeded",
  status_url: "/api/v1/runs/run%3A1",
  terminal_cassette_artifact_id: "artifact:cassette",
  view_schema_version: "run-view@1",
} as const;

const finding = {
  created_at: "2026-07-19T12:01:00Z",
  finding_id: "finding:1",
  payload: {
    defect_class: "dead_quest",
    message: "任务不可达",
    oracle_type: "deterministic",
    payload_schema_version: "finding-payload@1",
    producer_id: "graph",
    producer_run_id: "run:1",
    severity: "critical",
    snapshot_id: "snapshot:ir",
    source: "checker",
    status: "confirmed",
  },
  revision: 1,
  revision_schema_version: "finding-revision@1",
} as const;

const command = {
  applied_at: null,
  client_id: "client:1",
  client_seq: 1,
  command_id: "command:1",
  created_at: "2026-07-19T12:02:00Z",
  payload_schema_id: "run-cancel@1",
  rejection_code: "run_terminal",
  result_event_seq: 4,
  revision: 1,
  run_id: "run:1",
  status: "rejected",
  type: "cancel",
} as const;

const trace: TraceSummary = {
  duration_ns: null,
  ended_at: null,
  root_span_id: null,
  run_ids: ["run:1"],
  service_names: ["worker"],
  span_count: 2,
  started_at: "2026-07-19T12:00:00Z",
  status: "unset",
  trace_id: "trace:1",
  trace_schema_version: "trace-summary@1",
  truncated: false,
};

function response<T>(data: T, status = 200) {
  return {
    data,
    response: new Response(status === 200 ? JSON.stringify(data) : undefined, { status }),
  };
}

function page<T>(items: T[], nextCursor: string | null) {
  return {
    expires_at: "2026-07-19T13:00:00Z",
    items,
    next_cursor: nextCursor,
    page_schema_version: "page@1",
    read_snapshot_id: "snapshot:1",
  } as const;
}

function tracePage(items: TraceSummary[], nextCursor: string | null) {
  return {
    coverage_end: "2026-07-19T12:10:00Z",
    coverage_start: "2026-07-19T12:00:00Z",
    items,
    next_cursor: nextCursor,
    page_schema_version: "trace-summary-page@1",
    truncated: false,
  } as const;
}

function artifact(artifactId: string, kind: "run_result" | "run_failure") {
  return {
    artifact: {
      artifact_id: artifactId,
      created_at: "2026-07-19T12:00:00Z",
      domain_scope: "all",
      kind,
      lineage_schema_version: "lineage@2",
      parent_artifact_ids: [],
      payload_hash: "a".repeat(64),
      payload_schema_id: `${kind.replace("run_", "run-")}@1`,
      summary_schema_version: "artifact-summary@1",
      version_tuple: {},
    },
    payload: { manifest: artifactId },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  } as const;
}

describe("run detail API", () => {
  it("loads one bounded page per collection and passes each next_cursor byte-for-byte on demand", async () => {
    const cursors = {
      commands: "opaque.commands+/=%2Ftail",
      findings: "opaque.findings+/=%2Ftail",
      traces: "opaque.traces+/=%2Ftail",
    };
    const get = vi.fn(async (path: string, options: unknown) => {
      const cursor = (options as { params?: { query?: { cursor?: string } } }).params?.query?.cursor;
      if (path === "/api/v1/runs/{run_id}") return response(run);
      if (path === "/api/v1/runs/{run_id}/findings") {
        return response(
          page(
            [{ ...finding, finding_id: cursor ? "finding:2" : "finding:1" }],
            cursor ? null : cursors.findings,
          ),
        );
      }
      if (path === "/api/v1/runs/{run_id}/commands") {
        return response(
          page(
            [{ ...command, command_id: cursor ? "command:2" : "command:1" }],
            cursor ? null : cursors.commands,
          ),
        );
      }
      if (path === "/api/v1/runs/{run_id}/traces") {
        return response(
          tracePage([{ ...trace, trace_id: cursor ? "trace:2" : "trace:1" }], cursor ? null : cursors.traces),
        );
      }
      if (path === "/api/v1/artifacts/{artifact_id}") {
        const artifactId = (options as { params: { path: { artifact_id: string } } }).params.path.artifact_id;
        return response(artifact(artifactId, "run_result"));
      }
      throw new Error(`Unexpected path: ${path}`);
    });
    const api = createRunDetailApi({ GET: get } as unknown as GameForgeOpenApiClient);

    const detail = await api.load("run:1");

    expect(detail.run).toEqual(run);
    expect(detail.findingsPage.items.map((item) => item.finding_id)).toEqual(["finding:1"]);
    expect(detail.findingsPage.next_cursor).toBe(cursors.findings);
    expect(detail.commandsPage.items.map((item) => item.command_id)).toEqual(["command:1"]);
    expect(detail.commandsPage.next_cursor).toBe(cursors.commands);
    expect(detail.tracesPage.items.map((item) => item.trace_id)).toEqual(["trace:1"]);
    expect(detail.tracesPage.next_cursor).toBe(cursors.traces);
    expect(detail.resultManifest?.artifact.artifact_id).toBe("artifact:result");
    expect(detail.failureManifest).toBeNull();
    expect(get.mock.calls.filter(([path]) => path === "/api/v1/runs/{run_id}/findings")).toHaveLength(1);
    expect(get.mock.calls.filter(([path]) => path === "/api/v1/runs/{run_id}/commands")).toHaveLength(1);
    expect(get.mock.calls.filter(([path]) => path === "/api/v1/runs/{run_id}/traces")).toHaveLength(1);

    const [nextFindings, nextCommands, nextTraces] = await Promise.all([
      api.loadFindingsPage("run:1", cursors.findings),
      api.loadCommandsPage("run:1", cursors.commands),
      api.loadTracesPage("run:1", cursors.traces),
    ]);

    expect(nextFindings.items[0]?.finding_id).toBe("finding:2");
    expect(nextCommands.items[0]?.command_id).toBe("command:2");
    expect(nextTraces.items[0]?.trace_id).toBe("trace:2");
    expect(get).toHaveBeenCalledWith(
      "/api/v1/runs/{run_id}/findings",
      expect.objectContaining({ params: { path: { run_id: "run:1" }, query: { cursor: cursors.findings } } }),
    );
    expect(get).toHaveBeenCalledWith(
      "/api/v1/runs/{run_id}/commands",
      expect.objectContaining({ params: { path: { run_id: "run:1" }, query: { cursor: cursors.commands } } }),
    );
    expect(get).toHaveBeenCalledWith(
      "/api/v1/runs/{run_id}/traces",
      expect.objectContaining({ params: { path: { run_id: "run:1" }, query: { cursor: cursors.traces } } }),
    );
  });

  it("does not invent terminal manifests when the RunView has no artifact IDs", async () => {
    const get = vi.fn(async (path: string) => {
      if (path === "/api/v1/runs/{run_id}") {
        return response({
          events_url: "/api/v1/runs/run%3Aqueued/events",
          revision: 1,
          run_id: "run:queued",
          status: "queued",
          status_url: "/api/v1/runs/run%3Aqueued",
          view_schema_version: "run-view@1",
        });
      }
      if (path === "/api/v1/runs/{run_id}/traces") return response(tracePage([], null));
      return response(page([], null));
    });
    const api = createRunDetailApi({ GET: get } as unknown as GameForgeOpenApiClient);

    const detail = await api.load("run:queued");

    expect(detail.resultManifest).toBeNull();
    expect(detail.failureManifest).toBeNull();
    expect(get.mock.calls.map(([path]) => path)).not.toContain("/api/v1/artifacts/{artifact_id}");
  });

  it("preserves an expired next_cursor for an explicit first-page restart", async () => {
    const staleCursor = "signed.opaque+/=%2Ftail";
    const get = vi.fn(async (_path: string, options: unknown) => {
      const cursor = (options as { params?: { query?: { cursor?: string } } }).params?.query?.cursor;
      if (cursor === staleCursor) {
        return {
          error: {
            code: "cursor_expired",
            conflict_set_id: null,
            detail: "读取快照已过期",
            earliest_cursor: null,
            instance: "/api/v1/runs/run:1/commands",
            request_id: "request:1",
            retry_after_s: null,
            run_id: "run:1",
            status: 410,
            title: "Cursor expired",
            trace_id: null,
            type: "about:blank",
          },
          response: new Response(undefined, { status: 410 }),
        };
      }
      return response(page([], null));
    });
    const api = createRunDetailApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await expect(api.loadCommandsPage("run:1", staleCursor)).rejects.toMatchObject({
      name: "CursorExpiredError",
      staleCursor,
    });
    expect(get).toHaveBeenCalledWith(
      "/api/v1/runs/{run_id}/commands",
      expect.objectContaining({ params: { path: { run_id: "run:1" }, query: { cursor: staleCursor } } }),
    );
  });
});
