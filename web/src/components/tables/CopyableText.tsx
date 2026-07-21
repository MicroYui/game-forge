import { Check, Copy, TriangleAlert } from "lucide-react";
import { useId, useState } from "react";

import "./tables.css";

type CopyState = "idle" | "copied" | "failed";

export function CopyableText({
  copyLabel = "复制",
  scrollable = false,
  value,
}: {
  copyLabel?: string;
  scrollable?: boolean;
  value: string;
}) {
  const tooltipId = useId();
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const Icon = copyState === "copied" ? Check : copyState === "failed" ? TriangleAlert : Copy;

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  }

  return (
    <span className="gf-copyable">
      <code
        className={`gf-copyable__value${scrollable ? " gf-copyable__value--scrollable" : ""}`}
        tabIndex={scrollable ? 0 : undefined}
      >
        {value}
      </code>
      <button
        aria-describedby={tooltipId}
        aria-label={copyLabel}
        className="gf-copyable__button"
        onClick={copy}
        type="button"
      >
        <Icon aria-hidden="true" size={15} />
      </button>
      <span className="gf-copyable__tooltip" id={tooltipId} role="tooltip">
        {copyLabel}
      </span>
      <span aria-live="polite" className="u-sr-only">
        {copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : ""}
      </span>
    </span>
  );
}
