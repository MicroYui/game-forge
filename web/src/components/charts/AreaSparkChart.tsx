import { Area, AreaChart, ResponsiveContainer } from "recharts";

import { ChartFrame } from "./ChartFrame";

export interface AreaSparkDatum {
  label: string;
  value: number;
}

export interface AreaSparkChartProps {
  data: readonly AreaSparkDatum[];
  labelLabel?: string;
  summary: string;
  title: string;
  valueFormatter?: (value: number) => string;
  valueLabel: string;
}

const defaultFormatter = (value: number) => new Intl.NumberFormat("zh-CN").format(value);

export function AreaSparkChart({
  data,
  labelLabel = "时间",
  summary,
  title,
  valueFormatter = defaultFormatter,
  valueLabel,
}: AreaSparkChartProps) {
  return (
    <ChartFrame
      className="gf-chart--spark"
      columns={[
        { key: "label", label: labelLabel },
        { key: "value", label: valueLabel },
      ]}
      rows={data.map((item) => ({ label: item.label, value: valueFormatter(item.value) }))}
      summary={summary}
      title={title}
    >
      <div className="gf-chart__plot gf-chart__plot--spark" aria-hidden="true">
        {data.length === 0 ? (
          <p className="gf-chart__empty">暂无数据</p>
        ) : (
          <ResponsiveContainer height="100%" minWidth={0} width="100%">
            <AreaChart
              accessibilityLayer={false}
              data={[...data]}
              margin={{ bottom: 4, left: 3, right: 3, top: 4 }}
            >
              <Area
                dataKey="value"
                dot={{ fill: "var(--surface)", r: 2.5, strokeWidth: 2 }}
                fill="var(--viz-sequential-teal-2)"
                fillOpacity={0.58}
                isAnimationActive={false}
                stroke="var(--viz-teal)"
                strokeWidth={2}
                type="monotone"
              />
            </AreaChart>
          </ResponsiveContainer>
        )}
      </div>
    </ChartFrame>
  );
}
