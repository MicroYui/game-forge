import { describe, expect, it } from "vitest";

import { breadcrumbsFor } from "./routes";

describe("breadcrumbsFor", () => {
  it.each([
    ["/specs/artifact%3Aspec%3Afrontier", "规格详情"],
    ["/constraints/artifact%3Aconstraint%3Afrontier", "约束快照"],
    ["/constraint-proposals/artifact%3Aproposal%3Afrontier", "约束候选"],
  ])("keeps Task 7 detail routes under the Spec/KG parent", (pathname, detailTitle) => {
    expect(breadcrumbsFor(pathname)).toEqual([
      { path: "/specs", title: "工作台" },
      { path: "/specs", title: "规范与知识图谱" },
      { title: detailTitle },
    ]);
  });

  it.each([
    ["/reviews/artifact%3Areview%3Afrontier", "Review 详情"],
    ["/findings/finding%3Afrontier/revisions/7", "Finding 修订"],
  ])("keeps Task 9 detail routes under the Review parent", (pathname, detailTitle) => {
    expect(breadcrumbsFor(pathname)).toEqual([
      { path: "/specs", title: "工作台" },
      { path: "/reviews", title: "审查报告" },
      { title: detailTitle },
    ]);
  });

  it.each([
    ["/patches/artifact%3Apatch%3Afrontier", "补丁详情"],
    ["/rollback-requests/artifact%3Arollback%3Afrontier", "回滚请求"],
    ["/refs/spec%2Fmain/history", "引用历史"],
  ])("keeps Task 11 workflow routes under the Patch parent", (pathname, detailTitle) => {
    expect(breadcrumbsFor(pathname)).toEqual([
      { path: "/specs", title: "工作台" },
      { path: "/patches", title: "补丁与差异" },
      { title: detailTitle },
    ]);
  });
});
