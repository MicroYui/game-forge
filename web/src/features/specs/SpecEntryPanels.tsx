import { useQuery } from "@tanstack/react-query";
import { Bot, FileUp, UserRoundPen } from "lucide-react";
import { useEffect, useState } from "react";

import { createMutationIntent, ReauthenticationRequiredError, type MutationIntent } from "../../api/csrf";
import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import type { components } from "../../api/generated/openapi";
import { ReauthenticationLink } from "../../app/ReauthenticationLink";
import { TechnicalDetails } from "../../components/identity";
import { ProblemPanel, StatePanel } from "../../components/ui";
import {
  specWorkflowApi,
  type ArtifactPage,
  type ConstraintProposalReadView,
  type ConstraintProposeRequest,
  type ConstraintSnapshotView,
  type ExecutionOptionResolveRequest,
  type ExecutionProfilePage,
  type HumanConstraintDraftRequest,
  type HumanSpecUploadRequest,
  type RunAccepted,
  type SpecView,
  type SpecWorkflowApi,
} from "./api";
import { ConstraintRefBindingFields, type ConstraintRefSelection } from "./ConstraintRefBindingFields";

export type SpecEntryPanelsApi = Pick<
  SpecWorkflowApi,
  | "draftConstraint"
  | "listExecutionProfiles"
  | "listRefHistory"
  | "proposeConstraint"
  | "resolveExecutionOption"
  | "uploadSpec"
>;

export interface SpecEntryCatalogs {
  constraints: readonly ConstraintSnapshotView[];
  proposals: readonly ConstraintProposalReadView[];
  sources: readonly ArtifactPage["items"][number][];
  specs: readonly SpecView[];
}

export interface ProjectConstraintAuthoringContext {
  baseConstraintArtifactId: string | null;
  baseConstraintRevision: number | null;
  constraintRefName: string;
  projectId: string;
  projectName: string;
  sourceArtifactIds: readonly string[];
}

const emptyCatalogs: SpecEntryCatalogs = {
  constraints: [],
  proposals: [],
  sources: [],
  specs: [],
};

type ExecutionProfile = ExecutionProfilePage["items"][number];
type ExpectedRefMode = "" | "exact" | "none";
type LlmExecutionMode = ConstraintProposeRequest["llm_execution_mode"];
type ProspectiveConstraintRequest = components["schemas"]["ProspectiveConstraintProposeRequestV1"];

interface ProfileState {
  error: Error | null;
  items: ExecutionProfile[];
  loading: boolean;
  nextCursor: string | null;
  readSnapshotId: string;
}

interface HumanAttempt {
  error: Error | null;
  intent: MutationIntent;
  pending: boolean;
  request: HumanConstraintDraftRequest;
  result: ConstraintProposalReadView | null;
}

interface AgentAttempt {
  error: Error | null;
  intent: MutationIntent;
  pending: boolean;
  prospectiveRequest: ProspectiveConstraintRequest;
  request: ExecutionOptionResolveRequest;
  resolvedRequest: ConstraintProposeRequest | null;
  result: RunAccepted | null;
}

interface SpecAttempt {
  error: Error | null;
  intent: MutationIntent;
  pending: boolean;
  request: HumanSpecUploadRequest;
  result: SpecView | null;
}

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("创建请求失败。");
}

function blocksNewIntent(error: Error | null | undefined): boolean {
  return error != null && !(error instanceof ApiProblemError);
}

function splitIds(value: string): string[] {
  return value
    .split(/[\s,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function projectRuleQuery(context: ProjectConstraintAuthoringContext): string {
  const query = new URLSearchParams({
    constraintRef: context.constraintRefName,
    project: context.projectId,
    projectName: context.projectName,
    section: "proposals",
  });
  if (context.baseConstraintArtifactId && context.baseConstraintRevision) {
    query.set("constraint", context.baseConstraintArtifactId);
    query.set("constraintRevision", String(context.baseConstraintRevision));
  }
  for (const sourceArtifactId of context.sourceArtifactIds) query.append("source", sourceArtifactId);
  return query.toString();
}

function contentDomainLabel(domainIds: readonly string[]): string {
  const labels: Record<string, string> = {
    builtin: "内置内容",
    "domain:combat": "战斗系统",
    "domain:economy": "经济系统",
    "domain:narrative": "叙事内容",
    "domain:quest": "任务系统",
    "domain:rewards": "奖励系统",
  };
  return domainIds.map((item) => labels[item] ?? item.replace(/^domain:/u, "")).join(" · ");
}

function parseExpectedRef(
  mode: ExpectedRefMode,
  artifactId: string,
  revision: string,
): HumanConstraintDraftRequest["expected_ref"] | undefined {
  if (mode === "none") return null;
  if (mode !== "exact") return undefined;
  const parsedRevision = Number(revision);
  if (!artifactId.trim() || !Number.isInteger(parsedRevision) || parsedRevision < 1) return undefined;
  return { artifact_id: artifactId.trim(), revision: parsedRevision };
}

function parseConstraintArray(
  value: string,
): { ok: true; value: HumanConstraintDraftRequest["constraints"] } | { ok: false } {
  try {
    const parsed: unknown = JSON.parse(value);
    if (
      !Array.isArray(parsed) ||
      parsed.some((item) => typeof item !== "object" || item === null || Array.isArray(item))
    ) {
      return { ok: false };
    }
    return {
      ok: true,
      value: parsed as HumanConstraintDraftRequest["constraints"],
    };
  } catch {
    return { ok: false };
  }
}

function grammarFromConstraints(parsed: ReturnType<typeof parseConstraintArray> | null): string | null {
  if (!parsed?.ok) return null;
  const values = new Set(
    parsed.value.flatMap((constraint) => {
      if (
        typeof constraint === "object" &&
        constraint !== null &&
        !Array.isArray(constraint) &&
        "dsl_grammar_version" in constraint &&
        typeof constraint.dsl_grammar_version === "string" &&
        constraint.dsl_grammar_version
      ) {
        return [constraint.dsl_grammar_version];
      }
      return [];
    }),
  );
  return values.size === 1 ? [...values][0]! : null;
}

function parseContentObject(
  value: string,
): { ok: true; value: HumanSpecUploadRequest["content_payload"] } | { ok: false } {
  try {
    const parsed: unknown = JSON.parse(value);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return { ok: false };
    return {
      ok: true,
      value: parsed as HumanSpecUploadRequest["content_payload"],
    };
  } catch {
    return { ok: false };
  }
}

function profileKey(profile: ExecutionProfile): string {
  return `${profile.profile.profile_id}@${profile.profile.version}`;
}

function knownSourceOptions(catalogs: SpecEntryCatalogs): { id: string; label: string }[] {
  const values = new Map<string, string>();
  const ordinals = new Map<string, number>();
  for (const source of catalogs.sources) {
    const kindLabel = source.kind === "source_raw" ? "原始策划材料" : "已解析策划材料";
    const createdAt = source.created_at?.slice(0, 10) ?? "时间未知";
    const ordinal = (ordinals.get(kindLabel) ?? 0) + 1;
    ordinals.set(kindLabel, ordinal);
    values.set(source.artifact_id, `${kindLabel} · ${createdAt} · 第 ${ordinal} 份`);
  }
  for (const proposal of catalogs.proposals) {
    for (const binding of proposal.proposal.source_bindings) {
      if (!values.has(binding.source_artifact_id)) {
        values.set(binding.source_artifact_id, `既有提案来源 · ${proposal.proposal.rationale}`);
      }
    }
  }
  return [...values].map(([id, label]) => ({ id, label }));
}

function observedDomainIds(catalogs: SpecEntryCatalogs): string[] {
  const values = new Set<string>();
  for (const artifact of [
    ...catalogs.specs.map((item) => item.artifact),
    ...catalogs.constraints.map((item) => item.artifact),
    ...catalogs.proposals.map((item) => item.artifact),
  ]) {
    if (artifact.domain_scope && artifact.domain_scope !== "all") {
      artifact.domain_scope.domain_ids.forEach((domainId) => values.add(domainId));
    }
  }
  return [...values].sort();
}

function BaseConstraintPicker({
  catalogs,
  disabled = false,
  label,
  onChange,
  value,
}: {
  catalogs: SpecEntryCatalogs;
  disabled?: boolean;
  label: string;
  onChange(value: string): void;
  value: string;
}) {
  return (
    <label>
      {label}
      <select disabled={disabled} onChange={(event) => onChange(event.target.value)} value={value}>
        <option value="">不基于现有规则（从头提取）</option>
        {catalogs.constraints.map((snapshot, index) => (
          <option key={snapshot.artifact.artifact_id} value={snapshot.artifact.artifact_id}>
            规则版本 {index + 1} · {snapshot.constraints.length} 条规则 ·{" "}
            {snapshot.artifact.created_at?.slice(0, 10) ?? "时间未知"}
          </option>
        ))}
      </select>
    </label>
  );
}

function SourceArtifactPicker({
  catalogs,
  disabled = false,
  label,
  onChange,
  value,
}: {
  catalogs: SpecEntryCatalogs;
  disabled?: boolean;
  label: string;
  onChange(value: string): void;
  value: string;
}) {
  const selected = new Set(splitIds(value));
  const options = knownSourceOptions(catalogs);
  function toggle(id: string, checked: boolean) {
    const next = new Set(selected);
    if (checked) next.add(id);
    else next.delete(id);
    onChange([...next].sort().join("\n"));
  }
  return (
    <fieldset className="gf-specs__resource-picker">
      <legend>{label}</legend>
      {options.length === 0 ? (
        <p>当前没有可用的策划材料；可在高级入口添加已有材料标识。</p>
      ) : (
        options.map((option) => (
          <label key={option.id}>
            <input
              checked={selected.has(option.id)}
              disabled={disabled}
              onChange={(event) => toggle(option.id, event.target.checked)}
              type="checkbox"
            />
            <span>{option.label}</span>
          </label>
        ))
      )}
      {disabled ? (
        <p>材料已由当前游戏项目锁定；如需调整，请返回项目材料区。</p>
      ) : (
        <details>
          <summary>高级：添加其他来源标识</summary>
          <textarea
            aria-label={`${label} 高级 Artifact IDs`}
            onChange={(event) => onChange(event.target.value)}
            rows={3}
            value={value}
          />
        </details>
      )}
    </fieldset>
  );
}

function DomainPicker({
  catalogs,
  label,
  onChange,
  value,
}: {
  catalogs: SpecEntryCatalogs;
  label: string;
  onChange(value: string): void;
  value: string;
}) {
  const selected = new Set(splitIds(value));
  const options = observedDomainIds(catalogs);
  function toggle(id: string, checked: boolean) {
    const next = new Set(selected);
    if (checked) next.add(id);
    else next.delete(id);
    onChange([...next].sort().join(" "));
  }
  return (
    <fieldset className="gf-specs__resource-picker">
      <legend>{label}</legend>
      {options.map((domainId) => (
        <label key={domainId}>
          <input
            checked={selected.has(domainId)}
            onChange={(event) => toggle(domainId, event.target.checked)}
            type="checkbox"
          />
          <span>{domainId.replace(/^domain:/, "")}</span>
        </label>
      ))}
      <details>
        <summary>高级：添加目录未展示的域</summary>
        <input
          aria-label={`${label} 高级 domain IDs`}
          onChange={(event) => onChange(event.target.value)}
          type="text"
          value={value}
        />
      </details>
    </fieldset>
  );
}

function isActiveExtractionProfile(profile: ExecutionProfile): boolean {
  return (
    profile.status === "active" &&
    profile.profile_kind === "constraint_extraction" &&
    profile.compatible_run_kinds.some(
      (runKind) => runKind.kind === "constraint_proposal.propose" && runKind.version === 1,
    )
  );
}

function MutationFailure({ error, onRetry }: { error: Error; onRetry(): void }) {
  if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
  if (error instanceof ReauthenticationRequiredError) {
    return (
      <StatePanel
        action={<ReauthenticationLink />}
        description="当前浏览器标签页没有可用 CSRF 会话；未发送新的创建请求。"
        state="error"
        title="需要重新登录"
      />
    );
  }
  return (
    <StatePanel
      action={
        <button className="gf-secondary-button" onClick={onRetry} type="button">
          使用同一 intent 明确重试
        </button>
      }
      description="网络结果未知；页面不会自动创建新 intent。请先确认目录状态，必要时使用同一 intent 明确重试。"
      state="error"
      title="创建结果未知"
    />
  );
}

function HumanConstraintEntry({ api, catalogs }: { api: SpecEntryPanelsApi; catalogs: SpecEntryCatalogs }) {
  const [refSelection, setRefSelection] = useState<ConstraintRefSelection | null>(null);
  const [baseSnapshotId, setBaseSnapshotId] = useState("");
  const [domainIds, setDomainIds] = useState("");
  const [sourceArtifactIds, setSourceArtifactIds] = useState("");
  const [rationale, setRationale] = useState("");
  const [constraintsJson, setConstraintsJson] = useState("");
  const [attempt, setAttempt] = useState<HumanAttempt | null>(null);

  const parsedConstraints = constraintsJson.trim() ? parseConstraintArray(constraintsJson) : null;
  const selectedBase = catalogs.constraints.find(
    (snapshot) => snapshot.artifact.artifact_id === baseSnapshotId,
  );
  const dslGrammarVersion = selectedBase?.dsl_grammar_version ?? grammarFromConstraints(parsedConstraints);
  const domains = splitIds(domainIds);
  const sources = splitIds(sourceArtifactIds);
  const canSubmit =
    !attempt?.pending &&
    !blocksNewIntent(attempt?.error) &&
    refSelection !== null &&
    Boolean(dslGrammarVersion) &&
    domains.length > 0 &&
    sources.length > 0 &&
    Boolean(rationale.trim()) &&
    parsedConstraints?.ok === true;

  async function executeHuman(frozen: HumanAttempt) {
    setAttempt({ ...frozen, error: null, pending: true, result: null });
    try {
      const result = await api.draftConstraint(frozen.request, frozen.intent);
      setAttempt({ ...frozen, error: null, pending: false, result });
    } catch (error) {
      setAttempt({
        ...frozen,
        error: normalizedError(error),
        pending: false,
        result: null,
      });
    }
  }

  function submitHuman() {
    if (!canSubmit || refSelection === null || !dslGrammarVersion || parsedConstraints?.ok !== true) return;
    const request: HumanConstraintDraftRequest = {
      base_constraint_snapshot_artifact_id: baseSnapshotId.trim() || null,
      constraints: parsedConstraints.value,
      domain_scope: { domain_ids: domains },
      dsl_grammar_version: dslGrammarVersion,
      expected_ref: refSelection.expectedRef,
      rationale: rationale.trim(),
      ref_name: refSelection.refName,
      request_schema_version: "human-constraint-draft-request@1",
      source_artifact_ids: sources,
    };
    void executeHuman({
      error: null,
      intent: createMutationIntent(),
      pending: false,
      request,
      result: null,
    });
  }

  return (
    <article className="gf-specs__entry-card" data-entry="human">
      <header>
        <UserRoundPen aria-hidden="true" size={21} />
        <div>
          <p className="gf-specs__kicker">Direct typed authoring</p>
          <h3>Human typed draft</h3>
          <p>人工提交 typed constraints；创建 Artifact 不等于发布为权威约束。</p>
        </div>
      </header>
      <form
        className="gf-form gf-specs__entry-form"
        onSubmit={(event) => {
          event.preventDefault();
          submitHuman();
        }}
      >
        <ConstraintRefBindingFields
          api={api}
          name="human-constraint-target"
          onChange={setRefSelection}
          value={refSelection}
        />
        <BaseConstraintPicker
          catalogs={catalogs}
          label="基于哪个约束快照（可选）"
          onChange={setBaseSnapshotId}
          value={baseSnapshotId}
        />
        <DomainPicker catalogs={catalogs} label="适用游戏域" onChange={setDomainIds} value={domainIds} />
        <SourceArtifactPicker
          catalogs={catalogs}
          label="规则来源"
          onChange={setSourceArtifactIds}
          value={sourceArtifactIds}
        />
        <p className="gf-specs__binding-summary">
          DSL grammar：
          <strong>{dslGrammarVersion ?? "请先选择 base 或填写带 DSL 的规则"}</strong>
        </p>
        <label>
          Human rationale
          <textarea onChange={(event) => setRationale(event.target.value)} rows={3} value={rationale} />
        </label>
        <label>
          Typed constraints JSON
          <textarea
            aria-describedby="human-constraints-hint"
            className="gf-specs__code-input"
            onChange={(event) => setConstraintsJson(event.target.value)}
            rows={8}
            value={constraintsJson}
          />
        </label>
        <p className="gf-specs__field-hint" id="human-constraints-hint">
          {parsedConstraints === null
            ? "输入 JSON array；这里只检查 array/object 形状，最终以 server typed contract 为准。"
            : parsedConstraints.ok
              ? "JSON array 形状可用；字段与语义仍由 server 裁决。"
              : "需要 JSON array，且每个条目必须是 object。"}
        </p>
        <button disabled={!canSubmit} type="submit">
          {attempt?.pending ? "正在创建…" : "创建 Human typed draft"}
        </button>
      </form>
      {attempt?.error && <MutationFailure error={attempt.error} onRetry={() => void executeHuman(attempt)} />}
      {attempt?.result && (
        <div className="gf-specs__entry-success" role="status">
          <strong>规则提案已创建</strong>
          <a href={`/constraint-proposals/${encodeURIComponent(attempt.result.artifact.artifact_id)}`}>
            检查规则提案
          </a>
          <TechnicalDetails
            items={[{ label: "Proposal Artifact ID", value: attempt.result.artifact.artifact_id }]}
            summary="查看提案技术信息"
          />
        </div>
      )}
    </article>
  );
}

function AgentConstraintEntry({
  api,
  catalogs,
  projectContext,
}: {
  api: SpecEntryPanelsApi;
  catalogs: SpecEntryCatalogs;
  projectContext: ProjectConstraintAuthoringContext | null;
}) {
  const profileQuery = useQuery({
    queryFn: () => api.listExecutionProfiles(null),
    queryKey: ["spec-entry", "constraint-extraction-profiles"],
    retry: false,
  });
  const [profiles, setProfiles] = useState<ProfileState | null>(null);
  const [sourceArtifactIds, setSourceArtifactIds] = useState(
    () => projectContext?.sourceArtifactIds.join("\n") ?? "",
  );
  const [baseSnapshotId, setBaseSnapshotId] = useState(projectContext?.baseConstraintArtifactId ?? "");
  const [dslGrammarVersion, setDslGrammarVersion] = useState("");
  const [authoringGoal, setAuthoringGoal] = useState("");
  const [profileSelection, setProfileSelection] = useState("");
  const [mode, setMode] = useState<"" | LlmExecutionMode>("live");
  const [replaySourceRunId, setReplaySourceRunId] = useState("");
  const [attempt, setAttempt] = useState<AgentAttempt | null>(null);

  useEffect(() => {
    if (!projectContext) return;
    setSourceArtifactIds(projectContext.sourceArtifactIds.join("\n"));
    setBaseSnapshotId(projectContext.baseConstraintArtifactId ?? "");
  }, [projectContext]);

  useEffect(() => {
    if (!profileQuery.data) return;
    setProfiles({
      error: null,
      items: profileQuery.data.items,
      loading: false,
      nextCursor: profileQuery.data.next_cursor ?? null,
      readSnapshotId: profileQuery.data.read_snapshot_id,
    });
    const available = profileQuery.data.items.filter(isActiveExtractionProfile);
    if (profileQuery.data.next_cursor == null && available.length === 1) {
      setProfileSelection((current) => current || profileKey(available[0]!));
    }
  }, [profileQuery.data]);

  const activeProfiles = (profiles?.items ?? profileQuery.data?.items ?? []).filter(
    isActiveExtractionProfile,
  );
  const selectedProfile = activeProfiles.find((profile) => profileKey(profile) === profileSelection);
  const sources = splitIds(sourceArtifactIds);
  const selectedBase = catalogs.constraints.find(
    (snapshot) => snapshot.artifact.artifact_id === baseSnapshotId,
  );
  useEffect(() => {
    if (selectedBase) setDslGrammarVersion(selectedBase.dsl_grammar_version);
  }, [selectedBase]);
  const observedGrammars = [
    ...new Set([
      "dsl@1",
      ...catalogs.constraints.map((snapshot) => snapshot.dsl_grammar_version),
      ...catalogs.proposals.map((proposal) => proposal.proposal.dsl_grammar_version),
    ]),
  ].sort();
  const soleObservedGrammar = observedGrammars.length === 1 ? observedGrammars[0]! : null;
  useEffect(() => {
    if (soleObservedGrammar) {
      setDslGrammarVersion((current) => current || soleObservedGrammar);
    }
  }, [soleObservedGrammar]);
  const replayRuns = catalogs.proposals.filter(
    (proposal) => proposal.proposal.produced_by === "agent" && proposal.proposal.producer_run_id,
  );
  const profileCatalogReady = !profileQuery.isPending && !profileQuery.isError && !profiles?.error;
  const canSubmit =
    !attempt?.pending &&
    !blocksNewIntent(attempt?.error) &&
    profileCatalogReady &&
    sources.length > 0 &&
    Boolean(dslGrammarVersion.trim()) &&
    Boolean(authoringGoal.trim()) &&
    selectedProfile !== undefined &&
    mode !== "" &&
    (mode !== "replay" || Boolean(replaySourceRunId.trim()));

  async function loadMoreProfiles() {
    const current = profiles;
    if (!current?.nextCursor) return;
    setProfiles({ ...current, error: null, loading: true });
    try {
      const next = await api.listExecutionProfiles(current.nextCursor);
      if (next.read_snapshot_id !== current.readSnapshotId) {
        throw new Error("Execution profile 目录快照已变化，请重新开始。");
      }
      setProfiles({
        error: null,
        items: [...current.items, ...next.items],
        loading: false,
        nextCursor: next.next_cursor ?? null,
        readSnapshotId: current.readSnapshotId,
      });
    } catch (error) {
      setProfiles({
        ...current,
        error: normalizedError(error),
        loading: false,
      });
    }
  }

  async function restartProfiles() {
    const current = profiles;
    if (!current) return;
    setProfiles({ ...current, error: null, loading: true });
    try {
      const first = await api.listExecutionProfiles(null);
      setProfiles({
        error: null,
        items: first.items,
        loading: false,
        nextCursor: first.next_cursor ?? null,
        readSnapshotId: first.read_snapshot_id,
      });
      setProfileSelection("");
    } catch (error) {
      setProfiles({
        ...current,
        error: normalizedError(error),
        loading: false,
      });
    }
  }

  async function executeAgent(frozen: AgentAttempt) {
    setAttempt({ ...frozen, error: null, pending: true, result: null });
    let resolvedRequest = frozen.resolvedRequest;
    try {
      if (resolvedRequest === null) {
        const option = await api.resolveExecutionOption(frozen.request);
        if (
          option.resource_operation_id !== frozen.request.resource_operation_id ||
          option.run_kind.kind !== frozen.request.run_kind.kind ||
          option.run_kind.version !== frozen.request.run_kind.version ||
          option.llm_execution_mode !== frozen.request.llm_execution_mode
        ) {
          throw new Error("Execution option did not match the requested operation binding.");
        }
        if (frozen.request.llm_execution_mode === "replay" && !option.cassette_artifact_id) {
          throw new Error("Replay execution option did not bind a cassette Artifact.");
        }
        resolvedRequest = {
          ...frozen.prospectiveRequest,
          cassette_artifact_id: option.cassette_artifact_id ?? null,
          execution_version_plan: option.execution_version_plan,
        };
        setAttempt({ ...frozen, pending: true, resolvedRequest });
      }
      const result = await api.proposeConstraint(resolvedRequest, frozen.intent);
      setAttempt({
        ...frozen,
        error: null,
        pending: false,
        resolvedRequest,
        result,
      });
    } catch (error) {
      setAttempt({
        ...frozen,
        error: normalizedError(error),
        pending: false,
        resolvedRequest,
        result: null,
      });
    }
  }

  function submitAgent() {
    if (!canSubmit || !selectedProfile) return;
    const prospectiveRequest: ProspectiveConstraintRequest = {
      authoring_goal_text: authoringGoal.trim(),
      base_constraint_snapshot_artifact_id: baseSnapshotId.trim() || null,
      cassette_artifact_id: null,
      domain_scope: selectedProfile.domain_scope,
      dsl_grammar_version: dslGrammarVersion.trim(),
      execution_version_plan: null,
      extraction_policy: selectedProfile.profile,
      llm_execution_mode: mode,
      request_schema_version: "constraint-propose-request@1",
      source_artifact_ids: sources,
    };
    const request: ExecutionOptionResolveRequest = {
      llm_execution_mode: mode,
      prospective_request: prospectiveRequest,
      replay_source_run_id: mode === "replay" ? replaySourceRunId.trim() : null,
      request_schema_version: "execution-option-resolve-request@1",
      resource_operation_id: "propose_constraint_api_v1_constraint_proposals_propose_post",
      run_kind: { kind: "constraint_proposal.propose", version: 1 },
    };
    void executeAgent({
      error: null,
      intent: createMutationIntent(),
      pending: false,
      prospectiveRequest,
      request,
      resolvedRequest: null,
      result: null,
    });
  }

  return (
    <article className="gf-specs__entry-card" data-entry="agent">
      <header>
        <Bot aria-hidden="true" size={21} />
        <div>
          <p className="gf-specs__kicker">AI 规则助手</p>
          <h3>从策划材料提取规则</h3>
          <p>选择材料并说明重点，AI 会生成待确认的规则提案，不会直接发布。</p>
        </div>
      </header>
      <form
        className="gf-form gf-specs__entry-form"
        onSubmit={(event) => {
          event.preventDefault();
          submitAgent();
        }}
      >
        <SourceArtifactPicker
          catalogs={catalogs}
          disabled={projectContext !== null}
          label="选择策划材料"
          onChange={setSourceArtifactIds}
          value={sourceArtifactIds}
        />
        <BaseConstraintPicker
          catalogs={catalogs}
          disabled={projectContext?.baseConstraintArtifactId != null}
          label="基于哪个现有规则版本（可选）"
          onChange={(value) => {
            setBaseSnapshotId(value);
            const selected = catalogs.constraints.find((snapshot) => snapshot.artifact.artifact_id === value);
            if (selected) setDslGrammarVersion(selected.dsl_grammar_version);
          }}
          value={baseSnapshotId}
        />
        <p className="gf-specs__binding-summary">
          适用内容领域：
          <strong>
            {selectedProfile ? contentDomainLabel(selectedProfile.domain_scope.domain_ids) : "正在准备"}
          </strong>
        </p>
        {selectedProfile && (
          <TechnicalDetails
            items={[{ label: "内容领域代码", value: selectedProfile.domain_scope.domain_ids.join(", ") }]}
            summary="查看领域技术信息"
          />
        )}
        <label>
          你希望 AI 重点提取什么？
          <textarea
            placeholder="例如：提取所有金币产出上限、商店回收价格和任务前置条件。"
            onChange={(event) => setAuthoringGoal(event.target.value)}
            rows={3}
            value={authoringGoal}
          />
        </label>
        <details
          className="gf-specs__advanced-binding"
          open={!selectedProfile || !dslGrammarVersion || !mode ? true : undefined}
        >
          <summary>高级设置</summary>
          <div className="gf-form">
            <label>
              规则格式
              <select
                disabled={selectedBase !== undefined}
                onChange={(event) => setDslGrammarVersion(event.target.value)}
                value={dslGrammarVersion}
              >
                <option value="">请选择规则格式</option>
                {observedGrammars.map((grammar) => (
                  <option key={grammar} value={grammar}>
                    {grammar}
                  </option>
                ))}
              </select>
            </label>
            <label>
              AI 提取方案
              <select
                disabled={!profileCatalogReady || profiles?.loading}
                onChange={(event) => setProfileSelection(event.target.value)}
                value={profileSelection}
              >
                <option value="">请选择可用方案</option>
                {activeProfiles.map((profile) => (
                  <option key={profileKey(profile)} value={profileKey(profile)}>
                    {profile.display_name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              AI 运行方式
              <select onChange={(event) => setMode(event.target.value as "" | LlmExecutionMode)} value={mode}>
                <option value="live">在线生成（推荐）</option>
                <option value="record">在线生成并保存回放</option>
                <option value="replay">使用历史回放（测试用）</option>
              </select>
            </label>
          </div>
        </details>
        {mode === "replay" && (
          <label>
            回放来源
            <select onChange={(event) => setReplaySourceRunId(event.target.value)} value={replaySourceRunId}>
              <option value="">请选择既有 Agent 提案 Run</option>
              {replayRuns.map((proposal, index) => (
                <option key={proposal.proposal.producer_run_id!} value={proposal.proposal.producer_run_id!}>
                  历史提案 {index + 1} · {proposal.proposal.rationale}
                </option>
              ))}
            </select>
          </label>
        )}
        <button disabled={!canSubmit} type="submit">
          {attempt?.pending ? "正在提取规则…" : "生成规则提案"}
        </button>
      </form>

      {profileQuery.isPending && (
        <StatePanel
          description="正在读取分页 execution profile catalog。"
          headingLevel={3}
          state="loading"
          title="正在读取 Agent profiles"
        />
      )}
      {profileQuery.isError &&
        (profileQuery.error instanceof ApiProblemError ? (
          <ProblemPanel problem={profileQuery.error.problem} />
        ) : (
          <StatePanel
            action={
              <button
                className="gf-secondary-button"
                onClick={() => void profileQuery.refetch()}
                type="button"
              >
                重试 profile 目录
              </button>
            }
            description="未选择任何隐式 profile fallback。"
            headingLevel={3}
            state="error"
            title="Agent profile 目录读取失败"
          />
        ))}
      {!profileQuery.isPending && !profileQuery.isError && activeProfiles.length === 0 && (
        <StatePanel
          description="当前目录页没有兼容 constraint_proposal.propose@1 的 active constraint_extraction profile。"
          headingLevel={3}
          state="empty"
          title="没有可用的 Agent profile"
        />
      )}
      {profiles?.nextCursor && (
        <button
          className="gf-secondary-button"
          disabled={profiles.loading}
          onClick={() => void loadMoreProfiles()}
          type="button"
        >
          {profiles.loading ? "正在加载 profiles…" : "加载更多 Agent profiles"}
        </button>
      )}
      {profiles?.error && (
        <StatePanel
          action={
            profiles.error instanceof CursorExpiredError ? (
              <button className="gf-secondary-button" onClick={() => void restartProfiles()} type="button">
                从 profile 目录首页重新开始
              </button>
            ) : profiles.nextCursor ? (
              <button className="gf-secondary-button" onClick={() => void loadMoreProfiles()} type="button">
                重试 profile 下一页
              </button>
            ) : undefined
          }
          description="Profile 分页失败；已加载选项不代表最新目录。"
          headingLevel={3}
          state="error"
          title={profiles.error instanceof CursorExpiredError ? "Profile 游标已过期" : "Profile 分页失败"}
        />
      )}
      {attempt?.error && <MutationFailure error={attempt.error} onRetry={() => void executeAgent(attempt)} />}
      {attempt?.result && (
        <div className="gf-specs__entry-success" role="status">
          <strong>AI 规则提取已开始</strong>
          <a
            href={`/runs/${encodeURIComponent(attempt.result.run_id)}${projectContext ? `?${projectRuleQuery(projectContext)}` : ""}`}
          >
            查看提取进度
          </a>
          {projectContext && (
            <a href={`/specs?${projectRuleQuery(projectContext)}`}>提取完成后查看项目提案</a>
          )}
          <TechnicalDetails
            items={[{ label: "Run ID", value: attempt.result.run_id }]}
            summary="查看运行技术信息"
          />
        </div>
      )}
    </article>
  );
}

function HumanSpecEntry({ api, catalogs }: { api: SpecEntryPanelsApi; catalogs: SpecEntryCatalogs }) {
  const [schemaRegistryVersion, setSchemaRegistryVersion] = useState("");
  const [refName, setRefName] = useState("");
  const [refMode, setRefMode] = useState<ExpectedRefMode>("");
  const [expectedArtifactId, setExpectedArtifactId] = useState("");
  const [expectedRevision, setExpectedRevision] = useState("");
  const [domainIds, setDomainIds] = useState("");
  const [contentJson, setContentJson] = useState("");
  const [attempt, setAttempt] = useState<SpecAttempt | null>(null);

  const expectedRef = parseExpectedRef(refMode, expectedArtifactId, expectedRevision);
  const content = contentJson.trim() ? parseContentObject(contentJson) : null;
  const metaSchemaVersion =
    content?.ok &&
    "meta_schema_version" in content.value &&
    typeof content.value.meta_schema_version === "string" &&
    content.value.meta_schema_version
      ? content.value.meta_schema_version
      : null;
  const observedRegistries = [...new Set(catalogs.specs.map((spec) => spec.schema_registry_version))].sort();
  const boundSpecs = catalogs.specs.filter((spec) => spec.ref_name && spec.ref_value);
  const domains = splitIds(domainIds);
  const canSubmit =
    !attempt?.pending &&
    !blocksNewIntent(attempt?.error) &&
    Boolean(schemaRegistryVersion.trim()) &&
    Boolean(metaSchemaVersion) &&
    Boolean(refName.trim()) &&
    expectedRef !== undefined &&
    domains.length > 0 &&
    content?.ok === true;

  async function executeSpec(frozen: SpecAttempt) {
    setAttempt({ ...frozen, error: null, pending: true, result: null });
    try {
      const result = await api.uploadSpec(frozen.request, frozen.intent);
      setAttempt({ ...frozen, error: null, pending: false, result });
    } catch (error) {
      setAttempt({
        ...frozen,
        error: normalizedError(error),
        pending: false,
        result: null,
      });
    }
  }

  function submitSpec() {
    if (!canSubmit || expectedRef === undefined || content?.ok !== true || !metaSchemaVersion) return;
    const request: HumanSpecUploadRequest = {
      content_payload: content.value,
      domain_scope: { domain_ids: domains },
      expected_ref: expectedRef,
      meta_schema_version: metaSchemaVersion,
      ref_name: refName.trim(),
      request_schema_version: "human-spec-upload-request@1",
      schema_registry_version: schemaRegistryVersion.trim(),
    };
    void executeSpec({
      error: null,
      intent: createMutationIntent(),
      pending: false,
      request,
      result: null,
    });
  }

  return (
    <article className="gf-specs__entry-card gf-specs__entry-card--wide" data-entry="spec">
      <header>
        <FileUp aria-hidden="true" size={21} />
        <div>
          <p className="gf-specs__kicker">Schema-bound ingest</p>
          <h3>Human spec upload</h3>
          <p>上传明确 registry/meta binding 的 JSON payload，并显式声明 ref 并发前提。</p>
        </div>
      </header>
      <form
        className="gf-form gf-specs__entry-form gf-specs__entry-form--wide"
        onSubmit={(event) => {
          event.preventDefault();
          submitSpec();
        }}
      >
        <div className="gf-specs__form-pair">
          <label>
            Schema registry version
            <select
              onChange={(event) => setSchemaRegistryVersion(event.target.value)}
              value={schemaRegistryVersion}
            >
              <option value="">请选择已观察到的 registry</option>
              {observedRegistries.map((version) => (
                <option key={version} value={version}>
                  {version}
                </option>
              ))}
            </select>
          </label>
          <p className="gf-specs__binding-summary">
            Meta schema：
            <strong>{metaSchemaVersion ?? "从 content JSON 自动读取"}</strong>
          </p>
        </div>
        <details className="gf-specs__advanced-binding">
          <summary>高级：使用目录未展示的 schema registry</summary>
          <label>
            Schema registry version
            <input
              onChange={(event) => setSchemaRegistryVersion(event.target.value)}
              type="text"
              value={schemaRegistryVersion}
            />
          </label>
        </details>
        <fieldset className="gf-specs__ref-choice">
          <legend>Spec 发布位置</legend>
          <div>
            <label>
              <input
                checked={refMode === "none"}
                name="spec-ref-mode"
                onChange={() => {
                  setRefMode("none");
                  setExpectedArtifactId("");
                  setExpectedRevision("");
                }}
                type="radio"
              />
              创建新 Spec ref
            </label>
            <label>
              <input
                checked={refMode === "exact"}
                name="spec-ref-mode"
                onChange={() => {
                  setRefMode("exact");
                  setRefName("");
                  setExpectedArtifactId("");
                  setExpectedRevision("");
                }}
                type="radio"
              />
              更新已有 Spec ref
            </label>
          </div>
          {refMode === "none" && (
            <label>
              新 Ref 名称
              <input onChange={(event) => setRefName(event.target.value)} type="text" value={refName} />
            </label>
          )}
          {refMode === "exact" && (
            <label>
              当前 Spec ref
              <select
                onChange={(event) => {
                  const spec = boundSpecs.find(
                    (candidate) => candidate.artifact.artifact_id === event.target.value,
                  );
                  setRefName(spec?.ref_name ?? "");
                  setExpectedArtifactId(spec?.ref_value?.artifact_id ?? "");
                  setExpectedRevision(spec?.ref_value ? String(spec.ref_value.revision) : "");
                }}
                value={expectedArtifactId}
              >
                <option value="">请选择当前 ref</option>
                {boundSpecs.map((spec) => (
                  <option key={spec.artifact.artifact_id} value={spec.artifact.artifact_id}>
                    {spec.ref_name} · 第 {spec.ref_value!.revision} 版
                  </option>
                ))}
              </select>
            </label>
          )}
        </fieldset>
        <DomainPicker catalogs={catalogs} label="Spec 游戏域" onChange={setDomainIds} value={domainIds} />
        <label>
          Spec content JSON
          <textarea
            aria-describedby="spec-content-hint"
            className="gf-specs__code-input"
            onChange={(event) => setContentJson(event.target.value)}
            rows={9}
            value={contentJson}
          />
        </label>
        <p className="gf-specs__field-hint" id="spec-content-hint">
          {content === null
            ? "输入 JSON object；这里只检查顶层 object 形状，最终以 server schema registry 为准。"
            : content.ok
              ? "JSON object 形状可用；schema 与内容仍由 server 裁决。"
              : "Spec content 必须是 JSON object。"}
        </p>
        <button disabled={!canSubmit} type="submit">
          {attempt?.pending ? "正在上传…" : "上传 Human spec"}
        </button>
      </form>
      {attempt?.error && <MutationFailure error={attempt.error} onRetry={() => void executeSpec(attempt)} />}
      {attempt?.result && (
        <div className="gf-specs__entry-success" role="status">
          <strong>设计内容版本已创建</strong>
          <a href={`/specs/${encodeURIComponent(attempt.result.artifact.artifact_id)}`}>查看设计内容</a>
          <TechnicalDetails
            items={[{ label: "Spec Artifact ID", value: attempt.result.artifact.artifact_id }]}
            summary="查看内容版本技术信息"
          />
        </div>
      )}
    </article>
  );
}

export function SpecEntryPanels({
  api = specWorkflowApi,
  catalogs = emptyCatalogs,
  projectContext = null,
}: {
  api?: SpecEntryPanelsApi;
  catalogs?: SpecEntryCatalogs;
  projectContext?: ProjectConstraintAuthoringContext | null;
}) {
  return (
    <section className="gf-specs__entries" aria-labelledby="spec-entry-title" id="create-rules">
      {projectContext && (
        <aside className="gf-specs__project-context" role="note">
          <Bot aria-hidden="true" size={19} />
          <div>
            <strong>
              已绑定{projectContext.projectName}项目的 {projectContext.sourceArtifactIds.length} 份策划材料
            </strong>
            <p>
              {projectContext.baseConstraintArtifactId
                ? "AI 会基于项目当前规则版本提出增量修改。"
                : "这个项目尚未发布规则，AI 会从所选材料建立首份规则提案。"}
            </p>
          </div>
        </aside>
      )}
      <header>
        <div>
          <p className="gf-specs__kicker">创建新内容</p>
          <h2 id="spec-entry-title">从策划材料开始</h2>
        </div>
        <p>AI 先生成候选；策划确认、确定性验证和审批通过后才会发布。</p>
      </header>
      <div className="gf-specs__entry-grid gf-specs__entry-grid--primary">
        <AgentConstraintEntry api={api} catalogs={catalogs} projectContext={projectContext} />
      </div>
      <details className="gf-specs__manual-tools">
        <summary>高级：手工导入结构化内容或规则</summary>
        <div className="gf-specs__entry-grid">
          <HumanConstraintEntry api={api} catalogs={catalogs} />
          <HumanSpecEntry api={api} catalogs={catalogs} />
        </div>
      </details>
    </section>
  );
}
