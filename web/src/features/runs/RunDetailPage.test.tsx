import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RUN_COMMAND_SUBPROTOCOL, type RunCommandClient } from "../../api/commands";
import { storeCsrfToken } from "../../api/csrf";
import type { RunEvent } from "../../api/generated/sse-run-event-v1";
import { CursorExpiredError } from "../../api/pagination";
import { sanitizeProblem } from "../../api/problem";
import { createQueryClient } from "../../api/query-client";
import { RunDetailPage } from "./RunDetailPage";
import type {
  ArtifactPayloadView,
  FindingRevision,
  FindingRevisionPage,
  RunCommandPage,
  RunDetailApi,
  RunDetailSnapshot,
  RunEventStreamCallbacks,
  RunEventStreamHandle,
  TraceSummaryPage,
} from "./api";

const finding: FindingRevision = {
  created_at: "2026-07-19T12:00:00Z",
  finding_id: "finding:1",
  payload: {
    defect_class: "dead_quest",
    message: "任务不可达",
    oracle_type: "deterministic",
    payload_schema_version: "finding-payload@1",
    producer_id: "graph",
    producer_run_id: "run:1",
    severity: "critical",
    snapshot_id: "snapshot:1",
    source: "checker",
    status: "confirmed",
  },
  revision: 2,
  revision_schema_version: "finding-revision@1",
  supersedes_revision: 1,
};

const resultManifest: ArtifactPayloadView = {
  artifact: {
    artifact_id: "artifact:result",
    created_at: "2026-07-19T12:00:00Z",
    domain_scope: "all",
    kind: "run_result",
    lineage_schema_version: "lineage@2",
    parent_artifact_ids: [],
    payload_hash: "a".repeat(64),
    payload_schema_id: "run-result@1",
    summary_schema_version: "artifact-summary@1",
    version_tuple: {},
  },
  payload: { outcome_code: "completed" },
  resource_revision: 1,
  view_schema_version: "artifact-payload-view@1",
};

const failureManifest: ArtifactPayloadView = {
  ...resultManifest,
  artifact: {
    ...resultManifest.artifact,
    artifact_id: "artifact:failure",
    kind: "run_failure",
    payload_hash: "f".repeat(64),
    payload_schema_id: "run-failure@1",
  },
  payload: { cause_code: "validation_failed" },
};

function page<T>(items: T[], nextCursor: string | null) {
  return {
    expires_at: "2026-07-19T13:00:00Z",
    items,
    next_cursor: nextCursor,
    page_schema_version: "page@1",
    read_snapshot_id: "snapshot:1",
  };
}

function tracePage(items: TraceSummaryPage["items"], nextCursor: string | null): TraceSummaryPage {
  return {
    coverage_end: "2026-07-19T12:10:00Z",
    coverage_start: "2026-07-19T12:00:00Z",
    items,
    next_cursor: nextCursor,
    page_schema_version: "trace-summary-page@1",
    truncated: false,
  };
}

const detailSnapshot: RunDetailSnapshot = {
  commandsPage: page(
    [
      {
        applied_at: null,
        client_id: "client:1",
        client_seq: 1,
        command_id: "command:1",
        created_at: "2026-07-19T11:59:00Z",
        payload_schema_id: "run-cancel@1",
        rejection_code: "run_terminal",
        result_event_seq: 4,
        revision: 2,
        run_id: "run:1",
        status: "rejected",
        type: "cancel",
      },
    ],
    "commands.next+/=",
  ) as RunCommandPage,
  failureManifest: null,
  findingsPage: page([finding], "findings.next+/=") as FindingRevisionPage,
  resultManifest,
  run: {
    attempt_no: 1,
    events_url: "/api/v1/runs/run%3A1/events",
    failure_artifact_id: null,
    result_artifact_id: "artifact:result",
    revision: 4,
    run_id: "run:1",
    status: "succeeded",
    status_url: "/api/v1/runs/run%3A1",
    terminal_cassette_artifact_id: "artifact:cassette",
    view_schema_version: "run-view@1",
  },
  tracesPage: tracePage(
    [
      {
        duration_ns: 25,
        ended_at: "2026-07-19T12:01:00Z",
        root_span_id: "span:1",
        run_ids: ["run:1"],
        service_names: ["worker"],
        span_count: 3,
        started_at: "2026-07-19T12:00:00Z",
        status: "ok",
        trace_id: "trace:1",
        trace_schema_version: "trace-summary@1",
        truncated: false,
      },
    ],
    "traces.next+/=",
  ),
};

class FakeEventStream implements RunEventStreamHandle {
  readonly close = vi.fn();
  readonly restart = vi.fn(async () => undefined);
  readonly start = vi.fn(async () => undefined);

  constructor(readonly callbacks: RunEventStreamCallbacks) {}

  emit(event: RunEvent, cursor: string) {
    this.callbacks.onEvent(event, cursor);
  }

  expire(earliestCursor: string) {
    this.callbacks.onStateChange({ earliestCursor, status: "expired" });
  }

  disconnect() {
    this.callbacks.onStateChange({ status: "disconnected" });
  }
}

class RecoverySocket {
  static latest: RecoverySocket | undefined;

  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onopen: ((event: Event) => void) | null = null;
  protocol = RUN_COMMAND_SUBPROTOCOL;
  sent: string[] = [];

  constructor(
    _url: string | URL,
    readonly protocols: string | string[],
  ) {
    RecoverySocket.latest = this;
  }

  close() {}

  send(data: string) {
    this.sent.push(data);
  }

  open() {
    this.onopen?.(new Event("open"));
  }

  disconnect() {
    this.onclose?.(new CloseEvent("close"));
  }
}

function createApi(overrides: Partial<RunDetailApi> = {}) {
  let stream: FakeEventStream | undefined;
  const api: RunDetailApi = {
    createEventStream(callbacks) {
      stream = new FakeEventStream(callbacks);
      return stream;
    },
    load: vi.fn(async () => detailSnapshot),
    loadCommandsPage: vi.fn(async () => page([], null) as RunCommandPage),
    loadFindingsPage: vi.fn(async () => page([], null) as FindingRevisionPage),
    loadTracesPage: vi.fn(async () => tracePage([], null)),
    ...overrides,
  };
  return { api, stream: () => stream! };
}

function commandClient(): RunCommandClient {
  return {
    createCancelIntent: vi.fn(),
    submit: vi.fn(),
  } as unknown as RunCommandClient;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function renderPage(api: RunDetailApi, client: RunCommandClient = commandClient(), runId = "run:1") {
  const queryClient = createQueryClient();
  const result = render(
    <QueryClientProvider client={queryClient}>
      <RunDetailPage api={api} commandClient={client} runId={runId} />
    </QueryClientProvider>,
  );
  return {
    ...result,
    client,
    queryClient,
    rerenderRun(nextRunId: string) {
      result.rerender(
        <QueryClientProvider client={queryClient}>
          <RunDetailPage api={api} commandClient={client} runId={nextRunId} />
        </QueryClientProvider>,
      );
    },
  };
}

describe("RunDetailPage", () => {
  beforeEach(() => sessionStorage.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("renders the exact RunView, events, findings, manifest, commands, and trace links", async () => {
    const { api, stream } = createApi();
    renderPage(api);

    expect(screen.getByRole("status")).toHaveTextContent("正在读取运行详情");
    expect(screen.getByRole("heading", { level: 1, name: "运行详情" })).toBeVisible();
    expect(await screen.findByRole("heading", { name: "运行 run:1" })).toBeVisible();
    expect(screen.getByText("/api/v1/runs/run%3A1/events")).toBeVisible();
    expect(screen.getByRole("link", { name: "finding:1 · r2" })).toHaveAttribute(
      "href",
      "/findings/finding%3A1/revisions/2",
    );
    expect(screen.getByText("任务不可达")).toBeVisible();
    expect(screen.getByRole("link", { name: "打开结果工件" })).toHaveAttribute(
      "href",
      "/artifacts/artifact%3Aresult",
    );
    expect(screen.getByText(/"outcome_code": "completed"/)).toBeVisible();
    expect(screen.getByText("当前 RunView 未绑定失败清单。")).toBeVisible();
    expect(screen.getByText("command:1 · rejected")).toBeVisible();
    expect(screen.getByRole("link", { name: "trace:1" })).toHaveAttribute(
      "href",
      "/observability/traces/trace%3A1",
    );

    act(() => {
      stream().emit(
        {
          attempt_no: 1,
          data: {
            attempt_no: 1,
            completed_units: 2,
            data_schema_version: "attempt-progress@1",
            phase_code: "checking",
            total_units: 4,
          },
          data_schema_version: "attempt-progress@1",
          event_schema_version: "run-event@1",
          event_type: "attempt.progress",
          occurred_at: "2026-07-19T12:00:30Z",
          run_id: "run:1",
          seq: 3,
          trace_id: "trace:1",
        },
        "3",
      );
    });

    expect(screen.getByText("attempt.progress · 2026-07-19T12:00:30Z")).toBeVisible();
    expect(screen.getByText("已完成 2 / 4")).toBeVisible();
  });

  it("keeps a long manifest payload keyboard-scrollable", async () => {
    const longValue = "payload".repeat(512);
    const { api } = createApi({
      load: vi.fn(async () => ({
        ...detailSnapshot,
        resultManifest: {
          ...resultManifest,
          payload: { long_value: longValue },
        },
      })),
    });
    renderPage(api);

    const payload = await screen.findByLabelText("结果清单 payload");
    expect(payload).toHaveAttribute("tabindex", "0");
    expect(payload).toHaveTextContent(longValue);
  });

  it("loads each collection page only after an explicit action and appends its items", async () => {
    const nextFinding = { ...finding, finding_id: "finding:2", revision: 1 };
    const nextCommand = {
      ...detailSnapshot.commandsPage.items[0]!,
      command_id: "command:2",
    };
    const nextTrace = {
      ...detailSnapshot.tracesPage.items[0]!,
      trace_id: "trace:2",
    };
    const { api } = createApi({
      loadCommandsPage: vi.fn(async () => page([nextCommand], null) as RunCommandPage),
      loadFindingsPage: vi.fn(async () => page([nextFinding], null) as FindingRevisionPage),
      loadTracesPage: vi.fn(async () => tracePage([nextTrace], null)),
    });
    const user = userEvent.setup();
    renderPage(api);
    await screen.findByRole("heading", { name: "运行 run:1" });

    expect(api.loadFindingsPage).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "加载更多Findings" }));
    await user.click(screen.getByRole("button", { name: "加载更多命令" }));
    await user.click(screen.getByRole("button", { name: "加载更多追踪" }));

    expect(await screen.findByRole("link", { name: "finding:2 · r1" })).toBeVisible();
    expect(screen.getByText("command:2 · rejected")).toBeVisible();
    expect(screen.getByRole("link", { name: "trace:2" })).toBeVisible();
    expect(api.loadFindingsPage).toHaveBeenCalledWith("run:1", "findings.next+/=");
    expect(api.loadCommandsPage).toHaveBeenCalledWith("run:1", "commands.next+/=");
    expect(api.loadTracesPage).toHaveBeenCalledWith("run:1", "traces.next+/=");
  });

  it("ignores an old page response after the same Run receives a fresh snapshot", async () => {
    const pending = deferred<FindingRevisionPage>();
    const lateFinding = { ...finding, finding_id: "finding:late" };
    const freshFinding = { ...finding, finding_id: "finding:fresh" };
    const loadFindingsPage = vi.fn(async () => pending.promise);
    const { api } = createApi({ loadFindingsPage });
    const user = userEvent.setup();
    const { queryClient } = renderPage(api);
    await screen.findByRole("heading", { name: "运行 run:1" });

    await user.click(screen.getByRole("button", { name: "加载更多Findings" }));
    act(() => {
      queryClient.setQueryData<RunDetailSnapshot>(["run-detail", "run:1"], {
        ...detailSnapshot,
        findingsPage: page([freshFinding], null) as FindingRevisionPage,
        run: { ...detailSnapshot.run, revision: 5 },
      });
    });
    expect(await screen.findByRole("link", { name: "finding:fresh · r2" })).toBeVisible();
    await act(async () => pending.resolve(page([lateFinding], null) as FindingRevisionPage));

    expect(screen.getByRole("link", { name: "finding:fresh · r2" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "finding:late · r2" })).not.toBeInTheDocument();
  });

  it("never lets an old Run page or stream state overwrite a reused route", async () => {
    const pending = deferred<FindingRevisionPage>();
    const staleFinding = { ...finding, finding_id: "finding:stale-run-1" };
    const runTwo: RunDetailSnapshot = {
      ...detailSnapshot,
      commandsPage: page([], null) as RunCommandPage,
      findingsPage: page([{ ...finding, finding_id: "finding:run-2" }], null) as FindingRevisionPage,
      run: {
        ...detailSnapshot.run,
        events_url: "/api/v1/runs/run%3A2/events",
        run_id: "run:2",
        status_url: "/api/v1/runs/run%3A2",
      },
      tracesPage: tracePage([], null),
    };
    const { api, stream } = createApi({
      load: vi.fn(async (requestedRunId) => (requestedRunId === "run:2" ? runTwo : detailSnapshot)),
      loadFindingsPage: vi.fn(async () => pending.promise),
    });
    const user = userEvent.setup();
    const { rerenderRun } = renderPage(api);
    await screen.findByRole("heading", { name: "运行 run:1" });
    const oldStream = stream();

    await user.click(screen.getByRole("button", { name: "加载更多Findings" }));
    act(() => {
      oldStream.emit(
        {
          attempt_no: 1,
          data: {
            attempt_no: 1,
            completed_units: 1,
            data_schema_version: "attempt-progress@1",
            phase_code: "old-run-progress",
            total_units: 2,
          },
          data_schema_version: "attempt-progress@1",
          event_schema_version: "run-event@1",
          event_type: "attempt.progress",
          occurred_at: "2026-07-19T12:00:30Z",
          run_id: "run:1",
          seq: 3,
          trace_id: null,
        },
        "3",
      );
      oldStream.expire("17");
    });

    rerenderRun("run:2");

    expect(await screen.findByRole("heading", { name: "运行 run:2" })).toBeVisible();
    expect(screen.getByRole("link", { name: "finding:run-2 · r2" })).toBeVisible();
    expect(screen.queryByText("attempt.progress · 2026-07-19T12:00:30Z")).not.toBeInTheDocument();
    expect(screen.queryByText(/最早可用游标 17/)).not.toBeInTheDocument();

    await act(async () => pending.resolve(page([staleFinding], null) as FindingRevisionPage));

    expect(screen.getByRole("link", { name: "finding:run-2 · r2" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "finding:stale-run-1 · r2" })).not.toBeInTheDocument();
  });

  it("keeps an expired page cursor until the operator explicitly restarts that collection", async () => {
    const expired = new CursorExpiredError(
      sanitizeProblem({
        code: "cursor_expired",
        conflict_set_id: null,
        detail: "读取快照已过期",
        earliest_cursor: null,
        instance: "/api/v1/runs/run:1/findings",
        request_id: "request:1",
        retry_after_s: null,
        run_id: "run:1",
        status: 410,
        title: "Cursor expired",
        trace_id: null,
        type: "about:blank",
      }),
      "findings.next+/=",
    );
    const loadFindingsPage = vi
      .fn<RunDetailApi["loadFindingsPage"]>()
      .mockRejectedValueOnce(expired)
      .mockResolvedValueOnce(
        page([{ ...finding, finding_id: "finding:fresh" }], null) as FindingRevisionPage,
      );
    const { api } = createApi({ loadFindingsPage });
    const user = userEvent.setup();
    renderPage(api);
    await screen.findByRole("heading", { name: "运行 run:1" });

    await user.click(screen.getByRole("button", { name: "加载更多Findings" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("读取快照已过期");
    expect(loadFindingsPage).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "从首屏重新读取 Findings" }));

    expect(await screen.findByRole("link", { name: "finding:fresh · r2" })).toBeVisible();
    expect(loadFindingsPage).toHaveBeenNthCalledWith(2, "run:1", null);
  });

  it("states honestly when the authority has no findings, traces, or terminal manifests", async () => {
    const empty: RunDetailSnapshot = {
      commandsPage: page([], null) as RunCommandPage,
      failureManifest: null,
      findingsPage: page([], null) as FindingRevisionPage,
      resultManifest: null,
      run: {
        events_url: "/api/v1/runs/run%3A1/events",
        revision: 1,
        run_id: "run:1",
        status: "queued",
        status_url: "/api/v1/runs/run%3A1",
        view_schema_version: "run-view@1",
      },
      tracesPage: tracePage([], null),
    };
    const { api } = createApi({ load: vi.fn(async () => empty) });
    renderPage(api);

    expect(await screen.findByText("当前 RunView 未绑定结果工件。")).toBeVisible();
    expect(screen.getByText("当前 RunView 未绑定失败清单。")).toBeVisible();
    expect(screen.getByText("此运行尚未发布 Finding。")).toBeVisible();
    expect(screen.getByText("此运行尚无可读追踪。")).toBeVisible();
  });

  it("renders a failure manifest only from an exact failure_artifact_id", async () => {
    const failed: RunDetailSnapshot = {
      ...detailSnapshot,
      failureManifest,
      resultManifest: null,
      run: {
        ...detailSnapshot.run,
        failure_artifact_id: "artifact:failure",
        result_artifact_id: null,
        status: "failed",
      },
    };
    const { api } = createApi({ load: vi.fn(async () => failed) });
    renderPage(api);

    expect(await screen.findByRole("link", { name: "打开失败清单" })).toHaveAttribute(
      "href",
      "/artifacts/artifact%3Afailure",
    );
    expect(screen.getByText(/"cause_code": "validation_failed"/)).toBeVisible();
    expect(screen.getByText("当前 RunView 未绑定结果工件。")).toBeVisible();
  });

  it("keeps an expired SSE cursor until the operator explicitly restarts the stream", async () => {
    const { api, stream } = createApi();
    const user = userEvent.setup();
    renderPage(api);
    await screen.findByRole("heading", { name: "运行 run:1" });

    stream().restart.mockRejectedValueOnce(new Error("事件流重启失败"));
    act(() => stream().expire("17"));

    expect(screen.getByRole("alert")).toHaveTextContent("最早可用游标 17");
    expect(stream().restart).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "重新开始事件流" }));

    expect(stream().restart).toHaveBeenCalledTimes(1);
    expect(await screen.findByRole("alert")).toHaveTextContent("事件流重启失败");
  });

  it("offers an explicit resumable reconnect after an ordinary SSE disconnect", async () => {
    const { api, stream } = createApi();
    const user = userEvent.setup();
    renderPage(api);
    await screen.findByRole("heading", { name: "运行 run:1" });

    act(() => stream().disconnect());

    expect(screen.getByRole("status")).toHaveTextContent("事件流连接已中断");
    expect(stream().start).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "重新连接事件流" }));
    expect(stream().start).toHaveBeenCalledTimes(2);
  });

  it("composes the production command client with persisted GET recovery and the owned SSE stream", async () => {
    vi.stubGlobal("WebSocket", RecoverySocket);
    storeCsrfToken("csrf-route");
    const runningSnapshot: RunDetailSnapshot = {
      ...detailSnapshot,
      commandsPage: page([], null) as RunCommandPage,
      resultManifest: null,
      run: {
        ...detailSnapshot.run,
        result_artifact_id: null,
        status: "running",
      },
    };
    const loadCommandsPage = vi.fn<RunDetailApi["loadCommandsPage"]>(async (_runId, cursor) => {
      const sent = JSON.parse(RecoverySocket.latest!.sent[0]) as {
        client_id: string;
        client_seq: number;
        command_id: string;
      };
      if (cursor === null) return page([], "commands.recovery+/=") as RunCommandPage;
      return page(
        [
          {
            applied_at: null,
            client_id: sent.client_id,
            client_seq: sent.client_seq,
            command_id: sent.command_id,
            created_at: "2026-07-19T12:00:00Z",
            payload_schema_id: "run-cancel@1",
            rejection_code: null,
            result_event_seq: null,
            revision: 1,
            run_id: "run:1",
            status: "pending",
            type: "cancel",
          },
        ],
        null,
      ) as RunCommandPage;
    });
    const { api, stream } = createApi({
      load: vi.fn(async () => runningSnapshot),
      loadCommandsPage,
    });
    const user = userEvent.setup();
    const queryClient = createQueryClient();
    render(
      <QueryClientProvider client={queryClient}>
        <RunDetailPage api={api} runId="run:1" />
      </QueryClientProvider>,
    );
    await screen.findByRole("heading", { name: "运行 run:1" });

    await user.click(screen.getByRole("button", { name: "取消运行" }));
    expect(RecoverySocket.latest?.protocols).toEqual([RUN_COMMAND_SUBPROTOCOL, "gameforge.csrf.csrf-route"]);
    RecoverySocket.latest!.open();
    RecoverySocket.latest!.disconnect();

    expect(await screen.findByText("取消命令已持久化")).toBeVisible();
    expect(loadCommandsPage).toHaveBeenNthCalledWith(1, "run:1", null);
    expect(loadCommandsPage).toHaveBeenNthCalledWith(2, "run:1", "commands.recovery+/=");
    expect(stream().start).toHaveBeenCalledTimes(2);
  });
});
