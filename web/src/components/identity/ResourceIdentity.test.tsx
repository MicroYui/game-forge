import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ResourceIdentity, TechnicalDetails, compactDateTime } from "./ResourceIdentity";

describe("ResourceIdentity", () => {
  it("leads with a business label and keeps the exact identifier behind disclosure", async () => {
    const user = userEvent.setup();
    const identifier = `sha256:${"a".repeat(64)}`;

    render(
      <ResourceIdentity
        actionLabel="查看报告"
        details={[{ copyLabel: "复制报告标识", label: "报告标识", value: identifier }]}
        description="7 月 24 日 10:30 · 发现 2 个问题"
        href="/reviews/report"
        title="内容检查报告"
      />,
    );

    expect(screen.getByText("内容检查报告")).toBeVisible();
    expect(screen.getByText("7 月 24 日 10:30 · 发现 2 个问题")).toBeVisible();
    expect(screen.getByRole("link", { name: "查看报告" })).toHaveAttribute("href", "/reviews/report");
    expect(screen.getByText(identifier)).not.toBeVisible();

    await user.click(screen.getByText("技术信息"));

    expect(screen.getByText(identifier)).toBeVisible();
    expect(screen.getByRole("button", { name: "复制报告标识" })).toBeVisible();
  });

  it("supports multiple exact values without promoting them to primary content", async () => {
    const user = userEvent.setup();

    render(
      <TechnicalDetails
        items={[
          { label: "目录快照", value: "read-snapshot:immutable:123" },
          { label: "工具版本", value: "review@1" },
        ]}
        summary="目录技术信息"
      />,
    );

    const disclosure = screen.getByText("目录技术信息").closest("details")!;
    expect(within(disclosure).getByText("read-snapshot:immutable:123")).not.toBeVisible();

    await user.click(screen.getByText("目录技术信息"));

    expect(within(disclosure).getByText("read-snapshot:immutable:123")).toBeVisible();
    expect(within(disclosure).getByText("review@1")).toBeVisible();
  });
});

describe("compactDateTime", () => {
  it("renders a UTC instant in the product's China Standard Time", () => {
    expect(compactDateTime("2026-07-26T04:56:00Z")).toBe("2026年7月26日 12:56");
  });

  it("shifts the calendar day when the UTC instant belongs to the previous day", () => {
    expect(compactDateTime("2026-07-25T20:30:00Z")).toBe("2026年7月26日 04:30");
  });

  it("keeps an offset-bearing instant on the same wall clock", () => {
    expect(compactDateTime("2026-07-26T12:56:00+08:00")).toBe("2026年7月26日 12:56");
  });

  it("reports a missing or unreadable timestamp instead of echoing the wire value", () => {
    expect(compactDateTime(null)).toBe("时间未知");
    expect(compactDateTime("not-a-timestamp")).toBe("时间未知");
  });
});
