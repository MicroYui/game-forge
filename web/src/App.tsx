import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell, LoginPage, NotFoundPage, RequireAuth, RunDetailRoute } from "./app/router";
import { ApprovalDetailRoute, ApprovalsRoute } from "./features/approvals";
import { ArtifactDetailRoute, ArtifactLineageRoute } from "./features/artifacts";
import { EvalRoute } from "./features/eval";
import { GenerationRoute } from "./features/generation";
import { ObservabilityRoute, TraceDetailRoute } from "./features/observability";
import {
  PatchDetailRoute,
  PatchWorkspaceRoute,
  RefHistoryRoute,
  RollbackDetailRoute,
} from "./features/patches";
import { PlaytestRoute } from "./features/playtest";
import { FindingDetailRoute, ReviewDetailRoute, ReviewWorkspaceRoute } from "./features/review";
import {
  ConstraintProposalRoute,
  ConstraintSnapshotRoute,
  SpecDetailRoute,
  SpecWorkspaceRoute,
} from "./features/specs";
const VisualFoundationPage = import.meta.env.DEV
  ? lazy(() =>
      import("./features/visual-foundation").then((module) => ({ default: module.VisualFoundationPage })),
    )
  : null;

export default function App() {
  return (
    <Routes>
      <Route element={<LoginPage />} path="/login" />
      <Route element={<RequireAuth />}>
        <Route element={<AppShell />}>
          <Route element={<Navigate replace to="/specs" />} index />
          <Route element={<SpecWorkspaceRoute />} path="/specs" />
          <Route element={<SpecDetailRoute />} path="/specs/:artifactId" />
          <Route element={<ConstraintSnapshotRoute />} path="/constraints/:artifactId" />
          <Route element={<ConstraintProposalRoute />} path="/constraint-proposals/:artifactId" />
          <Route element={<GenerationRoute />} path="/generation" />
          <Route element={<ReviewWorkspaceRoute />} path="/reviews" />
          <Route element={<ReviewDetailRoute />} path="/reviews/:artifactId" />
          <Route element={<PlaytestRoute />} path="/playtest" />
          <Route element={<PatchWorkspaceRoute />} path="/patches" />
          <Route element={<EvalRoute />} path="/eval" />
          <Route element={<ObservabilityRoute />} path="/observability" />
          <Route element={<ApprovalsRoute />} path="/approvals" />
          <Route element={<RunDetailRoute />} path="/runs/:runId" />
          <Route element={<FindingDetailRoute />} path="/findings/:findingId/revisions/:revision" />
          <Route element={<PatchDetailRoute />} path="/patches/:patchId" />
          <Route element={<RollbackDetailRoute />} path="/rollback-requests/:artifactId" />
          <Route element={<ApprovalDetailRoute />} path="/approvals/:approvalId" />
          <Route element={<ArtifactDetailRoute />} path="/artifacts/:artifactId" />
          <Route element={<ArtifactLineageRoute />} path="/artifacts/:artifactId/lineage" />
          <Route element={<RefHistoryRoute />} path="/refs/:refName/history" />
          <Route element={<TraceDetailRoute />} path="/observability/traces/:traceId" />
          {VisualFoundationPage !== null && (
            <Route
              element={
                <Suspense fallback={<p className="gf-page">正在载入视觉评审…</p>}>
                  <VisualFoundationPage />
                </Suspense>
              }
              path="/__visual__/foundation"
            />
          )}
          <Route element={<NotFoundPage />} path="*" />
        </Route>
      </Route>
    </Routes>
  );
}
