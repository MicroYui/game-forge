import { Navigate, useParams } from "react-router-dom";

import { ApprovalDetailPage, ApprovalsPage } from "./ApprovalsPage";

export function ApprovalsRoute() {
  return <ApprovalsPage />;
}

export function ApprovalDetailRoute() {
  const { approvalId } = useParams<{ approvalId: string }>();
  if (!approvalId) return <Navigate replace to="/approvals" />;
  return <ApprovalDetailPage approvalId={approvalId} key={approvalId} />;
}
