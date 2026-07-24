import type { components } from "../../api/generated/openapi";

type GraphItem = components["schemas"]["GraphItemV1"];
type Entity = components["schemas"]["Entity"];
type Relation = components["schemas"]["Relation"];
type NodeType = components["schemas"]["NodeType"];
type EdgeType = components["schemas"]["EdgeType"];
type TypedOp = components["schemas"]["TypedOp"];
type TypedOpKind = TypedOp["op"];

type ValueKind = "string" | "number" | "boolean" | "null" | "json";

export interface ValueDraft {
  kind: ValueKind;
  text: string;
}

export interface AttributeDraft {
  key: string;
  rowId: string;
  value: ValueDraft;
}

export interface StructuredOperationDraft {
  attributePath: string;
  attributes: AttributeDraft[];
  bidirectional: boolean;
  destinationEntityRef: string;
  entityName: string;
  entityRef: string;
  entityType: NodeType;
  newValue: ValueDraft;
  op: TypedOpKind;
  relationLabel: string;
  relationRef: string;
  relationType: EdgeType;
  rowId: string;
  sourceEntityRef: string;
  subgraphLabel: string;
  subgraphResourceKind: "entity" | "relation";
}

interface CompileResult {
  error: string | null;
  ops: TypedOp[];
}

const NODE_TYPES: readonly { label: string; value: NodeType }[] = [
  { label: "非玩家角色", value: "NPC" },
  { label: "角色", value: "CHARACTER" },
  { label: "区域", value: "REGION" },
  { label: "任务", value: "QUEST" },
  { label: "任务步骤", value: "QUEST_STEP" },
  { label: "对话节点", value: "DIALOGUE_NODE" },
  { label: "阵营", value: "FACTION" },
  { label: "生成点", value: "SPAWN_POINT" },
  { label: "交互物", value: "INTERACTABLE" },
  { label: "道具", value: "ITEM" },
  { label: "怪物", value: "MONSTER" },
  { label: "货币", value: "CURRENCY" },
  { label: "商店", value: "SHOP" },
  { label: "掉落表", value: "DROP_TABLE" },
  { label: "奖励表", value: "REWARD_TABLE" },
  { label: "卡池", value: "GACHA_POOL" },
  { label: "事件", value: "EVENT" },
  { label: "解锁条件", value: "UNLOCK_CONDITION" },
  { label: "装备", value: "EQUIPMENT" },
  { label: "技能", value: "SKILL" },
  { label: "状态效果", value: "STATUS_EFFECT" },
  { label: "效果", value: "EFFECT" },
  { label: "战斗遭遇", value: "BATTLE_ENCOUNTER" },
  { label: "公式", value: "FORMULA" },
];

const RELATION_TYPES: readonly { label: string; value: EdgeType }[] = [
  { label: "结盟 / 好友", value: "ALLY_WITH" },
  { label: "位于", value: "LOCATED_IN" },
  { label: "包含步骤", value: "HAS_STEP" },
  { label: "前置于", value: "PRECEDES" },
  { label: "需要", value: "REQUIRES" },
  { label: "受条件约束", value: "GATED_BY" },
  { label: "解锁", value: "UNLOCKS" },
  { label: "开始于", value: "STARTS_AT" },
  { label: "对话对象", value: "TALKS_TO" },
  { label: "由其触发", value: "TRIGGERED_BY" },
  { label: "包含", value: "CONTAINS" },
  { label: "生成", value: "SPAWNS" },
  { label: "通往", value: "PATH_TO" },
  { label: "产出掉落", value: "DROPS_FROM" },
  { label: "赋予", value: "GRANTS" },
  { label: "消耗", value: "CONSUMES" },
  { label: "奖励", value: "REWARDS" },
  { label: "出售", value: "SELLS" },
  { label: "使用技能", value: "USES_SKILL" },
  { label: "施加效果", value: "APPLIES_EFFECT" },
  { label: "使用属性曲线", value: "HAS_STAT_CURVE" },
  { label: "敌对", value: "HOSTILE_TO" },
  { label: "隶属于", value: "BELONGS_TO" },
  { label: "揭示", value: "REVEALS" },
  { label: "引用", value: "REFERENCES" },
];

const OPERATION_TYPES: readonly { label: string; value: TypedOpKind }[] = [
  { label: "新增实体", value: "add_entity" },
  { label: "删除实体", value: "delete_entity" },
  { label: "修改实体字段", value: "set_entity_attr" },
  { label: "新增关系", value: "add_relation" },
  { label: "删除关系", value: "delete_relation" },
  { label: "修改关系字段", value: "set_relation_attr" },
  { label: "替换子图中的对象", value: "replace_subgraph" },
];

const nodeTypeLabel = (value: NodeType) =>
  NODE_TYPES.find((candidate) => candidate.value === value)?.label ?? value;

const relationTypeLabel = (value: EdgeType) =>
  RELATION_TYPES.find((candidate) => candidate.value === value)?.label ?? value;

export function createStructuredOperation(rowId: string): StructuredOperationDraft {
  return {
    attributePath: "",
    attributes: [],
    bidirectional: false,
    destinationEntityRef: "",
    entityName: "",
    entityRef: "",
    entityType: "NPC",
    newValue: { kind: "string", text: "" },
    op: "add_entity",
    relationLabel: "",
    relationRef: "",
    relationType: "ALLY_WITH",
    rowId,
    sourceEntityRef: "",
    subgraphLabel: "",
    subgraphResourceKind: "entity",
  };
}

function graphEntities(items: readonly GraphItem[]): Entity[] {
  return items.flatMap((item) => (item.item_kind === "entity" && item.entity ? [item.entity] : []));
}

function graphRelations(items: readonly GraphItem[]): Relation[] {
  return items.flatMap((item) => (item.item_kind === "relation" && item.relation ? [item.relation] : []));
}

function entityDisplayName(entity: Entity): string {
  const candidate = entity.attrs?.display_name ?? entity.attrs?.name ?? entity.attrs?.title;
  return typeof candidate === "string" && candidate.trim()
    ? candidate.trim()
    : `未命名${nodeTypeLabel(entity.type)}`;
}

function entityRef(entityId: string): string {
  return `entity:${entityId}`;
}

function relationRef(relationId: string): string {
  return `relation:${relationId}`;
}

function entityIdFromRef(reference: string, addedEntityIds: ReadonlyMap<string, string>): string | null {
  if (reference.startsWith("entity:")) return reference.slice("entity:".length) || null;
  if (reference.startsWith("new:")) return addedEntityIds.get(reference.slice("new:".length)) ?? null;
  return null;
}

function relationIdFromRef(reference: string): string | null {
  return reference.startsWith("relation:") ? reference.slice("relation:".length) || null : null;
}

function businessSlug(value: string): string {
  return value
    .normalize("NFKC")
    .trim()
    .replace(/\s+/gu, "-")
    .replace(/[^\p{L}\p{N}_-]+/gu, "-")
    .replace(/^-+|-+$/gu, "")
    .toLocaleLowerCase("zh-CN");
}

function uniqueId(base: string, occupied: Set<string>): string {
  let candidate = base;
  let suffix = 2;
  while (occupied.has(candidate)) {
    candidate = `${base}:${suffix}`;
    suffix += 1;
  }
  occupied.add(candidate);
  return candidate;
}

function relationIdFragment(value: string): string {
  return value
    .normalize("NFKC")
    .replace(/[^\p{L}\p{N}_-]+/gu, "_")
    .replace(/^_+|_+$/gu, "")
    .toLocaleLowerCase("zh-CN");
}

function parseValue(value: ValueDraft): { error: string | null; value: unknown } {
  if (value.kind === "string") return { error: null, value: value.text };
  if (value.kind === "null") return { error: null, value: null };
  if (value.kind === "boolean") return { error: null, value: value.text === "true" };
  if (value.kind === "number") {
    const parsed = Number(value.text);
    return value.text.trim() && Number.isFinite(parsed)
      ? { error: null, value: parsed }
      : { error: "数值字段必须填写有效数字。", value: undefined };
  }
  try {
    const parsed: unknown = JSON.parse(value.text);
    return typeof parsed === "object" && parsed !== null
      ? { error: null, value: parsed }
      : { error: "对象 / 数组值必须填写有效 JSON 对象或数组。", value: undefined };
  } catch {
    return { error: "对象 / 数组值必须填写有效 JSON 对象或数组。", value: undefined };
  }
}

function setNested(target: Record<string, unknown>, path: string, value: unknown): void {
  const parts = path.split(".").map((part) => part.trim());
  let cursor = target;
  for (const part of parts.slice(0, -1)) {
    const current = cursor[part];
    const next =
      typeof current === "object" && current !== null && !Array.isArray(current)
        ? { ...(current as Record<string, unknown>) }
        : {};
    cursor[part] = next;
    cursor = next;
  }
  cursor[parts[parts.length - 1]!] = value;
}

function getNested(target: Record<string, unknown> | null | undefined, path: string): unknown {
  let cursor: unknown = target;
  for (const part of path.split(".")) {
    if (typeof cursor !== "object" || cursor === null || Array.isArray(cursor)) return undefined;
    cursor = (cursor as Record<string, unknown>)[part];
  }
  return cursor;
}

function compileAttributes(
  rows: readonly AttributeDraft[],
  initial: Record<string, unknown> = {},
): { attrs: Record<string, unknown>; error: string | null } {
  const attrs = structuredClone(initial);
  const keys = new Set<string>();
  for (const row of rows) {
    const key = row.key.trim();
    if (!key) return { attrs, error: "已添加的字段必须填写字段名。" };
    if (keys.has(key)) return { attrs, error: `字段「${key}」重复。` };
    keys.add(key);
    const parsed = parseValue(row.value);
    if (parsed.error) return { attrs, error: parsed.error };
    setNested(attrs, key, parsed.value);
  }
  return { attrs, error: null };
}

function entityPayload(entity: Entity, attrs: Record<string, unknown>): Record<string, unknown> {
  return {
    attrs,
    id: entity.id,
    schema_version: entity.schema_version,
    ...(entity.source_ref !== undefined ? { source_ref: entity.source_ref } : {}),
    ...(entity.tags !== undefined ? { tags: entity.tags } : {}),
    type: entity.type,
  };
}

function relationPayload(relation: Relation, attrs: Record<string, unknown>): Record<string, unknown> {
  return {
    attrs,
    dst_id: relation.dst_id,
    id: relation.id,
    schema_version: relation.schema_version,
    ...(relation.source_ref !== undefined ? { source_ref: relation.source_ref } : {}),
    src_id: relation.src_id,
    type: relation.type,
  };
}

function exactEntityWire(entity: Entity): Record<string, unknown> {
  return {
    attrs: structuredClone(entity.attrs ?? {}),
    id: entity.id,
    schema_version: entity.schema_version,
    source_ref: structuredClone(entity.source_ref ?? null),
    tags: structuredClone(entity.tags ?? null),
    type: entity.type,
  };
}

function exactRelationWire(relation: Relation): Record<string, unknown> {
  return {
    attrs: structuredClone(relation.attrs ?? null),
    dst_id: relation.dst_id,
    id: relation.id,
    schema_version: relation.schema_version,
    source_ref: structuredClone(relation.source_ref ?? null),
    src_id: relation.src_id,
    type: relation.type,
  };
}

function incomplete(message: string): CompileResult {
  return { error: message, ops: [] };
}

export function compileStructuredOperations(
  drafts: readonly StructuredOperationDraft[],
  items: readonly GraphItem[],
): CompileResult {
  if (drafts.length === 0) return incomplete("请至少添加一项变更。");
  const entities = graphEntities(items);
  const relations = graphRelations(items);
  const entitiesById = new Map(entities.map((entity) => [entity.id, entity]));
  const relationsById = new Map(relations.map((relation) => [relation.id, relation]));
  const occupiedEntityIds = new Set(entitiesById.keys());
  const occupiedRelationIds = new Set(relationsById.keys());
  const addedEntityIds = new Map<string, string>();

  for (const draft of drafts) {
    if (draft.op !== "add_entity") continue;
    const slug = businessSlug(draft.entityName);
    if (!slug) return incomplete("新增实体必须填写业务名称。");
    addedEntityIds.set(
      draft.rowId,
      uniqueId(`${draft.entityType.toLocaleLowerCase()}:${slug}`, occupiedEntityIds),
    );
  }

  const ops: TypedOp[] = [];
  for (const [index, draft] of drafts.entries()) {
    const opId = `op:structured-${index + 1}`;
    if (draft.op === "add_entity") {
      const attributes = compileAttributes(draft.attributes, { name: draft.entityName.trim() });
      if (attributes.error) return incomplete(attributes.error);
      ops.push({
        new_value: { attrs: attributes.attrs, type: draft.entityType },
        op: draft.op,
        op_id: opId,
        target: addedEntityIds.get(draft.rowId)!,
      });
      continue;
    }

    if (draft.op === "delete_entity") {
      const target = entityIdFromRef(draft.entityRef, addedEntityIds);
      if (!target) return incomplete("请选择要删除的实体。");
      ops.push({ op: draft.op, op_id: opId, target });
      continue;
    }

    if (draft.op === "set_entity_attr") {
      const entityId = entityIdFromRef(draft.entityRef, addedEntityIds);
      const path = draft.attributePath.trim();
      if (!entityId || !path) return incomplete("请选择实体并填写要修改的字段。");
      const parsed = parseValue(draft.newValue);
      if (parsed.error) return incomplete(parsed.error);
      const oldValue = getNested(entitiesById.get(entityId)?.attrs, path);
      ops.push({
        new_value: parsed.value,
        ...(oldValue !== undefined ? { old_value: oldValue } : {}),
        op: draft.op,
        op_id: opId,
        target: `${entityId}.${path}`,
      });
      continue;
    }

    if (draft.op === "add_relation") {
      const sourceId = entityIdFromRef(draft.sourceEntityRef, addedEntityIds);
      const destinationId = entityIdFromRef(draft.destinationEntityRef, addedEntityIds);
      if (!sourceId || !destinationId) return incomplete("请选择关系的起点和终点。");
      const attributes = compileAttributes(
        draft.attributes,
        draft.relationLabel.trim() ? { label: draft.relationLabel.trim() } : {},
      );
      if (attributes.error) return incomplete(attributes.error);
      const payload = {
        ...(Object.keys(attributes.attrs).length > 0 ? { attrs: attributes.attrs } : {}),
        dst_id: destinationId,
        src_id: sourceId,
        type: draft.relationType,
      };
      const target = uniqueId(
        `relation:${draft.relationType.toLocaleLowerCase()}:${relationIdFragment(sourceId)}:${relationIdFragment(destinationId)}`,
        occupiedRelationIds,
      );
      ops.push({ new_value: payload, op: draft.op, op_id: opId, target });
      if (draft.bidirectional) {
        const reverseTarget = uniqueId(
          `relation:${draft.relationType.toLocaleLowerCase()}:${relationIdFragment(destinationId)}:${relationIdFragment(sourceId)}`,
          occupiedRelationIds,
        );
        ops.push({
          new_value: { ...payload, dst_id: sourceId, src_id: destinationId },
          op: draft.op,
          op_id: `${opId}-reverse`,
          target: reverseTarget,
        });
      }
      continue;
    }

    if (draft.op === "delete_relation") {
      const target = relationIdFromRef(draft.relationRef);
      if (!target) return incomplete("请选择要删除的关系。");
      ops.push({ op: draft.op, op_id: opId, target });
      continue;
    }

    if (draft.op === "set_relation_attr") {
      const relationId = relationIdFromRef(draft.relationRef);
      const path = draft.attributePath.trim();
      if (!relationId || !path) return incomplete("请选择关系并填写要修改的字段。");
      const parsed = parseValue(draft.newValue);
      if (parsed.error) return incomplete(parsed.error);
      const oldValue = getNested(relationsById.get(relationId)?.attrs, path);
      ops.push({
        new_value: parsed.value,
        ...(oldValue !== undefined ? { old_value: oldValue } : {}),
        op: draft.op,
        op_id: opId,
        target: `${relationId}.${path}`,
      });
      continue;
    }

    const label = businessSlug(draft.subgraphLabel);
    if (!label) return incomplete("替换子图时必须填写这组变更的业务名称。");
    if (draft.subgraphResourceKind === "entity") {
      const selectedId = entityIdFromRef(draft.entityRef, addedEntityIds);
      const selected = selectedId ? entitiesById.get(selectedId) : undefined;
      if (!selected) return incomplete("请选择要写入子图的现有实体。");
      const attributes = compileAttributes(draft.attributes, selected.attrs ?? {});
      if (attributes.error) return incomplete(attributes.error);
      ops.push({
        new_value: { entities: [entityPayload(selected, attributes.attrs)], relations: [] },
        old_value: { entities: { [selected.id]: exactEntityWire(selected) }, relations: {} },
        op: draft.op,
        op_id: opId,
        target: `subgraph:${draft.subgraphLabel.trim()}`,
      });
    } else {
      const selectedId = relationIdFromRef(draft.relationRef);
      const selected = selectedId ? relationsById.get(selectedId) : undefined;
      if (!selected) return incomplete("请选择要写入子图的现有关系。");
      const attributes = compileAttributes(draft.attributes, selected.attrs ?? {});
      if (attributes.error) return incomplete(attributes.error);
      ops.push({
        new_value: { entities: [], relations: [relationPayload(selected, attributes.attrs)] },
        old_value: { entities: {}, relations: { [selected.id]: exactRelationWire(selected) } },
        op: draft.op,
        op_id: opId,
        target: `subgraph:${draft.subgraphLabel.trim()}`,
      });
    }
  }
  return { error: null, ops };
}

function nextRowId(prefix: string, ids: readonly string[]): string {
  const maximum = ids.reduce((current, id) => {
    const match = new RegExp(`^${prefix}-(\\d+)$`, "u").exec(id);
    return match ? Math.max(current, Number(match[1])) : current;
  }, 0);
  return `${prefix}-${maximum + 1}`;
}

function ValueEditor({
  label,
  onChange,
  value,
}: {
  label: string;
  onChange: (value: ValueDraft) => void;
  value: ValueDraft;
}) {
  return (
    <div className="gf-specs__value-editor">
      <label>
        值类型
        <select
          onChange={(event) => {
            const kind = event.target.value as ValueKind;
            onChange({
              kind,
              text: kind === "boolean" ? "true" : kind === "null" ? "" : value.text,
            });
          }}
          value={value.kind}
        >
          <option value="string">文本</option>
          <option value="number">数字</option>
          <option value="boolean">是 / 否</option>
          <option value="null">空值</option>
          <option value="json">对象 / 数组</option>
        </select>
      </label>
      {value.kind === "boolean" ? (
        <label>
          {label}
          <select onChange={(event) => onChange({ ...value, text: event.target.value })} value={value.text}>
            <option value="true">是</option>
            <option value="false">否</option>
          </select>
        </label>
      ) : value.kind === "null" ? (
        <p className="gf-specs__structured-hint">{label}将设为空值。</p>
      ) : value.kind === "json" ? (
        <label>
          {label}
          <textarea
            className="u-mono"
            onChange={(event) => onChange({ ...value, text: event.target.value })}
            placeholder='例如 {"level": 2}'
            rows={3}
            value={value.text}
          />
        </label>
      ) : (
        <label>
          {label}
          <input
            inputMode={value.kind === "number" ? "decimal" : undefined}
            onChange={(event) => onChange({ ...value, text: event.target.value })}
            value={value.text}
          />
        </label>
      )}
    </div>
  );
}

function AttributesEditor({
  onChange,
  rows,
}: {
  onChange: (rows: AttributeDraft[]) => void;
  rows: AttributeDraft[];
}) {
  return (
    <div className="gf-specs__attributes-editor">
      {rows.map((row, index) => (
        <div className="gf-specs__attribute-row" key={row.rowId}>
          <label>
            字段名
            <input
              onChange={(event) =>
                onChange(
                  rows.map((candidate, candidateIndex) =>
                    candidateIndex === index ? { ...candidate, key: event.target.value } : candidate,
                  ),
                )
              }
              placeholder="例如 profession"
              value={row.key}
            />
          </label>
          <ValueEditor
            label="字段值"
            onChange={(value) =>
              onChange(
                rows.map((candidate, candidateIndex) =>
                  candidateIndex === index ? { ...candidate, value } : candidate,
                ),
              )
            }
            value={row.value}
          />
          <button
            aria-label={`删除字段 ${index + 1}`}
            className="gf-secondary-button"
            onClick={() => onChange(rows.filter((_, candidateIndex) => candidateIndex !== index))}
            type="button"
          >
            删除字段
          </button>
        </div>
      ))}
      <button
        className="gf-secondary-button"
        onClick={() =>
          onChange([
            ...rows,
            {
              key: "",
              rowId: nextRowId(
                "attribute",
                rows.map((row) => row.rowId),
              ),
              value: { kind: "string", text: "" },
            },
          ])
        }
        type="button"
      >
        添加字段
      </button>
    </div>
  );
}

function EntitySelect({
  addedEntities,
  entities,
  label,
  onChange,
  value,
}: {
  addedEntities?: readonly StructuredOperationDraft[];
  entities: readonly Entity[];
  label: string;
  onChange: (value: string) => void;
  value: string;
}) {
  return (
    <label>
      {label}
      <select onChange={(event) => onChange(event.target.value)} value={value}>
        <option value="">请选择</option>
        {addedEntities?.map((draft) => (
          <option key={`new:${draft.rowId}`} value={`new:${draft.rowId}`}>
            {draft.entityName.trim() || "未命名新实体"} · 新增的{nodeTypeLabel(draft.entityType)}
          </option>
        ))}
        {entities.map((entity) => (
          <option key={entity.id} value={entityRef(entity.id)}>
            {entityDisplayName(entity)} · {nodeTypeLabel(entity.type)}
          </option>
        ))}
      </select>
    </label>
  );
}

function relationDisplayName(relation: Relation, entitiesById: ReadonlyMap<string, Entity>): string {
  const source = entitiesById.get(relation.src_id);
  const destination = entitiesById.get(relation.dst_id);
  return `${source ? entityDisplayName(source) : "未载入起点"} — ${relationTypeLabel(relation.type)} → ${
    destination ? entityDisplayName(destination) : "未载入终点"
  }`;
}

function RelationSelect({
  entitiesById,
  label,
  onChange,
  relations,
  value,
}: {
  entitiesById: ReadonlyMap<string, Entity>;
  label: string;
  onChange: (value: string) => void;
  relations: readonly Relation[];
  value: string;
}) {
  return (
    <label>
      {label}
      <select onChange={(event) => onChange(event.target.value)} value={value}>
        <option value="">请选择</option>
        {relations.map((relation) => (
          <option key={relation.id} value={relationRef(relation.id)}>
            {relationDisplayName(relation, entitiesById)}
          </option>
        ))}
      </select>
    </label>
  );
}

export function StructuredPatchEditor({
  graphItems,
  onChange,
  operations,
}: {
  graphItems: readonly GraphItem[];
  onChange: (operations: StructuredOperationDraft[]) => void;
  operations: StructuredOperationDraft[];
}) {
  const entities = graphEntities(graphItems);
  const relations = graphRelations(graphItems);
  const entitiesById = new Map(entities.map((entity) => [entity.id, entity]));

  function updateOperation(index: number, values: Partial<StructuredOperationDraft>) {
    onChange(
      operations.map((operation, candidateIndex) =>
        candidateIndex === index ? { ...operation, ...values } : operation,
      ),
    );
  }

  return (
    <section aria-labelledby="structured-patch-title" className="gf-specs__structured-patch">
      <header>
        <div>
          <h3 id="structured-patch-title">变更内容</h3>
          <p>按业务名称选择图谱对象；系统会生成并保留 exact TypedOp，不需要复制任何 ID。</p>
        </div>
        <button
          className="gf-secondary-button"
          onClick={() =>
            onChange([
              ...operations,
              createStructuredOperation(
                nextRowId(
                  "structured-row",
                  operations.map((operation) => operation.rowId),
                ),
              ),
            ])
          }
          type="button"
        >
          添加一项变更
        </button>
      </header>
      <ol className="gf-specs__operation-list">
        {operations.map((operation, index) => {
          const availableAddedEntities = operations
            .slice(0, index)
            .filter((candidate) => candidate.op === "add_entity");
          const selectedEntity = entitiesById.get(entityIdFromRef(operation.entityRef, new Map()) ?? "");
          const selectedRelation = relations.find(
            (relation) => relation.id === relationIdFromRef(operation.relationRef),
          );
          const attributeSuggestions =
            operation.op === "set_relation_attr"
              ? Object.keys(selectedRelation?.attrs ?? {})
              : Object.keys(selectedEntity?.attrs ?? {});
          return (
            <li key={operation.rowId}>
              <fieldset aria-label={`变更 ${index + 1}`}>
                <legend>变更 {index + 1}</legend>
                <div className="gf-specs__operation-heading">
                  <label>
                    操作类型
                    <select
                      onChange={(event) => updateOperation(index, { op: event.target.value as TypedOpKind })}
                      value={operation.op}
                    >
                      {OPERATION_TYPES.map((candidate) => (
                        <option key={candidate.value} value={candidate.value}>
                          {candidate.label}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    aria-label={`删除变更 ${index + 1}`}
                    className="gf-secondary-button"
                    disabled={operations.length === 1}
                    onClick={() =>
                      onChange(operations.filter((_, candidateIndex) => candidateIndex !== index))
                    }
                    type="button"
                  >
                    删除
                  </button>
                </div>

                {operation.op === "add_entity" && (
                  <div className="gf-specs__operation-fields">
                    <label>
                      实体类型
                      <select
                        onChange={(event) =>
                          updateOperation(index, { entityType: event.target.value as NodeType })
                        }
                        value={operation.entityType}
                      >
                        {NODE_TYPES.map((candidate) => (
                          <option key={candidate.value} value={candidate.value}>
                            {candidate.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      实体名称
                      <input
                        onChange={(event) => updateOperation(index, { entityName: event.target.value })}
                        placeholder="例如 林逸"
                        value={operation.entityName}
                      />
                    </label>
                    <AttributesEditor
                      onChange={(attributes) => updateOperation(index, { attributes })}
                      rows={operation.attributes}
                    />
                  </div>
                )}

                {operation.op === "delete_entity" && (
                  <EntitySelect
                    addedEntities={availableAddedEntities}
                    entities={entities}
                    label="要删除的实体"
                    onChange={(entityRefValue) => updateOperation(index, { entityRef: entityRefValue })}
                    value={operation.entityRef}
                  />
                )}

                {operation.op === "set_entity_attr" && (
                  <div className="gf-specs__operation-fields">
                    <EntitySelect
                      addedEntities={availableAddedEntities}
                      entities={entities}
                      label="要修改的实体"
                      onChange={(entityRefValue) => updateOperation(index, { entityRef: entityRefValue })}
                      value={operation.entityRef}
                    />
                    <label>
                      字段
                      <input
                        list={`${operation.rowId}-entity-attributes`}
                        onChange={(event) => updateOperation(index, { attributePath: event.target.value })}
                        placeholder="例如 reward_gold"
                        value={operation.attributePath}
                      />
                      <datalist id={`${operation.rowId}-entity-attributes`}>
                        {attributeSuggestions.map((attribute) => (
                          <option key={attribute} value={attribute} />
                        ))}
                      </datalist>
                    </label>
                    {operation.attributePath.trim() && selectedEntity && (
                      <p className="gf-specs__current-value">
                        当前值：
                        <code>
                          {JSON.stringify(getNested(selectedEntity.attrs, operation.attributePath.trim())) ??
                            "未设置"}
                        </code>
                      </p>
                    )}
                    <ValueEditor
                      label="新值"
                      onChange={(newValue) => updateOperation(index, { newValue })}
                      value={operation.newValue}
                    />
                  </div>
                )}

                {operation.op === "add_relation" && (
                  <div className="gf-specs__operation-fields">
                    <label>
                      关系类型
                      <select
                        onChange={(event) =>
                          updateOperation(index, { relationType: event.target.value as EdgeType })
                        }
                        value={operation.relationType}
                      >
                        {RELATION_TYPES.map((candidate) => (
                          <option key={candidate.value} value={candidate.value}>
                            {candidate.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <EntitySelect
                      addedEntities={availableAddedEntities}
                      entities={entities}
                      label="起点"
                      onChange={(sourceEntityRef) => updateOperation(index, { sourceEntityRef })}
                      value={operation.sourceEntityRef}
                    />
                    <EntitySelect
                      addedEntities={availableAddedEntities}
                      entities={entities}
                      label="终点"
                      onChange={(destinationEntityRef) => updateOperation(index, { destinationEntityRef })}
                      value={operation.destinationEntityRef}
                    />
                    <label>
                      关系名称（可选）
                      <input
                        onChange={(event) => updateOperation(index, { relationLabel: event.target.value })}
                        placeholder="例如 好友"
                        value={operation.relationLabel}
                      />
                    </label>
                    <label className="gf-cluster">
                      <input
                        checked={operation.bidirectional}
                        onChange={(event) => updateOperation(index, { bidirectional: event.target.checked })}
                        type="checkbox"
                      />
                      同时创建反向关系
                    </label>
                    <AttributesEditor
                      onChange={(attributes) => updateOperation(index, { attributes })}
                      rows={operation.attributes}
                    />
                  </div>
                )}

                {operation.op === "delete_relation" && (
                  <RelationSelect
                    entitiesById={entitiesById}
                    label="要删除的关系"
                    onChange={(relationRefValue) => updateOperation(index, { relationRef: relationRefValue })}
                    relations={relations}
                    value={operation.relationRef}
                  />
                )}

                {operation.op === "set_relation_attr" && (
                  <div className="gf-specs__operation-fields">
                    <RelationSelect
                      entitiesById={entitiesById}
                      label="要修改的关系"
                      onChange={(relationRefValue) =>
                        updateOperation(index, { relationRef: relationRefValue })
                      }
                      relations={relations}
                      value={operation.relationRef}
                    />
                    <label>
                      字段
                      <input
                        list={`${operation.rowId}-relation-attributes`}
                        onChange={(event) => updateOperation(index, { attributePath: event.target.value })}
                        placeholder="例如 trust"
                        value={operation.attributePath}
                      />
                      <datalist id={`${operation.rowId}-relation-attributes`}>
                        {attributeSuggestions.map((attribute) => (
                          <option key={attribute} value={attribute} />
                        ))}
                      </datalist>
                    </label>
                    {operation.attributePath.trim() && selectedRelation && (
                      <p className="gf-specs__current-value">
                        当前值：
                        <code>
                          {JSON.stringify(
                            getNested(selectedRelation.attrs, operation.attributePath.trim()),
                          ) ?? "未设置"}
                        </code>
                      </p>
                    )}
                    <ValueEditor
                      label="新值"
                      onChange={(newValue) => updateOperation(index, { newValue })}
                      value={operation.newValue}
                    />
                  </div>
                )}

                {operation.op === "replace_subgraph" && (
                  <div className="gf-specs__operation-fields">
                    <label>
                      这组变更的名称
                      <input
                        onChange={(event) => updateOperation(index, { subgraphLabel: event.target.value })}
                        placeholder="例如 前哨任务奖励"
                        value={operation.subgraphLabel}
                      />
                    </label>
                    <label>
                      对象类型
                      <select
                        onChange={(event) =>
                          updateOperation(index, {
                            subgraphResourceKind: event.target.value as "entity" | "relation",
                          })
                        }
                        value={operation.subgraphResourceKind}
                      >
                        <option value="entity">实体</option>
                        <option value="relation">关系</option>
                      </select>
                    </label>
                    {operation.subgraphResourceKind === "entity" ? (
                      <EntitySelect
                        entities={entities}
                        label="要写入子图的实体"
                        onChange={(entityRefValue) => updateOperation(index, { entityRef: entityRefValue })}
                        value={operation.entityRef}
                      />
                    ) : (
                      <RelationSelect
                        entitiesById={entitiesById}
                        label="要写入子图的关系"
                        onChange={(relationRefValue) =>
                          updateOperation(index, { relationRef: relationRefValue })
                        }
                        relations={relations}
                        value={operation.relationRef}
                      />
                    )}
                    <p className="gf-specs__structured-hint">
                      保留所选对象的完整字段，并用下面填写的字段覆盖；需要批量 payload 时可使用高级入口。
                    </p>
                    <AttributesEditor
                      onChange={(attributes) => updateOperation(index, { attributes })}
                      rows={operation.attributes}
                    />
                  </div>
                )}
              </fieldset>
            </li>
          );
        })}
      </ol>
      <p className="gf-specs__structured-hint">
        下拉选项来自上方已加载的有界图谱；找不到对象时，先在图谱中加载下一页。
      </p>
    </section>
  );
}
