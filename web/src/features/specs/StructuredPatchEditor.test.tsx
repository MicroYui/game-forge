import { describe, expect, it } from "vitest";

import type { components } from "../../api/generated/openapi";
import {
  compileStructuredOperations,
  createStructuredOperation,
  type StructuredOperationDraft,
} from "./StructuredPatchEditor";

type GraphItem = components["schemas"]["GraphItemV1"];

const quest: GraphItem = {
  entity: {
    attrs: { name: "前哨信标", reward_gold: 120 },
    id: "quest:frontier-beacon",
    schema_version: "ir-core@1",
    tags: ["主线"],
    type: "QUEST",
  },
  item_id: "quest:frontier-beacon",
  item_kind: "entity",
  item_schema_version: "graph-item@1",
};

const lincheng: GraphItem = {
  entity: {
    attrs: { name: "林澈" },
    id: "npc:lincheng",
    schema_version: "ir-core@1",
    type: "NPC",
  },
  item_id: "npc:lincheng",
  item_kind: "entity",
  item_schema_version: "graph-item@1",
};

const friendship: GraphItem = {
  item_id: "relation:friend:existing",
  item_kind: "relation",
  item_schema_version: "graph-item@1",
  relation: {
    attrs: { label: "旧友", trust: 40 },
    dst_id: "npc:lincheng",
    id: "relation:friend:existing",
    schema_version: "ir-core@1",
    src_id: "quest:frontier-beacon",
    type: "ALLY_WITH",
  },
};

function draft(rowId: string, values: Partial<StructuredOperationDraft>): StructuredOperationDraft {
  return { ...createStructuredOperation(rowId), ...values };
}

describe("compileStructuredOperations", () => {
  it("compiles all seven TypedOp kinds from readable graph selections without copied IDs", () => {
    const result = compileStructuredOperations(
      [
        draft("row-1", {
          attributes: [
            { key: "profession", rowId: "attribute-1", value: { kind: "string", text: "侦察员" } },
          ],
          entityName: "林逸",
          entityType: "NPC",
          op: "add_entity",
        }),
        draft("row-2", { entityRef: "entity:quest:frontier-beacon", op: "delete_entity" }),
        draft("row-3", {
          attributePath: "reward_gold",
          entityRef: "entity:quest:frontier-beacon",
          newValue: { kind: "number", text: "80" },
          op: "set_entity_attr",
        }),
        draft("row-4", {
          destinationEntityRef: "entity:npc:lincheng",
          op: "add_relation",
          relationLabel: "好友",
          relationType: "ALLY_WITH",
          sourceEntityRef: "new:row-1",
        }),
        draft("row-5", { op: "delete_relation", relationRef: "relation:relation:friend:existing" }),
        draft("row-6", {
          attributePath: "trust",
          newValue: { kind: "number", text: "80" },
          op: "set_relation_attr",
          relationRef: "relation:relation:friend:existing",
        }),
        draft("row-7", {
          attributes: [{ key: "reward_gold", rowId: "attribute-1", value: { kind: "number", text: "90" } }],
          entityRef: "entity:quest:frontier-beacon",
          op: "replace_subgraph",
          subgraphLabel: "前哨任务奖励",
          subgraphResourceKind: "entity",
        }),
      ],
      [quest, lincheng, friendship],
    );

    expect(result).toEqual({
      error: null,
      ops: [
        {
          new_value: { attrs: { name: "林逸", profession: "侦察员" }, type: "NPC" },
          op: "add_entity",
          op_id: "op:structured-1",
          target: "npc:林逸",
        },
        {
          op: "delete_entity",
          op_id: "op:structured-2",
          target: "quest:frontier-beacon",
        },
        {
          new_value: 80,
          old_value: 120,
          op: "set_entity_attr",
          op_id: "op:structured-3",
          target: "quest:frontier-beacon.reward_gold",
        },
        {
          new_value: {
            attrs: { label: "好友" },
            dst_id: "npc:lincheng",
            src_id: "npc:林逸",
            type: "ALLY_WITH",
          },
          op: "add_relation",
          op_id: "op:structured-4",
          target: "relation:ally_with:npc_林逸:npc_lincheng",
        },
        {
          op: "delete_relation",
          op_id: "op:structured-5",
          target: "relation:friend:existing",
        },
        {
          new_value: 80,
          old_value: 40,
          op: "set_relation_attr",
          op_id: "op:structured-6",
          target: "relation:friend:existing.trust",
        },
        {
          new_value: {
            entities: [
              {
                attrs: { name: "前哨信标", reward_gold: 90 },
                id: "quest:frontier-beacon",
                schema_version: "ir-core@1",
                tags: ["主线"],
                type: "QUEST",
              },
            ],
            relations: [],
          },
          old_value: {
            entities: {
              "quest:frontier-beacon": {
                attrs: { name: "前哨信标", reward_gold: 120 },
                id: "quest:frontier-beacon",
                schema_version: "ir-core@1",
                source_ref: null,
                tags: ["主线"],
                type: "QUEST",
              },
            },
            relations: {},
          },
          op: "replace_subgraph",
          op_id: "op:structured-7",
          target: "subgraph:前哨任务奖励",
        },
      ],
    });
  });

  it("expands a readable bidirectional relation into two exact operations", () => {
    const result = compileStructuredOperations(
      [
        draft("row-1", { entityName: "林逸", entityType: "NPC", op: "add_entity" }),
        draft("row-2", {
          bidirectional: true,
          destinationEntityRef: "entity:npc:lincheng",
          op: "add_relation",
          relationLabel: "好友",
          relationType: "ALLY_WITH",
          sourceEntityRef: "new:row-1",
        }),
      ],
      [lincheng],
    );

    expect(result.error).toBeNull();
    expect(result.ops).toEqual([
      {
        new_value: { attrs: { name: "林逸" }, type: "NPC" },
        op: "add_entity",
        op_id: "op:structured-1",
        target: "npc:林逸",
      },
      {
        new_value: {
          attrs: { label: "好友" },
          dst_id: "npc:lincheng",
          src_id: "npc:林逸",
          type: "ALLY_WITH",
        },
        op: "add_relation",
        op_id: "op:structured-2",
        target: "relation:ally_with:npc_林逸:npc_lincheng",
      },
      {
        new_value: {
          attrs: { label: "好友" },
          dst_id: "npc:林逸",
          src_id: "npc:lincheng",
          type: "ALLY_WITH",
        },
        op: "add_relation",
        op_id: "op:structured-2-reverse",
        target: "relation:ally_with:npc_lincheng:npc_林逸",
      },
    ]);
  });

  it("captures the exact relation wire before a structured subgraph replacement", () => {
    const result = compileStructuredOperations(
      [
        draft("row-1", {
          attributes: [{ key: "trust", rowId: "attribute-1", value: { kind: "number", text: "80" } }],
          op: "replace_subgraph",
          relationRef: "relation:relation:friend:existing",
          subgraphLabel: "好友信任度",
          subgraphResourceKind: "relation",
        }),
      ],
      [quest, lincheng, friendship],
    );

    expect(result).toEqual({
      error: null,
      ops: [
        {
          new_value: {
            entities: [],
            relations: [
              {
                attrs: { label: "旧友", trust: 80 },
                dst_id: "npc:lincheng",
                id: "relation:friend:existing",
                schema_version: "ir-core@1",
                src_id: "quest:frontier-beacon",
                type: "ALLY_WITH",
              },
            ],
          },
          old_value: {
            entities: {},
            relations: {
              "relation:friend:existing": {
                attrs: { label: "旧友", trust: 40 },
                dst_id: "npc:lincheng",
                id: "relation:friend:existing",
                schema_version: "ir-core@1",
                source_ref: null,
                src_id: "quest:frontier-beacon",
                type: "ALLY_WITH",
              },
            },
          },
          op: "replace_subgraph",
          op_id: "op:structured-1",
          target: "subgraph:好友信任度",
        },
      ],
    });
  });
});
