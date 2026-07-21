import type { ReactNode } from "react";

import "./tables.css";

export interface CursorTableColumn<T> {
  header: string;
  id: string;
  render(item: T): ReactNode;
}

export type CursorPaginationState = "ready" | "loading" | "expired" | "error";

export interface CursorTableProps<T> {
  caption: string;
  columns: readonly CursorTableColumn<T>[];
  emptyLabel?: string;
  getRowKey(item: T): string;
  headingLevel?: 2 | 3;
  items: readonly T[];
  nextCursor?: string | null;
  onLoadMore?(cursor: string): void;
  onRestart?(): void;
  paginationState?: CursorPaginationState;
  toolbar?: ReactNode;
}

export function CursorTable<T>({
  caption,
  columns,
  emptyLabel = "暂无数据",
  getRowKey,
  headingLevel = 3,
  items,
  nextCursor,
  onLoadMore,
  onRestart,
  paginationState = "ready",
  toolbar,
}: CursorTableProps<T>) {
  const canLoadMore = Boolean(nextCursor && onLoadMore);
  const needsRestart = paginationState === "expired";
  const Heading = headingLevel === 2 ? "h2" : "h3";

  return (
    <section className="gf-cursor-table" aria-label={caption}>
      <div className="gf-cursor-table__toolbar">
        <Heading className="gf-cursor-table__heading">{caption}</Heading>
        {toolbar && <div className="gf-cursor-table__toolbar-actions">{toolbar}</div>}
      </div>

      <div className="gf-cursor-table__scroll" tabIndex={0}>
        <table>
          <caption className="u-sr-only">{caption}</caption>
          <thead>
            <tr>
              {columns.map((column) => (
                <th key={column.id} scope="col">
                  {column.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={getRowKey(item)}>
                {columns.map((column) => (
                  <td key={column.id}>{column.render(item)}</td>
                ))}
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td className="gf-cursor-table__empty" colSpan={columns.length}>
                  {emptyLabel}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="gf-cursor-table__pagination">
        {paginationState === "expired" && (
          <p role="status">分页游标已过期；现有行仅代表过期前已读取的快照。</p>
        )}
        {paginationState === "error" && <p role="status">下一页读取失败。</p>}
        {needsRestart && onRestart && (
          <button className="gf-secondary-button" onClick={onRestart} type="button">
            重新开始查询
          </button>
        )}
        {paginationState === "error" && canLoadMore && (
          <button
            className="gf-secondary-button"
            onClick={() => {
              if (nextCursor) onLoadMore?.(nextCursor);
            }}
            type="button"
          >
            重试下一页
          </button>
        )}
        {paginationState !== "error" && !needsRestart && canLoadMore && (
          <button
            className="gf-secondary-button"
            disabled={paginationState === "loading"}
            onClick={() => {
              if (nextCursor) onLoadMore?.(nextCursor);
            }}
            type="button"
          >
            {paginationState === "loading" ? "正在加载…" : "加载下一页"}
          </button>
        )}
        {paginationState !== "error" && !needsRestart && !canLoadMore && <p>已到末页</p>}
      </div>
    </section>
  );
}
