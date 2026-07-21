import { Navigate, useParams } from "react-router-dom";

import { ObservabilityPage } from "./ObservabilityPage";
import { TraceDetailPage } from "./TraceDetailPage";

export function ObservabilityRoute() {
  return <ObservabilityPage />;
}

export function TraceDetailRoute() {
  const { traceId } = useParams<{ traceId: string }>();
  if (!traceId) return <Navigate replace to="/observability" />;
  return <TraceDetailPage traceId={traceId} />;
}
