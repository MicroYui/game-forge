/** Generated from the committed GameForge API contracts. Do not edit by hand. */

/**
 * One server frame on WS /runs/{id}/commands: exactly one of a RunCommandAckV1 (accepted/duplicate) or a RunCommandProblemV1 (RFC 9457 problem). REST cancel returns the ack as JSON and errors as HTTP Problem responses. Lease/fencing worker columns are structurally absent.
 */
export type RunCommandServerFrame = RunCommandAckV1 | RunCommandProblemV1;
export type AckSchemaVersion = "run-command-ack@1";
export type ClientId = string;
export type ClientSeq = number;
export type CommandId = string;
export type CommandRevision = number;
export type PersistedStatus = "pending" | "claimed" | "applied" | "rejected";
export type RunRevision = number;
export type Status = "accepted" | "duplicate";
export type ClientSeq1 = number | null;
export type CommandId1 = string | null;
export type Code = string;
export type ConflictSetId = string | null;
export type Detail = string;
export type EarliestCursor = string | null;
export type Errors =
  | {
      [k: string]: JsonValue;
    }[]
  | null;
export type JsonValue = unknown;
export type Instance = string;
export type RequestId = string;
export type RetryAfterS = number | null;
export type RunId = string | null;
export type Status1 = number;
export type Title = string;
export type TraceId = string | null;
export type Type = string;
export type ProblemSchemaVersion = "run-command-problem@1";

export interface RunCommandAckV1 {
  ack_schema_version: AckSchemaVersion;
  client_id: ClientId;
  client_seq: ClientSeq;
  command_id: CommandId;
  command_revision: CommandRevision;
  persisted_status: PersistedStatus;
  run_revision: RunRevision;
  status: Status;
}
export interface RunCommandProblemV1 {
  client_seq?: ClientSeq1;
  command_id?: CommandId1;
  problem: Problem;
  problem_schema_version: ProblemSchemaVersion;
}
export interface Problem {
  code: Code;
  conflict_set_id?: ConflictSetId;
  detail: Detail;
  earliest_cursor?: EarliestCursor;
  errors?: Errors;
  instance: Instance;
  request_id: RequestId;
  retry_after_s?: RetryAfterS;
  run_id?: RunId;
  status: Status1;
  title: Title;
  trace_id?: TraceId;
  type: Type;
}
