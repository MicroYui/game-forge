import { useQuery } from "@tanstack/react-query";
import { GitCommitHorizontal, History, RotateCcw } from "lucide-react";
import { useMemo, useRef, useState } from "react";

import { createMutationIntent, ReauthenticationRequiredError } from "../../api/csrf";
import type { components } from "../../api/generated/openapi";
import { cursorFromPage } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { compactDateTime, TechnicalDetails } from "../../components/identity";
import { CopyableText } from "../../components/tables";
import { ProblemPanel, StatePanel } from "../../components/ui";
import { currentRefFromCompleteHistory } from "./authority";
import {
  patchWorkflowApi,
  type ApprovalView,
  type ArtifactPayloadView,
  type PatchWorkflowApi,
  type RefHistoryEntry,
  type RollbackDraftRequest,
  type RollbackRequestReadView,
} from "./api";
import {
  collectRollbackSnapshotDiff,
  RollbackContentComparison,
  type RollbackSnapshotDiff,
} from "./RollbackContentComparison";
import "./patches.css";
import { profileKey } from "../execution-profiles";

type ExecutionProfile = components["schemas"]["ExecutionProfileViewV1"];

interface RefHistoryData {
  current: Readonly<components["schemas"]["RefValue"]>;
  entries: RefHistoryEntry[];
  readSnapshotId: string;
}

interface MutationState {
  error: Error | null;
  pending: boolean;
  retry: (() => Promise<void>) | null;
}

interface DraftProfilesData {
  contentDiff: RollbackSnapshotDiff | null;
  currentArtifact: ArtifactPayloadView;
  profiles: ExecutionProfile[];
  targetArtifact: ArtifactPayloadView;
}

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("Rollback draft operation failed.");
}

function unknownOutcome(error: Error): boolean {
  return !(error instanceof ApiProblemError) && !(error instanceof ReauthenticationRequiredError);
}

function rollbackProfileBusinessLabel(profile: ExecutionProfile): string {
  return /\p{Script=Han}/u.test(profile.display_name) ? profile.display_name : "安全回退验证方案";
}

function supportsRunKind(profile: ExecutionProfile, kind: string): boolean {
  return profile.compatible_run_kinds.some((candidate) => candidate.kind === kind && candidate.version === 1);
}

function profileCoversDomains(profile: ExecutionProfile, requiredDomainIds: readonly string[]): boolean {
  const covered = new Set(profile.domain_scope.domain_ids);
  return requiredDomainIds.every((domainId) => covered.has(domainId));
}

function artifactDomainIds(
  scope: components["schemas"]["ArtifactSummaryV1"]["domain_scope"],
): readonly string[] {
  if (scope === null || scope === "all") {
    throw new Error("Rollback draft requires an exact domain-scoped Artifact.");
  }
  return scope.domain_ids;
}

async function collectHistory(
  api: PatchWorkflowApi,
  refName: string,
): Promise<{ entries: RefHistoryEntry[]; readSnapshotId: string }> {
  const entries: RefHistoryEntry[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listRefHistory(refName, cursor);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new Error("Ref history changed read snapshot.");
    }
    readSnapshotId = page.read_snapshot_id;
    entries.push(...page.items);
    const next = cursorFromPage(page);
    if (next === null) {
      return { entries, readSnapshotId: readSnapshotId ?? "" };
    }
    if (seen.has(next)) throw new Error("Ref history returned a cursor cycle.");
    seen.add(next);
    cursor = next;
  }
  throw new Error("Ref history exceeded its bounded page count.");
}

async function collectRollbackProfiles(api: PatchWorkflowApi): Promise<ExecutionProfile[]> {
  const profiles: ExecutionProfile[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listExecutionProfiles(
      { limit: 100, profile_kind: "rollback", status: "active" },
      cursor,
    );
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new Error("Rollback profile catalog changed read snapshot.");
    }
    readSnapshotId = page.read_snapshot_id;
    for (const profile of page.items) {
      if (profile.profile_kind !== "rollback" || profile.status !== "active") {
        throw new Error("Rollback profile catalog returned a non-active rollback item.");
      }
      profiles.push(profile);
    }
    const next = cursorFromPage(page);
    if (next === null) return profiles;
    if (seen.has(next)) throw new Error("Rollback profile catalog returned a cursor cycle.");
    seen.add(next);
    cursor = next;
  }
  throw new Error("Rollback profile catalog exceeded its bounded page count.");
}

async function collectApprovals(api: PatchWorkflowApi): Promise<ApprovalView[]> {
  const approvals: ApprovalView[] = [];
  const approvalIds = new Set<string>();
  const cursors = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listApprovals(cursor);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new Error("Approval catalog changed read snapshot.");
    }
    readSnapshotId = page.read_snapshot_id;
    for (const approval of page.items) {
      if (approvalIds.has(approval.approval.approval_id)) {
        throw new Error("Approval catalog returned a duplicate Approval.");
      }
      approvalIds.add(approval.approval.approval_id);
      approvals.push(approval);
    }
    const next = cursorFromPage(page);
    if (next === null) return approvals;
    if (cursors.has(next)) throw new Error("Approval catalog returned a cursor cycle.");
    cursors.add(next);
    cursor = next;
  }
  throw new Error("Approval catalog exceeded its bounded page count.");
}

async function loadRefHistory(api: PatchWorkflowApi, refName: string): Promise<RefHistoryData> {
  const history = await collectHistory(api, refName);
  const current = currentRefFromCompleteHistory(refName, history.entries, null);
  return { current, entries: history.entries, readSnapshotId: history.readSnapshotId };
}

async function loadDraftProfiles(
  api: PatchWorkflowApi,
  currentArtifactId: string,
  targetArtifactId: string,
): Promise<DraftProfilesData> {
  const [currentArtifact, targetArtifact, profiles] = await Promise.all([
    api.getArtifact(currentArtifactId),
    currentArtifactId === targetArtifactId ? Promise.resolve(null) : api.getArtifact(targetArtifactId),
    collectRollbackProfiles(api),
  ]);
  if (
    currentArtifact.artifact.artifact_id !== currentArtifactId ||
    (targetArtifact !== null && targetArtifact.artifact.artifact_id !== targetArtifactId)
  ) {
    throw new Error("Rollback draft Artifact domain authority is inconsistent.");
  }
  const currentDomainIds = artifactDomainIds(currentArtifact.artifact.domain_scope);
  const targetScope = targetArtifact?.artifact.domain_scope ?? currentArtifact.artifact.domain_scope;
  const targetDomainIds = artifactDomainIds(targetScope);
  const targetDomains = new Set(targetDomainIds);
  if (currentDomainIds.some((domainId) => !targetDomains.has(domainId))) {
    throw new Error("Historical rollback target does not cover the current ref domain scope.");
  }
  const resolvedTargetArtifact = targetArtifact ?? currentArtifact;
  return {
    contentDiff: await collectRollbackSnapshotDiff(api, currentArtifact, resolvedTargetArtifact),
    currentArtifact,
    profiles: profiles.filter(
      (profile) =>
        supportsRunKind(profile, "rollback.validate") && profileCoversDomains(profile, targetDomainIds),
    ),
    targetArtifact: resolvedTargetArtifact,
  };
}

function reversalCandidates(
  approvals: readonly ApprovalView[],
  refName: string,
  current: components["schemas"]["RefValue"],
  target: components["schemas"]["RefValue"],
): ApprovalView[] {
  if (current.revision !== target.revision + 1) return [];
  return approvals.filter(({ approval }) => {
    const binding = approval.target_binding;
    return (
      approval.status === "applied" &&
      binding !== null &&
      binding !== undefined &&
      binding.ref_name === refName &&
      binding.target_artifact_id === current.artifact_id &&
      binding.expected_ref?.artifact_id === target.artifact_id &&
      binding.expected_ref.revision === target.revision
    );
  });
}

function refDisplayName(refName: string): string {
  const segments = refName.split("/").filter(Boolean);
  const leaf = segments[segments.length - 1] ?? refName;
  if (["live", "head", "current"].includes(leaf)) return "当前正式内容";
  if (refName.includes("constraint")) return "正式约束";
  return `发布内容 · ${leaf}`;
}

function approvalSubjectLabel(kind: ApprovalView["approval"]["subject_kind"]): string {
  if (kind === "patch") return "内容修改";
  if (kind === "rollback_request") return "版本回退";
  return "约束发布";
}

export function RefHistoryPage({
  api = patchWorkflowApi,
  refName,
}: {
  api?: PatchWorkflowApi;
  refName: string;
}) {
  const history = useQuery({
    queryFn: () => loadRefHistory(api, refName),
    queryKey: ["ref-history", refName],
    retry: false,
  });
  const mutationLock = useRef(false);
  const [selectedRevision, setSelectedRevision] = useState<number | null>(null);
  const [profileSelection, setProfileSelection] = useState("");
  const [reason, setReason] = useState("");
  const [reversesApprovalId, setReversesApprovalId] = useState("");
  const [mutation, setMutation] = useState<MutationState | null>(null);
  const [created, setCreated] = useState<RollbackRequestReadView | null>(null);

  const selectedEntry = history.data?.entries.find((entry) => entry.value.revision === selectedRevision);
  const draftProfiles = useQuery({
    enabled: history.data !== undefined && selectedEntry !== undefined,
    queryFn: () =>
      loadDraftProfiles(api, history.data!.current.artifact_id, selectedEntry!.value.artifact_id),
    queryKey: [
      "ref-history",
      refName,
      "rollback-profiles",
      history.data?.current.artifact_id,
      history.data?.current.revision,
      selectedEntry?.value.artifact_id,
      selectedEntry?.value.revision,
    ],
    retry: false,
  });
  const approvals = useQuery({
    enabled: selectedEntry !== undefined,
    queryFn: () => collectApprovals(api),
    queryKey: ["ref-history", refName, "reversal-approvals"],
    retry: false,
  });
  const selectedProfile = draftProfiles.data?.profiles.find(
    (profile) => profileKey(profile) === profileSelection,
  );
  const approvalCandidates = useMemo(
    () =>
      history.data && selectedEntry
        ? reversalCandidates(approvals.data ?? [], refName, history.data.current, selectedEntry.value)
        : [],
    [approvals.data, history.data, refName, selectedEntry],
  );
  const historicalEntries = useMemo(
    () =>
      history.data
        ? history.data.entries.filter((entry) => entry.value.revision < history.data.current.revision)
        : [],
    [history.data],
  );

  function draft() {
    const data = history.data;
    if (
      !data ||
      !selectedEntry ||
      !selectedProfile ||
      reason.trim() === "" ||
      selectedEntry.value.revision >= data.current.revision ||
      mutation !== null
    ) {
      return;
    }
    const request: RollbackDraftRequest = {
      expected_current_ref: data.current,
      reason: reason.trim(),
      request_schema_version: "rollback-draft-request@1",
      reverses_approval_id: reversesApprovalId.trim() || null,
      rollback_profile: selectedProfile.profile,
      target_artifact_id: selectedEntry.value.artifact_id,
      target_history_revision: selectedEntry.value.revision,
    };
    const intent = createMutationIntent();
    const execute = async () => {
      if (mutationLock.current) return;
      mutationLock.current = true;
      setMutation({ error: null, pending: true, retry: null });
      try {
        const result = await api.draftRollback(refName, request, intent);
        setCreated(result);
        setMutation(null);
      } catch (error) {
        const normalized = normalizedError(error);
        setMutation({
          error: normalized,
          pending: false,
          retry: unknownOutcome(normalized) ? execute : null,
        });
      } finally {
        mutationLock.current = false;
      }
    };
    void execute();
  }

  async function reload() {
    const refreshed = await history.refetch();
    if (refreshed.isSuccess) {
      setMutation(null);
      setCreated(null);
    }
  }

  if (history.isPending) {
    return (
      <div className="gf-page gf-patches">
        <StatePanel
          description="正在读取完整分页、严格递增且绑定 read snapshot 的 RBAC-visible ref history。"
          headingLevel={1}
          state="loading"
          title="正在读取 Ref History"
        />
      </div>
    );
  }

  if (history.isError) {
    return (
      <div className="gf-page gf-patches">
        {history.error instanceof ApiProblemError ? (
          <ProblemPanel problem={history.error.problem} />
        ) : (
          <StatePanel
            action={<button onClick={() => void history.refetch()}>重新读取</button>}
            description="必须从同一 read snapshot 读完整 history，页面不会用局部历史猜测 current ref。"
            headingLevel={1}
            state="error"
            title="Ref History authority 不可用"
          />
        )}
      </div>
    );
  }

  const data = history.data;
  const canDraft =
    selectedEntry !== undefined &&
    selectedEntry.value.revision < data.current.revision &&
    selectedProfile !== undefined &&
    reason.trim() !== "" &&
    mutation === null;

  return (
    <div className="gf-page gf-patches gf-ref-history" data-layout="editorial-ref-history">
      <nav aria-label="版本历史导航" className="gf-patches__back-nav">
        <a href="/patches">返回修改工作台</a>
        <a href={`/artifacts/${encodeURIComponent(data.current.artifact_id)}`}>查看当前版本完整记录</a>
      </nav>

      <header className="gf-patches__hero">
        <div>
          <p className="gf-patches__kicker">可追溯的正式版本</p>
          <h1>{refDisplayName(refName)}的版本历史</h1>
          <p>
            选择一个历史版本即可发起安全回退。创建请求、验证和审批都不会改变正式内容，最终应用后才会更新。
          </p>
          <TechnicalDetails items={[{ label: "Ref name", value: refName }]} summary="查看发布位置技术信息" />
        </div>
        <div className="gf-patches__hero-mark" aria-hidden="true">
          <History size={30} />
          <span>版本</span>
        </div>
      </header>

      <dl className="gf-patches__facts" aria-label="当前正式版本">
        <div>
          <dt>当前版本</dt>
          <dd>第 {data.current.revision} 版</dd>
        </div>
      </dl>
      <TechnicalDetails
        items={[
          { label: "Current Artifact ID", value: data.current.artifact_id },
          { label: "Read snapshot", value: data.readSnapshotId },
        ]}
        summary="查看版本读取技术信息"
      />

      <section className="gf-patches__workspace-section" aria-labelledby="ref-timeline-title">
        <header>
          <GitCommitHorizontal aria-hidden="true" size={21} />
          <div>
            <h2 id="ref-timeline-title">选择要恢复的历史版本</h2>
            <p>当前版本无需回退；请从下方选择更早的版本，并先核对回退后的内容变化。</p>
          </div>
        </header>
        <ol className="gf-patches__history-list gf-patches__history-list--selectable">
          {[...data.entries].reverse().map((entry) => {
            const isCurrent = entry.value.revision === data.current.revision;
            return (
              <li key={entry.value.revision}>
                <span>第 {entry.value.revision} 版</span>
                <div>
                  {isCurrent ? (
                    <strong>当前使用中的版本</strong>
                  ) : (
                    <label>
                      <input
                        aria-label={`回退到第 ${entry.value.revision} 版`}
                        checked={selectedRevision === entry.value.revision}
                        name="rollback-target"
                        onChange={() => {
                          setSelectedRevision(entry.value.revision);
                          setProfileSelection("");
                          setReversesApprovalId("");
                        }}
                        type="radio"
                      />
                      选择第 {entry.value.revision} 版
                    </label>
                  )}
                  <details>
                    <summary>查看版本技术信息</summary>
                    <CopyableText
                      copyLabel={`复制 revision ${entry.value.revision} Artifact ID`}
                      value={entry.value.artifact_id}
                    />
                  </details>
                </div>
              </li>
            );
          })}
        </ol>
      </section>

      {selectedEntry && draftProfiles.data && (
        <RollbackContentComparison
          current={draftProfiles.data.currentArtifact}
          currentLabel={`当前第 ${data.current.revision} 版`}
          diff={draftProfiles.data.contentDiff}
          target={draftProfiles.data.targetArtifact}
          targetLabel={`目标第 ${selectedEntry.value.revision} 版`}
        />
      )}

      <section className="gf-patches__workspace-section" aria-labelledby="rollback-draft-title">
        <header>
          <RotateCcw aria-hidden="true" size={21} />
          <div>
            <h2 id="rollback-draft-title">说明原因并创建回退请求</h2>
            <p>系统会锁定当前版本、所选历史版本和验证策略，避免审批过程中目标悄悄变化。</p>
          </div>
        </header>
        {historicalEntries.length === 0 ? (
          <StatePanel
            description="当前发布位置还没有可回退的历史版本。"
            state="empty"
            title="暂无可回退版本"
          />
        ) : (
          <div className="gf-patches__execution-form">
            <label>
              回退验证方案
              <select onChange={(event) => setProfileSelection(event.target.value)} value={profileSelection}>
                <option value="">请选择验证方案</option>
                {(draftProfiles.data?.profiles ?? []).map((profile) => (
                  <option key={profileKey(profile)} value={profileKey(profile)}>
                    {rollbackProfileBusinessLabel(profile)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              被回滚的审批（可选）
              <select
                disabled={approvals.isPending || approvals.isError}
                onChange={(event) => setReversesApprovalId(event.target.value)}
                value={reversesApprovalId}
              >
                <option value="">不关联审批状态</option>
                {approvalCandidates.map(({ approval }) => (
                  <option key={approval.approval_id} value={approval.approval_id}>
                    {approvalSubjectLabel(approval.subject_kind)} · {compactDateTime(approval.applied_at)}
                  </option>
                ))}
              </select>
            </label>
            <p className="gf-patches__muted">
              {approvalCandidates.length > 0
                ? "仅列出与本次版本变化完整对应、且已应用的审批记录。"
                : "当前可见的审批记录中，没有能与这两个连续版本完整对应的已应用审批。"}
            </p>
            <details className="gf-patches__form-wide">
              <summary>高级：输入审计记录中的审批编号</summary>
              <label>
                被回退的审批编号
                <input
                  onChange={(event) => setReversesApprovalId(event.target.value)}
                  value={reversesApprovalId}
                />
              </label>
            </details>
            <label className="gf-patches__form-wide">
              回退原因
              <textarea onChange={(event) => setReason(event.target.value)} rows={3} value={reason} />
            </label>
          </div>
        )}
        {draftProfiles.isPending && selectedEntry && (
          <StatePanel
            description="正在核对当前版本、目标版本与可用的回退验证方案。"
            state="loading"
            title="正在准备回退验证"
          />
        )}
        {draftProfiles.isError && (
          <StatePanel
            action={<button onClick={() => void draftProfiles.refetch()}>重试读取验证方案</button>}
            description="未能确认所选历史版本的适用内容范围与回退验证方案是否匹配。"
            state="error"
            title="回退验证方案不可用"
          />
        )}
        <div className="gf-patches__action-row">
          <button disabled={!canDraft} onClick={draft} type="button">
            创建回退请求
          </button>
          <span className="gf-patches__muted">
            创建请求不会修改正式内容；当前仍为第 {data.current.revision} 版。
          </span>
        </div>
        {mutation?.pending && (
          <StatePanel description="正在创建不可改写的回退请求记录。" state="loading" title="创建中" />
        )}
        {mutation?.error && (
          <div className="gf-patches__mutation-error">
            {mutation.error instanceof ApiProblemError ? (
              <ProblemPanel problem={mutation.error.problem} />
            ) : (
              <StatePanel
                description={mutation.error.message}
                state="error"
                title="Rollback draft outcome 未确认"
              />
            )}
            {mutation.retry && (
              <button className="gf-secondary-button" onClick={() => void mutation.retry?.()} type="button">
                重试同一 intent
              </button>
            )}
            <button className="gf-secondary-button" onClick={() => void reload()} type="button">
              重新读取 authority
            </button>
          </div>
        )}
        {created && (
          <div className="gf-patches__live-receipt" role="status">
            <StatePanel
              action={
                <a href={`/rollback-requests/${encodeURIComponent(created.artifact.artifact_id)}`}>
                  继续验证回退请求
                </a>
              }
              description="请求已安全保存；下一步仍需完成验证、独立审批并最终应用。"
              state="terminal"
              title="回退请求已创建"
            />
          </div>
        )}
      </section>
    </div>
  );
}
