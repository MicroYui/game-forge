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

    expect(await screen.findByRole("heading", { level: 1, name: "Eval / Bench" })).toBeVisible();
    expect(screen.getByRole("link", { name: "打开 BenchReport Artifact" })).toHaveAttribute(
      "href",
      "/artifacts/artifact%3Abench-report%3A2026-07-20",
    );
    expect(screen.getByRole("link", { name: "查看 BenchReport 血缘" })).toHaveAttribute(
      "href",
      "/artifacts/artifact%3Abench-report%3A2026-07-20/lineage",
    );
    expect(screen.getByText('"bench-report:2026-07-20"')).toBeVisible();

    const deterministic = screen.getByRole("table", { name: "确定性 BDR" });
    const simulation = screen.getByRole("table", { name: "仿真 BDR" });
    const llm = screen.getByRole("table", { name: "LLM 辅助 BDR" });
    expect(within(deterministic).getAllByRole("row")).toHaveLength(11);
    expect(within(simulation).getAllByRole("row")).toHaveLength(2);
    expect(within(llm).getAllByRole("row")).toHaveLength(5);

    const economy = within(simulation).getByRole("row", { name: /economy_collapse/ });
    expect(economy).toHaveTextContent("82 / 82");
    expect(economy).toHaveTextContent("82");
    expect(economy).toHaveTextContent("100%");
    expect(economy).toHaveTextContent("wilson95");
    expect(economy).toHaveTextContent("0.022");
    expect(economy).toHaveTextContent("0.05");
    expect(economy).toHaveTextContent("measured");
    expect(economy).toHaveTextContent("seeded-checker-sim@1");
    expect(economy).toHaveTextContent("seeded");

    expect(within(llm).getByRole("row", { name: /character_violation/ })).toHaveTextContent("llm-assisted");
    expect(screen.getByRole("table", { name: "Narrative clean FP" })).toHaveTextContent("narrative_clean_fp");
    const narrativeProvenance = screen.getAllByRole("complementary", {
      name: "Narrative metric provenance",
    });
    expect(narrativeProvenance[0]).toHaveTextContent("openai / gpt-5.6-sol / pre-m4@1");
    expect(narrativeProvenance[0]).toHaveTextContent(report().narrative.protocol_sha256);
    expect(narrativeProvenance[0]).toHaveTextContent(report().narrative.corpus_manifest_sha256);
  });

  it("keeps a long BenchReport Artifact ID keyboard-scrollable", async () => {
    const artifactId = `artifact:${"a".repeat(512)}`;
    renderPage(api({ getBenchReport: vi.fn(async () => read({ artifactId })) }));

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

    await screen.findByRole("heading", { level: 1, name: "Eval / Bench" });
    const headline = screen.getByRole("region", { name: "关键指标" });
    expect(within(headline).getByRole("heading", { name: "Oracle FP" })).toBeVisible();
    expect(within(headline).getByRole("heading", { name: "Constraint FP" })).toBeVisible();
    expect(within(headline).getByRole("heading", { name: "Fix Pass Rate" })).toBeVisible();
    expect(within(headline).getByText("0/1 (0%)")).toBeVisible();
    expect(within(headline).getByText("0/902 (0%)")).toBeVisible();
    expect(within(headline).getByText("10/10 (100%)")).toBeVisible();

    const fpTable = screen.getByRole("table", { name: "False-positive 指标" });
    expect(within(fpTable).getByText("oracle_fp")).toBeVisible();
    expect(within(fpTable).getAllByText("constraint_fp")).toHaveLength(2);
    expect(within(fpTable).getByText("narrative_clean_fp")).toBeVisible();
    expect(within(fpTable).getByText("future_false_positive_metric")).toBeVisible();

    const agentTable = screen.getByRole("table", { name: "Agent outcomes" });
    expect(within(agentTable).getByText("fix_pass_rate")).toBeVisible();
    expect(within(agentTable).getByText("playtest_completion_layered")).toBeVisible();
    expect(within(agentTable).getByText("future_agent_metric")).toBeVisible();
    const agentChart = screen.getByRole("figure", { name: "Agent outcome rates" });
    expect(within(agentChart).getByRole("row", { name: "Playtest · layered 70%" })).toBeInTheDocument();
  });

  it("shows external development and verification separately with source and underpowered status", async () => {
    renderPage(api());
    const section = await screen.findByRole("region", { name: "外部效度" });

    expect(within(section).getByText("endless_sky")).toBeVisible();
    expect(within(section).getByText("https://github.com/endless-sky/endless-sky.git")).toBeVisible();
    expect(within(section).getAllByText("8 / 8").length).toBeGreaterThanOrEqual(2);
    expect(within(section).getByText("endless-sky-reader@1")).toBeVisible();
    expect(within(section).getByText("endless-sky-adapter@1")).toBeVisible();
    expect(within(section).getAllByText("underpowered")).toHaveLength(8);
    expect(within(section).getByRole("table", { name: "External development" })).toBeVisible();
    expect(within(section).getByRole("table", { name: "External verification" })).toBeVisible();
    expect(within(section).getByText("external_after_oracle_fp")).toBeVisible();
  });

  it("renders HED distributions and dispositions without collapsing them into one score", async () => {
    renderPage(api());
    const section = await screen.findByRole("region", { name: "Human-Edit-Distance" });

    const distributions = within(section).getByRole("table", { name: "HED distributions" });
    expect(within(distributions).getByText("hed_raw_distance")).toBeVisible();
    expect(within(distributions).getByText("hed_normalized_distance")).toBeVisible();
    expect(within(distributions).getByText(/mean 9.375/)).toBeVisible();
    expect(within(distributions).getByText(/mean 0.907/)).toBeVisible();

    const dispositions = within(section).getByRole("table", { name: "HED dispositions" });
    expect(within(dispositions).getByText("hed_unchanged")).toBeVisible();
    expect(within(dispositions).getByText("hed_edited")).toBeVisible();
    expect(within(dispositions).getByText("hed_unusable")).toBeVisible();
    expect(within(dispositions).getByText("hed_protocol_failure")).toBeVisible();
  });

  it("preserves deferred human QA as named missing states and never renders a zero or pass verdict", async () => {
    const pending = pendingQaReport();
    renderPage(api({ getBenchReport: vi.fn(async () => read({ report: pending })) }));
    const qa = await screen.findByRole("region", { name: "真人 QA" });

    expect(
      within(qa).getByText(
        "八场实测使用隔离本地 QA Runner；正确场次按实际 active 计分，错误或超时场次按 8 分钟计分；原始 active 时长另行完整保留。",
      ),
    ).toBeVisible();
    expect(within(qa).queryByText(/真实 Console/)).not.toBeInTheDocument();
    const states = within(qa).getByLabelText("QA evidence states");
    expect(within(states).getByText("pending_human_evidence")).toBeVisible();
    expect(within(states).getByText("evidence_missing")).toBeVisible();
    expect(within(states).getByText("not_measured")).toBeVisible();
    expect(within(states).getByText("qa.evidence_missing")).toBeVisible();
    expect(within(qa).getByText("scenarios/external_cases/endless_sky/qa-evidence.json")).toBeVisible();
    expect(within(qa).getByText("planned catalog entry / not bound")).toBeVisible();
    expect(within(qa).getByText("Exact QA evidence_ref 未绑定。")).toBeVisible();
    const qaTable = within(qa).getByRole("table", { name: "QA metrics" });
    expect(within(qaTable).queryByText(/0%|pass/i)).not.toBeInTheDocument();
    for (const row of within(qaTable).getAllByRole("row").slice(1)) {
      expect(row).toHaveTextContent("等待真人证据");
    }
  });

  it("shows measured QA results when exact human evidence becomes available", async () => {
    const measured = measuredQaReport();
    renderPage(api({ getBenchReport: vi.fn(async () => read({ report: measured })) }));
    const qa = await screen.findByRole("region", { name: "真人 QA" });

    expect(within(qa).getByText("savings")).toBeVisible();
    expect(within(qa).getByText("3/4 (75%)")).toBeVisible();
    expect(within(qa).getByText("0/4 (0%)")).toBeVisible();
    expect(within(qa).getByText(/mean 3.408/)).toBeVisible();
    expect(within(qa).queryByText("qa.evidence_missing")).not.toBeInTheDocument();
    expect(within(qa).queryByText("pending_human_evidence")).not.toBeInTheDocument();
  });

  it("does not call human evidence available when one measured QA metric lacks evidence", async () => {
    const measured = measuredQaReport();
    measured.qa = {
      ...measured.qa,
      manual_success: { ...measured.qa.manual_success, evidence_ref: null },
    };
    renderPage(api({ getBenchReport: vi.fn(async () => read({ report: measured })) }));

    const qa = await screen.findByRole("region", { name: "真人 QA" });
    const states = within(qa).getByLabelText("QA evidence states");
    expect(within(states).getByText("evidence_missing")).toBeVisible();
    expect(within(states).getByText("qa.evidence_missing")).toBeVisible();
    expect(within(states).queryByText("human evidence available")).not.toBeInTheDocument();
  });

  it("separates six Agent workloads from deterministic runtime and exposes unknown attempts honestly", async () => {
    renderPage(api());
    const cost = await screen.findByRole("region", { name: "成本与延迟" });

    expect(within(cost).getAllByTestId("agent-workload")).toHaveLength(6);
    const repair = within(cost).getByRole("article", { name: "repair-search" });
    expect(within(repair).getByText("openai / gpt-5.6-sol / pre-m4@1")).toBeVisible();
    expect(within(repair).getAllByText("10 / 10")).toHaveLength(2);
    expect(within(repair).getByText("0 / 0")).toBeVisible();
    expect(within(repair).getByText("10 unknown records")).toBeVisible();
    expect(within(repair).getByText("not_measured")).toBeVisible();
    expect(within(repair).getByText("Provider record-time latency", { exact: true })).toBeVisible();
    expect(within(repair).getByText("Tokens / sample CI", { exact: true })).toBeVisible();
    expect(within(repair).getByText("Provider record-time latency CI", { exact: true })).toBeVisible();
    expect(within(cost).getByText(/不是 E2E 运行时，也不是 replay 播放耗时/)).toBeVisible();

    const deterministic = within(cost).getByRole("article", { name: "确定性运行时" });
    expect(within(deterministic).getByText("seeded-checker-sim-pipeline")).toBeVisible();
    expect(within(deterministic).getByText(/mean 6.457/)).toBeVisible();
    expect(within(deterministic).getByText("seeded-runtime@1")).toBeVisible();
  });

  it("lists version and evidence paths with hashes while keeping local paths non-clickable", async () => {
    renderPage(api());
    const evidence = await screen.findByRole("table", { name: "Evidence catalog" });
    const qaRow = within(evidence).getByRole("row", { name: /qa available qa-evidence@2/ });
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

    expect(await screen.findByText("X-Artifact-ID 缺失；未从报告内容猜测 Artifact 身份")).toBeVisible();
    expect(screen.queryByRole("link", { name: "打开 BenchReport Artifact" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "查看 BenchReport 血缘" })).not.toBeInTheDocument();
  });

  it("renders a safe Problem response and retries only after an explicit click", async () => {
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

    expect(await screen.findByRole("alert")).toHaveTextContent("dependency_unavailable");
    expect(getBenchReport).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "重试读取 BenchReport" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Eval / Bench" })).toBeVisible();
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
