import { useQuery } from "@tanstack/react-query";
import { BadgeCheck, GitCommitHorizontal, RotateCcw, Send, ShieldCheck, Waypoints } from "lucide-react";
import { useMemo, useRef, useState } from "react";

import { createMutationIntent, ReauthenticationRequiredError } from "../../api/csrf";
import type { components } from "../../api/generated/openapi";
import { cursorFromPage } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { CopyableText } from "../../components/tables";
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

function shortId(value: string): string {
  return value.length <= 24 ? value : `${value.slice(0, 12)}…${value.slice(-8)}`;
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
      <h3>Workflow evidence Artifact ledger</h3>
      <p className="gf-patches__muted">
        中性索引：history、schema、impact 与 regression 证据只有在解析 exact requirement 后才能分类。
      </p>
      <div className="gf-patches__evidence-list">
        {item.evidence_set_artifact_id ? (
          <a href={`/artifacts/${encodeURIComponent(item.evidence_set_artifact_id)}`}>
            EvidenceSet · {item.evidence_set_artifact_id}
          </a>
        ) : (
          <p>尚无 EvidenceSet；rollback validation 尚未形成可审批 verdict。</p>
        )}
        {item.last_validation_failure_artifact_id && (
          <a href={`/artifacts/${encodeURIComponent(item.last_validation_failure_artifact_id)}`}>
            Validation failure · {item.last_validation_failure_artifact_id}
          </a>
        )}
        {item.regression_evidence_artifact_ids.map((artifactId) => (
          <a href={`/artifacts/${encodeURIComponent(artifactId)}`} key={artifactId}>
            Regression / impact companion · {artifactId}
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
          description="正在闭合 RollbackRequest、Approval、target binding、ref history 与 target content lineage。"
          headingLevel={1}
          state="loading"
          title="正在读取 Rollback workflow"
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
            description="Rollback subject、binding、Approval、history 或 lineage 未能形成 exact authority。"
            headingLevel={1}
            state="error"
            title="Rollback authority 不可用"
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
        <a href="/patches">返回 Patch / Diff</a>
        <a href={`/refs/${encodeURIComponent(data.target.ref_name)}/history`}>Ref history</a>
        <a href={`/artifacts/${encodeURIComponent(data.current.value.artifact.artifact_id)}`}>
          Request Artifact
        </a>
        <a href={`/artifacts/${encodeURIComponent(data.target.target_artifact_id)}`}>Target Artifact</a>
      </nav>

      <header className="gf-patches__hero gf-patches__hero--detail">
        <div>
          <p className="gf-patches__kicker">Rollback request · governed ref transition</p>
          <h1>Rollback {data.target.ref_name}</h1>
          <p>{data.current.value.request.reason}</p>
        </div>
        <span className="gf-patches__status-mark">
          <RotateCcw aria-hidden="true" size={17} />
          {item.status}
        </span>
      </header>

      <dl className="gf-patches__facts" aria-label="Rollback exact workflow authority">
        <div className="gf-patches__fact-wide">
          <dt>Rollback Artifact</dt>
          <dd>
            <CopyableText
              copyLabel="复制 Rollback Artifact ID"
              value={data.current.value.artifact.artifact_id}
            />
          </dd>
        </div>
        <div>
          <dt>ETag</dt>
          <dd>
            <CopyableText copyLabel="复制 Rollback ETag" value={data.current.etag} />
          </dd>
        </div>
        <div>
          <dt>Workflow</dt>
          <dd>
            head {data.binding.subject_head_revision} · workflow {data.binding.workflow_revision}
          </dd>
        </div>
        <div>
          <dt>Current ref</dt>
          <dd>Current · revision {data.currentRef.revision}</dd>
        </div>
        <div className="gf-patches__fact-wide">
          <dt>Current 技术身份</dt>
          <dd>
            <details>
              <summary>查看 current ref Artifact ID</summary>
              <CopyableText copyLabel="复制 current ref Artifact ID" value={data.currentRef.artifact_id} />
            </details>
          </dd>
        </div>
        <div>
          <dt>Approval</dt>
          <dd>
            <a href={`/approvals/${encodeURIComponent(data.binding.approval_id)}`}>
              {data.binding.approval_id}
            </a>
          </dd>
        </div>
      </dl>

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
        currentLabel={`Current revision ${data.current.value.request.expected_current_ref.revision}`}
        diff={data.contentDiff}
        target={data.targetArtifact}
        targetLabel={`目标 revision ${data.current.value.request.target_history_revision}`}
      />

      <section className="gf-patches__workspace-section" aria-labelledby="rollback-target-title">
        <header>
          <GitCommitHorizontal aria-hidden="true" size={21} />
          <div>
            <h2 id="rollback-target-title">Frozen ref transition</h2>
            <p>所有后续命令复制 retained target binding；页面不从 current ref 重新推导目标。</p>
          </div>
        </header>
        <dl className="gf-patches__target-ledger">
          <div>
            <dt>Expected current ref</dt>
            <dd>revision {data.current.value.request.expected_current_ref.revision}</dd>
          </div>
          <div>
            <dt>Historical target</dt>
            <dd>history revision {data.current.value.request.target_history_revision}</dd>
          </div>
          <div>
            <dt>Target digest</dt>
            <dd>
              <CopyableText copyLabel="复制 rollback target digest" value={data.target.target_digest} />
            </dd>
          </div>
          <div>
            <dt>Frozen rollback profile</dt>
            <dd>
              {data.current.value.request.rollback_profile_binding.profile.profile_id}@
              {data.current.value.request.rollback_profile_binding.profile.version} · catalog{" "}
              {data.current.value.request.rollback_profile_binding.catalog_version} ·{" "}
              {data.rollbackProfile.status}
            </dd>
          </div>
          <div>
            <dt>Reverses approval</dt>
            <dd>{data.current.value.request.reverses_approval_id ?? "未绑定"}</dd>
          </div>
          <div>
            <dt>Target kind / snapshot</dt>
            <dd>
              {data.target.target_artifact_kind} · {data.target.target_snapshot_id ?? "not applicable"}
            </dd>
          </div>
          <div className="gf-patches__fact-wide">
            <dt>Exact transition identity</dt>
            <dd>
              <details>
                <summary>查看 current 与 target Artifact ID</summary>
                <CopyableText
                  copyLabel="复制 frozen current Artifact ID"
                  value={data.current.value.request.expected_current_ref.artifact_id}
                />
                <CopyableText
                  copyLabel="复制 frozen target Artifact ID"
                  value={data.target.target_artifact_id}
                />
              </details>
            </dd>
          </div>
        </dl>
      </section>

      <section className="gf-patches__workspace-section" aria-labelledby="rollback-history-title">
        <header>
          <Waypoints aria-hidden="true" size={21} />
          <div>
            <h2 id="rollback-history-title">Ref history</h2>
            <p>apply 前不新增 revision；apply 成功后 current 指向历史 Artifact，但 revision 继续单调递增。</p>
          </div>
        </header>
        <ol className="gf-patches__history-list">
          {[...data.history].reverse().map((entry) => (
            <li key={entry.value.revision}>
              <span>
                {entry.value.revision === data.currentRef.revision ? "Current" : "revision"}{" "}
                {entry.value.revision}
              </span>
              <details>
                <summary>查看 exact Artifact 身份</summary>
                <CopyableText
                  copyLabel={`复制 history revision ${entry.value.revision} Artifact ID`}
                  value={entry.value.artifact_id}
                />
              </details>
            </li>
          ))}
        </ol>
      </section>

      <section className="gf-patches__workspace-section" aria-labelledby="rollback-validation-title">
        <header>
          <ShieldCheck aria-hidden="true" size={21} />
          <div>
            <h2 id="rollback-validation-title">Rollback validation</h2>
            <p>
              history、schema compatibility、impact 与 regression 由 exact validation Run 形成 EvidenceSet。
            </p>
          </div>
        </header>
        <div className="gf-patches__execution-form">
          <label>
            Frozen rollback policy
            <input
              disabled
              value={`${data.current.value.request.rollback_profile_binding.profile.profile_id}@${data.current.value.request.rollback_profile_binding.profile.version}`}
            />
          </label>
          <label>
            Schema compatibility policy
            <select onChange={(event) => setSchemaProfileKey(event.target.value)} value={schemaProfileKey}>
              <option value="">选择 active schema compatibility profile</option>
              {data.schemaProfiles.map((profile) => (
                <option key={profileKey(profile)} value={profileKey(profile)}>
                  {profile.display_name} · {profileKey(profile)}
                </option>
              ))}
            </select>
          </label>
          <fieldset className="gf-patches__checklist">
            <legend>Impact profiles</legend>
            {data.impactProfiles.length === 0 ? (
              <span className="gf-patches__muted">没有 active impact profile</span>
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
                    {profile.display_name} · {key}
                  </label>
                );
              })
            )}
          </fieldset>
          <label>
            Seed
            <input min={0} onChange={(event) => setSeed(event.target.value)} type="number" value={seed} />
          </label>
          <p className="gf-patches__muted">
            Seed {seedRequired ? "required by the resolved stochastic/regression closure" : "not applicable"}.
          </p>
          <fieldset className="gf-patches__checklist gf-patches__form-wide">
            <legend>Regression suites（可选）</legend>
            <label>
              搜索回归套件
              <input
                onChange={(event) => setRegressionSearch(event.target.value)}
                type="search"
                value={regressionSearch}
              />
            </label>
            {data.regressionSuites.length === 0 ? (
              <span className="gf-patches__muted">当前 Artifact 目录没有 regression_suite。</span>
            ) : visibleRegressionSuites.length === 0 ? (
              <span className="gf-patches__muted">没有匹配的回归套件。</span>
            ) : (
              visibleRegressionSuites.map((artifact) => (
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
                  回归套件 · {artifact.created_at?.slice(0, 10) ?? "时间未知"} · {artifact.payload_schema_id}{" "}
                  · {shortId(artifact.artifact_id)}
                </label>
              ))
            )}
          </fieldset>
        </div>
        <div className="gf-patches__action-row">
          <button disabled={!canValidate} onClick={validate} type="button">
            启动 rollback validation
          </button>
          {acceptedRunId && (
            <div className="gf-patches__live-receipt" role="status">
              <a href={`/runs/${encodeURIComponent(acceptedRunId)}`}>打开 accepted Run</a>
            </div>
          )}
          {item.active_validation_run_id && (
            <a href={`/runs/${encodeURIComponent(item.active_validation_run_id)}`}>
              打开 active validation Run
            </a>
          )}
        </div>
        <EvidenceLedger data={data} />
      </section>

      <section className="gf-patches__workspace-section" aria-labelledby="rollback-approval-title">
        <header>
          <Send aria-hidden="true" size={21} />
          <div>
            <h2 id="rollback-approval-title" ref={applyReturnFocusRef} tabIndex={-1}>
              Independent approval & apply
            </h2>
            <p>Rollback 永不 auto-apply；maker-checker 决定来自独立 ApprovalItem。</p>
          </div>
        </header>
        <div className="gf-patches__approval-actions">
          <button disabled={!canSubmit} onClick={submit} type="button">
            提交独立人工审批
          </button>
          <a href={`/approvals/${encodeURIComponent(data.binding.approval_id)}`}>打开 Approval</a>
          <button disabled={!canApply} onClick={() => setConfirmApply(true)} type="button">
            Apply approved rollback
          </button>
        </div>
        {applyResult && (
          <div className="gf-patches__live-receipt" role="status">
            <StatePanel
              description={`ref now points to ${applyResult.ref_value.artifact_id} at revision ${applyResult.ref_value.revision}.`}
              state="terminal"
              title="Rollback 已通过 ref transition 应用"
            />
            {applyResult.ref_transition_id && (
              <p>
                Ref transition · <code>{applyResult.ref_transition_id}</code>
              </p>
            )}
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
            <h2 id="target-lineage-title">Historical target content lineage</h2>
            <p>这是 target Artifact 的 immutable parent DAG；apply 只追加 ref history 与 RefTransition。</p>
          </div>
        </header>
        <p className="gf-patches__principle">
          RefTransition is not a content-lineage edge；页面不会把 rollback request 画成 target 的新 parent。
        </p>
        {data.lineage.length === 0 ? (
          <p className="gf-patches__muted">Target Artifact 没有可见 parent lineage。</p>
        ) : (
          <ul className="gf-patches__history-list">
            {data.lineage.map((entry) => (
              <li key={`${entry.depth}:${entry.artifact.artifact_id}`}>
                <span>depth {entry.depth}</span>
                <a href={`/artifacts/${encodeURIComponent(entry.artifact.artifact_id)}`}>
                  {entry.artifact.artifact_id}
                </a>
              </li>
            ))}
          </ul>
        )}
      </section>

      <ConfirmDialog
        confirmLabel="确认 Apply rollback"
        description={`将 ${data.target.ref_name} 从 revision ${data.current.value.request.expected_current_ref.revision} 指向历史 Artifact ${data.target.target_artifact_id}。此动作需要服务器再次执行 exact ref CAS。`}
        onCancel={() => setConfirmApply(false)}
        onConfirm={apply}
        open={confirmApply}
        returnFocusRef={applyReturnFocusRef}
        title="Apply approved rollback?"
      />
    </div>
  );
}
