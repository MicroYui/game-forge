import type { ReactNode } from "react";

import { CopyableText } from "../tables";
import "./identity.css";
import { PRODUCT_TIME_ZONE } from "../../features/time";

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

const productDateTimeFormat = new Intl.DateTimeFormat("zh-CN", {
  timeZone: PRODUCT_TIME_ZONE,
  year: "numeric",
  month: "numeric",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

export function compactDateTime(value: string | null | undefined): string {
  if (!value) return "时间未知";
  const instant = new Date(value);
  if (!Number.isFinite(instant.valueOf())) return "时间未知";
  const parts = new Map(productDateTimeFormat.formatToParts(instant).map((part) => [part.type, part.value]));
  const year = parts.get("year");
  const month = parts.get("month");
  const day = parts.get("day");
  const hour = parts.get("hour");
  const minute = parts.get("minute");
  if (!year || !month || !day || !hour || !minute) return "时间未知";
  return `${year}年${Number(month)}月${Number(day)}日 ${hour}:${minute}`;
}
