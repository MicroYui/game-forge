import type { components } from "../../api/generated/openapi";
import "./diff.css";

type JsonValueState = components["schemas"]["JsonValueState"];

function valueText(value: unknown): string {
  if (value === null) return "JSON null";
  return JSON.stringify(value, null, 2) ?? "JSON null";
}

export function JsonValueStateView({ state }: { state: JsonValueState }) {
  if (state.presence === "missing") {
    return (
      <span className="gf-json-state gf-json-state--missing" data-presence="missing">
        缺失（MISSING）
      </span>
    );
  }

  return (
    <pre className="gf-json-state gf-json-state--present" data-presence="present">
      {valueText(state.value)}
    </pre>
  );
}
