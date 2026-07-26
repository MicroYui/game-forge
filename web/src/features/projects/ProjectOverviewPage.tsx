import { useQuery } from "@tanstack/react-query";
import { BookOpenText, GitBranch, Gamepad2, ScrollText, Sparkles } from "lucide-react";
import { Link } from "react-router-dom";

import { compactDateTime, TechnicalDetails } from "../../components/identity";
import { KnowledgeGraph } from "../../components/kg";
import { StatePanel } from "../../components/ui";
import { projectsApi, type GraphPage, type Project, type ProjectMaterial, type ProjectsApi } from "./api";
import { projectLink } from "./links";
import "./projects.css";

/** The Artifact whose graph shows what the game currently is. */
function currentContentArtifact(project: Project): string {
  return project.current_content_ref?.artifact_id ?? project.bootstrap_snapshot_artifact_id;
}

function authoringHref(projectId: string): string {
  return `/projects/${encodeURIComponent(projectId)}/authoring`;
}

function graphCounts(page: GraphPage | undefined): { entities: number; relations: number } {
  let entities = 0;
  let relations = 0;
  for (const item of page?.items ?? []) {
    if (item.item_kind === "entity") entities += 1;
    else relations += 1;
  }
  return { entities, relations };
}

export function ProjectOverviewPage({
  api = projectsApi,
  projectId,
}: {
  api?: ProjectsApi;
  projectId: string;
}) {
  const projectQuery = useQuery({
    queryFn: () => api.getProject(projectId),
    queryKey: ["project-overview", projectId],
    retry: false,
  });
  const project = projectQuery.data?.value ?? null;
  const contentArtifactId = project ? currentContentArtifact(project) : null;
  const materialQuery = useQuery({
    queryFn: () => api.listMaterials(projectId),
    queryKey: ["project-overview", "materials", projectId],
    retry: false,
  });
  const graphQuery = useQuery({
    enabled: contentArtifactId !== null,
    queryFn: () => api.listContentGraph(contentArtifactId!, null),
    queryKey: ["project-overview", "graph", contentArtifactId ?? ""],
    retry: false,
  });

  if (projectQuery.isPending) {
    return (
      <div className="gf-page gf-project-overview">
        <StatePanel description="正在读取这个游戏的当前内容。" state="loading" title="正在加载" />
      </div>
    );
  }
  if (projectQuery.error || project === null) {
    return (
      <div className="gf-page gf-project-overview">
        <StatePanel
          action={
            <button className="gf-secondary-button" onClick={() => void projectQuery.refetch()} type="button">
              重新读取
            </button>
          }
          description="这个游戏项目暂时读不出来；稍后重试或返回项目列表。"
          headingLevel={1}
          state="error"
          title="项目读取失败"
        />
      </div>
    );
  }

  const materials: readonly ProjectMaterial[] = materialQuery.data?.items ?? [];
  const counts = graphCounts(graphQuery.data);
  const published = project.current_content_ref !== null;
  const hasGraph = counts.entities + counts.relations > 0;

  return (
    <div className="gf-page gf-project-overview">
      <header className="gf-project-overview__hero">
        <p className="gf-project-overview__kicker">{project.genre || "游戏项目"}</p>
        <h1>{project.display_name}</h1>
        <p className="gf-project-overview__pitch">
          {project.description || "还没有写下这个游戏的一句话创意。"}
        </p>
        <dl className="gf-project-overview__facts">
          <div>
            <dt>正式内容</dt>
            <dd>{published ? `第 ${project.current_content_ref!.revision} 版内容` : "尚未发布"}</dd>
          </div>
          <div>
            <dt>规则</dt>
            <dd>
              {project.current_constraint_ref
                ? `第 ${project.current_constraint_ref.revision} 版规则`
                : "尚未建立"}
            </dd>
          </div>
          <div>
            <dt>最近更新</dt>
            <dd>{compactDateTime(project.updated_at)}</dd>
          </div>
        </dl>
        <TechnicalDetails
          items={[
            { copyLabel: "复制项目标识", label: "项目标识", value: project.project_id },
            { label: "内容发布位置", value: project.content_ref_name },
            { label: "规则发布位置", value: project.constraint_ref_name },
          ]}
          summary="查看项目技术信息"
        />
      </header>

      <section aria-labelledby="project-graph-title" className="gf-project-overview__graph">
        <header>
          <h2 id="project-graph-title">
            <GitBranch aria-hidden="true" size={18} /> 游戏内容图谱
          </h2>
          <p>{hasGraph ? `${counts.entities} 个内容 · ${counts.relations} 条关系` : "尚未建立内容图谱"}</p>
        </header>
        {hasGraph ? (
          <>
            <KnowledgeGraph ariaLabel="内容关系视图" items={graphQuery.data?.items ?? []} />
            <Link
              className="gf-project-overview__graph-link"
              to={`/specs/${encodeURIComponent(currentContentArtifact(project))}`}
            >
              查看完整图谱
            </Link>
          </>
        ) : (
          <StatePanel
            action={
              <Link className="gf-primary-button" to={authoringHref(project.project_id)}>
                从策划材料开始
              </Link>
            }
            description="先给它一些策划材料，AI 会提取角色、地点、任务和它们的关系；你也可以直接手动搭第一批内容。"
            state="empty"
            title="这个游戏还没有内容"
          />
        )}
      </section>

      <section aria-labelledby="project-next-title" className="gf-project-overview__actions">
        <h2 id="project-next-title">继续创作</h2>
        {materialQuery.isPending ? (
          <StatePanel description="正在读取这个游戏的策划材料。" state="loading" title="正在加载" />
        ) : materialQuery.error ? (
          <StatePanel
            action={
              <button
                className="gf-secondary-button"
                onClick={() => void materialQuery.refetch()}
                type="button"
              >
                重新读取
              </button>
            }
            description="读不出这个游戏的策划材料，所以暂时给不出会带上材料的入口；稍后重试。"
            state="error"
            title="材料读取失败"
          />
        ) : (
          <ul>
            <li>
              <Link aria-label="从策划材料生成内容" to={authoringHref(project.project_id)}>
                <BookOpenText aria-hidden="true" size={18} />
                <span>
                  <strong>从策划材料生成内容</strong>
                  <small>上传或粘贴策划案，AI 提取实体与关系，确认后发布新版本</small>
                </span>
              </Link>
            </li>
            <li>
              <Link aria-label="生成与维护规则" to={projectLink("/specs", project, materials)}>
                <ScrollText aria-hidden="true" size={18} />
                <span>
                  <strong>生成与维护规则</strong>
                  <small>建立任务、经济、战斗等确定性约束</small>
                </span>
              </Link>
            </li>
            <li>
              <Link aria-label="进入自动试玩" to={projectLink("/playtest", project, materials)}>
                <Gamepad2 aria-hidden="true" size={18} />
                <span>
                  <strong>进入自动试玩</strong>
                  <small>{published ? "让 Agent 按当前内容跑一遍任务链" : "发布首个内容版本后可用"}</small>
                </span>
              </Link>
            </li>
            <li>
              <Link aria-label="继续生成内容" to={projectLink("/generation", project, materials)}>
                <Sparkles aria-hidden="true" size={18} />
                <span>
                  <strong>继续生成内容</strong>
                  <small>在当前内容基础上让 AI 补充 NPC、任务或数值</small>
                </span>
              </Link>
            </li>
          </ul>
        )}
      </section>
    </div>
  );
}
