import { useQuery } from "@tanstack/react-query";
import {
  Braces,
  FileKey2,
  FlaskConical,
  GitBranch,
  Link2,
  MessageSquareWarning,
  ScanSearch,
  ShieldCheck,
} from "lucide-react";

import { ApiProblemError } from "../../api/problem";
import { CursorExpiredError } from "../../api/pagination";
import type { components } from "../../api/generated/openapi";
import { EvidenceSections, FindingCard, findingDisplayMessage } from "../../components/evidence";
import { TechnicalDetails } from "../../components/identity";
import { ProblemPanel, StatePanel } from "../../components/ui";
import {
  bindReviewAuthority,
  requireReviewEnvelope,
  resolveReviewLineage,
  reviewProducerRunCandidate,
  ReviewAuthorityError,
  type BoundReviewAuthority,
  type ReviewFindingBinding,
} from "./authority";
import { reviewApi, type ReviewApi, type RunFindingLinkView } from "./api";
import "./review.css";

type Finding = components["schemas"]["Finding"];

interface OpaquePage<T> {
  items: T[];
  next_cursor?: string | null;
  read_snapshot_id: string;
}

const MAX_DETAIL_PAGES = 256;

async function collectPages<T>(
  first: OpaquePage<T>,
  load: (cursor: string) => Promise<OpaquePage<T>>,
): Promise<T[]> {
  const items = [...first.items];
  let cursor = first.next_cursor ?? null;
  const seenCursors = new Set<string>();
  let pageCount = 1;
  while (cursor !== null) {
    if (pageCount >= MAX_DETAIL_PAGES) {
      throw new ReviewAuthorityError("Review detail pagination exceeded its bounded page count.");
    }
    if (seenCursors.has(cursor)) {
      throw new ReviewAuthorityError("Review detail pagination returned a cursor cycle.");
    }
    seenCursors.add(cursor);
    const next = await load(cursor);
    if (next.read_snapshot_id !== first.read_snapshot_id) {
      throw new ReviewAuthorityError("Review detail pagination changed read snapshot.");
    }
    items.push(...next.items);
    cursor = next.next_cursor ?? null;
    pageCount += 1;
  }
  return items;
}

async function loadDetail(
  api: ReviewApi,
  artifactId: string,
  snapshotContextArtifactId?: string,
  sourceRunId?: string,
): Promise<BoundReviewAuthority> {
  const review = await api.getReview(artifactId);
  requireReviewEnvelope(review, artifactId);
  const producerRunCandidate = reviewProducerRunCandidate(review.report);
  const explicitSourceRunId = sourceRunId?.trim() || undefined;
  const producerBindingPromise =
    producerRunCandidate === null
      ? Promise.resolve(null)
      : api.getReviewProducerBinding(artifactId, producerRunCandidate);
  const sourceBindingPromise =
    explicitSourceRunId === undefined
      ? Promise.resolve(null)
      : explicitSourceRunId === producerRunCandidate
        ? producerBindingPromise
        : api.getReviewProducerBinding(artifactId, explicitSourceRunId).catch((error: unknown) => {
            if (error instanceof ApiProblemError && error.problem.status === 404) return null;
            throw error;
          });
  const [firstLineage, producerBinding, sourceProducerBinding] = await Promise.all([
    api.listLineage(artifactId, null),
    producerBindingPromise,
    sourceBindingPromise,
  ]);
  const lineage = await collectPages(firstLineage, (cursor) => api.listLineage(artifactId, cursor));
  let exactFindingLinks: RunFindingLinkView[] = [];
  if (producerBinding?.finding_authority === "exact-run-links") {
    const firstLinks = await api.listRunFindingLinks(producerBinding.run_id, null);
    exactFindingLinks = await collectPages(firstLinks, (cursor) =>
      api.listRunFindingLinks(producerBinding.run_id, cursor),
    );
  }
  const lineageAuthority = resolveReviewLineage(review, lineage, artifactId, snapshotContextArtifactId);
  const [previewAuthority, constraintAuthority] = await Promise.all([
    api.getSpec(lineageAuthority.preview.artifact_id),
    lineageAuthority.constraint === null
      ? Promise.resolve(null)
      : api.getConstraint(lineageAuthority.constraint.artifact_id),
  ]);
  return bindReviewAuthority({
    constraintAuthority,
    exactFindingLinks,
    lineage,
    previewAuthority,
    producerBinding,
    requestedArtifactId: artifactId,
    review,
    sourceProducerBinding,
    sourceRunId: explicitSourceRunId,
    sourceRunOccurrence:
      explicitSourceRunId === undefined ? null : sourceProducerBinding === null ? "not-found" : "verified",
    snapshotContextArtifactId,
  });
}

const embeddedStatusLabels = {
  accepted_risk: "已接受风险",
  confirmed: "已确认",
  dismissed: "已忽略",
  fixed: "已修复",
  unproven: "未证明",
} as const;

const embeddedSeverityLabels = {
  critical: "严重",
  major: "重要",
  minor: "一般",
} as const;

const defectClassLabels: Readonly<Record<string, string>> = {
  dead_quest: "任务无法完成",
  economy_collapse: "经济系统可能失衡",
  playtest_incomplete: "试玩未完成",
  quest_dead_end: "任务流程存在死路",
  reward_out_of_range: "数值超出允许范围",
  unreachable_target: "目标无法到达",
};

function defectClassLabel(value: string): string {
  return defectClassLabels[value] ?? "其他规则问题";
}

const embeddedOracleMeta = {
  deterministic: { icon: ShieldCheck, label: "确定性预言机" },
  "llm-assisted": { icon: MessageSquareWarning, label: "AI 建议（需人工确认）" },
  simulation: { icon: FlaskConical, label: "仿真证据（描述性）" },
} as const;

function EmbeddedFindingCard({ finding }: { finding: Finding }) {
  const oracle = embeddedOracleMeta[finding.oracle_type];
  const OracleIcon = oracle.icon;
  const displayMessage = findingDisplayMessage(finding.defect_class, finding.message);
  return (
    <article
      className="gf-finding-card gf-review__embedded-finding"
      data-oracle={finding.oracle_type}
      data-severity={finding.severity}
    >
      <header className="gf-finding-card__header">
        <div className="gf-finding-card__badges">
          <span className="u-status" data-severity-label={finding.severity}>
            {embeddedSeverityLabels[finding.severity]}
          </span>
          <span className="u-status" data-oracle-label={finding.oracle_type}>
            <OracleIcon aria-hidden="true" size={14} />
            {oracle.label}
          </span>
          <span className="u-status" data-status-label={finding.status}>
            {embeddedStatusLabels[finding.status]}
          </span>
        </div>
        <h3>{displayMessage}</h3>
        <p className="gf-review__embedded-warning">这条问题没有独立历史版本，页面只展示报告内原始结果。</p>
      </header>
      <dl className="gf-finding-card__facts">
        <div>
          <dt>问题类型</dt>
          <dd>{defectClassLabel(finding.defect_class)}</dd>
        </div>
      </dl>
      <TechnicalDetails
        items={[
          { label: "问题 ID", value: finding.id },
          { label: "生成运行 ID", value: finding.producer_run_id },
          { label: "问题类型代码", value: finding.defect_class },
          { label: "内容快照 ID", value: finding.snapshot_id },
          ...(displayMessage === finding.message
            ? []
            : [{ label: "检查器原始说明", value: finding.message }]),
        ]}
        summary="查看问题技术信息"
      />
      <section className="gf-finding-card__repro" aria-label="内嵌问题最小复现">
        <h4>最小复现</h4>
        {finding.minimal_repro === undefined ? (
          <p className="gf-finding-card__empty">未提供复现数据</p>
        ) : (
          <details>
            <summary>查看原始复现数据</summary>
            <pre tabIndex={0}>{JSON.stringify(finding.minimal_repro, null, 2)}</pre>
          </details>
        )}
      </section>
      <section className="gf-finding-card__evidence" aria-label="内嵌问题证据">
        <h4>检查证据</h4>
        {finding.evidence === undefined ? (
          <p className="gf-finding-card__empty">未提供检查证据数据</p>
        ) : (
          <details>
            <summary>查看原始证据数据</summary>
            <pre tabIndex={0}>{JSON.stringify(finding.evidence, null, 2)}</pre>
          </details>
        )}
      </section>
    </article>
  );
}

function FindingBucket({ bindings }: { bindings: ReviewFindingBinding[] }) {
  return (
    <div className="gf-review__finding-list">
      {bindings.map((binding) =>
        binding.exact ? (
          <FindingCard
            authorityBinding={{
              attemptNo: binding.exact.attempt_no,
              evidenceArtifactId: binding.exact.evidence_artifact_id,
              findingDigest: binding.exact.finding_digest,
              ordinal: binding.exact.ordinal,
            }}
            detailHref={`/findings/${encodeURIComponent(binding.exact.finding.finding_id)}/revisions/${binding.exact.finding.revision}`}
            finding={binding.exact.finding}
            key={`${binding.exact.finding.finding_id}:${binding.exact.finding.revision}`}
          />
        ) : (
          <EmbeddedFindingCard finding={binding.embedded} key={binding.embedded.id} />
        ),
      )}
    </div>
  );
}

function DetailError({ error, onRetry }: { error: Error; onRetry(): void }) {
  if (error instanceof CursorExpiredError) {
    return (
      <StatePanel
        action={
          <button className="gf-secondary-button" onClick={onRetry} type="button">
            从第一页重新读取全部内容
          </button>
        }
        description="读取期间报告内容发生了更新。为避免混用前后两批数据，旧结果已停止使用。"
        headingLevel={1}
        state="error"
        title="报告内容已更新，请重新读取"
      />
    );
  }
  if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
  if (error instanceof ReviewAuthorityError) {
    return (
      <>
        <StatePanel
          action={
            <button className="gf-secondary-button" onClick={onRetry} type="button">
              重新读取完整报告
            </button>
          }
          description="报告、被检查内容或问题证据未能完整对应。为避免展示不可靠结论，页面已安全停止。"
          headingLevel={1}
          state="error"
          title="无法完整核对检查报告"
        />
        <TechnicalDetails
          items={[{ label: "核对失败原因", value: error.message }]}
          summary="查看错误技术信息"
        />
      </>
    );
  }
  return (
    <StatePanel
      action={
        <button className="gf-secondary-button" onClick={onRetry} type="button">
          重试
        </button>
      }
      description="检查报告暂时无法读取，请稍后重试。"
      headingLevel={1}
      state="error"
      title="无法读取检查报告"
    />
  );
}

export function ReviewDetailPage({
  api = reviewApi,
  artifactId,
  snapshotContextArtifactId,
  sourceRunId,
}: {
  api?: ReviewApi;
  artifactId: string;
  snapshotContextArtifactId?: string;
  sourceRunId?: string;
}) {
  const query = useQuery({
    queryFn: () => loadDetail(api, artifactId, snapshotContextArtifactId, sourceRunId),
    queryKey: ["review-detail", artifactId, snapshotContextArtifactId ?? null, sourceRunId ?? null],
    retry: false,
  });

  if (query.isPending) {
    return (
      <div className="gf-page gf-review">
        <StatePanel
          description="正在核对报告、被检查内容、使用的规则和问题证据。"
          headingLevel={1}
          state="loading"
          title="正在读取检查报告"
        />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="gf-page gf-review">
        <DetailError error={query.error} onRetry={() => void query.refetch()} />
      </div>
    );
  }

  const bound = query.data;
  const tuple = bound.review.artifact.version_tuple;
  const counts = {
    deterministic: bound.buckets.deterministic.length,
    simulation: bound.buckets.simulation.length,
    suggestion: bound.buckets.suggestion.length,
    unproven: bound.buckets.unproven.length,
  };
  const total = counts.deterministic + counts.simulation + counts.suggestion + counts.unproven;
  const authorityOccurrence = bound.producerBinding ?? bound.sourceProducerBinding;
  const sourceIsDistinctOccurrence =
    bound.sourceProducerBinding !== null &&
    bound.producerBinding !== null &&
    bound.sourceProducerBinding.run_id !== bound.producerBinding.run_id;

  return (
    <div className="gf-page gf-review gf-review-detail" data-layout="editorial-review-detail">
      <header className="gf-review-detail__hero">
        <div>
          <p className="gf-review__kicker">自动检查结果</p>
          <h1>内容检查报告</h1>
          <p>{total} 个问题；没有发现问题也不等于所有规则都已证明通过。</p>
        </div>
        <div className="gf-review-detail__seal">
          <ScanSearch aria-hidden="true" size={28} />
          <span>不可变报告</span>
        </div>
      </header>

      <ul className="gf-review-detail__counts" aria-label="问题分类计数">
        <li>
          <span>确定性</span>
          <strong>{counts.deterministic}</strong>
        </li>
        <li>
          <span>仿真</span>
          <strong>{counts.simulation}</strong>
        </li>
        <li>
          <span>AI 建议</span>
          <strong>{counts.suggestion}</strong>
        </li>
        <li>
          <span>未证明</span>
          <strong>{counts.unproven}</strong>
        </li>
      </ul>

      {(sourceRunId || bound.snapshotContextMatches !== null) && (
        <aside className="gf-review__context" aria-label="报告来源说明">
          <Link2 aria-hidden="true" size={20} />
          <div>
            {sourceRunId && (
              <p>
                <a href={`/runs/${encodeURIComponent(sourceRunId)}`}>查看来源运行</a>
                {bound.sourceRunOccurrence === "not-found"
                  ? " 尚未确认是这份报告的生成记录；这里只保留快捷入口。"
                  : bound.producerRunId === sourceRunId
                    ? " 已确认是生成这份报告的运行记录。"
                    : bound.sourceRunOccurrence === "verified" && bound.producerBinding === null
                      ? " 已确认是生成报告的记录；报告未发现问题，无需逐项绑定问题证据。"
                      : bound.sourceRunOccurrence === "verified"
                        ? " 是另一条已验证的报告生成记录；问题证据仍以各自记录为准。"
                        : " 仅作快捷导航，尚未要求系统核对它与报告的关系。"}
              </p>
            )}
            {bound.snapshotContextMatches === true && <p>打开的内容预览与本报告一致。</p>}
            {bound.snapshotContextMatches === false && (
              <p className="gf-review__context-miss">
                打开的内容预览与本报告不一致；页面仍保留原始报告，不会悄悄替换被检查内容。
              </p>
            )}
          </div>
        </aside>
      )}

      <section className="gf-review-detail__authority" aria-labelledby="review-authority-title">
        <header>
          <FileKey2 aria-hidden="true" size={22} />
          <div>
            <h2 id="review-authority-title">本报告检查了什么</h2>
            <p>内容版本、规则版本和执行记录都已固定，打开报告不会悄悄切换到最新版本。</p>
          </div>
        </header>
        <dl>
          <div>
            <dt>报告内容</dt>
            <dd>已固定为生成报告时的内容版本</dd>
          </div>
          <div>
            <dt>被检查的内容</dt>
            <dd>
              <a href={`/specs/${encodeURIComponent(bound.preview.artifact_id)}`}>查看被检查的内容</a>
            </dd>
          </div>
          <div>
            <dt>使用的规则</dt>
            <dd>
              {bound.constraint ? (
                <a href={`/constraints/${encodeURIComponent(bound.constraint.artifact_id)}`}>
                  查看使用的规则版本
                </a>
              ) : (
                "本次检查没有绑定额外规则"
              )}
            </dd>
          </div>
          <div>
            <dt>执行记录</dt>
            <dd>
              {authorityOccurrence ? (
                <a href={`/runs/${encodeURIComponent(authorityOccurrence.run_id)}`}>查看本次检查的运行记录</a>
              ) : (
                "没有可验证的运行记录"
              )}
            </dd>
          </div>
          <div>
            <dt>问题依据</dt>
            <dd>
              {bound.findingAuthority === "exact-run-links"
                ? "每个问题都绑定了固定检查证据"
                : bound.findingAuthority === "embedded-only"
                  ? "问题保存在报告内，没有独立历史版本"
                  : "本报告没有发现问题"}
            </dd>
          </div>
          <div>
            <dt>运行结果</dt>
            <dd>
              {authorityOccurrence ? (
                <a href={`/artifacts/${encodeURIComponent(authorityOccurrence.terminal_manifest_id)}`}>
                  查看运行结果清单
                </a>
              ) : (
                "没有运行结果清单"
              )}
            </dd>
          </div>
          <div>
            <dt>结果判定方式</dt>
            <dd>{authorityOccurrence ? "已固定并可审计" : "不适用"}</dd>
          </div>
          {sourceIsDistinctOccurrence && bound.sourceProducerBinding && (
            <div>
              <dt>另一条来源运行</dt>
              <dd>
                <a href={`/runs/${encodeURIComponent(bound.sourceProducerBinding.run_id)}`}>
                  查看另一条已验证的来源运行
                </a>
              </dd>
            </div>
          )}
          <div>
            <dt>内容来源关系</dt>
            <dd>
              <a href={`/artifacts/${encodeURIComponent(bound.review.artifact.artifact_id)}/lineage`}>
                查看完整血缘
              </a>
            </dd>
          </div>
        </dl>
      </section>

      <TechnicalDetails
        items={[
          { label: "报告记录 ID", value: bound.review.artifact.artifact_id },
          { label: "报告数据版本", value: bound.review.report.review_schema_version },
          { label: "内容快照 ID", value: bound.review.report.snapshot_id },
          { label: "预览记录 ID", value: bound.preview.artifact_id },
          ...(bound.constraint ? [{ label: "规则记录 ID", value: bound.constraint.artifact_id }] : []),
          ...(authorityOccurrence
            ? [
                { label: "运行 ID", value: authorityOccurrence.run_id },
                { label: "结果清单 ID", value: authorityOccurrence.terminal_manifest_id },
                {
                  label: "结果策略",
                  value: `${authorityOccurrence.outcome_policy_id}@${authorityOccurrence.outcome_policy_version}`,
                },
                { label: "结果规则", value: authorityOccurrence.outcome_rule_id },
                { label: "清单角色", value: authorityOccurrence.manifest_role },
              ]
            : []),
          ...(sourceIsDistinctOccurrence && bound.sourceProducerBinding
            ? [
                { label: "另一来源运行 ID", value: bound.sourceProducerBinding.run_id },
                {
                  label: "另一来源结果策略",
                  value: `${bound.sourceProducerBinding.outcome_policy_id}@${bound.sourceProducerBinding.outcome_policy_version}`,
                },
                { label: "另一来源结果规则", value: bound.sourceProducerBinding.outcome_rule_id },
                { label: "另一来源清单角色", value: bound.sourceProducerBinding.manifest_role },
              ]
            : []),
        ]}
        summary="查看报告技术信息"
      />

      <section className="gf-review-detail__tool" aria-labelledby="review-tool-title">
        <header>
          <Braces aria-hidden="true" size={22} />
          <div>
            <h2 className="u-sr-only" id="review-tool-title">
              检查工具技术信息
            </h2>
          </div>
        </header>
        <TechnicalDetails
          items={[
            ["工具版本", tuple.tool_version],
            ["模型快照", tuple.model_snapshot],
            ["提示词版本", tuple.prompt_version],
            ["Agent 流程版本", tuple.agent_graph_version],
            ["随机种子", tuple.seed],
            ["回放记录", tuple.cassette_id],
          ].map(([label, value]) => ({
            label: String(label),
            value: value == null ? "不适用" : String(value),
          }))}
          summary="查看检查工具技术信息"
        />
      </section>

      {bound.review.report.by_defect_class && bound.review.report.by_defect_class.length > 0 && (
        <section className="gf-review-detail__classes" aria-labelledby="review-classes-title">
          <header>
            <GitBranch aria-hidden="true" size={22} />
            <h2 id="review-classes-title">问题类型汇总</h2>
          </header>
          <ul>
            {bound.review.report.by_defect_class.map((item) => (
              <li key={`${item.defect_class}:${item.severity}`}>
                <span>{defectClassLabel(item.defect_class)}</span>
                <span>{embeddedSeverityLabels[item.severity]}</span>
                <strong>{item.count}</strong>
                <TechnicalDetails
                  items={[{ label: "问题类型代码", value: item.defect_class }]}
                  summary="技术信息"
                />
              </li>
            ))}
          </ul>
        </section>
      )}

      <EvidenceSections
        deterministic={
          bound.buckets.deterministic.length > 0 ? (
            <FindingBucket bindings={bound.buckets.deterministic} />
          ) : undefined
        }
        simulation={
          bound.buckets.simulation.length > 0 ? (
            <FindingBucket bindings={bound.buckets.simulation} />
          ) : undefined
        }
        suggestion={
          bound.buckets.suggestion.length > 0 ? (
            <FindingBucket bindings={bound.buckets.suggestion} />
          ) : undefined
        }
        unproven={
          bound.buckets.unproven.length > 0 ? <FindingBucket bindings={bound.buckets.unproven} /> : undefined
        }
      />
    </div>
  );
}
