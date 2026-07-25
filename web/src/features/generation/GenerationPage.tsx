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
import { compactDateTime, ResourceIdentity, TechnicalDetails } from "../../components/identity";
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
import { profileKey } from "../execution-profiles";

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

interface GenerationProjectContext {
  constraintArtifactId: string;
  constraintRefName: string;
  constraintRevision: number;
  contentArtifactId: string;
  contentRefName: string;
  contentRevision: number;
  projectId: string;
  projectName: string;
  sourceArtifactIds: string[];
}

function parseProjectContext(searchParams: URLSearchParams): {
  error: string | null;
  value: GenerationProjectContext | null;
} {
  const projectId = searchParams.get("project")?.trim() ?? "";
  if (!projectId) return { error: null, value: null };
  const contentArtifactId = searchParams.get("content")?.trim() ?? "";
  const contentRefName = searchParams.get("contentRef")?.trim() ?? "";
  const revisionText = searchParams.get("contentRevision")?.trim() ?? "";
  const constraintArtifactId = searchParams.get("constraint")?.trim() ?? "";
  const constraintRefName = searchParams.get("constraintRef")?.trim() ?? "";
  const constraintRevisionText = searchParams.get("constraintRevision")?.trim() ?? "";
  if (
    !contentArtifactId ||
    !contentRefName ||
    !/^[1-9]\d*$/u.test(revisionText) ||
    !constraintArtifactId ||
    !constraintRefName ||
    !/^[1-9]\d*$/u.test(constraintRevisionText)
  ) {
    return {
      error: "项目入口缺少准确的当前内容或规则版本，请返回项目刷新后重试。",
      value: null,
    };
  }
  const sources = [
    ...new Set(
      searchParams
        .getAll("source")
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ].sort();
  if (sources.length > 64 || sources.some((item) => item.length > 512)) {
    return { error: "项目入口携带的材料来源超出允许范围，请返回项目重新选择。", value: null };
  }
  return {
    error: null,
    value: {
      constraintArtifactId,
      constraintRefName,
      constraintRevision: Number(constraintRevisionText),
      contentArtifactId,
      contentRefName,
      contentRevision: Number(revisionText),
      projectId,
      projectName: searchParams.get("projectName")?.trim() || projectId.replace(/^project:/u, ""),
      sourceArtifactIds: sources,
    },
  };
}

function appendImmutableCatalogItem<T extends { artifact: { artifact_id: string } }>(
  items: T[],
  exact: T,
): T[] {
  return items.some((item) => item.artifact.artifact_id === exact.artifact.artifact_id)
    ? items
    : [...items, exact];
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

function publicationLabel(refName: string): string {
  const leaf = refName.split("/").filter(Boolean).pop() ?? refName;
  if (["head", "live", "current"].includes(leaf)) return "当前正式内容";
  const domain = domainLabel(`domain:${leaf}`);
  return domain === leaf ? `正式内容 · ${leaf}` : `${domain}正式内容`;
}

function specLabel(spec: SpecView): string {
  if (spec.ref_name && spec.ref_value)
    return `${publicationLabel(spec.ref_name)} · 第 ${spec.ref_value.revision} 版`;
  return "尚未发布的内容版本";
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
  if (constraint.constraints.length === 0) return "没有额外规则（仍会执行基础校验）";
  return summary ? `${summary} · ${count}` : count;
}

function executionProfileLabel(profile: ExecutionProfile): string {
  if (!profile.profile.profile_id.startsWith("builtin.")) return profile.display_name;
  if (profile.profile_kind === "generation") return "系统默认内容生成方案";
  if (profile.profile_kind === "environment") return "标准试玩环境（Aureus）";
  if (profile.profile_kind === "config_export") return "标准游戏配置表（CSV）";
  return profile.display_name;
}

function constraintOptionLabel(
  constraint: ConstraintCatalogItem,
  collidingSummaries: number,
  collisionOrdinal = 1,
): string {
  const summary = constraintLabel(constraint);
  if (collidingSummaries < 2) return summary;
  return `${summary} · ${compactDateTime(constraint.artifact.created_at)}创建 · 同名版本 ${collisionOrdinal}`;
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

function GenerationAuthoring({
  api,
  onAccepted,
  projectContext,
}: {
  api: GenerationApi;
  onAccepted(runId: string): void;
  projectContext: GenerationProjectContext | null;
}) {
  const catalog = useQuery({
    queryFn: async () => {
      let [specs, constraints, profiles, replayRuns] = await Promise.all([
        api.listSpecs(null),
        api.listConstraints(null),
        api.listExecutionProfiles(null),
        api.listReplaySourceRuns(null),
      ]);
      if (projectContext) {
        const exactSpec = await api.getSpec(projectContext.contentArtifactId);
        if (
          exactSpec.artifact.artifact_id !== projectContext.contentArtifactId ||
          exactSpec.ref_name !== projectContext.contentRefName ||
          exactSpec.ref_value?.artifact_id !== projectContext.contentArtifactId ||
          exactSpec.ref_value.revision !== projectContext.contentRevision
        ) {
          throw new Error("The project content authority changed before generation authoring loaded.");
        }
        specs = { ...specs, items: appendImmutableCatalogItem(specs.items, exactSpec) };
        const exactConstraint = await api.getConstraint(projectContext.constraintArtifactId);
        if (exactConstraint.artifact.artifact_id !== projectContext.constraintArtifactId) {
          throw new Error("The project constraint authority differed from its exact binding.");
        }
        constraints = {
          ...constraints,
          items: appendImmutableCatalogItem(constraints.items, exactConstraint),
        };
      }
      return { constraints, profiles, replayRuns, specs };
    },
    queryKey: [
      "generation",
      "authoring-catalog",
      projectContext?.projectId ?? null,
      projectContext?.contentArtifactId ?? null,
      projectContext?.contentRevision ?? null,
      projectContext?.constraintArtifactId ?? null,
    ],
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
  const [mode, setMode] = useState<"" | LlmExecutionMode>("live");
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

    if (projectContext) {
      setSpecId(projectContext.contentArtifactId);
      setConstraintId(projectContext.constraintArtifactId);
    }

    const refBound = catalog.data.specs.items.filter((item) => item.ref_name && item.ref_value);
    if (catalog.data.specs.next_cursor == null && refBound.length === 1) {
      setSpecId((current) => current || refBound[0]!.artifact.artifact_id);
    }
    if (catalog.data.constraints.next_cursor == null && catalog.data.constraints.items.length === 1) {
      setConstraintId((current) => current || catalog.data.constraints.items[0]!.artifact.artifact_id);
    }
    if (catalog.data.profiles.next_cursor == null) {
      const availableGeneration = catalog.data.profiles.items.filter(
        (profile) =>
          profile.status === "active" &&
          profile.profile_kind === "generation" &&
          supportsRunKind(profile, "generation.propose"),
      );
      const availableEnvironments = catalog.data.profiles.items.filter(
        (profile) => profile.status === "active" && profile.profile_kind === "environment",
      );
      if (availableGeneration.length === 1) {
        const selected = availableGeneration[0]!;
        setGenerationKey((current) => current || profileKey(selected));
        if (selected.domain_scope.domain_ids.length === 1) {
          setDomainIds((current) => (current.length ? current : [...selected.domain_scope.domain_ids]));
        }
      }
      if (availableEnvironments.length === 1) {
        const selected = availableEnvironments[0]!;
        setEnvironmentKey((current) => current || profileKey(selected));
        const availableExports = catalog.data.profiles.items.filter(
          (profile) =>
            profile.status === "active" &&
            profile.profile_kind === "config_export" &&
            supportsRunKind(profile, "generation.propose") &&
            sameProfile(profile.target_environment_profile, selected.profile),
        );
        if (availableExports.length === 1) {
          setExportKeys((current) => (current.length ? current : [profileKey(availableExports[0]!)]));
        }
      }
    }
  }, [catalog.data, projectContext]);

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
  const constraintCollisionOrdinals = new Map<string, number>();
  const constraintsByLabel = new Map<string, ConstraintCatalogItem[]>();
  for (const item of constraints) {
    const label = constraintLabel(item);
    constraintsByLabel.set(label, [...(constraintsByLabel.get(label) ?? []), item]);
  }
  for (const items of constraintsByLabel.values()) {
    items
      .sort(
        (left, right) =>
          (left.artifact.created_at ?? "").localeCompare(right.artifact.created_at ?? "") ||
          left.artifact.artifact_id.localeCompare(right.artifact.artifact_id),
      )
      .forEach((item, index) => constraintCollisionOrdinals.set(item.artifact.artifact_id, index + 1));
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
      source_artifact_ids: projectContext?.sourceArtifactIds ?? [],
      target: {
        expected_ref: selectedSpec.ref_value,
        ref_name: selectedSpec.ref_name,
      },
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
    <>
      {projectContext && (
        <section className="gf-generation__project-context" aria-label="项目上下文">
          <GitBranch aria-hidden="true" size={18} />
          <div>
            <strong>
              已绑定{projectContext.projectName}项目的当前版本与 {projectContext.sourceArtifactIds.length}{" "}
              份材料
            </strong>
            <span>提交时会再次核对内容发布位置和版本；若项目已更新，操作会停止并要求刷新。</span>
          </div>
        </section>
      )}
      <div className="gf-generation__authoring-layout">
        <section className="gf-generation__authoring" aria-labelledby="generation-input-title">
          <header>
            <p className="gf-generation__kicker">从策划目标开始</p>
            <h2 id="generation-input-title">描述你想生成或修改的内容</h2>
            <p>系统会绑定明确的内容版本和规则，AI 只提交候选，不会直接改动正式内容。</p>
          </header>
          <form
            className="gf-form"
            onSubmit={(event) => {
              event.preventDefault();
              submit();
            }}
          >
            <label className="gf-generation__goal-field">
              你想让 AI 做什么？
              <textarea
                onChange={(event) => setGoal(event.target.value)}
                placeholder="例如：把前哨任务的金币奖励调整到规则允许的范围，并保持任务链可完成。"
                rows={5}
                value={goal}
              />
            </label>
            {refBoundSpecs.length > 5 && (
              <label>
                搜索内容版本
                <input
                  onChange={(event) => setSpecQuery(event.target.value)}
                  placeholder="按发布位置或版本搜索"
                  type="search"
                  value={specQuery}
                />
              </label>
            )}
            <label>
              要修改的内容版本
              <select
                disabled={projectContext !== null}
                onChange={(event) => setSpecId(event.target.value)}
                value={specId}
              >
                <option value="">请选择一个已发布的内容版本</option>
                {visibleSpecs.map((item) => (
                  <option key={item.artifact.artifact_id} value={item.artifact.artifact_id}>
                    {specLabel(item)}
                  </option>
                ))}
              </select>
            </label>
            <CatalogPageControl
              label="内容版本"
              onLoad={() => void readCatalogPage(specCatalog, setSpecCatalog, api.listSpecs.bind(api), false)}
              onRestart={() =>
                void readCatalogPage(specCatalog, setSpecCatalog, api.listSpecs.bind(api), true)
              }
              state={specCatalog}
            />
            {constraints.length > 5 && (
              <label>
                搜索规则
                <input
                  onChange={(event) => setConstraintQuery(event.target.value)}
                  placeholder="按规则名称搜索"
                  type="search"
                  value={constraintQuery}
                />
              </label>
            )}
            <label>
              本次遵守的规则
              <select
                disabled={projectContext !== null}
                onChange={(event) => setConstraintId(event.target.value)}
                value={constraintId}
              >
                <option value="">请选择规则版本</option>
                {visibleConstraints.map((item) => (
                  <option key={item.artifact.artifact_id} value={item.artifact.artifact_id}>
                    {constraintOptionLabel(
                      item,
                      constraintLabelCounts.get(constraintLabel(item)) ?? 0,
                      constraintCollisionOrdinals.get(item.artifact.artifact_id),
                    )}
                  </option>
                ))}
              </select>
            </label>
            <CatalogPageControl
              label="规则版本"
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
            <fieldset>
              <legend>内容领域</legend>
              {!selectedGeneration ? (
                <p>请在高级设置中选择 AI 生成方案。</p>
              ) : domainOptions.length === 0 ? (
                <p>所选 AI 生成方案没有可用领域，不能启动生成。</p>
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
            <details
              className="gf-generation__advanced-settings"
              open={
                selectedGeneration === undefined ||
                selectedEnvironment === undefined ||
                selectedExports.length === 0 ||
                mode === ""
                  ? true
                  : undefined
              }
            >
              <summary>高级设置</summary>
              <div className="gf-form">
                <label>
                  AI 生成方案
                  <select
                    onChange={(event) => {
                      setGenerationKey(event.target.value);
                      setDomainIds([]);
                    }}
                    value={generationKey}
                  >
                    <option value="">请选择可用的 AI 生成方案</option>
                    {generationProfiles.map((item) => (
                      <option key={profileKey(item)} value={profileKey(item)}>
                        {executionProfileLabel(item)}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  试玩环境
                  <select
                    onChange={(event) => {
                      setEnvironmentKey(event.target.value);
                      setExportKeys([]);
                    }}
                    value={environmentKey}
                  >
                    <option value="">请选择试玩环境</option>
                    {environmentProfiles.map((item) => (
                      <option key={profileKey(item)} value={profileKey(item)}>
                        {executionProfileLabel(item)}
                      </option>
                    ))}
                  </select>
                </label>
                <fieldset>
                  <legend>候选配置格式</legend>
                  {!selectedEnvironment ? (
                    <p>请先选择试玩环境。</p>
                  ) : exportProfiles.length === 0 ? (
                    <p>该试玩环境没有可用的配置格式。</p>
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
                          {executionProfileLabel(item)}
                        </label>
                      );
                    })
                  )}
                </fieldset>
                <label>
                  AI 运行方式
                  <select
                    onChange={(event) => {
                      const next = event.target.value as "" | LlmExecutionMode;
                      setMode(next);
                      if (next !== "replay") setReplaySourceRunId("");
                    }}
                    value={mode}
                  >
                    <option value="live">在线生成（推荐）</option>
                    <option value="record">在线生成并保存回放</option>
                    <option value="replay">使用历史回放（测试用）</option>
                  </select>
                </label>
                <CatalogPageControl
                  label="运行方案"
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
              </div>
            </details>
            {mode === "replay" && (
              <>
                <label>
                  回放来源
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
              <p role="alert">所选内容没有明确的发布版本，不能作为正式修改目标。</p>
            )}
            <button disabled={!canSubmit} type="submit">
              {attempt?.pending ? "正在解析并提交…" : "开始生成"}
            </button>
          </form>
          {attempt && <MutationFailure attempt={attempt} onRetry={() => void execute(attempt)} />}
        </section>

        <aside className="gf-generation__authority-ledger" aria-label="本次生成使用的内容与规则">
          <p className="gf-generation__kicker">提交前确认</p>
          <h2>本次生成会使用</h2>
          <dl>
            <div>
              <dt>内容版本</dt>
              <dd>{selectedSpec ? specLabel(selectedSpec) : "未选择"}</dd>
            </div>
            <div>
              <dt>发布位置</dt>
              <dd>
                {selectedSpec?.ref_name && selectedSpec.ref_value
                  ? publicationLabel(selectedSpec.ref_name)
                  : "未绑定"}
              </dd>
            </div>
            <div>
              <dt>规则</dt>
              <dd>
                {selectedConstraint
                  ? constraintOptionLabel(
                      selectedConstraint,
                      constraintLabelCounts.get(constraintLabel(selectedConstraint)) ?? 0,
                      constraintCollisionOrdinals.get(selectedConstraint.artifact.artifact_id),
                    )
                  : "未选择"}
              </dd>
            </div>
            <div>
              <dt>AI 方案</dt>
              <dd>{selectedGeneration ? executionProfileLabel(selectedGeneration) : "未选择"}</dd>
            </div>
            <div>
              <dt>试玩环境</dt>
              <dd>{selectedEnvironment ? executionProfileLabel(selectedEnvironment) : "未选择"}</dd>
            </div>
            <div>
              <dt>配置格式</dt>
              <dd>
                {selectedExports.length ? selectedExports.map(executionProfileLabel).join(" · ") : "未选择"}
              </dd>
            </div>
            {projectContext && (
              <div>
                <dt>项目材料</dt>
                <dd>{projectContext.sourceArtifactIds.length} 份已绑定来源</dd>
              </div>
            )}
          </dl>
          <TechnicalDetails
            items={[
              ...(selectedSpec
                ? [
                    {
                      label: "内容标识",
                      value: selectedSpec.artifact.artifact_id,
                    },
                  ]
                : []),
              ...(selectedSpec?.ref_name ? [{ label: "发布位置", value: selectedSpec.ref_name }] : []),
              ...(selectedConstraint
                ? [
                    {
                      label: "规则版本标识",
                      value: selectedConstraint.artifact.artifact_id,
                    },
                  ]
                : []),
              ...(selectedGeneration
                ? [
                    {
                      label: "AI 方案标识",
                      value: profileKey(selectedGeneration),
                    },
                  ]
                : []),
              ...(selectedEnvironment ? [{ label: "环境标识", value: profileKey(selectedEnvironment) }] : []),
              ...selectedExports.map((item) => ({
                label: "配置格式标识",
                value: profileKey(item),
              })),
              ...(selectedConstraint
                ? [
                    {
                      label: "规则语法版本",
                      value: selectedConstraint.dsl_grammar_version,
                    },
                  ]
                : []),
            ]}
          />
        </aside>
      </div>
    </>
  );
}

type CandidateArtifact = PassedGenerationCandidate["patch"];

function artifactKindLabel(kind: CandidateArtifact["kind"]): string {
  const labels: Partial<Record<CandidateArtifact["kind"], string>> = {
    checker_run: "确定性检查证据",
    config_export: "可试玩配置",
    ir_snapshot: "修改后预览",
    patch: "修改方案",
    regression_evidence: "回归验证证据",
    review_report: "AI 建议报告",
    simulation_run: "模拟验证证据",
  };
  return labels[kind] ?? "运行产物";
}

function approvalStatusLabel(status: string): string {
  return (
    {
      approved: "已批准",
      draft: "草稿",
      pending: "等待审批",
      rejected: "已驳回",
      superseded: "已由新版本替代",
    }[status] ?? status
  );
}

function validationStatusLabel(status: string): string {
  return (
    {
      failed: "未通过",
      not_started: "尚未验证",
      passed: "已通过",
      running: "验证中",
    }[status] ?? status
  );
}

function ArtifactCard({ artifact, label }: { artifact: CandidateArtifact; label: string }) {
  return (
    <article className="gf-generation__artifact-card">
      <ResourceIdentity
        actionLabel="查看详情"
        description={`${artifactKindLabel(artifact.kind)} · ${compactDateTime(artifact.created_at)}`}
        details={[
          { label: "内容标识", value: artifact.artifact_id },
          { label: "数据类型", value: artifact.kind },
          { label: "结构版本", value: artifact.payload_schema_id ?? "未声明" },
        ]}
        href={artifactHref(artifact.artifact_id)}
        title={label}
      />
    </article>
  );
}

function ArtifactList({ artifacts }: { artifacts: readonly CandidateArtifact[] }) {
  if (artifacts.length === 0) return <p className="gf-generation__empty-copy">暂无此类工件。</p>;
  return (
    <ul className="gf-generation__artifact-list">
      {artifacts.map((artifact) => (
        <li key={artifact.artifact_id}>
          <ResourceIdentity
            actionLabel="查看证据"
            description={compactDateTime(artifact.created_at)}
            details={[
              { label: "证据标识", value: artifact.artifact_id },
              { label: "数据类型", value: artifact.kind },
              {
                label: "结构版本",
                value: artifact.payload_schema_id ?? "未声明",
              },
            ]}
            href={artifactHref(artifact.artifact_id)}
            title={artifactKindLabel(artifact.kind)}
          />
        </li>
      ))}
    </ul>
  );
}

function IntermediateList({ intermediates }: { intermediates: PassedGenerationCandidate["intermediates"] }) {
  return (
    <TechnicalDetails
      items={intermediates.map((intermediate, index) => ({
        label: `运行记录 ${index + 1}`,
        value: intermediate.artifactId,
      }))}
      summary={`运行记录（${intermediates.length}，仅供审计）`}
    />
  );
}

function OutcomeEvidence({
  evidence,
  intermediates,
  mode = "passed",
}: {
  evidence: readonly CandidateArtifact[];
  intermediates: PassedGenerationCandidate["intermediates"];
  mode?: "passed" | "rejected";
}) {
  const deterministic = evidence.filter(
    (artifact) => artifact.kind === "checker_run" || artifact.kind === "regression_evidence",
  );
  const simulation = evidence.filter((artifact) => artifact.kind === "simulation_run");
  const suggestion = evidence.filter((artifact) => artifact.kind === "review_report");
  return (
    <section className="gf-generation__evidence" aria-labelledby="generation-evidence-title">
      <header>
        <p className="gf-generation__kicker">检查结果</p>
        <h2 id="generation-evidence-title">
          {mode === "passed" ? "为什么这个候选可以继续" : "为什么这份提议被拦截"}
        </h2>
      </header>
      <EvidenceSections
        deterministic={deterministic.length > 0 ? <ArtifactList artifacts={deterministic} /> : undefined}
        simulation={simulation.length > 0 ? <ArtifactList artifacts={simulation} /> : undefined}
        suggestion={suggestion.length > 0 ? <ArtifactList artifacts={suggestion} /> : undefined}
      />
      {intermediates.length > 0 && (
        <section className="gf-generation__supporting" aria-labelledby="generation-supporting-title">
          <h3 id="generation-supporting-title">可回放的运行记录</h3>
          <p>仅用于排查和审计，不影响本次检查结论。</p>
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
        <p className="gf-generation__kicker">本次生成的内容</p>
        <h2 id="generation-candidate-title">从修改方案到可试玩配置</h2>
        <p>这些内容仍是候选版本，完成检查、审批和应用后才会更新正式内容。</p>
      </header>
      <div className="gf-generation__candidate-chain">
        <ArtifactCard artifact={candidate.patch} label="修改方案" />
        <ArrowRight aria-hidden="true" size={18} />
        <ArtifactCard artifact={candidate.preview} label="修改后预览" />
        {candidate.configExports.map((artifact) => (
          <div className="gf-generation__candidate-next" key={artifact.artifact_id}>
            <ArrowRight aria-hidden="true" size={18} />
            <ArtifactCard artifact={artifact} label="可试玩配置" />
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
        <p className="gf-generation__kicker">被拦截的候选记录</p>
        <h2 id="generation-rejected-candidate-title">未进入正式流程的修改与预览</h2>
        <p>两项仅用于解释和审计；系统没有创建审批流程，也没有生成可试玩配置。</p>
      </header>
      <div className="gf-generation__candidate-chain">
        <ArtifactCard artifact={candidate.patch} label="被拦截的修改方案" />
        <ArrowRight aria-hidden="true" size={18} />
        <ArtifactCard artifact={candidate.preview} label="被拦截的内容预览" />
      </div>
    </section>
  );
}

function generationFieldLabel(fieldPath: string): string {
  return fieldPath === "reward.gold" ? "金币奖励" : fieldPath;
}

const rejectedOperationLabels = {
  add_entity: "新增内容实体",
  add_relation: "新增内容关系",
  delete_entity: "删除内容实体",
  delete_relation: "删除内容关系",
  replace_subgraph: "替换一组关联内容",
  set_entity_attr: "修改实体属性",
  set_relation_attr: "修改关系属性",
} as const;

const proposalRejectionLabels = {
  candidate_work_budget_exceeded: "候选内容规模超过当前安全检查范围",
  empty_ops: "AI 没有给出可执行的修改",
  identity_conflict: "内容名称或关系引用无法唯一对应",
  inapplicable_ops: "这份修改无法应用到当前内容",
  malformed_ops: "AI 给出的修改格式不完整",
  model_response_unparseable: "AI 返回的内容无法解析",
} as const;

const deterministicFindingLabels: Readonly<Record<string, string>> = {
  cyclic_dependency: "内容依赖形成循环",
  dangling_reference: "引用了不存在的内容",
  duplicate_identity: "存在重复的内容名称或标识",
  invalid_generation_proposal: "生成提议无法安全应用",
  unreachable_content: "存在无法到达的内容",
};

function rejectedBlockerLabel(blocker: GateRejectedGenerationOutcome["blockers"][number]): string {
  if (blocker.kind === "numeric-limit") {
    return `${generationFieldLabel(blocker.fieldPath)} ${blocker.actualValue} 超过规则上限 ${blocker.limit}`;
  }
  if (blocker.kind === "proposal-rejection") return proposalRejectionLabels[blocker.reasonCode];
  return deterministicFindingLabels[blocker.defectClass] ?? "确定性检查发现内容问题";
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
        title={`拦截成功：${rejectedBlockerLabel(primaryBlocker)}`}
      />
      <section className="gf-generation__rejection-summary" aria-label="门禁拦截摘要">
        <section aria-label="提议改动">
          <p className="gf-generation__kicker">提议改动</p>
          <h2>候选值没有进入正式内容</h2>
          <ul>
            {outcome.changes.map((change) =>
              change.kind === "numeric-field" ? (
                <li key={change.operationId}>
                  <span>{change.entityTitle ?? "一个内容项"}</span>{" "}
                  <strong>
                    {generationFieldLabel(change.fieldPath)} {change.oldValue} → {change.newValue}
                  </strong>
                  <TechnicalDetails
                    items={[
                      { label: "内容标识", value: change.entityId },
                      { label: "字段路径", value: change.fieldPath },
                      { label: "操作标识", value: change.operationId },
                    ]}
                    summary="查看改动技术信息"
                  />
                </li>
              ) : (
                <li key={change.operationId}>
                  <strong>{rejectedOperationLabels[change.operationKind]}</strong>
                  <span>这项提议未进入正式内容。</span>
                  <TechnicalDetails
                    items={[
                      { label: "操作标识", value: change.operationId },
                      { label: "操作类型", value: change.operationKind },
                      { label: "目标标识", value: change.target },
                      ...(change.sourceId ? [{ label: "来源标识", value: change.sourceId }] : []),
                      ...(change.destinationId
                        ? [{ label: "目标内容标识", value: change.destinationId }]
                        : []),
                    ]}
                    summary="查看改动技术信息"
                  />
                </li>
              ),
            )}
          </ul>
        </section>
        <section aria-label="拦截原因">
          <p className="gf-generation__kicker">拦截原因</p>
          <h2>确定性检查已确认问题</h2>
          <ul>
            {outcome.blockers.map((blocker, index) => (
              <li key={`${blocker.kind}:${index}`}>
                <strong>{rejectedBlockerLabel(blocker)}</strong>
                <span>系统已阻止这份提议进入正式流程。</span>
                <TechnicalDetails
                  items={
                    blocker.kind === "numeric-limit"
                      ? [
                          { label: "规则标识", value: blocker.constraintId },
                          { label: "内容标识", value: blocker.entityId },
                          { label: "字段路径", value: blocker.fieldPath },
                        ]
                      : blocker.kind === "proposal-rejection"
                        ? [{ label: "拦截原因代码", value: blocker.reasonCode }]
                        : [
                            { label: "问题类型", value: blocker.defectClass },
                            ...(blocker.entityIds.length > 0
                              ? [{ label: "相关内容标识", value: blocker.entityIds.join(", ") }]
                              : []),
                            ...(blocker.relationIds.length > 0
                              ? [{ label: "相关关系标识", value: blocker.relationIds.join(", ") }]
                              : []),
                          ]
                  }
                  summary="查看拦截技术信息"
                />
              </li>
            ))}
          </ul>
        </section>
        <section aria-label="正式内容状态">
          <p className="gf-generation__kicker">正式内容</p>
          <h2>正式内容未变化</h2>
          <p>这份提议只作为记录保留；系统没有创建待审批修改，也没有生成试玩配置，正式版本保持不变。</p>
        </section>
      </section>
      <details className="gf-generation__technical-evidence">
        <summary>查看技术证据（{technicalEvidenceCount}）</summary>
        <div>
          <RejectedCandidateChain candidate={outcome.candidate} />
          <OutcomeEvidence
            evidence={outcome.candidate.evidence}
            intermediates={outcome.candidate.intermediates}
            mode="rejected"
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
        <p className="gf-generation__kicker">版本继承关系</p>
        <h2 id="generation-revision-history-title">旧审批状态不会继承</h2>
        <p>上一版的审批决定和证据会完整保留；当前版本需要重新完成自己的检查与审批。</p>
      </header>
      <div className="gf-generation__approval-compare">
        <section aria-label="旧 Patch workflow 状态">
          <h3>上一版修改 · 第 {previous.subject_revision} 版</h3>
          <dl>
            <div>
              <dt>状态</dt>
              <dd>{approvalStatusLabel(previous.status)}</dd>
            </div>
            <div>
              <dt>是否为当前版本</dt>
              <dd>{previousBinding.is_current_head ? "是" : "否"}</dd>
            </div>
            <div>
              <dt>验证证据</dt>
              <dd>{previousEvidenceCount}</dd>
            </div>
            <div>
              <dt>审批决定</dt>
              <dd>{previous.decisions.length}</dd>
            </div>
          </dl>
          <TechnicalDetails
            items={[
              { label: "修改标识", value: previousPatch.artifact.artifact_id },
              ...(previous.evidence_set_artifact_id
                ? [
                    {
                      label: "证据集标识",
                      value: previous.evidence_set_artifact_id,
                    },
                  ]
                : []),
            ]}
          />
        </section>
        <section aria-label="新 Patch workflow 状态">
          <h3>当前修改 · 第 {current.subject_revision} 版</h3>
          <dl>
            <div>
              <dt>状态</dt>
              <dd>{approvalStatusLabel(current.status)}</dd>
            </div>
            <div>
              <dt>验证证据</dt>
              <dd>{currentEvidenceCount}</dd>
            </div>
            <div>
              <dt>审批决定</dt>
              <dd>{current.decisions.length}</dd>
            </div>
          </dl>
          <TechnicalDetails
            items={[
              {
                label: "当前修改标识",
                value: outcome.patch.value.artifact.artifact_id,
              },
              ...(outcome.patch.value.patch.supersedes_artifact_id
                ? [
                    {
                      label: "上一版修改标识",
                      value: outcome.patch.value.patch.supersedes_artifact_id,
                    },
                  ]
                : []),
              ...(current.evidence_set_artifact_id
                ? [
                    {
                      label: "证据集标识",
                      value: current.evidence_set_artifact_id,
                    },
                  ]
                : []),
            ]}
          />
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
        description="系统已完成第一轮确定性检查。候选仍需内容检查、审批并由你明确应用，才会进入正式版本。"
        state="terminal"
        title="候选内容已通过初步检查"
      />
      <CandidateChain candidate={candidate} />
      <section className="gf-generation__workflow-ledger" aria-labelledby="generation-workflow-title">
        <header>
          <p className="gf-generation__kicker">后续流程</p>
          <h2 id="generation-workflow-title">这次修改的当前状态</h2>
        </header>
        <dl>
          <div>
            <dt>基于</dt>
            <dd>
              正式内容
              {baseSpec.ref_value ? ` · 第 ${baseSpec.ref_value.revision} 版` : ""}
            </dd>
          </div>
          <div>
            <dt>遵守规则</dt>
            <dd>{constraint.constraints.length} 条已发布规则</dd>
          </div>
          <div>
            <dt>修改版本</dt>
            <dd>第 {patch.value.patch.revision} 版</dd>
          </div>
          <div>
            <dt>审批</dt>
            <dd>{approvalStatusLabel(approvalItem.status)}</dd>
          </div>
          <div>
            <dt>完整验证</dt>
            <dd>{validationStatusLabel(patch.value.validation_status)}</dd>
          </div>
          <div>
            <dt>是否为当前修改</dt>
            <dd>{binding.is_current_head ? "是" : `否 · 当前为第 ${binding.subject_head_revision} 版`}</dd>
          </div>
        </dl>
        <TechnicalDetails
          items={[
            { label: "原内容标识", value: baseSpec.artifact.artifact_id },
            { label: "规则版本标识", value: constraint.artifact.artifact_id },
            { label: "修改标识", value: patch.value.artifact.artifact_id },
          ]}
        />
        <a className="gf-primary-link" href={`/patches/${encodeURIComponent(candidate.patch.artifact_id)}`}>
          打开修改详情 <ArrowRight aria-hidden="true" size={16} />
        </a>
      </section>
      <SnapshotDiffView diff={diff.diff} entries={diff.page.items} />
      <PreviousApproval outcome={outcome} />
      <OutcomeEvidence evidence={candidate.evidence} intermediates={candidate.intermediates} />
      <nav className="gf-generation__next-actions" aria-label="候选后续动作">
        <div>
          <p className="gf-generation__kicker">继续完善候选内容</p>
          <h2>下一步</h2>
        </div>
        <a
          href={sourceRunHref("/reviews", candidate.runId, {
            snapshot: candidate.preview.artifact_id,
            constraint: constraint.artifact.artifact_id,
          })}
        >
          检查这次修改
        </a>
        {candidate.configExports.map((config) => {
          const context = {
            preview: candidate.preview.artifact_id,
            config: config.artifact_id,
            constraint: constraint.artifact.artifact_id,
          };
          return (
            <span className="gf-generation__next-config" key={config.artifact_id}>
              <a
                href={sourceRunHref("/playtest", candidate.runId, {
                  ...context,
                  action: "derive",
                })}
              >
                创建试玩任务
              </a>
              <a href={sourceRunHref("/playtest", candidate.runId, context)}>直接进入自动试玩</a>
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
        description="正在核对本次生成的修改、预览和后续操作资格。"
        state="loading"
        title="正在读取生成结果"
      />
    );
  }
  if (outcome.isError) {
    if (outcome.error instanceof ApiProblemError) return <ProblemPanel problem={outcome.error.problem} />;
    const unsafe = outcome.error instanceof UnsafeGenerationOutcomeError;
    return (
      <>
        <StatePanel
          action={
            unsafe ? undefined : (
              <button className="gf-secondary-button" onClick={() => void outcome.refetch()} type="button">
                重新读取生成结果
              </button>
            )
          }
          description={
            unsafe
              ? "生成记录之间存在不一致，系统已停止展示候选内容和后续操作。"
              : "生成结果读取失败；页面不会使用不完整数据猜测后续操作资格。"
          }
          state="error"
          title={unsafe ? "生成结果无法安全展示" : "无法读取生成结果"}
        />
        {unsafe && (
          <TechnicalDetails
            items={[{ label: "完整性校验信息", value: outcome.error.message }]}
            summary="查看校验技术信息"
          />
        )}
      </>
    );
  }
  if (outcome.data.kind === "passed") return <PassedOutcome outcome={outcome.data} />;
  if (outcome.data.kind === "gate-rejected") return <GateRejectedOutcome outcome={outcome.data} />;
  if (outcome.data.kind === "failure") return <FailedOutcome candidate={outcome.data.candidate} />;
  return (
    <>
      <StatePanel
        description="本次生成的记录没有通过完整性校验，系统已停止后续操作。"
        state="error"
        title="生成结果无法安全展示"
      />
      <TechnicalDetails
        items={[{ label: "完整性校验原因", value: outcome.data.candidate.reason }]}
        summary="查看校验技术信息"
      />
    </>
  );
}

function GenerationRun({ api, runId }: { api: GenerationApi; runId: string }) {
  const [events, setEvents] = useState<RunEventItem[]>([]);
  const [streamState, setStreamState] = useState<RunEventStreamState>({
    status: "idle",
  });
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
      description="系统已完成生成后的第一轮确定性检查。"
      state={
        run.data?.status === "succeeded"
          ? "terminal"
          : run.data && terminalStatuses.has(run.data.status)
            ? "error"
            : "streaming"
      }
      title="初步确定性检查"
    />
  ) : null;

  return (
    <section className="gf-generation__run" aria-labelledby="generation-run-title">
      <header>
        <p className="gf-generation__kicker">AI 内容助手</p>
        <h1 id="generation-run-title">生成结果</h1>
        <p>你可以在这里查看候选内容、检查结论和下一步操作。</p>
        <TechnicalDetails items={[{ label: "运行标识", value: runId }]} summary="运行技术信息" />
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
  const projectContextResult = useMemo(() => parseProjectContext(searchParams), [searchParams]);

  return (
    <div className="gf-page gf-generation">
      {runId === null ? (
        <>
          <header className="gf-generation__hero">
            <div>
              <p className="gf-generation__kicker">AI 内容助手</p>
              <h1>内容生成</h1>
              <p>说清楚策划目标，AI 会基于当前内容与规则生成候选，并在提交前自动检查。</p>
            </div>
            <div className="gf-generation__hero-marks" aria-label="生成原则">
              <span>
                <Database aria-hidden="true" size={16} /> 固定内容版本
              </span>
              <span>
                <ShieldCheck aria-hidden="true" size={16} /> 确定性检查
              </span>
              <span>
                <Bot aria-hidden="true" size={16} /> 只生成候选
              </span>
            </div>
          </header>
          {projectContextResult.error ? (
            <StatePanel
              action={
                <a className="gf-secondary-button" href="/projects">
                  返回游戏项目
                </a>
              }
              description={projectContextResult.error}
              state="error"
              title="项目版本绑定不完整"
            />
          ) : (
            <GenerationAuthoring
              api={api}
              onAccepted={(acceptedRunId) => {
                const next = new URLSearchParams(searchParams);
                next.set("run", acceptedRunId);
                setSearchParams(next);
              }}
              projectContext={projectContextResult.value}
            />
          )}
        </>
      ) : (
        <>
          <nav aria-label="生成运行导航" className="gf-generation__run-nav">
            <button
              className="gf-secondary-button"
              onClick={() => {
                const next = new URLSearchParams(searchParams);
                next.delete("run");
                setSearchParams(projectContextResult.value ? next : {});
              }}
              type="button"
            >
              开始另一次生成
            </button>
            <a href={`/runs/${encodeURIComponent(runId)}`}>
              <PlayCircle aria-hidden="true" size={16} /> 打开完整 Run
            </a>
            {projectContextResult.value ? (
              <a href={`/projects/${encodeURIComponent(projectContextResult.value.projectId)}`}>
                <GitBranch aria-hidden="true" size={16} /> 返回{projectContextResult.value.projectName}项目
              </a>
            ) : (
              <a href="/specs">
                <GitBranch aria-hidden="true" size={16} /> 返回 Spec/KG
              </a>
            )}
          </nav>
          <GenerationRun api={api} runId={runId} />
        </>
      )}
    </div>
  );
}
