import { CircleHelp, FlaskConical, MessageSquareWarning, ShieldCheck } from "lucide-react";
import { useId, type ReactNode } from "react";

import "./evidence.css";

const sections = [
  {
    description: "由图、ASP、SMT 等可判定检查给出的结论。",
    icon: ShieldCheck,
    key: "deterministic",
    title: "确定性预言机",
  },
  {
    description: "用于 what-if 与分布观察；它是描述性证据，不冒充确定性证明。",
    icon: FlaskConical,
    key: "simulation",
    title: "仿真证据（描述性）",
  },
  {
    description: "模型只提供建议，必须由人确认或由确定性预言机复验。",
    icon: MessageSquareWarning,
    key: "suggestion",
    title: "LLM 建议（需人确认）",
  },
  {
    description: "证据不足、未知、超时、超预算或尚待验证；未证明绝不等于通过。",
    empty: "暂无未证明结果",
    icon: CircleHelp,
    key: "unproven",
    title: "未证明（不可视为通过）",
  },
] as const;

export function EvidenceSections({
  deterministic,
  simulation,
  suggestion,
  unproven,
}: {
  deterministic?: ReactNode;
  simulation?: ReactNode;
  suggestion?: ReactNode;
  unproven?: ReactNode;
}) {
  const instanceId = useId();
  const contents = { deterministic, simulation, suggestion, unproven };

  return (
    <div className="gf-evidence-sections">
      {sections.map((section) => {
        const Icon = section.icon;
        const headingId = `${instanceId}-evidence-section-${section.key}`;
        return (
          <section
            aria-labelledby={headingId}
            className="gf-evidence-section"
            data-evidence-kind={section.key}
            key={section.key}
          >
            <header className="gf-evidence-section__header">
              <Icon aria-hidden="true" size={20} />
              <div>
                <h2 id={headingId}>{section.title}</h2>
                <p>{section.description}</p>
              </div>
            </header>
            <div className="gf-evidence-section__content">
              {contents[section.key] ?? (
                <p className="gf-evidence-section__empty">
                  {"empty" in section ? section.empty : "暂无此类证据"}
                </p>
              )}
            </div>
          </section>
        );
      })}
    </div>
  );
}
