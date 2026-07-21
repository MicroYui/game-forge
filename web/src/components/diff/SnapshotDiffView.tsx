import type { components } from "../../api/generated/openapi";
import { useId } from "react";
import { CopyableText } from "../tables";
import { JsonValueStateView } from "./JsonValueStateView";
import "./diff.css";

type SnapshotDiff = components["schemas"]["SnapshotDiff"];
type SnapshotDiffEntry = components["schemas"]["SnapshotDiffEntry"];

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
          <h2 id={headingId}>字段级 Diff</h2>
          <p>
            本页 {entries.length} / 共 {diff.entry_count} 项；缺失值与 JSON null 分开显示。
          </p>
        </div>
        <dl>
          <div>
            <dt>Base</dt>
            <dd>
              <CopyableText copyLabel="复制 Base 快照 ID" value={diff.base_snapshot_id} />
            </dd>
          </div>
          <div>
            <dt>Target</dt>
            <dd>
              <CopyableText copyLabel="复制 Target 快照 ID" value={diff.target_snapshot_id} />
            </dd>
          </div>
        </dl>
      </header>
      <div className="gf-diff__scroll" tabIndex={0}>
        <table>
          <caption className="u-sr-only">字段级快照差异</caption>
          <thead>
            <tr>
              <th scope="col">JSON Pointer</th>
              <th scope="col">Before</th>
              <th scope="col">After</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry.path}>
                <th scope="row">
                  <CopyableText copyLabel="复制 JSON Pointer" value={entry.path} />
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
