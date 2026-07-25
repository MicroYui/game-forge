import { useQuery } from "@tanstack/react-query";
import { BadgeCheck, GitCommitHorizontal, RotateCcw, Send, ShieldCheck, Waypoints } from "lucide-react";
import { useMemo, useRef, useState } from "react";

import { createMutationIntent, ReauthenticationRequiredError } from "../../api/csrf";
import type { components } from "../../api/generated/openapi";
import { cursorFromPage } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { compactDateTime, TechnicalDetails } from "../../components/identity";
import { ConfirmDialog, ProblemPanel, StatePanel } from "../../components/ui";
import {
  buildRollbackApplyRequest,
  currentRefFromCompleteHistory,
  type RollbackTargetBinding,
  verifyRollbackApplyResult,
  verifyRollbackWorkflowAuthority,
} from "./authority";
import {
  patchWorkflowApi,
  type ApprovalView,
  type ArtifactKind,
  type ArtifactPage,
  type ArtifactPayloadView,
  type LineagePage,
  type PatchWorkflowApi,
  type RefHistoryEntry,
  type RollbackRequestReadView,
  type RollbackValidationAdmissionRequest,
  type SubjectApprovalBindingView,
  type VersionedResource,
  type WorkflowApplyResult,
} from "./api";
import {
  collectRollbackSnapshotDiff,
  RollbackContentComparison,
  type RollbackSnapshotDiff,
} from "./RollbackContentComparison";
import "./patches.css";

type ExecutionProfile = components["schemas"]["ExecutionProfileViewV1"];
type ProfileKind = ExecutionProfile["profile_kind"];
type RefValue = components["schemas"]["RefValue"];

interface RollbackDetailData {
  approval: VersionedResource<ApprovalView>;
  binding: SubjectApprovalBindingView;
  current: VersionedResource<RollbackRequestReadView>;
  currentArtifact: ArtifactPayloadView;
  contentDiff: RollbackSnapshotDiff | null;
  currentRef: Readonly<RefValue>;
  evidence: ArtifactPayloadView | null;
  failure: ArtifactPayloadView | null;
  history: RefHistoryEntry[];
  impactProfiles: ExecutionProfile[];
  lineage: LineagePage["items"];
  rollbackProfile: ExecutionProfile;
  regressionSuites: ArtifactPage["items"];
  schemaProfiles: ExecutionProfile[];
  target: Readonly<RollbackTargetBinding>;
  targetArtifact: ArtifactPayloadView;
}

interface MutationState {
  error: Error | null;
  label: string;
  pending: boolean;
  retry: (() => Promise<void>) | null;
}

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("Rollback workflow operation failed.");
}

function unknownOutcome(error: Error): boolean {
  return !(error instanceof ApiProblemError) && !(error instanceof ReauthenticationRequiredError);
}

function sameRef(left: RefValue | null | undefined, right: RefValue | null | undefined): boolean {
  return left?.artifact_id === right?.artifact_id && left?.revision === right?.revision;
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

async function collectRefHistory(api: PatchWorkflowApi, refName: string): Promise<RefHistoryEntry[]> {
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
    if (next === null) return entries;
    if (seen.has(next)) throw new Error("Ref history returned a cursor cycle.");
    seen.add(next);
    cursor = next;
  }
  throw new Error("Ref history exceeded its bounded page count.");
}

async function collectArtifacts(api: PatchWorkflowApi, kind: ArtifactKind): Promise<ArtifactPage["items"]> {
  const artifacts: ArtifactPage["items"] = [];
  const artifactIds = new Set<string>();
  const cursors = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listArtifacts(kind, cursor);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new Error(`${kind} catalog changed read snapshot.`);
    }
    readSnapshotId = page.read_snapshot_id;
    for (const artifact of page.items) {
      if (artifact.kind !== kind) throw new Error(`${kind} catalog returned the wrong Artifact kind.`);
      if (artifactIds.has(artifact.artifact_id)) throw new Error(`${kind} catalog returned a duplicate.`);
      artifactIds.add(artifact.artifact_id);
      artifacts.push(artifact);
    }
    const next = cursorFromPage(page);
    if (next === null) return artifacts;
    if (cursors.has(next)) throw new Error(`${kind} catalog returned a cursor cycle.`);
    cursors.add(next);
    cursor = next;
  }
  throw new Error(`${kind} catalog exceeded its bounded page count.`);
}

function workflowStatusLabel(value: string): string {
  const labels: Record<string, string> = {
    applied: "已完成回退",
    approved: "已批准，待应用",
    auto_apply_eligible: "验证通过，可应用",
    changes_requested: "需要修改后重新提交",
    draft: "草案，待验证",
    pending_approval: "等待审批",
    rejected: "审批未通过",
    rolled_back: "已被后续回退",
    submitted: "等待审批",
    superseded: "已由新版本替代",
    validating: "正在验证",
    validation_failed: "验证未通过",
    validated: "验证通过，待审批",
  };
  return labels[value] ?? "处理中";
}

function artifactKindLabel(value: string): string {
  const labels: Record<string, string> = {
    constraint_snapshot: "约束版本",
    ir_snapshot: "设计内容版本",
    patch: "修改草案",
    rollback_request: "回退请求",
  };
  return labels[value] ?? "内容版本";
}

async function collectLineage(api: PatchWorkflowApi, artifactId: string): Promise<LineagePage["items"]> {
  const entries: LineagePage["items"] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listLineage(artifactId, cursor);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new Error("Target content lineage changed read snapshot.");
    }
    readSnapshotId = page.read_snapshot_id;
    entries.push(...page.items);
    const next = cursorFromPage(page);
    if (next === null) return entries;
    if (seen.has(next)) throw new Error("Target content lineage returned a cursor cycle.");
    seen.add(next);
    cursor = next;
  }
  throw new Error("Target content lineage exceeded its bounded page count.");
}

async function collectProfiles(api: PatchWorkflowApi, kind: ProfileKind): Promise<ExecutionProfile[]> {
  const profiles: ExecutionProfile[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listExecutionProfiles(
      { limit: 100, profile_kind: kind, status: "active" },
      cursor,
    );
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new Error(`${kind} profile catalog changed read snapshot.`);
    }
    readSnapshotId = page.read_snapshot_id;
    for (const profile of page.items) {
      if (profile.profile_kind !== kind || profile.status !== "active") {
        throw new Error(`Profile catalog returned a non-active ${kind} item.`);
      }
      profiles.push(profile);
    }
    const next = cursorFromPage(page);
    if (next === null) return profiles;
    if (seen.has(next)) throw new Error(`${kind} profile catalog returned a cursor cycle.`);
    seen.add(next);
    cursor = next;
  }
  throw new Error(`${kind} profile catalog exceeded its bounded page count.`);
}

async function loadRollbackDetail(api: PatchWorkflowApi, artifactId: string): Promise<RollbackDetailData> {
  const current = await api.getRollbackRequest(artifactId);
  const binding = await api.getApprovalBinding(artifactId);
  const approval = await api.getApproval(binding.approval_id);
  const item = approval.value.approval;
  const request = current.value.request;
  const [
    targetArtifact,
    currentArtifact,
    history,
    lineage,
    rollbackProfile,
    schemaProfiles,
    impactProfiles,
    evidence,
    failure,
    regressionSuites,
  ] = await Promise.all([
    api.getArtifact(request.target_artifact_id),
    api.getArtifact(request.expected_current_ref.artifact_id),
    collectRefHistory(api, request.ref_name),
    collectLineage(api, request.target_artifact_id),
    api.getExecutionProfile(
      request.rollback_profile_binding.profile.profile_id,
      request.rollback_profile_binding.profile.version,
    ),
    collectProfiles(api, "schema_compatibility"),
    collectProfiles(api, "impact_analysis"),
    item.evidence_set_artifact_id ? api.getArtifact(item.evidence_set_artifact_id) : Promise.resolve(null),
    item.last_validation_failure_artifact_id
      ? api.getArtifact(item.last_validation_failure_artifact_id)
      : Promise.resolve(null),
    collectArtifacts(api, "regression_suite"),
  ]);
  if (
    targetArtifact.artifact.artifact_id !== request.target_artifact_id ||
    currentArtifact.artifact.artifact_id !== request.expected_current_ref.artifact_id
  ) {
    throw new Error("Rollback content comparison Artifact identity is inconsistent.");
  }
  const contentDiff = await collectRollbackSnapshotDiff(api, currentArtifact, targetArtifact);
  const target = verifyRollbackWorkflowAuthority({
    approval: approval.value,
    binding,
    history,
    historyNextCursor: null,
    subject: current.value,
    targetArtifact,
  });
  const requiredDomainIds = item.domain_scope.domain_ids;
  if (
    rollbackProfile.profile.profile_id !== request.rollback_profile_binding.profile.profile_id ||
    rollbackProfile.profile.version !== request.rollback_profile_binding.profile.version ||
    rollbackProfile.profile_kind !== "rollback" ||
    rollbackProfile.profile_payload_hash !== request.rollback_profile_binding.profile_payload_hash ||
    !supportsRunKind(rollbackProfile, "rollback.validate") ||
    !profileCoversDomains(rollbackProfile, requiredDomainIds)
  ) {
    throw new Error("Frozen rollback ExecutionProfile does not match its retained binding.");
  }
  const currentRef = currentRefFromCompleteHistory(target.ref_name, history, null);
  return {
    approval,
    binding,
    contentDiff,
    current,
    currentArtifact,
    currentRef,
    evidence,
    failure,
    history,
    impactProfiles: impactProfiles.filter(
      (profile) =>
        supportsRunKind(profile, "rollback.validate") && profileCoversDomains(profile, requiredDomainIds),
    ),
    lineage,
    rollbackProfile,
    regressionSuites,
    schemaProfiles: schemaProfiles.filter(
      (profile) =>
        supportsRunKind(profile, "rollback.validate") && profileCoversDomains(profile, requiredDomainIds),
    ),
    target,
    targetArtifact,
  };
}

function authorityProjection(data: RollbackDetailData) {
  return {
    approval: data.approval.value,
    binding: data.binding,
    history: data.history,
    historyNextCursor: null,
    subject: data.current.value,
    targetArtifact: data.targetArtifact,
  } as const;
}

function EvidenceLedger({ data }: { data: RollbackDetailData }) {
  const item = data.approval.value.approval;
  return (
    <div className="gf-patches__evidence-ledger">
      <h3>验证依据</h3>
      <p className="gf-patches__muted">
        系统会保留版本历史、兼容性、影响分析和回归测试结果，审批人可逐项核对。
      </p>
      <div className="gf-patches__evidence-list">
        {item.evidence_set_artifact_id ? (
          <a href={`/artifacts/${encodeURIComponent(item.evidence_set_artifact_id)}`}>查看完整验证依据</a>
        ) : (
          <p>尚无 EvidenceSet；rollback validation 尚未形成可审批 verdict。</p>
        )}
        {item.last_validation_failure_artifact_id && (
          <a href={`/artifacts/${encodeURIComponent(item.last_validation_failure_artifact_id)}`}>
            查看最近一次验证失败记录
          </a>
        )}
        {item.regression_evidence_artifact_ids.map((artifactId, index) => (
          <a href={`/artifacts/${encodeURIComponent(artifactId)}`} key={artifactId}>
            查看回归与影响分析依据 {index + 1}
          </a>
        ))}
        {data.evidence && data.evidence.artifact.artifact_id !== item.evidence_set_artifact_id && (
          <p role="alert">Evidence read identity mismatch.</p>
        )}
        {data.failure && data.failure.artifact.artifact_id !== item.last_validation_failure_artifact_id && (
          <p role="alert">Failure read identity mismatch.</p>
        )}
      </div>
    </div>
  );
}

export function RollbackDetailPage({
  api = patchWorkflowApi,
  artifactId,
}: {
  api?: PatchWorkflowApi;
  artifactId: string;
}) {
  const workflow = useQuery({
    queryFn: () => loadRollbackDetail(api, artifactId),
    queryKey: ["rollback-detail", artifactId],
    retry: false,
  });
  const mutationLock = useRef(false);
  const [schemaProfileKey, setSchemaProfileKey] = useState("");
  const [impactKeys, setImpactKeys] = useState<Set<string>>(new Set());
  const [regressionSuiteIds, setRegressionSuiteIds] = useState<Set<string>>(new Set());
  const [regressionSearch, setRegressionSearch] = useState("");
  const [seed, setSeed] = useState("1");
  const [acceptedRunId, setAcceptedRunId] = useState<string | null>(null);
  const [mutation, setMutation] = useState<MutationState | null>(null);
  const [confirmApply, setConfirmApply] = useState(false);
  const [applyResult, setApplyResult] = useState<WorkflowApplyResult | null>(null);
  const applyReturnFocusRef = useRef<HTMLHeadingElement>(null);

  const selectedSchema = workflow.data?.schemaProfiles.find(
    (profile) => profileKey(profile) === schemaProfileKey,
  );
  const selectedImpact = useMemo(
    () => workflow.data?.impactProfiles.filter((profile) => impactKeys.has(profileKey(profile))) ?? [],
    [impactKeys, workflow.data?.impactProfiles],
  );
  const regressionIds = useMemo(() => [...regressionSuiteIds].sort(), [regressionSuiteIds]);
  const visibleRegressionSuites = useMemo(() => {
    const query = regressionSearch.trim().toLocaleLowerCase();
    if (!query) return workflow.data?.regressionSuites ?? [];
    return (workflow.data?.regressionSuites ?? []).filter((artifact) =>
      [artifact.payload_schema_id, artifact.artifact_id, artifact.created_at ?? ""]
        .join(" ")
        .toLocaleLowerCase()
        .includes(query),
    );
  }, [regressionSearch, workflow.data?.regressionSuites]);
  const parsedSeed = Number(seed);
  const seedIsValid = seed.trim() !== "" && Number.isSafeInteger(parsedSeed) && parsedSeed >= 0;
  const seedRequired =
    (workflow.data?.rollbackProfile.stochastic ?? false) ||
    (selectedSchema?.stochastic ?? false) ||
    selectedImpact.some((profile) => profile.stochastic) ||
    regressionIds.length > 0;

  async function reload() {
    const refreshed = await workflow.refetch();
    if (refreshed.isSuccess) {
      setMutation(null);
      setAcceptedRunId(null);
      setApplyResult(null);
      setConfirmApply(false);
    }
  }

  function runFrozen<T>(
    label: string,
    send: (intent: ReturnType<typeof createMutationIntent>) => Promise<T>,
    after: (value: T) => Promise<void> | void,
  ) {
    const intent = createMutationIntent();
    const execute = async () => {
      if (mutationLock.current) return;
      mutationLock.current = true;
      setMutation({ error: null, label, pending: true, retry: null });
      try {
        const value = await send(intent);
        await after(value);
        setMutation(null);
      } catch (error) {
        const normalized = normalizedError(error);
        setMutation({
          error: normalized,
          label,
          pending: false,
          retry: unknownOutcome(normalized) ? execute : null,
        });
      } finally {
        mutationLock.current = false;
      }
    };
    void execute();
  }

  if (workflow.isPending) {
    return (
      <div className="gf-page gf-patches">
        <StatePanel
          description="正在核对回退请求、审批、正式版本历史和目标内容。"
          headingLevel={1}
          state="loading"
          title="正在读取回退流程"
        />
      </div>
    );
  }

  if (workflow.isError) {
    return (
      <div className="gf-page gf-patches">
        {workflow.error instanceof ApiProblemError ? (
          <ProblemPanel problem={workflow.error.problem} />
        ) : (
          <StatePanel
            action={<button onClick={() => void workflow.refetch()}>重试</button>}
            description="回退请求、审批、版本历史或目标内容未能完整核对；为避免误操作，页面已停止继续。"
            headingLevel={1}
            state="error"
            title="回退信息暂不可用"
          />
        )}
      </div>
    );
  }

  const data = workflow.data;
  const item = data.approval.value.approval;
  const refDrifted = !sameRef(data.current.value.request.expected_current_ref, data.currentRef);
  const actionsLocked = mutation !== null || !data.binding.is_current_head;
  const canValidate =
    item.status === "draft" &&
    data.rollbackProfile.status === "active" &&
    !refDrifted &&
    selectedSchema !== undefined &&
    (!seedRequired || seedIsValid) &&
    !actionsLocked;
  const canSubmit =
    item.status === "validated" && item.evidence_set_artifact_id != null && !refDrifted && !actionsLocked;
  const canApply = item.status === "approved" && !refDrifted && !actionsLocked;

  function validate() {
    if (!canValidate || !selectedSchema) return;
    const request: RollbackValidationAdmissionRequest = {
      approval_id: data.binding.approval_id,
      expected_current_ref: data.current.value.request.expected_current_ref,
      expected_subject_head_revision: data.binding.subject_head_revision,
      expected_workflow_revision: data.binding.workflow_revision,
      impact_profiles: selectedImpact.map((profile) => profile.profile),
      ref_name: data.target.ref_name,
      regression_suite_artifact_ids: regressionIds,
      request_schema_version: "rollback-validation-admission-request@1",
      rollback_profile: data.current.value.request.rollback_profile_binding.profile,
      schema_compatibility_policy: selectedSchema.profile,
      seed: seedRequired ? parsedSeed : null,
      subject_digest: data.binding.subject_digest,
      target_artifact_id: data.target.target_artifact_id,
      target_history_revision: data.current.value.request.target_history_revision,
    };
    runFrozen(
      "Rollback validation",
      (intent) => api.validateRollback(data.current, request, intent),
      async (accepted) => {
        setAcceptedRunId(accepted.run_id);
        const refreshed = await workflow.refetch();
        if (!refreshed.isSuccess || !refreshed.data) {
          throw new Error("Validated rollback authority could not be reloaded.");
        }
      },
    );
  }

  function submit() {
    if (!canSubmit) return;
    const request: components["schemas"]["SubmitForApprovalRequestV1"] = {
      approval_id: data.binding.approval_id,
      expected_workflow_revision: data.binding.workflow_revision,
      request_schema_version: "submit-for-approval-request@1",
    };
    runFrozen(
      "Submit rollback for approval",
      (intent) => api.submitRollbackForApproval(data.current, request, intent),
      async () => {
        const refreshed = await workflow.refetch();
        if (!refreshed.isSuccess || !refreshed.data) {
          throw new Error("Submitted rollback authority could not be reloaded.");
        }
      },
    );
  }

  function apply() {
    if (!canApply) return;
    const request = buildRollbackApplyRequest({
      ...authorityProjection(data),
    });
    setConfirmApply(false);
    runFrozen(
      "Apply rollback",
      (intent) => api.applyRollback(data.current, request, intent),
      async (result) => {
        const refreshed = await workflow.refetch();
        if (!refreshed.isSuccess || !refreshed.data) {
          throw new Error("Applied rollback authority could not be reloaded.");
        }
        verifyRollbackApplyResult({
          after: authorityProjection(refreshed.data),
          before: authorityProjection(data),
          result,
        });
        setApplyResult(result);
      },
    );
  }

  return (
    <div className="gf-page gf-patches gf-rollback-detail" data-layout="editorial-rollback-detail">
      <nav aria-label="Rollback 导航" className="gf-patches__back-nav">
        <a href="/patches">返回修改工作台</a>
        <a href={`/refs/${encodeURIComponent(data.target.ref_name)}/history`}>查看正式版本历史</a>
        <a href={`/artifacts/${encodeURIComponent(data.current.value.artifact.artifact_id)}`}>
          查看回退请求完整记录
        </a>
        <a href={`/artifacts/${encodeURIComponent(data.target.target_artifact_id)}`}>查看目标版本完整记录</a>
      </nav>

      <header className="gf-patches__hero gf-patches__hero--detail">
        <div>
          <p className="gf-patches__kicker">安全版本回退</p>
          <h1>回退正式内容</h1>
          <p>{data.current.value.request.reason}</p>
        </div>
        <span className="gf-patches__status-mark">
          <RotateCcw aria-hidden="true" size={17} />
          {workflowStatusLabel(item.status)}
        </span>
      </header>

      <dl className="gf-patches__facts" aria-label="回退请求概况">
        <div>
          <dt>当前正式版本</dt>
          <dd>第 {data.currentRef.revision} 版</dd>
        </div>
        <div>
          <dt>准备恢复</dt>
          <dd>历史第 {data.current.value.request.target_history_revision} 版</dd>
        </div>
        <div>
          <dt>独立审批</dt>
          <dd>
            <a href={`/approvals/${encodeURIComponent(data.binding.approval_id)}`}>查看审批进度</a>
          </dd>
        </div>
      </dl>
      <TechnicalDetails
        items={[
          { label: "Rollback Artifact ID", value: data.current.value.artifact.artifact_id },
          { label: "ETag", value: data.current.etag },
          { label: "Current ref Artifact ID", value: data.currentRef.artifact_id },
          { label: "Approval ID", value: data.binding.approval_id },
          { label: "Ref name", value: data.target.ref_name },
          { label: "Subject head revision", value: String(data.binding.subject_head_revision) },
          { label: "Workflow revision", value: String(data.binding.workflow_revision) },
        ]}
        summary="查看回退请求技术信息"
      />

      {refDrifted && item.status !== "applied" && (
        <StatePanel
          action={<button onClick={() => void reload()}>重新读取 authority</button>}
          description="live ref 已不再等于 draft 冻结的 expected_current_ref；validate、submit 与 apply 均已禁用。"
          state="error"
          title="Rollback request 已 stale"
        />
      )}

      <RollbackContentComparison
        current={data.currentArtifact}
        currentLabel={`当前第 ${data.current.value.request.expected_current_ref.revision} 版`}
        diff={data.contentDiff}
        target={data.targetArtifact}
        targetLabel={`目标第 ${data.current.value.request.target_history_revision} 版`}
      />

      <section className="gf-patches__workspace-section" aria-labelledby="rollback-target-title">
        <header>
          <GitCommitHorizontal aria-hidden="true" size={21} />
          <div>
            <h2 id="rollback-target-title">已锁定的回退目标</h2>
            <p>后续验证和审批始终使用这里锁定的当前版本与历史目标，避免目标在流程中发生变化。</p>
          </div>
        </header>
        <dl className="gf-patches__target-ledger">
          <div>
            <dt>发起时的正式版本</dt>
            <dd>第 {data.current.value.request.expected_current_ref.revision} 版</dd>
          </div>
          <div>
            <dt>要恢复的历史版本</dt>
            <dd>第 {data.current.value.request.target_history_revision} 版</dd>
          </div>
          <div>
            <dt>验证方案</dt>
            <dd>{data.rollbackProfile.display_name}</dd>
          </div>
          <div>
            <dt>关联原审批</dt>
            <dd>{data.current.value.request.reverses_approval_id ? "已关联" : "未关联"}</dd>
          </div>
          <div>
            <dt>目标内容类型</dt>
            <dd>{artifactKindLabel(data.target.target_artifact_kind)}</dd>
          </div>
          <div className="gf-patches__fact-wide">
            <dt>技术信息</dt>
            <dd>
              <TechnicalDetails
                items={[
                  { label: "Target digest", value: data.target.target_digest },
                  {
                    label: "Rollback profile",
                    value: `${data.current.value.request.rollback_profile_binding.profile.profile_id}@${data.current.value.request.rollback_profile_binding.profile.version}`,
                  },
                  {
                    label: "Profile catalog version",
                    value: String(data.current.value.request.rollback_profile_binding.catalog_version),
                  },
                  {
                    label: "Reverses approval ID",
                    value: data.current.value.request.reverses_approval_id ?? "未绑定",
                  },
                  { label: "Target snapshot ID", value: data.target.target_snapshot_id ?? "不适用" },
                  {
                    label: "Frozen current Artifact ID",
                    value: data.current.value.request.expected_current_ref.artifact_id,
                  },
                  { label: "Frozen target Artifact ID", value: data.target.target_artifact_id },
                ]}
                summary="查看目标绑定技术信息"
              />
            </dd>
          </div>
        </dl>
      </section>

      <section className="gf-patches__workspace-section" aria-labelledby="rollback-history-title">
        <header>
          <Waypoints aria-hidden="true" size={21} />
          <div>
            <h2 id="rollback-history-title">正式版本历史</h2>
            <p>最终应用前不会新增版本；应用成功后，恢复的内容会成为一个新的正式版本。</p>
          </div>
        </header>
        <ol className="gf-patches__history-list">
          {[...data.history].reverse().map((entry) => (
            <li key={entry.value.revision}>
              <span>
                {entry.value.revision === data.currentRef.revision ? "当前" : "历史"}第 {entry.value.revision}{" "}
                版
              </span>
              <TechnicalDetails
                items={[{ label: "Artifact ID", value: entry.value.artifact_id }]}
                summary="查看版本技术信息"
              />
            </li>
          ))}
        </ol>
      </section>

      <section className="gf-patches__workspace-section" aria-labelledby="rollback-validation-title">
        <header>
          <ShieldCheck aria-hidden="true" size={21} />
          <div>
            <h2 id="rollback-validation-title">验证回退是否安全</h2>
            <p>系统会检查版本历史、结构兼容性、影响范围和所选回归套件，并保存可追溯的验证依据。</p>
          </div>
        </header>
        <div className="gf-patches__execution-form">
          <label>
            回退验证方案
            <input disabled value={data.rollbackProfile.display_name} />
          </label>
          <label>
            结构兼容性检查方案
            <select onChange={(event) => setSchemaProfileKey(event.target.value)} value={schemaProfileKey}>
              <option value="">请选择兼容性检查方案</option>
              {data.schemaProfiles.map((profile) => (
                <option key={profileKey(profile)} value={profileKey(profile)}>
                  {profile.display_name}
                </option>
              ))}
            </select>
          </label>
          <fieldset className="gf-patches__checklist">
            <legend>影响分析方案</legend>
            {data.impactProfiles.length === 0 ? (
              <span className="gf-patches__muted">暂无可用的影响分析方案</span>
            ) : (
              data.impactProfiles.map((profile) => {
                const key = profileKey(profile);
                return (
                  <label key={key}>
                    <input
                      checked={impactKeys.has(key)}
                      onChange={(event) => {
                        const next = new Set(impactKeys);
                        if (event.target.checked) next.add(key);
                        else next.delete(key);
                        setImpactKeys(next);
                      }}
                      type="checkbox"
                    />
                    {profile.display_name}
                  </label>
                );
              })
            )}
          </fieldset>
          <label>
            随机种子
            <input min={0} onChange={(event) => setSeed(event.target.value)} type="number" value={seed} />
          </label>
          <p className="gf-patches__muted">
            {seedRequired ? "所选检查包含随机或回归过程，需要固定种子以便复现。" : "当前检查无需随机种子。"}
          </p>
          <fieldset className="gf-patches__checklist gf-patches__form-wide">
            <legend>回归测试套件（可选）</legend>
            <label>
              搜索回归套件
              <input
                onChange={(event) => setRegressionSearch(event.target.value)}
                type="search"
                value={regressionSearch}
              />
            </label>
            {data.regressionSuites.length === 0 ? (
              <span className="gf-patches__muted">当前没有可选的回归测试套件。</span>
            ) : visibleRegressionSuites.length === 0 ? (
              <span className="gf-patches__muted">没有匹配的回归套件。</span>
            ) : (
              visibleRegressionSuites.map((artifact, index) => (
                <label key={artifact.artifact_id}>
                  <input
                    checked={regressionSuiteIds.has(artifact.artifact_id)}
                    onChange={(event) => {
                      const next = new Set(regressionSuiteIds);
                      if (event.target.checked) next.add(artifact.artifact_id);
                      else next.delete(artifact.artifact_id);
                      setRegressionSuiteIds(next);
                    }}
                    type="checkbox"
                  />
                  回归套件 {index + 1} · {compactDateTime(artifact.created_at)}
                </label>
              ))
            )}
          </fieldset>
        </div>
        <div className="gf-patches__action-row">
          <button disabled={!canValidate} onClick={validate} type="button">
            开始安全验证
          </button>
          {acceptedRunId && (
            <div className="gf-patches__live-receipt" role="status">
              <a href={`/runs/${encodeURIComponent(acceptedRunId)}`}>查看本次验证进度</a>
            </div>
          )}
          {item.active_validation_run_id && (
            <a href={`/runs/${encodeURIComponent(item.active_validation_run_id)}`}>查看正在进行的验证</a>
          )}
        </div>
        <EvidenceLedger data={data} />
      </section>

      <section className="gf-patches__workspace-section" aria-labelledby="rollback-approval-title">
        <header>
          <Send aria-hidden="true" size={21} />
          <div>
            <h2 id="rollback-approval-title" ref={applyReturnFocusRef} tabIndex={-1}>
              独立审批并应用
            </h2>
            <p>版本回退不会自动应用，必须由另一位有权限的负责人独立审批。</p>
          </div>
        </header>
        <div className="gf-patches__approval-actions">
          <button disabled={!canSubmit} onClick={submit} type="button">
            提交独立人工审批
          </button>
          <a href={`/approvals/${encodeURIComponent(data.binding.approval_id)}`}>查看审批详情</a>
          <button disabled={!canApply} onClick={() => setConfirmApply(true)} type="button">
            应用已批准的回退
          </button>
        </div>
        {applyResult && (
          <div className="gf-patches__live-receipt" role="status">
            <StatePanel
              description={`历史内容已恢复，并成为新的正式第 ${applyResult.ref_value.revision} 版。`}
              state="terminal"
              title="版本回退已完成"
            />
            <TechnicalDetails
              items={[
                { label: "Artifact ID", value: applyResult.ref_value.artifact_id },
                ...(applyResult.ref_transition_id
                  ? [{ label: "Ref transition ID", value: applyResult.ref_transition_id }]
                  : []),
              ]}
              summary="查看应用结果技术信息"
            />
          </div>
        )}
      </section>

      {mutation?.pending && (
        <StatePanel description={`正在执行 ${mutation.label}。`} state="loading" title="工作流命令进行中" />
      )}
      {mutation?.error && (
        <div className="gf-patches__mutation-error">
          {mutation.error instanceof ApiProblemError ? (
            <ProblemPanel problem={mutation.error.problem} />
          ) : (
            <StatePanel
              description={mutation.error.message}
              state="error"
              title={`${mutation.label} outcome 未确认`}
            />
          )}
          <div className="gf-patches__action-row">
            {mutation.retry && (
              <button className="gf-secondary-button" onClick={() => void mutation.retry?.()} type="button">
                重试同一 intent
              </button>
            )}
            <button className="gf-secondary-button" onClick={() => void reload()} type="button">
              重新读取 authority
            </button>
          </div>
        </div>
      )}

      <section className="gf-patches__workspace-section" aria-labelledby="target-lineage-title">
        <header>
          <BadgeCheck aria-hidden="true" size={21} />
          <div>
            <h2 id="target-lineage-title">目标版本的来源</h2>
            <p>这里展示所选历史内容的来源链，方便追溯它由哪些更早版本演变而来。</p>
          </div>
        </header>
        <p className="gf-patches__principle">回退只会新增一条正式版本历史，不会改写原有内容的来源关系。</p>
        {data.lineage.length === 0 ? (
          <p className="gf-patches__muted">所选历史版本没有更早的可见来源。</p>
        ) : (
          <ul className="gf-patches__history-list">
            {data.lineage.map((entry, index) => (
              <li key={`${entry.depth}:${entry.artifact.artifact_id}`}>
                <span>来源层级 {entry.depth}</span>
                <a href={`/artifacts/${encodeURIComponent(entry.artifact.artifact_id)}`}>
                  查看来源版本 {index + 1}
                </a>
                <TechnicalDetails
                  items={[{ label: "Artifact ID", value: entry.artifact.artifact_id }]}
                  summary="查看来源技术信息"
                />
              </li>
            ))}
          </ul>
        )}
      </section>

      <ConfirmDialog
        confirmLabel="确认应用回退"
        description={`这会把历史第 ${data.current.value.request.target_history_revision} 版的内容恢复为新的正式版本。系统会在提交时再次确认当前版本没有变化。`}
        onCancel={() => setConfirmApply(false)}
        onConfirm={apply}
        open={confirmApply}
        returnFocusRef={applyReturnFocusRef}
        title="确认应用已批准的版本回退？"
      />
    </div>
  );
}
