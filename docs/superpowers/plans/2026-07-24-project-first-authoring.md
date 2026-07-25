# Project-first authoring implementation plan

> Goal：完整走通新建游戏项目 → 材料 → AI 实体/关系草案 → 图形编辑 → 首版发布 → 规则/内容/试玩，并补齐 platform_admin 全权限与策略化自审。

## 执行原则

- 全程 TDD；每个 task 先落失败测试，再实现。
- 不新增第二套 Snapshot/Patch/Approval authority。
- 解析/同一化在 `spine`，LLM 在 `agents`，平台编排在 `platform`，适配器在 `runtime/apps`。
- 保留当前 dirty worktree 中既有 Web UX 变更，增量修改、避免覆盖。
- 每个 task 完成后运行 focused tests；每个 phase 完成后运行契约、ruff、typecheck/build。

## Phase A — 冻结契约与确定性主干

### Task 1：项目/材料/抽取 API contracts

先写 `tests/contracts/m4d/test_project_authoring.py` 与 OpenAPI 失败测试。新增 `gameforge/contracts/projects.py`，覆盖 GameProject、Material、Extraction、requests/views/pages；抽取契约包含 exact `planning_scope`、`needs_resolution` 和策划可读 `validation_issues`；为 generation request/payload 增加 `source_artifact_ids=()`；Role 增加 `platform_admin`；冻结 ETag、idempotency、文件 header/size/format bounds。

### Task 2：identity-normalization@1

先写 examples + Hypothesis：Unicode、separator、op order、key order、alias merge、冲突、endpoint rewrite，并与朴素 canonical group partition 对拍。实现 `gameforge/spine/identity_normalization.py`，输出 canonical ops、aliases、merges、conflicts、metrics；无冲突时必须可直接 `apply_patch`。

### Task 3：材料解析器

先写 plain/markdown/html/Feishu block JSON/CSV/DOCX/XLSX golden fixtures，以及 zip bomb、zip slip、损坏 XML、外部关系、公式、超限测试。实现 `gameforge/spine/ingestion/` registry + parsers，输出 canonical UTF-8 text 与 warnings。

## Phase B — 持久化、服务与 API

### Task 4：0015 migration + repository

测试 upgrade/downgrade、约束/索引、round-trip、CAS、stable pagination、archive、并发冲突。实现三张 project 表、SQLAlchemy rows、transaction-bound `SqlProjectRepository`，加入 UoW capability。

### Task 5：项目创建与读取

测试创建项目同时发布唯一空 bootstrap Artifact、content ref 保持 null，以及 RBAC、idempotency、审计、ETag、list/detail/archive。实现 `platform/projects/service.py`、API port/router 和 local composition。

### Task 6：材料写入与 provenance

测试原件逐字节、rendered text、双 Artifact lineage/provenance、重复请求重放，以及无权限/未知格式/解析失败/超限。实现 planning_document original + tool_output rendered publication、text/raw upload、read/archive。

### Task 7：材料驱动 generation

测试 admission 校验 exact source kind/provenance/domain；Run payload、prompt context、lineage policy、handler bridge 绑定 source IDs 与 `planning_scope`；normalizer 在 gate 前执行且冲突进入 deterministic evidence。实现 generation source field 全链路、bounded material context、normalizer 和 project extraction create/read；可编辑 proposal 被 deterministic gate 拒绝时投影为 `needs_resolution`，真实解析/输出失败仍为 `failed`。

### Task 8：项目 content draft bridge

测试 edited graph → canonical diff → HumanPatchDraftRequest → exact preview/ApprovalItem，以及 base/ref 漂移、blocking conflict、非法图和幂等。项目 draft endpoint 只接受 typed entity/relation DTO；服务端重新同一化、构建 typed ops、调用现有 workflow authority并更新项目映射。

## Phase C — platform_admin

### Task 9：全权限角色与 bootstrap

测试 bootstrap 精确获得 platform_admin/identity_admin/tooling；platform_admin 在所有产品资源/action 上允许，普通角色保持最小权限。实现角色合同、bootstrap ID/result/service/CLI、builtin/demo RolePolicy grants 和中文角色名。

### Task 10：显式自审权限

测试 proposer 自审默认 Forbidden；冻结和当前 RolePolicy 同时授权 `approval.self_decide` 时允许；撤权、非 human、scope 不覆盖、evidence 未通过、revision stale 均拒绝；审计标记 privileged self decision。修改 maker-checker evaluator、vote revalidation 和 apply reauthorization，不硬编码登录名。

## Phase D — Web 产品旅程

### Task 11：项目导航和列表/创建

测试路由、breadcrumb、empty/loading/error、创建表单与权限提示。实现 `/projects` 登录默认首页、项目卡片和“游戏项目”主导航；旧八页保留为工作台入口。

### Task 12：材料工作台

测试粘贴、拖放/文件选择、支持格式、解析警告、archive、重试。实现项目详情 stepper、上传状态和格式说明；默认不显示裸 Artifact SHA。

### Task 13：AI 抽取与进度

测试内容范围选择 → resolve execution option → start extraction → SSE/poll terminal → load candidate，以及 aliases/metrics/conflicts/validation issues 和错误恢复。实现“从材料生成内容草案”、复用 RunProgress，显示内容范围、来源、自动合并、待处理确定性问题与待确认冲突。

### Task 14：可编辑知识图谱

测试 entity/relation CRUD、属性 key canonical preview、conflict resolution、undo、键盘路径。基于现有 Cytoscape 视觉语言实现 `ProjectGraphEditor`：画布 + 列表 + inspector，draft state 转 typed graph DTO，技术详情折叠。

### Task 15：首版发布与后续入口

测试创建 human Patch 后进入 validate/submit/approve/apply，项目 ref revision=1 后 active；项目规则/内容/试玩链接预填 exact bindings。实现发布 checklist、Patch/Approval 深链、authority 自动刷新和后续入口。

## Phase E — Schema、运行环境与验收

### Task 16：OpenAPI/client/docs

更新 canonical OpenAPI、生成 TS contracts、README 页面/operation 数与使用说明，更新 AGENTS/CLAUDE 进度锚点。

### Task 17：真实旅程与视觉门禁（✅）

Python integration 使用 fresh SQLite/ObjectStore + API/worker；Playwright 已闭合项目创建、Feishu material、AI candidate、可视化编辑、首版发布、规则生成/人工修订/管理员自审发布、继续生成 NPC、第二版发布、派生试玩任务与真实 Playtest Run。产品 API 未 mock；无任务素材会诚实输出“仍有试玩任务未完成”并用单步诊断预算结束，不伪造通过。既有视觉、响应式、dark/light、a11y 与 keyboard 门禁继续由 M4d 回归覆盖。

### Task 18：全仓验证与本地完整服务（✅）

迁移 upgrade/downgrade 与实际 `0014 → 0015` 均通过；non-Bench 5040 passed / 1 skipped、Bench 813 passed、7 import contracts、Ruff 与 focused generation 27/27 全绿。Web 为 79 files / 754 tests，typecheck、build、format 与 4 份 API contracts 全绿；真实 E2E 6/6、visual 70/70、a11y 21/21。完整本地库已在停服后备份为 `gameforge-before-project-first-20260724-205204.db`，随后迁移并保留 exact `roles@local-full-project-first-1`；Local Administrator 原子获得 `platform_admin` 且写入 audit，普通 maker 自审仍 fail closed。API、worker、HTTPS Vite 已重启，`/readyz` 七项检查与 0015 三张项目表均通过；全程未操作用户浏览器。

## Phase F — 抽取质量与内容生命周期

### Task 19：当前生成语义与限时活动（✅）

废弃旧 prompt/旧类型兼容分支，冻结唯一 `generation@7` / `generation-graph@7`；单次模型输出预算提高到 32,000 tokens，材料按 exact UTF-8 边界分块并在 provider 截断时递归重试。闭集类型提示明确 QUEST/QUEST_STEP、地点、奖励、经济关系、限时活动的 owner/member 层级及 availability exact keys，确定性 Finding 以 `needs_resolution` 在项目页展示。

新增 `game_foundation | permanent_feature | limited_event | live_update | auto` 内容范围；为限时活动冻结 `event-availability@1`、唯一 owner、活动成员关系、领奖期和 `hide_from_active_content`。GraphChecker 集成 lifecycle checker；active-content 投影按显式时刻隐藏过期活动而不改写不可变 Snapshot。

真实《梦中未寄出的信》限时活动材料以 RECORD 跑通 `generation@7` / `generation-graph@7`：生成并规范化 139 项操作，唯一确定性待办为材料没有绝对上线时间的 `unbound_event_schedule`；问题卡直接显示中文实体名，并给出开始时间、玩法结束、领奖截止与时区的补全提示。“设置活动档期”动作会自动选中对应活动、定位档期表单并聚焦开始时间，不要求策划在关系图中寻找入口。Python 分组全仓为 1759 passed、2208 passed、1876 passed / 1 skipped，根依赖门禁 27 passed；最终变更后专项 282 passed、项目/API 11 passed、Spine 244 passed。Web 79 files / 758 tests、typecheck、build、format、4 份 API contracts，以及 Ruff、schema、`git diff --check` 全绿；完整 API、worker、HTTPS Vite 已重启，`/readyz` 七项检查通过。

## Phase G — 多材料、多提案与策划撤回

### Task 20：提案历史、批量材料与放弃提案（✅）

同一游戏项目可长期保留多份策划材料与多次 AI 提案；文件选择器支持一次选择多份文件，一次提案可显式组合 1–64 份材料，超过上限会在启动前给出策划可读提示。项目页新增“提案记录”，展示每次提案的材料数、内容范围、创建时间、运行结果和放弃状态，不再只暴露最后一次提取。

“放弃提案”是独立于 Run 结果的策划处置：只允许原提案 maker 或 `platform_admin` 对终态提案执行，要求强 ETag、expected revision、幂等键和原因；重复请求只产生一次审计。放弃后项目的 current proposal 指针回退到最近一份未放弃提案，材料、Run、Artifact、确定性检查证据与独立发布草案全部保留。未发布提案的放弃与已发布内容的治理回滚严格分离；页面不会把已放弃提案继续当作可编辑/发布入口，也会明确说明独立发布草案仍是审计记录。

最终门禁为 non-Bench 5061 passed / 1 skipped、Bench 813 passed、Web 79 files / 760 tests、7 import contracts、Ruff、typecheck、build、format、4 份 API contracts、schema 与 `git diff --check` 全绿；项目/API 专项 82 passed，最后响应契约专项 61 passed。完整 API、worker、HTTPS Vite 已重启，`/readyz` 七项检查及既有 6 条提取记录的新契约读取均通过；全程未操作用户浏览器。

## Phase H — 提交前全量回归

### Task 21：真实端到端回归与三处缺陷修复（✅）

Task 20 的门禁没有覆盖 Playwright；本次提交前补跑全量回归，暴露并修复三处真实缺陷，全部先写失败测试再实现。

1. **bootstrap 留存策略校验与旧组合 fixture 不一致**：`bootstrap` 要求留存 RolePolicy 给 `identity_admin` 全局 metric 读（由 `test_bootstrap_rejects_composite_admin_that_cannot_read_global_system_metrics` 锁定），但 `tests/e2e/m4c/test_composition.py` 与 `tests/apps/test_identity_cli.py` 的策略仍是旧形状，导致 13 个 Python 用例 fail closed。按 canonical fixture 补齐该 grant。
2. **新增 `builtin.checker@2` 打穿两处「只有一个内置方案」的前端假设**：Review 启动卡把同一 kind 的内置方案压成同一个中文名，v1/v2 变成两个同名复选框（键盘与读屏无法区分），现按 `PatchDetailPage` 既有写法带上版本号；Patch 详情页的推荐预选是「候选恰好一个才预选」，多版本后一个都不选，而「开始验证」仍可点击，策划会得到一次注定失败的验证——改为「同一 profile_id 有多个版本时预选最新版本」，多个不同 profile_id 时仍不猜测。REPLAY 必须与录制身份一致，因此 Journey A 的修复回放显式选回录制时的 checker v1。
3. **约束提案页读偏斜死锁**：proposal、subject binding 与 ApprovalView 是三次独立读取，验证在读取中途转为终态时三者身份不一致，页面把这种瞬时偏斜当成永久致命错误并同时停止轮询，永远停在“审批绑定不一致”。改为有界重读（3 次）后再判定，仍不一致才 fail closed；exact 身份守卫本身不放宽。

回归门禁为 non-Bench 5067 passed / 1 skipped、Bench 813 passed、Web 79 files / 765 tests、真实 E2E 6/6、visual 70/70、a11y 21/21、7 import contracts、Ruff、typecheck、build、format、4 份 API contracts 与 `git diff --check` 全绿。

## 完成定义

只有 Task 1–21 全部完成、真实链路闭合、服务可供用户自行体验且无剩余 P0/P1，才把该增量标为 ✅ 并结束 goal。
