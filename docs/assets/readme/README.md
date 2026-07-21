# README 媒体来源说明

本目录只保存 GitHub 首页展示所需的轻量媒体与示意图。

## Journey A 截图

`hero-journey-a-v2-zh.png` 与 `01`–`11` PNG 来自同一次
`journey-a-authoring.spec.ts` 真实端到端录制：

- 本地 API 与 worker 真实启动；
- 产品 API 未 mock / intercept；
- Agent 调用使用冻结 cassette REPLAY；
- 浏览器与 launcher 的外部网络均 fail-closed；
- 录制同时通过完整 Journey A Playwright 断言。

画面中的时间、哈希、身份与内容均为测试证据，不代表在线生产数据。截图选择点由
`web/scripts/journey-a-demo-storyboard.ts` 的 `DEMO_README_FRAMES` 冻结，并由单测校验顺序与主 / 次证据位置。

## 视频

中文 V2 实测约 83.96 秒、1280 × 720、无音轨 H.264 MP4，由同一次 WebM 录制转码生成。
当前使用仓库内单个 4,273,106-byte 压缩文件；SHA-256 为
`3b173dec05f5dec91a43ff4b4151f7573ebb925528c0034826ab683d39d0ffb0`。
转码固定为 `libx264 / CRF 25 / preset slow / yuv420p / faststart / no audio`。
GitHub 首页以封面链接该 raw MP4；GitHub 当前把仓库二进制作为下载响应提供，因此 CTA
明确标为“下载 / 播放”，不冒充 README 内联播放器。
后续可替换版本应优先使用 GitHub attachment / Release，避免把重复大二进制写入 Git 历史。

## 示意图

- `product-loop.svg`：产品闭环与 trust boundary 解释图。
- `evidence-surfaces.svg`：Aureus、Flare、Endless Sky 三种证据面的范围图。

两张 SVG 都是仓库原生说明图，不是产品截图，也没有包含第三方美术资源。
