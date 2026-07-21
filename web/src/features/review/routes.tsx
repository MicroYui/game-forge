import { Navigate, useParams, useSearchParams } from "react-router-dom";

import { FindingDetailPage } from "./FindingDetailPage";
import { ReviewDetailPage } from "./ReviewDetailPage";
import { ReviewWorkspacePage } from "./ReviewWorkspacePage";

export function ReviewWorkspaceRoute() {
  return <ReviewWorkspacePage />;
}

export function ReviewDetailRoute() {
  const { artifactId } = useParams<{ artifactId: string }>();
  const [searchParams] = useSearchParams();
  if (!artifactId) return <Navigate replace to="/reviews" />;
  return (
    <ReviewDetailPage
      artifactId={artifactId}
      snapshotContextArtifactId={searchParams.get("snapshot") ?? undefined}
      sourceRunId={searchParams.get("sourceRun") ?? undefined}
    />
  );
}

export function FindingDetailRoute() {
  const { findingId, revision: revisionParam } = useParams<{
    findingId: string;
    revision: string;
  }>();
  const revision = Number(revisionParam);
  if (!findingId || !Number.isSafeInteger(revision) || revision < 1 || String(revision) !== revisionParam) {
    return <Navigate replace to="/reviews" />;
  }
  return <FindingDetailPage findingId={findingId} revision={revision} />;
}
