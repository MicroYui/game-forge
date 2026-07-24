import cytoscape, { type Core, type EventObject, type StylesheetJson } from "cytoscape";
import { ArrowRight, Check, CircleDot, GitBranch, Maximize2, Search } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import { CopyableText } from "../tables";
import type { CursorPaginationState } from "../tables/CursorTable";
import {
  adaptGraphItems,
  formatSourceRef,
  graphFactDisplayName,
  graphFactKey,
  graphSearchText,
  graphTypeLabel,
  toCytoscapeElements,
  type EntityGraphFact,
  type GraphFact,
  type GraphItem,
  type RelationGraphFact,
} from "./model";
import "./kg.css";

interface GraphPalette {
  background: string;
  edge: string;
  edgeLabel: string;
  ink: string;
  muted: string;
  node: string;
  reference: string;
  selected: string;
  surface: string;
}

const paletteFallback: GraphPalette = {
  background: "#f7f8f6",
  edge: "#956316",
  edgeLabel: "#4f5852",
  ink: "#222624",
  muted: "#667069",
  node: "#216c67",
  reference: "#89928b",
  selected: "#4f63a5",
  surface: "#fff",
};

function readPalette(): GraphPalette {
  if (typeof document === "undefined") return paletteFallback;
  const styles = window.getComputedStyle(document.documentElement);
  const token = (name: string, fallback: string) => styles.getPropertyValue(name).trim() || fallback;
  return {
    background: token("--surface-2", paletteFallback.background),
    edge: token("--suggestion", paletteFallback.edge),
    edgeLabel: token("--ink-2", paletteFallback.edgeLabel),
    ink: token("--ink", paletteFallback.ink),
    muted: token("--muted", paletteFallback.muted),
    node: token("--deterministic", paletteFallback.node),
    reference: token("--faint", paletteFallback.reference),
    selected: token("--info", paletteFallback.selected),
    surface: token("--surface", paletteFallback.surface),
  };
}

function useGraphPalette(): GraphPalette {
  const [palette, setPalette] = useState(readPalette);

  useEffect(() => {
    const root = document.documentElement;
    const observer = new MutationObserver(() => setPalette(readPalette()));
    observer.observe(root, { attributeFilter: ["data-theme"], attributes: true });
    return () => observer.disconnect();
  }, []);

  return palette;
}

function graphStyles(palette: GraphPalette): StylesheetJson {
  return [
    {
      selector: "node",
      style: {
        "background-color": palette.surface,
        "border-color": palette.node,
        "border-width": 2,
        color: palette.ink,
        "font-family": '"SF Mono", ui-monospace, monospace',
        "font-size": 10,
        height: "48px",
        label: "data(label)",
        "text-halign": "center",
        "text-max-width": "108px",
        "text-valign": "center",
        "text-wrap": "wrap",
        width: "128px",
      },
    },
    {
      selector: "node.gf-kg-node--reference",
      style: {
        "background-color": palette.background,
        "border-color": palette.reference,
        "border-style": "dashed",
        color: palette.muted,
      },
    },
    {
      selector: "edge",
      style: {
        "curve-style": "bezier",
        "font-family": '"SF Mono", ui-monospace, monospace',
        "font-size": 9,
        label: "data(label)",
        "line-color": palette.edge,
        "target-arrow-color": palette.edge,
        "target-arrow-shape": "triangle",
        "text-background-color": palette.background,
        "text-background-opacity": 1,
        "text-background-padding": "2px",
        "text-rotation": "autorotate",
        color: palette.edgeLabel,
        width: "1.5px",
      },
    },
    {
      selector: ":selected, .is-selected",
      style: {
        "border-color": palette.selected,
        "border-width": 4,
        "line-color": palette.selected,
        "target-arrow-color": palette.selected,
        "z-index": 20,
      },
    },
    {
      selector: ".is-search-muted",
      style: { opacity: 0.22 },
    },
  ];
}

function FactKind({ fact }: { fact: GraphFact }) {
  const Icon = fact.kind === "entity" ? CircleDot : GitBranch;
  return (
    <span className="gf-kg__kind">
      <Icon aria-hidden="true" size={14} />
      {fact.kind === "entity" ? "实体" : "关系"}
    </span>
  );
}

function Endpoint({ entity, id }: { entity?: EntityGraphFact; id: string }) {
  return (
    <span className="gf-kg__endpoint">
      <strong>{entity ? graphFactDisplayName(entity) : "未载入实体"}</strong>
      <CopyableText copyLabel={`复制端点实体 ID ${id}`} value={id} />
      {!entity && <span className="gf-kg__endpoint-note">端点未包含在当前页</span>}
    </span>
  );
}

function FactInspector({
  entitiesById,
  fact,
}: {
  entitiesById: ReadonlyMap<string, EntityGraphFact>;
  fact: GraphFact;
}) {
  return (
    <aside className="gf-kg__inspector" aria-label="已选图谱事实" tabIndex={0}>
      <div className="gf-kg__inspector-heading">
        <FactKind fact={fact} />
        <span className="gf-kg__selection-label">当前选择：{fact.kind === "entity" ? "实体" : "关系"}</span>
      </div>
      <div className="gf-kg__inspector-title">
        <h3>{graphFactDisplayName(fact)}</h3>
        <CopyableText
          copyLabel={`复制${fact.kind === "entity" ? "实体" : "关系"} ID ${fact.id}`}
          scrollable
          value={fact.id}
        />
      </div>
      <dl className="gf-kg__facts">
        <div>
          <dt>类型</dt>
          <dd className="gf-kg__type-value">
            <span>{graphTypeLabel(fact)}</span>
            <code>{fact.type}</code>
          </dd>
        </div>
        <div>
          <dt>Schema</dt>
          <dd>
            <code>{fact.schemaVersion}</code>
          </dd>
        </div>
        {fact.kind === "relation" && (
          <>
            <div>
              <dt>起点实体</dt>
              <dd>
                <Endpoint entity={entitiesById.get(fact.srcId)} id={fact.srcId} />
              </dd>
            </div>
            <div>
              <dt>终点实体</dt>
              <dd>
                <Endpoint entity={entitiesById.get(fact.dstId)} id={fact.dstId} />
              </dd>
            </div>
          </>
        )}
        {fact.kind === "entity" && (
          <div>
            <dt>标签</dt>
            <dd>{fact.tags.length > 0 ? fact.tags.join(" · ") : "无"}</dd>
          </div>
        )}
        <div>
          <dt>来源</dt>
          <dd>{formatSourceRef(fact.sourceRef)}</dd>
        </div>
        <div>
          <dt>来源适配器</dt>
          <dd>
            <code>{fact.sourceRef?.adapter ?? "无"}</code>
          </dd>
        </div>
      </dl>
      <div className="gf-kg__attributes">
        <h3>属性</h3>
        {Object.keys(fact.attrs).length === 0 ? (
          <p>无属性</p>
        ) : (
          <pre>{JSON.stringify(fact.attrs, null, 2)}</pre>
        )}
      </div>
    </aside>
  );
}

function relationEndpoints(fact: RelationGraphFact, entitiesById: ReadonlyMap<string, EntityGraphFact>) {
  const source = entitiesById.get(fact.srcId);
  const target = entitiesById.get(fact.dstId);
  return (
    <span className="gf-kg__relation-summary">
      <span>
        <strong>{source ? graphFactDisplayName(source) : "未载入实体"}</strong>
        <code>{fact.srcId}</code>
      </span>
      <ArrowRight aria-hidden="true" size={14} />
      <span>
        <strong>{target ? graphFactDisplayName(target) : "未载入实体"}</strong>
        <code>{fact.dstId}</code>
      </span>
    </span>
  );
}

export interface KnowledgeGraphProps {
  ariaLabel?: string;
  items: readonly GraphItem[];
  nextCursor?: string | null;
  onLoadMore?(cursor: string): void;
  onRestart?(): void;
  onSearchQueryChange?(query: string): void;
  onSelectedFactKeyChange?(factKey: string | null): void;
  pageLabel?: string;
  paginationState?: CursorPaginationState;
  searchQuery?: string;
  selectedFactKey?: string | null;
}

function updateGraphSelection(cy: Core, selectedKey: string | null) {
  cy.elements().unselect();
  cy.elements().removeClass("is-selected");
  if (selectedKey === null) return;
  const selected = cy.$id(selectedKey);
  if (!selected.empty()) {
    selected.select();
    selected.addClass("is-selected");
  }
}

function updateGraphSearch(cy: Core, facts: readonly GraphFact[], normalizedQuery: string) {
  cy.elements().removeClass("is-search-muted");
  if (!normalizedQuery) return;
  for (const fact of facts) {
    if (!graphSearchText(fact).includes(normalizedQuery))
      cy.$id(graphFactKey(fact)).addClass("is-search-muted");
  }
}

export function KnowledgeGraph({
  ariaLabel = "知识图谱",
  items,
  nextCursor,
  onLoadMore,
  onRestart,
  onSearchQueryChange,
  onSelectedFactKeyChange,
  pageLabel = "当前图谱页",
  paginationState = "ready",
  searchQuery,
  selectedFactKey,
}: KnowledgeGraphProps) {
  const listTitleId = useId();
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const facts = useMemo(() => adaptGraphItems(items), [items]);
  const elements = useMemo(() => toCytoscapeElements(facts), [facts]);
  const entitiesById = useMemo(
    () =>
      new Map(
        facts
          .filter((fact): fact is EntityGraphFact => fact.kind === "entity")
          .map((fact) => [fact.id, fact] as const),
      ),
    [facts],
  );
  const palette = useGraphPalette();
  const [internalQuery, setInternalQuery] = useState("");
  const query = searchQuery ?? internalQuery;
  const updateQuery = useCallback(
    (nextQuery: string) => {
      if (searchQuery === undefined) setInternalQuery(nextQuery);
      onSearchQueryChange?.(nextQuery);
    },
    [onSearchQueryChange, searchQuery],
  );
  const normalizedQuery = query.trim().toLocaleLowerCase("zh-CN");
  const visibleFacts = useMemo(
    () => (normalizedQuery ? facts.filter((fact) => graphSearchText(fact).includes(normalizedQuery)) : facts),
    [facts, normalizedQuery],
  );
  const [internalSelectedKey, setInternalSelectedKey] = useState<string | null>(() =>
    facts.length > 0 ? graphFactKey(facts[0]!) : null,
  );
  const selectionIsControlled = selectedFactKey !== undefined;
  const selectedKey = selectionIsControlled ? selectedFactKey : internalSelectedKey;
  const updateSelectedKey = useCallback(
    (nextKey: string | null) => {
      if (!selectionIsControlled) setInternalSelectedKey(nextKey);
      onSelectedFactKeyChange?.(nextKey);
    },
    [onSelectedFactKeyChange, selectionIsControlled],
  );
  const selectedKeyRef = useRef(selectedKey);
  const queryRef = useRef(normalizedQuery);
  selectedKeyRef.current = selectedKey;
  queryRef.current = normalizedQuery;

  const selectedFact = facts.find((fact) => graphFactKey(fact) === selectedKey) ?? null;
  const entityCount = facts.filter((fact) => fact.kind === "entity").length;
  const relationCount = facts.length - entityCount;
  const hasReferenceNodes = facts.some(
    (fact) => fact.kind === "relation" && (!entitiesById.has(fact.srcId) || !entitiesById.has(fact.dstId)),
  );

  useEffect(() => {
    const selectableFacts = normalizedQuery ? visibleFacts : facts;
    if (selectableFacts.length === 0) {
      if (selectedKey !== null) updateSelectedKey(null);
      return;
    }
    if (selectedKey !== null && selectableFacts.some((fact) => graphFactKey(fact) === selectedKey)) return;
    updateSelectedKey(graphFactKey(selectableFacts[0]!));
  }, [facts, normalizedQuery, selectedKey, updateSelectedKey, visibleFacts]);

  useEffect(() => {
    const container = containerRef.current;
    if (container === null) return;

    const cy = cytoscape({
      boxSelectionEnabled: false,
      container,
      elements,
      layout: { avoidOverlap: true, fit: true, name: "grid", padding: 28 },
      maxZoom: 1.8,
      minZoom: 0.55,
      selectionType: "single",
      style: graphStyles(palette),
    });
    const handleTap = (event: EventObject) => {
      const factKey = event.target.data("factKey") as unknown;
      if (typeof factKey === "string") updateSelectedKey(factKey);
    };
    cy.on("tap", "node, edge", handleTap);
    cyRef.current = cy;
    updateGraphSelection(cy, selectedKeyRef.current);
    updateGraphSearch(cy, facts, queryRef.current);

    return () => {
      cy.off("tap", "node, edge", handleTap);
      if (cyRef.current === cy) cyRef.current = null;
      cy.destroy();
    };
  }, [elements, facts, palette, updateSelectedKey]);

  useEffect(() => {
    if (cyRef.current !== null) updateGraphSelection(cyRef.current, selectedKey);
  }, [selectedKey]);

  useEffect(() => {
    if (cyRef.current !== null) updateGraphSearch(cyRef.current, facts, normalizedQuery);
  }, [facts, normalizedQuery]);

  const canLoadMore = Boolean(nextCursor && onLoadMore && paginationState !== "expired");

  return (
    <section className="gf-kg" aria-label={ariaLabel}>
      <header className="gf-kg__header">
        <div>
          <p className="gf-kg__eyebrow">有界 Spec-IR 图谱</p>
          <h2>{pageLabel}</h2>
        </div>
        <p className="gf-kg__count">
          当前有界页：{entityCount} 个实体，{relationCount} 条关系
        </p>
      </header>

      <div className="gf-kg__workspace">
        <div className="gf-kg__canvas-panel">
          <div className="gf-kg__canvas-toolbar">
            <span>关系视图</span>
            <button
              className="gf-secondary-button"
              onClick={() => cyRef.current?.fit(undefined, 28)}
              type="button"
            >
              <Maximize2 aria-hidden="true" size={15} />
              适配视图
            </button>
          </div>
          <div
            className="gf-kg__canvas"
            aria-hidden="true"
            data-testid="knowledge-graph-canvas"
            ref={containerRef}
          />
          <p className="gf-kg__canvas-note">画布使用稳定网格布局；全部事实也列在下方，可通过键盘完整检查。</p>
          {hasReferenceNodes && <p className="gf-kg__reference-note">关系仍可在事实列表中完整检查。</p>}
        </div>
        {selectedFact ? (
          <FactInspector entitiesById={entitiesById} fact={selectedFact} />
        ) : (
          <aside className="gf-kg__inspector gf-kg__inspector--empty" aria-label="已选图谱事实" tabIndex={0}>
            <p>{facts.length === 0 ? "当前页没有图谱事实。" : "请选择一个图谱事实。"}</p>
          </aside>
        )}
      </div>

      <section className="gf-kg__list" aria-labelledby={listTitleId}>
        <div className="gf-kg__list-toolbar">
          <div>
            <h3 id={listTitleId}>图谱事实列表</h3>
            <p>与画布共享选择状态；列表是无 canvas 环境的完整事实界面。</p>
          </div>
          <label className="gf-kg__search">
            <span>搜索当前图谱页</span>
            <span className="gf-kg__search-control">
              <Search aria-hidden="true" size={16} />
              <input
                onChange={(event) => updateQuery(event.target.value)}
                placeholder="ID、类型、端点、属性或来源"
                type="search"
                value={query}
              />
            </span>
          </label>
        </div>
        <p className="gf-kg__search-status" role="status">
          {normalizedQuery
            ? `找到 ${visibleFacts.length} 项；画布中其余事实已弱化。`
            : `显示当前页全部 ${facts.length} 项。`}
        </p>
        <div className="gf-kg__table-scroll" tabIndex={0}>
          <table aria-label="图谱事实列表">
            <thead>
              <tr>
                <th scope="col">类别</th>
                <th scope="col">名称与类型</th>
                <th scope="col">技术标识</th>
                <th scope="col">关系端点 / 标签</th>
                <th scope="col">检查</th>
              </tr>
            </thead>
            <tbody>
              {visibleFacts.map((fact) => {
                const key = graphFactKey(fact);
                const selected = key === selectedKey;
                return (
                  <tr aria-selected={selected} data-selected={selected || undefined} key={key}>
                    <td>
                      <FactKind fact={fact} />
                    </td>
                    <td className="gf-kg__fact-name">
                      <strong>{graphFactDisplayName(fact)}</strong>
                      <span>
                        {fact.kind === "entity" ? graphTypeLabel(fact) : "关系类型"} ·{" "}
                        <code>{fact.type}</code>
                      </span>
                    </td>
                    <td>
                      <CopyableText
                        copyLabel={`复制${fact.kind === "entity" ? "实体" : "关系"} ID ${fact.id}`}
                        scrollable
                        value={fact.id}
                      />
                    </td>
                    <td>
                      {fact.kind === "relation"
                        ? relationEndpoints(fact, entitiesById)
                        : fact.tags.join(" · ") || "无标签"}
                    </td>
                    <td>
                      <button
                        aria-label={`检查${fact.kind === "entity" ? "实体" : "关系"} ${fact.id}`}
                        className="gf-kg__inspect-button"
                        onClick={() => updateSelectedKey(key)}
                        type="button"
                      >
                        {selected && <Check aria-hidden="true" size={14} />}
                        {selected ? "已选择" : "检查"}
                      </button>
                    </td>
                  </tr>
                );
              })}
              {visibleFacts.length === 0 && (
                <tr>
                  <td className="gf-kg__empty" colSpan={5}>
                    当前页没有匹配的图谱事实。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <footer className="gf-kg__pagination">
        {paginationState === "expired" && (
          <p role="status">图谱分页游标已过期；当前事实仅代表过期前已读取的快照，请显式重新开始。</p>
        )}
        {paginationState === "error" && <p role="status">下一页图谱读取失败；当前页仍可检查。</p>}
        {paginationState === "expired" && onRestart && (
          <button className="gf-secondary-button" onClick={onRestart} type="button">
            重新开始图谱查询
          </button>
        )}
        {canLoadMore && (
          <button
            className="gf-secondary-button"
            disabled={paginationState === "loading"}
            onClick={() => {
              if (nextCursor) onLoadMore?.(nextCursor);
            }}
            type="button"
          >
            {paginationState === "loading"
              ? "正在加载下一页…"
              : paginationState === "error"
                ? "重试下一页图谱"
                : "加载下一页图谱"}
          </button>
        )}
        {!canLoadMore && paginationState !== "expired" && paginationState !== "error" && (
          <p>当前读取已到末页。</p>
        )}
      </footer>
    </section>
  );
}
