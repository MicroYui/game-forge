import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Bot, Database, GitBranch, PlayCircle, ShieldCheck } from "lucide-react";
import { type Dispatch, type SetStateAction, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { createMutationIntent, ReauthenticationRequiredError, type MutationIntent } from "../../api/csrf";
import type { RunEvent } from "../../api/generated/sse-run-event-v1";
import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import type { RunEventStreamState } from "../../api/sse";
import { ReauthenticationLink } from "../../app/ReauthenticationLink";
import { SnapshotDiffView } from "../../components/diff";
import { EvidenceSections } from "../../components/evidence";
import { RunProgress, type RunEventItem } from "../../components/run-progress";
import { ProblemPanel, StatePanel } from "../../components/ui";
import { replaySourceOptionLabel, type ReplaySourceRun } from "../runs/replaySources";
import {
  generationApi,
  type ExecutionOptionResolveRequest,
  type ExecutionProfilePage,
  type GenerationApi,
  type GenerationEventStreamHandle,
  type GenerationProposeRequest,
  type ProspectiveGenerationProposeRequest,
  type RunAccepted,
  type RunView,
  type SpecView,
} from "./api";
import type {
  FailedGenerationCandidate,
  PassedGenerationCandidate,
  RejectedGenerationCandidate,
} from "./candidate";
import {
  type GateRejectedGenerationOutcome,
  loadGenerationOutcome,
  type PassedGenerationOutcome,
  UnsafeGenerationOutcomeError,
} from "./outcome";

import "./generation.css";

type ExecutionProfile = ExecutionProfilePage["items"][number];
type LlmExecutionMode = ProspectiveGenerationProposeRequest["llm_execution_mode"];
type ConstraintCatalogItem = Awaited<ReturnType<GenerationApi["listConstraints"]>>["items"][number];

interface CatalogPageState<T> {
  error?: Error;
  items: T[];
  loading: boolean;
  nextCursor: string | null;
  readSnapshotId: string;
}

class CatalogSnapshotChangedError extends Error {
  constructor() {
    super("Catalog read snapshot changed.");
    this.name = "CatalogSnapshotChangedError";
  }
}

interface GenerationAttempt {
  error: Error | null;
  intent: MutationIntent;
  pending: boolean;
  prospective: ProspectiveGenerationProposeRequest;
  request: ExecutionOptionResolveRequest;
  resolved: GenerationProposeRequest | null;
  result: RunAccepted | null;
}

function catalogState<T>(page: {
  items: T[];
  next_cursor?: string | null;
  read_snapshot_id: string;
}): CatalogPageState<T> {
  return {
    items: page.items,
    loading: false,
    nextCursor: page.next_cursor ?? null,
    readSnapshotId: page.read_snapshot_id,
  };
}

async function readCatalogPage<T>(
  current: CatalogPageState<T>,
  setCurrent: Dispatch<SetStateAction<CatalogPageState<T> | null>>,
  read: (cursor: string | null) => Promise<{
    items: T[];
    next_cursor?: string | null;
    read_snapshot_id: string;
  }>,
  restart: boolean,
): Promise<void> {
  const cursor = restart ? null : current.nextCursor;
  if (!restart && cursor === null) return;
  setCurrent({ ...current, error: undefined, loading: true });
  try {
    const next = await read(cursor);
    if (!restart && next.read_snapshot_id !== current.readSnapshotId) {
      throw new CatalogSnapshotChangedError();
    }
    setCurrent({
      ...catalogState(next),
      items: restart ? next.items : [...current.items, ...next.items],
    });
  } catch (error) {
    setCurrent({
      ...current,
      error: normalizedError(error),
      loading: false,
    });
  }
}

function CatalogPageControl<T>({
  label,
  onLoad,
  onRestart,
  state,
}: {
  label: string;
  onLoad(): void;
  onRestart(): void;
  state: CatalogPageState<T>;
}) {
  if (state.error) {
    const restartRequired =
      state.error instanceof CursorExpiredError || state.error instanceof CatalogSnapshotChangedError;
    return (
      <div className="gf-generation__catalog-control">
        <p role="alert">
          {state.error instanceof CursorExpiredError
            ? `${label} 游标已过期。`
            : state.error instanceof CatalogSnapshotChangedError
              ? `${label} 分页快照已变化。`
              : `${label} 分页失败。`}
        </p>
        <button className="gf-secondary-button" onClick={restartRequired ? onRestart : onLoad} type="button">
          {restartRequired ? `从首屏重读 ${label}` : `重试加载更多 ${label}`}
        </button>
      </div>
    );
  }
  if (state.loading) return <p role="status">正在读取更多 {label}…</p>;
  if (state.nextCursor === null) return null;
  return (
    <button className="gf-secondary-button" onClick={onLoad} type="button">
      加载更多 {label}
    </button>
  );
}

const terminalEvents = new Set<RunEvent["event_type"]>([
  "run.succeeded",
  "run.failed",
  "run.cancelled",
  "run.timed_out",
]);

const terminalStatuses = new Set<RunView["status"]>(["succeeded", "failed", "cancelled", "timed_out"]);

function artifactHref(artifactId: string): string {
  return `/artifacts/${encodeURIComponent(artifactId)}`;
}

function sourceRunHref(path: string, runId: string, extra: Record<string, string> = {}): string {
  const search = new URLSearchParams({ sourceRun: runId, ...extra });
  return `${path}?${search.toString()}`;
}

function profileKey(profile: ExecutionProfile): string {
  return `${profile.profile.profile_id}@${profile.profile.version}`;
}

function sameProfile(
  left: ExecutionProfile["profile"] | null | undefined,
  right: ExecutionProfile["profile"] | null | undefined,
): boolean {
  return left?.profile_id === right?.profile_id && left?.version === right?.version;
}

function supportsRunKind(profile: ExecutionProfile, kind: string, version = 1): boolean {
  return profile.compatible_run_kinds.some(
    (candidate) => candidate.kind === kind && candidate.version === version,
  );
}

function domainLabel(domainId: string): string {
  return (
    {
      builtin: "内置规则域",
      "domain:combat": "战斗系统",
      "domain:economy": "经济系统",
      "domain:narrative": "叙事内容",
      "domain:quest": "任务系统",
      "domain:rewards": "奖励系统",
    }[domainId] ?? domainId.replace(/^domain:/, "")
  );
}

function specLabel(spec: SpecView): string {
  if (spec.ref_name && spec.ref_value) return `${spec.ref_name} · 第 ${spec.ref_value.revision} 版`;
  return `未绑定发布指针 · ${spec.schema_registry_version}`;
}

function constraintLabel(constraint: ConstraintCatalogItem): string {
  const first = constraint.constraints[0];
  const record =
    first && typeof first === "object" && !Array.isArray(first) ? (first as Record<string, unknown>) : null;
  const summary = record
    ? ["description", "name", "id", "expression", "assert"]
        .map((key) => record[key])
        .find((value): value is string => typeof value === "string" && value.trim().length > 0)
    : null;
  const count = `${constraint.constraints.length} 条规则`;
  return summary ? `${summary} · ${count}` : `${count} · ${constraint.dsl_grammar_version}`;
}

function constraintOptionLabel(constraint: ConstraintCatalogItem, collidingSummaries: number): string {
  const summary = constraintLabel(constraint);
  if (collidingSummaries < 2) return summary;
  const created = constraint.artifact.created_at?.slice(0, 10) ?? "时间未知";
  const identity = constraint.artifact.artifact_id.slice(-8);
  return `${summary} · ${created} · …${identity}`;
}

function matchesQuery(query: string, ...values: Array<string | null | undefined>): boolean {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return true;
  return values.some((value) => value?.toLocaleLowerCase().includes(needle));
}

function runLabel(run: ReplaySourceRun): string {
  return replaySourceOptionLabel(run);
}

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("生成请求失败。");
}

function blocksNewIntent(error: Error | null | undefined): boolean {
  return error != null && !(error instanceof ApiProblemError);
}

function sameDomainScope(left: { domain_ids: string[] }, right: { domain_ids: string[] }): boolean {
  return (
    left.domain_ids.length === right.domain_ids.length &&
    left.domain_ids.every((item, index) => item === right.domain_ids[index])
  );
}

function MutationFailure({ attempt, onRetry }: { attempt: GenerationAttempt; onRetry(): void }) {
  if (!attempt.error) return null;
  if (attempt.error instanceof ApiProblemError) return <ProblemPanel problem={attempt.error.problem} />;
  if (attempt.error instanceof ReauthenticationRequiredError) {
    return (
      <StatePanel
        action={<ReauthenticationLink />}
        description="当前浏览器标签页没有可用 CSRF 会话；未发送新的生成请求。"
        state="error"
        title="需要重新登录"
      />
    );
  }
  return (
    <StatePanel
      action={
        <button className="gf-secondary-button" onClick={onRetry} type="button">
          以同一 intent 重试
        </button>
      }
      description="网络结果未知；页面保留已解析的 exact request 与同一 Idempotency-Key，不会自动创建新 intent。"
      state="error"
      title="生成结果未知"
    />
  );
}

function GenerationAuthoring({ api, onAccepted }: { api: GenerationApi; onAccepted(runId: string): void }) {
  const catalog = useQuery({
    queryFn: async () => {
      const [specs, constraints, profiles, replayRuns] = await Promise.all([
        api.listSpecs(null),
        api.listConstraints(null),
        api.listExecutionProfiles(null),
        api.listReplaySourceRuns(null),
      ]);
      return { constraints, profiles, replayRuns, specs };
    },
    queryKey: ["generation", "authoring-catalog"],
    retry: false,
  });
  const [specId, setSpecId] = useState("");
  const [constraintId, setConstraintId] = useState("");
  const [generationKey, setGenerationKey] = useState("");
  const [environmentKey, setEnvironmentKey] = useState("");
  const [exportKeys, setExportKeys] = useState<string[]>([]);
  const [domainIds, setDomainIds] = useState<string[]>([]);
  const [specQuery, setSpecQuery] = useState("");
  const [constraintQuery, setConstraintQuery] = useState("");
  const [goal, setGoal] = useState("");
  const [mode, setMode] = useState<"" | LlmExecutionMode>("");
  const [replaySourceRunId, setReplaySourceRunId] = useState("");
  const [attempt, setAttempt] = useState<GenerationAttempt | null>(null);
  const [specCatalog, setSpecCatalog] = useState<CatalogPageState<SpecView> | null>(null);
  const [constraintCatalog, setConstraintCatalog] = useState<CatalogPageState<ConstraintCatalogItem> | null>(
    null,
  );
  const [profileCatalog, setProfileCatalog] = useState<CatalogPageState<ExecutionProfile> | null>(null);
  const [replayRunCatalog, setReplayRunCatalog] = useState<CatalogPageState<ReplaySourceRun> | null>(null);

  useEffect(() => {
    if (!catalog.data) return;
    setSpecCatalog(catalogState(catalog.data.specs));
    setConstraintCatalog(catalogState(catalog.data.constraints));
    setProfileCatalog(catalogState(catalog.data.profiles));
    setReplayRunCatalog(catalogState(catalog.data.replayRuns));
  }, [catalog.data]);

  if (catalog.isPending) {
    return (
      <StatePanel
        description="正在读取 Spec、ConstraintSnapshot 与 execution profile 的有界目录。"
        headingLevel={1}
        state="loading"
        title="正在准备生成输入"
      />
    );
  }
  if (catalog.isError) {
    return catalog.error instanceof ApiProblemError ? (
      <ProblemPanel problem={catalog.error.problem} />
    ) : (
      <StatePanel
        action={
          <button className="gf-secondary-button" onClick={() => void catalog.refetch()} type="button">
            重试目录读取
          </button>
        }
        description="生成页没有使用任何隐藏 profile 或 authority fallback。"
        headingLevel={1}
        state="error"
        title="无法读取生成目录"
      />
    );
  }
  if (!specCatalog || !constraintCatalog || !profileCatalog || !replayRunCatalog) {
    return <StatePanel description="正在固定目录分页快照。" state="loading" title="正在准备 exact 目录" />;
  }

  const specs = specCatalog.items;
  const constraints = constraintCatalog.items;
  const constraintLabelCounts = new Map<string, number>();
  for (const item of constraints) {
    const label = constraintLabel(item);
    constraintLabelCounts.set(label, (constraintLabelCounts.get(label) ?? 0) + 1);
  }
  const profiles = profileCatalog.items;
  const generationProfiles = profiles.filter(
    (profile) =>
      profile.status === "active" &&
      profile.profile_kind === "generation" &&
      supportsRunKind(profile, "generation.propose"),
  );
  const environmentProfiles = profiles.filter(
    (profile) => profile.status === "active" && profile.profile_kind === "environment",
  );
  const refBoundSpecs = specs.filter((item) => item.ref_name != null && item.ref_value != null);
  const selectedSpec = refBoundSpecs.find((item) => item.artifact.artifact_id === specId);
  const selectedConstraint = constraints.find((item) => item.artifact.artifact_id === constraintId);
  const selectedGeneration = generationProfiles.find((profile) => profileKey(profile) === generationKey);
  const selectedEnvironment = environmentProfiles.find((profile) => profileKey(profile) === environmentKey);
  const exportProfiles = profiles.filter(
    (profile) =>
      profile.status === "active" &&
      profile.profile_kind === "config_export" &&
      supportsRunKind(profile, "generation.propose") &&
      sameProfile(profile.target_environment_profile, selectedEnvironment?.profile),
  );
  const selectedExports = exportKeys
    .map((key) => exportProfiles.find((profile) => profileKey(profile) === key))
    .filter((profile): profile is ExecutionProfile => profile !== undefined)
    .sort((left, right) => profileKey(left).localeCompare(profileKey(right)));
  const domains = [...domainIds].sort();
  const domainOptions = selectedGeneration?.domain_scope.domain_ids ?? [];
  const visibleSpecs = refBoundSpecs.filter((item) =>
    matchesQuery(specQuery, specLabel(item), item.ref_name, item.schema_registry_version),
  );
  const visibleConstraints = constraints.filter((item) =>
    matchesQuery(
      constraintQuery,
      constraintLabel(item),
      item.dsl_grammar_version,
      item.artifact.created_at,
      item.artifact.artifact_id,
    ),
  );
  const hasExactTarget = Boolean(selectedSpec?.ref_name && selectedSpec.ref_value);
  const canSubmit =
    !attempt?.pending &&
    !blocksNewIntent(attempt?.error) &&
    selectedSpec !== undefined &&
    selectedConstraint !== undefined &&
    selectedGeneration !== undefined &&
    selectedEnvironment !== undefined &&
    selectedExports.length > 0 &&
    hasExactTarget &&
    domains.length > 0 &&
    goal.trim().length > 0 &&
    mode !== "" &&
    (mode !== "replay" || replaySourceRunId.trim().length > 0);

  async function execute(frozen: GenerationAttempt) {
    setAttempt({ ...frozen, error: null, pending: true, result: null });
    let resolved = frozen.resolved;
    try {
      if (resolved === null) {
        const option = await api.resolveExecutionOption(frozen.request);
        if (
          option.resource_operation_id !== frozen.request.resource_operation_id ||
          option.run_kind.kind !== frozen.request.run_kind.kind ||
          option.run_kind.version !== frozen.request.run_kind.version ||
          option.llm_execution_mode !== frozen.request.llm_execution_mode ||
          !sameDomainScope(option.domain_scope, frozen.prospective.domain_scope) ||
          (frozen.request.llm_execution_mode === "replay" &&
            (!option.cassette_artifact_id || option.source_run_id !== frozen.request.replay_source_run_id))
        ) {
          throw new Error("Execution option did not match the requested generation binding.");
        }
        resolved = {
          ...frozen.prospective,
          cassette_artifact_id: option.cassette_artifact_id ?? null,
          execution_version_plan: option.execution_version_plan,
        };
        setAttempt({ ...frozen, pending: true, resolved });
      }
      const result = await api.proposeGeneration(resolved, frozen.intent);
      setAttempt({ ...frozen, error: null, pending: false, resolved, result });
      onAccepted(result.run_id);
    } catch (error) {
      setAttempt({
        ...frozen,
        error: normalizedError(error),
        pending: false,
        resolved,
        result: null,
      });
    }
  }

  function submit() {
    if (
      !canSubmit ||
      !selectedSpec?.ref_name ||
      !selectedSpec.ref_value ||
      !selectedConstraint ||
      !selectedGeneration
    ) {
      return;
    }
    const prospective: ProspectiveGenerationProposeRequest = {
      base_snapshot_artifact_id: selectedSpec.artifact.artifact_id,
      candidate_export_profiles: selectedExports.map((profile) => profile.profile),
      cassette_artifact_id: null,
      constraint_snapshot_artifact_id: selectedConstraint.artifact.artifact_id,
      domain_scope: { domain_ids: domains },
      execution_version_plan: null,
      findings: [],
      generation_policy: selectedGeneration.profile,
      llm_execution_mode: mode,
      objective_goal_text: goal.trim(),
      request_schema_version: "generation-propose-request@1",
      target: { expected_ref: selectedSpec.ref_value, ref_name: selectedSpec.ref_name },
    };
    const request: ExecutionOptionResolveRequest = {
      llm_execution_mode: mode,
      prospective_request: prospective,
      replay_source_run_id: mode === "replay" ? replaySourceRunId.trim() : null,
      request_schema_version: "execution-option-resolve-request@1",
      resource_operation_id: "propose_generation_api_v1_generation_propose_post",
      run_kind: { kind: "generation.propose", version: 1 },
    };
    void execute({
      error: null,
      intent: createMutationIntent(),
      pending: false,
      prospective,
      request,
      resolved: null,
      result: null,
    });
  }

  return (
    <div className="gf-generation__authoring-layout">
      <section className="gf-generation__authoring" aria-labelledby="generation-input-title">
        <header>
          <p className="gf-generation__kicker">Exact authority → bounded Agent run</p>
          <h2 id="generation-input-title">目标与 exact authority</h2>
          <p>所有 profile、base、constraint、environment 与 ref 前提都必须显式选择。</p>
        </header>
        <form
          className="gf-form"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <label>
            搜索可用规格
            <input
              onChange={(event) => setSpecQuery(event.target.value)}
              placeholder="按发布指针或版本搜索"
              type="search"
              value={specQuery}
            />
          </label>
          <label>
            Base Spec / ref
            <select onChange={(event) => setSpecId(event.target.value)} value={specId}>
              <option value="">请选择 exact ref-bound Spec</option>
              {visibleSpecs.map((item) => (
                <option key={item.artifact.artifact_id} value={item.artifact.artifact_id}>
                  {specLabel(item)}
                </option>
              ))}
            </select>
          </label>
          <CatalogPageControl
            label="Base Specs"
            onLoad={() => void readCatalogPage(specCatalog, setSpecCatalog, api.listSpecs.bind(api), false)}
            onRestart={() => void readCatalogPage(specCatalog, setSpecCatalog, api.listSpecs.bind(api), true)}
            state={specCatalog}
          />
          <label>
            搜索约束规则
            <input
              onChange={(event) => setConstraintQuery(event.target.value)}
              placeholder="按规则名称或 DSL 版本搜索"
              type="search"
              value={constraintQuery}
            />
          </label>
          <label>
            Constraint snapshot
            <select onChange={(event) => setConstraintId(event.target.value)} value={constraintId}>
              <option value="">请选择 exact ConstraintSnapshot</option>
              {visibleConstraints.map((item) => (
                <option key={item.artifact.artifact_id} value={item.artifact.artifact_id}>
                  {constraintOptionLabel(item, constraintLabelCounts.get(constraintLabel(item)) ?? 0)}
                </option>
              ))}
            </select>
          </label>
          <CatalogPageControl
            label="Constraint snapshots"
            onLoad={() =>
              void readCatalogPage(
                constraintCatalog,
                setConstraintCatalog,
                api.listConstraints.bind(api),
                false,
              )
            }
            onRestart={() =>
              void readCatalogPage(
                constraintCatalog,
                setConstraintCatalog,
                api.listConstraints.bind(api),
                true,
              )
            }
            state={constraintCatalog}
          />
          <label>
            Generation profile
            <select
              onChange={(event) => {
                setGenerationKey(event.target.value);
                setDomainIds([]);
              }}
              value={generationKey}
            >
              <option value="">请选择 active generation profile</option>
              {generationProfiles.map((item) => (
                <option key={profileKey(item)} value={profileKey(item)}>
                  {item.display_name} · {profileKey(item)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Environment profile
            <select
              onChange={(event) => {
                setEnvironmentKey(event.target.value);
                setExportKeys([]);
              }}
              value={environmentKey}
            >
              <option value="">请选择 active environment profile</option>
              {environmentProfiles.map((item) => (
                <option key={profileKey(item)} value={profileKey(item)}>
                  {item.display_name} · {profileKey(item)}
                </option>
              ))}
            </select>
          </label>
          <fieldset>
            <legend>Candidate export profiles</legend>
            {!selectedEnvironment ? (
              <p>先选择 environment；页面不会使用默认导出器。</p>
            ) : exportProfiles.length === 0 ? (
              <p>该 environment 没有 active config_export profile。</p>
            ) : (
              exportProfiles.map((item) => {
                const key = profileKey(item);
                return (
                  <label key={key}>
                    <input
                      checked={exportKeys.includes(key)}
                      onChange={(event) =>
                        setExportKeys((current) =>
                          event.target.checked
                            ? [...current, key].sort()
                            : current.filter((candidate) => candidate !== key),
                        )
                      }
                      type="checkbox"
                    />
                    {item.display_name} · {key}
                  </label>
                );
              })
            )}
          </fieldset>
          <CatalogPageControl
            label="Execution profiles"
            onLoad={() =>
              void readCatalogPage(
                profileCatalog,
                setProfileCatalog,
                api.listExecutionProfiles.bind(api),
                false,
              )
            }
            onRestart={() =>
              void readCatalogPage(
                profileCatalog,
                setProfileCatalog,
                api.listExecutionProfiles.bind(api),
                true,
              )
            }
            state={profileCatalog}
          />
          <fieldset>
            <legend>内容领域</legend>
            {!selectedGeneration ? (
              <p>先选择 Generation profile，页面会列出它明确覆盖的业务领域。</p>
            ) : domainOptions.length === 0 ? (
              <p>所选 Generation profile 没有可用领域，不能启动生成。</p>
            ) : (
              domainOptions.map((domainId) => (
                <label key={domainId}>
                  <input
                    checked={domainIds.includes(domainId)}
                    onChange={(event) =>
                      setDomainIds((current) =>
                        event.target.checked
                          ? [...current, domainId].sort()
                          : current.filter((candidate) => candidate !== domainId),
                      )
                    }
                    type="checkbox"
                  />
                  {domainLabel(domainId)}
                </label>
              ))
            )}
          </fieldset>
          <label>
            Authenticated authoring goal
            <textarea onChange={(event) => setGoal(event.target.value)} rows={5} value={goal} />
          </label>
          <label>
            LLM execution mode
            <select
              onChange={(event) => {
                const next = event.target.value as "" | LlmExecutionMode;
                setMode(next);
                if (next !== "replay") setReplaySourceRunId("");
              }}
              value={mode}
            >
              <option value="">请选择 live / record / replay</option>
              <option value="live">live</option>
              <option value="record">record</option>
              <option value="replay">replay</option>
            </select>
          </label>
          {mode === "replay" && (
            <>
              <label>
                Replay source Run
                <select
                  onChange={(event) => setReplaySourceRunId(event.target.value)}
                  value={replaySourceRunId}
                >
                  <option value="">请选择一个已完成的回放来源</option>
                  {replayRunCatalog.items.map((run) => (
                    <option key={run.run_id} value={run.run_id}>
                      {runLabel(run)}
                    </option>
                  ))}
                </select>
              </label>
              <CatalogPageControl
                label="已完成 Runs"
                onLoad={() =>
                  void readCatalogPage(
                    replayRunCatalog,
                    setReplayRunCatalog,
                    api.listReplaySourceRuns.bind(api),
                    false,
                  )
                }
                onRestart={() =>
                  void readCatalogPage(
                    replayRunCatalog,
                    setReplayRunCatalog,
                    api.listReplaySourceRuns.bind(api),
                    true,
                  )
                }
                state={replayRunCatalog}
              />
            </>
          )}
          {!hasExactTarget && selectedSpec && (
            <p role="alert">所选 Spec 没有 exact ref_value，不能作为正式内容 target。</p>
          )}
          <button disabled={!canSubmit} type="submit">
            {attempt?.pending ? "正在解析并提交…" : "开始生成"}
          </button>
        </form>
        {attempt && <MutationFailure attempt={attempt} onRetry={() => void execute(attempt)} />}
      </section>

      <aside className="gf-generation__authority-ledger" aria-label="生成 authority 账页">
        <p className="gf-generation__kicker">Immutable selection ledger</p>
        <h2>当前 exact 绑定</h2>
        <dl>
          <div>
            <dt>Base</dt>
            <dd>{selectedSpec ? specLabel(selectedSpec) : "未选择"}</dd>
          </div>
          <div>
            <dt>Ref</dt>
            <dd>
              {selectedSpec?.ref_name && selectedSpec.ref_value
                ? `${selectedSpec.ref_name} · r${selectedSpec.ref_value.revision}`
                : "未绑定"}
            </dd>
          </div>
          <div>
            <dt>Constraint</dt>
            <dd>
              {selectedConstraint
                ? constraintOptionLabel(
                    selectedConstraint,
                    constraintLabelCounts.get(constraintLabel(selectedConstraint)) ?? 0,
                  )
                : "未选择"}
            </dd>
          </div>
          <div>
            <dt>Generation</dt>
            <dd>{selectedGeneration ? profileKey(selectedGeneration) : "未选择"}</dd>
          </div>
          <div>
            <dt>Environment</dt>
            <dd>{selectedEnvironment ? profileKey(selectedEnvironment) : "未选择"}</dd>
          </div>
          <div>
            <dt>Exports</dt>
            <dd>{selectedExports.length ? selectedExports.map(profileKey).join(" · ") : "未选择"}</dd>
          </div>
        </dl>
      </aside>
    </div>
  );
}

type CandidateArtifact = PassedGenerationCandidate["patch"];

function ArtifactCard({ artifact, label }: { artifact: CandidateArtifact; label: string }) {
  return (
    <article className="gf-generation__artifact-card">
      <p>{label}</p>
      <a href={artifactHref(artifact.artifact_id)}>{artifact.artifact_id}</a>
      <dl>
        <div>
          <dt>Kind</dt>
          <dd>{artifact.kind}</dd>
        </div>
        <div>
          <dt>Schema</dt>
          <dd>{artifact.payload_schema_id}</dd>
        </div>
      </dl>
    </article>
  );
}

function ArtifactList({ artifacts }: { artifacts: readonly CandidateArtifact[] }) {
  if (artifacts.length === 0) return <p className="gf-generation__empty-copy">暂无此类工件。</p>;
  return (
    <ul className="gf-generation__artifact-list">
      {artifacts.map((artifact) => (
        <li key={artifact.artifact_id}>
          <a href={artifactHref(artifact.artifact_id)}>{artifact.artifact_id}</a>
          <span>{artifact.kind}</span>
        </li>
      ))}
    </ul>
  );
}

function IntermediateList({ intermediates }: { intermediates: PassedGenerationCandidate["intermediates"] }) {
  return (
    <ul className="gf-generation__artifact-list">
      {intermediates.map((intermediate) => (
        <li key={intermediate.artifactId}>
          <code>{intermediate.artifactId}</code>
          <span>manifest-only · 敏感 payload 不经通用端点公开</span>
        </li>
      ))}
    </ul>
  );
}

function OutcomeEvidence({
  evidence,
  intermediates,
}: {
  evidence: readonly CandidateArtifact[];
  intermediates: PassedGenerationCandidate["intermediates"];
}) {
  const deterministic = evidence.filter(
    (artifact) => artifact.kind === "checker_run" || artifact.kind === "regression_evidence",
  );
  const simulation = evidence.filter((artifact) => artifact.kind === "simulation_run");
  const suggestion = evidence.filter((artifact) => artifact.kind === "review_report");
  return (
    <section className="gf-generation__evidence" aria-labelledby="generation-evidence-title">
      <header>
        <p className="gf-generation__kicker">Oracle-grounded result</p>
        <h2 id="generation-evidence-title">Gate evidence</h2>
      </header>
      <EvidenceSections
        deterministic={deterministic.length > 0 ? <ArtifactList artifacts={deterministic} /> : undefined}
        simulation={simulation.length > 0 ? <ArtifactList artifacts={simulation} /> : undefined}
        suggestion={suggestion.length > 0 ? <ArtifactList artifacts={suggestion} /> : undefined}
      />
      {intermediates.length > 0 && (
        <section className="gf-generation__supporting" aria-labelledby="generation-supporting-title">
          <h3 id="generation-supporting-title">Supporting runtime artifacts</h3>
          <p>这些工件支持回放和审计，不被当作 gate evidence。</p>
          <IntermediateList intermediates={intermediates} />
        </section>
      )}
    </section>
  );
}

function CandidateChain({ candidate }: { candidate: PassedGenerationCandidate }) {
  return (
    <section className="gf-generation__candidate" aria-labelledby="generation-candidate-title">
      <header>
        <p className="gf-generation__kicker">Immutable candidate ledger</p>
        <h2 id="generation-candidate-title">Patch → preview → config</h2>
        <p>候选链来自 RunResult 的 canonical closure；工件存在不代表 ref 已更新。</p>
      </header>
      <div className="gf-generation__candidate-chain">
        <ArtifactCard artifact={candidate.patch} label="Primary Patch" />
        <ArrowRight aria-hidden="true" size={18} />
        <ArtifactCard artifact={candidate.preview} label="唯一 Preview" />
        {candidate.configExports.map((artifact) => (
          <div className="gf-generation__candidate-next" key={artifact.artifact_id}>
            <ArrowRight aria-hidden="true" size={18} />
            <ArtifactCard artifact={artifact} label="Config export" />
          </div>
        ))}
      </div>
    </section>
  );
}

function RejectedCandidateChain({ candidate }: { candidate: RejectedGenerationCandidate }) {
  return (
    <section className="gf-generation__candidate" aria-labelledby="generation-rejected-candidate-title">
      <header>
        <p className="gf-generation__kicker">Evidence-only retained proposal</p>
        <h2 id="generation-rejected-candidate-title">Rejected Patch + preview</h2>
        <p>两项仅供解释与审计；服务器没有创建 workflow subject，也没有 config export。</p>
      </header>
      <div className="gf-generation__candidate-chain">
        <ArtifactCard artifact={candidate.patch} label="Evidence-only Patch" />
        <ArrowRight aria-hidden="true" size={18} />
        <ArtifactCard artifact={candidate.preview} label="Evidence-only Preview" />
      </div>
    </section>
  );
}

function generationFieldLabel(fieldPath: string): string {
  return fieldPath === "reward.gold" ? "金币奖励" : fieldPath;
}

function GateRejectedOutcome({ outcome }: { outcome: GateRejectedGenerationOutcome }) {
  const primaryBlocker = outcome.blockers[0];
  const technicalEvidenceCount = 2 + outcome.candidate.evidence.length;
  return (
    <div className="gf-generation__outcome-stack">
      <StatePanel
        action={
          <a className="gf-primary-link" href="/generation">
            调整目标后重新生成
          </a>
        }
        description="生成已完成，确定性门禁阻止了不合规提议；这不是系统故障。"
        state="terminal"
        title={`拦截成功：${generationFieldLabel(primaryBlocker.fieldPath)} ${primaryBlocker.actualValue} 超过上限 ${primaryBlocker.limit}`}
      />
      <section className="gf-generation__rejection-summary" aria-label="门禁拦截摘要">
        <section aria-label="提议改动">
          <p className="gf-generation__kicker">提议改动</p>
          <h2>候选值没有进入正式内容</h2>
          <ul>
            {outcome.changes.map((change) => (
              <li key={`${change.entityId}:${change.fieldPath}`}>
                <span>{change.entityTitle ?? change.entityId}</span>{" "}
                {change.entityTitle && <code>{change.entityId}</code>} <code>{change.fieldPath}</code>{" "}
                <strong>
                  {change.oldValue} → {change.newValue}
                </strong>
              </li>
            ))}
          </ul>
        </section>
        <section aria-label="拦截原因">
          <p className="gf-generation__kicker">拦截原因</p>
          <h2>确定性约束已确认违规</h2>
          <ul>
            {outcome.blockers.map((blocker) => (
              <li key={`${blocker.constraintId}:${blocker.entityId}:${blocker.fieldPath}`}>
                <code>{blocker.constraintId}</code>{" "}
                <strong>
                  {blocker.actualValue} &gt; {blocker.limit}
                </strong>{" "}
                <span>确定性检查 · confirmed</span>
              </li>
            ))}
          </ul>
        </section>
        <section aria-label="正式内容状态">
          <p className="gf-generation__kicker">正式内容</p>
          <h2>正式内容未变化</h2>
          <p>
            该提议仅作为 evidence 保留；没有创建 workflow subject 或 config export，也没有移动任何正式 ref。
          </p>
        </section>
      </section>
      <details className="gf-generation__technical-evidence">
        <summary>查看技术证据（{technicalEvidenceCount}）</summary>
        <div>
          <RejectedCandidateChain candidate={outcome.candidate} />
          <OutcomeEvidence
            evidence={outcome.candidate.evidence}
            intermediates={outcome.candidate.intermediates}
          />
        </div>
      </details>
    </div>
  );
}

function PreviousApproval({ outcome }: { outcome: PassedGenerationOutcome }) {
  const previous = outcome.previousApproval?.value.approval;
  const previousPatch = outcome.previousPatch?.value;
  const previousBinding = outcome.previousBinding;
  if (!previous || !previousPatch || !previousBinding) return null;
  const current = outcome.approval.value.approval;
  const currentEvidenceCount =
    (current.evidence_set_artifact_id ? 1 : 0) + current.regression_evidence_artifact_ids.length;
  const previousEvidenceCount =
    (previous.evidence_set_artifact_id ? 1 : 0) + previous.regression_evidence_artifact_ids.length;
  return (
    <section className="gf-generation__revision-history" aria-labelledby="generation-revision-history-title">
      <header>
        <p className="gf-generation__kicker">Repair successor boundary</p>
        <h2 id="generation-revision-history-title">旧审批状态不会继承</h2>
        <p>旧 revision 的决定与证据保持在旧 Approval；新 revision 从自己的 workflow 状态继续。</p>
      </header>
      <div className="gf-generation__approval-compare">
        <section aria-label="旧 Patch workflow 状态">
          <h3>旧 Approval · r{previous.subject_revision}</h3>
          <dl>
            <div>
              <dt>Patch</dt>
              <dd>{previousPatch.artifact.artifact_id}</dd>
            </div>
            <div>
              <dt>状态</dt>
              <dd>{previous.status}</dd>
            </div>
            <div>
              <dt>Head</dt>
              <dd>{previousBinding.is_current_head ? "current" : "non-current"}</dd>
            </div>
            <div>
              <dt>Evidence</dt>
              <dd>{previousEvidenceCount}</dd>
            </div>
            <div>
              <dt>Decisions</dt>
              <dd>{previous.decisions.length}</dd>
            </div>
            <div>
              <dt>EvidenceSet</dt>
              <dd>{previous.evidence_set_artifact_id ?? "无"}</dd>
            </div>
          </dl>
        </section>
        <section aria-label="新 Patch workflow 状态">
          <h3>新 Approval · r{current.subject_revision}</h3>
          <dl>
            <div>
              <dt>Patch</dt>
              <dd>{outcome.patch.value.artifact.artifact_id}</dd>
            </div>
            <div>
              <dt>Supersedes</dt>
              <dd>{outcome.patch.value.patch.supersedes_artifact_id}</dd>
            </div>
            <div>
              <dt>状态</dt>
              <dd>{current.status}</dd>
            </div>
            <div>
              <dt>Evidence</dt>
              <dd>{currentEvidenceCount}</dd>
            </div>
            <div>
              <dt>Decisions</dt>
              <dd>{current.decisions.length}</dd>
            </div>
            <div>
              <dt>EvidenceSet</dt>
              <dd>{current.evidence_set_artifact_id ?? "无"}</dd>
            </div>
          </dl>
        </section>
      </div>
    </section>
  );
}

function PassedOutcome({ outcome }: { outcome: PassedGenerationOutcome }) {
  const { approval, baseSpec, binding, candidate, constraint, diff, patch } = outcome;
  const approvalItem = approval.value.approval;
  return (
    <div className="gf-generation__outcome-stack">
      <StatePanel
        description="RunResult 与 workflow authority 已闭合；候选仍需后续验证、审批与显式应用。"
        state="terminal"
        title="generation_gate_passed"
      />
      <CandidateChain candidate={candidate} />
      <section className="gf-generation__workflow-ledger" aria-labelledby="generation-workflow-title">
        <header>
          <p className="gf-generation__kicker">Server-owned workflow state</p>
          <h2 id="generation-workflow-title">Patch workflow</h2>
        </header>
        <dl>
          <div>
            <dt>Exact base</dt>
            <dd>{baseSpec.artifact.artifact_id}</dd>
          </div>
          <div>
            <dt>Constraint</dt>
            <dd>{constraint.artifact.artifact_id}</dd>
          </div>
          <div>
            <dt>Patch revision</dt>
            <dd>{patch.value.patch.revision}</dd>
          </div>
          <div>
            <dt>Approval status</dt>
            <dd>{approvalItem.status}</dd>
          </div>
          <div>
            <dt>Validation</dt>
            <dd>{patch.value.validation_status}</dd>
          </div>
          <div>
            <dt>Current head</dt>
            <dd>{binding.is_current_head ? "是" : `否 · head r${binding.subject_head_revision}`}</dd>
          </div>
        </dl>
        <a className="gf-primary-link" href={`/patches/${encodeURIComponent(candidate.patch.artifact_id)}`}>
          打开 exact Patch workflow <ArrowRight aria-hidden="true" size={16} />
        </a>
      </section>
      <SnapshotDiffView diff={diff.diff} entries={diff.page.items} />
      <PreviousApproval outcome={outcome} />
      <OutcomeEvidence evidence={candidate.evidence} intermediates={candidate.intermediates} />
      <nav className="gf-generation__next-actions" aria-label="候选后续动作">
        <div>
          <p className="gf-generation__kicker">Continue with exact Run authority</p>
          <h2>下一步</h2>
        </div>
        <a
          href={sourceRunHref("/reviews", candidate.runId, {
            snapshot: candidate.preview.artifact_id,
            constraint: constraint.artifact.artifact_id,
          })}
        >
          Review 候选
        </a>
        {candidate.configExports.map((config) => {
          const context = {
            preview: candidate.preview.artifact_id,
            config: config.artifact_id,
            constraint: constraint.artifact.artifact_id,
          };
          return (
            <span className="gf-generation__next-config" key={config.artifact_id}>
              <a href={sourceRunHref("/playtest", candidate.runId, { ...context, action: "derive" })}>
                派生 TaskSuite · {config.artifact_id}
              </a>
              <a href={sourceRunHref("/playtest", candidate.runId, context)}>
                进入 Playtest · {config.artifact_id}
              </a>
            </span>
          );
        })}
      </nav>
    </div>
  );
}

function FailedOutcome({ candidate }: { candidate: FailedGenerationCandidate }) {
  return (
    <div className="gf-generation__outcome-stack">
      <StatePanel description={candidate.message} state="error" title={candidate.causeCode} />
      <OutcomeEvidence evidence={candidate.evidence} intermediates={candidate.intermediates} />
    </div>
  );
}

function GenerationOutcomePanel({ api, run }: { api: GenerationApi; run: RunView }) {
  const outcome = useQuery({
    enabled: terminalStatuses.has(run.status),
    queryFn: () => loadGenerationOutcome(api, run),
    queryKey: [
      "generation",
      "outcome",
      run.run_id,
      run.revision,
      run.result_artifact_id,
      run.failure_artifact_id,
    ],
    retry: false,
  });

  if (!terminalStatuses.has(run.status)) return null;
  if (outcome.isPending) {
    return (
      <StatePanel
        description="正在闭合 manifest、候选工件与 workflow authority。"
        state="loading"
        title="正在读取候选链"
      />
    );
  }
  if (outcome.isError) {
    if (outcome.error instanceof ApiProblemError) return <ProblemPanel problem={outcome.error.problem} />;
    const unsafe = outcome.error instanceof UnsafeGenerationOutcomeError;
    return (
      <StatePanel
        action={
          unsafe ? undefined : (
            <button className="gf-secondary-button" onClick={() => void outcome.refetch()} type="button">
              重试候选读取
            </button>
          )
        }
        description={
          unsafe
            ? outcome.error.message
            : "候选读取失败；页面没有展示底层异常，也没有从本地状态猜测 workflow 资格。"
        }
        state="error"
        title={unsafe ? "候选 authority 不安全" : "无法读取候选链"}
      />
    );
  }
  if (outcome.data.kind === "passed") return <PassedOutcome outcome={outcome.data} />;
  if (outcome.data.kind === "gate-rejected") return <GateRejectedOutcome outcome={outcome.data} />;
  if (outcome.data.kind === "failure") return <FailedOutcome candidate={outcome.data.candidate} />;
  return (
    <StatePanel
      description={`Run manifest 未通过 typed candidate guard：${outcome.data.candidate.reason}`}
      state="error"
      title={outcome.data.candidate.reason}
    />
  );
}

function GenerationRun({ api, runId }: { api: GenerationApi; runId: string }) {
  const [events, setEvents] = useState<RunEventItem[]>([]);
  const [streamState, setStreamState] = useState<RunEventStreamState>({ status: "idle" });
  const streamRef = useRef<GenerationEventStreamHandle>();
  const streamReceivedEventRef = useRef(false);
  const run = useQuery({
    queryFn: () => api.getRun(runId),
    queryKey: ["generation", "run", runId],
    retry: false,
  });
  const { refetch } = run;
  const hasTerminalRunView = run.data !== undefined && terminalStatuses.has(run.data.status);

  useEffect(() => {
    setEvents([]);
    setStreamState({ status: "idle" });
    streamReceivedEventRef.current = false;
    const stream = api.createEventStream({
      onEvent(event, cursor) {
        if (event.run_id !== runId) return;
        streamReceivedEventRef.current = true;
        setEvents((current) => {
          const key = `${event.run_id}:${event.seq}`;
          if (current.some((item) => `${item.event.run_id}:${item.event.seq}` === key)) return current;
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

  const preliminaryGate = useMemo(
    () =>
      [...events]
        .reverse()
        .find(
          ({ event }) =>
            event.event_type === "attempt.progress" &&
            event.data.phase_code === "generation.preliminary_gate",
        ),
    [events],
  );
  const preliminaryGatePanel = preliminaryGate ? (
    <StatePanel
      description="已从真实 SSE attempt.progress 观察到 generation.preliminary_gate。"
      state={
        run.data?.status === "succeeded"
          ? "terminal"
          : run.data && terminalStatuses.has(run.data.status)
            ? "error"
            : "streaming"
      }
      title="Preliminary gate"
    />
  ) : null;

  return (
    <section className="gf-generation__run" aria-labelledby="generation-run-title">
      <header>
        <p className="gf-generation__kicker">Run-backed authoring state</p>
        <h1 id="generation-run-title">生成结果</h1>
        <p className="gf-generation__run-identity">
          <span>Run ID</span>
          <code>{runId}</code>
        </p>
        <p>URL 只保存 Run ID；目标文本不进入地址栏。</p>
      </header>
      {!hasTerminalRunView && preliminaryGatePanel}
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
          description={`已保存的事件游标过期${streamState.earliestCursor ? `；最早游标 ${streamState.earliestCursor}` : ""}。`}
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
          description="事件流中断；页面不会清除 Last-Event-ID。"
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
            action={
              <button className="gf-secondary-button" onClick={() => void refetch()} type="button">
                重试 RunView
              </button>
            }
            description="运行读取失败；候选资格不会从本地事件猜测。"
            state="error"
            title="无法读取运行"
          />
        )
      ) : run.data.run_id !== runId ? (
        <StatePanel
          description="RunView identity 与 URL 中的 Run ID 不一致；页面不会读取任何候选或 workflow authority。"
          state="error"
          title="Run identity mismatch"
        />
      ) : terminalStatuses.has(run.data.status) ? (
        <>
          <GenerationOutcomePanel api={api} run={run.data} />
          <details className="gf-generation__run-technical">
            <summary>查看运行技术状态</summary>
            {preliminaryGatePanel}
            <RunProgress events={events} run={run.data} />
          </details>
        </>
      ) : (
        <RunProgress events={events} run={run.data} />
      )}
    </section>
  );
}

export function GenerationPage({ api = generationApi }: { api?: GenerationApi }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const runId = searchParams.get("run")?.trim() || null;

  return (
    <div className="gf-page gf-generation">
      {runId === null ? (
        <>
          <header className="gf-generation__hero">
            <div>
              <p className="gf-generation__kicker">Content generation · Proposal only</p>
              <h1>内容生成</h1>
              <p>输入策划目标，绑定 exact authority，让 Agent 只产候选并接受确定性 preliminary gate。</p>
            </div>
            <div className="gf-generation__hero-marks" aria-label="生成原则">
              <span>
                <Database aria-hidden="true" size={16} /> exact base
              </span>
              <span>
                <ShieldCheck aria-hidden="true" size={16} /> deterministic gate
              </span>
              <span>
                <Bot aria-hidden="true" size={16} /> proposal only
              </span>
            </div>
          </header>
          <GenerationAuthoring
            api={api}
            onAccepted={(acceptedRunId) => setSearchParams({ run: acceptedRunId })}
          />
        </>
      ) : (
        <>
          <nav aria-label="生成运行导航" className="gf-generation__run-nav">
            <button className="gf-secondary-button" onClick={() => setSearchParams({})} type="button">
              开始另一次生成
            </button>
            <a href={`/runs/${encodeURIComponent(runId)}`}>
              <PlayCircle aria-hidden="true" size={16} /> 打开完整 Run
            </a>
            <a href="/specs">
              <GitBranch aria-hidden="true" size={16} /> 返回 Spec/KG
            </a>
          </nav>
          <GenerationRun api={api} runId={runId} />
        </>
      )}
    </div>
  );
}
