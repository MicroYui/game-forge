import { useId, type ReactNode } from "react";

export interface ChartTableColumn {
  key: string;
  label: string;
}

export type ChartTableRow = Readonly<Record<string, ReactNode>>;

interface ChartFrameProps {
  children: ReactNode;
  className?: string;
  columns: readonly ChartTableColumn[];
  rows: readonly ChartTableRow[];
  summary: string;
  title: string;
}

export function ChartFrame({ children, className = "", columns, rows, summary, title }: ChartFrameProps) {
  const titleId = useId();

  return (
    <figure className={`gf-chart ${className}`.trim()} aria-labelledby={titleId}>
      <figcaption className="gf-chart__caption">
        <h3 id={titleId}>{title}</h3>
        <p className="gf-chart__summary">{summary}</p>
      </figcaption>

      {children}

      <details className="gf-chart__data">
        <summary>查看{title}数据表</summary>
        <div className="gf-chart__table-scroll" tabIndex={0}>
          <table aria-label={`${title}数据表`}>
            <thead>
              <tr>
                {columns.map((column) => (
                  <th key={column.key} scope="col">
                    {column.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {columns.map((column, columnIndex) => {
                    const Cell = columnIndex === 0 ? "th" : "td";
                    return (
                      <Cell key={column.key} scope={columnIndex === 0 ? "row" : undefined}>
                        {row[column.key]}
                      </Cell>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
