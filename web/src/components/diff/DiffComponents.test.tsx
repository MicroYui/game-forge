import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import { MergeResolver, SnapshotDiffView } from ".";

type MergeConflict = components["schemas"]["MergeConflict"];
type SnapshotDiff = components["schemas"]["SnapshotDiff"];
type SnapshotDiffEntry = components["schemas"]["SnapshotDiffEntry"];

const summary: SnapshotDiff = {
  base_snapshot_id: "snapshot:base",
  diff_schema_version: "snapshot-diff@1",
  entry_count: 1,
  target_snapshot_id: "snapshot:target",
};

const entries: SnapshotDiffEntry[] = [
  {
    after: { presence: "present", value: null },
    before: { presence: "missing" },
    path: "/entities/quest:bridge/attrs/reward",
  },
];

const conflict: MergeConflict = {
  allowed_resolutions: ["keep_current", "take_proposed", "custom"],
  base: { presence: "present", value: 80 },
  current: { presence: "missing" },
  id: "conflict:reward",
  kind: "value_changed",
  path: "/entities/quest:bridge/attrs/reward",
  proposed: { presence: "present", value: null },
};

describe("diff components", () => {
  it("keeps a missing value distinct from an explicit JSON null", () => {
    render(<SnapshotDiffView diff={summary} entries={entries} />);

    expect(screen.getByRole("heading", { name: "修改内容" })).toBeVisible();
    expect(screen.getByText("修改前版本")).toBeVisible();
    expect(screen.getByText("修改后版本")).toBeVisible();
    expect(screen.getByText("snapshot:base")).not.toBeVisible();
    expect(screen.getByText("snapshot:target")).not.toBeVisible();
    expect(screen.getByText("缺失（MISSING）")).toBeVisible();
    expect(screen.getByText("JSON null")).toBeVisible();
    expect(screen.getByText("任务 bridge / 奖励")).toBeVisible();
    expect(screen.getByText("/entities/quest:bridge/attrs/reward")).not.toBeVisible();
  });

  it("uses the planner-facing entity name for a whole-entity addition", () => {
    render(
      <SnapshotDiffView
        diff={summary}
        entries={[
          {
            after: {
              presence: "present",
              value: {
                attrs: { display_name: "风暴观测员", role: "记录天空港周边风暴" },
                schema_version: "ir-core@1",
                type: "NPC",
              },
            },
            before: { presence: "missing" },
            path: "/entities/npc:storm_observer",
          },
        ]}
      />,
    );

    expect(screen.getByRole("rowheader", { name: /角色 风暴观测员/u })).toBeVisible();
    expect(screen.queryByText("角色 storm_observer")).not.toBeInTheDocument();
    expect(screen.getByText("/entities/npc:storm_observer")).not.toBeVisible();
  });

  it("shows base/current/proposed and starts with no guessed resolution", async () => {
    const user = userEvent.setup();
    const onResolutionsChange = vi.fn();
    render(<MergeResolver conflicts={[conflict]} onResolutionsChange={onResolutionsChange} />);

    expect(screen.getByRole("heading", { name: "逐项处理内容冲突" })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "草案创建时" })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "当前正式内容" })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "草案建议" })).toBeVisible();
    expect(screen.getByText("/entities/quest:bridge/attrs/reward")).not.toBeVisible();
    const choices = screen.getAllByRole("radio");
    expect(choices).toHaveLength(3);
    expect(choices.every((choice) => !choice.hasAttribute("checked"))).toBe(true);

    await user.click(screen.getByRole("radio", { name: "采用这份草案的修改" }));
    expect(onResolutionsChange).toHaveBeenLastCalledWith([
      { choice: "take_proposed", conflict_id: "conflict:reward" },
    ]);
  });

  it("emits a custom resolution only after valid explicit JSON is supplied", async () => {
    const user = userEvent.setup();
    const onResolutionsChange = vi.fn();
    render(<MergeResolver conflicts={[conflict]} onResolutionsChange={onResolutionsChange} />);

    await user.click(screen.getByRole("radio", { name: "自己填写一个值（高级）" }));
    expect(onResolutionsChange).toHaveBeenLastCalledWith([]);

    await user.type(screen.getByRole("textbox", { name: "自定义内容值（JSON，高级）" }), "not-json");
    expect(screen.getByRole("alert")).toHaveTextContent("请输入有效 JSON");
    expect(onResolutionsChange).toHaveBeenLastCalledWith([]);

    await user.clear(screen.getByRole("textbox", { name: "自定义内容值（JSON，高级）" }));
    fireEvent.change(screen.getByRole("textbox", { name: "自定义内容值（JSON，高级）" }), {
      target: { value: '{"gold":96}' },
    });
    expect(onResolutionsChange).toHaveBeenLastCalledWith([
      {
        choice: "custom",
        conflict_id: "conflict:reward",
        custom_value: { gold: 96 },
      },
    ]);
  });
});
