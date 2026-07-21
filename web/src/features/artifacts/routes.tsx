import { Navigate, useParams } from "react-router-dom";

import { ArtifactDetailPage } from "./ArtifactDetailPage";

function ArtifactRoute({ routeMode }: { routeMode: "detail" | "lineage" }) {
  const { artifactId } = useParams<{ artifactId: string }>();
  if (!artifactId) return <Navigate replace to="/specs" />;
  return <ArtifactDetailPage artifactId={artifactId} routeMode={routeMode} />;
}

export function ArtifactDetailRoute() {
  return <ArtifactRoute routeMode="detail" />;
}

export function ArtifactLineageRoute() {
  return <ArtifactRoute routeMode="lineage" />;
}
