import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import canonicalReport from "../../../../scenarios/bench/bench-report.json";
import { ApiProblemError, type SafeProblem } from "../../api/problem";
import { createQueryClient } from "../../api/query-client";
import { EvalPage } from "./EvalPage";
import type { BenchReportRead, EvalApi } from "./api";
import type { BenchReportData } from "./model";

function decodeCanonicalFloats(value: unknown): unknown {
  if (typeof value === "string" && value.startsWith("f:")) return Number(value.slice(2));
  if (Array.isArray(value)) return value.map(decodeCanonicalFloats);
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, decodeCanonicalFloats(item)]));
  }
  return value;
}

function report(): BenchReportData {
  return decodeCanonicalFloats(structuredClone(canonicalReport)) as BenchReportData;
}

function pendingQaReport(): BenchReportData {
  const pending = report();
  pending.evidence = pending.evidence.map((item) =>
    item.evidence_id === "qa" ? { ...item, available: false, sha256: null } : item,
  );
  pending.qa = {
    ...pending.qa,
    assisted_success: {
      ...pending.qa.assisted_success,
      ci_high: null,
      ci_low: null,
      ci_method: null,
      evaluated_n: 0,
      evidence_ref: null,
      k: 0,
      rate: null,
      status: "pending",
    },
    conclusion: "pending",
    evidence_ref: null,
    manual_success: {
      ...pending.qa.manual_success,
      ci_high: null,
      ci_low: null,
      ci_method: null,
      evaluated_n: 0,
      evidence_ref: null,
      k: 0,
      rate: null,
      status: "pending",
    },
    paired_saved_fraction: {
      ...pending.qa.paired_saved_fraction,
      ci_high: null,
      ci_low: null,
      ci_method: null,
      evaluated_n: 0,
      evidence_ref: null,
      mean: null,
      median: null,
      p95: null,
      primary_estimate: null,
      status: "pending",
    },
    paired_saved_minutes: {
      ...pending.qa.paired_saved_minutes,
      ci_high: null,
      ci_low: null,
      ci_method: null,
      evaluated_n: 0,
      evidence_ref: null,
      mean: null,
      median: null,
      p95: null,
      primary_estimate: null,
      status: "pending",
    },
  };
  return pending;
}

function measuredQaReport(): BenchReportData {
  return report();
}

function read(overrides: Partial<BenchReportRead> = {}): BenchReportRead {
  return {
    artifactId: "artifact:bench-report:2026-07-20",
    etag: '"bench-report:2026-07-20"',
    report: report(),
    ...overrides,
  };
}

function api(overrides: Partial<EvalApi> = {}): EvalApi {
  return {
    getBenchReport: vi.fn(async () => read()),
    ...overrides,
  };
}

function renderPage(evalApi: EvalApi) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={["/eval"]}>
        <EvalPage api={evalApi} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("EvalPage", () => {
  it("renders the exact report authority and all 15 BDR classes in separate oracle partitions", async () => {
    renderPage(api());

    expect(await screen.findByRole("heading", { level: 1, name: "质量评测" })).toBeVisible();
    expect(screen.getByRole("link", { name: "查看报告来源记录" })).toHaveAttribute(
      "href",
      "/artifacts/artifact%3Abench-report%3A2026-07-20",
    );
    expect(screen.getByRole("link", { name: "查看报告血缘" })).toHaveAttribute(
      "href",
      "/artifacts/artifact%3Abench-report%3A2026-07-20/lineage",
    );
    expect(screen.getByText('"bench-report:2026-07-20"')).not.toBeVisible();

    const deterministic = screen.getByRole("table", { name: "确定性问题检出率" });
    const simulation = screen.getByRole("table", { name: "仿真问题检出率" });
    const llm = screen.getByRole("table", { name: "AI 辅助问题检出率" });
    expect(within(deterministic).getAllByRole("row")).toHaveLength(11);
    expect(within(simulation).getAllByRole("row")).toHaveLength(2);
    expect(within(llm).getAllByRole("row")).toHaveLength(5);

    const economy = within(simulation).getByRole("row", {
      name: /经济系统可能失衡/,
    });
    expect(economy).toHaveTextContent("82 / 82");
    expect(economy).toHaveTextContent("82");
    expect(economy).toHaveTextContent("100%");
    expect(economy).toHaveTextContent("95% 置信区间");
    expect(economy).toHaveTextContent("0.022");
    expect(economy).toHaveTextContent("0.05");
    expect(economy).toHaveTextContent("已测量");
    expect(economy).toHaveTextContent("seeded-checker-sim@1");
    expect(economy).toHaveTextContent("seeded");

    expect(within(llm).getByRole("row", { name: /角色设定冲突/ })).toHaveTextContent("AI 辅助问题检出率");
    expect(screen.getByRole("table", { name: "叙事检查误报率" })).toHaveTextContent("叙事检查误报率");
    const narrativeProvenance = screen.getAllByRole("complementary", {
      name: "叙事指标证据来源",
    });
    expect(narrativeProvenance[0]).toHaveTextContent("模型与评测材料已固定");
    expect(narrativeProvenance[0]).toHaveTextContent(report().narrative.protocol_sha256);
    expect(narrativeProvenance[0]).toHaveTextContent(report().narrative.corpus_manifest_sha256);
    for (const element of screen.getAllByText(report().narrative.protocol_sha256)) {
      expect(element).not.toBeVisible();
    }
  });

  it("keeps a long BenchReport Artifact ID keyboard-scrollable", async () => {
    const artifactId = `artifact:${"a".repeat(512)}`;
    renderPage(api({ getBenchReport: vi.fn(async () => read({ artifactId })) }));

    await userEvent.setup().click(await screen.findByText("查看报告技术信息"));
    const value = await screen.findByText(artifactId);
    expect(value).toHaveAttribute("tabindex", "0");
  });

  it("keeps oracle-FP, constraint-FP, other FP metrics, and every agent outcome independent", async () => {
    const extended = report();
    extended.false_positives.push({
      ...extended.false_positives[0]!,
      bucket: "future_fp",
      name: "future_false_positive_metric",
    });
    extended.agent.push({
      ...extended.agent[0]!,
      bucket: "future_agent",
      name: "future_agent_metric",
    });
    renderPage(api({ getBenchReport: vi.fn(async () => read({ report: extended })) }));

    await screen.findByRole("heading", { level: 1, name: "质量评测" });
    const headline = screen.getByRole("region", { name: "关键指标" });
    expect(within(headline).getByRole("heading", { name: "确定性检查误报率" })).toBeVisible();
    expect(within(headline).getByRole("heading", { name: "约束检查误报率" })).toBeVisible();
    expect(within(headline).getByRole("heading", { name: "修复通过率" })).toBeVisible();
    expect(within(headline).getByText("0/1 (0%)")).toBeVisible();
    expect(within(headline).getByText("0/902 (0%)")).toBeVisible();
    expect(within(headline).getByText("10/10 (100%)")).toBeVisible();

    const fpTable = screen.getByRole("table", { name: "误报率指标" });
    expect(within(fpTable).getByText("确定性检查误报率")).toBeVisible();
    expect(within(fpTable).getByText("约束检查误报率")).toBeVisible();
    expect(within(fpTable).getByText("叙事检查误报率")).toBeVisible();
    expect(within(fpTable).getByTitle("技术代码：future_false_positive_metric")).toBeVisible();

    const agentTable = screen.getByRole("table", { name: "智能助手效果" });
    expect(within(agentTable).getByText("修复通过率")).toBeVisible();
    expect(within(agentTable).getByText("分层规划试玩完成率")).toBeVisible();
    expect(within(agentTable).getByTitle("技术代码：future_agent_metric")).toBeVisible();
    const agentChart = screen.getByRole("figure", {
      name: "智能助手效果比率",
    });
    expect(within(agentChart).getByRole("row", { name: "分层规划试玩 70%" })).toBeInTheDocument();
  });

  it("shows external development and verification separately with source and underpowered status", async () => {
    renderPage(api());
    const section = await screen.findByRole("region", { name: "外部效度" });

    expect(within(section).getByText("Endless Sky")).toBeVisible();
    expect(within(section).getByText("GitHub 上的 Endless Sky 开源项目")).toBeVisible();
    expect(within(section).getAllByText("8 / 8").length).toBeGreaterThanOrEqual(2);
    expect(within(section).getByText("endless-sky-reader@1")).not.toBeVisible();
    expect(within(section).getByText("endless-sky-adapter@1")).not.toBeVisible();
    expect(within(section).getAllByText("样本量不足")).toHaveLength(8);
    expect(within(section).getByRole("table", { name: "外部病例开发组" })).toBeVisible();
    expect(within(section).getByRole("table", { name: "外部病例独立验证组" })).toBeVisible();
    expect(within(section).getAllByText("外部病例误报率")).toHaveLength(2);
  });

  it("renders HED distributions and dispositions without collapsing them into one score", async () => {
    renderPage(api());
    const section = await screen.findByRole("region", {
      name: "人工编辑距离",
    });

    const distributions = within(section).getByRole("table", {
      name: "编辑距离分布",
    });
    expect(within(distributions).getByText("原始编辑量")).toBeVisible();
    expect(within(distributions).getByText("标准化编辑距离")).toBeVisible();
    expect(within(distributions).getByText(/平均 9.375/)).toBeVisible();
    expect(within(distributions).getByText(/平均 0.907/)).toBeVisible();

    const dispositions = within(section).getByRole("table", {
      name: "人工处理结果",
    });
    expect(within(dispositions).getByText("无需人工编辑")).toBeVisible();
    expect(within(dispositions).getByText("需要人工编辑")).toBeVisible();
    expect(within(dispositions).getByText("结果无法使用")).toBeVisible();
    expect(within(dispositions).getByText("评测流程无效")).toBeVisible();
  });

  it("preserves deferred human QA as named missing states and never renders a zero or pass verdict", async () => {
    const pending = pendingQaReport();
    renderPage(api({ getBenchReport: vi.fn(async () => read({ report: pending })) }));
    const qa = await screen.findByRole("region", { name: "真人 QA" });

    expect(
      within(qa).getByText(
        "八场实测使用隔离的本地测试工具；正确场次按实际操作时间计分，错误或超时场次按 8 分钟计分，原始操作时长仍完整保留。",
      ),
    ).toBeVisible();
    expect(within(qa).queryByText(/真实 Console/)).not.toBeInTheDocument();
    const states = within(qa).getByLabelText("真人评测证据状态");
    expect(within(states).getByText("等待真人证据")).toBeVisible();
    expect(within(states).getByText("证据缺失")).toBeVisible();
    expect(within(states).getByText("尚未测量")).toBeVisible();
    expect(within(states).getByText("验收条件尚未满足")).toBeVisible();
    expect(within(qa).getByText("scenarios/external_cases/endless_sky/qa-evidence.json")).not.toBeVisible();
    expect(within(qa).getByText("有一条计划证据尚未绑定，不计入本次结果")).toBeVisible();
    expect(within(qa).getByText("尚未绑定可核验的真人评测证据。")).toBeVisible();
    const qaTable = within(qa).getByRole("table", { name: "真人评测指标" });
    expect(within(qaTable).queryByText(/0%|pass/i)).not.toBeInTheDocument();
    for (const row of within(qaTable).getAllByRole("row").slice(1)) {
      expect(row).toHaveTextContent("等待真人证据");
    }
  });

  it("shows measured QA results when exact human evidence becomes available", async () => {
    const measured = measuredQaReport();
    renderPage(api({ getBenchReport: vi.fn(async () => read({ report: measured })) }));
    const qa = await screen.findByRole("region", { name: "真人 QA" });

    expect(within(qa).getByText("工具能够节省策划时间")).toBeVisible();
    expect(within(qa).getByText("3/4 (75%)")).toBeVisible();
    expect(within(qa).getByText("0/4 (0%)")).toBeVisible();
    expect(within(qa).getByText(/平均 3.408/)).toBeVisible();
    expect(within(qa).queryByText("验收条件尚未满足")).not.toBeInTheDocument();
    expect(within(qa).queryByText("等待真人证据")).not.toBeInTheDocument();
  });

  it("does not call human evidence available when one measured QA metric lacks evidence", async () => {
    const measured = measuredQaReport();
    measured.qa = {
      ...measured.qa,
      manual_success: { ...measured.qa.manual_success, evidence_ref: null },
    };
    renderPage(api({ getBenchReport: vi.fn(async () => read({ report: measured })) }));

    const qa = await screen.findByRole("region", { name: "真人 QA" });
    const states = within(qa).getByLabelText("真人评测证据状态");
    expect(within(states).getByText("证据缺失")).toBeVisible();
    expect(within(states).getByText("验收条件尚未满足")).toBeVisible();
    expect(within(states).queryByText("真人证据可用")).not.toBeInTheDocument();
  });

  it("separates six Agent workloads from deterministic runtime and exposes unknown attempts honestly", async () => {
    renderPage(api());
    const cost = await screen.findByRole("region", { name: "成本与延迟" });

    expect(within(cost).getAllByTestId("agent-workload")).toHaveLength(6);
    const repair = within(cost).getByRole("article", { name: "自动修复搜索" });
    expect(within(repair).getByText("GPT-5.6（本次实测版本）")).toBeVisible();
    expect(within(repair).getAllByText("10 / 10")).toHaveLength(2);
    expect(within(repair).getByText("0 / 0")).toBeVisible();
    expect(within(repair).getByText("10 条")).toBeVisible();
    expect(within(repair).getByText("费用未测量")).toBeVisible();
    expect(within(repair).getByText("模型响应耗时", { exact: true })).toBeVisible();
    expect(within(repair).getByText("模型用量置信区间", { exact: true })).toBeVisible();
    expect(
      within(repair).getByText("响应耗时置信区间", {
        exact: true,
      }),
    ).toBeVisible();
    expect(within(cost).getByText(/不等于整条业务流程耗时，也不等于固定回放耗时/)).toBeVisible();

    const deterministic = within(cost).getByRole("article", {
      name: "确定性运行时",
    });
    expect(within(deterministic).getByText("确定性检查与仿真")).toBeVisible();
    expect(within(deterministic).getByText(/平均 6.457/)).toBeVisible();
    expect(within(deterministic).getByText("seeded-runtime@1")).not.toBeVisible();
  });

  it("lists version and evidence paths with hashes while keeping local paths non-clickable", async () => {
    renderPage(api());
    await userEvent.setup().click(await screen.findByText("查看版本与证据技术目录"));
    const evidence = await screen.findByRole("table", {
      name: "Evidence catalog",
    });
    const qaRow = within(evidence).getByRole("row", {
      name: /qa available qa-evidence@2/,
    });
    expect(qaRow).toHaveTextContent("scenarios/external_cases/endless_sky/qa-evidence.json");
    expect(within(qaRow).queryByRole("link")).not.toBeInTheDocument();
    const seededRow = within(evidence).getByRole("row", { name: /seeded/ });
    expect(seededRow).toHaveTextContent("b79af05fd4b0c774");

    const versions = screen.getByRole("table", { name: "Version bindings" });
    expect(within(versions).getByText("constraints")).toBeVisible();
    expect(within(versions).getByText("constraint-bundle@1")).toBeVisible();
    expect(
      within(versions).getByText("cdc3a2d7cc8cd3b32a881a03ad5cc42f5dafe6829d04fc3718ea74fd98e172a0"),
    ).toBeVisible();
  });

  it("does not invent an Artifact identity when X-Artifact-ID is absent", async () => {
    renderPage(api({ getBenchReport: vi.fn(async () => read({ artifactId: null })) }));

    expect(await screen.findByText("来源标识缺失，无法打开追溯记录")).toBeVisible();
    expect(screen.queryByRole("link", { name: "查看报告来源记录" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "查看报告血缘" })).not.toBeInTheDocument();
  });

  it("explains an unavailable quality report and retries only after an explicit click", async () => {
    const user = userEvent.setup();
    const problem: SafeProblem = {
      code: "dependency_unavailable",
      conflict_set_id: null,
      detail: "BenchReport storage is unavailable.",
      earliest_cursor: null,
      instance: "/api/v1/bench/report",
      request_id: "request:eval:503",
      retry_after_s: 3,
      run_id: null,
      status: 503,
      title: "Dependency unavailable",
      trace_id: "trace:eval:503",
      type: "about:blank",
    };
    const getBenchReport = vi
      .fn<EvalApi["getBenchReport"]>()
      .mockRejectedValueOnce(new ApiProblemError(problem))
      .mockResolvedValueOnce(read());
    renderPage(api({ getBenchReport }));

    expect(await screen.findByRole("alert")).toHaveTextContent("质量报告暂时不可读取");
    expect(screen.getByRole("alert")).toHaveTextContent("报告绑定与存储状态");
    await user.click(screen.getByText("查看报告读取技术信息"));
    expect(screen.getByText("dependency_unavailable")).toBeVisible();
    expect(getBenchReport).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "重试读取 BenchReport" }));

    expect(await screen.findByRole("heading", { level: 1, name: "质量评测" })).toBeVisible();
    await waitFor(() => expect(getBenchReport).toHaveBeenCalledTimes(2));
  });

  it("shows a semantic loading state while the report is pending", () => {
    renderPage(
      api({
        getBenchReport: vi.fn<EvalApi["getBenchReport"]>(() => new Promise<BenchReportRead>(() => undefined)),
      }),
    );

    expect(screen.getByRole("status")).toHaveTextContent("正在读取 BenchReport");
  });
});
