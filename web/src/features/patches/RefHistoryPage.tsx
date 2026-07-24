import { useQuery } from "@tanstack/react-query";
import { GitCommitHorizontal, History, RotateCcw } from "lucide-react";
import { useMemo, useRef, useState } from "react";

import { createMutationIntent, ReauthenticationRequiredError } from "../../api/csrf";
import type { components } from "../../api/generated/openapi";
import { cursorFromPage } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
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

function profileKey(profile: ExecutionProfile): string {
  return `${profile.profile.profile_id}@${profile.profile.version}`;
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

function shortId(value: string): string {
  return value.length <= 24 ? value : `${value.slice(0, 12)}…${value.slice(-8)}`;
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
      <nav aria-label="Ref history 导航" className="gf-patches__back-nav">
        <a href="/patches">返回 Patch / Diff</a>
        <a href={`/artifacts/${encodeURIComponent(data.current.artifact_id)}`}>检查当前版本技术详情</a>
      </nav>

      <header className="gf-patches__hero">
        <div>
          <p className="gf-patches__kicker">Append-only ref history · explicit rollback target</p>
          <h1>{refName}</h1>
          <p>
            历史 revision 是回滚选择权威；draft、validate 和 approve 都不会移动 ref，只有 apply 会新增
            history。
          </p>
        </div>
        <div className="gf-patches__hero-mark" aria-hidden="true">
          <History size={30} />
          <span>REF</span>
        </div>
      </header>

      <dl className="gf-patches__facts" aria-label="Current ref authority">
        <div>
          <dt>Current</dt>
          <dd>Current · revision {data.current.revision}</dd>
        </div>
        <div className="gf-patches__fact-wide">
          <dt>技术身份</dt>
          <dd>
            <details>
              <summary>查看 current Artifact ID</summary>
              <CopyableText copyLabel="复制 current Artifact ID" value={data.current.artifact_id} />
            </details>
          </dd>
        </div>
        <div>
          <dt>Read snapshot</dt>
          <dd>
            <CopyableText copyLabel="复制 ref read snapshot" value={data.readSnapshotId} />
          </dd>
        </div>
      </dl>

      <section className="gf-patches__workspace-section" aria-labelledby="ref-timeline-title">
        <header>
          <GitCommitHorizontal aria-hidden="true" size={21} />
          <div>
            <h2 id="ref-timeline-title">Ref revision timeline</h2>
            <p>选择 current 之前的一个 exact historical revision；当前 revision 不提供 no-op rollback。</p>
          </div>
        </header>
        <ol className="gf-patches__history-list gf-patches__history-list--selectable">
          {[...data.entries].reverse().map((entry) => {
            const isCurrent = entry.value.revision === data.current.revision;
            return (
              <li key={entry.value.revision}>
                <span>revision {entry.value.revision}</span>
                <div>
                  {isCurrent ? (
                    <strong>Current · revision {entry.value.revision}</strong>
                  ) : (
                    <label>
                      <input
                        aria-label={`回退到 revision ${entry.value.revision}`}
                        checked={selectedRevision === entry.value.revision}
                        name="rollback-target"
                        onChange={() => {
                          setSelectedRevision(entry.value.revision);
                          setProfileSelection("");
                          setReversesApprovalId("");
                        }}
                        type="radio"
                      />
                      回退到 revision {entry.value.revision}
                    </label>
                  )}
                  <details>
                    <summary>查看 exact Artifact 身份</summary>
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
          currentLabel={`Current revision ${data.current.revision}`}
          diff={draftProfiles.data.contentDiff}
          target={draftProfiles.data.targetArtifact}
          targetLabel={`目标 revision ${selectedEntry.value.revision}`}
        />
      )}

      <section className="gf-patches__workspace-section" aria-labelledby="rollback-draft-title">
        <header>
          <RotateCcw aria-hidden="true" size={21} />
          <div>
            <h2 id="rollback-draft-title">Draft a governed rollback</h2>
            <p>创建时冻结 current ref、historical target 与 exact rollback ExecutionProfile。</p>
          </div>
        </header>
        {historicalEntries.length === 0 ? (
          <StatePanel
            description="当前 ref 还没有可回退的历史 revision。"
            state="empty"
            title="没有 rollback target"
          />
        ) : (
          <div className="gf-patches__execution-form">
            <label>
              Rollback policy
              <select onChange={(event) => setProfileSelection(event.target.value)} value={profileSelection}>
                <option value="">选择 active rollback profile</option>
                {(draftProfiles.data?.profiles ?? []).map((profile) => (
                  <option key={profileKey(profile)} value={profileKey(profile)}>
                    {profile.display_name} · {profileKey(profile)}
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
                    {approval.subject_kind} · {approval.applied_at?.slice(0, 10) ?? "已应用"} ·{" "}
                    {shortId(approval.approval_id)}
                  </option>
                ))}
              </select>
            </label>
            <p className="gf-patches__muted">
              {approvalCandidates.length > 0
                ? "仅列出 exact expected_ref 与本次 current ref 连续闭合的 applied Approval。"
                : "当前可见审批目录没有能与这两个连续 revision 精确闭合的 applied Approval。"}
            </p>
            <details className="gf-patches__form-wide">
              <summary>高级：输入审计记录中的 Approval ID</summary>
              <label>
                Reverses approval ID
                <input
                  onChange={(event) => setReversesApprovalId(event.target.value)}
                  value={reversesApprovalId}
                />
              </label>
            </details>
            <label className="gf-patches__form-wide">
              Rollback reason
              <textarea onChange={(event) => setReason(event.target.value)} rows={3} value={reason} />
            </label>
          </div>
        )}
        {draftProfiles.isPending && selectedEntry && (
          <StatePanel
            description="正在读取 current/target domains 与 compatible rollback profiles。"
            state="loading"
            title="正在解析 rollback profile"
          />
        )}
        {draftProfiles.isError && (
          <StatePanel
            action={<button onClick={() => void draftProfiles.refetch()}>重试 profile authority</button>}
            description="未能闭合 selected target 的 domain 与 rollback.validate compatibility。"
            state="error"
            title="Rollback profiles 不可用"
          />
        )}
        <div className="gf-patches__action-row">
          <button disabled={!canDraft} onClick={draft} type="button">
            创建 Rollback request
          </button>
          <span className="gf-patches__muted">
            draft 创建不会移动 ref；当前仍为 revision {data.current.revision}。
          </span>
        </div>
        {mutation?.pending && (
          <StatePanel
            description="正在发布 immutable RollbackRequest Artifact。"
            state="loading"
            title="创建中"
          />
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
                  打开 Rollback request
                </a>
              }
              description="请求已创建为独立 immutable Artifact；下一步仍需 validate、human approve 与 apply。"
              state="terminal"
              title="Rollback request 已创建"
            />
          </div>
        )}
      </section>
    </div>
  );
}
