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

    expect(screen.getByText("缺失（MISSING）")).toBeVisible();
    expect(screen.getByText("JSON null")).toBeVisible();
    expect(screen.getByText("/entities/quest:bridge/attrs/reward")).toBeVisible();
  });

  it("shows base/current/proposed and starts with no guessed resolution", async () => {
    const user = userEvent.setup();
    const onResolutionsChange = vi.fn();
    render(<MergeResolver conflicts={[conflict]} onResolutionsChange={onResolutionsChange} />);

    expect(screen.getByRole("columnheader", { name: "Base" })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "Current" })).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "Proposed" })).toBeVisible();
    const choices = screen.getAllByRole("radio");
    expect(choices).toHaveLength(3);
    expect(choices.every((choice) => !choice.hasAttribute("checked"))).toBe(true);

    await user.click(screen.getByRole("radio", { name: "采用 Proposed" }));
    expect(onResolutionsChange).toHaveBeenLastCalledWith([
      { choice: "take_proposed", conflict_id: "conflict:reward" },
    ]);
  });

  it("emits a custom resolution only after valid explicit JSON is supplied", async () => {
    const user = userEvent.setup();
    const onResolutionsChange = vi.fn();
    render(<MergeResolver conflicts={[conflict]} onResolutionsChange={onResolutionsChange} />);

    await user.click(screen.getByRole("radio", { name: "自定义 JSON" }));
    expect(onResolutionsChange).toHaveBeenLastCalledWith([]);

    await user.type(screen.getByRole("textbox", { name: "conflict:reward 的自定义 JSON" }), "not-json");
    expect(screen.getByRole("alert")).toHaveTextContent("请输入有效 JSON");
    expect(onResolutionsChange).toHaveBeenLastCalledWith([]);

    await user.clear(screen.getByRole("textbox", { name: "conflict:reward 的自定义 JSON" }));
    fireEvent.change(screen.getByRole("textbox", { name: "conflict:reward 的自定义 JSON" }), {
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
