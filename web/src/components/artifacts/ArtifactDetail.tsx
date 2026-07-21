import { Info } from "lucide-react";
import { useId } from "react";

import type { components } from "../../api/generated/openapi";
import { CopyableText, CursorTable, type CursorPaginationState } from "../tables";
import "./artifacts.css";

type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];
type LineageEntry = components["schemas"]["LineageEntryV1"];
type LineagePage = components["schemas"]["OpaquePageV1_LineageEntryV1_"];
type VersionTuple = components["schemas"]["VersionTuple"];

const versionFields: readonly [keyof VersionTuple, string][] = [
  ["doc_version", "文档版本"],
  ["ir_snapshot_id", "IR 快照"],
  ["constraint_snapshot_id", "约束快照"],
  ["prompt_version", "Prompt 版本"],
  ["model_snapshot", "模型快照"],
  ["agent_graph_version", "Agent 图版本"],
  ["tool_version", "工具版本"],
  ["env_contract_version", "环境契约"],
  ["seed", "Seed"],
  ["cassette_id", "Cassette"],
];

function domainText(scope: ArtifactSummary["domain_scope"]): readonly string[] {
  if (scope === "all") return ["全部域"];
  if (scope === null) return ["非域资源"];
  return scope.domain_ids;
}

function tupleValue(value: string | number | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  return String(value);
}

export function ArtifactDetail({
  artifact,
  lineagePage,
  lineagePaginationState = "ready",
  onLoadMoreLineage,
  onRestartLineage,
}: {
  artifact: ArtifactSummary;
  lineagePage?: LineagePage;
  lineagePaginationState?: CursorPaginationState;
  onLoadMoreLineage?(cursor: string): void;
  onRestartLineage?(): void;
}) {
  const instanceId = useId();
  const detailHeadingId = `${instanceId}-detail`;
  const envelopeHeadingId = `${instanceId}-envelope`;
  const versionHeadingId = `${instanceId}-version`;
  const lineageHeadingId = `${instanceId}-lineage`;

  return (
    <article className="gf-artifact-detail" aria-labelledby={detailHeadingId}>
      <header className="gf-artifact-detail__header">
        <div>
          <p className="gf-artifact-detail__eyebrow">{artifact.kind}</p>
          <h1 id={detailHeadingId}>工件详情</h1>
        </div>
        <span className="u-status">{artifact.lineage_schema_version}</span>
      </header>

      <aside className="gf-artifact-detail__authority-note">
        <Info aria-hidden="true" size={18} />
        <p>工件存在不代表当前 ref 权威；权威版本只由相应 ref 与审批状态确定。</p>
      </aside>

      <section className="gf-artifact-detail__section" aria-labelledby={envelopeHeadingId}>
        <h2 id={envelopeHeadingId}>安全工件摘要</h2>
        <dl className="gf-artifact-detail__facts">
          <div className="gf-artifact-detail__wide">
            <dt>Artifact ID</dt>
            <dd>
              <CopyableText copyLabel="复制 Artifact ID" value={artifact.artifact_id} />
            </dd>
          </div>
          <div>
            <dt>Kind</dt>
            <dd>{artifact.kind}</dd>
          </div>
          <div>
            <dt>Payload schema</dt>
            <dd>{artifact.payload_schema_id ?? "历史工件未提供"}</dd>
          </div>
          <div>
            <dt>创建时间</dt>
            <dd>{artifact.created_at ?? "历史工件未提供"}</dd>
          </div>
          <div>
            <dt>直接父级</dt>
            <dd>{artifact.parent_artifact_ids.length} 个</dd>
          </div>
          <div className="gf-artifact-detail__wide">
            <dt>Payload SHA-256</dt>
            <dd>
              {artifact.payload_hash ? (
                <CopyableText copyLabel="复制 Payload SHA-256" value={artifact.payload_hash} />
              ) : (
                "历史工件未提供（不伪造）"
              )}
            </dd>
          </div>
          <div className="gf-artifact-detail__wide">
            <dt>Domain scope</dt>
            <dd className="gf-artifact-detail__domains">
              {domainText(artifact.domain_scope).map((domain) => (
                <CopyableText copyLabel="复制域 ID" key={domain} value={domain} />
              ))}
            </dd>
          </div>
        </dl>
      </section>

      <section className="gf-artifact-detail__section" aria-labelledby={versionHeadingId}>
        <h2 id={versionHeadingId}>VersionTuple</h2>
        <dl className="gf-artifact-detail__tuple">
          {versionFields.map(([field, label]) => {
            const value = tupleValue(artifact.version_tuple[field]);
            return (
              <div key={field}>
                <dt>{label}</dt>
                <dd>
                  {value === null ? (
                    <span>不适用</span>
                  ) : (
                    <CopyableText copyLabel={`复制${label}`} value={value} />
                  )}
                </dd>
              </div>
            );
          })}
        </dl>
      </section>

      <section className="gf-artifact-detail__section" aria-labelledby={lineageHeadingId}>
        <h2 className="u-sr-only" id={lineageHeadingId}>
          有界血缘
        </h2>
        {lineagePage ? (
          <CursorTable<LineageEntry>
            caption="血缘（有界分页）"
            columns={[
              {
                header: "Artifact",
                id: "artifact",
                render: (entry) => (
                  <CopyableText copyLabel="复制血缘 Artifact ID" value={entry.artifact.artifact_id} />
                ),
              },
              { header: "Kind", id: "kind", render: (entry) => entry.artifact.kind },
              { header: "深度", id: "depth", render: (entry) => entry.depth },
              {
                header: "Payload hash",
                id: "hash",
                render: (entry) => entry.artifact.payload_hash ?? "历史工件未提供",
              },
            ]}
            getRowKey={(entry) => `${entry.depth}:${entry.artifact.artifact_id}`}
            items={lineagePage.items}
            nextCursor={lineagePage.next_cursor}
            onLoadMore={onLoadMoreLineage}
            onRestart={onRestartLineage}
            paginationState={lineagePaginationState}
            toolbar={
              <span className="u-small">
                快照 {lineagePage.read_snapshot_id} · {lineagePage.expires_at} 到期
              </span>
            }
          />
        ) : (
          <p className="gf-artifact-detail__lineage-empty">尚未加载有界血缘页。</p>
        )}
      </section>
    </article>
  );
}
