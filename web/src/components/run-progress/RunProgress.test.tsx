import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RunProgress } from "./RunProgress";

describe("RunProgress", () => {
  it("renders authoritative run, event, command, and artifact links", () => {
    render(
      <RunProgress
        commands={[
          {
            applied_at: "2026-07-19T12:01:00Z",
            client_id: "client:1",
            client_seq: 1,
            command_id: "command:1",
            created_at: "2026-07-19T12:00:00Z",
            payload_schema_id: "run-cancel@1",
            rejection_code: null,
            result_event_seq: 3,
            revision: 1,
            run_id: "run:1",
            status: "applied",
            type: "cancel",
          },
        ]}
        events={[
          {
            cursor: "9007199254740993",
            event: {
              attempt_no: 1,
              data: {
                attempt_no: 1,
                completed_units: 2,
                data_schema_version: "attempt-progress@1",
                phase_code: "checking",
                total_units: 4,
              },
              data_schema_version: "attempt-progress@1",
              event_schema_version: "run-event@1",
              event_type: "attempt.progress",
              occurred_at: "2026-07-19T12:00:00Z",
              run_id: "run:1",
              seq: 2,
              trace_id: "trace:1",
            },
          },
        ]}
        run={{
          attempt_no: 1,
          events_url: "/api/v1/runs/run:1/events",
          failure_artifact_id: null,
          result_artifact_id: "artifact:result",
          revision: 4,
          run_id: "run:1",
          status: "succeeded",
          status_url: "/api/v1/runs/run:1",
          terminal_cassette_artifact_id: "artifact:cassette",
          view_schema_version: "run-view@1",
        }}
        traceHref="/observability/traces/trace%3A1"
      />,
    );

    expect(screen.getByRole("heading", { name: "运行进度" })).toBeVisible();
    expect(screen.getByText("succeeded")).toBeVisible();
    expect(screen.getByRole("progressbar")).toHaveAttribute("value", "2");
    expect(screen.getByText("已完成 2 / 4")).toBeVisible();
    expect(screen.getByRole("link", { name: "结果工件" })).toHaveAttribute(
      "href",
      "/artifacts/artifact%3Aresult",
    );
    expect(screen.getByRole("link", { name: "追踪 trace:1" })).toHaveAttribute(
      "href",
      "/observability/traces/trace%3A1",
    );
    expect(screen.getByText("command:1 · applied")).toBeVisible();
  });

  it("renders progress without a guessed maximum when total units are unknown", () => {
    render(
      <RunProgress
        events={[
          {
            cursor: "1",
            event: {
              attempt_no: 1,
              data: {
                attempt_no: 1,
                completed_units: 3,
                data_schema_version: "attempt-progress@1",
                phase_code: "discovering",
                total_units: null,
              },
              data_schema_version: "attempt-progress@1",
              event_schema_version: "run-event@1",
              event_type: "attempt.progress",
              occurred_at: "2026-07-19T12:00:00Z",
              run_id: "run:1",
              seq: 1,
              trace_id: null,
            },
          },
        ]}
        run={{
          events_url: "/api/v1/runs/run:1/events",
          revision: 2,
          run_id: "run:1",
          status: "running",
          status_url: "/api/v1/runs/run:1",
          view_schema_version: "run-view@1",
        }}
      />,
    );

    expect(screen.getByRole("progressbar")).not.toHaveAttribute("value");
    expect(screen.getByText("已完成 3")).toBeVisible();
  });
});
