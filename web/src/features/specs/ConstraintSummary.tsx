import { AlertTriangle, Braces, CircleCheck, Scale } from "lucide-react";

import type { components } from "../../api/generated/openapi";
import { CopyableText } from "../../components/tables";
import "./specs.css";

type Constraint = components["schemas"]["Constraint"];
type Selector = components["schemas"]["Selector"];

const kindLabels: Record<Constraint["kind"], string> = {
  narrative: "叙事规则",
  numeric: "数值规则",
  structural: "结构规则",
};

const oracleLabels: Record<Constraint["oracle"], string> = {
  deterministic: "确定性检查",
  "llm-assisted": "模型辅助，需人工兜底",
  mixed: "混合检查",
};

const severityLabels: Record<Constraint["severity"], string> = {
  critical: "严重",
  major: "重要",
  minor: "一般",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function selector(value: unknown): Selector | null | undefined {
  if (value == null) return null;
  if (
    !isRecord(value) ||
    typeof value.node_type !== "string" ||
    !value.node_type ||
    typeof value.var !== "string" ||
    !value.var ||
    (value.where !== undefined && !isRecord(value.where))
  ) {
    return undefined;
  }
  return {
    node_type: value.node_type,
    var: value.var,
    ...(value.where === undefined ? {} : { where: value.where }),
  };
}

export function parseConstraint(value: unknown): Constraint | null {
  if (!isRecord(value)) return null;
  const scope = selector(value.scope);
  const forall = selector(value.forall);
  const predicates = value.predicates;
  if (
    typeof value.id !== "string" ||
    !value.id ||
    typeof value.assert !== "string" ||
    !value.assert ||
    typeof value.dsl_grammar_version !== "string" ||
    !value.dsl_grammar_version ||
    !(value.kind === "structural" || value.kind === "numeric" || value.kind === "narrative") ||
    !(value.oracle === "deterministic" || value.oracle === "llm-assisted" || value.oracle === "mixed") ||
    !(value.severity === "critical" || value.severity === "major" || value.severity === "minor") ||
    (value.note !== undefined && value.note !== null && typeof value.note !== "string") ||
    scope === undefined ||
    forall === undefined ||
    (predicates !== undefined &&
      (!Array.isArray(predicates) ||
        predicates.some(
          (predicate) =>
            !isRecord(predicate) ||
            typeof predicate.expr !== "string" ||
            !predicate.expr ||
            !(predicate.oracle === "deterministic" || predicate.oracle === "llm-assisted"),
        )))
  ) {
    return null;
  }
  return value as Constraint;
}

function readableExpression(value: string): string {
  return value.replace(/<=/g, "≤").replace(/>=/g, "≥").replace(/!=/g, "≠").replace(/==/g, "=");
}

function SelectorSummary({ label, value }: { label: string; value: Selector }) {
  const where = value.where ?? {};
  return (
    <div className="gf-constraint-summary__selector">
      <strong>{label}</strong>
      <span>
        {value.node_type} · 变量 {value.var}
      </span>
      {Object.keys(where).length > 0 && <code>{JSON.stringify(where)}</code>}
    </div>
  );
}

export function ConstraintSummary({ constraint }: { constraint: Constraint }) {
  return (
    <article className="gf-constraint-summary">
      <header>
        <div>
          <p>{kindLabels[constraint.kind]}</p>
          <h3>{constraint.note?.trim() || constraint.id}</h3>
        </div>
        <span className={`u-status u-status--${constraint.severity === "critical" ? "danger" : "info"}`}>
          {severityLabels[constraint.severity]}
        </span>
      </header>

      <div className="gf-constraint-summary__rule">
        <Scale aria-hidden="true" size={18} />
        <div>
          <span>规则表达式</span>
          <strong>{readableExpression(constraint.assert)}</strong>
        </div>
      </div>

      {constraint.scope ? (
        <SelectorSummary label="适用对象" value={constraint.scope} />
      ) : (
        <div className="gf-constraint-summary__warning" role="alert">
          <AlertTriangle aria-hidden="true" size={18} />
          <div>
            <strong>未声明适用对象</strong>
            <span>数值或结构检查可能找不到要验证的实体；修订时请补充 scope。</span>
          </div>
        </div>
      )}

      <div className="gf-constraint-summary__oracle">
        <CircleCheck aria-hidden="true" size={17} />
        {oracleLabels[constraint.oracle]}
      </div>

      {(constraint.forall || (constraint.predicates?.length ?? 0) > 0) && (
        <details>
          <summary>查看量词、前提与筛选条件</summary>
          {constraint.forall && <SelectorSummary label="对每个对象" value={constraint.forall} />}
          {constraint.predicates?.map((predicate, index) => (
            <p key={`${predicate.expr}:${index}`}>
              前提 {index + 1}：<code>{predicate.expr}</code> · {oracleLabels[predicate.oracle]}
            </p>
          ))}
        </details>
      )}

      <details className="gf-constraint-summary__technical">
        <summary>
          <Braces aria-hidden="true" size={15} /> 技术详情
        </summary>
        <dl>
          <div>
            <dt>约束键</dt>
            <dd>
              <CopyableText copyLabel="复制约束键" value={constraint.id} />
            </dd>
          </div>
          <div>
            <dt>DSL</dt>
            <dd>{constraint.dsl_grammar_version}</dd>
          </div>
        </dl>
        <pre tabIndex={0}>{JSON.stringify(constraint, null, 2)}</pre>
      </details>
    </article>
  );
}

export function ConstraintSummaryList({ values }: { values: readonly unknown[] }) {
  const parsed = values.map(parseConstraint);
  if (parsed.some((value) => value === null)) {
    return (
      <div className="gf-constraint-summary__unsafe" role="alert">
        约束载荷与当前 typed contract 不一致，页面已停止解释，避免展示错误语义。
      </div>
    );
  }
  return (
    <div className="gf-constraint-summary-list">
      {(parsed as Constraint[]).map((constraint) => (
        <ConstraintSummary constraint={constraint} key={constraint.id} />
      ))}
    </div>
  );
}
