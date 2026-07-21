import { useEffect, useId, useRef, useState } from "react";

import {
  provideInputAvailability,
  type CreateCancelIntentOptions,
  type RunCommandIntent,
  type RunCommandReceipt,
} from "../../api/commands";
import type { components } from "../../api/generated/openapi";
import { ApiProblemError, type SafeProblem } from "../../api/problem";

type RunStatus = components["schemas"]["RunViewV1"]["status"];

interface CommandClient {
  createCancelIntent(options: CreateCancelIntentOptions): RunCommandIntent;
  submit(intent: RunCommandIntent): Promise<RunCommandReceipt>;
}

export interface RunCommandControlsProps {
  client: CommandClient;
  runId: string;
  runRevision: number;
  runStatus: RunStatus;
  onProblem?: (problem: SafeProblem) => Promise<void>;
  onPersisted?: (receipt: RunCommandReceipt) => void;
}

const terminalStatuses = new Set<RunStatus>(["succeeded", "failed", "cancelled", "timed_out"]);

export function RunCommandControls({
  client,
  runId,
  runRevision,
  runStatus,
  onProblem,
  onPersisted,
}: RunCommandControlsProps) {
  const [intent, setIntent] = useState<RunCommandIntent>();
  const [failureMessage, setFailureMessage] = useState<string>();
  const [problem, setProblem] = useState<SafeProblem>();
  const [rejectedRevision, setRejectedRevision] = useState<number>();
  const [state, setState] = useState<"idle" | "submitting" | "persisted" | "failed" | "rejected">("idle");
  const currentRunId = useRef(runId);
  const currentRunRevision = useRef(runRevision);
  const titleId = useId();
  currentRunId.current = runId;
  currentRunRevision.current = runRevision;

  useEffect(() => {
    setIntent(undefined);
    setFailureMessage(undefined);
    setProblem(undefined);
    setRejectedRevision(undefined);
    setState("idle");
  }, [runId]);

  useEffect(() => {
    if (rejectedRevision !== undefined && runRevision !== rejectedRevision) {
      setRejectedRevision(undefined);
      setState("idle");
    }
  }, [rejectedRevision, runRevision]);

  const submitCancel = async () => {
    const selected =
      intent ??
      client.createCancelIntent({
        expectedRunRevision: runRevision,
        reasonCode: "operator_cancelled",
        runId,
      });
    if (!intent) setIntent(selected);
    setProblem(undefined);
    setFailureMessage(undefined);
    setRejectedRevision(undefined);
    setState("submitting");
    let receipt: RunCommandReceipt;
    try {
      receipt = await client.submit(selected);
    } catch (error) {
      if (currentRunId.current !== selected.runId) return;
      if (error instanceof ApiProblemError) {
        const requiresNewRevision = error.problem.status === 409;
        setIntent(undefined);
        setProblem(error.problem);
        setRejectedRevision(requiresNewRevision ? selected.command.expected_run_revision : undefined);
        setState("rejected");
        if (onProblem) {
          try {
            await onProblem(error.problem);
          } catch (refreshError) {
            console.error("Run command authority refresh failed.", refreshError);
            return;
          }
          if (currentRunId.current !== selected.runId) return;
          if (requiresNewRevision && currentRunRevision.current === selected.command.expected_run_revision) {
            return;
          }
          setRejectedRevision(undefined);
          setState("idle");
        }
        return;
      }
      setFailureMessage(error instanceof Error ? error.message : "运行命令状态尚未确认。");
      setState("failed");
      return;
    }
    if (currentRunId.current !== selected.runId) return;
    setState("persisted");
    try {
      onPersisted?.(receipt);
    } catch (error) {
      console.error("Run command persisted callback failed.", error);
    }
  };

  return (
    <section aria-labelledby={titleId}>
      <h3 id={titleId}>运行控制</h3>
      <button
        disabled={
          state === "submitting" ||
          state === "persisted" ||
          state === "rejected" ||
          terminalStatuses.has(runStatus)
        }
        onClick={() => void submitCancel()}
        type="button"
      >
        {state === "failed" ? "重试取消命令" : state === "rejected" ? "等待刷新运行状态" : "取消运行"}
      </button>
      <button disabled type="button">
        提供输入
      </button>
      <p>{provideInputAvailability.reason}</p>
      {state === "submitting" && <p role="status">正在等待持久确认</p>}
      {state === "persisted" && <p role="status">取消命令已持久化</p>}
      {state === "failed" && <p role="alert">{failureMessage ?? "命令状态尚未确认，可以重试同一命令。"}</p>}
      {problem && <p role="alert">命令被拒绝：{problem.detail}</p>}
    </section>
  );
}
