import { useSelectedProject } from "./selection";

/**
 * Which game the content pages are showing.
 *
 * "全部游戏" is not a convenience: seeded catalog content, bench reports and DR drills
 * genuinely belong to no project, and without an unfiltered view they would disappear
 * from the product entirely.
 */
export function ProjectSelector() {
  const { projectId, projects, loading, select } = useSelectedProject();

  if (loading || projects.length === 0) return null;

  return (
    <label className="gf-project-selector">
      <span className="u-sr-only">当前游戏</span>
      <select onChange={(event) => select(event.target.value || null)} value={projectId ?? ""}>
        <option value="">全部游戏</option>
        {projects.map((project) => (
          <option key={project.project_id} value={project.project_id}>
            {project.display_name}
          </option>
        ))}
      </select>
    </label>
  );
}
