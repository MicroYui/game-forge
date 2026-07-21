import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AreaSparkChart, CostBarChart, HorizontalBarChart, RingChart, TraceWaterfall } from "./index";

class ImmediateResizeObserver implements ResizeObserver {
  constructor(private readonly callback: ResizeObserverCallback) {}

  disconnect() {}

  observe(target: Element) {
    const contentRect = target.getBoundingClientRect();
    this.callback(
      [
        {
          borderBoxSize: [],
          contentBoxSize: [],
          contentRect,
          devicePixelContentBoxSize: [],
          target,
        },
      ],
      this,
    );
  }

  unobserve() {}
}

function installChartLayout() {
  vi.stubGlobal("ResizeObserver", ImmediateResizeObserver);
  return vi.spyOn(Element.prototype, "getBoundingClientRect").mockReturnValue({
    bottom: 240,
    height: 240,
    left: 0,
    right: 640,
    toJSON: () => ({}),
    top: 0,
    width: 640,
    x: 0,
    y: 0,
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("ChartKit", () => {
  it("renders an area spark with a visible summary and an exact table alternative", () => {
    render(
      <AreaSparkChart
        data={[
          { label: "周一", value: 0.72 },
          { label: "周二", value: 0.81 },
          { label: "周三", value: 0.9 },
        ]}
        labelLabel="日期"
        summary="修复通过率连续三日上升，当前为 90%。"
        title="修复通过率"
        valueLabel="通过率"
        valueFormatter={(value) => `${Math.round(value * 100)}%`}
      />,
    );

    expect(screen.getByRole("figure", { name: "修复通过率" })).toBeVisible();
    expect(screen.getByText("修复通过率连续三日上升，当前为 90%。")).toBeVisible();
    expect(screen.getByText("查看修复通过率数据表")).toBeVisible();

    const table = screen.getByRole("table", { hidden: true });
    expect(within(table).getByRole("columnheader", { name: "日期" })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: "通过率" })).toBeInTheDocument();
    expect(within(table).getByText("90%")).toBeInTheDocument();
  });

  it("labels every ring and horizontal-bar category without relying on color", () => {
    const { rerender } = render(
      <RingChart
        data={[
          { label: "结构缺陷", value: 8 },
          { label: "经济缺陷", value: 3 },
        ]}
        summary="共检出 11 项，其中结构缺陷 8 项。"
        title="缺陷构成"
        valueLabel="数量"
      />,
    );

    expect(
      screen.getAllByText("结构缺陷").some((node) => node.classList.contains("gf-chart__legend-label")),
    ).toBe(true);
    expect(screen.getAllByText("8").some((node) => node.tagName === "STRONG")).toBe(true);
    expect(
      screen.getAllByText("经济缺陷").some((node) => node.classList.contains("gf-chart__legend-label")),
    ).toBe(true);
    expect(screen.getAllByText("3").some((node) => node.tagName === "STRONG")).toBe(true);

    rerender(
      <HorizontalBarChart
        data={[
          { label: "悬挂引用", value: 8 },
          { label: "经济崩坏", value: 3 },
        ]}
        summary="悬挂引用是当前数量最高的缺陷类型。"
        title="按缺陷类型"
        valueLabel="发现数"
      />,
    );

    expect(screen.getByText("悬挂引用是当前数量最高的缺陷类型。")).toBeVisible();
    expect(screen.getByRole("table", { hidden: true })).toHaveTextContent("经济崩坏");
  });

  it("keeps all four decorative Recharts roots out of the keyboard and accessibility trees", async () => {
    installChartLayout();
    render(
      <>
        <AreaSparkChart
          data={[{ label: "周一", value: 0.9 }]}
          summary="当前为 90%。"
          title="面积图"
          valueLabel="通过率"
        />
        <RingChart
          data={[{ label: "结构缺陷", value: 8 }]}
          summary="结构缺陷 8 项。"
          title="环图"
          valueLabel="数量"
        />
        <HorizontalBarChart
          data={[{ label: "悬挂引用", value: 8 }]}
          summary="悬挂引用 8 项。"
          title="横向条形图"
          valueLabel="发现数"
        />
        <CostBarChart
          data={[
            {
              consumed: { exact: "45", plot: 45 },
              label: "输出 token",
              limit: { exact: "100", plot: 100 },
              reserved: { exact: "15", plot: 15 },
              unit: "token",
            },
          ]}
          summary="输出 token 在预算内。"
          title="成本条形图"
        />
      </>,
    );

    const titles = ["面积图", "环图", "横向条形图", "成本条形图"] as const;
    await waitFor(() => {
      for (const title of titles) {
        const figure = screen.getByRole("figure", { name: title });
        expect(figure.querySelector(".gf-chart__plot svg"), `${title} must render its chart`).not.toBeNull();
      }
    });

    const focusableSelector = 'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])';
    const focusableCounts = Object.fromEntries(
      titles.map((title) => {
        const figure = screen.getByRole("figure", { name: title });
        const decorativePlot = figure.querySelector('.gf-chart__plot[aria-hidden="true"]');
        expect(decorativePlot, `${title} must keep its plot decorative`).not.toBeNull();
        return [title, decorativePlot!.querySelectorAll(focusableSelector).length];
      }),
    );

    expect(focusableCounts).toEqual({ 面积图: 0, 环图: 0, 横向条形图: 0, 成本条形图: 0 });
  });

  it("renders trace timing and status text in a semantic waterfall", () => {
    render(
      <TraceWaterfall
        spans={[
          { durationMs: 120, id: "span:root", name: "验证补丁", startMs: 0, status: "ok" },
          {
            durationMs: 42,
            id: "span:checker",
            name: "确定性检查器",
            parentId: "span:root",
            startMs: 18,
            status: "error",
          },
        ]}
        summary="验证补丁耗时 120 毫秒；确定性检查器返回错误。"
        title="执行瀑布"
      />,
    );

    const figure = screen.getByRole("figure", { name: "执行瀑布" });
    expect(within(figure).getByText("验证补丁耗时 120 毫秒；确定性检查器返回错误。")).toBeVisible();
    expect(
      within(figure)
        .getAllByText("成功")
        .some((node) => node.classList.contains("gf-waterfall__status")),
    ).toBe(true);
    expect(
      within(figure)
        .getAllByText("错误")
        .some((node) => node.classList.contains("gf-waterfall__status")),
    ).toBe(true);
    expect(within(figure).getByText("18 ms → 60 ms")).toBeVisible();

    const scrollContainer = figure.querySelector(".gf-chart__plot--waterfall");
    expect(scrollContainer).toHaveAttribute("tabindex", "0");
    expect(scrollContainer).toHaveAccessibleName("执行瀑布时间轴");
  });

  it("renders cost segments, exact values, and explicit budget status", () => {
    render(
      <CostBarChart
        data={[
          {
            consumed: { exact: "45", plot: 45 },
            label: "输出 token",
            limit: { exact: "100", plot: 100 },
            reserved: { exact: "15", plot: 15 },
            unit: "token",
          },
          {
            consumed: { exact: "120", plot: 120 },
            label: "Agent 步数",
            limit: { exact: "100", plot: 100 },
            reserved: { exact: "0", plot: 0 },
            unit: "step",
          },
        ]}
        summary="输出 token 仍在预算内；Agent 步数已超出预算。"
        title="成本预算"
      />,
    );

    expect(screen.getByText("输出 token 仍在预算内；Agent 步数已超出预算。")).toBeVisible();
    expect(screen.getAllByText("预算内").some((node) => node.tagName === "STRONG")).toBe(true);
    expect(screen.getAllByText("超出预算").some((node) => node.tagName === "STRONG")).toBe(true);
    expect(screen.getByRole("table", { hidden: true })).toHaveTextContent("45 token");
    expect(screen.getByRole("table", { hidden: true })).toHaveTextContent("120 step");
  });

  it("presents an exact zero budget without dividing the chart scale by zero", () => {
    render(
      <CostBarChart
        data={[
          {
            consumed: { exact: "0", plot: 0 },
            label: "请求数",
            limit: { exact: "0", plot: 0 },
            reserved: { exact: "0", plot: 0 },
            unit: "request",
          },
        ]}
        summary="请求预算为 0。"
        title="零预算"
      />,
    );

    expect(screen.getAllByText("预算为 0").some((node) => node.tagName === "STRONG")).toBe(true);
    expect(screen.getByRole("table", { hidden: true })).toHaveTextContent("0 request");
  });

  it("preserves wire decimal strings beyond Number precision while plotting approximate values separately", () => {
    render(
      <CostBarChart
        data={[
          {
            consumed: { exact: "9007199254740993", plot: 9007199254740992 },
            label: "高精度成本",
            limit: {
              exact: "9007199254740993.00000000000000000000000000000000000002",
              plot: 9007199254740992,
            },
            reserved: {
              exact: "0.00000000000000000000000000000000000001",
              plot: 1e-38,
            },
            unit: "token",
          },
        ]}
        summary="精确字符串用于表格与状态；图形仅使用近似值。"
        title="精确成本"
      />,
    );

    const table = screen.getByRole("table", { hidden: true });
    expect(table).toHaveTextContent("9007199254740993 token");
    expect(table).toHaveTextContent("0.00000000000000000000000000000000000001 token");
    expect(table).toHaveTextContent("9007199254740993.00000000000000000000000000000000000002 token");
    expect(screen.getAllByText("预算内").some((node) => node.tagName === "STRONG")).toBe(true);
  });

  it("shows unavailable cost observations explicitly instead of substituting zero", () => {
    render(
      <CostBarChart
        data={[
          {
            consumed: { exact: null, plot: null },
            label: "上游未报告",
            limit: { exact: "10", plot: 10 },
            reserved: { exact: "0", plot: 0 },
            unit: "request",
          },
        ]}
        summary="已使用量不可用。"
        title="未报告成本"
      />,
    );

    expect(screen.getByRole("table", { hidden: true })).toHaveTextContent("不可用");
    expect(screen.getAllByText("数据不可用").some((node) => node.tagName === "STRONG")).toBe(true);
    expect(screen.queryByText("预算为 0")).not.toBeInTheDocument();
  });
});
