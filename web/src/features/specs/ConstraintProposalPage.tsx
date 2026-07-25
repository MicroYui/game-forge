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
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { createMutationIntent } from "../../api/csrf";
import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { EvidenceSections } from "../../components/evidence";
import { TechnicalDetails } from "../../components/identity";
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
import { profileKey } from "../execution-profiles";

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

const constraintWorkflowStatusLabels: Readonly<Record<string, string>> = {
  applied: "已发布",
  approved: "已批准",
  changes_requested: "需要修改",
  draft: "编辑中",
  pending_approval: "待审批",
  rejected: "未通过审批",
  rolled_back: "已回退",
  superseded: "已被新版替代",
  validated: "检查已通过",
  validating: "检查中",
  validation_failed: "检查未通过",
};

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
    return {
      ok: true,
      value: parsed as HumanConstraintRevisionRequest["constraints"],
    };
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
    return {
      kind: "unsafe",
      schemaId: artifactView.artifact.payload_schema_id ?? null,
    };
  }
  const payload = artifactView.payload;
  if (!isRecord(payload) || payload.evidence_schema_version !== "evidence-set@1") {
    return {
      kind: "unsafe",
      schemaId: artifactView.artifact.payload_schema_id,
    };
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
    return {
      kind: "unsafe",
      schemaId: artifactView.artifact.payload_schema_id,
    };
  }
  if (status === "passed" && !evidenceTargetMatches(payload.target_binding, constraintTarget(approval))) {
    return {
      kind: "unsafe",
      schemaId: artifactView.artifact.payload_schema_id,
    };
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
      return {
        kind: "unsafe",
        schemaId: artifactView.artifact.payload_schema_id,
      };
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
    return {
      kind: "unsafe",
      schemaId: artifactView.artifact.payload_schema_id ?? null,
    };
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
    return {
      kind: "unsafe",
      schemaId: artifactView.artifact.payload_schema_id,
    };
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
      return {
        kind: "unsafe",
        schemaId: artifactView.artifact.payload_schema_id,
      };
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
    return {
      kind: "unsafe",
      schemaId: artifactView.artifact.payload_schema_id ?? null,
    };
  }
  const payload = artifactView.payload;
  if (
    !isRecord(payload) ||
    payload.failure_schema_version !== "run-failure@1" ||
    typeof payload.run_id !== "string" ||
    typeof payload.cause_code !== "string" ||
    typeof payload.redacted_message !== "string"
  ) {
    return {
      kind: "unsafe",
      schemaId: artifactView.artifact.payload_schema_id,
    };
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
        <strong>
          确定性证据：
          {evidence.status === "passed" ? "validated" : evidence.status}
        </strong>
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
            编译证据 schema 无法安全解释：
            <code>{compileEvidence.schemaId ?? "unknown"}</code>。
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
  const [searchParams] = useSearchParams();
  const projectContextResult = useMemo(() => {
    const projectId = searchParams.get("project")?.trim() ?? "";
    if (!projectId) return { error: null, value: null };
    const refName = searchParams.get("constraintRef")?.trim() ?? "";
    const artifactId = searchParams.get("constraint")?.trim() || null;
    const revisionText = searchParams.get("constraintRevision")?.trim() || null;
    if (!refName || (artifactId === null) !== (revisionText === null)) {
      return {
        error: "项目规则发布位置或当前版本绑定不完整，请返回项目刷新后重试。",
        value: null,
      };
    }
    if (revisionText !== null && !/^[1-9]\d*$/u.test(revisionText)) {
      return { error: "项目当前规则版本号无效，请返回项目刷新后重试。", value: null };
    }
    return {
      error: null,
      value: {
        expectedRef:
          artifactId && revisionText ? { artifact_id: artifactId, revision: Number(revisionText) } : null,
        projectId,
        projectName: searchParams.get("projectName")?.trim() || projectId.replace(/^project:/u, ""),
        refName,
      },
    };
  }, [searchParams]);
  const projectContext = projectContextResult.value;
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
    const initialTarget = target
      ? { expectedRef: target.expected_ref ?? null, refName: target.ref_name }
      : projectContext
        ? { expectedRef: projectContext.expectedRef, refName: projectContext.refName }
        : null;
    setRationale(data.current.value.proposal.rationale);
    setRevisionConstraintsJson(JSON.stringify(data.current.value.proposal.constraints, null, 2));
    setRefName(initialTarget?.refName ?? "");
    setExpectedRefArtifactId(initialTarget?.expectedRef?.artifact_id ?? "");
    setExpectedRefRevision(initialTarget?.expectedRef ? String(initialTarget.expectedRef.revision) : "");
    setConfirmMissingRef(initialTarget !== null && initialTarget.expectedRef == null);
    setRefSelection(initialTarget);
    setFollowUpRefSelection(null);
    setFollowUpDraft(null);
    setRequirementId("");
  }, [projectContext, workflow.data]);

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
      setProfileState({
        ...current,
        error: normalizedError(error),
        loading: false,
      });
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
          description="正在读取规则内容、审批进度和可用的自动检查方案。"
          headingLevel={1}
          state="loading"
          title="正在读取规则草案"
        />
      </div>
    );
  }

  const loadError = workflow.error ?? profileQuery.error;
  if (loadError) {
    return (
      <div className="gf-page gf-specs">
        <header className="gf-page-header">
          <p className="gf-specs__kicker">游戏规则修改</p>
          <h1>规则修改草案</h1>
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
            description="规则草案读取失败；未展示底层异常内容。"
            state="error"
            title="无法读取规则草案"
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
          description="规则草案仍在准备中。"
          headingLevel={1}
          state="loading"
          title="正在读取规则草案"
        />
      </div>
    );
  }
  if (!data.binding || !data.approval) {
    return (
      <div className="gf-page gf-specs">
        <StatePanel
          description="服务器没有返回与这份草案匹配的审批记录；为避免审批错对象，相关操作已停止。"
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
  if (projectContextResult.error) {
    return (
      <div className="gf-page gf-specs">
        <StatePanel
          action={
            <a className="gf-secondary-button" href="/projects">
              返回游戏项目
            </a>
          }
          description={projectContextResult.error}
          headingLevel={1}
          state="error"
          title="项目规则绑定不完整"
        />
      </div>
    );
  }
  const itemTarget = constraintTarget(item);
  const projectTargetMismatch =
    projectContext !== null &&
    (baseArtifactId !== (projectContext.expectedRef?.artifact_id ?? null) ||
      (itemTarget !== null &&
        (itemTarget.ref_name !== projectContext.refName ||
          (itemTarget.expected_ref?.artifact_id ?? null) !==
            (projectContext.expectedRef?.artifact_id ?? null) ||
          (itemTarget.expected_ref?.revision ?? null) !== (projectContext.expectedRef?.revision ?? null))));
  if (projectTargetMismatch) {
    return (
      <div className="gf-page gf-specs">
        <StatePanel
          action={
            <a
              className="gf-secondary-button"
              href={`/projects/${encodeURIComponent(projectContext.projectId)}`}
            >
              返回项目刷新
            </a>
          }
          description="这份提案的基础规则或既有发布目标与项目当前版本不一致；页面没有自动改写或重定向。"
          headingLevel={1}
          state="error"
          title="提案不属于项目当前规则版本"
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
        const query = searchParams.toString();
        navigate(
          `/constraint-proposals/${encodeURIComponent(revised.artifact.artifact_id)}${query ? `?${query}` : ""}`,
          {
            replace: true,
          },
        );
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
      <nav aria-label="规则草案导航" className="gf-specs__back-nav">
        {projectContext ? (
          <a href={`/projects/${encodeURIComponent(projectContext.projectId)}`}>
            返回{projectContext.projectName}项目
          </a>
        ) : (
          <a href="/specs">返回内容工作台</a>
        )}
        <a href={`/artifacts/${encodeURIComponent(proposal.artifact.artifact_id)}`}>查看来源记录</a>
        <a href={`/approvals/${encodeURIComponent(binding.approval_id)}`}>查看审批进度</a>
        <a
          href={`/constraint-proposals/${encodeURIComponent(proposal.artifact.artifact_id)}${searchParams.size ? `?${searchParams.toString()}` : ""}`}
        >
          查看当前草案版本
        </a>
      </nav>

      <header className="gf-specs__hero gf-specs__hero--detail">
        <div>
          <p className="gf-specs__kicker">游戏规则修改</p>
          <h1>规则修改草案</h1>
          <p className="gf-specs__lede">
            核对规则内容、自动检查结果和审批进度；通过并发布前不会影响正式内容。
          </p>
        </div>
        <span className="gf-specs__status-mark">
          {isHuman ? <UserRound aria-hidden="true" size={17} /> : <Bot aria-hidden="true" size={17} />}
          {hasHumanAuthorRevision ? "人工已修订" : isHuman ? "人工初稿 · 仍需确认" : "AI 起草 · 必须人工确认"}
        </span>
      </header>

      {projectContext && (
        <aside className="gf-specs__project-context" role="note">
          <GitBranch aria-hidden="true" size={19} />
          <div>
            <strong>已绑定{projectContext.projectName}项目的规则发布位置</strong>
            <p>发布位置和当前规则版本已锁定；若项目状态变化，服务器会按 exact binding 拒绝旧请求。</p>
          </div>
        </aside>
      )}

      <dl className="gf-specs__facts" aria-label="规则草案概览">
        <div>
          <dt>规则数量</dt>
          <dd>{proposal.proposal.constraints.length} 条</dd>
        </div>
        <div>
          <dt>创建方式</dt>
          <dd>{isHuman ? "人工创建" : "AI 起草"}</dd>
        </div>
        <div>
          <dt>草案状态</dt>
          <dd>{constraintWorkflowStatusLabels[item.status] ?? item.status}</dd>
        </div>
        <div>
          <dt>基于已有规则</dt>
          <dd>{typeof baseArtifactId === "string" ? "是" : "否"}</dd>
        </div>
      </dl>
      <TechnicalDetails
        items={[
          {
            label: "Proposal Artifact ID",
            value: proposal.artifact.artifact_id,
          },
          { label: "Exact ETag", value: current.etag },
          { label: "Approval ID", value: binding.approval_id },
          {
            label: "Subject head revision",
            value: String(binding.subject_head_revision),
          },
          {
            label: "Workflow revision",
            value: String(binding.workflow_revision),
          },
          { label: "Subject digest", value: binding.subject_digest },
          {
            label: "Base constraint Artifact ID",
            value:
              typeof baseArtifactId === "string"
                ? baseArtifactId
                : (proposal.proposal.base_constraint_snapshot_id ?? "未绑定"),
          },
        ]}
        summary="查看规则草案技术信息"
      />

      {!isHuman && (
        <aside className="gf-specs__semantic-note" role="note">
          <Bot aria-hidden="true" size={20} />
          <div>
            <strong>AI 只负责起草</strong>
            <p>请先核对并保存一次人工修订，之后才能运行自动检查。</p>
          </div>
        </aside>
      )}

      {isHuman && !hasHumanAuthorRevision && !isHistoricalRevision && (
        <aside className="gf-specs__semantic-note" role="note">
          <UserRound aria-hidden="true" size={20} />
          <div>
            <strong>这份人工初稿仍需确认</strong>
            <p>请在下方核对内容并保存一次修订，之后才能运行自动检查。</p>
          </div>
        </aside>
      )}

      {!baseIsResolved && (
        <StatePanel
          description="系统无法唯一确认这份草案基于哪一版正式规则；为避免改错版本，修订与检查已停止。"
          state="error"
          title="找不到草案所基于的规则版本"
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
            description="操作失败；未展示底层异常内容。请重新读取最新状态后重试。"
            state="error"
            title="操作未完成"
          />
        ))}

      <section className="gf-specs__workspace-section" aria-labelledby="evidence-title">
        <header className="gf-specs__section-heading">
          <ShieldCheck aria-hidden="true" size={19} />
          <div>
            <h2 id="evidence-title">自动检查结果</h2>
            <p>这里只展示系统保存的确定性检查结论，不会把“运行结束”误当成“规则正确”。</p>
          </div>
        </header>
        <EvidenceStatus compileEvidence={compileEvidence} evidence={evidence} failure={failure} />
        {item.active_validation_run_id && (
          <a href={`/runs/${encodeURIComponent(item.active_validation_run_id)}`}>查看当前检查进度</a>
        )}
        {acceptedRunId && <a href={`/runs/${encodeURIComponent(acceptedRunId)}`}>查看检查过程</a>}
      </section>

      <section className="gf-specs__workspace-section" aria-labelledby="authority-binding-title">
        <header className="gf-specs__section-heading">
          <GitBranch aria-hidden="true" size={19} />
          <div>
            <h2 id="authority-binding-title">选择发布位置</h2>
            <p>明确选择这份规则将发布到哪里；系统不会根据名称自行猜测。</p>
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
                    ? `基于第 ${target.expected_ref.revision} 版`
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
                  disabled={
                    projectContext !== null || mutationPending || mutationLocked || followUpDraft !== null
                  }
                  name="follow-up-proposal-target"
                  onChange={setFollowUpRefSelection}
                  value={followUpRefSelection}
                />
                {followUpRefSelection &&
                  (followUpRefSelection.expectedRef === null ||
                    followUpRefSelection.refName !== target.ref_name) && (
                    <p className="gf-specs__field-hint" role="alert">
                      后续提案必须选择已有的 {target.ref_name}
                      ；不能把这次修改悄悄发布到别的 ref。
                    </p>
                  )}
              </div>
            )}
          </>
        ) : (
          <ConstraintRefBindingFields
            api={api}
            disabled={projectContext !== null || mutationPending || mutationLocked}
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
            <p>核对当前规则并写明修订原因；来源与规则语法会继续沿用，不需要重复配置。</p>
          </div>
        </header>
        {isHistoricalRevision && (
          <aside className="gf-specs__semantic-note" role="note">
            <AlertTriangle aria-hidden="true" size={20} />
            <div>
              <strong>这是保留的历史版本</strong>
              <p>它已被后续版本取代，只供审计和回看，不能再编辑或提交。</p>
              <a href="/specs">返回内容工作台查看当前草案</a>
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
              <p className="gf-specs__authority-label">当前使用中</p>
              <h2>已发布为权威约束</h2>
              <p>正式规则已更新为第 {published.ref_value.revision} 版</p>
              <TechnicalDetails
                items={[
                  { label: "Ref name", value: published.ref_name },
                  { label: "Artifact ID", value: published.ref_value.artifact_id },
                  ...(published.ref_transition_id
                    ? [{ label: "Ref transition ID", value: published.ref_transition_id }]
                    : []),
                ]}
                summary="查看发布结果技术信息"
              />
              <a
                href={`/constraints/${encodeURIComponent(published.ref_value.artifact_id)}?ref=${encodeURIComponent(published.ref_name)}`}
              >
                查看已发布的权威约束
              </a>
              <a href={`/refs/${encodeURIComponent(published.ref_name)}/history`}>查看正式规则版本历史</a>
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
