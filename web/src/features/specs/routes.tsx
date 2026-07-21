import { Navigate, useParams } from "react-router-dom";

import { ConstraintProposalPage } from "./ConstraintProposalPage";
import { ConstraintSnapshotPage } from "./ConstraintSnapshotPage";
import { SpecDetailPage } from "./SpecDetailPage";
import { SpecWorkspacePage } from "./SpecWorkspacePage";

export function SpecWorkspaceRoute() {
  return <SpecWorkspacePage />;
}

export function SpecDetailRoute() {
  const { artifactId } = useParams<{ artifactId: string }>();
  if (!artifactId) return <Navigate replace to="/specs" />;
  return <SpecDetailPage artifactId={artifactId} />;
}

export function ConstraintSnapshotRoute() {
  const { artifactId } = useParams<{ artifactId: string }>();
  if (!artifactId) return <Navigate replace to="/specs?section=constraints" />;
  return <ConstraintSnapshotPage artifactId={artifactId} />;
}

export function ConstraintProposalRoute() {
  const { artifactId } = useParams<{ artifactId: string }>();
  if (!artifactId) return <Navigate replace to="/specs?section=proposals" />;
  return <ConstraintProposalPage artifactId={artifactId} />;
}
