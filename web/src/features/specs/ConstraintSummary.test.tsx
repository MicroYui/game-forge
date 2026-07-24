import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { components } from "../../api/generated/openapi";
import { ConstraintSummaryList } from "./ConstraintSummary";

const constraint: components["schemas"]["Constraint"] = {
  assert: "reward_gold <= 80",
  dsl_grammar_version: "dsl@1",
  forall: { node_type: "REWARD", var: "reward", where: { currency: "gold" } },
  id: "side_quest_reward_gold_cap",
  kind: "numeric",
  note: "支线任务奖励金币不得超过 80。",
  oracle: "deterministic",
  predicates: [{ expr: "reward_gold >= 0", oracle: "deterministic" }],
  scope: { node_type: "QUEST", var: "q", where: { category: "side" } },
  severity: "major",
};

describe("ConstraintSummary", () => {
  it("renders the rule, scope, predicates and exact technical identity in readable Chinese", () => {
    render(<ConstraintSummaryList values={[constraint]} />);

    expect(screen.getByRole("heading", { name: "支线任务奖励金币不得超过 80。" })).toBeVisible();
    expect(screen.getByText("reward_gold ≤ 80")).toBeVisible();
    expect(screen.getByText(/QUEST · 变量 q/)).toBeVisible();
    expect(screen.getByText("确定性检查")).toBeVisible();
    expect(screen.getByText("重要")).toBeVisible();
    expect(screen.getByText("查看量词、前提与筛选条件")).toBeVisible();
    expect(screen.getByText("技术详情")).toBeVisible();
  });

  it("warns prominently when scope is absent", () => {
    render(<ConstraintSummaryList values={[{ ...constraint, scope: null }]} />);

    expect(screen.getByRole("alert")).toHaveTextContent("未声明适用对象");
    expect(screen.getByRole("alert")).toHaveTextContent("修订时请补充 scope");
  });

  it("fails closed instead of interpreting malformed values", () => {
    render(<ConstraintSummaryList values={[{ ...constraint, scope: { node_type: 42 } }]} />);

    expect(screen.getByRole("alert")).toHaveTextContent("typed contract 不一致");
    expect(screen.queryByText("reward_gold ≤ 80")).not.toBeInTheDocument();
  });
});
