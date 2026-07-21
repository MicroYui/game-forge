import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef, useState } from "react";
import { describe, expect, it, vi } from "vitest";

import type { SafeProblem } from "../../api/problem";
import { ToastProvider, useToast } from "../../app/providers";
import { messages } from "../../i18n/zh-CN";
import { ConfirmDialog } from "./ConfirmDialog";
import { ProblemPanel } from "./ProblemPanel";
import { StatePanel } from "./StatePanel";

const problem: SafeProblem = {
  code: "revision_conflict",
  conflict_set_id: "conflict:set",
  detail: "工作流修订已变化。",
  earliest_cursor: null,
  instance: "/api/v1/patches/patch:1:apply",
  request_id: "request:1",
  retry_after_s: null,
  run_id: "run:1",
  status: 409,
  title: "Revision conflict",
  trace_id: "trace:1",
  type: "https://gameforge.dev/problems/revision-conflict",
};

function ToastProbe() {
  const { pushToast } = useToast();
  return (
    <button onClick={() => pushToast({ message: "Patch 已保存为新修订。", tone: "success" })} type="button">
      保存
    </button>
  );
}

function ConfirmProbe({ onConfirm }: { onConfirm(): void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)} type="button">
        打开确认
      </button>
      <ConfirmDialog
        confirmLabel="确认回滚"
        description="ref 将重指已审批的历史工件。"
        onCancel={() => setOpen(false)}
        onConfirm={() => {
          onConfirm();
          setOpen(false);
        }}
        open={open}
        title="确认回滚？"
      />
    </>
  );
}

function DisabledConfirmProbe() {
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState(false);
  const fallbackRef = useRef<HTMLHeadingElement>(null);
  return (
    <>
      <h2 ref={fallbackRef} tabIndex={-1}>
        Apply status
      </h2>
      <button disabled={done} onClick={() => setOpen(true)} type="button">
        Apply
      </button>
      <ConfirmDialog
        confirmLabel="Confirm Apply"
        description="Apply the frozen target."
        onCancel={() => setOpen(false)}
        onConfirm={() => {
          setDone(true);
          setOpen(false);
        }}
        open={open}
        returnFocusRef={fallbackRef}
        title="Apply?"
      />
    </>
  );
}

describe("shared interaction primitives", () => {
  it.each([
    ["empty", messages.states.empty],
    ["loading", messages.states.loading],
    ["error", messages.states.error],
    ["streaming", messages.states.streaming],
    ["terminal", messages.states.terminal],
  ] as const)("labels the %s state with text and semantics", (state, label) => {
    render(<StatePanel description="状态详情" state={state} title={label} />);

    const element = screen.getByText(label).closest("section");
    expect(element).toHaveAttribute("data-state", state);
    expect(screen.getByText("状态详情")).toBeVisible();
  });

  it("lets a page-level state own the h1 without changing the default component level", () => {
    const { rerender } = render(
      <StatePanel description="正在确认会话" headingLevel={1} state="loading" title="正在加载" />,
    );

    expect(screen.getByRole("heading", { level: 1, name: "正在加载" })).toBeVisible();
    rerender(<StatePanel description="局部状态" state="empty" title="暂无内容" />);
    expect(screen.getByRole("heading", { level: 2, name: "暂无内容" })).toBeVisible();
  });

  it("renders only the safe RFC 9457 projection with correlation links", () => {
    render(<ProblemPanel problem={problem} />);

    expect(screen.getByRole("alert")).toHaveTextContent("工作流修订已变化。");
    expect(screen.getByText("revision_conflict")).toBeVisible();
    expect(screen.getByText("request:1")).toBeVisible();
    expect(screen.getByRole("link", { name: /run:1/ })).toHaveAttribute("href", "/runs/run%3A1");
    expect(screen.getByRole("link", { name: /trace:1/ })).toHaveAttribute(
      "href",
      "/observability/traces/trace%3A1",
    );
  });

  it("announces and dismisses a toast without relying on color", async () => {
    const user = userEvent.setup();
    render(
      <ToastProvider>
        <ToastProbe />
      </ToastProvider>,
    );

    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(screen.getByRole("status")).toHaveTextContent("Patch 已保存为新修订。");
    expect(screen.getByRole("status")).toHaveTextContent(messages.toast.success);
    expect(screen.getByRole("button", { name: messages.toast.dismiss })).toHaveAttribute(
      "data-tooltip",
      messages.toast.dismiss,
    );
    await user.click(screen.getByRole("button", { name: messages.toast.dismiss }));
    expect(screen.queryByText("Patch 已保存为新修订。")).not.toBeInTheDocument();
  });

  it("focuses a safe action, supports Escape, and returns focus", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<ConfirmProbe onConfirm={onConfirm} />);
    const trigger = screen.getByRole("button", { name: "打开确认" });

    await user.click(trigger);
    expect(screen.getByRole("dialog", { name: "确认回滚？" })).toBeVisible();
    expect(screen.getByRole("button", { name: messages.confirm.cancel })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "确认回滚" }));
    expect(onConfirm).toHaveBeenCalledOnce();
    expect(trigger).toHaveFocus();
  });

  it("keeps keyboard focus inside an open confirmation dialog", async () => {
    const user = userEvent.setup();
    render(<ConfirmProbe onConfirm={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "打开确认" }));
    const cancel = screen.getByRole("button", { name: messages.confirm.cancel });
    const confirm = screen.getByRole("button", { name: "确认回滚" });

    expect(cancel).toHaveFocus();
    await user.tab({ shift: true });
    expect(confirm).toHaveFocus();
    await user.tab();
    expect(cancel).toHaveFocus();
  });

  it("returns focus to a safe fallback when confirmation disables its trigger", async () => {
    const user = userEvent.setup();
    render(<DisabledConfirmProbe />);

    await user.click(screen.getByRole("button", { name: "Apply" }));
    await user.click(screen.getByRole("button", { name: "Confirm Apply" }));

    expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
    expect(screen.getByRole("heading", { name: "Apply status" })).toHaveFocus();
  });
});
