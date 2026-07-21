import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  RUN_COMMAND_SUBPROTOCOL,
  RunCommandClient,
  provideInputAvailability,
  type RunCommandSocket,
} from "./commands";
import { readCsrfToken, storeCsrfToken } from "./csrf";
import { ApiProblemError } from "./problem";

class FakeSocket implements RunCommandSocket {
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onopen: ((event: Event) => void) | null = null;
  protocol = RUN_COMMAND_SUBPROTOCOL;
  sent: string[] = [];

  close(): void {}

  send(data: string): void {
    this.sent.push(data);
  }

  open(): void {
    this.onopen?.(new Event("open"));
  }

  message(value: unknown): void {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(value) }));
  }

  disconnect(): void {
    this.onclose?.(new CloseEvent("close"));
  }
}

describe("RunCommandClient", () => {
  beforeEach(() => sessionStorage.clear());
  afterEach(() => vi.useRealTimers());

  it("keeps a session client id, increments client_seq, and reuses one intent on retry", async () => {
    const sockets: FakeSocket[] = [];
    const protocols: string[][] = [];
    const openWebSocket = vi.fn((_url: string, offered: string[]) => {
      protocols.push(offered);
      const socket = new FakeSocket();
      sockets.push(socket);
      return socket;
    });
    const firstClient = new RunCommandClient({
      csrfToken: () => "csrf-value",
      openWebSocket,
    });
    const intent = firstClient.createCancelIntent({
      expectedRunRevision: 3,
      reasonCode: "operator_cancelled",
      runId: "run:1",
    });
    const nextIntent = new RunCommandClient({
      csrfToken: () => "csrf-value",
      openWebSocket,
    }).createCancelIntent({
      expectedRunRevision: 4,
      reasonCode: "operator_cancelled",
      runId: "run:2",
    });

    expect(nextIntent.command.client_id).toBe(intent.command.client_id);
    expect(nextIntent.command.client_seq).toBe(intent.command.client_seq + 1);
    expect(intent.command.command_id).not.toBe(intent.command.idempotency_key);

    const first = firstClient.submit(intent);
    sockets[0].open();
    const firstFrame = JSON.parse(sockets[0].sent[0]);
    sockets[0].message({
      ack_schema_version: "run-command-ack@1",
      client_id: intent.command.client_id,
      client_seq: intent.command.client_seq,
      command_id: intent.command.command_id,
      command_revision: 1,
      persisted_status: "applied",
      run_revision: 4,
      status: "accepted",
    });
    await expect(first).resolves.toMatchObject({ source: "ack" });

    const retry = firstClient.submit(intent);
    sockets[1].open();
    expect(JSON.parse(sockets[1].sent[0])).toEqual(firstFrame);
    sockets[1].message({
      ack_schema_version: "run-command-ack@1",
      client_id: intent.command.client_id,
      client_seq: intent.command.client_seq,
      command_id: intent.command.command_id,
      command_revision: 1,
      persisted_status: "applied",
      run_revision: 4,
      status: "duplicate",
    });
    await expect(retry).resolves.toMatchObject({ ack: { status: "duplicate" }, source: "ack" });
    expect(protocols).toEqual([
      [RUN_COMMAND_SUBPROTOCOL, "gameforge.csrf.csrf-value"],
      [RUN_COMMAND_SUBPROTOCOL, "gameforge.csrf.csrf-value"],
    ]);
  });

  it("recovers a disconnected intent from persisted commands and resumes SSE", async () => {
    const socket = new FakeSocket();
    const loadCommands = vi.fn(async () => [
      {
        applied_at: "2026-07-19T12:00:00Z",
        client_id: "client:other",
        client_seq: 1,
        command_id: "other",
        created_at: "2026-07-19T12:00:00Z",
        payload_schema_id: "run-cancel@1" as const,
        rejection_code: null,
        result_event_seq: 3,
        revision: 1,
        run_id: "run:1",
        status: "applied" as const,
        type: "cancel" as const,
      },
    ]);
    const resumeEvents = vi.fn(() => new Promise<void>(() => undefined));
    const client = new RunCommandClient({
      csrfToken: () => "csrf-value",
      loadCommands,
      openWebSocket: () => socket,
      resumeEvents,
    });
    const intent = client.createCancelIntent({
      expectedRunRevision: 3,
      reasonCode: "operator_cancelled",
      runId: "run:1",
    });
    loadCommands.mockResolvedValueOnce([
      {
        ...(await loadCommands())[0],
        client_id: intent.command.client_id,
        client_seq: intent.command.client_seq,
        command_id: intent.command.command_id,
      },
    ]);

    const result = client.submit(intent);
    socket.open();
    socket.disconnect();

    await expect(result).resolves.toMatchObject({
      command: { command_id: intent.command.command_id, status: "applied" },
      source: "recovery",
    });
    expect(resumeEvents).toHaveBeenCalledWith("run:1");
  });

  it("keeps provide_input honestly unavailable without authoritative interaction state", () => {
    expect(provideInputAvailability).toEqual({
      enabled: false,
      reason: "等待服务端提供权威交互请求后才能提交输入。",
    });
  });

  it("rejects a matching but incomplete ACK discriminator frame", async () => {
    const socket = new FakeSocket();
    const client = new RunCommandClient({
      csrfToken: () => "csrf-value",
      openWebSocket: () => socket,
    });
    const intent = client.createCancelIntent({
      expectedRunRevision: 3,
      reasonCode: "operator_cancelled",
      runId: "run:1",
    });

    const result = client.submit(intent);
    socket.open();
    socket.message({
      ack_schema_version: "run-command-ack@1",
      client_id: intent.command.client_id,
      client_seq: intent.command.client_seq,
      command_id: intent.command.command_id,
    });

    await expect(result).rejects.toThrow("ACK 与提交的 intent 不匹配");
  });

  it("sanitizes WS Problems and clears only invalid authentication material", async () => {
    storeCsrfToken("csrf-secret");
    const socket = new FakeSocket();
    const onSessionBoundary = vi.fn();
    const client = new RunCommandClient({
      csrfToken: () => readCsrfToken(),
      onSessionBoundary,
      openWebSocket: () => socket,
    });
    const intent = client.createCancelIntent({
      expectedRunRevision: 3,
      reasonCode: "operator_cancelled",
      runId: "run:1",
    });
    const result = client.submit(intent).catch((error: unknown) => error);
    socket.open();
    socket.message({
      client_seq: intent.command.client_seq,
      command_id: intent.command.command_id,
      problem: {
        code: "csrf_failed",
        detail: "csrf expired",
        errors: [{ token: "must-not-render" }],
        instance: "/api/v1/runs/run:1/commands",
        request_id: "request:csrf",
        status: 403,
        title: "CSRF failed",
        type: "about:blank",
      },
      problem_schema_version: "run-command-problem@1",
    });

    await expect(result).resolves.toBeInstanceOf(ApiProblemError);
    const error = (await result) as ApiProblemError;
    expect(error.problem.code).toBe("csrf_failed");
    expect(JSON.stringify(error.problem)).not.toContain("must-not-render");
    expect(readCsrfToken()).toBeNull();
    expect(onSessionBoundary).toHaveBeenCalledOnce();

    storeCsrfToken("still-valid");
    const rbacSocket = new FakeSocket();
    const rbacClient = new RunCommandClient({
      csrfToken: () => readCsrfToken(),
      onSessionBoundary,
      openWebSocket: () => rbacSocket,
    });
    const rbacIntent = rbacClient.createCancelIntent({
      expectedRunRevision: 3,
      reasonCode: "operator_cancelled",
      runId: "run:2",
    });
    const forbidden = rbacClient.submit(rbacIntent).catch((error: unknown) => error);
    rbacSocket.open();
    rbacSocket.message({
      client_seq: rbacIntent.command.client_seq,
      command_id: rbacIntent.command.command_id,
      problem: {
        code: "forbidden",
        detail: "not allowed",
        instance: "/api/v1/runs/run:2/commands",
        request_id: "request:forbidden",
        status: 403,
        title: "Forbidden",
        type: "about:blank",
      },
      problem_schema_version: "run-command-problem@1",
    });
    await forbidden;
    expect(readCsrfToken()).toBe("still-valid");
    expect(onSessionBoundary).toHaveBeenCalledOnce();
  });

  it("bounds ACK wait and recovers through persisted commands plus SSE", async () => {
    vi.useFakeTimers();
    const socket = new FakeSocket();
    let intent!: ReturnType<RunCommandClient["createCancelIntent"]>;
    const loadCommands = vi.fn(async () => [
      {
        applied_at: null,
        client_id: intent.command.client_id,
        client_seq: intent.command.client_seq,
        command_id: intent.command.command_id,
        created_at: "2026-07-19T12:00:00Z",
        payload_schema_id: "run-cancel@1" as const,
        rejection_code: null,
        result_event_seq: null,
        revision: 1,
        run_id: intent.runId,
        status: "pending" as const,
        type: "cancel" as const,
      },
    ]);
    const resumeEvents = vi.fn(async () => undefined);
    const client = new RunCommandClient({
      ackTimeoutMs: 25,
      csrfToken: () => "csrf-value",
      loadCommands,
      openWebSocket: () => socket,
      resumeEvents,
    });
    intent = client.createCancelIntent({
      expectedRunRevision: 3,
      reasonCode: "operator_cancelled",
      runId: "run:1",
    });

    const result = client.submit(intent);
    socket.open();
    await vi.advanceTimersByTimeAsync(25);

    await expect(result).resolves.toMatchObject({
      command: { command_id: intent.command.command_id },
      source: "recovery",
    });
    expect(loadCommands).toHaveBeenCalledTimes(1);
    expect(resumeEvents).toHaveBeenCalledWith("run:1");
  });

  it("clears the ACK timeout after a persisted server frame", async () => {
    vi.useFakeTimers();
    const socket = new FakeSocket();
    const loadCommands = vi.fn(async () => []);
    const resumeEvents = vi.fn(async () => undefined);
    const client = new RunCommandClient({
      ackTimeoutMs: 25,
      csrfToken: () => "csrf-value",
      loadCommands,
      openWebSocket: () => socket,
      resumeEvents,
    });
    const intent = client.createCancelIntent({
      expectedRunRevision: 3,
      reasonCode: "operator_cancelled",
      runId: "run:1",
    });

    const result = client.submit(intent);
    socket.open();
    socket.message({
      ack_schema_version: "run-command-ack@1",
      client_id: intent.command.client_id,
      client_seq: intent.command.client_seq,
      command_id: intent.command.command_id,
      command_revision: 1,
      persisted_status: "applied",
      run_revision: 4,
      status: "accepted",
    });
    await result;
    await vi.advanceTimersByTimeAsync(25);

    expect(loadCommands).not.toHaveBeenCalled();
    expect(resumeEvents).not.toHaveBeenCalled();
  });
});
