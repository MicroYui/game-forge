import { TriangleAlert } from "lucide-react";

import type { SafeProblem } from "../../api/problem";
import { messages } from "../../i18n/zh-CN";

function friendlyProblem(problem: SafeProblem): {
  detail: string;
  title: string;
} {
  if (problem.code === "revision_conflict" || problem.status === 409) {
    return {
      detail: "你打开页面后，相关内容已经发生变化。请重新读取最新版本，再确认是否继续。",
      title: "内容已被更新",
    };
  }
  if (problem.status === 401) {
    return { detail: "请重新登录后再试。", title: "登录状态已失效" };
  }
  if (problem.status === 403) {
    return { detail: "当前账号没有执行此操作的权限。", title: "没有操作权限" };
  }
  if (problem.status === 404) {
    return {
      detail: "这项内容可能已被移动、删除，或当前账号无权查看。",
      title: "没有找到相关内容",
    };
  }
  if (problem.status === 410) {
    return {
      detail: "当前列表版本已经过期，请重新开始查询。",
      title: "列表已更新",
    };
  }
  if (problem.status === 422) {
    return {
      detail: "部分输入未通过校验，请检查页面提示后重试。",
      title: "输入内容需要修改",
    };
  }
  if (problem.status === 429) {
    return { detail: "请稍候片刻再试。", title: "操作过于频繁" };
  }
  if (problem.status >= 500) {
    return {
      detail: "系统暂时无法完成操作，请稍后重试。",
      title: "系统暂时不可用",
    };
  }
  return {
    detail: "系统未能完成这次操作，请检查输入或稍后重试。",
    title: "操作未完成",
  };
}

export function ProblemPanel({ problem }: { problem: SafeProblem }) {
  const friendly = friendlyProblem(problem);
  return (
    <section className="gf-problem" data-code={problem.code} role="alert">
      <header className="gf-cluster">
        <TriangleAlert aria-hidden="true" size={20} />
        <div>
          <p className="u-small">操作未完成</p>
          <h2>{friendly.title}</h2>
        </div>
      </header>
      <p>{friendly.detail}</p>
      {problem.retry_after_s !== null && <p>请在 {problem.retry_after_s} 秒后重试。</p>}
      <details className="gf-problem__technical">
        <summary>查看错误技术信息</summary>
        <p>
          {problem.title}：{problem.detail}
        </p>
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
      </details>
    </section>
  );
}
