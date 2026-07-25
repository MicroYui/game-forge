import type { ReactNode } from "react";

import { CopyableText } from "../tables";
import "./identity.css";

export interface TechnicalDetail {
  copyLabel?: string;
  label: string;
  value: string;
}

export function TechnicalDetails({
  items,
  summary = "技术信息",
}: {
  items: readonly TechnicalDetail[];
  summary?: string;
}) {
  return (
    <details className="gf-technical-details">
      <summary>{summary}</summary>
      <dl>
        {items.map((item) => (
          <div key={`${item.label}\u0000${item.value}`}>
            <dt>{item.label}</dt>
            <dd>
              <CopyableText copyLabel={item.copyLabel ?? `复制${item.label}`} scrollable value={item.value} />
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

export function ResourceIdentity({
  actionLabel,
  description,
  details,
  href,
  title,
}: {
  actionLabel?: string;
  description?: ReactNode;
  details: readonly TechnicalDetail[];
  href?: string;
  title: string;
}) {
  return (
    <div className="gf-resource-identity">
      <strong>{title}</strong>
      {description && <span className="gf-resource-identity__description">{description}</span>}
      {href && actionLabel && (
        <a className="gf-resource-identity__action" href={href}>
          {actionLabel}
        </a>
      )}
      <TechnicalDetails items={details} />
    </div>
  );
}

export function compactDateTime(value: string | null | undefined): string {
  if (!value) return "时间未知";
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/u.exec(value);
  if (!match) return value;
  const [, year, month, day, hour, minute] = match;
  return `${year}年${Number(month)}月${Number(day)}日 ${hour}:${minute}`;
}
