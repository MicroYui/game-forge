import { GitBranch, Search } from "lucide-react";
import { useState } from "react";

import { ApiProblemError } from "../../api/problem";
import { CopyableText } from "../../components/tables";
import type { RefHistoryPage, SpecWorkflowApi } from "./api";

type RefBindingValue = RefHistoryPage["items"][number]["value"];
type RefApi = Pick<SpecWorkflowApi, "listRefHistory">;

export interface ConstraintRefSelection {
  expectedRef: RefBindingValue | null;
  refName: string;
}

async function currentRef(api: RefApi, refName: string): Promise<RefBindingValue> {
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  const values: RefBindingValue[] = [];
  const seen = new Set<string>();
  let complete = false;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listRefHistory(refName, cursor);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new Error("Ref history read snapshot changed.");
    }
    readSnapshotId = page.read_snapshot_id;
    values.push(...page.items.map((item) => item.value));
    const next = page.next_cursor ?? null;
    if (next === null) {
      complete = true;
      break;
    }
    if (seen.has(next)) throw new Error("Ref history cursor cycle.");
    seen.add(next);
    cursor = next;
  }
  if (!complete) throw new Error("Ref history exceeded its bounded page count.");
  if (values.length === 0) throw new Error("Ref history is empty.");
  return values.reduce((latest, value) => (value.revision > latest.revision ? value : latest));
}

export function ConstraintRefBindingFields({
  api,
  disabled = false,
  name,
  onChange,
  value,
}: {
  api: RefApi;
  disabled?: boolean;
  name: string;
  onChange(value: ConstraintRefSelection | null): void;
  value: ConstraintRefSelection | null;
}) {
  const [mode, setMode] = useState<"" | "existing" | "new">("");
  const [refName, setRefName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [advancedArtifactId, setAdvancedArtifactId] = useState("");
  const [advancedRevision, setAdvancedRevision] = useState("");

  function selectMode(next: "existing" | "new") {
    setMode(next);
    setError(null);
    onChange(next === "new" && refName.trim() ? { expectedRef: null, refName: refName.trim() } : null);
  }

  function changeRefName(next: string) {
    setRefName(next);
    setError(null);
    onChange(mode === "new" && next.trim() ? { expectedRef: null, refName: next.trim() } : null);
  }

  async function resolveExisting() {
    const normalized = refName.trim();
    if (!normalized) return;
    setLoading(true);
    setError(null);
    onChange(null);
    try {
      const expectedRef = await currentRef(api, normalized);
      onChange({ expectedRef, refName: normalized });
    } catch (caught) {
      setError(
        caught instanceof ApiProblemError && caught.problem.status === 404
          ? "没有找到这个 ref；如果要创建它，请选择“创建新 ref”。"
          : "无法读取完整 ref 历史；未选择任何版本。",
      );
    } finally {
      setLoading(false);
    }
  }

  function useAdvancedBinding() {
    const revision = Number(advancedRevision);
    if (!refName.trim() || !advancedArtifactId.trim() || !Number.isInteger(revision) || revision < 1) {
      return;
    }
    onChange({
      expectedRef: { artifact_id: advancedArtifactId.trim(), revision },
      refName: refName.trim(),
    });
  }

  return (
    <fieldset className="gf-specs__ref-choice" disabled={disabled}>
      <legend>发布位置</legend>
      <div>
        <label>
          <input
            checked={mode === "new"}
            name={`${name}-mode`}
            onChange={() => selectMode("new")}
            type="radio"
          />
          创建新 ref
        </label>
        <label>
          <input
            checked={mode === "existing"}
            name={`${name}-mode`}
            onChange={() => selectMode("existing")}
            type="radio"
          />
          更新已有 ref
        </label>
      </div>
      {mode !== "" && (
        <label>
          Ref 名称
          <input
            onChange={(event) => changeRefName(event.target.value)}
            placeholder="例如 constraints/head"
            type="text"
            value={refName}
          />
        </label>
      )}
      {mode === "new" && refName.trim() && (
        <p className="gf-specs__field-hint">
          将以“当前不存在”作为并发前提；如果同名 ref 已存在，服务器会拒绝，不会覆盖。
        </p>
      )}
      {mode === "existing" && (
        <button
          className="gf-secondary-button"
          disabled={loading || !refName.trim()}
          onClick={() => void resolveExisting()}
          type="button"
        >
          <Search aria-hidden="true" size={16} />
          {loading ? "正在查找当前版本" : "查找当前版本"}
        </button>
      )}
      {error && <p role="alert">{error}</p>}
      {value?.expectedRef && value.refName === refName.trim() && (
        <div className="gf-specs__resolved-ref" role="status">
          <GitBranch aria-hidden="true" size={18} />
          <div>
            <strong>{value.refName}</strong>
            <span>已选择当前 revision {value.expectedRef.revision}</span>
            <details>
              <summary>查看 exact 技术身份</summary>
              <CopyableText copyLabel="复制当前 ref Artifact ID" value={value.expectedRef.artifact_id} />
            </details>
          </div>
        </div>
      )}
      {mode === "existing" && (
        <details className="gf-specs__advanced-binding">
          <summary>高级：手动输入 exact binding</summary>
          <p>仅用于目录不可用但你已从审计记录取得 exact pair 的情况。</p>
          <label>
            Artifact ID
            <input
              onChange={(event) => setAdvancedArtifactId(event.target.value)}
              type="text"
              value={advancedArtifactId}
            />
          </label>
          <label>
            Revision
            <input
              min="1"
              onChange={(event) => setAdvancedRevision(event.target.value)}
              type="number"
              value={advancedRevision}
            />
          </label>
          <button className="gf-secondary-button" onClick={useAdvancedBinding} type="button">
            使用这个 exact binding
          </button>
        </details>
      )}
    </fieldset>
  );
}
