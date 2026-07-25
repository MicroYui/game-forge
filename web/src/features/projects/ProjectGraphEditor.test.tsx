import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { ProjectGraphEditor } from "./ProjectGraphEditor";
import type { ProjectGraphDraft } from "./model";

const cytoscapeMock = vi.hoisted(() => {
  const collection = {
    addClass: vi.fn(),
    empty: () => false,
    removeClass: vi.fn(),
    select: vi.fn(),
    unselect: vi.fn(),
  };
  const cy = {
    $id: vi.fn(() => collection),
    destroy: vi.fn(),
    elements: vi.fn(() => collection),
    fit: vi.fn(),
    off: vi.fn(),
    on: vi.fn(),
  };
  return { factory: vi.fn(() => cy) };
});

vi.mock("cytoscape", () => ({ default: cytoscapeMock.factory }));

const initial: ProjectGraphDraft = {
  entities: [
    {
      attrs: { display_name: "守空人" },
      id: "npc:sky-keeper",
      schema_version: "ir-core@1",
      tags: [],
      type: "NPC",
    },
    {
      attrs: { display_name: "天空港" },
      id: "region:sky-harbor",
      schema_version: "ir-core@1",
      tags: [],
      type: "REGION",
    },
  ],
  relations: [],
};

function ControlledEditor() {
  const [draft, setDraft] = useState(initial);
  return <ProjectGraphEditor onChange={setDraft} value={draft} />;
}

const limitedEvent: ProjectGraphDraft = {
  entities: [
    {
      attrs: {
        availability: {
          duration_days: 14,
          reward_claim_grace_days: 3,
          schedule_kind: "relative",
        },
        display_name: "梦中未寄出的信",
        scope_kind: "event",
        scope_role: "owner",
      },
      id: "event:dream-letters",
      schema_version: "ir-core@1",
      tags: [],
      type: "EVENT",
    },
  ],
  relations: [],
};

function ControlledLimitedEventEditor() {
  const [draft, setDraft] = useState(limitedEvent);
  return <ProjectGraphEditor onChange={setDraft} value={draft} />;
}

describe("ProjectGraphEditor", () => {
  it("supports novice entity, relation, attribute, delete, and undo paths", async () => {
    const user = userEvent.setup();
    render(<ControlledEditor />);

    await user.click(screen.getByRole("button", { name: "添加实体" }));
    expect(screen.getByLabelText("内容名称")).toHaveValue("新角色");
    await user.clear(screen.getByLabelText("内容名称"));
    await user.type(screen.getByLabelText("内容名称"), "天气管理员");

    await user.type(screen.getByLabelText("新属性名称"), "Air.Quality");
    expect(screen.getByText("air_quality")).toBeVisible();
    await user.type(screen.getByLabelText("新属性值"), "Clean");
    await user.click(screen.getByRole("button", { name: "添加属性" }));
    expect(screen.getByText("air_quality")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "添加关系" }));
    expect(screen.getByLabelText("关系类型")).toBeVisible();
    expect(screen.getByLabelText("起点内容")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "撤销上一步" }));
    expect(screen.queryByLabelText("关系类型")).not.toBeInTheDocument();
  });

  it("reports every controlled change to the project workspace", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<ProjectGraphEditor onChange={onChange} value={initial} />);

    await user.click(screen.getByRole("button", { name: "添加实体" }));

    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        entities: expect.arrayContaining([expect.objectContaining({ type: "NPC" })]),
      }),
    );
  });

  it("lets a planner bind a relative event duration to an absolute launch window without JSON", async () => {
    const user = userEvent.setup();
    render(<ControlledLimitedEventEditor />);

    expect(screen.getByText("材料写明：活动持续 14 天，结束后可领奖 3 天。")).toBeVisible();
    fireEvent.change(screen.getByLabelText("活动开始时间"), {
      target: { value: "2026-08-01T10:00" },
    });
    expect(screen.getByLabelText("玩法结束时间")).toHaveValue("2026-08-15T10:00");
    expect(screen.getByLabelText("奖励兑换截止")).toHaveValue("2026-08-18T10:00");
    await user.click(screen.getByRole("button", { name: "保存活动档期" }));

    expect(screen.getByText("活动档期已保存；到期后将从当前内容中隐藏。")).toBeVisible();
    expect((screen.getByLabelText("属性 JSON") as HTMLTextAreaElement).value).toContain(
      '"start_at": "2026-08-01T10:00:00+08:00"',
    );
  });
});
