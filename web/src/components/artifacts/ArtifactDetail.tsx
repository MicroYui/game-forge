import { Info } from "lucide-react";
import { useId } from "react";

import type { components } from "../../api/generated/openapi";
import { compactDateTime, TechnicalDetails } from "../identity";
import { CursorTable, type CursorPaginationState } from "../tables";
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
  if (scope === "all") return ["全部内容领域"];
  if (scope === null) return ["公共资源"];
  const labels: Readonly<Record<string, string>> = {
    "domain:combat": "战斗系统",
    "domain:economy": "经济系统",
    "domain:gacha": "抽卡系统",
    "domain:narrative": "叙事内容",
    "domain:quest": "任务系统",
    "domain:rewards": "奖励系统",
  };
  return scope.domain_ids.map((domain) => labels[domain] ?? domain.replace(/^domain:/u, ""));
}

function artifactKindLabel(kind: string): string {
  return (
    {
      constraint_snapshot: "规则版本",
      evidence_set: "检查证据",
      failure_manifest: "失败记录",
      ir_snapshot: "内容版本",
      patch: "修改方案",
      review_report: "检查报告",
      run_result: "运行结果",
    }[kind] ?? "系统记录"
  );
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
  const technicalItems = [
    { label: "记录 ID", value: artifact.artifact_id },
    { label: "记录类型代码", value: artifact.kind },
    { label: "数据格式", value: artifact.payload_schema_id ?? "历史记录未提供" },
    { label: "完整性摘要", value: artifact.payload_hash ?? "历史记录未提供" },
    { label: "来源结构版本", value: artifact.lineage_schema_version },
    ...(artifact.domain_scope !== null && artifact.domain_scope !== "all"
      ? artifact.domain_scope.domain_ids.map((domain) => ({ label: "内容领域 ID", value: domain }))
      : []),
  ];
  const kindLabel = artifactKindLabel(artifact.kind);

  return (
    <article className="gf-artifact-detail" aria-labelledby={detailHeadingId}>
      <header className="gf-artifact-detail__header">
        <div>
          <p className="gf-artifact-detail__eyebrow">可追溯记录</p>
          <h1 id={detailHeadingId}>{kindLabel}详情</h1>
        </div>
        <span className="u-status">{kindLabel}</span>
      </header>

      <aside className="gf-artifact-detail__authority-note">
        <Info aria-hidden="true" size={18} />
        <p>这份记录存在，不代表它已经成为正式内容；是否生效仍以发布位置和审批状态为准。</p>
      </aside>

      <section className="gf-artifact-detail__section" aria-labelledby={envelopeHeadingId}>
        <h2 id={envelopeHeadingId}>记录摘要</h2>
        <dl className="gf-artifact-detail__facts">
          <div>
            <dt>内容类型</dt>
            <dd>{kindLabel}</dd>
          </div>
          <div>
            <dt>内容领域</dt>
            <dd>{domainText(artifact.domain_scope).join("、")}</dd>
          </div>
          <div>
            <dt>创建时间</dt>
            <dd>{compactDateTime(artifact.created_at)}</dd>
          </div>
          <div>
            <dt>内容来源</dt>
            <dd>{artifact.parent_artifact_ids.length} 项直接来源</dd>
          </div>
          {!artifact.payload_hash && (
            <div>
              <dt>完整性信息</dt>
              <dd>未提供完整性摘要</dd>
            </div>
          )}
        </dl>
        <TechnicalDetails items={technicalItems} summary="查看记录技术信息" />
      </section>

      <section className="gf-artifact-detail__section" aria-labelledby={versionHeadingId}>
        <h2 className="u-sr-only" id={versionHeadingId}>
          版本与工具信息
        </h2>
        <TechnicalDetails
          items={versionFields.map(([field, label]) => ({
            label,
            value: tupleValue(artifact.version_tuple[field]) ?? "不适用",
          }))}
          summary="查看版本与工具技术信息"
        />
      </section>

      <section className="gf-artifact-detail__section" aria-labelledby={lineageHeadingId}>
        <h2 className="u-sr-only" id={lineageHeadingId}>
          有界血缘
        </h2>
        {lineagePage ? (
          <CursorTable<LineageEntry>
            caption="内容来源（分页）"
            columns={[
              {
                header: "来源内容",
                id: "artifact",
                render: (entry) => `${artifactKindLabel(entry.artifact.kind)} · 第 ${entry.depth} 层来源`,
              },
              {
                header: "创建时间",
                id: "created",
                render: (entry) => compactDateTime(entry.artifact.created_at),
              },
              {
                header: "技术信息",
                id: "technical",
                render: (entry) => (
                  <TechnicalDetails
                    items={[
                      { label: "来源记录 ID", value: entry.artifact.artifact_id },
                      { label: "记录类型代码", value: entry.artifact.kind },
                      { label: "完整性摘要", value: entry.artifact.payload_hash ?? "历史记录未提供" },
                    ]}
                    summary="查看"
                  />
                ),
              },
            ]}
            getRowKey={(entry) => `${entry.depth}:${entry.artifact.artifact_id}`}
            items={lineagePage.items}
            nextCursor={lineagePage.next_cursor}
            onLoadMore={onLoadMoreLineage}
            onRestart={onRestartLineage}
            paginationState={lineagePaginationState}
            toolbar={
              <TechnicalDetails
                items={[
                  { label: "读取快照 ID", value: lineagePage.read_snapshot_id },
                  { label: "数据有效期", value: lineagePage.expires_at },
                ]}
                summary="查看分页技术信息"
              />
            }
          />
        ) : (
          <p className="gf-artifact-detail__lineage-empty">尚未加载有界血缘页。</p>
        )}
      </section>
    </article>
  );
}
