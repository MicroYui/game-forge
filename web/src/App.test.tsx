import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import canonicalReport from "../../scenarios/bench/bench-report.json";
import { createQueryClient } from "./api/query-client";
import { ApiProblemError, type SafeProblem } from "./api/problem";
import App from "./App";
import {
  AuthProvider,
  ThemeProvider,
  ToastProvider,
  type AuthApi,
  type SessionBoundarySubscriber,
} from "./app/providers";
import type { AuthenticatedPrincipal } from "./app/auth-types";
import { messages } from "./i18n/zh-CN";

const principal: AuthenticatedPrincipal = {
  authz_revision: 1,
  credential_epoch: 1,
  display_name: "林澄",
  id: "principal:lincheng",
  kind: "human",
  revision: 1,
  roles: [
    {
      assignment_id: "role:tooling",
      assignment_schema_version: "role-assignment@1",
      granted_at: "2026-07-19T00:00:00Z",
      granted_by: { principal_id: "system:bootstrap", principal_kind: "system" },
      principal_id: "principal:lincheng",
      revision: 1,
      role: "tooling",
      scope: "all",
      status: "active",
    },
  ],
  status: "active",
};

afterEach(() => vi.unstubAllGlobals());

function unauthorized(): ApiProblemError {
  return new ApiProblemError({
    code: "auth_required",
    conflict_set_id: null,
    detail: "Authentication required.",
    earliest_cursor: null,
    instance: "/api/v1/auth/me",
    request_id: "request:auth",
    retry_after_s: null,
    run_id: null,
    status: 401,
    title: "Authentication required",
    trace_id: null,
    type: "about:blank",
  } satisfies SafeProblem);
}

function createAuthApi(overrides: Partial<AuthApi> = {}): AuthApi {
  return {
    login: vi.fn().mockResolvedValue(undefined),
    logout: vi.fn().mockResolvedValue(undefined),
    me: vi.fn().mockResolvedValue(principal),
    ...overrides,
  };
}

function stubEmptySpecWorkspace() {
  const emptyPage = (readSnapshotId: string) => ({
    expires_at: "2026-07-19T12:00:00Z",
    items: [],
    next_cursor: null,
    page_schema_version: "page@1",
    read_snapshot_id: readSnapshotId,
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const rawUrl = input instanceof Request ? input.url : input.toString();
      const pathname = new URL(rawUrl, "https://gameforge.test").pathname;
      const collection = pathname.match(
        /^\/api\/v1\/(specs|constraints|constraint-proposals|execution-profiles)$/,
      )?.[1];
      if (collection) {
        return Response.json(emptyPage(`read:app:${collection}`));
      }
      throw new Error(`Unexpected App test request: ${pathname}`);
    }),
  );
}

function stubEmptyReviewWorkspace() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const rawUrl = input instanceof Request ? input.url : input.toString();
      const pathname = new URL(rawUrl, "https://gameforge.test").pathname;
      if (pathname === "/api/v1/reviews") {
        return Response.json({
          expires_at: "2026-07-20T12:00:00Z",
          items: [],
          next_cursor: null,
          page_schema_version: "page@1",
          read_snapshot_id: "read:app:reviews",
        });
      }
      throw new Error(`Unexpected Review App test request: ${pathname}`);
    }),
  );
}

function stubEmptyPlaytestWorkspace() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const rawUrl = input instanceof Request ? input.url : input.toString();
      const pathname = new URL(rawUrl, "https://gameforge.test").pathname;
      if (pathname === "/api/v1/task-suites" || pathname === "/api/v1/execution-profiles") {
        return Response.json({
          expires_at: "2026-07-20T12:00:00Z",
          items: [],
          next_cursor: null,
          page_schema_version: "page@1",
          read_snapshot_id: `read:app:${pathname.slice(pathname.lastIndexOf("/") + 1)}`,
        });
      }
      throw new Error(`Unexpected Playtest App test request: ${pathname}`);
    }),
  );
}

function stubEmptyPatchWorkspace() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const rawUrl = input instanceof Request ? input.url : input.toString();
      const pathname = new URL(rawUrl, "https://gameforge.test").pathname;
      if (pathname === "/api/v1/patches" || pathname === "/api/v1/rollback-requests") {
        return Response.json({
          expires_at: "2026-07-20T12:00:00Z",
          items: [],
          next_cursor: null,
          page_schema_version: "page@1",
          read_snapshot_id: `read:app:${pathname.slice(pathname.lastIndexOf("/") + 1)}`,
        });
      }
      throw new Error(`Unexpected Patch App test request: ${pathname}`);
    }),
  );
}

function stubEmptyApprovalWorkspace() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const rawUrl = input instanceof Request ? input.url : input.toString();
      const pathname = new URL(rawUrl, "https://gameforge.test").pathname;
      if (pathname === "/api/v1/approvals") {
        return Response.json({
          expires_at: "2026-07-20T12:00:00Z",
          items: [],
          next_cursor: null,
          page_schema_version: "page@1",
          read_snapshot_id: "read:app:approvals",
        });
      }
      throw new Error(`Unexpected Approval App test request: ${pathname}`);
    }),
  );
}

function decodeCanonicalFloats(value: unknown): unknown {
  if (typeof value === "string" && value.startsWith("f:")) return Number(value.slice(2));
  if (Array.isArray(value)) return value.map(decodeCanonicalFloats);
  if (typeof value === "object" && value !== null) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, decodeCanonicalFloats(item)]));
  }
  return value;
}

function stubBenchReport() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const rawUrl = input instanceof Request ? input.url : input.toString();
      const pathname = new URL(rawUrl, "https://gameforge.test").pathname;
      if (pathname === "/api/v1/bench/report") {
        return Response.json(decodeCanonicalFloats(structuredClone(canonicalReport)), {
          headers: {
            ETag: '"bench-report:app"',
            "X-Artifact-ID": "artifact:bench-report:app",
          },
        });
      }
      throw new Error(`Unexpected Eval App test request: ${pathname}`);
    }),
  );
}

function renderApp(
  path = "/",
  api: AuthApi = createAuthApi(),
  subscribeToSessionBoundary?: SessionBoundarySubscriber,
) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <ThemeProvider>
          <ToastProvider>
            <AuthProvider api={api} subscribeToSessionBoundary={subscribeToSessionBoundary}>
              <App />
            </AuthProvider>
          </ToastProvider>
        </ThemeProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("App", () => {
  it("exposes the eight first-class routes in an authenticated semantic shell", async () => {
    stubEmptySpecWorkspace();
    renderApp();

    expect(await screen.findByRole("heading", { level: 1, name: "规格与约束快照" })).toBeVisible();
    expect(screen.getByRole("link", { name: messages.shell.skipToContent })).toHaveAttribute(
      "href",
      "#main-content",
    );
    expect(screen.getByRole("navigation", { name: messages.shell.primaryNavigation })).toBeVisible();
    expect(screen.getByRole("navigation", { name: messages.shell.breadcrumbs })).toBeVisible();
    expect(screen.getByRole("main")).toBeVisible();
    for (const label of Object.values(messages.routes)) {
      expect(screen.getByRole("link", { name: label })).toBeVisible();
    }
    expect(screen.getByText("林澄")).toBeVisible();
    expect(screen.getByText(messages.roles.tooling)).toBeVisible();
    expect(screen.getByRole("button", { name: messages.theme.dark })).toHaveAttribute(
      "data-tooltip",
      messages.theme.dark,
    );
    expect(screen.getByRole("button", { name: messages.auth.signOut })).toHaveAttribute(
      "data-tooltip",
      messages.auth.signOut,
    );
  });

  it("keeps the production Run detail route under the application shell", async () => {
    renderApp("/runs/run%3Aroute");

    expect(await screen.findByRole("navigation", { name: messages.shell.primaryNavigation })).toBeVisible();
    expect(await screen.findByRole("heading", { name: "运行详情暂不可用" })).toBeVisible();
    expect(screen.getByRole("main")).toContainElement(
      screen.getByRole("heading", { name: "运行详情暂不可用" }),
    );
    expect(screen.getByRole("navigation", { name: messages.shell.breadcrumbs })).toHaveTextContent(
      messages.details.run,
    );
  });

  it("mounts the production Generation route under the application shell", async () => {
    stubEmptySpecWorkspace();
    renderApp("/generation");

    expect(await screen.findByRole("heading", { level: 1, name: "内容生成" })).toBeVisible();
    expect(screen.getByRole("main")).toContainElement(
      screen.getByRole("heading", { level: 1, name: "内容生成" }),
    );
    expect(screen.getByRole("navigation", { name: messages.shell.breadcrumbs })).toHaveTextContent(
      messages.routes.generation,
    );
  });

  it("mounts the production Review route under the application shell", async () => {
    stubEmptyReviewWorkspace();
    renderApp("/reviews");

    expect(await screen.findByRole("heading", { level: 1, name: "审查报告" })).toBeVisible();
    expect(screen.getByRole("main")).toContainElement(
      screen.getByRole("heading", { level: 1, name: "审查报告" }),
    );
    expect(screen.getByRole("navigation", { name: messages.shell.breadcrumbs })).toHaveTextContent(
      messages.routes.reviews,
    );
  });

  it("mounts the production Playtest route under the application shell", async () => {
    stubEmptyPlaytestWorkspace();
    renderApp("/playtest");

    expect(await screen.findByRole("heading", { level: 1, name: "自动试玩" })).toBeVisible();
    expect(screen.getByRole("main")).toContainElement(
      screen.getByRole("heading", { level: 1, name: "自动试玩" }),
    );
    expect(screen.getByRole("navigation", { name: messages.shell.breadcrumbs })).toHaveTextContent(
      messages.routes.playtest,
    );
  });

  it("mounts the production Patch / Diff route under the application shell", async () => {
    stubEmptyPatchWorkspace();
    renderApp("/patches");

    expect(await screen.findByRole("heading", { level: 1, name: "Patch / Diff" })).toBeVisible();
    expect(screen.getByRole("main")).toContainElement(
      screen.getByRole("heading", { level: 1, name: "Patch / Diff" }),
    );
    expect(screen.getByRole("navigation", { name: messages.shell.breadcrumbs })).toHaveTextContent(
      messages.routes.patches,
    );
  });

  it("mounts the production Eval / Bench route under the application shell", async () => {
    stubBenchReport();
    renderApp("/eval");

    expect(await screen.findByRole("heading", { level: 1, name: "Eval / Bench" })).toBeVisible();
    expect(screen.getByRole("main")).toContainElement(
      screen.getByRole("heading", { level: 1, name: "Eval / Bench" }),
    );
    expect(screen.getByRole("navigation", { name: messages.shell.breadcrumbs })).toHaveTextContent(
      messages.routes.eval,
    );
  });

  it("mounts the production Approvals route under the application shell", async () => {
    stubEmptyApprovalWorkspace();
    renderApp("/approvals");

    expect(await screen.findByRole("heading", { level: 1, name: "Approvals" })).toBeVisible();
    expect(screen.getByRole("main")).toContainElement(
      screen.getByRole("heading", { level: 1, name: "Approvals" }),
    );
    expect(screen.getByRole("navigation", { name: messages.shell.breadcrumbs })).toHaveTextContent(
      messages.routes.approvals,
    );
  });

  it("returns to the requested protected route after login", async () => {
    stubEmptyApprovalWorkspace();
    const user = userEvent.setup();
    const api = createAuthApi({
      me: vi.fn().mockRejectedValueOnce(unauthorized()).mockResolvedValue(principal),
    });
    renderApp("/approvals", api);

    expect(await screen.findByRole("heading", { name: messages.auth.signIn })).toBeVisible();
    await user.type(screen.getByLabelText(messages.auth.loginName), "lincheng");
    await user.type(screen.getByLabelText(messages.auth.password), "not-rendered-after-submit");
    await user.click(screen.getByRole("button", { name: messages.auth.signInAction }));

    expect(await screen.findByRole("heading", { level: 1, name: "Approvals" })).toBeVisible();
    expect(api.login).toHaveBeenCalledWith({
      login_name: "lincheng",
      password: "not-rendered-after-submit",
      schema_version: "password-auth@1",
    });
    expect(screen.queryByDisplayValue("not-rendered-after-submit")).not.toBeInTheDocument();
  });

  it("logs out with one mutation intent and returns to login", async () => {
    stubEmptyReviewWorkspace();
    const user = userEvent.setup();
    const api = createAuthApi();
    renderApp("/reviews", api);

    await screen.findByRole("heading", { level: 1, name: messages.routes.reviews });
    await user.click(screen.getByRole("button", { name: messages.auth.signOut }));

    await waitFor(() => expect(api.logout).toHaveBeenCalledOnce());
    expect(await screen.findByRole("heading", { name: messages.auth.signIn })).toBeVisible();
    expect(api.logout).toHaveBeenCalledWith({ idempotencyKey: expect.any(String) });
  });

  it("does not claim logout succeeded when the server keeps the session", async () => {
    stubEmptyReviewWorkspace();
    const user = userEvent.setup();
    const api = createAuthApi({
      logout: vi.fn().mockRejectedValue(new Error("upstream details must stay hidden")),
    });
    renderApp("/reviews", api);

    await screen.findByRole("heading", { level: 1, name: messages.routes.reviews });
    await user.click(screen.getByRole("button", { name: messages.auth.signOut }));

    expect(await screen.findByText(messages.auth.signOutFailed)).toBeVisible();
    expect(await screen.findByRole("heading", { level: 1, name: messages.routes.reviews })).toBeVisible();
    expect(screen.queryByRole("heading", { name: messages.auth.signIn })).not.toBeInTheDocument();
    expect(api.me).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/upstream details/)).not.toBeInTheDocument();
  });

  it("returns to login after a failed logout only when session reconciliation says anonymous", async () => {
    stubEmptyReviewWorkspace();
    const user = userEvent.setup();
    const api = createAuthApi({
      logout: vi.fn().mockRejectedValue(new Error("transport failed")),
      me: vi.fn().mockResolvedValueOnce(principal).mockRejectedValueOnce(unauthorized()),
    });
    renderApp("/reviews", api);

    await screen.findByRole("heading", { level: 1, name: messages.routes.reviews });
    await user.click(screen.getByRole("button", { name: messages.auth.signOut }));

    expect(await screen.findByRole("heading", { level: 1, name: messages.auth.signIn })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent(messages.auth.signOutFailed);
  });

  it("expires the visible identity when any transport reports a session boundary", async () => {
    stubEmptyReviewWorkspace();
    let notifySessionBoundary: (() => void) | undefined;
    const subscribe: SessionBoundarySubscriber = (listener) => {
      notifySessionBoundary = listener;
      return () => {
        notifySessionBoundary = undefined;
      };
    };
    renderApp("/reviews", createAuthApi(), subscribe);
    await screen.findByRole("heading", { level: 1, name: messages.routes.reviews });

    act(() => notifySessionBoundary?.());

    expect(await screen.findByRole("heading", { name: messages.auth.signIn })).toBeVisible();
    expect(screen.queryByText("林澄")).not.toBeInTheDocument();
  });

  it("shows a safe visible message when login fails outside Problem transport", async () => {
    const user = userEvent.setup();
    const api = createAuthApi({
      login: vi.fn().mockRejectedValue(new Error("provider internals must not render")),
      me: vi.fn().mockRejectedValue(unauthorized()),
    });
    renderApp("/specs", api);
    await screen.findByRole("heading", { name: messages.auth.signIn });
    await user.type(screen.getByLabelText(messages.auth.loginName), "lincheng");
    await user.type(screen.getByLabelText(messages.auth.password), "secret");
    await user.click(screen.getByRole("button", { name: messages.auth.signInAction }));

    expect(await screen.findByRole("alert")).toHaveTextContent(messages.auth.signInFailed);
    expect(screen.queryByText(/provider internals/)).not.toBeInTheDocument();
  });

  it("opens and closes the responsive navigation without losing its label", async () => {
    const user = userEvent.setup();
    stubEmptySpecWorkspace();
    renderApp();
    await screen.findByRole("heading", { level: 1, name: "规格与约束快照" });
    const toggle = screen.getByRole("button", { name: messages.shell.openNavigation });

    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAttribute("data-tooltip", messages.shell.openNavigation);
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: messages.shell.closeNavigation })).toBeVisible();

    screen.getByRole("button", { name: messages.theme.dark }).focus();
    await user.keyboard("{Escape}");
    expect(screen.getByRole("button", { name: messages.shell.openNavigation })).toHaveFocus();
    expect(screen.getByRole("button", { name: messages.shell.openNavigation })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});
