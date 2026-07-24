import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";

import { describe, expect, expectTypeOf, it } from "vitest";

import type { components, operations, paths } from "./generated/openapi";
import type { RunEvent } from "./generated/sse-run-event-v1";
import type { RunCommandV1 } from "./generated/ws-client-command-v1";
import type { RunCommandServerFrame } from "./generated/ws-server-frame-v1";

type Problem = components["schemas"]["Problem"];
type ApprovalView = components["schemas"]["ApprovalViewV1"];
type ApprovalRequirementProgress = components["schemas"]["ApprovalRequirementProgressV1"];
type ApprovalDecisionEligibility = components["schemas"]["ApprovalDecisionEligibilityV1"];

const openApiDocument = JSON.parse(
  readFileSync(path.join(process.cwd(), "../docs/api/openapi-v1.json"), "utf8"),
) as {
  paths: Record<string, Record<string, { operationId?: string }>>;
  components: { schemas: Record<string, unknown> };
};

function commandPayloadVersion(command: RunCommandV1) {
  if (command.type === "cancel") {
    return command.payload.schema_version satisfies "run-cancel@1";
  }
  return command.payload.schema_version satisfies "playtest-provide-input@1";
}

function eventPayloadVersion(event: RunEvent) {
  if (event.event_type === "run.queued") {
    return event.data.data_schema_version satisfies "run-queued@1";
  }
  return event.data_schema_version;
}

function serverFrameOutcome(frame: RunCommandServerFrame) {
  if ("ack_schema_version" in frame) {
    return frame.status;
  }
  return frame.problem.code;
}

function handwrittenTypeScriptFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const entryPath = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      return entry.name === "generated" ? [] : handwrittenTypeScriptFiles(entryPath);
    }
    return /\.(?:ts|tsx)$/.test(entry.name) && !entry.name.includes(".test.") ? [entryPath] : [];
  });
}

function acceptsRunCommand(_command: RunCommandV1) {}
function acceptsRunEvent(_event: RunEvent) {}
function acceptsServerFrame(_frame: RunCommandServerFrame) {}

// @ts-expect-error cancel cannot carry a provide-input payload.
acceptsRunCommand({
  client_id: "browser:test",
  client_seq: 1,
  command_id: "command:test",
  expected_run_revision: 1,
  idempotency_key: "intent:test",
  type: "cancel",
  payload_schema_id: "playtest-provide-input@1",
  payload: {
    choice_id: "choice:test",
    expected_state_hash: "0".repeat(64),
    interaction_id: "interaction:test",
    schema_version: "playtest-provide-input@1",
  },
});

// @ts-expect-error a queued event cannot carry run-succeeded data.
acceptsRunEvent({
  run_id: "run:test",
  seq: 1,
  event_type: "run.queued",
  occurred_at: "2026-07-19T00:00:00Z",
  data_schema_version: "run-succeeded@1",
  data: {
    attempt_no: 1,
    data_schema_version: "run-succeeded@1",
    result_artifact_id: "artifact:test",
  },
  event_schema_version: "run-event@1",
});

acceptsServerFrame({
  // @ts-expect-error an ACK must use the ACK schema version.
  ack_schema_version: "run-command-problem@1",
  client_id: "browser:test",
  client_seq: 1,
  command_id: "command:test",
  command_revision: 1,
  persisted_status: "pending",
  run_revision: 1,
  status: "accepted",
});

describe("generated API contracts", () => {
  it("contains the complete additive M4d operation surface", () => {
    const operationIds = Object.values(openApiDocument.paths).flatMap((path) =>
      Object.values(path).flatMap((operation) => operation.operationId ?? []),
    );

    expect(operationIds).toHaveLength(78);
    expect(new Set(operationIds)).toHaveLength(78);
    expect(operationIds).toContain("artifact_catalog_api_v1_artifacts_get");
    expect(operationIds).toContain("resolve_execution_option_api_v1_execution_options_resolve_post");
    expect(operationIds).toContain(
      "subject_approval_binding_api_v1_workflow_subjects__artifact_id__approval_binding_get",
    );
    expect(operationIds).toContain(
      "constraint_validation_compiler_binding_api_v1_execution_profiles__profile_id__versions__version__constraint_validation_binding_get",
    );
    expect(operationIds).toContain(
      "task_suite_derivation_binding_api_v1_execution_profiles__profile_id__versions__version__task_suite_derivation_binding_get",
    );
    expect(operationIds).toContain(
      "review_producer_binding_api_v1_reviews__artifact_id__producer_binding_get",
    );
    expect(operationIds).toContain("run_finding_links_api_v1_runs__run_id__finding_links_get");
    expectTypeOf<paths>().toBeObject();
    expectTypeOf<operations>().toBeObject();
  });

  it("preserves representative versioned and discriminated wire shapes", () => {
    expectTypeOf<Problem>().toHaveProperty("code");
    expectTypeOf<ApprovalView>().toHaveProperty("current_actor_allowed_requirement_ids");
    expectTypeOf<ApprovalRequirementProgress>().toHaveProperty("decision_eligibility");
    expectTypeOf<ApprovalDecisionEligibility>().toHaveProperty("reason_codes");
    expectTypeOf<RunEvent>().toHaveProperty("data_schema_version");
    expectTypeOf<RunCommandV1>().toHaveProperty("type");
    expectTypeOf<RunCommandServerFrame>().toHaveProperty("command_id");
    expectTypeOf(commandPayloadVersion).returns.toMatchTypeOf<"run-cancel@1" | "playtest-provide-input@1">();
    expectTypeOf(eventPayloadVersion).returns.toBeString();
    expectTypeOf(serverFrameOutcome).returns.toBeString();
  });

  it("does not hand-declare generated wire DTO structures", () => {
    const streamingSchemaFiles = [
      "sse-run-event-v1.json",
      "ws-client-command-v1.json",
      "ws-server-frame-v1.json",
    ];
    const wireDtoNames = new Set(Object.keys(openApiDocument.components.schemas));
    for (const schemaFile of streamingSchemaFiles) {
      const schema = JSON.parse(
        readFileSync(path.join(process.cwd(), "../docs/api/schemas", schemaFile), "utf8"),
      ) as { title?: string; $defs?: Record<string, unknown> };
      if (schema.title) wireDtoNames.add(schema.title);
      for (const name of Object.keys(schema.$defs ?? {})) wireDtoNames.add(name);
    }

    const duplicateNames = handwrittenTypeScriptFiles(path.join(process.cwd(), "src")).flatMap((filename) => {
      const source = readFileSync(filename, "utf8");
      return [...source.matchAll(/\b(interface|type)\s+([A-Za-z_$][\w$]*)\b/g)]
        .filter((match) => {
          const declarationKind = match[1];
          const name = match[2];
          if (name === undefined || !wireDtoNames.has(name)) return false;
          if (declarationKind === "interface") return true;

          const remainder = source.slice((match.index ?? 0) + match[0].length);
          const generatedAlias = remainder.match(/^\s*=\s*components\["schemas"\]\["([^"]+)"\]\s*;/);
          return generatedAlias?.[1] !== name;
        })
        .map((match) => match[2]);
    });

    expect(duplicateNames).toEqual([]);
  });
});
