# GameForge 项目优先创作与材料抽取设计

> 日期：2026-07-24  
> 状态：已批准实施（产品负责人在本会话明确要求完整实现）  
> 依赖：PRD、foundations-contracts v0.3、M4 production hardening design  
> 实施计划：`docs/superpowers/plans/2026-07-24-project-first-authoring.md`

## 1. 问题与目标

当前产品能浏览 Spec、创建规则提案、生成 Patch、验证、审批和试玩，但缺少策划实际开始一款新游戏时最先需要的入口：

1. 创建游戏项目；
2. 输入创意或上传策划材料；
3. 先声明材料属于整款游戏、永久玩法、限时活动还是已有内容调整，再让 AI 从材料中提取实体与关系草案；
4. 在图形界面中增删修改；
5. 确认、验证、审批并发布首个内容版本；
6. 在项目上下文中继续生成规则、内容和试玩。

本设计补齐这条链路，并把“项目”设为产品信息架构的上层资源。它不替换既有 Artifact、Run、Patch、ApprovalItem、Ref 与 EvidenceSet，而是为它们提供策划可理解的项目上下文。

## 2. 不变量

1. 项目不是 IR Entity。`GameProjectV1` 是平台资源，项目内容仍是不可变 `ir_snapshot` Artifact。
2. 编辑不原地改 Snapshot。任何被确认的图编辑都编译为 typed Patch；发布只通过既有 validate → submit → decide → apply 链路移动项目 content ref。
3. AI 只提议。实体/关系输出必须先通过结构校验、标识同一化、Patch dry-run 和确定性 gate；存在无法自动合并的冲突时必须由人处理。
4. 原材料和规范化文本都不可变并有血缘。二进制原件不冒充 UTF-8；派生文本明确记录 parser、输入/输出 hash 和父 Artifact。
5. `spine` 保持 LLM-free；材料解析、标识规范化、图 diff/校验是确定性主干，模型调用仍只发生在 `agents`。
6. 普通 maker-checker 规则不变。只有持有冻结 RolePolicy 中 `approval.self_decide` 权限的人类 `platform_admin` 可以审批自己提议的 subject；决定与 apply 仍完整审计、重新授权并绑定精确 revision/digest。
7. 旧 Artifact/Run/审批/项目外页面必须继续可读、可回放；新增字段使用向后兼容默认值。
8. 内容范围是抽取 authority，不是展示标签。当前生成链路只有一套 `generation@7` 语义，不保留旧 prompt、旧类型别名或双写分支。
9. 限时内容到期只从指定时刻的 active-content 投影中隐藏；原始 Snapshot、Patch、审批、Run 与审计记录不可删除或原地改写。

## 3. 用户旅程

### 3.1 首次创作

```
项目列表
  → 创建项目（名称、代号、简介、类型、领域）
  → 添加材料（直接粘贴或上传）
  → 选择内容范围（自动判断 / 整个游戏 / 永久玩法 / 限时活动 / 已有内容调整）
  → AI 提取实体与关系
  → 同一化报告（自动合并 / 待确认冲突）
  → 图形编辑器（内容、关系、属性、来源）
  → 创建候选 Patch
  → 确定性验证
  → 提交审批
  → checker 审批；platform_admin 可按显式策略自审
  → apply，项目 content/head 产生 revision 1
```

项目创建时生成一个不可变的空 bootstrap `ir_snapshot`，仅作为首个 Patch 的 exact base；它不占用项目正式 `content/head`。因此“首个内容版本”仍发生在用户确认后的第一次 apply。

### 3.2 发布后继续创作

项目首页从当前 exact content ref 提供三个项目化入口：

- 规则：以本项目材料为 source、以本项目 constraint ref 为目标创建约束提案；
- 内容：以项目当前 content snapshot 为 base 创建 generation Patch；
- 试玩：以项目当前 content、constraint、config/task-suite 绑定进入既有 Playtest Run。

入口只负责预填 exact authority；底层仍使用现有版本化 API 与页面，不复制第二套规则、生成或试玩引擎。

## 4. 平台资源契约

### 4.1 `GameProjectV1`

```text
project_schema_version = "game-project@1"
project_id             = "project:<uuid>"
project_key            = 用户可读、租户内唯一的 kebab-case 代号
display_name           = 1..256
description            = 0..4096
genre                   = 0..128
status                  = draft | active | archived
domain_scope            = DomainScope
bootstrap_snapshot_artifact_id
content_ref_name        = "projects/<project_id>/content/head"
constraint_ref_name     = "projects/<project_id>/constraints/head"
current_content_ref     = RefValue | null
current_constraint_ref  = RefValue | null
latest_extraction_id    = string | null
latest_patch_artifact_id= string | null  # 最近一次人类确认的项目内容草案
latest_approval_id      = string | null  # 上述人类草案的精确 ApprovalItem
created_by / created_at / updated_at
revision                = monotonic positive integer
```

`project_key` 只用于人类导航，不进入 Artifact identity。重命名项目不改变内容快照。

`latest_patch_artifact_id` / `latest_approval_id` 不是“最近一次 Agent 输出”的快捷指针，
只在用户确认可编辑图谱并创建 human-produced Patch 后更新。Agent 生成的候选 Patch、预览
Snapshot 与可选 ApprovalItem 始终由 `ProjectExtractionV1` 按 exact Run authority 投影，避免
尚未由策划确认的模型输出冒充项目最新人工草案。

### 4.2 `ProjectMaterialV1`

```text
material_schema_version       = "project-material@1"
material_id                   = "material:<uuid>"
project_id
display_name
media_type
source_format                 = plain_text | markdown | html | feishu_blocks_json |
                                docx | xlsx | csv
original_source_artifact_id   = source_raw
rendered_source_artifact_id   = source_rendered
parser_id / parser_version
parse_status                  = ready | rejected
parse_warnings[]
byte_size / text_char_count
created_by / created_at
status                        = active | archived
revision
```

支持面向飞书的真实交换格式：

- 飞书文档复制出的纯文本 / Markdown / HTML；
- 飞书开放平台 document block JSON；
- 飞书文档导出的 DOCX；
- 飞书表格导出的 XLSX / CSV。

解析器只做确定性文本化，不自行解释游戏语义。压缩包格式防 zip bomb、路径穿越、外部关系和公式执行；HTML 删除 script/style 并只提取文本；XLSX 读取 shared strings、inline strings 与单元格值，不执行公式。

### 4.3 `ProjectExtractionV1`

```text
extraction_schema_version = "project-extraction@1"
extraction_id
project_id
material_ids[]
source_artifact_ids[]      # exact rendered sources
base_snapshot_artifact_id
planning_scope             = auto | game_foundation | permanent_feature |
                             limited_event | live_update
run_id
status                     = queued | running | needs_resolution | ready | failed
patch_artifact_id          = null | patch
preview_snapshot_artifact_id = null | ir_snapshot
approval_id                = null | ApprovalItem
normalization_summary      = counts + policy ref
validation_issues[]        = deterministic finding 的策划可读投影
created_by / created_at / updated_at
revision
```

Run 仍是执行真相；该资源只把 Run/Artifact 映射为项目旅程状态，不能伪造 terminal outcome。

`planning_scope` 在创建 Run 时写入不可变目标文本。`auto` 允许模型根据材料判断，其余值是用户给出的精确范围约束。`needs_resolution` 表示模型已经生成结构合法且可编辑的 Patch/preview，但确定性检查器或经济仿真仍发现问题；它不是“读取失败”。底层 Run 的 gate-rejected 结果仍保持不变，页面只把 Finding 转成标题、受影响内容和处理建议。解析失败、截断、非法类型或不存在可编辑 preview 的情况仍为 `failed`。

## 5. 材料与来源治理

### 5.1 原件

- Artifact kind：`source_raw`
- payload schema：`project-material-original@1`
- bytes：上传原始字节，逐字节保存
- source kind：`planning_document`
- trust：`reviewed_external`
- purpose：仅 `context`
- lineage：空

### 5.2 规范化文本

- Artifact kind：`source_rendered`
- payload schema：`project-material-rendered@1`
- bytes：UTF-8 canonical text
- source kind：`tool_output`
- trust：继承原件的最保守级别
- purpose：`context` / `tool_output`
- lineage：精确包含原件 Artifact ID
- transformation：`parser_id@version`、input hash、output hash

空文本、损坏压缩包、超限文件、未知格式或非确定性解析全部 fail closed。原件可以保留审计，但没有 `ready` rendered source 时不得进入模型 prompt。

## 6. AI 抽取与确定性同一化

### 6.1 生成输入

`generation-propose-request@1` 与 `generation-propose@1` 向后兼容增加：

```text
source_artifact_ids: tuple[ArtifactId, ...] = ()
```

每个 source 必须是可读的 `source_raw`/`source_rendered`、带合法 Provenance、purpose 允许 `context`，并处于请求者项目领域权限内。Run payload、prompt context、Patch/preview lineage 与 publication parent-role 都绑定这些 exact source IDs。

项目首次抽取以项目 bootstrap snapshot 为 base、正式 `content/head` 为 target；goal 明确要求从材料建立完整实体/关系草案，并携带 exact `planning_scope`。当前 generation agent 输出七类 typed op，随后进入确定性同一化器和 checker/simulation gate。

范围语义如下：

- `game_foundation`：整款游戏及核心常驻系统；缺省内容视为永久，不生成到期逻辑；
- `permanent_feature`：加入现有游戏的永久玩法或永久内容；必须能在活动关闭后独立成立；
- `limited_event`：一个活动由唯一 `EVENT` 实体以 `scope_role=owner` 拥有，活动专属实体以 `scope_role=member` 显式写入 owner、作用域和可用阶段；
- `live_update`：修改已有 authority，材料中未出现的旧事实不能被当作删除指令；
- `auto`：模型先按材料证据选择上述语义，但仍受同一闭集类型和确定性检查约束。

### 6.2 Canonical identity policy

冻结 `identity-normalization@1`：

1. 字符串先 Unicode NFKC，再 casefold；
2. `. _ - /` 与任意空白均视为同一分隔符，连续分隔符合并为 `_`；
3. Entity ID 保留 `:` 命名空间边界；缺失命名空间时按 NodeType 补齐小写前缀；
4. 属性路径的每个 lexical segment 使用同一规则，并把 `air.quality`、`air_quality` 规范到同一 key `air_quality`；
5. 同一 canonical ID 只有在 NodeType 相同才可合并；类型不同是 blocking conflict；
6. 同一 canonical attr 且值 canonical-equal 时自动合并；值不同时保留双方 evidence 并产生 blocking conflict，不静默覆盖；
7. Relation endpoint 通过完整 alias map 改写到 canonical Entity ID；悬空 endpoint 是 blocking conflict；
8. Relation ID 与属性使用同一 lexical policy；重复 relation 只有 type/src/dst 一致时可合并；
9. 非词法语义别名（如“体力”与“行动点”）只能由 AI 提议，必须由人确认，确定性层不自行等同。

同一化结果包含：canonical ops、alias groups、auto-merge records、blocking conflicts 和 source/op evidence。输出排序固定，输入 op 顺序变化不得改变 canonical 结果。

### 6.3 准确度边界

产品不以模型 confidence 冒充准确率。每次抽取显示四类可核验指标：

- 结构有效率：typed op / Entity / Relation schema 通过比例；
- 引用闭合率：关系 endpoint 可解析比例；
- 自动同一化：alias group 与等值合并数量；
- 待人工确认：冲突与语义别名数量。

候选只有在结构合法、引用闭合、无 blocking conflict 且 deterministic gate 通过时显示“可进入发布”；否则仍可编辑，但不能直接发布。

### 6.4 限时活动生命周期

限时活动不等于“到期删除数据”。`EVENT.attrs` 使用 typed `event-availability@1`：

- 已知上线时间时记录 `start_at`、`gameplay_end_at`、`reward_claim_end_at`、IANA 时区和 `hide_from_active_content`；
- 只有“持续 14 天”等相对信息时记录 `duration_days` 与领奖宽限期，不臆造上线日期；发布前由 `unbound_event_schedule` 要求策划补齐绝对档期；
- 活动根写入 `scope_kind=event`、`scope_role=owner`；活动专属内容写入 `scope_kind=event`、`scope_role=member`、`scope_owner_id=<EVENT>`，并可通过 `CONTAINS`、`HAS_STEP`、`REWARDS`、`GRANTS`、`APPLIES_EFFECT` 组成的有向归属路径纳入唯一活动；嵌套的 `EVENT` 玩法模块仍是 member，不会被误判成第二个活动根；
- 仅在活动结束后仍需领奖的内容写入 `availability_phase=reward_claim`；
- 永久内容不得通过 `REQUIRES` / `GATED_BY` 依赖限时内容，避免活动下线后破坏常驻主线。

确定性 active-content 投影按显式时刻得到 `scheduled → active → reward_claim → expired` 四阶段：活动期显示全部活动内容；领奖期只保留活动入口及领奖阶段内容；到期后活动与专属内容从运行视图隐藏。投影不修改 source Snapshot，因此复盘旧版本、回放 Run、查看玩家历史和审计仍有完整依据。相对但未绑定档期、非法窗口或缺 owner 的内容 fail closed，不进入当前运行视图。

## 7. 图形编辑器

项目编辑页采用“画布 + 清单 + 属性检查器”，而不是要求用户输入 IR JSON：

- 画布显示实体、关系、中文类型名和业务名称；技术 ID 默认弱化，技术详情可展开；
- “新增内容”选择实体类型，填写名称后自动给出可编辑 ID；
- “新增关系”通过起点、关系类型、终点创建；
- 检查器编辑名称、标签和 key/value 属性，支持删除；
- 属性 key 输入时实时显示 canonical key，发现 alias 时显示合并提示；
- 冲突中心逐条显示来源片段、两个值和三种解决方式（保留左/右/手工值）；
- 所有操作先进入可撤销的客户端 draft，确认时一次性编译为相对 exact base 的 typed Patch；
- 保存/发布前后端都重新运行同一 `identity-normalization@1` 与 schema 校验，客户端结果不作为 authority。

编辑器提供列表/键盘等价操作，画布不是唯一交互路径。

## 8. 发布与后续能力

### 8.1 首版发布

编辑确认后，项目 API 调用现有 `HumanPatchDraftRequestV1` 创建 immutable Patch/preview/ApprovalItem，并把最新 artifact/approval 映射回项目。之后沿用既有：

1. `patch:validate`；
2. `submit-for-approval`；
3. `approval decision`；
4. `patch:apply` 移动 `projects/<id>/content/head`。

项目详情按 ref authority 动态判断 `draft`/`active`，不相信客户端“已发布”标志。

### 8.2 项目化规则、内容与试玩

所有后续入口携带 `project_id` 作为导航/筛选上下文，同时绑定：

- 当前 content artifact/ref/revision；
- 当前 constraint artifact/ref/revision（如有）；
- 当前项目材料 rendered source IDs；
- 项目 domain scope。

Run 与 Artifact identity 不依赖可变项目名称。若 ref 在用户打开表单后变化，创建请求按 exact binding 冲突并要求刷新，不隐式 rebase。

## 9. RBAC 与管理员

新增 Role：`platform_admin`。

其角色策略不是代码中的 `if admin: allow`，而是当前 `RolePolicy` 中的完整 grants：

- identity、project、material、spec、constraint、generation、review、playtest、patch、approval、rollback、observability、cost、eval 的读写/执行权限；
- `approval.self_decide`：允许提议者本人作出决定；
- `approval.route_override`：允许跳过 assignee / route-role 匹配，但仍必须持有每条
  requirement 的 `required_permission`；
- 其余 approval、产品资源与全 domain grants。

首个人类 bootstrap 获得 `platform_admin + identity_admin + tooling`。普通管理员升级通过现有身份管理命令授予 `platform_admin`。

maker-checker 判断：

```text
if decision.actor == item.proposer:
    require human active principal
    require frozen item RolePolicy authorizes approval.self_decide
    require current role assignment still authorizes approval.self_decide
for each requirement selected by the decision:
    if frozen/current RolePolicy authorizes approval.route_override:
        skip assignee and route-role matching only
    else:
        require existing assignee and route-role matching
    always require requirement.required_permission
append a distinct approval.privileged_self_* audit action for a self decision
```

`approval.self_decide` 与 `approval.route_override` 互不蕴含；管理员要在自己不属于原审批路由时
完成自审，冻结与当前 RolePolicy 必须同时授权两者。route override 只覆盖人员分派和路由角色，
绝不覆盖 requirement 的 `required_permission`。自审也不豁免 distinct-voter（多 requirement
时仍按策略）、validation evidence、submit、apply reauthorization、If-Match、digest/ref CAS
或审计。

## 10. API 表面

新增资源 API（全部 `/api/v1`）：

- `POST /projects`
- `GET /projects`
- `GET /projects/{project_id}`
- `PATCH /projects/{project_id}`
- `POST /projects/{project_id}:archive`
- `POST /projects/{project_id}/materials:text`
- `POST /projects/{project_id}/materials:upload`
- `GET /projects/{project_id}/materials`
- `GET /projects/{project_id}/materials/{material_id}`
- `POST /projects/{project_id}/materials/{material_id}:archive`
- `POST /projects/{project_id}/extractions`
- `GET /projects/{project_id}/extractions/{extraction_id}`
- `POST /projects/{project_id}/content-drafts`

上传接口 body 是单文件原始 bytes，文件名、media type、source format 走受限 header；避免 base64 膨胀并不引入 multipart parser。所有 mutation 需要 `Idempotency-Key`；更新/归档还需要强 `If-Match`。

项目 page 使用稳定 cursor/read snapshot 分页；响应返回 ETag、X-Resource-Revision 和 private/no-cache。

## 11. 持久化

迁移 `0015_project_authoring` 新增：

- `game_projects`
- `project_materials`
- `project_extractions`

表只存平台映射与 monotonic revision；大内容仍在 ObjectStore，权威内容仍在 Artifact/Ref/Run/Approval 表。外键使用 RESTRICT；材料 archive 不删除 Artifact。项目 key 唯一；列表索引按 status/updated_at/project_id 稳定排序。

## 12. 验收

### 12.1 确定性与契约

- 属性测试证明 `air.quality`、`air_quality`、`AIR-QUALITY` 与全角变体同一化一致；
- op 排列、JSON key 排列不影响 canonical result；
- 类型冲突、值冲突、悬空关系 fail closed 且 evidence 完整；
- DOCX/XLSX/Feishu blocks fixtures 解析稳定；恶意 zip/HTML/公式不执行；
- 项目创建不移动正式 content ref；第一次 apply 才产生 revision 1；
- 项目表从 ref/Artifact/Run authority 投影，不可伪造状态。
- 限时活动窗口顺序、唯一 owner、`CONTAINS` 成员关系和永久到限时依赖由确定性检查器验证；
- 同一不可变 Snapshot 在活动中、领奖期和到期后产生稳定 active-content 投影，到期投影为空但源 Snapshot 字节不变。

### 12.2 权限

- content_designer 可建项目、上传材料、提议和编辑，但不能审批自己；
- approver 可按 route 决定；
- platform_admin 拥有全部产品权限且可自审；撤销其 role 后旧页面会立即拒绝决定/apply；
- 自审仍要求 passed evidence 和 exact current revision。

### 12.3 产品旅程

真实 API/worker/ObjectStore/SQLite、无产品 API mock：

1. 创建 fresh project；
2. 上传含 `air.quality`/`air_quality` 的 Feishu blocks + DOCX/XLSX fixtures；
3. Agent Run 产出实体/关系候选；
4. 分别选择整款游戏与限时活动范围，UI 显示自动合并、生命周期问题与冲突，用户完成增删改；
5. 生成 typed Patch、验证、提交、审批、apply；
6. 项目 content ref revision=1，知识图谱可读；
7. 从项目继续创建 constraint proposal、generation 和 playtest 输入；
8. admin 单身份完成 propose→approve→apply，审计明确标出 privileged self decision。

### 12.4 视觉与无障碍

- 360/768/1280/1440 宽度；浅色/深色；空/加载/错误/冲突/成功状态；
- 画布所有操作有键盘/列表等价入口；focus 可见；表单错误与冲突由文本说明；
- 小白路径默认不出现裸 SHA、Artifact ID、schema version；技术详情仍可复制和审计。

## 13. 延后但契约已定

本增量实现上述接口的完整本地生产路径。远程飞书 OAuth/tenant connector、Google Docs connector 和云对象存储适配器属于 M4e；它们复用这里已定的 material/provenance/parser 接口，不改变项目、材料、抽取或发布契约。
