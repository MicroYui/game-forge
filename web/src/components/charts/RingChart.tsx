import { Cell, Pie, PieChart, ResponsiveContainer } from "recharts";

import { ChartFrame } from "./ChartFrame";

export interface RingDatum {
  label: string;
  value: number;
}

export interface RingChartProps {
  data: readonly RingDatum[];
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

export function RingChart({
  data,
  summary,
  title,
  valueFormatter = defaultFormatter,
  valueLabel,
}: RingChartProps) {
  const total = data.reduce((sum, item) => sum + item.value, 0);

  return (
    <ChartFrame
      className="gf-chart--ring"
      columns={[
        { key: "label", label: "分类" },
        { key: "value", label: valueLabel },
      ]}
      rows={data.map((item) => ({ label: item.label, value: valueFormatter(item.value) }))}
      summary={summary}
      title={title}
    >
      <div className="gf-chart__ring-layout">
        <div className="gf-chart__plot gf-chart__plot--ring" aria-hidden="true">
          {data.length === 0 ? (
            <p className="gf-chart__empty">暂无数据</p>
          ) : (
            <>
              <ResponsiveContainer height="100%" minWidth={0} width="100%">
                <PieChart accessibilityLayer={false}>
                  <Pie
                    data={[...data]}
                    dataKey="value"
                    innerRadius="58%"
                    isAnimationActive={false}
                    nameKey="label"
                    outerRadius="82%"
                    paddingAngle={2}
                    rootTabIndex={-1}
                    stroke="var(--surface)"
                    strokeWidth={2}
                  >
                    {data.map((item, index) => (
                      <Cell fill={COLORS[index % COLORS.length]} key={item.label} />
                    ))}
                  </Pie>
                </PieChart>
              </ResponsiveContainer>
              <div className="gf-chart__ring-total">
                <strong>{valueFormatter(total)}</strong>
                <span>{valueLabel}</span>
              </div>
            </>
          )}
        </div>

        <ul className="gf-chart__legend" aria-label={`${title}分类`}>
          {data.map((item, index) => (
            <li key={item.label}>
              <span
                aria-hidden="true"
                className="gf-chart__swatch"
                style={{ backgroundColor: COLORS[index % COLORS.length] }}
              />
              <span className="gf-chart__legend-label">{item.label}</span>
              <strong>{valueFormatter(item.value)}</strong>
            </li>
          ))}
        </ul>
      </div>
    </ChartFrame>
  );
}
