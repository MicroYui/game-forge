import { useQuery } from "@tanstack/react-query";
import { BookOpenText, Database, FilePenLine, GitBranch, Network, PencilRuler } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";

import { createMutationIntent, type MutationIntent } from "../../api/csrf";
import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { KnowledgeGraph } from "../../components/kg";
import { CopyableText, type CursorPaginationState } from "../../components/tables";
import { ProblemPanel, StatePanel } from "../../components/ui";
import {
  specWorkflowApi,
  type ExecutionProfilePage,
  type GraphPage,
  type HumanPatchDraftRequest,
  type PatchArtifactReadView,
  type SpecWorkflowApi,
} from "./api";
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
      setProfiles({ ...current, error: normalizedError(error), loading: false });
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
    const operations = parseJsonArray(operationsJson);
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
    if (
      !refName.trim() ||
      expectedRef === undefined ||
      operations === null ||
      operations.length === 0 ||
      preconditions === null ||
      exportProfiles.length !== selectedExportProfiles.length ||
      !rationale.trim() ||
      !sideEffectRisk.trim()
    ) {
      setFormError(
        "请填写 exact ref、非空 operations 数组、合法 preconditions 数组、说明与风险；如选择 export profile，必须是 exact active 版本。",
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
          description="正在读取规格身份、schema registry 绑定与第一页有界图谱。"
          headingLevel={1}
          state="loading"
          title="正在读取规格详情"
        />
      </div>
    );
  }

  if (detail.isError) {
    return (
      <div className="gf-page gf-specs">
        <header className="gf-page-header">
          <p className="gf-specs__kicker">Design-Spec IR · Detail</p>
          <h1>规格详情</h1>
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
            description="规格详情读取失败；未展示底层异常内容。"
            state="error"
            title="无法读取规格详情"
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

  return (
    <div className="gf-page gf-specs gf-spec-detail">
      <nav aria-label="规格详情导航" className="gf-specs__back-nav">
        <a href="/specs">返回规格工作台</a>
        <a href={`/artifacts/${encodeURIComponent(spec.artifact.artifact_id)}`}>查看安全 Artifact 摘要</a>
      </nav>

      <header className="gf-specs__hero gf-specs__hero--detail">
        <div>
          <p className="gf-specs__kicker">Design-Spec IR · Immutable snapshot</p>
          <h1>规格详情</h1>
          <p className="gf-specs__lede">
            当前视图读取一个不可变 IR Snapshot，并以同一 Artifact 身份分页检查图谱事实。
          </p>
        </div>
        <span className="gf-specs__status-mark">
          <BookOpenText aria-hidden="true" size={17} />
          只读快照
        </span>
      </header>

      <dl className="gf-specs__facts" aria-label="规格身份与 schema 绑定">
        <div>
          <dt>Artifact ID</dt>
          <dd>
            <CopyableText copyLabel="复制规格 Artifact ID" value={spec.artifact.artifact_id} />
          </dd>
        </div>
        <div>
          <dt>Snapshot ID</dt>
          <dd>
            <CopyableText copyLabel="复制 Snapshot ID" value={spec.snapshot_id} />
          </dd>
        </div>
        <div>
          <dt>Payload schema</dt>
          <dd>
            <code>{spec.artifact.payload_schema_id ?? "未公开"}</code>
          </dd>
        </div>
        <div>
          <dt>Schema registry</dt>
          <dd className="gf-specs__inline-fact">
            <Database aria-hidden="true" size={14} />
            <code>{spec.schema_registry_version}</code>
          </dd>
        </div>
        <div>
          <dt>Registry digest</dt>
          <dd>
            <CopyableText
              copyLabel="复制 Schema Registry digest"
              value={detail.data.registry.registry_digest}
            />
          </dd>
        </div>
        <div>
          <dt>Registered schemas</dt>
          <dd>{Object.keys(detail.data.registry.schemas).length}</dd>
        </div>
        <div className="gf-specs__fact-wide">
          <dt>Ref authority</dt>
          <dd>
            {exactRef && spec.ref_name ? (
              <span className="gf-specs__inline-fact gf-specs__inline-fact--authority">
                <GitBranch aria-hidden="true" size={14} />
                <a href={`/refs/${encodeURIComponent(spec.ref_name)}/history`}>{exactRef}</a>
              </span>
            ) : (
              <span className="gf-specs__muted">未绑定 ref；该 Artifact 不应被称为当前规格。</span>
            )}
          </dd>
        </div>
      </dl>

      <aside className="gf-specs__edit-boundary" role="note">
        <PencilRuler aria-hidden="true" size={20} />
        <div>
          <strong>IR 编辑边界</strong>
          <p>
            本页不直接修改 Snapshot。任何人工编辑必须创建 typed Patch 草案，绑定 exact
            base/ref，随后进入验证与审批。
          </p>
        </div>
      </aside>

      <section className="gf-specs__workspace-section" aria-labelledby="patch-draft-title">
        <header className="gf-specs__section-heading">
          <FilePenLine aria-hidden="true" size={19} />
          <div>
            <h2 id="patch-draft-title">创建 typed Patch 草案</h2>
            <p>
              Snapshot 保持不可变；这里仅创建 <code>patch@2</code> candidate，并绑定当前 exact base/ref 与显式
              export profiles。
            </p>
          </div>
        </header>

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
              description="传输结果未知；页面保留完全相同的 payload 与 idempotency key，不会自动创建第二个草案。"
              state="error"
              title="Patch 创建结果未知"
            />
          ))}

        {formError && <p role="alert">{formError}</p>}

        <form className="gf-form" onSubmit={submitPatchDraft}>
          <fieldset disabled={draftPending || draftAttempt !== null || draftResult !== null}>
            <legend className="u-small">Exact Patch draft input</legend>
            <label>
              Ref name
              <input onChange={(event) => setRefName(event.target.value)} required value={refName} />
            </label>
            <label className="gf-cluster">
              <input
                checked={noCurrentRef}
                onChange={(event) => setNoCurrentRef(event.target.checked)}
                type="checkbox"
              />
              确认当前 ref 不存在（expected_ref=null）
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
            <label>
              Candidate export profile
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
                    {profile.display_name} · {profileKey(profile)}
                  </option>
                ))}
              </select>
              <span className="u-small" id="patch-export-profile-help">
                可选；选择时使用 exact active config_export profile，不使用隐藏默认值。
              </span>
            </label>
            {currentProfiles.nextCursor && (
              <button
                className="gf-secondary-button"
                disabled={currentProfiles.loading}
                onClick={() => void readProfilePage(currentProfiles.nextCursor, false)}
                type="button"
              >
                {currentProfiles.loading ? "正在加载 profiles" : "加载更多 profiles"}
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
                      重新读取 profile 目录
                    </button>
                  ) : undefined
                }
                description="Profile 分页读取失败；不会回退到隐式 export profile。"
                state="error"
                title="无法继续读取 profiles"
              />
            )}
            <label>
              Constraint snapshot Artifact ID（可选）
              <input
                onChange={(event) => setConstraintSnapshotId(event.target.value)}
                value={constraintSnapshotId}
              />
            </label>
            <label>
              Patch operations JSON
              <textarea
                className="u-mono"
                onChange={(event) => setOperationsJson(event.target.value)}
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
            <label>
              Patch rationale
              <textarea
                onChange={(event) => setRationale(event.target.value)}
                required
                rows={3}
                value={rationale}
              />
            </label>
            <label>
              Side-effect risk
              <input
                onChange={(event) => setSideEffectRisk(event.target.value)}
                required
                value={sideEffectRisk}
              />
            </label>
            <button type="submit">创建 Patch 草案</button>
          </fieldset>
        </form>

        {draftResult && (
          <section className="gf-specs__authority" data-authority="candidate">
            <FilePenLine aria-hidden="true" size={22} />
            <div>
              <p className="gf-specs__authority-label">Candidate</p>
              <h3>Typed Patch 草案已创建</h3>
              <CopyableText copyLabel="复制 Patch Artifact ID" value={draftResult.artifact.artifact_id} />
              <a href={`/patches/${encodeURIComponent(draftResult.artifact.artifact_id)}`}>打开 Patch 草案</a>
            </div>
          </section>
        )}
      </section>

      <section className="gf-specs__workspace-section" aria-labelledby="spec-graph-title">
        <header className="gf-specs__section-heading">
          <Network aria-hidden="true" size={19} />
          <div>
            <h2 id="spec-graph-title">有界知识图谱</h2>
            <p>
              图谱页 <code>{currentGraph.readSnapshotId}</code>；画布与键盘事实表共享选择。
            </p>
          </div>
        </header>
        {currentGraph.items.length === 0 ? (
          <StatePanel
            description="该结果不表示规格无效；当前读取快照仅没有可展示的 entity/relation。"
            state="empty"
            title="当前快照没有图谱事实"
          />
        ) : (
          <KnowledgeGraph
            ariaLabel="规格知识图谱"
            items={currentGraph.items}
            nextCursor={currentGraph.nextCursor}
            onLoadMore={(cursor) => void readGraphPage(cursor, false)}
            onRestart={() => void readGraphPage(null, true)}
            pageLabel={`Snapshot ${spec.snapshot_id}`}
            paginationState={graphPaginationState(currentGraph)}
          />
        )}
      </section>
    </div>
  );
}
