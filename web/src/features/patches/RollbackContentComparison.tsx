import type { components } from "../../api/generated/openapi";
import { cursorFromPage } from "../../api/pagination";
import { CopyableText } from "../../components/tables";
import type { ArtifactPayloadView, PatchWorkflowApi } from "./api";

const MAX_FACTS = 64;
const MAX_VISIBLE_CHANGES = 12;
const MAX_VALUE_CHARACTERS = 160;

type SnapshotDiffEntry = components["schemas"]["SnapshotDiffEntry"];

export interface RollbackSnapshotDiff {
  entries: SnapshotDiffEntry[];
  entryCount: number;
}

interface FactCollection {
  facts: Map<string, string>;
  truncated: boolean;
}

function displayValue(value: unknown): { text: string; truncated: boolean } {
  const raw =
    value === null ? "null" : typeof value === "string" ? value : (JSON.stringify(value) ?? String(value));
  return raw.length > MAX_VALUE_CHARACTERS
    ? { text: `${raw.slice(0, MAX_VALUE_CHARACTERS)}…`, truncated: true }
    : { text: raw, truncated: false };
}

function collectFacts(value: unknown, path: string, result: FactCollection, depth = 0): void {
  if (result.facts.size >= MAX_FACTS) {
    result.truncated = true;
    return;
  }
  if (value === null || typeof value !== "object") {
    const displayed = displayValue(value);
    result.facts.set(path || "内容", displayed.text);
    result.truncated ||= displayed.truncated;
    return;
  }
  if (depth >= 4) {
    result.facts.set(
      path || "内容",
      Array.isArray(value) ? `列表（${value.length} 项）` : `对象（${Object.keys(value).length} 个字段）`,
    );
    result.truncated = true;
    return;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) result.facts.set(path || "内容", "空列表");
    value
      .slice(0, 12)
      .forEach((item, index) => collectFacts(item, `${path || "内容"}[${index}]`, result, depth + 1));
    if (value.length > 12) result.truncated = true;
    return;
  }
  const entries = Object.entries(value as Record<string, unknown>).sort(([left], [right]) =>
    left.localeCompare(right),
  );
  if (entries.length === 0) result.facts.set(path || "内容", "空对象");
  for (const [key, item] of entries) {
    collectFacts(item, path ? `${path}.${key}` : key, result, depth + 1);
    if (result.facts.size >= MAX_FACTS) {
      if (entries[entries.length - 1]?.[0] !== key) result.truncated = true;
      break;
    }
  }
}

function displayState(state: SnapshotDiffEntry["before"]): string {
  return state.presence === "missing" ? "未设置" : displayValue(state.value).text;
}

export async function collectRollbackSnapshotDiff(
  api: PatchWorkflowApi,
  current: ArtifactPayloadView,
  target: ArtifactPayloadView,
): Promise<RollbackSnapshotDiff | null> {
  if (current.artifact.kind !== "ir_snapshot" || target.artifact.kind !== "ir_snapshot") {
    return null;
  }
  const currentSnapshotId = current.artifact.version_tuple.ir_snapshot_id;
  const targetSnapshotId = target.artifact.version_tuple.ir_snapshot_id;
  if (currentSnapshotId == null || targetSnapshotId == null) return null;

  const entries: SnapshotDiffEntry[] = [];
  const paths = new Set<string>();
  const cursors = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  let entryCount: number | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.getSnapshotDiff(currentSnapshotId, targetSnapshotId, cursor);
    if (
      page.diff.base_snapshot_id !== currentSnapshotId ||
      page.diff.target_snapshot_id !== targetSnapshotId
    ) {
      throw new Error("Rollback diff authority returned different snapshots.");
    }
    if (entryCount !== null && page.diff.entry_count !== entryCount) {
      throw new Error("Rollback diff authority changed its entry count.");
    }
    if (readSnapshotId !== null && page.page.read_snapshot_id !== readSnapshotId) {
      throw new Error("Rollback diff authority changed read snapshot.");
    }
    entryCount = page.diff.entry_count;
    readSnapshotId = page.page.read_snapshot_id;
    for (const entry of page.page.items) {
      if (paths.has(entry.path)) throw new Error("Rollback diff authority returned a duplicate path.");
      paths.add(entry.path);
      entries.push(entry);
    }
    const next = cursorFromPage(page.page);
    if (next === null) {
      if (entries.length !== entryCount) {
        throw new Error("Rollback diff authority did not return its complete entry set.");
      }
      return { entries, entryCount };
    }
    if (cursors.has(next)) throw new Error("Rollback diff authority returned a cursor cycle.");
    cursors.add(next);
    cursor = next;
  }
  throw new Error("Rollback diff authority exceeded its bounded page count.");
}

export function RollbackContentComparison({
  current,
  currentLabel,
  diff,
  target,
  targetLabel,
}: {
  current: ArtifactPayloadView;
  currentLabel: string;
  diff?: RollbackSnapshotDiff | null;
  target: ArtifactPayloadView;
  targetLabel: string;
}) {
  const currentFacts: FactCollection = { facts: new Map(), truncated: false };
  const targetFacts: FactCollection = { facts: new Map(), truncated: false };
  collectFacts(current.payload, "", currentFacts);
  collectFacts(target.payload, "", targetFacts);
  const fallbackChangedPaths = [...new Set([...currentFacts.facts.keys(), ...targetFacts.facts.keys()])]
    .filter((path) => currentFacts.facts.get(path) !== targetFacts.facts.get(path))
    .sort();
  const authoritative = diff !== null && diff !== undefined;
  const changedPaths = authoritative ? diff.entries.map((entry) => entry.path) : fallbackChangedPaths;
  const visibleChanges = changedPaths.slice(0, MAX_VISIBLE_CHANGES);
  const fallbackTruncated = currentFacts.truncated || targetFacts.truncated;
  const sameDigest =
    current.artifact.payload_hash !== null && current.artifact.payload_hash === target.artifact.payload_hash;
  const differentDigest =
    current.artifact.payload_hash !== null &&
    target.artifact.payload_hash !== null &&
    current.artifact.payload_hash !== target.artifact.payload_hash;

  return (
    <section aria-label="当前版本与回滚目标的内容变化" className="gf-patches__evidence-ledger">
      <h3>回滚后会改变什么</h3>
      <p className="gf-patches__muted">
        {currentLabel} → {targetLabel}；
        {authoritative
          ? "以下来自完整分页的确定性 snapshot diff。"
          : "以下是两个 immutable Artifact payload 的有界可读摘要。"}
      </p>
      {visibleChanges.length === 0 ? (
        authoritative ? (
          <p>完整 snapshot diff 确认没有内容字段变化。</p>
        ) : fallbackTruncated && differentDigest ? (
          <p role="status">摘要未覆盖全部内容，且 exact payload digest 不同，存在未展示变化。</p>
        ) : fallbackTruncated && !sameDigest ? (
          <p role="status">摘要未覆盖全部内容，无法据此断言没有变化。</p>
        ) : (
          <p>
            {sameDigest ? "Exact payload digest 相同，没有内容字段变化。" : "完整摘要未发现内容字段变化。"}
          </p>
        )
      ) : (
        <ul aria-label="回滚内容差异" className="gf-patches__history-list">
          {visibleChanges.map((path, index) => {
            const entry = authoritative ? diff.entries[index] : null;
            return (
              <li key={path}>
                <strong>{path}</strong>
                <span>
                  {entry ? displayState(entry.before) : (currentFacts.facts.get(path) ?? "未设置")} →{" "}
                  {entry ? displayState(entry.after) : (targetFacts.facts.get(path) ?? "未设置")}
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {changedPaths.length > MAX_VISIBLE_CHANGES && (
        <p className="gf-patches__muted">
          另有 {changedPaths.length - MAX_VISIBLE_CHANGES} 项已确认差异未在摘要展开。
        </p>
      )}
      {!authoritative && fallbackTruncated && visibleChanges.length > 0 && (
        <p className="gf-patches__muted">有界摘要未比较全部内容；除上述可见差异外，可能还有未展示变化。</p>
      )}
      <details>
        <summary>查看 exact Artifact 技术身份</summary>
        <CopyableText copyLabel="复制 current Artifact ID" value={current.artifact.artifact_id} />
        <CopyableText copyLabel="复制 target Artifact ID" value={target.artifact.artifact_id} />
      </details>
    </section>
  );
}
