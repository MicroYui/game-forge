import { useQuery } from "@tanstack/react-query";
import {
  BadgeCheck,
  Bot,
  FilePenLine,
  GitBranch,
  PlayCircle,
  Send,
  ShieldCheck,
  UserRound,
} from "lucide-react";
import { useEffect, useState } from "react";

import { createMutationIntent } from "../../api/csrf";
import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { EvidenceSections } from "../../components/evidence";
import { CopyableText } from "../../components/tables";
import { ProblemPanel, StatePanel } from "../../components/ui";
import {
  specWorkflowApi,
  type ApprovalView,
  type ArtifactPayloadView,
  type ConstraintProposalReadView,
  type ConstraintValidationAdmissionRequest,
  type ExecutionProfilePage,
  type HumanConstraintRevisionRequest,
  type SpecWorkflowApi,
  type SubjectApprovalBindingView,
  type SubmitForApprovalRequest,
  type VersionedResource,
  type WorkflowApplyRequest,
  type WorkflowApplyResult,
} from "./api";
import "./specs.css";

export type ConstraintProposalApi = Pick<
  SpecWorkflowApi,
  | "getApproval"
  | "getApprovalBinding"
  | "getArtifactPayload"
  | "getConstraintProposal"
  | "getConstraintValidationCompilerBinding"
  | "listExecutionProfiles"
  | "publishConstraint"
  | "reviseConstraint"
  | "submitConstraintForApproval"
  | "validateConstraint"
>;

type ApprovalRecord = ApprovalView["approval"];
type ApprovalRouteRequirement = ApprovalRecord["requirements"][number];
type ExecutionProfile = ExecutionProfilePage["items"][number];
type ConstraintTargetBinding = Extract<
  NonNullable<ApprovalRecord["target_binding"]>,
  { subject_kind: "constraint_proposal" }
>;

interface WorkflowData {
  approval: VersionedResource<ApprovalView> | null;
  baseArtifactId: string | null | undefined;
  binding: SubjectApprovalBindingView | null;
  current: VersionedResource<ConstraintProposalReadView>;
  evidenceArtifact: ArtifactPayloadView | null;
  failureArtifact: ArtifactPayloadView | null;
}

async function resolveBaseArtifactId(
  api: ConstraintProposalApi,
  current: VersionedResource<ConstraintProposalReadView>,
): Promise<string | null | undefined> {
  const snapshotId = current.value.proposal.base_constraint_snapshot_id;
  if (snapshotId == null) return null;
  const excludedParentIds = new Set([
    ...current.value.proposal.source_bindings.map((source) => source.source_artifact_id),
    ...(current.value.proposal.supersedes_artifact_id ? [current.value.proposal.supersedes_artifact_id] : []),
  ]);
  const candidateIds = current.value.artifact.parent_artifact_ids.filter(
    (parentId) => !excludedParentIds.has(parentId),
  );
  const candidates = await Promise.all(
    candidateIds.map((candidateId) => api.getArtifactPayload(candidateId)),
  );
  const matches = candidates.filter(
    (candidate) =>
      candidate.artifact.kind === "constraint_snapshot" &&
      candidate.artifact.version_tuple.constraint_snapshot_id === snapshotId,
  );
  return matches.length === 1 ? matches[0]?.artifact.artifact_id : undefined;
}

interface ProfileState {
  error: Error | null;
  items: ExecutionProfile[];
  loading: boolean;
  nextCursor: string | null;
  readSnapshotId: string;
}

type EvidenceView =
  | { kind: "none" }
  | { kind: "unsafe"; schemaId: string | null }
  | {
      kind: "evidence";
      requirements: { requirementId: string; status: string }[];
      runId: string;
      status: "passed" | "failed" | "unproven";
    };

type FailureView =
  | { kind: "none" }
  | { kind: "unsafe"; schemaId: string | null }
  | { causeCode: string; kind: "failure"; message: string; runId: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function evidenceTargetMatches(value: unknown, expected: ConstraintTargetBinding | null): boolean {
  if (expected === null || !isRecord(value)) return false;
  const expectedRef = value.expected_ref;
  const refMatches =
    expected.expected_ref == null
      ? expectedRef == null
      : isRecord(expectedRef) &&
        expectedRef.artifact_id === expected.expected_ref.artifact_id &&
        expectedRef.revision === expected.expected_ref.revision;
  return (
    value.binding_schema_version === "approval-target-binding@1" &&
    value.subject_kind === "constraint_proposal" &&
    value.target_artifact_kind === "constraint_snapshot" &&
    value.target_artifact_id === expected.target_artifact_id &&
    value.target_snapshot_id === expected.target_snapshot_id &&
    value.target_digest === expected.target_digest &&
    value.ref_name === expected.ref_name &&
    refMatches
  );
}

function parseEvidence(artifactView: ArtifactPayloadView | null, approval: ApprovalRecord): EvidenceView {
  if (artifactView === null) return { kind: "none" };
  if (
    artifactView.artifact.kind !== "validation_evidence" ||
    artifactView.artifact.artifact_id !== approval.evidence_set_artifact_id ||
    artifactView.artifact.payload_schema_id !== "evidence-set@1"
  ) {
    return { kind: "unsafe", schemaId: artifactView.artifact.payload_schema_id ?? null };
  }
  const payload = artifactView.payload;
  if (!isRecord(payload) || payload.evidence_schema_version !== "evidence-set@1") {
    return { kind: "unsafe", schemaId: artifactView.artifact.payload_schema_id };
  }
  const status = payload.overall_status;
  const runId = payload.validation_run_id;
  const requirements = payload.requirements;
  if (
    (status !== "passed" && status !== "failed" && status !== "unproven") ||
    typeof runId !== "string" ||
    payload.subject_artifact_id !== approval.subject_artifact_id ||
    payload.subject_digest !== approval.subject_digest ||
    !Array.isArray(requirements)
  ) {
    return { kind: "unsafe", schemaId: artifactView.artifact.payload_schema_id };
  }
  if (status === "passed" && !evidenceTargetMatches(payload.target_binding, constraintTarget(approval))) {
    return { kind: "unsafe", schemaId: artifactView.artifact.payload_schema_id };
  }
  const safeRequirements: { requirementId: string; status: string }[] = [];
  for (const requirement of requirements) {
    if (
      !isRecord(requirement) ||
      typeof requirement.requirement_id !== "string" ||
      typeof requirement.status !== "string"
    ) {
      return { kind: "unsafe", schemaId: artifactView.artifact.payload_schema_id };
    }
    safeRequirements.push({
      requirementId: requirement.requirement_id,
      status: requirement.status,
    });
  }
  return { kind: "evidence", requirements: safeRequirements, runId, status };
}

function parseFailure(artifactView: ArtifactPayloadView | null, approval: ApprovalRecord): FailureView {
  if (artifactView === null) return { kind: "none" };
  if (
    artifactView.artifact.kind !== "run_failure" ||
    artifactView.artifact.artifact_id !== approval.last_validation_failure_artifact_id ||
    artifactView.artifact.payload_schema_id !== "run-failure@1"
  ) {
    return { kind: "unsafe", schemaId: artifactView.artifact.payload_schema_id ?? null };
  }
  const payload = artifactView.payload;
  if (
    !isRecord(payload) ||
    payload.failure_schema_version !== "run-failure@1" ||
    typeof payload.run_id !== "string" ||
    typeof payload.cause_code !== "string" ||
    typeof payload.redacted_message !== "string"
  ) {
    return { kind: "unsafe", schemaId: artifactView.artifact.payload_schema_id };
  }
  return {
    causeCode: payload.cause_code,
    kind: "failure",
    message: payload.redacted_message,
    runId: payload.run_id,
  };
}

function constraintTarget(item: ApprovalRecord): ConstraintTargetBinding | null {
  const target = item.target_binding;
  return target?.subject_kind === "constraint_proposal" ? target : null;
}

function profileKey(profile: ExecutionProfile): string {
  return `${profile.profile.profile_id}@${profile.profile.version}`;
}

function permissionLabel(requirement: ApprovalRouteRequirement): string {
  const scope = requirement.required_permission.domain_scope;
  const domain = scope === "all" ? "all" : scope === null ? "global" : scope.domain_ids.join(", ");
  return `${requirement.required_permission.action} · ${requirement.required_permission.resource_kind} · ${domain}`;
}

function expectedRef(
  artifactId: string,
  revision: string,
  confirmMissing: boolean,
): { artifact_id: string; revision: number } | null | undefined {
  const normalizedId = artifactId.trim();
  const normalizedRevision = revision.trim();
  if (confirmMissing) {
    return !normalizedId && !normalizedRevision ? null : undefined;
  }
  if (!normalizedId && !normalizedRevision) return undefined;
  const parsedRevision = Number(normalizedRevision);
  if (!normalizedId || !Number.isInteger(parsedRevision) || parsedRevision < 1) return undefined;
  return { artifact_id: normalizedId, revision: parsedRevision };
}

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("操作失败。");
}

function EvidenceStatus({ evidence, failure }: { evidence: EvidenceView; failure: FailureView }) {
  let evidenceContent: React.ReactNode;
  if (evidence.kind === "none") {
    evidenceContent = <p>尚无 EvidenceSet；Run 状态不会被当作验证结论。</p>;
  } else if (evidence.kind === "unsafe") {
    evidenceContent = (
      <div>
        <strong>证据载荷无法安全解释</strong>
        <p>
          仅支持 <code>evidence-set@1</code>；收到 <code>{evidence.schemaId ?? "unknown"}</code>。
        </p>
      </div>
    );
  } else {
    evidenceContent = (
      <div>
        <strong>确定性证据：{evidence.status === "passed" ? "validated" : evidence.status}</strong>
        <p>EvidenceSet 固化 {evidence.requirements.length} 项 requirement disposition；状态来自证据载荷。</p>
        <a href={`/runs/${encodeURIComponent(evidence.runId)}`}>打开证据 Run</a>
      </div>
    );
  }

  const failureContent =
    failure.kind === "failure" ? (
      <aside className="gf-specs__semantic-note" role="note">
        <FilePenLine aria-hidden="true" size={18} />
        <div>
          <strong>{failure.causeCode}</strong>
          <p>{failure.message}</p>
          <a href={`/runs/${encodeURIComponent(failure.runId)}`}>打开失败 Run</a>
        </div>
      </aside>
    ) : failure.kind === "unsafe" ? (
      <p>最近失败工件 schema 不受支持，未解释其 payload。</p>
    ) : null;

  return (
    <EvidenceSections
      deterministic={
        <div>
          {evidenceContent}
          {failureContent}
        </div>
      }
    />
  );
}

export function ConstraintProposalPage({
  api = specWorkflowApi,
  artifactId,
}: {
  api?: ConstraintProposalApi;
  artifactId: string;
}) {
  const [currentArtifactId, setCurrentArtifactId] = useState(artifactId);
  const workflow = useQuery({
    queryFn: async (): Promise<WorkflowData> => {
      const current = await api.getConstraintProposal(currentArtifactId);
      const baseArtifactId = await resolveBaseArtifactId(api, current);
      let binding: SubjectApprovalBindingView;
      try {
        binding = await api.getApprovalBinding(current.value.artifact.artifact_id);
      } catch (error) {
        if (error instanceof ApiProblemError && error.problem.status === 404) {
          return {
            approval: null,
            baseArtifactId,
            binding: null,
            current,
            evidenceArtifact: null,
            failureArtifact: null,
          };
        }
        throw error;
      }
      const approval = await api.getApproval(binding.approval_id);
      const [evidenceArtifact, failureArtifact] = await Promise.all([
        approval.value.approval.evidence_set_artifact_id
          ? api.getArtifactPayload(approval.value.approval.evidence_set_artifact_id)
          : Promise.resolve(null),
        approval.value.approval.last_validation_failure_artifact_id
          ? api.getArtifactPayload(approval.value.approval.last_validation_failure_artifact_id)
          : Promise.resolve(null),
      ]);
      return { approval, baseArtifactId, binding, current, evidenceArtifact, failureArtifact };
    },
    queryKey: ["constraint-proposal", currentArtifactId],
    retry: false,
  });

  const profileQuery = useQuery({
    queryFn: () => api.listExecutionProfiles(null),
    queryKey: ["constraint-proposal", "execution-profiles"],
    retry: false,
  });
  const [profileState, setProfileState] = useState<ProfileState | null>(null);
  useEffect(() => {
    if (profileQuery.data) {
      setProfileState({
        error: null,
        items: profileQuery.data.items,
        loading: false,
        nextCursor: profileQuery.data.next_cursor ?? null,
        readSnapshotId: profileQuery.data.read_snapshot_id,
      });
    }
  }, [profileQuery.data]);

  const [refName, setRefName] = useState("");
  const [expectedRefArtifactId, setExpectedRefArtifactId] = useState("");
  const [expectedRefRevision, setExpectedRefRevision] = useState("");
  const [confirmMissingRef, setConfirmMissingRef] = useState(false);
  const [rationale, setRationale] = useState("");
  const [compilerKey, setCompilerKey] = useState("");
  const [validationKey, setValidationKey] = useState("");
  const [requirementId, setRequirementId] = useState("");
  const [mutationError, setMutationError] = useState<Error | null>(null);
  const [mutationPending, setMutationPending] = useState(false);
  const [mutationLocked, setMutationLocked] = useState(false);
  const [acceptedRunId, setAcceptedRunId] = useState<string | null>(null);
  const [published, setPublished] = useState<WorkflowApplyResult | null>(null);

  useEffect(() => {
    const data = workflow.data;
    if (!data?.approval) return;
    const target = constraintTarget(data.approval.value.approval);
    setRationale(data.current.value.proposal.rationale);
    setRefName(target?.ref_name ?? "");
    setExpectedRefArtifactId(target?.expected_ref?.artifact_id ?? "");
    setExpectedRefRevision(target?.expected_ref ? String(target.expected_ref.revision) : "");
    setConfirmMissingRef(target !== null && target.expected_ref == null);
    setRequirementId("");
  }, [workflow.data]);

  async function loadMoreProfiles() {
    const current = profileState;
    if (!current?.nextCursor) return;
    setProfileState({ ...current, error: null, loading: true });
    try {
      const next = await api.listExecutionProfiles(current.nextCursor);
      if (next.read_snapshot_id !== current.readSnapshotId) {
        throw new Error("Execution profile 目录快照已变化，请重新开始。");
      }
      setProfileState({
        error: null,
        items: [...current.items, ...next.items],
        loading: false,
        nextCursor: next.next_cursor ?? null,
        readSnapshotId: current.readSnapshotId,
      });
    } catch (error) {
      setProfileState({ ...current, error: normalizedError(error), loading: false });
    }
  }

  async function runMutation<T>(action: () => Promise<T>, after: (value: T) => Promise<void>) {
    setMutationError(null);
    setMutationPending(true);
    try {
      const value = await action();
      await after(value);
      setMutationLocked(false);
    } catch (error) {
      setMutationError(normalizedError(error));
      setMutationLocked(true);
    } finally {
      setMutationPending(false);
    }
  }

  if (workflow.isPending || profileQuery.isPending) {
    return (
      <div className="gf-page gf-specs">
        <StatePanel
          description="正在读取候选 Artifact、exact ETag、审批绑定与 execution profile 目录。"
          headingLevel={1}
          state="loading"
          title="正在读取约束候选"
        />
      </div>
    );
  }

  const loadError = workflow.error ?? profileQuery.error;
  if (loadError) {
    return (
      <div className="gf-page gf-specs">
        <header className="gf-page-header">
          <p className="gf-specs__kicker">Constraint proposal · Candidate</p>
          <h1>约束候选</h1>
        </header>
        {loadError instanceof ApiProblemError ? (
          <ProblemPanel problem={loadError.problem} />
        ) : (
          <StatePanel
            action={
              <button
                className="gf-secondary-button"
                onClick={() => void Promise.all([workflow.refetch(), profileQuery.refetch()])}
                type="button"
              >
                重试
              </button>
            }
            description="候选工作流读取失败；未展示底层异常内容。"
            state="error"
            title="无法读取约束候选"
          />
        )}
      </div>
    );
  }

  const data = workflow.data;
  const initialProfiles = profileQuery.data;
  if (!data || !initialProfiles) {
    return (
      <div className="gf-page gf-specs">
        <StatePanel
          description="响应尚未形成完整的 proposal 与 profile 读取快照。"
          headingLevel={1}
          state="loading"
          title="正在读取约束候选"
        />
      </div>
    );
  }
  if (!data.binding || !data.approval) {
    return (
      <div className="gf-page gf-specs">
        <StatePanel
          description="服务器未返回该 immutable proposal 的 retained approval binding；页面不会推导 approval ID。"
          headingLevel={1}
          state="error"
          title="审批绑定缺失"
        />
      </div>
    );
  }

  const current = data.current;
  const baseArtifactId = data.baseArtifactId;
  const proposal = current.value;
  const binding = data.binding;
  const approval = data.approval.value;
  const item = approval.approval;
  const bindingIsExact =
    binding.subject_kind === "constraint_proposal" &&
    binding.subject_artifact_id === proposal.artifact.artifact_id &&
    binding.subject_revision === proposal.proposal.revision &&
    binding.subject_revision === item.subject_revision &&
    binding.subject_head_revision >= binding.subject_revision &&
    binding.is_current_head === (binding.subject_head_revision === binding.subject_revision) &&
    binding.subject_series_id === item.subject_series_id &&
    binding.workflow_revision === item.workflow_revision &&
    binding.workflow_revision === proposal.workflow_revision &&
    binding.approval_status === item.status &&
    proposal.approval_status === item.status &&
    item.approval_id === binding.approval_id &&
    item.subject_kind === "constraint_proposal" &&
    item.subject_artifact_id === binding.subject_artifact_id &&
    item.subject_digest === binding.subject_digest;
  if (!bindingIsExact) {
    return (
      <div className="gf-page gf-specs">
        <StatePanel
          description="Proposal、subject binding 与 ApprovalView 身份不一致；所有工作流命令已停止。"
          headingLevel={1}
          state="error"
          title="审批绑定不一致"
        />
      </div>
    );
  }

  const profiles = profileState?.items ?? initialProfiles.items;
  const compilerProfiles = profiles.filter(
    (profile) => profile.status === "active" && profile.profile_kind === "constraint_compiler",
  );
  const validationProfiles = profiles.filter(
    (profile) =>
      profile.status === "active" &&
      profile.profile_kind === "validation" &&
      profile.compatible_run_kinds.some(
        (runKind) => runKind.kind === "constraint_proposal.validate" && runKind.version === 1,
      ),
  );
  const selectedCompiler = compilerProfiles.find((profile) => profileKey(profile) === compilerKey);
  const selectedValidation = validationProfiles.find((profile) => profileKey(profile) === validationKey);
  const selectedRequirement = item.requirements.find(
    (requirement) => requirement.requirement_id === requirementId,
  );
  const target = constraintTarget(item);
  const evidence = parseEvidence(data.evidenceArtifact, item);
  const failure = parseFailure(data.failureArtifact, item);
  const refValue = expectedRef(expectedRefArtifactId, expectedRefRevision, confirmMissingRef);
  const refIsValid = refName.trim().length > 0 && refValue !== undefined;
  const baseIsResolved =
    proposal.proposal.base_constraint_snapshot_id == null || typeof baseArtifactId === "string";
  const isHuman = proposal.proposal.produced_by === "human";
  const evidencePassed = evidence.kind === "evidence" && evidence.status === "passed";
  const canValidate =
    isHuman &&
    binding.is_current_head &&
    item.status === "draft" &&
    baseIsResolved &&
    refIsValid &&
    selectedCompiler !== undefined &&
    selectedValidation !== undefined &&
    !mutationLocked &&
    !mutationPending;
  const canRevise =
    binding.is_current_head &&
    baseIsResolved &&
    refIsValid &&
    rationale.trim().length > 0 &&
    !mutationLocked &&
    !mutationPending;
  const canSubmit =
    isHuman &&
    binding.is_current_head &&
    item.status === "validated" &&
    evidencePassed &&
    selectedRequirement !== undefined &&
    !mutationLocked &&
    !mutationPending;
  const canPublish =
    isHuman &&
    binding.is_current_head &&
    item.status === "approved" &&
    evidencePassed &&
    target !== null &&
    !mutationLocked &&
    !mutationPending;

  async function reloadServerState() {
    setMutationPending(true);
    const result = await workflow.refetch();
    if (result.isSuccess) {
      setMutationError(null);
      setMutationLocked(false);
    } else {
      setMutationError(normalizedError(result.error));
      setMutationLocked(true);
    }
    setMutationPending(false);
  }

  async function revise() {
    if (!canRevise || refValue === undefined) return;
    const request: HumanConstraintRevisionRequest = {
      approval_id: binding.approval_id,
      base_constraint_snapshot_artifact_id: baseArtifactId ?? null,
      constraints: proposal.proposal.constraints,
      domain_scope: proposal.proposal.domain_scope,
      dsl_grammar_version: proposal.proposal.dsl_grammar_version,
      expected_ref: refValue,
      expected_subject_head_revision: binding.subject_head_revision,
      expected_workflow_revision: binding.workflow_revision,
      rationale: rationale.trim(),
      ref_name: refName.trim(),
      request_schema_version: "human-constraint-revision-request@1",
      source_artifact_ids: proposal.proposal.source_bindings.map((source) => source.source_artifact_id),
    };
    await runMutation(
      () => api.reviseConstraint(current, request, createMutationIntent()),
      async (revised) => {
        setAcceptedRunId(null);
        setPublished(null);
        setCurrentArtifactId(revised.artifact.artifact_id);
      },
    );
  }

  async function validate() {
    if (!canValidate || refValue === undefined || !selectedCompiler || !selectedValidation) return;
    await runMutation(
      async () => {
        const compiler = await api.getConstraintValidationCompilerBinding(
          selectedCompiler.profile.profile_id,
          selectedCompiler.profile.version,
        );
        const request: ConstraintValidationAdmissionRequest = {
          approval_id: binding.approval_id,
          base_constraint_snapshot_artifact_id: baseArtifactId ?? null,
          compiler_profile: compiler.compiler_profile,
          differential_engines: compiler.differential_engines,
          dsl_grammar_version: proposal.proposal.dsl_grammar_version,
          expected_subject_head_revision: binding.subject_head_revision,
          expected_workflow_revision: binding.workflow_revision,
          golden_suite_artifact_id: null,
          regression_suite_artifact_ids: [],
          request_schema_version: "constraint-validation-admission-request@1",
          seed: null,
          subject_digest: binding.subject_digest,
          target: { expected_ref: refValue, ref_name: refName.trim() },
          validation_policy: selectedValidation.profile,
        };
        return api.validateConstraint(current, request, createMutationIntent());
      },
      async (accepted) => {
        setAcceptedRunId(accepted.run_id);
        await workflow.refetch();
      },
    );
  }

  async function submit() {
    if (!canSubmit) return;
    const request: SubmitForApprovalRequest = {
      approval_id: binding.approval_id,
      expected_workflow_revision: binding.workflow_revision,
      request_schema_version: "submit-for-approval-request@1",
    };
    await runMutation(
      () => api.submitConstraintForApproval(current, request, createMutationIntent()),
      async () => {
        await workflow.refetch();
      },
    );
  }

  async function publish() {
    if (!canPublish || target === null) return;
    const request: WorkflowApplyRequest = {
      approval_id: binding.approval_id,
      expected_ref: target.expected_ref ?? null,
      expected_workflow_revision: binding.workflow_revision,
      ref_name: target.ref_name,
      request_schema_version: "workflow-apply-request@1",
      subject_digest: binding.subject_digest,
      target_artifact_id: target.target_artifact_id,
      target_digest: target.target_digest,
    };
    await runMutation(
      () => api.publishConstraint(current, request, createMutationIntent()),
      async (result) => {
        setPublished(result);
        await workflow.refetch();
      },
    );
  }

  return (
    <div className="gf-page gf-specs gf-constraint-proposal">
      <nav aria-label="约束候选导航" className="gf-specs__back-nav">
        <a href="/specs">返回规格工作台</a>
        <a href={`/artifacts/${encodeURIComponent(proposal.artifact.artifact_id)}`}>查看安全 Artifact 摘要</a>
        <a href={`/approvals/${encodeURIComponent(binding.approval_id)}`}>打开 exact approval</a>
        <a href={`/constraint-proposals/${encodeURIComponent(proposal.artifact.artifact_id)}`}>
          当前 revision canonical detail
        </a>
      </nav>

      <header className="gf-specs__hero gf-specs__hero--detail">
        <div>
          <p className="gf-specs__kicker">Constraint proposal · Candidate Artifact</p>
          <h1>约束候选</h1>
          <p className="gf-specs__lede">
            候选 Artifact 不等于权威约束；只有确定性证据、另一位 Human 审批与 publish ref transition
            能完成权威化。
          </p>
        </div>
        <span className="gf-specs__status-mark">
          {isHuman ? <UserRound aria-hidden="true" size={17} /> : <Bot aria-hidden="true" size={17} />}
          {isHuman ? "Human 修订候选" : "Agent 候选 · 必须由 Human 修订"}
        </span>
      </header>

      <dl className="gf-specs__facts" aria-label="约束候选工作流身份">
        <div>
          <dt>Proposal Artifact</dt>
          <dd>
            <CopyableText copyLabel="复制 Proposal Artifact ID" value={proposal.artifact.artifact_id} />
          </dd>
        </div>
        <div>
          <dt>Exact ETag</dt>
          <dd>
            <CopyableText copyLabel="复制 Proposal ETag" value={current.etag} />
          </dd>
        </div>
        <div>
          <dt>Approval</dt>
          <dd>
            <CopyableText copyLabel="复制 Approval ID" value={binding.approval_id} />
          </dd>
        </div>
        <div>
          <dt>Workflow</dt>
          <dd>
            head {binding.subject_head_revision} · workflow {binding.workflow_revision} · {item.status}
          </dd>
        </div>
        <div className="gf-specs__fact-wide">
          <dt>Subject digest</dt>
          <dd>
            <CopyableText copyLabel="复制 Subject digest" value={binding.subject_digest} />
          </dd>
        </div>
        <div className="gf-specs__fact-wide">
          <dt>Base constraint</dt>
          <dd>
            {typeof baseArtifactId === "string" ? (
              <CopyableText copyLabel="复制 Base Constraint Artifact ID" value={baseArtifactId} />
            ) : proposal.proposal.base_constraint_snapshot_id == null ? (
              <span className="gf-specs__muted">无 base constraint snapshot</span>
            ) : (
              <span className="gf-specs__muted">parent summary 尚未唯一匹配</span>
            )}
          </dd>
        </div>
      </dl>

      {!isHuman && (
        <aside className="gf-specs__semantic-note" role="note">
          <Bot aria-hidden="true" size={20} />
          <div>
            <strong>Agent 只提交候选</strong>
            <p>必须先生成 produced_by=human 的 superseding revision，才能进入 compile / validate。</p>
          </div>
        </aside>
      )}

      {!baseIsResolved && (
        <StatePanel
          description="Proposal 声明了 base constraint snapshot，但 parent summaries 中没有唯一 kind/version-tuple 匹配；revision 与 validate 已停止。"
          state="error"
          title="Base Constraint Artifact 未唯一解析"
        />
      )}

      {mutationError &&
        (mutationError instanceof ApiProblemError ? (
          <div>
            <ProblemPanel problem={mutationError.problem} />
            {mutationError.problem.conflict_set_id && (
              <p className="gf-specs__muted" role="note">
                响应只提供 ConflictSet ID，没有 exact Patch Artifact ID；本页不会据此伪造 Patch 详情路由。
              </p>
            )}
            <button
              className="gf-secondary-button"
              disabled={mutationPending}
              onClick={() => void reloadServerState()}
              type="button"
            >
              重新读取服务器状态
            </button>
          </div>
        ) : (
          <StatePanel
            action={
              <button
                className="gf-secondary-button"
                disabled={mutationPending}
                onClick={() => void reloadServerState()}
                type="button"
              >
                重新读取服务器状态
              </button>
            }
            description="命令失败；未展示底层异常内容。请刷新 exact server state 后重试。"
            state="error"
            title="工作流命令失败"
          />
        ))}

      <section className="gf-specs__workspace-section" aria-labelledby="evidence-title">
        <header className="gf-specs__section-heading">
          <ShieldCheck aria-hidden="true" size={19} />
          <div>
            <h2 id="evidence-title">验证证据</h2>
            <p>仅解释 schema-guarded EvidenceSet 与 RunFailure；不从 Run status 推断 verdict。</p>
          </div>
        </header>
        <EvidenceStatus evidence={evidence} failure={failure} />
        {item.active_validation_run_id && (
          <a href={`/runs/${encodeURIComponent(item.active_validation_run_id)}`}>打开当前 validation Run</a>
        )}
        {acceptedRunId && <a href={`/runs/${encodeURIComponent(acceptedRunId)}`}>打开 validation Run</a>}
      </section>

      <section className="gf-specs__workspace-section" aria-labelledby="authority-binding-title">
        <header className="gf-specs__section-heading">
          <GitBranch aria-hidden="true" size={19} />
          <div>
            <h2 id="authority-binding-title">显式 ref 绑定</h2>
            <p>expected_ref=null 只能通过显式确认提交；页面不从 Artifact 名称或 kind 推断 authority。</p>
          </div>
        </header>
        <div className="gf-form">
          <label>
            Ref name
            <input onChange={(event) => setRefName(event.target.value)} type="text" value={refName} />
          </label>
          {!confirmMissingRef && (
            <>
              <label>
                Expected ref Artifact ID
                <input
                  onChange={(event) => setExpectedRefArtifactId(event.target.value)}
                  type="text"
                  value={expectedRefArtifactId}
                />
              </label>
              <label>
                Expected ref revision
                <input
                  min="1"
                  onChange={(event) => setExpectedRefRevision(event.target.value)}
                  type="number"
                  value={expectedRefRevision}
                />
              </label>
            </>
          )}
          <label className="gf-cluster">
            <input
              checked={confirmMissingRef}
              onChange={(event) => {
                const checked = event.target.checked;
                setConfirmMissingRef(checked);
                if (checked) {
                  setExpectedRefArtifactId("");
                  setExpectedRefRevision("");
                }
              }}
              type="checkbox"
            />
            确认当前 ref 不存在（expected_ref=null）
          </label>
        </div>
      </section>

      <section className="gf-specs__workspace-section" aria-labelledby="human-revision-title">
        <header className="gf-specs__section-heading">
          <FilePenLine aria-hidden="true" size={19} />
          <div>
            <h2 id="human-revision-title">人工接管与修订</h2>
            <p>复用当前 proposal 的 typed constraints、source bindings、base 与 DSL grammar。</p>
          </div>
        </header>
        <form
          className="gf-form"
          onSubmit={(event) => {
            event.preventDefault();
            void revise();
          }}
        >
          <label>
            修订说明
            <textarea onChange={(event) => setRationale(event.target.value)} rows={4} value={rationale} />
          </label>
          <button disabled={!canRevise} type="submit">
            提交人工修订
          </button>
        </form>
      </section>

      <section className="gf-specs__workspace-section" aria-labelledby="validation-title">
        <header className="gf-specs__section-heading">
          <PlayCircle aria-hidden="true" size={19} />
          <div>
            <h2 id="validation-title">Compile / validate</h2>
            <p>必须显式选择 active compiler 与 validation profile；没有自动默认值。</p>
          </div>
        </header>
        <div className="gf-form">
          <label>
            Compiler profile
            <select onChange={(event) => setCompilerKey(event.target.value)} value={compilerKey}>
              <option value="">请选择 active constraint_compiler</option>
              {compilerProfiles.map((profile) => (
                <option key={profileKey(profile)} value={profileKey(profile)}>
                  {profile.display_name} · {profileKey(profile)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Validation profile
            <select onChange={(event) => setValidationKey(event.target.value)} value={validationKey}>
              <option value="">请选择 active validation profile</option>
              {validationProfiles.map((profile) => (
                <option key={profileKey(profile)} value={profileKey(profile)}>
                  {profile.display_name} · {profileKey(profile)}
                </option>
              ))}
            </select>
          </label>
          <button disabled={!canValidate} onClick={() => void validate()} type="button">
            开始确定性验证
          </button>
        </div>
        {profileState?.nextCursor && (
          <button
            className="gf-secondary-button"
            disabled={profileState.loading}
            onClick={() => void loadMoreProfiles()}
            type="button"
          >
            {profileState.loading ? "正在加载 profiles" : "加载更多 profiles"}
          </button>
        )}
        {profileState?.error && (
          <StatePanel
            action={
              profileState.error instanceof CursorExpiredError ? (
                <button
                  className="gf-secondary-button"
                  onClick={() => void profileQuery.refetch()}
                  type="button"
                >
                  从目录首页重新开始
                </button>
              ) : undefined
            }
            description="Execution profile 分页读取失败；未选择任何隐式 fallback。"
            state="error"
            title="Profile 目录分页失败"
          />
        )}
      </section>

      <section className="gf-specs__workspace-section" aria-labelledby="approval-title">
        <header className="gf-specs__section-heading">
          <Send aria-hidden="true" size={19} />
          <div>
            <h2 id="approval-title">审批 handoff</h2>
            <p>选择 server-issued requirement，复制 route/permission，再交给另一位 Human。</p>
          </div>
        </header>
        <div className="gf-form">
          <p>此选择仅用于核对 server-frozen route，不进入 submit payload，也不会改变审批路由。</p>
          <label>
            Approval requirement
            <select onChange={(event) => setRequirementId(event.target.value)} value={requirementId}>
              <option value="">请选择 exact requirement</option>
              {item.requirements.map((requirement) => (
                <option key={requirement.requirement_id} value={requirement.requirement_id}>
                  {requirement.requirement_id}
                </option>
              ))}
            </select>
          </label>
          {selectedRequirement && (
            <dl className="gf-specs__facts">
              <div>
                <dt>Route role</dt>
                <dd>
                  <CopyableText copyLabel="复制 route role" value={selectedRequirement.route_role} />
                </dd>
              </div>
              <div>
                <dt>Required permission</dt>
                <dd>
                  <CopyableText
                    copyLabel="复制 required permission"
                    value={permissionLabel(selectedRequirement)}
                  />
                </dd>
              </div>
            </dl>
          )}
          <button disabled={!canSubmit} onClick={() => void submit()} type="button">
            提交审批
          </button>
        </div>
        <a href={`/approvals/${encodeURIComponent(binding.approval_id)}`}>交给另一位 Human 审批</a>
      </section>

      <section className="gf-specs__workspace-section" aria-labelledby="publish-title">
        <header className="gf-specs__section-heading">
          <BadgeCheck aria-hidden="true" size={19} />
          <div>
            <h2 id="publish-title">Publish authority</h2>
            <p>仅复制 approved ApprovalView 中的 ConstraintTargetBindingV1，不猜目标。</p>
          </div>
        </header>
        {target ? (
          <dl className="gf-specs__facts">
            <div>
              <dt>Candidate target</dt>
              <dd>{target.target_artifact_id}</dd>
            </div>
            <div>
              <dt>Target snapshot</dt>
              <dd>{target.target_snapshot_id}</dd>
            </div>
            <div className="gf-specs__fact-wide">
              <dt>Target ref</dt>
              <dd>{target.ref_name}</dd>
            </div>
          </dl>
        ) : (
          <p className="gf-specs__muted">尚无 server-issued ConstraintTargetBindingV1。</p>
        )}
        <button disabled={!canPublish} onClick={() => void publish()} type="button">
          发布权威约束
        </button>
        {published && (
          <section className="gf-specs__authority" data-authority="authoritative">
            <BadgeCheck aria-hidden="true" size={22} />
            <div>
              <p className="gf-specs__authority-label">Authority</p>
              <h2>已发布为权威约束</h2>
              <p>
                {published.ref_name} · revision {published.ref_value.revision}
              </p>
              {published.ref_transition_id && <code>{published.ref_transition_id}</code>}
              <a href={`/refs/${encodeURIComponent(published.ref_name)}/history`}>检查 ref 历史</a>
            </div>
          </section>
        )}
      </section>
    </div>
  );
}
