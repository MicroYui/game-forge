import { beforeEach, describe, expect, it, vi } from "vitest";

import { readCsrfToken, storeCsrfToken } from "./csrf";
import { ApiProblemError } from "./problem";
import { RunEventStream, runEventCursorKey } from "./sse";

const encoder = new TextEncoder();

function event(id: string, type: "run.queued" | "attempt.progress" | "run.cancelled"): string {
  const body =
    type === "run.queued"
      ? {
          data_schema_version: "run-queued@1",
          overall_deadline_utc: "2026-07-20T00:00:00Z",
          queue_deadline_utc: "2026-07-19T23:00:00Z",
          run_kind: { kind: "review", version: 1 },
        }
      : type === "attempt.progress"
        ? {
            attempt_no: 1,
            completed_units: 1,
            data_schema_version: "attempt-progress@1",
            phase_code: "checking",
            total_units: 2,
          }
        : {
            attempt_no: null,
            cause_code: "cancelled_by_user",
            data_schema_version: "run-terminated@1",
            failure_artifact_id: "artifact:failure",
          };
  const attempt = type === "attempt.progress" ? { attempt_no: 1 } : {};
  return `id:${id}\nevent:${type}\ndata:${JSON.stringify({
    ...attempt,
    data: body,
    data_schema_version: body.data_schema_version,
    event_schema_version: "run-event@1",
    event_type: type,
    occurred_at: "2026-07-19T12:00:00Z",
    run_id: "run:1",
    seq: Number(id),
    trace_id: null,
  })}\n\n`;
}

function response(chunks: string[], init: ResponseInit = {}): Response {
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
    { status: 200, headers: { "content-type": "text/event-stream" }, ...init },
  );
}

describe("RunEventStream", () => {
  beforeEach(() => sessionStorage.clear());

  it("resumes with the persisted frame id and deduplicates backlog/reconnect frames", async () => {
    sessionStorage.setItem(runEventCursorKey("run:1"), "9007199254740993");
    const received: string[] = [];
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        response([
          event("9007199254740993", "attempt.progress"),
          event("9007199254740994", "attempt.progress"),
        ]),
      )
      .mockResolvedValueOnce(
        response([
          event("9007199254740994", "attempt.progress"),
          event("9007199254740995", "attempt.progress"),
        ]),
      );
    const stream = new RunEventStream({
      fetch: fetcher,
      onEvent: (_value, cursor) => received.push(cursor),
      runId: "run:1",
    });

    await stream.start();
    await stream.start();

    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      "/api/v1/runs/run%3A1/events",
      expect.objectContaining({
        credentials: "include",
        headers: expect.objectContaining({ "Last-Event-ID": "9007199254740993" }),
      }),
    );
    expect(received).toEqual(["9007199254740994", "9007199254740995"]);
    expect(sessionStorage.getItem(runEventCursorKey("run:1"))).toBe("9007199254740995");
  });

  it("closes on a terminal event and does not reconnect implicitly", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(response([event("1", "run.queued"), event("2", "run.cancelled")]));
    const stream = new RunEventStream({ fetch: fetcher, runId: "run:1" });

    await stream.start();
    await stream.start();

    expect(stream.state.status).toBe("terminal");
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("retains the stale cursor and earliest cursor until an explicit restart", async () => {
    sessionStorage.setItem(runEventCursorKey("run:1"), "4");
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            code: "cursor_expired",
            detail: "expired",
            earliest_cursor: "8",
            instance: "/api/v1/runs/run:1/events",
            request_id: "request:1",
            status: 410,
            title: "Gone",
            type: "about:blank",
            errors: [{ secret: "must-not-render" }],
          }),
          { status: 410, headers: { "content-type": "application/problem+json" } },
        ),
      )
      .mockResolvedValueOnce(response([event("8", "run.queued")]));
    const stream = new RunEventStream({ fetch: fetcher, runId: "run:1" });

    await stream.start();

    expect(stream.state).toMatchObject({
      earliestCursor: "8",
      problem: { code: "cursor_expired", request_id: "request:1" },
      status: "expired",
    });
    expect(JSON.stringify(stream.state.problem)).not.toContain("must-not-render");
    expect(sessionStorage.getItem(runEventCursorKey("run:1"))).toBe("4");

    await stream.start();
    expect(fetcher).toHaveBeenCalledTimes(1);

    await stream.restart();

    expect(fetcher).toHaveBeenNthCalledWith(
      2,
      "/api/v1/runs/run%3A1/events",
      expect.objectContaining({ headers: { Accept: "text/event-stream" } }),
    );
    expect(sessionStorage.getItem(runEventCursorKey("run:1"))).toBe("8");
  });

  it("does not reopen when route cleanup closes a restart waiting on the active stream", async () => {
    let resolveFirst!: (value: Response) => void;
    const fetcher = vi.fn<typeof fetch>().mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          resolveFirst = resolve;
        }),
    );
    const stream = new RunEventStream({ fetch: fetcher, runId: "run:1" });
    const first = stream.start();
    const restarting = stream.restart();

    stream.close();
    resolveFirst(response([]));
    await Promise.all([first, restarting]);

    expect(stream.state.status).toBe("closed");
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("does not advance the durable cursor when event delivery fails", async () => {
    sessionStorage.setItem(runEventCursorKey("run:1"), "1");
    const stream = new RunEventStream({
      fetch: vi.fn<typeof fetch>().mockResolvedValue(response([event("2", "attempt.progress")])),
      onEvent: () => {
        throw new Error("reducer failed");
      },
      runId: "run:1",
    });

    await expect(stream.start()).rejects.toThrow("reducer failed");
    expect(sessionStorage.getItem(runEventCursorKey("run:1"))).toBe("1");
  });

  it("rejects an event whose data discriminator does not match its event type", async () => {
    const mismatched = event("1", "attempt.progress").replace(
      '"data_schema_version":"attempt-progress@1"',
      '"data_schema_version":"run-queued@1"',
    );
    const stream = new RunEventStream({
      fetch: vi.fn<typeof fetch>().mockResolvedValue(response([mismatched])),
      runId: "run:1",
    });

    await expect(stream.start()).rejects.toThrow("does not match the requested stream");
    expect(sessionStorage.getItem(runEventCursorKey("run:1"))).toBeNull();
  });

  it("keeps a safe Problem and clears CSRF on an SSE 401", async () => {
    storeCsrfToken("csrf-secret");
    const onSessionBoundary = vi.fn();
    const stream = new RunEventStream({
      fetch: vi.fn<typeof fetch>().mockResolvedValue(
        new Response(
          JSON.stringify({
            code: "auth_failed",
            detail: "session expired",
            errors: [{ token: "must-not-render" }],
            instance: "/api/v1/runs/run:1/events",
            request_id: "request:auth",
            status: 401,
            title: "Authentication failed",
            type: "about:blank",
          }),
          { status: 401, headers: { "content-type": "application/problem+json" } },
        ),
      ),
      onSessionBoundary,
      runId: "run:1",
    });

    const failure = stream.start().catch((error: unknown) => error);

    await expect(failure).resolves.toBeInstanceOf(ApiProblemError);
    const error = (await failure) as ApiProblemError;
    expect(error.problem).toMatchObject({ code: "auth_failed", request_id: "request:auth" });
    expect(JSON.stringify(error.problem)).not.toContain("must-not-render");
    expect(readCsrfToken()).toBeNull();
    expect(onSessionBoundary).toHaveBeenCalledOnce();
  });
});
