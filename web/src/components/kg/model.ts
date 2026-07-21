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
  return `${fact.kind} ${fact.type} ${fact.id} ${relationText} ${JSON.stringify(fact.attrs)} ${
    fact.sourceRef?.adapter ?? ""
  } ${formatSourceRef(fact.sourceRef)}`.toLocaleLowerCase("zh-CN");
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

function entityLabel(fact: EntityGraphFact): string {
  const candidate = fact.attrs.display_name ?? fact.attrs.name ?? fact.attrs.title;
  const name = typeof candidate === "string" && candidate.trim() ? candidate : fact.id;
  return `${fact.type}\n${compactLabel(name)}`;
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
      label: entityLabel(entity),
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
      label: relation.type,
      source: `entity:${relation.srcId}`,
      target: `entity:${relation.dstId}`,
      type: relation.type,
    },
  }));

  return [...entityElements, ...referenceElements, ...relationElements];
}
