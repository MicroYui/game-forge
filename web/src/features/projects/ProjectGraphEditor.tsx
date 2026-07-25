import { Plus, RotateCcw, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { KnowledgeGraph } from "../../components/kg";
import { TechnicalDetails } from "../../components/identity";
import {
  canonicalAttributeKeyPreview,
  edgeTypes,
  nodeTypes,
  toGraphItems,
  type ProjectEdgeType,
  type ProjectNodeType,
  type ProjectEntity,
  type ProjectGraphDraft,
  type ProjectRelation,
} from "./model";

const typeLabels: Readonly<Partial<Record<ProjectNodeType, string>>> = {
  BATTLE_ENCOUNTER: "战斗遭遇",
  CHARACTER: "角色",
  CURRENCY: "货币",
  DIALOGUE_NODE: "对话节点",
  EQUIPMENT: "装备",
  EVENT: "事件",
  FACTION: "阵营",
  GACHA_POOL: "卡池",
  ITEM: "道具",
  MONSTER: "怪物",
  NPC: "非玩家角色",
  QUEST: "任务",
  QUEST_STEP: "任务步骤",
  REGION: "区域",
  SHOP: "商店",
  SKILL: "技能",
};

const relationLabels: Readonly<Partial<Record<ProjectEdgeType, string>>> = {
  BELONGS_TO: "隶属于",
  CONTAINS: "包含",
  DROPS_FROM: "产出掉落",
  GRANTS: "赋予",
  HAS_STEP: "包含步骤",
  LOCATED_IN: "位于",
  PATH_TO: "通往",
  PRECEDES: "前置于",
  REFERENCES: "引用",
  REQUIRES: "需要",
  REWARDS: "奖励",
  SELLS: "出售",
  SPAWNS: "生成",
  TALKS_TO: "对话对象",
  UNLOCKS: "解锁",
};

function entityName(entity: ProjectEntity): string {
  const candidate = entity.attrs?.display_name ?? entity.attrs?.name ?? entity.attrs?.title;
  return typeof candidate === "string" && candidate.trim()
    ? candidate
    : `未命名${typeLabels[entity.type] ?? "内容"}`;
}

function entityEditableName(entity: ProjectEntity): string {
  const candidate = entity.attrs?.display_name ?? entity.attrs?.name ?? entity.attrs?.title;
  return typeof candidate === "string" ? candidate : "";
}

function nextId(prefix: string, ids: ReadonlySet<string>): string {
  for (let index = 1; index < 100_000; index += 1) {
    const candidate = `${prefix}:new_${index}`;
    if (!ids.has(candidate)) return candidate;
  }
  throw new Error("无法分配新的内容标识。");
}

function selectedEntity(value: ProjectGraphDraft, key: string | null): ProjectEntity | null {
  if (!key?.startsWith("entity:")) return null;
  return value.entities.find((entity) => `entity:${entity.id}` === key) ?? null;
}

function selectedRelation(value: ProjectGraphDraft, key: string | null): ProjectRelation | null {
  if (!key?.startsWith("relation:")) return null;
  return value.relations.find((relation) => `relation:${relation.id}` === key) ?? null;
}

function eventAvailability(entity: ProjectEntity | null): Record<string, unknown> | null {
  const candidate = entity?.attrs?.availability;
  return typeof candidate === "object" && candidate !== null && !Array.isArray(candidate)
    ? (candidate as Record<string, unknown>)
    : null;
}

function isLimitedEventOwner(entity: ProjectEntity | null): boolean {
  if (entity?.type !== "EVENT" || entity.attrs?.scope_kind !== "event") return false;
  return (
    entity.attrs.scope_role === "owner" || !entity.attrs.scope_owner_id || eventAvailability(entity) !== null
  );
}

function zonedParts(date: Date, timeZone: string): Record<string, number> {
  const formatter = new Intl.DateTimeFormat("en-CA", {
    day: "2-digit",
    hour: "2-digit",
    hourCycle: "h23",
    minute: "2-digit",
    month: "2-digit",
    second: "2-digit",
    timeZone,
    year: "numeric",
  });
  return Object.fromEntries(
    formatter
      .formatToParts(date)
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, Number(part.value)]),
  );
}

function localDateTimeParts(value: string): [number, number, number, number, number] {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/u.exec(value);
  if (!match) throw new Error("请填写完整的日期和时间。");
  return [Number(match[1]), Number(match[2]), Number(match[3]), Number(match[4]), Number(match[5])];
}

function zonedLocalToIso(value: string, timeZone: string): string {
  const [year, month, day, hour, minute] = localDateTimeParts(value);
  const desiredWallTime = Date.UTC(year, month - 1, day, hour, minute, 0);
  let instant = desiredWallTime;
  for (let iteration = 0; iteration < 3; iteration += 1) {
    const parts = zonedParts(new Date(instant), timeZone);
    const renderedWallTime = Date.UTC(
      parts.year!,
      parts.month! - 1,
      parts.day!,
      parts.hour!,
      parts.minute!,
      parts.second!,
    );
    instant += desiredWallTime - renderedWallTime;
  }
  const roundTrip = zonedParts(new Date(instant), timeZone);
  if (
    roundTrip.year !== year ||
    roundTrip.month !== month ||
    roundTrip.day !== day ||
    roundTrip.hour !== hour ||
    roundTrip.minute !== minute
  ) {
    throw new Error("这个当地时间在所选时区中不存在，请避开夏令时切换时刻。");
  }
  const renderedWallTime = Date.UTC(
    roundTrip.year!,
    roundTrip.month! - 1,
    roundTrip.day!,
    roundTrip.hour!,
    roundTrip.minute!,
    roundTrip.second!,
  );
  const offsetMinutes = Math.round((renderedWallTime - instant) / 60_000);
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const absoluteOffset = Math.abs(offsetMinutes);
  const offset = `${sign}${String(Math.floor(absoluteOffset / 60)).padStart(2, "0")}:${String(
    absoluteOffset % 60,
  ).padStart(2, "0")}`;
  return `${value}:00${offset}`;
}

function timestampToLocalInput(value: unknown, timeZone: string): string {
  if (typeof value !== "string") return "";
  const instant = new Date(value);
  if (Number.isNaN(instant.getTime())) return "";
  const parts = zonedParts(instant, timeZone);
  return `${String(parts.year).padStart(4, "0")}-${String(parts.month).padStart(2, "0")}-${String(
    parts.day,
  ).padStart(2, "0")}T${String(parts.hour).padStart(2, "0")}:${String(parts.minute).padStart(2, "0")}`;
}

function addLocalDays(value: string, days: number): string {
  const [year, month, day, hour, minute] = localDateTimeParts(value);
  const shifted = new Date(Date.UTC(year, month - 1, day + days, hour, minute));
  return `${String(shifted.getUTCFullYear()).padStart(4, "0")}-${String(shifted.getUTCMonth() + 1).padStart(
    2,
    "0",
  )}-${String(shifted.getUTCDate()).padStart(2, "0")}T${String(shifted.getUTCHours()).padStart(
    2,
    "0",
  )}:${String(shifted.getUTCMinutes()).padStart(2, "0")}`;
}

export function ProjectGraphEditor({
  focusRequest = null,
  onChange,
  value,
}: {
  focusRequest?: { entityId: string; requestId: number } | null;
  onChange(value: ProjectGraphDraft): void;
  value: ProjectGraphDraft;
}) {
  const graphItems = useMemo(() => toGraphItems(value), [value]);
  const [selectedKey, setSelectedKey] = useState<string | null>(() =>
    graphItems[0] ? `${graphItems[0].item_kind}:${graphItems[0].item_id}` : null,
  );
  const [history, setHistory] = useState<ProjectGraphDraft[]>([]);
  const [attributeName, setAttributeName] = useState("");
  const [attributeValue, setAttributeValue] = useState("");
  const [advancedAttrs, setAdvancedAttrs] = useState("{}");
  const [advancedError, setAdvancedError] = useState<string | null>(null);
  const [eventStart, setEventStart] = useState("");
  const [eventGameplayEnd, setEventGameplayEnd] = useState("");
  const [eventClaimEnd, setEventClaimEnd] = useState("");
  const [eventTimezone, setEventTimezone] = useState("Asia/Shanghai");
  const [eventScheduleMessage, setEventScheduleMessage] = useState<string | null>(null);
  const [eventScheduleError, setEventScheduleError] = useState<string | null>(null);
  const eventScheduleRef = useRef<HTMLElement>(null);
  const eventStartInputRef = useRef<HTMLInputElement>(null);
  const handledFocusRequest = useRef<number | null>(null);
  const scrolledFocusRequest = useRef<number | null>(null);
  const entity = selectedEntity(value, selectedKey);
  const relation = selectedRelation(value, selectedKey);
  const selected = entity ?? relation;

  useEffect(() => {
    if (selectedKey && graphItems.some((item) => `${item.item_kind}:${item.item_id}` === selectedKey)) return;
    const first = graphItems[0];
    setSelectedKey(first ? `${first.item_kind}:${first.item_id}` : null);
  }, [graphItems, selectedKey]);

  useEffect(() => {
    if (!focusRequest || handledFocusRequest.current === focusRequest.requestId) return;
    handledFocusRequest.current = focusRequest.requestId;
    if (value.entities.some((item) => item.id === focusRequest.entityId)) {
      setSelectedKey(`entity:${focusRequest.entityId}`);
    }
  }, [focusRequest, value.entities]);

  useEffect(() => {
    if (
      !focusRequest ||
      scrolledFocusRequest.current === focusRequest.requestId ||
      entity?.id !== focusRequest.entityId ||
      !isLimitedEventOwner(entity)
    ) {
      return;
    }
    scrolledFocusRequest.current = focusRequest.requestId;
    eventScheduleRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    eventStartInputRef.current?.focus();
  }, [entity, focusRequest]);

  useEffect(() => {
    setAdvancedAttrs(JSON.stringify(selected?.attrs ?? {}, null, 2));
    setAdvancedError(null);
    setAttributeName("");
    setAttributeValue("");
  }, [selectedKey, selected?.attrs]);

  useEffect(() => {
    const availability = eventAvailability(entity);
    const timezone = typeof availability?.timezone === "string" ? availability.timezone : "Asia/Shanghai";
    setEventTimezone(timezone);
    setEventStart(timestampToLocalInput(availability?.start_at, timezone));
    setEventGameplayEnd(timestampToLocalInput(availability?.gameplay_end_at, timezone));
    setEventClaimEnd(timestampToLocalInput(availability?.reward_claim_end_at, timezone));
    setEventScheduleMessage(null);
    setEventScheduleError(null);
  }, [selectedKey]);

  function commit(next: ProjectGraphDraft, nextSelection: string | null = selectedKey) {
    setHistory((current) => [...current.slice(-49), structuredClone(value)]);
    onChange(next);
    setSelectedKey(nextSelection);
  }

  function addEntity() {
    const id = nextId("npc", new Set(value.entities.map((item) => item.id)));
    const next: ProjectEntity = {
      attrs: { display_name: "新角色" },
      id,
      schema_version: "ir-core@1",
      source_ref: null,
      tags: [],
      type: "NPC",
    };
    commit({ ...value, entities: [...value.entities, next] }, `entity:${id}`);
  }

  function addRelation() {
    if (value.entities.length < 2) return;
    const id = nextId("rel", new Set(value.relations.map((item) => item.id)));
    const next: ProjectRelation = {
      attrs: {},
      dst_id: value.entities[1]!.id,
      id,
      schema_version: "ir-core@1",
      source_ref: null,
      src_id: value.entities[0]!.id,
      type: "REFERENCES",
    };
    commit({ ...value, relations: [...value.relations, next] }, `relation:${id}`);
  }

  function updateEntity(updates: Partial<ProjectEntity>) {
    if (!entity) return;
    commit({
      ...value,
      entities: value.entities.map((item) => (item.id === entity.id ? { ...item, ...updates } : item)),
    });
  }

  function updateRelation(updates: Partial<ProjectRelation>) {
    if (!relation) return;
    commit({
      ...value,
      relations: value.relations.map((item) => (item.id === relation.id ? { ...item, ...updates } : item)),
    });
  }

  function removeSelected() {
    if (entity) {
      commit(
        {
          entities: value.entities.filter((item) => item.id !== entity.id),
          relations: value.relations.filter((item) => item.src_id !== entity.id && item.dst_id !== entity.id),
        },
        null,
      );
    } else if (relation) {
      commit({ ...value, relations: value.relations.filter((item) => item.id !== relation.id) }, null);
    }
  }

  function undo() {
    const previous = history[history.length - 1];
    if (!previous) return;
    setHistory((current) => current.slice(0, -1));
    onChange(previous);
  }

  function addAttribute() {
    if (!selected) return;
    const key = canonicalAttributeKeyPreview(attributeName);
    if (!key) return;
    const nextAttrs = { ...(selected.attrs ?? {}), [key]: attributeValue };
    if (entity) updateEntity({ attrs: nextAttrs });
    else updateRelation({ attrs: nextAttrs });
    setAttributeName("");
    setAttributeValue("");
  }

  function saveAdvancedAttrs() {
    try {
      const parsed: unknown = JSON.parse(advancedAttrs);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new TypeError("属性必须是一个 JSON object。");
      }
      setAdvancedError(null);
      if (entity) updateEntity({ attrs: parsed as Record<string, unknown> });
      else if (relation) updateRelation({ attrs: parsed as Record<string, unknown> });
    } catch (error) {
      setAdvancedError(error instanceof Error ? error.message : "属性 JSON 无法解析。");
    }
  }

  function updateEventStart(value: string) {
    setEventStart(value);
    const availability = eventAvailability(entity);
    const duration = availability?.duration_days;
    const grace = availability?.reward_claim_grace_days;
    if (
      value &&
      typeof duration === "number" &&
      Number.isInteger(duration) &&
      duration > 0 &&
      typeof grace === "number" &&
      Number.isInteger(grace) &&
      grace >= 0
    ) {
      setEventGameplayEnd(addLocalDays(value, duration));
      setEventClaimEnd(addLocalDays(value, duration + grace));
    }
  }

  function saveEventSchedule() {
    if (!entity || !isLimitedEventOwner(entity)) return;
    setEventScheduleMessage(null);
    setEventScheduleError(null);
    try {
      if (!eventStart || !eventGameplayEnd || !eventClaimEnd || !eventTimezone.trim()) {
        throw new Error("请填写活动开始、玩法结束、奖励兑换截止和时区。");
      }
      new Intl.DateTimeFormat("zh-CN", { timeZone: eventTimezone.trim() }).format();
      const startAt = zonedLocalToIso(eventStart, eventTimezone.trim());
      const gameplayEndAt = zonedLocalToIso(eventGameplayEnd, eventTimezone.trim());
      const rewardClaimEndAt = zonedLocalToIso(eventClaimEnd, eventTimezone.trim());
      if (Date.parse(gameplayEndAt) <= Date.parse(startAt)) {
        throw new Error("玩法结束时间必须晚于活动开始时间。");
      }
      if (Date.parse(rewardClaimEndAt) < Date.parse(gameplayEndAt)) {
        throw new Error("奖励兑换截止不能早于玩法结束时间。");
      }
      updateEntity({
        attrs: {
          ...(entity.attrs ?? {}),
          availability: {
            availability_schema_version: "event-availability@1",
            expiration_policy: "hide_from_active_content",
            gameplay_end_at: gameplayEndAt,
            reward_claim_end_at: rewardClaimEndAt,
            schedule_kind: "absolute",
            start_at: startAt,
            timezone: eventTimezone.trim(),
          },
          scope_kind: "event",
          scope_role: "owner",
        },
      });
      setEventScheduleMessage("活动档期已保存；到期后将从当前内容中隐藏。");
    } catch (error) {
      setEventScheduleError(error instanceof Error ? error.message : "活动档期无法保存。");
    }
  }

  const canonicalKey = canonicalAttributeKeyPreview(attributeName);

  return (
    <section className="gf-project-graph-editor" aria-label="实体与关系编辑器">
      <header className="gf-project-graph-editor__header">
        <div>
          <p className="u-kicker">可编辑内容草案</p>
          <h2>实体与关系</h2>
          <p>先在图中理解关系，再用右侧表单修改；系统标识会自动生成。</p>
        </div>
        <div className="gf-project-graph-editor__actions">
          <button className="gf-secondary-button" onClick={addEntity} type="button">
            <Plus aria-hidden="true" size={16} />
            添加实体
          </button>
          <button
            className="gf-secondary-button"
            disabled={value.entities.length < 2}
            onClick={addRelation}
            title={value.entities.length < 2 ? "至少需要两个实体" : undefined}
            type="button"
          >
            <Plus aria-hidden="true" size={16} />
            添加关系
          </button>
          <button
            aria-label="撤销上一步"
            className="gf-secondary-button"
            disabled={history.length === 0}
            onClick={undo}
            type="button"
          >
            <RotateCcw aria-hidden="true" size={16} />
            撤销
          </button>
        </div>
      </header>

      <KnowledgeGraph
        ariaLabel="可编辑项目知识图谱"
        items={graphItems}
        onSelectedFactKeyChange={setSelectedKey}
        pageLabel="当前内容草案"
        selectedFactKey={selectedKey}
      />

      <section className="gf-project-graph-editor__inspector" aria-label="编辑已选内容">
        {!selected && <p>从图或清单选择一项内容后即可编辑。</p>}
        {entity && (
          <>
            <header>
              <div>
                <p className="u-kicker">实体</p>
                <h3>{entityName(entity)}</h3>
              </div>
              <button className="gf-danger-button" onClick={removeSelected} type="button">
                <Trash2 aria-hidden="true" size={15} />
                删除实体
              </button>
            </header>
            <div className="gf-form gf-project-graph-editor__fields">
              <label>
                内容名称
                <input
                  maxLength={256}
                  onChange={(event) =>
                    updateEntity({ attrs: { ...(entity.attrs ?? {}), display_name: event.target.value } })
                  }
                  value={entityEditableName(entity)}
                />
              </label>
              <label>
                内容类型
                <select
                  onChange={(event) => updateEntity({ type: event.target.value as ProjectNodeType })}
                  value={entity.type}
                >
                  {nodeTypes.map((type) => (
                    <option key={type} value={type}>
                      {typeLabels[type] ?? type}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                标签（用逗号分隔）
                <input
                  onChange={(event) =>
                    updateEntity({
                      tags: event.target.value
                        .split(/[,，]/u)
                        .map((item) => item.trim())
                        .filter(Boolean),
                    })
                  }
                  value={(entity.tags ?? []).join("，")}
                />
              </label>
            </div>
          </>
        )}
        {relation && (
          <>
            <header>
              <div>
                <p className="u-kicker">关系</p>
                <h3>{relationLabels[relation.type] ?? relation.type}</h3>
              </div>
              <button className="gf-danger-button" onClick={removeSelected} type="button">
                <Trash2 aria-hidden="true" size={15} />
                删除关系
              </button>
            </header>
            <div className="gf-form gf-project-graph-editor__fields">
              <label>
                关系类型
                <select
                  onChange={(event) => updateRelation({ type: event.target.value as ProjectEdgeType })}
                  value={relation.type}
                >
                  {edgeTypes.map((type) => (
                    <option key={type} value={type}>
                      {relationLabels[type] ?? type}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                起点内容
                <select
                  onChange={(event) => updateRelation({ src_id: event.target.value })}
                  value={relation.src_id}
                >
                  {value.entities.map((item) => (
                    <option key={item.id} value={item.id}>
                      {entityName(item)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                终点内容
                <select
                  onChange={(event) => updateRelation({ dst_id: event.target.value })}
                  value={relation.dst_id}
                >
                  {value.entities.map((item) => (
                    <option key={item.id} value={item.id}>
                      {entityName(item)}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          </>
        )}

        {entity && isLimitedEventOwner(entity) && (
          <section
            className="gf-project-graph-editor__event-schedule"
            aria-labelledby="event-schedule-title"
            id="project-event-schedule"
            ref={eventScheduleRef}
          >
            <header>
              <div>
                <p className="u-kicker">限时活动</p>
                <h4 id="event-schedule-title">活动档期</h4>
              </div>
              <span className="u-chip">到期后隐藏，不删除历史</span>
            </header>
            {eventAvailability(entity)?.schedule_kind === "relative" && (
              <p className="gf-project-graph-editor__schedule-note">
                材料写明：活动持续 {String(eventAvailability(entity)?.duration_days)} 天，结束后可领奖{" "}
                {String(eventAvailability(entity)?.reward_claim_grace_days)} 天。
              </p>
            )}
            <div className="gf-form gf-project-graph-editor__schedule-fields">
              <label>
                活动开始时间
                <input
                  onChange={(event) => updateEventStart(event.target.value)}
                  ref={eventStartInputRef}
                  type="datetime-local"
                  value={eventStart}
                />
              </label>
              <label>
                玩法结束时间
                <input
                  onChange={(event) => setEventGameplayEnd(event.target.value)}
                  type="datetime-local"
                  value={eventGameplayEnd}
                />
              </label>
              <label>
                奖励兑换截止
                <input
                  onChange={(event) => setEventClaimEnd(event.target.value)}
                  type="datetime-local"
                  value={eventClaimEnd}
                />
              </label>
              <label>
                活动时区
                <input
                  list="gameforge-event-timezones"
                  onChange={(event) => setEventTimezone(event.target.value)}
                  value={eventTimezone}
                />
                <datalist id="gameforge-event-timezones">
                  <option value="Asia/Shanghai" />
                  <option value="Asia/Tokyo" />
                  <option value="Europe/London" />
                  <option value="America/Los_Angeles" />
                  <option value="UTC" />
                </datalist>
              </label>
            </div>
            <div className="gf-project-graph-editor__schedule-actions">
              <button className="gf-secondary-button" onClick={saveEventSchedule} type="button">
                保存活动档期
              </button>
              <p>填写开始时间后，系统会按材料中的持续天数自动计算结束与领奖截止。</p>
            </div>
            {eventScheduleError && <p role="alert">{eventScheduleError}</p>}
            {eventScheduleMessage && <p role="status">{eventScheduleMessage}</p>}
          </section>
        )}

        {selected && (
          <div className="gf-project-graph-editor__attributes">
            <h4>补充属性</h4>
            <div className="gf-project-graph-editor__attribute-list">
              {Object.keys(selected.attrs ?? {})
                .filter((key) => key !== "display_name")
                .map((key) => (
                  <span className="u-chip" key={key}>
                    {key}
                  </span>
                ))}
            </div>
            <div className="gf-project-graph-editor__new-attribute">
              <label>
                新属性名称
                <input onChange={(event) => setAttributeName(event.target.value)} value={attributeName} />
              </label>
              <label>
                新属性值
                <input onChange={(event) => setAttributeValue(event.target.value)} value={attributeValue} />
              </label>
              <button disabled={!canonicalKey} onClick={addAttribute} type="button">
                添加属性
              </button>
            </div>
            {attributeName && (
              <p className="gf-project-graph-editor__canonical-preview" role="status">
                系统将保存为 <strong>{canonicalKey || "无效属性名"}</strong>；例如 air.quality 与 air_quality
                会归为同一项。
              </p>
            )}
            <details>
              <summary>高级：编辑全部属性 JSON</summary>
              <label>
                属性 JSON
                <textarea
                  className="gf-code-input"
                  onChange={(event) => setAdvancedAttrs(event.target.value)}
                  rows={8}
                  value={advancedAttrs}
                />
              </label>
              {advancedError && <p role="alert">{advancedError}</p>}
              <button className="gf-secondary-button" onClick={saveAdvancedAttrs} type="button">
                应用 JSON
              </button>
            </details>
            <TechnicalDetails
              items={[{ label: selected === entity ? "实体标识" : "关系标识", value: selected.id }]}
              summary="查看系统标识"
            />
          </div>
        )}
      </section>
    </section>
  );
}
