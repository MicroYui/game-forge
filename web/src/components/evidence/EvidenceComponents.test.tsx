import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { components } from "../../api/generated/openapi";
import { EvidenceSections, FindingCard } from ".";

type FindingRevision = components["schemas"]["FindingRevisionV1"];

const finding: FindingRevision = {
  created_at: "2026-07-19T10:00:00Z",
  finding_id: `finding:${"长".repeat(512)}`,
  payload: {
    confidence: null,
    constraint_id: "constraint:newbie-gold-cap",
    defect_class: "reward_out_of_range",
    entities: ["quest:newbie-bridge"],
    evidence: { actual: 120, maximum: 80 },
    message: "新手任务奖励超过冻结上限。",
    minimal_repro: {
      assertion: "reward_gold = 120 > 80",
      source_ref: {
        adapter: "aureus-csv",
        column: "reward_gold",
        file: "quest.csv",
        row: 17,
        sheet: "Quest",
      },
    },
    oracle_type: "deterministic",
    payload_schema_version: "finding-payload@1",
    producer_id: "checker:reward-cap",
    producer_run_id: "run:checker-1",
    relations: [],
    severity: "critical",
    snapshot_id: "snapshot:immutable-42",
    source: "checker",
    status: "confirmed",
  },
  revision: 7,
  revision_schema_version: "finding-revision@1",
  supersedes_revision: 6,
};

describe("evidence components", () => {
  it("separates deterministic, simulation, LLM, and unproven evidence with visible labels and icons", () => {
    render(
      <EvidenceSections
        deterministic={<p>图检查器反例</p>}
        simulation={<p>一万次仿真分布</p>}
        suggestion={<p>叙事一致性建议</p>}
        unproven={<p>z3 超时，结论未证明</p>}
      />,
    );

    const deterministic = screen.getByRole("region", { name: "确定性预言机" });
    const simulation = screen.getByRole("region", { name: "仿真证据（描述性）" });
    const suggestion = screen.getByRole("region", { name: "LLM 建议（需人确认）" });
    const unproven = screen.getByRole("region", { name: "未证明（不可视为通过）" });

    expect(deterministic).toHaveAttribute("data-evidence-kind", "deterministic");
    expect(simulation).toHaveAttribute("data-evidence-kind", "simulation");
    expect(suggestion).toHaveAttribute("data-evidence-kind", "suggestion");
    expect(unproven).toHaveAttribute("data-evidence-kind", "unproven");
    expect(within(deterministic).getByText("图检查器反例")).toBeVisible();
    expect(within(simulation).getByText("一万次仿真分布")).toBeVisible();
    expect(within(suggestion).getByText("叙事一致性建议")).toBeVisible();
    expect(within(unproven).getByText("z3 超时，结论未证明")).toBeVisible();
    expect(deterministic.querySelector("svg")).not.toBeNull();
    expect(simulation.querySelector("svg")).not.toBeNull();
    expect(suggestion.querySelector("svg")).not.toBeNull();
    expect(unproven.querySelector("svg")).not.toBeNull();
  });

  it("keeps the optional unproven partition visible with an honest empty state", () => {
    render(<EvidenceSections />);

    const unproven = screen.getByRole("region", { name: "未证明（不可视为通过）" });
    expect(within(unproven).getByText("暂无未证明结果")).toBeVisible();
    expect(
      within(unproven).getByText("证据不足、未知、超时、超预算或尚待验证；未证明绝不等于通过。"),
    ).toBeVisible();
  });

  it("renders a finding's exact immutable revision, oracle, repro, and source reference", () => {
    render(<FindingCard finding={finding} />);

    expect(screen.getByRole("article")).toHaveAttribute("data-oracle", "deterministic");
    expect(screen.getByText("严重 · critical")).toBeVisible();
    expect(screen.getByText("确定性预言机")).toBeVisible();
    expect(screen.getByText("已确认 · confirmed")).toBeVisible();
    expect(screen.getByText("不可变修订 7")).toBeVisible();
    expect(screen.getByText("snapshot:immutable-42")).toBeVisible();
    expect(screen.getByText("aureus-csv · quest.csv / Quest / 第 17 行 / reward_gold")).toBeVisible();
    expect(screen.getByText(/reward_gold = 120 > 80/)).toBeVisible();
    expect(screen.getByText(/"actual": 120/)).toBeVisible();
    expect(screen.getByText(/"maximum": 80/)).toBeVisible();
    expect(screen.getByText(finding.finding_id)).toHaveClass("gf-copyable__value");
    expect(screen.getByRole("button", { name: "复制 Finding ID" })).toBeVisible();
    expect(screen.queryByRole("link", { name: "查看 exact Finding 修订" })).not.toBeInTheDocument();
  });

  it.each([
    ["confirmed", "已确认 · confirmed"],
    ["unproven", "未证明 · unproven"],
    ["dismissed", "已驳回 · dismissed"],
    ["fixed", "已修复 · fixed"],
    ["accepted_risk", "已接受风险 · accepted_risk"],
  ] as const)("renders the %s lifecycle status without recasting it as pass", (status, label) => {
    render(<FindingCard finding={{ ...finding, payload: { ...finding.payload, status } }} />);

    expect(screen.getByText(label)).toHaveAttribute("data-status-label", status);
  });

  it("uses only a caller-supplied exact Finding detail href", () => {
    const detailHref = "/findings/finding%3Anewbie-gold?revision=7";
    render(<FindingCard detailHref={detailHref} finding={finding} />);

    expect(screen.getByRole("link", { name: "查看 exact Finding 修订" })).toHaveAttribute("href", detailHref);
  });

  it("shows the optional Run-link digest and evidence Artifact authority", () => {
    render(
      <FindingCard
        authorityBinding={{
          attemptNo: 2,
          evidenceArtifactId: "artifact:checker:quest-cap",
          findingDigest: "f".repeat(64),
          ordinal: 5,
        }}
        finding={finding}
      />,
    );

    expect(screen.getByText("attempt 2 · ordinal 5")).toBeVisible();
    expect(screen.getByText("f".repeat(64))).toBeVisible();
    expect(screen.getByRole("link", { name: "artifact:checker:quest-cap" })).toHaveAttribute(
      "href",
      "/artifacts/artifact%3Achecker%3Aquest-cap",
    );
  });
});
