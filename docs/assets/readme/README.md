# README 媒体来源说明

本目录保存 GitHub 首页使用的轻量媒体、产品截图与原生示意图。

## 完整使用流程

`hero-complete-workflow-zh.png`、`flow-01-input.png` 至
`flow-10-live-ref-history.png`，以及 `gameforge-complete-workflow-zh.mp4` 来自同一次
[浏览器端到端用例](../../../web/e2e/journey-a-authoring.spec.ts)真实执行：

- 输入帧在点击“开始生成”之前捕获，展示真实可提交的表单状态；
- 本地 API 与 worker 真实启动，产品 API 未 mock / intercept；
- Agent 调用使用冻结 cassette REPLAY；
- 浏览器与 launcher 的外部网络均 fail-closed；
- 候选、Review、Playtest、修复、审批、apply 与 ref history 全部来自同一次提交；
- 录制与完整 Playwright 产品断言一起通过。

画面中的时间、哈希、身份与内容属于隔离的本地示例证据，不代表在线生产数据。截图选择点由
`web/scripts/journey-a-demo-storyboard.ts` 的 `DEMO_README_FRAMES` 固定，并由单测校验顺序与主 / 次证据位置。

## 视频校验

- 文件：`gameforge-complete-workflow-zh.mp4`
- 时长：87.40 秒
- 画面：1280 × 720、25 fps、无音轨
- 编码：H.264 High / yuv420p / faststart
- 大小：3,464,065 bytes（约 3.30 MiB）
- SHA-256：`a18df6e87bcb5e1fa1d6a621b5e9b88457988b86dc2686555b507242cb5d2274`
- 转码：`libx264 / CRF 25 / preset slow / no audio`

GitHub 首页以封面链接 raw MP4。GitHub 将仓库二进制作为下载响应提供，因此 CTA 明确写作“下载 / 播放”，不冒充 README 内联播放器。

## 补充页面图

- `01-spec-authority.png`：版本化 Spec authority。
- `02-knowledge-graph.png`：可探索的 Spec-IR 知识图谱。
- `10-eval-bench.png`：版本化评测与证据引用。
- `11-observability.png`：Run、Trace、日志、成本与预算。

它们是同一隔离产品栈的真实本地页面截图，用于补充主流程没有停留展示的工作台区域。

## 原生示意图

- `product-loop.svg`：产品闭环与 trust boundary。
- `evidence-surfaces.svg`：Aureus、Flare、Endless Sky 三种证据面的范围。

两张 SVG 都是仓库原生说明图，不是产品截图，也不包含第三方美术资源。
