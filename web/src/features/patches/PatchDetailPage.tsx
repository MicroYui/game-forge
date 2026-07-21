import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  BadgeCheck,
  Bot,
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
import { CopyableText } from "../../components/tables";
import { ConfirmDialog, ProblemPanel, StatePanel } from "../../components/ui";
import {
  buildPatchApplyRequest,
  currentRefFromCompleteHistory,
  verifyPatchApplyResult,
  verifyPatchWorkflowAuthority,
  verifyReplacementRevision,
} from "./authority";
import { patchWorkflowApi, type PatchWorkflowApi, type VersionedResource } from "./api";
import "./patches.css";

type ApprovalView = components["schemas"]["ApprovalViewV1"];
type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
type ConflictResolution = components["schemas"]["ConflictResolution"];
type ExecutionOptionView = components["schemas"]["ExecutionOptionViewV1"];
type ExecutionProfile = components["schemas"]["ExecutionProfileViewV1"];
type FindingEvidenceBinding = components["schemas"]["FindingEvidenceBindingV1"];
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
type RefValue = components["schemas"]["RefValue"];
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
  target: Readonly<PatchTargetBinding>;
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

interface ProfileCatalog {
  repairChecker: ExecutionProfile[];
  repairConfigExport: ExecutionProfile[];
  repairSimulation: ExecutionProfile[];
  patchRepair: ExecutionProfile[];
  validation: ExecutionProfile[];
  validationChecker: ExecutionProfile[];
  validationSimulation: ExecutionProfile[];
}

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("Patch workflow operation failed.");
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

function parseLines(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/\r?\n/u)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
}

function parseFindingBindings(value: string): FindingEvidenceBinding[] | null {
  if (value.trim() === "") return [];
  try {
    const parsed: unknown = JSON.parse(value);
    if (!Array.isArray(parsed)) return null;
    return parsed as FindingEvidenceBinding[];
  } catch {
    return null;
  }
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
    patchRepair: patchRepair.filter((profile) => supportsRunKind(profile, "patch.repair")),
    repairChecker: checker.filter((profile) => supportsRunKind(profile, "patch.repair")),
    repairConfigExport: configExport.filter((profile) => supportsRunKind(profile, "patch.repair")),
    repairSimulation: simulation.filter((profile) => supportsRunKind(profile, "patch.repair")),
    validation: validation.filter((profile) => supportsRunKind(profile, "patch.validate")),
    validationChecker: checker.filter((profile) => supportsRunKind(profile, "patch.validate")),
    validationSimulation: simulation.filter((profile) => supportsRunKind(profile, "patch.validate")),
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
  const [baseArtifactId, history] = await Promise.all([
    resolvePatchBaseArtifactId(api, current.value, target),
    collectCurrentRefHistory(api, target),
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
  const [evidence, failure] = await Promise.all([
    approval.value.approval.evidence_set_artifact_id
      ? api.getArtifact(approval.value.approval.evidence_set_artifact_id)
      : Promise.resolve(null),
    approval.value.approval.last_validation_failure_artifact_id
      ? api.getArtifact(approval.value.approval.last_validation_failure_artifact_id)
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
            {profile.display_name} · {profileKey(profile)}
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
        <p className="gf-patches__muted">无 active profile</p>
      ) : (
        profiles.map((profile) => {
          const key = profileKey(profile);
          return (
            <label key={key}>
              <input
                checked={selected.has(key)}
                onChange={(event) => {
                  const next = new Set(selected);
                  if (event.target.checked) next.add(key);
                  else next.delete(key);
                  onChange(next);
                }}
                type="checkbox"
              />
              <span>{key}</span>
            </label>
          );
        })
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
          重新读取 exact server state
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
              重试同一 intent
            </button>
          )}
          <button className="gf-secondary-button" onClick={onReload} type="button">
            重新读取服务器状态
          </button>
        </div>
      }
      description={
        state.retry
          ? `${state.label} 结果未知；请求和 Idempotency-Key 已冻结。`
          : `${state.label} 失败；重新读取 authority 后才能发起新操作。`
      }
      state="error"
      title={state.retry ? "操作结果未知" : "工作流操作失败"}
    />
  );
}

function ReplacementReceipt({
  api,
  previous,
  replacementId,
}: {
  api: PatchWorkflowApi;
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
      return next;
    },
    queryKey: ["patch-replacement", previous.current.value.artifact.artifact_id, replacementId],
    retry: false,
  });
  if (replacement.isPending) {
    return (
      <StatePanel description="正在重验新旧 workflow authority。" state="loading" title="正在读取新修订" />
    );
  }
  if (replacement.isError) {
    return (
      <StatePanel
        description="新修订未能与 superseded predecessor 闭合；不会提供继续入口。"
        state="error"
        title="新 Patch revision authority 不一致"
      />
    );
  }
  return (
    <div className="gf-patches__live-receipt" role="status">
      <StatePanel
        action={<a href={`/patches/${encodeURIComponent(replacementId)}`}>打开新 Patch revision</a>}
        description={`新 revision ${replacement.data.subject.patch.revision} 为 draft；旧验证、证据与审批决定不继承。`}
        state="terminal"
        title="已创建独立 Patch revision"
      />
    </div>
  );
}

function EvidenceLedger({ data }: { data: PatchDetailData }) {
  const item = data.approval.value.approval;
  return (
    <div className="gf-patches__evidence-ledger">
      <h3>Workflow evidence Artifact ledger</h3>
      <p className="gf-patches__muted">
        中性索引：未解析 EvidenceSet requirements 前，不把这些 Artifact 冒充 deterministic、simulation 或
        suggestion 证明。
      </p>
      <div className="gf-patches__evidence-list">
        {item.evidence_set_artifact_id ? (
          <a href={`/artifacts/${encodeURIComponent(item.evidence_set_artifact_id)}`}>
            EvidenceSet · {item.evidence_set_artifact_id}
          </a>
        ) : (
          <p>尚无 EvidenceSet；Run status 不会被当作验证 verdict。</p>
        )}
        {item.last_validation_failure_artifact_id && (
          <a href={`/artifacts/${encodeURIComponent(item.last_validation_failure_artifact_id)}`}>
            最近 validation failure · {item.last_validation_failure_artifact_id}
          </a>
        )}
        {item.regression_evidence_artifact_ids.map((artifactId) => (
          <a href={`/artifacts/${encodeURIComponent(artifactId)}`} key={artifactId}>
            Regression / companion evidence · {artifactId}
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

export function PatchDetailPage({
  api = patchWorkflowApi,
  artifactId,
}: {
  api?: PatchWorkflowApi;
  artifactId: string;
}) {
  const [searchParams, setSearchParams] = useSearchParams();
  const workflow = useQuery({
    queryFn: () => loadPatchDetail(api, artifactId),
    queryKey: ["patch-detail", artifactId],
    retry: false,
  });
  const profiles = useQuery({
    queryFn: () => loadProfileCatalog(api),
    queryKey: ["patch-detail", "profiles"],
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
  const [configArtifactIds, setConfigArtifactIds] = useState("");
  const [constraintArtifactId, setConstraintArtifactId] = useState(
    searchParams.get("constraint")?.trim() ?? "",
  );
  const [reviewArtifactIds, setReviewArtifactIds] = useState(searchParams.get("review")?.trim() ?? "");
  const [traceArtifactIds, setTraceArtifactIds] = useState(searchParams.get("trace")?.trim() ?? "");
  const [regressionSuiteIds, setRegressionSuiteIds] = useState("");
  const [expectedFindingBindingsText, setExpectedFindingBindingsText] = useState("[]");
  const [findingBindingsText, setFindingBindingsText] = useState("[]");
  const [executionMode, setExecutionMode] = useState<"live" | "record" | "replay">("record");
  const [replaySourceRunId, setReplaySourceRunId] = useState("");
  const [seed, setSeed] = useState("1");

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
    setResolutions([]);
  }, [conflictSetId]);

  const catalog = useMemo(() => {
    if (!profiles.data || !workflow.data) return profiles.data;
    const requiredDomainIds = workflow.data.approval.value.approval.domain_scope.domain_ids;
    const filter = (items: ExecutionProfile[]) =>
      items.filter((profile) => profileCoversDomains(profile, requiredDomainIds));
    return {
      patchRepair: filter(profiles.data.patchRepair),
      repairChecker: filter(profiles.data.repairChecker),
      repairConfigExport: filter(profiles.data.repairConfigExport),
      repairSimulation: filter(profiles.data.repairSimulation),
      validation: filter(profiles.data.validation),
      validationChecker: filter(profiles.data.validationChecker),
      validationSimulation: filter(profiles.data.validationSimulation),
    };
  }, [profiles.data, workflow.data]);
  const selectedValidation = catalog?.validation.find(
    (profile) => profileKey(profile) === validationProfileKey,
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
  const expectedFindings = useMemo(
    () => parseFindingBindings(expectedFindingBindingsText),
    [expectedFindingBindingsText],
  );
  const findings = useMemo(() => parseFindingBindings(findingBindingsText), [findingBindingsText]);
  const regressionIds = useMemo(() => parseLines(regressionSuiteIds), [regressionSuiteIds]);
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

  async function reload() {
    const refreshed = await workflow.refetch();
    if (refreshed.isSuccess) {
      setMutation(null);
      setAcceptedRunId(null);
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
        setDiffState({ ...current, error: normalizedError(error), loading: false });
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
          description="正在读取 Patch、retained approval binding、complete ref history 与 ETag。"
          headingLevel={1}
          state="loading"
          title="正在读取 Patch workflow"
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
            description="Patch、binding、Approval 或 ref history 未能闭合。"
            headingLevel={1}
            state="error"
            title="Patch authority 不可用"
          />
        )}
      </div>
    );
  }

  const data = workflow.data;
  const item = data.approval.value.approval;
  const resolutionIds = new Set(resolutions.map((resolution) => resolution.conflict_id));
  const actionsLocked = mutation !== null || replacementId !== null || !data.binding.is_current_head;
  const refDrifted = !sameRef(data.target.expected_ref, data.currentRef);
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
    findings !== null &&
    !refDrifted &&
    (!validationSeedRequired || seedIsValid) &&
    !actionsLocked;
  const canRepair =
    item.status === "validation_failed" &&
    item.evidence_set_artifact_id !== null &&
    item.evidence_set_artifact_id !== undefined &&
    selectedRepair !== undefined &&
    findings !== null &&
    !refDrifted &&
    (!repairSeedRequired || seedIsValid) &&
    (executionMode !== "replay" || replaySourceRunId.trim() !== "") &&
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

  function validate() {
    if (!canValidate || !selectedValidation || expectedFindings === null || findings === null) {
      return;
    }
    const request: PatchValidationRequest = {
      approval_id: data.binding.approval_id,
      base_snapshot_artifact_id: data.baseArtifactId,
      candidate_config_export_artifact_ids: parseLines(configArtifactIds),
      checker_profiles: selectedValidationCheckers.map((profile) => profile.profile),
      constraint_snapshot_artifact_id: constraintArtifactId.trim() || null,
      expected_findings: expectedFindings,
      expected_subject_head_revision: data.binding.subject_head_revision,
      expected_workflow_revision: data.binding.workflow_revision,
      findings,
      playtest_trace_artifact_ids: parseLines(traceArtifactIds),
      preview_snapshot_artifact_id: data.target.target_artifact_id,
      regression_suite_artifact_ids: regressionIds,
      request_schema_version: "patch-validation-admission-request@1",
      review_artifact_ids: parseLines(reviewArtifactIds),
      seed: validationSeedRequired ? parsedSeed : null,
      simulation_profiles: selectedValidationSimulations.map((profile) => profile.profile),
      subject_digest: data.binding.subject_digest,
      target: { expected_ref: data.target.expected_ref, ref_name: data.target.ref_name },
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
    if (!canRepair || !selectedRepair || findings === null || !item.evidence_set_artifact_id) {
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
        findings,
        preview_snapshot_artifact_id: data.target.target_artifact_id,
        regression_suite_artifact_ids: regressionIds,
        repair_policy: selectedRepair.profile,
        schema_version: "patch-repair@1",
        simulation_profiles: selectedRepairSimulations.map((profile) => profile.profile),
        subject_patch_artifact_id: data.current.value.artifact.artifact_id,
        target: { expected_ref: data.target.expected_ref, ref_name: data.target.ref_name },
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
      setMutation({ error: null, label: "Patch repair", pending: true, retry: null });
      try {
        const accepted = await sendFrozen();
        setAcceptedRunId(accepted.run_id);
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
      <nav aria-label="Patch 导航" className="gf-patches__back-nav">
        <a href="/patches">返回 Patch ledger</a>
        <a href={`/artifacts/${encodeURIComponent(data.current.value.artifact.artifact_id)}`}>Artifact</a>
        <a href={`/approvals/${encodeURIComponent(data.binding.approval_id)}`}>Exact approval</a>
        <a href={`/refs/${encodeURIComponent(data.target.ref_name)}/history`}>Ref history</a>
      </nav>

      <header className="gf-patches__hero gf-patches__hero--detail">
        <div>
          <p className="gf-patches__kicker">Patch revision · immutable proposal</p>
          <h1>Patch revision {data.current.value.patch.revision}</h1>
          <p>{data.current.value.patch.rationale}</p>
        </div>
        <span className="gf-patches__status-mark">
          {data.current.value.patch.produced_by === "agent" ? <Bot size={17} /> : <BadgeCheck size={17} />}
          {item.status}
        </span>
      </header>

      <dl className="gf-patches__facts" aria-label="Patch exact workflow authority">
        <div>
          <dt>Patch Artifact</dt>
          <dd>
            <CopyableText
              copyLabel="复制 Patch Artifact ID"
              value={data.current.value.artifact.artifact_id}
            />
          </dd>
        </div>
        <div>
          <dt>ETag</dt>
          <dd>
            <CopyableText copyLabel="复制 Patch ETag" value={data.current.etag} />
          </dd>
        </div>
        <div>
          <dt>Subject head / workflow</dt>
          <dd>
            {data.binding.subject_head_revision} / {data.binding.workflow_revision}
          </dd>
        </div>
        <div>
          <dt>Approval status</dt>
          <dd>{item.status}</dd>
        </div>
        <div>
          <dt>Validation status</dt>
          <dd>{data.current.value.validation_status}</dd>
        </div>
        <div>
          <dt>Regression status</dt>
          <dd>{data.current.value.regression_status}</dd>
        </div>
        <div className="gf-patches__fact-wide">
          <dt>Subject digest</dt>
          <dd>
            <CopyableText copyLabel="复制 Patch subject digest" value={data.binding.subject_digest} />
          </dd>
        </div>
      </dl>

      {!data.binding.is_current_head && (
        <StatePanel
          action={
            replacementId ? (
              <a href={`/patches/${encodeURIComponent(replacementId)}`}>打开 successor</a>
            ) : undefined
          }
          description="当前 immutable revision 已不是 subject head；所有 mutation 均已停止。"
          state="terminal"
          title="Superseded Patch revision"
        />
      )}
      {refDrifted && data.binding.is_current_head && (
        <StatePanel
          action={
            canRebase ? (
              <button className="gf-secondary-button" onClick={rebase} type="button">
                创建 rebased Patch revision
              </button>
            ) : undefined
          }
          description="live ref 已不再等于 frozen expected ref；validate、repair、submit 与 apply 均已停止。"
          state="error"
          title="Patch target 已 stale"
        />
      )}
      {mutation && !mutation.pending && <MutationFailure onReload={() => void reload()} state={mutation} />}
      {mutation?.pending && (
        <StatePanel
          description={`${mutation.label} 请求已冻结并提交。`}
          state="loading"
          title="正在执行操作"
        />
      )}
      {replacementId && <ReplacementReceipt api={api} previous={data} replacementId={replacementId} />}

      <section aria-labelledby="patch-diff-title" className="gf-patches__workspace-section">
        <header>
          <GitBranch aria-hidden="true" size={20} />
          <div>
            <h2 id="patch-diff-title">Base / Current / Proposed</h2>
            <p>Base 与 Proposed 来自 Patch；Current 只来自 complete ref history + exact Spec read。</p>
          </div>
        </header>
        <dl className="gf-patches__three-way-summary">
          <div>
            <dt>Base</dt>
            <dd>{data.current.value.patch.base_snapshot_id}</dd>
          </div>
          <div>
            <dt>Current</dt>
            <dd>{data.currentSnapshotId ?? "ref 不存在"}</dd>
          </div>
          <div>
            <dt>Proposed</dt>
            <dd>{data.current.value.patch.target_snapshot_id}</dd>
          </div>
        </dl>
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
            <h2 id="patch-rebase-title">Rebase / conflict resolution</h2>
            <p>只有服务端定义的 keep_current / take_proposed / custom 可提交；前端不自动仲裁。</p>
          </div>
        </header>
        <dl className="gf-patches__target-ledger">
          <div>
            <dt>Frozen expected ref</dt>
            <dd>
              {data.target.expected_ref
                ? `${data.target.expected_ref.artifact_id}@${data.target.expected_ref.revision}`
                : "null"}
            </dd>
          </div>
          <div>
            <dt>Current ref</dt>
            <dd>
              {data.currentRef ? `${data.currentRef.artifact_id}@${data.currentRef.revision}` : "不存在"}
            </dd>
          </div>
        </dl>
        <button disabled={!canRebase} onClick={rebase} type="button">
          Rebase 到 exact current ref
        </button>
        {conflictSetId &&
          (conflicts.isPending ? (
            <StatePanel description="正在完整读取 ConflictSet。" state="loading" title="正在读取冲突" />
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
              description="ConflictSet 分页读取失败；必须丢弃旧页并从 cursor=null 重读。"
              state="error"
              title="冲突不可用"
            />
          ) : conflicts.data.length === 0 ? (
            <StatePanel
              description="服务端返回空 ConflictSet，未提交 resolution。"
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
                提交全部显式 resolutions
              </button>
            </>
          ))}
      </section>

      <section aria-labelledby="patch-evidence-title" className="gf-patches__workspace-section">
        <header>
          <ShieldCheck aria-hidden="true" size={20} />
          <div>
            <h2 id="patch-evidence-title">Validation / regression evidence</h2>
            <p>EvidenceSet 与 regression artifacts 是结论 authority；Run 成功本身不是通过。</p>
          </div>
        </header>
        <EvidenceLedger data={data} />
        {item.active_validation_run_id && (
          <a href={`/runs/${encodeURIComponent(item.active_validation_run_id)}`}>
            打开 active validation Run
          </a>
        )}
        {acceptedRunId && (
          <div className="gf-patches__live-receipt" role="status">
            <a href={`/runs/${encodeURIComponent(acceptedRunId)}`}>打开 accepted Run</a>
          </div>
        )}
      </section>

      <section aria-labelledby="patch-input-title" className="gf-patches__workspace-section">
        <header>
          <PlayCircle aria-hidden="true" size={20} />
          <div>
            <h2 id="patch-input-title">Exact validation inputs</h2>
            <p>上游 Artifact IDs 必须显式传入；页面不做 sibling 反向搜索。</p>
          </div>
        </header>
        {profiles.isError ? (
          <StatePanel
            description="Profile catalog 无法读取；validate/repair 已停止。"
            state="error"
            title="Profiles 不可用"
          />
        ) : profiles.isPending || !catalog ? (
          <StatePanel
            description="正在读取 active execution profiles。"
            state="loading"
            title="正在读取 profiles"
          />
        ) : (
          <div className="gf-patches__execution-form">
            <ProfileSelect
              label="Validation policy"
              onChange={setValidationProfileKey}
              profiles={catalog.validation}
              value={validationProfileKey}
            />
            <ProfileSelect
              label="Repair policy"
              onChange={setRepairProfileKey}
              profiles={catalog.patchRepair}
              value={repairProfileKey}
            />
            <ProfileChecklist
              label="Validation checker profiles"
              onChange={setValidationCheckerKeys}
              profiles={catalog.validationChecker}
              selected={validationCheckerKeys}
            />
            <ProfileChecklist
              label="Validation simulation profiles"
              onChange={setValidationSimulationKeys}
              profiles={catalog.validationSimulation}
              selected={validationSimulationKeys}
            />
            <ProfileChecklist
              label="Repair checker profiles"
              onChange={setRepairCheckerKeys}
              profiles={catalog.repairChecker}
              selected={repairCheckerKeys}
            />
            <ProfileChecklist
              label="Repair simulation profiles"
              onChange={setRepairSimulationKeys}
              profiles={catalog.repairSimulation}
              selected={repairSimulationKeys}
            />
            <ProfileChecklist
              label="Repair candidate export profiles"
              onChange={setRepairExportKeys}
              profiles={catalog.repairConfigExport}
              selected={repairExportKeys}
            />
            <label>
              <span>ConstraintSnapshot Artifact ID（可空）</span>
              <input
                onChange={(event) => setConstraintArtifactId(event.target.value)}
                value={constraintArtifactId}
              />
            </label>
            <label>
              <span>Candidate ConfigExport Artifact IDs（每行一个）</span>
              <textarea
                onChange={(event) => setConfigArtifactIds(event.target.value)}
                rows={3}
                value={configArtifactIds}
              />
            </label>
            <label>
              <span>Review Artifact IDs（每行一个）</span>
              <textarea
                onChange={(event) => setReviewArtifactIds(event.target.value)}
                rows={3}
                value={reviewArtifactIds}
              />
            </label>
            <label>
              <span>PlaytestTrace Artifact IDs（每行一个）</span>
              <textarea
                onChange={(event) => setTraceArtifactIds(event.target.value)}
                rows={3}
                value={traceArtifactIds}
              />
            </label>
            <label>
              <span>RegressionSuite Artifact IDs（每行一个）</span>
              <textarea
                onChange={(event) => setRegressionSuiteIds(event.target.value)}
                rows={3}
                value={regressionSuiteIds}
              />
            </label>
            <label className="gf-patches__form-wide">
              <span>Expected historical FindingEvidenceBindingV1[]（JSON）</span>
              <textarea
                aria-invalid={expectedFindings === null}
                onChange={(event) => setExpectedFindingBindingsText(event.target.value)}
                rows={6}
                value={expectedFindingBindingsText}
              />
            </label>
            <label className="gf-patches__form-wide">
              <span>Observed / repair FindingEvidenceBindingV1[]（JSON）</span>
              <textarea
                aria-invalid={findings === null}
                onChange={(event) => setFindingBindingsText(event.target.value)}
                rows={6}
                value={findingBindingsText}
              />
            </label>
            <label>
              <span>Seed</span>
              <input min="0" onChange={(event) => setSeed(event.target.value)} type="number" value={seed} />
            </label>
            <p className="gf-patches__muted">
              Validation seed {validationSeedRequired ? "required" : "not applicable"} · Repair seed{" "}
              {repairSeedRequired ? "required" : "not applicable"}.
            </p>
            <label>
              <span>Repair LLM mode</span>
              <select
                onChange={(event) => setExecutionMode(event.target.value as typeof executionMode)}
                value={executionMode}
              >
                <option value="record">record</option>
                <option value="live">live</option>
                <option value="replay">replay</option>
              </select>
            </label>
            {executionMode === "replay" && (
              <label>
                <span>Replay source Run</span>
                <input
                  onChange={(event) => setReplaySourceRunId(event.target.value)}
                  value={replaySourceRunId}
                />
              </label>
            )}
            <div className="gf-patches__action-row gf-patches__form-wide">
              <button disabled={!canValidate} onClick={validate} type="button">
                启动 exact validation
              </button>
              <button disabled={!canRepair} onClick={repair} type="button">
                Resolve 并启动 repair
              </button>
            </div>
          </div>
        )}
      </section>

      <section aria-labelledby="patch-approval-title" className="gf-patches__workspace-section">
        <header>
          <Send aria-hidden="true" size={20} />
          <div>
            <h2 id="patch-approval-title" ref={applyReturnFocusRef} tabIndex={-1}>
              Submit / approval / apply
            </h2>
            <p>Apply body 只复制已验证的 frozen target binding；不会从表单或 current ref 重算。</p>
          </div>
        </header>
        <div className="gf-patches__approval-actions">
          <button disabled={!canSubmit} onClick={submit} type="button">
            Submit for independent approval
          </button>
          <a className="gf-secondary-button" href={`/approvals/${encodeURIComponent(item.approval_id)}`}>
            打开审批详情
          </a>
          <button disabled={!canApply} onClick={() => setConfirmApply(true)} type="button">
            {item.status === "auto_apply_eligible" ? "Apply policy-eligible Patch" : "Apply approved Patch"}
          </button>
        </div>
        {applyResult && (
          <div className="gf-patches__live-receipt" role="status">
            <StatePanel
              action={
                <a href={`/refs/${encodeURIComponent(applyResult.ref_name)}/history`}>检查 ref history</a>
              }
              description={`${applyResult.ref_name} 现在指向 ${applyResult.ref_value.artifact_id}@${applyResult.ref_value.revision}。`}
              state="terminal"
              title="Patch 已通过 ref transition 应用"
            />
          </div>
        )}
      </section>

      <section aria-labelledby="patch-history-title" className="gf-patches__workspace-section">
        <header>
          <GitBranch aria-hidden="true" size={20} />
          <div>
            <h2 id="patch-history-title">Ref history</h2>
            <p>Apply 前 history 不会移动；每一项都是服务端 append-only revision。</p>
          </div>
        </header>
        <ol className="gf-patches__history-list">
          {data.history.map((entry) => (
            <li key={`${entry.value.revision}:${entry.value.artifact_id}`}>
              <span>revision {entry.value.revision}</span>
              <code>{entry.value.artifact_id}</code>
            </li>
          ))}
        </ol>
      </section>

      <footer className="gf-patches__principle">
        <AlertTriangle aria-hidden="true" size={17} />
        <span>所有 403/409 stale/self-approval 结果按服务端 Problem 原样 fail-closed；前端不近似授权。</span>
        <Sparkles aria-hidden="true" size={17} />
      </footer>

      <ConfirmDialog
        confirmLabel="确认 Apply"
        description={`将按 frozen target binding 更新 ${data.target.ref_name}；该操作不会修改 Patch Artifact。`}
        onCancel={() => setConfirmApply(false)}
        onConfirm={apply}
        open={confirmApply}
        returnFocusRef={applyReturnFocusRef}
        title="Apply approved Patch?"
      />
    </div>
  );
}
