import type { components } from "../../api/generated/openapi";

export type ProjectEntity = components["schemas"]["Entity"];
export type ProjectRelation = components["schemas"]["Relation"];
export type ProjectGraphItem = components["schemas"]["GraphItemV1"];
export type ProjectIdentityConflict = components["schemas"]["IdentityConflictV1"];
export type ProjectNodeType = components["schemas"]["NodeType"];
export type ProjectEdgeType = components["schemas"]["EdgeType"];
type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];

export interface ProjectGraphDraft {
  entities: ProjectEntity[];
  relations: ProjectRelation[];
}

export const nodeTypes = [
  "FACTION",
  "CHARACTER",
  "NPC",
  "QUEST",
  "QUEST_STEP",
  "DIALOGUE_NODE",
  "REGION",
  "SPAWN_POINT",
  "INTERACTABLE",
  "ITEM",
  "MONSTER",
  "CURRENCY",
  "SHOP",
  "DROP_TABLE",
  "REWARD_TABLE",
  "GACHA_POOL",
  "EVENT",
  "UNLOCK_CONDITION",
  "EQUIPMENT",
  "SKILL",
  "STATUS_EFFECT",
  "EFFECT",
  "BATTLE_ENCOUNTER",
  "FORMULA",
] as const satisfies readonly ProjectNodeType[];

export const edgeTypes = [
  "HAS_STEP",
  "PRECEDES",
  "REQUIRES",
  "GATED_BY",
  "UNLOCKS",
  "STARTS_AT",
  "TALKS_TO",
  "TRIGGERED_BY",
  "LOCATED_IN",
  "CONTAINS",
  "SPAWNS",
  "PATH_TO",
  "DROPS_FROM",
  "GRANTS",
  "CONSUMES",
  "REWARDS",
  "SELLS",
  "USES_SKILL",
  "APPLIES_EFFECT",
  "HAS_STAT_CURVE",
  "HOSTILE_TO",
  "ALLY_WITH",
  "BELONGS_TO",
  "REVEALS",
  "REFERENCES",
] as const satisfies readonly ProjectEdgeType[];

const nodeTypeSet = new Set<string>(nodeTypes);
const edgeTypeSet = new Set<string>(edgeTypes);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function recordMap(value: unknown, label: string): Record<string, Record<string, unknown>> {
  if (!isRecord(value)) throw new TypeError(`IR snapshot ${label} must be an object map.`);
  const result: Record<string, Record<string, unknown>> = {};
  for (const [id, candidate] of Object.entries(value)) {
    if (!id || !isRecord(candidate)) throw new TypeError(`IR snapshot ${label} contains an invalid item.`);
    result[id] = candidate;
  }
  return result;
}

function sourceRef(value: unknown): components["schemas"]["SourceRef"] | null | undefined {
  if (value === null || value === undefined) return value;
  if (!isRecord(value) || typeof value.adapter !== "string" || typeof value.file !== "string") {
    throw new TypeError("IR snapshot source_ref is malformed.");
  }
  return structuredClone(value) as components["schemas"]["SourceRef"];
}

function attrs(value: unknown): Record<string, unknown> {
  if (value === null || value === undefined) return {};
  if (!isRecord(value)) throw new TypeError("IR snapshot attrs must be an object.");
  return structuredClone(value);
}

export function snapshotArtifactToGraphDraft(view: ArtifactPayloadView): ProjectGraphDraft {
  if (view.artifact.kind !== "ir_snapshot" || view.artifact.payload_schema_id !== "ir-core@1") {
    throw new TypeError("Candidate Artifact is not a verified IR snapshot.");
  }
  if (!isRecord(view.payload)) throw new TypeError("IR snapshot payload must be an object.");
  const entityMap = recordMap(view.payload.entities, "entities");
  const relationMap = recordMap(view.payload.relations, "relations");
  const entities = Object.entries(entityMap)
    .map(([id, value]): ProjectEntity => {
      if (typeof value.type !== "string" || !nodeTypeSet.has(value.type)) {
        throw new TypeError(`IR snapshot entity ${id} has an unknown type.`);
      }
      if (
        value.tags !== null &&
        value.tags !== undefined &&
        (!Array.isArray(value.tags) || value.tags.some((tag) => typeof tag !== "string"))
      ) {
        throw new TypeError(`IR snapshot entity ${id} has malformed tags.`);
      }
      return {
        attrs: attrs(value.attrs),
        id,
        schema_version: typeof value.schema_version === "string" ? value.schema_version : "ir-core@1",
        source_ref: sourceRef(value.source_ref),
        tags: value.tags === null ? null : value.tags ? ([...value.tags] as string[]) : [],
        type: value.type as ProjectNodeType,
      };
    })
    .sort((left, right) => left.id.localeCompare(right.id));
  const relations = Object.entries(relationMap)
    .map(([id, value]): ProjectRelation => {
      if (typeof value.type !== "string" || !edgeTypeSet.has(value.type)) {
        throw new TypeError(`IR snapshot relation ${id} has an unknown type.`);
      }
      if (typeof value.src_id !== "string" || typeof value.dst_id !== "string") {
        throw new TypeError(`IR snapshot relation ${id} has malformed endpoints.`);
      }
      return {
        attrs: attrs(value.attrs),
        dst_id: value.dst_id,
        id,
        schema_version: typeof value.schema_version === "string" ? value.schema_version : "ir-core@1",
        source_ref: sourceRef(value.source_ref),
        src_id: value.src_id,
        type: value.type as ProjectEdgeType,
      };
    })
    .sort((left, right) => left.id.localeCompare(right.id));
  return { entities, relations };
}

export function toGraphItems(draft: ProjectGraphDraft): ProjectGraphItem[] {
  return [
    ...draft.entities.map(
      (entity): ProjectGraphItem => ({
        entity,
        item_id: entity.id,
        item_kind: "entity",
        item_schema_version: "graph-item@1",
        relation: null,
      }),
    ),
    ...draft.relations.map(
      (relation): ProjectGraphItem => ({
        entity: null,
        item_id: relation.id,
        item_kind: "relation",
        item_schema_version: "graph-item@1",
        relation,
      }),
    ),
  ];
}

export function canonicalAttributeKeyPreview(value: string): string {
  const token = value
    .normalize("NFKC")
    .toLowerCase()
    .trim()
    .replace(/[._/\\\-\s]+/gu, "_")
    .replace(/[^\p{L}\p{N}_]+/gu, "_")
    .replace(/_+/gu, "_")
    .replace(/^_+|_+$/gu, "");
  return token;
}

function setNestedValue(target: Record<string, unknown>, path: readonly string[], value: unknown): void {
  const [head, ...tail] = path;
  if (!head) return;
  if (tail.length === 0) {
    target[head] = structuredClone(value);
    return;
  }
  const current = isRecord(target[head]) ? { ...target[head] } : {};
  target[head] = current;
  setNestedValue(current, tail, value);
}

export function applyIdentityConflictResolution(
  draft: ProjectGraphDraft,
  conflict: ProjectIdentityConflict,
  candidateIndex: number,
): ProjectGraphDraft {
  const candidate = conflict.candidates[candidateIndex];
  if (!candidate) throw new RangeError("Identity conflict candidate does not exist.");
  return applyIdentityConflictValue(draft, conflict, candidate.value);
}

export function applyIdentityConflictValue(
  draft: ProjectGraphDraft,
  conflict: ProjectIdentityConflict,
  value: unknown,
): ProjectGraphDraft {
  const next = structuredClone(draft);
  const [identity, ...attributePath] = conflict.canonical_identity.split(".");
  if (!identity) throw new TypeError("冲突缺少可写回的内容位置。");

  const entity = next.entities.find((item) => item.id === identity);
  if (entity) {
    if (conflict.code === "entity_type_conflict") {
      if (typeof value !== "string" || !nodeTypeSet.has(value)) {
        throw new TypeError("手工内容类型不是系统支持的实体类型。");
      }
      entity.type = value as ProjectNodeType;
      return next;
    }
    if (attributePath.length > 0) {
      const nextAttrs = { ...(entity.attrs ?? {}) };
      setNestedValue(nextAttrs, attributePath, value);
      entity.attrs = nextAttrs;
      return next;
    }
    throw new TypeError("这个冲突没有可写回的实体属性，请在图形编辑器中直接修正内容。");
  }

  let relation = next.relations.find((item) => item.id === identity);
  if (!relation && conflict.code === "relation_shape_conflict" && isRecord(value)) {
    if (
      typeof value.src_id !== "string" ||
      typeof value.dst_id !== "string" ||
      typeof value.type !== "string" ||
      !edgeTypeSet.has(value.type)
    ) {
      throw new TypeError("手工关系必须包含有效的起点、终点和关系类型。");
    }
    relation = {
      attrs: attrs(value.attrs),
      dst_id: value.dst_id,
      id: identity,
      schema_version: "ir-core@1",
      source_ref: null,
      src_id: value.src_id,
      type: value.type as ProjectEdgeType,
    };
    next.relations.push(relation);
  }
  if (!relation) {
    throw new TypeError(`关系 ${identity} 不存在；请先在图形编辑器中添加这条关系，再确认端点。`);
  }
  const field = attributePath[0];
  if (field === "src_id" || field === "dst_id") {
    if (typeof value !== "string") throw new TypeError("关系端点必须选择一个具体实体。");
    if (!next.entities.some((item) => item.id === value)) {
      throw new TypeError(`实体 ${value} 不在当前草案中；请先添加该实体或选择现有内容。`);
    }
    relation[field] = value;
  } else if (field) {
    const nextAttrs = { ...(relation.attrs ?? {}) };
    setNestedValue(nextAttrs, attributePath, value);
    relation.attrs = nextAttrs;
  } else if (conflict.code === "relation_shape_conflict" && isRecord(value)) {
    if (typeof value.src_id === "string") relation.src_id = value.src_id;
    if (typeof value.dst_id === "string") relation.dst_id = value.dst_id;
    if (typeof value.type === "string" && edgeTypeSet.has(value.type)) {
      relation.type = value.type as ProjectEdgeType;
    }
    if (isRecord(value.attrs)) relation.attrs = structuredClone(value.attrs);
  } else {
    throw new TypeError("这个冲突没有可写回的关系字段，请在图形编辑器中直接修正内容。");
  }
  return next;
}
