import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { components } from "../../api/generated/openapi";
import { KnowledgeGraph } from "./KnowledgeGraph";

type GraphItem = components["schemas"]["GraphItemV1"];

const cytoscapeMock = vi.hoisted(() => {
  const destroy = vi.fn();
  const select = vi.fn();
  const unselect = vi.fn();
  const fit = vi.fn();
  const removeClass = vi.fn();
  const addClass = vi.fn();
  const on = vi.fn();
  const off = vi.fn();
  const idCollection = {
    addClass,
    empty: () => false,
    select,
    unselect,
  };
  const elementsCollection = {
    removeClass,
    unselect,
  };
  const cy = {
    $id: vi.fn(() => idCollection),
    destroy,
    elements: vi.fn(() => elementsCollection),
    fit,
    off,
    on,
  };
  const factory = vi.fn((_options: unknown) => cy);

  return {
    addClass,
    cy,
    destroy,
    factory,
    fit,
    idCollection,
    off,
    on,
    removeClass,
    select,
    unselect,
  };
});

vi.mock("cytoscape", () => ({ default: cytoscapeMock.factory }));

const longQuestId = `quest:${"失落车队".repeat(24)}:final`;

const graphItems: GraphItem[] = [
  {
    item_schema_version: "graph-item@1",
    item_kind: "entity",
    item_id: longQuestId,
    entity: {
      id: longQuestId,
      type: "QUEST",
      attrs: { name: "失落车队", reward_gold: 80 },
      tags: ["主线", "newbie-zone"],
      schema_version: "ir-core@1",
      source_ref: {
        adapter: "aureus-csv@1",
        file: "quests.csv",
        sheet: "Quest",
        row: 17,
        column: "quest_id",
      },
    },
  },
  {
    item_schema_version: "graph-item@1",
    item_kind: "entity",
    item_id: "step:talk-to-lincheng",
    entity: {
      id: "step:talk-to-lincheng",
      type: "QUEST_STEP",
      attrs: { order: 1 },
      schema_version: "ir-core@1",
    },
  },
  {
    item_schema_version: "graph-item@1",
    item_kind: "relation",
    item_id: "relation:quest-has-step-1",
    relation: {
      id: "relation:quest-has-step-1",
      type: "HAS_STEP",
      src_id: longQuestId,
      dst_id: "step:talk-to-lincheng",
      attrs: { required: true },
      schema_version: "ir-core@1",
    },
  },
];

describe("KnowledgeGraph", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    document.documentElement.removeAttribute("data-theme");
  });

  it("renders a bounded graph page with the stable simple layout and full semantic facts", () => {
    render(<KnowledgeGraph items={graphItems} pageLabel="图谱快照第 1 页" />);

    expect(cytoscapeMock.factory).toHaveBeenCalledTimes(1);
    const options = cytoscapeMock.factory.mock.calls[0]?.[0] as {
      elements: Array<{ classes?: string; data: Record<string, unknown> }>;
      layout: Record<string, unknown>;
    };
    expect(options.layout).toMatchObject({ name: "grid", avoidOverlap: true, fit: true });
    expect(options.elements).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ data: expect.objectContaining({ id: `entity:${longQuestId}` }) }),
        expect.objectContaining({ data: expect.objectContaining({ id: "entity:step:talk-to-lincheng" }) }),
        expect.objectContaining({
          data: expect.objectContaining({
            id: "relation:relation:quest-has-step-1",
            source: `entity:${longQuestId}`,
            target: "entity:step:talk-to-lincheng",
          }),
        }),
      ]),
    );

    expect(screen.getByRole("region", { name: "知识图谱" })).toBeVisible();
    expect(screen.getByText("图谱快照第 1 页")).toBeVisible();
    expect(screen.getByText("当前有界页：2 个实体，1 条关系")).toBeVisible();
    expect(screen.getAllByText(longQuestId).length).toBeGreaterThan(0);
    expect(screen.getByRole("table", { name: "图谱事实列表" })).toBeVisible();
    expect(screen.getByText("quests.csv · Quest · 第 17 行 · quest_id")).toBeVisible();
    expect(screen.getByText("当前选择：实体")).toBeVisible();
  });

  it("keeps list, inspector, search, and canvas selection synchronized", async () => {
    const user = userEvent.setup();
    render(<KnowledgeGraph items={graphItems} />);

    const relationRow = screen.getByRole("row", { name: /关系 HAS_STEP relation:quest-has-step-1/ });
    await user.click(within(relationRow).getByRole("button", { name: "检查关系 relation:quest-has-step-1" }));

    expect(within(relationRow).getByText("已选择")).toBeVisible();
    expect(screen.getByText("当前选择：关系")).toBeVisible();
    expect(screen.getByText("起点实体")).toBeVisible();
    expect(cytoscapeMock.cy.$id).toHaveBeenCalledWith("relation:relation:quest-has-step-1");
    expect(cytoscapeMock.select).toHaveBeenCalled();

    const tapHandler = cytoscapeMock.on.mock.calls.find(([event]) => event === "tap")?.[2];
    expect(tapHandler).toBeTypeOf("function");
    act(() => {
      tapHandler({ target: { data: () => "entity:step:talk-to-lincheng" } });
    });
    expect(screen.getByText("当前选择：实体")).toBeVisible();
    expect(screen.getByText("step:talk-to-lincheng", { selector: ".gf-kg__inspector-id" })).toBeVisible();

    await user.clear(screen.getByRole("searchbox", { name: "搜索当前图谱页" }));
    await user.type(screen.getByRole("searchbox", { name: "搜索当前图谱页" }), "HAS_STEP");
    const table = screen.getByRole("table", { name: "图谱事实列表" });
    expect(within(table).getAllByRole("row")).toHaveLength(2);
    expect(within(table).getByText("HAS_STEP")).toBeVisible();
    expect(screen.getByText("找到 1 项；画布中其余事实已弱化。")).toBeVisible();
  });

  it("keeps relations inspectable when their endpoint entities are outside the current bounded page", () => {
    const relationOnly: GraphItem[] = [graphItems[2]!];
    render(<KnowledgeGraph items={relationOnly} />);

    const options = cytoscapeMock.factory.mock.calls[0]?.[0] as {
      elements: Array<{ classes?: string; data: Record<string, unknown> }>;
    };
    expect(options.elements).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          classes: "gf-kg-node--reference",
          data: expect.objectContaining({ id: `entity:${longQuestId}`, loaded: false }),
        }),
        expect.objectContaining({
          classes: "gf-kg-node--reference",
          data: expect.objectContaining({ id: "entity:step:talk-to-lincheng", loaded: false }),
        }),
      ]),
    );
    expect(screen.getByText("当前有界页：0 个实体，1 条关系")).toBeVisible();
    expect(screen.getAllByText("端点未包含在当前页")).toHaveLength(2);
    expect(screen.getByText("关系仍可在事实列表中完整检查。")).toBeVisible();
  });

  it("passes an opaque next cursor verbatim and exposes explicit expired-cursor restart", async () => {
    const user = userEvent.setup();
    const onLoadMore = vi.fn();
    const onRestart = vi.fn();
    const { rerender } = render(
      <KnowledgeGraph
        items={graphItems}
        nextCursor="opaque/+cursor=="
        onLoadMore={onLoadMore}
        onRestart={onRestart}
      />,
    );

    await user.click(screen.getByRole("button", { name: "加载下一页图谱" }));
    expect(onLoadMore).toHaveBeenCalledWith("opaque/+cursor==");

    rerender(
      <KnowledgeGraph
        items={graphItems}
        nextCursor="opaque/+cursor=="
        onLoadMore={onLoadMore}
        onRestart={onRestart}
        paginationState="expired"
      />,
    );
    expect(screen.queryByRole("button", { name: "加载下一页图谱" })).not.toBeInTheDocument();
    expect(screen.getByText(/游标已过期/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "重新开始图谱查询" }));
    expect(onRestart).toHaveBeenCalledTimes(1);
  });

  it("destroys the Cytoscape instance on unmount", () => {
    const { unmount } = render(<KnowledgeGraph items={graphItems} />);
    unmount();

    expect(cytoscapeMock.off).toHaveBeenCalled();
    expect(cytoscapeMock.destroy).toHaveBeenCalledTimes(1);
  });

  it("uses text labels as well as visual emphasis for selected facts", () => {
    render(<KnowledgeGraph items={graphItems} />);

    const selectedRow = screen.getByRole("row", { name: new RegExp(`实体 QUEST ${longQuestId}`) });
    expect(within(selectedRow).getByText("已选择")).toBeVisible();
    expect(selectedRow).toHaveAttribute("aria-selected", "true");
  });

  it("does not make the inaccessible canvas the only fact surface", () => {
    render(<KnowledgeGraph items={graphItems} />);

    const canvas = screen.getByTestId("knowledge-graph-canvas");
    expect(canvas).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByRole("table", { name: "图谱事实列表" })).toBeVisible();
    const searchbox = screen.getByRole("searchbox", { name: "搜索当前图谱页" });
    searchbox.focus();
    expect(searchbox).toHaveFocus();
    const inspector = screen.getByRole("complementary", { name: "已选图谱事实" });
    inspector.focus();
    expect(inspector).toHaveFocus();
  });

  it("supports URL-owned search and selection without creating a second authority", async () => {
    const user = userEvent.setup();
    const onSearchQueryChange = vi.fn();
    const onSelectedFactKeyChange = vi.fn();
    render(
      <KnowledgeGraph
        items={graphItems}
        onSearchQueryChange={onSearchQueryChange}
        onSelectedFactKeyChange={onSelectedFactKeyChange}
        searchQuery=""
        selectedFactKey="relation:relation:quest-has-step-1"
      />,
    );

    expect(screen.getByRole("searchbox", { name: "搜索当前图谱页" })).toHaveValue("");
    expect(screen.getByText("当前选择：关系")).toBeVisible();
    const entityRow = screen.getByRole("row", { name: /实体 QUEST_STEP step:talk-to-lincheng/ });
    await user.click(within(entityRow).getByRole("button", { name: "检查实体 step:talk-to-lincheng" }));
    expect(onSelectedFactKeyChange).toHaveBeenCalledWith("entity:step:talk-to-lincheng");
    expect(screen.getByText("当前选择：关系")).toBeVisible();

    fireEvent.change(screen.getByRole("searchbox", { name: "搜索当前图谱页" }), {
      target: { value: "HAS_STEP" },
    });
    expect(onSearchQueryChange).toHaveBeenCalledWith("HAS_STEP");
  });

  it("clears an inaccessible selection when a search has no matches", async () => {
    const user = userEvent.setup();
    render(<KnowledgeGraph items={graphItems} />);

    await user.type(screen.getByRole("searchbox", { name: "搜索当前图谱页" }), "not-present-anywhere");
    expect(screen.getByText("请选择一个图谱事实。")).toBeVisible();
    expect(cytoscapeMock.unselect).toHaveBeenCalled();
  });
});
