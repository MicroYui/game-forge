import { useQuery } from "@tanstack/react-query";
import { Braces, FilePenLine, FileStack, GitBranch, LibraryBig, ShieldQuestion } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { compactDateTime, ResourceIdentity, TechnicalDetails } from "../../components/identity";
import { CursorTable, type CursorPaginationState, type CursorTableColumn } from "../../components/tables";
import { ProblemPanel, StatePanel } from "../../components/ui";
import { projectsApi, type Project, type ProjectsApi } from "../projects/api";
import { useSelectedProject } from "../projects/selection";
import {
  specWorkflowApi,
  type ArtifactKind,
  type ArtifactPage,
  type ConstraintProposalReadView,
  type ConstraintSnapshotView,
  type SpecView,
  type SpecWorkflowApi,
} from "./api";
import {
  SpecEntryPanels,
  type ProjectConstraintAuthoringContext,
  type SpecEntryPanelsApi,
} from "./SpecEntryPanels";
import "./specs.css";

export type SpecWorkspaceApi = SpecEntryPanelsApi &
  Pick<
    SpecWorkflowApi,
    "listArtifacts" | "listConstraintProposals" | "listConstraintSnapshots" | "listSpecs"
  > &
  // Projects own both the material names and the content ref each version sits on,
  // so this cross-project workspace has to read them to say what anything IS.
  Pick<ProjectsApi, "listMaterials" | "listProjects">;

interface CursorPageState<T> {
  error?: Error;
  items: T[];
  loading: boolean;
  nextCursor: string | null;
  readSnapshotId: string;
}

function toPageState<T>(page: {
  items: T[];
  next_cursor?: string | null;
  read_snapshot_id: string;
}): CursorPageState<T> {
  return {
    items: page.items,
    loading: false,
    nextCursor: page.next_cursor ?? null,
    readSnapshotId: page.read_snapshot_id,
  };
}

function paginationState<T>(state: CursorPageState<T>): CursorPaginationState {
  if (state.error instanceof CursorExpiredError) return "expired";
  if (state.error) return "error";
  return state.loading ? "loading" : "ready";
}

function normalizedPageError(error: unknown): Error {
  return error instanceof Error ? error : new Error("分页读取失败。");
}

async function readCompleteSourceCatalog(
  api: Pick<SpecWorkflowApi, "listArtifacts">,
  kind: Extract<ArtifactKind, "source_raw" | "source_rendered">,
  projectId: string | null,
): Promise<ArtifactPage["items"]> {
  const items: ArtifactPage["items"] = [];
  const artifactIds = new Set<string>();
  const cursors = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listArtifacts(kind, cursor, projectId);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new Error(`${kind} 来源目录分页快照发生变化。`);
    }
    readSnapshotId = page.read_snapshot_id;
    for (const artifact of page.items) {
      if (artifact.kind !== kind) throw new Error(`${kind} 来源目录返回了错误 kind。`);
      if (artifactIds.has(artifact.artifact_id)) throw new Error(`${kind} 来源目录返回了重复 Artifact。`);
      artifactIds.add(artifact.artifact_id);
      items.push(artifact);
    }
    const next = page.next_cursor ?? null;
    if (next === null) return items;
    if (cursors.has(next)) throw new Error(`${kind} 来源目录游标形成循环。`);
    cursors.add(next);
    cursor = next;
  }
  throw new Error(`${kind} 来源目录超过有界页数。`);
}

function domainLabel(value: SpecView["artifact"]["domain_scope"]): string {
  if (value === "all") return "全部域";
  if (value === null) return "未公开域投影";
  const labels: Record<string, string> = {
    builtin: "内置内容",
    "domain:combat": "战斗系统",
    "domain:economy": "经济系统",
    "domain:narrative": "叙事内容",
    "domain:quest": "任务系统",
    "domain:rewards": "奖励系统",
  };
  return (
    value.domain_ids.map((item) => labels[item] ?? item.replace(/^domain:/u, "")).join(" · ") || "无指定领域"
  );
}

const proposalStatusLabels: Record<string, string> = {
  applied: "已发布",
  approved: "已批准",
  changes_requested: "待修改",
  draft: "草稿",
  pending_approval: "待审批",
  rejected: "已驳回",
  superseded: "已被替代",
  validated: "验证通过",
  validating: "验证中",
  validation_failed: "验证失败",
};

function specVersionTitle(
  item: SpecView,
  unpublishedOrder: ReadonlyMap<string, number>,
  projectNames: ReadonlyMap<string, string>,
): string {
  const owner = item.ref_name ? projectNames.get(item.ref_name) : undefined;
  const version = item.ref_value
    ? `当前内容 · 第 ${item.ref_value.revision} 版`
    : `未发布内容 · 第 ${unpublishedOrder.get(item.artifact.artifact_id) ?? "—"} 份`;
  return owner ? `${owner} · ${version}` : version;
}

function specColumns(
  items: readonly SpecView[],
  projects: readonly Project[],
): readonly CursorTableColumn<SpecView>[] {
  const unpublishedOrder = new Map(
    items
      .filter((item) => !item.ref_value)
      .map((item, index) => [item.artifact.artifact_id, index + 1] as const),
  );
  // Revision alone does not identify a version: every project's content head starts
  // at 第 1 版, so two rows otherwise render byte-identical titles. The owning game
  // is what a planner actually recognises, and the ref name names it exactly —
  // `GameProjectV1` pins `content_ref_name` to `projects/{project_id}/content/head`.
  const projectNames = new Map(
    projects.map((project) => [project.content_ref_name, project.display_name] as const),
  );
  return [
    {
      header: "内容版本",
      id: "artifact",
      render: (item) => (
        <ResourceIdentity
          actionLabel="查看内容与关系图"
          description={`${compactDateTime(item.artifact.created_at)} · ${domainLabel(item.artifact.domain_scope)}`}
          details={[
            {
              copyLabel: "复制内容标识",
              label: "内容标识",
              value: item.artifact.artifact_id,
            },
            {
              copyLabel: "复制快照标识",
              label: "快照标识",
              value: item.snapshot_id,
            },
            { label: "数据格式版本", value: item.schema_registry_version },
          ]}
          href={`/specs/${encodeURIComponent(item.artifact.artifact_id)}`}
          title={specVersionTitle(item, unpublishedOrder, projectNames)}
        />
      ),
    },
    {
      header: "发布状态",
      id: "ref",
      render: (item) =>
        item.ref_name && item.ref_value ? (
          <div className="gf-specs__ref-binding">
            <GitBranch aria-hidden="true" size={14} />
            <div>
              <strong>当前发布版本</strong>
              <a href={`/refs/${encodeURIComponent(item.ref_name)}/history`}>查看版本历史</a>
              <TechnicalDetails
                items={[
                  { label: "发布位置", value: item.ref_name },
                  { label: "发布版本", value: String(item.ref_value.revision) },
                ]}
              />
            </div>
          </div>
        ) : (
          <span className="gf-specs__muted">未发布，不会被当作当前版本使用</span>
        ),
    },
    {
      header: "域",
      id: "domain",
      render: (item) => <span>{domainLabel(item.artifact.domain_scope)}</span>,
    },
  ];
}

const constraintColumns: readonly CursorTableColumn<ConstraintSnapshotView>[] = [
  {
    header: "规则版本",
    id: "artifact",
    render: (item) => (
      <ResourceIdentity
        actionLabel="查看规则"
        description={`${compactDateTime(item.artifact.created_at)} · ${item.constraints.length} 条规则`}
        details={[
          {
            copyLabel: "复制规则版本标识",
            label: "规则版本标识",
            value: item.artifact.artifact_id,
          },
          { label: "规则语法版本", value: item.dsl_grammar_version },
        ]}
        href={`/constraints/${encodeURIComponent(item.artifact.artifact_id)}`}
        title="校验规则"
      />
    ),
  },
  {
    header: "规则格式",
    id: "grammar",
    render: () => <span>已绑定，可确定性检查</span>,
  },
  {
    header: "条目",
    id: "count",
    render: (item) => `${item.constraints.length} 条`,
  },
  {
    header: "权威状态",
    id: "authority",
    render: () => (
      <span className="gf-specs__authority-unknown">
        <ShieldQuestion aria-hidden="true" size={14} />
        发布状态需查看发布记录
      </span>
    ),
  },
];

function projectProposalQuery(context: ProjectConstraintAuthoringContext): string {
  const query = new URLSearchParams({
    constraintRef: context.constraintRefName,
    project: context.projectId,
    projectName: context.projectName,
  });
  if (context.baseConstraintArtifactId && context.baseConstraintRevision) {
    query.set("constraint", context.baseConstraintArtifactId);
    query.set("constraintRevision", String(context.baseConstraintRevision));
  }
  return query.toString();
}

function proposalMatchesProject(
  proposal: ConstraintProposalReadView,
  context: ProjectConstraintAuthoringContext,
): boolean {
  const sources = proposal.proposal.source_bindings.map((binding) => binding.source_artifact_id).sort();
  return (
    proposal.proposal.base_constraint_snapshot_id === context.baseConstraintArtifactId &&
    sources.length === context.sourceArtifactIds.length &&
    sources.every((source, index) => source === context.sourceArtifactIds[index])
  );
}

function proposalColumns(
  projectContext: ProjectConstraintAuthoringContext | null,
): readonly CursorTableColumn<ConstraintProposalReadView>[] {
  const projectQuery = projectContext ? projectProposalQuery(projectContext) : "";
  return [
    {
      header: "规则提案",
      id: "artifact",
      render: (item) => (
        <ResourceIdentity
          actionLabel="查看提案"
          description={`${compactDateTime(item.artifact.created_at)} · ${item.proposal.constraints.length} 条规则`}
          details={[
            {
              copyLabel: "复制提案标识",
              label: "提案标识",
              value: item.artifact.artifact_id,
            },
          ]}
          href={`/constraint-proposals/${encodeURIComponent(item.artifact.artifact_id)}${projectQuery ? `?${projectQuery}` : ""}`}
          title={`${projectContext ? `${projectContext.projectName}项目 · ` : ""}规则提案 · 第 ${item.proposal.revision} 版`}
        />
      ),
    },
    {
      header: "创建方式",
      id: "producer",
      render: (item) => (
        <div className="gf-specs__table-primary">
          <strong>{item.proposal.produced_by === "agent" ? "AI 生成" : "人工创建"}</strong>
          {item.proposal.producer_run_id ? (
            <>
              <a href={`/runs/${encodeURIComponent(item.proposal.producer_run_id)}`}>查看生成记录</a>
              <TechnicalDetails
                items={[
                  {
                    label: "生成运行标识",
                    value: item.proposal.producer_run_id,
                  },
                ]}
              />
            </>
          ) : (
            <span className="gf-specs__muted">由策划直接创建</span>
          )}
        </div>
      ),
    },
    {
      header: "状态",
      id: "approval",
      render: (item) => <span>{proposalStatusLabels[item.approval_status] ?? item.approval_status}</span>,
    },
  ];
}

function WorkspaceError({ error, onRetry }: { error: Error; onRetry(): void }) {
  if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
  return (
    <StatePanel
      action={
        <button className="gf-secondary-button" onClick={onRetry} type="button">
          重试
        </button>
      }
      description="规格与约束快照读取失败；未展示底层异常内容。"
      state="error"
      title="无法读取规格工作台"
    />
  );
}

const workspaceApi: SpecWorkspaceApi = { ...specWorkflowApi, ...projectsApi };

export function SpecWorkspacePage({ api = workspaceApi }: { api?: SpecWorkspaceApi }) {
  const [searchParams] = useSearchParams();
  const projectContextResult = useMemo<{
    error: string | null;
    value: ProjectConstraintAuthoringContext | null;
  }>(() => {
    const projectId = searchParams.get("project")?.trim();
    if (!projectId) return { error: null, value: null };
    const constraintRefName = searchParams.get("constraintRef")?.trim() ?? "";
    const baseConstraintArtifactId = searchParams.get("constraint")?.trim() || null;
    const revisionText = searchParams.get("constraintRevision")?.trim() || null;
    if (!constraintRefName || (baseConstraintArtifactId === null) !== (revisionText === null)) {
      return {
        error: "项目规则发布位置或当前版本绑定不完整，请返回项目刷新后重试。",
        value: null,
      };
    }
    if (revisionText !== null && !/^[1-9]\d*$/u.test(revisionText)) {
      return { error: "项目当前规则版本号无效，请返回项目刷新后重试。", value: null };
    }
    const sourceArtifactIds = [
      ...new Set(
        searchParams
          .getAll("source")
          .map((item) => item.trim())
          .filter(Boolean),
      ),
    ].sort();
    return {
      error: null,
      value: {
        baseConstraintArtifactId,
        baseConstraintRevision: revisionText === null ? null : Number(revisionText),
        constraintRefName,
        projectId,
        projectName: searchParams.get("projectName")?.trim() || projectId.replace(/^project:/u, ""),
        sourceArtifactIds,
      },
    };
  }, [searchParams]);
  const projectContext = projectContextResult.value;
  // The shell's game selector narrows every list on this page.
  const { projectId: selectedProjectId } = useSelectedProject();
  const workspace = useQuery({
    queryFn: async () => {
      const [specs, constraintSnapshots, constraintProposals, sourceRaw, sourceRendered, projects] =
        await Promise.all([
          api.listSpecs(null),
          api.listConstraintSnapshots(null),
          api.listConstraintProposals(null),
          readCompleteSourceCatalog(api, "source_raw", selectedProjectId),
          readCompleteSourceCatalog(api, "source_rendered", selectedProjectId),
          api.listProjects(),
        ]);
      // One call per project: materials are only addressable under their owner, and
      // a planner's workspace holds few enough projects for that to be the honest read.
      const materials = (
        await Promise.all(projects.items.map((project) => api.listMaterials(project.project_id)))
      ).flatMap((page) => page.items);
      return {
        constraintProposals,
        constraintSnapshots,
        materials,
        projects: projects.items,
        sources: [...sourceRaw, ...sourceRendered],
        specs,
      };
    },
    queryKey: ["spec-workspace", selectedProjectId ?? ""],
    retry: false,
  });
  const [specs, setSpecs] = useState<CursorPageState<SpecView> | null>(null);
  const [constraintSnapshots, setConstraintSnapshots] =
    useState<CursorPageState<ConstraintSnapshotView> | null>(null);
  const [constraintProposals, setConstraintProposals] =
    useState<CursorPageState<ConstraintProposalReadView> | null>(null);

  useEffect(() => {
    if (!workspace.data) return;
    setSpecs(toPageState(workspace.data.specs));
    setConstraintSnapshots(toPageState(workspace.data.constraintSnapshots));
    setConstraintProposals(toPageState(workspace.data.constraintProposals));
  }, [workspace.data]);

  async function readSpecsPage(cursor: string | null, restart: boolean) {
    const current = specs;
    if (!current) return;
    setSpecs({ ...current, error: undefined, loading: true });
    try {
      const next = await api.listSpecs(cursor);
      if (!restart && next.read_snapshot_id !== current.readSnapshotId) {
        throw new Error("规格分页快照发生变化，请重新开始查询。");
      }
      setSpecs({
        ...toPageState(next),
        items: restart ? next.items : [...current.items, ...next.items],
      });
    } catch (error) {
      setSpecs({
        ...current,
        error: normalizedPageError(error),
        loading: false,
      });
    }
  }

  async function readConstraintPage(cursor: string | null, restart: boolean) {
    const current = constraintSnapshots;
    if (!current) return;
    setConstraintSnapshots({ ...current, error: undefined, loading: true });
    try {
      const next = await api.listConstraintSnapshots(cursor);
      if (!restart && next.read_snapshot_id !== current.readSnapshotId) {
        throw new Error("约束快照分页发生变化，请重新开始查询。");
      }
      setConstraintSnapshots({
        ...toPageState(next),
        items: restart ? next.items : [...current.items, ...next.items],
      });
    } catch (error) {
      setConstraintSnapshots({
        ...current,
        error: normalizedPageError(error),
        loading: false,
      });
    }
  }

  async function readProposalPage(cursor: string | null, restart: boolean) {
    const current = constraintProposals;
    if (!current) return;
    setConstraintProposals({ ...current, error: undefined, loading: true });
    try {
      const next = await api.listConstraintProposals(cursor);
      if (!restart && next.read_snapshot_id !== current.readSnapshotId) {
        throw new Error("约束提案分页发生变化，请重新开始查询。");
      }
      setConstraintProposals({
        ...toPageState(next),
        items: restart ? next.items : [...current.items, ...next.items],
      });
    } catch (error) {
      setConstraintProposals({
        ...current,
        error: normalizedPageError(error),
        loading: false,
      });
    }
  }

  if (workspace.isPending) {
    return (
      <div className="gf-page gf-specs">
        <StatePanel
          description="正在读取有界规格、constraint_snapshot、constraint proposal 与可用来源目录。"
          headingLevel={1}
          state="loading"
          title="正在读取规格工作台"
        />
      </div>
    );
  }

  if (workspace.isError) {
    return (
      <div className="gf-page gf-specs">
        <header className="gf-page-header">
          <p className="gf-specs__kicker">Design-Spec IR · Read workspace</p>
          <h1>规格与约束快照</h1>
        </header>
        <WorkspaceError error={workspace.error} onRetry={() => void workspace.refetch()} />
      </div>
    );
  }

  const currentSpecs = specs ?? toPageState(workspace.data.specs);
  const currentConstraints = constraintSnapshots ?? toPageState(workspace.data.constraintSnapshots);
  const currentProposals = constraintProposals ?? toPageState(workspace.data.constraintProposals);
  const visibleProposals = projectContext
    ? currentProposals.items.filter((proposal) => proposalMatchesProject(proposal, projectContext))
    : currentProposals.items;
  const empty =
    currentSpecs.items.length === 0 &&
    currentConstraints.items.length === 0 &&
    currentProposals.items.length === 0;

  return (
    <div className="gf-page gf-specs" data-layout="editorial-workspace">
      <header className="gf-specs__hero">
        <div>
          <p className="gf-specs__kicker">策划内容工作台</p>
          <h1>内容与规则</h1>
          <p className="gf-specs__lede">
            管理游戏内容版本、校验规则和 AI 提案；所有历史版本都可查看、比较和追溯。
          </p>
        </div>
        <dl className="gf-specs__edition" aria-label="当前有界读取摘要">
          <div>
            <dt>规格</dt>
            <dd>{currentSpecs.items.length}</dd>
          </div>
          <div>
            <dt>约束快照</dt>
            <dd>{currentConstraints.items.length}</dd>
          </div>
          <div>
            <dt>约束提案</dt>
            <dd>{currentProposals.items.length}</dd>
          </div>
        </dl>
      </header>

      <aside className="gf-specs__semantic-note" role="note">
        <Braces aria-hidden="true" size={20} />
        <div>
          <strong>只有标记为当前版本的内容，才会用于正式生成、检查和试玩。</strong>
          <p>未发布的内容和规则会保留为候选，方便继续修改，不会悄悄替换正式版本。</p>
        </div>
      </aside>

      {projectContextResult.error && (
        <StatePanel
          action={
            <a className="gf-secondary-button" href="/projects">
              返回游戏项目
            </a>
          }
          description={projectContextResult.error}
          state="error"
          title="项目规则绑定不完整"
        />
      )}

      {empty ? (
        <StatePanel
          description="当前授权范围内没有规格、约束快照或提案；本视图不会虚构默认或当前版本。"
          state="empty"
          title="尚无可读取的规格、约束快照或提案"
        />
      ) : (
        <div className="gf-specs__workspace-grid">
          <section className="gf-specs__workspace-section" aria-labelledby="constraint-proposals-title">
            <header className="gf-specs__section-heading">
              <FilePenLine aria-hidden="true" size={19} />
              <div>
                <h2 id="constraint-proposals-title">待处理的规则提案</h2>
                <p>查看 AI 或人工创建的新规则，以及它们当前的审批状态。</p>
              </div>
            </header>
            <CursorTable
              caption="规则提案"
              columns={proposalColumns(projectContext)}
              emptyLabel={projectContext ? "这个项目暂无规则提案" : "当前授权范围内没有约束提案"}
              getRowKey={(item) => item.artifact.artifact_id}
              items={visibleProposals}
              nextCursor={currentProposals.nextCursor}
              onLoadMore={(cursor) => void readProposalPage(cursor, false)}
              onRestart={() => void readProposalPage(null, true)}
              paginationState={paginationState(currentProposals)}
              toolbar={
                <div className="gf-specs__snapshot-label">
                  <FileStack aria-hidden="true" size={14} />
                  <TechnicalDetails
                    items={[
                      {
                        label: "目录读取快照",
                        value: currentProposals.readSnapshotId,
                      },
                    ]}
                    summary="目录技术信息"
                  />
                </div>
              }
            />
          </section>

          <section className="gf-specs__workspace-section" aria-labelledby="spec-artifacts-title">
            <header className="gf-specs__section-heading">
              <LibraryBig aria-hidden="true" size={19} />
              <div>
                <h2 id="spec-artifacts-title">内容版本</h2>
                <p>当前发布版本和未发布候选会明确区分，历史内容不会被误当作当前内容。</p>
              </div>
            </header>
            <CursorTable
              caption="内容版本列表"
              columns={specColumns(currentSpecs.items, workspace.data.projects)}
              emptyLabel="当前授权范围内没有规格工件"
              getRowKey={(item) => item.artifact.artifact_id}
              items={currentSpecs.items}
              nextCursor={currentSpecs.nextCursor}
              onLoadMore={(cursor) => void readSpecsPage(cursor, false)}
              onRestart={() => void readSpecsPage(null, true)}
              paginationState={paginationState(currentSpecs)}
              toolbar={
                <div className="gf-specs__snapshot-label">
                  <FileStack aria-hidden="true" size={14} />
                  <TechnicalDetails
                    items={[
                      {
                        label: "目录读取快照",
                        value: currentSpecs.readSnapshotId,
                      },
                    ]}
                    summary="目录技术信息"
                  />
                </div>
              }
            />
          </section>

          <section className="gf-specs__workspace-section" aria-labelledby="constraint-artifacts-title">
            <header className="gf-specs__section-heading">
              <FileStack aria-hidden="true" size={19} />
              <div>
                <h2 id="constraint-artifacts-title">校验规则版本</h2>
                <p>这里保存每个可追溯的规则版本；是否已发布需要查看对应的发布记录。</p>
              </div>
            </header>
            <CursorTable
              caption="校验规则版本列表"
              columns={constraintColumns}
              emptyLabel="当前授权范围内没有约束快照工件"
              getRowKey={(item) => item.artifact.artifact_id}
              items={currentConstraints.items}
              nextCursor={currentConstraints.nextCursor}
              onLoadMore={(cursor) => void readConstraintPage(cursor, false)}
              onRestart={() => void readConstraintPage(null, true)}
              paginationState={paginationState(currentConstraints)}
              toolbar={
                <div className="gf-specs__snapshot-label">
                  <FileStack aria-hidden="true" size={14} />
                  <TechnicalDetails
                    items={[
                      {
                        label: "目录读取快照",
                        value: currentConstraints.readSnapshotId,
                      },
                    ]}
                    summary="目录技术信息"
                  />
                </div>
              }
            />
          </section>
        </div>
      )}

      {!projectContextResult.error && (
        <SpecEntryPanels
          api={api}
          catalogs={{
            constraints: currentConstraints.items,
            materials: workspace.data.materials,
            proposals: currentProposals.items,
            sources: workspace.data.sources,
            specs: currentSpecs.items,
          }}
          projectContext={projectContext}
        />
      )}
    </div>
  );
}
