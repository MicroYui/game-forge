import { useQuery } from "@tanstack/react-query";
import { type Dispatch, type SetStateAction, useEffect, useMemo, useRef, useState } from "react";

import type { RunCommandClient } from "../../api/commands";
import type { RunEvent } from "../../api/generated/sse-run-event-v1";
import { CursorExpiredError, cursorFromPage } from "../../api/pagination";
import { createBrowserRunCommandClient } from "../../api/runtime";
import type { RunEventStreamState } from "../../api/sse";
import { RunCommandControls, RunProgress, type RunEventItem } from "../../components/run-progress";
import {
  runDetailApi,
  type ArtifactPayloadView,
  type FindingRevision,
  type RunCommandView,
  type RunDetailApi,
  type RunDetailSnapshot,
  type RunEventStreamHandle,
  type TraceSummary,
} from "./api";

export interface RunDetailPageProps {
  runId: string;
  commandClient?: RunCommandClient;
  api?: RunDetailApi;
}

type PageLike<T> = {
  items: T[];
  next_cursor?: string | null;
};

interface CollectionState<T> {
  generation: number;
  items: T[];
  nextCursor: string | null;
  ownerRunId: string;
  loading: boolean;
  error?: Error;
}

interface OwnedEventState {
  items: RunEventItem[];
  ownerRunId: string;
}

interface OwnedStreamState {
  ownerRunId: string;
  state: RunEventStreamState;
}

interface OwnedStream {
  ownerRunId: string;
  stream: RunEventStreamHandle;
}

const terminalEvents = new Set<RunEvent["event_type"]>([
  "run.succeeded",
  "run.failed",
  "run.cancelled",
  "run.timed_out",
]);

function artifactHref(artifactId: string): string {
  return `/artifacts/${encodeURIComponent(artifactId)}`;
}

function traceHref(traceId: string): string {
  return `/observability/traces/${encodeURIComponent(traceId)}`;
}

function findingHref(findingId: string, revision: number): string {
  return `/findings/${encodeURIComponent(findingId)}/revisions/${revision}`;
}

function nullable(value: string | number | null | undefined): string | number {
  return value ?? "未绑定";
}

function collectionFromPage<T>(
  page: PageLike<T>,
  ownerRunId: string,
  generation: number,
): CollectionState<T> {
  return {
    generation,
    items: page.items,
    loading: false,
    nextCursor: cursorFromPage(page),
    ownerRunId,
  };
}

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("分页读取失败。");
}

async function readCollectionPage<T>(
  current: CollectionState<T>,
  setCurrent: Dispatch<SetStateAction<CollectionState<T> | null>>,
  read: (cursor: string | null) => Promise<PageLike<T>>,
  restart: boolean,
  isCurrent: (ownerRunId: string, generation: number) => boolean,
): Promise<void> {
  const cursor = restart ? null : current.nextCursor;
  if (!restart && cursor === null) return;
  const matchesRequest = (candidate: CollectionState<T> | null): candidate is CollectionState<T> =>
    candidate !== null &&
    candidate.ownerRunId === current.ownerRunId &&
    candidate.generation === current.generation &&
    candidate.nextCursor === current.nextCursor;
  setCurrent((latest) => (matchesRequest(latest) ? { ...latest, error: undefined, loading: true } : latest));
  try {
    const page = await read(cursor);
    if (!isCurrent(current.ownerRunId, current.generation)) return;
    setCurrent((latest) =>
      matchesRequest(latest)
        ? {
            ...latest,
            items: restart ? page.items : [...latest.items, ...page.items],
            loading: false,
            nextCursor: cursorFromPage(page),
          }
        : latest,
    );
  } catch (error) {
    if (!isCurrent(current.ownerRunId, current.generation)) return;
    setCurrent((latest) =>
      matchesRequest(latest) ? { ...latest, error: normalizedError(error), loading: false } : latest,
    );
  }
}

function CollectionControls({
  label,
  onLoadMore,
  onRestart,
  state,
}: {
  label: string;
  onLoadMore(): void;
  onRestart(): void;
  state: CollectionState<unknown>;
}) {
  if (state.error) {
    const expired = state.error instanceof CursorExpiredError;
    return (
      <div>
        <p role="alert">{state.error.message}</p>
        <button onClick={expired ? onRestart : onLoadMore} type="button">
          {expired ? `从首屏重新读取 ${label}` : `重试加载更多${label}`}
        </button>
      </div>
    );
  }
  if (state.loading) return <p role="status">正在读取更多{label}…</p>;
  if (state.nextCursor === null) return null;
  return (
    <button onClick={onLoadMore} type="button">
      加载更多{label}
    </button>
  );
}

function ManifestPanel({
  emptyMessage,
  label,
  linkLabel,
  manifest,
}: {
  emptyMessage: string;
  label: string;
  linkLabel: string;
  manifest: ArtifactPayloadView | null;
}) {
  return (
    <section aria-label={label}>
      <h2>{label}</h2>
      {manifest === null ? (
        <p>{emptyMessage}</p>
      ) : (
        <>
          <dl>
            <div>
              <dt>工件</dt>
              <dd>{manifest.artifact.artifact_id}</dd>
            </div>
            <div>
              <dt>类型</dt>
              <dd>{manifest.artifact.kind}</dd>
            </div>
            <div>
              <dt>Payload schema</dt>
              <dd>{nullable(manifest.artifact.payload_schema_id)}</dd>
            </div>
            <div>
              <dt>Payload hash</dt>
              <dd>{nullable(manifest.artifact.payload_hash)}</dd>
            </div>
          </dl>
          <a href={artifactHref(manifest.artifact.artifact_id)}>{linkLabel}</a>
          <pre aria-label={`${label} payload`} tabIndex={0}>
            {JSON.stringify(manifest.payload, null, 2)}
          </pre>
        </>
      )}
    </section>
  );
}

export function RunDetailPage({
  api = runDetailApi,
  commandClient: providedCommandClient,
  runId,
}: RunDetailPageProps) {
  const [eventState, setEventState] = useState<OwnedEventState>({ items: [], ownerRunId: runId });
  const [ownedStreamState, setOwnedStreamState] = useState<OwnedStreamState>({
    ownerRunId: runId,
    state: { status: "idle" },
  });
  const [findingsState, setFindingsState] = useState<CollectionState<FindingRevision> | null>(null);
  const [commandsState, setCommandsState] = useState<CollectionState<RunCommandView> | null>(null);
  const [tracesState, setTracesState] = useState<CollectionState<TraceSummary> | null>(null);
  const streamRef = useRef<OwnedStream>();
  const activeRunIdRef = useRef(runId);
  activeRunIdRef.current = runId;
  const browserCommandClient = useMemo(
    () =>
      createBrowserRunCommandClient({
        async loadCommands(requestedRunId) {
          const commands: RunCommandView[] = [];
          let cursor: string | null = null;
          do {
            const page = await api.loadCommandsPage(requestedRunId, cursor);
            commands.push(...page.items);
            cursor = cursorFromPage(page);
          } while (cursor !== null);
          return commands;
        },
        async resumeEvents(requestedRunId) {
          const owned = streamRef.current;
          if (owned?.ownerRunId === requestedRunId) await owned.stream.start();
        },
      }),
    [api],
  );
  const commandClient = providedCommandClient ?? browserCommandClient;
  const detail = useQuery({
    queryFn: () => api.load(runId),
    queryKey: ["run-detail", runId],
    retry: false,
  });
  const detailIdentityRef = useRef<RunDetailSnapshot>();
  const detailGenerationRef = useRef(0);
  if (detail.data && detailIdentityRef.current !== detail.data) {
    detailIdentityRef.current = detail.data;
    detailGenerationRef.current += 1;
  }
  const detailGeneration = detailGenerationRef.current;
  const { refetch } = detail;

  useEffect(() => {
    if (!detail.data) return;
    setFindingsState(collectionFromPage(detail.data.findingsPage, runId, detailGeneration));
    setCommandsState(collectionFromPage(detail.data.commandsPage, runId, detailGeneration));
    setTracesState(collectionFromPage(detail.data.tracesPage, runId, detailGeneration));
  }, [detail.data, detailGeneration, runId]);

  useEffect(() => {
    const ownerRunId = runId;
    const publishStreamState = (state: RunEventStreamState) => {
      if (activeRunIdRef.current !== ownerRunId) return;
      setOwnedStreamState({ ownerRunId, state });
    };
    setEventState({ items: [], ownerRunId });
    publishStreamState({ status: "idle" });
    const stream = api.createEventStream({
      onEvent(event, cursor) {
        if (activeRunIdRef.current !== ownerRunId) return;
        setEventState((current) => ({
          items:
            current.ownerRunId === ownerRunId ? [...current.items, { cursor, event }] : [{ cursor, event }],
          ownerRunId,
        }));
        if (terminalEvents.has(event.event_type)) void refetch();
      },
      onStateChange: publishStreamState,
      runId: ownerRunId,
    });
    streamRef.current = { ownerRunId, stream };
    void stream.start().catch((error: unknown) => {
      publishStreamState({ error: normalizedError(error), status: "error" });
    });
    return () => {
      stream.close();
      if (streamRef.current?.stream === stream) streamRef.current = undefined;
    };
  }, [api, refetch, runId]);

  const publishStreamError = (ownerRunId: string, error: unknown) => {
    if (activeRunIdRef.current !== ownerRunId) return;
    setOwnedStreamState({
      ownerRunId,
      state: { error: normalizedError(error), status: "error" },
    });
  };

  const reconnectStream = () => {
    const owned = streamRef.current;
    if (!owned || owned.ownerRunId !== runId) return;
    void owned.stream.start().catch((error: unknown) => {
      publishStreamError(owned.ownerRunId, error);
    });
  };

  const restartStream = () => {
    const owned = streamRef.current;
    if (!owned || owned.ownerRunId !== runId) return;
    void owned.stream.restart().catch((error: unknown) => {
      publishStreamError(owned.ownerRunId, error);
    });
  };

  if (detail.isPending) {
    return (
      <div className="gf-page">
        <h1>运行详情</h1>
        <p role="status">正在读取运行详情…</p>
      </div>
    );
  }

  if (detail.isError) {
    const expired = detail.error instanceof CursorExpiredError;
    return (
      <div className="gf-page">
        <section aria-labelledby="run-detail-error-title">
          <h1 id="run-detail-error-title">运行详情暂不可用</h1>
          <p role="alert">{detail.error.message}</p>
          {!expired && <p>请确认权限或稍后重试。</p>}
          <button onClick={() => void refetch()} type="button">
            {expired ? "重新读取运行详情" : "重试读取"}
          </button>
        </section>
      </div>
    );
  }

  const { failureManifest, resultManifest, run } = detail.data;
  const ownsCurrentGeneration = (ownerRunId: string, generation: number) =>
    activeRunIdRef.current === ownerRunId && detailGenerationRef.current === generation;
  const findings = ownsCurrentGeneration(findingsState?.ownerRunId ?? "", findingsState?.generation ?? -1)
    ? findingsState!
    : collectionFromPage(detail.data.findingsPage, runId, detailGeneration);
  const commands = ownsCurrentGeneration(commandsState?.ownerRunId ?? "", commandsState?.generation ?? -1)
    ? commandsState!
    : collectionFromPage(detail.data.commandsPage, runId, detailGeneration);
  const traces = ownsCurrentGeneration(tracesState?.ownerRunId ?? "", tracesState?.generation ?? -1)
    ? tracesState!
    : collectionFromPage(detail.data.tracesPage, runId, detailGeneration);
  const events = eventState.ownerRunId === runId ? eventState.items : [];
  const streamState =
    ownedStreamState.ownerRunId === runId ? ownedStreamState.state : { status: "idle" as const };
  const firstTrace = traces.items[0];

  return (
    <div className="gf-page">
      <header>
        <p>权威 RunView · {run.view_schema_version}</p>
        <h1>运行 {run.run_id}</h1>
      </header>

      <section aria-labelledby="run-view-title">
        <h2 id="run-view-title">运行状态</h2>
        <dl>
          <div>
            <dt>状态</dt>
            <dd>{run.status}</dd>
          </div>
          <div>
            <dt>修订</dt>
            <dd>{run.revision}</dd>
          </div>
          <div>
            <dt>Attempt</dt>
            <dd>{nullable(run.attempt_no)}</dd>
          </div>
          <div>
            <dt>状态资源</dt>
            <dd>{run.status_url}</dd>
          </div>
          <div>
            <dt>事件资源</dt>
            <dd>{run.events_url}</dd>
          </div>
          <div>
            <dt>结果工件</dt>
            <dd>{nullable(run.result_artifact_id)}</dd>
          </div>
          <div>
            <dt>失败清单</dt>
            <dd>{nullable(run.failure_artifact_id)}</dd>
          </div>
          <div>
            <dt>终态 cassette</dt>
            <dd>{nullable(run.terminal_cassette_artifact_id)}</dd>
          </div>
        </dl>
      </section>

      {streamState.status === "expired" && (
        <section aria-label="事件流已过期">
          <p role="alert">
            已保存的事件游标超出保留范围
            {streamState.earliestCursor ? `；最早可用游标 ${streamState.earliestCursor}` : ""}。
          </p>
          <button onClick={restartStream} type="button">
            重新开始事件流
          </button>
        </section>
      )}
      {streamState.status === "disconnected" && (
        <section aria-label="事件流连接中断">
          <p role="status">事件流连接已中断；已保存的游标仍可用于续传。</p>
          <button onClick={reconnectStream} type="button">
            重新连接事件流
          </button>
        </section>
      )}
      {streamState.status === "error" && (
        <section aria-label="事件流错误">
          <p role="alert">{streamState.error?.message ?? "运行事件流读取失败。"}</p>
          <button onClick={reconnectStream} type="button">
            重新连接事件流
          </button>
        </section>
      )}

      <RunProgress
        commands={commands.items}
        events={events}
        run={run}
        traceHref={firstTrace ? traceHref(firstTrace.trace_id) : undefined}
      />
      <CollectionControls
        label="命令"
        onLoadMore={() =>
          void readCollectionPage(
            commands,
            setCommandsState,
            (cursor) => api.loadCommandsPage(commands.ownerRunId, cursor),
            false,
            ownsCurrentGeneration,
          )
        }
        onRestart={() =>
          void readCollectionPage(
            commands,
            setCommandsState,
            (cursor) => api.loadCommandsPage(commands.ownerRunId, cursor),
            true,
            ownsCurrentGeneration,
          )
        }
        state={commands}
      />
      <RunCommandControls
        client={commandClient}
        onProblem={async () => {
          await refetch();
        }}
        onPersisted={() => void refetch()}
        runId={run.run_id}
        runRevision={run.revision}
        runStatus={run.status}
      />

      <section aria-labelledby="run-findings-title">
        <h2 id="run-findings-title">Findings</h2>
        {findings.items.length === 0 ? (
          <p>此运行尚未发布 Finding。</p>
        ) : (
          <ol>
            {findings.items.map((finding) => (
              <li key={`${finding.finding_id}:${finding.revision}`}>
                <a href={findingHref(finding.finding_id, finding.revision)}>
                  {finding.finding_id} · r{finding.revision}
                </a>
                <p>
                  {finding.payload.oracle_type} · {finding.payload.severity} · {finding.payload.status}
                </p>
                <p>{finding.payload.message}</p>
              </li>
            ))}
          </ol>
        )}
        <CollectionControls
          label="Findings"
          onLoadMore={() =>
            void readCollectionPage(
              findings,
              setFindingsState,
              (cursor) => api.loadFindingsPage(findings.ownerRunId, cursor),
              false,
              ownsCurrentGeneration,
            )
          }
          onRestart={() =>
            void readCollectionPage(
              findings,
              setFindingsState,
              (cursor) => api.loadFindingsPage(findings.ownerRunId, cursor),
              true,
              ownsCurrentGeneration,
            )
          }
          state={findings}
        />
      </section>

      <ManifestPanel
        emptyMessage="当前 RunView 未绑定结果工件。"
        label="结果清单"
        linkLabel="打开结果工件"
        manifest={resultManifest}
      />
      <ManifestPanel
        emptyMessage="当前 RunView 未绑定失败清单。"
        label="失败清单"
        linkLabel="打开失败清单"
        manifest={failureManifest}
      />

      <section aria-labelledby="run-traces-title">
        <h2 id="run-traces-title">追踪</h2>
        {traces.items.length === 0 ? (
          <p>此运行尚无可读追踪。</p>
        ) : (
          <ol>
            {traces.items.map((trace) => (
              <li key={trace.trace_id}>
                <a href={traceHref(trace.trace_id)}>{trace.trace_id}</a>
                <span>
                  {" "}
                  · {trace.status} · {trace.span_count} spans
                </span>
              </li>
            ))}
          </ol>
        )}
        <CollectionControls
          label="追踪"
          onLoadMore={() =>
            void readCollectionPage(
              traces,
              setTracesState,
              (cursor) => api.loadTracesPage(traces.ownerRunId, cursor),
              false,
              ownsCurrentGeneration,
            )
          }
          onRestart={() =>
            void readCollectionPage(
              traces,
              setTracesState,
              (cursor) => api.loadTracesPage(traces.ownerRunId, cursor),
              true,
              ownsCurrentGeneration,
            )
          }
          state={traces}
        />
      </section>
    </div>
  );
}
