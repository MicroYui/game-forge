import { describe, expect, it } from "vitest";

import type { components } from "../../api/generated/openapi";
import { adaptGraphItems, graphFactKey, graphSearchText, toCytoscapeElements } from "./model";

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
    expect(graphSearchText(facts[0]!)).toContain("林澄");
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
  });
});
