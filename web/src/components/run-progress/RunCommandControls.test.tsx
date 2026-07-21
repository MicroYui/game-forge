import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { RunCommandIntent, RunCommandReceipt } from "../../api/commands";
import { ApiProblemError, sanitizeProblem } from "../../api/problem";
import { RunCommandControls } from "./RunCommandControls";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

const intent: RunCommandIntent = {
  command: {
    client_id: "client:1",
    client_seq: 1,
    command_id: "command:1",
    command_schema_version: "run-command@1",
    expected_run_revision: 3,
    idempotency_key: "intent:1",
    payload: {
      comment: null,
      reason_code: "operator_cancelled",
      schema_version: "run-cancel@1",
    },
    payload_schema_id: "run-cancel@1",
    type: "cancel",
  },
  runId: "run:1",
};

describe("RunCommandControls", () => {
  it("changes command state only after a persisted ACK", async () => {
    const pending = deferred<RunCommandReceipt>();
    const onPersisted = vi.fn();
    const client = {
      createCancelIntent: vi.fn(() => intent),
      submit: vi.fn(() => pending.promise),
    };
    const user = userEvent.setup();
    render(
      <RunCommandControls
        client={client}
        onPersisted={onPersisted}
        runId="run:1"
        runRevision={3}
        runStatus="running"
      />,
    );

    await user.click(screen.getByRole("button", { name: "取消运行" }));

    expect(screen.getByRole("status")).toHaveTextContent("正在等待持久确认");
    expect(onPersisted).not.toHaveBeenCalled();

    pending.resolve({
      ack: {
        ack_schema_version: "run-command-ack@1",
        client_id: "client:1",
        client_seq: 1,
        command_id: "command:1",
        command_revision: 1,
        persisted_status: "applied",
        run_revision: 4,
        status: "accepted",
      },
      source: "ack",
    });

    expect(await screen.findByText("取消命令已持久化")).toBeInTheDocument();
    expect(onPersisted).toHaveBeenCalledTimes(1);
  });

  it("stays persisted when the parent receipt callback throws", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const client = {
      createCancelIntent: vi.fn(() => intent),
      submit: vi.fn(async () => ({
        ack: {
          ack_schema_version: "run-command-ack@1" as const,
          client_id: "client:1",
          client_seq: 1,
          command_id: "command:1",
          command_revision: 1,
          persisted_status: "applied" as const,
          run_revision: 4,
          status: "accepted" as const,
        },
        source: "ack" as const,
      })),
    };
    const user = userEvent.setup();
    render(
      <RunCommandControls
        client={client}
        onPersisted={() => {
          throw new Error("parent refresh failed");
        }}
        runId="run:1"
        runRevision={3}
        runStatus="running"
      />,
    );

    await user.click(screen.getByRole("button", { name: "取消运行" }));

    expect(await screen.findByText("取消命令已持久化")).toBeVisible();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(consoleError).toHaveBeenCalledWith("Run command persisted callback failed.", expect.any(Error));
    consoleError.mockRestore();
  });

  it("retries a failed submission with the same frozen intent", async () => {
    const client = {
      createCancelIntent: vi.fn(() => intent),
      submit: vi
        .fn<() => Promise<RunCommandReceipt>>()
        .mockRejectedValueOnce(new Error("offline"))
        .mockResolvedValueOnce({
          command: {
            applied_at: null,
            client_id: "client:1",
            client_seq: 1,
            command_id: "command:1",
            created_at: "2026-07-19T12:00:00Z",
            payload_schema_id: "run-cancel@1",
            rejection_code: null,
            result_event_seq: null,
            revision: 1,
            run_id: "run:1",
            status: "pending",
            type: "cancel",
          },
          source: "recovery",
        }),
    };
    const user = userEvent.setup();
    const { rerender } = render(
      <RunCommandControls client={client} runId="run:1" runRevision={3} runStatus="running" />,
    );

    await user.click(screen.getByRole("button", { name: "取消运行" }));
    rerender(<RunCommandControls client={client} runId="run:1" runRevision={4} runStatus="running" />);
    await user.click(await screen.findByRole("button", { name: "重试取消命令" }));

    expect(client.createCancelIntent).toHaveBeenCalledTimes(1);
    expect(client.submit).toHaveBeenNthCalledWith(1, intent);
    expect(client.submit).toHaveBeenNthCalledWith(2, intent);
  });

  it("drops a rejected intent until refreshed run authority permits a new command", async () => {
    const refresh = deferred<void>();
    const onProblem = vi.fn<(problem: ReturnType<typeof sanitizeProblem>) => Promise<void>>(
      () => refresh.promise,
    );
    const created: RunCommandIntent[] = [];
    const client = {
      createCancelIntent: vi.fn((options: { expectedRunRevision: number; runId: string }) => {
        const next: RunCommandIntent = {
          command: {
            ...intent.command,
            client_seq: created.length + 1,
            command_id: `command:${created.length + 1}`,
            expected_run_revision: options.expectedRunRevision,
            idempotency_key: `intent:${created.length + 1}`,
          },
          runId: options.runId,
        };
        created.push(next);
        return next;
      }),
      submit: vi
        .fn<(intent: RunCommandIntent) => Promise<RunCommandReceipt>>()
        .mockRejectedValueOnce(
          new ApiProblemError(
            sanitizeProblem({
              code: "revision_conflict",
              conflict_set_id: "conflict:1",
              detail: "运行修订已变化",
              errors: [{ secret: "must-not-render" }],
              instance: "/api/v1/runs/run:1/commands",
              request_id: "request:conflict",
              status: 409,
              title: "Conflict",
              type: "about:blank",
            }),
          ),
        )
        .mockResolvedValueOnce({
          command: {
            applied_at: null,
            client_id: "client:1",
            client_seq: 2,
            command_id: "command:2",
            created_at: "2026-07-19T12:00:00Z",
            payload_schema_id: "run-cancel@1",
            rejection_code: null,
            result_event_seq: null,
            revision: 1,
            run_id: "run:1",
            status: "pending",
            type: "cancel",
          },
          source: "recovery",
        }),
    };
    const user = userEvent.setup();
    const { rerender } = render(
      <RunCommandControls
        client={client}
        onProblem={onProblem}
        runId="run:1"
        runRevision={3}
        runStatus="running"
      />,
    );

    await user.click(screen.getByRole("button", { name: "取消运行" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("运行修订已变化");
    expect(screen.getByRole("button", { name: "等待刷新运行状态" })).toBeDisabled();
    expect(onProblem).toHaveBeenCalledWith(
      expect.objectContaining({ code: "revision_conflict", request_id: "request:conflict" }),
    );
    expect(JSON.stringify(onProblem.mock.calls[0]?.[0])).not.toContain("must-not-render");

    refresh.resolve(undefined);
    await Promise.resolve();
    expect(screen.getByRole("button", { name: "等待刷新运行状态" })).toBeDisabled();

    rerender(
      <RunCommandControls
        client={client}
        onProblem={onProblem}
        runId="run:1"
        runRevision={4}
        runStatus="running"
      />,
    );
    await user.click(await screen.findByRole("button", { name: "取消运行" }));

    expect(client.createCancelIntent).toHaveBeenCalledTimes(2);
    expect(created[0]?.command.expected_run_revision).toBe(3);
    expect(created[1]?.command.expected_run_revision).toBe(4);
    expect(created[1]?.command.command_id).not.toBe(created[0]?.command.command_id);
    expect(created[1]?.command.idempotency_key).not.toBe(created[0]?.command.idempotency_key);
  });

  it("drops command state when navigating to a different run", async () => {
    const client = {
      createCancelIntent: vi.fn((options: { runId: string }) => ({
        ...intent,
        runId: options.runId,
      })),
      submit: vi.fn<() => Promise<RunCommandReceipt>>().mockRejectedValue(new Error("offline")),
    };
    const user = userEvent.setup();
    const { rerender } = render(
      <RunCommandControls client={client} runId="run:1" runRevision={3} runStatus="running" />,
    );
    await user.click(screen.getByRole("button", { name: "取消运行" }));
    expect(await screen.findByRole("button", { name: "重试取消命令" })).toBeVisible();

    rerender(<RunCommandControls client={client} runId="run:2" runRevision={1} runStatus="queued" />);

    await user.click(await screen.findByRole("button", { name: "取消运行" }));
    expect(client.createCancelIntent).toHaveBeenLastCalledWith(expect.objectContaining({ runId: "run:2" }));
  });

  it("ignores a late ACK from the run that was navigated away from", async () => {
    const pending = deferred<RunCommandReceipt>();
    const onPersisted = vi.fn();
    const client = {
      createCancelIntent: vi.fn(() => intent),
      submit: vi.fn(() => pending.promise),
    };
    const user = userEvent.setup();
    const { rerender } = render(
      <RunCommandControls
        client={client}
        onPersisted={onPersisted}
        runId="run:1"
        runRevision={3}
        runStatus="running"
      />,
    );
    await user.click(screen.getByRole("button", { name: "取消运行" }));

    rerender(
      <RunCommandControls
        client={client}
        onPersisted={onPersisted}
        runId="run:2"
        runRevision={1}
        runStatus="queued"
      />,
    );
    pending.resolve({
      ack: {
        ack_schema_version: "run-command-ack@1",
        client_id: "client:1",
        client_seq: 1,
        command_id: "command:1",
        command_revision: 1,
        persisted_status: "applied",
        run_revision: 4,
        status: "accepted",
      },
      source: "ack",
    });

    await Promise.resolve();
    expect(onPersisted).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "取消运行" })).toBeEnabled();
  });

  it("explains why provide_input is disabled", () => {
    render(
      <RunCommandControls
        client={{ createCancelIntent: vi.fn(), submit: vi.fn() }}
        runId="run:1"
        runRevision={3}
        runStatus="running"
      />,
    );

    expect(screen.getByRole("button", { name: "提供输入" })).toBeDisabled();
    expect(screen.getByText("等待服务端提供权威交互请求后才能提交输入。")).toBeVisible();
  });
});
