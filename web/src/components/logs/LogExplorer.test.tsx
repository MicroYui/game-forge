import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import { LogExplorer } from "./LogExplorer";

type LogRecordView = components["schemas"]["LogRecordViewV1"];

const LONG_ID = `trace:${"甲".repeat(512)}`;

function log(overrides: Partial<LogRecordView["record"]> = {}): LogRecordView {
  return {
    record: {
      event_name: "patch.validation.completed",
      fields: {
        attempt: 2,
        constraint_snapshot: "constraint:snapshot:7",
        raw_prompt: "绝不能出现的 prompt",
        raw_response: "绝不能出现的 raw response",
        client_secret: "绝不能出现的 secret",
        redacted_fields: ["raw_prompt", "raw_response", "client_secret"],
        secret_material: "即使服务端遗漏投影也不能出现",
      },
      level: "info",
      log_id: "log:1",
      log_schema_version: "log-record@1",
      message: "补丁验证完成：所有确定性检查均已收敛。",
      run_id: "run:1",
      service: "gameforge.worker",
      span_id: "span:1",
      trace_id: LONG_ID,
      ts_utc: "2026-07-19T12:00:00Z",
      ...overrides,
    },
    redacted_fields: ["raw_prompt", "raw_response", "client_secret"],
  };
}

describe("LogExplorer", () => {
  it("renders only safe M4c log fields, redaction evidence, and a trace link", () => {
    render(<LogExplorer items={[log()]} />);

    expect(screen.getByRole("heading", { name: "运行日志" })).toBeVisible();
    expect(screen.getByText("补丁验证完成：所有确定性检查均已收敛。")).toBeVisible();
    expect(screen.getByText("constraint_snapshot")).toBeVisible();
    expect(screen.getByText("constraint:snapshot:7")).toBeVisible();
    const redactionNotice = screen.getByText("4 个字段已脱敏");
    expect(redactionNotice).toBeVisible();
    expect(redactionNotice).not.toHaveAttribute("role");
    expect(screen.getByRole("link", { name: `查看追踪 ${LONG_ID}` })).toHaveAttribute(
      "href",
      `/observability/traces/${encodeURIComponent(LONG_ID)}`,
    );
    expect(screen.getByRole("button", { name: "复制追踪 ID" })).toHaveAttribute(
      "data-tooltip",
      "复制追踪 ID",
    );

    expect(screen.queryByText("raw_prompt")).not.toBeInTheDocument();
    expect(screen.queryByText("raw_response")).not.toBeInTheDocument();
    expect(screen.queryByText("client_secret")).not.toBeInTheDocument();
    expect(screen.queryByText("secret_material")).not.toBeInTheDocument();
    expect(screen.queryByText("绝不能出现的 prompt")).not.toBeInTheDocument();
    expect(screen.queryByText("绝不能出现的 raw response")).not.toBeInTheDocument();
    expect(screen.queryByText("绝不能出现的 secret")).not.toBeInTheDocument();
    expect(screen.queryByText("即使服务端遗漏投影也不能出现")).not.toBeInTheDocument();
  });

  it("keeps long Chinese and identifiers copyable without copying redacted values", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText");
    render(<LogExplorer items={[log()]} />);

    expect(screen.getByText(LONG_ID)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "复制追踪 ID" }));
    expect(writeText).toHaveBeenCalledWith(LONG_ID);

    await user.click(screen.getByRole("button", { name: "复制安全日志" }));
    await waitFor(() => expect(screen.getByText("已复制安全日志")).toBeInTheDocument());
    const lastCall = writeText.mock.calls[writeText.mock.calls.length - 1];
    const copiedLog = lastCall?.[0] ?? "";
    expect(copiedLog).toContain("补丁验证完成：所有确定性检查均已收敛。");
    expect(copiedLog).toContain("constraint:snapshot:7");
    expect(copiedLog).not.toContain("raw_prompt");
    expect(copiedLog).not.toContain("绝不能出现");
  });

  it("hides fields named by the server redaction projection even when their key is benign", () => {
    render(
      <LogExplorer
        items={[
          {
            ...log({ fields: { attempt: 2, upstream_payload: "[REDACTED]" } }),
            redacted_fields: ["upstream_payload"],
          },
        ]}
      />,
    );

    expect(screen.getByText("1 个字段已脱敏")).toBeVisible();
    expect(screen.queryByText("upstream_payload")).not.toBeInTheDocument();
    expect(screen.queryByText("[REDACTED]")).not.toBeInTheDocument();
    expect(screen.getByText("attempt")).toBeVisible();
  });

  it("redacts camelCase, snake_case, kebab-case, and dotted sensitive keys from rendering and copying", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText");
    const sensitiveFields = {
      accessToken: "camel-access-token-value",
      api_key: "snake-api-key-value",
      "client-secret": "kebab-client-secret-value",
      "raw.response": "dotted-raw-response-value",
      rawResponse: "camel-raw-response-value",
      safe_field: "safe-visible-value",
    };

    render(<LogExplorer items={[log({ fields: sensitiveFields })]} />);

    expect(screen.getByText("safe_field")).toBeVisible();
    expect(screen.getByText("safe-visible-value")).toBeVisible();
    for (const [key, value] of Object.entries(sensitiveFields).filter(([key]) => key !== "safe_field")) {
      expect(screen.queryByText(key)).not.toBeInTheDocument();
      expect(screen.queryByText(value)).not.toBeInTheDocument();
    }

    await user.click(screen.getByRole("button", { name: "复制安全日志" }));
    const copiedLog = writeText.mock.calls[writeText.mock.calls.length - 1]?.[0] ?? "";
    expect(copiedLog).toContain("safe_field=safe-visible-value");
    for (const [key, value] of Object.entries(sensitiveFields).filter(([key]) => key !== "safe_field")) {
      expect(copiedLog).not.toContain(key);
      expect(copiedLog).not.toContain(value);
    }
  });

  it("redacts nested prompt, raw payload, debug, and handler configuration fields", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText");
    render(
      <LogExplorer
        items={[
          log({
            fields: {
              envelope: {
                debug: "nested-debug-value",
                handlerConfig: "nested-handler-value",
                nested: [{ prompt: "nested-prompt-value", safe: "nested-safe-value" }],
                raw_payload: "nested-payload-value",
              },
            },
          }),
        ]}
      />,
    );

    expect(screen.getByText("nested-safe-value", { exact: false })).toBeVisible();
    for (const value of [
      "nested-debug-value",
      "nested-handler-value",
      "nested-prompt-value",
      "nested-payload-value",
    ]) {
      expect(screen.queryByText(value, { exact: false })).not.toBeInTheDocument();
    }

    await user.click(screen.getByRole("button", { name: "复制安全日志" }));
    const copiedLog = writeText.mock.calls[writeText.mock.calls.length - 1]?.[0] ?? "";
    expect(copiedLog).toContain("nested-safe-value");
    expect(copiedLog).not.toContain("nested-debug-value");
    expect(copiedLog).not.toContain("nested-handler-value");
    expect(copiedLog).not.toContain("nested-prompt-value");
    expect(copiedLog).not.toContain("nested-payload-value");
  });
});
