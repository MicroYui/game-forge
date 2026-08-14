# README 媒体来源说明

本目录保存 GitHub 首页使用的产品截图、无声演示视频与仓库原生示意图。

## 当前项目优先旅程

以下文件来自同一次 [`project-first-authoring.spec.ts`](../../../web/e2e/project-first-authoring.spec.ts) Playwright 执行：

- `hero-project-workflow-zh.png`；
- `project-flow-01-project.png` 至 `project-flow-09-playtest.png`；
- `gameforge-project-workflow-zh.mp4`。

该用例在 fresh workspace 完成“创建天空港项目 → 保存飞书材料 → AI 实体关系提案 → 确定性身份归一 → 图谱编辑 → 内容 v1 → 项目规则 → 继续生成 → 内容 v2 → 自动试玩”：

- 本地 API 与 worker 真实启动，产品 API 未 mock / intercept；
- Agent 运行经过真实 Model Router / worker 路径，但底层是隔离的固定模型替身，不是在线付费模型；
- 浏览器与 launcher 的外部网络均 fail-closed；
- 内容与规则候选真实经历验证、审批与 Apply；
- 演示中的审批使用 `platform_admin` 显式自审，普通 maker 自审仍被拒绝；
- 最终 Playtest 真实返回 `0 / 1`，页面显示“仍有试玩任务未完成”，没有为视频制造绿色结果；
- 录制与完整 Playwright 产品断言一起通过。

画面中的时间、身份、项目 ID 与内容属于隔离的本地示例，不代表在线生产数据。中文章节、顺序与截图文件名由 [`project-demo-storyboard.ts`](../../../web/scripts/project-demo-storyboard.ts) 固定，并由单测校验。

## 录制与视频校验

Playwright 原始视频由 Chromium 以 VP8 WebM、1280 × 720、25 fps 录制；仓库提交的是同一视频通过 Chromium `MediaRecorder` 转出的 H.264 / AVC MP4，请求 MIME 为 `video/mp4;codecs=avc1.42E01E`。转封装没有剪辑内容，也没有音轨。

- 文件：`gameforge-project-workflow-zh.mp4`
- 时长：92.912667 秒
- 画面：1280 × 720
- 音轨：无
- 容器 / 编码：fragmented MP4 / H.264 AVC (`avc1`)
- 大小：7,806,788 bytes（约 7.45 MiB）
- SHA-256：`a4bcae0b3badc9ec30f88845a9814ef8569a7660d07dd2b134d4e3671767b29c`

原始 WebM 未提交，其本次录制校验值为：

- 大小：7,897,755 bytes（约 7.53 MiB）
- SHA-256：`5ef0528f8a1f3fa6d65f64bbce1190e173020d09e1d506917b7de8530125aa0a`

转码后的 MP4 已在 Chromium 中读取并确认时长、1280 × 720 尺寸及无解码错误，并在 1、30、70 秒抽帧做视觉检查。GitHub 首页以封面链接 raw MP4，因此 CTA 写作“下载 / 播放”，不冒充 README 内联播放器。

## 复现录制

```bash
cd web
GAMEFORGE_RECORD_DEMO=1 npm run test:e2e -- --grep \
  "creates a game from Feishu material"
```

输出目录为 `web/test-results/demo-project-workflow/`：

- `gameforge-project-workflow-zh.webm`：Playwright 原始视频；
- `hero-project-workflow-zh.png`：README 封面；
- `readme-frames-project-workflow-zh/`：9 张同源流程截图。

## 补充页面图

- `01-spec-authority.png`：版本化 Spec authority；
- `02-knowledge-graph.png`：可探索的 Spec-IR 知识图谱；
- `10-eval-bench.png`：版本化评测与证据引用；
- `11-observability.png`：Run、Trace、日志、成本与预算。

它们是隔离产品栈的真实本地页面截图，用于补充主流程没有停留展示的工作台区域。

## 原生示意图

- `product-loop.svg`：产品闭环与 trust boundary；
- `evidence-surfaces.svg`：Aureus、Flare、Endless Sky 三种证据面的范围。

两张 SVG 都是仓库原生说明图，不是产品截图，也不包含第三方美术资源。
