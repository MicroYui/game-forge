import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  CSRF_STORAGE_KEY,
  ReauthenticationRequiredError,
  clearCsrfToken,
  createMutationIntent,
  headersForCsrfProtectedRequest,
  headersForIdempotentMutation,
  headersForVersionedMutation,
  readCsrfToken,
  storeCsrfToken,
} from "./csrf";

describe("session CSRF and mutation intents", () => {
  beforeEach(() => {
    sessionStorage.clear();
    localStorage.clear();
  });

  it("keeps the CSRF token only in sessionStorage", () => {
    storeCsrfToken("csrf-secret");

    expect(sessionStorage.getItem(CSRF_STORAGE_KEY)).toBe("csrf-secret");
    expect(readCsrfToken()).toBe("csrf-secret");
    expect(localStorage.length).toBe(0);

    clearCsrfToken();
    expect(sessionStorage.getItem(CSRF_STORAGE_KEY)).toBeNull();
  });

  it("reuses one UUID for one intent and creates a new UUID for a new intent", () => {
    vi.spyOn(crypto, "randomUUID")
      .mockReturnValueOnce("11111111-1111-4111-8111-111111111111")
      .mockReturnValueOnce("22222222-2222-4222-8222-222222222222");

    const firstIntent = createMutationIntent();
    storeCsrfToken("csrf-secret");

    expect(headersForIdempotentMutation(firstIntent)).toEqual({
      "Idempotency-Key": "11111111-1111-4111-8111-111111111111",
      "X-CSRF-Token": "csrf-secret",
    });
    expect(headersForIdempotentMutation(firstIntent)).toEqual({
      "Idempotency-Key": "11111111-1111-4111-8111-111111111111",
      "X-CSRF-Token": "csrf-secret",
    });
    expect(headersForVersionedMutation(firstIntent, '"revision:7"')).toEqual({
      "Idempotency-Key": "11111111-1111-4111-8111-111111111111",
      "If-Match": '"revision:7"',
      "X-CSRF-Token": "csrf-secret",
    });
    expect(createMutationIntent().idempotencyKey).toBe("22222222-2222-4222-8222-222222222222");
  });

  it("requires re-authentication before a mutation when a new tab has no CSRF token", () => {
    const intent = createMutationIntent();

    expect(() => headersForIdempotentMutation(intent)).toThrow(ReauthenticationRequiredError);
    expect(() => headersForCsrfProtectedRequest()).toThrow(ReauthenticationRequiredError);
  });

  it("uses only CSRF for cancel and the read-only POST resolver", () => {
    storeCsrfToken("csrf-secret");

    expect(headersForCsrfProtectedRequest()).toEqual({ "X-CSRF-Token": "csrf-secret" });
  });
});
