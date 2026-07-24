import createClient, { type Client } from "openapi-fetch";

import type { components, paths } from "./generated/openapi";
import {
  clearCsrfToken,
  headersForIdempotentMutation,
  type MutationIntent,
  type SessionStorage,
  storeCsrfToken,
} from "./csrf";
import { ApiProblemError, sanitizeProblem } from "./problem";

type PasswordAuthRequest = components["schemas"]["PasswordAuthRequestV1"];
type AuthenticatedPrincipal = components["schemas"]["Principal"];

export const PASSWORD_REAUTHENTICATION_HEADER = "X-GameForge-Reauthentication";
export const PASSWORD_REAUTHENTICATION_VALUE = "password";

export type LoginOptions = Readonly<{
  forceReauthentication?: boolean;
}>;

type ApiResponse<T> = {
  data?: T;
  error?: unknown;
  response: Response;
};

export type GameForgeOpenApiClient = Client<paths>;

export type GameForgeApi = {
  client: GameForgeOpenApiClient;
  login(request: PasswordAuthRequest, options?: LoginOptions): Promise<void>;
  logout(intent: MutationIntent): Promise<void>;
  me(): Promise<AuthenticatedPrincipal>;
};

type ClientOptions = {
  baseUrl?: string;
  fetch?: (request: Request) => Promise<Response>;
  onSessionBoundary?: () => void;
  storage?: SessionStorage;
};

export async function unwrapApiResponse<T>(result: ApiResponse<T>): Promise<T> {
  if (result.response.ok) return result.data as T;
  throw new ApiProblemError(sanitizeProblem(result.error));
}

export function responseEtag(response: Response): string | null {
  return response.headers.get("ETag");
}

export function createGameForgeApi(options: ClientOptions = {}): GameForgeApi {
  const storage = options.storage ?? globalThis.sessionStorage;
  const invalidateSession = (): void => {
    clearCsrfToken(storage);
    options.onSessionBoundary?.();
  };
  const client = createClient<paths>({
    baseUrl: options.baseUrl ?? "",
    credentials: "include",
    fetch: options.fetch,
  });

  client.use({
    onResponse({ response }) {
      if (response.status === 401) invalidateSession();
      return response;
    },
  });

  return {
    client,
    async login(request, loginOptions = {}) {
      const result = await client.POST("/api/v1/auth/login", {
        body: request,
        params: loginOptions.forceReauthentication
          ? {
              header: {
                [PASSWORD_REAUTHENTICATION_HEADER]: PASSWORD_REAUTHENTICATION_VALUE,
              },
            }
          : undefined,
      });
      await unwrapApiResponse<void>(result);
      invalidateSession();
      const csrfToken = result.response.headers.get("X-CSRF-Token");
      if (csrfToken !== null) storeCsrfToken(csrfToken, storage);
    },
    async logout(intent) {
      try {
        const result = await client.POST("/api/v1/auth/logout", {
          params: { header: headersForIdempotentMutation(intent, storage) },
        });
        await unwrapApiResponse<void>(result);
      } finally {
        invalidateSession();
      }
    },
    async me() {
      return unwrapApiResponse<AuthenticatedPrincipal>(await client.GET("/api/v1/auth/me"));
    },
  };
}
