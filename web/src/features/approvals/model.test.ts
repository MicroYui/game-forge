import { describe, expect, it } from "vitest";

import type { ApprovalViewData } from "./api";
import {
  actionEligibility,
  approvalSubjectHref,
  eligibilityReasonLabel,
  requirementRows,
  selectableRequirementIds,
  selectionIsEligible,
} from "./model";

function view(): ApprovalViewData {
  return {
    approval: {
      approval_id: "approval:multi-domain:7",
      approval_policy: { policy_digest: "a".repeat(64), policy_version: "approval-policy@1" },
      approval_schema_version: "approval@1",
      created_at: "2026-07-20T02:00:00Z",
      decisions: [],
      domain_registry_ref: { registry_digest: "b".repeat(64), registry_version: "domains@7" },
      domain_scope: { domain_ids: ["domain:economy", "domain:narrative"] },
      proposer: { principal_id: "human:alice", principal_kind: "human" },
      regression_evidence_artifact_ids: [],
      requirements: [
        {
          assignee_principal_ids: ["human:bob"],
          distinct_from_requirement_ids: ["requirement:narrative"],
          domain_scope: { domain_ids: ["domain:economy"] },
          min_approvals: 2,
          required_permission: {
            action: "approve",
            domain_scope: { domain_ids: ["domain:economy"] },
            resource_kind: "patch",
          },
          requirement_id: "requirement:economy",
          route_role: "numeric_designer",
        },
        {
          assignee_principal_ids: [],
          distinct_from_requirement_ids: ["requirement:economy"],
          domain_scope: { domain_ids: ["domain:narrative"] },
          min_approvals: 1,
          required_permission: {
            action: "approve",
            domain_scope: { domain_ids: ["domain:narrative"] },
            resource_kind: "patch",
          },
          requirement_id: "requirement:narrative",
          route_role: "content_designer",
        },
      ],
      role_policy_digest: "c".repeat(64),
      role_policy_version: "roles@9",
      route_policy: {
        domain_registry_ref: { registry_digest: "b".repeat(64), registry_version: "domains@7" },
        route_digest: "d".repeat(64),
        route_version: "routes@4",
      },
      status: "pending_approval",
      subject_artifact_id: "artifact:patch:7",
      subject_digest: "e".repeat(64),
      subject_kind: "patch",
      subject_revision: 3,
      subject_series_id: "patch-series:7",
      workflow_revision: 12,
    },
    current_actor_allowed_requirement_ids: ["requirement:economy", "requirement:narrative"],
    requirement_progress: [
      {
        decision_eligibility: [
          { decision: "approve", eligible: true, reason_codes: [] },
          { decision: "reject", eligible: true, reason_codes: [] },
          { decision: "request_changes", eligible: true, reason_codes: [] },
        ],
        domain_scope: { domain_ids: ["domain:economy"] },
        eligible_for_current_actor: true,
        min_approvals: 2,
        requirement_id: "requirement:economy",
        route_role: "numeric_designer",
        satisfied: false,
        unmet_distinct_from_requirement_ids: ["requirement:narrative"],
        valid_approval_count: 1,
      },
      {
        decision_eligibility: [
          {
            decision: "approve",
            eligible: false,
            reason_codes: ["distinct_requirement_conflict"],
          },
          { decision: "reject", eligible: true, reason_codes: [] },
          { decision: "request_changes", eligible: true, reason_codes: [] },
        ],
        domain_scope: { domain_ids: ["domain:narrative"] },
        eligible_for_current_actor: true,
        min_approvals: 1,
        requirement_id: "requirement:narrative",
        route_role: "content_designer",
        satisfied: false,
        unmet_distinct_from_requirement_ids: ["requirement:economy"],
        valid_approval_count: 0,
      },
    ],
    view_schema_version: "approval-view@1",
  } as ApprovalViewData;
}

describe("approval view model", () => {
  it("joins each exact progress row to its frozen requirement without deriving authority", () => {
    const rows = requirementRows(view());

    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({
      requirement: {
        assignee_principal_ids: ["human:bob"],
        requirement_id: "requirement:economy",
      },
      progress: {
        valid_approval_count: 1,
      },
    });
  });

  it("uses only the server action projection instead of the aggregate eligibility field", () => {
    const narrative = view().requirement_progress[1]!;

    expect(narrative.eligible_for_current_actor).toBe(true);
    expect(actionEligibility(narrative, "approve")).toEqual({
      decision: "approve",
      eligible: false,
      reason_codes: ["distinct_requirement_conflict"],
    });
    expect(selectableRequirementIds(view(), "approve")).toEqual(["requirement:economy"]);
    expect(selectableRequirementIds(view(), "reject")).toEqual([
      "requirement:economy",
      "requirement:narrative",
    ]);
  });

  it("keeps stale selected input but refuses to call it eligible after refreshed authority changes", () => {
    const refreshed = structuredClone(view());
    const economy = refreshed.requirement_progress[0]!;
    economy.decision_eligibility = economy.decision_eligibility.map((entry) =>
      entry.decision === "approve"
        ? { decision: "approve", eligible: false, reason_codes: ["route_role_missing"] }
        : entry,
    );

    expect(selectionIsEligible(refreshed, "approve", ["requirement:economy"])).toBe(false);
    expect(selectionIsEligible(refreshed, "approve", [])).toBe(false);
    expect(eligibilityReasonLabel("route_role_missing")).toContain("冻结路由角色");
  });

  it("links every subject kind to its existing governed detail route", () => {
    expect(approvalSubjectHref(view().approval)).toBe("/patches/artifact%3Apatch%3A7");
    expect(
      approvalSubjectHref({
        ...view().approval,
        subject_artifact_id: "artifact:constraint:7",
        subject_kind: "constraint_proposal",
      }),
    ).toBe("/constraint-proposals/artifact%3Aconstraint%3A7");
    expect(
      approvalSubjectHref({
        ...view().approval,
        subject_artifact_id: "artifact:rollback:7",
        subject_kind: "rollback_request",
      }),
    ).toBe("/rollback-requests/artifact%3Arollback%3A7");
  });
});
