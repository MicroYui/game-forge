import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ClipboardCheck, Gavel, LockKeyhole, Route, ShieldCheck, UsersRound } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { createMutationIntent } from "../../api/csrf";
import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { useToast } from "../../app/providers";
import {
  CopyableText,
  CursorTable,
  type CursorPaginationState,
  type CursorTableColumn,
} from "../../components/tables";
import { ConfirmDialog, PermissionGate, ProblemPanel, StatePanel } from "../../components/ui";
import { messages } from "../../i18n/zh-CN";
import {
  approvalsApi,
  type ApprovalAction,
  type ApprovalDecisionIntent,
  type ApprovalsApi,
  type ApprovalViewData,
  type VersionedApproval,
} from "./api";
import {
  actionEligibility,
  actionLabels,
  approvalSubjectHref,
  eligibilityReasonLabel,
  requirementRows,
  selectableRequirementIds,
  selectionIsEligible,
} from "./model";
import {
  ApprovalSubjectReview,
  ApprovalSubjectReviewFailure,
  loadApprovalSubjectReview,
} from "./ApprovalSubjectReview";
import "./approvals.css";

interface QueueState {
  error: Error | null;
  items: ApprovalViewData[];
  loading: boolean;
  nextCursor: string | null;
  readSnapshotId: string;
}

const approvalQueryKey = (approvalId: string) => ["approvals", "detail", approvalId] as const;

const decisionReasons: Record<ApprovalAction, readonly { label: string; value: string }[]> = {
  approve: [
    { label: "已核对实际改动与验证证据", value: "content_and_evidence_reviewed" },
    { label: "已核对确定性验证结果", value: "evidence_reviewed" },
    { label: "已核对发布目标与影响范围", value: "target_and_scope_reviewed" },
  ],
  reject: [
    { label: "受审内容不正确", value: "content_incorrect" },
    { label: "验证证据不足或不一致", value: "evidence_rejected" },
    { label: "发布目标或影响范围不正确", value: "target_or_scope_incorrect" },
  ],
  request_changes: [
    { label: "需要修订受审内容", value: "content_changes_required" },
    { label: "需要补充验证证据", value: "evidence_changes_required" },
    { label: "需要修正发布目标或影响范围", value: "target_or_scope_changes_required" },
  ],
};

const approvalStatusLabels: Record<ApprovalViewData["approval"]["status"], string> = {
  applied: "已应用",
  approved: "已批准",
  auto_apply_eligible: "可自动应用",
  changes_requested: "待修改",
  draft: "草案",
  pending_approval: "待审批",
  rejected: "已驳回",
  rolled_back: "已回滚",
  superseded: "已被替代",
  validated: "验证通过",
  validating: "验证中",
  validation_failed: "验证失败",
};

const subjectKindLabels: Record<ApprovalViewData["approval"]["subject_kind"], string> = {
  constraint_proposal: "约束提案",
  patch: "内容补丁",
  rollback_request: "回滚请求",
};

function normalizedError(error: unknown, fallback: string): Error {
  return error instanceof Error ? error : new Error(fallback);
}

function queuePaginationState(state: QueueState): CursorPaginationState {
  if (state.error instanceof CursorExpiredError) return "expired";
  if (state.error) return "error";
  return state.loading ? "loading" : "ready";
}

function actionRange(view: ApprovalViewData) {
  return (
    <span className="gf-approvals__nowrap" tabIndex={0}>
      {(["approve", "reject", "request_changes"] as const)
        .map((action) => `${actionLabels[action]} ${selectableRequirementIds(view, action).length}`)
        .join(" · ")}
    </span>
  );
}

const queueColumns: readonly CursorTableColumn<ApprovalViewData>[] = [
  {
    header: "审批",
    id: "approval",
    render: (view) => (
      <div className="gf-approvals__table-primary">
        <CopyableText copyLabel="复制审批 ID" scrollable value={view.approval.approval_id} />
        <a href={`/approvals/${encodeURIComponent(view.approval.approval_id)}`}>打开审批详情</a>
        <span>
          {subjectKindLabels[view.approval.subject_kind]} · 受审版本 {view.approval.subject_revision}
        </span>
      </div>
    ),
  },
  {
    header: "提议者",
    id: "proposer",
    render: (view) => (
      <div className="gf-approvals__table-primary">
        <CopyableText
          copyLabel="复制 proposer principal ID"
          scrollable
          value={view.approval.proposer.principal_id}
        />
        <span>{view.approval.proposer.principal_kind}</span>
      </div>
    ),
  },
  {
    header: "内容域",
    id: "domain",
    render: (view) => (
      <span className="gf-approvals__domain-list">
        {view.approval.domain_scope.domain_ids.map((domainId) => (
          <code className="gf-approvals__bounded-id" key={domainId} tabIndex={0}>
            {domainId}
          </code>
        ))}
      </span>
    ),
  },
  {
    header: "流程状态",
    id: "workflow",
    render: (view) => (
      <span className="gf-approvals__nowrap" tabIndex={0}>
        {approvalStatusLabels[view.approval.status]} · 流程版本 {view.approval.workflow_revision}
      </span>
    ),
  },
  {
    header: "审批进度",
    id: "progress",
    render: (view) => {
      const satisfied = view.requirement_progress.filter((progress) => progress.satisfied).length;
      return (
        <span className="gf-approvals__nowrap" tabIndex={0}>
          {satisfied} / {view.requirement_progress.length} 项职责已满足
        </span>
      );
    },
  },
  {
    header: "当前可处理",
    id: "eligibility",
    render: actionRange,
  },
];

function QueueReadError({ error, onRestart }: { error: Error; onRestart(): void }) {
  if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
  return (
    <StatePanel
      action={
        <button className="gf-secondary-button" onClick={onRestart} type="button">
          从第一页重新读取
        </button>
      }
      description="审批队列读取失败；页面不会合并不同的读取快照。"
      state="error"
      title="无法读取审批队列"
    />
  );
}

function ApprovalsHero() {
  return (
    <header className="gf-approvals__hero">
      <div>
        <p className="gf-approvals__kicker">制作与审查</p>
        <h1>审批队列</h1>
        <p>
          这里只展示服务端按当前身份投影的 <code>assignee=me</code>{" "}
          队列；权限、有效票与职责分离仍由平台守卫决定。
        </p>
      </div>
      <div aria-hidden="true" className="gf-approvals__hero-mark">
        <Gavel size={25} />
        <span>人工门禁</span>
      </div>
    </header>
  );
}

export function ApprovalsPage({ api = approvalsApi }: { api?: ApprovalsApi }) {
  const initialQuery = useQuery({
    queryFn: () => api.listMine(null),
    queryKey: ["approvals", "mine"],
    retry: false,
  });
  const [queue, setQueue] = useState<QueueState | null>(null);
  const requestEpoch = useRef(0);

  useEffect(() => {
    if (!initialQuery.data) return;
    requestEpoch.current += 1;
    setQueue({
      error: null,
      items: initialQuery.data.items,
      loading: false,
      nextCursor: initialQuery.data.next_cursor ?? null,
      readSnapshotId: initialQuery.data.read_snapshot_id,
    });
  }, [initialQuery.data]);

  async function readPage(cursor: string | null, restart: boolean) {
    const current = queue;
    const epoch = ++requestEpoch.current;
    if (current) setQueue({ ...current, error: null, loading: true });
    try {
      const next = await api.listMine(cursor);
      if (requestEpoch.current !== epoch) return;
      if (!restart && current && next.read_snapshot_id !== current.readSnapshotId) {
        throw new Error("Approval read snapshot changed.");
      }
      setQueue({
        error: null,
        items: restart || !current ? next.items : [...current.items, ...next.items],
        loading: false,
        nextCursor: next.next_cursor ?? null,
        readSnapshotId: next.read_snapshot_id,
      });
    } catch (error) {
      if (requestEpoch.current !== epoch) return;
      if (current) {
        setQueue({
          ...current,
          error: normalizedError(error, "审批队列读取失败。"),
          loading: false,
        });
      }
    }
  }

  if (initialQuery.isPending && queue === null) {
    return (
      <article className="gf-approvals gf-page">
        <ApprovalsHero />
        <StatePanel
          description="正在读取当前身份可处理的审批职责。"
          state="loading"
          title="正在载入审批队列"
        />
      </article>
    );
  }
  if (initialQuery.error && queue === null) {
    return (
      <article className="gf-approvals gf-page">
        <ApprovalsHero />
        <QueueReadError
          error={normalizedError(initialQuery.error, "审批队列读取失败。")}
          onRestart={() => void initialQuery.refetch()}
        />
      </article>
    );
  }
  if (queue === null) return null;

  return (
    <article className="gf-approvals gf-page">
      <ApprovalsHero />

      {queue.error && !(queue.error instanceof CursorExpiredError) && (
        <QueueReadError error={queue.error} onRestart={() => void readPage(null, true)} />
      )}
      <CursorTable
        caption="待我审批"
        columns={queueColumns}
        emptyLabel="当前没有可处理的审批职责。"
        getRowKey={(view) => view.approval.approval_id}
        headingLevel={2}
        items={queue.items}
        nextCursor={queue.nextCursor}
        onLoadMore={(cursor) => void readPage(cursor, false)}
        onRestart={() => void readPage(null, true)}
        paginationState={queuePaginationState(queue)}
        toolbar={<span className="u-small">读取快照 · {queue.readSnapshotId}</span>}
      />
    </article>
  );
}

function PolicyValue({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        <CopyableText copyLabel={`复制 ${label}`} scrollable value={value} />
      </dd>
    </div>
  );
}

function EligibilityCell({
  action,
  progress,
}: {
  action: ApprovalAction;
  progress: ApprovalViewData["requirement_progress"][number];
}) {
  const eligibility = actionEligibility(progress, action);
  if (eligibility.eligible) {
    return <span className="u-status u-status--ok">{actionLabels[action]}可用</span>;
  }
  return (
    <span className="gf-approvals__reason-list">
      {eligibility.reason_codes.map((reason) => (
        <span className="u-status u-status--suggestion" key={reason}>
          {eligibilityReasonLabel(reason)}
        </span>
      ))}
    </span>
  );
}

function domainLabel(domainId: string): string {
  return (
    {
      builtin: "内置规则域",
      "domain:combat": "战斗系统",
      "domain:economy": "经济系统",
      "domain:narrative": "叙事内容",
      "domain:quest": "任务系统",
      "domain:rewards": "奖励系统",
    }[domainId] ?? domainId
  );
}

function frozenPermissionLabel(
  permission: ApprovalViewData["approval"]["requirements"][number]["required_permission"],
): string {
  if (permission.action === "approval.decide") return "作出审批决定";
  const action = permission.action === "approve" ? "批准" : permission.action;
  const resource = permission.resource_kind === "constraint_proposal" ? "约束提案" : permission.resource_kind;
  return `${action}${resource}`;
}

function RequirementTable({
  action,
  onToggle,
  selected,
  view,
}: {
  action: ApprovalAction;
  onToggle(requirementId: string): void;
  selected: readonly string[];
  view: ApprovalViewData;
}) {
  const selectedIds = new Set(selected);
  return (
    <div className="gf-approvals__table-scroll" tabIndex={0}>
      <table aria-label="审批职责进度" className="gf-approvals__requirement-table">
        <thead>
          <tr>
            <th scope="col">选择</th>
            <th scope="col">审批职责与内容域</th>
            <th scope="col">冻结的审批规则</th>
            <th scope="col">有效票数</th>
            <th scope="col">职责隔离</th>
            <th scope="col">批准</th>
            <th scope="col">驳回</th>
            <th scope="col">请修改</th>
          </tr>
        </thead>
        <tbody>
          {requirementRows(view).map(({ progress, requirement }) => {
            const checked = selectedIds.has(progress.requirement_id);
            const currentEligibility = actionEligibility(progress, action);
            const role = messages.roles[progress.route_role];
            const domains = progress.domain_scope.domain_ids.map(domainLabel);
            return (
              <tr key={progress.requirement_id}>
                <td>
                  <input
                    aria-label={`选择 ${role} · ${domains.join("、")}`}
                    checked={checked}
                    disabled={!checked && !currentEligibility.eligible}
                    onChange={() => onToggle(progress.requirement_id)}
                    type="checkbox"
                  />
                </td>
                <th scope="row">
                  <strong>{role}</strong>
                  <span className="gf-approvals__domain-list">
                    {domains.map((domain) => (
                      <span key={domain}>{domain}</span>
                    ))}
                  </span>
                  <details>
                    <summary>技术信息</summary>
                    <code className="gf-approvals__bounded-id" tabIndex={0}>
                      {progress.requirement_id}
                    </code>
                  </details>
                </th>
                <td>
                  <strong>
                    至少 {progress.min_approvals} 位{role}
                  </strong>
                  <span>{frozenPermissionLabel(requirement.required_permission)}</span>
                  {requirement.assignee_principal_ids.length > 0 && (
                    <span>
                      指定审批人
                      {requirement.assignee_principal_ids.map((principalId) => (
                        <code className="gf-approvals__bounded-id" key={principalId} tabIndex={0}>
                          {principalId}
                        </code>
                      ))}
                    </span>
                  )}
                </td>
                <td>
                  <strong>
                    {progress.valid_approval_count} / {progress.min_approvals}
                  </strong>
                  <span>{progress.satisfied ? "已满足" : "仍需审批"}</span>
                </td>
                <td>
                  {progress.unmet_distinct_from_requirement_ids.length > 0 ? (
                    progress.unmet_distinct_from_requirement_ids.map((requirementId) => (
                      <code className="gf-approvals__bounded-id" key={requirementId} tabIndex={0}>
                        {requirementId}
                      </code>
                    ))
                  ) : (
                    <span>无职责隔离缺口</span>
                  )}
                </td>
                <td>
                  <EligibilityCell action="approve" progress={progress} />
                </td>
                <td>
                  <EligibilityCell action="reject" progress={progress} />
                </td>
                <td>
                  <EligibilityCell action="request_changes" progress={progress} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function DecisionLedger({ view }: { view: ApprovalViewData }) {
  return (
    <section aria-label="不可变决定记录" className="gf-approvals__section">
      <header className="gf-approvals__section-heading">
        <LockKeyhole aria-hidden="true" size={20} />
        <div>
          <h2>不可变决定记录</h2>
          <p>每条决定都绑定当时的流程版本；当前有效票数由服务端重新投影。</p>
        </div>
      </header>
      {view.approval.decisions.length === 0 ? (
        <StatePanel description="尚未追加任何决定。" state="empty" title="暂无决定" />
      ) : (
        <ol className="gf-approvals__decision-ledger">
          {view.approval.decisions.map((decision) => (
            <li key={decision.decision_id}>
              <header>
                <span className="u-status u-status--info">{actionLabels[decision.decision]}</span>
                <time dateTime={decision.occurred_at}>{decision.occurred_at}</time>
              </header>
              <CopyableText copyLabel="复制决定 ID" scrollable value={decision.decision_id} />
              <dl>
                <div>
                  <dt>决定人</dt>
                  <dd>
                    <code className="gf-approvals__bounded-id" tabIndex={0}>
                      {decision.actor.principal_id}
                    </code>
                    {decision.actor.principal_kind}
                  </dd>
                </div>
                <div>
                  <dt>提交时流程版本</dt>
                  <dd>{decision.expected_workflow_revision}</dd>
                </div>
                <div>
                  <dt>原因代码</dt>
                  <dd>
                    <code className="gf-approvals__bounded-id" tabIndex={0}>
                      {decision.reason_code}
                    </code>
                  </dd>
                </div>
                <div>
                  <dt>审批职责</dt>
                  <dd>
                    {decision.requirement_ids.map((id) => (
                      <code className="gf-approvals__bounded-id" key={id} tabIndex={0}>
                        {id}
                      </code>
                    ))}
                  </dd>
                </div>
              </dl>
              {decision.comment && <p>{decision.comment}</p>}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function decisionSuccessMessage(action: ApprovalAction, view: ApprovalViewData): string {
  if (action === "approve" && view.approval.status === "pending_approval") {
    return "部分批准已记录；其他审批职责仍待处理。";
  }
  return `${actionLabels[action]}决定已记录。`;
}

export function ApprovalDetailPage({
  api = approvalsApi,
  approvalId,
}: {
  api?: ApprovalsApi;
  approvalId: string;
}) {
  const queryClient = useQueryClient();
  const { pushToast } = useToast();
  const detailQuery = useQuery({
    queryFn: () => api.getApproval(approvalId),
    queryKey: approvalQueryKey(approvalId),
    retry: false,
  });
  const [action, setAction] = useState<ApprovalAction>("approve");
  const [selected, setSelected] = useState<string[]>([]);
  const [reasonCode, setReasonCode] = useState("");
  const [comment, setComment] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);

  const mutation = useMutation({
    mutationFn: async ({
      current,
      decision,
    }: {
      current: VersionedApproval;
      decision: ApprovalDecisionIntent;
    }) => api.decide(current, decision, createMutationIntent()),
    onError(error) {
      if (error instanceof ApiProblemError && error.problem.status === 403) {
        pushToast({
          message: "当前角色或权限已变化；正在重新读取服务端 eligibility。",
          tone: "info",
        });
        void detailQuery.refetch();
      }
    },
    onSuccess(next, variables) {
      queryClient.setQueryData(approvalQueryKey(approvalId), next);
      void queryClient.invalidateQueries({ queryKey: ["approvals", "mine"] });
      pushToast({
        message: decisionSuccessMessage(variables.decision.action, next.value),
        tone: "success",
      });
      setSelected([]);
      setReasonCode("");
      setComment("");
    },
    retry: false,
  });

  const current = detailQuery.data;
  const view = current?.value;
  const subjectReview = useQuery({
    enabled: view !== undefined,
    queryFn: () => {
      if (!view) throw new Error("Approval detail is unavailable.");
      return loadApprovalSubjectReview(api, view);
    },
    queryKey: ["approvals", "subject-review", approvalId, view?.approval.workflow_revision],
    retry: false,
  });
  const canSubmit =
    view !== undefined &&
    subjectReview.isSuccess &&
    selectionIsEligible(view, action, selected) &&
    reasonCode.trim().length > 0 &&
    reasonCode !== "__custom__" &&
    !mutation.isPending;
  const selectedEligibilityChanged =
    view !== undefined && selected.length > 0 && !selectionIsEligible(view, action, selected);
  const needsExplicitReconfirmation =
    view !== undefined &&
    view.approval.status === "pending_approval" &&
    view.requirement_progress.length > 0 &&
    view.requirement_progress.every((progress) => progress.satisfied);
  const currentActorCanReconfirm = view !== undefined && selectableRequirementIds(view, "approve").length > 0;
  const confirmationSubject = subjectReview.data
    ? subjectReview.data.kind === "patch"
      ? `${subjectReview.data.subject.patch.ops.length} 项 Patch 变更`
      : subjectReview.data.kind === "constraint_proposal"
        ? `${subjectReview.data.subject.proposal.constraints.length} 条约束`
        : `回退 ${subjectReview.data.subject.request.ref_name}`
    : "未完成核对的受审内容";
  const confirmationTarget = view?.approval.target_binding?.ref_name ?? "未绑定目标";
  const reasonOptions = decisionReasons[action];
  const reasonSelection =
    reasonCode === ""
      ? ""
      : reasonOptions.some((option) => option.value === reasonCode)
        ? reasonCode
        : "__custom__";

  const frozenPolicyRows = useMemo(() => {
    if (!view) return [];
    return [
      ["审批策略", view.approval.approval_policy.policy_version],
      ["审批策略摘要", view.approval.approval_policy.policy_digest],
      ["角色策略", view.approval.role_policy_version],
      ["角色策略摘要", view.approval.role_policy_digest],
      ["路由策略", view.approval.route_policy.route_version],
      ["路由策略摘要", view.approval.route_policy.route_digest],
      ["内容域注册表", view.approval.domain_registry_ref.registry_version],
      ["内容域注册表摘要", view.approval.domain_registry_ref.registry_digest],
    ] as const;
  }, [view]);

  function toggleRequirement(requirementId: string) {
    setSelected((currentSelection) =>
      currentSelection.includes(requirementId)
        ? currentSelection.filter((value) => value !== requirementId)
        : [...currentSelection, requirementId],
    );
  }

  function executeDecision() {
    if (!current || !canSubmit) return;
    mutation.mutate({
      current,
      decision: {
        action,
        comment: comment.trim().length > 0 ? comment : null,
        reasonCode: reasonCode.trim(),
        requirementIds: selected,
      },
    });
  }

  async function refreshAfterConflict() {
    await detailQuery.refetch();
    mutation.reset();
  }

  if (detailQuery.isPending) {
    return (
      <StatePanel
        description="正在读取不可变决定、当前有效票与逐动作 eligibility。"
        headingLevel={1}
        state="loading"
        title="正在载入审批详情"
      />
    );
  }
  if (detailQuery.error || !view || !current) {
    const error = detailQuery.error;
    if (error instanceof ApiProblemError) return <ProblemPanel problem={error.problem} />;
    return (
      <StatePanel
        action={
          <button className="gf-secondary-button" onClick={() => void detailQuery.refetch()} type="button">
            重试
          </button>
        }
        description="审批详情未能形成完整的版本化视图。"
        headingLevel={1}
        state="error"
        title="无法读取审批详情"
      />
    );
  }

  return (
    <article className="gf-approvals gf-page">
      <header className="gf-approvals__hero gf-approvals__hero--detail">
        <div>
          <p className="gf-approvals__kicker">精确人工授权</p>
          <h1>审批详情</h1>
          <div className="gf-approvals__hero-id">
            <CopyableText copyLabel="复制审批 ID" scrollable value={view.approval.approval_id} />
          </div>
          <p>
            {approvalStatusLabels[view.approval.status]} · 流程版本 {view.approval.workflow_revision}
          </p>
        </div>
        <div aria-hidden="true" className="gf-approvals__hero-mark">
          <ShieldCheck size={25} />
          <span>版本 {view.approval.workflow_revision}</span>
        </div>
      </header>

      <section aria-label="审批权威" className="gf-approvals__section">
        <header className="gf-approvals__section-heading">
          <UsersRound aria-hidden="true" size={20} />
          <div>
            <h2>受审对象与提议者</h2>
            <p>提议者、受审对象与内容域均来自冻结的审批记录。</p>
          </div>
        </header>
        <dl className="gf-approvals__authority-grid">
          <div>
            <dt>提议者</dt>
            <dd>
              <code className="gf-approvals__bounded-id" tabIndex={0}>
                {view.approval.proposer.principal_id}
              </code>
              {view.approval.proposer.principal_kind}
            </dd>
          </div>
          <div>
            <dt>受审对象</dt>
            <dd>
              {subjectKindLabels[view.approval.subject_kind]} · 版本 {view.approval.subject_revision}
            </dd>
            <a href={approvalSubjectHref(view.approval)}>打开受审对象</a>
          </div>
          <div>
            <dt>受审工件</dt>
            <dd>
              <CopyableText
                copyLabel="复制受审工件 ID"
                scrollable
                value={view.approval.subject_artifact_id}
              />
            </dd>
          </div>
          <div>
            <dt>受审内容摘要</dt>
            <dd>
              <CopyableText copyLabel="复制受审内容摘要" scrollable value={view.approval.subject_digest} />
            </dd>
          </div>
          <div>
            <dt>内容域</dt>
            <dd className="gf-approvals__domain-list">
              {view.approval.domain_scope.domain_ids.map((domainId) => (
                <code className="gf-approvals__bounded-id" key={domainId} tabIndex={0}>
                  {domainId}
                </code>
              ))}
            </dd>
          </div>
          <div>
            <dt>当前并发版本</dt>
            <dd>
              <code className="gf-approvals__bounded-id" tabIndex={0}>
                {current.etag}
              </code>
            </dd>
          </div>
        </dl>
      </section>

      {subjectReview.isPending ? (
        <StatePanel
          description="正在读取 exact 受审内容、冻结目标与 EvidenceSet。"
          state="loading"
          title="正在准备审批材料"
        />
      ) : subjectReview.isError ? (
        <ApprovalSubjectReviewFailure />
      ) : (
        <ApprovalSubjectReview data={subjectReview.data} />
      )}

      <section aria-label="冻结策略" className="gf-approvals__section">
        <header className="gf-approvals__section-heading">
          <Route aria-hidden="true" size={20} />
          <div>
            <h2>冻结策略闭包</h2>
            <p>页面展示审批记录冻结的版本与摘要，不读取或猜测当前别名。</p>
          </div>
        </header>
        <dl className="gf-approvals__policy-grid">
          {frozenPolicyRows.map(([label, value]) => (
            <PolicyValue key={label} label={label} value={value} />
          ))}
        </dl>
      </section>

      <section className="gf-approvals__section">
        <header className="gf-approvals__section-heading">
          <ClipboardCheck aria-hidden="true" size={20} />
          <div>
            <h2>逐项审批进度</h2>
            <p>票数、职责隔离缺口与三种动作资格全部使用当前服务端投影。</p>
          </div>
        </header>
        {needsExplicitReconfirmation && (
          <div aria-label="需要显式批准确认" className="gf-approvals__reconfirmation" role="status">
            <strong>全部审批职责的当前有效票均已满足，但流程仍处于待审批状态。</strong>
            <span>
              {currentActorCanReconfirm
                ? "当前身份可对已有有效票的审批职责再确认一次，以完成已批准终态。"
                : "需要一名已有当前有效票的审批者对其审批职责再确认一次。"}
            </span>
          </div>
        )}
        <RequirementTable action={action} onToggle={toggleRequirement} selected={selected} view={view} />
      </section>

      <section aria-label="提交审批决定" className="gf-approvals__section">
        <header className="gf-approvals__section-heading">
          <Gavel aria-hidden="true" size={20} />
          <div>
            <h2>提交审批决定</h2>
            <p>只提交勾选的审批职责；流程版本、并发版本与幂等键均绑定本次读取和单次用户意图。</p>
          </div>
        </header>
        <form
          className="gf-approvals__decision-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!canSubmit) return;
            setConfirmOpen(true);
          }}
        >
          <fieldset>
            <legend>决定类型</legend>
            <div className="gf-approvals__action-options">
              {(["approve", "reject", "request_changes"] as const).map((value) => (
                <label key={value}>
                  <input
                    checked={action === value}
                    name="approval-action"
                    onChange={() => {
                      setAction(value);
                      setReasonCode("");
                    }}
                    type="radio"
                    value={value}
                  />
                  <span>{actionLabels[value]}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <label>
            <span>决定原因</span>
            <select onChange={(event) => setReasonCode(event.target.value)} required value={reasonSelection}>
              <option value="">请选择与本次审阅相符的原因</option>
              {reasonOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
              <option value="__custom__">其他原因（高级）</option>
            </select>
          </label>
          {reasonSelection === "__custom__" && (
            <label>
              <span>自定义原因代码（高级）</span>
              <input
                maxLength={512}
                onChange={(event) => setReasonCode(event.target.value)}
                placeholder="例如：project_policy_exception_reviewed"
                required
                value={reasonCode === "__custom__" ? "" : reasonCode}
              />
            </label>
          )}
          <label>
            <span>补充说明</span>
            <textarea
              maxLength={4096}
              onChange={(event) => setComment(event.target.value)}
              rows={4}
              value={comment}
            />
          </label>
          <div className="gf-approvals__selection-summary" role="status">
            <strong>已选择 {selected.length} 项审批职责</strong>
            {selected.map((requirementId) => (
              <code className="gf-approvals__bounded-id" key={requirementId} tabIndex={0}>
                {requirementId}
              </code>
            ))}
            {selectedEligibilityChanged && (
              <span>服务端资格已变化；保留输入，但当前不能提交。可取消勾选后重新选择。</span>
            )}
          </div>
          <PermissionGate allowed={canSubmit} mode="disable">
            <button className="gf-approvals__submit" type="submit">
              {mutation.isPending ? "正在提交…" : `提交${actionLabels[action]}`}
            </button>
          </PermissionGate>
        </form>
      </section>

      {mutation.error instanceof ApiProblemError && (
        <section className="gf-approvals__mutation-problem">
          <ProblemPanel problem={mutation.error.problem} />
          {mutation.error.problem.status === 409 && (
            <button className="gf-secondary-button" onClick={() => void refreshAfterConflict()} type="button">
              刷新审批状态
            </button>
          )}
        </section>
      )}
      {mutation.error && !(mutation.error instanceof ApiProblemError) && (
        <StatePanel
          description="决定未获服务端确认；输入仍保留，页面不会自动重放。"
          state="error"
          title="决定提交失败"
        />
      )}

      <DecisionLedger view={view} />

      <ConfirmDialog
        confirmLabel={`确认${actionLabels[action]}`}
        description={`你将对 ${confirmationSubject} 作出${actionLabels[action]}决定；确定性证据集已通过，目标为 ${confirmationTarget}。将以流程版本 ${view.approval.workflow_revision} 对 ${selected.length} 项已选审批职责追加不可变记录。`}
        onCancel={() => setConfirmOpen(false)}
        onConfirm={() => {
          setConfirmOpen(false);
          executeDecision();
        }}
        open={confirmOpen}
        title={`确认${actionLabels[action]}决定`}
      />
    </article>
  );
}
