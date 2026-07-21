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
import "./approvals.css";

interface QueueState {
  error: Error | null;
  items: ApprovalViewData[];
  loading: boolean;
  nextCursor: string | null;
  readSnapshotId: string;
}

const approvalQueryKey = (approvalId: string) => ["approvals", "detail", approvalId] as const;

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
    header: "Approval",
    id: "approval",
    render: (view) => (
      <div className="gf-approvals__table-primary">
        <CopyableText copyLabel="复制 Approval ID" scrollable value={view.approval.approval_id} />
        <a href={`/approvals/${encodeURIComponent(view.approval.approval_id)}`}>打开审批详情</a>
        <span>
          {view.approval.subject_kind} · subject revision {view.approval.subject_revision}
        </span>
      </div>
    ),
  },
  {
    header: "Proposer",
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
    header: "Domain",
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
    header: "Workflow",
    id: "workflow",
    render: (view) => (
      <span className="gf-approvals__nowrap" tabIndex={0}>
        {view.approval.status} · revision {view.approval.workflow_revision}
      </span>
    ),
  },
  {
    header: "Requirement progress",
    id: "progress",
    render: (view) => {
      const satisfied = view.requirement_progress.filter((progress) => progress.satisfied).length;
      return (
        <span className="gf-approvals__nowrap" tabIndex={0}>
          {satisfied} / {view.requirement_progress.length} requirements satisfied
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
      description="审批队列读取失败；页面不会合并不同 read snapshot。"
      state="error"
      title="无法读取审批队列"
    />
  );
}

function ApprovalsHero() {
  return (
    <header className="gf-approvals__hero">
      <div>
        <p className="gf-approvals__kicker">Maker · checker</p>
        <h1>Approvals</h1>
        <p>
          这里只展示服务端按当前身份投影的 <code>assignee=me</code>{" "}
          队列；权限、有效票与职责分离仍由平台守卫决定。
        </p>
      </div>
      <div aria-hidden="true" className="gf-approvals__hero-mark">
        <Gavel size={25} />
        <span>HUMAN GATE</span>
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
          description="正在读取当前身份可处理的 requirement。"
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
        emptyLabel="当前没有可处理的审批 requirement。"
        getRowKey={(view) => view.approval.approval_id}
        headingLevel={2}
        items={queue.items}
        nextCursor={queue.nextCursor}
        onLoadMore={(cursor) => void readPage(cursor, false)}
        onRestart={() => void readPage(null, true)}
        paginationState={queuePaginationState(queue)}
        toolbar={<span className="u-small">read snapshot · {queue.readSnapshotId}</span>}
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
      <table aria-label="Requirement progress" className="gf-approvals__requirement-table">
        <thead>
          <tr>
            <th scope="col">选择</th>
            <th scope="col">Requirement / domain</th>
            <th scope="col">Frozen route</th>
            <th scope="col">Valid votes</th>
            <th scope="col">Distinct gaps</th>
            <th scope="col">批准</th>
            <th scope="col">驳回</th>
            <th scope="col">请修改</th>
          </tr>
        </thead>
        <tbody>
          {requirementRows(view).map(({ progress, requirement }) => {
            const checked = selectedIds.has(progress.requirement_id);
            const currentEligibility = actionEligibility(progress, action);
            return (
              <tr key={progress.requirement_id}>
                <td>
                  <input
                    aria-label={`选择 ${progress.requirement_id}`}
                    checked={checked}
                    disabled={!checked && !currentEligibility.eligible}
                    onChange={() => onToggle(progress.requirement_id)}
                    type="checkbox"
                  />
                </td>
                <th scope="row">
                  <code className="gf-approvals__bounded-id" tabIndex={0}>
                    {progress.requirement_id}
                  </code>
                  <span className="gf-approvals__domain-list">
                    {progress.domain_scope.domain_ids.map((domainId) => (
                      <code className="gf-approvals__bounded-id" key={domainId} tabIndex={0}>
                        {domainId}
                      </code>
                    ))}
                  </span>
                </th>
                <td>
                  <strong>{messages.roles[progress.route_role]}</strong>
                  <span>
                    min {progress.min_approvals} · {requirement.required_permission.action}{" "}
                    {requirement.required_permission.resource_kind}
                  </span>
                  {requirement.assignee_principal_ids.length > 0 && (
                    <span>
                      assigned
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
                  <span>{progress.satisfied ? "satisfied" : "still required"}</span>
                </td>
                <td>
                  {progress.unmet_distinct_from_requirement_ids.length > 0 ? (
                    progress.unmet_distinct_from_requirement_ids.map((requirementId) => (
                      <code className="gf-approvals__bounded-id" key={requirementId} tabIndex={0}>
                        {requirementId}
                      </code>
                    ))
                  ) : (
                    <span>无未满足 distinct gap</span>
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
          <h2>Immutable decisions</h2>
          <p>每条决定都绑定当时的 workflow revision；当前有效票数由服务端重新投影。</p>
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
              <CopyableText copyLabel="复制 Decision ID" scrollable value={decision.decision_id} />
              <dl>
                <div>
                  <dt>Actor</dt>
                  <dd>
                    <code className="gf-approvals__bounded-id" tabIndex={0}>
                      {decision.actor.principal_id}
                    </code>
                    {decision.actor.principal_kind}
                  </dd>
                </div>
                <div>
                  <dt>Expected revision</dt>
                  <dd>{decision.expected_workflow_revision}</dd>
                </div>
                <div>
                  <dt>Reason</dt>
                  <dd>
                    <code className="gf-approvals__bounded-id" tabIndex={0}>
                      {decision.reason_code}
                    </code>
                  </dd>
                </div>
                <div>
                  <dt>Requirements</dt>
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
    return "部分批准已记录；其他 requirement 仍待处理。";
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
  const canSubmit =
    view !== undefined &&
    selectionIsEligible(view, action, selected) &&
    reasonCode.trim().length > 0 &&
    !mutation.isPending;
  const selectedEligibilityChanged =
    view !== undefined && selected.length > 0 && !selectionIsEligible(view, action, selected);
  const needsExplicitReconfirmation =
    view !== undefined &&
    view.approval.status === "pending_approval" &&
    view.requirement_progress.length > 0 &&
    view.requirement_progress.every((progress) => progress.satisfied);
  const currentActorCanReconfirm = view !== undefined && selectableRequirementIds(view, "approve").length > 0;

  const frozenPolicyRows = useMemo(() => {
    if (!view) return [];
    return [
      ["Approval policy", view.approval.approval_policy.policy_version],
      ["Approval policy digest", view.approval.approval_policy.policy_digest],
      ["Role policy", view.approval.role_policy_version],
      ["Role policy digest", view.approval.role_policy_digest],
      ["Route policy", view.approval.route_policy.route_version],
      ["Route policy digest", view.approval.route_policy.route_digest],
      ["Domain registry", view.approval.domain_registry_ref.registry_version],
      ["Domain registry digest", view.approval.domain_registry_ref.registry_digest],
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
          <p className="gf-approvals__kicker">Exact human authority</p>
          <h1>审批详情</h1>
          <div className="gf-approvals__hero-id">
            <CopyableText copyLabel="复制 Approval ID" scrollable value={view.approval.approval_id} />
          </div>
          <p>
            {view.approval.status} · workflow revision {view.approval.workflow_revision}
          </p>
        </div>
        <div aria-hidden="true" className="gf-approvals__hero-mark">
          <ShieldCheck size={25} />
          <span>REV {view.approval.workflow_revision}</span>
        </div>
      </header>

      <section aria-label="审批权威" className="gf-approvals__section">
        <header className="gf-approvals__section-heading">
          <UsersRound aria-hidden="true" size={20} />
          <div>
            <h2>Subject & proposer</h2>
            <p>提议者、受审对象与 domain scope 均来自 ApprovalItem。</p>
          </div>
        </header>
        <dl className="gf-approvals__authority-grid">
          <div>
            <dt>Proposer</dt>
            <dd>
              <code className="gf-approvals__bounded-id" tabIndex={0}>
                {view.approval.proposer.principal_id}
              </code>
              {view.approval.proposer.principal_kind}
            </dd>
          </div>
          <div>
            <dt>Subject</dt>
            <dd>
              {view.approval.subject_kind} · revision {view.approval.subject_revision}
            </dd>
            <a href={approvalSubjectHref(view.approval)}>打开受审对象</a>
          </div>
          <div>
            <dt>Subject Artifact</dt>
            <dd>
              <CopyableText
                copyLabel="复制 Subject Artifact ID"
                scrollable
                value={view.approval.subject_artifact_id}
              />
            </dd>
          </div>
          <div>
            <dt>Subject digest</dt>
            <dd>
              <CopyableText copyLabel="复制 Subject digest" scrollable value={view.approval.subject_digest} />
            </dd>
          </div>
          <div>
            <dt>Domain scope</dt>
            <dd className="gf-approvals__domain-list">
              {view.approval.domain_scope.domain_ids.map((domainId) => (
                <code className="gf-approvals__bounded-id" key={domainId} tabIndex={0}>
                  {domainId}
                </code>
              ))}
            </dd>
          </div>
          <div>
            <dt>ETag authority</dt>
            <dd>
              <code className="gf-approvals__bounded-id" tabIndex={0}>
                {current.etag}
              </code>
            </dd>
          </div>
        </dl>
      </section>

      <section aria-label="冻结策略" className="gf-approvals__section">
        <header className="gf-approvals__section-heading">
          <Route aria-hidden="true" size={20} />
          <div>
            <h2>Frozen policy closure</h2>
            <p>页面展示 ApprovalItem 冻结的版本与摘要，不读取或猜测 current alias。</p>
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
            <h2>Requirement-level progress</h2>
            <p>票数、distinct gap 与三种动作资格全部使用当前服务端投影。</p>
          </div>
        </header>
        {needsExplicitReconfirmation && (
          <div aria-label="需要显式批准确认" className="gf-approvals__reconfirmation" role="status">
            <strong>全部 requirement 的当前有效票均已满足，但 workflow 仍为 pending_approval。</strong>
            <span>
              {currentActorCanReconfirm
                ? "当前身份可对已有有效票的 requirement 再确认一次，以完成 approved 终态。"
                : "需要一名已有当前有效票的审批者对其 requirement 再确认一次。"}
            </span>
          </div>
        )}
        <RequirementTable action={action} onToggle={toggleRequirement} selected={selected} view={view} />
      </section>

      <section aria-label="提交审批决定" className="gf-approvals__section">
        <header className="gf-approvals__section-heading">
          <Gavel aria-hidden="true" size={20} />
          <div>
            <h2>Append a decision</h2>
            <p>只提交勾选的 requirement IDs；revision、ETag 与 idempotency 由当前读取和单次用户意图绑定。</p>
          </div>
        </header>
        <form
          className="gf-approvals__decision-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (!canSubmit) return;
            if (action === "approve") executeDecision();
            else setConfirmOpen(true);
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
                    onChange={() => setAction(value)}
                    type="radio"
                    value={value}
                  />
                  <span>{actionLabels[value]}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <label>
            <span>决定原因代码</span>
            <input
              maxLength={512}
              onChange={(event) => setReasonCode(event.target.value)}
              required
              value={reasonCode}
            />
          </label>
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
            <strong>已选择 {selected.length} 个 requirement</strong>
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
        description={`将以 workflow revision ${view.approval.workflow_revision} 对 ${selected.length} 个已选 requirement 追加不可变${actionLabels[action]}决定。`}
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
