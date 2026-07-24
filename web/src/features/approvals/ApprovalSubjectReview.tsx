import { AlertTriangle, ArrowRight, FileDiff, GitBranch, RotateCcw, ShieldCheck } from "lucide-react";

import type { components } from "../../api/generated/openapi";
import { CopyableText } from "../../components/tables";
import { ConstraintSummaryList } from "../specs/ConstraintSummary";
import type {
  ApprovalArtifactPayload,
  ApprovalConstraintProposal,
  ApprovalPatch,
  ApprovalRollbackRequest,
  ApprovalsApi,
  ApprovalViewData,
} from "./api";

type ApprovalRecord = ApprovalViewData["approval"];
type ApprovalTarget = NonNullable<ApprovalRecord["target_binding"]>;
type RollbackTarget = Extract<ApprovalTarget, { subject_kind: "rollback_request" }>;
type TypedOp = components["schemas"]["TypedOp"];

interface EvidenceRequirement {
  evidenceArtifactId: string | null;
  kind: string;
  reasonCode: string | null;
  requirementId: string;
  status: string;
  toolVersion: string;
}

interface EvidenceReview {
  artifactId: string;
  overallStatus: "passed" | "failed" | "unproven";
  requirements: EvidenceRequirement[];
  runId: string;
}

interface PatchTargetGraph {
  entities: ReadonlyMap<string, Record<string, unknown>>;
  entityNames: ReadonlyMap<string, string>;
  relations: ReadonlyMap<string, Record<string, unknown>>;
}

export type ApprovalSubjectReviewData =
  | {
      evidence: EvidenceReview;
      kind: "patch";
      opSummaries: ReadonlyMap<string, string>;
      subject: ApprovalPatch;
      target: ApprovalTarget;
    }
  | {
      evidence: EvidenceReview;
      kind: "constraint_proposal";
      subject: ApprovalConstraintProposal;
      target: ApprovalTarget;
    }
  | {
      currentArtifact: ApprovalArtifactPayload;
      evidence: EvidenceReview;
      kind: "rollback_request";
      subject: ApprovalRollbackRequest;
      target: RollbackTarget;
      targetArtifact: ApprovalArtifactPayload;
    };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function canonicalJson(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonicalJson);
  if (!isRecord(value)) return value;
  return Object.fromEntries(
    Object.entries(value)
      .filter(([, entry]) => entry !== undefined)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, entry]) => [key, canonicalJson(entry)]),
  );
}

function sameTargetBinding(left: unknown, right: unknown): boolean {
  if (!isRecord(left) || !isRecord(right)) return false;
  const normalize = (value: Record<string, unknown>) => ({
    ...value,
    expected_ref: value.expected_ref ?? null,
    target_snapshot_id: value.target_snapshot_id ?? null,
  });
  return JSON.stringify(canonicalJson(normalize(left))) === JSON.stringify(canonicalJson(normalize(right)));
}

function sameCanonicalValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(canonicalJson(left)) === JSON.stringify(canonicalJson(right));
}

function isDigest(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function verifySubjectIdentity(
  approval: ApprovalRecord,
  subject: ApprovalPatch | ApprovalConstraintProposal | ApprovalRollbackRequest,
  expectedKind: ApprovalRecord["subject_kind"],
) {
  if (
    approval.subject_kind !== expectedKind ||
    subject.artifact.kind !== expectedKind ||
    subject.artifact.artifact_id !== approval.subject_artifact_id ||
    subject.artifact.payload_hash !== approval.subject_digest ||
    subject.workflow_revision !== approval.workflow_revision ||
    subject.approval_status !== approval.status
  ) {
    throw new Error("受审内容与 ApprovalItem 的 exact identity 不一致。");
  }
  const contentRevision =
    expectedKind === "patch"
      ? (subject as ApprovalPatch).patch.revision
      : expectedKind === "constraint_proposal"
        ? (subject as ApprovalConstraintProposal).proposal.revision
        : approval.subject_revision;
  if (contentRevision !== approval.subject_revision) {
    throw new Error("受审内容 revision 与 ApprovalItem 不一致。");
  }
}

function parseEvidence(artifact: ApprovalArtifactPayload, approval: ApprovalRecord): EvidenceReview {
  if (
    artifact.artifact.kind !== "validation_evidence" ||
    artifact.artifact.artifact_id !== approval.evidence_set_artifact_id ||
    artifact.artifact.payload_schema_id !== "evidence-set@1" ||
    !isRecord(artifact.payload)
  ) {
    throw new Error("审批证据不是 exact evidence-set@1。");
  }
  const payload = artifact.payload;
  if (
    payload.evidence_schema_version !== "evidence-set@1" ||
    payload.subject_artifact_id !== approval.subject_artifact_id ||
    payload.subject_digest !== approval.subject_digest ||
    !(
      payload.overall_status === "passed" ||
      payload.overall_status === "failed" ||
      payload.overall_status === "unproven"
    ) ||
    typeof payload.validation_run_id !== "string" ||
    !Array.isArray(payload.requirements) ||
    !sameTargetBinding(payload.target_binding, approval.target_binding)
  ) {
    throw new Error("EvidenceSet 未与受审内容和冻结目标闭合。");
  }
  const requirements: EvidenceRequirement[] = payload.requirements.map((value) => {
    if (
      !isRecord(value) ||
      typeof value.requirement_id !== "string" ||
      typeof value.kind !== "string" ||
      typeof value.status !== "string" ||
      typeof value.tool_version !== "string" ||
      (value.reason_code != null && typeof value.reason_code !== "string") ||
      (value.evidence_artifact_id != null && typeof value.evidence_artifact_id !== "string")
    ) {
      throw new Error("EvidenceSet requirement 无法安全解释。");
    }
    return {
      evidenceArtifactId: typeof value.evidence_artifact_id === "string" ? value.evidence_artifact_id : null,
      kind: value.kind,
      reasonCode: typeof value.reason_code === "string" ? value.reason_code : null,
      requirementId: value.requirement_id,
      status: value.status,
      toolVersion: value.tool_version,
    };
  });
  if (payload.overall_status !== "passed") {
    throw new Error("当前 EvidenceSet 不是 passed，不能作为批准依据。");
  }
  return {
    artifactId: artifact.artifact.artifact_id,
    overallStatus: payload.overall_status,
    requirements,
    runId: payload.validation_run_id,
  };
}

function parsePatchTargetGraph(artifact: ApprovalArtifactPayload, target: ApprovalTarget): PatchTargetGraph {
  if (
    target.target_artifact_kind !== "ir_snapshot" ||
    artifact.artifact.artifact_id !== target.target_artifact_id ||
    artifact.artifact.kind !== target.target_artifact_kind ||
    artifact.artifact.payload_hash !== target.target_digest ||
    artifact.artifact.payload_schema_id !== "ir-core@1" ||
    (target.target_snapshot_id != null &&
      artifact.artifact.version_tuple.ir_snapshot_id !== target.target_snapshot_id) ||
    !isRecord(artifact.payload) ||
    !isRecord(artifact.payload.entities) ||
    !isRecord(artifact.payload.relations)
  ) {
    throw new Error("Patch 目标快照与冻结 target binding 不一致。");
  }
  const entities = new Map<string, Record<string, unknown>>();
  const names = new Map<string, string>();
  for (const [id, value] of Object.entries(artifact.payload.entities)) {
    if (!isRecord(value)) throw new Error("Patch 目标快照包含无法解释的实体。");
    entities.set(id, value);
    const name = entityName(value);
    if (name) names.set(id, name);
  }
  const relations = new Map<string, Record<string, unknown>>();
  for (const [id, value] of Object.entries(artifact.payload.relations)) {
    if (!isRecord(value)) throw new Error("Patch 目标快照包含无法解释的关系。");
    relations.set(id, value);
  }
  return { entities, entityNames: names, relations };
}

function snapshotIdForTarget(
  kind: RollbackTarget["target_artifact_kind"],
  artifact: ApprovalArtifactPayload["artifact"],
): string | null {
  if (kind === "ir_snapshot") return artifact.version_tuple.ir_snapshot_id ?? null;
  if (kind === "constraint_snapshot") return artifact.version_tuple.constraint_snapshot_id ?? null;
  return null;
}

function verifyRollbackArtifact(
  artifact: ApprovalArtifactPayload,
  expectedArtifactId: string,
  expectedKind: RollbackTarget["target_artifact_kind"],
  expectedDigest?: string,
) {
  if (
    artifact.view_schema_version !== "artifact-payload-view@1" ||
    artifact.resource_revision !== 1 ||
    artifact.artifact.summary_schema_version !== "artifact-summary@1" ||
    artifact.artifact.lineage_schema_version !== "lineage@2" ||
    artifact.artifact.artifact_id !== expectedArtifactId ||
    artifact.artifact.kind !== expectedKind ||
    !isDigest(artifact.artifact.payload_hash) ||
    (expectedDigest !== undefined && artifact.artifact.payload_hash !== expectedDigest) ||
    typeof artifact.artifact.payload_schema_id !== "string" ||
    artifact.artifact.payload_schema_id.length === 0
  ) {
    throw new Error("回滚内容 Artifact 与冻结身份不一致。");
  }
}

function verifyRollbackContent(
  subject: ApprovalRollbackRequest,
  target: ApprovalTarget,
  currentArtifact: ApprovalArtifactPayload,
  targetArtifact: ApprovalArtifactPayload,
): RollbackTarget {
  if (target.subject_kind !== "rollback_request") {
    throw new Error("Rollback Approval 缺少冻结 rollback target binding。");
  }
  const request = subject.request;
  const expectedParents = new Set([request.expected_current_ref.artifact_id, request.target_artifact_id]);
  const actualParents = new Set(subject.artifact.parent_artifact_ids);
  if (
    subject.view_schema_version !== "rollback-request-read-view@1" ||
    subject.artifact.summary_schema_version !== "artifact-summary@1" ||
    subject.artifact.lineage_schema_version !== "lineage@2" ||
    subject.artifact.payload_schema_id !== "rollback-request@1" ||
    request.rollback_schema_version !== "rollback-request@1" ||
    target.binding_schema_version !== "approval-target-binding@1" ||
    target.ref_name !== request.ref_name ||
    target.expected_ref.artifact_id !== request.expected_current_ref.artifact_id ||
    target.expected_ref.revision !== request.expected_current_ref.revision ||
    target.target_artifact_id !== request.target_artifact_id ||
    !sameCanonicalValue(target.rollback_profile_binding, request.rollback_profile_binding) ||
    target.rollback_profile_binding.expected_profile_kind !== "rollback" ||
    target.rollback_profile_binding.field_path !== "/params/rollback_profile" ||
    !isDigest(target.rollback_profile_binding.catalog_digest) ||
    !isDigest(target.rollback_profile_binding.profile_payload_hash) ||
    !isDigest(target.target_digest) ||
    expectedParents.size !== 2 ||
    actualParents.size !== expectedParents.size ||
    subject.artifact.parent_artifact_ids.length !== expectedParents.size ||
    [...expectedParents].some((artifactId) => !actualParents.has(artifactId))
  ) {
    throw new Error("RollbackRequest 未与冻结 target binding 和 lineage 闭合。");
  }

  verifyRollbackArtifact(currentArtifact, target.expected_ref.artifact_id, target.target_artifact_kind);
  verifyRollbackArtifact(
    targetArtifact,
    target.target_artifact_id,
    target.target_artifact_kind,
    target.target_digest,
  );
  const targetSnapshotId = snapshotIdForTarget(target.target_artifact_kind, targetArtifact.artifact);
  const subjectSnapshotId = snapshotIdForTarget(target.target_artifact_kind, subject.artifact);
  if ((target.target_snapshot_id ?? null) !== targetSnapshotId || subjectSnapshotId !== targetSnapshotId) {
    throw new Error("回滚目标 Snapshot identity 未与 VersionTuple 闭合。");
  }
  return target;
}

export async function loadApprovalSubjectReview(
  api: ApprovalsApi,
  view: ApprovalViewData,
): Promise<ApprovalSubjectReviewData> {
  const approval = view.approval;
  if (!approval.evidence_set_artifact_id || !approval.target_binding) {
    throw new Error("ApprovalItem 缺少冻结目标或 EvidenceSet，审批已停止。");
  }
  const [subject, evidenceArtifact] = await Promise.all([
    approval.subject_kind === "patch"
      ? api.getPatch(approval.subject_artifact_id)
      : approval.subject_kind === "constraint_proposal"
        ? api.getConstraintProposal(approval.subject_artifact_id)
        : api.getRollbackRequest(approval.subject_artifact_id),
    api.getArtifactPayload(approval.evidence_set_artifact_id),
  ]);
  verifySubjectIdentity(approval, subject, approval.subject_kind);
  const evidence = parseEvidence(evidenceArtifact, approval);
  if (approval.subject_kind === "patch") {
    const targetArtifact = await api.getArtifactPayload(approval.target_binding.target_artifact_id);
    const patchSubject = subject as ApprovalPatch;
    const targetGraph = parsePatchTargetGraph(targetArtifact, approval.target_binding);
    return {
      evidence,
      kind: "patch",
      opSummaries: buildPatchOpSummaries(patchSubject.patch.ops, targetGraph),
      subject: patchSubject,
      target: approval.target_binding,
    };
  }
  if (approval.subject_kind === "rollback_request") {
    if (approval.target_binding.subject_kind !== "rollback_request") {
      throw new Error("Rollback Approval 的 target binding 类型不一致。");
    }
    const rollbackSubject = subject as ApprovalRollbackRequest;
    const [currentArtifact, targetArtifact] = await Promise.all([
      api.getArtifactPayload(approval.target_binding.expected_ref.artifact_id),
      api.getArtifactPayload(approval.target_binding.target_artifact_id),
    ]);
    const target = verifyRollbackContent(
      rollbackSubject,
      approval.target_binding,
      currentArtifact,
      targetArtifact,
    );
    return {
      currentArtifact,
      evidence,
      kind: "rollback_request",
      subject: rollbackSubject,
      target,
      targetArtifact,
    };
  }
  return {
    evidence,
    kind: "constraint_proposal",
    subject,
    target: approval.target_binding,
  } as ApprovalSubjectReviewData;
}

const opLabels: Record<TypedOp["op"], string> = {
  add_entity: "新增实体",
  add_relation: "新增关系",
  delete_entity: "删除实体",
  delete_relation: "删除关系",
  replace_subgraph: "替换子图",
  set_entity_attr: "修改实体字段",
  set_relation_attr: "修改关系字段",
};

function recordText(value: Record<string, unknown>, key: string): string | null {
  const candidate = value[key];
  return typeof candidate === "string" && candidate.trim() ? candidate.trim() : null;
}

function entityName(value: unknown): string | null {
  if (!isRecord(value) || !isRecord(value.attrs)) return null;
  return (
    recordText(value.attrs, "display_name") ??
    recordText(value.attrs, "name") ??
    recordText(value.attrs, "title") ??
    recordText(value.attrs, "label")
  );
}

type RollbackChangeKind = "changed" | "deleted" | "restored";

interface RollbackContentChange {
  after?: unknown;
  before?: unknown;
  kind: RollbackChangeKind;
  technicalPath: string;
  title: string;
}

function hasOwn(record: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function entityTitle(value: unknown): string {
  const type = isRecord(value) ? recordText(value, "type") : null;
  const name = entityName(value);
  return name ? `${type ?? "实体"}「${name}」` : `${type ?? "实体"}「未命名对象」`;
}

function entityNamesFromPayload(payload: unknown): ReadonlyMap<string, string> {
  const names = new Map<string, string>();
  if (!isRecord(payload) || !isRecord(payload.entities)) return names;
  for (const [id, value] of Object.entries(payload.entities)) {
    const name = entityName(value);
    if (name) names.set(id, name);
  }
  return names;
}

function relationTitle(value: unknown, names: ReadonlyMap<string, string>): string {
  if (!isRecord(value)) return "关系（未命名）";
  const attrs = isRecord(value.attrs) ? value.attrs : {};
  const label = recordText(attrs, "label") ?? recordText(value, "type") ?? "未命名";
  const sourceId = recordText(value, "src_id");
  const targetId = recordText(value, "dst_id");
  const endpoints =
    sourceId && targetId && names.has(sourceId) && names.has(targetId)
      ? ` · ${names.get(sourceId)} → ${names.get(targetId)}`
      : "";
  return `关系「${label}」${endpoints}`;
}

function collectionChanges(
  collectionName: "entities" | "relations",
  current: Record<string, unknown>,
  target: Record<string, unknown>,
  titleFor: (value: unknown) => string,
): RollbackContentChange[] {
  const changes: RollbackContentChange[] = [];
  const keys = [...new Set([...Object.keys(current), ...Object.keys(target)])].sort((left, right) =>
    left.localeCompare(right),
  );
  for (const key of keys) {
    const inCurrent = hasOwn(current, key);
    const inTarget = hasOwn(target, key);
    const currentValue = current[key];
    const targetValue = target[key];
    if (inCurrent && inTarget && sameCanonicalValue(currentValue, targetValue)) continue;
    changes.push({
      ...(inCurrent ? { before: currentValue } : {}),
      ...(inTarget ? { after: targetValue } : {}),
      kind: !inCurrent ? "restored" : !inTarget ? "deleted" : "changed",
      technicalPath: `/${collectionName}/${key}`,
      title: titleFor(inTarget ? targetValue : currentValue),
    });
  }
  return changes;
}

function rollbackFieldLabel(key: string): string {
  return (
    {
      constraints: "约束集合",
      meta_schema_version: "元数据版本",
      schema_version: "内容 Schema 版本",
    }[key] ?? key
  );
}

function rollbackContentChanges(currentPayload: unknown, targetPayload: unknown): RollbackContentChange[] {
  if (sameCanonicalValue(currentPayload, targetPayload)) return [];
  if (!isRecord(currentPayload) || !isRecord(targetPayload)) {
    return [
      {
        after: targetPayload,
        before: currentPayload,
        kind: "changed",
        technicalPath: "/",
        title: "完整业务内容",
      },
    ];
  }

  const changes: RollbackContentChange[] = [];
  const handled = new Set<string>();
  const names = new Map([
    ...entityNamesFromPayload(currentPayload),
    ...entityNamesFromPayload(targetPayload),
  ]);
  for (const collectionName of ["entities", "relations"] as const) {
    const currentCollection = currentPayload[collectionName];
    const targetCollection = targetPayload[collectionName];
    if (
      (currentCollection === undefined || isRecord(currentCollection)) &&
      (targetCollection === undefined || isRecord(targetCollection))
    ) {
      handled.add(collectionName);
      changes.push(
        ...collectionChanges(
          collectionName,
          isRecord(currentCollection) ? currentCollection : {},
          isRecord(targetCollection) ? targetCollection : {},
          collectionName === "entities" ? entityTitle : (value) => relationTitle(value, names),
        ),
      );
    }
  }

  const topLevelKeys = [...new Set([...Object.keys(currentPayload), ...Object.keys(targetPayload)])].sort(
    (left, right) => left.localeCompare(right),
  );
  for (const key of topLevelKeys) {
    if (handled.has(key)) continue;
    const inCurrent = hasOwn(currentPayload, key);
    const inTarget = hasOwn(targetPayload, key);
    const currentValue = currentPayload[key];
    const targetValue = targetPayload[key];
    if (inCurrent && inTarget && sameCanonicalValue(currentValue, targetValue)) continue;
    changes.push({
      ...(inCurrent ? { before: currentValue } : {}),
      ...(inTarget ? { after: targetValue } : {}),
      kind: !inCurrent ? "restored" : !inTarget ? "deleted" : "changed",
      technicalPath: `/${key}`,
      title: `字段「${rollbackFieldLabel(key)}」`,
    });
  }
  return changes;
}

function splitFieldTarget(target: string): { fieldPath: string; resourceId: string } {
  const separator = target.indexOf(".");
  if (separator <= 0 || separator === target.length - 1) {
    throw new Error("Patch 字段操作缺少可解释的资源与字段路径。");
  }
  return { fieldPath: target.slice(separator + 1), resourceId: target.slice(0, separator) };
}

function fieldLabel(fieldPath: string): string {
  const pathParts = fieldPath.split(".");
  const key = pathParts[pathParts.length - 1] ?? fieldPath;
  return (
    {
      amount: "数量",
      buy_prob: "购买概率",
      count: "数量",
      description: "说明",
      distance: "距离",
      display_name: "显示名称",
      gold: "金币",
      name: "名称",
      note: "备注",
      price: "价格",
      quantity: "数量",
      reward_gold: "金币奖励",
      title: "标题",
      weight: "权重",
    }[key] ?? `字段「${key}」`
  );
}

function inlineValue(value: unknown): string {
  if (value === undefined) return "未提供";
  if (value === null) return "JSON null";
  if (typeof value === "string") return `「${value}」`;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return "结构化值（详见原始字段）";
}

function relationTypeLabel(type: string | null): string {
  if (type === null) return "未命名关系";
  return (
    {
      ALLY_WITH: "结盟",
      CONTAINS: "包含",
      DROPS_FROM: "掉落自",
      FRIEND_OF: "好友",
      LOCATED_IN: "位于",
      PARTICIPATES_IN: "参与",
      PART_OF: "属于",
      REQUIRES: "需要",
      SELLS: "售卖",
      UNLOCKS: "解锁",
    }[type] ?? `“${type}”关系`
  );
}

function relationEndpoints(
  value: unknown,
  names: ReadonlyMap<string, string>,
): { label: string; source: string; target: string } {
  if (!isRecord(value)) {
    return { label: "未命名关系", source: "未命名对象", target: "未命名对象" };
  }
  const attrs = isRecord(value.attrs) ? value.attrs : {};
  const sourceId = recordText(value, "src_id");
  const targetId = recordText(value, "dst_id");
  return {
    label: recordText(attrs, "label") ?? relationTypeLabel(recordText(value, "type")),
    source: (sourceId && names.get(sourceId)) ?? "未命名对象",
    target: (targetId && names.get(targetId)) ?? "未命名对象",
  };
}

function relationListSummary(value: unknown, names: ReadonlyMap<string, string>): string {
  const relation = relationEndpoints(value, names);
  return `${relation.label}：${relation.source} → ${relation.target}`;
}

function relationSentence(value: unknown, names: ReadonlyMap<string, string>): string {
  const relation = relationEndpoints(value, names);
  return `${relation.source} ${relation.label} ${relation.target}`;
}

function recordsById(value: unknown, label: string): ReadonlyMap<string, Record<string, unknown>> {
  if (!Array.isArray(value)) throw new Error(`${label} 不是可解释的对象数组。`);
  const records = new Map<string, Record<string, unknown>>();
  for (const item of value) {
    if (!isRecord(item)) throw new Error(`${label} 包含无法解释的对象。`);
    const id = recordText(item, "id");
    if (!id || records.has(id)) throw new Error(`${label} 缺少唯一对象 identity。`);
    records.set(id, item);
  }
  return records;
}

function oldRecordsById(value: unknown, label: string): ReadonlyMap<string, Record<string, unknown>> {
  if (!isRecord(value)) throw new Error(`${label} 不是可解释的旧对象集合。`);
  const records = new Map<string, Record<string, unknown>>();
  for (const [id, item] of Object.entries(value)) {
    if (!isRecord(item)) throw new Error(`${label} 包含无法解释的旧对象。`);
    const embeddedId = recordText(item, "id");
    if (embeddedId !== null && embeddedId !== id) {
      throw new Error(`${label} 的对象 identity 不一致。`);
    }
    records.set(id, item);
  }
  return records;
}

function replaceSubgraphSummary(
  op: TypedOp,
  graph: PatchTargetGraph,
  names: ReadonlyMap<string, string>,
): string {
  if (!isRecord(op.new_value) || !isRecord(op.old_value)) {
    throw new Error("replace_subgraph 缺少可解释的 exact before/after 内容。");
  }
  const newEntities = recordsById(op.new_value.entities, "replace_subgraph entities");
  const newRelations = recordsById(op.new_value.relations, "replace_subgraph relations");
  const oldEntities = oldRecordsById(op.old_value.entities, "replace_subgraph old entities");
  const oldRelations = oldRecordsById(op.old_value.relations, "replace_subgraph old relations");
  const affected = [
    ...[...newEntities].map(([id, value]) => entityTitle(graph.entities.get(id) ?? value)),
    ...[...newRelations].map(([id, value]) => relationListSummary(graph.relations.get(id) ?? value, names)),
  ];
  const addedEntities = [...newEntities.keys()].filter((id) => !oldEntities.has(id)).length;
  const replacedEntities = newEntities.size - addedEntities;
  const addedRelations = [...newRelations.keys()].filter((id) => !oldRelations.has(id)).length;
  const replacedRelations = newRelations.size - addedRelations;
  return [
    `受影响对象：${affected.length > 0 ? affected.join("、") : "未命名对象"}`,
    `实体：新增 ${addedEntities} · 删除 0 · 替换 ${replacedEntities}`,
    `关系：新增 ${addedRelations} · 删除 0 · 替换 ${replacedRelations}`,
  ].join("；");
}

function buildPatchOpSummaries(
  ops: readonly TypedOp[],
  graph: PatchTargetGraph,
): ReadonlyMap<string, string> {
  const names = new Map(graph.entityNames);
  for (const op of ops) {
    const value = op.op === "delete_entity" ? op.old_value : op.new_value;
    if (op.op === "add_entity" || op.op === "delete_entity") {
      const name = entityName(value);
      if (name) names.set(op.target, name);
    }
  }

  const summaries = new Map<string, string>();
  for (const op of ops) {
    if (summaries.has(op.op_id)) throw new Error("Patch op_id 不唯一，无法安全展示。");
    let summary: string;
    if (op.op === "add_entity" || op.op === "delete_entity") {
      const value = op.op === "add_entity" ? (graph.entities.get(op.target) ?? op.new_value) : op.old_value;
      summary = `${op.op === "add_entity" ? "新增" : "删除"} ${entityTitle(value)}`;
    } else if (op.op === "add_relation" || op.op === "delete_relation") {
      const value =
        op.op === "add_relation" ? (graph.relations.get(op.target) ?? op.new_value) : op.old_value;
      summary = relationListSummary(value, names);
    } else if (op.op === "set_entity_attr") {
      const { fieldPath, resourceId } = splitFieldTarget(op.target);
      const resource = graph.entities.get(resourceId);
      summary = `${entityName(resource) ?? "未命名对象"}的${fieldLabel(fieldPath)}：${inlineValue(
        op.old_value,
      )} → ${inlineValue(op.new_value)}`;
    } else if (op.op === "set_relation_attr") {
      const { fieldPath, resourceId } = splitFieldTarget(op.target);
      summary = `${relationSentence(graph.relations.get(resourceId), names)}的${fieldLabel(
        fieldPath,
      )}：${inlineValue(op.old_value)} → ${inlineValue(op.new_value)}`;
    } else {
      summary = replaceSubgraphSummary(op, graph, names);
    }
    summaries.set(op.op_id, summary);
  }
  return summaries;
}

function OpValue({ absentLabel, value }: { absentLabel?: string; value: unknown }) {
  if ((value === undefined || value === null) && absentLabel) {
    return <span className="gf-approvals__absent-value">{absentLabel}</span>;
  }
  return value === undefined ? (
    <span className="gf-approvals__muted">无</span>
  ) : isRecord(value) || Array.isArray(value) ? (
    <details>
      <summary>查看原始字段</summary>
      <code>{JSON.stringify(value)}</code>
    </details>
  ) : (
    <code>{JSON.stringify(value)}</code>
  );
}

function evidenceKindLabel(kind: string): string {
  return (
    {
      checker: "确定性检查",
      config_export: "配置导出校验",
      constraint_compile: "约束编译与交叉验证",
      regression: "回归验证",
      simulation: "仿真回归",
    }[kind] ?? kind
  );
}

function evidenceStatusLabel(status: string): string {
  return (
    {
      failed: "未通过",
      not_applicable: "不适用",
      passed: "已通过",
      unproven: "未证明",
    }[status] ?? status
  );
}

function PatchReview({
  opSummaries,
  subject,
}: {
  opSummaries: ReadonlyMap<string, string>;
  subject: ApprovalPatch;
}) {
  const patch = subject.patch;
  return (
    <div className="gf-approvals__subject-content">
      <div className="gf-approvals__subject-lede">
        <FileDiff aria-hidden="true" size={20} />
        <div>
          <h3>{patch.rationale}</h3>
          <p>
            {patch.ops.length} 项变更 · 风险 {patch.side_effect_risk} ·{" "}
            {patch.produced_by === "human" ? "人工" : "Agent"}提案
          </p>
        </div>
      </div>
      {patch.expected_to_fix && patch.expected_to_fix.length > 0 && (
        <div>
          <strong>预期修复</strong>
          <ul>
            {patch.expected_to_fix.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}
      <ol className="gf-approvals__op-list" aria-label="Patch 变更内容">
        {patch.ops.map((op) => {
          const summary = opSummaries.get(op.op_id) ?? "未命名对象";
          return (
            <li key={op.op_id}>
              <header>
                <span className="u-status u-status--info">{opLabels[op.op]}</span>
                <strong>{summary}</strong>
                <details>
                  <summary>查看技术标识</summary>
                  <code>{op.target}</code>
                </details>
              </header>
              <div className="gf-approvals__before-after">
                <div>
                  <span>修改前</span>
                  <OpValue
                    absentLabel={
                      op.op === "add_entity" || op.op === "add_relation" ? "原先不存在" : undefined
                    }
                    value={op.old_value}
                  />
                </div>
                <ArrowRight aria-label="变更为" size={18} />
                <div>
                  <span>修改后</span>
                  <OpValue
                    absentLabel={
                      op.op === "delete_entity" || op.op === "delete_relation" ? "删除后不存在" : undefined
                    }
                    value={op.new_value}
                  />
                </div>
              </div>
            </li>
          );
        })}
      </ol>
      {patch.preconditions && patch.preconditions.length > 0 && (
        <details>
          <summary>查看 {patch.preconditions.length} 项前置条件</summary>
          <pre>{JSON.stringify(patch.preconditions, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}

function ConstraintReview({ subject }: { subject: ApprovalConstraintProposal }) {
  return (
    <div className="gf-approvals__subject-content">
      <div className="gf-approvals__subject-lede">
        <ShieldCheck aria-hidden="true" size={20} />
        <div>
          <h3>{subject.proposal.rationale}</h3>
          <p>{subject.proposal.constraints.length} 条 typed constraint</p>
        </div>
      </div>
      <ConstraintSummaryList values={subject.proposal.constraints} />
    </div>
  );
}

const rollbackChangeLabels: Record<RollbackChangeKind, string> = {
  changed: "回退后改变",
  deleted: "回退后删除",
  restored: "回退后恢复",
};

function RollbackReview({
  currentArtifact,
  subject,
  targetArtifact,
}: {
  currentArtifact: ApprovalArtifactPayload;
  subject: ApprovalRollbackRequest;
  targetArtifact: ApprovalArtifactPayload;
}) {
  const request = subject.request;
  const changes = rollbackContentChanges(currentArtifact.payload, targetArtifact.payload);
  const changeCounts = {
    changed: changes.filter((change) => change.kind === "changed").length,
    deleted: changes.filter((change) => change.kind === "deleted").length,
    restored: changes.filter((change) => change.kind === "restored").length,
  };
  return (
    <div className="gf-approvals__subject-content">
      <div className="gf-approvals__subject-lede">
        <RotateCcw aria-hidden="true" size={20} />
        <div>
          <h3>{request.reason}</h3>
          <p>
            将 {request.ref_name} 回退到历史 revision {request.target_history_revision}
          </p>
        </div>
      </div>
      <div className="gf-approvals__rollback-summary">
        <strong>回退后的实际内容变化</strong>
        <span>
          恢复 {changeCounts.restored} · 删除 {changeCounts.deleted} · 改变 {changeCounts.changed}
        </span>
      </div>
      {changes.length === 0 ? (
        <p className="gf-approvals__muted" role="status">
          当前与历史目标的业务 payload 完全一致；本次只移动 ref 指针。
        </p>
      ) : (
        <ol aria-label="回滚内容差异" className="gf-approvals__op-list gf-approvals__rollback-change-list">
          {changes.map((change) => (
            <li key={`${change.kind}:${change.technicalPath}`}>
              <header>
                <span
                  className={`u-status u-status--${
                    change.kind === "restored" ? "ok" : change.kind === "deleted" ? "danger" : "info"
                  }`}
                >
                  {rollbackChangeLabels[change.kind]}
                </span>
                <strong>{change.title}</strong>
                <details>
                  <summary>查看技术定位</summary>
                  <code>{change.technicalPath}</code>
                </details>
              </header>
              <div className="gf-approvals__before-after">
                <div>
                  <span>修改前</span>
                  <OpValue
                    absentLabel={change.before === undefined ? "当前版本中不存在" : undefined}
                    value={change.before}
                  />
                </div>
                <ArrowRight aria-label="回退后变为" size={18} />
                <div>
                  <span>回退后</span>
                  <OpValue
                    absentLabel={change.after === undefined ? "回退后不存在" : undefined}
                    value={change.after}
                  />
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}
      <details className="gf-approvals__rollback-technical">
        <summary>查看回滚技术身份</summary>
        <div className="gf-approvals__rollback-flow">
          <CopyableText copyLabel="复制当前 Artifact ID" value={currentArtifact.artifact.artifact_id} />
          <ArrowRight aria-label="回退到" size={18} />
          <CopyableText copyLabel="复制目标 Artifact ID" value={targetArtifact.artifact.artifact_id} />
        </div>
        <CopyableText copyLabel="复制当前 payload digest" value={currentArtifact.artifact.payload_hash!} />
        <CopyableText copyLabel="复制目标 payload digest" value={targetArtifact.artifact.payload_hash!} />
      </details>
    </div>
  );
}

function EvidenceReviewPanel({ evidence }: { evidence: EvidenceReview }) {
  return (
    <div className="gf-approvals__evidence-review">
      <header>
        <ShieldCheck aria-hidden="true" size={20} />
        <div>
          <h3>确定性验证已通过</h3>
          <p>{evidence.requirements.length} 项检查均由 exact EvidenceSet 固化。</p>
        </div>
      </header>
      <ul>
        {evidence.requirements.map((requirement) => (
          <li key={requirement.requirementId}>
            <span className="u-status u-status--ok">{evidenceStatusLabel(requirement.status)}</span>
            <strong>{evidenceKindLabel(requirement.kind)}</strong>
            <details>
              <summary>技术信息</summary>
              <code>{requirement.toolVersion}</code>
              {requirement.reasonCode && <code>{requirement.reasonCode}</code>}
            </details>
            {requirement.evidenceArtifactId && (
              <a href={`/artifacts/${encodeURIComponent(requirement.evidenceArtifactId)}`}>查看该项证据</a>
            )}
          </li>
        ))}
      </ul>
      <div className="gf-cluster">
        <a href={`/runs/${encodeURIComponent(evidence.runId)}`}>打开验证 Run</a>
        <a href={`/artifacts/${encodeURIComponent(evidence.artifactId)}`}>打开 EvidenceSet</a>
      </div>
    </div>
  );
}

function TargetReview({ target }: { target: ApprovalTarget }) {
  const action =
    target.subject_kind === "rollback_request"
      ? "回退到已选历史版本"
      : target.expected_ref
        ? `更新当前 revision ${target.expected_ref.revision}`
        : "创建新的权威 ref";
  return (
    <section aria-label="审批影响目标" className="gf-approvals__target-review">
      <GitBranch aria-hidden="true" size={20} />
      <div>
        <span>这次批准将影响</span>
        <strong>{target.ref_name}</strong>
        <p>{action}</p>
        <details>
          <summary>查看目标版本技术身份</summary>
          <CopyableText copyLabel="复制目标 Artifact ID" value={target.target_artifact_id} />
          {target.target_snapshot_id && (
            <CopyableText copyLabel="复制目标 Snapshot ID" value={target.target_snapshot_id} />
          )}
        </details>
      </div>
    </section>
  );
}

export function ApprovalSubjectReview({ data }: { data: ApprovalSubjectReviewData }) {
  return (
    <section aria-label="受审内容与验证依据" className="gf-approvals__section gf-approvals__subject-review">
      <header className="gf-approvals__section-heading">
        <FileDiff aria-hidden="true" size={20} />
        <div>
          <h2>你正在批准什么</h2>
          <p>先核对实际内容、目标与确定性证据，再提交决定。</p>
        </div>
      </header>
      {data.kind === "patch" ? (
        <PatchReview opSummaries={data.opSummaries} subject={data.subject} />
      ) : data.kind === "constraint_proposal" ? (
        <ConstraintReview subject={data.subject} />
      ) : (
        <RollbackReview
          currentArtifact={data.currentArtifact}
          subject={data.subject}
          targetArtifact={data.targetArtifact}
        />
      )}
      <TargetReview target={data.target} />
      <EvidenceReviewPanel evidence={data.evidence} />
      <details className="gf-approvals__subject-technical">
        <summary>查看受审对象技术身份</summary>
        <CopyableText copyLabel="复制受审 Artifact ID" value={data.subject.artifact.artifact_id} />
      </details>
    </section>
  );
}

export function ApprovalSubjectReviewFailure() {
  return (
    <section aria-label="受审内容与验证依据" className="gf-approvals__section">
      <div className="gf-approvals__review-failure" role="alert">
        <AlertTriangle aria-hidden="true" size={20} />
        <div>
          <strong>无法安全读取完整受审内容</strong>
          <p>内容、摘要、目标或 EvidenceSet 未闭合；审批决定已禁用。</p>
        </div>
      </div>
    </section>
  );
}
