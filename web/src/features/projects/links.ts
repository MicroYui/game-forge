import type { Project, ProjectMaterial } from "./api";

/** One builder for every project-scoped entry, so a link carries the same
 *  exact content and constraint bindings wherever it is offered. */
export function projectLink(path: string, project: Project, materials: readonly ProjectMaterial[]): string {
  const query = new URLSearchParams({ project: project.project_id, projectName: project.display_name });
  if (path === "/specs") {
    query.set("section", "proposals");
    query.set("constraintRef", project.constraint_ref_name);
  }
  const content = project.current_content_ref;
  if (content) {
    query.set(path === "/playtest" ? "projectContent" : "content", content.artifact_id);
    query.set("contentRef", project.content_ref_name);
    query.set("contentRevision", String(content.revision));
  }
  const constraint = project.current_constraint_ref;
  if (constraint) {
    query.set(path === "/playtest" ? "projectConstraint" : "constraint", constraint.artifact_id);
    if (path !== "/playtest") query.set("constraintRef", project.constraint_ref_name);
    query.set("constraintRevision", String(constraint.revision));
  }
  for (const material of materials) query.append("source", material.rendered_source_artifact_id);
  return `${path}?${query.toString()}`;
}
