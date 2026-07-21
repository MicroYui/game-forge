import type { components } from "./generated/openapi";

type WireProblem = components["schemas"]["Problem"];

export type SafeProblem = Pick<
  WireProblem,
  | "type"
  | "title"
  | "status"
  | "detail"
  | "instance"
  | "code"
  | "request_id"
  | "run_id"
  | "trace_id"
  | "earliest_cursor"
  | "conflict_set_id"
  | "retry_after_s"
>;

export function sanitizeProblem(value: unknown): SafeProblem {
  const problem = value as WireProblem;
  return {
    type: problem.type,
    title: problem.title,
    status: problem.status,
    detail: problem.detail,
    instance: problem.instance,
    code: problem.code,
    request_id: problem.request_id,
    run_id: problem.run_id,
    trace_id: problem.trace_id,
    earliest_cursor: problem.earliest_cursor,
    conflict_set_id: problem.conflict_set_id,
    retry_after_s: problem.retry_after_s,
  };
}

export class ApiProblemError extends Error {
  constructor(readonly problem: SafeProblem) {
    super(problem.detail || problem.title);
    this.name = "ApiProblemError";
  }
}
