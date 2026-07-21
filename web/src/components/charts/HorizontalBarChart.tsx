import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, XAxis, YAxis } from "recharts";

import { ChartFrame } from "./ChartFrame";

export interface HorizontalBarDatum {
  label: string;
  value: number;
}

export interface HorizontalBarChartProps {
  data: readonly HorizontalBarDatum[];
  summary: string;
  title: string;
  valueFormatter?: (value: number) => string;
  valueLabel: string;
}

const COLORS = [
  "var(--viz-teal)",
  "var(--viz-amber)",
  "var(--viz-indigo)",
  "var(--viz-sage)",
  "var(--viz-terracotta)",
  "var(--viz-taupe)",
] as const;

const defaultFormatter = (value: number) => new Intl.NumberFormat("zh-CN").format(value);

export function HorizontalBarChart({
  data,
  summary,
  title,
  valueFormatter = defaultFormatter,
  valueLabel,
}: HorizontalBarChartProps) {
  return (
    <ChartFrame
      className="gf-chart--bar"
      columns={[
        { key: "label", label: "分类" },
        { key: "value", label: valueLabel },
      ]}
      rows={data.map((item) => ({ label: item.label, value: valueFormatter(item.value) }))}
      summary={summary}
      title={title}
    >
      <div className="gf-chart__plot gf-chart__plot--bar" aria-hidden="true">
        {data.length === 0 ? (
          <p className="gf-chart__empty">暂无数据</p>
        ) : (
          <ResponsiveContainer height="100%" minWidth={0} width="100%">
            <BarChart
              accessibilityLayer={false}
              data={[...data]}
              layout="vertical"
              margin={{ bottom: 12, left: 4, right: 18, top: 8 }}
            >
              <CartesianGrid horizontal={false} stroke="var(--line)" />
              <XAxis
                axisLine={false}
                tick={{ fill: "var(--muted)", fontSize: 11 }}
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
              <Bar dataKey="value" isAnimationActive={false} maxBarSize={24} radius={[0, 4, 4, 0]}>
                {data.map((item, index) => (
                  <Cell fill={COLORS[index % COLORS.length]} key={item.label} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>
    </ChartFrame>
  );
}
