import { describe, expect, it } from "vitest";

import { ApiProblemError, sanitizeProblem } from "./problem";
import { CursorExpiredError, cursorFromPage, cursorQuery, requireExplicitCursorRestart } from "./pagination";

describe("opaque cursor pagination", () => {
  it("follows next_cursor byte-for-byte without decoding or repair", () => {
    const opaqueCursor = "eyJzaWduYXR1cmUiOiIrLz0ifQ==.%2Fopaque+tail";

    expect(cursorFromPage({ next_cursor: opaqueCursor })).toBe(opaqueCursor);
    expect(cursorQuery(opaqueCursor)).toEqual({ cursor: opaqueCursor });
    expect(cursorQuery(null)).toEqual({});
  });

  it("turns cursor_expired into an explicit restart state", () => {
    const cause = new ApiProblemError(
      sanitizeProblem({
        type: "about:blank",
        title: "游标已过期",
        status: 410,
        detail: "请重新开始查询",
        instance: "/api/v1/runs",
        code: "cursor_expired",
        request_id: "request:3",
        run_id: null,
        trace_id: null,
        earliest_cursor: "opaque:earliest",
        conflict_set_id: null,
        retry_after_s: null,
      }),
    );

    const expired = requireExplicitCursorRestart(cause);

    expect(expired).toBeInstanceOf(CursorExpiredError);
    expect(expired.staleCursor).toBeNull();
    expect(expired.problem.earliest_cursor).toBe("opaque:earliest");
    expect(expired.restart()).toEqual({});
  });
});
