import type { ApprovalAction, ApprovalViewData } from "./api";

export type ApprovalItemData = ApprovalViewData["approval"];
export type ApprovalProgressData = ApprovalViewData["requirement_progress"][number];
export type ApprovalRequirementData = ApprovalItemData["requirements"][number];
export type DecisionEligibilityData = ApprovalProgressData["decision_eligibility"][number];
export type EligibilityReasonCode = DecisionEligibilityData["reason_codes"][number];

export interface RequirementRow {
  progress: ApprovalProgressData;
  requirement: ApprovalRequirementData;
}

const reasonLabels: Readonly<Record<EligibilityReasonCode, string>> = {
  actor_already_decided_requirement: "当前身份已经对此审批职责作出决定",
  actor_not_active_human: "当前身份不是可用的人类身份",
  actor_not_assigned: "当前身份没有被指派到此审批职责",
  distinct_requirement_conflict: "当前身份已经承担了必须由另一人完成的审批职责",
  maker_checker_conflict: "职责隔离：提议者不能审批自己的提议",
  permission_denied: "当前权限没有覆盖这项审批职责的全部内容域",
  requirement_already_satisfied: "这项审批职责已经获得足够的有效批准",
  route_role_missing: "当前身份缺少冻结路由角色",
  workflow_not_pending: "审批已经不处于待审批状态",
};

export const actionLabels: Readonly<Record<ApprovalAction, string>> = {
  approve: "批准",
  reject: "驳回",
  request_changes: "请修改",
};

export function eligibilityReasonLabel(reason: EligibilityReasonCode): string {
  return reasonLabels[reason];
}

export function actionEligibility(
  progress: ApprovalProgressData,
  action: ApprovalAction,
): DecisionEligibilityData {
  const projection = progress.decision_eligibility.find((item) => item.decision === action);
  if (!projection) throw new Error(`Approval progress omitted action eligibility: ${action}`);
  return projection;
}

export function requirementRows(view: ApprovalViewData): readonly RequirementRow[] {
  const requirements = new Map(
    view.approval.requirements.map((requirement) => [requirement.requirement_id, requirement]),
  );
  return view.requirement_progress.map((progress) => {
    const requirement = requirements.get(progress.requirement_id);
    if (!requirement) {
      throw new Error(`Approval progress has no frozen requirement: ${progress.requirement_id}`);
    }
    return { progress, requirement };
  });
}

export function selectableRequirementIds(view: ApprovalViewData, action: ApprovalAction): readonly string[] {
  return view.requirement_progress
    .filter((progress) => actionEligibility(progress, action).eligible)
    .map((progress) => progress.requirement_id);
}

export function selectionIsEligible(
  view: ApprovalViewData,
  action: ApprovalAction,
  requirementIds: readonly string[],
): boolean {
  if (requirementIds.length === 0) return false;
  const selectable = new Set(selectableRequirementIds(view, action));
  return requirementIds.every((requirementId) => selectable.has(requirementId));
}

export function approvalSubjectHref(item: ApprovalItemData): string {
  const encoded = encodeURIComponent(item.subject_artifact_id);
  if (item.subject_kind === "patch") return `/patches/${encoded}`;
  if (item.subject_kind === "constraint_proposal") return `/constraint-proposals/${encoded}`;
  return `/rollback-requests/${encoded}`;
}
