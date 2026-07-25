import type { components } from "../../api/generated/openapi";
import { useId } from "react";
import { TechnicalDetails } from "../identity";
import { JsonValueStateView } from "./JsonValueStateView";
import "./diff.css";

type SnapshotDiff = components["schemas"]["SnapshotDiff"];
type SnapshotDiffEntry = components["schemas"]["SnapshotDiffEntry"];

const pathLabels: Record<string, string> = {
  attrs: "属性",
  cost: "消耗",
  description: "描述",
  economy: "经济系统",
  entities: "内容",
  gold: "金币",
  relations: "关系",
  reward: "奖励",
  title: "名称",
};

const entityTypeLabels: Record<string, string> = {
  battle_encounter: "战斗遭遇",
  character: "角色",
  currency: "货币",
  dialogue_node: "对话节点",
  drop_table: "掉落表",
  effect: "效果",
  equipment: "装备",
  event: "事件",
  faction: "阵营",
  formula: "公式",
  gacha_pool: "卡池",
  interactable: "交互物",
  item: "道具",
  monster: "怪物",
  npc: "角色",
  quest: "任务",
  quest_step: "任务步骤",
  region: "区域",
  reward: "奖励",
  reward_table: "奖励表",
  shop: "商店",
  skill: "技能",
  spawn_point: "生成点",
  status_effect: "状态效果",
  unlock_condition: "解锁条件",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function plannerFacingName(state: SnapshotDiffEntry["before"]): string | null {
  if (state.presence !== "present" || !isRecord(state.value)) return null;
  const attrs = isRecord(state.value.attrs) ? state.value.attrs : null;
  for (const key of ["display_name", "name", "title", "label"] as const) {
    const candidate = attrs?.[key] ?? state.value[key];
    if (typeof candidate === "string" && candidate.trim()) return candidate.trim();
  }
  return null;
}

function decodePointerToken(value: string): string {
  return value.replace(/~1/gu, "/").replace(/~0/gu, "~");
}

export function friendlyTechnicalPath(path: string): string {
  const parts = path
    .split("/")
    .slice(1)
    .map(decodePointerToken)
    .filter((part) => part !== "attrs" && part !== "entities");
  return parts
    .map((part) => {
      const [prefix, identity] = part.split(":", 2);
      if (identity && entityTypeLabels[prefix]) return `${entityTypeLabels[prefix]} ${identity}`;
      return pathLabels[part] ?? part.replace(/[-_]+/gu, " ");
    })
    .join(" / ");
}

function friendlyEntryPath(entry: SnapshotDiffEntry): string {
  const parts = entry.path.split("/").slice(1).map(decodePointerToken);
  if (parts.length === 2 && parts[0] === "entities") {
    const [prefix] = parts[1]!.split(":", 1);
    const name = plannerFacingName(entry.after) ?? plannerFacingName(entry.before);
    if (name) return `${entityTypeLabels[prefix] ?? "内容"} ${name}`;
  }
  return friendlyTechnicalPath(entry.path);
}

export function SnapshotDiffView({
  diff,
  entries,
}: {
  diff: SnapshotDiff;
  entries: readonly SnapshotDiffEntry[];
}) {
  const headingId = useId();
  return (
    <section className="gf-diff" aria-labelledby={headingId}>
      <header className="gf-diff__header">
        <div>
          <h2 id={headingId}>修改内容</h2>
          <p>
            本页显示 {entries.length} / 共 {diff.entry_count} 项具体变化。
          </p>
          <p>
            <span>修改前版本</span> → <span>修改后版本</span>
          </p>
        </div>
        <TechnicalDetails
          items={[
            { label: "修改前版本标识", value: diff.base_snapshot_id },
            { label: "修改后版本标识", value: diff.target_snapshot_id },
          ]}
          summary="版本技术信息"
        />
      </header>
      <div className="gf-diff__scroll" tabIndex={0}>
        <table>
          <caption className="u-sr-only">修改前后的字段差异</caption>
          <thead>
            <tr>
              <th scope="col">修改位置</th>
              <th scope="col">修改前</th>
              <th scope="col">修改后</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry.path}>
                <th scope="row">
                  <strong>{friendlyEntryPath(entry)}</strong>
                  <TechnicalDetails items={[{ label: "字段路径", value: entry.path }]} summary="字段定位" />
                </th>
                <td>
                  <JsonValueStateView state={entry.before} />
                </td>
                <td>
                  <JsonValueStateView state={entry.after} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
