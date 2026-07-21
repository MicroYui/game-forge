import {
  AreaSparkChart,
  CostBarChart,
  HorizontalBarChart,
  RingChart,
  TraceWaterfall,
} from "../../components/charts";
import { MergeResolver, SnapshotDiffView } from "../../components/diff";
import { EvidenceSections, FindingCard } from "../../components/evidence";
import { ArtifactDetail } from "../../components/artifacts";
import { KnowledgeGraph } from "../../components/kg";
import { LogExplorer } from "../../components/logs";
import { TracePlayer } from "../../components/playtest";
import { CopyableText, CursorTable } from "../../components/tables";
import { StatePanel } from "../../components/ui";
import { Activity, Eye, FlaskConical, Gamepad2, LayoutGrid, Network, RouteOff } from "lucide-react";
import { Link, useSearchParams } from "react-router-dom";

import {
  artifactLineagePage,
  artifactSummary,
  aureusSpatialFixture,
  evidenceFindings,
  graphItems,
  mergeConflicts,
  safeLogRecords,
  snapshotDiff,
  snapshotDiffEntries,
  visualTrace,
} from "./fixtures";
import "./visual-foundation.css";

const views = [
  { icon: LayoutGrid, key: "components", label: "共享组件" },
  { icon: Network, key: "kg", label: "知识图谱" },
  { icon: Gamepad2, key: "trace-generic", label: "通用轨迹" },
  { icon: FlaskConical, key: "trace-aureus", label: "Aureus 2D" },
  { icon: RouteOff, key: "trace-fallback", label: "未知渲染器回退" },
  { icon: Activity, key: "states", label: "瞬态与长内容" },
] as const;

type FoundationView = (typeof views)[number]["key"];

function isFoundationView(value: string | null): value is FoundationView {
  return views.some((view) => view.key === value);
}

function ReviewDataLabel({ children }: { children: string }) {
  return (
    <p className="gf-visual-foundation__fixture-label">
      <Eye aria-hidden="true" size={14} />
      {children}
    </p>
  );
}

function ComponentsView() {
  return (
    <div className="gf-visual-foundation__sections">
      <section className="gf-visual-foundation__section" aria-labelledby="vf-evidence-title">
        <header className="gf-visual-foundation__section-heading">
          <div>
            <p>Evidence language</p>
            <h2 id="vf-evidence-title">三类证据语义</h2>
          </div>
          <span>文字、图标与边界共同区分，不只依赖颜色</span>
        </header>
        <EvidenceSections
          deterministic={<FindingCard finding={evidenceFindings.deterministic} />}
          simulation={<FindingCard finding={evidenceFindings.simulation} />}
          suggestion={<FindingCard finding={evidenceFindings.suggestion} />}
        />
      </section>

      <section className="gf-visual-foundation__section" aria-labelledby="vf-diff-title">
        <header className="gf-visual-foundation__section-heading">
          <div>
            <p>Exact change review</p>
            <h2 id="vf-diff-title">缺失、JSON null 与显式三方选择</h2>
          </div>
          <span>前端不自动裁决冲突</span>
        </header>
        <div className="gf-visual-foundation__stack">
          <SnapshotDiffView diff={snapshotDiff} entries={snapshotDiffEntries} />
          <MergeResolver conflicts={mergeConflicts} onResolutionsChange={() => undefined} />
        </div>
      </section>

      <section className="gf-visual-foundation__section" aria-labelledby="vf-charts-title">
        <header className="gf-visual-foundation__section-heading">
          <div>
            <p>Purposeful measurement</p>
            <h2 id="vf-charts-title">克制的数据可视化</h2>
          </div>
          <span>每张图都保留文字摘要与数据表</span>
        </header>
        <div className="gf-visual-foundation__chart-grid">
          <AreaSparkChart
            data={[
              { label: "周一", value: 0.72 },
              { label: "周二", value: 0.76 },
              { label: "周三", value: 0.81 },
              { label: "周四", value: 0.84 },
              { label: "周五", value: 0.9 },
            ]}
            labelLabel="日期"
            summary="修复通过率稳定上升，当前为 90%。"
            title="修复通过率"
            valueFormatter={(value) => `${Math.round(value * 100)}%`}
            valueLabel="通过率"
          />
          <RingChart
            data={[
              { label: "结构缺陷", value: 8 },
              { label: "经济缺陷", value: 3 },
              { label: "叙事建议", value: 2 },
            ]}
            summary="13 项证据按缺陷类别分布，结构缺陷占比最高。"
            title="缺陷构成"
            valueLabel="数量"
          />
          <HorizontalBarChart
            data={[
              { label: "悬挂引用", value: 8 },
              { label: "不可达步骤", value: 5 },
              { label: "经济崩坏", value: 3 },
              { label: "叙事提示", value: 2 },
            ]}
            summary="悬挂引用是当前数量最高的缺陷类型。"
            title="按缺陷类型"
            valueLabel="发现数"
          />
          <TraceWaterfall
            spans={[
              { durationMs: 128, id: "span:root", name: "验证补丁", startMs: 0, status: "ok" },
              {
                durationMs: 44,
                id: "span:checker",
                name: "确定性检查器",
                parentId: "span:root",
                startMs: 16,
                status: "error",
              },
              {
                durationMs: 51,
                id: "span:simulation",
                name: "经济仿真",
                parentId: "span:root",
                startMs: 66,
                status: "ok",
              },
            ]}
            summary="补丁验证耗时 128 毫秒；确定性检查器返回错误。"
            title="执行瀑布"
          />
          <CostBarChart
            data={[
              {
                consumed: { exact: "9007199254740993", plot: 9007199254740992 },
                label: "输出 token",
                limit: { exact: "12000000000000000", plot: 12_000_000_000_000_000 },
                reserved: { exact: "2500000000000000.125", plot: 2_500_000_000_000_000 },
                unit: "token",
              },
              {
                consumed: { exact: "18", plot: 18 },
                label: "Agent 步数",
                limit: { exact: "20", plot: 20 },
                reserved: { exact: "2", plot: 2 },
                unit: "step",
              },
            ]}
            summary="精确成本沿用 wire Decimal 字符串；图形只使用独立近似值。"
            title="成本预算"
          />
        </div>
      </section>

      <section className="gf-visual-foundation__section" aria-labelledby="vf-log-title">
        <header className="gf-visual-foundation__section-heading">
          <div>
            <p>Safe observability</p>
            <h2 id="vf-log-title">日志安全投影</h2>
          </div>
          <span>prompt、raw response 与凭据字段不进入视图</span>
        </header>
        <LogExplorer items={safeLogRecords} title="验证运行日志" />
      </section>

      <section className="gf-visual-foundation__section" aria-labelledby="vf-artifact-title">
        <header className="gf-visual-foundation__section-heading">
          <div>
            <p>Immutable provenance</p>
            <h2 id="vf-artifact-title">安全工件摘要与有界血缘</h2>
          </div>
          <span>存在不等于当前 ref 权威</span>
        </header>
        <ArtifactDetail artifact={artifactSummary} lineagePage={artifactLineagePage} />
      </section>
    </div>
  );
}

function KnowledgeGraphView() {
  return (
    <section className="gf-visual-foundation__section" aria-labelledby="vf-kg-title">
      <header className="gf-visual-foundation__section-heading">
        <div>
          <p>Bounded graph inspection</p>
          <h2 id="vf-kg-title">Spec-IR 当前有界页</h2>
        </div>
        <span>虚线端点明确表示未包含在当前页</span>
      </header>
      <ReviewDataLabel>GraphItemV1 视觉 fixture · 包含页外端点 SPAWN_POINT:outpost-gate</ReviewDataLabel>
      <KnowledgeGraph
        items={graphItems}
        nextCursor="opaque:visual-graph-next-page"
        onLoadMore={() => undefined}
        pageLabel="前哨信标任务图谱"
      />
    </section>
  );
}

const longArtifactId = `artifact:${"a".repeat(512)}`;

function StatesView() {
  const rows = [
    {
      id: longArtifactId,
      state: "streaming",
      title:
        "这是一条用于验证长中文排版、工具栏边界和内部滚动行为的只读证据记录；内容可以完整检查，但不会被误写成生产权威状态。",
    },
  ] as const;

  return (
    <div className="gf-visual-foundation__sections">
      <section className="gf-visual-foundation__section" aria-labelledby="vf-states-title">
        <header className="gf-visual-foundation__section-heading">
          <div>
            <p>Transient state language</p>
            <h2 id="vf-states-title">加载、错误与空态</h2>
          </div>
          <span>在 reduced-motion 下保留状态语义，不依赖动画或颜色</span>
        </header>
        <ReviewDataLabel>受控瞬态 fixture · prefers-reduced-motion: reduce</ReviewDataLabel>
        <div className="gf-visual-foundation__state-grid">
          <div data-testid="visual-state-streaming">
            <StatePanel
              description="事件流仍在接收；已经持久化的证据可继续检查。"
              headingLevel={3}
              state="streaming"
              title="正在接收运行事件"
            />
          </div>
          <div data-testid="visual-state-error">
            <StatePanel
              action={
                <button className="gf-secondary-button" type="button">
                  重试读取
                </button>
              }
              description="下一页读取失败；现有行保持可见，游标不会由客户端修复。"
              headingLevel={3}
              state="error"
              title="读取失败"
            />
          </div>
          <div data-testid="visual-state-empty">
            <StatePanel
              description="当前冻结条件下没有可展示的记录。"
              headingLevel={3}
              state="empty"
              title="暂无记录"
            />
          </div>
        </div>
      </section>

      <section className="gf-visual-foundation__section" aria-labelledby="vf-long-content-title">
        <header className="gf-visual-foundation__section-heading">
          <div>
            <p>Bounded content stress</p>
            <h2 id="vf-long-content-title">长中文、512 位 ID 与分页</h2>
          </div>
          <span>长内容留在有界滚动区，复制动作和分页状态保持可操作</span>
        </header>
        <CursorTable
          caption="长内容证据记录"
          columns={[
            {
              header: "工件 ID",
              id: "id",
              render: (row) => <CopyableText copyLabel="复制完整工件 ID" scrollable value={row.id} />,
            },
            { header: "说明", id: "title", render: (row) => row.title },
            {
              header: "状态",
              id: "state",
              render: () => <span className="gf-visual-foundation__status">接收中</span>,
            },
          ]}
          getRowKey={(row) => row.id}
          headingLevel={3}
          items={rows}
          nextCursor="opaque:v3-visual-next-page"
          onLoadMore={() => undefined}
          paginationState="loading"
        />
      </section>
    </div>
  );
}

function TraceView({ view }: { view: Exclude<FoundationView, "components" | "kg" | "states"> }) {
  if (view === "trace-aureus") {
    return (
      <section className="gf-visual-foundation__section" aria-labelledby="vf-trace-aureus-title">
        <header className="gf-visual-foundation__section-heading">
          <div>
            <p>Optional spatial capability</p>
            <h2 id="vf-trace-aureus-title">Aureus 2D 独立展示载荷</h2>
          </div>
          <span>真实动作轨迹仍来自 playtest-trace@1</span>
        </header>
        <ReviewDataLabel>
          独立且明确标注的 aureus-spatial-2d@1 fixture，不属于 playtest-trace@1
        </ReviewDataLabel>
        <TracePlayer
          rendererPayload={aureusSpatialFixture}
          rendererRequest={{
            capabilities: ["spatial_2d"],
            environmentContractVersion: "env@1",
            rendererId: "aureus.spatial-2d",
            rendererVersion: 1,
            tracePayloadSchemaId: "playtest-trace@1",
          }}
          trace={visualTrace}
        />
      </section>
    );
  }

  if (view === "trace-fallback") {
    return (
      <section className="gf-visual-foundation__section" aria-labelledby="vf-trace-fallback-title">
        <header className="gf-visual-foundation__section-heading">
          <div>
            <p>Fail-open inspection, fail-closed authority</p>
            <h2 id="vf-trace-fallback-title">未知渲染器的通用回退</h2>
          </div>
          <span>未知环境仍完整显示 action/result/tick/hash</span>
        </header>
        <ReviewDataLabel>unknown.environment-map@9 请求 · 必须回退通用检查视图</ReviewDataLabel>
        <TracePlayer
          rendererPayload={{ unknown_payload: "不会作为可信渲染载荷执行" }}
          rendererRequest={{
            capabilities: ["spatial_2d"],
            environmentContractVersion: "unknown-env@9",
            rendererId: "unknown.environment-map",
            rendererVersion: 9,
            tracePayloadSchemaId: "playtest-trace@1",
          }}
          trace={visualTrace}
        />
      </section>
    );
  }

  return (
    <section className="gf-visual-foundation__section" aria-labelledby="vf-trace-generic-title">
      <header className="gf-visual-foundation__section-heading">
        <div>
          <p>Contract-honest timeline</p>
          <h2 id="vf-trace-generic-title">通用 Playtest 轨迹</h2>
        </div>
        <span>state/events 未在当前契约提供时明确显示不可用</span>
      </header>
      <ReviewDataLabel>真实 playtest-trace@1 payload 经 adaptPlaytestEpisodeTrace 适配</ReviewDataLabel>
      <TracePlayer
        rendererRequest={{
          capabilities: [],
          environmentContractVersion: "env@1",
          rendererId: "generic.timeline",
          rendererVersion: 1,
          tracePayloadSchemaId: "playtest-trace@1",
        }}
        trace={visualTrace}
      />
    </section>
  );
}

export function VisualFoundationPage() {
  const [searchParams] = useSearchParams();
  const requestedView = searchParams.get("view");
  const view: FoundationView = isFoundationView(requestedView) ? requestedView : "components";

  return (
    <div
      className="gf-page gf-visual-foundation"
      data-visual-foundation-view={view}
      data-testid="visual-foundation"
    >
      <header className="gf-visual-foundation__hero">
        <div>
          <p className="gf-visual-foundation__kicker">M4d · Human gate V1</p>
          <h1>Editorial 视觉基础</h1>
          <p className="gf-visual-foundation__lede">
            在固定信息架构和真实组件契约上评审排版、密度、语义层级与数据图形语言。
          </p>
        </div>
        <div className="gf-visual-foundation__edition" aria-label="评审版本">
          <span>Baseline</span>
          <strong>Editorial · V1</strong>
          <small>2026.07.19</small>
        </div>
      </header>

      <aside className="gf-visual-foundation__notice" role="note">
        <Eye aria-hidden="true" size={20} />
        <div>
          <strong>视觉评审数据，不是权威状态</strong>
          <p>所有 ID、证据、工件、图谱与轨迹均为只读 fixture；不得据此判断生产运行或 ref 权威。</p>
        </div>
      </aside>

      <nav className="gf-visual-foundation__nav" aria-label="视觉基础视图">
        {views.map((item) => {
          const Icon = item.icon;
          const active = item.key === view;
          return (
            <Link
              aria-current={active ? "page" : undefined}
              className={active ? "is-active" : undefined}
              key={item.key}
              to={`/__visual__/foundation?view=${item.key}`}
            >
              <Icon aria-hidden="true" size={16} />
              {item.label}
            </Link>
          );
        })}
      </nav>

      <div className="gf-visual-foundation__content">
        {view === "components" ? (
          <ComponentsView />
        ) : view === "kg" ? (
          <KnowledgeGraphView />
        ) : view === "states" ? (
          <StatesView />
        ) : (
          <TraceView view={view} />
        )}
      </div>
    </div>
  );
}
