import type { components } from "./generated/openapi";
import { clearCsrfToken, type SessionStorage } from "./csrf";
import type { RunCommandV1 } from "./generated/ws-client-command-v1";
import type { RunCommandAckV1, RunCommandProblemV1 } from "./generated/ws-server-frame-v1";
import { ApiProblemError, sanitizeProblem, type SafeProblem } from "./problem";

export const RUN_COMMAND_SUBPROTOCOL = "gameforge.run-commands.v1";
const CSRF_SUBPROTOCOL_PREFIX = "gameforge.csrf.";
const CLIENT_ID_KEY = "gameforge.run-commands.client-id";
const CLIENT_SEQ_KEY = "gameforge.run-commands.client-seq";
const DEFAULT_ACK_TIMEOUT_MS = 10_000;

export const provideInputAvailability = {
  enabled: false,
  reason: "等待服务端提供权威交互请求后才能提交输入。",
} as const;

export type RunCommandView = components["schemas"]["RunCommandViewV1"];

export interface RunCommandIntent {
  readonly runId: string;
  readonly command: RunCommandV1;
}

export type RunCommandReceipt =
  | { source: "ack"; ack: RunCommandAckV1 }
  | { source: "recovery"; command: RunCommandView };

export interface RunCommandSocket {
  protocol: string;
  onopen: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent) => void) | null;
  onerror: ((event: Event) => void) | null;
  onclose: ((event: CloseEvent) => void) | null;
  send(data: string): void;
  close(): void;
}

export interface RunCommandClientOptions {
  csrfToken: () => string | null;
  storage?: SessionStorage;
  onSessionBoundary?: () => void;
  openWebSocket?: (url: string, protocols: string[]) => RunCommandSocket;
  loadCommands?: (runId: string) => Promise<readonly RunCommandView[]>;
  resumeEvents?: (runId: string) => void | Promise<void>;
  ackTimeoutMs?: number;
}

export interface CreateCancelIntentOptions {
  runId: string;
  expectedRunRevision: number;
  reasonCode: string;
  comment?: string | null;
}

function createSocket(url: string, protocols: string[]): RunCommandSocket {
  return new WebSocket(url, protocols);
}

function websocketUrl(runId: string): string {
  const url = new URL(`/api/v1/runs/${encodeURIComponent(runId)}/commands`, location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

function isAck(value: unknown): value is RunCommandAckV1 {
  return (
    typeof value === "object" &&
    value !== null &&
    "ack_schema_version" in value &&
    value.ack_schema_version === "run-command-ack@1" &&
    "command_id" in value &&
    typeof value.command_id === "string" &&
    "client_id" in value &&
    typeof value.client_id === "string" &&
    "client_seq" in value &&
    Number.isInteger(value.client_seq) &&
    Number(value.client_seq) >= 1 &&
    "status" in value &&
    (value.status === "accepted" || value.status === "duplicate") &&
    "persisted_status" in value &&
    ["pending", "claimed", "applied", "rejected"].includes(String(value.persisted_status)) &&
    "command_revision" in value &&
    Number.isInteger(value.command_revision) &&
    Number(value.command_revision) >= 1 &&
    "run_revision" in value &&
    Number.isInteger(value.run_revision) &&
    Number(value.run_revision) >= 1
  );
}

function isProblem(value: unknown): value is RunCommandProblemV1 {
  return (
    typeof value === "object" &&
    value !== null &&
    "problem_schema_version" in value &&
    value.problem_schema_version === "run-command-problem@1" &&
    "problem" in value &&
    typeof value.problem === "object" &&
    value.problem !== null &&
    "detail" in value.problem &&
    typeof value.problem.detail === "string" &&
    "code" in value.problem &&
    typeof value.problem.code === "string" &&
    "status" in value.problem &&
    Number.isInteger(value.problem.status)
  );
}

function invalidAuthMaterial(problem: SafeProblem): boolean {
  return (
    problem.status === 401 ||
    problem.code === "auth_required" ||
    problem.code === "auth_failed" ||
    problem.code === "csrf_failed"
  );
}

export class RunCommandClient {
  readonly #ackTimeoutMs: number;
  readonly #csrfToken: RunCommandClientOptions["csrfToken"];
  readonly #loadCommands?: RunCommandClientOptions["loadCommands"];
  readonly #onSessionBoundary?: RunCommandClientOptions["onSessionBoundary"];
  readonly #openWebSocket: NonNullable<RunCommandClientOptions["openWebSocket"]>;
  readonly #resumeEvents?: RunCommandClientOptions["resumeEvents"];
  readonly #storage: SessionStorage;

  constructor(options: RunCommandClientOptions) {
    this.#ackTimeoutMs = options.ackTimeoutMs ?? DEFAULT_ACK_TIMEOUT_MS;
    this.#csrfToken = options.csrfToken;
    this.#loadCommands = options.loadCommands;
    this.#onSessionBoundary = options.onSessionBoundary;
    this.#openWebSocket = options.openWebSocket ?? createSocket;
    this.#resumeEvents = options.resumeEvents;
    this.#storage = options.storage ?? sessionStorage;
  }

  createCancelIntent(options: CreateCancelIntentOptions): RunCommandIntent {
    const command: RunCommandV1 = {
      client_id: this.#clientId(),
      client_seq: this.#nextClientSeq(),
      command_id: `command:${crypto.randomUUID()}`,
      command_schema_version: "run-command@1",
      expected_run_revision: options.expectedRunRevision,
      idempotency_key: crypto.randomUUID(),
      payload: {
        comment: options.comment ?? null,
        reason_code: options.reasonCode,
        schema_version: "run-cancel@1",
      },
      payload_schema_id: "run-cancel@1",
      type: "cancel",
    };
    return { command, runId: options.runId };
  }

  submit(intent: RunCommandIntent): Promise<RunCommandReceipt> {
    const csrfToken = this.#csrfToken();
    if (!csrfToken) {
      return Promise.reject(new Error("重新登录后才能提交运行命令。"));
    }

    return new Promise<RunCommandReceipt>((resolve, reject) => {
      let settled = false;
      let recovering = false;
      let ackTimeout: ReturnType<typeof setTimeout> | undefined;
      const socket = this.#openWebSocket(websocketUrl(intent.runId), [
        RUN_COMMAND_SUBPROTOCOL,
        `${CSRF_SUBPROTOCOL_PREFIX}${csrfToken}`,
      ]);
      const clearAckTimeout = (): void => {
        if (ackTimeout !== undefined) clearTimeout(ackTimeout);
      };
      const succeed = (receipt: RunCommandReceipt): void => {
        if (settled) return;
        settled = true;
        clearAckTimeout();
        socket.close();
        resolve(receipt);
      };
      const fail = (error: Error): void => {
        if (settled) return;
        settled = true;
        clearAckTimeout();
        socket.close();
        reject(error);
      };
      const recover = (): void => {
        if (settled || recovering) return;
        recovering = true;
        clearAckTimeout();
        void this.#recover(intent).then(succeed, fail);
      };

      socket.onopen = () => {
        if (socket.protocol !== RUN_COMMAND_SUBPROTOCOL) {
          fail(new Error("运行命令通道未协商受支持的子协议。"));
          return;
        }
        socket.send(JSON.stringify(intent.command));
      };
      socket.onmessage = (message) => {
        let frame: unknown;
        try {
          frame = JSON.parse(String(message.data));
        } catch {
          fail(new Error("运行命令响应不是有效 JSON。"));
          return;
        }
        if (isProblem(frame)) {
          const problem = sanitizeProblem(frame.problem);
          if (invalidAuthMaterial(problem)) {
            clearCsrfToken(this.#storage);
            this.#onSessionBoundary?.();
          }
          fail(new ApiProblemError(problem));
          return;
        }
        if (
          !isAck(frame) ||
          frame.command_id !== intent.command.command_id ||
          frame.client_id !== intent.command.client_id ||
          frame.client_seq !== intent.command.client_seq
        ) {
          fail(new Error("运行命令 ACK 与提交的 intent 不匹配。"));
          return;
        }
        succeed({ ack: frame, source: "ack" });
      };
      socket.onerror = recover;
      socket.onclose = recover;
      ackTimeout = setTimeout(recover, this.#ackTimeoutMs);
    });
  }

  async #recover(intent: RunCommandIntent): Promise<RunCommandReceipt> {
    if (!this.#loadCommands) throw new Error("运行命令连接中断，无法读取持久状态。");
    try {
      const resumed = this.#resumeEvents?.(intent.runId);
      if (resumed) {
        void resumed.catch((error: unknown) => {
          console.error("Run event stream recovery failed.", error);
        });
      }
    } catch (error) {
      console.error("Run event stream recovery failed.", error);
    }
    const commands = await this.#loadCommands(intent.runId);
    const command = commands.find(
      (candidate) =>
        candidate.command_id === intent.command.command_id &&
        candidate.client_id === intent.command.client_id &&
        candidate.client_seq === intent.command.client_seq,
    );
    if (!command) throw new Error("运行命令连接中断，持久命令尚不可见。");
    return { command, source: "recovery" };
  }

  #clientId(): string {
    const existing = this.#storage.getItem(CLIENT_ID_KEY);
    if (existing) return existing;
    const created = `client:${crypto.randomUUID()}`;
    this.#storage.setItem(CLIENT_ID_KEY, created);
    return created;
  }

  #nextClientSeq(): number {
    const previous = this.#storage.getItem(CLIENT_SEQ_KEY);
    const next = previous === null ? 1n : BigInt(previous) + 1n;
    if (next > BigInt(Number.MAX_SAFE_INTEGER)) {
      throw new Error("浏览器会话的运行命令序号已耗尽。请重新打开控制台。 ");
    }
    this.#storage.setItem(CLIENT_SEQ_KEY, next.toString());
    return Number(next);
  }
}
