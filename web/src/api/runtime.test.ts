import { beforeEach, describe, expect, it, vi } from "vitest";

import { queryClient } from "./query-client";
import { clearAuthorizedSessionState, subscribeSessionBoundary } from "./runtime";

describe("browser API runtime", () => {
  beforeEach(() => queryClient.clear());

  it("clears all authorized query data at an identity boundary", () => {
    queryClient.setQueryData(["run", "user-a"], { confidential: true });

    clearAuthorizedSessionState();

    expect(queryClient.getQueryData(["run", "user-a"])).toBeUndefined();
  });

  it("notifies active auth consumers and stops after unsubscribe", () => {
    const listener = vi.fn();
    const unsubscribe = subscribeSessionBoundary(listener);

    clearAuthorizedSessionState();
    unsubscribe();
    clearAuthorizedSessionState();

    expect(listener).toHaveBeenCalledOnce();
  });
});
