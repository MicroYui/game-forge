import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { projectsApi, type Project, type ProjectsApi } from "./api";

/** The query key every project-scoped page filters by, and the shell writes. */
export const PROJECT_PARAM = "project";

export interface SelectedProject {
  /** `null` means "all games" — the only view where content no project owns is reachable. */
  projectId: string | null;
  project: Project | null;
  projects: readonly Project[];
  loading: boolean;
  select(projectId: string | null): void;
}

/**
 * The selected game, held in the URL rather than in component state.
 *
 * The URL is the source of truth so a deep link is shareable, browser history works,
 * and React Query keys pick the selection up without a second copy of server state.
 */
export function useSelectedProject(api: Pick<ProjectsApi, "listProjects"> = projectsApi): SelectedProject {
  const [searchParams, setSearchParams] = useSearchParams();
  const projectId = searchParams.get(PROJECT_PARAM)?.trim() || null;

  const query = useQuery({
    queryFn: () => api.listProjects(),
    queryKey: ["project-selection", "projects"],
    retry: false,
  });
  const projects = useMemo(() => query.data?.items ?? [], [query.data]);
  const project = useMemo(
    () => projects.find((item) => item.project_id === projectId) ?? null,
    [projects, projectId],
  );

  const select = useCallback(
    (next: string | null) => {
      setSearchParams((current) => {
        const params = new URLSearchParams(current);
        if (next === null) params.delete(PROJECT_PARAM);
        else params.set(PROJECT_PARAM, next);
        // A cursor is signed over the query it belongs to, so it cannot survive a
        // change of game. Dropping it here asks for page one, which is correct.
        params.delete("cursor");
        return params;
      });
    },
    [setSearchParams],
  );

  return { projectId, project, projects, loading: query.isPending, select };
}

/** Carry the current selection onto another route, so switching pages keeps the game. */
export function scopedHref(path: string, projectId: string | null): string {
  if (projectId === null) return path;
  const [base, existing] = path.split("?", 2);
  const params = new URLSearchParams(existing ?? "");
  params.set(PROJECT_PARAM, projectId);
  return `${base}?${params.toString()}`;
}
