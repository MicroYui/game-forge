import { describe, expect, it } from "vitest";

import { breadcrumbsFor, navigationRoutes } from "./routes";

describe("breadcrumbsFor", () => {
  it("makes projects the product home and keeps project detail beneath it", () => {
    expect(navigationRoutes[0]).toMatchObject({ path: "/projects", title: "游戏项目" });
    expect(breadcrumbsFor("/projects")).toEqual([{ title: "游戏项目" }]);
    expect(breadcrumbsFor("/projects/project%3Asky-harbor")).toEqual([
      { path: "/projects", title: "游戏项目" },
      { title: "项目创作" },
    ]);
  });

  it.each([
    ["/specs/artifact%3Aspec%3Afrontier", "内容版本详情"],
    ["/constraints/artifact%3Aconstraint%3Afrontier", "规则版本详情"],
    ["/constraint-proposals/artifact%3Aproposal%3Afrontier", "规则修改草案"],
  ])("keeps Task 7 detail routes under the Spec/KG parent", (pathname, detailTitle) => {
    expect(breadcrumbsFor(pathname)).toEqual([
      { path: "/projects", title: "游戏项目" },
      { path: "/specs", title: "内容与规则" },
      { title: detailTitle },
    ]);
  });

  it.each([
    ["/reviews/artifact%3Areview%3Afrontier", "检查报告详情"],
    ["/findings/finding%3Afrontier/revisions/7", "问题记录详情"],
  ])("keeps Task 9 detail routes under the Review parent", (pathname, detailTitle) => {
    expect(breadcrumbsFor(pathname)).toEqual([
      { path: "/projects", title: "游戏项目" },
      { path: "/reviews", title: "内容检查" },
      { title: detailTitle },
    ]);
  });

  it.each([
    ["/patches/artifact%3Apatch%3Afrontier", "修改草案详情"],
    ["/rollback-requests/artifact%3Arollback%3Afrontier", "回滚请求"],
    ["/refs/spec%2Fmain/history", "版本历史"],
  ])("keeps Task 11 workflow routes under the Patch parent", (pathname, detailTitle) => {
    expect(breadcrumbsFor(pathname)).toEqual([
      { path: "/projects", title: "游戏项目" },
      { path: "/patches", title: "修改与版本" },
      { title: detailTitle },
    ]);
  });
});
