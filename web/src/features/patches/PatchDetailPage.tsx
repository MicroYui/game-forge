import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  BadgeCheck,
  Bot,
  FilePenLine,
  GitBranch,
  GitMerge,
  PlayCircle,
  Send,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { createMutationIntent, ReauthenticationRequiredError } from "../../api/csrf";
import type { components } from "../../api/generated/openapi";
import { cursorFromPage } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { MergeResolver, SnapshotDiffView } from "../../components/diff";
import { findingDisplayMessage } from "../../components/evidence";
import { TechnicalDetails, compactDateTime } from "../../components/identity";
import { ConfirmDialog, ProblemPanel, StatePanel } from "../../components/ui";
import { generationManifestArtifactIds, parseGenerationCandidateManifest } from "../generation/candidate";
import { replaySourceOptionLabel, type ReplaySourceRun } from "../runs/replaySources";
import {
  buildPatchApplyRequest,
  currentRefFromCompleteHistory,
  verifyPatchApplyResult,
  verifyPatchWorkflowAuthority,
  verifyReplacementChain,
  verifyReplacementRevision,
} from "./authority";
import { patchWorkflowApi, type PatchWorkflowApi, type VersionedResource } from "./api";
import "./patches.css";
import { profileBusinessContext, profileBusinessLabel, profileKey } from "../execution-profiles";

type ApprovalView = components["schemas"]["ApprovalViewV1"];
type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];
type ConflictResolution = components["schemas"]["ConflictResolution"];
type ExecutionOptionView = components["schemas"]["ExecutionOptionViewV1"];
type ExecutionProfile = components["schemas"]["ExecutionProfileViewV1"];
type FindingEvidenceBinding = components["schemas"]["FindingEvidenceBindingV1"];
type FindingRevision = components["schemas"]["FindingRevisionV1"];
type MergeConflict = components["schemas"]["MergeConflict"];
type PatchArtifactReadView = components["schemas"]["PatchArtifactReadViewV1"];
type PatchRepairRequest = components["schemas"]["PatchRepairRequestV1"];
type PatchTargetBinding = Extract<
  NonNullable<ApprovalView["approval"]["target_binding"]>,
  { subject_kind: "patch" }
>;
type PatchValidationRequest = components["schemas"]["PatchValidationAdmissionRequestV1"];
type ProfileKind = ExecutionProfile["profile_kind"];
type RebaseResult = components["schemas"]["RebaseResult"];
type RefHistoryEntry = components["schemas"]["RefHistoryEntryV1"];

const findingDefectLabels: Readonly<Record<string, string>> = {
  dead_quest: "任务无法完成",
  economy_collapse: "经济系统可能失衡",
  playtest_incomplete: "试玩未完成",
  quest_dead_end: "任务流程存在死路",
  reward_out_of_range: "数值超出允许范围",
  unreachable_target: "目标无法到达",
};

const findingStatusLabels: Readonly<Record<string, string>> = {
  accepted_risk: "已接受风险",
  confirmed: "已确认",
  dismissed: "已忽略",
  fixed: "已修复",
  unproven: "未证明",
};

const findingSourceLabels: Readonly<Record<string, string>> = {
  checker: "规则检查",
  llm: "AI 建议",
  playtest: "自动试玩",
  sim: "经济仿真",
};

const findingOracleLabels: Readonly<Record<string, string>> = {
  deterministic: "确定性结果",
  "llm-assisted": "AI 辅助，需确认",
  simulation: "仿真结果",
};
type RefValue = components["schemas"]["RefValue"];
type RunFindingLink = components["schemas"]["RunFindingLinkViewV1"];
type RunSubmissionRequest = components["schemas"]["RunSubmissionRequestV1"];
type RunView = components["schemas"]["RunViewV1"];
type LineageEntry = components["schemas"]["LineageEntryV1"];
type SnapshotDiffPage = components["schemas"]["SnapshotDiffHttpPageV1"];
type SubjectApprovalBinding = components["schemas"]["SubjectApprovalBindingViewV1"];
type WorkflowApplyResult = components["schemas"]["WorkflowApplyResultV1"];

interface PatchDetailData {
  approval: VersionedResource<ApprovalView>;
  baseArtifactId: string;
  binding: SubjectApprovalBinding;
  current: VersionedResource<PatchArtifactReadView>;
  currentRef: Readonly<RefValue> | null;
  currentSnapshotId: string | null;
  evidence: ArtifactPayloadView | null;
  failure: ArtifactPayloadView | null;
  history: RefHistoryEntry[];
  repairExpectedFindingAuthority: RepairExpectedFindingAuthority | null;
  target: Readonly<PatchTargetBinding>;
}

interface RepairExpectedFindingAuthority {
  bindings: FindingEvidenceBinding[];
  evidenceArtifactId: string;
}

interface DiffState {
  entries: SnapshotDiffPage["page"]["items"];
  error: Error | null;
  loading: boolean;
  nextCursor: string | null;
  readSnapshotId: string;
  summary: SnapshotDiffPage["diff"];
}

interface MutationState {
  error: Error | null;
  label: string;
  pending: boolean;
  retry: (() => Promise<void>) | null;
}

interface RepairRunHandoff {
  configExportArtifactIds: string[];
  patchPayloadHash: string | null;
  primaryPatchArtifactId: string | null;
  previewArtifactId: string | null;
  previewSnapshotId: string | null;
  status: RunView["status"];
}

interface ReplacementContinuation {
  configExportArtifactIds: string[];
  constraintArtifactId: string | null;
  patchPayloadHash: string;
  previewArtifactId: string;
  previewSnapshotId: string;
  producerRunId: string;
}

interface ProfileCatalog {
  focusedChecker: ExecutionProfile[];
  repairChecker: ExecutionProfile[];
  repairConfigExport: ExecutionProfile[];
  repairSimulation: ExecutionProfile[];
  patchRepair: ExecutionProfile[];
  validation: ExecutionProfile[];
  validationChecker: ExecutionProfile[];
  validationSimulation: ExecutionProfile[];
}

interface CurrentFindingOption {
  binding: FindingEvidenceBinding;
  finding: FindingRevision;
  link: RunFindingLink;
}

interface CurrentFindingEvidenceGroup {
  evidenceArtifactId: string;
  options: CurrentFindingOption[];
}

type SelectableArtifactKind =
  | "config_export"
  | "constraint_snapshot"
  | "playtest_trace"
  | "regression_suite"
  | "review_report";

type ArtifactCatalogItem = ArtifactSummary & { catalogPayload: unknown };

type ArtifactCatalog = Record<SelectableArtifactKind, ArtifactCatalogItem[]>;

type ArtifactSummaryCatalog = Record<SelectableArtifactKind, ArtifactSummary[]>;

type DeepLinkedArtifactIds = Record<SelectableArtifactKind, string[]>;

const selectableArtifactKinds: readonly SelectableArtifactKind[] = [
  "constraint_snapshot",
  "config_export",
  "review_report",
  "playtest_trace",
  "regression_suite",
];

const artifactKindLabels: Record<SelectableArtifactKind, string> = {
  config_export: "候选配置导出",
  constraint_snapshot: "约束快照",
  playtest_trace: "实测轨迹",
  regression_suite: "回归套件",
  review_report: "审查报告",
};

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("Patch workflow operation failed.");
}

function unknownOutcome(error: Error): boolean {
  return !(error instanceof ApiProblemError) && !(error instanceof ReauthenticationRequiredError);
}

function sameRef(left: RefValue | null | undefined, right: RefValue | null | undefined): boolean {
  return left?.artifact_id === right?.artifact_id && left?.revision === right?.revision;
}

function patchRationaleLabel(rationale: string): string {
  if (/\p{Script=Han}/u.test(rationale)) return rationale;
  if (/reward|econom|sink|gold|currency/iu.test(rationale)) {
    return "调整奖励与经济数值，使资源产出回到安全范围内。";
  }
  return "根据检查结果调整内容，并在应用前重新验证。";
}

function supportsRunKind(profile: ExecutionProfile, kind: string): boolean {
  return profile.compatible_run_kinds.some((candidate) => candidate.kind === kind && candidate.version === 1);
}

function profileCoversDomains(profile: ExecutionProfile, requiredDomainIds: readonly string[]): boolean {
  const covered = new Set(profile.domain_scope.domain_ids);
  return requiredDomainIds.every((domainId) => covered.has(domainId));
}

function parseFindingBindings(value: string): FindingEvidenceBinding[] | null {
  if (value.trim() === "") return [];
  try {
    const parsed: unknown = JSON.parse(value);
    return parseExactFindingBindings(parsed);
  } catch {
    return null;
  }
}

function exactQueryIds(searchParams: URLSearchParams, name: string): string[] {
  return [
    ...new Set(
      searchParams
        .getAll(name)
        .map((value) => value.trim())
        .filter(Boolean),
    ),
  ];
}

function deepLinkedArtifactIds(searchParams: URLSearchParams): DeepLinkedArtifactIds {
  return {
    config_export: exactQueryIds(searchParams, "config"),
    constraint_snapshot: exactQueryIds(searchParams, "constraint"),
    playtest_trace: exactQueryIds(searchParams, "trace"),
    regression_suite: exactQueryIds(searchParams, "regression"),
    review_report: exactQueryIds(searchParams, "review"),
  };
}

function sortedIds(values: ReadonlySet<string>): string[] {
  return [...values].sort((left, right) => left.localeCompare(right));
}

async function collectArtifactKind(
  api: PatchWorkflowApi,
  kind: SelectableArtifactKind,
): Promise<ArtifactSummary[]> {
  const artifacts: ArtifactSummary[] = [];
  const artifactIds = new Set<string>();
  const seenCursors = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listArtifacts(kind, cursor);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new Error(`${artifactKindLabels[kind]}目录的读取快照发生漂移。`);
    }
    readSnapshotId = page.read_snapshot_id;
    for (const artifact of page.items) {
      if (artifact.kind !== kind) {
        throw new Error(`${artifactKindLabels[kind]}目录返回了错误类型的 Artifact。`);
      }
      if (artifactIds.has(artifact.artifact_id)) {
        throw new Error(`${artifactKindLabels[kind]}目录重复返回同一 Artifact。`);
      }
      artifactIds.add(artifact.artifact_id);
      artifacts.push(artifact);
    }
    const next = page.next_cursor ?? null;
    if (next === null) return artifacts;
    if (seenCursors.has(next)) {
      throw new Error(`${artifactKindLabels[kind]}目录返回了循环游标。`);
    }
    seenCursors.add(next);
    cursor = next;
  }
  throw new Error(`${artifactKindLabels[kind]}目录超过了有界分页上限。`);
}

async function loadArtifactCatalog(
  api: PatchWorkflowApi,
  deepLinkedIds: DeepLinkedArtifactIds,
): Promise<ArtifactCatalog> {
  if (deepLinkedIds.constraint_snapshot.length > 1) {
    throw new Error("一个 Patch 页面不能预选多个约束快照。");
  }
  const collected = await Promise.all(
    selectableArtifactKinds.map(async (kind) => [kind, await collectArtifactKind(api, kind)] as const),
  );
  const summaries = Object.fromEntries(collected) as ArtifactSummaryCatalog;
  const exactViews = new Map<string, ArtifactPayloadView>();

  await Promise.all(
    selectableArtifactKinds.flatMap((kind) =>
      deepLinkedIds[kind].map(async (artifactId) => {
        const view = await api.getArtifact(artifactId);
        if (view.artifact.artifact_id !== artifactId || view.artifact.kind !== kind) {
          throw new Error(`链接中的${artifactKindLabels[kind]}无法通过 exact Artifact 类型校验。`);
        }
        exactViews.set(artifactId, view);
        if (!summaries[kind].some((artifact) => artifact.artifact_id === artifactId)) {
          summaries[kind].push(view.artifact);
        }
      }),
    ),
  );

  for (const kind of selectableArtifactKinds) {
    summaries[kind].sort((left, right) => {
      const created = (right.created_at ?? "").localeCompare(left.created_at ?? "");
      return created !== 0 ? created : left.artifact_id.localeCompare(right.artifact_id);
    });
  }

  const hydrated = await Promise.all(
    selectableArtifactKinds.map(async (kind) => {
      const artifacts = await Promise.all(
        summaries[kind].map(async (summary) => {
          const view = exactViews.get(summary.artifact_id) ?? (await api.getArtifact(summary.artifact_id));
          if (
            view.artifact.artifact_id !== summary.artifact_id ||
            view.artifact.kind !== kind ||
            (summary.payload_hash !== null &&
              summary.payload_hash !== undefined &&
              view.artifact.payload_hash !== summary.payload_hash)
          ) {
            throw new Error(`${artifactKindLabels[kind]}详情与目录中的不可变内容不一致。`);
          }
          return { ...view.artifact, catalogPayload: view.payload };
        }),
      );
      return [kind, artifacts] as const;
    }),
  );
  return Object.fromEntries(hydrated) as ArtifactCatalog;
}

function exactPatchConstraintArtifactId(
  subject: PatchArtifactReadView,
  catalog: ArtifactCatalog,
): string | null | undefined {
  const semanticConstraintId = subject.artifact.version_tuple.constraint_snapshot_id ?? null;
  const directParentIds = new Set(subject.artifact.parent_artifact_ids);
  const directConstraintParents = catalog.constraint_snapshot.filter((artifact) =>
    directParentIds.has(artifact.artifact_id),
  );
  if (semanticConstraintId === null) {
    return directConstraintParents.length === 0 ? null : undefined;
  }
  const matching = directConstraintParents.filter(
    (artifact) => artifact.version_tuple.constraint_snapshot_id === semanticConstraintId,
  );
  return directConstraintParents.length === 1 && matching.length === 1 ? matching[0].artifact_id : undefined;
}

function findingIdentity(finding: Pick<FindingRevision, "finding_id" | "revision">): string {
  return JSON.stringify([finding.finding_id, finding.revision]);
}

function groupCurrentFindingOptions(options: readonly CurrentFindingOption[]): CurrentFindingEvidenceGroup[] {
  const grouped = new Map<string, CurrentFindingOption[]>();
  for (const option of options) {
    const evidenceArtifactId = option.binding.evidence_artifact_id;
    const group = grouped.get(evidenceArtifactId);
    if (group) group.push(option);
    else grouped.set(evidenceArtifactId, [option]);
  }
  return [...grouped.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([evidenceArtifactId, groupOptions]) => ({
      evidenceArtifactId,
      options: groupOptions,
    }));
}

function validFindingRevision(finding: FindingRevision): boolean {
  const payload = finding.payload;
  return (
    finding.revision_schema_version === "finding-revision@1" &&
    isNonEmptyText(finding.finding_id) &&
    Number.isSafeInteger(finding.revision) &&
    finding.revision >= 1 &&
    (finding.supersedes_revision == null ||
      (Number.isSafeInteger(finding.supersedes_revision) &&
        finding.supersedes_revision >= 1 &&
        finding.supersedes_revision < finding.revision)) &&
    !(finding.revision === 1 && finding.supersedes_revision != null) &&
    isNonEmptyText(finding.created_at) &&
    payload.payload_schema_version === "finding-payload@1" &&
    ["checker", "sim", "playtest", "llm"].includes(payload.source) &&
    ["deterministic", "llm-assisted", "simulation"].includes(payload.oracle_type) &&
    ["critical", "major", "minor"].includes(payload.severity) &&
    ["confirmed", "unproven", "dismissed", "fixed", "accepted_risk"].includes(payload.status) &&
    typeof payload.producer_run_id === "string" &&
    isNonEmptyText(payload.producer_id) &&
    isNonEmptyText(payload.snapshot_id) &&
    isNonEmptyText(payload.defect_class) &&
    typeof payload.message === "string" &&
    (payload.entities === undefined ||
      (Array.isArray(payload.entities) && payload.entities.every(isNonEmptyText))) &&
    (payload.relations === undefined ||
      (Array.isArray(payload.relations) && payload.relations.every(isNonEmptyText))) &&
    (payload.constraint_id == null || isNonEmptyText(payload.constraint_id)) &&
    (payload.evidence === undefined || isRecord(payload.evidence)) &&
    (payload.minimal_repro === undefined || isRecord(payload.minimal_repro)) &&
    (payload.confidence == null ||
      (typeof payload.confidence === "number" && Number.isFinite(payload.confidence)))
  );
}

function sameFindingRevision(left: FindingRevision, right: FindingRevision): boolean {
  return JSON.stringify(canonicalJson(left)) === JSON.stringify(canonicalJson(right));
}

async function collectCurrentFindingRevisions(api: PatchWorkflowApi): Promise<FindingRevision[]> {
  const findings: FindingRevision[] = [];
  const identities = new Set<string>();
  const seenCursors = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  let previousFindingId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listFindings(cursor);
    if (
      page.page_schema_version !== "page@1" ||
      !isNonEmptyText(page.read_snapshot_id) ||
      !Array.isArray(page.items) ||
      (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId)
    ) {
      throw new Error("Current Finding catalog changed or returned an invalid page authority.");
    }
    readSnapshotId = page.read_snapshot_id;
    for (const finding of page.items) {
      const identity = findingIdentity(finding);
      if (
        !validFindingRevision(finding) ||
        identities.has(identity) ||
        (previousFindingId !== null && previousFindingId >= finding.finding_id)
      ) {
        throw new Error("Current Finding catalog returned duplicate, unordered, or malformed authority.");
      }
      identities.add(identity);
      findings.push(finding);
      previousFindingId = finding.finding_id;
    }
    const next = page.next_cursor ?? null;
    if (next === null) return findings;
    if (!isNonEmptyText(next) || seenCursors.has(next)) {
      throw new Error("Current Finding catalog returned an invalid cursor.");
    }
    seenCursors.add(next);
    cursor = next;
  }
  throw new Error("Current Finding catalog exceeded its bounded page count.");
}

async function collectRunFindingLinks(api: PatchWorkflowApi, runId: string): Promise<RunFindingLink[]> {
  const links: RunFindingLink[] = [];
  const identities = new Set<string>();
  const seenCursors = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  let previousAttempt = 0;
  let previousOrdinal = 0;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listRunFindingLinks(runId, cursor);
    if (
      page.page_schema_version !== "page@1" ||
      !isNonEmptyText(page.read_snapshot_id) ||
      !Array.isArray(page.items) ||
      (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId)
    ) {
      throw new Error("Run Finding links changed or returned an invalid page authority.");
    }
    readSnapshotId = page.read_snapshot_id;
    for (const link of page.items) {
      const identity = findingIdentity(link.finding);
      const orderedAfterPrevious =
        link.attempt_no > previousAttempt ||
        (link.attempt_no === previousAttempt && link.ordinal > previousOrdinal);
      if (
        link.view_schema_version !== "run-finding-link-view@1" ||
        link.run_id !== runId ||
        !Number.isSafeInteger(link.attempt_no) ||
        link.attempt_no < 1 ||
        !Number.isSafeInteger(link.ordinal) ||
        link.ordinal < 1 ||
        !orderedAfterPrevious ||
        !validFindingRevision(link.finding) ||
        link.finding.payload.producer_run_id !== runId ||
        !isSha256(link.finding_digest) ||
        !isNonEmptyText(link.evidence_artifact_id) ||
        identities.has(identity)
      ) {
        throw new Error("Run Finding links returned duplicate, conflicting, or malformed authority.");
      }
      identities.add(identity);
      links.push(link);
      previousAttempt = link.attempt_no;
      previousOrdinal = link.ordinal;
    }
    const next = page.next_cursor ?? null;
    if (next === null) return links;
    if (!isNonEmptyText(next) || seenCursors.has(next)) {
      throw new Error("Run Finding links returned an invalid cursor.");
    }
    seenCursors.add(next);
    cursor = next;
  }
  throw new Error("Run Finding links exceeded their bounded page count.");
}

async function loadCurrentFindingOptions(
  api: PatchWorkflowApi,
  targetSnapshotId: string,
): Promise<CurrentFindingOption[]> {
  const current = (await collectCurrentFindingRevisions(api)).filter(
    (finding) =>
      finding.payload.snapshot_id === targetSnapshotId && finding.payload.producer_run_id.length > 0,
  );
  const linksByRun = new Map<string, RunFindingLink[]>();
  for (const runId of [...new Set(current.map((finding) => finding.payload.producer_run_id))].sort()) {
    linksByRun.set(runId, await collectRunFindingLinks(api, runId));
  }
  return current.map((finding) => {
    const links = linksByRun.get(finding.payload.producer_run_id) ?? [];
    const matching = links.filter(
      (link) => link.finding.finding_id === finding.finding_id && link.finding.revision === finding.revision,
    );
    if (matching.length !== 1 || !sameFindingRevision(matching[0].finding, finding)) {
      throw new Error("Current Finding revision does not close against one exact producer Run link.");
    }
    const link = matching[0];
    return {
      binding: {
        evidence_artifact_id: link.evidence_artifact_id,
        finding_digest: link.finding_digest,
        finding_id: finding.finding_id,
        finding_revision: finding.revision,
      },
      finding,
      link,
    };
  });
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

async function collectReplaySourceRuns(api: PatchWorkflowApi): Promise<ReplaySourceRun[]> {
  const runs: ReplaySourceRun[] = [];
  const runIds = new Set<string>();
  const seenCursors = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listReplaySourceRuns(cursor);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new Error("Replay source Run catalog changed read snapshot.");
    }
    readSnapshotId = page.read_snapshot_id;
    for (const run of page.items) {
      if (run.terminal_cassette_artifact_id == null) {
        throw new Error("Replay source Run catalog returned a Run without a terminal cassette.");
      }
      if (runIds.has(run.run_id)) {
        throw new Error("Replay source Run catalog returned a duplicate Run.");
      }
      runIds.add(run.run_id);
      runs.push(run);
    }
    const next = cursorFromPage(page);
    if (next === null) return runs;
    if (seenCursors.has(next)) {
      throw new Error("Replay source Run catalog returned a cursor cycle.");
    }
    seenCursors.add(next);
    cursor = next;
  }
  throw new Error("Replay source Run catalog exceeded its bounded page count.");
}

async function collectPatchLineage(api: PatchWorkflowApi, artifactId: string): Promise<LineageEntry[]> {
  const entries: LineageEntry[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listLineage(artifactId, cursor);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new Error("Patch lineage changed read snapshot.");
    }
    readSnapshotId = page.read_snapshot_id;
    entries.push(...page.items);
    const next = cursorFromPage(page);
    if (next === null) return entries;
    if (seen.has(next)) throw new Error("Patch lineage returned a cursor cycle.");
    seen.add(next);
    cursor = next;
  }
  throw new Error("Patch lineage exceeded its bounded page count.");
}

async function resolvePatchBaseArtifactId(
  api: PatchWorkflowApi,
  subject: PatchArtifactReadView,
  target: Readonly<PatchTargetBinding>,
): Promise<string> {
  if (target.expected_ref !== null && target.expected_ref !== undefined) {
    return target.expected_ref.artifact_id;
  }
  const lineage = await collectPatchLineage(api, subject.artifact.artifact_id);
  const parentIds = new Set(subject.artifact.parent_artifact_ids);
  const candidates = lineage.filter(
    (entry) =>
      entry.depth === 1 &&
      parentIds.has(entry.artifact.artifact_id) &&
      entry.artifact.kind === "ir_snapshot" &&
      entry.artifact.version_tuple.ir_snapshot_id === subject.patch.base_snapshot_id,
  );
  if (candidates.length !== 1) {
    throw new Error("Patch base Artifact is not uniquely bound by its direct lineage.");
  }
  return candidates[0].artifact.artifact_id;
}

async function collectCurrentRefHistory(
  api: PatchWorkflowApi,
  target: Readonly<PatchTargetBinding>,
): Promise<RefHistoryEntry[]> {
  try {
    return await collectRefHistory(api, target.ref_name);
  } catch (error) {
    if (
      (target.expected_ref ?? null) === null &&
      error instanceof ApiProblemError &&
      error.problem.status === 404 &&
      error.problem.code === "not_found"
    ) {
      return [];
    }
    throw error;
  }
}

async function collectConflicts(api: PatchWorkflowApi, conflictSetId: string): Promise<MergeConflict[]> {
  const conflicts: MergeConflict[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listConflicts(conflictSetId, cursor);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new Error("Conflict set changed read snapshot.");
    }
    readSnapshotId = page.read_snapshot_id;
    conflicts.push(...page.items);
    const next = cursorFromPage(page);
    if (next === null) {
      if (new Set(conflicts.map((item) => item.id)).size !== conflicts.length) {
        throw new Error("Conflict set returned duplicate IDs.");
      }
      return conflicts;
    }
    if (seen.has(next)) throw new Error("Conflict set returned a cursor cycle.");
    seen.add(next);
    cursor = next;
  }
  throw new Error("Conflict set exceeded its bounded page count.");
}

async function loadFirstDiff(
  api: PatchWorkflowApi,
  baseSnapshotId: string,
  targetSnapshotId: string,
): Promise<DiffState> {
  const page = await api.getSnapshotDiff(baseSnapshotId, targetSnapshotId, null);
  if (page.diff.base_snapshot_id !== baseSnapshotId || page.diff.target_snapshot_id !== targetSnapshotId) {
    throw new Error("Diff authority returned different snapshots.");
  }
  return {
    entries: page.page.items,
    error: null,
    loading: false,
    nextCursor: page.page.next_cursor ?? null,
    readSnapshotId: page.page.read_snapshot_id,
    summary: page.diff,
  };
}

async function collectProfiles(api: PatchWorkflowApi, profileKind: ProfileKind): Promise<ExecutionProfile[]> {
  const profiles: ExecutionProfile[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;
  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listExecutionProfiles(
      { limit: 100, profile_kind: profileKind, status: "active" },
      cursor,
    );
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new Error(`${profileKind} profile catalog changed read snapshot.`);
    }
    readSnapshotId = page.read_snapshot_id;
    for (const profile of page.items) {
      if (profile.profile_kind !== profileKind || profile.status !== "active") {
        throw new Error(`Profile catalog returned a non-active ${profileKind} item.`);
      }
      profiles.push(profile);
    }
    const next = cursorFromPage(page);
    if (next === null) return profiles;
    if (seen.has(next)) throw new Error(`${profileKind} profile catalog returned a cursor cycle.`);
    seen.add(next);
    cursor = next;
  }
  throw new Error(`${profileKind} profile catalog exceeded its bounded page count.`);
}

async function loadProfileCatalog(api: PatchWorkflowApi): Promise<ProfileCatalog> {
  const [checker, configExport, patchRepair, simulation, validation] = await Promise.all([
    collectProfiles(api, "checker"),
    collectProfiles(api, "config_export"),
    collectProfiles(api, "patch_repair"),
    collectProfiles(api, "simulation"),
    collectProfiles(api, "validation"),
  ]);
  return {
    focusedChecker: checker.filter((profile) => supportsRunKind(profile, "checker.run")),
    patchRepair: patchRepair.filter((profile) => supportsRunKind(profile, "patch.repair")),
    repairChecker: checker.filter((profile) => supportsRunKind(profile, "patch.repair")),
    repairConfigExport: configExport.filter((profile) => supportsRunKind(profile, "patch.repair")),
    repairSimulation: simulation.filter((profile) => supportsRunKind(profile, "patch.repair")),
    validation: validation.filter((profile) => supportsRunKind(profile, "patch.validate")),
    validationChecker: checker.filter((profile) => supportsRunKind(profile, "patch.validate")),
    validationSimulation: simulation.filter((profile) => supportsRunKind(profile, "patch.validate")),
  };
}

async function loadRepairExpectedFindingAuthority(
  api: PatchWorkflowApi,
  current: VersionedResource<PatchArtifactReadView>,
  binding: SubjectApprovalBinding,
  approval: VersionedResource<ApprovalView>,
): Promise<RepairExpectedFindingAuthority | null> {
  const patch = current.value.patch;
  const predecessorArtifactId = patch.supersedes_artifact_id;
  const isRepairSuccessor =
    patch.produced_by === "agent" &&
    patch.revision > 1 &&
    patch.producer_run_id != null &&
    predecessorArtifactId != null;
  if (!isRepairSuccessor) return null;

  const [predecessor, predecessorBinding] = await Promise.all([
    api.getPatch(predecessorArtifactId),
    api.getApprovalBinding(predecessorArtifactId),
  ]);
  const predecessorApproval = await api.getApproval(predecessorBinding.approval_id);
  verifyReplacementChain(
    {
      approval: predecessorApproval.value,
      binding: predecessorBinding,
      subject: predecessor.value,
    },
    {
      approval: approval.value,
      binding,
      subject: current.value,
    },
  );

  const evidenceArtifactId = predecessorApproval.value.approval.evidence_set_artifact_id;
  if (!isNonEmptyText(evidenceArtifactId)) {
    throw new Error("Repair predecessor has no retained failed EvidenceSet.");
  }
  const evidenceArtifact = await api.getArtifact(evidenceArtifactId);
  const evidence = parsePatchEvidenceArtifact(evidenceArtifact, predecessorApproval.value.approval);
  if (
    evidence.kind !== "evidence" ||
    (evidence.overallStatus !== "failed" && evidence.overallStatus !== "unproven")
  ) {
    throw new Error("Repair predecessor EvidenceSet is not a failed exact authority.");
  }

  const directParents = new Set(current.value.artifact.parent_artifact_ids);
  if (
    !directParents.has(evidenceArtifactId) ||
    evidence.findingBindings.some((finding) => !directParents.has(finding.evidence_artifact_id))
  ) {
    throw new Error("Repair successor lineage omits predecessor Finding authority.");
  }
  const declaredFindingIds = patch.expected_to_fix ?? [];
  const canonicalDeclaredFindingIds = [...new Set(declaredFindingIds)].sort((left, right) =>
    left.localeCompare(right),
  );
  const evidenceFindingIds = evidence.findingBindings
    .map((finding) => finding.finding_id)
    .sort((left, right) => left.localeCompare(right));
  if (
    canonicalDeclaredFindingIds.length !== declaredFindingIds.length ||
    canonicalDeclaredFindingIds.length !== evidenceFindingIds.length ||
    canonicalDeclaredFindingIds.some((findingId, index) => findingId !== evidenceFindingIds[index])
  ) {
    throw new Error("Repair successor expected_to_fix differs from predecessor EvidenceSet.");
  }
  return {
    bindings: evidence.findingBindings,
    evidenceArtifactId,
  };
}

async function loadPatchDetail(api: PatchWorkflowApi, artifactId: string): Promise<PatchDetailData> {
  const current = await api.getPatch(artifactId);
  const binding = await api.getApprovalBinding(artifactId);
  const approval = await api.getApproval(binding.approval_id);
  const target = verifyPatchWorkflowAuthority({
    approval: approval.value,
    binding,
    subject: current.value,
  });
  const [baseArtifactId, history, repairExpectedFindingAuthority] = await Promise.all([
    resolvePatchBaseArtifactId(api, current.value, target),
    collectCurrentRefHistory(api, target),
    loadRepairExpectedFindingAuthority(api, current, binding, approval),
  ]);
  const currentRef =
    history.length === 0 ? null : currentRefFromCompleteHistory(target.ref_name, history, null);
  const currentSpec = currentRef ? await api.getSpec(currentRef.artifact_id) : null;
  if (
    currentRef !== null &&
    (currentSpec?.artifact.artifact_id !== currentRef.artifact_id || currentSpec.ref_name !== target.ref_name)
  ) {
    throw new Error("Current Spec read differs from complete ref history.");
  }
  const item = approval.value.approval;
  const currentEvidenceArtifactId = binding.is_current_head ? item.evidence_set_artifact_id : null;
  const [evidence, failure] = await Promise.all([
    currentEvidenceArtifactId ? api.getArtifact(currentEvidenceArtifactId) : Promise.resolve(null),
    item.last_validation_failure_artifact_id
      ? api.getArtifact(item.last_validation_failure_artifact_id)
      : Promise.resolve(null),
  ]);
  return {
    approval,
    baseArtifactId,
    binding,
    current,
    currentRef,
    currentSnapshotId: currentSpec?.snapshot_id ?? null,
    evidence,
    failure,
    history,
    repairExpectedFindingAuthority,
    target,
  };
}

function authorityProjection(data: PatchDetailData) {
  return {
    approval: data.approval.value,
    binding: data.binding,
    subject: data.current.value,
  } as const;
}

function ProfileSelect({
  label,
  onChange,
  profiles,
  value,
}: {
  label: string;
  onChange(value: string): void;
  profiles: readonly ExecutionProfile[];
  value: string;
}) {
  return (
    <label>
      <span>{label}</span>
      <select onChange={(event) => onChange(event.target.value)} value={value}>
        <option value="">请选择</option>
        {profiles.map((profile) => (
          <option key={profileKey(profile)} value={profileKey(profile)}>
            {profileBusinessLabel(profile)}
          </option>
        ))}
      </select>
    </label>
  );
}

function ProfileChecklist({
  label,
  onChange,
  profiles,
  selected,
}: {
  label: string;
  onChange(value: Set<string>): void;
  profiles: readonly ExecutionProfile[];
  selected: ReadonlySet<string>;
}) {
  return (
    <fieldset className="gf-patches__checklist">
      <legend>{label}</legend>
      {profiles.length === 0 ? (
        <p className="gf-patches__muted">没有可用方案</p>
      ) : (
        profiles.map((profile) => {
          const key = profileKey(profile);
          return (
            <label key={key}>
              <input
                aria-label={profileBusinessLabel(profile)}
                checked={selected.has(key)}
                onChange={(event) => {
                  const next = new Set(selected);
                  if (event.target.checked) next.add(key);
                  else next.delete(key);
                  onChange(next);
                }}
                type="checkbox"
              />
              <span className="gf-patches__profile-copy">
                <strong>{profileBusinessLabel(profile)}</strong>
                <small>{profileBusinessContext(profile)}</small>
              </span>
            </label>
          );
        })
      )}
    </fieldset>
  );
}

function CurrentFindingSelector({
  onChange,
  options,
  selected,
}: {
  onChange(value: Set<string>): void;
  options: readonly CurrentFindingOption[];
  selected: ReadonlySet<string>;
}) {
  const groups = groupCurrentFindingOptions(options);
  return (
    <fieldset className="gf-patches__checklist gf-patches__form-wide">
      <legend>本次要复验的问题</legend>
      <p className="gf-patches__muted">
        同一份检查证据中的问题需要整组选择或取消，避免只验证其中一部分。若希望失败后自动修复，请在验证前选择对应证据组。
      </p>
      {groups.length === 0 ? (
        <p className="gf-patches__muted">当前修改预览没有可选择的问题。</p>
      ) : (
        <div className="gf-patches__finding-evidence-groups">
          {groups.map((group, groupIndex) => {
            const checked = selected.has(group.evidenceArtifactId);
            const count = group.options.length;
            const countLabel = `${count} 个问题`;
            return (
              <section
                className="gf-patches__finding-evidence-group"
                data-selected={checked || undefined}
                key={group.evidenceArtifactId}
              >
                <label className="gf-patches__finding-evidence-toggle">
                  <input
                    aria-label={`选择问题证据组 ${groupIndex + 1}，${countLabel}`}
                    checked={checked}
                    onChange={(event) => {
                      const next = new Set(selected);
                      if (event.target.checked) next.add(group.evidenceArtifactId);
                      else next.delete(group.evidenceArtifactId);
                      onChange(next);
                    }}
                    type="checkbox"
                  />
                  <span>
                    <strong>
                      问题证据组 {groupIndex + 1} · {countLabel}
                    </strong>
                  </span>
                </label>
                <TechnicalDetails
                  items={[{ label: "Evidence Artifact ID", value: group.evidenceArtifactId }]}
                  summary="查看证据组技术信息"
                />
                <ul aria-label={`问题证据组 ${groupIndex + 1}`}>
                  {group.options.map((option) => {
                    const payload = option.finding.payload;
                    return (
                      <li key={findingIdentity(option.finding)}>
                        <div className="gf-patches__finding-heading">
                          <strong>{findingDefectLabels[payload.defect_class] ?? "内容问题"}</strong>
                          <span>
                            {findingStatusLabels[payload.status] ?? payload.status} ·{" "}
                            {findingSourceLabels[payload.source] ?? payload.source} ·{" "}
                            {findingOracleLabels[payload.oracle_type] ?? payload.oracle_type}
                          </span>
                        </div>
                        <p>{findingDisplayMessage(payload.defect_class, payload.message)}</p>
                        <details>
                          <summary>查看问题技术信息</summary>
                          {findingDisplayMessage(payload.defect_class, payload.message) !==
                            payload.message && <code>{payload.message}</code>}
                          <code>
                            {option.finding.finding_id}@{option.finding.revision}
                          </code>
                          <code>
                            {option.link.run_id} · attempt {option.link.attempt_no} · ordinal{" "}
                            {option.link.ordinal}
                          </code>
                        </details>
                      </li>
                    );
                  })}
                </ul>
              </section>
            );
          })}
        </div>
      )}
    </fieldset>
  );
}

function replayRunLabel(run: ReplaySourceRun): string {
  return replaySourceOptionLabel(run);
}

interface ArtifactPresentation {
  context: string;
  label: string;
}

const artifactDefectLabels: Readonly<Record<string, string>> = {
  dead_quest: "任务无法完成",
  economy_collapse: "经济系统可能失衡",
  event_scope_membership_missing: "限时活动内容未归入活动范围",
  identity_normalization: "名称或标识需要合并确认",
  invalid_event_lifecycle: "限时活动生命周期不完整",
  playtest_incomplete: "试玩未完成",
  quest_dead_end: "任务流程存在死路",
  reward_out_of_range: "数值超出允许范围",
  unbound_event_schedule: "限时活动缺少开始和结束时间",
  unreachable_target: "目标无法到达",
};

function objectValue(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function artifactCreatedLabel(artifact: ArtifactSummary): string {
  const label = compactDateTime(artifact.created_at);
  return label === "时间未知" ? "创建时间未记录" : label;
}

function artifactSnapshotId(artifact: ArtifactCatalogItem): string | null {
  const payload = objectValue(artifact.catalogPayload);
  const payloadSnapshot = payload?.snapshot_id;
  if (typeof payloadSnapshot === "string" && payloadSnapshot) return payloadSnapshot;
  return artifact.version_tuple.ir_snapshot_id ?? null;
}

function reviewPresentation(payload: Record<string, unknown>): { count: number; topics: string[] } {
  const classes = arrayValue(payload.by_defect_class)
    .map(objectValue)
    .filter((item): item is Record<string, unknown> => item !== null);
  const topics = [
    ...new Set(
      classes
        .map((item) => item.defect_class)
        .filter((value): value is string => typeof value === "string" && value.length > 0)
        .map((value) => artifactDefectLabels[value] ?? "其他内容规则问题"),
    ),
  ];
  const classifiedCount = classes.reduce((total, item) => {
    const count = item.count;
    return total + (Number.isSafeInteger(count) && Number(count) >= 0 ? Number(count) : 0);
  }, 0);
  const findingCount = [
    "deterministic_findings",
    "simulation_findings",
    "llm_assisted_findings",
    "unproven_findings",
  ].reduce((total, key) => total + arrayValue(payload[key]).length, 0);
  return { count: classifiedCount || findingCount, topics };
}

function artifactPresentation(
  artifact: ArtifactCatalogItem,
  subjectSnapshotId: string | null,
): ArtifactPresentation {
  const kindLabel = artifactKindLabels[artifact.kind as SelectableArtifactKind];
  const payload = objectValue(artifact.catalogPayload) ?? {};
  const created = artifactCreatedLabel(artifact);
  const snapshotId = artifactSnapshotId(artifact);
  const relationship =
    subjectSnapshotId && snapshotId
      ? snapshotId === subjectSnapshotId
        ? "对应当前修改"
        : "对应其他内容版本"
      : null;
  let label = kindLabel;
  let purpose: string | null = null;

  if (artifact.kind === "review_report") {
    const review = reviewPresentation(payload);
    const topicText = review.topics.length > 0 ? `：${review.topics.slice(0, 2).join("、")}` : "";
    label =
      review.count === 0 ? `${kindLabel} · 未发现问题` : `${kindLabel} · ${review.count} 个问题${topicText}`;
    purpose =
      payload.requirement_id === "generation-gate:review" ? "AI 草案生成时的前置审查" : "内容审查结果";
  } else if (artifact.kind === "constraint_snapshot") {
    const count = arrayValue(payload.constraints).length;
    label = count > 0 ? `${kindLabel} · ${count} 条规则` : `${kindLabel} · 暂无附加规则`;
  } else if (artifact.kind === "config_export") {
    const files = arrayValue(payload.files);
    label = files.length > 0 ? `${kindLabel} · ${files.length} 个配置文件` : `${kindLabel} · ${created}`;
    purpose = "用于确认修改后配置能够被游戏读取";
  } else if (artifact.kind === "playtest_trace") {
    const episodes = arrayValue(payload.episodes)
      .map(objectValue)
      .filter((item) => item !== null);
    const completed = episodes.filter((episode) => episode?.completed === true).length;
    label =
      episodes.length > 0
        ? `${kindLabel} · ${completed}/${episodes.length} 个场景完成`
        : `${kindLabel} · ${created}`;
    purpose = "自动试玩的真实执行记录";
  } else if (artifact.kind === "regression_suite") {
    const adapterPayload = objectValue(payload.adapter_payload);
    const caseCount = arrayValue(adapterPayload?.cases).length;
    label = caseCount > 0 ? `${kindLabel} · ${caseCount} 个回归用例` : `${kindLabel} · ${created}`;
    purpose = "用于复查已有玩法是否被这次修改破坏";
  }

  return {
    label,
    context: [relationship, purpose, `生成于 ${created}`].filter(Boolean).join(" · "),
  };
}

function artifactSearchText(artifact: ArtifactCatalogItem, subjectSnapshotId: string | null): string {
  const scope = artifact.domain_scope === "all" ? "all" : (artifact.domain_scope?.domain_ids ?? []).join(" ");
  const presentation = artifactPresentation(artifact, subjectSnapshotId);
  return [
    presentation.label,
    presentation.context,
    artifact.artifact_id,
    artifact.kind,
    artifact.payload_schema_id ?? "",
    artifact.created_at ?? "",
    artifact.version_tuple.constraint_snapshot_id ?? "",
    artifact.version_tuple.ir_snapshot_id ?? "",
    scope,
  ]
    .join(" ")
    .toLocaleLowerCase("zh-CN");
}

function ArtifactResourcePicker({
  artifacts,
  kind,
  locked = false,
  mode,
  onChange,
  selected,
  subjectSnapshotId,
}: {
  artifacts: readonly ArtifactCatalogItem[];
  kind: SelectableArtifactKind;
  locked?: boolean;
  mode: "multiple" | "single";
  onChange(value: Set<string>): void;
  selected: ReadonlySet<string>;
  subjectSnapshotId: string | null;
}) {
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLocaleLowerCase("zh-CN");
  const visible = normalizedQuery
    ? artifacts.filter(
        (artifact) =>
          selected.has(artifact.artifact_id) ||
          artifactSearchText(artifact, subjectSnapshotId).includes(normalizedQuery),
      )
    : artifacts;
  const label = artifactKindLabels[kind];
  return (
    <fieldset className="gf-patches__resource-picker gf-patches__form-wide">
      <legend>{label}</legend>
      <div className="gf-patches__resource-picker-heading">
        <label>
          <span>搜索{label}</span>
          <input
            onChange={(event) => setQuery(event.target.value)}
            placeholder="按时间搜索，也支持技术标识"
            type="search"
            value={query}
          />
        </label>
        <span className="gf-patches__selection-count">
          已选 {selected.size} 项 · 目录 {artifacts.length} 项
        </span>
      </div>
      {locked && <p className="gf-patches__muted">由这份修改自动绑定，不能在验证时替换。</p>}
      {mode === "single" && (
        <label className="gf-patches__resource-option gf-patches__resource-option--none">
          <input
            checked={selected.size === 0}
            disabled={locked}
            name={`artifact-picker-${kind}`}
            onChange={() => onChange(new Set())}
            type="radio"
          />
          <span>
            <strong>不绑定约束快照</strong>
            <small>仅在该工作流允许约束为空时使用。</small>
          </span>
        </label>
      )}
      {visible.length === 0 ? (
        <p className="gf-patches__muted">没有匹配的{label}。</p>
      ) : (
        <div className="gf-patches__resource-options">
          {visible.map((artifact) => {
            const checked = selected.has(artifact.artifact_id);
            const presentation = artifactPresentation(artifact, subjectSnapshotId);
            return (
              <label className="gf-patches__resource-option" key={artifact.artifact_id}>
                <input
                  aria-label={`${presentation.label} ${presentation.context}`}
                  checked={checked}
                  disabled={locked}
                  name={mode === "single" ? `artifact-picker-${kind}` : undefined}
                  onChange={(event) => {
                    if (mode === "single") {
                      onChange(event.target.checked ? new Set([artifact.artifact_id]) : new Set());
                      return;
                    }
                    const next = new Set(selected);
                    if (event.target.checked) next.add(artifact.artifact_id);
                    else next.delete(artifact.artifact_id);
                    onChange(next);
                  }}
                  type={mode === "single" ? "radio" : "checkbox"}
                />
                <span>
                  <strong>{presentation.label}</strong>
                  <small>{presentation.context}</small>
                </span>
              </label>
            );
          })}
        </div>
      )}
    </fieldset>
  );
}

function MutationFailure({ onReload, state }: { onReload(): void; state: MutationState }) {
  if (state.error instanceof ApiProblemError) {
    return (
      <div className="gf-patches__mutation-error">
        <ProblemPanel problem={state.error.problem} />
        <button className="gf-secondary-button" onClick={onReload} type="button">
          重新读取服务器状态
        </button>
      </div>
    );
  }
  return (
    <StatePanel
      action={
        <div className="gf-cluster">
          {state.retry && (
            <button className="gf-secondary-button" onClick={() => void state.retry?.()} type="button">
              重试同一请求
            </button>
          )}
          <button className="gf-secondary-button" onClick={onReload} type="button">
            重新读取服务器状态
          </button>
        </div>
      }
      description={
        state.retry
          ? `${state.label}结果未知；系统已保留原请求，可安全重试。`
          : `${state.label}失败；重新读取最新状态后才能发起新操作。`
      }
      state="error"
      title={state.retry ? "操作结果未知" : "工作流操作失败"}
    />
  );
}

function replacementHref(replacementId: string, continuation?: ReplacementContinuation): string {
  const params = new URLSearchParams();
  if (continuation?.constraintArtifactId) {
    params.set("constraint", continuation.constraintArtifactId);
  }
  for (const configExportArtifactId of continuation?.configExportArtifactIds ?? []) {
    params.append("config", configExportArtifactId);
  }
  const query = params.toString();
  return `/patches/${encodeURIComponent(replacementId)}${query === "" ? "" : `?${query}`}`;
}

function ReplacementReceipt({
  api,
  continuation,
  previous,
  replacementId,
}: {
  api: PatchWorkflowApi;
  continuation?: ReplacementContinuation;
  previous: PatchDetailData;
  replacementId: string;
}) {
  const replacement = useQuery({
    queryFn: async () => {
      const [previousPatch, previousBinding, previousApproval, nextPatch, nextBinding] = await Promise.all([
        api.getPatch(previous.current.value.artifact.artifact_id),
        api.getApprovalBinding(previous.current.value.artifact.artifact_id),
        api.getApproval(previous.binding.approval_id),
        api.getPatch(replacementId),
        api.getApprovalBinding(replacementId),
      ]);
      const nextApproval = await api.getApproval(nextBinding.approval_id);
      const prior = {
        approval: previousApproval.value,
        binding: previousBinding,
        subject: previousPatch.value,
      };
      const next = {
        approval: nextApproval.value,
        binding: nextBinding,
        subject: nextPatch.value,
      };
      verifyReplacementRevision(prior, next);
      const nextTarget = next.approval.approval.target_binding;
      if (
        continuation &&
        (next.subject.artifact.artifact_id !== replacementId ||
          next.subject.artifact.payload_hash !== continuation.patchPayloadHash ||
          next.subject.patch.producer_run_id !== continuation.producerRunId ||
          nextTarget?.subject_kind !== "patch" ||
          nextTarget.target_artifact_id !== continuation.previewArtifactId ||
          nextTarget.target_snapshot_id !== continuation.previewSnapshotId)
      ) {
        throw new Error("Replacement Patch differs from the verified Repair RunResult authority.");
      }
      return next;
    },
    queryKey: [
      "patch-replacement",
      previous.current.value.artifact.artifact_id,
      replacementId,
      continuation?.producerRunId ?? null,
      continuation?.patchPayloadHash ?? null,
      continuation?.previewArtifactId ?? null,
      continuation?.previewSnapshotId ?? null,
    ],
    retry: false,
  });
  if (replacement.isPending) {
    return <StatePanel description="正在核对新旧草案的版本关系。" state="loading" title="正在读取新版本" />;
  }
  if (replacement.isError) {
    return (
      <StatePanel
        description="新版本未能与被替代的旧版本完整对应，因此不会提供继续入口。"
        state="error"
        title="无法确认新版本"
      />
    );
  }
  return (
    <div className="gf-patches__live-receipt" role="status">
      <StatePanel
        action={<a href={replacementHref(replacementId, continuation)}>打开新修改草案</a>}
        description={`已创建第 ${replacement.data.subject.patch.revision} 版草案；旧验证、证据与审批决定不会继承。`}
        state="terminal"
        title="已创建独立的新版本"
      />
    </div>
  );
}

type PatchEvidenceView =
  | { kind: "none" }
  | { kind: "unsafe" }
  | {
      kind: "evidence";
      findingBindings: FindingEvidenceBinding[];
      overallStatus: string;
      requirements: {
        evidenceArtifactId: string | null;
        kind: string;
        reasonCode: string | null;
        requirementId: string;
        status: string;
        toolVersion: string;
      }[];
      runId: string;
    };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyText(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isSha256(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function hasExactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const canonicalExpected = [...expected].sort();
  return (
    actual.length === canonicalExpected.length &&
    actual.every((key, index) => key === canonicalExpected[index])
  );
}

function parseExactFindingBindings(value: unknown): FindingEvidenceBinding[] | null {
  if (!Array.isArray(value)) return null;
  const bindings: FindingEvidenceBinding[] = [];
  const identities = new Set<string>();
  let previous: FindingEvidenceBinding | null = null;
  for (const candidate of value) {
    if (
      !isRecord(candidate) ||
      !hasExactKeys(candidate, [
        "evidence_artifact_id",
        "finding_digest",
        "finding_id",
        "finding_revision",
      ]) ||
      !isNonEmptyText(candidate.evidence_artifact_id) ||
      !isSha256(candidate.finding_digest) ||
      !isNonEmptyText(candidate.finding_id) ||
      !Number.isSafeInteger(candidate.finding_revision) ||
      Number(candidate.finding_revision) < 1 ||
      identities.has(candidate.finding_id)
    ) {
      return null;
    }
    const binding: FindingEvidenceBinding = {
      evidence_artifact_id: candidate.evidence_artifact_id,
      finding_digest: candidate.finding_digest,
      finding_id: candidate.finding_id,
      finding_revision: Number(candidate.finding_revision),
    };
    if (
      previous !== null &&
      (previous.finding_id > binding.finding_id ||
        (previous.finding_id === binding.finding_id && previous.finding_revision >= binding.finding_revision))
    ) {
      return null;
    }
    identities.add(binding.finding_id);
    bindings.push(binding);
    previous = binding;
  }
  return bindings;
}

function parseCanonicalIds(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.some((item) => !isNonEmptyText(item))) return null;
  const ids = value as string[];
  const canonical = [...new Set(ids)].sort();
  return canonical.length === ids.length && canonical.every((item, index) => item === ids[index])
    ? [...ids]
    : null;
}

async function loadRepairRunHandoff(api: PatchWorkflowApi, expectedRunId: string): Promise<RepairRunHandoff> {
  const run = await api.getRun(expectedRunId);
  if (run.view_schema_version !== "run-view@1" || run.run_id !== expectedRunId) {
    throw new Error("Repair Run identity is inconsistent.");
  }
  if (run.status !== "succeeded") {
    return {
      configExportArtifactIds: [],
      patchPayloadHash: null,
      primaryPatchArtifactId: null,
      previewArtifactId: null,
      previewSnapshotId: null,
      status: run.status,
    };
  }
  if (!isNonEmptyText(run.result_artifact_id) || run.failure_artifact_id != null) {
    throw new Error("Succeeded Repair Run has no unambiguous RunResult.");
  }
  const manifest = await api.getArtifact(run.result_artifact_id);
  if (
    manifest.view_schema_version !== "artifact-payload-view@1" ||
    !Number.isSafeInteger(manifest.resource_revision) ||
    manifest.resource_revision < 1 ||
    manifest.artifact.summary_schema_version !== "artifact-summary@1" ||
    manifest.artifact.lineage_schema_version !== "lineage@2" ||
    manifest.artifact.artifact_id !== run.result_artifact_id ||
    manifest.artifact.kind !== "run_result" ||
    manifest.artifact.payload_schema_id !== "run-result@1" ||
    !isSha256(manifest.artifact.payload_hash) ||
    !isRecord(manifest.payload)
  ) {
    throw new Error("Repair RunResult envelope is invalid.");
  }
  const payload = manifest.payload;
  const summary = payload.summary;
  if (
    !isRecord(summary) ||
    !hasExactKeys(summary, [
      "finding_count",
      "outcome_code",
      "primary_artifact_kind",
      "produced_artifact_count",
      "summary_schema_version",
    ]) ||
    summary.summary_schema_version !== "run-result-summary@1" ||
    summary.outcome_code !== "repair_verified" ||
    summary.primary_artifact_kind !== "patch" ||
    !Number.isSafeInteger(summary.produced_artifact_count) ||
    !Number.isSafeInteger(summary.finding_count) ||
    !Array.isArray(payload.produced_artifact_ids) ||
    summary.produced_artifact_count !== payload.produced_artifact_ids.length ||
    summary.finding_count !== payload.finding_count ||
    run.attempt_no !== payload.attempt_no
  ) {
    throw new Error("Repair RunResult summary is invalid.");
  }
  const readableArtifactIds = generationManifestArtifactIds(manifest, expectedRunId);
  if (readableArtifactIds === null) {
    throw new Error("Repair RunResult projection is invalid.");
  }
  const produced = await Promise.all(readableArtifactIds.map((artifactId) => api.getArtifact(artifactId)));
  if (
    produced.some(
      (artifact, index) =>
        artifact.view_schema_version !== "artifact-payload-view@1" ||
        artifact.artifact.artifact_id !== readableArtifactIds[index] ||
        artifact.artifact.summary_schema_version !== "artifact-summary@1" ||
        artifact.artifact.lineage_schema_version !== "lineage@2" ||
        !isSha256(artifact.artifact.payload_hash),
    )
  ) {
    throw new Error("Repair RunResult produced Artifact identity is invalid.");
  }
  const parsed = parseGenerationCandidateManifest(
    manifest,
    expectedRunId,
    produced.map((artifact) => artifact.artifact),
  );
  const patchPayloadHash = parsed.kind === "passed" ? parsed.patch.payload_hash : null;
  if (
    parsed.kind !== "passed" ||
    parsed.runKind.kind !== "patch.repair" ||
    parsed.runKind.version !== 1 ||
    parsed.patch.artifact_id !== parsed.primaryArtifactId ||
    parsed.patch.payload_schema_id !== "patch@2" ||
    !isSha256(patchPayloadHash) ||
    parsed.configExports.length === 0 ||
    parsed.configExports.some((configExport) => configExport.payload_schema_id !== "config-export-package@1")
  ) {
    throw new Error("Repair RunResult does not bind a repaired Patch and config export.");
  }
  const previewSnapshotId = parsed.preview.version_tuple.ir_snapshot_id;
  if (
    !isNonEmptyText(previewSnapshotId) ||
    parsed.configExports.some(
      (configExport) => configExport.version_tuple.ir_snapshot_id !== previewSnapshotId,
    )
  ) {
    throw new Error("Repair output config does not bind the repaired preview snapshot.");
  }
  return {
    configExportArtifactIds: parsed.configExports
      .map((configExport) => configExport.artifact_id)
      .sort((left, right) => left.localeCompare(right)),
    patchPayloadHash,
    primaryPatchArtifactId: parsed.primaryArtifactId,
    previewArtifactId: parsed.preview.artifact_id,
    previewSnapshotId,
    status: "succeeded",
  };
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

function evidenceKindLabel(kind: string): string {
  return (
    {
      checker: "确定性检查",
      config_export: "配置导出校验",
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

function parsePatchEvidenceArtifact(
  artifact: ArtifactPayloadView | null,
  approval: ApprovalView["approval"],
): PatchEvidenceView {
  if (artifact === null || approval.evidence_set_artifact_id == null) return { kind: "unsafe" };
  if (
    artifact.view_schema_version !== "artifact-payload-view@1" ||
    !Number.isSafeInteger(artifact.resource_revision) ||
    artifact.resource_revision < 1 ||
    artifact.artifact.summary_schema_version !== "artifact-summary@1" ||
    artifact.artifact.lineage_schema_version !== "lineage@2" ||
    artifact.artifact.kind !== "validation_evidence" ||
    artifact.artifact.artifact_id !== approval.evidence_set_artifact_id ||
    artifact.artifact.payload_schema_id !== "evidence-set@1" ||
    !isSha256(artifact.artifact.payload_hash) ||
    !isRecord(artifact.payload)
  ) {
    return { kind: "unsafe" };
  }
  const payload = artifact.payload;
  if (
    !hasExactKeys(payload, [
      "evidence_schema_version",
      "finding_bindings",
      "overall_status",
      "policy_version",
      "requirements",
      "subject_artifact_id",
      "subject_digest",
      "supporting_artifact_ids",
      "target_binding",
      "validation_run_id",
    ]) ||
    payload.evidence_schema_version !== "evidence-set@1" ||
    payload.subject_artifact_id !== approval.subject_artifact_id ||
    payload.subject_digest !== approval.subject_digest ||
    !isNonEmptyText(payload.policy_version) ||
    !isNonEmptyText(payload.validation_run_id) ||
    !["failed", "passed", "unproven"].includes(String(payload.overall_status)) ||
    approval.active_validation_run_id != null ||
    approval.last_validation_failure_artifact_id != null ||
    !sameTargetBinding(payload.target_binding, approval.target_binding) ||
    !Array.isArray(payload.requirements)
  ) {
    return { kind: "unsafe" };
  }
  const findingBindings = parseExactFindingBindings(payload.finding_bindings);
  const supportingArtifactIds = parseCanonicalIds(payload.supporting_artifact_ids);
  if (findingBindings === null || supportingArtifactIds === null) return { kind: "unsafe" };
  const requirements: Extract<PatchEvidenceView, { kind: "evidence" }>["requirements"] = [];
  let previousRequirementId: string | null = null;
  for (const value of payload.requirements) {
    if (!isRecord(value)) return { kind: "unsafe" };
    const normalizedValue = {
      ...value,
      evidence_artifact_id: value.evidence_artifact_id ?? null,
      reason_code: value.reason_code ?? null,
    };
    if (
      !hasExactKeys(normalizedValue, [
        "applicability",
        "evidence_artifact_id",
        "kind",
        "reason_code",
        "requirement_id",
        "status",
        "tool_version",
      ]) ||
      !isNonEmptyText(value.requirement_id) ||
      !isNonEmptyText(value.kind) ||
      !isNonEmptyText(value.tool_version) ||
      !["required", "not_applicable"].includes(String(value.applicability)) ||
      !["passed", "failed", "unproven", "not_applicable"].includes(String(value.status)) ||
      (value.reason_code != null && !isNonEmptyText(value.reason_code)) ||
      (value.evidence_artifact_id != null && !isNonEmptyText(value.evidence_artifact_id)) ||
      (previousRequirementId !== null && previousRequirementId >= value.requirement_id) ||
      (value.applicability === "not_applicable" &&
        (value.status !== "not_applicable" ||
          value.evidence_artifact_id != null ||
          value.reason_code == null)) ||
      (value.applicability === "required" && value.status === "not_applicable") ||
      (["passed", "failed"].includes(String(value.status)) && value.evidence_artifact_id == null) ||
      (value.status === "unproven" && value.reason_code == null)
    ) {
      return { kind: "unsafe" };
    }
    requirements.push({
      evidenceArtifactId: typeof value.evidence_artifact_id === "string" ? value.evidence_artifact_id : null,
      kind: value.kind,
      reasonCode: typeof value.reason_code === "string" ? value.reason_code : null,
      requirementId: value.requirement_id,
      status: String(value.status),
      toolVersion: value.tool_version,
    });
    previousRequirementId = value.requirement_id;
  }
  const requiredStatuses = payload.requirements
    .filter((value) => isRecord(value) && value.applicability === "required")
    .map((value) => value.status);
  const derivedOverallStatus = requiredStatuses.includes("failed")
    ? "failed"
    : requiredStatuses.includes("unproven")
      ? "unproven"
      : "passed";
  if (payload.overall_status !== derivedOverallStatus) return { kind: "unsafe" };
  const target = approval.target_binding;
  if (target == null || target.subject_kind !== "patch") return { kind: "unsafe" };
  const expectedParents = new Set([
    approval.subject_artifact_id,
    target.target_artifact_id,
    ...supportingArtifactIds,
    ...findingBindings.map((binding) => binding.evidence_artifact_id),
    ...requirements.flatMap((requirement) =>
      requirement.evidenceArtifactId === null ? [] : [requirement.evidenceArtifactId],
    ),
  ]);
  const actualParents = new Set(artifact.artifact.parent_artifact_ids);
  if (
    actualParents.size !== artifact.artifact.parent_artifact_ids.length ||
    actualParents.size !== expectedParents.size ||
    [...expectedParents].some((artifactId) => !actualParents.has(artifactId)) ||
    artifact.artifact.version_tuple.ir_snapshot_id !== target.target_snapshot_id
  ) {
    return { kind: "unsafe" };
  }
  return {
    findingBindings,
    kind: "evidence",
    overallStatus: String(payload.overall_status),
    requirements,
    runId: payload.validation_run_id,
  };
}

function patchEvidenceView(data: PatchDetailData): PatchEvidenceView {
  const approval = data.approval.value.approval;
  if (!data.binding.is_current_head || approval.evidence_set_artifact_id == null) {
    return { kind: "none" };
  }
  const evidence = parsePatchEvidenceArtifact(data.evidence, approval);
  if (evidence.kind !== "evidence") return evidence;
  if (approval.status === "validation_failed") {
    return evidence.overallStatus === "failed" || evidence.overallStatus === "unproven"
      ? evidence
      : { kind: "unsafe" };
  }
  const passedEvidenceStatuses = new Set([
    "validated",
    "pending_approval",
    "auto_apply_eligible",
    "approved",
    "changes_requested",
    "rejected",
    "applied",
    "rolled_back",
  ]);
  return passedEvidenceStatuses.has(approval.status) && evidence.overallStatus === "passed"
    ? evidence
    : { kind: "unsafe" };
}

function EvidenceLedger({ data, evidence }: { data: PatchDetailData; evidence: PatchEvidenceView }) {
  const item = data.approval.value.approval;
  return (
    <div className="gf-patches__evidence-ledger">
      <h3>验证证据记录</h3>
      <p className="gf-patches__muted">
        系统会逐项核对证据要求；在核对完成前，不会把普通记录冒充确定性检查、仿真或人工建议的证明。
      </p>
      <div className="gf-patches__evidence-list">
        {evidence.kind === "none" ? (
          <p>尚无验证证据；仅仅运行成功不代表修改已经通过。</p>
        ) : evidence.kind === "unsafe" ? (
          <p role="alert">验证证据的来源、目标或数据格式不一致，页面已停止解释。</p>
        ) : (
          <div className="gf-patches__evidence-summary">
            <strong>确定性结论：{evidenceStatusLabel(evidence.overallStatus)}</strong>
            <p>已从可核验的证据记录读取 {evidence.requirements.length} 项验证要求。</p>
            {evidence.findingBindings.length === 0 ? (
              <p className="gf-patches__muted">
                {evidence.overallStatus === "passed"
                  ? "本次验证没有需要关闭的历史问题。"
                  : "验证证据中没有可交给自动修复的问题。"}
              </p>
            ) : (
              <>
                <h4>{evidence.overallStatus === "passed" ? "已关闭的历史问题" : "自动修复目标问题"}</h4>
                <ul
                  aria-label={evidence.overallStatus === "passed" ? "已关闭的历史问题" : "自动修复目标问题"}
                >
                  {evidence.findingBindings.map((binding, index) => (
                    <li key={`${binding.finding_id}@${binding.finding_revision}`}>
                      <strong>问题 {index + 1}</strong>
                      <span>已核对第 {binding.finding_revision} 版问题记录</span>
                      <a href={`/artifacts/${encodeURIComponent(binding.evidence_artifact_id)}`}>
                        查看问题证据
                      </a>
                      <TechnicalDetails
                        items={[
                          { label: "Finding ID", value: binding.finding_id },
                          { label: "Finding revision", value: String(binding.finding_revision) },
                          { label: "Finding digest", value: binding.finding_digest },
                          { label: "Evidence Artifact ID", value: binding.evidence_artifact_id },
                        ]}
                        summary="查看问题绑定技术信息"
                      />
                    </li>
                  ))}
                </ul>
              </>
            )}
            <ul>
              {evidence.requirements.map((requirement) => (
                <li key={requirement.requirementId}>
                  <span className={`u-status u-status--${requirement.status === "passed" ? "ok" : "danger"}`}>
                    {evidenceStatusLabel(requirement.status)}
                  </span>
                  <strong>{evidenceKindLabel(requirement.kind)}</strong>
                  <details>
                    <summary>技术信息</summary>
                    <code>{requirement.toolVersion}</code>
                    {requirement.reasonCode && <code>{requirement.reasonCode}</code>}
                  </details>
                  {requirement.evidenceArtifactId && (
                    <a href={`/artifacts/${encodeURIComponent(requirement.evidenceArtifactId)}`}>
                      查看该项证据
                    </a>
                  )}
                </li>
              ))}
            </ul>
            <a href={`/runs/${encodeURIComponent(evidence.runId)}`}>查看验证过程</a>
            <a href={`/artifacts/${encodeURIComponent(item.evidence_set_artifact_id!)}`}>查看完整验证依据</a>
          </div>
        )}
        {item.last_validation_failure_artifact_id && (
          <a href={`/artifacts/${encodeURIComponent(item.last_validation_failure_artifact_id)}`}>
            查看最近一次验证失败记录
          </a>
        )}
        {item.regression_evidence_artifact_ids.map((artifactId, index) => (
          <a href={`/artifacts/${encodeURIComponent(artifactId)}`} key={artifactId}>
            查看回归验证依据 {index + 1}
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

const patchOpLabels: Record<components["schemas"]["TypedOp"]["op"], string> = {
  add_entity: "新增实体",
  add_relation: "新增关系",
  delete_entity: "删除实体",
  delete_relation: "删除关系",
  replace_subgraph: "替换子图",
  set_entity_attr: "修改实体字段",
  set_relation_attr: "修改关系字段",
};

const approvalStatusLabels: Readonly<Record<string, string>> = {
  applied: "已应用",
  approved: "已批准",
  auto_apply_eligible: "可自动应用",
  changes_requested: "待修改",
  draft: "草案",
  pending_approval: "待审批",
  rejected: "未通过审批",
  rolled_back: "已回滚",
  superseded: "已被替代",
  validated: "检查已通过",
  validating: "检查中",
  validation_failed: "检查未通过",
};

const checkStatusLabels: Readonly<Record<string, string>> = {
  failed: "未通过",
  not_started: "尚未开始",
  passed: "已通过",
  running: "进行中",
};

function approvalStatusLabel(value: string): string {
  return approvalStatusLabels[value] ?? value;
}

function checkStatusLabel(value: string): string {
  return checkStatusLabels[value] ?? value;
}

function patchRiskLabel(value: string): string {
  return ({ high: "高", low: "低", medium: "中" } as Readonly<Record<string, string>>)[value] ?? value;
}

function isTechnicalDisplayValue(value: string): boolean {
  return (
    /^(?:artifact|entity|evidence|finding|item|npc|patch|quest|ref|region|relation|run|sha(?:256)?|snapshot|step):/iu.test(
      value,
    ) || /^[a-f\d]{40,}$/iu.test(value)
  );
}

function patchValueLabel(value: unknown): string {
  if (value === undefined) return "无";
  if (value === null) return "未设置";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") return new Intl.NumberFormat("zh-CN").format(value);
  if (typeof value === "string") return isTechnicalDisplayValue(value) ? "已绑定内容（见技术信息）" : value;
  if (Array.isArray(value)) {
    if (value.length === 0) return "无";
    if (value.every((item) => ["boolean", "number", "string"].includes(typeof item))) {
      return value.map(patchValueLabel).join("、");
    }
    return `已配置 ${value.length} 项内容（见技术信息）`;
  }
  if (typeof value === "object") {
    const record = value as Readonly<Record<string, unknown>>;
    const name = record.display_name ?? record.name ?? record.title ?? record.label;
    if (typeof name === "string" && name.trim()) return name.trim();
    return "已配置结构化内容（见技术信息）";
  }
  return String(value);
}

function PatchPayloadSummary({ patch }: { patch: PatchArtifactReadView["patch"] }) {
  return (
    <section aria-labelledby="patch-content-title" className="gf-patches__workspace-section">
      <header>
        <FilePenLine aria-hidden="true" size={20} />
        <div>
          <h2 id="patch-content-title">这份修改会做什么</h2>
          <p>
            {patch.ops.length} 项修改 · 影响风险：
            {patchRiskLabel(patch.side_effect_risk)}；下方可逐项核对。
          </p>
        </div>
      </header>
      {patch.expected_to_fix && patch.expected_to_fix.length > 0 && (
        <div className="gf-patches__expected-fixes">
          <strong>预期修复</strong>
          <ul>
            {patch.expected_to_fix.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}
      <ol className="gf-patches__op-list" aria-label="修改内容列表">
        {patch.ops.map((op, index) => (
          <li key={op.op_id}>
            <header>
              <span className="u-status u-status--info">{patchOpLabels[op.op]}</span>
              <strong>第 {index + 1} 项变更</strong>
            </header>
            <div className="gf-patches__op-values">
              <div>
                <span>修改前</span>
                {op.old_value == null && (op.op === "add_entity" || op.op === "add_relation") ? (
                  <span className="gf-patches__absent-value">原先不存在</span>
                ) : (
                  <span>{patchValueLabel(op.old_value)}</span>
                )}
              </div>
              <span aria-hidden="true">→</span>
              <div>
                <span>修改后</span>
                {op.new_value == null && (op.op === "delete_entity" || op.op === "delete_relation") ? (
                  <span className="gf-patches__absent-value">删除后不存在</span>
                ) : (
                  <span>{patchValueLabel(op.new_value)}</span>
                )}
              </div>
            </div>
            <TechnicalDetails
              items={[
                { label: "操作 ID", value: op.op_id },
                { label: "内部操作类型", value: op.op },
                { label: "精确修改目标", value: op.target },
                {
                  label: "原始修改前数据",
                  value: JSON.stringify(op.old_value ?? null, null, 2),
                },
                {
                  label: "原始修改后数据",
                  value: JSON.stringify(op.new_value ?? null, null, 2),
                },
              ]}
              summary="查看本项技术信息"
            />
          </li>
        ))}
      </ol>
      {patch.preconditions && patch.preconditions.length > 0 && (
        <details className="gf-patches__preconditions">
          <summary>查看 {patch.preconditions.length} 项应用前提</summary>
          <pre tabIndex={0}>{JSON.stringify(patch.preconditions, null, 2)}</pre>
        </details>
      )}
    </section>
  );
}

export function PatchDetailPage({
  api = patchWorkflowApi,
  artifactId,
}: {
  api?: PatchWorkflowApi;
  artifactId: string;
}) {
  const [searchParams, setSearchParams] = useSearchParams();
  const initialArtifactIds = useRef(deepLinkedArtifactIds(searchParams)).current;
  const workflow = useQuery({
    queryFn: () => loadPatchDetail(api, artifactId),
    queryKey: ["patch-detail", artifactId],
    refetchInterval: (query) =>
      query.state.data?.approval.value.approval.status === "validating" ? 250 : false,
    retry: false,
  });
  const profiles = useQuery({
    queryFn: () => loadProfileCatalog(api),
    queryKey: ["patch-detail", "profiles"],
    retry: false,
  });
  const artifactCatalog = useQuery({
    queryFn: () => loadArtifactCatalog(api, initialArtifactIds),
    queryKey: ["patch-detail", artifactId, "artifact-catalog", initialArtifactIds],
    retry: false,
  });
  const boundConstraintArtifactId = useMemo(() => {
    if (!workflow.data || !artifactCatalog.data) return undefined;
    return exactPatchConstraintArtifactId(workflow.data.current.value, artifactCatalog.data);
  }, [artifactCatalog.data, workflow.data]);
  const findingTargetSnapshotId = workflow.data?.target.target_snapshot_id ?? null;
  const findingAuthorityRequired =
    workflow.data?.binding.is_current_head === true &&
    workflow.data.approval.value.approval.status === "draft" &&
    findingTargetSnapshotId !== null;
  const findingCatalog = useQuery({
    enabled: findingAuthorityRequired,
    queryFn: () => loadCurrentFindingOptions(api, findingTargetSnapshotId!),
    queryKey: ["patch-detail", artifactId, "current-findings", findingTargetSnapshotId],
    retry: false,
  });
  const replayRuns = useQuery({
    queryFn: () => collectReplaySourceRuns(api),
    queryKey: ["patch-detail", "replay-source-runs"],
    retry: false,
  });
  const conflictSetId = searchParams.get("conflictSet")?.trim() || null;
  const conflicts = useQuery({
    enabled: conflictSetId !== null,
    queryFn: () => collectConflicts(api, conflictSetId!),
    queryKey: ["patch-conflicts", conflictSetId],
    retry: false,
  });
  const [diffState, setDiffState] = useState<DiffState | null>(null);
  const diffEpoch = useRef(0);
  const mutationLock = useRef(false);
  const [mutation, setMutation] = useState<MutationState | null>(null);
  const [replacementId, setReplacementId] = useState<string | null>(null);
  const [resolutions, setResolutions] = useState<ConflictResolution[]>([]);
  const [acceptedRunId, setAcceptedRunId] = useState<string | null>(null);
  const [repairRunId, setRepairRunId] = useState<string | null>(null);
  const [repairConstraintArtifactId, setRepairConstraintArtifactId] = useState<string | null>(null);
  const [applyResult, setApplyResult] = useState<WorkflowApplyResult | null>(null);
  const [confirmApply, setConfirmApply] = useState(false);
  const applyReturnFocusRef = useRef<HTMLHeadingElement>(null);
  const [validationProfileKey, setValidationProfileKey] = useState("");
  const [repairProfileKey, setRepairProfileKey] = useState("");
  const [validationCheckerKeys, setValidationCheckerKeys] = useState<Set<string>>(new Set());
  const [validationSimulationKeys, setValidationSimulationKeys] = useState<Set<string>>(new Set());
  const [repairCheckerKeys, setRepairCheckerKeys] = useState<Set<string>>(new Set());
  const [repairSimulationKeys, setRepairSimulationKeys] = useState<Set<string>>(new Set());
  const [repairExportKeys, setRepairExportKeys] = useState<Set<string>>(new Set());
  const [configArtifactIds, setConfigArtifactIds] = useState<Set<string>>(
    () => new Set(initialArtifactIds.config_export),
  );
  const [constraintArtifactId, setConstraintArtifactId] = useState(
    initialArtifactIds.constraint_snapshot[0] ?? "",
  );
  const initializedConstraintBindingFor = useRef<string | null>(null);
  const [reviewArtifactIds, setReviewArtifactIds] = useState<Set<string>>(
    () => new Set(initialArtifactIds.review_report),
  );
  const [traceArtifactIds, setTraceArtifactIds] = useState<Set<string>>(
    () => new Set(initialArtifactIds.playtest_trace),
  );
  const [regressionSuiteIds, setRegressionSuiteIds] = useState<Set<string>>(
    () => new Set(initialArtifactIds.regression_suite),
  );
  const [selectedFindingEvidenceIds, setSelectedFindingEvidenceIds] = useState<Set<string>>(new Set());
  const [focusedCheckerProfileKey, setFocusedCheckerProfileKey] = useState("");
  const [focusedCheckRunId, setFocusedCheckRunId] = useState<string | null>(null);
  const [focusedFindingCount, setFocusedFindingCount] = useState<number | null>(null);
  const integratedFocusedRunId = useRef<string | null>(null);
  const initializedProfileDefaultsFor = useRef<string | null>(null);
  const [expectedFindingBindingsText, setExpectedFindingBindingsText] = useState("[]");
  const [executionMode, setExecutionMode] = useState<"live" | "record" | "replay">("record");
  const [replaySourceRunId, setReplaySourceRunId] = useState("");
  const [seed, setSeed] = useState("1");
  const focusedCheckRun = useQuery({
    enabled: focusedCheckRunId !== null,
    queryFn: () => api.getRun(focusedCheckRunId!),
    queryKey: ["patch-detail", artifactId, "focused-check-run", focusedCheckRunId],
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && ["succeeded", "failed", "cancelled", "timed_out"].includes(status) ? false : 250;
    },
    retry: false,
  });
  const repairHandoff = useQuery({
    enabled: repairRunId !== null,
    queryFn: () => loadRepairRunHandoff(api, repairRunId!),
    queryKey: ["patch-detail", artifactId, "repair-handoff", repairRunId],
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && ["succeeded", "failed", "cancelled", "timed_out"].includes(status) ? false : 250;
    },
    retry: false,
  });

  const restartDiff = useCallback(() => {
    const data = workflow.data;
    if (!data) return;
    const baseSnapshotId = data.current.value.patch.base_snapshot_id;
    const targetSnapshotId = data.current.value.patch.target_snapshot_id;
    const epoch = ++diffEpoch.current;
    setDiffState(null);
    void loadFirstDiff(api, baseSnapshotId, targetSnapshotId)
      .then((next) => {
        if (diffEpoch.current !== epoch) return;
        setDiffState(next);
      })
      .catch((error: unknown) => {
        if (diffEpoch.current !== epoch) return;
        setDiffState({
          entries: [],
          error: normalizedError(error),
          loading: false,
          nextCursor: null,
          readSnapshotId: "",
          summary: {
            base_snapshot_id: baseSnapshotId,
            diff_schema_version: "snapshot-diff@1",
            entry_count: 0,
            target_snapshot_id: targetSnapshotId,
          },
        });
      });
  }, [api, workflow.data]);

  useEffect(() => {
    restartDiff();
  }, [restartDiff]);

  useEffect(() => {
    if (boundConstraintArtifactId === undefined || initializedConstraintBindingFor.current === artifactId) {
      return;
    }
    initializedConstraintBindingFor.current = artifactId;
    setConstraintArtifactId(boundConstraintArtifactId ?? "");
  }, [artifactId, boundConstraintArtifactId]);

  useEffect(() => {
    setResolutions([]);
  }, [conflictSetId]);

  useEffect(() => {
    const run = focusedCheckRun.data;
    if (run?.status !== "succeeded" || integratedFocusedRunId.current === run.run_id) return;
    integratedFocusedRunId.current = run.run_id;
    void findingCatalog.refetch().then((result) => {
      if (!result.data) {
        integratedFocusedRunId.current = null;
        return;
      }
      const focused = result.data.filter((option) => option.link.run_id === run.run_id);
      setSelectedFindingEvidenceIds(new Set(focused.map((option) => option.binding.evidence_artifact_id)));
      setReviewArtifactIds(new Set());
      setFocusedFindingCount(focused.length);
    });
  }, [findingCatalog, focusedCheckRun.data]);

  useEffect(() => {
    if (repairHandoff.data?.status === "succeeded" && repairHandoff.data.primaryPatchArtifactId !== null) {
      setReplacementId(repairHandoff.data.primaryPatchArtifactId);
    }
  }, [repairHandoff.data]);

  const catalog = useMemo(() => {
    if (!profiles.data || !workflow.data) return profiles.data;
    const requiredDomainIds = workflow.data.approval.value.approval.domain_scope.domain_ids;
    const filter = (items: ExecutionProfile[]) =>
      items.filter((profile) => profileCoversDomains(profile, requiredDomainIds));
    return {
      focusedChecker: filter(profiles.data.focusedChecker),
      patchRepair: filter(profiles.data.patchRepair),
      repairChecker: filter(profiles.data.repairChecker),
      repairConfigExport: filter(profiles.data.repairConfigExport),
      repairSimulation: filter(profiles.data.repairSimulation),
      validation: filter(profiles.data.validation),
      validationChecker: filter(profiles.data.validationChecker),
      validationSimulation: filter(profiles.data.validationSimulation),
    };
  }, [profiles.data, workflow.data]);
  useEffect(() => {
    if (!workflow.data || !catalog || initializedProfileDefaultsFor.current === artifactId) return;
    initializedProfileDefaultsFor.current = artifactId;
    if (catalog.validation.length === 1) {
      setValidationProfileKey(profileKey(catalog.validation[0]));
    }
    if (catalog.patchRepair.length === 1) {
      setRepairProfileKey(profileKey(catalog.patchRepair[0]));
    }
    if (catalog.validationChecker.length === 1) {
      setValidationCheckerKeys(new Set([profileKey(catalog.validationChecker[0])]));
    }
    if (catalog.repairChecker.length === 1) {
      setRepairCheckerKeys(new Set([profileKey(catalog.repairChecker[0])]));
    }
    if (catalog.repairConfigExport.length === 1) {
      setRepairExportKeys(new Set([profileKey(catalog.repairConfigExport[0])]));
    }
  }, [artifactId, catalog, workflow.data]);
  const selectedValidation = catalog?.validation.find(
    (profile) => profileKey(profile) === validationProfileKey,
  );
  const effectiveFocusedCheckerProfileKey =
    focusedCheckerProfileKey ||
    (catalog?.focusedChecker.length === 1 ? profileKey(catalog.focusedChecker[0]) : "");
  const selectedFocusedChecker = catalog?.focusedChecker.find(
    (profile) => profileKey(profile) === effectiveFocusedCheckerProfileKey,
  );
  const selectedRepair = catalog?.patchRepair.find((profile) => profileKey(profile) === repairProfileKey);
  const selectedValidationCheckers =
    catalog?.validationChecker.filter((profile) => validationCheckerKeys.has(profileKey(profile))) ?? [];
  const selectedValidationSimulations =
    catalog?.validationSimulation.filter((profile) => validationSimulationKeys.has(profileKey(profile))) ??
    [];
  const selectedRepairCheckers =
    catalog?.repairChecker.filter((profile) => repairCheckerKeys.has(profileKey(profile))) ?? [];
  const selectedRepairSimulations =
    catalog?.repairSimulation.filter((profile) => repairSimulationKeys.has(profileKey(profile))) ?? [];
  const selectedRepairExports =
    catalog?.repairConfigExport.filter((profile) => repairExportKeys.has(profileKey(profile))) ?? [];
  const selectedCurrentFindingOptions =
    findingCatalog.data?.filter((option) =>
      selectedFindingEvidenceIds.has(option.binding.evidence_artifact_id),
    ) ?? [];
  const selectedCurrentFindingBindings = selectedCurrentFindingOptions.map((option) => option.binding);
  const currentFindingEvidenceIds = new Set(
    findingCatalog.data?.map((option) => option.binding.evidence_artifact_id) ?? [],
  );
  const selectedFindingSelectionValid = [...selectedFindingEvidenceIds].every((evidenceArtifactId) =>
    currentFindingEvidenceIds.has(evidenceArtifactId),
  );
  const manuallyBoundExpectedFindings = useMemo(
    () => parseFindingBindings(expectedFindingBindingsText),
    [expectedFindingBindingsText],
  );
  const expectedFindings =
    workflow.data?.repairExpectedFindingAuthority?.bindings ?? manuallyBoundExpectedFindings;
  const configIds = useMemo(() => sortedIds(configArtifactIds), [configArtifactIds]);
  const reviewIds = useMemo(() => sortedIds(reviewArtifactIds), [reviewArtifactIds]);
  const traceIds = useMemo(() => sortedIds(traceArtifactIds), [traceArtifactIds]);
  const regressionIds = useMemo(() => sortedIds(regressionSuiteIds), [regressionSuiteIds]);
  const selectedResourcesAreValid = useMemo(() => {
    if (!artifactCatalog.data) return false;
    const includes = (kind: SelectableArtifactKind, artifactId: string) =>
      artifactCatalog.data[kind].some((artifact) => artifact.artifact_id === artifactId);
    return (
      boundConstraintArtifactId !== undefined &&
      constraintArtifactId === (boundConstraintArtifactId ?? "") &&
      (constraintArtifactId === "" || includes("constraint_snapshot", constraintArtifactId)) &&
      configIds.every((artifactId) => includes("config_export", artifactId)) &&
      reviewIds.every((artifactId) => includes("review_report", artifactId)) &&
      traceIds.every((artifactId) => includes("playtest_trace", artifactId)) &&
      regressionIds.every((artifactId) => includes("regression_suite", artifactId))
    );
  }, [
    artifactCatalog.data,
    boundConstraintArtifactId,
    configIds,
    constraintArtifactId,
    regressionIds,
    reviewIds,
    traceIds,
  ]);
  const parsedSeed = Number(seed);
  const seedIsValid = seed.trim() !== "" && Number.isSafeInteger(parsedSeed) && parsedSeed >= 0;
  const validationSeedRequired =
    (selectedValidation?.stochastic ?? false) ||
    selectedValidationCheckers.some((profile) => profile.stochastic) ||
    selectedValidationSimulations.some((profile) => profile.stochastic) ||
    regressionIds.length > 0;
  const repairSeedRequired =
    (selectedRepair?.stochastic ?? false) ||
    selectedRepairCheckers.some((profile) => profile.stochastic) ||
    selectedRepairSimulations.some((profile) => profile.stochastic) ||
    selectedRepairExports.some((profile) => profile.stochastic) ||
    regressionIds.length > 0;
  const selectedReplaySource = replayRuns.data?.find((run) => run.run_id === replaySourceRunId);

  async function reload() {
    const refreshed = await workflow.refetch();
    if (refreshed.isSuccess) {
      setMutation(null);
      setAcceptedRunId(null);
      setRepairRunId(null);
      setRepairConstraintArtifactId(null);
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

  async function loadMoreDiff() {
    const current = diffState;
    const data = workflow.data;
    if (!current?.nextCursor || !data) return;
    const epoch = ++diffEpoch.current;
    setDiffState({ ...current, error: null, loading: true });
    try {
      const next = await api.getSnapshotDiff(
        data.current.value.patch.base_snapshot_id,
        data.current.value.patch.target_snapshot_id,
        current.nextCursor,
      );
      if (diffEpoch.current !== epoch) return;
      if (
        next.page.read_snapshot_id !== current.readSnapshotId ||
        next.diff.base_snapshot_id !== current.summary.base_snapshot_id ||
        next.diff.target_snapshot_id !== current.summary.target_snapshot_id
      ) {
        throw new Error("Diff pagination authority changed.");
      }
      setDiffState({
        ...current,
        entries: [...current.entries, ...next.page.items],
        loading: false,
        nextCursor: next.page.next_cursor ?? null,
      });
    } catch (error) {
      if (diffEpoch.current === epoch) {
        setDiffState({
          ...current,
          error: normalizedError(error),
          loading: false,
        });
      }
    }
  }

  function recordRebaseResult(result: RebaseResult) {
    if (result.status === "clean" && result.new_patch_artifact_id) {
      setReplacementId(result.new_patch_artifact_id);
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.delete("conflictSet");
          return next;
        },
        { replace: true },
      );
      return;
    }
    if (result.status === "conflicted" && result.conflict_set_id) {
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.set("conflictSet", result.conflict_set_id!);
          return next;
        },
        { replace: true },
      );
      return;
    }
    throw new Error("Rebase result is internally inconsistent.");
  }

  if (workflow.isPending) {
    return (
      <div className="gf-page gf-patches">
        <StatePanel
          description="正在读取修改草案、审批进度和完整版本历史。"
          headingLevel={1}
          state="loading"
          title="正在准备修改草案"
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
            description="修改草案、审批进度或版本历史未能完整核对。为避免误操作，页面已停止后续操作。"
            headingLevel={1}
            state="error"
            title="暂时无法确认修改草案"
          />
        )}
      </div>
    );
  }

  const data = workflow.data;
  const item = data.approval.value.approval;
  const validationMutation = mutation?.label === "Patch validation" ? mutation : null;
  const selectedConstraintArtifactId = constraintArtifactId.trim();
  const reviewCandidateHref = (() => {
    if (selectedConstraintArtifactId === "") return null;
    const params = new URLSearchParams({
      snapshot: data.target.target_artifact_id,
      constraint: selectedConstraintArtifactId,
    });
    const producerRunId = data.current.value.patch.producer_run_id?.trim();
    if (producerRunId) params.set("sourceRun", producerRunId);
    return `/reviews?${params.toString()}`;
  })();
  const evidence = patchEvidenceView(data);
  const validationRunId =
    repairRunId === null
      ? (acceptedRunId ??
        item.active_validation_run_id ??
        (evidence.kind === "evidence" ? evidence.runId : null))
      : null;
  const repairFindings = evidence.kind === "evidence" ? evidence.findingBindings : null;
  const resolutionIds = new Set(resolutions.map((resolution) => resolution.conflict_id));
  const repairContinuation =
    repairRunId !== null &&
    repairHandoff.data?.status === "succeeded" &&
    repairHandoff.data.configExportArtifactIds.length > 0 &&
    repairHandoff.data.patchPayloadHash !== null &&
    repairHandoff.data.primaryPatchArtifactId === replacementId &&
    repairHandoff.data.previewArtifactId !== null &&
    repairHandoff.data.previewSnapshotId !== null
      ? {
          configExportArtifactIds: repairHandoff.data.configExportArtifactIds,
          constraintArtifactId: repairConstraintArtifactId,
          patchPayloadHash: repairHandoff.data.patchPayloadHash,
          previewArtifactId: repairHandoff.data.previewArtifactId,
          previewSnapshotId: repairHandoff.data.previewSnapshotId,
          producerRunId: repairRunId,
        }
      : undefined;
  const actionsLocked =
    mutation !== null || repairRunId !== null || replacementId !== null || !data.binding.is_current_head;
  const refDrifted = !sameRef(data.target.expected_ref, data.currentRef);
  const focusedCheckIsRunning =
    focusedCheckRunId !== null &&
    !["succeeded", "failed", "cancelled", "timed_out"].includes(focusedCheckRun.data?.status ?? "queued");
  const canRunFocusedCheck =
    item.status === "draft" &&
    selectedConstraintArtifactId !== "" &&
    selectedFocusedChecker !== undefined &&
    selectedResourcesAreValid &&
    !refDrifted &&
    !focusedCheckIsRunning &&
    !actionsLocked;
  const revisionCanBeRebased = !["applied", "rolled_back", "superseded"].includes(item.status);
  const canRebase = revisionCanBeRebased && refDrifted && data.currentRef !== null && !actionsLocked;
  const canResolve =
    revisionCanBeRebased &&
    refDrifted &&
    conflictSetId !== null &&
    conflicts.data !== undefined &&
    conflicts.data.length > 0 &&
    resolutions.length === conflicts.data.length &&
    resolutionIds.size === conflicts.data.length &&
    conflicts.data.every((conflict) => resolutionIds.has(conflict.id)) &&
    data.currentRef !== null &&
    !actionsLocked;
  const canValidate =
    item.status === "draft" &&
    selectedValidation !== undefined &&
    expectedFindings !== null &&
    findingCatalog.isSuccess &&
    selectedFindingSelectionValid &&
    selectedResourcesAreValid &&
    !refDrifted &&
    (!validationSeedRequired || seedIsValid) &&
    !actionsLocked;
  const canRepair =
    item.status === "validation_failed" &&
    item.evidence_set_artifact_id !== null &&
    item.evidence_set_artifact_id !== undefined &&
    selectedRepair !== undefined &&
    repairFindings !== null &&
    repairFindings.length > 0 &&
    selectedResourcesAreValid &&
    !refDrifted &&
    (!repairSeedRequired || seedIsValid) &&
    (executionMode !== "replay" || selectedReplaySource !== undefined) &&
    !actionsLocked;
  const canSubmit =
    item.status === "validated" && item.evidence_set_artifact_id != null && !refDrifted && !actionsLocked;
  const canApply =
    (item.status === "approved" || item.status === "auto_apply_eligible") && !refDrifted && !actionsLocked;

  function rebase() {
    if (!canRebase || data.currentRef === null) return;
    const request: components["schemas"]["PatchRebaseRequestV1"] = {
      approval_id: data.binding.approval_id,
      expected_ref: data.currentRef,
      expected_subject_head_revision: data.binding.subject_head_revision,
      expected_workflow_revision: data.binding.workflow_revision,
      ref_name: data.target.ref_name,
      request_schema_version: "patch-rebase-request@1",
    };
    runFrozen(
      "Patch rebase",
      (intent) => api.rebasePatch(data.current, request, intent),
      (result) => recordRebaseResult(result),
    );
  }

  function resolveConflicts() {
    if (!canResolve || data.currentRef === null || conflictSetId === null) return;
    const request: components["schemas"]["ResolveConflictsRequestV1"] = {
      approval_id: data.binding.approval_id,
      conflict_set_id: conflictSetId,
      expected_ref: data.currentRef,
      expected_subject_head_revision: data.binding.subject_head_revision,
      expected_workflow_revision: data.binding.workflow_revision,
      ref_name: data.target.ref_name,
      request_schema_version: "resolve-conflicts-request@1",
      resolutions,
    };
    runFrozen(
      "Conflict resolution",
      (intent) => api.resolvePatchConflicts(data.current, request, intent),
      (result) => recordRebaseResult(result),
    );
  }

  function runFocusedConstraintCheck() {
    if (!canRunFocusedCheck || !selectedFocusedChecker) return;
    const request: RunSubmissionRequest = {
      cassette_artifact_id: null,
      execution_version_plan: null,
      llm_execution_mode: "not_applicable",
      params: {
        checker_ids: [],
        checker_profile: selectedFocusedChecker.profile,
        constraint_snapshot_artifact_id: selectedConstraintArtifactId,
        defect_classes: [],
        schema_version: "checker-run@1",
        selection: { entity_ids: [], mode: "full", relation_ids: [] },
        snapshot_artifact_id: data.target.target_artifact_id,
      },
      request_schema_version: "run-submission-request@1",
      seed: null,
    };
    runFrozen(
      "Focused constraint checker",
      (intent) => api.submitRun(request, intent),
      (accepted) => {
        integratedFocusedRunId.current = null;
        setFocusedFindingCount(null);
        setFocusedCheckRunId(accepted.run_id);
      },
    );
  }

  function validate() {
    if (!canValidate || !selectedValidation || expectedFindings === null) {
      return;
    }
    setAcceptedRunId(null);
    const request: PatchValidationRequest = {
      approval_id: data.binding.approval_id,
      base_snapshot_artifact_id: data.baseArtifactId,
      candidate_config_export_artifact_ids: configIds,
      checker_profiles: selectedValidationCheckers.map((profile) => profile.profile),
      constraint_snapshot_artifact_id: constraintArtifactId.trim() || null,
      expected_findings: expectedFindings,
      expected_subject_head_revision: data.binding.subject_head_revision,
      expected_workflow_revision: data.binding.workflow_revision,
      findings: selectedCurrentFindingBindings,
      playtest_trace_artifact_ids: traceIds,
      preview_snapshot_artifact_id: data.target.target_artifact_id,
      regression_suite_artifact_ids: regressionIds,
      request_schema_version: "patch-validation-admission-request@1",
      review_artifact_ids: reviewIds,
      seed: validationSeedRequired ? parsedSeed : null,
      simulation_profiles: selectedValidationSimulations.map((profile) => profile.profile),
      subject_digest: data.binding.subject_digest,
      target: {
        expected_ref: data.target.expected_ref,
        ref_name: data.target.ref_name,
      },
      validation_policy: selectedValidation.profile,
    };
    runFrozen(
      "Patch validation",
      (intent) => api.validatePatch(data.current, request, intent),
      async (accepted) => {
        setAcceptedRunId(accepted.run_id);
        const refreshed = await workflow.refetch();
        if (!refreshed.isSuccess || !refreshed.data) {
          throw new Error("Validated Patch authority could not be reloaded.");
        }
      },
    );
  }

  function repair() {
    if (!canRepair || !selectedRepair || repairFindings === null || !item.evidence_set_artifact_id) {
      return;
    }
    const prospective: components["schemas"]["ProspectivePatchRepairRequestV1"] = {
      cassette_artifact_id: null,
      execution_version_plan: null,
      llm_execution_mode: executionMode,
      params: {
        base_snapshot_artifact_id: data.baseArtifactId,
        candidate_export_profiles: selectedRepairExports.map((profile) => profile.profile),
        checker_profiles: selectedRepairCheckers.map((profile) => profile.profile),
        constraint_snapshot_artifact_id: constraintArtifactId.trim() || null,
        expected_subject_head_revision: data.binding.subject_head_revision,
        expected_workflow_revision: data.binding.workflow_revision,
        findings: repairFindings,
        preview_snapshot_artifact_id: data.target.target_artifact_id,
        regression_suite_artifact_ids: regressionIds,
        repair_policy: selectedRepair.profile,
        schema_version: "patch-repair@1",
        simulation_profiles: selectedRepairSimulations.map((profile) => profile.profile),
        subject_patch_artifact_id: data.current.value.artifact.artifact_id,
        target: {
          expected_ref: data.target.expected_ref,
          ref_name: data.target.ref_name,
        },
        validation_evidence_artifact_id: item.evidence_set_artifact_id,
      },
      request_schema_version: "patch-repair-request@1",
      seed: repairSeedRequired ? parsedSeed : null,
    };
    const resolverRequest: components["schemas"]["ExecutionOptionResolveRequestV1"] = {
      llm_execution_mode: executionMode,
      prospective_request: prospective,
      replay_source_run_id: executionMode === "replay" ? replaySourceRunId.trim() : null,
      request_schema_version: "execution-option-resolve-request@1",
      resource_operation_id: "repair_patch_api_v1_patches__artifact_id__repair_post",
      run_kind: { kind: "patch.repair", version: 1 },
    };
    const intent = createMutationIntent();
    let frozenRequest: PatchRepairRequest | null = null;
    const sendFrozen = async () => {
      if (frozenRequest === null) {
        const option: ExecutionOptionView = await api.resolveExecutionOption(resolverRequest);
        if (
          option.resource_operation_id !== resolverRequest.resource_operation_id ||
          option.run_kind.kind !== "patch.repair" ||
          option.run_kind.version !== 1 ||
          option.llm_execution_mode !== executionMode ||
          option.source_run_id !== resolverRequest.replay_source_run_id
        ) {
          throw new Error("Resolved repair option differs from the prospective request.");
        }
        frozenRequest = {
          ...prospective,
          cassette_artifact_id: option.cassette_artifact_id,
          execution_version_plan: option.execution_version_plan,
        };
      }
      return api.repairPatch(frozenRequest, intent);
    };
    const execute = async () => {
      if (mutationLock.current) return;
      mutationLock.current = true;
      setMutation({
        error: null,
        label: "Patch repair",
        pending: true,
        retry: null,
      });
      try {
        const accepted = await sendFrozen();
        setAcceptedRunId(accepted.run_id);
        setRepairConstraintArtifactId(constraintArtifactId.trim() || null);
        setRepairRunId(accepted.run_id);
        setMutation(null);
      } catch (error) {
        const normalized = normalizedError(error);
        setMutation({
          error: normalized,
          label: "Patch repair",
          pending: false,
          retry: unknownOutcome(normalized) ? execute : null,
        });
      } finally {
        mutationLock.current = false;
      }
    };
    void execute();
  }

  function submit() {
    if (!canSubmit) return;
    const request: components["schemas"]["SubmitForApprovalRequestV1"] = {
      approval_id: data.binding.approval_id,
      expected_workflow_revision: data.binding.workflow_revision,
      request_schema_version: "submit-for-approval-request@1",
    };
    runFrozen(
      "Submit for approval",
      (intent) => api.submitPatchForApproval(data.current, request, intent),
      async () => {
        const refreshed = await workflow.refetch();
        if (!refreshed.isSuccess || !refreshed.data) {
          throw new Error("Submitted Patch authority could not be reloaded.");
        }
      },
    );
  }

  function apply() {
    if (!canApply) return;
    const request = buildPatchApplyRequest({
      ...authorityProjection(data),
    });
    setConfirmApply(false);
    runFrozen(
      "Apply Patch",
      (intent) => api.applyPatch(data.current, request, intent),
      async (result) => {
        const refreshed = await workflow.refetch();
        if (!refreshed.isSuccess || !refreshed.data) {
          throw new Error("Applied Patch authority could not be reloaded.");
        }
        verifyPatchApplyResult({
          after: authorityProjection(refreshed.data),
          afterHistory: refreshed.data.history,
          before: authorityProjection(data),
          beforeHistory: data.history,
          result,
        });
        setApplyResult(result);
      },
    );
  }

  return (
    <div className="gf-page gf-patches gf-patch-detail" data-layout="editorial-patch-detail">
      <nav aria-label="修改草案导航" className="gf-patches__back-nav">
        <a href="/patches">返回修改与版本</a>
        <a href={`/artifacts/${encodeURIComponent(data.current.value.artifact.artifact_id)}`}>查看来源记录</a>
        <a href={`/approvals/${encodeURIComponent(data.binding.approval_id)}`}>查看审批进度</a>
        <a href={`/refs/${encodeURIComponent(data.target.ref_name)}/history`}>查看版本历史</a>
      </nav>

      <header className="gf-patches__hero gf-patches__hero--detail">
        <div>
          <p className="gf-patches__kicker">内容修改草案</p>
          <h1>修改草案 · 第 {data.current.value.patch.revision} 版</h1>
          <p>{patchRationaleLabel(data.current.value.patch.rationale)}</p>
        </div>
        <span className="gf-patches__status-mark">
          {data.current.value.patch.produced_by === "agent" ? <Bot size={17} /> : <BadgeCheck size={17} />}
          {approvalStatusLabel(item.status)}
        </span>
      </header>

      <dl className="gf-patches__facts" aria-label="修改草案进度">
        <div>
          <dt>创建方式</dt>
          <dd>{data.current.value.patch.produced_by === "agent" ? "AI 起草" : "人工创建"}</dd>
        </div>
        <div>
          <dt>审批进度</dt>
          <dd>{approvalStatusLabel(item.status)}</dd>
        </div>
        <div>
          <dt>内容检查</dt>
          <dd>{checkStatusLabel(data.current.value.validation_status)}</dd>
        </div>
        <div>
          <dt>回归检查</dt>
          <dd>{checkStatusLabel(data.current.value.regression_status)}</dd>
        </div>
      </dl>

      <TechnicalDetails
        items={[
          {
            copyLabel: "复制修改草案 Artifact ID",
            label: "Patch Artifact ID",
            value: data.current.value.artifact.artifact_id,
          },
          {
            copyLabel: "复制修改草案 ETag",
            label: "ETag",
            value: data.current.etag,
          },
          {
            label: "Subject head revision",
            value: String(data.binding.subject_head_revision),
          },
          {
            label: "Workflow revision",
            value: String(data.binding.workflow_revision),
          },
          {
            copyLabel: "复制修改草案摘要",
            label: "Subject digest",
            value: data.binding.subject_digest,
          },
          ...(patchRationaleLabel(data.current.value.patch.rationale) === data.current.value.patch.rationale
            ? []
            : [
                {
                  label: "原始修改理由",
                  value: data.current.value.patch.rationale,
                },
              ]),
        ]}
        summary="查看草案技术信息"
      />

      {!data.binding.is_current_head && (
        <StatePanel
          action={
            replacementId ? (
              <a href={replacementHref(replacementId, repairContinuation)}>打开后续版本</a>
            ) : undefined
          }
          description="当前草案已被更新版本替代，所有修改操作均已停止。"
          state="terminal"
          title="这份草案已被替代"
        />
      )}
      {refDrifted && data.binding.is_current_head && (
        <StatePanel
          action={
            canRebase ? (
              <button className="gf-secondary-button" onClick={rebase} type="button">
                基于当前正式版本重新计算
              </button>
            ) : undefined
          }
          description="正式内容已在别处更新。请先基于最新版本重新计算，再继续验证、修复或审批。"
          state="error"
          title="草案基于的版本已过期"
        />
      )}
      {mutation && mutation.label !== "Patch validation" && !mutation.pending && (
        <MutationFailure onReload={() => void reload()} state={mutation} />
      )}
      {mutation?.pending && mutation.label !== "Patch validation" && (
        <StatePanel
          description={`${mutation.label} 请求已冻结并提交。`}
          state="loading"
          title="正在执行操作"
        />
      )}
      {repairRunId &&
        (repairHandoff.isPending ? (
          <StatePanel
            description="正在确认自动修复任务的最新状态。"
            state="loading"
            title="自动修复正在运行"
          />
        ) : repairHandoff.isError ? (
          <StatePanel
            action={
              <button className="gf-secondary-button" onClick={() => void reload()} type="button">
                重新读取服务器状态
              </button>
            }
            description="自动修复的任务、结果或产出未能完整核对，因此不会提供新的修改草案入口。"
            state="error"
            title="暂时无法确认修复结果"
          />
        ) : repairHandoff.data.status === "succeeded" ? (
          <StatePanel
            description="修复结果、修改草案和配置导出均已核对；请从下方经过重验的入口继续。"
            state="terminal"
            title="自动修复结果已验证"
          />
        ) : ["failed", "cancelled", "timed_out"].includes(repairHandoff.data.status) ? (
          <StatePanel
            action={
              <button className="gf-secondary-button" onClick={() => void reload()} type="button">
                重新读取服务器状态
              </button>
            }
            description="自动修复已经结束，但没有形成可采信的新草案；当前版本保持不变。"
            state="error"
            title={
              repairHandoff.data.status === "failed"
                ? "自动修复失败"
                : repairHandoff.data.status === "cancelled"
                  ? "自动修复已取消"
                  : "自动修复已超时"
            }
          />
        ) : (
          <StatePanel
            description="页面会继续读取任务状态，无需重复提交。"
            state="loading"
            title="自动修复正在运行"
          />
        ))}
      {replacementId && (
        <ReplacementReceipt
          api={api}
          continuation={repairContinuation}
          previous={data}
          replacementId={replacementId}
        />
      )}

      <PatchPayloadSummary patch={data.current.value.patch} />

      <section aria-labelledby="patch-diff-title" className="gf-patches__workspace-section">
        <header>
          <GitBranch aria-hidden="true" size={20} />
          <div>
            <h2 id="patch-diff-title">修改前后对比</h2>
            <p>对照草案开始时的内容、当前正式内容和修改后的预览，确认没有覆盖别人的新改动。</p>
          </div>
        </header>
        <dl className="gf-patches__three-way-summary">
          <div>
            <dt>草案基于</dt>
            <dd>创建草案时的内容版本</dd>
          </div>
          <div>
            <dt>当前正式内容</dt>
            <dd>{data.currentRef ? `当前内容 · 第 ${data.currentRef.revision} 版` : "尚无正式内容"}</dd>
          </div>
          <div>
            <dt>修改后预览</dt>
            <dd>待检查并应用的新版本</dd>
          </div>
        </dl>
        <TechnicalDetails
          items={[
            {
              label: "Base Snapshot",
              value: data.current.value.patch.base_snapshot_id,
            },
            {
              label: "Current Snapshot",
              value: data.currentSnapshotId ?? "ref 不存在",
            },
            {
              label: "Proposed Artifact",
              value: data.target.target_artifact_id,
            },
            {
              label: "Proposed Snapshot",
              value: data.target.target_snapshot_id,
            },
          ]}
          summary="查看对比技术信息"
        />
        {diffState === null ? (
          <StatePanel description="正在读取字段级 Diff。" state="loading" title="正在读取 Diff" />
        ) : diffState.error ? (
          <StatePanel
            action={
              <button className="gf-secondary-button" onClick={restartDiff} type="button">
                从第一页重新读取 Diff
              </button>
            }
            description="Diff 分页 authority 无法闭合；旧 entries 已停止使用。"
            state="error"
            title="Diff 不可用"
          />
        ) : (
          <>
            <SnapshotDiffView diff={diffState.summary} entries={diffState.entries} />
            {diffState.nextCursor && (
              <button
                className="gf-secondary-button"
                disabled={diffState.loading}
                onClick={() => void loadMoreDiff()}
                type="button"
              >
                {diffState.loading ? "正在加载…" : "加载更多 Diff entries"}
              </button>
            )}
          </>
        )}
      </section>

      <section aria-labelledby="patch-rebase-title" className="gf-patches__workspace-section">
        <header>
          <GitMerge aria-hidden="true" size={20} />
          <div>
            <h2 id="patch-rebase-title">处理版本冲突</h2>
            <p>如果正式内容在草案创建后又被修改，请先重新计算，再逐项决定保留哪一版。</p>
          </div>
        </header>
        <dl className="gf-patches__target-ledger">
          <div>
            <dt>草案原本基于</dt>
            <dd>
              {data.target.expected_ref
                ? `当前内容 · 第 ${data.target.expected_ref.revision} 版`
                : "当时尚无正式内容"}
            </dd>
          </div>
          <div>
            <dt>现在正式内容</dt>
            <dd>{data.currentRef ? `当前内容 · 第 ${data.currentRef.revision} 版` : "尚无正式内容"}</dd>
          </div>
        </dl>
        <TechnicalDetails
          items={[
            {
              label: "Frozen expected ref",
              value: data.target.expected_ref
                ? `${data.target.expected_ref.artifact_id}@${data.target.expected_ref.revision}`
                : "null",
            },
            {
              label: "Current ref",
              value: data.currentRef
                ? `${data.currentRef.artifact_id}@${data.currentRef.revision}`
                : "不存在",
            },
          ]}
          summary="查看版本绑定技术信息"
        />
        <button disabled={!canRebase} onClick={rebase} type="button">
          重新基于当前版本计算
        </button>
        {conflictSetId &&
          (conflicts.isPending ? (
            <StatePanel description="正在读取全部冲突项。" state="loading" title="正在读取冲突" />
          ) : conflicts.isError ? (
            <StatePanel
              action={
                <button
                  className="gf-secondary-button"
                  onClick={() => void conflicts.refetch()}
                  type="button"
                >
                  从第一页重新读取冲突
                </button>
              }
              description="冲突列表读取失败，已停止使用不完整数据；请从第一页重新读取。"
              state="error"
              title="冲突不可用"
            />
          ) : conflicts.data.length === 0 ? (
            <StatePanel
              description="系统没有返回可处理的冲突项，未保存任何选择。"
              state="error"
              title="冲突集合为空"
            />
          ) : (
            <>
              <MergeResolver
                conflicts={conflicts.data}
                key={conflictSetId}
                onResolutionsChange={setResolutions}
              />
              <button disabled={!canResolve} onClick={resolveConflicts} type="button">
                保存全部冲突处理结果
              </button>
            </>
          ))}
      </section>

      <section aria-labelledby="patch-evidence-title" className="gf-patches__workspace-section">
        <header>
          <ShieldCheck aria-hidden="true" size={20} />
          <div>
            <h2 id="patch-evidence-title">验证与回归证据</h2>
            <p>只有通过逐项核验的证据才能支持结论；运行成功本身不等于验证通过。</p>
          </div>
        </header>
        <EvidenceLedger data={data} evidence={evidence} />
        {item.active_validation_run_id && (
          <a href={`/runs/${encodeURIComponent(item.active_validation_run_id)}`}>查看正在进行的验证</a>
        )}
        {acceptedRunId && (
          <div className="gf-patches__live-receipt" role="status">
            <a href={`/runs/${encodeURIComponent(acceptedRunId)}`}>查看已受理的运行</a>
          </div>
        )}
      </section>

      <section aria-labelledby="patch-input-title" className="gf-patches__workspace-section">
        <header>
          <PlayCircle aria-hidden="true" size={20} />
          <div>
            <h2 id="patch-input-title">验证所需内容</h2>
            <p>从已校验类型的完整目录中选择规则、配置和历史证据，不需要手工复制任何编号。</p>
          </div>
        </header>
        {profiles.isError ||
        artifactCatalog.isError ||
        (findingAuthorityRequired && findingCatalog.isError) ? (
          <div className="gf-patches__blocked-inputs">
            <StatePanel
              description={
                findingAuthorityRequired && findingCatalog.isError
                  ? "当前问题记录无法与产生它的检查任务完整对应，验证已停止。"
                  : artifactCatalog.isError
                    ? "所需内容目录未能完整核对，验证和自动修复已停止。"
                    : "验证方案目录暂时无法读取，验证和自动修复已停止。"
              }
              state="error"
              title={
                findingAuthorityRequired && findingCatalog.isError
                  ? "无法确认问题记录"
                  : artifactCatalog.isError
                    ? "资源目录无法确认"
                    : "验证方案不可用"
              }
            />
            <div className="gf-patches__action-row">
              <button disabled type="button">
                开始验证
              </button>
              <button disabled type="button">
                开始自动修复
              </button>
            </div>
          </div>
        ) : profiles.isPending ||
          artifactCatalog.isPending ||
          (findingAuthorityRequired && findingCatalog.isPending) ||
          !catalog ||
          !artifactCatalog.data ? (
          <StatePanel
            description="正在读取可用方案、验证资源和当前问题证据。"
            state="loading"
            title="正在准备可选择的验证资源"
          />
        ) : (
          <div className="gf-patches__execution-form">
            <div className="gf-patches__form-stage-heading gf-patches__form-wide">
              <span aria-hidden="true">1</span>
              <div>
                <h3>先验证当前修改</h3>
                <p>选择本轮要执行的规则检查和仿真；下方约束由这份修改自动绑定。</p>
              </div>
            </div>
            <ProfileSelect
              label="验证方案"
              onChange={setValidationProfileKey}
              profiles={catalog.validation}
              value={validationProfileKey}
            />
            <ProfileChecklist
              label="验证使用的确定性检查"
              onChange={setValidationCheckerKeys}
              profiles={catalog.validationChecker}
              selected={validationCheckerKeys}
            />
            <ProfileChecklist
              label="验证使用的经济仿真"
              onChange={setValidationSimulationKeys}
              profiles={catalog.validationSimulation}
              selected={validationSimulationKeys}
            />
            <ArtifactResourcePicker
              artifacts={artifactCatalog.data.constraint_snapshot}
              kind="constraint_snapshot"
              locked
              mode="single"
              onChange={(value) => setConstraintArtifactId([...value][0] ?? "")}
              selected={constraintArtifactId ? new Set([constraintArtifactId]) : new Set()}
              subjectSnapshotId={data.current.value.patch.target_snapshot_id}
            />
            {boundConstraintArtifactId === undefined && (
              <p className="gf-patches__form-wide" role="alert">
                无法确认这份修改实际绑定的规则，验证已安全停止。请重新生成修改或联系管理员检查血缘。
              </p>
            )}
            {reviewCandidateHref && (
              <div className="gf-patches__form-wide">
                <div className="gf-patches__action-row">
                  <a className="gf-secondary-button" href={reviewCandidateHref}>
                    审查当前修改候选
                  </a>
                  <button disabled={!canRunFocusedCheck} onClick={runFocusedConstraintCheck} type="button">
                    聚焦检查所选约束
                  </button>
                </div>
                <p className="gf-patches__muted">
                  系统会带上当前修改候选和所选规则；来源运行只用于辅助定位。
                </p>
                <p className="gf-patches__muted">
                  聚焦检查只执行所选规则，不混入完整检查中的其他未证明项；完成后会自动选择对应问题证据组。
                </p>
                {catalog.focusedChecker.length > 1 && (
                  <ProfileSelect
                    label="聚焦检查方案"
                    onChange={setFocusedCheckerProfileKey}
                    profiles={catalog.focusedChecker}
                    value={effectiveFocusedCheckerProfileKey}
                  />
                )}
                {catalog.focusedChecker.length === 0 && (
                  <p className="gf-patches__muted">没有可用于聚焦检查的方案。</p>
                )}
                {focusedCheckRunId && (
                  <div className="gf-patches__live-receipt" role="status">
                    <a href={`/runs/${encodeURIComponent(focusedCheckRunId)}`}>查看聚焦检查运行</a>
                    <span>
                      {focusedCheckRun.data?.status === "succeeded"
                        ? focusedFindingCount === null
                          ? "检查完成，正在载入问题记录。"
                          : focusedFindingCount > 0
                            ? `已载入 ${focusedFindingCount} 个问题。`
                            : "检查完成，所选约束未发现缺陷。"
                        : focusedCheckRun.data?.status === "failed"
                          ? "检查运行失败；修改草案与证据选择未改变。"
                          : focusedCheckRun.data?.status === "cancelled"
                            ? "检查已取消；修改草案与证据选择未改变。"
                            : focusedCheckRun.data?.status === "timed_out"
                              ? "检查超时；修改草案与证据选择未改变。"
                              : "确定性检查运行中。"}
                    </span>
                  </div>
                )}
              </div>
            )}
            <div className="gf-patches__form-stage-heading gf-patches__form-wide">
              <span aria-hidden="true">2</span>
              <div>
                <h3>验证失败后：自动修复并重新检查</h3>
                <p>这里的检查只在自动修复生成新草案后执行，不会与第一步重复运行。</p>
              </div>
            </div>
            <ProfileSelect
              label="自动修复方案"
              onChange={setRepairProfileKey}
              profiles={catalog.patchRepair}
              value={repairProfileKey}
            />
            <ProfileChecklist
              label="修复后的确定性检查"
              onChange={setRepairCheckerKeys}
              profiles={catalog.repairChecker}
              selected={repairCheckerKeys}
            />
            <ProfileChecklist
              label="修复后的经济仿真"
              onChange={setRepairSimulationKeys}
              profiles={catalog.repairSimulation}
              selected={repairSimulationKeys}
            />
            <ProfileChecklist
              label="修复后的配置导出验证"
              onChange={setRepairExportKeys}
              profiles={catalog.repairConfigExport}
              selected={repairExportKeys}
            />
            <div className="gf-patches__form-stage-heading gf-patches__form-wide">
              <span aria-hidden="true">3</span>
              <div>
                <h3>可选：引用已有验证结果</h3>
                <p>只选择确实属于当前修改的配置、审查、试玩或回归结果；每项都会标明内容和版本关系。</p>
              </div>
            </div>
            <ArtifactResourcePicker
              artifacts={artifactCatalog.data.config_export}
              kind="config_export"
              mode="multiple"
              onChange={setConfigArtifactIds}
              selected={configArtifactIds}
              subjectSnapshotId={data.current.value.patch.target_snapshot_id}
            />
            <ArtifactResourcePicker
              artifacts={artifactCatalog.data.review_report}
              kind="review_report"
              mode="multiple"
              onChange={setReviewArtifactIds}
              selected={reviewArtifactIds}
              subjectSnapshotId={data.current.value.patch.target_snapshot_id}
            />
            <ArtifactResourcePicker
              artifacts={artifactCatalog.data.playtest_trace}
              kind="playtest_trace"
              mode="multiple"
              onChange={setTraceArtifactIds}
              selected={traceArtifactIds}
              subjectSnapshotId={data.current.value.patch.target_snapshot_id}
            />
            <ArtifactResourcePicker
              artifacts={artifactCatalog.data.regression_suite}
              kind="regression_suite"
              mode="multiple"
              onChange={setRegressionSuiteIds}
              selected={regressionSuiteIds}
              subjectSnapshotId={data.current.value.patch.target_snapshot_id}
            />
            {findingAuthorityRequired && (
              <CurrentFindingSelector
                onChange={setSelectedFindingEvidenceIds}
                options={findingCatalog.data ?? []}
                selected={selectedFindingEvidenceIds}
              />
            )}
            {data.repairExpectedFindingAuthority ? (
              <div className="gf-patches__form-wide">
                <StatePanel
                  action={
                    <a
                      href={`/artifacts/${encodeURIComponent(
                        data.repairExpectedFindingAuthority.evidenceArtifactId,
                      )}`}
                    >
                      查看前序验证证据
                    </a>
                  }
                  description={`已从前序失败证据恢复 ${data.repairExpectedFindingAuthority.bindings.length} 项历史问题。`}
                  state="terminal"
                  title="自动修复验证上下文已接续"
                />
              </div>
            ) : (
              <details className="gf-patches__advanced-bindings gf-patches__form-wide">
                <summary>高级：精确问题证据绑定</summary>
                <p className="gf-patches__muted">
                  仅用于首次修改草案关闭历史问题；后续自动修复会从前序保留证据自动恢复，不能粘贴或推断。
                </p>
                <label>
                  <span>历史问题证据绑定（JSON）</span>
                  <textarea
                    aria-invalid={manuallyBoundExpectedFindings === null}
                    onChange={(event) => setExpectedFindingBindingsText(event.target.value)}
                    rows={6}
                    value={expectedFindingBindingsText}
                  />
                </label>
              </details>
            )}
            <label>
              <span>随机种子（仅仿真或 AI 方案需要）</span>
              <input min="0" onChange={(event) => setSeed(event.target.value)} type="number" value={seed} />
            </label>
            <p className="gf-patches__muted">
              验证种子{validationSeedRequired ? "必填" : "不需要"} · 自动修复种子
              {repairSeedRequired ? "必填" : "不需要"}
            </p>
            <label>
              <span>自动修复的 AI 运行方式</span>
              <select
                onChange={(event) => {
                  const next = event.target.value as typeof executionMode;
                  setExecutionMode(next);
                  if (next !== "replay") setReplaySourceRunId("");
                }}
                value={executionMode}
              >
                <option value="record">在线生成并保存回放</option>
                <option value="live">在线生成</option>
                <option value="replay">使用历史回放</option>
              </select>
            </label>
            {executionMode === "replay" && (
              <div>
                <label>
                  <span>历史回放来源</span>
                  <select
                    disabled={replayRuns.isPending || replayRuns.isError || replayRuns.data.length === 0}
                    onChange={(event) => setReplaySourceRunId(event.target.value)}
                    value={replaySourceRunId}
                  >
                    <option value="">
                      {replayRuns.isPending
                        ? "正在读取可回放运行…"
                        : replayRuns.isError
                          ? "可回放运行目录不可用"
                          : replayRuns.data.length === 0
                            ? "没有可回放运行"
                            : "请选择一个可回放运行"}
                    </option>
                    {replayRuns.data?.map((run) => (
                      <option key={run.run_id} value={run.run_id}>
                        {replayRunLabel(run)}
                      </option>
                    ))}
                  </select>
                </label>
                {replayRuns.isError ? (
                  <p className="gf-patches__muted" role="alert">
                    无法读取可回放运行；修复将保持禁用。
                  </p>
                ) : replayRuns.data?.length === 0 ? (
                  <p className="gf-patches__muted">没有可回放运行。请先完成一次在线生成或录制。</p>
                ) : null}
              </div>
            )}
            <div className="gf-patches__action-row gf-patches__form-wide">
              <button disabled={!canValidate} onClick={validate} type="button">
                开始验证
              </button>
              <button disabled={!canRepair} onClick={repair} type="button">
                开始自动修复
              </button>
            </div>
            {validationMutation && (
              <div className="gf-patches__form-wide gf-patches__validation-feedback">
                {validationMutation.pending ? (
                  <StatePanel
                    description="正在提交本次验证，页面会在任务受理后继续显示运行状态。"
                    state="loading"
                    title="正在开始验证"
                  />
                ) : (
                  <MutationFailure onReload={() => void reload()} state={validationMutation} />
                )}
              </div>
            )}
            {validationRunId && (
              <div className="gf-patches__form-wide gf-patches__validation-feedback">
                <StatePanel
                  action={
                    <div className="gf-cluster">
                      <a href={`/runs/${encodeURIComponent(validationRunId)}`}>打开验证运行</a>
                      <a href="#patch-evidence-title">查看验证结果</a>
                    </div>
                  }
                  description={
                    item.status === "validated"
                      ? "本次验证已经通过，完整证据已归入上方的验证与回归证据。"
                      : item.status === "validation_failed"
                        ? "本次验证已结束但未通过，可在上方查看具体问题并决定是否修复。"
                        : "验证任务已经受理，页面会持续读取状态；无需重复点击。"
                  }
                  state={
                    item.status === "validated"
                      ? "terminal"
                      : item.status === "validation_failed"
                        ? "error"
                        : "streaming"
                  }
                  title={
                    item.status === "validated"
                      ? "验证通过"
                      : item.status === "validation_failed"
                        ? "验证未通过"
                        : "验证正在运行"
                  }
                />
              </div>
            )}
          </div>
        )}
      </section>

      <section aria-labelledby="patch-approval-title" className="gf-patches__workspace-section">
        <header>
          <Send aria-hidden="true" size={20} />
          <div>
            <h2 id="patch-approval-title" ref={applyReturnFocusRef} tabIndex={-1}>
              提交审批并应用
            </h2>
            <p>验证通过后交给另一位负责人审批；只有最终应用会更新正式内容。</p>
          </div>
        </header>
        <div className="gf-patches__approval-actions">
          <button disabled={!canSubmit} onClick={submit} type="button">
            提交独立审批
          </button>
          <a className="gf-secondary-button" href={`/approvals/${encodeURIComponent(item.approval_id)}`}>
            打开审批详情
          </a>
          <button disabled={!canApply} onClick={() => setConfirmApply(true)} type="button">
            {item.status === "auto_apply_eligible" ? "应用符合策略的修改" : "应用已批准的修改"}
          </button>
        </div>
        {applyResult && (
          <div className="gf-patches__live-receipt" role="status">
            <StatePanel
              action={<a href={`/refs/${encodeURIComponent(applyResult.ref_name)}/history`}>查看版本历史</a>}
              description={`正式内容已更新为第 ${applyResult.ref_value.revision} 版。`}
              state="terminal"
              title="修改已应用"
            />
            <TechnicalDetails
              items={[
                { label: "发布位置", value: applyResult.ref_name },
                { label: "Artifact ID", value: applyResult.ref_value.artifact_id },
              ]}
              summary="查看应用结果技术信息"
            />
          </div>
        )}
      </section>

      <section aria-labelledby="patch-history-title" className="gf-patches__workspace-section">
        <header>
          <GitBranch aria-hidden="true" size={20} />
          <div>
            <h2 id="patch-history-title">正式版本历史</h2>
            <p>应用修改前不会新增版本；每次成功应用都会留下不可改写的历史记录。</p>
          </div>
        </header>
        <ol className="gf-patches__history-list">
          {data.history.map((entry) => (
            <li key={`${entry.value.revision}:${entry.value.artifact_id}`}>
              <span>第 {entry.value.revision} 版</span>
              <TechnicalDetails
                items={[{ label: "Artifact ID", value: entry.value.artifact_id }]}
                summary="查看版本技术信息"
              />
            </li>
          ))}
        </ol>
      </section>

      <footer className="gf-patches__principle">
        <AlertTriangle aria-hidden="true" size={17} />
        <span>权限不足、版本过期或本人审批等情况会安全停止；页面不会猜测或绕过服务端授权。</span>
        <Sparkles aria-hidden="true" size={17} />
      </footer>

      <ConfirmDialog
        confirmLabel="确认应用"
        description="这会把已验证且获批的修改更新到正式内容；修改草案和审计记录会保留。"
        onCancel={() => setConfirmApply(false)}
        onConfirm={apply}
        open={confirmApply}
        returnFocusRef={applyReturnFocusRef}
        title="确认应用已批准的修改？"
      />
    </div>
  );
}
