import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ConstraintRefBindingFields, type ConstraintRefSelection } from "./ConstraintRefBindingFields";
import type { SpecWorkflowApi } from "./api";

function page(revision: number, nextCursor: string | null, readSnapshotId = "read:ref") {
  return {
    expires_at: "2026-07-23T12:00:00Z",
    items: [
      {
        entry_schema_version: "ref-history-entry@1" as const,
        ref_name: "constraints/head",
        value: { artifact_id: `artifact:constraint:${revision}`, revision },
      },
    ],
    next_cursor: nextCursor,
    page_schema_version: "page@1" as const,
    read_snapshot_id: readSnapshotId,
  };
}

function renderFields(api: Pick<SpecWorkflowApi, "listRefHistory">) {
  let value: ConstraintRefSelection | null = null;
  const onChange = vi.fn((next: ConstraintRefSelection | null) => {
    value = next;
    view.rerender(<ConstraintRefBindingFields api={api} name="target" onChange={onChange} value={value} />);
  });
  const view = render(
    <ConstraintRefBindingFields api={api} name="target" onChange={onChange} value={value} />,
  );
  return { onChange };
}

describe("ConstraintRefBindingFields", () => {
  it("creates a new ref with an explicit null expected value and no raw ID field", async () => {
    const user = userEvent.setup();
    const api = { listRefHistory: vi.fn() };
    const { onChange } = renderFields(api as Pick<SpecWorkflowApi, "listRefHistory">);

    await user.click(screen.getByRole("radio", { name: "创建新 ref" }));
    await user.type(screen.getByRole("textbox", { name: "Ref 名称" }), "constraints/head");

    expect(onChange).toHaveBeenLastCalledWith({ expectedRef: null, refName: "constraints/head" });
    expect(screen.queryByRole("textbox", { name: "Artifact ID" })).not.toBeInTheDocument();
  });

  it("reads every history page and selects the latest exact pair without copy-paste", async () => {
    const user = userEvent.setup();
    const listRefHistory = vi
      .fn<SpecWorkflowApi["listRefHistory"]>()
      .mockResolvedValueOnce(page(1, "cursor:2"))
      .mockResolvedValueOnce(page(2, null));
    const { onChange } = renderFields({ listRefHistory });

    await user.click(screen.getByRole("radio", { name: "更新已有 ref" }));
    await user.type(screen.getByRole("textbox", { name: "Ref 名称" }), "constraints/head");
    await user.click(screen.getByRole("button", { name: "查找当前版本" }));

    expect(await screen.findByRole("status")).toHaveTextContent("已选择当前 revision 2");
    expect(onChange).toHaveBeenLastCalledWith({
      expectedRef: { artifact_id: "artifact:constraint:2", revision: 2 },
      refName: "constraints/head",
    });
    expect(listRefHistory).toHaveBeenNthCalledWith(1, "constraints/head", null);
    expect(listRefHistory).toHaveBeenNthCalledWith(2, "constraints/head", "cursor:2");
  });

  it("fails closed when paginated history changes snapshot", async () => {
    const user = userEvent.setup();
    const listRefHistory = vi
      .fn<SpecWorkflowApi["listRefHistory"]>()
      .mockResolvedValueOnce(page(1, "cursor:2", "read:one"))
      .mockResolvedValueOnce(page(2, null, "read:two"));
    const { onChange } = renderFields({ listRefHistory });

    await user.click(screen.getByRole("radio", { name: "更新已有 ref" }));
    await user.type(screen.getByRole("textbox", { name: "Ref 名称" }), "constraints/head");
    await user.click(screen.getByRole("button", { name: "查找当前版本" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("无法读取完整 ref 历史");
    expect(onChange).toHaveBeenLastCalledWith(null);
  });
});
