import { useQuery } from "@tanstack/react-query";
import { FileSearch2, GitCommitHorizontal } from "lucide-react";

import { ApiProblemError } from "../../api/problem";
import { FindingCard } from "../../components/evidence";
import { compactDateTime, TechnicalDetails } from "../../components/identity";
import { ProblemPanel, StatePanel } from "../../components/ui";
import { requireExactFindingRoute, ReviewAuthorityError } from "./authority";
import { reviewApi, type ReviewApi } from "./api";
import "./review.css";

function FindingError({ error, onRetry }: { error: Error; onRetry(): void }) {
  if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
  if (error instanceof ReviewAuthorityError) {
    return (
      <StatePanel
        action={
          <button className="gf-secondary-button" onClick={onRetry} type="button">
            重新读取此版本
          </button>
        }
        description={error.message}
        headingLevel={1}
        state="error"
        title="问题版本校验失败"
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
      description="无法读取指定的问题版本。"
      headingLevel={1}
      state="error"
      title="无法读取问题版本"
    />
  );
}

export function FindingDetailPage({
  api = reviewApi,
  findingId,
  revision,
}: {
  api?: ReviewApi;
  findingId: string;
  revision: number;
}) {
  const query = useQuery({
    queryFn: async () =>
      requireExactFindingRoute(await api.getFinding(findingId, revision), findingId, revision),
    queryKey: ["finding-revision", findingId, revision],
    retry: false,
  });

  if (query.isPending) {
    return (
      <div className="gf-page gf-review">
        <StatePanel
          description="正在读取指定的问题历史版本。"
          headingLevel={1}
          state="loading"
          title="正在读取问题版本"
        />
      </div>
    );
  }
  if (query.isError) {
    return (
      <div className="gf-page gf-review">
        <FindingError error={query.error} onRetry={() => void query.refetch()} />
      </div>
    );
  }

  const finding = query.data;
  return (
    <div className="gf-page gf-review gf-finding-detail" data-layout="editorial-finding-detail">
      <header className="gf-finding-detail__hero">
        <div>
          <p className="gf-review__kicker">问题历史</p>
          <h1>问题详情</h1>
          <p>页面固定展示第 {finding.revision} 版，不会自动替换成更新版本。</p>
        </div>
        <FileSearch2 aria-hidden="true" size={34} />
      </header>

      <section className="gf-finding-detail__revision" aria-labelledby="finding-revision-title">
        <header>
          <GitCommitHorizontal aria-hidden="true" size={22} />
          <h2 id="finding-revision-title">版本信息</h2>
        </header>
        <dl>
          <div>
            <dt>当前查看</dt>
            <dd>第 {finding.revision} 版</dd>
          </div>
          <div>
            <dt>上一版本</dt>
            <dd>
              {finding.supersedes_revision == null ? "这是首个版本" : `第 ${finding.supersedes_revision} 版`}
            </dd>
          </div>
          <div>
            <dt>保存时间</dt>
            <dd>{compactDateTime(finding.created_at)}</dd>
          </div>
          <div>
            <dt>生成记录</dt>
            <dd>
              <a href={`/runs/${encodeURIComponent(finding.payload.producer_run_id)}`}>查看生成记录</a>
            </dd>
          </div>
        </dl>
      </section>

      <TechnicalDetails
        items={[
          { label: "问题 ID", value: finding.finding_id },
          { label: "生成运行 ID", value: finding.payload.producer_run_id },
          { label: "内容快照 ID", value: finding.payload.snapshot_id },
        ]}
        summary="查看问题版本技术信息"
      />

      <FindingCard finding={finding} />
    </div>
  );
}
