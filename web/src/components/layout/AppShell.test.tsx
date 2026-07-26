import { describe, expect, it } from "vitest";

import type { AuthenticatedPrincipal } from "../../app/auth-types";
import { activeRoleNames } from "./AppShell";

function assignment(
  overrides: Partial<AuthenticatedPrincipal["roles"][number]>,
): AuthenticatedPrincipal["roles"][number] {
  return {
    assignment_id: "assignment:1",
    assignment_schema_version: "role-assignment@1",
    granted_at: "2026-07-26T00:00:00Z",
    granted_by: { principal_id: "system:bootstrap", principal_kind: "system" },
    principal_id: "human:admin",
    revision: 1,
    role: "tooling",
    scope: "all",
    status: "active",
    ...overrides,
  };
}

describe("activeRoleNames", () => {
  it("names a platform role once even when it holds several scoped assignments", () => {
    // A platform-wide role needs one global and one all-domain assignment; that is
    // an authorization detail, not two identities for a planner to read.
    expect(
      activeRoleNames([
        assignment({ assignment_id: "a:global", role: "platform_admin", scope: null }),
        assignment({ assignment_id: "a:all", role: "platform_admin", scope: "all" }),
        assignment({ assignment_id: "a:tooling", role: "tooling" }),
      ]),
    ).toEqual(["platform_admin", "tooling"]);
  });

  it("drops revoked and inactive assignments", () => {
    expect(
      activeRoleNames([
        assignment({ assignment_id: "a:revoked", role: "qa", revoked_at: "2026-07-26T01:00:00Z" }),
        assignment({ assignment_id: "a:inactive", role: "content_designer", status: "revoked" }),
        assignment({ assignment_id: "a:active", role: "tooling" }),
      ]),
    ).toEqual(["tooling"]);
  });

  it("keeps the order the server reported", () => {
    expect(
      activeRoleNames([
        assignment({ assignment_id: "a:1", role: "numeric_designer" }),
        assignment({ assignment_id: "a:2", role: "content_designer" }),
      ]),
    ).toEqual(["numeric_designer", "content_designer"]);
  });
});
