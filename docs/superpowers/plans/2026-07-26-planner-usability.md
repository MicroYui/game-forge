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

### Task 6：内部标识收进技术信息 + 修断句（✅）

「修改与版本」的「流程状态」不再拖着 `· 流程版本 N`（乐观并发的簿记，说不出任何事），它进技术信息；「原版本 → 候选版本」——两个策划读不懂的 SHA——换成**「改了什么」**，按 Patch 的操作数出 `新增 N 项 · 修改 M 项`。其余带「流程版本」的位置（审批页、检查详情）本来就在折叠区里。retry-after 断句在层4 期间已修（`sanitizeProblem` 让缺失值保持 `undefined`，而面板判的是 `!== null`）。

**根因修掉一个真 bug**：两张启动卡的「高级设置」是 `open={条件 ? true : undefined}` 这种受控/非受控混血。React 只在 prop 变化时写属性，所以策划自己点过一次之后，那次选择要么被静默覆盖、要么永远卡住——面板关上就再也回不来，里面的字段这一轮都够不着。抽成 `Disclosure`：需要注意时自己打开，人一旦动手就听人的。同时它原来会在目录还没加载完就先弹开、数据到了又自己关上，现在只在**确实知道表单缺字段**时才打开。

## 片 5：真实可用性验收

### Task 7：把这条策划链路固化为 E2E（✅）

`e2e/support/planner-readable.ts` 的 `expectPlannerReadable()` 在既有真实链路的 7 个停靠点断言：**技术折叠区之外**不出现 SHA-256、`artifact:`/`sha256:`/`run:`/`extraction:` 等不透明标识、裸 UTC 时间戳。这些标识本身没问题（都是 exact ref），要求的是它们待在该待的地方，所以断言前先摘掉 `details`。

原计划里的「第 N 版单独成标题」没有做成通用断言：它在列表里才是问题（每行读起来一样），在详情页 H1 上不是；写成正则会误伤 `修改草案 · 第 3 版` 这类合法标题。列表标题的可读性由 `PatchWorkspacePage` 的单测直接锁定（以 rationale 为题、以操作数说明改动）。

## 片 6：项目是空间，不是一次向导（产品负责人 2026-07-26 提出）

原 `/projects/{id}` 是一个 1500 行的五步向导（项目→材料→提取→编辑→发布），首屏就是「添加策划材料」表单。项目因此等同于「一次创作流程」，策划建完项目看不到自己这款游戏现在是什么样。

### Task 8：项目主页改为游戏现状总览（✅）

`/projects/{id}` 改为 `ProjectOverviewPage`：游戏名与一句话创意、当前内容版本与规则版本、**可交互的游戏内容图谱**（`N 个内容 · M 条关系` + 查看完整图谱）、以及「继续创作」的四个入口。空项目不铺表单，给「这个游戏还没有内容」+「从策划材料开始」。

整条创作流程移到 `/projects/{id}/authoring`；「创建并添加材料」按钮直接落到那里，因为按钮承诺的下一步就是加材料。入口链接复用抽出的 `projectLink()`，保证总览与工作台交出的 exact content/constraint 绑定完全一致。

### Task 9：图谱能回答「这个东西连着谁」（✅）

选中一个实体时：它自身放大并填充高亮，`closedNeighborhood()` 内的内容与关系一起点亮，其余淡到 14%，180ms 过渡；`prefers-reduced-motion: reduce` 下过渡为 0（canvas 由 cytoscape 绘制，不吃 CSS 变量，所以在 JS 里读偏好）。

## 片 7：模型可选（产品负责人 2026-07-26 提出）

本地栈跑的是 hermetic 替身传输，AI 提取返回的是写死的固定结果。产品负责人要求「一会儿想用 gpt5.6sol，一会儿想用 opus5」，且「点开的时候自动获取，然后用户可选」。分四层推进。

### 层 1：按模型实际应答的协议分派（✅）

实测：`gpt-5.6-sol` 只在 `/responses` 应答（`/chat/completions` 返回 `unsupported_api_for_model`），`claude-opus-5` 在 `/v1/messages` 与 `/chat/completions` 都应答。新增 `ApiFlavorRoutedTransport`：按模型声明的 surface 分派，未声明或无对应传输一律 fail closed。

### 层 2：多模型目录 + 每模型路由策略 + 真实网关（✅）

网关的 `/v1/models` 自己报告每个模型的 `supported_endpoints`、上下文与输出上限、能力和分级。新增 `GatewayModelV1` + `parse_gateway_models()`/`fetch_gateway_models()` 读取它，**一次读取同时派生**三样东西，因此三者不可能互相漂移：

- `ModelCatalogSnapshotV1`（run 冻结的版本化目录，每个 descriptor 带 `api_flavor`）
- 每个可选模型一份 `RoutingPolicyV1`（规则的 primary 就是该模型，无 fallback）——因为 worker 的 router 永远取规则的 primary，所以「策划选哪个模型」在既有契约里就等于「选哪份路由策略」
- 部署的 `StructuredModelSnapshotManifestV1`（binding 增加 `api_flavor`，worker 据此组装 `ApiFlavorRoutedTransport`）

达不到 Agent 图声明能力（当前为 `reasoning`）的模型不进目录：提供出来只会让策划选完在计划校验处被拒。目录版本按内容复用，模型集合变了才升版；策略版本按 `catalog_version` 分段，跨目录版本永不撞号。

同时按硬规则 7 清掉三处遗留：层 1 那个按 `provider:model` 拆 id 的 dispatch 表（产品目录用的是不可逆 `provider:sha256:…`，那个假设本来就不成立）；`OpenAITransport` 的 openai SDK 实现改为与两个兄弟传输一致的 httpx（否则 `apps` 组合三种传输会撞 import-linter，而它们本来就该同形）；随之失效的 `router -> transport` 定向豁免与那条只测该豁免的用例。

### 层 3：可用模型读端点（✅）

`GET /api/v1/models` 在打开时现读网关，并与保留的路由权威对账：只列出**既在网关上被服务、又有一份该部署保留的路由策略指向它**的模型。缺任一条都不列——列出来只会让策划选完在计划校验处被拒。每条自带 `display_name` / 厂商 / 分级 / 上下文上限，以及那份**确切的路由策略 version+digest**，所以调用方拿到的就是"选它"所需的全部权威，不用再去找。默认模型标 `is_default`；当默认模型恰好被网关下掉时，其余仍可选，但**不预选任何一个**，逼策划显式选一次。没有网关的部署没有可选项，端点 fail closed 为依赖不可用。

### 层 4：启动卡上的模型选择器（✅）

`ModelPicker` 接到「AI 提取实体与关系」和「从策划材料提取规则」两张启动卡。选择被翻译成该模型的路由策略 ref，随请求进入 `ExecutionVersionPlanResolver`：**计划因此绑定到策划选的那份策略**，节点 allowlist 里只有那一个模型，worker 不可能路由到别处。没配网关的部署上选择器整个不渲染（那里本就没有可选项），运行仍走部署默认。

实跑证据：浏览器里选 Claude Opus 5 跑同一份「铁潮港」材料，产出 18 个 op（比 gpt-5.6-sol 的 15 个多出 `EVENT 大潮`），Patch 的 `model_snapshot` 正是 Opus 的 canonical id。

期间修掉三处真缺陷：① `AnthropicMessagesTransport` 不认路由层的 `max_output_tokens`，原样塞进 Messages 请求体导致网关 400——**选 Opus 必然失败**；② `OpenAITransport` 同样不翻译，而这个网关会**接受并忽略**未知字段，于是输出上限静默失效；③ `sanitizeProblem` 让缺失的 `retry_after_s` 保持 `undefined`，`undefined !== null` 使错误面板渲染出「请在  秒后重试。」（P7）。

## 完成定义

Task 1–9 全部完成、以策划视角实跑闭合、无剩余 P0/P1，且门禁全绿，才算该 goal 完成。
