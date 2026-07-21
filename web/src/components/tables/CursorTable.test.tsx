import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CopyableText, CursorTable } from ".";

const longId = `artifact:${"a".repeat(512)}`;

describe("CursorTable", () => {
  it("preserves item order and passes an opaque next cursor verbatim", async () => {
    const user = userEvent.setup();
    const onLoadMore = vi.fn();
    const cursor = "opaque.v1/不要解码/+==";

    render(
      <CursorTable
        caption="工件"
        columns={[{ id: "id", header: "ID", render: (item: { id: string }) => item.id }]}
        getRowKey={(item) => item.id}
        items={[{ id: "artifact:z" }, { id: "artifact:a" }]}
        nextCursor={cursor}
        onLoadMore={onLoadMore}
      />,
    );

    const rows = screen.getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("artifact:z")).toBeVisible();
    expect(within(rows[1]).getByText("artifact:a")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "加载下一页" }));
    expect(onLoadMore).toHaveBeenCalledWith(cursor);
  });

  it("requires an explicit restart after cursor expiry", async () => {
    const user = userEvent.setup();
    const onRestart = vi.fn();
    render(
      <CursorTable
        caption="冲突"
        columns={[{ id: "id", header: "ID", render: (item: { id: string }) => item.id }]}
        getRowKey={(item) => item.id}
        items={[]}
        nextCursor="stale-cursor"
        onLoadMore={vi.fn()}
        onRestart={onRestart}
        paginationState="expired"
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("分页游标已过期");
    expect(screen.queryByRole("button", { name: "加载下一页" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新开始查询" }));
    expect(onRestart).toHaveBeenCalledOnce();
  });

  it("supports a page-level h2 without changing the visible table-heading scale or announcing initial end", () => {
    render(
      <CursorTable
        caption="已授权 Run"
        columns={[{ id: "id", header: "ID", render: (item: { id: string }) => item.id }]}
        getRowKey={(item) => item.id}
        headingLevel={2}
        items={[{ id: "run:1" }]}
      />,
    );

    expect(screen.getByRole("heading", { level: 2, name: "已授权 Run" })).toHaveClass(
      "gf-cursor-table__heading",
    );
    expect(screen.getByText("已到末页")).not.toHaveAttribute("role");
  });

  it("retries an ordinary page failure with the same opaque cursor", async () => {
    const user = userEvent.setup();
    const onLoadMore = vi.fn();
    const onRestart = vi.fn();
    const cursor = "opaque.retry/原样/+==";
    render(
      <CursorTable
        caption="工件"
        columns={[{ id: "id", header: "ID", render: (item: { id: string }) => item.id }]}
        getRowKey={(item) => item.id}
        items={[{ id: "artifact:1" }]}
        nextCursor={cursor}
        onLoadMore={onLoadMore}
        onRestart={onRestart}
        paginationState="error"
      />,
    );

    await user.click(screen.getByRole("button", { name: "重试下一页" }));

    expect(onLoadMore).toHaveBeenCalledWith(cursor);
    expect(onRestart).not.toHaveBeenCalled();
  });

  it("wraps and copies a 512-character identifier without making the toolbar flexible", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(<CopyableText copyLabel="复制工件 ID" value={longId} />);

    expect(screen.getByText(longId)).toHaveClass("gf-copyable__value");
    expect(screen.getByText(longId).parentElement).toHaveClass("gf-copyable");
    await user.click(screen.getByRole("button", { name: "复制工件 ID" }));
    expect(writeText).toHaveBeenCalledWith(longId);
  });
});
