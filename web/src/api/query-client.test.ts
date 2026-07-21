import { describe, expect, it } from "vitest";

import { createQueryClient } from "./query-client";

describe("query retry policy", () => {
  it("never retries mutations automatically", () => {
    const queryClient = createQueryClient();

    expect(queryClient.getDefaultOptions().mutations?.retry).toBe(false);
  });
});
