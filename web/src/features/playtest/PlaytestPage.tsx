import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Boxes, FlaskConical, Gamepad2, GitBranch, ListChecks, Route, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import type { RunCommandClient } from "../../api/commands";
import { createMutationIntent, ReauthenticationRequiredError, type MutationIntent } from "../../api/csrf";
import type { RunEvent } from "../../api/generated/sse-run-event-v1";
import { cursorFromPage, CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { createBrowserRunCommandClient } from "../../api/runtime";
import type { RunEventStreamState } from "../../api/sse";
import { ReauthenticationLink } from "../../app/ReauthenticationLink";
import { RunCommandControls, RunProgress, type RunEventItem } from "../../components/run-progress";
import { ProblemPanel, StatePanel } from "../../components/ui";
import { replaySourceOptionLabel, type ReplaySourceRun } from "../runs/replaySources";
import {
  playtestApi,
  type ArtifactPayloadView,
  type ArtifactSummaryPage,
  type ExecutionOptionResolveRequest,
  type ExecutionProfileListFilters,
  type ExecutionProfilePage,
  type PlaytestApi,
  type PlaytestEventStreamHandle,
  type PlaytestRunRequest,
  type ProspectivePlaytestRunRequest,
  type RunAccepted,
  type RunView,
  type TaskSuiteArtifactView,
  type TaskSuiteDeriveRequest,
  type TaskSuiteListFilters,
} from "./api";
import { requireTaskSuiteAuthority, type TaskSuiteNavigationCandidate } from "./authority";
import { PlaytestTerminalPanel } from "./PlaytestTerminalPanel";

import "./playtest.css";

type ExecutionProfile = ExecutionProfilePage["items"][number];
type LlmExecutionMode = ProspectivePlaytestRunRequest["llm_execution_mode"];
type InteractionMode = ProspectivePlaytestRunRequest["params"]["interaction_mode"];

const terminalEvents = new Set<RunEvent["event_type"]>([
  "run.succeeded",
  "run.failed",
  "run.cancelled",
  "run.timed_out",
]);
const terminalRunStatuses = new Set<RunView["status"]>(["succeeded", "failed", "cancelled", "timed_out"]);

interface CandidateContextIds {
  sourceRunId: string | null;
  previewArtifactId: string;
  configArtifactId: string;
  constraintArtifactId: string;
}

interface CandidateContextAuthority extends CandidateContextIds {
  envContractVersion: string;
  environmentProfile: { profile_id: string; version: number };
  config: ArtifactPayloadView;
  requiredDomainIds: readonly string[];
}

interface DeriveAttempt {
  accepted: RunAccepted | null;
  error: Error | null;
  intent: MutationIntent | null;
  ownerKey: string | null;
  pending: boolean;
  request: TaskSuiteDeriveRequest | null;
}

interface LaunchAttempt {
  accepted: RunAccepted | null;
  error: Error | null;
  intent: MutationIntent | null;
  ownerKey: string | null;
  pending: boolean;
  request: PlaytestRunRequest | null;
}

interface RecoveredSuiteChoice {
  artifactId: string;
  sourceRunId: string;
}

class PlaytestPageAuthorityError extends Error {
  override name = "PlaytestPageAuthorityError";
}

class ExecutionOptionResolutionError extends Error {
  override name = "ExecutionOptionResolutionError";
}

function isUnknownTransportError(error: Error | null): boolean {
  return (
    error !== null &&
    !(error instanceof ApiProblemError) &&
    !(error instanceof ReauthenticationRequiredError) &&
    !(error instanceof PlaytestPageAuthorityError)
  );
}

function isUnknownDeriveAttempt(
  attempt: DeriveAttempt | null,
): attempt is DeriveAttempt & { intent: MutationIntent; request: TaskSuiteDeriveRequest } {
  return (
    attempt !== null &&
    attempt.intent !== null &&
    attempt.request !== null &&
    isUnknownTransportError(attempt.error)
  );
}

function isUnknownLaunchAttempt(
  attempt: LaunchAttempt | null,
): attempt is LaunchAttempt & { intent: MutationIntent; request: PlaytestRunRequest } {
  return (
    attempt !== null &&
    attempt.intent !== null &&
    attempt.request !== null &&
    isUnknownTransportError(attempt.error)
  );
}

function fail(message: string): never {
  throw new PlaytestPageAuthorityError(message);
}

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("Playtest request failed.");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isProfileRef(value: unknown): value is { profile_id: string; version: number } {
  return (
    isRecord(value) &&
    typeof value.profile_id === "string" &&
    value.profile_id.length > 0 &&
    Number.isSafeInteger(value.version) &&
    Number(value.version) >= 1
  );
}

function sameProfile(
  left: { profile_id: string; version: number } | null | undefined,
  right: { profile_id: string; version: number } | null | undefined,
): boolean {
  return left?.profile_id === right?.profile_id && left?.version === right?.version;
}

function profileKey(profile: { profile_id: string; version: number }): string {
  return `${profile.profile_id}@${profile.version}`;
}

function unionDomainIds(
  ...scopes: ReadonlyArray<{ domain_ids: readonly string[] } | "all" | null>
): string[] {
  const domainIds = scopes.flatMap((scope) => {
    if (scope === null || scope === "all") {
      fail("Dedicated content authority returned a non-concrete domain scope.");
    }
    return scope.domain_ids;
  });
  return [...new Set(domainIds)].sort();
}

function profileCoversDomains(profile: ExecutionProfile, requiredDomainIds: readonly string[]): boolean {
  const covered = new Set(profile.domain_scope.domain_ids);
  return requiredDomainIds.every((domainId) => covered.has(domainId));
}

function taskSuiteDomainIds(suite: TaskSuiteArtifactView): string[] {
  return unionDomainIds(
    suite.artifact.domain_scope,
    ...suite.task_suite.episodes.map((episode) => episode.domain_scope),
  );
}

function taskSuiteOwnerKey(suite: TaskSuiteArtifactView): string {
  return `${suite.artifact.artifact_id}\u0000${suite.artifact.payload_hash}`;
}

interface ConfigCandidate {
  artifactId: string;
  constraintArtifactId: string;
  createdAt: string | null;
  domainIds: readonly string[];
  envContractVersion: string;
  environmentProfile: { profile_id: string; version: number };
  previewArtifactId: string;
}

function requireConfigCandidate(
  manifest: ArtifactPayloadView,
  summary: ArtifactSummaryPage["items"][number],
): ConfigCandidate {
  const payload = manifest.payload;
  if (
    summary.summary_schema_version !== "artifact-summary@1" ||
    summary.kind !== "config_export" ||
    summary.payload_schema_id !== "config-export-package@1" ||
    typeof summary.payload_hash !== "string" ||
    manifest.view_schema_version !== "artifact-payload-view@1" ||
    manifest.artifact.artifact_id !== summary.artifact_id ||
    manifest.artifact.kind !== "config_export" ||
    manifest.artifact.payload_schema_id !== "config-export-package@1" ||
    manifest.artifact.payload_hash !== summary.payload_hash ||
    !isRecord(payload) ||
    payload.package_schema_version !== "config-export-package@1" ||
    typeof payload.source_preview_artifact_id !== "string" ||
    payload.source_preview_artifact_id.length === 0 ||
    typeof payload.constraint_snapshot_artifact_id !== "string" ||
    payload.constraint_snapshot_artifact_id.length === 0 ||
    !isProfileRef(payload.target_environment_profile) ||
    typeof payload.env_contract_version !== "string" ||
    payload.env_contract_version.length === 0 ||
    manifest.artifact.domain_scope === null ||
    manifest.artifact.domain_scope === "all"
  ) {
    fail("ConfigExport catalog item does not close against its immutable payload authority.");
  }
  return {
    artifactId: manifest.artifact.artifact_id,
    constraintArtifactId: payload.constraint_snapshot_artifact_id,
    createdAt: manifest.artifact.created_at ?? null,
    domainIds: manifest.artifact.domain_scope.domain_ids,
    envContractVersion: payload.env_contract_version,
    environmentProfile: payload.target_environment_profile,
    previewArtifactId: payload.source_preview_artifact_id,
  };
}

function supportsRunKind(profile: ExecutionProfile, kind: string): boolean {
  return profile.compatible_run_kinds.some((candidate) => candidate.kind === kind && candidate.version === 1);
}

async function loadCandidateContext(
  api: PlaytestApi,
  ids: CandidateContextIds,
): Promise<CandidateContextAuthority> {
  const [preview, constraint, config] = await Promise.all([
    api.getSpec(ids.previewArtifactId),
    api.getConstraint(ids.constraintArtifactId),
    api.getArtifact(ids.configArtifactId),
  ]);
  if (
    preview.artifact.artifact_id !== ids.previewArtifactId ||
    preview.artifact.kind !== "ir_snapshot" ||
    preview.artifact.payload_schema_id !== "ir-core@1" ||
    constraint.artifact.artifact_id !== ids.constraintArtifactId ||
    constraint.artifact.kind !== "constraint_snapshot" ||
    constraint.artifact.payload_schema_id !== "constraint-snapshot@1" ||
    config.artifact.artifact_id !== ids.configArtifactId ||
    config.artifact.kind !== "config_export" ||
    config.artifact.payload_schema_id !== "config-export-package@1" ||
    !isRecord(config.payload) ||
    config.payload.package_schema_version !== "config-export-package@1" ||
    config.payload.source_preview_artifact_id !== ids.previewArtifactId ||
    config.payload.constraint_snapshot_artifact_id !== ids.constraintArtifactId ||
    !isProfileRef(config.payload.target_environment_profile) ||
    typeof config.payload.env_contract_version !== "string" ||
    config.payload.env_contract_version.length === 0 ||
    preview.snapshot_id !== config.artifact.version_tuple.ir_snapshot_id ||
    constraint.artifact.version_tuple.constraint_snapshot_id !==
      config.artifact.version_tuple.constraint_snapshot_id
  ) {
    fail("Generation navigation context does not close against dedicated content authority.");
  }
  return {
    ...ids,
    config,
    envContractVersion: config.payload.env_contract_version,
    environmentProfile: config.payload.target_environment_profile,
    requiredDomainIds: unionDomainIds(
      preview.artifact.domain_scope,
      config.artifact.domain_scope,
      constraint.artifact.domain_scope,
    ),
  };
}

async function collectExecutionProfiles(
  api: PlaytestApi,
  filters: ExecutionProfileListFilters,
): Promise<ExecutionProfile[]> {
  const items: ExecutionProfile[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listExecutionProfiles(filters, cursor);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      fail("Execution profile pagination changed read snapshot.");
    }
    readSnapshotId = page.read_snapshot_id;
    items.push(...page.items);
    const next = cursorFromPage(page);
    if (next === null) return items;
    if (seen.has(next)) fail("Execution profile pagination returned a cursor cycle.");
    seen.add(next);
    cursor = next;
  }
  fail("Execution profile pagination exceeded its bounded page count.");
}

interface TaskSuitePageParam {
  cursor: string | null;
  readSnapshotId: string | null;
}

interface ConfigCandidatePageParam {
  cursor: string | null;
  readSnapshotId: string | null;
}

interface LoadedConfigCandidatePage {
  candidates: ConfigCandidate[];
  cursor: string | null;
  page: ArtifactSummaryPage;
}

async function loadConfigCandidatePage(
  api: PlaytestApi,
  pageParam: ConfigCandidatePageParam,
): Promise<LoadedConfigCandidatePage> {
  const page = await api.listConfigExports(pageParam.cursor);
  if (pageParam.readSnapshotId !== null && page.read_snapshot_id !== pageParam.readSnapshotId) {
    fail("ConfigExport pagination changed read snapshot.");
  }
  const artifactIds = new Set<string>();
  for (const summary of page.items) {
    if (artifactIds.has(summary.artifact_id)) {
      fail("ConfigExport catalog page returned a duplicate immutable Artifact.");
    }
    artifactIds.add(summary.artifact_id);
  }
  const candidates = await Promise.all(
    page.items.map(async (summary) =>
      requireConfigCandidate(await api.getArtifact(summary.artifact_id), summary),
    ),
  );
  return { candidates, cursor: pageParam.cursor, page };
}

interface LoadedTaskSuitePage {
  cursor: string | null;
  page: Awaited<ReturnType<PlaytestApi["listTaskSuites"]>>;
}

async function loadTaskSuitePage(
  api: PlaytestApi,
  filters: TaskSuiteListFilters,
  navigationCandidate: TaskSuiteNavigationCandidate,
  pageParam: TaskSuitePageParam,
): Promise<LoadedTaskSuitePage> {
  const page = await api.listTaskSuites(filters, pageParam.cursor);
  if (pageParam.readSnapshotId !== null && page.read_snapshot_id !== pageParam.readSnapshotId) {
    fail("TaskSuite pagination changed read snapshot.");
  }
  const artifactIds = new Set<string>();
  for (const view of page.items) {
    const authority = requireTaskSuiteAuthority(view, view.artifact.artifact_id, navigationCandidate);
    if (!authority.navigation.matches) {
      fail(
        `TaskSuite list item differs from the exact navigation candidate: ${authority.navigation.mismatches.join(", ")}.`,
      );
    }
    if (artifactIds.has(view.artifact.artifact_id)) {
      fail("TaskSuite page returned a duplicate immutable Artifact.");
    }
    artifactIds.add(view.artifact.artifact_id);
  }
  return { cursor: pageParam.cursor, page };
}

async function loadSelectedTaskSuite(
  api: PlaytestApi,
  artifactId: string,
  navigationCandidate: TaskSuiteNavigationCandidate,
): Promise<TaskSuiteArtifactView> {
  const view = await api.getTaskSuite(artifactId);
  const authority = requireTaskSuiteAuthority(view, artifactId, navigationCandidate);
  if (!authority.navigation.matches) {
    fail(
      `Selected TaskSuite differs from the current navigation candidate: ${authority.navigation.mismatches.join(", ")}.`,
    );
  }
  return authority.view;
}

function useProfileCatalog(api: PlaytestApi) {
  return useQuery({
    queryFn: async () => {
      const [derivations, activePlanners, replayOnlyPlanners] = await Promise.all([
        collectExecutionProfiles(api, {
          limit: 100,
          profile_kind: "task_suite_derivation",
          run_kind: "task_suite.derive",
          run_kind_version: 1,
          status: "active",
        }),
        collectExecutionProfiles(api, {
          limit: 100,
          profile_kind: "playtest_planner",
          run_kind: "playtest.run",
          run_kind_version: 1,
          status: "active",
        }),
        collectExecutionProfiles(api, {
          limit: 100,
          profile_kind: "playtest_planner",
          run_kind: "playtest.run",
          run_kind_version: 1,
          status: "replay_only",
        }),
      ]);
      return {
        derivations: derivations.filter(
          (profile) =>
            profile.profile_kind === "task_suite_derivation" &&
            profile.status === "active" &&
            supportsRunKind(profile, "task_suite.derive"),
        ),
        activePlanners: activePlanners.filter(
          (profile) =>
            profile.profile_kind === "playtest_planner" &&
            profile.status === "active" &&
            supportsRunKind(profile, "playtest.run"),
        ),
        replayOnlyPlanners: replayOnlyPlanners.filter(
          (profile) =>
            profile.profile_kind === "playtest_planner" &&
            profile.status === "replay_only" &&
            supportsRunKind(profile, "playtest.run"),
        ),
      };
    },
    queryKey: ["playtest", "profile-catalog"],
    retry: false,
  });
}

function MutationError({ error }: { error: Error | null }) {
  if (error === null) return null;
  if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
  if (error instanceof PlaytestPageAuthorityError) {
    return <StatePanel description={error.message} state="error" title="Playtest launch docket 无效" />;
  }
  if (error instanceof ExecutionOptionResolutionError) {
    return (
      <StatePanel
        description="执行选项读取失败；Playtest mutation 尚未发送，可重新解析。"
        state="error"
        title="执行选项未解析"
      />
    );
  }
  if (error instanceof ReauthenticationRequiredError) {
    return (
      <StatePanel
        action={<ReauthenticationLink />}
        description="当前标签页没有可用 CSRF 会话；请求未发送。"
        state="error"
        title="需要重新登录"
      />
    );
  }
  return (
    <StatePanel
      description="传输结果未知；再次提交会复用同一 MutationIntent，不会静默创建另一条 Run。"
      state="error"
      title="请求结果未知"
    />
  );
}

export async function collectRunCommands(
  api: PlaytestApi,
  runId: string,
): Promise<Awaited<ReturnType<PlaytestApi["listRunCommands"]>>["items"]> {
  const commands: Awaited<ReturnType<PlaytestApi["listRunCommands"]>>["items"] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listRunCommands(runId, cursor);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      fail("Run command pagination changed read snapshot.");
    }
    readSnapshotId = page.read_snapshot_id;
    commands.push(...page.items);
    const next = cursorFromPage(page);
    if (next === null) return commands;
    if (seen.has(next)) fail("Run command pagination returned a cursor cycle.");
    seen.add(next);
    cursor = next;
  }
  fail("Run command pagination exceeded its bounded page count.");
}

async function collectReplaySourceRuns(api: PlaytestApi): Promise<ReplaySourceRun[]> {
  const runs: ReplaySourceRun[] = [];
  const seenCursors = new Set<string>();
  const seenRuns = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listReplaySourceRuns(cursor);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      fail("Replay source Run pagination changed read snapshot.");
    }
    readSnapshotId = page.read_snapshot_id;
    for (const run of page.items) {
      if (seenRuns.has(run.run_id)) fail("Replay source Run pagination returned a duplicate Run.");
      seenRuns.add(run.run_id);
      runs.push(run);
    }
    const next = cursorFromPage(page);
    if (next === null) return runs;
    if (seenCursors.has(next)) fail("Replay source Run pagination returned a cursor cycle.");
    seenCursors.add(next);
    cursor = next;
  }
  fail("Replay source Run pagination exceeded its bounded page count.");
}

function replayRunLabel(run: ReplaySourceRun): string {
  return replaySourceOptionLabel(run);
}

function TrackedRun({
  api,
  commandClient: providedCommandClient,
  label,
  runId,
}: {
  api: PlaytestApi;
  commandClient?: RunCommandClient;
  label: string;
  runId: string;
}) {
  const [events, setEvents] = useState<RunEventItem[]>([]);
  const [streamState, setStreamState] = useState<RunEventStreamState>({ status: "idle" });
  const streamRef = useRef<PlaytestEventStreamHandle>();
  const streamReceivedEventRef = useRef(false);
  const run = useQuery({
    queryFn: () => api.getRun(runId),
    queryKey: ["playtest", "run", runId],
    retry: false,
  });
  const { refetch } = run;
  const browserCommandClient = useMemo(
    () =>
      createBrowserRunCommandClient({
        loadCommands: (requestedRunId) => collectRunCommands(api, requestedRunId),
        async resumeEvents(requestedRunId) {
          if (requestedRunId === runId) await streamRef.current?.start();
        },
      }),
    [api, runId],
  );
  const commandClient = providedCommandClient ?? browserCommandClient;
  const hasTerminalRunView = run.data !== undefined && terminalRunStatuses.has(run.data.status);

  useEffect(() => {
    setEvents([]);
    setStreamState({ status: "idle" });
    streamReceivedEventRef.current = false;
    const stream = api.createEventStream({
      onEvent(event, cursor) {
        if (event.run_id !== runId) return;
        streamReceivedEventRef.current = true;
        setEvents((current) => {
          if (current.some((item) => item.cursor === cursor)) return current;
          return [...current, { cursor, event }];
        });
        if (terminalEvents.has(event.event_type)) void refetch();
      },
      onStateChange(state) {
        if (state.status === "connecting") streamReceivedEventRef.current = false;
        setStreamState(state);
      },
      runId,
    });
    streamRef.current = stream;
    void stream.start().catch((error: unknown) => {
      setStreamState({ error: normalizedError(error), status: "error" });
    });
    return () => {
      stream.close();
      if (streamRef.current === stream) streamRef.current = undefined;
    };
  }, [api, refetch, runId]);

  return (
    <section className="gf-playtest__run" aria-label={`${label} ${runId}`}>
      <header>
        <p className="gf-playtest__kicker">Run-backed operation</p>
        <h2>{label}</h2>
        <code>{runId}</code>
      </header>
      {streamState.status === "expired" && (
        <StatePanel
          action={
            <button
              className="gf-secondary-button"
              onClick={() => void streamRef.current?.restart()}
              type="button"
            >
              从最早保留事件重新开始
            </button>
          }
          description="SSE resume cursor 已过期；页面不会静默跳过事件。"
          state="error"
          title="事件流需要显式重启"
        />
      )}
      {((streamState.status === "disconnected" && (!hasTerminalRunView || streamReceivedEventRef.current)) ||
        streamState.status === "error") && (
        <StatePanel
          action={
            <button
              className="gf-secondary-button"
              onClick={() => void streamRef.current?.start()}
              type="button"
            >
              使用已保存 cursor 重连
            </button>
          }
          description="事件流中断；Last-Event-ID 仍被保留。"
          state="error"
          title="事件流连接中断"
        />
      )}
      {run.isPending ? (
        <StatePanel description="正在读取权威 RunView。" state="loading" title="正在读取运行" />
      ) : run.isError ? (
        run.error instanceof ApiProblemError ? (
          <ProblemPanel problem={run.error.problem} />
        ) : (
          <StatePanel
            description="运行读取失败；不会从本地 SSE 猜测终态。"
            state="error"
            title="无法读取运行"
          />
        )
      ) : run.data.run_id !== runId ? (
        <StatePanel
          description="RunView identity 与请求不一致。"
          state="error"
          title="Run identity mismatch"
        />
      ) : (
        <>
          <RunProgress events={events} run={run.data} />
          <RunCommandControls
            client={commandClient}
            onPersisted={() => void refetch()}
            onProblem={async () => void (await refetch())}
            runId={runId}
            runRevision={run.data.revision}
            runStatus={run.data.status}
          />
        </>
      )}
    </section>
  );
}

async function loadDerivedTaskSuite(
  api: PlaytestApi,
  expectedRunId: string,
  run: RunView,
  context: CandidateContextAuthority,
): Promise<TaskSuiteArtifactView> {
  if (
    run.view_schema_version !== "run-view@1" ||
    run.run_id !== expectedRunId ||
    !Number.isSafeInteger(run.revision) ||
    run.revision < 1 ||
    run.status !== "succeeded" ||
    run.result_artifact_id == null ||
    run.failure_artifact_id !== null
  ) {
    fail("TaskSuite derivation Run has no successful RunResult authority.");
  }
  const manifest = await api.getArtifact(run.result_artifact_id);
  const payload = manifest.payload;
  if (
    manifest.artifact.artifact_id !== run.result_artifact_id ||
    manifest.artifact.kind !== "run_result" ||
    manifest.artifact.payload_schema_id !== "run-result@1" ||
    !isRecord(payload) ||
    payload.result_schema_version !== "run-result@1" ||
    payload.run_id !== run.run_id ||
    !isRecord(payload.run_kind) ||
    payload.run_kind.kind !== "task_suite.derive" ||
    payload.run_kind.version !== 1 ||
    payload.outcome_code !== "task_suite_derived" ||
    typeof payload.primary_artifact_id !== "string" ||
    payload.primary_artifact_id.length === 0 ||
    !Array.isArray(payload.produced_artifact_ids) ||
    !payload.produced_artifact_ids.includes(payload.primary_artifact_id) ||
    !isRecord(payload.summary) ||
    payload.summary.primary_artifact_kind !== "task_suite" ||
    payload.summary.outcome_code !== "task_suite_derived"
  ) {
    fail("TaskSuite derivation RunResult does not bind an exact primary TaskSuite.");
  }
  const suite = await api.getTaskSuite(payload.primary_artifact_id);
  const authority = requireTaskSuiteAuthority(suite, payload.primary_artifact_id, {
    configArtifactId: context.configArtifactId,
    constraintSnapshotArtifactId: context.constraintArtifactId,
    environmentProfile: context.environmentProfile,
    sourcePreviewArtifactId: context.previewArtifactId,
  });
  if (!authority.navigation.matches) {
    fail(
      `Derived TaskSuite differs from the exact candidate: ${authority.navigation.mismatches.join(", ")}.`,
    );
  }
  return authority.view;
}

function candidateContextKey(context: CandidateContextAuthority): string {
  return [
    context.previewArtifactId,
    context.configArtifactId,
    context.constraintArtifactId,
    context.environmentProfile.profile_id,
    context.environmentProfile.version,
    context.envContractVersion,
    context.config.artifact.payload_hash,
  ].join("\u0000");
}

function DerivedSuiteRecovery({
  api,
  context,
  onResolved,
  runId,
}: {
  api: PlaytestApi;
  context: CandidateContextAuthority;
  onResolved(artifactId: string, sourceRunId: string): void;
  runId: string;
}) {
  const ownerKey = candidateContextKey(context);
  const deliveredRef = useRef<string | null>(null);
  const run = useQuery({
    queryFn: () => api.getRun(runId),
    queryKey: ["playtest", "run", runId],
    retry: false,
  });
  const result = useQuery({
    enabled: run.data?.status === "succeeded",
    queryFn: async () => ({
      ownerKey,
      suite: await loadDerivedTaskSuite(api, runId, run.data!, context),
    }),
    queryKey: ["playtest", "derived-task-suite", runId, run.data?.revision ?? null, ownerKey],
    retry: false,
  });

  useEffect(() => {
    if (result.data?.ownerKey !== ownerKey) return;
    const artifactId = result.data.suite.artifact.artifact_id;
    const deliveryKey = `${runId}\u0000${ownerKey}\u0000${artifactId}`;
    if (deliveredRef.current === deliveryKey) return;
    deliveredRef.current = deliveryKey;
    onResolved(artifactId, runId);
  }, [onResolved, ownerKey, result.data, runId]);

  if (run.data?.status !== "succeeded") return null;
  if (result.isPending) {
    return (
      <StatePanel
        description="正在从 RunResult 读取 primary TaskSuite 并重验候选绑定。"
        state="loading"
        title="正在闭合派生结果"
      />
    );
  }
  if (result.isError) {
    if (result.error instanceof ApiProblemError) return <ProblemPanel problem={result.error.problem} />;
    return (
      <StatePanel
        description={result.error.message}
        state="error"
        title="派生 TaskSuite authority 无法闭合"
      />
    );
  }
  return (
    <StatePanel
      description={`已从 RunResult 闭合并绑定 exact TaskSuite ${result.data.suite.artifact.artifact_id}。`}
      state="terminal"
      title="TaskSuite 派生完成"
    />
  );
}

function SuiteCard({
  disabled,
  onSelect,
  selected,
  suite,
}: {
  disabled: boolean;
  onSelect(): void;
  selected: boolean;
  suite: TaskSuiteArtifactView;
}) {
  return (
    <article className="gf-playtest__suite-card" data-selected={selected || undefined}>
      <header>
        <div>
          <p>Immutable TaskSuite</p>
          <code>{suite.artifact.artifact_id}</code>
        </div>
        <span>{suite.task_suite.episodes.length} episodes</span>
      </header>
      <dl>
        <div>
          <dt>Config</dt>
          <dd>{suite.task_suite.config_export_artifact_id}</dd>
        </div>
        <div>
          <dt>Environment</dt>
          <dd>{profileKey(suite.task_suite.environment_profile)}</dd>
        </div>
        <div>
          <dt>Derivation</dt>
          <dd>{profileKey(suite.task_suite.suite_profile)}</dd>
        </div>
        <div>
          <dt>Oracle registry</dt>
          <dd>v{suite.task_suite.completion_oracle_registry_ref.registry_version}</dd>
        </div>
      </dl>
      <button
        aria-label={selected ? "已选择" : `选择 ${suite.artifact.artifact_id}`}
        className={selected ? "gf-secondary-button" : "gf-primary-button"}
        disabled={disabled}
        onClick={onSelect}
        type="button"
      >
        {selected ? "已选择" : "选择此 TaskSuite"}
      </button>
    </article>
  );
}

function ConfigCandidateCard({ candidate, onSelect }: { candidate: ConfigCandidate; onSelect(): void }) {
  const generatedAt = candidate.createdAt?.replace("T", " ").replace("Z", " UTC") ?? "时间未记录";
  const candidateTitle = candidate.createdAt ? `内容候选 · ${candidate.createdAt.slice(11, 16)}` : "内容候选";
  return (
    <article className="gf-playtest__candidate-card">
      <header>
        <div>
          <p>可试玩配置</p>
          <h3>{candidateTitle}</h3>
        </div>
        <span>{profileKey(candidate.environmentProfile)}</span>
      </header>
      <dl>
        <div>
          <dt>生成时间</dt>
          <dd>{generatedAt}</dd>
        </div>
        <div>
          <dt>运行环境</dt>
          <dd>{candidate.envContractVersion}</dd>
        </div>
        <div>
          <dt>覆盖领域</dt>
          <dd>{candidate.domainIds.join("、")}</dd>
        </div>
      </dl>
      <details>
        <summary>查看精确绑定</summary>
        <dl>
          <div>
            <dt>配置</dt>
            <dd>{candidate.artifactId}</dd>
          </div>
          <div>
            <dt>内容预览</dt>
            <dd>{candidate.previewArtifactId}</dd>
          </div>
          <div>
            <dt>规则快照</dt>
            <dd>{candidate.constraintArtifactId}</dd>
          </div>
        </dl>
      </details>
      <button
        aria-label={`使用配置 ${candidate.artifactId}`}
        className="gf-primary-button"
        onClick={onSelect}
        type="button"
      >
        选择并准备 TaskSuite
      </button>
    </article>
  );
}

export function PlaytestPage({
  api = playtestApi,
  commandClient,
}: {
  api?: PlaytestApi;
  commandClient?: RunCommandClient;
}) {
  const [searchParams, setSearchParams] = useSearchParams();
  const sourceRunId = searchParams.get("sourceRun")?.trim() || null;
  const previewArtifactId = searchParams.get("preview")?.trim() || null;
  const configArtifactId = searchParams.get("config")?.trim() || null;
  const constraintArtifactId = searchParams.get("constraint")?.trim() || null;
  const selectedSuiteId = searchParams.get("suite")?.trim() || null;
  const deriveRunId = searchParams.get("deriveRun")?.trim() || null;
  const playtestRunId = searchParams.get("run")?.trim() || null;
  const deriveRequested = searchParams.get("action") === "derive";
  const routeOwnerKey = [
    previewArtifactId,
    configArtifactId,
    constraintArtifactId,
    selectedSuiteId,
    deriveRunId,
    playtestRunId,
  ].join("\u0000");
  const contextCount = [previewArtifactId, configArtifactId, constraintArtifactId].filter(Boolean).length;
  const contextIsPartial = contextCount > 0 && contextCount < 3;
  const showCandidateCatalog =
    contextCount === 0 && selectedSuiteId === null && deriveRunId === null && playtestRunId === null;
  const contextIds: CandidateContextIds | null =
    contextCount === 3
      ? {
          configArtifactId: configArtifactId!,
          constraintArtifactId: constraintArtifactId!,
          previewArtifactId: previewArtifactId!,
          sourceRunId,
        }
      : null;
  const context = useQuery({
    enabled: contextIds !== null,
    queryFn: () => loadCandidateContext(api, contextIds!),
    queryKey: [
      "playtest",
      "candidate-context",
      previewArtifactId,
      configArtifactId,
      constraintArtifactId,
      sourceRunId,
    ],
    retry: false,
  });
  const profiles = useProfileCatalog(api);
  const [derivationKey, setDerivationKey] = useState("");
  const [plannerKey, setPlannerKey] = useState("");
  const [deriveAttempt, setDeriveAttempt] = useState<DeriveAttempt | null>(null);
  const [launchAttempt, setLaunchAttempt] = useState<LaunchAttempt | null>(null);
  const [selectedEpisodeIds, setSelectedEpisodeIds] = useState<Set<string>>(new Set());
  const [maxSteps, setMaxSteps] = useState("1");
  const [seed, setSeed] = useState("1");
  const [mode, setMode] = useState<LlmExecutionMode>("record");
  const [interactionMode, setInteractionMode] = useState<InteractionMode>("autonomous");
  const [replaySourceRunId, setReplaySourceRunId] = useState("");
  const replayRuns = useQuery({
    enabled: mode === "replay",
    queryFn: () => collectReplaySourceRuns(api),
    queryKey: ["playtest", "replay-source-runs"],
    retry: false,
  });
  const [candidateReadGeneration, setCandidateReadGeneration] = useState(0);
  const [suiteReadGeneration, setSuiteReadGeneration] = useState(0);
  const [recoveredSuite, setRecoveredSuite] = useState<RecoveredSuiteChoice | null>(null);
  const mutationChainRef = useRef<"derive" | "launch" | null>(null);
  const currentSuiteOwnerRef = useRef<string | null>(null);
  const currentContextOwnerRef = useRef<string | null>(null);
  const latestSearchParamsRef = useRef(searchParams);
  const mountedRef = useRef(false);
  const routeGenerationRef = useRef({ generation: 0, ownerKey: routeOwnerKey });
  latestSearchParamsRef.current = searchParams;
  if (routeGenerationRef.current.ownerKey !== routeOwnerKey) {
    routeGenerationRef.current = {
      generation: routeGenerationRef.current.generation + 1,
      ownerKey: routeOwnerKey,
    };
  }

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      currentContextOwnerRef.current = null;
      currentSuiteOwnerRef.current = null;
    };
  }, []);

  const configCandidates = useInfiniteQuery({
    enabled: showCandidateCatalog,
    initialPageParam: { cursor: null, readSnapshotId: null } as ConfigCandidatePageParam,
    queryFn: ({ pageParam }) => loadConfigCandidatePage(api, pageParam),
    getNextPageParam: (lastPage, allPages): ConfigCandidatePageParam | undefined => {
      const next = cursorFromPage(lastPage.page);
      if (next === null) return undefined;
      if (allPages.length >= 256) fail("ConfigExport pagination exceeded its bounded page count.");
      if (allPages.some((loaded) => loaded.cursor === next)) {
        fail("ConfigExport pagination returned a cursor cycle.");
      }
      return { cursor: next, readSnapshotId: lastPage.page.read_snapshot_id };
    },
    queryKey: ["playtest", "config-candidates", candidateReadGeneration],
    retry: false,
  });

  const suiteList = useInfiniteQuery({
    enabled: !contextIsPartial && (contextIds === null || context.isSuccess),
    initialPageParam: { cursor: null, readSnapshotId: null } as TaskSuitePageParam,
    queryFn: ({ pageParam }) => {
      const authority = context.data;
      return loadTaskSuitePage(
        api,
        authority
          ? {
              config_artifact_id: authority.configArtifactId,
              constraint_artifact_id: authority.constraintArtifactId,
              environment_profile_id: authority.environmentProfile.profile_id,
              environment_profile_version: authority.environmentProfile.version,
              limit: 100,
            }
          : { limit: 100 },
        authority
          ? {
              configArtifactId: authority.configArtifactId,
              constraintSnapshotArtifactId: authority.constraintArtifactId,
              environmentProfile: authority.environmentProfile,
              sourcePreviewArtifactId: authority.previewArtifactId,
            }
          : {},
        pageParam,
      );
    },
    getNextPageParam: (lastPage, allPages): TaskSuitePageParam | undefined => {
      const next = cursorFromPage(lastPage.page);
      if (next === null) return undefined;
      if (allPages.length >= 256) fail("TaskSuite pagination exceeded its bounded page count.");
      if (allPages.some((loaded) => loaded.cursor === next)) {
        fail("TaskSuite pagination returned a cursor cycle.");
      }
      return { cursor: next, readSnapshotId: lastPage.page.read_snapshot_id };
    },
    queryKey: [
      "playtest",
      "task-suites",
      contextIsPartial ? "partial" : contextIds === null ? "browse" : "exact",
      suiteReadGeneration,
      context.data?.previewArtifactId ?? null,
      context.data?.configArtifactId ?? null,
      context.data?.constraintArtifactId ?? null,
      context.data?.environmentProfile.profile_id ?? null,
      context.data?.environmentProfile.version ?? null,
      context.data?.config.artifact.payload_hash ?? null,
    ],
    retry: false,
  });
  const selectedSuite = useQuery({
    enabled: !contextIsPartial && selectedSuiteId !== null && (contextIds === null || context.isSuccess),
    queryFn: () =>
      loadSelectedTaskSuite(
        api,
        selectedSuiteId!,
        context.data
          ? {
              configArtifactId: context.data.configArtifactId,
              constraintSnapshotArtifactId: context.data.constraintArtifactId,
              environmentProfile: context.data.environmentProfile,
              sourcePreviewArtifactId: context.data.previewArtifactId,
            }
          : {},
      ),
    queryKey: [
      "playtest",
      "task-suite",
      selectedSuiteId,
      context.data?.previewArtifactId ?? null,
      context.data?.configArtifactId ?? null,
      context.data?.constraintArtifactId ?? null,
      context.data?.environmentProfile.profile_id ?? null,
      context.data?.environmentProfile.version ?? null,
    ],
    retry: false,
  });

  const selectedSuiteOwnerKey =
    selectedSuite.data?.artifact.artifact_id === selectedSuiteId
      ? taskSuiteOwnerKey(selectedSuite.data)
      : null;
  currentSuiteOwnerRef.current = selectedSuiteOwnerKey;
  currentContextOwnerRef.current = context.data ? candidateContextKey(context.data) : null;

  const derivationOptions = useMemo(() => {
    if (!profiles.data) return [];
    if (!context.data) return profiles.data.derivations;
    return profiles.data.derivations.filter(
      (profile) =>
        sameProfile(profile.target_environment_profile, context.data.environmentProfile) &&
        profileCoversDomains(profile, context.data.requiredDomainIds),
    );
  }, [context.data, profiles.data]);
  const plannerOptions = useMemo(() => {
    if (!profiles.data || !selectedSuite.data) return [];
    const candidates =
      mode === "replay"
        ? [...profiles.data.activePlanners, ...profiles.data.replayOnlyPlanners]
        : profiles.data.activePlanners;
    const requiredDomainIds = taskSuiteDomainIds(selectedSuite.data);
    return candidates.filter((profile) => profileCoversDomains(profile, requiredDomainIds));
  }, [mode, profiles.data, selectedSuite.data]);

  useEffect(() => {
    if (!derivationOptions.some((profile) => profileKey(profile.profile) === derivationKey)) {
      setDerivationKey(derivationOptions[0] ? profileKey(derivationOptions[0].profile) : "");
    }
  }, [derivationKey, derivationOptions]);
  useEffect(() => {
    if (!plannerOptions.some((profile) => profileKey(profile.profile) === plannerKey)) {
      setPlannerKey(plannerOptions[0] ? profileKey(plannerOptions[0].profile) : "");
    }
  }, [plannerKey, plannerOptions]);

  const derivationProfile = derivationOptions.find(
    (profile) => profileKey(profile.profile) === derivationKey,
  );
  const plannerProfile = plannerOptions.find((profile) => profileKey(profile.profile) === plannerKey);
  const derivationBinding = useQuery({
    enabled: derivationProfile !== undefined,
    queryFn: () =>
      api.getTaskSuiteDerivationBinding(
        derivationProfile!.profile.profile_id,
        derivationProfile!.profile.version,
      ),
    queryKey: [
      "playtest",
      "task-suite-derivation-binding",
      derivationProfile?.profile.profile_id ?? null,
      derivationProfile?.profile.version ?? null,
    ],
    retry: false,
  });

  useEffect(() => {
    if (!selectedSuite.data) return;
    setSelectedEpisodeIds(new Set(selectedSuite.data.task_suite.episodes.map((item) => item.episode_id)));
    setMaxSteps(String(Math.min(...selectedSuite.data.task_suite.episodes.map((item) => item.step_budget))));
    setLaunchAttempt((current) => {
      if (current?.pending || isUnknownLaunchAttempt(current) || current?.accepted) return current;
      return null;
    });
  }, [selectedSuite.data]);

  function updateSearch(values: Record<string, string | null>, options: { replace?: boolean } = {}) {
    if (!mountedRef.current) return;
    const next = new URLSearchParams(latestSearchParamsRef.current);
    for (const [key, value] of Object.entries(values)) {
      if (value === null) next.delete(key);
      else next.set(key, value);
    }
    latestSearchParamsRef.current = next;
    setSearchParams(next, options);
  }

  function routeIsCurrent(generation: number): boolean {
    return mountedRef.current && routeGenerationRef.current.generation === generation;
  }

  function requestExactDerivation() {
    const suite = selectedSuite.data;
    updateSearch({
      action: "derive",
      config: context.data?.configArtifactId ?? suite?.task_suite.config_export_artifact_id ?? null,
      constraint:
        context.data?.constraintArtifactId ?? suite?.task_suite.constraint_snapshot_artifact_id ?? null,
      preview: context.data?.previewArtifactId ?? suite?.task_suite.source_preview_artifact_id ?? null,
      run: null,
    });
    window.setTimeout(() => {
      document.getElementById("playtest-derive")?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    }, 0);
  }

  async function submitDerive() {
    if (
      mutationChainRef.current !== null ||
      launchAttempt?.pending === true ||
      isUnknownLaunchAttempt(launchAttempt)
    ) {
      return;
    }
    if (isUnknownDeriveAttempt(deriveAttempt)) {
      const frozen = deriveAttempt;
      const routeGeneration = routeGenerationRef.current.generation;
      mutationChainRef.current = "derive";
      setDeriveAttempt({ ...frozen, error: null, pending: true });
      try {
        const accepted = await api.deriveTaskSuite(frozen.request, frozen.intent);
        if (!mountedRef.current) return;
        setDeriveAttempt({ ...frozen, accepted, error: null, pending: false });
        if (routeIsCurrent(routeGeneration) && currentContextOwnerRef.current === frozen.ownerKey) {
          updateSearch({ deriveRun: accepted.run_id, run: null });
        }
      } catch (error) {
        if (mountedRef.current) {
          setDeriveAttempt({
            ...frozen,
            accepted: null,
            error: normalizedError(error),
            pending: false,
          });
        }
      } finally {
        if (mutationChainRef.current === "derive") mutationChainRef.current = null;
      }
      return;
    }
    if (!context.data || !derivationProfile || !derivationBinding.data) return;
    const ownerKey = candidateContextKey(context.data);
    const binding = derivationBinding.data;
    if (
      !sameProfile(binding.derivation_profile, derivationProfile.profile) ||
      binding.profile_payload_hash !== derivationProfile.profile_payload_hash ||
      binding.run_kind.kind !== "task_suite.derive" ||
      binding.run_kind.version !== 1 ||
      !sameProfile(binding.target_environment_profile, context.data.environmentProfile)
    ) {
      setDeriveAttempt({
        accepted: null,
        error: new PlaytestPageAuthorityError(
          "Derivation binding differs from the selected profile, run kind, or exact environment.",
        ),
        intent: null,
        ownerKey,
        pending: false,
        request: null,
      });
      return;
    }
    const request: TaskSuiteDeriveRequest = {
      params: {
        completion_oracle_registry_ref: binding.completion_oracle_registry_ref,
        config_artifact_id: context.data.configArtifactId,
        constraint_snapshot_artifact_id: context.data.constraintArtifactId,
        derivation_profile: binding.derivation_profile,
        environment_profile: binding.target_environment_profile,
        schema_version: "task-suite-derive@1",
        source_preview_artifact_id: context.data.previewArtifactId,
      },
      request_schema_version: "task-suite-derive-request@1",
    };
    const intent = createMutationIntent();
    const routeGeneration = routeGenerationRef.current.generation;
    mutationChainRef.current = "derive";
    setDeriveAttempt({ accepted: null, error: null, intent, ownerKey, pending: true, request });
    try {
      const accepted = await api.deriveTaskSuite(request, intent);
      if (!mountedRef.current) return;
      setDeriveAttempt({ accepted, error: null, intent, ownerKey, pending: false, request });
      if (routeIsCurrent(routeGeneration) && currentContextOwnerRef.current === ownerKey) {
        updateSearch({ deriveRun: accepted.run_id, run: null });
      }
    } catch (error) {
      if (mountedRef.current) {
        setDeriveAttempt({
          accepted: null,
          error: normalizedError(error),
          intent,
          ownerKey,
          pending: false,
          request,
        });
      }
    } finally {
      if (mutationChainRef.current === "derive") mutationChainRef.current = null;
    }
  }

  async function submitPlaytest() {
    if (
      mutationChainRef.current !== null ||
      deriveAttempt?.pending === true ||
      isUnknownDeriveAttempt(deriveAttempt)
    ) {
      return;
    }
    if (isUnknownLaunchAttempt(launchAttempt)) {
      const frozen = launchAttempt;
      const routeGeneration = routeGenerationRef.current.generation;
      mutationChainRef.current = "launch";
      setLaunchAttempt({ ...frozen, error: null, pending: true });
      try {
        const accepted = await api.runPlaytest(frozen.request, frozen.intent);
        if (!mountedRef.current) return;
        setLaunchAttempt({ ...frozen, accepted, error: null, pending: false });
        if (routeIsCurrent(routeGeneration) && currentSuiteOwnerRef.current === frozen.ownerKey) {
          updateSearch({ run: accepted.run_id });
        }
      } catch (error) {
        if (mountedRef.current) {
          setLaunchAttempt({
            ...frozen,
            accepted: null,
            error: normalizedError(error),
            pending: false,
          });
        }
      } finally {
        if (mutationChainRef.current === "launch") mutationChainRef.current = null;
      }
      return;
    }
    const exactSuite = selectedSuite.data;
    if (!exactSuite || !plannerProfile) return;
    const ownerKey = taskSuiteOwnerKey(exactSuite);
    const selected = exactSuite.task_suite.episodes
      .filter((episode) => selectedEpisodeIds.has(episode.episode_id))
      .sort((left, right) => left.episode_id.localeCompare(right.episode_id));
    const parsedSteps = Number(maxSteps);
    const parsedSeed = Number(seed);
    if (
      selected.length === 0 ||
      !Number.isSafeInteger(parsedSteps) ||
      parsedSteps < 1 ||
      parsedSteps > Math.min(...selected.map((episode) => episode.step_budget)) ||
      seed.trim() === "" ||
      !Number.isSafeInteger(parsedSeed) ||
      parsedSeed < 0 ||
      (mode === "replay" && replayRuns.data?.some((run) => run.run_id === replaySourceRunId.trim()) !== true)
    ) {
      setLaunchAttempt({
        accepted: null,
        error: new PlaytestPageAuthorityError(
          "Episode subset, step budget, seed, or replay source is invalid.",
        ),
        intent: null,
        ownerKey,
        pending: false,
        request: null,
      });
      return;
    }
    const prospective: ProspectivePlaytestRunRequest = {
      cassette_artifact_id: null,
      execution_version_plan: null,
      llm_execution_mode: mode,
      params: {
        config_artifact_id: exactSuite.task_suite.config_export_artifact_id,
        constraint_snapshot_artifact_id: exactSuite.task_suite.constraint_snapshot_artifact_id,
        environment_profile: exactSuite.task_suite.environment_profile,
        episodes: selected.map((episode) => ({
          episode_id: episode.episode_id,
          scenario_spec_artifact_id: episode.scenario_spec_artifact_id,
        })),
        interaction_mode: interactionMode,
        max_steps_per_episode: parsedSteps,
        planner_policy: plannerProfile.profile,
        schema_version: "playtest-run@1",
        task_suite_artifact_id: exactSuite.artifact.artifact_id,
      },
      request_schema_version: "playtest-run-request@1",
      seed: parsedSeed,
    };
    const resolverRequest: ExecutionOptionResolveRequest = {
      llm_execution_mode: mode,
      prospective_request: prospective,
      replay_source_run_id: mode === "replay" ? replaySourceRunId.trim() : null,
      request_schema_version: "execution-option-resolve-request@1",
      resource_operation_id: "run_playtest_api_v1_playtest_run_post",
      run_kind: { kind: "playtest.run", version: 1 },
    };
    const routeGeneration = routeGenerationRef.current.generation;
    mutationChainRef.current = "launch";
    setLaunchAttempt({
      accepted: null,
      error: null,
      intent: null,
      ownerKey,
      pending: true,
      request: null,
    });
    let option: Awaited<ReturnType<PlaytestApi["resolveExecutionOption"]>>;
    try {
      option = await api.resolveExecutionOption(resolverRequest);
      if (
        option.resource_operation_id !== resolverRequest.resource_operation_id ||
        option.run_kind.kind !== "playtest.run" ||
        option.run_kind.version !== 1 ||
        option.llm_execution_mode !== mode ||
        option.source_run_id !== resolverRequest.replay_source_run_id ||
        option.domain_scope.domain_ids.join("\u0000") !== taskSuiteDomainIds(exactSuite).join("\u0000")
      ) {
        fail("Resolved execution option differs from the prospective Playtest request.");
      }
    } catch (error) {
      const normalized = normalizedError(error);
      if (mountedRef.current) {
        setLaunchAttempt({
          accepted: null,
          error:
            normalized instanceof ApiProblemError || normalized instanceof ReauthenticationRequiredError
              ? normalized
              : new ExecutionOptionResolutionError(normalized.message),
          intent: null,
          ownerKey,
          pending: false,
          request: null,
        });
      }
      if (mutationChainRef.current === "launch") mutationChainRef.current = null;
      return;
    }
    if (!routeIsCurrent(routeGeneration) || currentSuiteOwnerRef.current !== ownerKey) {
      if (mountedRef.current) {
        setLaunchAttempt({
          accepted: null,
          error: new PlaytestPageAuthorityError(
            "The exact TaskSuite owner changed while execution options were resolving.",
          ),
          intent: null,
          ownerKey,
          pending: false,
          request: null,
        });
      }
      if (mutationChainRef.current === "launch") mutationChainRef.current = null;
      return;
    }
    const request: PlaytestRunRequest = {
      ...prospective,
      cassette_artifact_id: option.cassette_artifact_id,
      execution_version_plan: option.execution_version_plan,
    };
    const intent = createMutationIntent();
    setLaunchAttempt({ accepted: null, error: null, intent, ownerKey, pending: true, request });
    try {
      const accepted = await api.runPlaytest(request, intent);
      if (!mountedRef.current) return;
      setLaunchAttempt({ accepted, error: null, intent, ownerKey, pending: false, request });
      if (routeIsCurrent(routeGeneration) && currentSuiteOwnerRef.current === ownerKey) {
        updateSearch({ run: accepted.run_id });
      }
    } catch (error) {
      if (mountedRef.current) {
        setLaunchAttempt({
          accepted: null,
          error: normalizedError(error),
          intent,
          ownerKey,
          pending: false,
          request,
        });
      }
    } finally {
      if (mutationChainRef.current === "launch") mutationChainRef.current = null;
    }
  }

  const suiteProjection = useMemo(() => {
    const items = suiteList.data?.pages.flatMap((loaded) => loaded.page.items) ?? [];
    const artifactIds = new Set<string>();
    for (const item of items) {
      if (artifactIds.has(item.artifact.artifact_id)) {
        return {
          error: new PlaytestPageAuthorityError(
            "TaskSuite pagination returned a duplicate immutable Artifact.",
          ),
          items: [] as TaskSuiteArtifactView[],
        };
      }
      artifactIds.add(item.artifact.artifact_id);
    }
    return { error: null, items };
  }, [suiteList.data]);
  const configCandidateProjection = useMemo(() => {
    const items = configCandidates.data?.pages.flatMap((loaded) => loaded.candidates) ?? [];
    const artifactIds = new Set<string>();
    for (const item of items) {
      if (artifactIds.has(item.artifactId)) {
        return {
          error: new PlaytestPageAuthorityError(
            "ConfigExport pagination returned a duplicate immutable Artifact.",
          ),
          items: [] as ConfigCandidate[],
        };
      }
      artifactIds.add(item.artifactId);
    }
    return { error: null, items };
  }, [configCandidates.data]);
  const suiteListError = suiteList.error ?? suiteProjection.error;
  const configCandidateError = configCandidates.error ?? configCandidateProjection.error;
  const contextError = contextIsPartial
    ? new PlaytestPageAuthorityError("preview, config, and constraint navigation IDs must be complete.")
    : context.error;
  const suites = suiteProjection.items;
  const candidateConfigs = configCandidateProjection.items;
  const deriveOutcomeUnknown = isUnknownDeriveAttempt(deriveAttempt);
  const launchOutcomeUnknown = isUnknownLaunchAttempt(launchAttempt);
  const deriveOperationLocked = deriveAttempt?.pending === true || deriveOutcomeUnknown;
  const launchOperationLocked = launchAttempt?.pending === true || launchOutcomeUnknown;
  const launchControlsLocked = launchOperationLocked || deriveOperationLocked;
  const deriveControlsLocked = deriveOperationLocked || launchOperationLocked;
  const detachedAcceptedDeriveRunId =
    deriveAttempt?.accepted && deriveAttempt.accepted.run_id !== deriveRunId
      ? deriveAttempt.accepted.run_id
      : null;
  const detachedAcceptedRunId =
    launchAttempt?.accepted &&
    (launchAttempt.ownerKey !== selectedSuiteOwnerKey || launchAttempt.accepted.run_id !== playtestRunId)
      ? launchAttempt.accepted.run_id
      : null;

  return (
    <div className="gf-page gf-playtest" data-layout="editorial-playtest">
      <header className="gf-playtest__hero">
        <div>
          <p className="gf-playtest__kicker">Deterministic oracle · Agent execution · exact trace</p>
          <h1>自动试玩</h1>
          <p>
            从不可变 TaskSuite 选择明确 episode，让真实 Playtest Agent 运行；Run 成功与任务通过始终分开陈述。
          </p>
        </div>
        <div className="gf-playtest__hero-mark" aria-hidden="true">
          <Gamepad2 size={30} />
          <span>PLAYTEST</span>
        </div>
      </header>

      {contextError ? (
        <StatePanel description={contextError.message} state="error" title="候选绑定无法闭合" />
      ) : contextIds && context.isPending ? (
        <StatePanel
          description="正在通过专用 reads 重验 preview、config 与 constraint。"
          state="loading"
          title="正在闭合候选绑定"
        />
      ) : context.data ? (
        <section className="gf-playtest__context" aria-labelledby="playtest-context-title" role="region">
          <header>
            <GitBranch aria-hidden="true" size={22} />
            <div>
              <h2 id="playtest-context-title">候选绑定账本</h2>
              <p>URL 只携带导航候选；以下身份已由专用资源读取与 ConfigExport payload 对拍。</p>
            </div>
          </header>
          <ol>
            <li>
              <span>Preview</span>
              <code>{context.data.previewArtifactId}</code>
            </li>
            <li>
              <span>Config</span>
              <code>{context.data.configArtifactId}</code>
            </li>
            <li>
              <span>Constraint</span>
              <code>{context.data.constraintArtifactId}</code>
            </li>
            <li>
              <span>Environment</span>
              <code>{profileKey(context.data.environmentProfile)}</code>
            </li>
          </ol>
          {context.data.sourceRunId && (
            <p>
              导航来源：
              <a href={`/runs/${encodeURIComponent(context.data.sourceRunId)}`}>{context.data.sourceRunId}</a>
              ；不作为 TaskSuite authority。
            </p>
          )}
        </section>
      ) : showCandidateCatalog ? (
        <section
          aria-labelledby="playtest-candidate-catalog-title"
          className="gf-playtest__candidate-catalog"
          role="region"
        >
          <header>
            <Gamepad2 aria-hidden="true" size={22} />
            <div>
              <p className="gf-playtest__kicker">Same-tab candidate selection</p>
              <h2 id="playtest-candidate-catalog-title">选择待试玩候选</h2>
              <p>直接选择已生成的配置；页面会在当前标签页重验内容预览、规则与运行环境。</p>
            </div>
          </header>
          {configCandidates.isPending ? (
            <StatePanel
              description="正在读取可用于自动试玩的配置目录。"
              state="loading"
              title="正在发现候选配置"
            />
          ) : configCandidateError ? (
            configCandidateError instanceof CursorExpiredError ? (
              <StatePanel
                action={
                  <button
                    className="gf-secondary-button"
                    onClick={() => setCandidateReadGeneration((current) => current + 1)}
                    type="button"
                  >
                    从第一页重新读取
                  </button>
                }
                description="候选配置 cursor 已过期；页面没有静默跳过缺失页。"
                state="error"
                title="候选配置目录游标已过期"
              />
            ) : configCandidateError instanceof ApiProblemError ? (
              <ProblemPanel problem={configCandidateError.problem} />
            ) : (
              <StatePanel
                description={configCandidateError.message || "候选配置目录读取失败；未猜测或回退到最新配置。"}
                state="error"
                title="无法发现候选配置"
              />
            )
          ) : candidateConfigs.length === 0 ? (
            <StatePanel
              action={
                <a className="gf-secondary-button" href="/generation">
                  前往内容生成
                </a>
              }
              description="当前没有可用于派生 TaskSuite 的 ConfigExport；请先生成一个候选。"
              state="empty"
              title="暂无待试玩候选"
            />
          ) : (
            <>
              <div className="gf-playtest__candidate-grid">
                {candidateConfigs.map((candidate) => (
                  <ConfigCandidateCard
                    candidate={candidate}
                    key={candidate.artifactId}
                    onSelect={() => {
                      setRecoveredSuite(null);
                      updateSearch({
                        action: "derive",
                        config: candidate.artifactId,
                        constraint: candidate.constraintArtifactId,
                        deriveRun: null,
                        preview: candidate.previewArtifactId,
                        run: null,
                        sourceRun: null,
                        suite: null,
                      });
                    }}
                  />
                ))}
              </div>
              {configCandidates.hasNextPage && (
                <div className="gf-playtest__candidate-pagination">
                  <button
                    className="gf-secondary-button"
                    disabled={configCandidates.isFetchingNextPage}
                    onClick={() => void configCandidates.fetchNextPage()}
                    type="button"
                  >
                    {configCandidates.isFetchingNextPage ? "正在加载下一页…" : "加载更多候选配置"}
                  </button>
                  <span>已载入 {candidateConfigs.length} 个可试玩配置</span>
                </div>
              )}
            </>
          )}
        </section>
      ) : (
        <StatePanel
          description="正在按已选择的 TaskSuite 或 Run 恢复工作区；不会从未绑定的 ConfigExport 猜测候选。"
          state="loading"
          title="正在恢复自动试玩上下文"
        />
      )}

      {!contextIsPartial && profiles.isError && (
        <StatePanel
          action={
            <button className="gf-secondary-button" onClick={() => void profiles.refetch()} type="button">
              {profiles.error instanceof CursorExpiredError
                ? "从第一页重新读取 profile 目录"
                : "重试读取 profile 目录"}
            </button>
          }
          description={
            profiles.error instanceof ApiProblemError
              ? profiles.error.problem.detail
              : profiles.error.message || "Execution profile 目录读取失败。"
          }
          state="error"
          title={
            profiles.error instanceof CursorExpiredError ? "Profile 目录游标已过期" : "Profile 目录不可用"
          }
        />
      )}

      <section
        aria-labelledby="playtest-suite-ledger-title"
        className="gf-playtest__suite-ledger"
        data-standalone={context.data ? undefined : "true"}
      >
        <header>
          <Boxes aria-hidden="true" size={22} />
          <div>
            <p className="gf-playtest__kicker">Immutable suite ledger</p>
            <h2 id="playtest-suite-ledger-title">TaskSuite 发现</h2>
          </div>
        </header>
        {contextIsPartial ? (
          <StatePanel
            description="补全 preview、config 与 constraint 后才会读取或展示 TaskSuite。"
            state="error"
            title="TaskSuite authority 已阻断"
          />
        ) : suiteList.isPending ? (
          <StatePanel
            description="正在读取服务端精确筛选的 TaskSuite。"
            state="loading"
            title="正在发现 TaskSuite"
          />
        ) : suiteListError ? (
          suiteListError instanceof CursorExpiredError ? (
            <StatePanel
              action={
                <button
                  className="gf-secondary-button"
                  onClick={() => setSuiteReadGeneration((current) => current + 1)}
                  type="button"
                >
                  从第一页重新读取
                </button>
              }
              description="TaskSuite cursor 已过期；页面没有静默跳过缺失页。"
              state="error"
              title="TaskSuite 目录游标已过期"
            />
          ) : suiteListError instanceof ApiProblemError ? (
            <ProblemPanel problem={suiteListError.problem} />
          ) : (
            <StatePanel
              description={suiteListError.message || "TaskSuite 目录读取失败；未使用客户端过滤替代。"}
              state="error"
              title="无法发现 TaskSuite"
            />
          )
        ) : suites.length === 0 ? (
          <StatePanel
            action={
              context.data ? (
                <button className="gf-secondary-button" onClick={requestExactDerivation} type="button">
                  前往派生设置
                </button>
              ) : undefined
            }
            description="当前 exact config/constraint/environment 下没有已派生 TaskSuite。"
            state="empty"
            title="没有匹配的 TaskSuite"
          />
        ) : (
          <>
            <div className="gf-playtest__suite-grid">
              {suites.map((item) => (
                <SuiteCard
                  disabled={launchControlsLocked}
                  key={item.artifact.artifact_id}
                  onSelect={() => {
                    setRecoveredSuite(null);
                    updateSearch({
                      deriveRun: null,
                      run: null,
                      suite: item.artifact.artifact_id,
                    });
                  }}
                  selected={selectedSuiteId === item.artifact.artifact_id}
                  suite={item}
                />
              ))}
            </div>
            {suiteList.hasNextPage && (
              <div className="gf-playtest__suite-pagination">
                <button
                  className="gf-secondary-button"
                  disabled={suiteList.isFetchingNextPage}
                  onClick={() => void suiteList.fetchNextPage()}
                  type="button"
                >
                  {suiteList.isFetchingNextPage ? "正在加载下一页…" : "加载更多 TaskSuite"}
                </button>
                <span>已载入 {suites.length} 个 immutable suites</span>
              </div>
            )}
          </>
        )}
      </section>

      {context.data && (
        <section
          className="gf-playtest__derive"
          aria-labelledby="playtest-derive-title"
          data-requested={deriveRequested || undefined}
          id="playtest-derive"
        >
          <header>
            <FlaskConical aria-hidden="true" size={22} />
            <div>
              <p className="gf-playtest__kicker">Exact deterministic derivation</p>
              <h2 id="playtest-derive-title">派生 TaskSuite</h2>
            </div>
          </header>
          {deriveRequested && (
            <p className="gf-playtest__derive-notice" role="status">
              已切换到显式重新派生；确认 profile 后提交新的 TaskSuite Run，旧 suite 不会被静默复用。
            </p>
          )}
          {!deriveOutcomeUnknown && profiles.isPending ? (
            <p role="status">正在读取派生 profile…</p>
          ) : !deriveOutcomeUnknown && profiles.isError ? (
            <p role="status">请先使用上方操作恢复 execution profile 目录。</p>
          ) : !deriveOutcomeUnknown && derivationOptions.length === 0 ? (
            <p role="alert">没有覆盖 exact candidate domain 的 active derivation profile。</p>
          ) : (
            <>
              <label>
                <span>TaskSuite derivation profile</span>
                <select
                  disabled={deriveControlsLocked}
                  value={derivationKey}
                  onChange={(event) => setDerivationKey(event.target.value)}
                >
                  {derivationOptions.map((profile) => (
                    <option key={profileKey(profile.profile)} value={profileKey(profile.profile)}>
                      {profileKey(profile.profile)} · {profile.domain_scope.domain_ids.join(", ")}
                    </option>
                  ))}
                </select>
              </label>
              {derivationBinding.isPending ? (
                <StatePanel
                  description="正在读取所选 profile 的冻结派生 binding。"
                  state="loading"
                  title="正在读取派生 binding"
                />
              ) : derivationBinding.isError ? (
                <StatePanel
                  action={
                    <button
                      className="gf-secondary-button"
                      onClick={() => void derivationBinding.refetch()}
                      type="button"
                    >
                      重试读取派生 binding
                    </button>
                  }
                  description={
                    derivationBinding.error instanceof ApiProblemError
                      ? derivationBinding.error.problem.detail
                      : derivationBinding.error.message || "派生 binding 读取失败。"
                  }
                  state="error"
                  title="派生 binding 不可用"
                />
              ) : derivationBinding.data ? (
                <dl className="gf-playtest__binding">
                  <div>
                    <dt>Oracle registry</dt>
                    <dd>
                      v{derivationBinding.data.completion_oracle_registry_ref.registry_version} ·{" "}
                      {derivationBinding.data.completion_oracle_registry_ref.digest}
                    </dd>
                  </div>
                  <div>
                    <dt>Max scenarios</dt>
                    <dd>{derivationBinding.data.max_scenarios}</dd>
                  </div>
                  <div>
                    <dt>Prepared bytes</dt>
                    <dd>{derivationBinding.data.max_total_prepared_artifact_bytes}</dd>
                  </div>
                </dl>
              ) : null}
              <button
                className="gf-primary-button"
                disabled={
                  deriveAttempt?.pending ||
                  launchOperationLocked ||
                  (!derivationBinding.data && !deriveOutcomeUnknown)
                }
                onClick={() => void submitDerive()}
                type="button"
              >
                {deriveAttempt?.pending
                  ? "正在提交派生 Run…"
                  : deriveOutcomeUnknown
                    ? "重试同一派生 intent"
                    : "派生 exact TaskSuite"}
              </button>
              {deriveOutcomeUnknown && (
                <p className="gf-playtest__frozen-attempt" role="status">
                  结果未知；派生请求与 Idempotency-Key 已冻结，只能重试同一 intent。
                </p>
              )}
              <MutationError error={deriveAttempt?.error ?? null} />
            </>
          )}
        </section>
      )}

      {detachedAcceptedDeriveRunId && (
        <StatePanel
          action={
            <a href={`/runs/${encodeURIComponent(detachedAcceptedDeriveRunId)}`}>查看 accepted 派生 Run</a>
          }
          description="派生 Run 已被服务端接受，但候选上下文已变化；页面没有把它挂到新的 owner。"
          state="terminal"
          title="TaskSuite 派生 Run 已接受"
        />
      )}

      {!contextIsPartial && deriveRunId && (
        <>
          <TrackedRun
            api={api}
            commandClient={commandClient}
            label="TaskSuite 派生 Run"
            runId={deriveRunId}
          />
          {context.data && (
            <DerivedSuiteRecovery
              api={api}
              context={context.data}
              onResolved={(artifactId, sourceRunId) => {
                if (artifactId !== selectedSuiteId) {
                  if (
                    latestSearchParamsRef.current.get("deriveRun") !== sourceRunId ||
                    deriveOperationLocked ||
                    launchOperationLocked ||
                    playtestRunId !== null ||
                    selectedSuiteId !== null
                  ) {
                    setRecoveredSuite({ artifactId, sourceRunId });
                    return;
                  }
                  setDeriveAttempt((current) => (current?.accepted?.run_id === sourceRunId ? null : current));
                  updateSearch(
                    {
                      action: null,
                      deriveRun: null,
                      run: null,
                      suite: artifactId,
                    },
                    { replace: true },
                  );
                  setRecoveredSuite(null);
                  setSuiteReadGeneration((current) => current + 1);
                }
              }}
              runId={deriveRunId}
            />
          )}
        </>
      )}

      {recoveredSuite && (
        <StatePanel
          action={
            <button
              className="gf-secondary-button"
              disabled={deriveOperationLocked || launchOperationLocked}
              onClick={() => {
                const consumesTrackedRun =
                  latestSearchParamsRef.current.get("deriveRun") === recoveredSuite.sourceRunId;
                setDeriveAttempt((current) =>
                  current?.accepted?.run_id === recoveredSuite.sourceRunId ? null : current,
                );
                updateSearch({
                  action: null,
                  ...(consumesTrackedRun ? { deriveRun: null } : {}),
                  run: null,
                  suite: recoveredSuite.artifactId,
                });
                setRecoveredSuite(null);
                setSuiteReadGeneration((current) => current + 1);
              }}
              type="button"
            >
              选择新派生的 {recoveredSuite.artifactId}
            </button>
          }
          description="新派生的 TaskSuite 已就绪；当前 launch owner 不会被自动替换。"
          state="terminal"
          title="等待显式切换 TaskSuite"
        />
      )}

      {!contextIsPartial && selectedSuiteId && selectedSuite.isPending && (
        <StatePanel
          description="正在读取选中的 immutable TaskSuite。"
          state="loading"
          title="正在准备 launch docket"
        />
      )}
      {!contextIsPartial &&
        selectedSuite.isError &&
        (selectedSuite.error instanceof ApiProblemError ? (
          <ProblemPanel problem={selectedSuite.error.problem} />
        ) : (
          <StatePanel
            description="选中的 TaskSuite 无法读取。"
            state="error"
            title="TaskSuite authority 不可用"
          />
        ))}
      {!contextIsPartial && selectedSuite.data && (
        <section className="gf-playtest__launch" aria-labelledby="playtest-launch-title" role="region">
          <header>
            <ListChecks aria-hidden="true" size={22} />
            <div>
              <p className="gf-playtest__kicker">Explicit episode selection</p>
              <h2 id="playtest-launch-title">Playtest launch docket</h2>
              <code>{selectedSuite.data.artifact.artifact_id}</code>
            </div>
          </header>
          {profiles.isSuccess && plannerOptions.length === 0 && !launchOutcomeUnknown && (
            <StatePanel
              description={`没有 ${mode} 生命周期 profile 覆盖 exact TaskSuite domain：${taskSuiteDomainIds(selectedSuite.data).join(", ")}。`}
              state="error"
              title="没有兼容的 Playtest planner"
            />
          )}
          <fieldset disabled={launchControlsLocked}>
            <legend>Exact episode subset</legend>
            {selectedSuite.data.task_suite.episodes.map((episode) => (
              <label key={episode.episode_id}>
                <input
                  checked={selectedEpisodeIds.has(episode.episode_id)}
                  onChange={(event) => {
                    setSelectedEpisodeIds((current) => {
                      const next = new Set(current);
                      if (event.target.checked) next.add(episode.episode_id);
                      else next.delete(episode.episode_id);
                      return next;
                    });
                    setLaunchAttempt(null);
                  }}
                  type="checkbox"
                />
                <span>
                  {episode.episode_id} · 最多 {episode.step_budget} 步
                </span>
              </label>
            ))}
          </fieldset>
          <div className="gf-playtest__launch-grid">
            <label>
              <span>Planner profile</span>
              <select
                disabled={launchControlsLocked}
                value={plannerKey}
                onChange={(event) => setPlannerKey(event.target.value)}
              >
                {plannerOptions.map((profile) => (
                  <option key={profileKey(profile.profile)} value={profileKey(profile.profile)}>
                    {profileKey(profile.profile)} · {profile.status} ·{" "}
                    {profile.domain_scope.domain_ids.join(", ")}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>LLM execution mode</span>
              <select
                disabled={launchControlsLocked}
                value={mode}
                onChange={(event) => {
                  const next = event.target.value as LlmExecutionMode;
                  setMode(next);
                  if (next !== "replay") setReplaySourceRunId("");
                }}
              >
                <option value="record">record</option>
                <option value="live">live</option>
                <option value="replay">replay</option>
              </select>
            </label>
            <label>
              <span>Interaction mode</span>
              <select
                disabled={launchControlsLocked}
                value={interactionMode}
                onChange={(event) => setInteractionMode(event.target.value as InteractionMode)}
              >
                <option value="autonomous">autonomous</option>
                <option value="bounded_choice">bounded_choice</option>
              </select>
            </label>
            <label>
              <span>Seed</span>
              <input
                disabled={launchControlsLocked}
                min="0"
                onChange={(event) => setSeed(event.target.value)}
                step="1"
                type="number"
                value={seed}
              />
            </label>
            <label>
              <span>每 episode 最大步数</span>
              <input
                aria-label="每 episode 最大步数"
                disabled={launchControlsLocked}
                min="1"
                onChange={(event) => setMaxSteps(event.target.value)}
                step="1"
                type="number"
                value={maxSteps}
              />
            </label>
            {mode === "replay" && (
              <label>
                <span>Replay source Run</span>
                <select
                  disabled={launchControlsLocked}
                  onChange={(event) => setReplaySourceRunId(event.target.value)}
                  value={replaySourceRunId}
                >
                  <option value="">
                    {replayRuns.isPending ? "正在读取已完成运行…" : "请选择一个已完成的回放来源"}
                  </option>
                  {replayRuns.data?.map((run) => (
                    <option key={run.run_id} value={run.run_id}>
                      {replayRunLabel(run)}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
          <button
            className="gf-primary-button"
            disabled={launchAttempt?.pending || (!plannerProfile && !launchOutcomeUnknown)}
            onClick={() => void submitPlaytest()}
            type="button"
          >
            {launchAttempt?.pending
              ? "正在解析并提交…"
              : launchOutcomeUnknown
                ? "重试同一 Playtest intent"
                : "解析并启动 Playtest"}
          </button>
          {launchOutcomeUnknown && (
            <p className="gf-playtest__frozen-attempt" role="status">
              结果未知；exact episode subset、execution plan 与 Idempotency-Key 已冻结，只能重试同一 intent。
            </p>
          )}
          <MutationError error={launchAttempt?.error ?? null} />
          {launchAttempt?.error instanceof ApiProblemError &&
            launchAttempt.error.problem.code === "stale_task_suite" && (
              <button className="gf-secondary-button" onClick={requestExactDerivation} type="button">
                按当前候选重新派生 TaskSuite
              </button>
            )}
        </section>
      )}

      {detachedAcceptedRunId && (
        <StatePanel
          action={<a href={`/runs/${encodeURIComponent(detachedAcceptedRunId)}`}>查看 accepted Run</a>}
          description="Run 已被服务端接受，但当前路由 owner 已变化；页面没有把它挂到新的 suite 或 Run。"
          state="terminal"
          title="Playtest Run 已接受"
        />
      )}

      {!contextIsPartial && playtestRunId && (
        <>
          <TrackedRun api={api} commandClient={commandClient} label="Playtest Run" runId={playtestRunId} />
          <PlaytestTerminalPanel
            api={api}
            request={launchAttempt?.accepted?.run_id === playtestRunId ? launchAttempt.request : null}
            runId={playtestRunId}
            suite={selectedSuite.data ?? null}
          />
        </>
      )}

      <footer className="gf-playtest__principle">
        <ShieldCheck aria-hidden="true" size={18} />
        <span>Completion oracle 与 trace 由确定性契约判定；LLM Agent 只负责有边界地执行。</span>
        <Route aria-hidden="true" size={18} />
      </footer>
    </div>
  );
}
