import { Navigate, useParams } from "react-router-dom";

import { PatchDetailPage } from "./PatchDetailPage";
import { PatchWorkspacePage } from "./PatchWorkspacePage";
import { RefHistoryPage } from "./RefHistoryPage";
import { RollbackDetailPage } from "./RollbackDetailPage";

export function PatchWorkspaceRoute() {
  return <PatchWorkspacePage />;
}

export function PatchDetailRoute() {
  const { patchId } = useParams<{ patchId: string }>();
  if (!patchId) return <Navigate replace to="/patches" />;
  return <PatchDetailPage artifactId={patchId} key={patchId} />;
}

export function RefHistoryRoute() {
  const { refName } = useParams<{ refName: string }>();
  if (!refName) return <Navigate replace to="/patches" />;
  return <RefHistoryPage key={refName} refName={refName} />;
}

export function RollbackDetailRoute() {
  const { artifactId } = useParams<{ artifactId: string }>();
  if (!artifactId) return <Navigate replace to="/patches" />;
  return <RollbackDetailPage artifactId={artifactId} key={artifactId} />;
}
