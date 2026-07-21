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

中文 V2 实测约 83.96 秒、1280 × 720、无配音 WebM，同一次录制生成。当前使用仓库内单个
7,666,033-byte 压缩文件；SHA-256 为
`ceadecab0b53c46bfd6c8eb9ecc2184e524fd4a393fb6487d67a8e3c59b02d72`。
后续可替换版本应优先使用 GitHub attachment / Release，避免把重复大二进制写入 Git 历史。

## 示意图

- `product-loop.svg`：产品闭环与 trust boundary 解释图。
- `evidence-surfaces.svg`：Aureus、Flare、Endless Sky 三种证据面的范围图。

两张 SVG 都是仓库原生说明图，不是产品截图，也没有包含第三方美术资源。
