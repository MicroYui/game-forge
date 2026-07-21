import { useQuery } from "@tanstack/react-query";
import { Bot, FileUp, UserRoundPen } from "lucide-react";
import { useEffect, useState } from "react";

import { createMutationIntent, ReauthenticationRequiredError, type MutationIntent } from "../../api/csrf";
import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import type { components } from "../../api/generated/openapi";
import { ProblemPanel, StatePanel } from "../../components/ui";
import {
  specWorkflowApi,
  type ConstraintProposalReadView,
  type ConstraintProposeRequest,
  type ExecutionOptionResolveRequest,
  type ExecutionProfilePage,
  type HumanConstraintDraftRequest,
  type HumanSpecUploadRequest,
  type RunAccepted,
  type SpecView,
  type SpecWorkflowApi,
} from "./api";

export type SpecEntryPanelsApi = Pick<
  SpecWorkflowApi,
  "draftConstraint" | "listExecutionProfiles" | "proposeConstraint" | "resolveExecutionOption" | "uploadSpec"
>;

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
    return { ok: true, value: parsed as HumanConstraintDraftRequest["constraints"] };
  } catch {
    return { ok: false };
  }
}

function parseContentObject(
  value: string,
): { ok: true; value: HumanSpecUploadRequest["content_payload"] } | { ok: false } {
  try {
    const parsed: unknown = JSON.parse(value);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return { ok: false };
    return { ok: true, value: parsed as HumanSpecUploadRequest["content_payload"] };
  } catch {
    return { ok: false };
  }
}

function profileKey(profile: ExecutionProfile): string {
  return `${profile.profile.profile_id}@${profile.profile.version}`;
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

function ExpectedRefFields({
  artifactLabel,
  artifactId,
  exactLabel,
  groupLabel,
  mode,
  name,
  noneLabel,
  onArtifactIdChange,
  onModeChange,
  onRevisionChange,
  revisionLabel,
  revision,
}: {
  artifactLabel: string;
  artifactId: string;
  exactLabel: string;
  groupLabel: string;
  mode: ExpectedRefMode;
  name: string;
  noneLabel: string;
  onArtifactIdChange(value: string): void;
  onModeChange(value: ExpectedRefMode): void;
  onRevisionChange(value: string): void;
  revisionLabel: string;
  revision: string;
}) {
  return (
    <fieldset className="gf-specs__ref-choice">
      <legend>{groupLabel}</legend>
      <div>
        <label>
          <input checked={mode === "none"} name={name} onChange={() => onModeChange("none")} type="radio" />
          {noneLabel}
        </label>
        <label>
          <input checked={mode === "exact"} name={name} onChange={() => onModeChange("exact")} type="radio" />
          {exactLabel}
        </label>
      </div>
      {mode === "exact" && (
        <div className="gf-specs__form-pair">
          <label>
            {artifactLabel}
            <input
              onChange={(event) => onArtifactIdChange(event.target.value)}
              type="text"
              value={artifactId}
            />
          </label>
          <label>
            {revisionLabel}
            <input
              min="1"
              onChange={(event) => onRevisionChange(event.target.value)}
              type="number"
              value={revision}
            />
          </label>
        </div>
      )}
    </fieldset>
  );
}

function MutationFailure({ error, onRetry }: { error: Error; onRetry(): void }) {
  if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
  if (error instanceof ReauthenticationRequiredError) {
    return (
      <StatePanel
        action={
          <a className="gf-secondary-button" href="/login">
            重新登录
          </a>
        }
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

function HumanConstraintEntry({ api }: { api: SpecEntryPanelsApi }) {
  const [refName, setRefName] = useState("");
  const [refMode, setRefMode] = useState<ExpectedRefMode>("");
  const [expectedArtifactId, setExpectedArtifactId] = useState("");
  const [expectedRevision, setExpectedRevision] = useState("");
  const [baseSnapshotId, setBaseSnapshotId] = useState("");
  const [domainIds, setDomainIds] = useState("");
  const [dslGrammarVersion, setDslGrammarVersion] = useState("");
  const [sourceArtifactIds, setSourceArtifactIds] = useState("");
  const [rationale, setRationale] = useState("");
  const [constraintsJson, setConstraintsJson] = useState("");
  const [attempt, setAttempt] = useState<HumanAttempt | null>(null);

  const expectedRef = parseExpectedRef(refMode, expectedArtifactId, expectedRevision);
  const parsedConstraints = constraintsJson.trim() ? parseConstraintArray(constraintsJson) : null;
  const domains = splitIds(domainIds);
  const sources = splitIds(sourceArtifactIds);
  const canSubmit =
    !attempt?.pending &&
    !blocksNewIntent(attempt?.error) &&
    Boolean(refName.trim()) &&
    expectedRef !== undefined &&
    Boolean(dslGrammarVersion.trim()) &&
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
      setAttempt({ ...frozen, error: normalizedError(error), pending: false, result: null });
    }
  }

  function submitHuman() {
    if (!canSubmit || expectedRef === undefined || parsedConstraints?.ok !== true) return;
    const request: HumanConstraintDraftRequest = {
      base_constraint_snapshot_artifact_id: baseSnapshotId.trim() || null,
      constraints: parsedConstraints.value,
      domain_scope: { domain_ids: domains },
      dsl_grammar_version: dslGrammarVersion.trim(),
      expected_ref: expectedRef,
      rationale: rationale.trim(),
      ref_name: refName.trim(),
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
        <label>
          Human ref name
          <input onChange={(event) => setRefName(event.target.value)} type="text" value={refName} />
        </label>
        <ExpectedRefFields
          artifactLabel="Human expected ref Artifact ID"
          artifactId={expectedArtifactId}
          exactLabel="Exact current ref"
          groupLabel="Human expected ref"
          mode={refMode}
          name="human-expected-ref"
          noneLabel="No current ref"
          onArtifactIdChange={setExpectedArtifactId}
          onModeChange={setRefMode}
          onRevisionChange={setExpectedRevision}
          revisionLabel="Human expected ref revision"
          revision={expectedRevision}
        />
        <label>
          Human base ConstraintSnapshot Artifact ID
          <input
            onChange={(event) => setBaseSnapshotId(event.target.value)}
            placeholder="留空表示无 base snapshot"
            type="text"
            value={baseSnapshotId}
          />
        </label>
        <div className="gf-specs__form-pair">
          <label>
            Human domain IDs
            <input onChange={(event) => setDomainIds(event.target.value)} type="text" value={domainIds} />
          </label>
          <label>
            Human DSL grammar
            <input
              onChange={(event) => setDslGrammarVersion(event.target.value)}
              type="text"
              value={dslGrammarVersion}
            />
          </label>
        </div>
        <label>
          Human source Artifact IDs
          <textarea
            onChange={(event) => setSourceArtifactIds(event.target.value)}
            rows={2}
            value={sourceArtifactIds}
          />
        </label>
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
          <strong>Human proposal 已创建</strong>
          <a href={`/constraint-proposals/${encodeURIComponent(attempt.result.artifact.artifact_id)}`}>
            打开 proposal {attempt.result.artifact.artifact_id}
          </a>
        </div>
      )}
    </article>
  );
}

function AgentConstraintEntry({ api }: { api: SpecEntryPanelsApi }) {
  const profileQuery = useQuery({
    queryFn: () => api.listExecutionProfiles(null),
    queryKey: ["spec-entry", "constraint-extraction-profiles"],
    retry: false,
  });
  const [profiles, setProfiles] = useState<ProfileState | null>(null);
  const [sourceArtifactIds, setSourceArtifactIds] = useState("");
  const [baseSnapshotId, setBaseSnapshotId] = useState("");
  const [domainIds, setDomainIds] = useState("");
  const [dslGrammarVersion, setDslGrammarVersion] = useState("");
  const [authoringGoal, setAuthoringGoal] = useState("");
  const [profileSelection, setProfileSelection] = useState("");
  const [mode, setMode] = useState<"" | LlmExecutionMode>("");
  const [replaySourceRunId, setReplaySourceRunId] = useState("");
  const [attempt, setAttempt] = useState<AgentAttempt | null>(null);

  useEffect(() => {
    if (!profileQuery.data) return;
    setProfiles({
      error: null,
      items: profileQuery.data.items,
      loading: false,
      nextCursor: profileQuery.data.next_cursor ?? null,
      readSnapshotId: profileQuery.data.read_snapshot_id,
    });
  }, [profileQuery.data]);

  const activeProfiles = (profiles?.items ?? profileQuery.data?.items ?? []).filter(
    isActiveExtractionProfile,
  );
  const selectedProfile = activeProfiles.find((profile) => profileKey(profile) === profileSelection);
  const sources = splitIds(sourceArtifactIds);
  const domains = splitIds(domainIds);
  const profileCatalogReady = !profileQuery.isPending && !profileQuery.isError && !profiles?.error;
  const canSubmit =
    !attempt?.pending &&
    !blocksNewIntent(attempt?.error) &&
    profileCatalogReady &&
    sources.length > 0 &&
    domains.length > 0 &&
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
      setProfiles({ ...current, error: normalizedError(error), loading: false });
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
      setProfiles({ ...current, error: normalizedError(error), loading: false });
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
      domain_scope: { domain_ids: domains },
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
          <p className="gf-specs__kicker">Bounded Agent authoring</p>
          <h3>Agent 提案</h3>
          <p>先解析 exact execution option，再创建可审计 Run；Agent 不直接写 constraint ref。</p>
        </div>
      </header>
      <form
        className="gf-form gf-specs__entry-form"
        onSubmit={(event) => {
          event.preventDefault();
          submitAgent();
        }}
      >
        <label>
          Agent source Artifact IDs
          <textarea
            onChange={(event) => setSourceArtifactIds(event.target.value)}
            rows={2}
            value={sourceArtifactIds}
          />
        </label>
        <label>
          Agent base ConstraintSnapshot Artifact ID
          <input
            onChange={(event) => setBaseSnapshotId(event.target.value)}
            placeholder="留空表示无 base snapshot"
            type="text"
            value={baseSnapshotId}
          />
        </label>
        <div className="gf-specs__form-pair">
          <label>
            Agent domain IDs
            <input onChange={(event) => setDomainIds(event.target.value)} type="text" value={domainIds} />
          </label>
          <label>
            Agent DSL grammar
            <input
              onChange={(event) => setDslGrammarVersion(event.target.value)}
              type="text"
              value={dslGrammarVersion}
            />
          </label>
        </div>
        <label>
          Agent authoring goal
          <textarea
            onChange={(event) => setAuthoringGoal(event.target.value)}
            rows={3}
            value={authoringGoal}
          />
        </label>
        <div className="gf-specs__form-pair">
          <label>
            Agent execution profile
            <select
              disabled={!profileCatalogReady || profiles?.loading}
              onChange={(event) => setProfileSelection(event.target.value)}
              value={profileSelection}
            >
              <option value="">请选择 active constraint_extraction profile</option>
              {activeProfiles.map((profile) => (
                <option key={profileKey(profile)} value={profileKey(profile)}>
                  {profile.display_name} · {profileKey(profile)}
                </option>
              ))}
            </select>
          </label>
          <label>
            LLM execution mode
            <select onChange={(event) => setMode(event.target.value as "" | LlmExecutionMode)} value={mode}>
              <option value="">请选择 live / record / replay</option>
              <option value="live">live</option>
              <option value="record">record</option>
              <option value="replay">replay</option>
            </select>
          </label>
        </div>
        {mode === "replay" && (
          <label>
            Replay source Run
            <input
              onChange={(event) => setReplaySourceRunId(event.target.value)}
              type="text"
              value={replaySourceRunId}
            />
          </label>
        )}
        <p className="gf-specs__field-hint">
          Profile 与执行模式必须明确选择；页面不设置隐式默认或 fallback。
        </p>
        <button disabled={!canSubmit} type="submit">
          {attempt?.pending ? "正在解析并创建…" : "生成 Agent 候选"}
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
          <strong>Agent Run 已接受</strong>
          <a href={`/runs/${encodeURIComponent(attempt.result.run_id)}`}>打开 Run {attempt.result.run_id}</a>
        </div>
      )}
    </article>
  );
}

function HumanSpecEntry({ api }: { api: SpecEntryPanelsApi }) {
  const [schemaRegistryVersion, setSchemaRegistryVersion] = useState("");
  const [metaSchemaVersion, setMetaSchemaVersion] = useState("");
  const [refName, setRefName] = useState("");
  const [refMode, setRefMode] = useState<ExpectedRefMode>("");
  const [expectedArtifactId, setExpectedArtifactId] = useState("");
  const [expectedRevision, setExpectedRevision] = useState("");
  const [domainIds, setDomainIds] = useState("");
  const [contentJson, setContentJson] = useState("");
  const [attempt, setAttempt] = useState<SpecAttempt | null>(null);

  const expectedRef = parseExpectedRef(refMode, expectedArtifactId, expectedRevision);
  const content = contentJson.trim() ? parseContentObject(contentJson) : null;
  const domains = splitIds(domainIds);
  const canSubmit =
    !attempt?.pending &&
    !blocksNewIntent(attempt?.error) &&
    Boolean(schemaRegistryVersion.trim()) &&
    Boolean(metaSchemaVersion.trim()) &&
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
      setAttempt({ ...frozen, error: normalizedError(error), pending: false, result: null });
    }
  }

  function submitSpec() {
    if (!canSubmit || expectedRef === undefined || content?.ok !== true) return;
    const request: HumanSpecUploadRequest = {
      content_payload: content.value,
      domain_scope: { domain_ids: domains },
      expected_ref: expectedRef,
      meta_schema_version: metaSchemaVersion.trim(),
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
            <input
              onChange={(event) => setSchemaRegistryVersion(event.target.value)}
              type="text"
              value={schemaRegistryVersion}
            />
          </label>
          <label>
            Meta schema version
            <input
              onChange={(event) => setMetaSchemaVersion(event.target.value)}
              type="text"
              value={metaSchemaVersion}
            />
          </label>
        </div>
        <label>
          Spec ref name
          <input onChange={(event) => setRefName(event.target.value)} type="text" value={refName} />
        </label>
        <ExpectedRefFields
          artifactLabel="Spec expected ref Artifact ID"
          artifactId={expectedArtifactId}
          exactLabel="Spec exact current ref"
          groupLabel="Spec expected ref"
          mode={refMode}
          name="spec-expected-ref"
          noneLabel="Spec has no current ref"
          onArtifactIdChange={setExpectedArtifactId}
          onModeChange={setRefMode}
          onRevisionChange={setExpectedRevision}
          revisionLabel="Spec expected ref revision"
          revision={expectedRevision}
        />
        <label>
          Spec domain IDs
          <input onChange={(event) => setDomainIds(event.target.value)} type="text" value={domainIds} />
        </label>
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
          <strong>Spec Artifact 已创建</strong>
          <a href={`/specs/${encodeURIComponent(attempt.result.artifact.artifact_id)}`}>
            打开 Spec {attempt.result.artifact.artifact_id}
          </a>
        </div>
      )}
    </article>
  );
}

export function SpecEntryPanels({ api = specWorkflowApi }: { api?: SpecEntryPanelsApi }) {
  return (
    <section className="gf-specs__entries" aria-labelledby="spec-entry-title">
      <header>
        <div>
          <p className="gf-specs__kicker">Authoring desk</p>
          <h2 id="spec-entry-title">明确输入，创建候选</h2>
        </div>
        <p>入口独立；proposal 仍需 Human 修订、确定性验证、审批与 publish。</p>
      </header>
      <div className="gf-specs__entry-grid">
        <AgentConstraintEntry api={api} />
        <HumanConstraintEntry api={api} />
        <HumanSpecEntry api={api} />
      </div>
    </section>
  );
}
