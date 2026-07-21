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
import { EvidenceSections, FindingCard } from "../../components/evidence";
import { CopyableText } from "../../components/tables";
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
  accepted_risk: "已接受风险 · accepted_risk",
  confirmed: "已确认 · confirmed",
  dismissed: "已驳回 · dismissed",
  fixed: "已修复 · fixed",
  unproven: "未证明 · unproven",
} as const;

const embeddedSeverityLabels = {
  critical: "严重 · critical",
  major: "主要 · major",
  minor: "次要 · minor",
} as const;

const embeddedOracleMeta = {
  deterministic: { icon: ShieldCheck, label: "确定性预言机" },
  "llm-assisted": { icon: MessageSquareWarning, label: "LLM 建议（需人确认）" },
  simulation: { icon: FlaskConical, label: "仿真证据（描述性）" },
} as const;

function EmbeddedFindingCard({ finding }: { finding: Finding }) {
  const oracle = embeddedOracleMeta[finding.oracle_type];
  const OracleIcon = oracle.icon;
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
        <h3>{finding.message}</h3>
        <p className="gf-review__embedded-warning">无 immutable revision；未回退 latest</p>
      </header>
      <dl className="gf-finding-card__facts">
        <div>
          <dt>Finding ID</dt>
          <dd>
            <CopyableText copyLabel="复制内嵌 Finding ID" value={finding.id} />
          </dd>
        </div>
        <div>
          <dt>生产 Run</dt>
          <dd>
            <CopyableText copyLabel="复制内嵌 Finding producer Run" value={finding.producer_run_id} />
          </dd>
        </div>
      </dl>
      <section className="gf-finding-card__repro" aria-label="内嵌 Finding 最小复现">
        <h4>最小复现（报告内嵌）</h4>
        {finding.minimal_repro === undefined ? (
          <p className="gf-finding-card__empty">未提供 minimal_repro</p>
        ) : (
          <pre tabIndex={0}>{JSON.stringify(finding.minimal_repro, null, 2)}</pre>
        )}
      </section>
      <section className="gf-finding-card__evidence" aria-label="内嵌 Finding evidence payload">
        <h4>证据 payload（报告内嵌）</h4>
        {finding.evidence === undefined ? (
          <p className="gf-finding-card__empty">未提供 evidence payload</p>
        ) : (
          <pre tabIndex={0}>{JSON.stringify(finding.evidence, null, 2)}</pre>
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

function versionValue(value: string | number | null | undefined): string {
  return value === null || value === undefined ? "不适用 (N/A)" : String(value);
}

function DetailError({ error, onRetry }: { error: Error; onRetry(): void }) {
  if (error instanceof CursorExpiredError) {
    return (
      <StatePanel
        action={
          <button className="gf-secondary-button" onClick={onRetry} type="button">
            从第一页重新读取全部权威
          </button>
        }
        description="详情读取期间分页快照已过期；旧游标不会被静默替换。"
        headingLevel={1}
        state="error"
        title="Review 详情快照已过期"
      />
    );
  }
  if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
  if (error instanceof ReviewAuthorityError) {
    return (
      <StatePanel
        action={
          <button className="gf-secondary-button" onClick={onRetry} type="button">
            重新读取全部权威
          </button>
        }
        description={error.message}
        headingLevel={1}
        state="error"
        title="Review 权威闭合失败"
      />
    );
  }
  return (
    <StatePanel
      action={
        <button className="gf-secondary-button" onClick={onRetry} type="button">
          重试
        </button>
      }
      description="Review 详情读取失败；未显示底层异常。"
      headingLevel={1}
      state="error"
      title="无法读取 Review Report"
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
          description="正在闭合 Review、producer occurrence、direct lineage、专用 Spec/Constraint authority 与 Run Finding links。"
          headingLevel={1}
          state="loading"
          title="正在读取 Review Report"
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
          <p className="gf-review__kicker">Immutable correctness report</p>
          <h1>Review Report</h1>
          <CopyableText copyLabel="复制 Review Artifact ID" value={bound.review.artifact.artifact_id} />
          <p>{total} 条 Finding；0 不代表通过</p>
        </div>
        <div className="gf-review-detail__seal">
          <ScanSearch aria-hidden="true" size={28} />
          <span>
            <span className="u-sr-only">Review schema：</span>
            {bound.review.report.review_schema_version}
          </span>
        </div>
      </header>

      <ul className="gf-review-detail__counts" aria-label="Finding 分区计数">
        <li>
          <span>确定性</span>
          <strong>{counts.deterministic}</strong>
        </li>
        <li>
          <span>仿真</span>
          <strong>{counts.simulation}</strong>
        </li>
        <li>
          <span>LLM 建议</span>
          <strong>{counts.suggestion}</strong>
        </li>
        <li>
          <span>未证明</span>
          <strong>{counts.unproven}</strong>
        </li>
      </ul>

      {(sourceRunId || bound.snapshotContextMatches !== null) && (
        <aside className="gf-review__context" aria-label="Review 请求上下文">
          <Link2 aria-hidden="true" size={20} />
          <div>
            {sourceRunId && (
              <p>
                <a href={`/runs/${encodeURIComponent(sourceRunId)}`}>{sourceRunId}</a>
                {bound.sourceRunOccurrence === "not-found"
                  ? " 未验证为该 Review 的 producer occurrence；仅保留为导航上下文。"
                  : bound.producerRunId === sourceRunId
                    ? " 与服务端验证的 Review producer occurrence 一致。"
                    : bound.sourceRunOccurrence === "verified" && bound.producerBinding === null
                      ? " 已由服务端验证为该 Review 的 producer occurrence；报告无 Finding，因此 Finding authority 不适用。"
                      : bound.sourceRunOccurrence === "verified"
                        ? " 是另一条已验证的 Review producer occurrence；Finding authority 仍绑定其自身的 exact occurrence。"
                        : " 仅作为导航上下文；未请求 producer occurrence 验证。"}
              </p>
            )}
            {bound.snapshotContextMatches === true && <p>direct preview 与请求上下文一致。</p>}
            {bound.snapshotContextMatches === false && (
              <p className="gf-review__context-miss">
                direct preview 与请求上下文不一致；页面保留真实报告且不替换权威。
              </p>
            )}
          </div>
        </aside>
      )}

      <section className="gf-review-detail__authority" aria-labelledby="review-authority-title">
        <header>
          <FileKey2 aria-hidden="true" size={22} />
          <div>
            <h2 id="review-authority-title">Exact authority ledger</h2>
            <p>Preview/constraint 经 direct lineage 与专用读取对拍；Artifact 存在本身不表示当前 ref。</p>
          </div>
        </header>
        <dl>
          <div>
            <dt>Review snapshot</dt>
            <dd>
              <CopyableText copyLabel="复制 Review snapshot" value={bound.review.report.snapshot_id} />
            </dd>
          </div>
          <div>
            <dt>Exact preview</dt>
            <dd>
              <a href={`/specs/${encodeURIComponent(bound.preview.artifact_id)}`}>打开 exact preview</a>
            </dd>
          </div>
          <div>
            <dt>Exact constraint</dt>
            <dd>
              {bound.constraint ? (
                <a href={`/constraints/${encodeURIComponent(bound.constraint.artifact_id)}`}>
                  打开 exact constraint
                </a>
              ) : (
                "不适用 (N/A)"
              )}
            </dd>
          </div>
          <div>
            <dt>Producer occurrence</dt>
            <dd>
              {authorityOccurrence ? (
                <a href={`/runs/${encodeURIComponent(authorityOccurrence.run_id)}`}>
                  打开 Review producer Run
                </a>
              ) : (
                "未取得 verified producer occurrence；未从工具版本猜测 producer"
              )}
            </dd>
          </div>
          <div>
            <dt>Finding authority</dt>
            <dd>
              {bound.findingAuthority === "exact-run-links"
                ? "Run-scoped immutable links + digest + evidence Artifact"
                : bound.findingAuthority === "embedded-only"
                  ? "仅报告内嵌 Finding；未伪造 revision"
                  : "不适用 (N/A)：报告没有 Finding"}
            </dd>
          </div>
          <div>
            <dt>Terminal manifest</dt>
            <dd>
              {authorityOccurrence ? (
                <a href={`/artifacts/${encodeURIComponent(authorityOccurrence.terminal_manifest_id)}`}>
                  {authorityOccurrence.terminal_manifest_kind} · {authorityOccurrence.terminal_status}
                </a>
              ) : (
                "不适用 (N/A)"
              )}
            </dd>
          </div>
          <div>
            <dt>Frozen outcome policy</dt>
            <dd>
              {authorityOccurrence
                ? `${authorityOccurrence.outcome_policy_id}@${authorityOccurrence.outcome_policy_version} · ${authorityOccurrence.outcome_rule_id} · ${authorityOccurrence.manifest_role}`
                : "不适用 (N/A)"}
            </dd>
          </div>
          {sourceIsDistinctOccurrence && bound.sourceProducerBinding && (
            <div>
              <dt>Explicit source occurrence</dt>
              <dd>
                <a href={`/runs/${encodeURIComponent(bound.sourceProducerBinding.run_id)}`}>
                  {bound.sourceProducerBinding.run_id}
                </a>
                {` · ${bound.sourceProducerBinding.outcome_policy_id}@${bound.sourceProducerBinding.outcome_policy_version} · ${bound.sourceProducerBinding.outcome_rule_id} · ${bound.sourceProducerBinding.manifest_role}`}
              </dd>
            </div>
          )}
          <div>
            <dt>Artifact lineage</dt>
            <dd>
              <a href={`/artifacts/${encodeURIComponent(bound.review.artifact.artifact_id)}/lineage`}>
                查看完整血缘
              </a>
            </dd>
          </div>
        </dl>
      </section>

      <section className="gf-review-detail__tool" aria-labelledby="review-tool-title">
        <header>
          <Braces aria-hidden="true" size={22} />
          <div>
            <h2 id="review-tool-title">Frozen VersionTuple</h2>
            <p>这里展示 immutable 工具身份；不冒充未公开的 current review profile。</p>
          </div>
        </header>
        <dl>
          {[
            ["tool_version", tuple.tool_version],
            ["model_snapshot", tuple.model_snapshot],
            ["prompt_version", tuple.prompt_version],
            ["agent_graph_version", tuple.agent_graph_version],
            ["seed", tuple.seed],
            ["cassette_id", tuple.cassette_id],
          ].map(([label, value]) => (
            <div key={String(label)}>
              <dt>{label}</dt>
              <dd>
                <code>{versionValue(value)}</code>
              </dd>
            </div>
          ))}
        </dl>
      </section>

      {bound.review.report.by_defect_class && bound.review.report.by_defect_class.length > 0 && (
        <section className="gf-review-detail__classes" aria-labelledby="review-classes-title">
          <header>
            <GitBranch aria-hidden="true" size={22} />
            <h2 id="review-classes-title">Defect class index</h2>
          </header>
          <ul>
            {bound.review.report.by_defect_class.map((item) => (
              <li key={`${item.defect_class}:${item.severity}`}>
                <code>{item.defect_class}</code>
                <span>{item.severity}</span>
                <strong>{item.count}</strong>
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
