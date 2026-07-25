import { useQuery } from "@tanstack/react-query";
import { BookOpenText, FilePenLine, GitBranch, Network, PencilRuler } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";

import { createMutationIntent, type MutationIntent } from "../../api/csrf";
import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { TechnicalDetails } from "../../components/identity";
import { KnowledgeGraph } from "../../components/kg";
import { type CursorPaginationState } from "../../components/tables";
import { ProblemPanel, StatePanel } from "../../components/ui";
import {
  specWorkflowApi,
  type ExecutionProfilePage,
  type GraphPage,
  type HumanPatchDraftRequest,
  type PatchArtifactReadView,
  type SpecWorkflowApi,
} from "./api";
import {
  compileStructuredOperations,
  createStructuredOperation,
  StructuredPatchEditor,
  type StructuredOperationDraft,
} from "./StructuredPatchEditor";
import "./specs.css";

export type SpecDetailApi = Pick<
  SpecWorkflowApi,
  "draftPatch" | "getSchemaRegistry" | "getSpec" | "listExecutionProfiles" | "listSpecGraph"
>;

interface GraphState {
  error?: Error;
  items: GraphPage["items"];
  loading: boolean;
  nextCursor: string | null;
  readSnapshotId: string;
}

interface ProfileState {
  error?: Error;
  items: ExecutionProfilePage["items"];
  loading: boolean;
  nextCursor: string | null;
  readSnapshotId: string;
}

interface PatchDraftAttempt {
  intent: MutationIntent;
  request: HumanPatchDraftRequest;
}

function graphState(page: GraphPage): GraphState {
  return {
    items: page.items,
    loading: false,
    nextCursor: page.next_cursor ?? null,
    readSnapshotId: page.read_snapshot_id,
  };
}

function graphPaginationState(state: GraphState): CursorPaginationState {
  if (state.error instanceof CursorExpiredError) return "expired";
  if (state.error) return "error";
  return state.loading ? "loading" : "ready";
}

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("图谱读取失败。");
}

function profileKey(profile: ExecutionProfilePage["items"][number]): string {
  return `${profile.profile.profile_id}@${profile.profile.version}`;
}

function parseJsonArray(value: string): unknown[] | null {
  try {
    const parsed: unknown = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function SpecDetailPage({
  api = specWorkflowApi,
  artifactId,
}: {
  api?: SpecDetailApi;
  artifactId: string;
}) {
  const detail = useQuery({
    queryFn: async () => {
      const spec = await api.getSpec(artifactId);
      const [graph, registry, profiles] = await Promise.all([
        api.listSpecGraph(artifactId, null),
        api.getSchemaRegistry(spec.schema_registry_version),
        api.listExecutionProfiles(null),
      ]);
      return { graph, profiles, registry, spec };
    },
    queryKey: ["spec-detail", artifactId],
    retry: false,
  });
  const [graph, setGraph] = useState<GraphState | null>(null);
  const [profiles, setProfiles] = useState<ProfileState | null>(null);
  const [refName, setRefName] = useState("");
  const [expectedRefArtifactId, setExpectedRefArtifactId] = useState("");
  const [expectedRefRevision, setExpectedRefRevision] = useState("");
  const [noCurrentRef, setNoCurrentRef] = useState(false);
  const [constraintSnapshotId, setConstraintSnapshotId] = useState("");
  const [selectedExportProfiles, setSelectedExportProfiles] = useState<string[]>([]);
  const [structuredOperations, setStructuredOperations] = useState<StructuredOperationDraft[]>([
    createStructuredOperation("structured-row-1"),
  ]);
  const [useRawOperations, setUseRawOperations] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [operationsJson, setOperationsJson] = useState("[]");
  const [preconditionsJson, setPreconditionsJson] = useState("[]");
  const [expectedToFix, setExpectedToFix] = useState("");
  const [rationale, setRationale] = useState("");
  const [sideEffectRisk, setSideEffectRisk] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [draftAttempt, setDraftAttempt] = useState<PatchDraftAttempt | null>(null);
  const [draftError, setDraftError] = useState<Error | null>(null);
  const [draftPending, setDraftPending] = useState(false);
  const [draftResult, setDraftResult] = useState<PatchArtifactReadView | null>(null);

  useEffect(() => {
    if (!detail.data) return;
    setGraph(graphState(detail.data.graph));
    setProfiles({
      items: detail.data.profiles.items,
      loading: false,
      nextCursor: detail.data.profiles.next_cursor ?? null,
      readSnapshotId: detail.data.profiles.read_snapshot_id,
    });
    setRefName(detail.data.spec.ref_name ?? "");
    setExpectedRefArtifactId(detail.data.spec.ref_value?.artifact_id ?? "");
    setExpectedRefRevision(detail.data.spec.ref_value ? String(detail.data.spec.ref_value.revision) : "");
    setNoCurrentRef(detail.data.spec.ref_value === null || detail.data.spec.ref_value === undefined);
  }, [detail.data]);

  async function readGraphPage(cursor: string | null, restart: boolean) {
    const current = graph;
    if (!current) return;
    setGraph({ ...current, error: undefined, loading: true });
    try {
      const next = await api.listSpecGraph(artifactId, cursor);
      if (!restart && next.read_snapshot_id !== current.readSnapshotId) {
        throw new Error("图谱读取快照发生变化，请重新开始。");
      }
      setGraph({
        ...graphState(next),
        items: restart ? next.items : [...current.items, ...next.items],
      });
    } catch (error) {
      setGraph({ ...current, error: normalizedError(error), loading: false });
    }
  }

  async function readProfilePage(cursor: string | null, restart: boolean) {
    const current = profiles;
    if (!current) return;
    setProfiles({ ...current, error: undefined, loading: true });
    try {
      const next = await api.listExecutionProfiles(cursor);
      if (!restart && next.read_snapshot_id !== current.readSnapshotId) {
        throw new Error("Execution profile 目录快照发生变化，请重新开始。");
      }
      setProfiles({
        items: restart ? next.items : [...current.items, ...next.items],
        loading: false,
        nextCursor: next.next_cursor ?? null,
        readSnapshotId: next.read_snapshot_id,
      });
    } catch (error) {
      setProfiles({
        ...current,
        error: normalizedError(error),
        loading: false,
      });
    }
  }

  async function executePatchDraft(attempt: PatchDraftAttempt) {
    setDraftPending(true);
    setDraftError(null);
    try {
      const result = await api.draftPatch(attempt.request, attempt.intent);
      setDraftResult(result);
      setDraftAttempt(null);
    } catch (error) {
      setDraftError(error instanceof Error ? error : new Error("Patch 草案创建失败。"));
    } finally {
      setDraftPending(false);
    }
  }

  function submitPatchDraft(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (draftAttempt || draftPending || !detail.data) return;
    setFormError(null);
    const structured = compileStructuredOperations(structuredOperations, currentGraph.items);
    const operations = useRawOperations ? parseJsonArray(operationsJson) : structured.ops;
    const preconditions = parseJsonArray(preconditionsJson);
    const exactRevision = Number(expectedRefRevision);
    const expectedRef = noCurrentRef
      ? null
      : expectedRefArtifactId.trim() && Number.isInteger(exactRevision) && exactRevision > 0
        ? { artifact_id: expectedRefArtifactId.trim(), revision: exactRevision }
        : undefined;
    const availableProfiles = profiles?.items ?? detail.data.profiles.items;
    const exportProfiles = selectedExportProfiles.flatMap((key) => {
      const profile = availableProfiles.find(
        (candidate) =>
          candidate.status === "active" &&
          candidate.profile_kind === "config_export" &&
          profileKey(candidate) === key,
      );
      return profile ? [profile.profile] : [];
    });
    if (exportProfiles.length > 0 && !constraintSnapshotId.trim()) {
      setFormError(
        "已选择配置导出方案；配置导出必须绑定约束快照。请在“高级：精确绑定、前置条件与原始 JSON”中填写约束快照 Artifact ID，或取消配置导出方案。当前输入已保留，草案尚未提交。",
      );
      return;
    }
    if (
      !refName.trim() ||
      expectedRef === undefined ||
      operations === null ||
      operations.length === 0 ||
      (!useRawOperations && structured.error !== null) ||
      preconditions === null ||
      exportProfiles.length !== selectedExportProfiles.length ||
      !rationale.trim() ||
      !sideEffectRisk.trim()
    ) {
      setFormError(
        structured.error ??
          "请填写 exact ref、非空 operations、合法 preconditions、说明与风险；如选择 export profile，必须是 exact active 版本。",
      );
      return;
    }
    const request: HumanPatchDraftRequest = {
      base_snapshot_artifact_id: detail.data.spec.artifact.artifact_id,
      candidate_export_profiles: exportProfiles,
      constraint_snapshot_artifact_id: constraintSnapshotId.trim() || null,
      expected_ref: expectedRef,
      expected_to_fix: expectedToFix
        .split("\n")
        .map((value) => value.trim())
        .filter(Boolean),
      ops: operations as HumanPatchDraftRequest["ops"],
      preconditions: preconditions as HumanPatchDraftRequest["preconditions"],
      rationale: rationale.trim(),
      ref_name: refName.trim(),
      request_schema_version: "human-patch-draft-request@1",
      side_effect_risk: sideEffectRisk.trim(),
    };
    const attempt = { intent: createMutationIntent(), request };
    setDraftAttempt(attempt);
    void executePatchDraft(attempt);
  }

  if (detail.isPending) {
    return (
      <div className="gf-page gf-specs">
        <StatePanel
          description="正在整理这一版的内容、关系和可用修改方式。"
          headingLevel={1}
          state="loading"
          title="正在读取内容版本"
        />
      </div>
    );
  }

  if (detail.isError) {
    return (
      <div className="gf-page gf-specs">
        <header className="gf-page-header">
          <p className="gf-specs__kicker">游戏内容版本</p>
          <h1>内容版本详情</h1>
        </header>
        {detail.error instanceof ApiProblemError ? (
          <ProblemPanel problem={detail.error.problem} />
        ) : (
          <StatePanel
            action={
              <button className="gf-secondary-button" onClick={() => void detail.refetch()} type="button">
                重试
              </button>
            }
            description="内容版本读取失败；现有内容不会受到影响。"
            state="error"
            title="无法读取内容版本"
          />
        )}
      </div>
    );
  }

  const spec = detail.data.spec;
  const currentGraph = graph ?? graphState(detail.data.graph);
  const currentProfiles = profiles ?? {
    items: detail.data.profiles.items,
    loading: false,
    nextCursor: detail.data.profiles.next_cursor ?? null,
    readSnapshotId: detail.data.profiles.read_snapshot_id,
  };
  const exportProfiles = currentProfiles.items.filter(
    (profile) => profile.status === "active" && profile.profile_kind === "config_export",
  );
  const exactRef =
    spec.ref_name && spec.ref_value ? `${spec.ref_name} · revision ${spec.ref_value.revision}` : null;
  const entityCount = currentGraph.items.filter((item) => item.item_kind === "entity").length;
  const relationCount = currentGraph.items.length - entityCount;
  const registeredSchemaCount = Object.keys(detail.data.registry.schemas).length;

  return (
    <div className="gf-page gf-specs gf-spec-detail">
      <nav aria-label="内容版本导航" className="gf-specs__back-nav">
        <a href="/specs">返回内容工作台</a>
        <a href={`/artifacts/${encodeURIComponent(spec.artifact.artifact_id)}`}>查看校验与来源记录</a>
      </nav>

      <header className="gf-specs__hero gf-specs__hero--detail">
        <div>
          <p className="gf-specs__kicker">游戏内容版本</p>
          <h1>内容版本详情</h1>
          <p className="gf-specs__lede">查看这一版中的角色、任务、道具及其关系；这里不会直接改动正式内容。</p>
        </div>
        <span className="gf-specs__status-mark">
          <BookOpenText aria-hidden="true" size={17} />
          只读版本
        </span>
      </header>

      <dl className="gf-specs__facts" aria-label="内容版本概览">
        <div>
          <dt>版本状态</dt>
          <dd>
            {exactRef && spec.ref_name && spec.ref_value ? (
              <span className="gf-specs__inline-fact gf-specs__inline-fact--authority">
                <GitBranch aria-hidden="true" size={14} />
                <a href={`/refs/${encodeURIComponent(spec.ref_name)}/history`}>
                  当前内容 · 第 {spec.ref_value.revision} 版
                </a>
              </span>
            ) : (
              <span className="gf-specs__muted">尚未发布为当前内容</span>
            )}
          </dd>
        </div>
        <div>
          <dt>本页内容</dt>
          <dd>
            {entityCount} 项内容 · {relationCount} 条关系
          </dd>
        </div>
        <div>
          <dt>结构校验</dt>
          <dd>已按 {registeredSchemaCount} 种登记结构读取</dd>
        </div>
      </dl>

      <TechnicalDetails
        items={[
          {
            copyLabel: "复制内容版本 Artifact ID",
            label: "Artifact ID",
            value: spec.artifact.artifact_id,
          },
          {
            copyLabel: "复制内容快照 ID",
            label: "Snapshot ID",
            value: spec.snapshot_id,
          },
          {
            label: "Payload schema",
            value: spec.artifact.payload_schema_id ?? "未公开",
          },
          { label: "Schema registry", value: spec.schema_registry_version },
          {
            copyLabel: "复制 Registry digest",
            label: "Registry digest",
            value: detail.data.registry.registry_digest,
          },
          { label: "图谱读取快照", value: currentGraph.readSnapshotId },
          ...(exactRef ? [{ label: "Exact ref authority", value: exactRef }] : []),
        ]}
        summary="查看版本技术信息"
      />

      <section className="gf-specs__workspace-section" aria-labelledby="spec-graph-title">
        <header className="gf-specs__section-heading">
          <Network aria-hidden="true" size={19} />
          <div>
            <h2 id="spec-graph-title">内容关系图</h2>
            <p>直观看清角色、任务、道具之间如何关联；点击任意内容即可查看详情。</p>
          </div>
        </header>
        {currentGraph.items.length === 0 ? (
          <StatePanel
            description="这一版当前没有可展示的角色、任务、道具或关系。"
            state="empty"
            title="这一版还没有内容关系"
          />
        ) : (
          <KnowledgeGraph
            ariaLabel="内容关系图"
            items={currentGraph.items}
            nextCursor={currentGraph.nextCursor}
            onLoadMore={(cursor) => void readGraphPage(cursor, false)}
            onRestart={() => void readGraphPage(null, true)}
            pageLabel="当前版本的内容关系"
            paginationState={graphPaginationState(currentGraph)}
          />
        )}
      </section>

      <aside className="gf-specs__edit-boundary" role="note">
        <PencilRuler aria-hidden="true" size={20} />
        <div>
          <strong>修改方式</strong>
          <p>这里不会直接覆盖正式内容。先创建修改草案，系统完成检查与审批后才会应用。</p>
        </div>
      </aside>

      <section className="gf-specs__workspace-section" aria-labelledby="patch-draft-title">
        <header className="gf-specs__section-heading">
          <FilePenLine aria-hidden="true" size={19} />
          <div>
            <h2 id="patch-draft-title">创建修改草案</h2>
            <p>按名称选择要改的内容，确认预览后再进入检查和审批；当前版本不会被直接修改。</p>
          </div>
        </header>

        <div className="gf-specs__patch-content">
          {draftError &&
            (draftError instanceof ApiProblemError ? (
              <div>
                <ProblemPanel problem={draftError.problem} />
                <button
                  className="gf-secondary-button"
                  onClick={() => {
                    setDraftAttempt(null);
                    setDraftError(null);
                  }}
                  type="button"
                >
                  修正草案输入
                </button>
              </div>
            ) : (
              <StatePanel
                action={
                  draftAttempt ? (
                    <button
                      className="gf-secondary-button"
                      disabled={draftPending}
                      onClick={() => void executePatchDraft(draftAttempt)}
                      type="button"
                    >
                      以同一 intent 重试
                    </button>
                  ) : undefined
                }
                description="系统尚未确认请求是否成功。请使用下方重试按钮继续；系统不会重复创建草案。"
                state="error"
                title="修改草案创建结果未知"
              />
            ))}

          {formError && <p role="alert">{formError}</p>}

          <form className="gf-form" onSubmit={submitPatchDraft}>
            <fieldset disabled={draftPending || draftAttempt !== null || draftResult !== null}>
              <legend className="u-small">修改草案</legend>
              <div className="gf-specs__patch-destination">
                <div>
                  <strong>提交到</strong>
                  <span>{noCurrentRef ? "新的内容发布位置" : `当前内容 · 第 ${expectedRefRevision} 版`}</span>
                </div>
                {noCurrentRef ? (
                  <label className="gf-cluster">
                    新发布位置名称
                    <input onChange={(event) => setRefName(event.target.value)} required value={refName} />
                  </label>
                ) : (
                  <TechnicalDetails
                    items={[{ label: "Ref name", value: refName }]}
                    summary="查看发布位置技术信息"
                  />
                )}
              </div>

              <StructuredPatchEditor
                graphItems={currentGraph.items}
                onChange={setStructuredOperations}
                operations={structuredOperations}
              />
              {useRawOperations && (
                <p className="gf-specs__raw-mode-note" role="status">
                  当前将提交高级区中的原始 TypedOp JSON；上方可视化变更暂不进入请求。取消高级区勾选即可恢复。
                </p>
              )}

              <details className="gf-specs__advanced-binding">
                <summary>高级：配置导出验证</summary>
                <label>
                  配置导出方案（可选）
                  <select
                    aria-describedby="patch-export-profile-help"
                    multiple
                    onChange={(event) =>
                      setSelectedExportProfiles(
                        [...event.currentTarget.selectedOptions].map((option) => option.value),
                      )
                    }
                    size={Math.min(Math.max(exportProfiles.length, 2), 5)}
                    value={selectedExportProfiles}
                  >
                    {exportProfiles.map((profile) => (
                      <option key={profileKey(profile)} value={profileKey(profile)}>
                        {profile.display_name}
                      </option>
                    ))}
                  </select>
                  <span className="u-small" id="patch-export-profile-help">
                    仅在这份修改需要额外验证导出配置时选择。
                  </span>
                </label>
                {currentProfiles.nextCursor && (
                  <button
                    className="gf-secondary-button"
                    disabled={currentProfiles.loading}
                    onClick={() => void readProfilePage(currentProfiles.nextCursor, false)}
                    type="button"
                  >
                    {currentProfiles.loading ? "正在加载方案" : "加载更多方案"}
                  </button>
                )}
                {currentProfiles.error && (
                  <StatePanel
                    action={
                      currentProfiles.error instanceof CursorExpiredError ? (
                        <button
                          className="gf-secondary-button"
                          onClick={() => void readProfilePage(null, true)}
                          type="button"
                        >
                          重新读取方案目录
                        </button>
                      ) : undefined
                    }
                    description="验证方案读取失败；系统不会擅自改用其他方案。"
                    state="error"
                    title="无法继续读取验证方案"
                  />
                )}
              </details>

              <label>
                变更说明
                <textarea
                  onChange={(event) => setRationale(event.target.value)}
                  placeholder="说明为什么要做这些变更"
                  required
                  rows={3}
                  value={rationale}
                />
              </label>
              <label>
                可能影响（必填）
                <input
                  onChange={(event) => setSideEffectRisk(event.target.value)}
                  placeholder="例如：低风险，仅新增叙事实体"
                  required
                  value={sideEffectRisk}
                />
              </label>

              <details
                className="gf-specs__advanced-binding"
                onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}
              >
                <summary>高级：精确绑定、前置条件与原始 JSON</summary>
                {advancedOpen && (
                  <div className="gf-specs__advanced-fields">
                    <p className="gf-specs__structured-hint">
                      仅在重放既有请求或处理无法由可视化编辑器表达的 payload 时使用。
                    </p>
                    <label className="gf-cluster">
                      <input
                        checked={noCurrentRef}
                        onChange={(event) => setNoCurrentRef(event.target.checked)}
                        type="checkbox"
                      />
                      确认当前发布位置不存在
                    </label>
                    {!noCurrentRef && (
                      <div className="gf-form">
                        <label>
                          Expected ref Artifact ID
                          <input
                            onChange={(event) => setExpectedRefArtifactId(event.target.value)}
                            required
                            value={expectedRefArtifactId}
                          />
                        </label>
                        <label>
                          Expected ref revision
                          <input
                            min="1"
                            onChange={(event) => setExpectedRefRevision(event.target.value)}
                            required
                            type="number"
                            value={expectedRefRevision}
                          />
                        </label>
                      </div>
                    )}
                    {!noCurrentRef && (
                      <label>
                        Target ref name
                        <input
                          onChange={(event) => setRefName(event.target.value)}
                          required
                          value={refName}
                        />
                      </label>
                    )}
                    <label>
                      Constraint snapshot Artifact ID（可选）
                      <input
                        onChange={(event) => setConstraintSnapshotId(event.target.value)}
                        value={constraintSnapshotId}
                      />
                    </label>
                    <label className="gf-cluster">
                      <input
                        checked={useRawOperations}
                        onChange={(event) => setUseRawOperations(event.target.checked)}
                        type="checkbox"
                      />
                      使用原始 TypedOp JSON 替代上方可视化变更
                    </label>
                    <label>
                      Patch operations JSON
                      <textarea
                        className="u-mono"
                        onChange={(event) => {
                          setOperationsJson(event.target.value);
                          setUseRawOperations(true);
                        }}
                        rows={8}
                        value={operationsJson}
                      />
                    </label>
                    <label>
                      Preconditions JSON
                      <textarea
                        className="u-mono"
                        onChange={(event) => setPreconditionsJson(event.target.value)}
                        rows={4}
                        value={preconditionsJson}
                      />
                    </label>
                    <label>
                      Expected Finding IDs（每行一个，可选）
                      <textarea
                        onChange={(event) => setExpectedToFix(event.target.value)}
                        rows={3}
                        value={expectedToFix}
                      />
                    </label>
                  </div>
                )}
              </details>

              <button type="submit">创建修改草案</button>
            </fieldset>
          </form>

          {draftResult && (
            <section className="gf-specs__authority" data-authority="candidate">
              <FilePenLine aria-hidden="true" size={22} />
              <div>
                <p className="gf-specs__authority-label">待验证草案</p>
                <h3>修改草案已创建</h3>
                <p>下一步请检查实际改动并运行验证；创建草案不会直接修改正式内容。</p>
                <a href={`/patches/${encodeURIComponent(draftResult.artifact.artifact_id)}`}>检查修改草案</a>
                <TechnicalDetails
                  items={[{ label: "Patch Artifact ID", value: draftResult.artifact.artifact_id }]}
                  summary="查看草案技术信息"
                />
              </div>
            </section>
          )}
        </div>
      </section>
    </div>
  );
}
