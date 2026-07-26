import { useQuery } from "@tanstack/react-query";
import { ArrowRight, FolderKanban, Plus } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { createMutationIntent } from "../../api/csrf";
import { ApiProblemError } from "../../api/problem";
import { compactDateTime, TechnicalDetails } from "../../components/identity";
import { ProblemPanel, StatePanel } from "../../components/ui";
import { projectsApi, type ProjectsApi } from "./api";
import "./projects.css";

type ProjectsPageApi = Pick<ProjectsApi, "createProject" | "listProjects">;

const projectStatusLabels: Readonly<Record<string, string>> = {
  active: "已有发布版本",
  archived: "已归档",
  draft: "尚未发布内容",
};

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("项目操作未完成。");
}

function ProjectListError({ error, retry }: { error: Error; retry(): void }) {
  if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
  return (
    <StatePanel
      action={
        <button className="gf-secondary-button" onClick={retry} type="button">
          重新读取
        </button>
      }
      description="暂时无法读取项目列表，请稍后重试。"
      state="error"
      title="项目列表读取失败"
    />
  );
}

export function ProjectsPage({ api = projectsApi }: { api?: ProjectsPageApi }) {
  const navigate = useNavigate();
  const projects = useQuery({
    queryFn: () => api.listProjects(),
    queryKey: ["projects"],
    retry: false,
  });
  const [displayName, setDisplayName] = useState("");
  const [projectKey, setProjectKey] = useState("");
  const [genre, setGenre] = useState("");
  const [description, setDescription] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<Error | null>(null);

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setSubmitError(null);
    try {
      const created = await api.createProject(
        {
          description,
          display_name: displayName,
          domain_scope: { domain_ids: ["builtin"] },
          genre,
          project_key: projectKey,
          request_schema_version: "project-create-request@1",
        },
        createMutationIntent(),
      );
      // The button promises material next, so land where material is added.
      navigate(`/projects/${encodeURIComponent(created.value.project_id)}/authoring`);
    } catch (error) {
      setSubmitError(normalizedError(error));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="gf-page gf-projects">
      <header className="gf-projects__hero">
        <div>
          <p className="u-kicker">从一个游戏想法开始</p>
          <h1>游戏项目</h1>
          <p>把创意、策划文档、实体关系、规则和试玩都放进同一个项目里。</p>
        </div>
        <div className="gf-projects__hero-mark" aria-hidden="true">
          <FolderKanban size={34} />
          <span>项目是你的创作入口</span>
        </div>
      </header>

      <div className="gf-projects__layout">
        <section className="gf-projects__catalog" aria-labelledby="project-catalog-title">
          <header>
            <div>
              <p className="u-kicker">继续创作</p>
              <h2 id="project-catalog-title">我的项目</h2>
            </div>
            {projects.data && <span className="u-chip">{projects.data.items.length} 个</span>}
          </header>

          {projects.isPending && (
            <StatePanel description="正在读取你的游戏项目。" state="loading" title="正在加载项目" />
          )}
          {projects.isError && (
            <ProjectListError error={normalizedError(projects.error)} retry={() => void projects.refetch()} />
          )}
          {projects.data?.items.length === 0 && (
            <StatePanel
              description="先在右侧填写一个名字和一句话创意，创建后就能添加策划材料。"
              state="empty"
              title="还没有游戏项目"
            />
          )}
          {projects.data && projects.data.items.length > 0 && (
            <div className="gf-projects__cards">
              {projects.data.items.map((project) => (
                <article className="gf-projects__card" key={project.project_id}>
                  <header>
                    <span className={project.status === "active" ? "u-status u-status--ok" : "u-status"}>
                      {projectStatusLabels[project.status] ?? project.status}
                    </span>
                    <h3>{project.display_name}</h3>
                    <p>{project.description || "还没有填写项目简介。"}</p>
                  </header>
                  <dl>
                    <div>
                      <dt>类型</dt>
                      <dd>{project.genre || "待补充"}</dd>
                    </div>
                    <div>
                      <dt>最近更新</dt>
                      <dd>{compactDateTime(project.updated_at)}</dd>
                    </div>
                  </dl>
                  <Link
                    className="gf-primary-link"
                    to={`/projects/${encodeURIComponent(project.project_id)}`}
                  >
                    进入项目
                    <ArrowRight aria-hidden="true" size={16} />
                  </Link>
                  <TechnicalDetails
                    items={[
                      { copyLabel: "复制项目标识", label: "项目标识", value: project.project_id },
                      { label: "项目代号", value: project.project_key },
                    ]}
                    summary="查看技术信息"
                  />
                </article>
              ))}
            </div>
          )}
        </section>

        <section className="gf-projects__create" aria-labelledby="create-project-title">
          <header>
            <span className="gf-projects__step-number">1</span>
            <div>
              <p className="u-kicker">新游戏</p>
              <h2 id="create-project-title">创建项目</h2>
              <p>这里只建立创作空间，不会自动发布任何内容。</p>
            </div>
          </header>
          {submitError instanceof ApiProblemError && <ProblemPanel problem={submitError.problem} />}
          {submitError && !(submitError instanceof ApiProblemError) && (
            <p className="gf-projects__inline-error" role="alert">
              项目暂时没有创建成功，请检查输入后重试。
            </p>
          )}
          <form className="gf-form" onSubmit={(event) => void createProject(event)}>
            <label>
              游戏名称
              <input
                autoComplete="off"
                maxLength={256}
                onChange={(event) => setDisplayName(event.target.value)}
                placeholder="例如：天空港计划"
                required
                value={displayName}
              />
            </label>
            <label>
              项目代号
              <input
                aria-label="项目代号"
                autoComplete="off"
                maxLength={64}
                onChange={(event) => setProjectKey(event.target.value.toLowerCase())}
                pattern="[a-z0-9]+(?:-[a-z0-9]+)*"
                placeholder="例如：sky-harbor"
                required
                value={projectKey}
              />
              <small>使用英文小写、数字和短横线；仅用于项目导航。</small>
            </label>
            <label>
              游戏类型
              <input
                maxLength={128}
                onChange={(event) => setGenre(event.target.value)}
                placeholder="例如：叙事经营、动作 RPG"
                value={genre}
              />
            </label>
            <label>
              一句话创意
              <textarea
                maxLength={4096}
                onChange={(event) => setDescription(event.target.value)}
                placeholder="玩家是谁、做什么、最特别的体验是什么？"
                rows={4}
                value={description}
              />
            </label>
            <button disabled={submitting} type="submit">
              <Plus aria-hidden="true" size={17} />
              {submitting ? "正在创建…" : "创建并添加材料"}
            </button>
          </form>
        </section>
      </div>
    </div>
  );
}
