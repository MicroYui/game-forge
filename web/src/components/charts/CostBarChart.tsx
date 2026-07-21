import { CircleCheck, CircleHelp, CircleMinus, TriangleAlert } from "lucide-react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, XAxis, YAxis } from "recharts";

import { ChartFrame } from "./ChartFrame";

export interface CostBarDatum {
  consumed: CostBarValue;
  label: string;
  limit: CostBarValue;
  reserved: CostBarValue;
  unit: string;
}

export interface CostBarValue {
  /** Exact Decimal string from the wire; null means the observation is unavailable. */
  exact: string | null;
  /** Finite non-negative approximation used only for chart geometry. */
  plot: number | null;
}

export interface CostBarChartProps {
  data: readonly CostBarDatum[];
  summary: string;
  title: string;
  valueFormatter?: (exactValue: string, unit: string) => string;
}

type BudgetStatus = "数据不可用" | "超出预算" | "预算为 0" | "预算内";

interface ScaledDecimal {
  coefficient: bigint;
  scale: number;
}

function parseNonNegativeDecimal(value: string): ScaledDecimal | null {
  const match = /^(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(value);
  if (!match) return null;
  const fraction = match[2] ?? "";
  const exponent = Number(match[3] ?? "0");
  if (!Number.isSafeInteger(exponent)) return null;
  return {
    coefficient: BigInt(`${match[1]}${fraction}`),
    scale: fraction.length - exponent,
  };
}

function alignCoefficient(value: ScaledDecimal, scale: number): bigint {
  return value.coefficient * 10n ** BigInt(scale - value.scale);
}

function compareConsumedWithLimit(item: CostBarDatum): -1 | 0 | 1 | null {
  if (item.consumed.exact === null || item.reserved.exact === null || item.limit.exact === null) {
    return null;
  }
  const consumed = parseNonNegativeDecimal(item.consumed.exact);
  const reserved = parseNonNegativeDecimal(item.reserved.exact);
  const limit = parseNonNegativeDecimal(item.limit.exact);
  if (!consumed || !reserved || !limit) return null;
  const scale = Math.max(consumed.scale, reserved.scale, limit.scale);
  const used = alignCoefficient(consumed, scale) + alignCoefficient(reserved, scale);
  const exactLimit = alignCoefficient(limit, scale);
  return used < exactLimit ? -1 : used > exactLimit ? 1 : 0;
}

function budgetStatus(item: CostBarDatum): BudgetStatus {
  const comparison = compareConsumedWithLimit(item);
  if (comparison === null) return "数据不可用";
  if (comparison > 0) return "超出预算";
  const limit = parseNonNegativeDecimal(item.limit.exact!);
  return limit?.coefficient === 0n ? "预算为 0" : "预算内";
}

const defaultFormatter = (exactValue: string, unit: string) => `${exactValue} ${unit}`;

function formatAmount(
  value: CostBarValue,
  unit: string,
  formatter: NonNullable<CostBarChartProps["valueFormatter"]>,
): string {
  return value.exact === null ? "不可用" : formatter(value.exact, unit);
}

function canPlot(value: CostBarValue): value is CostBarValue & { exact: string; plot: number } {
  return value.exact !== null && value.plot !== null && Number.isFinite(value.plot) && value.plot >= 0;
}

export function CostBarChart({ data, summary, title, valueFormatter = defaultFormatter }: CostBarChartProps) {
  const plotData = data.map((item) => {
    if (!canPlot(item.consumed) || !canPlot(item.reserved) || !canPlot(item.limit)) {
      return {
        ...item,
        consumedPercent: null,
        remainingPercent: null,
        reservedPercent: null,
      };
    }
    const scale =
      item.limit.plot === 0 ? Math.max(item.consumed.plot + item.reserved.plot, 1) : item.limit.plot;
    const consumedPercent = (item.consumed.plot / scale) * 100;
    const reservedPercent = (item.reserved.plot / scale) * 100;
    return {
      ...item,
      consumedPercent,
      remainingPercent: Math.max(100 - consumedPercent - reservedPercent, 0),
      reservedPercent,
    };
  });
  const maxPercent = Math.max(
    100,
    ...plotData.map((item) =>
      item.consumedPercent === null || item.reservedPercent === null
        ? 0
        : item.consumedPercent + item.reservedPercent,
    ),
  );

  return (
    <ChartFrame
      className="gf-chart--cost"
      columns={[
        { key: "label", label: "成本维度" },
        { key: "consumed", label: "已使用" },
        { key: "reserved", label: "已预留" },
        { key: "limit", label: "预算" },
        { key: "status", label: "状态" },
      ]}
      rows={data.map((item) => ({
        consumed: formatAmount(item.consumed, item.unit, valueFormatter),
        label: item.label,
        limit: formatAmount(item.limit, item.unit, valueFormatter),
        reserved: formatAmount(item.reserved, item.unit, valueFormatter),
        status: budgetStatus(item),
      }))}
      summary={summary}
      title={title}
    >
      <div className="gf-chart__cost-key" aria-label="成本段说明">
        <span>
          <i className="gf-chart__swatch gf-chart__swatch--consumed" aria-hidden="true" />
          已使用
        </span>
        <span>
          <i className="gf-chart__swatch gf-chart__swatch--reserved" aria-hidden="true" />
          已预留
        </span>
        <span>
          <i className="gf-chart__swatch gf-chart__swatch--remaining" aria-hidden="true" />
          剩余
        </span>
      </div>

      <div className="gf-chart__plot gf-chart__plot--cost" aria-hidden="true">
        {data.length === 0 ? (
          <p className="gf-chart__empty">暂无数据</p>
        ) : (
          <ResponsiveContainer height="100%" minWidth={0} width="100%">
            <BarChart
              accessibilityLayer={false}
              data={plotData}
              layout="vertical"
              margin={{ bottom: 12, left: 4, right: 18, top: 8 }}
            >
              <CartesianGrid horizontal={false} stroke="var(--line)" />
              <XAxis
                axisLine={false}
                domain={[0, maxPercent]}
                tick={{ fill: "var(--muted)", fontSize: 11 }}
                tickFormatter={(value: number) => `${Math.round(value)}%`}
                tickLine={false}
                type="number"
              />
              <YAxis
                axisLine={false}
                dataKey="label"
                tick={{ fill: "var(--ink-2)", fontSize: 12 }}
                tickLine={false}
                type="category"
                width={112}
              />
              <Bar
                dataKey="consumedPercent"
                fill="var(--viz-teal)"
                isAnimationActive={false}
                maxBarSize={24}
                stackId="budget"
              />
              <Bar
                dataKey="reservedPercent"
                fill="var(--viz-amber)"
                isAnimationActive={false}
                maxBarSize={24}
                stackId="budget"
              />
              <Bar
                dataKey="remainingPercent"
                fill="var(--line-strong)"
                isAnimationActive={false}
                maxBarSize={24}
                radius={[0, 4, 4, 0]}
                stackId="budget"
              />
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>

      <ul className="gf-chart__cost-statuses">
        {data.map((item) => {
          const status = budgetStatus(item);
          const exceeded = status === "超出预算";
          const zero = status === "预算为 0";
          const unavailable = status === "数据不可用";
          const Icon = exceeded ? TriangleAlert : zero ? CircleMinus : unavailable ? CircleHelp : CircleCheck;
          return (
            <li
              data-status={exceeded ? "error" : zero ? "zero" : unavailable ? "unavailable" : "ok"}
              key={item.label}
            >
              <span>{item.label}</span>
              <strong>
                <Icon aria-hidden="true" size={14} strokeWidth={1.8} />
                {status}
              </strong>
            </li>
          );
        })}
      </ul>
    </ChartFrame>
  );
}
