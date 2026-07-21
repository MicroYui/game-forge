/** Generated from the committed GameForge API contracts. Do not edit by hand. */

/**
 * The full command envelope shared by WS /runs/{id}/commands and POST /runs/{id}:cancel (where type must be cancel). `payload` is a discriminated (`schema_version`) RunCommandPayload union.
 */
export type RunCommandV1 = {
  client_id: ClientId;
  client_seq: ClientSeq;
  command_id: CommandId;
  command_schema_version?: CommandSchemaVersion;
  expected_run_revision: ExpectedRunRevision;
  idempotency_key: IdempotencyKey;
  payload: Payload;
  payload_schema_id: PayloadSchemaId;
  type: Type;
} & (
  | {
      payload: CancelRunPayloadV1;
      payload_schema_id: "run-cancel@1";
      type: "cancel";
      [k: string]: unknown;
    }
  | {
      payload: PlaytestProvideInputPayloadV1;
      payload_schema_id: "playtest-provide-input@1";
      type: "provide_input";
      [k: string]: unknown;
    }
);
export type ClientId = string;
export type ClientSeq = number;
export type CommandId = string;
export type CommandSchemaVersion = "run-command@1";
export type ExpectedRunRevision = number;
export type IdempotencyKey = string;
export type Payload = CancelRunPayloadV1 | PlaytestProvideInputPayloadV1;
export type Comment = string | null;
export type ReasonCode = string;
export type SchemaVersion = "run-cancel@1";
export type ChoiceId = string;
export type ExpectedStateHash = string;
export type InteractionId = string;
export type SchemaVersion1 = "playtest-provide-input@1";
export type PayloadSchemaId = "run-cancel@1" | "playtest-provide-input@1";
export type Type = "cancel" | "provide_input";

export interface CancelRunPayloadV1 {
  comment?: Comment;
  reason_code: ReasonCode;
  schema_version: SchemaVersion;
}
export interface PlaytestProvideInputPayloadV1 {
  choice_id: ChoiceId;
  expected_state_hash: ExpectedStateHash;
  interaction_id: InteractionId;
  schema_version: SchemaVersion1;
}
