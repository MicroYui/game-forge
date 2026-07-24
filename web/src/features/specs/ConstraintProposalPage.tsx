import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  BadgeCheck,
  Bot,
  FilePenLine,
  GitBranch,
  PlayCircle,
  Send,
  ShieldCheck,
  UserRound,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { createMutationIntent } from "../../api/csrf";
import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { EvidenceSections } from "../../components/evidence";
import { CopyableText } from "../../components/tables";
import { ConfirmDialog, ProblemPanel, StatePanel } from "../../components/ui";
import { messages } from "../../i18n/zh-CN";
import {
  specWorkflowApi,
  type ApprovalView,
  type ArtifactPayloadView,
  type ConstraintProposalReadView,
  type ConstraintValidationAdmissionRequest,
  type ExecutionProfilePage,
  type HumanConstraintDraftRequest,
  type HumanConstraintRevisionRequest,
  type SpecWorkflowApi,
  type SubjectApprovalBindingView,
  type SubmitForApprovalRequest,
  type VersionedResource,
  type WorkflowApplyRequest,
  type WorkflowApplyResult,
} from "./api";
import { ConstraintRefBindingFields, type ConstraintRefSelection } from "./ConstraintRefBindingFields";
import { ConstraintSummaryList } from "./ConstraintSummary";
import "./specs.css";

export type ConstraintProposalApi = Pick<
  SpecWorkflowApi,
  | "getApproval"
  | "getApprovalBinding"
  | "getArtifactPayload"
  | "getConstraintProposal"
  | "getConstraintValidationCompilerBinding"
  | "listExecutionProfiles"
  | "listRefHistory"
  | "draftConstraint"
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

const REVISION_OPEN_STATUSES: ReadonlySet<ApprovalRecord["status"]> = new Set([
  "draft",
  "validating",
  "validation_failed",
  "validated",
  "pending_approval",
  "approved",
  "changes_requested",
  "rejected",
]);

interface WorkflowData {
  approval: VersionedResource<ApprovalView> | null;
  baseArtifactId: string | null | undefined;
  binding: SubjectApprovalBindingView | null;
  current: VersionedResource<ConstraintProposalReadView>;
  evidenceArtifact: ArtifactPayloadView | null;
  failureArtifact: ArtifactPayloadView | null;
  requirementArtifacts: ArtifactPayloadView[];
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
      requirements: {
        evidenceArtifactId: string | null;
        kind: string;
        reasonCode: string | null;
        requirementId: string;
        status: string;
        toolVersion: string;
      }[];
      runId: string;
      status: "passed" | "failed" | "unproven";
    };

type CompileEvidenceView =
  | { kind: "none" }
  | { kind: "unsafe"; schemaId: string | null }
  | {
      kind: "compile";
      overallStatus: "passed" | "failed" | "unproven";
      stages: {
        engineId: string | null;
        reasonCode: string | null;
        stage: "parse" | "typecheck" | "compile" | "differential" | "golden";
        stageId: string;
        status: "passed" | "failed" | "unproven" | "not_applicable";
      }[];
    };

type FailureView =
  | { kind: "none" }
  | { kind: "unsafe"; schemaId: string | null }
  | { causeCode: string; kind: "failure"; message: string; runId: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseRevisionConstraints(
  value: string,
): { ok: true; value: HumanConstraintRevisionRequest["constraints"] } | { ok: false } {
  try {
    const parsed: unknown = JSON.parse(value);
    if (!Array.isArray(parsed) || parsed.length === 0 || parsed.some((item) => !isRecord(item))) {
      return { ok: false };
    }
    return { ok: true, value: parsed as HumanConstraintRevisionRequest["constraints"] };
  } catch {
    return { ok: false };
  }
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
  const safeRequirements: Extract<EvidenceView, { kind: "evidence" }>["requirements"] = [];
  for (const requirement of requirements) {
    if (
      !isRecord(requirement) ||
      typeof requirement.requirement_id !== "string" ||
      typeof requirement.status !== "string" ||
      typeof requirement.kind !== "string" ||
      typeof requirement.tool_version !== "string" ||
      (requirement.reason_code !== null &&
        requirement.reason_code !== undefined &&
        typeof requirement.reason_code !== "string") ||
      (requirement.evidence_artifact_id !== null &&
        requirement.evidence_artifact_id !== undefined &&
        typeof requirement.evidence_artifact_id !== "string")
    ) {
      return { kind: "unsafe", schemaId: artifactView.artifact.payload_schema_id };
    }
    safeRequirements.push({
      evidenceArtifactId:
        typeof requirement.evidence_artifact_id === "string" ? requirement.evidence_artifact_id : null,
      kind: requirement.kind,
      reasonCode: typeof requirement.reason_code === "string" ? requirement.reason_code : null,
      requirementId: requirement.requirement_id,
      status: requirement.status,
      toolVersion: requirement.tool_version,
    });
  }
  return { kind: "evidence", requirements: safeRequirements, runId, status };
}

function parseCompileEvidence(
  artifacts: readonly ArtifactPayloadView[],
  evidence: EvidenceView,
  approval: ApprovalRecord,
): CompileEvidenceView {
  if (evidence.kind !== "evidence") return { kind: "none" };
  const compileRequirement = evidence.requirements.find((item) => item.kind === "constraint_compile");
  if (!compileRequirement?.evidenceArtifactId) return { kind: "none" };
  const artifactView = artifacts.find(
    (item) => item.artifact.artifact_id === compileRequirement.evidenceArtifactId,
  );
  if (!artifactView) return { kind: "none" };
  if (
    artifactView.artifact.kind !== "validation_evidence" ||
    artifactView.artifact.payload_schema_id !== "constraint-compile-evidence@1"
  ) {
    return { kind: "unsafe", schemaId: artifactView.artifact.payload_schema_id ?? null };
  }
  const payload = artifactView.payload;
  if (
    !isRecord(payload) ||
    payload.evidence_schema_version !== "constraint-compile-evidence@1" ||
    payload.proposal_artifact_id !== approval.subject_artifact_id ||
    !(
      payload.overall_status === "passed" ||
      payload.overall_status === "failed" ||
      payload.overall_status === "unproven"
    ) ||
    payload.overall_status !== compileRequirement.status ||
    !Array.isArray(payload.stages)
  ) {
    return { kind: "unsafe", schemaId: artifactView.artifact.payload_schema_id };
  }
  const stages: Extract<CompileEvidenceView, { kind: "compile" }>["stages"] = [];
  for (const value of payload.stages) {
    if (
      !isRecord(value) ||
      typeof value.stage_id !== "string" ||
      !(
        value.stage === "parse" ||
        value.stage === "typecheck" ||
        value.stage === "compile" ||
        value.stage === "differential" ||
        value.stage === "golden"
      ) ||
      !(
        value.status === "passed" ||
        value.status === "failed" ||
        value.status === "unproven" ||
        value.status === "not_applicable"
      ) ||
      (value.engine_id !== null && value.engine_id !== undefined && typeof value.engine_id !== "string") ||
      (value.reason_code !== null && value.reason_code !== undefined && typeof value.reason_code !== "string")
    ) {
      return { kind: "unsafe", schemaId: artifactView.artifact.payload_schema_id };
    }
    stages.push({
      engineId: typeof value.engine_id === "string" ? value.engine_id : null,
      reasonCode: typeof value.reason_code === "string" ? value.reason_code : null,
      stage: value.stage,
      stageId: value.stage_id,
      status: value.status,
    });
  }
  return { kind: "compile", overallStatus: payload.overall_status, stages };
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

function readablePermission(requirement: ApprovalRouteRequirement): string {
  if (requirement.required_permission.action === "approval.decide") return "作出审批决定";
  const action =
    requirement.required_permission.action === "approve" ? "批准" : requirement.required_permission.action;
  const resource =
    requirement.required_permission.resource_kind === "constraint_proposal"
      ? "约束提案"
      : requirement.required_permission.resource_kind;
  return `${action}${resource}`;
}

function readableDomain(domainId: string): string {
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

const stageLabels: Record<
  Extract<CompileEvidenceView, { kind: "compile" }>["stages"][number]["stage"],
  string
> = {
  compile: "生成检查计划",
  differential: "多引擎交叉验证",
  golden: "黄金用例回归",
  parse: "解析表达式",
  typecheck: "检查类型与作用范围",
};

function reasonExplanation(reasonCode: string | null): { description: string; nextStep: string } | null {
  if (reasonCode === null) return null;
  if (
    reasonCode === "selector_scope_ambiguous" ||
    reasonCode === "numeric_reference_witness_selector_unsupported" ||
    reasonCode === "z3_numeric_witness_selector_unsupported"
  ) {
    return {
      description: "检查器无法确定这条规则要作用于哪些游戏对象。",
      nextStep: "在规则中补充 scope，例如把适用对象设为 QUEST，再提交新的人工修订。",
    };
  }
  if (reasonCode === "empty_assert_expression" || reasonCode === "assert_parse_error") {
    return {
      description: "规则表达式为空或无法按当前 DSL 解析。",
      nextStep: "检查表达式拼写与运算符，然后提交新的人工修订。",
    };
  }
  if (reasonCode === "dsl_grammar_version_mismatch") {
    return {
      description: "规则声明的 DSL 版本与本次编译绑定不一致。",
      nextStep: "让规则与所选 base snapshot 使用同一 DSL grammar 后重新验证。",
    };
  }
  if (reasonCode === "execution_short_circuited") {
    return {
      description: "更早的检查阶段未通过，因此本阶段没有继续执行。",
      nextStep: "先处理上方第一条失败或未证明原因，再重新验证。",
    };
  }
  if (reasonCode === "engine_domain_not_applicable" || reasonCode === "golden_suite_absent") {
    return {
      description: "该检查维度不适用于当前候选，不会被冒充为通过。",
      nextStep: "无需单独修改；以其他 required requirement 的结论为准。",
    };
  }
  return {
    description: `检查器返回原因：${reasonCode}。`,
    nextStep: "打开证据 Run 查看完整技术链，修订后重新验证。",
  };
}

function EvidenceStatus({
  compileEvidence,
  evidence,
  failure,
}: {
  compileEvidence: CompileEvidenceView;
  evidence: EvidenceView;
  failure: FailureView;
}) {
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
    const problemStages =
      compileEvidence.kind === "compile"
        ? compileEvidence.stages.filter((stage) => stage.status === "failed" || stage.status === "unproven")
        : [];
    const firstProblem =
      problemStages.find((stage) => stage.reasonCode !== "execution_short_circuited") ?? problemStages[0];
    const guidance = reasonExplanation(firstProblem?.reasonCode ?? null);
    evidenceContent = (
      <div className="gf-specs__evidence-summary">
        <strong>确定性证据：{evidence.status === "passed" ? "validated" : evidence.status}</strong>
        <p>
          本次验证记录了 {evidence.requirements.length} 项检查；结论来自 EvidenceSet，而不是 Run
          的技术执行状态。
        </p>
        {guidance && firstProblem && (
          <aside className="gf-specs__validation-guidance" role="alert">
            <AlertTriangle aria-hidden="true" size={19} />
            <div>
              <strong>
                {firstProblem.engineId ? `${firstProblem.engineId} · ` : ""}
                {stageLabels[firstProblem.stage]}未证明
              </strong>
              <p>{guidance.description}</p>
              <p>
                <b>下一步：</b>
                {guidance.nextStep}
              </p>
              {firstProblem.reasonCode && <code>{firstProblem.reasonCode}</code>}
            </div>
          </aside>
        )}
        <ul className="gf-specs__requirement-list" aria-label="验证 requirement 结果">
          {evidence.requirements.map((requirement) => (
            <li key={requirement.requirementId}>
              <span className={`u-status u-status--${requirement.status === "passed" ? "ok" : "danger"}`}>
                {requirement.status}
              </span>
              <strong>
                {requirement.kind === "constraint_compile" ? "约束编译与交叉验证" : requirement.kind}
              </strong>
              <span>{requirement.toolVersion}</span>
              {requirement.reasonCode && <code>{requirement.reasonCode}</code>}
            </li>
          ))}
        </ul>
        {compileEvidence.kind === "compile" && (
          <details className="gf-specs__compile-stages">
            <summary>查看每个编译与检查引擎</summary>
            <ul>
              {compileEvidence.stages.map((stage) => (
                <li key={stage.stageId}>
                  <strong>{stageLabels[stage.stage]}</strong>
                  <span>{stage.engineId ?? "内置阶段"}</span>
                  <span>{stage.status}</span>
                  {stage.reasonCode && <code>{stage.reasonCode}</code>}
                </li>
              ))}
            </ul>
          </details>
        )}
        {compileEvidence.kind === "unsafe" && (
          <p role="alert">
            编译证据 schema 无法安全解释：<code>{compileEvidence.schemaId ?? "unknown"}</code>。
          </p>
        )}
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
  const navigate = useNavigate();
  const [currentArtifactId, setCurrentArtifactId] = useState(artifactId);
  useEffect(() => setCurrentArtifactId(artifactId), [artifactId]);
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
            requirementArtifacts: [],
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
      const evidence = parseEvidence(evidenceArtifact, approval.value.approval);
      const requirementArtifactIds =
        evidence.kind === "evidence"
          ? [
              ...new Set(
                evidence.requirements.flatMap((item) =>
                  item.evidenceArtifactId ? [item.evidenceArtifactId] : [],
                ),
              ),
            ]
          : [];
      const requirementArtifacts = await Promise.all(
        requirementArtifactIds.map((artifactId) => api.getArtifactPayload(artifactId)),
      );
      return {
        approval,
        baseArtifactId,
        binding,
        current,
        evidenceArtifact,
        failureArtifact,
        requirementArtifacts,
      };
    },
    queryKey: ["constraint-proposal", currentArtifactId],
    refetchInterval: (query) =>
      query.state.data?.approval?.value.approval.status === "validating" ? 500 : false,
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
  const [refSelection, setRefSelection] = useState<ConstraintRefSelection | null>(null);
  const [followUpRefSelection, setFollowUpRefSelection] = useState<ConstraintRefSelection | null>(null);
  const [rationale, setRationale] = useState("");
  const [revisionConstraintsJson, setRevisionConstraintsJson] = useState("");
  const [compilerKey, setCompilerKey] = useState("");
  const [validationKey, setValidationKey] = useState("");
  const [requirementId, setRequirementId] = useState("");
  const [mutationError, setMutationError] = useState<Error | null>(null);
  const [mutationPending, setMutationPending] = useState(false);
  const [mutationLocked, setMutationLocked] = useState(false);
  const [acceptedRunId, setAcceptedRunId] = useState<string | null>(null);
  const [published, setPublished] = useState<WorkflowApplyResult | null>(null);
  const [followUpDraft, setFollowUpDraft] = useState<ConstraintProposalReadView | null>(null);
  const [publishConfirmOpen, setPublishConfirmOpen] = useState(false);
  const initializedProposalArtifactId = useRef<string | null>(null);

  useEffect(() => {
    const data = workflow.data;
    if (!data?.approval) return;
    const proposalArtifactId = data.current.value.artifact.artifact_id;
    if (initializedProposalArtifactId.current === proposalArtifactId) return;
    initializedProposalArtifactId.current = proposalArtifactId;
    const target = constraintTarget(data.approval.value.approval);
    setRationale(data.current.value.proposal.rationale);
    setRevisionConstraintsJson(JSON.stringify(data.current.value.proposal.constraints, null, 2));
    setRefName(target?.ref_name ?? "");
    setExpectedRefArtifactId(target?.expected_ref?.artifact_id ?? "");
    setExpectedRefRevision(target?.expected_ref ? String(target.expected_ref.revision) : "");
    setConfirmMissingRef(target !== null && target.expected_ref == null);
    setRefSelection(target ? { expectedRef: target.expected_ref ?? null, refName: target.ref_name } : null);
    setFollowUpRefSelection(null);
    setFollowUpDraft(null);
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
      const normalized = normalizedError(error);
      setMutationError(normalized);
      setMutationLocked(!(normalized instanceof ApiProblemError && normalized.problem.status === 422));
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
  const compileEvidence = parseCompileEvidence(data.requirementArtifacts, evidence, item);
  const failure = parseFailure(data.failureArtifact, item);
  const refValue = expectedRef(expectedRefArtifactId, expectedRefRevision, confirmMissingRef);
  const refIsValid = refName.trim().length > 0 && refValue !== undefined;
  const baseIsResolved =
    proposal.proposal.base_constraint_snapshot_id == null || typeof baseArtifactId === "string";
  const isHuman = proposal.proposal.produced_by === "human";
  const hasHumanAuthorRevision =
    isHuman &&
    proposal.proposal.producer_run_id === null &&
    item.proposer.principal_kind === "human" &&
    proposal.proposal.revision > 1 &&
    proposal.proposal.supersedes_artifact_id !== null;
  const isPublishedTerminal = item.status === "applied" || item.status === "rolled_back";
  const isHistoricalRevision = item.status === "superseded";
  const parsedRevisionConstraints = revisionConstraintsJson.trim()
    ? parseRevisionConstraints(revisionConstraintsJson)
    : null;
  const evidencePassed = evidence.kind === "evidence" && evidence.status === "passed";
  const canValidate =
    hasHumanAuthorRevision &&
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
    REVISION_OPEN_STATUSES.has(item.status) &&
    baseIsResolved &&
    refIsValid &&
    rationale.trim().length > 0 &&
    parsedRevisionConstraints?.ok === true &&
    !mutationLocked &&
    !mutationPending;
  const canCreateFollowUp =
    isPublishedTerminal &&
    binding.is_current_head &&
    target !== null &&
    followUpDraft === null &&
    followUpRefSelection?.expectedRef != null &&
    followUpRefSelection.refName === target.ref_name &&
    rationale.trim().length > 0 &&
    parsedRevisionConstraints?.ok === true &&
    !mutationLocked &&
    !mutationPending;
  const canSubmit =
    hasHumanAuthorRevision &&
    binding.is_current_head &&
    item.status === "validated" &&
    evidencePassed &&
    selectedRequirement !== undefined &&
    !mutationLocked &&
    !mutationPending;
  const canPublish =
    hasHumanAuthorRevision &&
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

  function updateRefSelection(selection: ConstraintRefSelection | null) {
    setRefSelection(selection);
    setRefName(selection?.refName ?? "");
    setExpectedRefArtifactId(selection?.expectedRef?.artifact_id ?? "");
    setExpectedRefRevision(selection?.expectedRef ? String(selection.expectedRef.revision) : "");
    setConfirmMissingRef(selection !== null && selection.expectedRef === null);
  }

  async function revise() {
    if (!canRevise || refValue === undefined || parsedRevisionConstraints?.ok !== true) return;
    const request: HumanConstraintRevisionRequest = {
      approval_id: binding.approval_id,
      base_constraint_snapshot_artifact_id: baseArtifactId ?? null,
      constraints: parsedRevisionConstraints.value,
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
        navigate(`/constraint-proposals/${encodeURIComponent(revised.artifact.artifact_id)}`, {
          replace: true,
        });
      },
    );
  }

  async function createFollowUp() {
    if (
      !canCreateFollowUp ||
      target === null ||
      followUpRefSelection?.expectedRef == null ||
      parsedRevisionConstraints?.ok !== true
    ) {
      return;
    }
    const request: HumanConstraintDraftRequest = {
      base_constraint_snapshot_artifact_id: followUpRefSelection.expectedRef.artifact_id,
      constraints: parsedRevisionConstraints.value,
      domain_scope: proposal.proposal.domain_scope,
      dsl_grammar_version: proposal.proposal.dsl_grammar_version,
      expected_ref: followUpRefSelection.expectedRef,
      rationale: rationale.trim(),
      ref_name: followUpRefSelection.refName,
      request_schema_version: "human-constraint-draft-request@1",
      source_artifact_ids: proposal.proposal.source_bindings.map((source) => source.source_artifact_id),
    };
    await runMutation(
      () => api.draftConstraint(request, createMutationIntent()),
      async (draft) => {
        setFollowUpDraft(draft);
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
          {hasHumanAuthorRevision
            ? "Human 修订候选"
            : isHuman
              ? "Human 初稿 · 仍需确认修订"
              : "Agent 候选 · 必须由 Human 修订"}
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

      {isHuman && !hasHumanAuthorRevision && !isHistoricalRevision && (
        <aside className="gf-specs__semantic-note" role="note">
          <UserRound aria-hidden="true" size={20} />
          <div>
            <strong>这份 Human 初稿还不是可验证 revision</strong>
            <p>请在下方复核内容并提交一次人工修订；生成不可变的 superseding revision 后才能验证。</p>
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
            {mutationLocked ? (
              <button
                className="gf-secondary-button"
                disabled={mutationPending}
                onClick={() => void reloadServerState()}
                type="button"
              >
                重新读取服务器状态
              </button>
            ) : (
              <p className="gf-specs__muted" role="note">
                请求未写入服务器；修正上面的表单后可以直接重试。
              </p>
            )}
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
        <EvidenceStatus compileEvidence={compileEvidence} evidence={evidence} failure={failure} />
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
        {target && item.status !== "draft" ? (
          <>
            <div className="gf-specs__resolved-ref">
              <GitBranch aria-hidden="true" size={18} />
              <div>
                <strong>{target.ref_name}</strong>
                <span>
                  {target.expected_ref
                    ? `冻结于 revision ${target.expected_ref.revision}`
                    : item.status === "applied" || item.status === "rolled_back"
                      ? "发布时以新 ref 创建；这里显示的是历史前提"
                      : "冻结为新 ref（发布前必须仍不存在）"}
                </span>
                <details>
                  <summary>查看 exact target binding</summary>
                  <pre tabIndex={0}>{JSON.stringify(target, null, 2)}</pre>
                </details>
              </div>
            </div>
            {isPublishedTerminal && binding.is_current_head && (
              <div className="gf-stack">
                <aside className="gf-specs__semantic-note" role="note">
                  <AlertTriangle aria-hidden="true" size={20} />
                  <div>
                    <strong>已发布记录不可原地修订</strong>
                    <p>
                      已发布 revision 必须保持不可变。请选择 {target.ref_name}{" "}
                      的当前版本，系统会基于当前权威快照创建一条新的后续提案。
                    </p>
                  </div>
                </aside>
                <ConstraintRefBindingFields
                  api={api}
                  disabled={mutationPending || mutationLocked || followUpDraft !== null}
                  name="follow-up-proposal-target"
                  onChange={setFollowUpRefSelection}
                  value={followUpRefSelection}
                />
                {followUpRefSelection &&
                  (followUpRefSelection.expectedRef === null ||
                    followUpRefSelection.refName !== target.ref_name) && (
                    <p className="gf-specs__field-hint" role="alert">
                      后续提案必须选择已有的 {target.ref_name}；不能把这次修改悄悄发布到别的 ref。
                    </p>
                  )}
              </div>
            )}
          </>
        ) : (
          <ConstraintRefBindingFields
            api={api}
            disabled={mutationPending || mutationLocked}
            name="proposal-target"
            onChange={updateRefSelection}
            value={refSelection}
          />
        )}
      </section>

      <section className="gf-specs__workspace-section" aria-labelledby="human-revision-title">
        <header className="gf-specs__section-heading">
          <FilePenLine aria-hidden="true" size={19} />
          <div>
            <h2 id="human-revision-title">人工接管与修订</h2>
            <p>
              审阅并编辑当前 typed constraints；source bindings、base 与 DSL grammar 继续沿用本 revision。
            </p>
          </div>
        </header>
        {isHistoricalRevision && (
          <aside className="gf-specs__semantic-note" role="note">
            <AlertTriangle aria-hidden="true" size={20} />
            <div>
              <strong>这是已保留的历史 revision</strong>
              <p>它已被后续 revision 取代，只供审计和回看，不能再编辑或提交。</p>
              <a href="/specs">返回规格工作台查看当前候选</a>
            </div>
          </aside>
        )}
        <form
          className="gf-form"
          onSubmit={(event) => {
            event.preventDefault();
            if (isPublishedTerminal) {
              void createFollowUp();
            } else {
              void revise();
            }
          }}
        >
          <div className="gf-specs__constraint-review">
            <h3>当前候选规则</h3>
            <ConstraintSummaryList values={proposal.proposal.constraints} />
            <details>
              <summary>查看当前 immutable typed constraints JSON</summary>
              <pre aria-label="当前 immutable typed constraints" tabIndex={0}>
                {JSON.stringify(proposal.proposal.constraints, null, 2)}
              </pre>
            </details>
          </div>
          <label>
            修订后的 typed constraints JSON
            <textarea
              aria-describedby="revision-constraints-hint"
              className="gf-specs__code-input"
              disabled={isHistoricalRevision}
              onChange={(event) => setRevisionConstraintsJson(event.target.value)}
              rows={14}
              value={revisionConstraintsJson}
            />
          </label>
          <p className="gf-specs__field-hint" id="revision-constraints-hint">
            {parsedRevisionConstraints === null
              ? "输入至少一条 constraint 的 JSON array。"
              : parsedRevisionConstraints.ok
                ? "当前 JSON array 可提交；字段与语义仍由 server typed contract 和确定性验证裁决。"
                : "需要 JSON array，且每个条目必须是 object。"}
          </p>
          <label>
            修订说明
            <textarea
              disabled={isHistoricalRevision}
              onChange={(event) => setRationale(event.target.value)}
              rows={4}
              value={rationale}
            />
          </label>
          {isPublishedTerminal ? (
            <button disabled={!canCreateFollowUp} type="submit">
              创建后续提案草稿
            </button>
          ) : (
            <button disabled={!canRevise} type="submit">
              提交人工修订
            </button>
          )}
          {followUpDraft && (
            <div className="gf-specs__entry-success" role="status">
              <strong>后续提案草稿已创建</strong>
              <p>下一步请打开新提案，再确认并提交一次人工修订；之后才能开始确定性验证。</p>
              <a href={`/constraint-proposals/${encodeURIComponent(followUpDraft.artifact.artifact_id)}`}>
                打开后续提案并确认人工修订
              </a>
            </div>
          )}
        </form>
      </section>

      <section className="gf-specs__workspace-section" aria-labelledby="validation-title">
        <header className="gf-specs__section-heading">
          <PlayCircle aria-hidden="true" size={19} />
          <div>
            <h2 id="validation-title">编译与确定性验证</h2>
            <p>选择本次使用的约束编译器和验证方案；系统不会悄悄采用其他方案。</p>
          </div>
        </header>
        <div className="gf-form">
          <label>
            约束编译器
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
            验证方案
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
            <h2 id="approval-title">交给另一位同事审批</h2>
            <p>选择由服务器冻结的审批职责，核对需要的角色和权限后提交。</p>
          </div>
        </header>
        <div className="gf-form">
          <p>这里不会改变审批规则，只是让你确认系统将把提案交给谁审。</p>
          <label>
            审批职责
            <select onChange={(event) => setRequirementId(event.target.value)} value={requirementId}>
              <option value="">请选择审批职责</option>
              {item.requirements.map((requirement) => (
                <option key={requirement.requirement_id} value={requirement.requirement_id}>
                  {messages.roles[requirement.route_role]} · 至少 {requirement.min_approvals} 人确认
                </option>
              ))}
            </select>
          </label>
          {selectedRequirement && (
            <dl className="gf-specs__facts">
              <div>
                <dt>负责角色</dt>
                <dd>{messages.roles[selectedRequirement.route_role]}</dd>
              </div>
              <div>
                <dt>需要操作</dt>
                <dd>{readablePermission(selectedRequirement)}</dd>
              </div>
              <div>
                <dt>覆盖内容域</dt>
                <dd>{selectedRequirement.domain_scope.domain_ids.map(readableDomain).join("、")}</dd>
              </div>
            </dl>
          )}
          {selectedRequirement && (
            <details>
              <summary>查看审批路由技术信息</summary>
              <div className="gf-stack">
                <CopyableText copyLabel="复制 requirement ID" value={selectedRequirement.requirement_id} />
                <CopyableText copyLabel="复制 route role" value={selectedRequirement.route_role} />
                <CopyableText
                  copyLabel="复制 required permission"
                  value={permissionLabel(selectedRequirement)}
                />
              </div>
            </details>
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
            <h2 id="publish-title">发布权威约束</h2>
            <p>审批通过后，把已核对的候选版本发布到冻结的约束 ref。</p>
          </div>
        </header>
        {target ? (
          <dl className="gf-specs__facts">
            <div>
              <dt>发布位置</dt>
              <dd>
                {target.ref_name}
                <a
                  href={`/constraints/${encodeURIComponent(target.target_artifact_id)}?ref=${encodeURIComponent(target.ref_name)}`}
                >
                  检查候选快照内容与 ref 状态
                </a>
              </dd>
            </div>
            <div>
              <dt>发布方式</dt>
              <dd>
                {target.expected_ref ? `更新 revision ${target.expected_ref.revision}` : "创建新的约束 ref"}
              </dd>
            </div>
            <div className="gf-specs__fact-wide">
              <details>
                <summary>查看候选版本技术身份</summary>
                <CopyableText copyLabel="复制 Candidate Artifact ID" value={target.target_artifact_id} />
                <CopyableText copyLabel="复制 Target snapshot ID" value={target.target_snapshot_id} />
              </details>
            </div>
          </dl>
        ) : (
          <p className="gf-specs__muted">尚无 server-issued ConstraintTargetBindingV1。</p>
        )}
        <button disabled={!canPublish} onClick={() => setPublishConfirmOpen(true)} type="button">
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
              <a
                href={`/constraints/${encodeURIComponent(published.ref_value.artifact_id)}?ref=${encodeURIComponent(published.ref_name)}`}
              >
                查看已发布的权威约束
              </a>
              <a href={`/refs/${encodeURIComponent(published.ref_name)}/history`}>检查 ref 历史</a>
            </div>
          </section>
        )}
      </section>
      <ConfirmDialog
        confirmLabel="确认发布"
        description={
          target
            ? `将 ${proposal.proposal.constraints.length} 条已验证并获批的约束发布到 ${target.ref_name}。这会追加一条不可变 ref transition。`
            : "发布目标尚未形成，不能继续。"
        }
        onCancel={() => setPublishConfirmOpen(false)}
        onConfirm={() => {
          setPublishConfirmOpen(false);
          void publish();
        }}
        open={publishConfirmOpen}
        title="确认发布权威约束"
      />
    </div>
  );
}
