import { useId, useMemo, useState } from "react";

import type { components } from "../../api/generated/openapi";
import { TechnicalDetails } from "../identity";
import { JsonValueStateView } from "./JsonValueStateView";
import { friendlyTechnicalPath } from "./SnapshotDiffView";
import "./diff.css";

type ConflictResolution = components["schemas"]["ConflictResolution"];
type MergeConflict = components["schemas"]["MergeConflict"];
type ResolutionChoice = ConflictResolution["choice"];

interface ResolutionDraft {
  choice?: ResolutionChoice;
  customText: string;
  error?: string;
}

const choiceLabels: Record<ResolutionChoice, string> = {
  custom: "自己填写一个值（高级）",
  keep_current: "保留当前正式内容",
  take_proposed: "采用这份草案的修改",
};

const choiceOrder: readonly ResolutionChoice[] = ["keep_current", "take_proposed", "custom"];

function parseCustomValue(text: string): { error?: string; value?: unknown } {
  if (text.trim() === "") return {};
  try {
    return { value: JSON.parse(text) as unknown };
  } catch {
    return { error: "请输入有效 JSON" };
  }
}

function buildResolutions(
  conflicts: readonly MergeConflict[],
  drafts: Readonly<Record<string, ResolutionDraft>>,
): ConflictResolution[] {
  const resolutions: ConflictResolution[] = [];
  for (const conflict of conflicts) {
    const draft = drafts[conflict.id];
    if (!draft?.choice) continue;
    if (draft.choice === "custom") {
      const parsed = parseCustomValue(draft.customText);
      if (parsed.error || parsed.value === undefined) continue;
      resolutions.push({
        choice: "custom",
        conflict_id: conflict.id,
        custom_value: parsed.value,
      });
      continue;
    }
    resolutions.push({ choice: draft.choice, conflict_id: conflict.id });
  }
  return resolutions;
}

export function MergeResolver({
  conflicts,
  onResolutionsChange,
}: {
  conflicts: readonly MergeConflict[];
  onResolutionsChange(resolutions: ConflictResolution[]): void;
}) {
  const instanceId = useId();
  const [drafts, setDrafts] = useState<Record<string, ResolutionDraft>>({});
  const completedCount = useMemo(() => buildResolutions(conflicts, drafts).length, [conflicts, drafts]);

  function commit(next: Record<string, ResolutionDraft>) {
    setDrafts(next);
    onResolutionsChange(buildResolutions(conflicts, next));
  }

  function selectChoice(conflict: MergeConflict, choice: ResolutionChoice) {
    const current = drafts[conflict.id] ?? { customText: "" };
    const parsed = choice === "custom" ? parseCustomValue(current.customText) : {};
    commit({
      ...drafts,
      [conflict.id]: {
        ...current,
        choice,
        error: parsed.error,
      },
    });
  }

  function updateCustom(conflict: MergeConflict, customText: string) {
    const parsed = parseCustomValue(customText);
    commit({
      ...drafts,
      [conflict.id]: {
        choice: "custom",
        customText,
        error: parsed.error,
      },
    });
  }

  return (
    <section className="gf-merge" aria-labelledby={`${instanceId}-heading`}>
      <header className="gf-merge__header">
        <div>
          <h2 id={`${instanceId}-heading`}>逐项处理内容冲突</h2>
          <p>每项都需要你亲自选择；系统和 AI 不会替你覆盖正式内容。</p>
        </div>
        <p aria-live="polite">
          已处理 {completedCount} / {conflicts.length}
        </p>
      </header>

      <div className="gf-merge__items">
        {conflicts.map((conflict, index) => {
          const draft = drafts[conflict.id] ?? { customText: "" };
          const customInputId = `${instanceId}-custom-${index}`;
          const radioName = `${instanceId}-resolution-${index}`;
          return (
            <article className="gf-merge-conflict" key={conflict.id}>
              <header>
                <div>
                  <h3>{friendlyTechnicalPath(conflict.path)}</h3>
                  <TechnicalDetails
                    items={[
                      { label: "字段路径", value: conflict.path },
                      { label: "冲突类型", value: conflict.kind },
                      { label: "冲突标识", value: conflict.id },
                    ]}
                    summary="查看冲突技术信息"
                  />
                </div>
                <span className="u-status">
                  {buildResolutions([conflict], { [conflict.id]: draft }).length > 0 ? "已选择" : "等待选择"}
                </span>
              </header>

              <div className="gf-merge-conflict__scroll" tabIndex={0}>
                <table>
                  <caption className="u-sr-only">
                    {friendlyTechnicalPath(conflict.path)} 的冲突内容对比
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">草案创建时</th>
                      <th scope="col">当前正式内容</th>
                      <th scope="col">草案建议</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td>
                        <JsonValueStateView state={conflict.base} />
                      </td>
                      <td>
                        <JsonValueStateView state={conflict.current} />
                      </td>
                      <td>
                        <JsonValueStateView state={conflict.proposed} />
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>

              <fieldset className="gf-merge-conflict__choices">
                <legend>选择最终采用的内容</legend>
                {choiceOrder
                  .filter((choice) => conflict.allowed_resolutions.includes(choice))
                  .map((choice) => (
                    <label key={choice}>
                      <input
                        checked={draft.choice === choice}
                        name={radioName}
                        onChange={() => selectChoice(conflict, choice)}
                        type="radio"
                        value={choice}
                      />
                      <span>{choiceLabels[choice]}</span>
                    </label>
                  ))}
              </fieldset>

              {draft.choice === "custom" && (
                <div className="gf-merge-conflict__custom">
                  <label htmlFor={customInputId}>自定义内容值（JSON，高级）</label>
                  <textarea
                    id={customInputId}
                    onChange={(event) => updateCustom(conflict, event.currentTarget.value)}
                    rows={5}
                    spellCheck={false}
                    value={draft.customText}
                  />
                  {draft.error && <p role="alert">{draft.error}</p>}
                </div>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}
