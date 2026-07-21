import { matchPath } from "react-router-dom";

import { messages } from "../i18n/zh-CN";

export type NavigationIcon =
  | "specs"
  | "generation"
  | "reviews"
  | "playtest"
  | "patches"
  | "eval"
  | "observability"
  | "approvals";

export type NavigationRoute = {
  icon: NavigationIcon;
  path: string;
  title: string;
};

export const navigationRoutes: readonly NavigationRoute[] = [
  { icon: "specs", path: "/specs", title: messages.routes.specs },
  { icon: "generation", path: "/generation", title: messages.routes.generation },
  { icon: "reviews", path: "/reviews", title: messages.routes.reviews },
  { icon: "playtest", path: "/playtest", title: messages.routes.playtest },
  { icon: "patches", path: "/patches", title: messages.routes.patches },
  { icon: "eval", path: "/eval", title: messages.routes.eval },
  { icon: "observability", path: "/observability", title: messages.routes.observability },
  { icon: "approvals", path: "/approvals", title: messages.routes.approvals },
] as const;

type DetailRoute = {
  parentPath?: string;
  path: string;
  title: string;
};

export const detailRoutes: readonly DetailRoute[] = [
  { parentPath: "/specs", path: "/specs/:artifactId", title: messages.details.spec },
  {
    parentPath: "/specs",
    path: "/constraints/:artifactId",
    title: messages.details.constraintSnapshot,
  },
  {
    parentPath: "/specs",
    path: "/constraint-proposals/:artifactId",
    title: messages.details.constraintProposal,
  },
  { parentPath: "/observability", path: "/runs/:runId", title: messages.details.run },
  {
    parentPath: "/reviews",
    path: "/reviews/:artifactId",
    title: messages.details.review,
  },
  {
    parentPath: "/reviews",
    path: "/findings/:findingId/revisions/:revision",
    title: messages.details.finding,
  },
  { parentPath: "/patches", path: "/patches/:patchId", title: messages.details.patch },
  {
    parentPath: "/patches",
    path: "/rollback-requests/:artifactId",
    title: messages.details.rollback,
  },
  { parentPath: "/approvals", path: "/approvals/:approvalId", title: messages.details.approval },
  { path: "/artifacts/:artifactId", title: messages.details.artifact },
  { path: "/artifacts/:artifactId/lineage", title: messages.details.artifactLineage },
  { parentPath: "/patches", path: "/refs/:refName/history", title: messages.details.refHistory },
  { parentPath: "/observability", path: "/observability/traces/:traceId", title: messages.details.trace },
  ...(import.meta.env.DEV
    ? [{ path: "/__visual__/foundation", title: "视觉基础评审" } satisfies DetailRoute]
    : []),
] as const;

export type BreadcrumbItem = { path?: string; title: string };

export function breadcrumbsFor(pathname: string): BreadcrumbItem[] {
  const root: BreadcrumbItem = { path: "/specs", title: messages.shell.home };
  const topLevel = navigationRoutes.find((route) => matchPath({ end: true, path: route.path }, pathname));
  if (topLevel) return [root, { title: topLevel.title }];
  const detail = detailRoutes.find((route) => matchPath({ end: true, path: route.path }, pathname));
  if (!detail) return [root, { title: messages.details.notFound }];
  const parent = detail.parentPath
    ? navigationRoutes.find((route) => route.path === detail.parentPath)
    : undefined;
  return [root, ...(parent ? [{ path: parent.path, title: parent.title }] : []), { title: detail.title }];
}
