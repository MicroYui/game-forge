import { describe, expect, it } from "vitest";

import { ApiProblemError, sanitizeProblem } from "./problem";

describe("Problem rendering boundary", () => {
  it("keeps only the public correlation and recovery fields", () => {
    const problem = sanitizeProblem({
      type: "about:blank",
      title: "游标已过期",
      status: 410,
      detail: "请重新开始查询",
      instance: "/api/v1/runs",
      code: "cursor_expired",
      request_id: "request:1",
      run_id: "run:1",
      trace_id: "trace:1",
      earliest_cursor: "opaque:earliest",
      conflict_set_id: "conflict:1",
      retry_after_s: null,
      errors: [{ password: "must-not-render" }],
      raw_response: "must-not-render",
      secret: "must-not-render",
    });

    expect(problem).toEqual({
      type: "about:blank",
      title: "游标已过期",
      status: 410,
      detail: "请重新开始查询",
      instance: "/api/v1/runs",
      code: "cursor_expired",
      request_id: "request:1",
      run_id: "run:1",
      trace_id: "trace:1",
      earliest_cursor: "opaque:earliest",
      conflict_set_id: "conflict:1",
      retry_after_s: null,
    });
    expect(JSON.stringify(problem)).not.toContain("must-not-render");
  });

  it("throws a typed sanitized API error", () => {
    const error = new ApiProblemError(
      sanitizeProblem({
        type: "about:blank",
        title: "未认证",
        status: 401,
        detail: "请重新登录",
        instance: "/api/v1/auth/me",
        code: "auth_required",
        request_id: "request:2",
        run_id: null,
        trace_id: null,
        earliest_cursor: null,
        conflict_set_id: null,
        retry_after_s: null,
      }),
    );

    expect(error.name).toBe("ApiProblemError");
    expect(error.problem.code).toBe("auth_required");
  });
});
