import { beforeEach, describe, expect, it, vi } from "vitest";
import type { FetchOptions } from "openapi-fetch";

import {
  createMutationIntent,
  headersForCsrfProtectedRequest,
  headersForIdempotentMutation,
  headersForVersionedMutation,
  readCsrfToken,
  storeCsrfToken,
} from "./csrf";
import { createGameForgeApi, responseEtag } from "./client";
import type { paths } from "./generated/openapi";
import { ApiProblemError } from "./problem";
import { createQueryClient } from "./query-client";

type ResolverRequest = NonNullable<FetchOptions<paths["/api/v1/execution-options:resolve"]["post"]>["body"]>;
type CancelRequest = NonNullable<FetchOptions<paths["/api/v1/runs/{run_id}:cancel"]["post"]>["body"]>;
type GenerationRequest = NonNullable<FetchOptions<paths["/api/v1/generation:propose"]["post"]>["body"]>;
type ApprovalRequest = NonNullable<
  FetchOptions<paths["/api/v1/approvals/{approval_id}:approve"]["post"]>["body"]
>;

function securityHeaders(request: Request): Record<string, string> {
  return Object.fromEntries(
    ["x-csrf-token", "idempotency-key", "if-match"].flatMap((name) => {
      const value = request.headers.get(name);
      return value === null ? [] : [[name, value]];
    }),
  );
}

describe("typed HTTP and auth client", () => {
  beforeEach(() => sessionStorage.clear());

  it("uses credentialed openapi-fetch and stores only the login response CSRF header", async () => {
    const requests: Request[] = [];
    const fetch = vi.fn(async (request: Request) => {
      requests.push(request);
      return new Response(null, { status: 204, headers: { "X-CSRF-Token": "csrf-from-header" } });
    });
    const api = createGameForgeApi({ baseUrl: "https://console.test", fetch });

    await api.login({ schema_version: "password-auth@1", login_name: "alice", password: "secret" });

    expect(requests).toHaveLength(1);
    expect(requests[0]?.credentials).toBe("include");
    expect(requests[0]?.url).toBe("https://console.test/api/v1/auth/login");
    expect(requests[0]?.headers.get("X-GameForge-Reauthentication")).toBeNull();
    expect(readCsrfToken()).toBe("csrf-from-header");
    expect(sessionStorage.length).toBe(1);
  });

  it("marks only explicit password reauthentication on the login wire", async () => {
    const requests: Request[] = [];
    const fetch = vi.fn(async (request: Request) => {
      requests.push(request);
      return new Response(null, { status: 204, headers: { "X-CSRF-Token": "replacement-csrf" } });
    });
    const api = createGameForgeApi({ baseUrl: "https://console.test", fetch });

    await api.login(
      { schema_version: "password-auth@1", login_name: "alice", password: "secret" },
      { forceReauthentication: true },
    );

    expect(requests).toHaveLength(1);
    expect(requests[0]?.headers.get("X-GameForge-Reauthentication")).toBe("password");
    expect(readCsrfToken()).toBe("replacement-csrf");
  });

  it("allows cookie-backed reads without local CSRF material", async () => {
    const fetch = vi.fn(async () =>
      Response.json({
        id: "principal:1",
        kind: "human",
        display_name: "Alice",
        status: "active",
        roles: [],
        revision: 1,
        credential_epoch: 1,
        authz_revision: 1,
      }),
    );
    const api = createGameForgeApi({ baseUrl: "https://console.test", fetch });

    const principal = await api.me();

    expect(principal.id).toBe("principal:1");
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("sends only the security headers required by representative typed operations", async () => {
    const requests: Request[] = [];
    const fetch = vi.fn(async (request: Request) => {
      requests.push(request);
      return Response.json({});
    });
    const api = createGameForgeApi({ baseUrl: "https://console.test", fetch });
    storeCsrfToken("csrf-secret");
    const idempotentIntent = createMutationIntent();
    const versionedIntent = createMutationIntent();

    await api.client.POST("/api/v1/execution-options:resolve", {
      params: { header: headersForCsrfProtectedRequest() },
      body: {} as ResolverRequest,
    });
    await api.client.POST("/api/v1/runs/{run_id}:cancel", {
      params: {
        path: { run_id: "run:1" },
        header: headersForCsrfProtectedRequest(),
      },
      body: {} as CancelRequest,
    });
    await api.client.POST("/api/v1/generation:propose", {
      params: { header: headersForIdempotentMutation(idempotentIntent) },
      body: {} as GenerationRequest,
    });
    await api.client.POST("/api/v1/approvals/{approval_id}:approve", {
      params: {
        path: { approval_id: "approval:1" },
        header: headersForVersionedMutation(versionedIntent, '"approval:7"'),
      },
      body: {} as ApprovalRequest,
    });

    expect(requests.map(securityHeaders)).toEqual([
      { "x-csrf-token": "csrf-secret" },
      { "x-csrf-token": "csrf-secret" },
      {
        "x-csrf-token": "csrf-secret",
        "idempotency-key": idempotentIntent.idempotencyKey,
      },
      {
        "x-csrf-token": "csrf-secret",
        "idempotency-key": versionedIntent.idempotencyKey,
        "if-match": '"approval:7"',
      },
    ]);
  });

  it("preserves the server ETag as an opaque If-Match value", () => {
    const response = Response.json({}, { headers: { ETag: '"sha256:opaque"' } });

    expect(responseEtag(response)).toBe('"sha256:opaque"');
  });

  it("clears local auth material on logout and on any 401", async () => {
    storeCsrfToken("csrf-secret");
    const responses = [
      new Response(null, { status: 204 }),
      Response.json(
        {
          type: "about:blank",
          title: "未认证",
          status: 401,
          detail: "请重新登录",
          instance: "/api/v1/auth/me",
          code: "auth_required",
          request_id: "request:4",
          run_id: null,
          trace_id: null,
          earliest_cursor: null,
          conflict_set_id: null,
          retry_after_s: null,
          internal: "must-not-render",
        },
        { status: 401, headers: { "content-type": "application/problem+json" } },
      ),
    ];
    const fetch = vi.fn(async () => responses.shift() ?? new Response(null, { status: 500 }));
    const api = createGameForgeApi({ baseUrl: "https://console.test", fetch });

    await api.logout(createMutationIntent());
    expect(readCsrfToken()).toBeNull();

    storeCsrfToken("csrf-secret-again");
    await expect(api.me()).rejects.toBeInstanceOf(ApiProblemError);
    expect(readCsrfToken()).toBeNull();
  });

  it("clears authorized query data at every identity boundary", async () => {
    const queryClient = createQueryClient();
    const responses = [
      new Response(null, {
        status: 204,
        headers: { "X-CSRF-Token": "csrf-user-b" },
      }),
      new Response(null, { status: 204 }),
      Response.json(
        {
          type: "about:blank",
          title: "未认证",
          status: 401,
          detail: "请重新登录",
          instance: "/api/v1/auth/me",
          code: "auth_required",
          request_id: "request:session-boundary",
        },
        { status: 401, headers: { "content-type": "application/problem+json" } },
      ),
    ];
    const api = createGameForgeApi({
      baseUrl: "https://console.test",
      fetch: vi.fn(async () => responses.shift() ?? new Response(null, { status: 500 })),
      onSessionBoundary: () => queryClient.clear(),
    });

    queryClient.setQueryData(["authorized", "user-a"], { secret: "A" });
    await api.login({
      schema_version: "password-auth@1",
      login_name: "user-b",
      password: "secret",
    });
    expect(queryClient.getQueryData(["authorized", "user-a"])).toBeUndefined();

    queryClient.setQueryData(["authorized", "user-b"], { secret: "B" });
    await api.logout(createMutationIntent());
    expect(queryClient.getQueryData(["authorized", "user-b"])).toBeUndefined();

    storeCsrfToken("csrf-user-c");
    queryClient.setQueryData(["authorized", "user-c"], { secret: "C" });
    await expect(api.me()).rejects.toBeInstanceOf(ApiProblemError);
    expect(queryClient.getQueryData(["authorized", "user-c"])).toBeUndefined();
  });
});
