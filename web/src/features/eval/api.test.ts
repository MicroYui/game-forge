import { describe, expect, it, vi } from "vitest";

import type { GameForgeOpenApiClient } from "../../api/client";
import type { BenchReportDto } from "./api";
import { createEvalApi } from "./api";

const report = {
  schema_version: "bench-report@2",
  evidence: [],
  false_positives: [],
  seeded: [],
} as unknown as BenchReportDto;

function success(headers: HeadersInit = {}) {
  return {
    data: report,
    response: Response.json(report, { headers, status: 200 }),
  };
}

describe("Eval API", () => {
  it("reads the exact BenchReport path and returns authority from the same 200 response", async () => {
    const get = vi.fn(async () =>
      success({
        ETag: '"bench-report:7"',
        "X-Artifact-ID": "artifact:bench-report:7",
      }),
    );
    const api = createEvalApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await expect(api.getBenchReport()).resolves.toEqual({
      artifactId: "artifact:bench-report:7",
      etag: '"bench-report:7"',
      report,
    });
    expect(get).toHaveBeenCalledOnce();
    expect(get).toHaveBeenCalledWith("/api/v1/bench/report");
  });

  it("preserves a 503 problem through the shared ApiProblemError boundary", async () => {
    const problem = {
      code: "dependency_unavailable",
      conflict_set_id: null,
      detail: "BenchReport storage is unavailable",
      earliest_cursor: null,
      instance: "/api/v1/bench/report",
      request_id: "request:bench:503",
      retry_after_s: 3,
      run_id: null,
      status: 503,
      title: "Dependency unavailable",
      trace_id: "trace:bench:503",
      type: "about:blank",
    } as const;
    const get = vi.fn(async () => ({
      error: problem,
      response: Response.json(problem, {
        headers: { "Content-Type": "application/problem+json" },
        status: 503,
      }),
    }));
    const api = createEvalApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await expect(api.getBenchReport()).rejects.toMatchObject({
      name: "ApiProblemError",
      problem,
    });
  });

  it("fails closed when a successful response omits its ETag", async () => {
    const get = vi.fn(async () => success({ "X-Artifact-ID": "artifact:bench-report:without-etag" }));
    const api = createEvalApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await expect(api.getBenchReport()).rejects.toThrow(
      "The server response did not include the required ETag.",
    );
  });

  it("keeps a missing artifact header null instead of guessing from report content", async () => {
    const reportWithMisleadingContent = {
      ...report,
      artifact_id: "artifact:must-not-be-guessed",
    } as unknown as BenchReportDto;
    const get = vi.fn(async () => ({
      data: reportWithMisleadingContent,
      response: Response.json(reportWithMisleadingContent, {
        headers: { ETag: '"bench-report:8"' },
        status: 200,
      }),
    }));
    const api = createEvalApi({ GET: get } as unknown as GameForgeOpenApiClient);

    await expect(api.getBenchReport()).resolves.toEqual({
      artifactId: null,
      etag: '"bench-report:8"',
      report: reportWithMisleadingContent,
    });
  });
});
