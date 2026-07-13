# GameForge M4 生产化硬化设计（Production Hardening）

> 从可信主干 + Agent + Bench，走到端到端可观测、可血缘回滚、可审批、可运维的**生产级工程成熟度**平台，并交付好看的 Web Console 全页面。

| 字段 | 值 |
|---|---|
| 状态 | **设计最终定稿（M4a–M4e 五节逐节冻结，并完成跨节终审修订）；实现未开始。** 本文件只完成设计审查，任何"✅ 已满足"仅指设计层，实现验收项见 §9，未达成前一律未完成。 |
| 日期 | 2026-07-13 |
| 里程碑 | M4（生产化硬化）；实现切片 M4a→M4b→M4c→M4d→M4e，每片独立 plan/TDD/review |
| 真相源 | PRD `2026-07-03-gameforge-prd.md`（§8/§9/§10/§11/§12/§12A/§16）、地基契约 `2026-07-03-gameforge-foundations-contracts.md` **v0.3**（§1 分层、§5 Artifact/ObjectRef/VersionTuple/血缘审计、§6 Finding/Patch、§7 Model Router/Cassette） |
| 基线分支 | `master`（无 `main`）；提交不带任何 AI 协作者署名 |

---

## 0. 决策摘要与治理注记

M4 把已完成的确定性主干（M0–M1）、有边界 Agent 层（M2）、GameForge-Bench（M3）**生产化硬化**：可观测/成本治理、版本血缘/回滚/审计打磨、RBAC/审批工作流、前端全页面，外加 §12A 平台工程面（DR、元 schema 迁移、并发一致性、内部安全、SLO/容量、部署 CI/CD）。

**四个地基决策（已确认）：**

1. **契约全定 + 本地确定性实现**：全套生产 Protocol（存储/对象存储/追踪/指标/身份/成本…）现在定全；本地/确定性适配器（SQLite、本地 ObjectStore、文件/内存 exporter、进程内注册表、注入式时钟）完整实现；真实云适配器（PostgreSQL/S3/OTLP/Prometheus/Tempo/Loki）接口完整并对临时容器做契约测试。**默认零实网、可回放、CI 全绿**；真实基础设施的部署实现可延后（M4e），契约现在全定。
2. **FastAPI 服务 + 实时 React SPA**：`apps/api`（REST + SSE/WS）+ `apps/worker`（持久执行）；React SPA 实时对接；agent 调用走 cassette 回放保持确定性；e2e = Playwright + API 契约测试。
3. **本地身份库 + 拆分式认证契约**：`IdentityAuthenticator` / `SessionManager` / `ApiKeyAuthenticator` 本地实现；`OidcProvider` 接口现定、实现按 PRD 非目标"不做对外 SSO"保持不做（接口保留不算简化）。actor = 真实身份，强制 proposer≠approver。
4. **一份完整设计 + 分阶段实现 M4a–M4e**：本文档覆盖全部契约与页面；实现分片推进，后片可延后实现，契约不裁剪。

**治理注记（写进首页）：M3 工程实现已完成；真人 QA 由产品负责人明确延后，且不阻塞 M4 的设计与实现推进；`qa.evidence_missing` 在 combined acceptance 中继续如实保留，绝不豁免成"通过"。** 延后的是*实现证据*，不是把失败改写成成功。此条改动了旧路线图"M4 受 M3 真人证据阻塞"的措辞，属产品负责人决定。

**不采用的路线**：微服务拆分（过度设计，项目是内部生产成熟度而非对外部署，违背 YAGNI）；纯库+CLI+静态工件（不满足"审批工作流端到端跑通"与交互式 Console）；LLM 自动裁决审批/成本（违反确定性优先 + 人工兜底）。

---

## 1. 冻结骨架：模块化单体 + 分层落点

**形态 A — 模块化单体（modular monolith）+ 可插拔适配器**：一个部署制品、两个长期部署入口（`apps/api` / `apps/worker`）+ 一个仅由 SolverExecutor 启动的受控子进程入口（`apps/solver_worker`），内部按平台域清晰模块化。日后拆微服务只是把模块搬出去，接口不变。

**分层落点严守地基契约 §1（不可违背）：**

```
contracts   ← 所有包可依赖；自身不依赖任何业务包
runtime     → contracts                         # 底层能力，无业务；CI 禁 runtime→{spine,agents,platform,apps}
spine       → contracts                         # 确定性主干，仅此一项；禁 LLM SDK
env         → contracts
game/aureus → contracts + env
agents      → contracts + spine + env + runtime
platform    → contracts + spine + runtime       # 产品平台域：rbac/approval/audit/workflow/lineage/diff/cost-policy/slo；禁 platform→{agents,game,apps}
apps/api    → 全部业务模块                       # 组合根（可依赖 agents/game/platform/spine/runtime）
apps/worker → 同 apps/api                        # 持久执行入口
apps/solver_worker → 导入 spine 执行 checker      # 受控 solver 子进程入口（见 §7.E）
web         → 仅经 apps/api（不直接 import 业务包）
```

> 关键修正：API/审批/可观测/成本的**组合**属 `apps/*`，不属 `platform`——契约 §1 显式规定 `platform → contracts+spine+runtime`，正是为消除 `platform.api → agents → platform.model_router` 的隐性循环。同理 `runtime` 禁止 import `spine`，故 solver 隔离的**机制**在 `runtime`、**执行 checker 的入口**在 `apps/solver_worker`。

**M4a–M4e 阶段边界（设计一次全定，实现分批）：**

| 片 | 主题 | 现在锁死的契约 |
|---|---|---|
| M4a | 平台核心 + 持久化地基 | Artifact/ObjectRef/VersionTuple producer 强制 + 存储能力 Protocol（Repository/Transaction/UnitOfWork/RefStore/AuditSink/ObjectStore/ObjectGc）+ SQLite/本地实现；命令级原子事务矩阵；字段级 SnapshotDiff + 三方 ConflictSet/rebase + RollbackPolicy；审计硬化（tamper-evident）；身份/RBAC 契约 + maker-checker + 域路由 + ApprovalItem 状态机 + auto-apply 门；持久 Run/Job（RunRecord/RunEvent/RunLease + fencing） |
| M4b | 可观测 + 成本 + 可靠性 | RunCorrelation/TraceContext + Tracer/Span/Exporter + MetricSink + StructuredLogger；持久 CostLedger（分层 BudgetSetSnapshot/ReservationGroup/UsageEntry/PermitGroup）+ 分层路由 + provider 前缀缓存指令 + 完整 `request_hash` 响应缓存；CircuitBreaker；SLODefinition + AlertSink |
| M4c | API 网关 + 执行 | `apps/api`+`apps/worker`；IdentityAuthenticator/SessionManager/ApiKeyAuthenticator/OidcProvider；`/api/v1` REST + RFC 9457 错误；可续传 SSE + 持久 WS 命令；有界 trace/log/metrics/cost 查询；OpenAPI 冻结 + IA/旅程定稿；Journey A/B walking-skeleton 验收 |
| M4d | 前端全页面 | design tokens + 组件系统 + 8 页 IA/交互状态 + API 消费 + KG 可视化；TS 类型从契约生成；Journey A/B 各 happy+failure 的 Playwright E2E |
| M4e | 生产运维 + 真实适配器 | PostgreSQL/S3/OTLP+Tempo/Loki/Prometheus 适配器 + 容器契约测试；tamper-resistant + 外部锚定；DR（BackupCheckpoint + 独立 RecoveryCatalog + 真实计时演练）；元 schema/DSL codec+migrator registry；SolverExecutor 跨平台隔离；来源治理管线；k8s 参考部署 + CI/CD；容量/SLO 预注册 + 压测 |

---

## 2. 贯穿式不变量 + 三条附加约束

**不变量：**
- **依赖方向单向**：M4 新代码落在 `contracts/runtime/spine/platform/agents/apps/web`；`spine` 的 M4 改动仅限确定性的 IR codec/migrator、DSL compiler/migrator 与其测试，且仍**只依赖 `contracts`**，永不 `spine→上层`。CI import-linter + AST 白名单强制（含 no-LLM-SDK、no-langgraph）。
- **确定性/可回放默认**：一切默认零实网；agent 走 cassette 回放；trace/metrics/配额有确定性文件/内存 sink + 注入时钟做 CI 断言；真实基础设施是可选适配器、独立 CI stage。
- **确定性优先不动摇**：对错判定仍归 图/ASP/SMT/仿真；RBAC/审批/可观测/成本都是**治理与呈现**，不改判定权威。
- **接口全定、实现分批**：后片可延后*实现*，绝不砍字段/缩接口。
- **TDD 全程**；提交无 AI 署名；基线 `master`。

**附加约束（正确性/确定性加固）：**
1. **ObjectStore 不参与伪分布式事务**：对象先按内容 SHA-256 不可变写入并校验 → DB UnitOfWork 原子发布引用。允许可回收孤儿对象；**已提交 DB 记录永不指向缺失对象**。GC 仅删"无任何已提交 `Artifact.object_ref` 引用 **且** 过安全窗/保留期"的对象版本，附二次 DB 引用检查与 generation 条件删除。
2. **RunCorrelation/TraceContext 不改依赖方向**：`spine` 不导入 `runtime.observability`；agent/checker/sim/playtest 的 span 由**上层编排边界**包裹；进入确定性产物的只有 contracts 层的 `run_id`/`producer_run_id` 字符串，`Tracer`/`Span`/`TraceContext` 类型不进 spine，也不进 canonical artifact hash。
3. **时钟可注入且分栏**：持久时间戳/deadline 用注入式 **UTC clock**；进程内耗时 + wall-time 配额用注入式 **monotonic clock**；二者不混。REPLAY/测试用冻结时钟或记录时延重演；**回放速度绝不误计为原始调用延迟或成本**。

---

## 3. M4a — 平台核心 + 持久化地基

### 3.1 存储能力 Protocol（ISP 拆分 + facade）

不做万能 KV，拆成能力 Protocol，由 facade 组合；SQLite 完整实现（M4a），PostgreSQL 适配器（M4e）。Artifact/ObjectRef/ArtifactKind 与 VersionTuple producer matrix 直接采用地基契约 v0.3 §5：**发布前校验适用字段，缺失 fail-closed**；历史工件缺证据只报告 `evidence_missing`，不补假默认值。`Artifact.object_ref` 是 ObjectGc live-set 与 Run result publication 的权威引用。

```text
Repository[T]  : get(id)->T? · put(T)(同ID同canonical内容才幂等；同ID异内容→IntegrityViolation) · page(cursor)->Page[T]
RefStore       : get(name)->RefValue? · history(name, cursor)->Page · compare_and_set(name, expected:RefValue?, new_artifact_id)->RefValue|Conflict
                 RefValue { artifact_id, revision }        # 单调 revision，双匹配防 A→B→A（ABA）
AuditSink      : append(record:AuditRecordV2, *, tx)->AuditRecordV2           # 仅追加；subject/correlation/initiated_by 采用地基 v0.3 §5
ObjectBindingRepository: resolve(ref,store_id?)->ObjectBinding · bind_verified(ref,location,expected_revision?) · retire(binding,expected_revision)
ObjectStore    : put_verified(bytes|stream)->StoredObject{ref:ObjectRef,location:ObjectLocation} · open(location) · stat(location) · list_versions(cursor)->Page[ObjectStat] · delete_if_generation(location)
ObjectGc       : plan(cursor,safe_before)->Page[GcCandidate] · collect(candidate)->deleted|retained_referenced|retained_generation_changed|retention_active
                 # 独立策略服务；ObjectStore 只提供原语，业务写路径不持有 GC 能力
Transaction    : 仅由 UnitOfWork.begin() 创建的有生存期句柄；tx.refs / tx.audit / tx.approvals / tx.lineage / tx.object_bindings / tx.runs / tx.cost
                 全部绑定同一 connection/session 与事务快照；禁止直接构造、跨 UoW 混用或隐式嵌套；context 退出后访问 fail-closed
UnitOfWork     : begin()->ContextManager[Transaction]；正常退出 commit，异常 rollback；commit/rollback 后关闭句柄
StorageFacade  : 组合以上；暴露给 platform；集合接口一律 cursor 分页（filter/sort/read-snapshot 绑定，稳定排序）
```

`list_versions` 必须枚举**每个 key 的全部 backend generation/version**，不能只列当前 key；否则 S3 并发上传留下的非当前孤儿版本永远无法进入 GC。ObjectStore 只返回“已验证内容 + 本后端位置”的 StoredObject，不知道该 location 是否已发布；ArtifactRepository/ObjectBindingRepository 在 UoW 内保留或建立 active binding。ObjectGc 的 live set = 全部已提交 Artifact.object_ref + 复制/保留策略要求（含 §7.D RecoveryCatalog 在备份保留期内 pin 的 manifest/数据目标 location）；删除候选是具体 ObjectLocation，删除前二次查 active binding/Artifact/RecoveryCatalog 引用并按 backend_generation 条件删除。

**LocalObjectStore durability**：在目标目录的**同一文件系统**创建临时文件 → 流式计算并复核 SHA-256/size → flush + file `fsync` → 原子 `rename` 到内容寻址路径 → parent-directory `fsync`；已存在同 hash 对象先复核再幂等返回，异内容/大小 fail-closed。崩溃遗留临时文件只由 ObjectGc 在安全窗后清理。

ArtifactRepository 同 ID 重试按 `{lineage_schema_version,kind,version_tuple,lineage,payload_hash,meta}` 与 `ObjectRef.{key,sha256,size_bytes}` 比较：完全相同则返回已存行及当前 active binding；后来上传的同字节 location 不得悄悄改绑定，只能经显式 bind/remap CAS（恢复/迁移时复核并审计）或留给 GC。任一 canonical 内容、父集合、immutable meta 或对象身份差异均 `IntegrityViolation`。

### 3.2 原子提交边界（命令级 UoW 矩阵）

统一不变量：对象 blob **先** `put_verified`，DB UoW 只发布已校验 ObjectRef + active ObjectBinding；所有变更命令带 scoped idempotency key + canonical request hash，并以 workflow/ref/run revision 或 fencing token 做 CAS；任何业务/workflow 状态变化与对应 `audit@2` 同事务。高频 lease heartbeat 只是 operational renewal，不逐次写权威 audit（发 telemetry），但 claim/start/expiry/retry/terminal 必须审计。ObjectStore 永不伪装参与 DB 事务，失败只留下可回收孤儿 location。

| 命令 | 同一 UnitOfWork 内必须全成或全败的写入 | 明确不做 |
|---|---|---|
| subject draft/revision publish | idempotency + `patch@2`/`constraint_proposal`/`rollback_request` Artifact/ObjectRef insert + derived lineage + SubjectHead CAS；Patch 另把纯计算得到的非权威 preview `ir_snapshot` 与请求的条件 adapter `config_export` 作为同一候选集发布，Patch/rollback 在新 ApprovalItem(`draft`) 中写入完整且之后不可替换的 exact target binding，**不移动任何 ref/history**；constraint proposal 初始 binding 唯一允许为 null；若为新 revision，同事务 CAS 旧 current ApprovalItem→`superseded`（active validation Run 同时 request cancel）+ 新 ApprovalItem(workflow_revision=1, supersedes_approval_id) + audit；rollback request 的 current/target Artifact 都是 lineage parent | payload 构造、`apply_patch`、adapter export 与 blob 写入在事务外；不原地改 subject；不继承旧 evidence/decision/auto proof；不把 create 与审批 decision 混为一步；preview 存在不等于已应用 |
| validation start | idempotency + ApprovalItem workflow CAS `draft→validating`（绑 active validation run）+ 下述 Run create 全套写入 + audit | submit 不隐式执行长 validation；同一 subject revision 只允许一个 active validation Run |
| validation completion | 校验 current lease identity/fencing/expiry + run revision；若 ApprovalItem 仍是当前 head/validating/active_run 匹配，则 Patch/rollback 复核其 draft 时已冻结的 exact target binding；constraint proposal 若 compile 形成 candidate，则无论最终 validated 或 validation_failed 都先发布非权威 candidate `constraint_snapshot`，再将 item binding 唯一一次 null→exact CAS；未形成 candidate 则 binding 保持 null且 overall=failed/unproven；插 immutable EvidenceSet/validation\|regression evidence（含与同一 target 精确绑定的 Finding/Review/Playtest supporting evidence）+ `run_result` lineage，存在 target 时 EvidenceSet.binding 与 item binding 逐字段相等；Patch 仅在冻结 deterministic auto-apply policy 的专用 passed outcome 下同批发布 `auto_apply_proof@1` 并写入 item，其余 outcome proof 必须为空；再 CAS `validated\|validation_failed`；Attempt/Run terminal + RunEvent + ledger settle + PermitGroup/BudgetHold release + audit | execution failure/cancel/timeout 不写 target binding；若 subject 已 superseded，走 typed `subject_superseded` cancelled terminal：不发布/挂接旧 EvidenceSet/proof，旧 item 保持 superseded，但 Run/Cost/Event/audit 正常终结；其他错配 fail-closed；不得用另一 snapshot/config 的 Review 或 Playtest 证据凑门禁 |
| validation execution failure/cancel/timeout | 按相应 terminal 路径发布 `run_failure`；仅当 ApprovalItem 仍是 current head 且 `validating`/active_run 匹配时，CAS `validating→draft`、清 active run 并记录 last_validation_failure_artifact_id；若已 `superseded`，保持 superseded，只终结 Run/Cost/Event/audit；其他错配 fail-closed | 不让 current item 卡在 validating；不复活 superseded item；不把 cancel/timeout/dependency/worker 崩溃误写成确定性 `validation_failed` |
| submit for approval | idempotency + 重验 immutable EvidenceSet、route/policy 与 ApprovalItem 已有 target/ref binding 逐字段相等 + workflow CAS `validated→pending_approval\|auto_apply_eligible` + audit | 不写入、替换或“补全” target binding，不碰 subject Artifact/ref/lineage；auto 分支仅 Patch |
| approval decision | idempotency + append immutable ApprovalDecision + workflow-revision CAS；部分 requirement 获批时 `pending_approval→pending_approval`，全部满足才→`approved`，任一有效 reject/request_changes 则→对应终态 + audit | decision 不覆盖 subject/evidence/其他 requirement；同 actor 不重复计数；maker-checker 与全域权限在平台守卫重验 |
| patch apply | UoW 内重做 platform guard + item CAS `approved\|auto_apply_eligible→applied`；auto 分支重验 subject digest/ref revision/deterministic proof/policy version；核验获批 exact target Artifact/ObjectRef/lineage 后只做 ref CAS/ref_history + audit | `apply_patch` 纯计算与目标快照重算在事务外；结果必须同时匹配获批的 `target_artifact_id`/`target_snapshot_id`/digest。复用 draft/revision 时已发布的 preview Artifact，禁止 apply 时补发或另造 target；历史 Patch 必须先显式导入为带 preview/target binding 的新 workflow revision，不能在 apply 路径隐式升级 |
| constraint proposal publish | UoW 内重做 platform guard + approved ApprovalItem CAS→`applied` + exact proposal revision/validation evidence/target Artifact ID/digest/`target_snapshot_id` 绑定核对 + 已发布 candidate `constraint_snapshot` 的 ObjectRef/lineage/schema 可读检查 + constraint ref CAS/ref_history + audit | 不在 publish/apply 时补发或另造 target；人工修订、确定性 compile/validate、candidate 目标构造与 blob 写入早于该事务；不原地改 proposal，不让 Agent 直接写权威约束 |
| rollback apply | idempotency + rollback ApprovalItem CAS→`applied` + history/target existence/schema-readable/RBAC 检查 + ref CAS/ref_history + immutable `RefTransition` + audit；若明确撤销某已发布 patch/constraint，同事务 CAS 原 item→`rolled_back` | **不写 lineage edge**；rollback_request item 自身不再转 `rolled_back` |
| Run create | idempotency(request_hash) + 对 run/principal/system 全部适用 Budget 冻结 BudgetSetSnapshot 并原子建立 run-level BudgetHold ReservationGroup + `RunRecord(queued)`（含 server-injected 非 canonical dispatch trace carrier）+ initial run-level RunEvent(`attempt_no=null`) + audit | 尚无 attempt/lease/fencing，不写 0 sentinel；不占 execution concurrency PermitGroup；任一预算拒绝/revision 冲突则全体不占用且不产生可领取 Run；进程内 signal 不是权威队列；trace carrier 不进 payload/request/artifact hash |
| claim | Run revision CAS `queued\|retry_wait→leased` + 对全部适用并发预算原子 acquire execution PermitGroup + 新 RunAttempt + 新 lease_id/attempt_no/单调 fencing_token + RunEvent + audit | 任一 permit 拒绝则不领取且全体不占用；不覆盖旧 attempt，不复用 fencing token；claim 不与旧 attempt 清算混在同一命令 |
| attempt start | current lease_id/fencing/未过期 + run revision CAS；以 DB-authoritative UTC 固定 `attempt_deadline_utc=min(now+attempt_timeout,overall_deadline)`；Run/Attempt `leased→running` + started event + audit | 不在执行用户代码后补写 running；heartbeat 不延长该 deadline |
| lease heartbeat/renew | 同一 UoW 以 expected lease_version/PermitGroup revision CAS，原子续 RunLease + execution PermitGroup 的全部 permits（不得超过 attempt/overall deadline）并返回递增 versions | 任一 permit 续租失败则全体与 lease 都不续；普通 attempt 写不匹配易变化的 lease_version，避免 heartbeat 让合法 worker 自我 fencing；renew 不延长 attempt deadline |
| prompt render publication | canonical renderer 在事务外从已绑定 source Artifact 构造有界 PromptPartV1、写 `source_rendered` blob，并由 exact rendered messages 计算 request_hash；UoW 内校验 current lease/fencing/deadline、source IDs/hash/ProvenanceV1/purpose，以 RunAttempt CAS 领取 call_ordinal，发布含 exact `prompt_version/agent_graph_version` 的 `source_rendered` Artifact/ObjectRef/lineage + RunIntermediateArtifactLink(call_ordinal,request_hash) + audit | 必须在对应 reserve/provider call 或 cassette replay **之前**提交；不把 prompt 正文写 RunEvent/log；失败/重试 attempt 的 rendered 证据不删除、不等 Run 成功才首次发布 |
| RECORD response capture | provider response/record-shard blob 事务外规范化并 `put_verified`；UoW 内校验 current lease/fencing/deadline 及既有 prompt link 的 call_ordinal/request_hash，按该 ordinal 发布单-record `cassette_bundle` Artifact/lineage，原子 CostLedger reconcile + audit；提交成功后才把 response 交给 Agent | 不把 raw response 放普通 RunEvent/log；发布失败则 response 不进入 Agent 状态，既有 provider 成本仍按 ReservationGroup 结算；不等 Run 成功才首次持久化，不另分配/复用 ordinal |
| cancel request | idempotency + RunCommandRecord(`applied`) persist + run revision CAS 设置 cancel_requested + run-level Event + audit；queued 且无 lease 可直接走 terminal cancel | active worker 协作观察 RunRecord 标志；客户端不接触 fencing token；不留下含糊的 pending cancel command |
| lease expiry/reaper | 事务外准备 attempt-scope `run_failure` 与（RECORD 且 LLM-capable 时）attempt cassette bundle blob；UoW 内以 current lease identity/fencing + run revision CAS 发布二者，关闭旧 attempt/PermitGroup，已有 ReservationGroup 按实际 usage 或保守上界 settle；可重试时 Run→`retry_wait`；直接终态时同一 UoW 再发布 run aggregate cassette + run-scope final `run_failure`（其 lineage 含该 attempt failure），再置终态；RunEvent + audit | retry 时不写 `RunRecord.failure_artifact_id`（只在 Attempt/Event 留证）；**仅** retry_wait 保留 run-level BudgetHold；直接终态的 attempt/run 两个 manifest 必须分别匹配冻结 Outcome policy，不把 attempt manifest 冒充最终 Run failure |
| retryable attempt close | 事务外准备 attempt `run_failure` 与（适用时）attempt cassette bundle；UoW 内校验 current lease identity/fencing/未过期 + run revision，发布 Artifact/lineage，Attempt→failed(retryable)，ReservationGroup reconcile，Run→`retry_wait`，关闭 lease/PermitGroup，持久 retry_not_before_utc，RunEvent + audit | `RunRecord.failure_artifact_id` 仍为空；保留 run-level BudgetHold；随后独立 claim 才分配新 attempt/token |
| terminal success | 校验 current lease_id/fencing_token/未过期 + run revision；RECORD 且 LLM-capable 时先发布本 attempt bundle + run aggregate bundle；调用 RunKind allowlisted ResultPublisher 写领域 Artifact/records，再插 immutable RunResult manifest/lineage；Attempt outcome + `RunRecord.result_artifact_id`/terminal cassette/terminal status CAS + terminal RunEvent + CostLedger reconcile/settle + PermitGroup/run BudgetHold release + audit | 结果/aggregate blob 在事务外准备；publisher 不接受任意表名/callable；不要求 heartbeat 后的旧 lease_version；未知 usage 不释放预留 |
| terminal failure / cancel / timeout | worker 发布校验 current lease identity/fencing/expiry；存在 active attempt 时，先发布独立 attempt-scope `run_failure` 并令 `RunAttempt.failure_artifact_id` 只指向它，再发布 lineage 聚合全部 closed-attempt failures 的 run-scope final `run_failure`；queued 且从未 claim 时只发布 attempt_no=null 的 run-scope failure；retry_wait 控制面终结时证明无 active lease，以最大已关闭 attempt_no 聚合既有 attempt failures，只发布 run-scope failure，不重复关闭旧 attempt；RECORD LLM-capable 时发布对应 run cassette；Run terminal + `RunRecord.failure_artifact_id`(仅 run scope) + final cassette pointer + RunEvent + ledger settlement + PermitGroup/run BudgetHold close/release + audit | 所有终态都关闭 hold；不创建假 `run_result`，不绕过 active lease，不把异步失败伪装成 HTTP 错误；无 provider response 被消费时空 bundle 是显式证据，不省略 cassette_id；active attempt 的两个 manifest 与 retry_wait 的 run-only manifest 都必须匹配冻结 policy/projection，run manifest 永不自引 |

ref CAS 始终双匹配 `{artifact_id, revision}`；`ref_history.seq` 直接使用成功提交后的 ref revision，禁止 `MAX(seq)+1`。attempt-scoped 普通写校验 current `{lease_id,fencing_token,expires_at}`，只有 heartbeat CAS `lease_version`；过期 token 禁止新 reserve/外部调用/业务发布，但对其**已持久 ReservationGroup** 的 reconcile 必须按 group/request/attempt 幂等记入真实 usage，缺失则由 reaper 以保守上界 settle。SQLite 本地实现启用 `foreign_keys=ON` + WAL，write UoW 用 `BEGIN IMMEDIATE`；所有 CAS 以条件 `UPDATE` 的 `rowcount==1` 判定。PostgreSQL 的逐命令策略见 §7.A。

### 3.3 三个状态机

**① ApprovalItem（maker-checker 权威真相源）** — `PatchView` 的 validation/regression/approval status 全由此处派生；Patch payload 不保存可变状态。核心 DTO 一次冻结：

```text
ConstraintSourceBinding { source_artifact_id, source_ref?, provenance_hash }
ConstraintProposalV1 {
  proposal_schema_version:constraint-proposal@1, revision, supersedes_artifact_id?, base_constraint_snapshot_id?,
  dsl_grammar_version, domain_scope, constraints:[typed ConstraintDefinition...], source_bindings:[ConstraintSourceBinding...],
  produced_by:agent|human, producer_run_id?, rationale
}
RollbackRequestV1 {
  rollback_schema_version:rollback-request@1, ref_name, expected_current_ref:RefValue, target_artifact_id, target_history_revision,
  rollback_profile_binding:ResolvedExecutionProfileBindingV1, reason, reverses_approval_id?
}
ApprovalStatus = draft | validating | validation_failed | validated | pending_approval | auto_apply_eligible |
                 approved | changes_requested | rejected | applied | rolled_back | superseded
SubjectKind    = patch | constraint_proposal | rollback_request
EvidenceRequirement { requirement_id, kind, applicability:required|not_applicable,
                      status:passed|failed|unproven|not_applicable, evidence_artifact_id?, reason_code?, tool_version }
PatchTargetBindingV1 {
  binding_schema_version:approval-target-binding@1, subject_kind:patch,
  target_artifact_kind:ir_snapshot, target_artifact_id, target_snapshot_id, target_digest,
  ref_name, expected_ref:RefValue|null
}
ConstraintTargetBindingV1 {
  binding_schema_version:approval-target-binding@1, subject_kind:constraint_proposal,
  target_artifact_kind:constraint_snapshot, target_artifact_id, target_snapshot_id, target_digest,
  ref_name, expected_ref:RefValue|null
}
RollbackTargetBindingV1 {
  binding_schema_version:approval-target-binding@1, subject_kind:rollback_request,
  target_artifact_kind:ArtifactKind, target_artifact_id, target_snapshot_id?, target_digest,
  ref_name, expected_ref:RefValue, rollback_profile_binding:ResolvedExecutionProfileBindingV1
}
ApprovalTargetBinding = PatchTargetBindingV1 | ConstraintTargetBindingV1 | RollbackTargetBindingV1
FindingEvidenceBindingV1 { finding_id, finding_revision, evidence_artifact_id, finding_digest }
EvidenceSet { evidence_schema_version:evidence-set@1, subject_artifact_id, subject_digest, policy_version, validation_run_id,
              target_binding:ApprovalTargetBinding?, supporting_artifact_ids[], finding_bindings:[FindingEvidenceBindingV1...],
              requirements:[EvidenceRequirement...], overall_status:passed|failed|unproven }
              # 整体作为 immutable validation_evidence Artifact；回归可另指 regression_evidence Artifact
ConstraintCompileStageV1 {
  stage_id, stage:parse|typecheck|compile|differential|golden,
  status:passed|failed|unproven|not_applicable, engine_id?, engine_version?, reason_code?
}
ConstraintCompileEvidenceV1 {
  evidence_schema_version:constraint-compile-evidence@1, proposal_artifact_id,
  base_constraint_snapshot_artifact_id?, candidate_constraint_snapshot_artifact_id?, dsl_grammar_version,
  compiler_profile:ProfileRefV1, stages:[ConstraintCompileStageV1...], overall_status:passed|failed|unproven
}
ApprovalRequirement { requirement_id, domain_scope:DomainScope, required_permission:Permission, route_role:Role,
                      min_approvals, assignee_principal_ids[], distinct_from_requirement_ids[] }
ApprovalDecision { decision_id, requirement_ids[], decision:approve|reject|request_changes, actor:AuditActor,
                   expected_workflow_revision, reason_code, comment?, occurred_at }
ApprovalPolicyRefV1 { policy_version, policy_digest }
ApprovalPolicyV1 {
  policy_schema_version:approval-policy@1, policy_version,
  subject_kinds:[SubjectKind...], maker_checker_required:true,
  human_approver_required:true, reauthorize_on_decision:true, reauthorize_on_apply:true,
  rollback_requires_approval:true, terminal_revision_immutable:true,
  policy_digest
}
ApprovalPolicyRegistryV1 {
  registry_schema_version:approval-policy-registry@1, policies:[ApprovalPolicyV1...], registry_digest
}
QualifiedOutcomeRuleRefV1 { resolved_policy_id, outcome_rule_id }
DeterministicOracleRefV1 { oracle_id, oracle_version, oracle_digest }
DeterministicOracleDefinitionV1 {
  oracle_schema_version:deterministic-oracle@1, oracle_id, oracle_version,
  engine_kind:graph|asp|smt|simulation|playtest_completion,
  tool_version, domain_registry:DomainRegistryRefV1, supported_domain_scope:DomainScope|all,
  evidence_artifact_kinds[], evidence_payload_schema_ids[], predicate_schema_id,
  oracle_digest
}
DeterministicOracleRegistryRefV1 { registry_version, registry_digest }
DeterministicOracleRegistryV1 {
  registry_schema_version:deterministic-oracle-registry@1, registry_version,
  definitions:[DeterministicOracleDefinitionV1...], registry_digest
}
AutoApplyPolicyRegistryRefV1 { registry_version, registry_digest }
AutoApplyPolicyRefV1 {
  registry:AutoApplyPolicyRegistryRefV1, policy_id, policy_version, policy_digest
}
AutoApplyPolicyV1 {
  policy_schema_version:auto-apply-policy@1, policy_id, policy_version,
  subject_kind:patch, allowed_operation_kinds[], maximum_operation_count,
  domain_registry:DomainRegistryRefV1,
  deterministic_oracle_registry:DeterministicOracleRegistryRefV1,
  required_deterministic_oracles:[DeterministicOracleRefV1...],
  required_outcome_rules:[QualifiedOutcomeRuleRefV1...],
  allowed_domain_scopes:[DomainScope...], forbidden_domain_scopes:[DomainScope...],
  require_no_numeric_value_change, require_no_narrative_text_change,
  allowed_ref_names[]
}
AutoApplyPolicyRegistryV1 {
  registry_schema_version:auto-apply-policy-registry@1, registry_version,
  policies:[AutoApplyPolicyV1...], registry_digest
}
AutoApplyValidationProfileBindingV1 {
  validation_profile:ProfileRefV1, validation_profile_payload_hash,
  policy:AutoApplyPolicyRefV1
}
AutoApplyOracleEvidenceBindingV1 {
  oracle:DeterministicOracleRefV1, evaluated_domain_scope:DomainScope,
  evidence_artifact_id, evidence_payload_hash
}
AutoApplyOutcomeEvidenceBindingV1 {
  rule:QualifiedOutcomeRuleRefV1, requirement_id, evidence_artifact_id, evidence_payload_hash
}
AutoApplyProofV1 {
  proof_schema_version:auto-apply-proof@1, subject_artifact_id, subject_digest,
  target_binding:PatchTargetBindingV1, affected_domain_scope:DomainScope,
  validation_evidence_artifact_id, regression_evidence_artifact_ids[],
  validation_profile_binding:AutoApplyValidationProfileBindingV1,
  deterministic_oracle_evidence:[AutoApplyOracleEvidenceBindingV1...],
  required_outcome_evidence:[AutoApplyOutcomeEvidenceBindingV1...],
  policy:AutoApplyPolicyRefV1
}
AutoApplyProofBindingV1 {
  proof_artifact_id, policy:AutoApplyPolicyRefV1,
  subject_digest, target_digest, expected_ref:RefValue|null, validation_evidence_artifact_id
}
ApprovalItem {
  approval_schema_version, approval_id, subject_series_id, subject_revision, subject_kind, subject_artifact_id, subject_digest,
  status, workflow_revision, supersedes_approval_id?, proposer:AuditActor,
  domain_scope:DomainScope, domain_registry_ref:DomainRegistryRefV1, route_policy:DomainRoutePolicyRefV1,
  role_policy_version, role_policy_digest, approval_policy:ApprovalPolicyRefV1,
  requirements:[ApprovalRequirement...], decisions:[ApprovalDecision...],
  active_validation_run_id?, last_validation_failure_artifact_id?, evidence_set_artifact_id?, regression_evidence_artifact_ids[],
  target_binding?, auto_apply_proof:AutoApplyProofBindingV1?,
  created_at, submitted_at?, decided_at?, applied_at?
}
SubjectHead { subject_series_id, current_subject_artifact_id, current_approval_id, revision }
FindingPayloadV1 {
  payload_schema_version:finding-payload@1,
  source, producer_id, producer_run_id, oracle_type, defect_class, severity,
  snapshot_id, entities[], relations[], constraint_id?, evidence, minimal_repro,
  status, confidence?, message
} # foundations Finding 去掉 id/finding_schema_version/created_at 后的完整字段；不得再嵌 identity/time
FindingRevisionV1 {
  revision_schema_version:finding-revision@1,
  finding_id, revision, supersedes_revision?, created_at,
  payload:FindingPayloadV1
}
FindingDigestPayloadV1 {
  revision_schema_version:finding-revision@1,
  finding_id, revision, supersedes_revision?, payload:FindingPayloadV1
} # 明确不含 created_at

`DeterministicOracleDefinitionV1.oracle_digest=sha256(canonical_json(payload excluding oracle_digest))`；definitions 按唯一 `(oracle_id,oracle_version)` 排序，kind/schema allowlist stable unique，registry digest 对 definitions 的 canonical historical payload计算且排除自身 digest。`AutoApplyPolicyRefV1.policy_digest=sha256(canonical_json(AutoApplyPolicyV1))`；policy registry digest 同样对排除自身 digest 的 canonical payload 计算，policies 按唯一 `(policy_id,policy_version)` 排序；两个 registry 都保留全部历史版本。policy.domain_registry 必须等于 ApprovalItem/target domain 所绑 exact registry；required oracle/rule refs、allowed operation/ref/domain scope arrays stable unique；rule identity 是 `{resolved_policy_id,outcome_rule_id}`，绝不把裸 rule ID 当全局唯一。

Auto-apply 的唯一 affected scope 是服务端从真实 subject/base/target canonical diff 重算并与 `ApprovalItem.domain_scope` 逐字段对拍后写入 `AutoApplyProofV1.affected_domain_scope` 的 non-empty DomainScope，不从客户端另读第二份 scope。其每个 domain ID 必须被非空 `allowed_domain_scopes` 的并集 all-of 覆盖，且与任一 `forbidden_domain_scopes` 的 exact ID 集合交集为空；forbidden 优先，scope parent/child 不隐式展开，两个数组按 canonical domain-ID tuple 排序去重。allowed/forbidden 自相交、未知 domain、registry 不同或 affected scope 覆盖不全都使 policy readiness/validation fail-closed。numeric/narrative guard 从 exact base→target canonical diff 与 exact schema重算，字段分类未知即拒绝；validation terminal、submit 与 apply 三处都运行同一纯 scope/oracle guard，不只信 proof 生成时结果。

每个 oracle ref 必须从 exact historical registry 按 id/version/digest 唯一解析，ref digest 与 definition 内嵌 digest相等，definition.domain_registry 与 policy 相等，supported scope 对 affected scope all-of 覆盖。每份 evidence binding 的 `evaluated_domain_scope` 必须等于 affected scope，且 evidence payload/hash/lineage 内的 exact scope、subject、target、predicate 与 deterministic verdict=`passed` 逐字段相等；failed/unproven、LLM/mixed/human verdict 或未声明 scope 都不能成为 auto-apply 权威。policy 未覆盖、domain/oracle registry/ref/digest 不闭合、forbidden scope 相交或任一 predicate 不是 allowlisted deterministic oracle即不合格。

`validation` ExecutionProfile 对 `patch.validate@1` 必须使用 §5.3 的 `ValidationProfileDetailsV1`，其中 exact `auto_apply_policy` 为 null 或上述完整 ref；Run 创建把 validation profile payload hash + registry/ref 闭包冻结，不能从 current policy alias读取。proof 的 profile binding 必须等于 RunPayload 的 resolved validation profile；required oracle 与 qualified outcome requirements 各自全量一一映射到 proof binding，evidence hash、EvidenceSet requirement、resolved policy snapshot 与 Artifact lineage 全部交叉校验。AutoApplyProofBindingV1.proof_artifact_id 必须指 kind=`validation_evidence`、payload schema=`auto-apply-proof@1` 的 Artifact；payload、binding、EvidenceSet 与 ApprovalTargetBinding 的 subject/target/ref/digest 逐字段相等，lineage 直接绑定 subject + target + EvidenceSet + 全部 deterministic/outcome/regression evidence，不另造悬空 ArtifactKind。proof 只能由 `patch.validate@1` 的 `patch_validation_auto_eligible` 专用 success policy 与 EvidenceSet 同一 terminal UoW 发布，workflow effect 原子写入 ApprovalItem；submit/apply 都重新从 exact historical registries 解析 policy/oracles，复核 profile binding、proof hash/lineage 与 current ref，再 CAS，绝不现场补造 proof或读取 current alias。普通 passed、failed、unproven、execution failure或 superseded outcome 的 proof 必须为空；constraint/rollback 永不产生 auto proof。ConstraintProposalV1/RollbackRequestV1 与 PatchV2 一样只作 immutable payload，
API `{id}`/supersedes 都指 Artifact.artifact_id，created_at 只在 Artifact envelope。
Patch/constraint 的 `ApprovalTargetBinding.expected_ref` 与 `AutoApplyProofBindingV1.expected_ref` 字段始终存在：`null` 是“目标 ref 必须不存在”的 CAS 前提，绝不表示未知或未提供；非 null 时同时匹配 artifact_id + revision。rollback 必须撤销已存在 ref，故 `RollbackTargetBindingV1.expected_ref` 非 null，并逐字段等于 request.expected_current_ref。

摘要算法冻结如下，wire 表示沿用 foundations v0.3 §5 的 64 位小写 SHA-256：`subject_digest = subject ArtifactV2.payload_hash`，`target_digest = target ArtifactV2.payload_hash`，不得对 Artifact envelope 再做第二套摘要。Finding 以稳定 series ID + immutable revision 存储，生命周期/status 改动创建新 `FindingRevisionV1`；wrapper 以独立 `revision_schema_version=finding-revision@1` 判别，inner payload 以 `payload_schema_version=finding-payload@1` 判别并排除 id/revision/created_at，消除双身份与双时间真相。foundations §6.1 的历史 flat Finding 继续由 `LegacyFindingV1` reader 按其 `finding_schema_version` 只读解析，wire union 以 `revision_schema_version` 是否存在区分；不得把 legacy flat shape 和 wrapper 复用同一 discriminator。历史 Finding 要进入 M4 validation/repair binding，必须显式发布 `FindingRevisionV1`（保留 legacy evidence/producer 关联），不能在 read path 伪造 revision/digest。`finding_digest = lowerhex(SHA256(UTF8("gameforge.finding-revision@1") || 0x00 || UTF8(typed_canonical_json(FindingDigestPayloadV1))))`，其中 `0x00` 是单个 NUL 分隔字节。`typed_canonical_json` 是 M4 Finding 专用的加法式无碰撞投影：null/bool/int/finite-float/string/array/object 使用互不相交的类型标签，对象键按字典序排列，float 使用稳定 IEEE-754 十六进制表示并保留 signed zero；因此 missing≠null、bool≠int、float≠string、`-0.0`≠`0.0`。历史 foundations `canonical_json` 的 null-dropping/float 规则及既有 snapshot/artifact ID 保持不变，不追溯改写。`created_at` 不入摘要，故同一确定性/cassette 回放不会因持久化时钟不同而漂移。API、validation、repair/generation 服务端都从持久对象重算并 constant-time 比较，客户端传入值只作 stale assertion，绝不作为摘要真相源；Finding revision 或 evidence Artifact 不匹配即 409/validation fail-closed。

`typed_canonical_json` 必须直接消费经过契约校验的 Python-mode JSON 值树，再由它自身生成 tagged JSON；禁止先经过会把非有限浮点降为 `null` 的 JSON-mode serializer。`inf`、`-inf`、`nan` 不属于可接受 wire，摘要构造、repository 写入与持久行读取都必须 fail-closed，绝不与 `null` 共用摘要或幂等身份。

`ConstraintProposalV1.produced_by` 与 workflow `PatchV2` 使用同一生产者不变量：`agent` 时 `producer_run_id` 必填、必须解析到成功 Run，且该 RunResult 的 `produced_artifact_ids` 必须包含本 proposal/Patch；`human` 时必须为 null，真实作者只进 immutable Artifact meta 与 `audit@2`。唯一例外是 failure-code allowlist 中的 `generation_gate_rejected` evidence-only Patch：其 `producer_run_id` 必须解析到该终态非成功 generation Run，且 Artifact ID 必须被同一 RunFailure 的 `evidence_artifact_ids`/lineage 精确包含；它不得进入 SubjectHead/ApprovalItem 或任何 Patch workflow API。任何入口都不得用 request_id、trace_id 或当前默认 Run 冒充 producer，也不得把该例外泛化到其他 failure code/subject kind。

ApprovalItem 的 `proposer` 是对 subject revision 负责的 maker，不是执行 publisher 的 worker：同步 human draft 使用当前 `ActorContext.principal`；异步 generation/repair/constraint-proposal success 使用 immutable `RunRecord.initiated_by`，worker service/system 只进入 `AuditRecordV2.actor`（即实际 executed_by 执行者）。service/system 直接发起时可成为 proposer，但仍不能冒充 human，且除 exact deterministic auto-apply 外必须由有权 human 审批；human A 发起的 Run 永远不能因 worker 是 service 而让 A 满足 `proposer≠approver`。新 superseding revision 以该 revision 的 accountable initiator 为 proposer并重新路由，禁止继承旧批准。

`ApprovalPolicyV1.policy_digest=sha256(canonical_json(payload excluding policy_digest))`，subject kinds 按 enum 顺序 stable unique；registry policies 按唯一 `policy_version` 排序，`registry_digest=sha256(canonical_json(payload excluding registry_digest))`，exact history 在引用它的 Approval/audit 保留期内不可删除。M4 初始 registry 只允许上述全部布尔守卫为 true，未知/摘要不符 policy readiness fail-closed；将来放宽任一守卫必须 bump policy/version并创建新 ApprovalItem，不能改写既有 item。状态机与 DomainRoute/RolePolicy 仍是机器权威，ApprovalPolicy ref 只选择冻结的守卫集合，不允许任意 handler/callable。

draft → validating
validating → validated | validation_failed | draft(execution_failed|cancelled|timed_out only)
validated → pending_approval | auto_apply_eligible
pending_approval → pending_approval(partial approve) | approved | changes_requested | rejected
approved | auto_apply_eligible → applied
applied → rolled_back   # 仅被后续 ref rollback 撤销发布结果的 patch/constraint item；rollback_request item 不走此边
draft|validating|validation_failed|validated|pending_approval|auto_apply_eligible|approved|changes_requested|rejected → superseded
守卫:
  draft→validating      : 以 workflow-revision CAS 绑定唯一 active Run；API 立即返回 202，不在 submit 内跑长任务
  validating→validated : 按 subject-specific policy 产生版本化 EvidenceSet，required 项全部通过；不适用项显式 not_applicable+reason，绝不补假 evidence
                           patch = applicable checkers + 受影响域 sim/regression + policy 要求的 Review/Playtest；所有证据必须绑定同一 target Artifact/snapshot/config；
                           constraint_proposal = parse/typecheck/compile + compiler differential/golden + 受影响域 regression；
                           rollback_request = history/target/schema/DSL compatibility + RollbackPolicy 规定的 impact/regression
  validating→draft     : 仅执行/依赖失败、用户取消或超时；清 active_validation_run_id、绑定 run_failure 后允许显式重试；确定性未通过必须进入 terminal validation_failed
  →pending_approval      : patch 按所有受影响 domain；constraint_proposal 按 constraint domains + constraint_admin；rollback_request 按目标 ref/domains + RollbackPolicy，
                           经版本化 DomainRoute 生成覆盖完整 DomainScope 的 requirements，缺任何域 route 即 fail-closed
  pending→pending        : 仅 append 某些 requirement 的 approve decision 并递增 workflow revision；同 actor/requirement 重试幂等、不重复计数
  →approved              : 每个 requirement 都达到 min_approvals，distinct 约束满足；所有 approver 是 human、≠proposer 且在决策时持对应全域 permission
  →auto_apply_eligible   : **仅 subject=patch**；validation 已用专用 outcome 同批发布并挂接 exact `auto-apply-proof@1`；submit 重验 proof payload/hash/lineage、
                           EvidenceSet、subject/target/ref 与冻结 AutoApplyPolicy id/version/digest 全相等且仍为 current，不能在 submit 现场重新判定或补证；constraint_proposal/rollback_request 必须由 human 批准
  →applied               : 按 subject 分派到 §3.2 patch apply / constraint proposal publish / rollback apply 边界（UoW 原子）
不变量: changes_requested/rejected/validation_failed 对当前不可变 revision 终态；修改创建新 revision 经 supersedes_artifact_id 关联；
        subject draft publish 同时创建唯一 ApprovalItem；Patch/rollback 的 target_binding 此时必填且 immutable；
        constraint proposal 仅在一次完成了 compile 且形成 candidate 的 validation completion UoW 中允许 target_binding 从 null→exact，非 null 后永不替换；
        EvidenceSet.target_binding 必须与完成该事务后的 ApprovalItem binding 逐字段相等；后续 submit/decision/apply **只做 workflow-revision CAS**，不再二选一创建新 item；
        新 subject revision 才创建带 supersedes_approval_id 的新 item并推进 SubjectHead；validate/submit/decision/apply 均重验 current head；
        多域 subject 不得取“主域/第一个域”；requirements 必须覆盖 domain_scope 全集，单一 human 可覆盖多个 requirement 但必须逐项持权；
        superseded 永远不可执行/复活；所有迁移带 CAS，未列出的迁移一律拒绝
```

`constraint_proposal` 的权威化流程固定为：Agent/human 只能先产不可变 proposal；human author 以 typed editor 创建 `produced_by=human` 的新 proposal revision（`supersedes_artifact_id` 指旧版）→ 按其声明 grammar 发起确定性 compile/validate Run；只要执行完成且 compile 形成 candidate，completion 就先发布非权威 candidate `constraint_snapshot` Artifact（direct lineage=proposal + 可选 base；source 由 proposal 的 `source_bindings` 与 direct lineage 传递，不重复挂边；不以 EvidenceSet 为 parent，避免 target↔evidence 环），在同一 UoW 产生 `ConstraintCompileEvidenceV1` + EvidenceSet，并将 ApprovalItem.target_binding **唯一一次**从 null CAS 为该 Artifact 的 exact ID/digest/`target_snapshot_id`/expected constraint-ref revision；candidate 后续回归未过可进入 `validation_failed`，但 binding 仍精确记录实际判过的对象。若 parse/compile 根本未形成 candidate，则 binding 保持 null 且 overall 必为 failed/unproven，但 compile evidence 仍须完整记录实际执行/未执行 stage。只有 `validated` 强制 non-null，submit 也强制 non-null并只重验 EvidenceSet 与 binding 相等后推进状态 → **另一 human** approve → §3.2 原子发布仅核验 exact Artifact 并移动 constraint ref。Artifact 存在不代表权威，ref 才代表权威。compile evidence、candidate 与获批绑定任一不一致、ref 已变化或 proposal 未经 human authoring均 fail-closed；原 proposal/source/evidence 仍可经 transitive lineage 完整追溯。

`ConstraintCompileEvidenceV1.stages` 按 `(stage,stage_id)` 稳定排序且 stage_id 唯一：parse/typecheck/compile 各恰好一项；每个 `differential_engines:SolverEngineRefV1` 恰好一项 differential；请求有 golden suite 时恰好一项 golden，否则该 stage 仍以 `not_applicable+reason_code` 出现。engine/version 必须等于 payload/profile 冻结值。candidate ID 非 null 当且仅当 compile 形成并同批发布 exact candidate；任何 required stage failed/unproven 时 EvidenceSet 不得 passed。compile evidence Artifact 的 direct lineage 仅为 proposal、可选 base与可选 candidate，EvidenceSet 再引用 compile evidence，故来源完整且不造 candidate↔evidence 环。

Patch preview 是可检查、可回放但**非权威**的候选 Artifact：`ir_snapshot` lineage 必须至少包含 base snapshot + Patch；只有请求非空 adapter profile 时才发布 `config_export`，其 lineage 必须包含该 preview + 实际 constraint snapshot，并按 profile 一一产出。Review/Playtest/Repair 只消费这些已提交 Artifact ID，不接收浏览器上传的临时对象。Patch draft/revision 发布时已经把 preview 的 exact binding 写入 ApprovalItem 且此后不可替换；`EvidenceSet.target_binding` 必须逐字段复制并校验该 binding，`supporting_artifact_ids` 冻结 Review、Playtest 和回归证据的 exact IDs，`finding_bindings` 冻结对应 Finding series/revision/digest/evidence Artifact。submit 只断言相等并 CAS 状态。只有获批 `apply` 的 ref CAS 才让 preview 成为当前版本。

`ApprovalTargetBinding` 用 `subject_kind` discriminator 的上述 JSON Schema `oneOf` 冻结三类必填字段，不能只靠散文校验。Patch binding 的 target 字段全部必填；rollback draft 创建时必须完整绑定 current/target/ref，并把 subject 中的 exact rollback profile binding 逐字段复制到 target binding。该 binding 固定 `field_path=/params/rollback_profile`、`expected_profile_kind=rollback`，含 profile payload hash 与 catalog version/digest，因而后续 validate/submit/apply 不读取 current profile。任何必填字段缺失都不得推进。constraint proposal 只按上一段的唯一 null→exact 时点补齐。Patch validation 对每个 `supporting_artifact_id` 重验 kind、payload hash、VersionTuple、producer Run、policy/tool/model/cassette 版本及 lineage：Review 必须直接消费该 preview，Playtest trace 必须消费由该 preview 导出的 exact config，Finding revision/digest 必须与持久记录一致。缺失、stale、跨 preview、policy 不符或 required Playtest `skip/not_applicable` 一律 failed/unproven，绝不拼接成 passed EvidenceSet。adapter/export 只由 versioned profile 选择；平台契约不硬编码 Aureus、Flare 或任何具体游戏，Aureus 仅作为 Journey A 的参考 Agent-Env 测试 profile。

`generation_gate_rejected` 为可复核性保留 rejected Patch、内存 gate 所判定内容的 `ir_snapshot` preview 与 checker/sim/review evidence，并把它们列入 RunFailure evidence/lineage；这些 Artifact **不是 workflow subject**：不创建 SubjectHead/ApprovalItem、不产 `config_export`、所有 Patch workflow 命令因找不到 current subject head 而 fail-closed。这样既不把未过门禁内容称为候选，也不丢失重放 gate 所需的输入与判定证据。

**② RunRecord / RunEvent / RunLease（持久异步执行，脱离 HTTP 生命周期）**

```text
RunKindRef { kind, version }
ProfileRefV1 { profile_id, version }
FailureClassV1 = business_rule | validation | transient_dependency | permanent_dependency | quota |
                 execution | cancelled | timeout | lease | subject_superseded | integrity
DependencyFailureV1 {
  dependency_schema_version:dependency-failure@1,
  dependency_kind:model_provider|database|object_store|cost_ledger|solver_executor|simulation_backend|game_environment|identity_provider,
  dependency_id, operation_code, classifier_code, upstream_status_code?, retry_after_ms?
}
FailureClassifierRefV1 { classifier_version, classifier_digest }
FailureClassificationRuleV1 {
  cause_code, failure_class:FailureClassV1, intrinsic_retry_eligible,
  dependency_required, allowed_dependency_kinds[]
}
FailureClassifierV1 {
  classifier_schema_version:failure-classifier@1, classifier_version,
  rules:[FailureClassificationRuleV1...], classifier_digest
}
RetryPolicyRefV1 { retry_policy_id, retry_policy_version, retry_policy_digest }
RetryDecisionV1 {
  decision_schema_version:retry-decision@1, cause_code, failure_class:FailureClassV1,
  intrinsic_retry_eligible, decision:retry|terminal,
  reason_code:transient_eligible|retry_after|max_attempts_exhausted|queue_deadline_exhausted|attempt_deadline_exhausted|
              overall_deadline_exhausted|budget_exhausted|policy_forbidden|not_retry_eligible,
  retry_not_before_utc?, classifier:FailureClassifierRefV1, retry_policy:RetryPolicyRefV1, evaluated_at_utc
}
PlannedAgentNodeVersionV1 {
  agent_node_id, prompt_version, tool_version, allowed_model_snapshots[]
}
ExecutionVersionPlanV1 {
  plan_schema_version:execution-version-plan@1, agent_graph_version,
  nodes:[PlannedAgentNodeVersionV1...], model_catalog_version, model_catalog_digest,
  routing_policy_version, routing_policy_digest, plan_digest
}
ResolvedExecutionProfileBindingV1 {
  field_path, profile:ProfileRefV1, expected_profile_kind, profile_payload_hash,
  catalog_version, catalog_digest
}
ResolvedArtifactRequirementV1 {
  requirement_id, outcome_rule_id, artifact_kind:ArtifactKind, payload_schema_id,
  producer_profile_field_path?, ordinal
}
ResolvedPolicySnapshotV1 {
  snapshot_schema_version:resolved-policy@1, resolved_policy_id,
  source_profile_field_path, source_profile_payload_hash,
  requirements:[ResolvedArtifactRequirementV1...], digest
}
RunPolicyBindingV1 { binding_key, policy_kind, policy_id, policy_version, policy_digest }
RunSchemaBindingV1 { binding_key, schema_id }
RunDispatchTraceCarrierV1 {
  carrier_schema_version:run-dispatch-trace@1, traceparent, tracestate?
} # 由服务端 TraceCarrier.inject 生成的有界 W3C carrier；非客户端任意字典
RunPayloadEnvelope {
  payload_schema_version, input_artifact_ids[], version_tuple:VersionTuple,
  execution_version_plan:ExecutionVersionPlanV1?,
  policy_bindings:[RunPolicyBindingV1...], schema_bindings:[RunSchemaBindingV1...],
  execution_profile_catalog_version, execution_profile_catalog_digest,
  resolved_profiles:[ResolvedExecutionProfileBindingV1...],
  resolved_policy_snapshots:[ResolvedPolicySnapshotV1...],
  budget_set_snapshot_id, seed?, llm_execution_mode:not_applicable|live|record|replay, cassette_artifact_id?, params:RunKindPayload
}
RunRecord {
  run_schema_version, run_id, kind:RunKindRef, status, revision,
  idempotency_scope, idempotency_key, request_hash, payload:RunPayloadEnvelope, payload_hash,
  run_kind_definition_digest, outcome_policy_set_digest, migration_capability_matrix?:MigrationCapabilityMatrixRefV1,
  failure_classifier:FailureClassifierRefV1,
  dispatch_trace_carrier:RunDispatchTraceCarrierV1?,
  initiated_by:AuditActor, queue_deadline_utc, attempt_timeout_ns, overall_deadline_utc,
  cancel_requested_at?, cancel_requested_by?, current_attempt_no?, next_attempt_no, next_fencing_token, next_event_seq,
  budget_set_snapshot_id, run_budget_hold_group_id, concurrency_permit_group_id?, retry_policy:RetryPolicyRefV1, max_attempts, retry_not_before_utc?,
  result_artifact_id?, failure_artifact_id?, terminal_cassette_artifact_id?, created_at, updated_at
}
RunAttempt {
  run_id, attempt_no, status, fencing_token, worker_principal_id, trace_id?,
  next_call_ordinal, started_at?, attempt_deadline_utc?, ended_at?, failure_class?, retryable?,
  failure_artifact_id?, cassette_bundle_artifact_id?
}
RunLease {
  lease_id, run_id, attempt_no, fencing_token, lease_version, owner_principal_id,
  acquired_at, heartbeat_at, expires_at, status:active|closed|expired
}
RunEvent {
  event_schema_version, run_id, seq, event_type, attempt_no?, occurred_at, data_schema_version, data, trace_id?
  # run-level event 的 attempt_no=null；attempt-scoped event 必须为正整数；(run_id,seq) 唯一且严格递增
}
RunIntermediateArtifactLinkV1 { link_schema_version, run_id, attempt_no, call_ordinal, artifact_id,
                                role:prompt_rendered, request_hash, fencing_token, published_at }
RunFindingLinkV1 {
  link_schema_version:run-finding-link@1, run_id, attempt_no, ordinal,
  finding_id, finding_revision, finding_digest, evidence_artifact_id
}
RunResultV1 {
  result_schema_version:run-result@1, run_id, attempt_no, run_kind:RunKindRef,
  primary_artifact_id, produced_artifact_ids[], finding_count, outcome_code, summary:RunResultSummaryV1,
  requirement_dispositions:[RequirementDispositionV1...],
  version_projection:RunManifestVersionProjectionV1
}
RunFailureV1 {
  failure_schema_version:run-failure@1, run_id, attempt_no?, run_kind:RunKindRef, cause_code, failure_class:FailureClassV1,
  retryable, retry_decision:RetryDecisionV1, dependency?:DependencyFailureV1,
  redacted_message, evidence_artifact_ids[], requirement_dispositions:[RequirementDispositionV1...], occurred_at,
  version_projection:RunManifestVersionProjectionV1
}

`RunPayloadEnvelope.budget_set_snapshot_id` 是 immutable canonical budget binding；`RunRecord.budget_set_snapshot_id` 只是同值的索引投影。Run create UoW 必须把两者与 CostLedger 新建的 `BudgetSetSnapshot.budget_set_snapshot_id/run_id` 逐字段写成相等，claim、reserve、terminal publication 与 recovery 都重新校验三者；Record 列没有独立 mutator，任何不等均为 `IntegrityViolation`，worker/router/ledger 不得各自选择一个“较新”值。

Run payload 不接受任意 policy/schema map：两组 bindings 都按唯一 `binding_key` 排序，RunKindDefinition + exact resolved profiles 冻结每个 kind 的完整 required-key 集，创建时必须逐项填满且 policy ref 可从保留 registry按 kind/id/version/digest 唯一解析；额外键、缺键、摘要不符或 current alias 补值都拒绝。REPLAY 的 typed bindings 与 native source Run 或 verified legacy manifest 必须逐项相等。

`ExecutionVersionPlanV1.nodes` 按唯一 `agent_node_id` 排序，allowed models stable unique；model catalog/routing policy 必须以 exact version+digest 从保留 registry 解析，且每条 routing rule 可达模型都是对应 node allowlist 成员。`plan_digest=sha256(canonical_json(payload excluding plan_digest))`。`llm_execution_mode=live|record|replay` 时 plan 必填，`not_applicable` 时必须 null；Run 创建的 `version_tuple.prompt_version/model_snapshot` 是 plan 全部允许成员的 single/set projection，只表示冻结执行边界。attempt/run 终态再从实际 rendered request + native RoutingDecision/cassette，或 foundations v0.3 §7 verified legacy-import 的 LegacyImportRoutingDecision/cassette evidence，构造 foundations v0.3 §5 `ExecutionIdentityV1`，按 transition policy把 terminal tuple 投影为实际 single/set/null；两种 decision kind 不能互换。domain Artifact 只绑定对其有因果贡献的 invocation 子集。任何实际 node/prompt/model/tool 不在 plan、tuple/meta/identity digest 不闭合或从 current registry补值都 fail-closed。

RunResultV1.produced_artifact_ids 必须非空、canonical 稳定排序去重；primary_artifact_id 必须恰为其中一个成员。`finding_count` 必须等于同一 terminal UoW 新增的 RunFindingLink 数量，Finding 不是 Artifact，不能混入 produced_artifact_ids 或 lineage。
RunKind ResultPublisher 必须在 terminal UoW 内按 foundations v0.3 §5.1 生成 projection：RunResult 与 RunRecord 最终 RunFailure 使用 `manifest_scope=run`；**每个非成功 closed attempt**（retry、lease expiry，以及 active attempt 的最终 failure/cancel/timeout）先使用 `manifest_scope=attempt` 发布独立 RunFailure。attempt scope 的 projection `attempt_no` 必须为正并匹配外层 RunFailure/RunAttempt，且 `RunAttempt.failure_artifact_id` 只指该 attempt manifest；run scope 已有 attempt 时必须填本清单聚合的最大最终 attempt并匹配外层 RunResult/RunFailure，且 `RunRecord.failure_artifact_id` 只指 run manifest，仅从未 claim 的 queued 控制面终结可为 null。`retry_wait` 期间控制面取消或整体超时必须先证明无 active lease，只聚合全部既有 closed-attempt manifests 并发布 run-scope final failure；它使用最大已关闭 `attempt_no`，不创建或重复关闭 attempt。success 的 `produced_artifact_ids` 与 failure 的 `evidence_artifact_ids` 都精确等于 projection 中**全部** `publication=run_published` 且 `role!=input` 的 parent；OutcomeArtifactPolicy 的 primary/output rule 均投影成 manifest `role=output`，唯一 primary 由 `RunResultV1.primary_artifact_id` 单独标识；success 可以有 evidence role，failure policy则禁止 primary/output rule，明确的业务拒绝产物一律归 evidence。Run 创建时的 `input_artifact_ids` 精确等于 projection 中 existing input parent。REPLAY cassette 是带 `cassette_scope=replay_input` 的 input；RECORD attempt manifest 必须有同 attempt 的 attempt_bundle，run manifest 必须有 run_bundle，二者都是带对应 cassette_scope 的 intermediate，不占第二个互斥角色。run-scope failure 的 parents 包含所有 closed attempt 的独立 failure manifests，包括刚发布的最终 current attempt manifest；外层 manifest Artifact.lineage 精确等于 projection 全部 parent ID 的稳定去重集合，run manifest 自身不得出现。领域 Artifact 也不得反向引用尚未生成的 manifest Artifact ID（只可引用预先存在的 run_id），因此不造 hash/lineage 环。success primary 必须且只能匹配 OutcomeArtifactPolicy 的 primary rule。任何集合多一项、少一项、角色不符或未发布引用均 fail-closed。

`RunAttempt.failure_artifact_id/failure_class/retryable` 只是 attempt-scope `RunFailureV1` 的只读投影：非成功关闭时须在同一 UoW 逐字段相等，成功时三者必须全为 null；没有独立 mutator，recovery/readiness 对不等或悬空指针 fail-closed。

RunStatus       : queued → leased → running；leased|running → retry_wait → leased ...；
                  queued|leased|running|retry_wait → (succeeded | failed | cancelled | timed_out)，但 success 仅允许 current running attempt
                  queued 仅在无 active lease 时可由 queue deadline/cancel 直接非成功终态；leased/running 先记 cancel_requested，
                  再由持 lease worker 或以 lease CAS 取得处置权的 reaper 发布终态；retry_wait 仅在证明无 active lease 后由控制面终结
RunAttemptStatus: leased → running → (succeeded | failed | cancelled | timed_out | lease_expired)
RunLease guard  : heartbeat 用 expected lease_version CAS 并接收新 version；普通 attempt 写只校 current lease_id/fencing_token/未过期 + run revision，
                  不匹配易变 lease_version；run-level queued/cancel_requested 等由 control plane 仅以 run revision CAS 写入；过期 worker永不发布业务结果
                  （at-least-once execution + fencing 下 exactly-once publication/accounting）
恢复/重试       : lease expiry 或可重试 transient failure，且 attempt/deadline/budget 仍允许 → 原子关闭旧 attempt 并转 retry_wait；
                  后续独立 claim 才分配新 attempt_no/fencing_token；否则写类型化终态 run_failure
幂等     : idempotency_key 绑 request_hash；同 key 同 payload 返原 Run，异 payload → 冲突
超时     : 区分排队超时 / 单次执行超时 / 整体 deadline(UTC)；单 attempt 耗时用 monotonic
取消     : cancel_requested 协作取消；保留每次 attempt
RunEvent : append-only 单调 cursor(seq)；SSE 用 Last-Event-ID=cursor 断线续传；WS 命令状态按持久 command cursor 恢复，RunEvent 仍回到 SSE
Run payload 绑: 输入 Artifact + 完整适用 VersionTuple + ExecutionVersionPlan（node/prompt/tool + model catalog/routing policy exact digest）+ policy/schema snapshot + 预算快照；worker 禁从进程当前默认值补齐
Trace carrier : API 创建 Run 时把当前 server span 经 `TraceCarrier.inject` 写入 `RunRecord.dispatch_trace_carrier`；该字段不进 payload/request/artifact hash。每次 claim 后 worker 从 DB authoritative record `extract`，为该 attempt 建 consumer span（重试 attempt 各自为同一远端 parent 的 child），并把实际 trace_id 写 RunAttempt/Event；API/worker 任一重启不丢 parent/flags/state。carrier 缺失或 telemetry 无法导出时按 best-effort 新建/丢弃 telemetry，不改变业务结果
终态     : success UoW 先发布所有领域 Artifact，再发布 immutable `run_result` Artifact（payload=RunResultV1 manifest），
           `RunRecord.result_artifact_id` 永远指 manifest，消费者经 primary/produced IDs 取领域产物；failure/cancel/timeout 指 `run_failure`(RunFailureV1)
审计     : `AuditRecordV2.actor`=实际 executed_by(service/system)，`initiated_by` 保留原 human/service；subject 定位 run/attempt/lease/result，correlation 带 request/run/trace
```

RunKind/version 对应的 payload/result schema 由 §5.5 registry 唯一解释，未知组合 fail-closed。领域 Artifact 与 RunResult manifest 的 blob 都在写事务外完成，Artifact/ObjectRef/lineage + terminal Run/Attempt/Event/Cost/audit 在同一 UoW 发布。多节点 PostgreSQL 下 claim/renew/reclaim/expiry 条件必须在同一事务使用 **DB-authoritative UTC**（或等价单一 time authority），禁止各 worker 用本机 UTC 争 lease；SQLite/测试使用注入式 UtcClock，单 attempt duration 仍只用进程 monotonic clock。

LLM execution mode 不混：`replay` 创建时必须绑定 foundations v0.3 §7 的 run-scoped native 或 verified-import cassette bundle，严格按其中 attempt/shard/route 顺序消费且不在线补录；`record` 创建时 cassette 必须为空，每个已消费 response 先发布 record shard，每次 attempt 关闭发布 attempt bundle，任一 Run 终态发布 run aggregate bundle（0 调用也发布空 bundle），对应 attempt/final RunResult 或 RunFailure 的 VersionTuple/ExecutionIdentity/lineage 指向它；`live` 不产 cassette，Agent 产物 meta 必须 `replayability=online_only`。`not_applicable` 要求 ExecutionVersionPlan、execution identity、prompt/model/agent_graph/cassette 字段全空，不把 checker/sim/migration/DR 伪装成 LIVE；纯确定性结果满足完整 tuple+seed 才可标 `deterministic_recompute`，真实 DR 等只标 `operational_observation`。只有 record/replay 工件可标 `cassette_replay`。

`attempt_no`、fencing epoch 与 `RunEvent.seq` 都由 RunRecord head 的 revision CAS 原子领取并递增（或 PostgreSQL 等价 `UPDATE … RETURNING`），禁止 `MAX(...)+1`；Event insert 必须与触发它的状态变化同一 UoW。SQLite/PG 共享契约测试用多连接并发证明编号无重复、event seq 严格单调且无已提交状态缺事件。`expected_ref=null` 是 CAS 的显式“该 ref 必须不存在”，与字段缺失严格区分并纳入 request/approval digest。

每次 LLM logical call 的 `call_ordinal` 由 current RunAttempt.next_call_ordinal 在 prompt-render publication UoW 中 CAS 领取（从 1 开始），`(run_id,attempt_no,call_ordinal)` 唯一；同 scoped idempotency key 只能幂等返回同 artifact_id/request_hash，异内容即冲突。RECORD response shard 与 REPLAY record 都必须匹配该 link 的 call_ordinal + request_hash；没有被 Agent 消费的 provider response 不得形成 shard。并发调用、崩溃后留下的无 response ordinal 与 transport retry 均不得靠 `MAX+1` 重编号或复用。

**③ RollbackPolicy（exact rollback ExecutionProfile；受策略、可审计的指针重指）** — 这里的 RollbackPolicy 就是 `profile_kind=rollback` 的版本化 ExecutionProfile，不再另造一套平行 policy registry。回滚是 **ref 级事件，不写 lineage 回滚边**（内容 lineage 是派生 DAG，`B→A` 边会造环）。

```text
RefTransitionV1 {
  transition_schema_version, transition_id, kind:rollback, ref_name,
  from_ref:RefValue, to_ref:RefValue, approval_item_id,
  actor:AuditActor, initiated_by:AuditActor?, request_id, occurred_at
}
# transition_id = "ref-transition:sha256:" + sha256(canonical_json(payload excluding transition_id))；append-only，与 ref_history/audit 同事务

rollback(name, target_artifact_id):
  同一事务视图检查: rollback_request ApprovalItem 已批准且 revision 匹配 & exact rollback profile binding 与 subject/target/validation Run 一致
                  & history 成员 & artifact 存在 & schema/DSL 可读 & 当前 ref revision & RBAC/审批策略
  执行: rollback ApprovalItem CAS→applied + RefStore.compare_and_set(name, 当前, target) + ref_history
        + RefTransitionV1 + audit；若绑定原 patch/constraint 发布，则其 item CAS→rolled_back
  失败: fail-closed（PatchRejected 同族异常）
```

RollbackRequest Artifact、RollbackTargetBinding、validation Run 的 `resolved_profiles[/params/rollback_profile]` 三处 binding 必须逐字段相等；创建 draft 时解析并冻结，validation/submit/apply 从保留的 exact catalog snapshot 重验。这样“RollbackPolicy”只有 ExecutionProfile 一份权威配置，不新增第二真相源。

### 3.4 身份与 RBAC 契约（M4a 定契约 + 授权引擎；认证实现在 M4c）

```text
PrincipalKind        = human | service | system     # system 永不从 HTTP 凭据认证，仅可信组合根创建
Role                 = content_designer | numeric_designer | qa | tooling | constraint_admin | gacha_compliance_reviewer | identity_admin
PrincipalRecordV1    { principal_schema_version:principal@1, principal_id, kind, display_name,
                       status:active|disabled, credential_epoch, authz_revision, revision,
                       created_at, updated_at, disabled_at?, disabled_reason? }
RoleAssignmentV1     { assignment_schema_version:role-assignment@1, assignment_id, principal_id, role,
                       scope:DomainScope|all|null, status:active|revoked, revision,
                       granted_at, granted_by:AuditActor, revoked_at?, revoked_by?, revoke_reason? }
Principal            { id, kind, display_name, status, revision, credential_epoch, authz_revision,
                       roles:[RoleAssignmentV1...] } # current record + active assignments 的服务端投影，非独立可变真相
DomainId             = 版本化 DomainRegistry 中的稳定 opaque ID（非仅 Aureus/Flare 枚举）
DomainDefinitionV1   { domain_id, display_name, description?, parent_domain_id?, tags[], status:active|deprecated }
DomainRegistryRefV1  { registry_version, registry_digest }
DomainRegistryV1     { registry_schema_version:domain-registry@1, registry_version,
                       definitions:[DomainDefinitionV1...], registry_digest }
DomainScope          { domain_ids:[DomainId...] }  # 非空、canonical 稳定排序去重；跨域是集合，不存在隐式 primary domain
Permission           { action, resource_kind, domain_scope:DomainScope|all|null } # null=非域资源，all=显式全域 grant；结构化，非解析字符串
RolePolicy           { policy_version, domain_registry_ref:DomainRegistryRefV1,
                       grants:{Role:[Permission...]}, effective_from, policy_digest } # identity_admin 含 identity.manage
AuthenticationContext{ mechanism:session|api_key|trusted_internal, credential_id? } # credential_id 是持久凭据记录 ID，绝非 secret
ActorContext         { principal, authentication, session_id?, request_id }          # human browser 可带 session；service/system 无浏览器 session
                                                                                     # 组合根仍生成 request_id；流经平台操作并写 audit actor
DomainRouteRule      { rule_id, domain_selector:DomainScope|all, subject_kinds[], route_role:Role,
                       required_action, resource_kind, min_approvals, distinct_from_rule_ids[] }
DomainRoutePolicyRefV1 { route_version, route_digest, domain_registry_ref:DomainRegistryRefV1 }
DomainRoutePolicy    { route_version, domain_registry_ref:DomainRegistryRefV1,
                       rules:[DomainRouteRule...], effective_from, route_digest }
默认配置: 经济/数值→numeric_designer · 叙事→content_designer · 抽卡→gacha_compliance_reviewer · 结构→tooling；其他游戏/域只增配置与 DomainRegistry
授权引擎(platform/rbac，纯函数): authorize(actor, permission, resource) -> allow|deny
  # 加载真实资源后判定；principal 跨 roles 的 grants 可合并，但 requested DomainScope 的每个 DomainId 都必须被同 action/resource grant 覆盖（all-of）
```
> `gacha_compliance_reviewer` 是内建审批职责（现有人员可兼任，非商业合规系统）——PRD §5.4 明确"抽卡→合规角色"。

`DomainRegistryV1.definitions` 按唯一 `domain_id` 排序；parent 必须存在且无环，deprecated 只禁止新资源选择，不使历史 Artifact/Approval 无法读取。`registry_digest=sha256(canonical_json(payload excluding registry_digest))`；Role/Route policy digest 同理排除自身并把 grants/rules canonical 排序。所有 registry/policy 历史版本在引用它的 Artifact、Approval、audit 或 replay 保留期内不可删除；DomainScope、RoleAssignment.scope、Permission 与 route rule 必须只引用 exact registry 中的 ID，未知 ID fail-closed。ApprovalItem.domain_registry_ref、route_policy.domain_registry_ref 与 subject/target 的 domain registry 必须逐字段相等。`RoleAssignmentV1` 的 active 唯一键是 `(principal_id,role,canonical_scope)`；grant/revoke 与 principal `authz_revision` 递增同一 UoW，disable 同时递增 `credential_epoch/authz_revision/revision`，使认证缓存与授权缓存立即失效。其他游戏只增加 domain definitions、profile 与 route 配置，不增加平台枚举或游戏专用判断。

授权覆盖按每个 active `(RoleAssignment,RolePolicy grant)` 先求 scope 交集再合并：`all ∩ X = X`，两个 DomainScope 取 domain ID 集合交集，空交集不授予；`null` 只匹配同为 null 的**非域资源**，绝不等价 all，也不能与 DomainScope 混用。requested DomainScope 的每个 ID 必须被至少一个同 action/resource 的有效交集覆盖，允许多个 assignment/role 合并覆盖，但不能用一个 scoped assignment 放大 policy 的 all grant。requested non-domain permission 则只由 assignment.scope=null + grant.domain_scope=null 覆盖。授权决策、列表过滤、decision/apply 重验都使用同一纯函数与 exact current Principal assignments/RolePolicy，不得各自实现近似规则。

### 3.5 字段级 SnapshotDiff + 三方冲突/合并

```text
diff_snapshots(base_id, target_id) -> SnapshotDiff        # snapshot↔snapshot API，非仅 patch 驱动
SnapshotDiff: 稳定 JSON Pointer；显式区分 MISSING vs JSON null；比较**完整 canonical 对象**（type/schema_version/source_ref/relation endpoints，非仅 attrs）；
              数组默认有序，仅 Schema Registry 声明为集合的路径按 identity 排序

JsonValueState     { presence:missing|present, value? }
MergeConflict      { id, path, kind, base:JsonValueState, current:JsonValueState, proposed:JsonValueState,
                     allowed_resolutions:[keep_current|take_proposed|custom] }
ConflictSet        { schema_version, id, base_snapshot_id, current_snapshot_id, proposed_patch_artifact_id,
                     expected_ref_revision, conflict_count:1..1024, non_conflicting_ops_digest, created_at }
ConflictResolution { conflict_id, choice, custom_value? }
RebaseResult       { status:clean|conflicted, new_patch_artifact_id?, conflict_set_id? }
```

三方计算固定为 `base / current / proposed`；ConflictSet 不可变，conflicts 按稳定 JSON Pointer cursor 分页。数组合并沿用 Schema Registry identity，未声明集合的数组不做猜测式集合合并。`rebase/resolve` 每次都重验当前 ref revision；成功必须创建带 `supersedes_artifact_id` 的**新 Patch Artifact**，推进 SubjectHead、重跑 validation/regression 并重新审批，旧 ApprovalItem 与 auto-apply proof 永不继承。非冲突 op 可确定性搬运；冲突项必须显式选择，禁止 LLM 自动裁决。

### 3.6 审计硬化（tamper-**evident**，非 proof）

`AuditGate` 包装 `platform/audit`：M4 新记录写地基 v0.3 的 `audit@2`。`actor:AuditActor` 是实际执行者（来自当前 `ActorContext`；worker 任务通常是 service/system），人与执行者不同时用 `initiated_by:AuditActor` 保留原始 human/service；`subject:AuditSubject` 必须精确定位 artifact/ref/run/approval/lease/budget/session 等权威资源，`correlation:AuditCorrelation` 承载 request/run/trace 关联，不依赖可丢 telemetry 才能解释操作。保留 `audit@1` reader，旧字符串 actor/可选 artifact_id 不伪造身份、subject 或 correlation 升级。每次 append 在事务内 **CAS/锁定 audit head、仅验前驱链接**；**全链 `verify_chain()` 只在启动/特权操作/定期任务/显式命令**（避免每 append 全扫的 O(n²)）；链断 fail-closed。**诚实注明**：无外部可信 head 时哈希链**不能证明尾截断**未发生；真抗篡改（DB-WORM/object-lock/外部锚定）归 M4e §7.C。

### 3.7 M4a 显式延后

PostgreSQL/S3 真实适配器 + 容器契约测试（M4e）、认证/session/中间件（M4c）、真 tamper-resistant WORM + solver 进程隔离（M4e）、可观测/成本（M4b）。

---

## 4. M4b — 可观测 + 成本治理 + 可靠性

### 4.1 关联与传播契约（附加约束2）

```text
RunCorrelation { run_id, attempt_no? }        # 业务关联，可入任务 envelope；不进 spine/canonical hash
TraceContext   { trace_id, span_id, trace_flags, trace_state? }   # W3C 传播；TraceCarrier.inject/extract 跨 API/队列/worker
spine 只产确定性 producer_run_id；编排边界在 spine 调用外层开 span 并记 producer_run_id 为 span 属性做关联
```

SQLite `RunRecord` 是本地/默认 worker 队列的唯一权威 carrier：API 只把当前服务端 TraceContext 注入有界 `RunDispatchTraceCarrierV1{traceparent,tracestate?}`，与 Run/initial Event/audit 同 UoW 持久化；worker 每次 claim 后从该 DB 字段提取远端 parent，再创建 attempt consumer span。carrier 是 operational metadata，明确排除于 `RunPayloadEnvelope`、`request_hash`、`payload_hash`、Artifact payload/meta/canonical hash；HTTP 入站 header 先由 tracer 验证，客户端不能直接写 RunRecord carrier。这样 API/worker 分进程、通知丢失和任一进程重启后仍保持父子传播；telemetry invalid/drop 仍遵循 best-effort，不阻断 Run。

### 4.2 Telemetry（有界 best-effort）

```text
SpanData(不可变){ trace_id, span_id, parent_span_id?, name, attributes, links, events, status, error, resource, schema_version,
                  started_at(UTC), ended_at(UTC), duration_ns(monotonic差值) }
                  attributes/events/resource 只接受 allowlisted primitive/有界数组，限制 key 数、单值与总字节；超限丢弃并计 dropped，不把大对象转存 telemetry
TimeRangeV1 { start_utc, end_utc }
TraceQueryV1 { run_id?, service?, status?, time_range:TimeRangeV1, cursor?, limit }
TraceSummaryV1 { trace_schema_version, trace_id, root_span_id?, run_ids[], started_at, ended_at?,
                 duration_ns?, status, span_count, service_names[], truncated }
TraceSummaryPageV1 { page_schema_version, items:[TraceSummaryV1...], next_cursor?,
                     coverage_start, coverage_end, truncated }
SpanViewV1 { span:SpanData, redacted_attribute_keys[], redacted_event_fields[] }
SpanPageV1 { page_schema_version, trace_id, items:[SpanViewV1...], next_cursor?, truncated }
可注入: IdGenerator · Sampler · SpanProcessor · SpanExporter；异步上下文用 contextvars
SpanExporter : InMemory(测试断言) + File(NDJSON,CI diff) + OTLP 适配器接口(M4e)
              REPLAY span 记本次真实执行耗时；cassette 原始时延单列 recorded_provider_latency_ms，不冒充 span duration
MetricSink   : readiness 冻结 exact MetricDescriptorRegistryRefV1，并以其中的 MetricDescriptorRefV1 获取 Counter.add / Histogram.record / Gauge.set（发射原始 numerator/denominator；BDR/FP/Fix-Pass 在查询层聚合，避免"平均比例"错）
              MetricDescriptor 冻结 type/unit/label keys/histogram buckets；**禁 run_id/span_id/artifact_id/principal_id 进 label**（高基数→trace/log）；registry + descriptor 级/全局 series 上限
              **不复用** bench 的 Binary/DistributionMetric（那是带 planned_n/CI/evidence 的不可变报告，非时序点）
MetricDescriptorRegistryRefV1 { registry_version, registry_digest }
MetricDescriptorRefV1 { metric_name, descriptor_version, descriptor_digest }
MetricDescriptorV1 { descriptor_schema_version:metric-descriptor@1, metric_name, descriptor_version,
                     metric_type:counter|histogram|gauge, unit_schema_version:metric-units@1, unit, label_keys[], histogram_bucket_bounds[],
                     series_limit, descriptor_digest }
MetricDescriptorRegistryV1 { registry_schema_version:metric-descriptor-registry@1, registry_version,
                             descriptors:[MetricDescriptorV1...], global_series_limit, registry_digest }
MetricLabelMatcherV1 { key, operation:eq|in, values[] }
MetricPointV1 { point_schema_version:metric-point@1, point_id, descriptor:MetricDescriptorRefV1, metric_type:counter|histogram|gauge,
                ts_utc, value, labels{} }
MetricQueryV1 { query_schema_version:metric-query@1, descriptor_refs:[MetricDescriptorRefV1...], time_range:TimeRangeV1, resolution_s,
                label_matchers:[MetricLabelMatcherV1...], max_points, cursor?, series_limit }
ScalarMetricSampleV1 { ts_utc, value }
HistogramMetricSampleV1 { ts_utc, count, sum?, cumulative_bucket_counts[] }
MetricSeriesV1 { descriptor:MetricDescriptorRefV1, metric_name, metric_type, unit, labels{}, bucket_bounds[]?,
                 scalar_points:[ScalarMetricSampleV1...]?, histogram_points:[HistogramMetricSampleV1...]? }
MetricPageV1 { page_schema_version, series:[MetricSeriesV1...], next_cursor?,
               coverage_start, coverage_end, effective_resolution_s, truncated }
StructuredLogger: JSON 行 { ts(UTC), level, msg, service, event_name, schema_version, request_id?, run_id?, trace_id?, span_id?, producer_run_id?, 结构化异常 }
                 **默认禁记**密钥/完整 prompt/raw response/大对象正文
LogRecordV1 { log_schema_version, log_id, ts_utc, level, message, service, event_name,
              request_id?, run_id?, trace_id?, span_id?, producer_run_id?, fields{} }
TraceQueryStore : put(SpanData) · get(trace_id,span_id) · query_traces(TraceQueryV1)->TraceSummaryPageV1 · page_spans(trace_id,cursor,limit)->SpanPageV1；span 数/时间窗/返回字节有硬上限
LogQueryStore   : append(LogRecordV1) · query(LogQuery)->LogPage；MetricQueryStore: record(MetricPointV1) · query(MetricQueryV1)->MetricPageV1
本地实现        : InMemoryTelemetryStore(单测) + LocalTelemetryStore(独立 SQLite WAL，API/worker 跨进程与重启可读、稳定排序、版本化 retention)；
                  File/NDJSON exporter 仍用于 CI diff。M4e Tempo/Loki/Prometheus query adapter 实现同一读契约
故障语义: telemetry exporter 有界 best-effort，失败记 dropped-telemetry 指标但**不改业务结果**；
         Audit(§3.6) 与 CostLedger(§4.3) **不可丢、fail-closed**，不套 best-effort
```

`MetricDescriptorV1.descriptor_digest=sha256(canonical_json(payload excluding descriptor_digest))`；descriptors 按唯一 `(metric_name,descriptor_version)` 排序，`registry_digest=sha256(canonical_json(payload excluding registry_digest))`。label keys stable unique；`metric-units@1` 是本文冻结的 closed canonical allowlist `{1,count,ratio,token,request,step,ns,ms,s,byte}`，新 unit 必须 bump `unit_schema_version`，不引用悬空外部 registry；series limit 为正。histogram bounds 只对 histogram 非空且必须是非空、严格递增的有限数，counter/gauge 必须为空。每个 MetricPoint 必须按 exact name/version/digest 解析保留的 historical descriptor，`metric_type` 相等，labels key 集必须与 descriptor 声明**完全相等**，禁止的高基数 key 即使被声明也使 registry readiness 失败；counter value 必须是非负有限 delta，gauge/histogram value 必须有限。`point_id` 在 store 全局唯一：同 ID + 同 canonical payload 幂等，异 payload 为 `IntegrityViolation`。未知 ref、digest/type 不符或超 descriptor/global series limit 直接拒绝并计受控 dropped 指标。descriptor/registry 历史版本保留到所有 raw point/rollup、SLO/Alert、保存查询和活跃 cursor 均过期；rollup 也携带 exact descriptor ref，持久 point/query 永不解析 mutable current alias。查询必须给按 `(metric_name,descriptor_version,descriptor_digest)` stable-unique 的 exact descriptor refs，禁止仅按同名 metric 跨版本混聚合。

Trace summary/span 稳定按 `(started_at,trace_id|span_id)`、metric series 按 `(descriptor.metric_name,descriptor.descriptor_version,descriptor.descriptor_digest,canonical_labels)`、原始 point 按 `(ts_utc,point_id)` 排序；所有 ID/labels/attributes 与 page cursor 都经 allowlist、RBAC 与字段脱敏。matcher 按 key 排序且每 key 至多一条，`eq` 恰有一个值，`in` 非空且 values stable unique；只能引用所查询 descriptors 共同声明的 label key。time range 采用 `[start_utc,end_utc)`，resolution bucket 从 Unix epoch 整数倍边界对齐：counter 对 bucket 内 delta 求和，gauge 取 `(ts_utc,point_id)` 最后一项，histogram 把 raw observation 按 exact descriptor bounds 聚合为 cumulative counts；histogram `sum` 仅在全部输入可求和时返回，否则为 null。空 bucket 不补零、不插值；不同 descriptor ref 或 canonical label set 永不混合。

time range、limit、series_limit、max_points、resolution、单 series 点数和总返回字节均有服务端硬上限；请求超限返回 `query_too_broad→422`，不得静默改 resolution 或截成“完整”结果。cursor 绑定 exact descriptor refs、canonical query、authz fingerprint 与 retention snapshot。counter/gauge 只允许 `scalar_points` 且无 bucket bounds；histogram 只允许 `histogram_points`，`MetricSeriesV1` 的 name/type/unit/bounds 必须逐字段等于 exact descriptor，每个 sample 的 cumulative counts 长度恰为 `len(bounds)+1`（末项为 +Inf/总 count）、非负单调且末项等于 count，sum unavailable 时为 null而非 0。客户端不能传任意 bucket 或混合两种 point union。TraceSummary/TraceSummaryPage/SpanPage/MetricDescriptorRegistryV1/MetricPageV1 是 API/TS wire 的唯一 DTO，store adapter 不得暴露 Tempo/PromQL/Loki 私有结构。

### 4.3 成本作为控制（cost-as-control）

```text
CostDimension = input_token | output_token | cache_read_token | cache_write_token | request | agent_step |
                wall_time_ns | concurrent_run | monetary
CostAmount { dimension, value, unit, currency? }
Budget {
  budget_schema_version, budget_id, scope_kind:run|principal|system, scope_id, policy_version,
  limits:[CostAmount], reserved:[CostAmount], consumed:[CostAmount], status:active|exhausted|closed,
  revision, deadline_utc?, created_at
}
BudgetSnapshot { snapshot_schema_version, snapshot_id, budget_id, scope_kind, scope_id, policy_version,
                 budget_revision_at_freeze, limits, reserved, consumed, captured_at }
BudgetSetSnapshot { set_schema_version, budget_set_snapshot_id, run_id, selection_policy_version,
                    snapshots:[BudgetSnapshot...], captured_at }  # run/principal/system 全部适用预算的冻结成员集；不是共享 Budget 的永久 revision 锁
ReservationGroup {
  group_schema_version, reservation_group_id, scope:run_budget_hold|attempt_call|agent_step, run_id,
  budget_set_snapshot_id, parent_hold_group_id?, attempt_no?, request_hash, transport_attempt?, fencing_token?, idempotency_key,
  budget_reservation_ids[], status:reserved|reconciled|held_unknown|conservatively_settled|late_reconciled|released,
  revision, created_at, expires_at?
}
BudgetReservation {
  reservation_schema_version, reservation_id, reservation_group_id, budget_id, reserved:[CostAmount],
  status:reserved|reconciled|held_unknown|conservatively_settled|late_reconciled|released, revision
}
UsageEntry {
  usage_schema_version, usage_id, reservation_group_id, budget_reservation_ids[], scope, run_id, attempt_no?, request_hash, transport_attempt?,
  execution_source:online|full_response_cache|cassette_replay, provider_prefix_cache:CacheHitObservation, retry_index,
  token_usage:TokenUsageObservation, latency:LatencyObservation, wall_time_ns, monetary:MonetaryObservation,
  routing_decision_kind:native|legacy_import|null, routing_decision_id?, fencing_token_at_reserve?, adjustment_of_usage_id?, recorded_at
}
PermitGroup { group_schema_version, permit_group_id, budget_set_snapshot_id, run_id, lease_id, fencing_token, permit_ids[],
              status:active|released|expired, revision, acquired_at, expires_at }
ConcurrencyPermit { permit_schema_version, permit_id, permit_group_id, budget_id, run_id, lease_id, fencing_token,
                    status:active|released|expired, revision, acquired_at, expires_at }
CacheHitObservation { status:reported|unavailable, hit? }  # unavailable 与真实 false 严格区分
MonetaryObservation { status:reported|unavailable, amount?, currency?, price_book_version?, quote_effective_at? }
  reported 必须来自 provider/model/observed_at 精确命中的 PriceQuote，amount/currency/version/effective time 全填；unavailable 时金额为空，unknown≠0
CostLedger(持久，SQLite/M4b → PG/M4e): freeze_budget_set · reserve_many · reconcile_group · settle_unknown_group · late_reconcile_group · close_hold_group · acquire/renew/release_permit_group
  `concurrent_run` 是**仅 permit**的瞬时容量维度：只允许出现在 Budget.limits，不进入 Budget.reserved/consumed、ReservationGroup/BudgetReservation/UsageEntry；
  active ConcurrencyPermit 数是唯一占用真相源。run_budget_hold 明确排除该维度；worker 每次 claim 才为当前 lease 对全部适用 scope 原子 acquire PermitGroup，
  attempt 关闭/lease 过期/Run 终态即按 fencing CAS release/expire，retry 的新 claim 必须重新获取，绝不由 hold 与 permit 双重占位
  freeze_budget_set + 建立 run_budget_hold 在同一 ledger UoW 锁定当前 Budget 行并**仅在此时**复核 budget_revision_at_freeze，
  与 RunRecord/初始 Event/audit 同事务提交；BudgetSetSnapshot 此后冻结成员、策略与创建时证据，不要求共享 principal/system Budget 永远停在该 revision
  创建 hold 与 acquire PermitGroup 均对快照中的全部适用 run/principal/system budgets **全成全败**；任一 current budget 超额/关闭/OCC 冲突不留下部分 Reservation/Permit。
  group idempotency 绑定成员 budget IDs、parent hold reservation IDs、request hash 与金额；revision 只作首次 CAS 前提，已存在同 key/group 的重放按持久记录返回，
  不因其他 Run 合法推进共享 Budget revision 把同请求误判为异 payload
  所有多 budget 操作按 `(scope_kind,budget_id)` 稳定顺序锁定/更新，防死锁；创建 hold/permit 读取当时 current revision，子划拨则校验 parent hold member identity + current reservation revision/余额
  UsageEntry 只记录一次真实调用 observation；reconcile_group 把同一 usage 原子计入每个 scope 的 BudgetReservation，查询不得跨 scope 相加后冒充调用总成本
  run_budget_hold: parent_hold/attempt_no/transport_attempt/fencing_token 全为 null，在 Run create 时按版本化策略给出的本 Run 硬上限对全部适用 budgets 建立外层保留；
  agent_step: parent_hold_group_id/attempt_no/fencing_token 必填、transport_attempt=null；attempt_call: parent_hold_group_id/attempt_no/transport_attempt/fencing_token 全必填且为正/非空；禁止 0 sentinel
  调用/step 前原子 reserve_many：子 group 只从同一 BudgetSetSnapshot 的 parent hold **未分配余额**中划拨，不把 Budget.reserved 再加一遍，
  也不再拿 budget_revision_at_freeze 对共享 Budget 做永久 CAS；parent reservation revision/成员不匹配才是真冲突；
  无法精确估算则划拨配置上界，hold 余额不足即 fail-closed；新 reserve/调用必须校验 current fencing/deadline
  调用后按 reservation_group_id + request/attempt/transport 幂等 reconcile；即使 lease 已过期，也必须把已发生 usage 记入原 attempt，
  reconcile 锁 parent reservation 与当时 current Budget 行，以同事务内读取的 current revision 做 CAS/retry，把实际量从 parent hold 的 reserved 原子转入各 Budget.consumed，
  并把未用划拨退回 hold；不能拿冻结 revision 覆盖其他 Run 的合法更新，也不能因 fencing 失败丢账；
  拿不到 usage 进入 held_unknown，由 reaper 保守 settle，不释放未知消耗；Run 终态 close_hold_group 才释放全部未分配余额
  一次 attempt 可有任意多个 agent_step/attempt_call ReservationGroup；RunAttempt 不保存单一 settlement FK，按 `(run_id,attempt_no)` 查询 ledger，
  API 聚合必须同时返回 group 状态分布与 held_unknown/late adjustment，不把部分已结算冒充 attempt 全部结清
  conservatively_settled 后迟到真实 usage 走 late_reconcile：以原 provider usage identity 幂等、append adjustment UsageEntry，
  只按 actual−conservative 差额修正 consumed/reserved（负差释放多留、正差计 overage/可能 exhausted），保留原结算记录且绝不双计
  UsageEntry.routing_decision_kind/id 必须同空或同非空：logical model route 的 online/full-cache/cassette-replay usage 都必须非空并解析 exact variant，
  verified legacy replay 只能是 legacy_import；纯 agent-step/conservative settlement 等不对应模型 route 的 entry 才允许两者都空。late adjustment 逐字段继承原 entry ref
PriceBook Protocol:
  lookup(provider, model_snapshot, observed_at_utc)->PriceQuote|PriceUnavailable
  PriceQuote { price_book_version, provider, model_snapshot, effective_from, effective_to?, currency,
               rate_unit, input_rate, output_rate, cache_read_rate?, cache_write_rate? }
货币配额: monetary_status="unavailable"，仅 PriceBook provider/model/时间有效区间**精确匹配**才启用；跨币种不隐式换算，无匹配不声称某 tier"更便宜"
usage 规范化: 类型化 TokenUsageObservation / LatencyObservation / CacheHitObservation 带 reported|unavailable（unknown≠0、unknown hit≠false）；覆盖 in/out/cache-read/cache-write/total
             分别计数 logical / full-response-cache / cassette-replay / provider-prefix-cache / transport-attempt / retry
版本兼容: foundations v0.3 §7 的 wire union 是唯一真相；M4 新请求/录制只写 `model-router@2`/`cassette@2`，但永久读取现有 `model-router@1`/嵌套 `cassette@1`。
          legacy observation 只按冻结字段存在性/别名映射，routing/cache-hit unknown 保持 unavailable；不得先用新模型 default 补 0 再映射，也不得重写已有 Opus cassette bytes
缓存(严分):
  provider prompt-cache: 复用稳定 KG+约束前缀，**仍发一次模型调用**（降 token 成本）
  本地完整响应缓存      : **仅按完整 request_hash 命中**（现有 session cache 保留）；仅凭前缀 hash 复用完整响应=返错答案，禁止
  REPLAY: cassette 权威，miss **fail-closed**；按 CassetteRecord 的 TokenUsageObservation/recorded provider latency 重演预算，
          observation unavailable 时按配置上界保守预留/settle，绝不补 0；本地 replay duration 只记执行 span，不冒充 provider latency
ModelDescriptor { provider, model_snapshot, tier, capabilities[], context_limit, max_output_tokens,
                  prompt_cache_support, status:active|disabled }
ModelCatalogSnapshot { catalog_schema_version, catalog_version, models:[ModelDescriptor], created_at, catalog_digest }
RoutingRule { rule_id, task_kind, domain_scope?, required_capabilities[], primary_model_snapshot, allowed_fallback_chain[], budget_predicates[] }
RoutingPolicy { routing_schema_version, policy_version, catalog_version, catalog_digest,
                rules:[RoutingRule], failure_classifier_version, routing_policy_digest }
RoutingDecision { decision_schema_version, decision_id, run_id, attempt_no, request_hash, rule_id, model_snapshot, tier, reason_code,
                  budget_set_snapshot_id, fallback_from?, fallback_index, policy_version, routing_policy_digest,
                  catalog_version, catalog_digest,
                  execution_source:online|full_response_cache|cassette_replay, decided_at }
路由: native M4 调用每 route 持久化 RoutingDecision；换模只走规则中的显式 fallback chain；**REPLAY 重现 cassette 已记录选择**，不临时换模。
      verified `cassette@1` import 没有历史 M4 route，按 foundations v0.3 §7 持久化并引用内容寻址的 LegacyImportRoutingDecisionV1，
      `routing_decision_kind=legacy_import`，绝不另造 native RoutingDecision 冒充历史 rule/tier/budget/reason；Usage/ExecutionIdentity 都用同一 kind+ID。
      PriceBook 无 provider/model/有效期精确匹配时，不用 tier 名称声称更便宜，本地 monetary 默认 unavailable
```

`ModelCatalogSnapshot.models` 按唯一 `(provider,model_snapshot)` 排序，descriptor 的 capabilities stable unique；`catalog_digest=sha256(canonical_json(payload excluding catalog_digest))`。RoutingPolicy.rules 按唯一 `rule_id` 排序；同 task/domain 可有多条按有界 budget predicate 精确匹配的规则，但任何输入最多命中一条，否则 readiness fail-closed。fallback chain 保留有语义的顺序且成员唯一，其余 allowlist canonical；`routing_policy_digest=sha256(canonical_json(payload excluding routing_policy_digest))`。catalog/policy 历史版本在任一 Run/Usage/RoutingDecision/Artifact/cassette 保留期内不可删除；native ExecutionVersionPlan/RoutingDecision/REPLAY record 与 registry object 的 version/digest/模型选择必须全相等，不能只按 current version 或 tier 名称解析。legacy-import 分支则必须按 foundations v0.3 §7 的 exact wire/request/profile/catalog/verification-policy/digest 闭包解析，不能套 native RoutingPolicy 等式，也不能降级为裸 decision ID。

`model_snapshot` 是 provider-qualified 的稳定 opaque ID（例如由 provider namespace + served snapshot 构成），全 catalog 全局唯一；`ModelDescriptor.provider` 必须与其 namespace 相等。同一 ID 出现在不同 provider、RoutingRule/ExecutionVersionPlan 引用不存在的 ID或 cassette record 的结构化 ModelSnapshot 不能规范化为同一 ID都使 registry/readiness fail-closed。VersionTuple/ExecutionIdentity 的 model member 始终使用该 canonical ID，PriceBook 仍以 provider + canonical ID 双匹配，不能仅按展示模型名路由或计价。

### 4.4 可靠性

```text
retry: 类型化 failure classifier，只重试明确 transient + 幂等请求；受总 deadline / Retry-After / 预算约束；clock/sleeper/jitter 注入；每 attempt 计费 + 建 span
CircuitBreakerState: closed → open → half_open → closed|open
CircuitBreaker: 版本化配置冻结 rolling window/min samples/failure threshold/open cooldown/half-open max concurrent probes/success threshold；
                **只由 failure classifier 标记的外部/基础设施故障计数**。closed 达阈值→open；cooldown 后只放有界 probe→half_open；
                probe 全部满足阈值→closed，任一计数故障→open。open 非 probe 调用返回类型化 DependencyUnavailable；
                单约束 solver timeout / unknown/unproven / 验证失败绝不击穿共享 breaker；换模只由 RoutingPolicy 决定
degrade: solver unknown→unproven（现有保留，绝不静默 pass）+ 分层降级模型
timeout/幂等: 来自 M4a RunRecord 三类超时与 idempotency_key
```

### 4.5 SLO + 告警

```text
MetricPredicate { descriptor:MetricDescriptorRefV1, allowed_label_matchers, comparator, threshold, unit }  # exact descriptor，非任意查询字符串或 current alias
SLIDefinition { sli_schema_version, metric_registry:MetricDescriptorRegistryRefV1,
                eligible:MetricPredicate, good:MetricPredicate, total_aggregation:count|sum,
                workload_profile_id, missing_data:exclude|bad|hold, late_data_grace, policy_version }
WorkloadProfile { profile_schema_version, profile_id, dataset_artifact_id, entity_count, relation_count, constraint_count,
                  task_count?, concurrency, environment_fingerprint }
SLODefinition { slo_schema_version, slo_id, name, sli:SLIDefinition, objective, rolling_window,
                minimum_samples, evaluation_interval, effective_from, policy_version }
SLOEvaluation { evaluation_schema_version, evaluation_id, slo_id, window_start, window_end,
                eligible_count, good_count, total_value, ratio?, missing_count, late_count, status:met|breached|insufficient_data }
AlertRule { alert_schema_version, alert_rule_id, slo_id, breach_threshold, for_duration, severity,
            dedup_key_template, cooldown, insufficient_data_action:hold|resolve|fire, policy_version }
AlertInstance { alert_instance_id, alert_rule_id, dedup_key, state:pending|firing|resolved,
                pending_since?, fired_at?, resolved_at?, last_evaluation_id, last_delivery_at?, revision }
Alert 状态机: resolved/不存在 --breach→ pending --for持续 breach→ firing --恢复→ resolved；cooldown 抑制重复投递但不篡改状态，
               missing/late 按冻结 policy 处理；每次迁移 revision CAS，可用 fake clock 回放
AlertSink: deliver(AlertInstance,SLOEvaluation,idempotency_key)->delivered|duplicate|failed；InMemory/File 测试实现现在交付，真实 PagerDuty 等延后
初值: checker/sim/50-任务回归阈值由**已测 baseline** 生成写入版本化配置——无阈值+窗口只叫 metric 不叫 SLO
eligibility: 在线 provider/服务 SLO 只用 ONLINE 本次 duration/provider latency；REPLAY 本地 duration 不混入 provider SLO，
             cassette recorded latency 只用于 replay 成本/bench 与单独标记的历史分布
```

### 4.6 M4b 验收锚点（TDD）

API→DB Run carrier→独立 worker 跨重启 span 父子/flags/state · carrier 不进 payload/request/Artifact hash · fake clock 下 duration · 回放耗时 vs 录制时延分栏 · run/principal/system 多层 reserve/acquire 全成全败、无部分占用且不重复计算调用成本 · child reservation 不把 parent hold 重复计入 reserved · 崩溃/重试后账本不重不丢 · 过期 fencing token 不能新消费/发布但既有 ReservationGroup 必须 reconcile 或保守 settle · 同前缀异后缀不命中响应缓存 · breaker 不把 `unproven` 当基础设施故障 · 高基数 label 被拒 · exporter 故障不改 checker 结果 · SLO missing-data + 告警状态机可回放。

---

## 5. M4c — API 网关 + 执行

### 5.1 认证 / 会话 / 身份（本地实现，OIDC 接口现定实现延后）

```text
SecretText / SessionToken / ApiKeySecret / OidcCode 只允许存在于 transport/request 内存，repr/serialization/logging 一律 redacted，禁止持久化
PasswordAuthRequestV1 { schema_version, login_name, password:SecretText }
ApiKeyAuthRequestV1   { schema_version, api_key:ApiKeySecret }
AuthenticationResultV1 { principal_id, principal_kind, credential_id, credential_version, authenticated_at }

LoginNameNormalizationPolicyV1 {
  policy_schema_version:login-name-normalization@1, policy_version,
  unicode_normalization:NFKC, trim_unicode_whitespace:true, case_mapping:unicode_casefold,
  reject_categories:[control,surrogate,private_use], minimum_codepoints, maximum_codepoints, policy_digest
}
PasswordCredentialRecordV1 { credential_id, principal_id, normalized_login_name,
                             normalization_policy_version, normalization_policy_digest,
                             password_hash, hash_policy_version, credential_version,
                             status:active|disabled, changed_at, revision }
ApiKeyRecordV1 { api_key_id, principal_id, key_prefix, key_digest, credential_version,
                 status:active|revoked|expired, created_at, expires_at?, revoked_at?, revision }
PasswordHashPolicyV1 { policy_version, algorithm:argon2id, memory_kib, iterations, parallelism, salt_bytes,
                       rehash_on_login, effective_from }

SessionPolicyV1 { policy_version, absolute_ttl_s, idle_ttl_s, touch_interval_s, signing_key_set_version,
                  csrf_mode:synchronizer_token, same_site:strict|lax, secure_cookie_required }
SessionRecordV1 { session_id, principal_id, source_credential_id, credential_version, token_digest, csrf_secret_digest,
                  signing_key_id, issued_at, absolute_expires_at, idle_expires_at, last_seen_at,
                  revoked_at?, revoke_reason?, revision }
SessionIssueRequestV1 { principal_id, source_credential_id, credential_version, session_policy_version }
SessionIssueV1 { session_id, session_token:SessionToken, csrf_token:SecretText, absolute_expires_at, idle_expires_at }
SessionContextV1 { session_id, principal_id, source_credential_id, credential_version,
                   issued_at, absolute_expires_at, idle_expires_at, session_policy_version }

OidcBeginRequestV1 { provider_id, redirect_uri_id, return_to_path? }
OidcAuthorizationRedirectV1 { authorization_url, state_handle, expires_at }
OidcTransactionRecordV1 { transaction_id, provider_id, state_digest, nonce_digest, sealed_pkce_verifier,
                          redirect_uri_id, return_to_path?, created_at, expires_at, consumed_at?, revision }
OidcCallbackV1 { provider_id, state:SecretText, code:OidcCode, redirect_uri_id }
OidcIdentityV1 { issuer, subject, email?, display_name?, claims_digest, provider_id }

IdentityAuthenticator.verify_password(PasswordAuthRequestV1)->AuthenticationResultV1 | AuthError
ApiKeyAuthenticator.authenticate(ApiKeyAuthRequestV1)->AuthenticationResultV1 | AuthError
SessionManager.issue(SessionIssueRequestV1)->SessionIssueV1
SessionManager.resolve(SessionToken,csrf_token?,request_method)->SessionContextV1 | AuthError
SessionManager.revoke(session_id,expected_revision,reason,actor)->SessionRecordV1
OidcProvider.begin(OidcBeginRequestV1)->OidcAuthorizationRedirectV1
OidcProvider.complete(OidcCallbackV1)->OidcIdentityV1 | AuthError
AuthError = AuthFailed | CredentialDisabled | CredentialExpired | SessionExpired | SessionRevoked | OidcStateInvalid

human → 密码/OIDC 成功后发行 session；service → API key；system → 仅可信组合根创建，永不从 HTTP 凭据认证。对外登录失败统一脱敏为 `auth_failed`，
内部 typed reason 只进受限 audit/metric，不能枚举账号。Session token 是带 key_id 的签名 opaque token且 DB 只存摘要；CSRF token 与 session digest 绑定；
签名 key set/version 来自 secret manager，轮换期仅接受 policy 明确的 active+grace keys，绝不把 key 放 DB。绝对/idle expiry 都检查，touch 按间隔 CAS；
resolve API key/session 时必须联查当前 Principal status、credential version/status 和当前 roles，principal disabled 或 credential version bump 后既有凭据立即失效，
不信 token 中的 roles。密码按当前 Argon2id policy 成功登录后可原子 rehash；API key 明文/session/CSRF 只显示或返回一次。
浏览器: HttpOnly + Secure + policy SameSite cookie；所有非 safe method 校验 CSRF；WS 校验 Origin 并在每条命令重做 session/authz。
provisioning: 可信 `gameforge identity` CLI 也调用同一 platform service/UoW（禁止手改 DB）。`bootstrap` 仅在 identity store 为空时以唯一约束/CAS
              创建首个 human principal 并赋 `identity_admin + tooling` roles；权限来自当前 RolePolicy，竞态只有一个成功。
              后续 create/disable principal、grant/revoke role、issue/revoke API key/session 均要求当前 actor 持相应 Permission并写 audit；
              system principal 仍只能由可信组合根创建，不能 bootstrap 成 HTTP credential。OIDC transaction 单次消费；state/nonce/PKCE/redirect allowlist 全校验
```

身份库的权威 DTO 是 §3.4 `PrincipalRecordV1 + RoleAssignmentV1`，`Principal` 只在每次认证/授权时投影。`LoginNameNormalizationPolicyV1.policy_digest=sha256(canonical_json(payload excluding policy_digest))`；历史 policy 永久保留，credential 逐字段绑定 exact version/digest。`normalized_login_name` 按该 policy 计算并全局唯一；认证按 credential 所绑 policy规范化输入，升级 policy 必须以 CAS 显式迁移并先证明全量唯一性，不能静默重算造成别名碰撞。human 才可持 password/OIDC binding，service 才可持 API key，system 不得有任何 HTTP credential。create/disable principal、grant/revoke role 与 credential/session 变更均使用 expected revision CAS，并在同一 UoW 更新 principal `credential_epoch/authz_revision`、append audit；查询或缓存发现 epoch/revision 不同必须重新加载，不能继续信旧 Principal 投影。

### 5.2 授权与中间件顺序

```text
authn 中间件建 ActorContext；资源级 `{action,resource_kind,domain_scope}` 在 endpoint 依赖/平台 service **加载真实资源后**判定
列表接口按权限**过滤数据读**；变更操作在 UoW 内**再执行平台守卫**（API 非唯一防线）
cost **移出 HTTP 中间件链**（GET 不耗 Agent budget）；reserve 在建 Run / worker / model / agent-step 边界
冻结顺序: request-id/trace/error-wrapping → authn/CSRF → endpoint authz → handler（用测试锁定 Starlette 实际执行顺序）
```

### 5.3 REST 面 + 错误契约

`/api/v1` 全端点；同步非 2xx 统一为 RFC 9457 `application/problem+json`：

```text
Problem { type, title, status, detail, instance, code, request_id,
          run_id?, trace_id?, errors?, retry_after_s?, earliest_cursor?, conflict_set_id? }
ApprovalDecisionRequestV1 { request_schema_version, decision:approve|reject|request_changes,
                            requirement_ids[], expected_workflow_revision, reason_code, comment? }
ApprovalRequirementProgressV1 { requirement_id, domain_scope, route_role, min_approvals, valid_approval_count,
                                satisfied, eligible_for_current_actor, unmet_distinct_from_requirement_ids[] }
ApprovalViewV1 { approval:ApprovalItem, requirement_progress:[ApprovalRequirementProgressV1...],
                 current_actor_allowed_requirement_ids[] }
ExecutionProfileKindV1 = generation|patch_repair|constraint_extraction|review|llm_triage|checker|simulation|workload|
                         config_export|task_suite_derivation|environment|playtest_planner|validation|constraint_compiler|
                         rollback|schema_compatibility|impact_analysis|bench_evaluator|artifact_migrator|dr_plan|
                         restore_target|dr_verifier
EnvironmentContractDescriptorV1 {
  env_contract_version, reset_schema_id, action_schema_id, observation_schema_id
}
GenericProfileDetailsV1 { details_kind:generic }
EnvironmentProfileDetailsV1 { details_kind:environment, contract:EnvironmentContractDescriptorV1 }
ConfigExportProfileDetailsV1 {
  details_kind:config_export, target_environment_profile:ProfileRefV1,
  env_contract_version, format_schema_id, package_schema_version:config-export-package@1
}
ValidationProfileDetailsV1 {
  details_kind:validation, subject_kinds:[SubjectKind...],
  auto_apply_policy:AutoApplyPolicyRefV1?
}
MigrationEdgeV1 {
  edge_id, source_kind:ArtifactKind, source_payload_schema_id, target_payload_schema_id,
  target_meta_schema_version, target_dsl_grammar_version?,
  golden_replay_policy:required|not_applicable, golden_fixture_set_digest?, not_applicable_reason_code?
}
MigrationProfileDetailsV1 { details_kind:artifact_migrator, edges:[MigrationEdgeV1...] }
MigrationCapabilityMatrixRefV1 { matrix_version, matrix_digest }
MigrationEdgeCapabilityV1 {
  source_kind:ArtifactKind, source_payload_schema_id, target_payload_schema_id,
  target_meta_schema_version, target_dsl_grammar_version?,
  capability:publish_same_kind|report_only|needs_re_extract|needs_re_compile,
  publication_lineage_policy_ref?:ArtifactLineagePolicyRefV1
}
MigrationKindDefaultV1 { source_kind:ArtifactKind, unsupported_edge_action:reject_409|report_only|needs_re_extract|needs_re_compile }
MigrationCapabilityMatrixV1 {
  matrix_schema_version:migration-capability-matrix@1, matrix_version,
  kind_defaults:[MigrationKindDefaultV1...], edges:[MigrationEdgeCapabilityV1...], matrix_digest
}
MigrationCapabilityMatrixRegistryV1 {
  registry_schema_version:migration-capability-matrix-registry@1,
  matrices:[MigrationCapabilityMatrixV1...], registry_digest
}
ExecutionProfileDetailsV1 = GenericProfileDetailsV1 | EnvironmentProfileDetailsV1 |
                            ConfigExportProfileDetailsV1 | ValidationProfileDetailsV1 | MigrationProfileDetailsV1
ExecutionProfileDefinitionV1 {
  definition_schema_version:execution-profile@1, profile:ProfileRefV1, profile_kind:ExecutionProfileKindV1,
  compatible_run_kinds:[RunKindRef...], domain_scope:DomainScope, stochastic,
  input_schema_ids[], output_schema_ids[], required_capabilities[], display_name,
  handler_key, config_schema_id, config, config_hash, details:ExecutionProfileDetailsV1
}
ExecutionProfileLifecycleV1 {
  profile:ProfileRefV1, state:active|replay_only|disabled, revision, reason_code?, changed_at
}
ExecutionProfileCatalogSnapshotV1 {
  catalog_schema_version, catalog_version, definitions:[ExecutionProfileDefinitionV1...],
  lifecycle:[ExecutionProfileLifecycleV1...], catalog_digest
}
ExecutionProfileViewV1 {
  profile:ProfileRefV1, profile_payload_hash, profile_kind:ExecutionProfileKindV1, status:active|replay_only|disabled,
  compatible_run_kinds:[RunKindRef...], domain_scope:DomainScope, stochastic,
  input_schema_ids[], output_schema_ids[], required_capabilities[], display_name,
  env_contract_version?, target_environment_profile?
}
```

`ApprovalRequirementProgressV1` 是按 ApprovalItem 冻结的 route/policy、不可变 decisions 与**当前**身份/角色计算的服务端投影；`valid_approval_count` 只统计满足 maker-checker、requirement permission 与 distinct 约束的 decision。`eligible_for_current_actor`/`current_actor_allowed_requirement_ids` 只用于界面提示，提交 decision 时平台守卫仍须在同一 UoW 重新加载身份、资源和 workflow revision 后鉴权，客户端不得据此获得权限。

ExecutionProfileDefinition 以 `(profile_id,version)` 唯一；`config` 是受 `config_schema_id` 校验的有界 canonical JSON，`config_hash=sha256(canonical_json(config))`，只允许 secret reference/环境变量名而禁止密钥值。`profile_payload_hash=sha256(canonical_json(definition))`，同 ref 异 definition/config hash 为 `IntegrityViolation`。每个 catalog snapshot 中，每个 definition **恰有一条** lifecycle row，definitions 与 lifecycle 的 ProfileRef 集合必须完全相同；两数组都按 `(profile_id,version)` 稳定排序且 ProfileRef 唯一，同一 snapshot 禁止放同 profile 的多条历史 revision。该 row 就是该 exact catalog snapshot 的权威状态：新 profile 从正整数 revision=1 开始；后续 catalog 中状态/reason 未变则逐字段复制 revision/changed_at，发生变化才 revision+1 并刷新 changed_at，禁止跳号或倒退；历史状态由旧 catalog snapshot 保留。`catalog_digest=sha256(canonical_json({catalog_schema_version,catalog_version,definitions,lifecycle}))`，明确排除 `catalog_digest` 自身。任一 lifecycle 变化创建新 catalog version，不改写历史 snapshot；catalog snapshot 与 definition/config 在所有引用它们的 Run/Artifact 保留期内不可删除。Run 创建时把每个 DTO field path 对应的 `ResolvedExecutionProfileBindingV1` 与 catalog version/digest 冻结，worker 只从该 exact catalog snapshot 读取 definition 内的 handler/config 和唯一 lifecycle row，并复核 profile/config hash，绝不按 profile ID 读取 current 或依赖悬空外部配置。`details` 是按 `profile_kind` 的 JSON Schema `oneOf`：environment 必须且只能用 `EnvironmentProfileDetailsV1`；config_export 必须且只能用 `ConfigExportProfileDetailsV1`，其 environment profile/env contract 必须可解析且一致；validation 必须且只能用 `ValidationProfileDetailsV1`，只有 subject_kinds 含 patch 时允许 non-null auto policy，且 registry/ref 闭包必须可解析；artifact_migrator 必须且只能用 `MigrationProfileDetailsV1`，edges 按唯一 edge_id 排序且 source/target tuple 唯一，golden required 时 fixture-set digest 必填且 reason 为空，not_applicable 时 digest 为空且 reason 必填；其余 kind 用 generic。内部 `handler_key/config` 不经 API 返回，View 只投影无密钥字段。

Profile field→kind 映射同样由 schema 锁死：`generation_policy→generation`、`repair_policy→patch_repair`、`extraction_policy→constraint_extraction`、`review_profile→review`、`llm_triage_policy→llm_triage`、`checker_profile(s)→checker`、`simulation_profile(s)→simulation`、`workload_profile→workload`、`candidate_export_profiles→config_export`、`derivation_profile→task_suite_derivation`、`environment_profile→environment`、`planner_policy→playtest_planner`、`validation_policy→validation`、`compiler_profile→constraint_compiler`、`rollback_profile→rollback`、`schema_compatibility_policy→schema_compatibility`、`impact_profiles→impact_analysis`、`evaluator_profile→bench_evaluator`、`migrator→artifact_migrator`、`dr_plan→dr_plan`、`restore_target_profile→restore_target`、`verification_profile→dr_verifier`。kind、RunKind compatibility、domain、input/output schema 或 capability 任一不符即 409，不能把 planner/environment 等同为任意 ProfileRef。

稳定映射：`invalid_cursor→400`；`auth_required→401`；`forbidden|csrf_failed|origin_rejected→403`；`not_found→404`；`cursor_expired→410`；`payload_too_large→413`；`revision_conflict|idempotency_conflict|workflow_guard|patch_precondition_failed|stale_conflict_set|stale_task_suite→409`；`request_schema_invalid|query_too_broad→422`；`quota_exceeded→429`；`dependency_unavailable→503`；`integrity_violation→500`（完全脱敏并告警）。框架产生的 400/404/405/422 也包装为同一 schema；内部 IntegrityViolation 不泄底层。

| 资源 | 关键端点 |
|---|---|
| auth | `POST /auth/login` `POST /auth/logout` `GET /auth/me` |
| specs/KG | `GET /specs`(分页) `GET /specs/{id}`(snapshot/ref revision、ETag、schema-registry version) `GET /specs/{id}/graph`(分页) `POST /specs` `GET /schema-registry/{version}`；IR 人工编辑一律 `POST /patches` 产 typed Patch draft，**无原地 PATCH Snapshot** |
| constraints | `GET /constraints`(分页) `GET /constraints/{artifact_id}` `GET /constraint-proposals`(分页) `GET /constraint-proposals/{artifact_id}` `POST /constraint-proposals`(human typed draft) `POST /constraint-proposals:propose`→202(agent draft) `POST /constraint-proposals/{id}:revise`(human typed revision) `:validate`→202 `:submit-for-approval` `:publish`；**无原地 PATCH Constraint** |
| generation | `POST /generation:propose`→202；gate 通过时 RunResult primary=`patch@2`，同一 terminal UoW 发布非权威 preview `ir_snapshot`、按非空 export profiles 条件发布 `config_export` 与 gate evidence，并创建 draft ApprovalItem，**只提议不应用**；`generation_gate_rejected` 是非重试业务 RunFailure，只发布 evidence-only rejected Patch/preview/gate evidence，不创建 SubjectHead/ApprovalItem/config export、不改变 ref；客户端从成功 RunResult 跳转 `GET /patches/{artifact_id}` |
| review | `GET /reviews`(分页) `GET /reviews/{review_artifact_id}`（同快照多 tool/policy 版本可多份） `GET /findings`(分页) `GET /findings/{id}`(latest view) `GET /findings/{id}/revisions/{revision}`(immutable exact revision) |
| task suites | `GET /task-suites`(按 config/constraint/environment profile 过滤并分页) `GET /task-suites/{artifact_id}` `POST /task-suites:derive`→202（固定创建 `task_suite.derive@1`，返回 exact suite/scenario Artifacts） |
| playtest | `POST /playtest:run`→202（服务端重验 exact task suite/config/constraint/environment/episodes） `GET /playtest/{run_id}/result`（返回游戏动作轨迹 Artifact；OTel trace 走 `/runs/{id}/traces`） |
| patch/diff | `GET /patches`(分页) `GET /patches/{artifact_id}`（仅有 SubjectHead 的 workflow Patch；rejected evidence-only Patch 只经 RunFailure→`/artifacts/{id}` 读取） `POST /patches` `POST /patches/{id}:repair`→202（Repair Agent 产 superseding exact-base revision） `POST /patches/{id}:validate`→202 `:submit-for-approval` `GET /diff?base&target&cursor&limit` `POST /patches/{id}:rebase` `:resolve-conflicts` `GET /conflict-sets/{id}/conflicts`(分页) `POST /patches/{id}:apply`（绑 workflow revision + exact target Artifact；接受 human `approved` 或重验仍有效的 `auto_apply_eligible` proof） |
| refs/rollback | `GET /rollback-requests`(分页) `GET /rollback-requests/{artifact_id}` `POST /refs/{name}/rollback-requests`(draft) `POST /rollback-requests/{id}:validate`→202 `:submit-for-approval` `:apply`（走 M4a RollbackPolicy；无 create→apply 捷径） |
| artifacts/lineage | `GET /artifacts/{id}` `GET /artifacts/{id}/lineage`(分页) `GET /refs/{name}/history`(分页) |
| approvals | `GET /approvals?assignee=me`(分页，items=`ApprovalViewV1`) `GET /approvals/{id}`→`ApprovalViewV1` `POST /approvals/{id}:approve\|reject\|request_changes`（body=`ApprovalDecisionRequestV1`；human≠proposer；可部分满足 requirements） |
| runs | `POST /runs`(仅 §5.5 `generic_runs_endpoint` kind/version) `GET /runs`(分页) `GET /runs/{id}` `GET /runs/{id}/findings?cursor&limit`（只返该 Run 发布的 exact immutable revisions） `GET /runs/{id}/events`(SSE) `GET /runs/{id}/traces?cursor&limit`→`TraceSummaryPageV1` `GET /runs/{id}/commands?cursor&limit` `WS /runs/{id}/commands` `POST /runs/{id}:cancel`（同样接收 type=`cancel` 的 `RunCommandV1`，与 WS 共用 CommandService） |
| eval/bench | `GET /bench/report`(BenchReport v2) |
| execution profiles | `GET /execution-profiles`(按 kind/run-kind/domain/status 过滤并分页) `GET /execution-profiles/{profile_id}/versions/{version}`→`ExecutionProfileViewV1`；只读、RBAC、无密钥/连接信息；active 可建兼容 Run，replay_only 仅可做下述 exact historical REPLAY，disabled 只可审计读取、不可执行 |
| observability | `GET /traces/{trace_id}`→`TraceSummaryV1` `GET /traces/{trace_id}/spans?cursor&limit`→`SpanPageV1` `GET /logs/query`→`LogPage` `GET /metrics/descriptors`→exact `MetricDescriptorRegistryV1` `GET /metrics/query`→`MetricPageV1`（query 按 §4.2 exact descriptor refs/time range/resolution/允许 label/max_points） `GET /cost/{run_id}`（均有界+RBAC，不返 prompt/raw response） |
| health | `GET /livez`(不访依赖) `GET /readyz`(migration head + DB/ObjectStore/CostLedger + **缓存的最近一次 AuditGate 结果**；启动初始化、定期/特权验证更新，不在每次 probe 全链扫描) |

`LogQuery { start,end,service?,level?,event_name?,run_id?,trace_id?,span_id?,producer_run_id?,cursor?,limit }`；`LogRecordView { schema_version,log_id,ts,level,event_name,message,service,run_id?,trace_id?,span_id?,producer_run_id?,fields,redacted_fields[] }`；`LogPage { items,next_cursor?,coverage_start,coverage_end,truncated }`。服务端限制最大时间窗、limit、返回字节数和 allowlisted filters，按 `(ts,log_id)` 稳定分页；字段级脱敏，密钥、完整 prompt/raw response 永不返回。run/trace 等高基数字段只作查询条件，不变成 metric label。

Profile lifecycle 的执行语义无隐式例外：`live|record|not_applicable` Run 的全部 resolved profiles 必须为 active；`replay` 可使用 active 或 replay_only。使用 replay_only 时，`cassette_artifact_id` 必须是 existing run-scoped bundle并走唯一一个来源分支：native M4 bundle 的 `run_id` 必填且解析到保留来源 Run，新 Run 的 ProfileRef/profile hash、输入 Artifact/hash、ExecutionVersionPlan/VersionTuple、policy/schema snapshot 与来源 Run 逐字段相等；foundations v0.3 §7 的 verified legacy-import bundle 则 `run_id=null`，必须带 `status=verified` 的 `LegacyCassetteRunImportManifestV1`，新 Run 与 manifest 冻结的 exact 输入/profile/version/typed policy+schema bindings/call order/execution identity 逐字段相等，每个 invocation/Usage 都引用同一 `routing_decision_kind=legacy_import` 内容寻址 decision，禁止另造 native decision。`evidence_missing` import 永不允许进入 M4 Run；它只保留 loose legacy direct replay兼容。两分支都要求显式 `{replay,run,domain}` 权限、handler/schema 仍受支持且 bundle lineage闭合；不得伪造历史 Run 或从 current registry补字段。disabled 永不用于新执行（含 REPLAY），但历史 Run/Artifact/cassette 仍可按 RBAC 读取；要保留可重演能力必须把版本置为 replay_only，而不是 disabled。

语义：HTTP request schema 或有界 query policy 校验失败=`422`（如 `query_too_broad`）；检查未过=正常业务结果（例如 `generation_gate_rejected` 是带 evidence 的非重试 RunFailure，validation 未过则是 EvidenceSet + `validation_failed` 工作流状态）；未过却申请应用=409 workflow guard；已 202 的异步失败写 versioned `run_failure` Artifact + 终态 RunEvent，不伪装成 HTTP 错误。

### 5.4 幂等 / OCC / 分页（全命令统一）

除登录外所有 create/revise/repair/validate/submit/approve/reject/rebase/resolve/apply/publish/rollback/cancel/WS-command 用 scoped idempotency key + canonical request hash（异 payload→409）；GET 返 revision/ETag，写命令 `If-Match`/expected revision CAS；所有集合用绑定 filter/sort/read-snapshot 的 cursor 分页（含 specs/runs/reviews/findings/run-findings/patches/proposals/task-suites/execution-profiles、graph 节点/边、diff entries、lineage、conflicts、trace spans、logs），**禁无界 `/graph`、`/diff`、`/traces`、`/logs/query` 与 `/metrics/query`**。解决 ConflictSet 或发起 Repair 时同时校验其绑定的 current subject/snapshot/ref/workflow revision，过期返回 `stale_conflict_set|revision_conflict→409`。

```text
ReadSnapshotV1 {
  snapshot_schema_version, snapshot_id, resource_kind, query_hash, authz_fingerprint,
  stable_sort_schema_id, strategy:immutable_high_watermark|materialized_view,
  high_watermark?, materialized_item_count?, created_at, expires_at
}
MaterializedReadItemV1 { snapshot_id, ordinal, resource_id, observed_revision, view_schema_id, canonical_view, view_hash }
PageCursorV1 { cursor_schema_version, snapshot_id, position, page_size, query_hash, opaque_signature }
PageV1[T] { page_schema_version, read_snapshot_id, items:[T...], next_cursor?, expires_at }
```

`query_hash=sha256(canonical_json({api_version,resource_kind,filters,stable_sort,page_projection}))`；`authz_fingerprint` 绑定 principal、当前 RolePolicy version 与数据域，不把 role/权限信任给客户端。Artifact/ref-history/audit 等不可变或 append-only 集合用 high-watermark + keyset，并在 snapshot TTL 内 pin 对应 retention；runs/approvals/sessions 等会因 status/assignee 改变而进出 filter 的集合，在首请求以同一 DB 读事务物化**有界、已授权、稳定排序的 canonical list view**（不只 ID），后续按 ordinal 返回，避免跨页字段漂移。若结果超过 `max_materialized_snapshot_items`，返回类型化 `query_too_broad→422`，要求缩小时间/状态/域过滤，不偷偷退化为不一致 keyset。

cursor 是服务端签名的 opaque token，绑定 snapshot/query/page size；签名无效或跨 query 使用→`invalid_cursor→400`，跨 principal 复用按当前认证结果→401/403。每页都重做当前 authn/authz：principal/credential disabled→401，资源权限收窄→403；仍有权限但 RolePolicy/authz fingerprint 变化、snapshot/retention 过期或 materialized view 缺失→`cursor_expired→410`，客户端显式重开查询。保证是在未过期且 authz 不变的同一 snapshot 内，拼接各页恰好得到首请求时的有序视图，**无重复、无遗漏**；之后新建/改状态的资源不混入。SQLite/PG 共享契约测试覆盖跨连接插入/更新/删除、TTL、权限撤销和 cursor 篡改；绝不靠跨 HTTP 的长事务维持 snapshot。

### 5.5 可续传 SSE + 持久 WS 命令 + apps/worker

契约落点按语义拆分而非全塞进 API：Run/Outcome/manifest DTO 在 `contracts/jobs.py`，TaskSuite/CompletionOracle 在 `contracts/playtest.py`（可复用既有 `env_types`，但不得依赖 game/aureus），Finding revision 在 `contracts/findings.py`，ExecutionProfile catalog DTO 在 `contracts/execution_profiles.py`；`contracts/api.py` 只放 transport request/response/problem/SSE/WS envelope 并引用这些纯类型。实现与 registry/publisher 在 apps/platform/runtime，`contracts` 不含 handler 或业务依赖。

```text
RetryPolicySnapshot { retry_schema_version, retry_policy_id, retry_policy_version, max_attempts,
                      retryable_failure_classes:[FailureClassV1...],
                      backoff:fixed|exponential, base_delay_ms, max_delay_ms, jitter_policy, honor_retry_after,
                      retry_policy_digest }
PreparedArtifact { kind, payload_schema_id, version_tuple, lineage[], payload_hash, meta, object_ref:ObjectRef, location:ObjectLocation }
PreparedFindingV1 { finding_id, expected_previous_revision:positive-int|null, evidence_artifact_index, payload:FindingPayloadV1 }
PreparedRunResultSummaryV1 { summary_schema_version:prepared-run-result-summary@1, outcome_code, primary_artifact_kind, prepared_domain_artifact_count, prepared_finding_count }
RunResultSummaryV1 { summary_schema_version:run-result-summary@1, outcome_code, primary_artifact_kind, produced_artifact_count, finding_count }
RequirementDispositionV1 {
  resolved_policy_id, outcome_rule_id, requirement_id,
  status:produced|not_executed, reason_code?
}
PreparedRunResult { prepared_schema_version:prepared-run-result@1, run_id, attempt_no, run_kind:RunKindRef,
                    primary_index, artifacts:[PreparedArtifact...], findings:[PreparedFindingV1...],
                    requirement_dispositions:[RequirementDispositionV1...], summary:PreparedRunResultSummaryV1 }
PreparedRunFailure { prepared_schema_version:prepared-run-failure@1, run_id, attempt_no?, run_kind:RunKindRef,
                     artifacts:[PreparedArtifact...], requirement_dispositions:[RequirementDispositionV1...],
                     cause_code, failure_class:FailureClassV1, intrinsic_retry_eligible,
                     classifier:FailureClassifierRefV1, dependency?:DependencyFailureV1, redacted_message }
PreparedRunOutcome = PreparedRunResult | PreparedRunFailure  # sealed union；按 prepared_schema_version 判别
TerminalPublisherHooks { on_success, on_failure, on_cancel, on_timeout }  # registry keys，非客户端字符串
ArtifactIdentityBindingV1 {
  collection_item_pointer?,                         # null=集合元素本身
  artifact_value_source:artifact_id|payload,
  artifact_payload_pointer?                        # iff artifact_value_source=payload
}
JsonCollectionCountBindingV1 {
  source:run_payload|prepared_primary_payload,
  collection_pointer, identity_binding:ArtifactIdentityBindingV1?
}
ResolvedPolicyCountBindingV1 {
  source:resolved_policy_snapshot, resolved_policy_id, outcome_rule_id,
  identity_binding:ArtifactIdentityBindingV1
}
ResolvedPolicySubsetCountBindingV1 {
  source:resolved_policy_subset, resolved_policy_id, outcome_rule_id,
  allowed_not_executed_reason_codes[], identity_binding:ArtifactIdentityBindingV1
}
IntermediateCountBindingV1 {
  source:published_intermediate_links, link_role:prompt_rendered,
  scope:current_attempt|all_attempts
}
ExecutionModeCountBindingV1 {
  source:execution_mode,
  exact_count_by_mode:{not_applicable,live,record,replay}
}
ArtifactCountBindingV1 = JsonCollectionCountBindingV1 | ResolvedPolicyCountBindingV1 | ResolvedPolicySubsetCountBindingV1 |
                         IntermediateCountBindingV1 | ExecutionModeCountBindingV1
ArtifactLineagePolicyRefV1 { policy_id, policy_version, digest }
ArtifactParentRuleV1 {
  parent_role, source:run_input|run_intermediate|prepared_rule|child_payload_reference,
  source_rule_id?, child_payload_pointer?, artifact_kinds[], payload_schema_ids[],
  min_count, max_count?, direct_parent:true
}
VersionFieldProjectionRuleV1 {
  field, source:producer_value|parent_role|constant_null,
  parent_role?, equality_parent_roles[]
}
ArtifactLineagePolicyV1 {
  policy_schema_version, policy_id, policy_version, child_kind, child_payload_schema_ids[],
  parent_rules:[ArtifactParentRuleV1...], version_projection:[VersionFieldProjectionRuleV1...],
  allow_unmatched_parents:false
}
OutcomeArtifactRuleV1 {
  rule_id, role:primary|output|evidence,
  artifact_kind, payload_schema_ids[],
  min_count, max_count?, count_binding:ArtifactCountBindingV1?,
  lineage_policy_ref:ArtifactLineagePolicyRefV1
}
VersionTransitionPolicyRefV1 { policy_id, policy_version, digest }
VersionTransitionModeRuleV1 {
  llm_execution_mode:not_applicable|live|record|replay,
  field_rules:[{field,operation:copy_frozen|set_null_no_invocation|set_from_execution_identity|set_from_exact_cassette_parent,
                cassette_scope?}...]
}
VersionTransitionPolicyV1 {
  policy_schema_version, policy_id, policy_version, manifest_scope:attempt|run,
  mode_rules:[VersionTransitionModeRuleV1...]
}
OutcomeArtifactPolicyV1 {
  policy_schema_version, policy_id, policy_version, outcome_code,
  prepared_outcome:success|failure,
  publication_scope:attempt|run,
  attempt_terminal_status?:failed|cancelled|timed_out|lease_expired,
  run_status_after_publication:retry_wait|succeeded|failed|cancelled|timed_out,
  failure_class:FailureClassV1?, retry_disposition:retry|terminal?,
  artifact_rules:[OutcomeArtifactRuleV1...],
  workflow_effect_key, version_transition_policy_ref:VersionTransitionPolicyRefV1
}
FindingOutputPolicyRefV1 { policy_id, policy_version, digest }
FindingOutputPolicyV1 {
  policy_schema_version:finding-output-policy@1, policy_id, policy_version,
  max_findings, allowed_evidence_outcome_rule_ids[], allowed_oracle_types[], allowed_sources[]
}
RuntimeParentRuleSetRef { rule_set_id, version, digest }
RuntimeParentRuleV1 {
  rule_id, manifest_scope:attempt|run|both,
  source:run_input|published_intermediate|record_shard|attempt_bundle|run_bundle|closed_attempt_failure,
  parent_role:input|intermediate, artifact_kind:ArtifactKind, payload_schema_ids[],
  attempt_selector:none|current|all_closed, min_count, max_count?, count_binding:ArtifactCountBindingV1?
}
RuntimeParentRuleSetV1 {
  rule_set_schema_version:runtime-parent-rules@1, rule_set_id, version,
  rules:[RuntimeParentRuleV1...]
}
RunKindDefinition { definition_schema_version:run-kind-definition@1, kind, version, status:active|disabled, payload_schema_id,
                    prepared_result_schema_id, prepared_failure_schema_id, result_schema_id, failure_schema_id,
                    outcome_policies:[OutcomeArtifactPolicyV1...], runtime_parent_rule_set:RuntimeParentRuleSetRef,
                    finding_output_policy_ref:FindingOutputPolicyRefV1?,
                    allowed_command_schema_ids[],
                    creation_mode:generic_runs_endpoint|resource_endpoint_only|internal_only, allowed_llm_execution_modes[],
                    seed_policy:required|forbidden|profile_dependent, seed_derivation_version?,
                    required_permission:Permission, executor_key, terminal_hooks:TerminalPublisherHooks,
                    failure_classifier:FailureClassifierRefV1, retry_policy:RetryPolicyRefV1,
                    migration_capability_matrix?:MigrationCapabilityMatrixRefV1 }
```

`executor_key/terminal_hooks/workflow_effect_key` 只能由组合根 registry 映射到已注册 handler/publisher/effect，绝不是客户端 import path/callable。所有 M4 kind 的 `prepared_result_schema_id=prepared-run-result@1`、`prepared_failure_schema_id=prepared-run-failure@1`、`result_schema_id=run-result@1`、`failure_schema_id=run-failure@1`；权威细节在 allowlisted Artifact payload 中，summary 不承载任意字典。worker 的 `prepared_domain_artifact_count/prepared_finding_count` 必须分别等于 `PreparedRunResult.artifacts/findings` 长度；`primary_index` 必须命中 artifacts，prepared summary 的 `primary_artifact_kind` 等于该项 kind，prepared summary/outcome 的 `outcome_code` 等于所选 policy。publisher 另从最终 manifest projection/RunFindingLink 重算 `produced_artifact_count/finding_count`；最终 `RunResultV1.outcome_code=summary.outcome_code=所选 policy.outcome_code`，`primary_artifact_id` 必须等于 `primary_index` 对应 PreparedArtifact 发布后的 Artifact ID，且其 kind 等于 summary 的 `primary_artifact_kind`，两个 count 必须等于外层对应集合/字段，绝不沿用 worker 的 prepared count。Run 创建时把 `RunKindRef.version` + 完整 definition digest、outcome policy-set digest，以及其引用的 lineage/transition/runtime-parent/finding-output policy `{id,version,digest}` 和条件 `migration_capability_matrix` 闭包冻结进 RunRecord，并把 resolved policy snapshots 放入 payload；terminal 必须按该快照执行，历史 registry 版本在所有引用 Run/Artifact/Finding 的保留期内不可删除，绝不读取后来变化的 current registry。

摘要闭包不是实现自由度：`RuntimeParentRuleSetRef.digest=sha256(canonical_json(RuntimeParentRuleSetV1))`，其中 rules 按唯一 `rule_id` 排序；`outcome_policy_set_digest=sha256(canonical_json({policy_set_schema_version:outcome-policy-set@1,run_kind:{kind,version},policies}))`，policies 按唯一 `(policy_id,policy_version)` 排序且每个 `artifact_rules` 按唯一 `rule_id` 排序；`run_kind_definition_digest=sha256(canonical_json(RunKindDefinition))`。三式均排除外部 digest 字段本身。所有语义集合在计算前 canonical 化：command/schema/source/oracle allowlist stable unique 排序，LLM mode 按 `not_applicable,live,record,replay` 固定顺序，transition mode 唯一且按同顺序、field rules 按 VersionTuple 冻结字段顺序，lineage parent rules 与 version projection 按各自唯一 ID/field 排序；重复、未知值或顺序归一化后碰撞使 registry readiness fail-closed。Run 创建时重算三种摘要与全部引用 policy digest，terminal 再从保留的 exact registry version 重算并与 RunRecord/ref **逐字段**比较，不能只信持久字符串或 current alias。

worker 只返回一个 PreparedRunOutcome。控制面先选出完整 publication plan，publisher 逐个读取 blob 重验 `payload_schema_id`/kind/hash/location，并要求每个 PreparedArtifact 在**整个 plan 的所有 policies**中恰好匹配一条 OutcomeArtifactRule；一个 Artifact 不得被 attempt/run 两层重复消费。`payload_schema_ids` 必须是非空 exact-version allowlist，禁止 wildcard；未知、重叠、少一项或多一项都 fail-closed。active attempt 的 final failure 使用同一个 PreparedRunFailure：attempt-close policy 由控制面从其稳定 cause code/class/intrinsic eligibility + 本次 `RetryDecisionV1` + current lease/runtime parents 合成 attempt-scope RunFailure，`artifact_rules=[]`、dispositions=[]，不消费 worker 领域 Artifact；随后同 UoW 的 run-scope aggregate policy 独占匹配 Prepared artifacts/dispositions并发布业务 evidence。无 active attempt 的 queued/retry_wait cancel/timeout 不调用 executor，由控制面从持久命令/deadline 生成 artifacts/dispositions 均为空的 typed PreparedRunFailure；queued 的 attempt_no=null，retry_wait 的 attempt_no=最大已关闭 attempt。这样 attempt manifest 只证明 attempt 已关闭，run manifest 才表达整个 Run 的领域拒绝证据，不要求 worker 返回第二个 outcome，也不要求 worker 预知发布瞬间的预算/deadline/剩余 attempt。

`max_count=null` 仅表示无固定上界。`ArtifactCountBindingV1` 在 JSON Schema 中是以 `source` 判别的 `oneOf`：JSON collection 分支必须有 RFC 6901 `collection_pointer` 且禁止 policy/intermediate/mode 字段，pointer 必须解析为有界数组；`resolved_policy_snapshot` 分支要求冻结 requirement **全量**一一对应；`resolved_policy_subset` 分支用下述完整 disposition 集合确定允许的 produced 子集；intermediate 分支只能读取已提交 link 并按 manifest scope 过滤；execution-mode 分支只接受四个非负整数且禁止 pointer。identity binding 存在时，collection item 与 Artifact ID 或 Artifact payload pointer 必须形成稳定一一映射；`artifact_value_source=artifact_id` 时禁止 payload pointer，`=payload` 时 payload pointer 必填。由此 publisher 按冻结 Run payload/resolved policy/primary payload/已提交 intermediate links 求得**精确数量和身份集合**，不能指进程默认配置，也不能用重复 profile/export 冒充 cardinality。

`ResolvedPolicySnapshotV1.digest=sha256(canonical_json({snapshot_schema_version,resolved_policy_id,source_profile_field_path,source_profile_payload_hash,requirements}))`，requirements 按 `(outcome_rule_id,ordinal,requirement_id)` 稳定排序且三个字段组合唯一；`producer_profile_field_path` 非空时必须解析到同一 Run 的 exact `resolved_profiles`。Run 创建服务从 profile 内联 config 与当前请求确定性解析 requirement list 后即冻结 snapshot，worker/publisher 不得重新读取 profile current。使用两种 resolved-policy binding 的 Artifact payload schema 都必须暴露受 payload hash 保护的 `requirement_id`，identity binding 固定为 requirement `/requirement_id` ↔ Artifact payload `/requirement_id`；kind/schema/producer profile 也须逐项等于该 requirement。`ResolvedPolicyCountBindingV1` 要求全量 Artifact 一一对应；`ResolvedPolicySubsetCountBindingV1` 则要求 PreparedRunOutcome 对该 `{resolved_policy_id,outcome_rule_id}` 的每个冻结 requirement **恰有一条** `RequirementDispositionV1`，按 `(resolved_policy_id,outcome_rule_id,requirement_id)` stable unique 排序：`produced` 必须无 reason 且恰有匹配 Artifact，`not_executed` 必须无 Artifact、reason 必填且在 policy 的 `allowed_not_executed_reason_codes` exact allowlist。publisher 将完整 rows 原样重验并只复制到消费该 subset 的 run-scope RunResult/RunFailure；attempt-close manifest 的 dispositions 固定为空。缺 row、额外 ID、重复、状态/Artifact 不一致或未允许 reason 均 fail-closed。这样 early-stop 可以诚实表达“未执行”，但不能把任意遗漏伪装成合法子集。整个 publication plan 没有任何 subset binding 时 Prepared dispositions 必须为空。

`FindingOutputPolicyRefV1.digest=sha256(canonical_json(FindingOutputPolicyV1))`，RunKindDefinition 的 ref 为 null 时 `PreparedRunResult.findings` 必须为空；非空时数量不得超过 `max_findings`。每个 PreparedFinding 的 `evidence_artifact_index` 必须指向同批 PreparedArtifact，且该 Artifact 恰好匹配 policy allowlist 中的 Outcome rule、oracle/source、当前 run/attempt 与同一 snapshot/config；payload.producer_run_id 必须等于当前 run。publisher 对 `(finding_id,expected_previous_revision)` 做 series-head CAS，分配不可变正整数 revision，重算 digest，再按 `(finding_id,revision)` 稳定排序分配正整数 ordinal，并在发布领域 Artifact/RunResult 的**同一 UoW**插入 FindingRevisionV1 + RunFindingLinkV1；`(run_id,attempt_no,ordinal)` 与 `(run_id,finding_id,finding_revision)` 分别唯一，任一冲突整笔回滚，不能留下“报告里有 Finding、Run 却枚举不到”的半状态。初始映射冻结为 `review.run@1→review-findings@1`、`checker.run@1→checker-findings@1`、`simulation.run@1→simulation-findings@1`、`playtest.run@1→playtest-findings@1`、三个 validation kind→`validation-findings@1`；其余初始 kind 为 null。RunFailure 不发布 Finding；非成功业务 evidence 若需要 Finding，必须改成显式 success-with-negative-business-result policy，不能借 failure hook 写旁路记录。

每条 OutcomeArtifactRule 还必须解析 exact `ArtifactLineagePolicyRefV1`。policy digest=`sha256(canonical_json(ArtifactLineagePolicyV1))`；`parent_rules` 的 `source_rule_id` 仅在 `source=prepared_rule` 时必填，`child_payload_pointer` 仅在 `source=child_payload_reference` 时必填，其余分支禁止这些字段。publisher 在同一 terminal UoW 的事务视图中把每个 child 的裸 `lineage[]` 反向匹配为 typed parent roles：parent 必须来自 Run 创建时冻结 input、当前 scope 已提交 intermediate、同批且已匹配的 sibling rule，或 child payload 中显式引用且同时属于前三类的 Artifact；逐一校验存在性/kind/schema/hash、direct parent、cardinality 与 stable identity，任何未匹配/重复/悬空 parent 均拒绝。`version_projection` 必须把 VersionTuple 的每个字段恰好声明一次；`producer_value` 只能取 Run 冻结 producer snapshot，`parent_role` 必须唯一解析，`equality_parent_roles` 全部相等后才可继承，`constant_null` 只用于 producer matrix 明确不适用字段。这样实现 foundations v0.3 §5.1 的 typed role projection，不允许 `lineage[]` 自报即真。

`FailureClassifierV1.classifier_digest=sha256(canonical_json(payload excluding classifier_digest))`，rules 按唯一 `cause_code` 排序；dependency kind allowlist stable unique。`RetryPolicySnapshot.retry_policy_digest` 同理对 canonical payload 排除自身计算。RunKindDefinition/RunRecord/Prepared failure 的 exact classifier ref 和 RunKindDefinition/RunRecord 的 retry-policy ref 必须闭合；`RunRecord.max_attempts` 是 exact RetryPolicySnapshot.max_attempts 的只读同值投影，create/claim/recovery/terminal 均重验且无独立 mutator。历史版本在引用 Run/Artifact 保留期内不可删除。publisher 用冻结 classifier 重算 Prepared 的 cause_code→class/intrinsic eligibility/dependency-required 映射并与 worker字段逐项相等，未知 cause code 或自报不符 fail-closed。`DependencyFailureV1` 只含有界非敏感 registry ID/operation/classifier code/可选状态与 Retry-After，不含 endpoint、凭据或原始上游消息；transient/permanent dependency class 必填且 allowed kind 匹配，其他 class 必须 null。active attempt 与最终 run 两层 RunFailure 必须从同一 Prepared 对象逐字段复制 cause code/class/dependency；redacted message 不参与分类。

Outcome policy 的 selector tuple=`{outcome_code,prepared_outcome,publication_scope,attempt_terminal_status|null,run_status_after_publication,failure_class|null,retry_disposition|null}`，同一 RunKindDefinition 内必须唯一且互斥；registry load 对重复或可重叠 selector 直接 readiness failure，不能依赖数组顺序。`publication_scope=attempt` 只允许 failure、要求 `attempt_terminal_status` 与正 `attempt_no`，且 `run_status_after_publication` 只能是 `retry_wait` 或与随后同 UoW run-scope final publication 一致的终态；`publication_scope=run` 要求 Run 为对应终态，success 只允许 `succeeded`。从未 claim 的 queued 或已关闭 attempt 后的 retry_wait 控制面终结都要求 `attempt_terminal_status` absent；前者外层 `attempt_no=null`，后者外层 `attempt_no=最大已关闭 attempt`。`failure_class/retry_disposition` 当且仅当 `prepared_outcome=failure` 时必填，success policy 必须 absent；FailureClass 只接受 §3.3 `FailureClassV1`。

控制面在 terminal UoW 内按 exact classifier + RetryPolicy + current attempt count/budget/queue/attempt/overall deadline + Retry-After 生成 immutable `RetryDecisionV1`：Prepared 只声明 intrinsic eligibility，不声明最终 retryable。decision=`retry` 当且仅当 intrinsic eligible 且全部控制面条件仍允许，并选择一个 `retry_disposition=retry` 的 attempt-scope policy；否则 reason 精确说明耗尽/禁止条件，选择 `retry_disposition=terminal`。`RunFailureV1.retryable` 必须等于 `(retry_decision.decision=retry)`；RunFailure cause_code/class 必须等于 decision，attempt-final 与 run-final 两层复制同一 terminal decision，retry attempt 只发布 attempt manifest。**任何存在 active attempt 的最终非成功路径**（含业务 gate reject、repair unverified、dependency/quota/integrity/execution failure、cancel、timeout、lease expiry、subject superseded）都先选择一个同 cause-code/class 的 attempt-scope close policy，再在同一 UoW 选择 run-scope aggregate policy。从未 claim 的 queued cancel/queue timeout 与无 active lease 的 retry_wait cancel/overall timeout 只有 run-scope policy；retry_wait 分支不重新关闭旧 attempt。plan 中任一 policy 缺失、重复、classification/decision不闭合、prepared allocation不唯一或状态迁移不闭合则整笔 UoW 回滚。

`VersionTransitionPolicyRefV1.digest=sha256(canonical_json(VersionTransitionPolicyV1))`。初始 `attempt-manifest-transition@1` 与 `run-manifest-transition@1` 都为四种 execution mode 各冻结一条 exhaustive mode rule，并把 VersionTuple 每个字段恰好覆盖一次：live/record/replay 对 `prompt_version/model_snapshot/agent_graph_version` 使用 terminal Artifact `meta.execution_identity` 的 exact projection；该 scope 没有 invocation 时 prompt/model 用 `set_null_no_invocation`，但 agent graph 仍从冻结 plan copy；RECORD 另把 `cassette_id` 分别从 exact `attempt_bundle`/`run_bundle` parent 设置；REPLAY 保持创建时 exact replay-input cassette ID，并要求 identity 与 bundle/import manifest 逐 route 相等；live cassette 仍为 null；not_applicable 无 identity bindings且全字段按 producer matrix copy/null。`set_from_execution_identity` 只能用于这三个字段并要求 digest/tuple/meta闭合，`set_null_no_invocation` 只能在 binding 空集时使用。Outcome policy ref、RunRecord 冻结 ref 与最终 projection ref 必须逐字段相等；未知/摘要不符/字段未覆盖/非法 scope 或实际 invocation 越出 ExecutionVersionPlan 的 transition policy 一律 fail-closed。

`RuntimeParentRuleSetRef.digest=sha256(canonical_json(RuntimeParentRuleSetV1))`，rules 按唯一 `rule_id` 稳定排序，payload schema allowlist stable unique；ref 的 id/version/digest 必须与保留 registry object 逐字段相等。它是 outcome policy 的机器可执行公共部分：terminal validator 对“已提交 RunIntermediateArtifactLink + PreparedRunOutcome + cassette publication + 已关闭 attempts”的并集校验。初始 `runtime-parents@1` 规定：实际 LLM call/replay 前发布的 `source_rendered` 数量精确等于已提交 prompt links；RECORD 的 record shard/attempt/run aggregate cassette 数量和层级按 foundations v0.3 §7，REPLAY 只允许创建时绑定的 existing run bundle，live/not_applicable 为 0。attempt-scope manifest 只能包含其 exact `attempt_no` 的 prompt links/source_rendered/record shards/attempt bundle，以及**分配给该 attempt policy**的 evidence；final business failure 的 attempt policy artifact rules 为空，因此不会复制 run-scope业务 evidence/dispositions。run-scope manifest 聚合全部 attempts 的 runtime intermediates，并把每个已关闭 attempt 的独立 attempt-scope `RunAttempt.failure_artifact_id`（包括刚关闭的最终 current attempt）作为 `role=intermediate` parent，数量/attempt_no 与持久 RunAttempt 精确对应。成功 attempt 的 failure pointer 必须为空；queued 未 claim 终结没有 attempt parent。`retry_wait` 控制面终结同样聚合全部既有 closed-attempt parents，但没有 current attempt parent，也不生成新的 attempt manifest。当前正在生成的 run-scope manifest 只写 `RunRecord.failure_artifact_id`，永不写入任何 RunAttempt，也不得引用自身；以上 parent 全部进入 manifest projection，任何中间工件不能绕过 allowlist。当前正在生成的 Run manifest 自身由 publication scope/status 固定生成，不计入 OutcomeArtifactPolicy 的领域 Artifact rules。

初始 outcome policies 冻结下列 Journey A/B 必经产品路径及公共非成功路径。表中 `kind[schema]` 是 `OutcomeArtifactRuleV1.{artifact_kind,payload_schema_ids=[schema]}` 的规范缩写；`resolved(<id>,<rule>)` 是 `ResolvedPolicyCountBindingV1`，按 requirement ID 与 Artifact payload `/requirement_id` 全量一一对拍；`subset(<id>,<rule>,<reasons>)` 是 `ResolvedPolicySubsetCountBindingV1`，只能按 final manifest 中完整 disposition rows 省略带允许 reason 的未执行项。每行的 policy ID/outcome code、规则、schema、count binding 与 workflow effect 都是 registry 的规范输入，不是说明性示例：

| Policy selector | 精确领域 Artifact 规则 | Workflow effect / finding policy |
|---|---|---|
| policy `generation-gate-pass@1`; code=`generation_gate_passed`; run,succeeded | primary `patch[patch@2]`=1；output `ir_snapshot[ir-core@1]`=1；`config_export[config-export-package@1]` count binding=`{source:run_payload,collection_pointer:/params/candidate_export_profiles,identity_binding:{artifact_value_source:payload,artifact_payload_pointer:/export_profile}}`；gate `checker_run[checker-report@1]`=`resolved(generation-gate,checker)`、`simulation_run[simulation-result@1]`=`resolved(generation-gate,simulation)`、`review_report[review@1]`=`resolved(generation-gate,review)` | `create_patch_subject_head_and_draft@1` |
| policy `generation-gate-rejected@1`; code=`generation_gate_rejected`; run,failed,class=`business_rule`,retry_disposition=terminal | evidence-only `patch[patch@2]`=1、`ir_snapshot[ir-core@1]`=1；`config_export`=0；`checker_run[checker-report@1]`=`resolved(generation-gate,checker)`、`simulation_run[simulation-result@1]`=`resolved(generation-gate,simulation)`、`review_report[review@1]`=`resolved(generation-gate,review)` | `no_workflow_subject@1` |
| policy `generation-gate-rejected-attempt-final@1`; code=`generation_gate_rejected`; attempt,failed→run failed,class=`business_rule`,retry_disposition=terminal | artifact rules=0、dispositions=0；只发布 exact current-attempt runtime parents/attempt manifest；随后同 UoW 必选上行 run policy消费全部业务 evidence | `close_attempt_for_terminal@1` |
| policy `repair-verified@1`; code=`repair_verified`; run,succeeded | primary superseding `patch[patch@2]`=1；output `ir_snapshot[ir-core@1]`=1；`config_export[config-export-package@1]` 按 `/params/candidate_export_profiles` 一一绑定；`checker_run[checker-report@1]`=`resolved(repair-verifier,checker)`、`simulation_run[simulation-result@1]`=`resolved(repair-verifier,simulation)`、`regression_evidence[regression-evidence@1]`=`resolved(repair-verifier,regression)` | `supersede_patch_head_create_draft@1` |
| policy `repair-unverified@1`; code=`repair_unverified`; run,failed,class=`validation`,retry_disposition=terminal | new `patch/ir_snapshot/config_export`=0；`checker_run`=`subset(repair-verifier,checker,{prior_requirement_failed,search_exhausted,execution_short_circuited})`、`simulation_run`=`subset(repair-verifier,simulation,{prior_requirement_failed,search_exhausted,execution_short_circuited})`、`regression_evidence`=`subset(repair-verifier,regression,{prior_requirement_failed,search_exhausted,execution_short_circuited})`；每个冻结 requirement 必有 produced/not_executed disposition，不允许静默遗漏、snapshot 外或重复项 | `leave_patch_head_unchanged@1` |
| policy `repair-unverified-attempt-final@1`; code=`repair_unverified`; attempt,failed→run failed,class=`validation`,retry_disposition=terminal | artifact rules=0、dispositions=0；只发布 exact current-attempt runtime parents/attempt manifest；随后同 UoW 必选上行 run policy消费 subset evidence/dispositions | `close_attempt_for_terminal@1` |
| policy `constraint-proposal-drafted@1`; code=`constraint_proposal_drafted`; run,succeeded | primary `constraint_proposal[constraint-proposal@1]`=1 | `create_constraint_subject_head_and_draft@1` |
| policy `review-completed@1`; code=`review_completed`; run,succeeded | primary `review_report[review@1]`=1；output `checker_run[checker-report@1]` 按 `/params/checker_profiles` 一一绑定；`simulation_run[simulation-result@1]` 按 `/params/simulation_profiles` 一一绑定 | `no_workflow_change@1` + `review-findings@1` |
| policy `checker-completed@1`; code=`checker_completed`; run,succeeded | primary `checker_run[checker-report@1]`=1 | `no_workflow_change@1` + `checker-findings@1` |
| policy `simulation-completed@1`; code=`simulation_completed`; run,succeeded | primary `simulation_run[simulation-result@1]`=1 | `no_workflow_change@1` + `simulation-findings@1` |
| policy `task-suite-derived@1`; code=`task_suite_derived`; run,succeeded | primary `task_suite[task-suite@1]`=1；output `scenario_spec[scenario-spec@1]` count binding=`{source:prepared_primary_payload,collection_pointer:/episodes,identity_binding:{collection_item_pointer:/scenario_spec_artifact_id,artifact_value_source:artifact_id}}`，且等于 suite lineage scenario parents | `no_workflow_change@1` |
| policy `playtest-completed@1`; code=`playtest_completed`; run,succeeded | primary `playtest_trace[playtest-trace@1]`=1，payload 精确绑定 config/constraint/task-suite/environment/profile/seed 与 selected `{episode_id,scenario_spec_artifact_id}` bindings | `no_workflow_change@1` + `playtest-findings@1` |
| policy `patch-validation-passed@1`; code=`patch_validation_passed`; run,succeeded | primary `validation_evidence[evidence-set@1]`=1；output `regression_evidence[regression-evidence@1]`=`resolved(patch-validation,regression)`；EvidenceSet.overall_status=`passed`；`auto_apply_proof`=0 | `set_patch_validated@1` + `validation-findings@1` |
| policy `patch-validation-auto-eligible@1`; code=`patch_validation_auto_eligible`; run,succeeded | primary `validation_evidence[evidence-set@1]`=1；output regression 同上；evidence `validation_evidence[auto-apply-proof@1]`=1，payload/lineage 精确绑定同批 EvidenceSet、subject/target/ref、全部 deterministic/regression evidence 与冻结 auto policy digest；EvidenceSet.overall_status=`passed` | `set_patch_validated_with_auto_proof@1` + `validation-findings@1` |
| policies `patch-validation-failed@1` / `patch-validation-unproven@1`; codes=`patch_validation_failed` / `patch_validation_unproven`; run,succeeded | primary `validation_evidence[evidence-set@1]`=1；output regression=`resolved(patch-validation,regression)`；分别强制 overall_status=`failed` / `unproven`；`auto_apply_proof`=0 | `set_patch_validation_failed@1` + `validation-findings@1` |
| policy `constraint-validated-with-candidate@1`; code=`constraint_validated`; run,succeeded | primary `validation_evidence[evidence-set@1]`=1；output `constraint_snapshot[constraint-snapshot@1]`=1；evidence `validation_evidence[constraint-compile-evidence@1]`=1；`regression_evidence[regression-evidence@1]`=`resolved(constraint-validation,regression)` | `set_exact_binding_and_validated@1` + `validation-findings@1` |
| policy `constraint-validation-failed-with-candidate@1`; code=`constraint_validation_failed_with_candidate`; run,succeeded | primary `validation_evidence[evidence-set@1]`=1；output `constraint_snapshot[constraint-snapshot@1]`=1；compile evidence=1；regression evidence 使用 `subset(constraint-validation,regression,{prior_requirement_failed,execution_short_circuited})`，完整 dispositions 与 EvidenceSet requirement statuses 对拍 | `set_exact_binding_and_validation_failed@1` + `validation-findings@1` |
| policy `constraint-validation-failed-without-candidate@1`; code=`constraint_validation_failed_without_candidate`; run,succeeded | primary `validation_evidence[evidence-set@1]`=1；`constraint_snapshot`=0；evidence `validation_evidence[constraint-compile-evidence@1]`=1 且 candidate ID=null；regression rule 用 `subset(constraint-validation,regression,{compile_failed,candidate_unavailable})` 且 produced set 必为空 | `leave_binding_null_and_validation_failed@1` + `validation-findings@1` |
| policies `rollback-validation-passed@1` / `rollback-validation-failed@1` / `rollback-validation-unproven@1`; codes=`rollback_validation_passed` / `rollback_validation_failed` / `rollback_validation_unproven`; run,succeeded | primary `validation_evidence[evidence-set@1]`=1；output `regression_evidence[regression-evidence@1]`=`resolved(rollback-validation,regression)`；overall status 与 code 一致 | `set_rollback_validated@1` / `set_rollback_validation_failed@1` / `set_rollback_validation_failed@1` + `validation-findings@1` |
| policy `dependency-unavailable-attempt-retry@1`; code=`dependency_unavailable`; attempt,failed→retry_wait,class=`transient_dependency`,retry_disposition=retry；policy `lease-expired-attempt-retry@1`; code=`lease_expired`; attempt,lease_expired→retry_wait,class=`lease`,retry_disposition=retry | artifact rules=0、dispositions=0；只允许 exact current-attempt `runtime-parents@1` intermediate/cassette | `close_attempt_for_retry@1`；不改 workflow/SubjectHead |
| policy `lease-expired-attempt-final-failed@1`; code=`lease_expired`; attempt,lease_expired→run failed,class=`lease`,retry_disposition=terminal；policy `lease-expired-attempt-final-timeout@1`; same code；attempt,lease_expired→run timed_out,class=`lease`,retry_disposition=terminal | artifact rules=0、dispositions=0；只允许 exact current-attempt runtime parents；随后同 UoW 必有同 status 的 run-scope final policy | `close_attempt_for_terminal@1` |
| policy `execution-failed-attempt-final@1`; code=`execution_failed`,attempt failed→run failed,class=`execution`,retry_disposition=terminal；`cancelled-attempt-final@1`; code=`cancelled`,attempt cancelled→run cancelled,class=`cancelled`,terminal；`timed-out-attempt-final@1`; code=`timed_out`,attempt timed_out→run timed_out,class=`timeout`,terminal；`subject-superseded-attempt-final@1`; code=`subject_superseded`,attempt cancelled→run cancelled,class=`subject_superseded`,terminal | artifact rules=0、dispositions=0；只允许 exact current-attempt runtime parents；先写 `RunAttempt.failure_artifact_id`，随后同 UoW 必有对应 run-scope aggregate policy | `close_attempt_for_terminal@1` |
| policies `dependency-unavailable-attempt-final@1` / `permanent-dependency-attempt-final@1` / `quota-exceeded-attempt-final@1` / `integrity-violation-attempt-final@1`; codes/classes 分别=`dependency_unavailable/transient_dependency`、`permanent_dependency_failed/permanent_dependency`、`quota_exceeded/quota`、`integrity_violation/integrity`; attempt failed→run failed,retry_disposition=terminal | artifact rules=0、dispositions=0；只允许 exact current-attempt runtime parents；随后同 UoW 选同 cause-code/class 的 run policy | `close_attempt_for_terminal@1`；integrity 同时触发受限告警 |
| run policies `execution-failed@1`(`execution_failed`,`execution`,terminal,failed)；`cancelled@1`(`cancelled`,`cancelled`,terminal,cancelled, active attempt)；`control-cancelled@1`(`cancelled`,`cancelled`,terminal,cancelled, no active attempt)；`timed-out@1`(`timed_out`,`timeout`,terminal,timed_out, active attempt)；`queue-timed-out@1`(`queue_timed_out`,`timeout`,terminal,timed_out)；`retry-wait-timed-out@1`(`timed_out`,`timeout`,terminal,timed_out, no active attempt)；`subject-superseded@1`(`subject_superseded`,`subject_superseded`,terminal,cancelled)；`lease-expired-final-failed@1`(`lease_expired`,`lease`,terminal,failed)；`lease-expired-final-timeout@1`(`lease_expired`,`lease`,terminal,timed_out)；`dependency-unavailable@1`(`dependency_unavailable`,`transient_dependency`,terminal,failed)；`permanent-dependency-failed@1`(`permanent_dependency_failed`,`permanent_dependency`,terminal,failed)；`quota-exceeded@1`(`quota_exceeded`,`quota`,terminal,failed)；`integrity-violation@1`(`integrity_violation`,`integrity`,terminal,failed) | 新领域 Artifact=0；只允许 `runtime-parents@1` 已提交 intermediate/cassette/attempt failures；active-attempt policy 具有非空 `attempt_terminal_status` 并聚合刚发布的 attempt manifest；`control-cancelled` 覆盖从未 claim queued 与 retry_wait，两者都证明无 active lease，再分别使用 null / 最大已关闭 attempt_no；`queue-timed-out` 仅 queued，`retry-wait-timed-out` 仅 retry_wait 且聚合既有 attempts，不创建 attempt；Run create 预算拒绝不创建 Run | `terminal_only@1`；validation kind 可映射 `restore_current_draft@1`，但不得发布领域 target/evidence |

`artifact.migrate@1` 的 success policy 不是留给实现动态猜测，而由 RunKindDefinition/RunRecord 冻结的 exact `MigrationCapabilityMatrixRefV1` 物化有限 family；其他 RunKind 的 matrix ref 必须 null。matrix digest 对 canonical payload 排除自身计算：`kind_defaults` 必须按 ArtifactKind enum 顺序**恰好覆盖每个值一次**，edges 按唯一 `(source_kind,source_payload_schema_id,target_payload_schema_id,target_meta_schema_version,target_dsl_grammar_version|null)` 排序；`publish_same_kind` edge 必须有 exact publication lineage policy ref，其他 capability 的 ref 必须为空。profile 的 MigrationEdge 先按该完整 tuple 查 exact matrix edge：命中时全部字段一致；未命中时只允许使用该 source kind 的 default，且 default 绝不能产生 `publish_same_kind`。重复、摘要不符或 wildcard readiness fail-closed。matrix registry 按唯一 `matrix_version` 保留 canonical 历史内容，digest/registry digest 均排除自身字段；Run create 与 terminal 重算 exact matrix digest，引用 Run/Artifact 保留期内不得删除。

初始 matrix 对 `run_result|run_failure|patch|constraint_proposal|rollback_request|cassette_bundle` 禁止 `publish_same_kind`：这些类型分别受 manifest projection、maker/producer、审批或 replay identity 的额外不变量约束，普通 migration Run 不能伪造；它们只能按 exact edge/default 产 report、needs_re_extract/needs_re_compile 或 409。其他 kind 也**不会因出现在 enum 就自动可发布**；只有 matrix 中显式 `publish_same_kind` 且其 kind-specific lineage policy与 foundations producer matrix 都验证通过的 exact source→target edge，registry 才物化 `artifact-migration-published-<K>@1` / code=`artifact_migration_published.<K>`，primary report=1、output 同 kind/exact target schema=1。

公共 family 仍包括：`artifact-migration-reported@1` 只允许 `publish_mode=report_only` 且只产 report；`artifact-migration-compatible@1` 只在 source 已是 exact target schema/version 时产 compatible report；`artifact-migration-needs-action@1` 按 matrix capability 只产 needs_re_extract/needs_re_compile report。publisher 交叉校验 payload mode、source truth、profile edge、matrix capability、report typed checks/status/ID、lineage 与 producer matrix，因此每个业务结果恰有一个可达 policy；report-only/needs-action 不发布目标，publish 不得换 kind，执行异常仍走公共 failure policies。完整 enum coverage 的含义是“每个 kind 都有明确默认处置”，不是“每个 kind 都能伪造一个迁移后实例”。

每个表内 Artifact rule 的 `rule_id` 由 policy 固定为语义名（`primary|preview|config-export|checker|simulation|review|scenario|trace|evidence-set|auto-apply-proof|compile-evidence|regression|migration-report|migrated-artifact`，同 policy 内唯一），其 `lineage_policy_ref` 固定为 `{policy_id}/{rule_id}-lineage@1` 的 exact version+digest。对应 policy 内容至少锁死这些 direct roles：preview=`base+patch`；config=`preview+constraint`；checker/review/simulation=`snapshot/preview+可选 constraint+声明的 scenario`；scenario=`preview+config+constraint`；task-suite=`preview+config+constraint+全部 scenarios`；playtest trace=`config+constraint+task-suite+选中 scenarios`；constraint candidate=`proposal+可选 base`；compile evidence=`proposal+可选 base+可选 candidate`；EvidenceSet=`subject+可选 target+全部 Finding/supporting/compile/regression evidence`；auto-apply proof=`subject+target+EvidenceSet+全部 qualified outcome/deterministic oracle/regression evidence`；migration report=`source+可选 migrated-artifact`，migrated artifact=`source`。LLM 路径另由 `runtime-parents@1` 纳入 exact rendered/cassette parents。每个 policy 的 `parent_rules`/VersionTuple projection 仍须作为完整 canonical policy 存在并在 Run 创建时冻结 digest；上述命名规则不能替代内容摘要，未知或摘要不符 readiness fail-closed。

其余 active RunKind 也必须为每个可达 success outcome/failure code 物化同结构 policy；§5.5 “Primary / additional Artifact”表只是这些 policy 的人类可读派生投影，registry load 时必须反向校验，不形成第二真相源。未知 outcome/failure code 或缺 policy 使 readiness 失败。

匹配 policy 后 publisher 再按 terminal outcome 在同一 UoW 执行写入：generation gate pass 原子写 Patch + preview `ir_snapshot`/条件 `config_export` + gate evidence + draft ApprovalItem，gate reject 写 evidence-only rejected Patch/preview/gate evidence + typed RunFailure、但无 SubjectHead/ApprovalItem；patch repair verified success 写 superseding Patch revision + 新 preview/条件 config/verifier evidence、CAS 旧 item→superseded 并创建新 draft ApprovalItem，`repair_unverified` 只写 RunFailure/evidence且不推进 SubjectHead；constraint-proposal agent success 写 proposal Artifact + draft ApprovalItem；review/checker/simulation/playtest/validation success 按 finding-output policy 原子写对应 Artifact + FindingRevision + RunFindingLink；validation completion 写 EvidenceSet、复核 supporting artifacts 与 exact target binding后做 Approval workflow CAS。validation execution failure/cancel/timeout 仅在 item 仍 current+validating+active_run 匹配时退回 draft，已 superseded 则只终结 Run/Cost/Event/audit。任一 Artifact/Finding/workflow hook 写失败则整个 terminal publication 回滚；明确的 `subject_superseded` 是非重试业务终止。

PreparedRunFailure 的 `artifacts` 可空且只承载 plan 中 run-scope 或 retry attempt policy 明确允许的新领域 evidence；terminal publisher 先在**完整 publication plan**内做唯一 rule allocation，再与 `runtime-parents@1` 要求的本 attempt intermediates及（run-scope 时）**全部 closed-attempt 独立 failure manifests**做 canonical 去重，按 §3.3 projection 公式精确写入各 scope 的 `RunFailureV1.evidence_artifact_ids`/lineage。final attempt-close policy 固定不消费业务 artifacts/dispositions，run-scope policy才消费并聚合，不能丢证据、漏重试/最终 attempt 历史、重复计入或引用未发布 Artifact。业务拒绝、transient/permanent dependency、quota、integrity、execution、cancel、timeout、lease 与 superseded 各自使用冻结 code/class allowlist；只有明确列出的业务拒绝 run-scope hook 可以发布领域 evidence，绝不能借 failure hook 绕过 SubjectHead/ApprovalItem 状态机。

**Run payload DTO（`RunPayloadEnvelope.params` 的封闭 union）**：

```text
RefReadBindingV1 { ref_name, expected_ref:RefValue|null }  # 字段必有；null=必须不存在，绝非“未提供”
GraphSelectionV1 { mode:full|ids, entity_ids[], relation_ids[] }  # full 时两数组必须空；ids 时稳定排序去重
PromptGoalBindingV1 { source_artifact_id, expected_payload_hash }  # purpose 由字段固定为 user_goal；客户端不能自报 trust/purpose
ConfigExportFileV1 { relative_path, media_type, content_sha256, size_bytes, content_bytes }
ConfigExportPackageV1 {
  package_schema_version:config-export-package@1,
  export_profile:ProfileRefV1, target_environment_profile:ProfileRefV1, env_contract_version,
  source_preview_artifact_id, constraint_snapshot_artifact_id, format_schema_id,
  files:[ConfigExportFileV1...]{min_items=1}
}
ValidationSubjectBindingV1 {
  approval_id, expected_workflow_revision, subject_head_revision,
  subject_artifact_id, subject_digest, active_validation_run_id
}

GenerationProposePayloadV1 {
  schema_version:generation-propose@1, base_snapshot_artifact_id, constraint_snapshot_artifact_id?,
  findings:[FindingEvidenceBindingV1...],
  objective_goal:PromptGoalBindingV1, domain_scope, target:RefReadBindingV1, generation_policy:ProfileRefV1,
  candidate_export_profiles:[ProfileRefV1...]
}
PatchRepairPayloadV1 {
  schema_version:patch-repair@1, subject_patch_artifact_id, expected_subject_head_revision, expected_workflow_revision,
  base_snapshot_artifact_id, preview_snapshot_artifact_id, constraint_snapshot_artifact_id?, validation_evidence_artifact_id,
  findings:[FindingEvidenceBindingV1...], target:RefReadBindingV1, repair_policy:ProfileRefV1,
  checker_profiles:[ProfileRefV1...], simulation_profiles:[ProfileRefV1...],
  regression_suite_artifact_ids[], candidate_export_profiles:[ProfileRefV1...]
}
ConstraintProposalProposePayloadV1 {
  schema_version:constraint-proposal-propose@1, source_artifact_ids[], base_constraint_snapshot_artifact_id?,
  domain_scope:DomainScope, authoring_goal:PromptGoalBindingV1, dsl_grammar_version, extraction_policy:ProfileRefV1
}
ReviewRunPayloadV1 {
  schema_version:review-run@1, snapshot_artifact_id, constraint_snapshot_artifact_id?, selection:GraphSelectionV1,
  review_profile:ProfileRefV1, checker_profiles:[ProfileRefV1...], simulation_profiles:[ProfileRefV1...], llm_triage_policy:ProfileRefV1?
}
CheckerRunPayloadV1 {
  schema_version:checker-run@1, snapshot_artifact_id, constraint_snapshot_artifact_id?, selection:GraphSelectionV1,
  checker_profile:ProfileRefV1, checker_ids[], defect_classes[]
}
SimulationRunPayloadV1 {
  schema_version:simulation-run@1, snapshot_artifact_id, constraint_snapshot_artifact_id?, scenario_artifact_id?,
  simulation_profile:ProfileRefV1, workload_profile:ProfileRefV1, replication_count, horizon_steps
}
PlaytestEpisodeBindingV1 { episode_id, scenario_spec_artifact_id }
PlaytestRunPayloadV1 {
  schema_version:playtest-run@1, config_artifact_id, constraint_snapshot_artifact_id, task_suite_artifact_id,
  episodes:[PlaytestEpisodeBindingV1...], environment_profile:ProfileRefV1, planner_policy:ProfileRefV1,
  max_steps_per_episode, interaction_mode:autonomous|bounded_choice
}
CompletionOracleRefV1 { oracle_id, version, params_schema_id, params }
CompletionOracleDefinitionV1 { oracle_id, version, params_schema_id, result_schema_id, executor_key }
CompletionOracleRegistryRefV1 { registry_version, digest }
CompletionOracleRegistryV1 {
  registry_schema_version, registry_version, definitions:[CompletionOracleDefinitionV1...], registry_digest
}
ScenarioResetBindingV1 { reset_schema_id, payload_hash, payload }
ScenarioSpecV1 {
  scenario_spec_schema_version:scenario-spec@1, scenario_id,
  source_preview_artifact_id, config_export_artifact_id, constraint_snapshot_artifact_id,
  environment_profile:ProfileRefV1, env_contract_version, domain_scope:DomainScope,
  reset_binding:ScenarioResetBindingV1
}
TaskEpisodeV1 {
  episode_id, scenario_spec_artifact_id, completion_oracle:CompletionOracleRefV1,
  domain_scope:DomainScope, reset_binding:ScenarioResetBindingV1, step_budget
}
TaskSuiteV1 {
  task_suite_schema_version:task-suite@1, suite_profile:ProfileRefV1,
  source_preview_artifact_id, config_export_artifact_id, constraint_snapshot_artifact_id,
  environment_profile:ProfileRefV1, env_contract_version,
  completion_oracle_registry_ref:CompletionOracleRegistryRefV1,
  episodes:[TaskEpisodeV1...]{min_items=1}
}
TaskSuiteDerivePayloadV1 {
  schema_version:task-suite-derive@1, source_preview_artifact_id, config_artifact_id,
  constraint_snapshot_artifact_id, derivation_profile:ProfileRefV1, environment_profile:ProfileRefV1,
  completion_oracle_registry_ref:CompletionOracleRegistryRefV1
}
PatchValidationPayloadV1 {
  schema_version:patch-validation@1, subject:ValidationSubjectBindingV1, base_snapshot_artifact_id,
  preview_snapshot_artifact_id, candidate_config_export_artifact_ids[], target:RefReadBindingV1,
  validation_policy:ProfileRefV1, checker_profiles:[ProfileRefV1...], simulation_profiles:[ProfileRefV1...],
  findings:[FindingEvidenceBindingV1...], review_artifact_ids[], playtest_trace_artifact_ids[], regression_suite_artifact_ids[]
}
SolverEngineRefV1 { engine_id, version }
ConstraintValidationPayloadV1 {
  schema_version:constraint-validation@1, subject:ValidationSubjectBindingV1, base_constraint_snapshot_artifact_id?,
  target:RefReadBindingV1, dsl_grammar_version, compiler_profile:ProfileRefV1, differential_engines:[SolverEngineRefV1...],
  golden_suite_artifact_id?, regression_suite_artifact_ids[], validation_policy:ProfileRefV1
}
RollbackValidationPayloadV1 {
  schema_version:rollback-validation@1, subject:ValidationSubjectBindingV1, ref_name, expected_current_ref:RefValue,
  target_artifact_id, target_history_revision, rollback_profile:ProfileRefV1,
  schema_compatibility_policy:ProfileRefV1, impact_profiles:[ProfileRefV1...], regression_suite_artifact_ids[]
}
BenchRunPayloadV1 {
  schema_version:bench-run@1, dataset_artifact_id, benchmark_spec_artifact_id, partition_ids[],
  evaluator_profile:ProfileRefV1, repetition_count, execution_scope:execute_cases|aggregate_results,
  case_result_artifact_ids[]
}
ArtifactMigrationPayloadV1 {
  schema_version:artifact-migration@1, source_artifact_id, target_payload_schema_id,
  target_meta_schema_version, target_dsl_grammar_version?,
  migrator:ProfileRefV1, publish_mode:report_only|publish_migrated_artifact
}
SourceReadableCheckResultV1 { result_schema_version:migration-source-readable-result@1,
                              source_payload_hash, reader_schema_id?, canonical_payload_hash?,
                              readable:true|false|unavailable }
TargetReaderResolvedCheckResultV1 { result_schema_version:migration-target-reader-result@1,
                                    target_payload_schema_id, reader_schema_id?, registry_entry_digest?,
                                    resolved:true|false|unavailable }
MigrationPathResolvedCheckResultV1 { result_schema_version:migration-path-result@1,
                                     source_payload_schema_id, target_payload_schema_id,
                                     migration_registry_digest, edge_ids[], path_digest?,
                                     resolved:true|false|unavailable }
TargetPayloadValidCheckResultV1 { result_schema_version:migration-target-valid-result@1,
                                  target_payload_hash?, target_payload_schema_id, validator_tool_version,
                                  valid:true|false|unavailable }
CanonicalRoundTripCheckResultV1 { result_schema_version:migration-round-trip-result@1,
                                  first_canonical_hash?, round_trip_canonical_hash?, equal:true|false|unavailable }
SemanticInvariantsCheckResultV1 { result_schema_version:migration-semantic-result@1,
                                  invariant_profile:ProfileRefV1, invariant_set_digest,
                                  evaluated_count, evaluation_complete, failed_invariant_ids[] }
GoldenReplayCheckResultV1 { result_schema_version:migration-golden-replay-result@1,
                            fixture_set_digest, case_count, replay_complete, failed_case_ids[],
                            replay_result_digest?, comparison_digest? }
PublishBindingCheckResultV1 { result_schema_version:migration-publish-binding-result@1,
                              source_kind:ArtifactKind, target_kind:ArtifactKind, target_payload_schema_id,
                              lineage_policy_ref?:ArtifactLineagePolicyRefV1,
                              version_transition_policy_ref?:VersionTransitionPolicyRefV1,
                              binding_valid:true|false|unavailable }
MigrationCheckResultV1 = SourceReadableCheckResultV1 | TargetReaderResolvedCheckResultV1 |
                         MigrationPathResolvedCheckResultV1 | TargetPayloadValidCheckResultV1 |
                         CanonicalRoundTripCheckResultV1 | SemanticInvariantsCheckResultV1 |
                         GoldenReplayCheckResultV1 | PublishBindingCheckResultV1
MigrationCheckV1 {
  check_schema_version:migration-check@1, check_id,
  check_type:source_readable|target_reader_resolved|migration_path_resolved|target_payload_valid|
             canonical_round_trip|semantic_invariants|golden_replay|publish_binding,
  status:passed|failed|unproven|not_applicable,
  reason_code?, result?:MigrationCheckResultV1
}
MigrationReportV1 {
  report_schema_version:migration-report@1, source_artifact_id, source_kind:ArtifactKind, source_payload_schema_id,
  target_payload_schema_id, target_meta_schema_version, target_dsl_grammar_version?, migrator:ProfileRefV1,
  requested_publish_mode, status:compatible|migration_available|migrated|needs_re_extract|needs_re_compile,
  migrated_artifact_id?, reason_code?, checks:[MigrationCheckV1...]
}
DrDrillPayloadV1 {
  schema_version:dr-drill@1, dr_plan:ProfileRefV1, recovery_catalog_entry_id, expected_checkpoint_id,
  restore_target_profile:ProfileRefV1, verification_profile:ProfileRefV1, destroy_restored_target_after_verification
}
RunKindPayload = GenerationProposePayloadV1 | PatchRepairPayloadV1 | ConstraintProposalProposePayloadV1 | ReviewRunPayloadV1 |
                 CheckerRunPayloadV1 | SimulationRunPayloadV1 | PlaytestRunPayloadV1 | TaskSuiteDerivePayloadV1 |
                 PatchValidationPayloadV1 | ConstraintValidationPayloadV1 | RollbackValidationPayloadV1 |
                 BenchRunPayloadV1 | ArtifactMigrationPayloadV1 | DrDrillPayloadV1
```

所有字符串/数组/计数在 JSON Schema 中冻结长度与上限；ID 数组 canonical 排序去重，ProfileRef 数组 stable unique，Finding bindings 按 `finding_id` stable 排序且每个 finding series 只允许一个 exact revision。payload 引用的每个 Artifact（含 PromptGoalBinding、Finding evidence、Playtest 选中 ScenarioSpec）必须与 envelope `input_artifact_ids` **精确同集**（不能藏额外输入），kind/hash/VersionTuple 必须解析一致；Finding binding 的 revision/digest 和 evidence Artifact 由服务端重算，延迟读取“当前 Finding”或只绑 finding_id 均禁止。Patch validation 将同一 stable bindings 复制进 EvidenceSet.finding_bindings，并以 `validation_run_id` 反查 immutable Run payload逐字段对拍；二者不等不得发布 passed/unproven EvidenceSet。每个 profile field 必须在 `resolved_profiles` 恰有一条 field-path binding，ProfileRef/kind/hash/catalog snapshot 与 §5.3 定义逐字段一致，worker 禁读“当前默认 profile”。字段允许 kind 冻结为：goal=`source_raw` 且 ProvenanceV1 必须由服务端认证来源分配 `trusted_internal` 与允许 `user_goal` 的 SourceKindId，snapshot/preview=`ir_snapshot`，constraint=`constraint_snapshot`，constraint source=`source_raw|source_rendered`，scenario=`scenario_spec`，task suite=`task_suite`，regression suite=`regression_suite`，golden suite=`golden_suite`，Finding evidence=`review_report|checker_run|simulation_run|playtest_trace|validation_evidence|regression_evidence`，Bench dataset/spec=`bench_dataset`/`benchmark_spec`，Bench case result=`checker_run|simulation_run|playtest_trace|review_report|run_result|validation_evidence|regression_evidence`，playtest config/candidate config=`config_export`；migration source 是唯一允许任意 ArtifactKind 的字段。DR drill 不直接信主库中的 manifest Artifact ID，而以签名 `recovery_catalog_entry_id` 为自举输入；entry 内的 manifest 必须是 `operational_evidence` 且 payload schema=`backup-object-manifest@1`，验签并导入后才成为该 Run 的 lineage/input Artifact。subject/target 则按 `SubjectKind`、ApprovalItem 与 RollbackPolicy 精确交叉校验。LLM mode 与 seed 由下表的 RunKindDefinition 约束，不能仅凭字段是否出现猜测。Agent 约束提议只能走 `constraint_proposal.propose@1`，human typed draft 只走同步 resource endpoint，二者都创建 draft ApprovalItem，随后仍必须 human revision 才可 validate/publish。

Artifact migration 在 Run 创建时加载 source Artifact，冻结其 exact kind/payload schema，并要求 `target_payload_schema_id` 位于该 RunKindDefinition 版本为 source kind 固定的非空 exact allowlist；migrator profile 的 input/output schema、target meta/DSL version 必须一致，未知迁移边直接 409 而非启动任意 handler。MigrationReport 的 source/target/profile/request 字段逐字段复制 payload 与 source truth；只有 `status=migrated` 时 migrated_artifact_id 必填，`compatible|migration_available|needs_re_extract|needs_re_compile` 时该 ID 必须为空。report reason 在 `compatible|migrated` 时必须 null，在 `migration_available|needs_re_extract|needs_re_compile` 时必填版本化 code。checks 按固定 check-type enum 顺序且 `check_id=check_type`；passed 禁 reason，failed/unproven 必有 reason，not_applicable 必有版本化 reason。executed check（passed/failed/unproven）的 `result` 必填且其 discriminator 必须与 check_type 唯一对应，not_applicable 的 result 必须 null；每种 result 的数组/字符串/计数有 JSON Schema 硬上限并直接受 MigrationReport Artifact payload hash 保护，不引用悬空 evidence Artifact，也不允许任意 details 字典。

布尔 verdict result 只有 true/false/unavailable 分别对应 passed/failed/unproven；round-trip 同理。source-readable passed 还要求 reader/canonical hash 非空；target-reader passed 要求 reader/registry digest 非空；path passed 要求非空 edge IDs/path digest；target-valid passed 要求 target hash；publish-binding passed 要求两份 exact policy ref。semantic 只有 `evaluation_complete=true + failed=[]` 为 passed，complete + 非空 failed 为 failed，未完成为 unproven；golden 只有 `replay_complete=true + failed_case_ids=[]` 且 replay/comparison digest 非空为 passed，complete + 非空 failed 为 failed，未完成为 unproven，其 fixture-set digest 必须等于 exact profile edge。任一 status/result/presence 不一致即拒绝发布。

每份 report 恰有上述八个 check_type（未来新增需 bump report schema）：`compatible` 要求 source/target-reader/target-valid/round-trip/semantic 全 passed，path/golden/publish-binding 显式 not_applicable；`migration_available` 要求 source/target-reader/path passed，其余因 `report_only` not_applicable；`migrated` 要求七个非-golden check 全 passed，`golden_replay` 默认也必须 passed，只有本次 exact source-kind/schema→target/meta/DSL edge 在 exact `MigrationProfileDetailsV1` 中明确 `golden_replay_policy=not_applicable` 时才允许 `not_applicable` 且 reason 必须逐字段等于 edge 的版本化 code；`needs_re_extract` / `needs_re_compile` 要求 source-readable passed、migration-path-resolved failed 或 unproven并使用对应 reason，其余未执行项 not_applicable。golden check 不能在运行时任意省略。report status、typed results、profile edge 与 outcome policy 不一致即 fail-closed。迁移输出必须保持 source ArtifactKind，payload 精确使用 target schema，lineage direct parent 为 source，VersionTuple 差异只由 versioned migration lineage policy 解释；原 Artifact 永不改写。

Generation/Repair 的 export 条件规则固定：`candidate_export_profiles=[]` 时允许 `constraint_snapshot_artifact_id=null`，且 `config_export` 数量必须为 0；profiles 非空时 constraint 必填并进入 envelope exact input set，输出 `config_export` 数量必须等于 profile 数量，每个 payload 精确绑定一个唯一 ProfileRef。每个 `config_export` Artifact 的 payload schema 必须是 `ConfigExportPackageV1`：`export_profile` 等于请求中的 exact config_export profile，`target_environment_profile/env_contract_version/format_schema_id/package_schema_version` 等于该 immutable profile definition，preview/constraint ID 等于本候选；`files` 按 NFC `relative_path` 排序且路径 stable unique，拒绝绝对路径、`..` 与分隔符歧义，每个内容 hash/size 复核。package canonical encoding 对 manifest 字段、文件长度和原始 bytes 做确定性 framing，Artifact.object_ref 直接指完整 package（无未纳入 GC live-set 的嵌套对象）；publisher 可按 schema 读取受 payload hash 保护的 `/export_profile` 做 identity binding。Journey A 必须使用非空 profiles + exact constraint snapshot，不能走无 export 分支冒充通过。

`task_suite.derive@1` 的 envelope inputs 精确为 preview/config/constraint；创建 Run 前先重验 config package/lineage/VersionTuple 精确指向该 preview+constraint。config 的 `env_contract_version` **必须非 null**，其 package `target_environment_profile` 必须等于 payload.environment_profile，且三者必须逐字段等于 exact EnvironmentProfileDetails contract，否则 409；“缺字段”不按兼容默认值通过。deriver 先按 versioned profile 产生一组 `ScenarioSpecV1`，再发布恰好一个非空 `TaskSuiteV1`。每个 ScenarioSpec 的 preview/config/constraint/environment/env-contract/domain 与 derive 输入完全相等；`reset_binding.reset_schema_id` 等于 environment contract，payload 按该 schema 校验且 `payload_hash=sha256(canonical_json(payload))`。suite 的 `suite_profile/source_preview/config/constraint/environment_profile/env_contract_version/completion_oracle_registry_ref` 必须逐字段等于 derive payload与 profile；episode ID、scenario ID 与 scenario Artifact ID 均 stable unique，TaskEpisode 的 domain/reset_binding 必须等于所引 ScenarioSpec，episodes 对 scenario IDs 的引用与 task_suite lineage 中 scenario parents 一一覆盖，并由 `source=prepared_primary_payload,collection_pointer=/episodes,collection_item_pointer=/scenario_spec_artifact_id,artifact_value_source=artifact_id` 的合法 RFC 6901 count binding 机器校验。`registry_digest=sha256(canonical_json({registry_schema_version,registry_version,definitions}))`（definitions 按 `(oracle_id,version)` 排序且 registry_digest 自身排除）；CompletionOracleRef 必须按 payload 冻结的 `{registry_version,digest}` 从历史 registry 唯一解析，params 按 exact schema 校验，`executor_key` 只由可信组合根映射且确定性 oracle 才能决定完成/失败，客户端不能传 callable。

TaskSuite lineage 精确包含 typed roles `preview/config/constraint/scenarios`；每个 ScenarioSpec lineage 只含 typed `preview/config/constraint`，避免环。ScenarioSpec 的 VersionTuple 对适用的 `doc_version/ir_snapshot_id/constraint_snapshot_id` 从 preview/config/constraint 共同继承并要求相等，`env_contract_version` 从 exact environment profile 继承，`tool_version` 来自 deriver，其他字段按 producer matrix 显式 producer/null；TaskSuite 对同一组 doc/IR/constraint/env 字段作相同投影，tool 仍来自 deriver，不机械 merge scenario producer-local 字段。创建 Playtest 时逐字段核对 config、constraint、environment profile/env contract、scenario reset schema 和 suite tuple；`episodes` 必须按 episode_id stable unique 排序且是 TaskSuite episodes 的**非空 exact 子集**，每个 `{episode_id,scenario_spec_artifact_id}` 与 suite row 逐字段相等（执行全部也要显式列全，空数组不暗示 all），所选 ScenarioSpec IDs 连同 config/constraint/suite 一起进入 Run envelope exact input set，因而 playtest trace 的 direct `selected_scenarios` lineage role 可合法解析；`max_steps_per_episode` 不得超过任一所选 episode 的正 `step_budget`。修复后 preview/config 变化必须重新 derive，旧 suite 不得复用。Aureus 只是一个 environment/profile fixture，契约不含游戏名。

Constraint validation 的 `differential_engines` 按 `(engine_id,version)` stable unique，要求至少两个 exact engine ref；不得用裸版本字符串或进程默认 solver。`ConstraintCompileStageV1.stage=differential` 时 engine_id+engine_version 必填，且每个请求 engine 恰有一条 stage；其他 stage 禁止这两个字段。`status=passed` 时 reason_code 必须为空，`failed|unproven|not_applicable` 时 reason_code 必填并来自版本化 allowlist；engine 集合、stage 集合或版本任一不等即 validation failed/unproven，不能静默少跑一台引擎。

`generation:propose` / `constraints:propose` 的资源请求可以接收用户输入文本，但组合根必须先按 blob-first 规则创建不可变 `source_raw` Artifact：human session 只可由服务端分配 SourceKindId=`authenticated_human_goal`，受信 service 则为 `trusted_service_goal`；trust、purpose、origin_ref 与 connector identity 均由当前认证上下文、SourceKindRegistry 和版本化 provenance policy 分配，客户端字段一律不采信。随后同一平台命令才创建引用该 Artifact/hash 的 Run，失败不得留下可执行 Run。worker 只从 binding 构造 `PromptPartV1(purpose=user_goal)`，经 canonical renderer 产生并在调用前发布 `source_rendered` Artifact；该 rendered Artifact 是所有实际 LLM Run 的条件 additional kind，并作为 Patch/constraint proposal/review/playtest/Bench Agent 产物的 lineage parent，成功时进入 RunResult produced IDs，非成功时进入 RunFailure evidence/lineage。`source_raw/source_rendered` 读取按敏感内容 RBAC；不得把裸 goal、完整 prompt 或 raw response 回填 telemetry。

**RunKind registry（完整创建面与命令面）**：

| Run kind | Payload schema | Primary / additional Artifact | Creation mode | Allowed command schemas |
|---|---|---|---|---|
| `generation.propose@1` | `GenerationProposePayloadV1` | gate pass: `patch` / `ir_snapshot`,条件 `config_export`(数量=export profiles),`checker_run`,`simulation_run`,`review_report`；gate reject: evidence-only `patch`,`ir_snapshot`,gate evidence + RunFailure | `resource_endpoint_only` | `run.cancel@1` |
| `patch.repair@1` | `PatchRepairPayloadV1` | `patch` / `ir_snapshot`,条件 `config_export`(数量=export profiles),`checker_run`,`simulation_run`,`regression_evidence`；unverified 无 Patch/preview/config revision | `resource_endpoint_only` | `run.cancel@1` |
| `constraint_proposal.propose@1` | `ConstraintProposalProposePayloadV1` | `constraint_proposal` / 生成只产 draft | `resource_endpoint_only` | `run.cancel@1` |
| `review.run@1` | `ReviewRunPayloadV1` | `review_report` / `checker_run`,`simulation_run` | `generic_runs_endpoint` | `run.cancel@1` |
| `checker.run@1` | `CheckerRunPayloadV1` | `checker_run` | `generic_runs_endpoint` | `run.cancel@1` |
| `simulation.run@1` | `SimulationRunPayloadV1` | `simulation_run` | `generic_runs_endpoint` | `run.cancel@1` |
| `task_suite.derive@1` | `TaskSuiteDerivePayloadV1` | `task_suite` / `scenario_spec`(1..N) | `resource_endpoint_only` | `run.cancel@1` |
| `playtest.run@1` | `PlaytestRunPayloadV1` | `playtest_trace` | `resource_endpoint_only` | `run.cancel@1`,`playtest.provide-input@1` |
| `patch.validate@1` | `PatchValidationPayloadV1` | `validation_evidence` / `regression_evidence`；绑定 exact Finding revisions + 既有 preview/review/playtest；仅专用 deterministic outcome 另产 `auto-apply-proof@1` | `resource_endpoint_only` | `run.cancel@1` |
| `constraint_proposal.validate@1` | `ConstraintValidationPayloadV1` | `validation_evidence` / `regression_evidence` + 条件 candidate `constraint_snapshot`（compile 形成 candidate 时恰好 1，否则 0） | `resource_endpoint_only` | `run.cancel@1` |
| `rollback.validate@1` | `RollbackValidationPayloadV1` | `validation_evidence` / `regression_evidence` | `resource_endpoint_only` | `run.cancel@1` |
| `bench.run@1` | `BenchRunPayloadV1` | `bench_report` | `generic_runs_endpoint` | `run.cancel@1` |
| `artifact.migrate@1` | `ArtifactMigrationPayloadV1` | `migration_report`；仅 `artifact_migration_published.<source-kind>` 另产同 kind、exact target schema Artifact | `internal_only` | `run.cancel@1` |
| `dr.drill@1` | `DrDrillPayloadV1` | `operational_evidence` | `internal_only` | `run.cancel@1` |

其余 RunKindDefinition 字段同样冻结，不留隐式 handler：

| Run kind | Required permission | Executor / success publisher | Retry policy | LLM modes | Seed policy |
|---|---|---|---|---|---|
| `generation.propose@1` | `{propose,patch,payload.domain_scope}` | `generation_proposer@1` / `publish_gated_patch_preview@1` | `llm_transient@1` | live,record,replay | forbidden |
| `patch.repair@1` | `{propose,patch,subject domain}` | `repair_search@1` / `publish_patch_revision_preview@1` | `llm_transient@1` | live,record,replay | profile_dependent (`subseed@1`) |
| `constraint_proposal.propose@1` | `{propose,constraint_proposal,payload.domain_scope}` | `constraint_proposer@1` / `publish_constraint_proposal_draft@1` | `llm_transient@1` | live,record,replay | forbidden |
| `review.run@1` | `{run,review,payload selection domain}` | `review_runner@1` / `publish_review@1` | `composite_transient@1` | not_applicable,live,record,replay | profile_dependent (`subseed@1`) |
| `checker.run@1` | `{run,checker,payload selection domain}` | `checker_runner@1` / `publish_checker@1` | `deterministic_job@1` | not_applicable | forbidden |
| `simulation.run@1` | `{run,simulation,payload selection domain}` | `simulation_runner@1` / `publish_simulation@1` | `deterministic_job@1` | not_applicable | required (`subseed@1`) |
| `task_suite.derive@1` | `{derive,task_suite,config/constraint domain}` | `task_suite_deriver@1` / `publish_task_suite@1` | `deterministic_job@1` | not_applicable | forbidden |
| `playtest.run@1` | `{run,playtest,task-suite domain}` | `playtest_runner@1` / `publish_playtest@1` | `agent_environment@1` | live,record,replay | required (`subseed@1`) |
| `patch.validate@1` | `{validate,patch,subject domain}` | `patch_validator@1` / `publish_validation_completion@1` | `validation_job@1` | not_applicable | profile_dependent (`subseed@1`) |
| `constraint_proposal.validate@1` | `{validate,constraint_proposal,subject domain}` | `constraint_validator@1` / `publish_validation_completion@1` | `validation_job@1` | not_applicable | profile_dependent (`subseed@1`) |
| `rollback.validate@1` | `{validate,rollback_request,subject domain}` | `rollback_validator@1` / `publish_validation_success@1` | `validation_job@1` | not_applicable | profile_dependent (`subseed@1`) |
| `bench.run@1` | `{run,bench,dataset domain}` | `bench_runner@1` / `publish_bench@1` | `deterministic_job@1` | not_applicable,live,record,replay | profile_dependent (`subseed@1`) |
| `artifact.migrate@1` | `{migrate,artifact,artifact domain}` | `artifact_migrator@1` / `publish_migration@1` | `migration_job@1` | not_applicable | forbidden |
| `dr.drill@1` | `{drill,operations,null}` | `dr_drill_runner@1` / `publish_operational_evidence@1` | `operational_job@1` | not_applicable | forbidden |

表中 permission 是结构化 `{action,resource_kind,domain_scope}`，domain 必须由加载后的真实 Artifact/subject/task suite 解析并与 payload 声明交叉校验，不能直接信客户端文本。初始表项全部 `status=active`。review 的 `llm_triage_policy=null` 时 mode 必须 `not_applicable`，非空时必须是 live/record/replay。Bench `aggregate_results` 要求 case_result_artifact_ids 非空且 mode=`not_applicable`；`execute_cases` 要求该数组为空，并按 benchmark spec 是否含 agent cases选择 not_applicable 或 live/record/replay，整次 Bench 的全部 agent 调用共享一个有序 run-scoped cassette bundle。其余 mode/profile 不匹配直接 422/409。`TerminalPublisherHooks.on_success` 取表中 publisher；三个 validation kind 的 `on_failure/on_cancel/on_timeout` 均为 `publish_validation_non_success@1`，其余 kind 分别为 `publish_run_failure@1/publish_run_cancel@1/publish_run_timeout@1`，不能为 null 或进程默认函数。RetryPolicySnapshot 由表中 ID+版本解析并在 Run 创建时复制到 RunRecord；registry、policy、publisher 或 handler 缺失即 readiness 失败。

`patch.repair@1` 只能修当前 SubjectHead，并以 subject-head revision + Approval workflow revision 双 CAS 防止基于旧 Finding/决定修复。executor 同时加载原始 base、旧 Patch、其 preview、当前 failed/unproven validation evidence 和 exact Finding evidence，在 preview 上做有界 verifier-guided search，但成功产物必须是**相对原始 current-ref base 的完整组合 Patch revision**，绝不是以未获批 preview 为 base 的第二段 patch；`supersedes_artifact_id` 指旧 Patch，target ref 的 `{artifact_id,revision}` 必须仍与 payload 匹配。组合结果重新 exact-base apply，且通过 repair policy 的确定性 checker/sim/regression 后才允许 publisher 推进 SubjectHead。搜索耗尽、证据 stale、目标 ref 漂移或无法证明组合等价均终结为 `repair_unverified`，旧 head/item/ref 不变。

seed 契约：`required` 必填、`forbidden` 必须为 null；`profile_dependent` 时所有 versioned profile 都声明 `stochastic`，任一为真则 root seed 必填，否则必须为 null。所有 episode/case/replication/validation child 的 seed 用 `subseed@1 = uint64_be(first_8_bytes(sha256(canonical_json({root_seed,run_kind,profile_id,profile_version,case_id,replication_index}))))` 派生，完整 tuple 与 derivation version 写入 Evidence/Run payload；禁止 Python/process hash 或数组遍历偶然顺序。validation/review 中任何 sim/env regression 都受此规则约束。

`POST /runs` 只接受 `generic_runs_endpoint`；资源端点只能创建其固定 kind/version，不能透传客户端 kind；`internal_only` 只由持特权 permission 的可信 service 调 platform service。未知/disabled kind、schema ID、command schema 或 mode 一律 fail-closed。

每个 RunRecord 冻结 exact `failure_classifier`、`retry_policy{id,version,digest}`、`max_attempts/retry_not_before_utc?`；retry close 用注入式 DB UTC + sleeper/jitter policy + Retry-After 计算并**持久化** next eligible UTC，claim 以同一 DB time authority 原子检查 attempt count、budget、overall deadline 与 `now>=retry_not_before_utc`，禁止热循环或各 worker 临时改策略。`RetryDecisionV1.decision=retry` 时 decision、RunRecord 与 `RetryScheduledDataV1` 三处 `retry_not_before_utc` 必填且逐字段相等；terminal 时 decision 与 RunRecord next-retry 均为 null且不发布 retry-scheduled event。RunFailure/Prepared/decision 的 cause code 与 failure class 逐字段相等。

**RunEvent registry（wire `event_type → data schema` 的唯一解释）**：

```text
RunEventDefinitionV1 { event_type, data_schema_id, attempt_scope:run|attempt|either, terminal, allowed_from_statuses[] }
RunEventRegistryV1 { registry_schema_version, registry_version, definitions:[RunEventDefinitionV1...] }

RunQueuedDataV1          { run_kind:RunKindRef, queue_deadline_utc, overall_deadline_utc }
CancelRequestedDataV1    { command_id, reason_code }
CommandAcceptedDataV1    { command_id, command_type, command_revision }
AttemptLeasedDataV1      { attempt_no, lease_expires_at }
AttemptStartedDataV1     { attempt_no, started_at, attempt_deadline_utc }
AttemptProgressDataV1    { attempt_no, phase_code, completed_units, total_units?, detail_artifact_id? }
LeaseExpiredDataV1       { attempt_no, failure_artifact_id, will_retry }
RetryScheduledDataV1     { attempt_no, failure_artifact_id, cause_code, failure_class,
                           retry_decision:RetryDecisionV1, retry_not_before_utc }
CommandOutcomeDataV1     { command_id, command_type, command_revision, outcome_code }
RunSucceededDataV1       { attempt_no, result_artifact_id }
RunTerminatedDataV1      { attempt_no?, failure_artifact_id, cause_code }
```

| Event type | Data schema | Scope | Allowed from Run status | Terminal |
|---|---|---|---|---|
| `run.queued` | `RunQueuedDataV1` | run | create | 否 |
| `run.cancel_requested` | `CancelRequestedDataV1` | run | queued,leased,running,retry_wait | 否 |
| `run.command_accepted` | `CommandAcceptedDataV1` | run | leased,running | 否 |
| `attempt.leased` | `AttemptLeasedDataV1` | attempt | queued,retry_wait | 否 |
| `attempt.started` | `AttemptStartedDataV1` | attempt | leased | 否 |
| `attempt.progress` | `AttemptProgressDataV1` | attempt | running | 否 |
| `attempt.lease_expired` | `LeaseExpiredDataV1` | attempt | leased,running | 否 |
| `attempt.retry_scheduled` | `RetryScheduledDataV1` | attempt | leased,running | 否 |
| `run.command_applied` | `CommandOutcomeDataV1` | either | queued,leased,running,retry_wait | 否 |
| `run.command_rejected` | `CommandOutcomeDataV1` | either | queued,leased,running,retry_wait | 否 |
| `run.succeeded` | `RunSucceededDataV1` | attempt | running | 是 |
| `run.failed` / `run.cancelled` / `run.timed_out` | `RunTerminatedDataV1` | either | queued,leased,running,retry_wait | 是 |

registry 冻结 allowed-from 状态集合并由状态机生成测试穷举；表中 `either` 用于无 active attempt 的控制面终结与 active attempt 命令。active attempt 终态事件是 attempt-scoped，envelope 与 data 的 `attempt_no` 都等于 current attempt；从未 claim 的 queued 终结是 run-scoped且二者都为 null；retry_wait 控制面终结也是 run-scoped，envelope `attempt_no=null`，但 `RunTerminatedDataV1.attempt_no` 必须等于最终 RunFailure 的最大已关闭 attempt。所有 `RunTerminatedDataV1.failure_artifact_id/cause_code/attempt_no` 与所指 run-scope `RunFailureV1` 逐字段相等。`attempt.retry_scheduled` 只描述已关闭的当前 attempt 与下一次可 claim 时间，**不分配也不暴露未来 attempt_no/fencing token**；它们只在后续独立 claim 时由 RunRecord head 原子领取。`data_schema_id`、envelope `data_schema_version` 与实际 payload discriminator 三者必须一致，未知 event/data 组合拒绝发布；API 永不暴露 lease_id、fencing token、完整 prompt/raw response。新增 event 或字段必须新增 schema/version 并过 SSE/TS breaking-change 检查。

```text
POST /runs → 仅接受 `generic_runs_endpoint` kind；§3.2 同一 UoW 写 RunRecord + initial Event + run-level BudgetHold ReservationGroup + audit
           → 202 RunAccepted{run_id,status_url,events_url}
GET /runs/{id} → RunView{run_id,status,revision,attempt_no?,result_artifact_id?,failure_artifact_id?,terminal_cassette_artifact_id?,status_url,events_url}
                  queued 且尚无 attempt 时 attempt_no=null；一旦分配只返回当前/最后 attempt 的正整数编号
                  success 只指向 immutable `run_result`；非成功终态只指向 versioned `run_failure`
apps/worker: 经 DB claim lease(fencing) 发现 queued/retry_wait 任务；reaper 只负责关闭过期 attempt 并转 retry_wait/终态，后续再独立 claim；
             进程内 signal 仅降延迟提示，丢失可由 DB 扫描恢复（无双写窗口）；
             不在每个 Uvicorn worker 隐式起权威 worker；heartbeat 独立于可能阻塞 event loop 的 checker/sim/Agent 调用；优雅关闭先停 claim
SSE: RunEventEnvelope{schema_version,run_id,seq,event_type,attempt_no?,occurred_at,data_schema_version,data,trace_id?}；`attempt_no=null` 仅用于 queued/cancel_requested 等 run-level event，attempt-scoped event 必须为正整数（禁止 0）；wire canonical 固定为 `id:{seq}\nevent:{event_type}\ndata:{canonical envelope JSON}\n\n`：
     实现"读 seq>cursor → 等通知/轮询 → 再读 DB"（通知仅 hint，避免 backlog↔live 丢事件）；heartbeat=comment 不落库不推进 cursor
     慢客户端有界批次+背压；终态正常关闭；过保留期→410+earliest cursor；每次连接/续传重鉴权；传输是 at-least-once，
     前端状态层必须以 `(run_id,seq)` 幂等去重并持久化 last processed cursor，服务端只承诺已提交 seq 无漏，不宣称端到端 exactly-once delivery
CancelRunPayloadV1 { schema_version:run-cancel@1, reason_code, comment? }
PlaytestProvideInputPayloadV1 { schema_version:playtest-provide-input@1, interaction_id, expected_state_hash, choice_id }
RunCommandV1 { command_schema_version, command_id, client_id, client_seq, idempotency_key, expected_run_revision,
               type:cancel|provide_input, payload_schema_id, payload:CancelRunPayloadV1|PlaytestProvideInputPayloadV1 }
RunCommandRecordV1 {
  record_schema_version, run_id, command:RunCommandV1, request_hash, actor:AuditActor,
  status:pending|claimed|applied|rejected, revision, created_at, claimed_at?, claimed_attempt_no?, claimed_fencing_token?,
  applied_at?, result_event_seq?, rejection_code?
}
RunCommandViewV1 { run_id, command_id, client_id, client_seq, type, payload_schema_id,
                   status, revision, created_at, applied_at?, result_event_seq?, rejection_code? }
RunCommandAckV1 { ack_schema_version, command_id, client_id, client_seq, status:accepted|duplicate,
                  persisted_status:pending|claimed|applied|rejected, command_revision, run_revision }
RunCommandProblemV1 { problem_schema_version, command_id?, client_seq?, problem:Problem }
WS `/api/v1/runs/{id}/commands`: 客户端仅提 RunCommandV1；服务端 frame 仅 RunCommandAckV1|RunCommandProblemV1；
    `(run_id,client_id,client_seq)` 与 command_id 均唯一，同序号异 hash→409；command type/version 由 registry 按 RunKind 限定；
    **永不接触 fencing token**
    `run.cancel@1`/`playtest.provide-input@1` 字段如上，只有 RunKind registry 允许的组合可持久化；取消/交互命令**先提交再 ACK**；
    接受命令的 UoW 写 RunCommandRecord + 对应 run mutation/Event/audit：active cancel 原子置 cancel_requested 并将 command→applied，
    queued cancel 可直接走 terminal；provide_input 写 pending + command_accepted Event。worker 以自己 current lease/fencing 在 UoW CAS pending→claimed，
    消费后以同一 attempt/fencing CAS claimed→applied|rejected + RunEvent + audit；过期 worker 不得 claim/apply；reaper 在关闭过期 attempt 时把其未应用 claimed command CAS 回 pending，
    Run 终态 UoW 把剩余 pending/claimed command 标 `rejected(run_terminal)`，不留下永久悬空状态。重复请求返回原 record 的稳定 ACK，异 hash→409
    `GET /runs/{id}/commands?cursor&limit` 按 `(created_at,command_id)` 稳定返回 RunCommandViewV1 供断线恢复；record 的 request_hash/actor 只供内部审计视图
    握手 session/API-key 认证 + Origin；每条消息重做 run 级 authz + 序号/max size/背压；RunEvent 恢复仍只走 SSE cursor
REST `POST /api/v1/runs/{id}:cancel` 也接收完整 RunCommandV1（type 必须为 cancel、payload 必须为 run-cancel@1），
    走同一 `CommandService.submit`、唯一约束、request-hash、RunCommandRecord/Event/audit UoW 与稳定 ACK；不得另写“直接置 cancel flag”的旁路
```

### 5.6 OpenAPI + Journey A/B walking-skeleton 验收

OpenAPI 固定 FastAPI/Pydantic 版本后 canonical 落盘，**breaking-change 检查**（非逐字节）；SSE/WS envelope 导出版本化 JSON Schema；M4d TS 类型从这些契约生成。**IA/用户旅程随 M4c 定稿**（视觉留 M4d）。

**Journey A happy path（作者时刻，PRD §4.2/§12A.1）**：使用两个**完全独立 session client** A/B。`A 登录 → 从 execution-profile catalog 选择 exact active versions + exact constraint snapshot + 非空 export profiles → POST /generation:propose(REPLAY) → SSE 观察 preliminary gate → 读取 RunResult 中 Patch + 同一候选的 preview ir_snapshot/config_export/gate evidence → POST /task-suites:derive(同一 preview/config/constraint/environment profile) → 从 RunResult 取得 exact task_suite/scenarios → POST /runs(review.run@1, 同一 preview, REPLAY) 并读取确定性/LLM 建议分区 Review Report → POST /playtest:run(同一 config/constraint/task_suite/environment/planner profile + seed + exact episode/scenario bindings, REPLAY) → 读取 playtest_trace 并从 GET /runs/{id}/findings 取得该 Run 原子发布的 exact Finding revision/digest → :validate(Patch + exact preview/config/review/playtest + FindingEvidenceBindingV1 集合) 得 validation_failed EvidenceSet → POST /patches/{id}:repair(REPLAY) 得 superseding exact-base Patch revision + 新 preview/config → 对新候选重新 derive task suite、重跑 Review 与 Playtest，并从各 Run 的 finding links 取得 exact revisions → :validate 得 passed EvidenceSet → submit-for-approval → B(≠A,持全部域权限) approve → :apply(UoW 原子) → Eval/Observability 查询结果、trace/log/cost/latency`。断言 repair 前旧 Patch 不能 submit/apply、旧 ApprovalItem/evidence 不继承，旧 task suite 对新 config 创建 Playtest 返回 `stale_task_suite→409`；最终 apply 前 target ref 始终不变，apply 只指向 ApprovalTargetBinding 精确绑定的 target Artifact。该 happy path **必须实际执行 Playtest Agent**；`skip/not_applicable` 不计通过。契约始终按 Agent-Env/profile 选择 adapter，验收 fixture 使用 Aureus 不构成产品特化。

**Journey A failure path**：固定 `generation_gate_rejected` cassette/fixture；202 后由 SSE 正常观察非重试业务 RunFailure，并可读取 evidence-only rejected Patch/preview/gate evidence，断言没有 SubjectHead/ApprovalItem/config export/config execution，所有 workflow 命令 fail-closed，ref/history 不变。另以 API/组件测试覆盖 `playtest stuck → repair_unverified` 时无新 SubjectHead/ApprovalItem 且 submit/apply 均被拒。所有 Agent Run 绑定冻结 cassette，测试进程禁止外部网络出口，不能用 mock UI 或不相干 fixture 冒充同一候选链。

**Journey B happy path（Live-Ops + maker-checker）**：两个独立 session client A/B：`A 登录 → POST /patches 提交人写内容补丁并取得 preview → :validate 跑版本化不变量/经济回归 → 读取 passed EvidenceSet → submit-for-approval → B(≠A,持域权限) approve → :apply → 创建 rollback_request → :validate → submit → B approve → :apply → 断言 ref/history、audit、lineage、trace/log/cost 全关联`。**Journey B failure path** 固定一份引入回归的补丁：validation 产生 Finding + failed EvidenceSet，submit/apply 返回 workflow guard、ref/history 不变；同时覆盖 A 自批被拒与旧 workflow/ref revision 409。

跨旅程失败矩阵继续覆盖：异 payload冲突 / 两个编辑者制造三方冲突→新 Patch revision→重验→重新审批 / SSE 重连已提交 seq 无漏且重复按 `(run_id,seq)` 幂等去重 / API-worker 重启与 lease-expiry→retry_wait→独立 claim / 多层 BudgetHold 或 PermitGroup 任一 scope 不可用时 Run 不可领取且无部分占用 / 过期 worker 不发布业务结果或新 reserve、但既有 ReservationGroup 必须 reconcile/保守 settle / WS 重复命令只执行一次且 claimed command 可由 reaper 恢复 / 异步 quota-dependency 失败进 RunEvent / 未授权不能读 run-trace-log。Journey A/B 全程 REPLAY 零实网；以上只证明对应验收条目，**不单独宣称整个 M4 完成**。

约束发布另有 API contract + E2E，明确区分两条入口：Agent 路径是 `POST /constraint-proposals:propose → constraint_proposal.propose@1 Run → agent draft`；人工路径是 `POST /constraint-proposals → human typed draft`。两者都必须再由 A 以 `:revise` 创建用于发布的 immutable human-authored revision，之后 `compile/validate Run 产生 exact candidate constraint Artifact → A submit → B(≠A) approve → publish(ref CAS only)`；断言获批前 candidate Artifact 可审计但非权威，获批后同一 Artifact 经 constraint ref/history 成为权威，proposal/evidence lineage 与 audit 同时可见。缺 human revision、未获批、target digest 不符或旧 expected ref revision 均拒绝，且不得通过 auto-apply 绕过。

---

## 6. M4d — 前端全页面

### 6.1 视觉方向（已敲定）

Editorial 中性纸白底 + 暖调点色 + 一等公民但**克制**的数据可视化（"有目的地度量"，非为监控而监控）；避免 cream/beige 单色主导，保证操作台长期扫描不疲劳。UI 字体 = **思源宋体 Source Han Serif SC**（自托管、按使用字符集 subset 的 VF woff2，`font-display:swap`；正文 400/标题 600），代码/id/hash/公式 = **SF Mono**（非 Apple 环境回退 `ui-monospace/JetBrains Mono`）。

### 6.2 Design Tokens（CSS 变量 + tokens 模块，light-first，dark 变体一并定义）

```text
light 表面 --bg #f3f4f2 --surface #fff --surface-2 #f7f8f6 --sidebar #e8ebe8
light 墨   --ink #222624 --ink-2 #4f5852 --muted #667069 --faint #89928b   线 --line #dfe3df --line-strong #cbd1cc
light 语义 --deterministic #216c67 --suggestion #956316 --danger #b43b2e --ok #4d7955 --info #4f63a5
dark 表面  --bg #141715 --surface #1b1f1c --surface-2 #222723 --sidebar #101311
dark 墨    --ink #f1f4f1 --ink-2 #c8cec9 --muted #adb5ae --faint #838c85   线 --line #353b36 --line-strong #4a524c
dark 语义  --deterministic #69bdb5 --suggestion #e2ad55 --danger #ef8174 --ok #91c498 --info #94a8ed
字号 display28 h1-22 h2-18 h3-15 body14 small12 micro11；所有文字 letter-spacing=0；字号不随 viewport width 缩放
间距 4·8·12·16·24·32·48   圆角 sm4/md8/pill999（pill 仅 chip/status/toggle）
阴影 card:0 1px3 rgba(20,35,28,.08)   动效 hover120/panel200 ease(尊重 reduced-motion)
dataviz 分类 teal/amber/indigo/sage/terracotta/taupe；顺序=teal ramp；发散=terracotta↔teal（实现期按 dataviz skill 校验 WCAG-AA + 色盲安全）
**严格分区不只靠颜色**（附图标/文字标签）；card/modal/tool panel 圆角均不超过 8px
--faint 仅用于 disabled/nonessential decoration，普通 micro/small 文本至少用 --muted；两主题逐 token 做 WCAG-AA 对比测试
```

### 6.3 组件系统（冻结）

分区容器（确定性 vs LLM 建议，非仅颜色）· Finding 卡（severity+最小复现+source_ref）· 字段级 Diff 视图 · 三栏 Merge Resolver（base/current/proposed）· KG 图（Cytoscape）· PlaytestTracePlayer（通用动作/事件时间轴、step/播放/暂停/倍速、tick/state_hash、failure/loop/stuck marker、Finding 跳转 + profile/schema 驱动 renderer；2D map 只是 Aureus fixture 的可选 capability）· Chart kit（面积 spark/环/横条/trace 瀑布/成本柱）· Log Explorer（脱敏字段+trace 跳转）· 审批队列项（proposer/approver/域/policy version + 每个 requirement 的角色、门槛、有效票数、distinct 未满足项与当前 actor 可决策范围）· Run 进度（SSE 续传）· WS 命令控件 · 游标分页表 · 身份/角色芯片 · 权限门控包裹（UI 隐藏/禁用，服务端权威）· 状态原语（空/加载/错误/流式/终态）· problem+json 错误呈现 · Toast · 危险操作确认 · 面包屑 · 主题切换。控件图标统一使用 Lucide；熟悉动作优先 icon button，icon-only 必有 `aria-label` 与 hover/focus tooltip，不能用自绘 SVG 替代已有图标。

### 6.4 前端架构

路由（8 页 + run/finding/patch/approval 详情）· 服务态查询库（ETag/If-Match OCC + idempotency key）· OpenAPI→typed client · execution-profile catalog 驱动的兼容 selector（不写死默认 profile）· SSE 按 cursor 续传并以 `(run_id,seq)` 去重/持久化 cursor · WS 提交版本化 RunCommand · 鉴权 UI（按 Principal 隐藏/禁用，服务端权威）· RFC 9457 错误边界。

Trace renderer 使用版本化 `TraceRendererRegistryV1{registry_version,definitions[],registry_digest}`；definition 冻结 `{renderer_id,version,status:active|disabled,environment_contract_versions[],trace_payload_schema_ids[],capabilities[],component_key}`，按唯一 `(renderer_id,version)` 排序并摘要，`component_key` 只映射到制品内注册组件，不能加载客户端脚本。EnvironmentProfile/trace payload 只能请求 registry 中兼容 renderer；未知/disabled/不兼容 renderer 一律回退通用 state/action/event JSON + timeline，仍可完整检查与导航，绝不空白或硬编码 Aureus。2D map renderer 仅在 profile 声明 `spatial_2d` capability 且 state schema可验证时启用。

### 6.5 8 页 IA + 交互状态 + API 消费

| 页 | 目的 | 关键状态 | 主要动作→端点 | RBAC |
|---|---|---|---|---|
| Spec/KG | 上传/编辑 IR、看 KG、管理+提议约束 | 空/加载/图/human Patch draft/验证/冲突合并/Agent 提议 Run(SSE)/human typed draft/人工修订/编译验证/待审/已发布/约束列表 | `POST /specs`·`GET /specs/{id}/graph`·`GET /schema-registry/{version}`·`POST /patches`·`POST /constraint-proposals:propose`→202·`POST /constraint-proposals`·`:revise`·`:validate`·`:submit-for-approval`·`:publish` | `content_designer`/`numeric_designer` 按 domain_scope 编辑；`constraint_admin` author/publish 权威约束；另一有权 human 审批 |
| Generation | 输入目标→preliminary gate→同一 preview 的 Review/TaskSuite/Playtest→有 Finding 则 Repair→Patch 工作流 | profile 选择/输入/生成中(SSE)/gate rejected/preview+gate evidence/Review/derive suite/Playtest/repairing/superseding Patch 链接 | `GET /execution-profiles`·`POST /generation:propose`→202·SSE·`GET /patches/{primary_artifact_id}`·`POST /task-suites:derive`→202·跳转同一 preview 的 review/playtest/repair | `tooling`/`content_designer` 按权限授权；生成与修复只提议，preview 非当前 ref |
| Review | 分区看 Finding+最小复现，并确认其 exact preview/revision | 列表/确定性区/LLM 区/单 Finding/immutable revision/多 review/preview 绑定 | `GET /reviews`·`GET /reviews/{id}`·`GET /runs/{run_id}/findings`·`GET /findings`·`GET /findings/{id}`·`GET /findings/{id}/revisions/{revision}` | 全角色读(数据按权限过滤) |
| Playtest | 对候选 config 的 exact task suite 跑链→薄渲染回放→定位卡死/循环 | suite 发现/derive/排队/运行中(SSE)/时间轴播放/step/倍速/marker/Finding 跳转/preview+config+suite 绑定 | `GET /task-suites`·`POST /task-suites:derive`→202·`POST /playtest:run`→202·`GET /playtest/{run_id}/result` 动作轨迹 Artifact | `qa`/`tooling` |
| Patch/Diff | 字段级 diff→三方冲突解析→Repair revision→验证/回归→应用/回滚 | diff/base-current-proposed/冲突选择/repairing/新 preview/验证中/失败/完成/待审/已应用/回滚请求/验证/待审/已回滚 | `GET /diff`·`GET/POST /patches`·`:repair`·`:rebase`·`:resolve-conflicts`·`:validate`·`:submit-for-approval`·`:apply`·`POST /refs/{name}/rollback-requests`·`/rollback-requests/{id}:validate\|submit-for-approval\|apply` | `content_designer`/`numeric_designer`/`tooling` 按域起草；Repair 仅产新 draft revision；应用/回滚受审批门 |
| Eval/Bench | 分类 BDR/FP/Fix-Pass/HED/QA/成本延迟与证据来源 | 报告加载/分桶/CI/`evidence_missing`/`not_measured`/`pending_human_evidence`；HED/QA-hours 缺证据时不显示 0、不计通过 | `GET /bench/report`（含 evidence provenance/status） | 全角色读 |
| Observability | trace 树/日志/指标/成本 | trace/logs/metrics/cost/跨视图关联 | `/runs/{id}/traces`·`/traces/{tid}`·`/logs/query`·`/metrics/query`·`/cost/{id}` | 有权角色；字段脱敏，不返 prompt/raw |
| Approvals | 待我审批→批准/驳回/请改（另一身份） | 队列/单项/职责分离拦截/多域 requirement 逐项进度/部分获批/全部满足 | `GET /approvals?assignee=me`·`GET /approvals/{id}`·`:approve\|reject\|request_changes`（选择本人有权处理的 requirement） | DomainRoute：经济→`numeric_designer`/叙事→`content_designer`/抽卡→`gacha_compliance_reviewer`/结构→`tooling`；服务端逐 requirement 授权 |

### 6.6 无障碍 + i18n + 测试

WCAG-AA 对比 · 键盘可达 + 焦点 · 分区不只靠颜色 · 中文优先 + i18n 脚手架（文案抽出，多语实现 v-next）· Playwright E2E 至少固定 `journey-a-authoring` 与 `journey-b-liveops` 两套 suite，**各自**包含 §5.6 的 happy path + failure path · 组件测试 · TS 类型从契约生成（漂移即编译失败）· 两身份审批使用独立 browser context/session；全程 apps/api + cassette REPLAY 且测试进程 deny external egress。Journey A happy suite 必须从 catalog 选择 exact profiles，对 generation 与 repair 的每个 preview/config 分别 derive exact task suite 并实际运行 Playtest；旧 suite 对新 config 必须 `stale_task_suite→409`，`skip/not_applicable`、mock UI 或脱链 fixture 均不得算通过。Trace player 另固定通用 renderer、Aureus 可选 2D renderer与未知 renderer fallback 三组 fixture，未知环境仍完整显示 state/action/event，不得空白或崩溃。

视觉验收固定 desktop `1440×900`/`1280×720` 与 mobile `390×844`/`412×915`：light/dark、reduced-motion、长中文、长 ID/hash、分页/流式/错误/空状态均做截图与 overflow/overlap 断言；固定格式的图、diff、merge、toolbar 使用稳定 grid/aspect-ratio/minmax 尺寸，动态内容不得造成布局跳动。自动 WCAG 检查不能替代人工截图审查，但两者都进入 M4d review 证据。

### 6.7 M4d 显式延后

实时协同编辑、复杂图自动布局、多语实现（v-next）。

---

## 7. M4e — 生产运维 + 真实适配器

### 7.A 真实云适配器 + 容器契约测试

- **PostgreSQL**（实现 §3.1 Protocol）：PG **无** SQLite `BEGIN IMMEDIATE` 等价统一模式，按命令冻结并发策略——ref/approval/lease/cost 用条件 `UPDATE…WHERE revision/fencing_token… RETURNING`；队列 claim 用单事务 CTE `SELECT … FOR UPDATE SKIP LOCKED` + 条件 UPDATE/RETURNING；reaper 只关闭过期 attempt 并转 retry_wait/终态，后续 claim 才分配新 token。claim/renew/reaper/expiry 全用同事务 **DB-authoritative UTC**；Run head 条件 UPDATE/RETURNING 分配 attempt/fencing/event seq（非 `MAX+1`）；多层 budget/permit 按 `(scope_kind,budget_id)` 稳定锁序做余额+revision 条件更新，任一失败整组回滚；audit head 用行锁或 head-CAS 串行化；rollback 的 history/artifact/compat/当前 ref 检查处同一事务快照；`ref_history.seq` 用提交后 ref revision。应用连接用**非 owner/非 superuser** 角色，migration owner 单独。索引补 `ref_history(name,seq)` 等。
- **S3/MinIO**（实现 ObjectStore + ObjectBindingRepository）：`ObjectRef` 仅含 logical key + 真实 **SHA-256 + size**；bucket/store 与 VersionId 放 `ObjectLocation{store_id,backend_generation}`（multipart ETag ≠ 内容 SHA-256）。put 返回本次 verified location；DB UoW 建/保留 active ObjectBinding，ObjectStore 不知道“已发布 generation”。跨桶复制/恢复验证目标 hash/size 后以 binding revision CAS 重绑并审计，Artifact 不变；并发 race 的额外 location 是 GC 候选。完整上传成功才发布 DB binding；清理未完成 multipart。WORM retention 期条件删除可能失败→`RetentionActive`，GC 延后重试。统一本地 + S3 于 `runtime/object_store/`。
- **可观测查询后端（关键）**：OTLP Collector 只接收转发、非存储/查询；Prometheus 是 scrape/query 非 push。冻结生产拓扑 **OTLP Collector→Tempo(trace) · JSON/OTLP logs→Loki(log) · Prometheus(metrics)**；`runtime` 提供**有界 query 适配器**，`apps/api` 暴露 M4c 类型化 trace/log/metrics 查询（否则 Observability 页无法工作）。exporter 可 fan-out，但产品查询源必须持久、跨 API/worker 可见、有 retention。
- **容器契约测试**：testcontainers 式对临时容器跑与本地适配器**共享的**契约测试，另加**多连接并发 / 事务中断 / 死锁重试 / 连接丢失 / fault injection**；provider-specific（AWS Object Lock 细节）放独立显式 lane。

### 7.B 元 schema / DSL 版本 + 迁移器（区分两类）

IR Snapshot 带 `meta_schema_version`（进内容哈希）；**DSL grammar version 属 Constraint**（`dsl.py`），不给每个 IR 快照强塞。落点冻结：

- `contracts` 只定义纯 DTO/version/status（含 `MigrationReportV1` / `MigrationCheckV1`），不承载执行逻辑。
- `spine/ir/codecs` + `spine/ir/migrations` 持有版本化 IR reader/writer 与纯确定性 migrator；`spine/dsl/codecs` + `spine/dsl/compiler_registry` + `spine/dsl/migrations` 持有各 grammar reader/compiler/migrator，仍只 import contracts。
- `apps/worker` 组合 spine 的纯转换与 platform/runtime UoW/ObjectStore；spine 不接 DB、ObjectStore、telemetry 或 LLM。

持久 Snapshot/Constraint **必须按 payload 声明版本**选择 reader/compiler，禁止用构造器当前默认版本把旧数据静默当最新版。迁移**不原地改旧工件**，只经 §5.5 冻结的 `artifact.migrate@1` policy family 发布 `migration_report@1` 与条件同-kind `lineage@2` Artifact；source kind/schema、target schema/meta/DSL、migrator profile、report status/ID、lineage/VersionTuple 全量对拍。`report_only` 永不发布目标；`publish_migrated_artifact` 只有对应 `artifact_migration_published.<source-kind>` 才发布，compatible/migration_available/needs_re_extract/needs_re_compile 只写诚实报告；未知版本/迁移边 fail-closed `UnsupportedSchemaVersion`。每个受支持旧版本有 golden replay fixture（不删旧 reader/compiler）。

DB 用 **expand/contract 迁移**，蓝绿期新旧应用同时兼容；`lineage@2.object_ref` 先以 nullable 列兼容 lineage@1，`audit@2` 与 audit@1 reader 同期存在，旧数据不伪造引用或 actor。CI downgrade 只在临时库验证，**生产默认不自动执行破坏性 Alembic downgrade**。Alembic `0002+`（RBAC/approval/run/cost/session/telemetry/object/lineage@2/audit@2 表列）前向/回滚双测。

### 7.C DB/对象 tamper-resistant + 外部锚定 tamper-evident（准确威胁模型）

PG `REVOKE UPDATE/DELETE` + 触发器只挡应用角色，table owner/superuser 仍可绕过；S3 Object Lock + 外部锚**不构成绝对 tamper-proof**。故命名为 **tamper-resistant + externally anchored tamper-evident**。

```text
AuditAnchorPayload { anchor_schema_version, chain_id, seq, head_hash, previous_anchor_id?, anchored_at, anchor_policy_version }
SignedAuditAnchor { anchor_id, payload:AuditAnchorPayload, key_id, signature_algorithm, signature }
AnchorPublication { publication_schema_version, anchor_id, sink_id, sink_receipt, published_at }
anchor_id = sha256(canonical_json(AuditAnchorPayload))；signature 覆盖 `{anchor_id,payload,key_id,signature_algorithm}` 的 canonical bytes；
previous_anchor_id 必须等于同 chain 前一已发布 SignedAuditAnchor.anchor_id（首锚为 null）
AnchorSigner: sign(payload,key_id)->SignedAuditAnchor · verify(SignedAuditAnchor)->bool
AuditAnchorSink: append(SignedAuditAnchor,idempotency_key)->AnchorPublication · get(anchor_id) · latest(chain_id) · verify_receipt(anchor,publication)->bool
verifier 按 key_id+algorithm 选版本化公钥，支持密钥轮换但不重签历史 anchor；sink receipt 不参与 anchor_id/signature，消除发布回执循环
实现: DeterministicSigner + InMemory/FileAppendOnlyAnchorSink（契约/故障注入测试）；参考部署用独立凭据的 Ed25519/KMS signer +
      S3 Object Lock AnchorSink（或等价独立 WORM 信任域），receipt 校验失败 fail-closed；kind/k3d 用独立 MinIO bucket/账号跑 append/get/latest/receipt test
锚用与 DB 分离的签名密钥/凭据/不可删除存储；声明最大未锚定窗口；锚写失败告警+幂等重试，不伪装成功
验收须证明: 改历史行 / 删中间行 / 截断已锚定尾部 均可检出；最新 anchor 之后的尾部仅在下次锚定后受保护
```

### 7.D DR / 备份 / 恢复演练（跨存储一致检查点）

```text
BackupObjectEntryV1 { object_ref:ObjectRef, source_location:ObjectLocation, destination_location:ObjectLocation,
                      source_binding_revision, copied_sha256, copied_size_bytes }
BackupObjectManifestV1 { manifest_schema_version:backup-object-manifest@1, checkpoint_id, data_pg_lsn,
                         merkle_encoding_version:sha256-domain-separated-binary@1,
                         entries:[BackupObjectEntryV1...], entry_count, entries_merkle_root }
PgBackupReceiptV1 { receipt_schema_version, repository_id, base_backup_id, base_backup_manifest_hash,
                    wal_archive_set_id, wal_start_lsn, wal_end_lsn, immutable_until?, observed_at,
                    provider_receipt_digest }
PgRecoverySourceV1 { source_schema_version, receipt:PgBackupReceiptV1, target_lsn }
PgBackupVerificationV1 { verification_schema_version, verification_id, recovery_source_hash,
                         base_backup_verified, wal_range_verified, target_reachable, verified_at, evidence_hash }
PgRestoreResultV1 { result_schema_version, recovery_source_hash, target_profile:ProfileRefV1,
                    status:succeeded|failed, restored_lsn?, started_at, ended_at, evidence_hash, failure_code? }
PgBackupRepository: seal_recovery_source(target_lsn)->PgRecoverySourceV1 ·
                    verify_recovery_source(source)->PgBackupVerificationV1 ·
                    restore(source,target_profile,deadline_utc)->PgRestoreResultV1
RecoveryPointV1 { recovery_point_schema_version, checkpoint_id, data_pg_lsn, pitr_timestamp, migration_head,
                  pg_recovery_source:PgRecoverySourceV1, backup_object_manifest_artifact_id, object_manifest_hash,
                  audit_anchor_id, audit_head_hash, config_version, created_at }
BackupCheckpointV1 { checkpoint_schema_version, recovery_point:RecoveryPointV1,
                     recovery_catalog_entry_id?, recovery_catalog_publication:RecoveryCatalogPublicationV1?,
                     status:preparing|recoverable|failed, revision, created_at, updated_at }
ObjectCopyReceiptV1 { receipt_schema_version, location:ObjectLocation, verified_sha256, verified_size_bytes,
                      retention_mode, retain_until?, observed_at, provider_receipt_digest }
RecoveryCatalogEntryPayloadV1 { catalog_schema_version, previous_catalog_entry_id?,
                                recovery_point:RecoveryPointV1, manifest_artifact:ArtifactV2, manifest_destination_location:ObjectLocation,
                                manifest_copy_receipt:ObjectCopyReceiptV1,
                                catalog_policy_version, created_at }
SignedRecoveryCatalogEntryV1 { catalog_entry_id, payload:RecoveryCatalogEntryPayloadV1,
                               key_id, signature_algorithm, signature }
RecoveryCatalogPublicationV1 { publication_schema_version, catalog_entry_id, sink_id, sink_receipt, published_at }
RecoveryCatalogRecordV1 { record_schema_version, entry:SignedRecoveryCatalogEntryV1,
                          publication:RecoveryCatalogPublicationV1 }
catalog_entry_id = sha256(canonical_json(RecoveryCatalogEntryPayloadV1))；signature 覆盖 `{catalog_entry_id,payload,key_id,signature_algorithm}`；
RecoveryCatalog: append(SignedRecoveryCatalogEntryV1,expected_previous_entry_id?,idempotency_key)->RecoveryCatalogPublicationV1 ·
                 get_record(entry_id)->RecoveryCatalogRecordV1 · find_checkpoint(checkpoint_id)->RecoveryCatalogRecordV1? ·
                 latest()->RecoveryCatalogRecordV1? · verify_signature(entry)->bool · verify_receipt(entry,publication)->bool

object manifest 是 kind=`operational_evidence`、payload schema=`backup-object-manifest@1` 的 lineage@2 Artifact；其 payload 只覆盖 **data_pg_lsn 数据 cut** 可见的全部已提交 ObjectRef、source/destination ObjectLocation/backend_generation、binding revision/hash。manifest 不能自包含；其 Artifact envelope、目标 location 与 typed copy receipt 由独立 RecoveryCatalog 保存。receipt.location 必须等于 manifest_destination_location，receipt 的 hash/size 必须等于 manifest Artifact.object_ref，retention 必须覆盖 checkpoint 保留期，任一不符即拒绝发布 catalog entry。
RecoveryCatalog 使用与主 DB/主 ObjectStore 分离的凭据、签名密钥和 append-only/WORM sink，retention 不短于最长备份保留期；`checkpoint_id` 唯一，append 的 expected previous ID 与 payload.previous ID 都必须匹配 catalog 当前 head。它只接收字段完整且已验证对象/PG 备份 receipt 的 immutable `RecoveryPointV1`，**不嵌入主库中尚待 CAS 的 recoverable status**，避免“先 recoverable 才能入 catalog、先入 catalog 才能 recoverable”的循环。它可复用 §7.C 的版本化 signer/sink 原语，但使用独立 chain/policy/credential。签名、previous entry、PG backup receipt、manifest copy 的 location/hash/size/retention 复核或 catalog sink receipt 任一验证失败均 fail-closed；catalog 发布失败时主库 checkpoint 不得宣称 recoverable。
manifest 确定性编码：entries 按 `(object_ref.key,object_ref.sha256,object_ref.size_bytes)` 升序，**每个 ObjectRef 恰一项**，重复 ObjectRef/source location/destination location 一律 `IntegrityViolation`；entry_count 必须等于数组长度。Merkle leaf=`SHA256(0x00 || UTF8(canonical_json(entry)))`，internal=`SHA256(0x01 || left_raw_32_bytes || right_raw_32_bytes)`，奇数层最后一个 hash 复制后配对，空数组 root=`SHA256(0x02)`；root 以小写 hex 编码。改变排序、canonical JSON 或树规则必须 bump merkle_encoding_version。
一致性守卫：manifest payload 的 checkpoint_id/data_pg_lsn 必须等于 RecoveryPoint；PgRecoverySource.target_lsn 必须等于 data_pg_lsn，receipt.wal_start_lsn≤target_lsn≤receipt.wal_end_lsn，且 `verify_recovery_source` 必须确认 base backup manifest、逐段 WAL checksum、连续性与 target 可达；RecoveryPoint 的 manifest ID/hash 必须等于 catalog 内 Artifact envelope 的 artifact_id/payload_hash；Publication.catalog_entry_id 必须等于 entry ID。`BackupCheckpointV1.status=recoverable` 时 entry ID/publication 必填且 recovery_point 必须与签名 entry 逐字段一致；preparing/failed 时二者必须同时为空。任一不变量不满足即 `IntegrityViolation`。
落点：纯 DTO/Protocol 在 `contracts/operations.py`；`runtime/recovery_catalog` 提供 deterministic FileAppendOnlyRecoveryCatalog（本地/CI）与 S3 Object Lock/WORM adapter；`runtime/persistence/postgres_backup` 提供 FakePgBackupRepository（编排测试，不作 DR 证据）与参考部署 PgBackRestRepository。catalog adapters 共享 append/head-CAS/idempotency/signature/receipt/retention/fault-injection 契约测试；PG adapters 共享 seal/verify/restore 契约，PgBackRest 对临时 PostgreSQL 做 base-backup + WAL + PITR 容器集成测试。
备份顺序冻结为：① 以 DB-authoritative `data_pg_lsn` 建立数据 cut，生成可从独立备份仓定位 base backup + WAL 范围的 `PgRecoverySourceV1`，并枚举该 cut 的 live ObjectRef；② 复制对象、逐项复核 hash/size，生成并写 manifest Artifact/blob；③ append + 验证 SignedRecoveryCatalogEntry/receipt；④ **只有前三步全通过**，主库中的 `BackupCheckpointV1` 才记录 catalog entry/receipt 并 CAS→recoverable。主库中的 checkpoint/manifest 行方便日常查询，但**不是**灾难恢复自举根；禁止另存不受 RecoveryCatalog、GC 或 DR 追踪的裸 ObjectRef。
备份: PG 快照 + WAL 归档 + 对象跨桶复制；审计/快照 append-only + 内容哈希校验
目标: RPO ≤ 1h(最新可恢复业务提交距故障点) · RTO ≤ 4h(声明故障到 readiness 全绿)
演练: 在主 DB/ObjectBinding 完全不可用时，仅凭 RecoveryCatalog 验签/验 receipt 取得 RecoveryPoint + PG recovery source + manifest Artifact/对象；复核后按 `PgRecoverySourceV1` PITR 到 data_pg_lsn，再导入 catalog 中的 manifest Artifact/BackupCheckpoint 投影，按 manifest 原子重建 active ObjectBinding，并验证每个 live ObjectRef 可解析且对象 hash/size 正确 · audit chain/anchor · lineage · approval/run/cost/session · ≥1 cassette replay
     计时覆盖 catalog bootstrap、PITR、对象重绑、投影导入和 readiness；证据记环境/catalog entry/备份 ID/起止时间/失败步骤/canonical evidence hash（非仅总耗时）
```

### 7.E SolverExecutor 跨平台隔离（依赖落点修正）

`runtime` 禁依赖 spine，故 `runtime/solver_exec` 只做**通用进程隔离机制**；`apps/solver_worker` 是受控子进程入口，导入 spine 执行 checker。

```text
SolverLimits { cpu_time_s, memory_bytes, pids_max, wall_time_ms, stdin_bytes, stdout_bytes, stderr_bytes, result_bytes }
SolverTaskV1 { task_schema_version, task_id, task_kind:asp_solve|smt_solve, task_version,
               input_artifact_id, input_hash, payload_schema_version, payload, limits:SolverLimits, request_hash }
SolverResultV1 { result_schema_version, task_id, task_kind, status:success|timeout|memory_exceeded|cpu_exceeded|
                 executor_unavailable|solver_unknown|invalid_task|output_too_large,
                 output_schema_version?, output?, output_hash?, duration_ns, peak_memory_bytes?, exit_code?, stderr_digest? }
SolverTaskRegistry { task_kind, task_version, payload_schema_id, result_schema_id, handler_key, max_limits }
SolverExecutor.run(SolverTaskV1)->SolverResultV1；registry 只含 `asp_solve@1`/`smt_solve@1`，unknown/version/schema/超 max limit fail-closed；
  禁客户端提供 command/module/callable/pickle，输入输出只用有界 canonical JSON
  LinuxCgroupExecutor: cgroup v2 memory/pids + RLIMIT_CPU(总 CPU 时间；cpu.max 只是速率限制不够) + wall watchdog；
    另建 user/mount/pid/**network namespace**（空网络命名空间，无外部接口）并用 seccomp deny socket/connect 等网络 syscall，
    受控只读 root/input + tmpfs cwd，关闭继承 fd，环境变量 allowlist（无模型/DB/对象存储密钥），限制 stdout/stderr/result，超限杀整个进程组。
    cgroup 本身不声称禁网；namespace/seccomp 能力不可用时 production executor fail-closed，不降级成“有网隔离”
  LocalRlimitExecutor: subprocess + setrlimit + wall watchdog + 有界 I/O（macOS，开发期 best-effort，**不提供硬禁网/文件系统隔离，不作安全验收证据**）
  FakeExecutor       : CI 确定性 fake clock/资源计账（只验编排，**不作隔离证据**）
capability matrix 明确；声称 Windows 支持须加 Job Object adapter
```
现有 z3 timeout / ASP grounding 预算保留为**进程内第一道**，此为**进程外第二道**。

### 7.F 来源治理（source-traceable + 结构隔离 + 下游门禁，**非 injection-proof**）

通用文本无法可靠"中和所有内嵌指令"，正确保证 = 来源可追踪 + 结构隔离 + 输出仍受确定性/人工门禁。

```text
TrustLevel = trusted_internal | reviewed_external | untrusted_external
SourceKindId = 版本化 SourceKindRegistry 中的稳定 opaque ID
SourceKindDefinitionV1 { source_kind_id, allowed_trust_levels[], allowed_prompt_purposes[], description_code }
SourceKindRegistryV1 { registry_schema_version, registry_version, definitions:[SourceKindDefinitionV1...] }
OriginRefV1 { origin_schema_version, opaque_source_id, source_revision }  # 在 connector_id 范围内稳定；无上游 revision 时用内容 hash；不含 secret/raw URI
ProvenanceV1 { provenance_schema_version, source_kind_registry_version, source_kind_id:SourceKindId,
               origin_ref:OriginRefV1, parent_source_artifact_ids[], connector_id, connector_version,
               trust:TrustLevel, source_hash, transformations:[{tool_version,input_hash,output_hash}] }
trust 由**受信 ingestion connector 的已认证配置分配**（不读取 payload 自报值）；多来源派生取最保守 trust
内建 source kind 至少含 authenticated_human_goal / trusted_service_goal / planning_document / open_source_content / tool_output / retrieval_result；其他游戏/connector 只扩 registry
外部摄取/认证 goal 的 source_raw 必须无 parent；tool_output/retrieval_result 等派生 source_raw 与 source_rendered/多源派生按 artifact_id 稳定排序去重引用实际 source parents，且与 Artifact.lineage 中的 source parents 完全一致
Provenance 永不保存承载它的 source Artifact 自身 ID；origin_ref 由 connector 在 Artifact ID 计算前分配，从根上消除自引用 hash
原始内容=`source_raw` Artifact；prompt-rendered 内容=`source_rendered` Artifact，均不可变并记 renderer/sanitizer version + hash
prompt 边界: Artifact binding → PromptPartV1{ prompt_part_schema_version, text, provenance:ProvenanceV1,
                                              purpose:instruction|context|tool_output|user_goal }；Run payload 禁止内联裸 goal/prompt
            trust 唯一取自 provenance；`instruction|user_goal` 仅允许 trusted_internal 且由可信组合根按认证来源赋值，
            reviewed_external/untrusted_external 只能是 `context|tool_output`；payload 不得自报 trust 或 purpose
            统一定界 + 长度限制 + canonical rendering；覆盖文档/开源内容/检索结果/tool output
Human authoring + compile/validate 先产生**非权威 candidate constraint_snapshot Artifact**；另一 human approval + §3.2 exact ref CAS 只把该已验证 Artifact 权威化，保留 proposal/evidence/raw/rendered lineage，不在 publish 时另造 target，也不篡改原始来源标签
```

落点：`contracts/provenance.py` 放上述纯数据；`platform/provenance` 维护 SourceKindRegistry + connector/trust policy；`apps/api` 的 ingestion/goal 组合根创建 raw Artifact；`agents` 只消费由 Artifact binding 解析出的 PromptPartV1 并产 rendered Artifact。任何 API DTO 中的用户文本都只能先转成带认证来源的 `source_raw`，不能直接进入 RunPayload/agent；外部来源只能成为 `context|tool_output`，不能借 goal 字段升级为 trusted instruction。恶意内嵌指令不能直接写任何权威状态：对 authoritative constraints 最多先形成 proposal，且必经 typed compile/validate + human authoring + 另一 human approval；对配置最多先影响 Patch draft，之后只能走冻结的 deterministic auto-apply policy（exact subject/target/ref/policy proof 全量重验）或另一 human 审批，不能绕过任一门禁。文档不宣称通用 sanitizer 能证明 injection-proof，也不误称所有受影响 Patch 都必然停在人工待审态。

### 7.G 容器化 + CI/CD + 参考部署

同一不可变镜像两个长期部署 entrypoint（api/worker）；`apps/solver_worker` 只作为 SolverExecutor 受控子进程，不单独常驻部署。`deploy/k8s`（Kustomize）为参考声明式目标，**kind/k3d integration stage** 验证 migration Job / API rollout / worker drain / readiness / 流量切换 / 应用版本回滚；migration 是**单独 Job**（非每 API replica 自动执行）；部署回滚只回到与当前 DB schema 兼容的镜像；镜像/PG/MinIO/Collector/Tempo/Loki/Prometheus 版本按 **digest 固定**。compose 仅本地全栈。流水线门禁 = 全量 pytest + 7 契约 dep-lint + ruff + cassette REPLAY + Alembic 前向/回滚 + 容器契约测试 + Playwright E2E。

### 7.H 容量 / SLO 预注册 + 压测

流程冻结 **探索 baseline → 版本化预注册容量/SLO → 独立 verification run**（不测完后按结果设必过阈值）。容量表在 M4e 完成前给数值，至少覆盖 entity/relation/constraint 数量、artifact/trace 大小、并发 Run/worker、RunEvent 速率、checker/sim、50-任务回归的 p95/p99、token 与 wall-time 预算。证据记硬件/容器 limits/数据集 hash/seed/冷热启动/重复次数。性能 lane 独立于普通单测；功能正确性仍留默认零实网 CI。

### 7.I 密钥 + OIDC

密钥仍 env/secret-manager（不入库不入日志，现状已满足）；OIDC 适配器接口 M4c 已定，实现按非目标保持不做。

### 7.J M4e 显式延后

OIDC 实现（PRD 非目标）、多区域/HA 生产拓扑、真实告警投递（PagerDuty 等，`AlertSink` 接口已定）= v-next。

---

## 8. 内部安全（§12A.5 汇总）

RBAC 在 API 层强制（§5.2）· 审批/应用/回滚鉴权（§3.3/§5.3）· 来源治理管线（§7.F）· DSL→solver 双道隔离（进程内预算 + 进程外 SolverExecutor，§7.E）· 密钥 env/secret-manager 不入库不入日志（§7.I）· 提示注入按"来源可追踪 + 结构隔离 + 确定性/人工门禁"处理，不宣称 injection-proof。

---

## 9. 验收（§16 M4，均为**未完成**实现验收项）

> 当前通过的是 **M4a–M4e 设计审查**，非实现。下列目标只有在五片实现 + 全量测试 + 容器/DR/容量证据 + M4d E2E 全部完成并合回 `master` 后才成立。

- [ ] 端到端 trace/log/metrics/成本/延迟可观测（M4b 契约 + M4e Tempo/Loki/Prometheus 真实适配器 + 跨 API/DB authoritative Run carrier/独立 worker 与进程重启仍保持父子/flags/state，可查询、可关联、RBAC、脱敏、有界；carrier 不进入 request/payload/Artifact hash）
- [ ] 本地 `LocalTelemetryStore` 在独立 API/worker 进程、进程重启与 retention 边界下仍可查询同一 trace/log/metric；MetricPoint exact descriptor digest/point-id 幂等、同 timestamp tie-break、counter-sum/gauge-last/histogram-bucket 聚合、跨 descriptor version 不混算与超界不静默降采样均有契约测试；`TraceSummaryPageV1/SpanPageV1/MetricPageV1` 与生产 query adapter 通过同一有界分页/脱敏/retention 测试
- [ ] 任意产物血缘追溯 + 回滚（lineage@2/ObjectRef/VersionTuple producer matrix + 单/多 prompt/model `ExecutionIdentityV1` projection + exact rollback ExecutionProfile binding + RefTransition + audit@2；历史 evidence missing 不伪造）
- [ ] SubjectHead 与 validation completion 并发时，旧 revision 原子进入 `superseded`、旧 Run 以 `subject_superseded` 终结且 EvidenceSet 不挂到新 head；旧审批/evidence/auto proof 不继承、不复活
- [ ] 多连接并发 claim/retry/reaper 下 attempt_no 与 fencing_token 单调且不重复，retry event 不预分配未来编号，RunEvent.seq 严格递增；每个已提交状态变化恰有对应事件，过期 worker 不能发布结果
- [ ] PRD Journey A `journey-a-authoring` Playwright happy+failure 全链按 §5.6 跑通：从 profile catalog 选择 exact versions，generation/repair 均使用非空 export profile + exact constraint；每个 preview/config 先 `task_suite.derive@1`，再做 Review 与 **实际 Playtest**，并经 RunFindingLink 读取 exact Finding revisions → failed EvidenceSet → `patch.repair@1` superseding exact-base revision → 新 preview 重新 derive suite/重验 → 两独立身份审批 → apply；旧 suite 对新 config 返回 `stale_task_suite→409`，apply 前 ref 不变，target Artifact/lineage 连续；`generation_gate_rejected` 仅产 evidence-only artifacts、`repair_unverified` 不推进新 head，二者都不产生可提交/可应用 subject；全程 REPLAY 且 deny external egress
- [ ] PRD Journey B `journey-b-liveops` Playwright happy+failure 全链按 §5.6 跑通：人写 Patch 的版本化不变量/经济回归、两独立身份 maker-checker、应用与受审回滚；回归失败、自批、旧 workflow/ref revision 均 fail-closed 且 ref/history 不变
- [ ] maker-checker 审批工作流端到端跑通（Journey A/B 均用完全独立身份 session；异步 publisher 的 proposer=Run.initiated_by 而非 worker；含身份/角色变更后的提交与 apply 重验）
- [ ] DomainRegistry/RoleAssignment/RolePolicy/DomainRoute 历史 digest 可解析；assignment scope 与 permission scope 取交集且 `null≠all`。多域 ApprovalItem 的 requirements 覆盖完整 DomainScope；partial approval 保持 pending，各 requirement 的 min/distinct/权限逐项满足后才能 approved，UI 与 API 展示相同进度且 apply 时全量重验
- [ ] generation gate pass 原子发布 Patch draft + 非权威 preview `ir_snapshot`/条件 `config_export` + gate evidence + draft ApprovalItem；gate reject 发布 evidence-only rejected Patch/preview/gate evidence + RunFailure，但无 SubjectHead/ApprovalItem/config export；Repair verified 才发布 superseding complete Patch revision 与新 preview/条件 config，unverified 不推进 head；所有 workflow Patch 再经 exact supporting-evidence validation/approval/apply。constraint proposal 的 Agent Run 入口与 human typed draft 入口均经 human immutable revision + compile/validate 先发布 exact 非权威 candidate `constraint_snapshot`，另一 human 审批后同一 UoW 只以 ref CAS 权威化该 Artifact；proposal/evidence lineage、constraint ref/history、audit 同事务可见且不可 auto-apply 绕过
- [ ] OutcomeArtifactPolicy 对 generation pass/reject、repair verified/unverified、constraint proposal/candidate、Review/Checker/Simulation、TaskSuite derive（合法 `/episodes` identity binding）、Playtest（exact episode/scenario inputs）、Patch/rollback validation、qualified auto-apply proof、artifact migration policy family 与公共 dependency/quota/integrity/failure/cancel/timeout 逐项验证 exact kind/schema/cardinality/profile identity；resolved policy snapshot 不读 current，partial evidence 用完整 produced/not_executed dispositions，Prepared artifact 在完整 publication plan 中多一项/少一项/重叠匹配均 fail-closed。worker intrinsic failure classification 与控制面 RetryDecision 分离，同一个 transient Prepared failure 可按当时 attempt/budget/deadline 进入 retry 或 terminal policy；typed dependency、cause/class、retry schedule 三处投影闭合，queued quota 不造无 attempt RunFailure。每个 active 非成功 final attempt（含 gate reject/repair unverified）先发布不消费业务 evidence/dispositions 的 attempt-scope failure，再由 run-scope policy独占消费 Prepared evidence并聚合全部 closed-attempt failures且无自引；retry_wait 取消/整体超时证明无 active lease、不开新 attempt，以最大已关闭 attempt 聚合全部既有 manifests，run-scoped terminal event envelope 为 null attempt 而 data/RunFailure 保留该最大值；manifest projection 的 input/intermediate/output/evidence 集合与 lineage 精确一致，RECORD attempt/run scope 使用 exact attempt/run cassette bundle，父 tuple 不被机械 merge；Finding revision/link 与对应 Artifact/RunResult 同 UoW 发布且可分页精确枚举
- [ ] 两个独立编辑者制造三方冲突 → 新 Patch revision → 重验/回归 → 重新审批的 API + Merge UI E2E（旧批准不继承）
- [ ] run/principal/system 多层 BudgetHold/ReservationGroup/PermitGroup 任一 scope 拒绝时全体不分配；RunPayload/RunRecord/CostLedger 的 budget-set snapshot ID 永远同值；其他 Run 推进共享 Budget revision 不使既有 hold 的合法子划拨假冲突，`concurrent_run` 仅由 lease Permit 占用且 retry 重新获取；崩溃、unknown usage 与 late reconcile 不重不丢，所有 Run 终态关闭 hold/permit 且无孤立 reservation
- [ ] `record_shard→attempt→run` cassette bundle 覆盖成功、失败、retry、lease expiry、cancel、timeout、零调用及同 logical call 的 fallback route；ExecutionIdentity 对单值/聚合 prompt+model 投影可重算。失败 attempt 也可经 lineage/GC/DR 保留，恢复后 REPLAY 严格按 bundle/route 顺序运行且 MISS fail-closed；既有嵌套 `cassette@1`/`model-router@1` 原字节保留，legacy `latency_ms=0` 映射 unavailable；verified import 以 exact rendered request/profile/typed policy+schema bindings/call order、完整 parsed wire 对拍、内容寻址 legacy decision 与 verification-policy digest 建 `run_id=null` manifest，缺证据才 evidence_missing，摘要/结构矛盾为 IntegrityViolation；同一历史 Opus cassette 无 provider 重录即可进入验证通过的回放且不伪造 native RoutingDecision
- [ ] 持久 WS command 在 ACK 前提交，重复 command/seq 只执行一次；API/worker 重启或 lease expiry 后 claimed command 回到 pending 或随 Run 终态明确 rejected，断线客户端可由命令列表 + SSE cursor 恢复
- [ ] read-snapshot 分页在并发插入/更新/删除下拼接结果无重复无遗漏；cursor 篡改、过期、跨 principal 与权限撤销分别返回冻结错误且不会泄露数据
- [ ] ExecutionProfile catalog 每个 definition 恰有一条同 ProfileRef lifecycle row、集合完全相等；validation details 的 auto-apply policy/qualified outcome rules/deterministic oracle registries 全部按历史 digest 唯一解析，affected domain 必须被 allowed all-of 覆盖、不得与 forbidden 相交，每个 deterministic oracle supported/evaluated scope 覆盖且 validation/submit/apply 重跑同一 guard；artifact-migrator edge 的 golden required/not-applicable 与 fixture digest 精确冻结。跨 catalog 状态 revision 只在真实变化时单调 +1，active/replay_only/disabled 的创建与 native/verified-import REPLAY 守卫按 exact snapshot 执行。ModelCatalog/RoutingPolicy/ExecutionVersionPlan 与 RunKindDefinition/outcome-policy-set/runtime-parent digest 对无语义数组乱序稳定、对任一语义字段变化敏感，缺失/重复/摘要不符使 readiness/terminal fail-closed
- [ ] 本地身份 bootstrap 竞态只创建一个首位管理员；PrincipalRecord/RoleAssignment 的 revision/epoch 即时生效；登录名 exact normalization policy 可迁移且碰撞 fail-closed；密码/API key/session 轮换、禁用与 revoke 立即生效，旧 session/role claim 不被信任，CSRF/WS Origin/OIDC state-nonce-PKCE 契约测试通过
- [ ] tamper-resistant + 外部锚定 tamper-evident：签名与 sink receipt 都验证；**已锚定范围内**改行/删中间行/截尾可检出，receipt/签名/previous anchor 篡改 fail-closed；最新锚点后的窗口不声称可独立证明尾截断（§7.C）
- [ ] DR 真实计时演练达 RPO≤1h/RTO≤4h，跨存储一致检查点；在主 DB/主对象库均不可用的前提下，仅凭独立 RecoveryCatalog 验证签名/previous entry/PG backup receipt/manifest receipt/catalog receipt，取得 RecoveryPoint + PG recovery source + manifest；PITR 到 data_pg_lsn 后逐 ObjectRef 复核 hash/size 并以 revision-CAS 原子重建 ObjectBinding，旧 backend generation 不进入 Artifact，恢复后 readiness 与至少一次 cassette replay 通过（§7.D）
- [ ] PgBackupRepository 对真实临时 PostgreSQL 完成 base backup + 连续 WAL + 指定 LSN 恢复；缺段/坏 checksum/目标越界 fail-closed。相同 BackupObjectEntry 集合任意输入顺序产生同一 manifest hash/Merkle root，重复 ObjectRef/location 被拒，catalog/checkpoint/manifest 各字段交叉不一致被拒
- [ ] 元 schema/DSL 迁移器 + golden replay fixtures + `MigrationReportV1` 八项有界 inline typed/status-specific results（无悬空 evidence Artifact；布尔/semantic/golden status 双向约束；migrated 七项非-golden passed，golden 仅 exact profile edge 可 N/A）+ exact historical `MigrationCapabilityMatrixV1` 对 ArtifactKind enum 完整覆盖且 manifest/workflow/cassette kinds 不被机械迁移；`artifact.migrate@1` 仅为完整 source/target/meta/DSL tuple 的 exact publish edge 生成同-kind outcome，未命中 edge 仅走 source-kind 非发布 default，其余 report/compatible/needs-action/409（mode/source kind/target schema/lineage/producer matrix 全量对拍）+ Alembic 前向/回滚双测（§7.B）
- [ ] SolverExecutor 进程外隔离提供 Linux CPU/memory/pids/wall/I/O 杀限与**硬无网络**实测证据；namespace/seccomp 任一能力缺失时 production fail-closed，LocalRlimit/Fake 不冒充安全证据（§7.E）
- [ ] 来源治理：payload 不能冒充 connector trust/SourceKindId、最保守传播、raw/rendered Artifact、PromptPart 定界；Provenance 只含 connector origin_ref + 实际 parent IDs、不含承载它的 Artifact 自身 ID，source parents 与 lineage 一致；generation/constraint goal 先成为认证来源 `source_raw` 且 Run payload 无裸文本，每个 `source_rendered` 在模型调用/回放前发布并在成功或失败 lineage 中保留；外部来源不能借 goal 升级 trust；恶意内嵌指令不能直接写权威状态，Constraint 必经 human authoring + 独立审批，Patch 仅可经 exact deterministic auto-apply proof 或另一 human 审批后生效（§7.F）
- [ ] kind/k3d 参考部署证据：migration Job、API rollout、worker drain、readiness、流量切换、schema-compatible image rollback 全部实跑（§7.G）
- [ ] 容量/SLO 预注册 + 独立 verification run 证据（§7.H）
- [ ] M4d desktop/mobile light/dark 截图、长文本无 overflow/overlap、WCAG-AA、键盘与 reduced-motion；TraceRenderer generic/Aureus-2D/unknown fallback 均有证据（§6.6）
- [ ] 全量 pytest + 7 契约 dep-lint + ruff + Python/TS typecheck + REPLAY + 容器契约测试 + Playwright E2E 全绿
- [ ] combined M3 acceptance 仍如实保留 `qa.evidence_missing`（真人 QA 延后，不豁免）

CI 隔离：容器 integration 用独立 `GAMEFORGE_INTEGRATION=1` job（不复用 `GAMEFORGE_LLM_LIVE`）；内部容器网络不触发真实 LLM，镜像按 digest 预拉取。

---

## 10. 显式延后清单（延后实现，非砍接口）

OIDC 实现（非目标）· 多区域/HA 拓扑 · 真实告警投递（AlertSink 接口已定）· 实时协同编辑 · 复杂图自动布局 · 多语实现 · 真 Windows solver 隔离（Job Object adapter）——全部 v-next；本文档契约全定。

---

## 11. 自审结论

- 无待定占位符、“可选实现”或依赖未来审批的设计决策；所有跨模块契约（Artifact/ObjectRef/存储/身份/审批/Run/成本/追踪/迁移/隔离/来源/API）字段与状态语义一次定全。
- 严守分层：M4 新代码在 `contracts/runtime/spine/platform/agents/apps/web`；`spine` 只新增确定性 codec/compiler/migrator 并仍只依赖 contracts；`runtime` 不依赖 spine（solver 执行走 `apps/solver_worker`）；`Tracer/Span/TraceContext` 不进 spine 与 canonical hash。
- 诚实措辞：tamper-resistant + tamper-evident（非 tamper-proof）；来源治理非 injection-proof；at-least-once execution + exactly-once publication；货币成本 unavailable 直到 PriceBook；验收项全部标未完成。
- 确定性/可回放默认不破坏：本地 SQLite/文件 sink/注入时钟；真实适配器独立 CI stage、按 flag 触发；回放速度不误计为成本/延迟；LLM `live` 无 cassette 时明确标 online-only，非 LLM Run 使用 `not_applicable` 而不伪装 `live`。
- 未提前实现 M4 之外能力；无新增插件系统/审批服务外的框架。
