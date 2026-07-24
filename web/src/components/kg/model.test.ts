import { describe, expect, it } from "vitest";

import type { components } from "../../api/generated/openapi";
import {
  adaptGraphItems,
  graphFactDisplayName,
  graphFactKey,
  graphSearchText,
  graphTypeLabel,
  toCytoscapeElements,
} from "./model";

type GraphItem = components["schemas"]["GraphItemV1"];

const items: GraphItem[] = [
  {
    item_schema_version: "graph-item@1",
    item_kind: "entity",
    item_id: "npc:lincheng",
    entity: {
      id: "npc:lincheng",
      type: "NPC",
      attrs: { display_name: "林澄" },
      schema_version: "ir-core@1",
    },
  },
  {
    item_schema_version: "graph-item@1",
    item_kind: "relation",
    item_id: "relation:talks-to",
    relation: {
      id: "relation:talks-to",
      type: "TALKS_TO",
      src_id: "step:opening",
      dst_id: "npc:lincheng",
      schema_version: "ir-core@1",
    },
  },
];

describe("knowledge graph adapter boundary", () => {
  it("narrows the generated optional wire shape without copying the DTO", () => {
    const facts = adaptGraphItems(items);

    expect(facts).toEqual([
      expect.objectContaining({ kind: "entity", id: "npc:lincheng", type: "NPC" }),
      expect.objectContaining({
        kind: "relation",
        id: "relation:talks-to",
        type: "TALKS_TO",
        srcId: "step:opening",
        dstId: "npc:lincheng",
      }),
    ]);
    expect(graphFactKey(facts[0]!)).toBe("entity:npc:lincheng");
    expect(graphFactKey(facts[1]!)).toBe("relation:relation:talks-to");
    expect(graphFactDisplayName(facts[0]!)).toBe("林澄");
    expect(graphTypeLabel(facts[0]!)).toBe("非玩家角色");
    expect(graphFactDisplayName(facts[1]!)).toBe("对话对象");
    expect(graphTypeLabel(facts[1]!)).toBe("对话对象");
    expect(graphSearchText(facts[0]!)).toContain("林澄");
    expect(graphSearchText(facts[0]!)).toContain("非玩家角色");
  });

  it("rejects an impossible discriminator/payload mismatch at the one adapter seam", () => {
    const invalid = {
      ...items[0],
      item_id: "npc:other",
    } as GraphItem;

    expect(() => adaptGraphItems([invalid])).toThrow(/graph-item@1 payload/);
  });

  it("adds stable reference nodes only for relation endpoints absent from this page", () => {
    const elements = toCytoscapeElements(adaptGraphItems(items));

    expect(elements.map((element) => element.data.id)).toEqual([
      "entity:npc:lincheng",
      "entity:step:opening",
      "relation:relation:talks-to",
    ]);
    expect(elements[1]).toMatchObject({
      classes: "gf-kg-node--reference",
      data: { id: "entity:step:opening", loaded: false },
    });
    expect(elements[0]).toMatchObject({ data: { label: "非玩家角色\n林澄" } });
    expect(elements[2]).toMatchObject({ data: { label: "对话对象" } });
  });

  it("uses readable unknown-type fallbacks while retaining the exact wire type", () => {
    const [fact] = adaptGraphItems([
      {
        entity: {
          attrs: {},
          id: "custom:unnamed",
          schema_version: "custom@1",
          type: "CUSTOM_NODE",
        },
        item_id: "custom:unnamed",
        item_kind: "entity",
        item_schema_version: "graph-item@1",
      } as unknown as GraphItem,
    ]);

    expect(graphTypeLabel(fact!)).toBe("其他实体类型");
    expect(graphFactDisplayName(fact!)).toBe("未命名其他实体类型");
    expect(graphSearchText(fact!)).toContain("custom_node");
  });

  it("uses a relation business label before the generic relation type", () => {
    const [fact] = adaptGraphItems([
      {
        item_id: "relation:friends",
        item_kind: "relation",
        item_schema_version: "graph-item@1",
        relation: {
          attrs: { label: "好友" },
          dst_id: "npc:lincheng",
          id: "relation:friends",
          schema_version: "ir-core@1",
          src_id: "npc:linyi",
          type: "ALLY_WITH",
        },
      } as GraphItem,
    ]);

    expect(graphFactDisplayName(fact!)).toBe("好友");
    expect(graphTypeLabel(fact!)).toBe("结盟");
    expect(graphSearchText(fact!)).toContain("好友");
  });
});
