import { useQuery } from "@tanstack/react-query";
import { FileSearch2, GitCommitHorizontal } from "lucide-react";

import { ApiProblemError } from "../../api/problem";
import { FindingCard } from "../../components/evidence";
import { CopyableText } from "../../components/tables";
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
            重新读取 exact revision
          </button>
        }
        description={error.message}
        headingLevel={1}
        state="error"
        title="Finding 权威闭合失败"
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
      description="Finding immutable revision 读取失败；未显示底层异常。"
      headingLevel={1}
      state="error"
      title="无法读取 Finding 修订"
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
          description="正在读取 route 指定的 immutable Finding revision。"
          headingLevel={1}
          state="loading"
          title="正在读取 Finding 修订"
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
          <p className="gf-review__kicker">Exact finding history · no latest fallback</p>
          <h1>Finding immutable revision</h1>
          <CopyableText copyLabel="复制 Finding detail ID" value={finding.finding_id} />
        </div>
        <FileSearch2 aria-hidden="true" size={34} />
      </header>

      <section className="gf-finding-detail__revision" aria-labelledby="finding-revision-title">
        <header>
          <GitCommitHorizontal aria-hidden="true" size={22} />
          <h2 id="finding-revision-title">Revision binding</h2>
        </header>
        <dl>
          <div>
            <dt>Requested revision</dt>
            <dd>{finding.revision}</dd>
          </div>
          <div>
            <dt>Supersedes</dt>
            <dd>{finding.supersedes_revision ?? "初始修订"}</dd>
          </div>
          <div>
            <dt>Persisted at</dt>
            <dd>{finding.created_at}</dd>
          </div>
          <div>
            <dt>Producer Run</dt>
            <dd>
              <a href={`/runs/${encodeURIComponent(finding.payload.producer_run_id)}`}>打开 producer Run</a>
            </dd>
          </div>
        </dl>
      </section>

      <FindingCard finding={finding} />
    </div>
  );
}
