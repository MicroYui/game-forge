/** Generated from the committed GameForge API contracts. Do not edit by hand. */

/**
 * The `data:` payload object of one Server-Sent Event on GET /runs/{id}/events. The `data` field is a discriminated (`data_schema_version`) RunEventData union; `event_type` is the 14-value RunEventType. The SSE framing itself is `id:{seq}\nevent:{type}\ndata:{canonical_json}\n\n`.
 */
export type RunEvent = {
  attempt_no?: AttemptNo;
  data: Data;
  data_schema_version: DataSchemaVersion11;
  event_schema_version: EventSchemaVersion;
  event_type: EventType;
  occurred_at: OccurredAt;
  run_id: RunId;
  seq: Seq;
  trace_id?: TraceId;
} & (
  | {
      attempt_no?: null;
      data: RunQueuedDataV1;
      data_schema_version: "run-queued@1";
      event_type: "run.queued";
      [k: string]: unknown;
    }
  | {
      attempt_no?: null;
      data: CancelRequestedDataV1;
      data_schema_version: "cancel-requested@1";
      event_type: "run.cancel_requested";
      [k: string]: unknown;
    }
  | {
      attempt_no?: null;
      data: CommandAcceptedDataV1;
      data_schema_version: "command-accepted@1";
      event_type: "run.command_accepted";
      [k: string]: unknown;
    }
  | {
      attempt_no: number;
      data: AttemptLeasedDataV1;
      data_schema_version: "attempt-leased@1";
      event_type: "attempt.leased";
      [k: string]: unknown;
    }
  | {
      attempt_no: number;
      data: AttemptStartedDataV1;
      data_schema_version: "attempt-started@1";
      event_type: "attempt.started";
      [k: string]: unknown;
    }
  | {
      attempt_no: number;
      data: AttemptProgressDataV1;
      data_schema_version: "attempt-progress@1";
      event_type: "attempt.progress";
      [k: string]: unknown;
    }
  | {
      attempt_no: number;
      data: LeaseExpiredDataV1;
      data_schema_version: "lease-expired@1";
      event_type: "attempt.lease_expired";
      [k: string]: unknown;
    }
  | {
      attempt_no: number;
      data: RetryScheduledDataV1;
      data_schema_version: "retry-scheduled@1";
      event_type: "attempt.retry_scheduled";
      [k: string]: unknown;
    }
  | {
      data: CommandOutcomeDataV1;
      data_schema_version: "command-outcome@1";
      event_type: "run.command_applied";
      [k: string]: unknown;
    }
  | {
      data: CommandOutcomeDataV1;
      data_schema_version: "command-outcome@1";
      event_type: "run.command_rejected";
      [k: string]: unknown;
    }
  | {
      attempt_no: number;
      data: RunSucceededDataV1;
      data_schema_version: "run-succeeded@1";
      event_type: "run.succeeded";
      [k: string]: unknown;
    }
  | {
      data: RunTerminatedDataV1;
      data_schema_version: "run-terminated@1";
      event_type: "run.failed";
      [k: string]: unknown;
    }
  | {
      data: RunTerminatedDataV1;
      data_schema_version: "run-terminated@1";
      event_type: "run.cancelled";
      [k: string]: unknown;
    }
  | {
      data: RunTerminatedDataV1;
      data_schema_version: "run-terminated@1";
      event_type: "run.timed_out";
      [k: string]: unknown;
    }
);
export type AttemptNo = number | null;
export type Data =
  | RunQueuedDataV1
  | CancelRequestedDataV1
  | CommandAcceptedDataV1
  | AttemptLeasedDataV1
  | AttemptStartedDataV1
  | AttemptProgressDataV1
  | LeaseExpiredDataV1
  | RetryScheduledDataV1
  | CommandOutcomeDataV1
  | RunSucceededDataV1
  | RunTerminatedDataV1;
export type DataSchemaVersion = "run-queued@1";
export type OverallDeadlineUtc = string;
export type QueueDeadlineUtc = string;
export type Kind = string;
export type Version = number;
export type CommandId = string;
export type DataSchemaVersion1 = "cancel-requested@1";
export type ReasonCode = string;
export type CommandId1 = string;
export type CommandRevision = number;
export type CommandType = "cancel" | "provide_input";
export type DataSchemaVersion2 = "command-accepted@1";
export type AttemptNo1 = number;
export type DataSchemaVersion3 = "attempt-leased@1";
export type LeaseExpiresAt = string;
export type AttemptDeadlineUtc = string;
export type AttemptNo2 = number;
export type DataSchemaVersion4 = "attempt-started@1";
export type StartedAt = string;
export type AttemptNo3 = number;
export type CompletedUnits = number;
export type DataSchemaVersion5 = "attempt-progress@1";
export type DetailArtifactId = string | null;
export type PhaseCode = string;
export type TotalUnits = number | null;
export type AttemptNo4 = number;
export type DataSchemaVersion6 = "lease-expired@1";
export type FailureArtifactId = string;
export type WillRetry = boolean;
export type AttemptNo5 = number;
export type CauseCode = string;
export type DataSchemaVersion7 = "retry-scheduled@1";
export type FailureArtifactId1 = string;
export type FailureClass =
  | "business_rule"
  | "validation"
  | "transient_dependency"
  | "permanent_dependency"
  | "quota"
  | "execution"
  | "cancelled"
  | "timeout"
  | "lease"
  | "subject_superseded"
  | "integrity";
export type CauseCode1 = string;
export type ClassifierDigest = string;
export type ClassifierVersion = number;
export type Decision = "retry" | "terminal";
export type DecisionSchemaVersion = "retry-decision@1";
export type EvaluatedAtUtc = string;
export type FailureClass1 =
  | "business_rule"
  | "validation"
  | "transient_dependency"
  | "permanent_dependency"
  | "quota"
  | "execution"
  | "cancelled"
  | "timeout"
  | "lease"
  | "subject_superseded"
  | "integrity";
export type IntrinsicRetryEligible = boolean;
export type ReasonCode1 =
  | "transient_eligible"
  | "retry_after"
  | "max_attempts_exhausted"
  | "queue_deadline_exhausted"
  | "attempt_deadline_exhausted"
  | "overall_deadline_exhausted"
  | "budget_exhausted"
  | "policy_forbidden"
  | "not_retry_eligible";
export type RetryNotBeforeUtc = string | null;
export type RetryPolicyDigest = string;
export type RetryPolicyId = string;
export type RetryPolicyVersion = number;
export type RetryNotBeforeUtc1 = string;
export type CommandId2 = string;
export type CommandRevision1 = number;
export type CommandType1 = "cancel" | "provide_input";
export type DataSchemaVersion8 = "command-outcome@1";
export type OutcomeCode = string;
export type AttemptNo6 = number;
export type DataSchemaVersion9 = "run-succeeded@1";
export type ResultArtifactId = string;
export type AttemptNo7 = number | null;
export type CauseCode2 = string;
export type DataSchemaVersion10 = "run-terminated@1";
export type FailureArtifactId2 = string;
export type DataSchemaVersion11 = string;
export type EventSchemaVersion = "run-event@1";
export type EventType =
  | "run.queued"
  | "run.cancel_requested"
  | "run.command_accepted"
  | "attempt.leased"
  | "attempt.started"
  | "attempt.progress"
  | "attempt.lease_expired"
  | "attempt.retry_scheduled"
  | "run.command_applied"
  | "run.command_rejected"
  | "run.succeeded"
  | "run.failed"
  | "run.cancelled"
  | "run.timed_out";
export type OccurredAt = string;
export type RunId = string;
export type Seq = number;
export type TraceId = string | null;

export interface RunQueuedDataV1 {
  data_schema_version: DataSchemaVersion;
  overall_deadline_utc: OverallDeadlineUtc;
  queue_deadline_utc: QueueDeadlineUtc;
  run_kind: RunKindRef;
}
export interface RunKindRef {
  kind: Kind;
  version: Version;
}
export interface CancelRequestedDataV1 {
  command_id: CommandId;
  data_schema_version: DataSchemaVersion1;
  reason_code: ReasonCode;
}
export interface CommandAcceptedDataV1 {
  command_id: CommandId1;
  command_revision: CommandRevision;
  command_type: CommandType;
  data_schema_version: DataSchemaVersion2;
}
export interface AttemptLeasedDataV1 {
  attempt_no: AttemptNo1;
  data_schema_version: DataSchemaVersion3;
  lease_expires_at: LeaseExpiresAt;
}
export interface AttemptStartedDataV1 {
  attempt_deadline_utc: AttemptDeadlineUtc;
  attempt_no: AttemptNo2;
  data_schema_version: DataSchemaVersion4;
  started_at: StartedAt;
}
export interface AttemptProgressDataV1 {
  attempt_no: AttemptNo3;
  completed_units: CompletedUnits;
  data_schema_version: DataSchemaVersion5;
  detail_artifact_id?: DetailArtifactId;
  phase_code: PhaseCode;
  total_units?: TotalUnits;
}
export interface LeaseExpiredDataV1 {
  attempt_no: AttemptNo4;
  data_schema_version: DataSchemaVersion6;
  failure_artifact_id: FailureArtifactId;
  will_retry: WillRetry;
}
export interface RetryScheduledDataV1 {
  attempt_no: AttemptNo5;
  cause_code: CauseCode;
  data_schema_version: DataSchemaVersion7;
  failure_artifact_id: FailureArtifactId1;
  failure_class: FailureClass;
  retry_decision: RetryDecisionV1;
  retry_not_before_utc: RetryNotBeforeUtc1;
}
export interface RetryDecisionV1 {
  cause_code: CauseCode1;
  classifier: FailureClassifierRefV1;
  decision: Decision;
  decision_schema_version?: DecisionSchemaVersion;
  evaluated_at_utc: EvaluatedAtUtc;
  failure_class: FailureClass1;
  intrinsic_retry_eligible: IntrinsicRetryEligible;
  reason_code: ReasonCode1;
  retry_not_before_utc?: RetryNotBeforeUtc;
  retry_policy: RetryPolicyRefV1;
}
export interface FailureClassifierRefV1 {
  classifier_digest: ClassifierDigest;
  classifier_version: ClassifierVersion;
}
export interface RetryPolicyRefV1 {
  retry_policy_digest: RetryPolicyDigest;
  retry_policy_id: RetryPolicyId;
  retry_policy_version: RetryPolicyVersion;
}
export interface CommandOutcomeDataV1 {
  command_id: CommandId2;
  command_revision: CommandRevision1;
  command_type: CommandType1;
  data_schema_version: DataSchemaVersion8;
  outcome_code: OutcomeCode;
}
export interface RunSucceededDataV1 {
  attempt_no: AttemptNo6;
  data_schema_version: DataSchemaVersion9;
  result_artifact_id: ResultArtifactId;
}
export interface RunTerminatedDataV1 {
  attempt_no?: AttemptNo7;
  cause_code: CauseCode2;
  data_schema_version: DataSchemaVersion10;
  failure_artifact_id: FailureArtifactId2;
}
