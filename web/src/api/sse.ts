import { createParser, type EventSourceMessage, type ParseError } from "eventsource-parser";

import { clearCsrfToken, type SessionStorage } from "./csrf";
import type { RunEvent } from "./generated/sse-run-event-v1";
import { ApiProblemError, sanitizeProblem, type SafeProblem } from "./problem";

const terminalEventTypes = new Set<RunEvent["event_type"]>([
  "run.succeeded",
  "run.failed",
  "run.cancelled",
  "run.timed_out",
]);

const dataSchemaByEventType: Record<RunEvent["event_type"], string> = {
  "attempt.leased": "attempt-leased@1",
  "attempt.lease_expired": "lease-expired@1",
  "attempt.progress": "attempt-progress@1",
  "attempt.retry_scheduled": "retry-scheduled@1",
  "attempt.started": "attempt-started@1",
  "run.cancel_requested": "cancel-requested@1",
  "run.cancelled": "run-terminated@1",
  "run.command_accepted": "command-accepted@1",
  "run.command_applied": "command-outcome@1",
  "run.command_rejected": "command-outcome@1",
  "run.failed": "run-terminated@1",
  "run.queued": "run-queued@1",
  "run.succeeded": "run-succeeded@1",
  "run.timed_out": "run-terminated@1",
};

export type RunEventStreamStatus =
  | "idle"
  | "connecting"
  | "open"
  | "disconnected"
  | "expired"
  | "terminal"
  | "closed"
  | "error";

export interface RunEventStreamState {
  status: RunEventStreamStatus;
  earliestCursor?: string;
  error?: Error;
  problem?: SafeProblem;
}

export interface RunEventStreamOptions {
  runId: string;
  fetch?: typeof fetch;
  storage?: SessionStorage;
  onEvent?: (event: RunEvent, cursor: string) => void;
  onSessionBoundary?: () => void;
  onStateChange?: (state: RunEventStreamState) => void;
}

export function runEventCursorKey(runId: string): string {
  return `gameforge.run-events.last-event-id:${runId}`;
}

function isCursor(value: string | undefined): value is string {
  return value !== undefined && /^(0|[1-9]\d*)$/.test(value);
}

function compareCursors(left: string, right: string): number {
  if (left.length !== right.length) return left.length < right.length ? -1 : 1;
  return left === right ? 0 : left < right ? -1 : 1;
}

function parseEvent(message: EventSourceMessage, runId: string): RunEvent {
  if (!isCursor(message.id)) throw new Error("Run event is missing a canonical frame id.");
  const value: unknown = JSON.parse(message.data);
  const eventType = message.event as RunEvent["event_type"] | undefined;
  const expectedDataSchema = eventType && dataSchemaByEventType[eventType];
  if (
    typeof value !== "object" ||
    value === null ||
    !("event_schema_version" in value) ||
    value.event_schema_version !== "run-event@1" ||
    !("run_id" in value) ||
    value.run_id !== runId ||
    !("event_type" in value) ||
    value.event_type !== eventType ||
    expectedDataSchema === undefined ||
    !("data_schema_version" in value) ||
    value.data_schema_version !== expectedDataSchema ||
    !("data" in value) ||
    typeof value.data !== "object" ||
    value.data === null ||
    !("data_schema_version" in value.data) ||
    value.data.data_schema_version !== expectedDataSchema
  ) {
    throw new Error("Run event does not match the requested stream.");
  }
  return value as RunEvent;
}

async function safeResponseProblem(response: Response): Promise<SafeProblem | undefined> {
  try {
    return sanitizeProblem(await response.json());
  } catch {
    return undefined;
  }
}

export class RunEventStream {
  readonly #fetch: typeof fetch;
  readonly #onEvent?: RunEventStreamOptions["onEvent"];
  readonly #onSessionBoundary?: RunEventStreamOptions["onSessionBoundary"];
  readonly #onStateChange?: RunEventStreamOptions["onStateChange"];
  readonly #runId: string;
  readonly #storage: SessionStorage;
  #abortController: AbortController | undefined;
  #active: Promise<void> | undefined;
  #closed = false;
  #terminal = false;
  state: RunEventStreamState = { status: "idle" };

  constructor(options: RunEventStreamOptions) {
    this.#runId = options.runId;
    this.#fetch = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.#storage = options.storage ?? sessionStorage;
    this.#onEvent = options.onEvent;
    this.#onSessionBoundary = options.onSessionBoundary;
    this.#onStateChange = options.onStateChange;
  }

  start(): Promise<void> {
    if (this.#active) return this.#active;
    if (this.#closed || this.#terminal || this.state.status === "expired") {
      return Promise.resolve();
    }
    this.#active = this.#connect().finally(() => {
      this.#active = undefined;
      this.#abortController = undefined;
    });
    return this.#active;
  }

  async restart(): Promise<void> {
    if (this.#closed) return;
    this.#abortController?.abort();
    await this.#active?.catch(() => undefined);
    if (this.#closed) return;
    this.#storage.removeItem(runEventCursorKey(this.#runId));
    this.#terminal = false;
    this.#setState({ status: "idle" });
    await this.start();
  }

  close(): void {
    this.#closed = true;
    this.#abortController?.abort();
    this.#setState({ status: "closed" });
  }

  async #connect(): Promise<void> {
    this.#abortController = new AbortController();
    const cursorKey = runEventCursorKey(this.#runId);
    let lastCursor = this.#storage.getItem(cursorKey) ?? undefined;
    const headers: Record<string, string> = { Accept: "text/event-stream" };
    if (lastCursor !== undefined) headers["Last-Event-ID"] = lastCursor;
    this.#setState({ status: "connecting" });

    try {
      const response = await this.#fetch(`/api/v1/runs/${encodeURIComponent(this.#runId)}/events`, {
        credentials: "include",
        headers,
        signal: this.#abortController.signal,
      });
      if (this.#closed) return;
      if (!response.ok) {
        const problem = await safeResponseProblem(response);
        if (response.status === 401) {
          clearCsrfToken(this.#storage);
          this.#onSessionBoundary?.();
        }
        if (response.status !== 410) {
          throw problem
            ? new ApiProblemError(problem)
            : new Error(`Run event stream failed with HTTP ${response.status}.`);
        }
        this.#setState({
          earliestCursor: problem?.earliest_cursor ?? undefined,
          problem,
          status: "expired",
        });
        return;
      }
      if (response.body === null) {
        throw new Error(`Run event stream failed with HTTP ${response.status}.`);
      }
      this.#setState({ status: "open" });
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let parseError: ParseError | undefined;
      const parser = createParser({
        onError(error) {
          parseError = error;
        },
        onEvent: (message) => {
          const event = parseEvent(message, this.#runId);
          const cursor = message.id!;
          if (lastCursor !== undefined && compareCursors(cursor, lastCursor) <= 0) return;
          this.#onEvent?.(event, cursor);
          lastCursor = cursor;
          this.#storage.setItem(cursorKey, cursor);
          if (terminalEventTypes.has(event.event_type)) this.#terminal = true;
        },
      });

      while (!this.#closed && !this.#terminal) {
        const { done, value } = await reader.read();
        if (done) break;
        parser.feed(decoder.decode(value, { stream: true }));
        if (parseError) throw parseError;
      }
      if (this.#terminal) {
        await reader.cancel();
        this.#setState({ status: "terminal" });
      } else if (!this.#closed) {
        this.#setState({ status: "disconnected" });
      }
    } catch (error) {
      if (this.#closed || this.#abortController.signal.aborted) return;
      const normalized = error instanceof Error ? error : new Error("Run event stream failed.");
      this.#setState({ error: normalized, status: "error" });
      throw normalized;
    }
  }

  #setState(state: RunEventStreamState): void {
    this.state = state;
    this.#onStateChange?.(state);
  }
}
