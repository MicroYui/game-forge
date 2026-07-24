import type { ElementDefinition } from "cytoscape";

import type { components } from "../../api/generated/openapi";

export type GraphItem = components["schemas"]["GraphItemV1"];
export type GraphSourceRef = components["schemas"]["SourceRef"];

interface GraphFactBase {
  attrs: Readonly<Record<string, unknown>>;
  id: string;
  schemaVersion: string;
  sourceRef: GraphSourceRef | null;
  type: string;
}

export interface EntityGraphFact extends GraphFactBase {
  kind: "entity";
  tags: readonly string[];
}

export interface RelationGraphFact extends GraphFactBase {
  dstId: string;
  kind: "relation";
  srcId: string;
}

export type GraphFact = EntityGraphFact | RelationGraphFact;

const ENTITY_TYPE_LABELS: Readonly<Record<string, string>> = {
  BATTLE_ENCOUNTER: "战斗遭遇",
  CHARACTER: "角色",
  CURRENCY: "货币",
  DIALOGUE_NODE: "对话节点",
  DROP_TABLE: "掉落表",
  EFFECT: "效果",
  EQUIPMENT: "装备",
  EVENT: "事件",
  FACTION: "阵营",
  FORMULA: "公式",
  GACHA_POOL: "卡池",
  INTERACTABLE: "交互物",
  ITEM: "道具",
  MONSTER: "怪物",
  NPC: "非玩家角色",
  QUEST: "任务",
  QUEST_STEP: "任务步骤",
  REGION: "区域",
  REWARD_TABLE: "奖励表",
  SHOP: "商店",
  SKILL: "技能",
  SPAWN_POINT: "生成点",
  STATUS_EFFECT: "状态效果",
  UNLOCK_CONDITION: "解锁条件",
};

const RELATION_TYPE_LABELS: Readonly<Record<string, string>> = {
  ALLY_WITH: "结盟",
  APPLIES_EFFECT: "施加效果",
  BELONGS_TO: "隶属于",
  CONSUMES: "消耗",
  CONTAINS: "包含",
  DROPS_FROM: "产出掉落",
  GATED_BY: "受条件约束",
  GRANTS: "赋予",
  HAS_STAT_CURVE: "使用属性曲线",
  HAS_STEP: "包含步骤",
  HOSTILE_TO: "敌对",
  LOCATED_IN: "位于",
  PATH_TO: "通往",
  PRECEDES: "前置于",
  REFERENCES: "引用",
  REQUIRES: "需要",
  REVEALS: "揭示",
  REWARDS: "奖励",
  SELLS: "出售",
  SPAWNS: "生成",
  STARTS_AT: "开始于",
  TALKS_TO: "对话对象",
  TRIGGERED_BY: "由其触发",
  UNLOCKS: "解锁",
  USES_SKILL: "使用技能",
};

export function graphTypeLabel(fact: GraphFact): string {
  if (fact.kind === "entity") return ENTITY_TYPE_LABELS[fact.type] ?? "其他实体类型";
  return RELATION_TYPE_LABELS[fact.type] ?? "其他关系类型";
}

export function graphFactDisplayName(fact: GraphFact): string {
  if (fact.kind === "relation") {
    const candidate = fact.attrs.display_name ?? fact.attrs.name ?? fact.attrs.title ?? fact.attrs.label;
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
    return graphTypeLabel(fact);
  }
  const candidate = fact.attrs.display_name ?? fact.attrs.name ?? fact.attrs.title;
  if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  return `未命名${graphTypeLabel(fact)}`;
}

function entityFact(item: GraphItem): EntityGraphFact {
  const entity = item.entity;
  if (item.item_kind !== "entity" || entity === null || entity === undefined || entity.id !== item.item_id) {
    throw new TypeError("graph-item@1 payload does not match its entity discriminator");
  }
  if (item.relation !== null && item.relation !== undefined) {
    throw new TypeError("graph-item@1 entity cannot also contain a relation payload");
  }

  return {
    attrs: entity.attrs ?? {},
    id: entity.id,
    kind: "entity",
    schemaVersion: entity.schema_version,
    sourceRef: entity.source_ref ?? null,
    tags: entity.tags ?? [],
    type: entity.type,
  };
}

function relationFact(item: GraphItem): RelationGraphFact {
  const relation = item.relation;
  if (
    item.item_kind !== "relation" ||
    relation === null ||
    relation === undefined ||
    relation.id !== item.item_id
  ) {
    throw new TypeError("graph-item@1 payload does not match its relation discriminator");
  }
  if (item.entity !== null && item.entity !== undefined) {
    throw new TypeError("graph-item@1 relation cannot also contain an entity payload");
  }

  return {
    attrs: relation.attrs ?? {},
    dstId: relation.dst_id,
    id: relation.id,
    kind: "relation",
    schemaVersion: relation.schema_version,
    sourceRef: relation.source_ref ?? null,
    srcId: relation.src_id,
    type: relation.type,
  };
}

export function adaptGraphItems(items: readonly GraphItem[]): readonly GraphFact[] {
  const facts = items.map((item) => (item.item_kind === "entity" ? entityFact(item) : relationFact(item)));
  const keys = new Set<string>();
  for (const fact of facts) {
    const key = graphFactKey(fact);
    if (keys.has(key)) throw new TypeError(`graph-item@1 contains duplicate fact ${key}`);
    keys.add(key);
  }
  return facts;
}

export function graphFactKey(fact: GraphFact): string {
  return `${fact.kind}:${fact.id}`;
}

export function graphSearchText(fact: GraphFact): string {
  const relationText = fact.kind === "relation" ? `${fact.srcId} ${fact.dstId}` : fact.tags.join(" ");
  return `${fact.kind} ${graphTypeLabel(fact)} ${graphFactDisplayName(fact)} ${fact.type} ${fact.id} ${relationText} ${JSON.stringify(fact.attrs)} ${fact.sourceRef?.adapter ?? ""} ${formatSourceRef(fact.sourceRef)}`.toLocaleLowerCase(
    "zh-CN",
  );
}

export function formatSourceRef(sourceRef: GraphSourceRef | null): string {
  if (sourceRef === null) return "无来源定位";
  const parts = [sourceRef.file];
  if (sourceRef.sheet) parts.push(sourceRef.sheet);
  if (sourceRef.row !== null && sourceRef.row !== undefined) parts.push(`第 ${sourceRef.row} 行`);
  if (sourceRef.column) parts.push(sourceRef.column);
  return parts.join(" · ");
}

function compactLabel(value: string, maximum = 30): string {
  if (value.length <= maximum) return value;
  const head = Math.ceil((maximum - 1) / 2);
  const tail = Math.floor((maximum - 1) / 2);
  return `${value.slice(0, head)}…${value.slice(-tail)}`;
}

export function toCytoscapeElements(facts: readonly GraphFact[]): ElementDefinition[] {
  const entities = facts.filter((fact): fact is EntityGraphFact => fact.kind === "entity");
  const relations = facts.filter((fact): fact is RelationGraphFact => fact.kind === "relation");
  const loadedEntityIds = new Set(entities.map((entity) => entity.id));
  const referenceIds = new Set<string>();

  for (const relation of relations) {
    if (!loadedEntityIds.has(relation.srcId)) referenceIds.add(relation.srcId);
    if (!loadedEntityIds.has(relation.dstId)) referenceIds.add(relation.dstId);
  }

  const entityElements: ElementDefinition[] = entities.map((entity) => ({
    classes: "gf-kg-node--entity",
    data: {
      factKey: graphFactKey(entity),
      id: graphFactKey(entity),
      label: `${graphTypeLabel(entity)}\n${compactLabel(graphFactDisplayName(entity))}`,
      loaded: true,
      type: entity.type,
    },
  }));
  const referenceElements: ElementDefinition[] = [...referenceIds]
    .sort((left, right) => left.localeCompare(right))
    .map((id) => ({
      classes: "gf-kg-node--reference",
      data: {
        id: `entity:${id}`,
        label: `未载入端点\n${compactLabel(id)}`,
        loaded: false,
        type: "REFERENCE",
      },
    }));
  const relationElements: ElementDefinition[] = relations.map((relation) => ({
    classes: "gf-kg-edge--relation",
    data: {
      factKey: graphFactKey(relation),
      id: graphFactKey(relation),
      label: graphTypeLabel(relation),
      source: `entity:${relation.srcId}`,
      target: `entity:${relation.dstId}`,
      type: relation.type,
    },
  }));

  return [...entityElements, ...referenceElements, ...relationElements];
}
