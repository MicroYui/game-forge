# 策划可用性硬化实现计划

> 日期：2026-07-26
> Goal：让策划在不认识 SHA、Artifact、revision 的前提下，独立看懂并走完整条链路；管理员拥有产品全部读写权限；全站时间按东八区呈现。
> 依赖：PRD、foundations-contracts v0.3、M4 production hardening design、硬规则 1–8（尤其 7 不兼容旧实现、8 不 overdesign）

## 实证问题清单

以下每条都来自 2026-07-26 以 `admin` 身份在真实服务上跑完整条策划链路的截图（项目创建 → 材料 → AI 提取 → 图谱编辑 → 发布草案 → 验证 → 审批 → 应用 → 各工作台），证据在 `/tmp/gameforge-ux-audit/flow/`。

| # | 问题 | 实证 |
|---|---|---|
| P1 | 管理员打不开系统指标 | 运行监控页「系统指标」区显示「没有操作权限」。`platform_admin` 的 grants 是各角色权限并集 + 项目相关，没有 `read metric`（trace/log/bench 同缺）。根因是**产品没有内置默认 RolePolicy**，每个部署各写一份，必然漏 |
| P2 | 「修改与版本」6 条记录全叫「内容修改 · 第 1 版」 | 无项目名、无内容标题、无变更摘要，策划无法分辨哪条是哪条 |
| P3 | 「内容版本列表」10 条全叫「未发布内容 · 第 N 份」/「当前内容 · 第 1 版」 | 同上，且看不出属于哪个游戏项目 |
| P4 | 材料选择器丢失策划起的名字 | 材料名为「天空港核心创意」，列表却显示「原始策划材料 · 2026-07-26 · 第 1 份」×7、「已解析策划材料 · … · 第 N 份」×6 |
| P5 | 全站时间是 UTC | 列表显示「2026年7月26日 04:56」，东八区实际为 12:56；运行监控时间窗直接显示裸 ISO `2026-07-26T03:54:00.519Z` |
| P6 | 内部术语泄漏到策划界面 | 「流程版本 5」（workflow_revision）、「原版本 → 候选版本」这类说法说不出实际改了什么 |
| P7 | 断句 bug | 错误面板显示「请在 **秒**后重试。」——retry-after 数值缺失 |

> 不在本 goal 范围：URL 路径里的 `sha256%3A…`。它是资源的 exact 标识，改成可猜的短 ID 会破坏 exact authority；地址栏不是策划的阅读界面。

## 执行原则

- 全程 TDD：每片先落失败测试再实现。
- 不改存储与契约的 UTC 语义（审计/Artifact 时间戳仍是 UTC ISO）；时区只作用于**展示层**。
- 不新增第二套标题来源：标题由既有权威数据派生（项目名、材料名、Patch 摘要），不另建冗余字段的写路径。
- 每片跑完 focused tests，每片结束跑契约 / ruff / typecheck / build。

## 片 1：内置默认角色策略（P1）

### Task 1：`builtin_role_policy()` 落在产品侧

先写失败测试：`platform_admin` 覆盖产品全部 `(action, resource_kind)` 组合（含 `read metric`/`read trace`/`read log`/`read bench`），且覆盖其余每个角色的全部授权。实现 `gameforge/platform/registry/defaults.py::builtin_role_policy(registry)`，作为**唯一**默认策略来源。

### Task 2：所有部署与夹具改用它

`tests/support/identity.py`、`project_live` 启动器、本地组合根改为调用 `builtin_role_policy()`；删掉各处手写 grants。跑通后管理员在运行监控页能读到系统指标。

## 片 2：展示层时区（P5）

### Task 3：统一时间呈现

先写失败测试锁定：同一 UTC 时刻在列表、详情、时间窗输入框都呈现为东八区，且带明确时区标注。新增 `web/src/features/time.ts` 单一格式化入口（`Asia/Shanghai`），替换所有直接渲染 ISO 串的位置；运行监控时间窗改为可读的本地时间输入。存储与 API 契约不变。

## 片 3：可读标题（P2/P3/P4）

### Task 4：修改草案以「改了什么」为标题（✅）

`PatchWorkspacePage` 的主栏改为 `patchRationaleLabel(patch.rationale)`，版本号与时间退到副行。rationale 是 Patch 的不可变属性，不会随任何改名而过期。

### Task 5：材料可改名，名字始终取自当前权威数据（✅）

**设计更正**：最初把材料名写进 Artifact 的 `meta.display_title` 并加 `ArtifactSummaryV1.display_title` 投影，前提是"材料不可改名"。产品负责人指出改名是正常需求——而项目本来就能改名，于是**任何写进不可变 Artifact 的名字都会过期**。该字段与两处 meta 写入已整体撤销。

改为：
- 新增 `POST /projects/{id}/materials/{material_id}:rename`（强 ETag + expected_revision + 幂等 + 审计），Artifact 的字节与血缘不动；同一请求重试走幂等重放而不是被自己造成的 revision 变化判为过期。
- 项目工作台的材料卡片提供重命名入口。
- AI 提取面板改为读**项目材料列表**取当前名字标注来源；无项目上下文时仍回退到结构化标签。

「内容与规则」页的内容版本列表跨项目，暂无当前项目名来源，保留结构化标签，留待后续接入。

## 片 4：术语与缺值（P6/P7）

### Task 6：内部标识收进技术信息 + 修断句

`流程版本 N` 等内部 revision 移入折叠区；「原版本 → 候选版本」改为说明实际变更；修复 retry-after 数值缺失导致的断句。

## 片 5：真实可用性验收

### Task 7：把这条策划链路固化为 E2E

将本次审计脚本固化为 Playwright 用例：以策划视角跑完整链路，并断言**页面可见文本中不出现** SHA、`artifact:`、`run:`、裸 UTC ISO 串、以及「第 N 版」单独成标题的情况。全量门禁（non-Bench / Bench / Web / E2E / visual / a11y / 契约 / ruff / typecheck / build）全绿后提交。

## 完成定义

Task 1–7 全部完成、以策划视角实跑闭合、无剩余 P0/P1，且门禁全绿，才算该 goal 完成。
