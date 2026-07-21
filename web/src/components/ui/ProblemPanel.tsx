import { TriangleAlert } from "lucide-react";

import type { SafeProblem } from "../../api/problem";
import { messages } from "../../i18n/zh-CN";

export function ProblemPanel({ problem }: { problem: SafeProblem }) {
  return (
    <section className="gf-problem" data-code={problem.code} role="alert">
      <header className="gf-cluster">
        <TriangleAlert aria-hidden="true" size={20} />
        <div>
          <p className="u-small">{problem.status}</p>
          <h2>{problem.title}</h2>
        </div>
      </header>
      <p>{problem.detail}</p>
      <dl className="gf-problem__details">
        <div>
          <dt>{messages.problem.code}</dt>
          <dd className="u-mono">{problem.code}</dd>
        </div>
        <div>
          <dt>{messages.problem.request}</dt>
          <dd className="u-mono">{problem.request_id}</dd>
        </div>
        {problem.run_id && (
          <div>
            <dt>{messages.problem.run}</dt>
            <dd>
              <a className="u-mono" href={`/runs/${encodeURIComponent(problem.run_id)}`}>
                {problem.run_id}
              </a>
            </dd>
          </div>
        )}
        {problem.trace_id && (
          <div>
            <dt>{messages.problem.trace}</dt>
            <dd>
              <a className="u-mono" href={`/observability/traces/${encodeURIComponent(problem.trace_id)}`}>
                {problem.trace_id}
              </a>
            </dd>
          </div>
        )}
        {problem.conflict_set_id && (
          <div>
            <dt>{messages.problem.conflict}</dt>
            <dd className="u-mono">{problem.conflict_set_id}</dd>
          </div>
        )}
        {problem.retry_after_s !== null && (
          <div>
            <dt>{messages.problem.retryAfter}</dt>
            <dd>{problem.retry_after_s}s</dd>
          </div>
        )}
      </dl>
    </section>
  );
}
