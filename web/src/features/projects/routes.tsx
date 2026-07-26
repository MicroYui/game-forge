import { Navigate, useParams } from "react-router-dom";

import { ProjectOverviewPage } from "./ProjectOverviewPage";
import { ProjectsPage } from "./ProjectsPage";
import { ProjectWorkspacePage } from "./ProjectWorkspacePage";

export function ProjectsRoute() {
  return <ProjectsPage />;
}

export function ProjectOverviewRoute() {
  const { projectId } = useParams<{ projectId: string }>();
  if (!projectId) return <Navigate replace to="/projects" />;
  return <ProjectOverviewPage projectId={projectId} />;
}

export function ProjectWorkspaceRoute() {
  const { projectId } = useParams<{ projectId: string }>();
  if (!projectId) return <Navigate replace to="/projects" />;
  return <ProjectWorkspacePage projectId={projectId} />;
}
