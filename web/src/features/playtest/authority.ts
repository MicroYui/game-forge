import type { components } from "../../api/generated/openapi";

type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];
type CompletionOracleRef = components["schemas"]["CompletionOracleRefV1"];
type PlaytestEpisodeBinding = components["schemas"]["PlaytestEpisodeBindingV1"];
type PlaytestRunRequest = components["schemas"]["PlaytestRunRequestV1"];
type ProfileRef = components["schemas"]["ProfileRefV1"];
type RunFindingLink = components["schemas"]["RunFindingLinkViewV1"];
type RunView = components["schemas"]["RunViewV1"];
type TaskEpisode = components["schemas"]["TaskEpisodeV1"];
type TaskSuiteArtifactView = components["schemas"]["TaskSuiteArtifactViewV1"];
type VersionTuple = components["schemas"]["VersionTuple"];

const VERSION_TUPLE_FIELDS = [
  "doc_version",
  "ir_snapshot_id",
  "constraint_snapshot_id",
  "prompt_version",
  "model_snapshot",
  "agent_graph_version",
  "tool_version",
  "env_contract_version",
  "seed",
  "cassette_id",
] as const satisfies readonly (keyof VersionTuple)[];

const SHA256_HEX = /^[0-9a-f]{64}$/;
const STATE_HASH = /^sha256:[0-9a-f]{64}$/;

export class PlaytestAuthorityError extends Error {
  override name = "PlaytestAuthorityError";
}

export type TaskSuiteNavigationField = "preview" | "config" | "constraint" | "environment";

/** URL/search state is only a navigation hint. The TaskSuite payload remains authority. */
export interface TaskSuiteNavigationCandidate {
  sourcePreviewArtifactId?: string | null;
  configArtifactId?: string | null;
  constraintSnapshotArtifactId?: string | null;
  environmentProfile?: ProfileRef | null;
}

export interface TaskSuiteNavigationAssessment {
  matches: boolean;
  mismatches: TaskSuiteNavigationField[];
  providedFields: TaskSuiteNavigationField[];
}

export interface TaskSuiteAuthority {
  episodes: readonly TaskEpisode[];
  minStepBudget: number;
  navigation: TaskSuiteNavigationAssessment;
  view: TaskSuiteArtifactView;
}

export interface EpisodeSelectionAuthority {
  episodes: readonly PlaytestEpisodeBinding[];
  maxStepsPerEpisode: number;
  minStepBudget: number;
  suiteEpisodes: readonly TaskEpisode[];
}

export interface PlaytestTraceEpisodeAuthority {
  actionCount: number;
  completed: boolean;
  completionOracle: CompletionOracleRef;
  episodeId: string;
  executionStepLimit: number;
  raw: Readonly<Record<string, unknown>>;
  scenarioSpecArtifactId: string;
  stepBudget: number;
  terminalReason:
    | "completion_oracle_satisfied"
    | "step_limit_exhausted"
    | "deterministic_abort"
    | "agent_stopped";
}

export interface PlaytestTraceAuthority {
  artifact: ArtifactPayloadView;
  episodes: readonly PlaytestTraceEpisodeAuthority[];
  rawPayload: Readonly<Record<string, unknown>>;
}

interface PlaytestManifestParentAuthority {
  artifactId: string;
  publication: "existing" | "run_published";
  role: "input" | "intermediate" | "output" | "evidence";
}

interface PlaytestManifestAuthority {
  attemptNo: number | null;
  parents: readonly PlaytestManifestParentAuthority[];
  terminalVersionTuple: VersionTuple;
}

interface PlaytestTerminalBase {
  attemptNo: number | null;
  manifest: ArtifactPayloadView;
  manifestAuthority: PlaytestManifestAuthority;
  requestCandidateStatus: "not_provided" | "stale" | "visible_bindings_match";
  run: RunView;
}

export interface SucceededPlaytestAuthority extends PlaytestTerminalBase {
  allEpisodesCompleted: boolean;
  completedEpisodeCount: number;
  findingCount: number;
  kind: "succeeded";
  resultArtifact: ArtifactPayloadView;
  runStatus: "succeeded";
  selection: EpisodeSelectionAuthority;
  trace: PlaytestTraceAuthority;
}

export interface FailedPlaytestAuthority extends PlaytestTerminalBase {
  causeCode: string;
  kind: "failed";
  message: string;
  resultArtifact: null;
  runStatus: "failed" | "cancelled" | "timed_out";
  /** A browser-local request can explain a failure view, but is never terminal authority. */
  selectionCandidate: EpisodeSelectionAuthority | null;
}

export type PlaytestTerminalAuthority = SucceededPlaytestAuthority | FailedPlaytestAuthority;

function fail(message: string): never {
  throw new PlaytestAuthorityError(message);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value);
}

function isPositiveInteger(value: unknown): value is number {
  return isInteger(value) && value > 0;
}

function isNonNegativeInteger(value: unknown): value is number {
  return isInteger(value) && value >= 0;
}

// OpenAPI currently projects UInt64 seeds as JSON numbers. Their equality is
// server-verified before the result endpoint responds; the browser must not
// reject a valid projection merely because it exceeds Number.MAX_SAFE_INTEGER.
function isNonNegativeJsonInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

function isSha256(value: unknown): value is string {
  return typeof value === "string" && SHA256_HEX.test(value);
}

function isStateHash(value: unknown): value is string {
  return typeof value === "string" && STATE_HASH.test(value);
}

function compareStrings(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function canonicalJson(value: unknown): string {
  if (value === undefined) return "undefined";
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  return `{${Object.entries(value as Record<string, unknown>)
    .filter(([, item]) => item !== undefined)
    .sort(([left], [right]) => compareStrings(left, right))
    .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`)
    .join(",")}}`;
}

function sameProfile(left: unknown, right: ProfileRef): boolean {
  return isRecord(left) && left.profile_id === right.profile_id && left.version === right.version;
}

function requireProfile(value: unknown, label: string): asserts value is ProfileRef {
  if (!isRecord(value) || !isNonEmptyString(value.profile_id) || !isPositiveInteger(value.version)) {
    fail(`${label} is not an exact ProfileRef.`);
  }
}

function sameOracle(left: unknown, right: CompletionOracleRef): boolean {
  return canonicalJson(left) === canonicalJson(right);
}

function requireOracle(value: unknown): asserts value is CompletionOracleRef {
  if (
    !isRecord(value) ||
    !isNonEmptyString(value.oracle_id) ||
    !isPositiveInteger(value.version) ||
    !isNonEmptyString(value.params_schema_id) ||
    !("params" in value)
  ) {
    fail("TaskSuite completion oracle binding is invalid.");
  }
}

function sortedUniqueStrings(value: readonly string[]): string[] | null {
  if (!value.every(isNonEmptyString) || new Set(value).size !== value.length) return null;
  return [...value].sort(compareStrings);
}

function sameStringSet(left: readonly string[], right: readonly string[]): boolean {
  const leftSorted = sortedUniqueStrings(left);
  const rightSorted = sortedUniqueStrings(right);
  return (
    leftSorted !== null &&
    rightSorted !== null &&
    leftSorted.length === rightSorted.length &&
    leftSorted.every((item, index) => item === rightSorted[index])
  );
}

function containsStringSet(container: readonly string[], required: readonly string[]): boolean {
  const containerSorted = sortedUniqueStrings(container);
  const requiredSorted = sortedUniqueStrings(required);
  if (containerSorted === null || requiredSorted === null) return false;
  const containerSet = new Set(containerSorted);
  return requiredSorted.every((item) => containerSet.has(item));
}

function tupleValue(tuple: VersionTuple, key: keyof VersionTuple): string | number | null {
  return tuple[key] ?? null;
}

function sameVersionTuple(left: VersionTuple, right: VersionTuple): boolean {
  return VERSION_TUPLE_FIELDS.every((key) => tupleValue(left, key) === tupleValue(right, key));
}

function recordVersionTuple(value: unknown): VersionTuple | null {
  if (!isRecord(value)) return null;
  const tuple: VersionTuple = {};
  for (const key of VERSION_TUPLE_FIELDS) {
    const item = value[key];
    if (item !== undefined && item !== null && typeof item !== "string" && typeof item !== "number") {
      return null;
    }
    if (item !== undefined) Object.assign(tuple, { [key]: item });
  }
  return tuple;
}

function requireDomainScope(value: unknown): void {
  if (!isRecord(value) || !Array.isArray(value.domain_ids)) {
    fail("TaskSuite episode domain scope is invalid.");
  }
  const domainIds = value.domain_ids;
  if (
    domainIds.length === 0 ||
    !domainIds.every(isNonEmptyString) ||
    new Set(domainIds).size !== domainIds.length
  ) {
    fail("TaskSuite episode domain scope is invalid.");
  }
}

function validateTaskSuiteEnvelope(
  view: TaskSuiteArtifactView,
  requestedArtifactId: string,
): readonly TaskEpisode[] {
  const { artifact, task_suite: suite } = view;
  if (
    view.view_schema_version !== "task-suite-artifact-view@1" ||
    artifact.summary_schema_version !== "artifact-summary@1" ||
    artifact.artifact_id !== requestedArtifactId ||
    artifact.kind !== "task_suite" ||
    artifact.payload_schema_id !== "task-suite@1" ||
    artifact.lineage_schema_version !== "lineage@2" ||
    !isSha256(artifact.payload_hash) ||
    suite.task_suite_schema_version !== "task-suite@1"
  ) {
    fail("TaskSuite route identity or immutable Artifact envelope is invalid.");
  }

  const tuple = artifact.version_tuple;
  if (
    !isNonEmptyString(tuple.ir_snapshot_id) ||
    !isNonEmptyString(tuple.constraint_snapshot_id) ||
    !isNonEmptyString(tuple.tool_version) ||
    tuple.env_contract_version !== suite.env_contract_version ||
    !isNonEmptyString(suite.env_contract_version) ||
    tuple.seed != null ||
    tuple.prompt_version != null ||
    tuple.model_snapshot != null ||
    tuple.agent_graph_version != null ||
    tuple.cassette_id != null
  ) {
    fail("TaskSuite VersionTuple does not describe deterministic derivation authority.");
  }

  requireProfile(suite.suite_profile, "TaskSuite derivation profile");
  requireProfile(suite.environment_profile, "TaskSuite environment profile");
  if (
    !isPositiveInteger(suite.completion_oracle_registry_ref.registry_version) ||
    !isSha256(suite.completion_oracle_registry_ref.digest)
  ) {
    fail("TaskSuite completion oracle registry binding is invalid.");
  }
  if (
    !isNonEmptyString(suite.source_preview_artifact_id) ||
    !isNonEmptyString(suite.config_export_artifact_id) ||
    !isNonEmptyString(suite.constraint_snapshot_artifact_id) ||
    !Array.isArray(suite.episodes) ||
    suite.episodes.length === 0
  ) {
    fail("TaskSuite has incomplete preview, config, constraint, or episode authority.");
  }

  const episodeIds = new Set<string>();
  const scenarioIds = new Set<string>();
  let previousEpisodeId: string | null = null;
  for (const episode of suite.episodes) {
    if (
      !isNonEmptyString(episode.episode_id) ||
      !isNonEmptyString(episode.scenario_spec_artifact_id) ||
      !isPositiveInteger(episode.step_budget) ||
      episodeIds.has(episode.episode_id) ||
      scenarioIds.has(episode.scenario_spec_artifact_id) ||
      (previousEpisodeId !== null && compareStrings(episode.episode_id, previousEpisodeId) <= 0)
    ) {
      fail("TaskSuite episodes are empty, duplicated, unsorted, or have an invalid step budget.");
    }
    requireOracle(episode.completion_oracle);
    requireDomainScope(episode.domain_scope);
    if (
      !isNonEmptyString(episode.reset_binding.reset_schema_id) ||
      !isSha256(episode.reset_binding.payload_hash)
    ) {
      fail("TaskSuite reset binding is invalid.");
    }
    episodeIds.add(episode.episode_id);
    scenarioIds.add(episode.scenario_spec_artifact_id);
    previousEpisodeId = episode.episode_id;
  }

  const expectedParents = [
    suite.source_preview_artifact_id,
    suite.config_export_artifact_id,
    suite.constraint_snapshot_artifact_id,
    ...suite.episodes.map((episode) => episode.scenario_spec_artifact_id),
  ];
  if (!sameStringSet(artifact.parent_artifact_ids, expectedParents)) {
    fail("TaskSuite direct lineage does not close over preview, config, constraint, and scenarios.");
  }
  return suite.episodes;
}

export function assessTaskSuiteNavigationCandidate(
  view: TaskSuiteArtifactView,
  candidate: TaskSuiteNavigationCandidate = {},
): TaskSuiteNavigationAssessment {
  validateTaskSuiteEnvelope(view, view.artifact.artifact_id);
  const providedFields: TaskSuiteNavigationField[] = [];
  const mismatches: TaskSuiteNavigationField[] = [];
  const suite = view.task_suite;

  const compare = (field: TaskSuiteNavigationField, provided: boolean, matches: boolean): void => {
    if (!provided) return;
    providedFields.push(field);
    if (!matches) mismatches.push(field);
  };

  compare(
    "preview",
    candidate.sourcePreviewArtifactId != null,
    candidate.sourcePreviewArtifactId === suite.source_preview_artifact_id,
  );
  compare(
    "config",
    candidate.configArtifactId != null,
    candidate.configArtifactId === suite.config_export_artifact_id,
  );
  compare(
    "constraint",
    candidate.constraintSnapshotArtifactId != null,
    candidate.constraintSnapshotArtifactId === suite.constraint_snapshot_artifact_id,
  );
  compare(
    "environment",
    candidate.environmentProfile != null,
    candidate.environmentProfile != null &&
      sameProfile(candidate.environmentProfile, suite.environment_profile),
  );

  return { matches: mismatches.length === 0, mismatches, providedFields };
}

export function requireTaskSuiteAuthority(
  view: TaskSuiteArtifactView,
  requestedArtifactId: string,
  navigationCandidate: TaskSuiteNavigationCandidate = {},
): TaskSuiteAuthority {
  const episodes = validateTaskSuiteEnvelope(view, requestedArtifactId);
  return {
    episodes,
    minStepBudget: Math.min(...episodes.map((episode) => episode.step_budget)),
    navigation: assessTaskSuiteNavigationCandidate(view, navigationCandidate),
    view,
  };
}

export function requireEpisodeSelection(
  suiteView: TaskSuiteArtifactView,
  selectedEpisodes: readonly PlaytestEpisodeBinding[],
  maxStepsPerEpisode: number,
): EpisodeSelectionAuthority {
  const authority = requireTaskSuiteAuthority(suiteView, suiteView.artifact.artifact_id);
  if (!Array.isArray(selectedEpisodes) || selectedEpisodes.length === 0) {
    fail("Playtest requires a non-empty explicit episode subset.");
  }
  if (!isPositiveInteger(maxStepsPerEpisode)) {
    fail("Playtest max steps must be a positive integer.");
  }

  const suiteByEpisode = new Map(authority.episodes.map((episode) => [episode.episode_id, episode] as const));
  const episodeIds = new Set<string>();
  const scenarioIds = new Set<string>();
  const selectedSuiteEpisodes: TaskEpisode[] = [];
  for (const selected of selectedEpisodes) {
    if (
      !isNonEmptyString(selected.episode_id) ||
      !isNonEmptyString(selected.scenario_spec_artifact_id) ||
      episodeIds.has(selected.episode_id) ||
      scenarioIds.has(selected.scenario_spec_artifact_id)
    ) {
      fail("Playtest episode selection contains a duplicate or invalid binding.");
    }
    const suiteEpisode = suiteByEpisode.get(selected.episode_id);
    if (
      suiteEpisode === undefined ||
      suiteEpisode.scenario_spec_artifact_id !== selected.scenario_spec_artifact_id
    ) {
      fail("Playtest episode/scenario pair is not an exact TaskSuite member.");
    }
    episodeIds.add(selected.episode_id);
    scenarioIds.add(selected.scenario_spec_artifact_id);
    selectedSuiteEpisodes.push(suiteEpisode);
  }

  selectedSuiteEpisodes.sort((left, right) => compareStrings(left.episode_id, right.episode_id));
  const minStepBudget = Math.min(...selectedSuiteEpisodes.map((episode) => episode.step_budget));
  if (maxStepsPerEpisode > minStepBudget) {
    fail("Playtest max steps exceed the minimum selected episode budget.");
  }

  return {
    episodes: selectedSuiteEpisodes.map((episode) => ({
      episode_id: episode.episode_id,
      scenario_spec_artifact_id: episode.scenario_spec_artifact_id,
    })),
    maxStepsPerEpisode,
    minStepBudget,
    suiteEpisodes: selectedSuiteEpisodes,
  };
}

function requireRunKind(value: unknown): void {
  if (!isRecord(value) || value.kind !== "playtest.run" || value.version !== 1) {
    fail("Terminal manifest is not for playtest.run@1.");
  }
}

function parseAttemptNo(value: unknown): number | null {
  if (value === null) return null;
  if (!isPositiveInteger(value)) fail("Terminal manifest attempt number is invalid.");
  return value;
}

function parseManifestParents(value: unknown): PlaytestManifestParentAuthority[] {
  if (!Array.isArray(value)) fail("Terminal manifest parent projection is unavailable.");
  const parents: PlaytestManifestParentAuthority[] = [];
  const ids = new Set<string>();
  for (const item of value) {
    if (
      !isRecord(item) ||
      !isNonEmptyString(item.artifact_id) ||
      (item.role !== "input" &&
        item.role !== "intermediate" &&
        item.role !== "output" &&
        item.role !== "evidence") ||
      (item.publication !== "existing" && item.publication !== "run_published") ||
      ids.has(item.artifact_id) ||
      (item.role === "input" && item.publication !== "existing") ||
      (item.role !== "input" && item.publication !== "run_published")
    ) {
      fail("Terminal manifest parent projection is invalid.");
    }
    ids.add(item.artifact_id);
    parents.push({
      artifactId: item.artifact_id,
      publication: item.publication,
      role: item.role,
    });
  }
  return parents;
}

function parseManifestProjection(
  value: unknown,
  artifact: ArtifactSummary,
  attemptNo: number | null,
): PlaytestManifestAuthority {
  if (
    !isRecord(value) ||
    value.projection_schema_version !== "run-manifest-version-projection@1" ||
    value.manifest_scope !== "run" ||
    !isSha256(value.run_payload_hash)
  ) {
    fail("Terminal manifest has no run-scoped version projection.");
  }
  requireRunKind(value.run_kind);
  const projectionAttempt = parseAttemptNo(value.attempt_no);
  if (projectionAttempt !== attemptNo) {
    fail("Terminal manifest attempt differs from its version projection.");
  }
  const parents = parseManifestParents(value.parents);
  if (
    !sameStringSet(
      artifact.parent_artifact_ids,
      parents.map((parent) => parent.artifactId),
    )
  ) {
    fail("Terminal manifest lineage differs from its typed parent projection.");
  }
  const terminalVersionTuple = recordVersionTuple(value.terminal_version_tuple);
  if (terminalVersionTuple === null || !sameVersionTuple(terminalVersionTuple, artifact.version_tuple)) {
    fail("Terminal manifest VersionTuple differs from its terminal projection.");
  }
  return { attemptNo, parents, terminalVersionTuple };
}

function requireFrozenInputTuple(
  projectionValue: unknown,
  suite: TaskSuiteArtifactView,
  expectedSeed?: number,
): VersionTuple {
  if (!isRecord(projectionValue)) fail("Terminal manifest projection is unavailable.");
  const frozen = recordVersionTuple(projectionValue.frozen_input_version_tuple);
  if (
    frozen === null ||
    frozen.ir_snapshot_id !== suite.artifact.version_tuple.ir_snapshot_id ||
    frozen.constraint_snapshot_id !== suite.artifact.version_tuple.constraint_snapshot_id ||
    frozen.env_contract_version !== suite.task_suite.env_contract_version ||
    !isNonNegativeJsonInteger(frozen.seed) ||
    (expectedSeed !== undefined && frozen.seed !== expectedSeed)
  ) {
    fail("Terminal manifest frozen input tuple differs from Playtest result authority.");
  }
  return frozen;
}

function expectedSelectionInputIds(
  suite: TaskSuiteArtifactView,
  selection: EpisodeSelectionAuthority,
): string[] {
  return [
    suite.task_suite.config_export_artifact_id,
    suite.task_suite.constraint_snapshot_artifact_id,
    suite.artifact.artifact_id,
    ...selection.episodes.map((episode) => episode.scenario_spec_artifact_id),
  ];
}

function requireRequestCandidate(
  requestValue: PlaytestRunRequest,
  suite: TaskSuiteArtifactView,
): EpisodeSelectionAuthority {
  if (
    requestValue.request_schema_version !== "playtest-run-request@1" ||
    requestValue.params.schema_version !== "playtest-run@1" ||
    !isNonNegativeJsonInteger(requestValue.seed) ||
    (requestValue.llm_execution_mode !== "live" &&
      requestValue.llm_execution_mode !== "record" &&
      requestValue.llm_execution_mode !== "replay")
  ) {
    fail("Browser-local Playtest request candidate is invalid.");
  }
  if (
    (requestValue.llm_execution_mode === "replay" && !isNonEmptyString(requestValue.cassette_artifact_id)) ||
    (requestValue.llm_execution_mode !== "replay" && requestValue.cassette_artifact_id != null)
  ) {
    fail("Browser-local replay candidate has no cassette binding.");
  }
  requireProfile(requestValue.params.environment_profile, "Playtest environment profile");
  requireProfile(requestValue.params.planner_policy, "Playtest planner profile");
  if (
    requestValue.params.task_suite_artifact_id !== suite.artifact.artifact_id ||
    requestValue.params.config_artifact_id !== suite.task_suite.config_export_artifact_id ||
    requestValue.params.constraint_snapshot_artifact_id !==
      suite.task_suite.constraint_snapshot_artifact_id ||
    !sameProfile(requestValue.params.environment_profile, suite.task_suite.environment_profile)
  ) {
    fail("Browser-local Playtest request candidate differs from TaskSuite authority.");
  }
  return requireEpisodeSelection(
    suite,
    requestValue.params.episodes,
    requestValue.params.max_steps_per_episode,
  );
}

function assessRequestCandidateAgainstTrace(
  requestValue: PlaytestRunRequest,
  suite: TaskSuiteArtifactView,
  tracePayload: Readonly<Record<string, unknown>>,
  traceSelection: EpisodeSelectionAuthority,
): "stale" | "visible_bindings_match" {
  try {
    const candidateSelection = requireRequestCandidate(requestValue, suite);
    const matches =
      requestValue.params.config_artifact_id === tracePayload.config_artifact_id &&
      requestValue.params.constraint_snapshot_artifact_id === tracePayload.constraint_snapshot_artifact_id &&
      requestValue.params.task_suite_artifact_id === tracePayload.task_suite_artifact_id &&
      sameProfile(tracePayload.environment_profile, requestValue.params.environment_profile) &&
      sameProfile(tracePayload.planner_policy, requestValue.params.planner_policy) &&
      requestValue.params.interaction_mode === tracePayload.interaction_mode &&
      requestValue.params.max_steps_per_episode === tracePayload.requested_max_steps_per_episode &&
      requestValue.seed === tracePayload.seed &&
      canonicalJson(candidateSelection.episodes) === canonicalJson(traceSelection.episodes);
    return matches ? "visible_bindings_match" : "stale";
  } catch (error) {
    if (error instanceof PlaytestAuthorityError) return "stale";
    throw error;
  }
}

function requireArtifactPayloadEnvelope(view: ArtifactPayloadView): void {
  if (
    view.view_schema_version !== "artifact-payload-view@1" ||
    view.resource_revision !== 1 ||
    view.artifact.summary_schema_version !== "artifact-summary@1" ||
    view.artifact.lineage_schema_version !== "lineage@2" ||
    !isSha256(view.artifact.payload_hash)
  ) {
    fail("Artifact payload view is not an immutable lineage@2 resource.");
  }
}

function requireTraceActionRecords(
  value: unknown,
  executionStepLimit: number,
): { count: number; finalStateHash: string | null } {
  if (!Array.isArray(value) || value.length > executionStepLimit) {
    fail("Playtest action trace exceeds its exact execution step limit.");
  }
  let previousTick = -1;
  let finalStateHash: string | null = null;
  for (const record of value) {
    if (
      !isRecord(record) ||
      !("action" in record) ||
      typeof record.last_action_result !== "string" ||
      !isNonNegativeInteger(record.tick) ||
      !isStateHash(record.state_hash) ||
      record.tick < previousTick
    ) {
      fail("Playtest action trace record is invalid.");
    }
    previousTick = record.tick;
    finalStateHash = record.state_hash;
  }
  return { count: value.length, finalStateHash };
}

function requireTraceEpisode(
  value: unknown,
  suiteEpisode: TaskEpisode,
  binding: {
    environmentProfile: ProfileRef;
    maxStepsPerEpisode: number;
    rootSeed: number;
    taskSuiteArtifactId: string;
  },
): PlaytestTraceEpisodeAuthority {
  if (!isRecord(value)) fail("Playtest trace episode is invalid.");
  const terminalReason = value.terminal_reason;
  if (
    value.episode_id !== suiteEpisode.episode_id ||
    value.scenario_spec_artifact_id !== suiteEpisode.scenario_spec_artifact_id ||
    value.step_budget !== suiteEpisode.step_budget ||
    value.execution_step_limit !== binding.maxStepsPerEpisode ||
    !sameOracle(value.completion_oracle, suiteEpisode.completion_oracle) ||
    typeof value.completed !== "boolean" ||
    (terminalReason !== "completion_oracle_satisfied" &&
      terminalReason !== "step_limit_exhausted" &&
      terminalReason !== "deterministic_abort" &&
      terminalReason !== "agent_stopped") ||
    value.completed !== (terminalReason === "completion_oracle_satisfied") ||
    !isStateHash(value.initial_state_hash) ||
    !isStateHash(value.final_state_hash) ||
    !isNonNegativeJsonInteger(value.seed)
  ) {
    fail("Playtest trace episode differs from TaskSuite or result authority.");
  }
  const executionStepLimit = value.execution_step_limit as number;
  const action = requireTraceActionRecords(value.action_trace, executionStepLimit);
  if (
    (action.finalStateHash === null && value.final_state_hash !== value.initial_state_hash) ||
    (action.finalStateHash !== null && value.final_state_hash !== action.finalStateHash)
  ) {
    fail("Playtest episode final state differs from its action trace.");
  }
  if (!isRecord(value.seed_binding)) {
    fail("Playtest episode has no complete seed binding.");
  }
  const seedBinding = value.seed_binding;
  if (
    seedBinding.seed_derivation_version !== "subseed@1" ||
    seedBinding.root_seed !== binding.rootSeed ||
    seedBinding.case_id !== `${binding.taskSuiteArtifactId}:${suiteEpisode.episode_id}` ||
    seedBinding.replication_index !== 0 ||
    seedBinding.seed !== value.seed ||
    !sameProfile(seedBinding.profile, binding.environmentProfile)
  ) {
    fail("Playtest episode seed binding differs from the server result projection.");
  }
  requireRunKind(seedBinding.run_kind);
  return {
    actionCount: action.count,
    completed: value.completed,
    completionOracle: suiteEpisode.completion_oracle,
    episodeId: suiteEpisode.episode_id,
    executionStepLimit,
    raw: value,
    scenarioSpecArtifactId: suiteEpisode.scenario_spec_artifact_id,
    stepBudget: suiteEpisode.step_budget,
    terminalReason,
  };
}

interface BoundPlaytestTrace {
  inputArtifactIds: readonly string[];
  seed: number;
  selection: EpisodeSelectionAuthority;
  trace: PlaytestTraceAuthority;
}

function requireTraceAuthority(
  result: ArtifactPayloadView,
  primaryArtifactId: string,
  terminalVersionTuple: VersionTuple,
  suite: TaskSuiteArtifactView,
): BoundPlaytestTrace {
  requireArtifactPayloadEnvelope(result);
  if (
    result.artifact.artifact_id !== primaryArtifactId ||
    result.artifact.kind !== "playtest_trace" ||
    result.artifact.payload_schema_id !== "playtest-trace@1" ||
    !sameVersionTuple(result.artifact.version_tuple, terminalVersionTuple)
  ) {
    fail("Playtest result Artifact differs from the RunResult primary binding.");
  }
  if (!isRecord(result.payload)) fail("Playtest trace payload is invalid.");
  const payload = result.payload;
  if (
    payload.playtest_trace_schema_version !== "playtest-trace@1" ||
    payload.config_artifact_id !== suite.task_suite.config_export_artifact_id ||
    payload.constraint_snapshot_artifact_id !== suite.task_suite.constraint_snapshot_artifact_id ||
    payload.task_suite_artifact_id !== suite.artifact.artifact_id ||
    !sameProfile(payload.environment_profile, suite.task_suite.environment_profile) ||
    payload.env_contract_version !== suite.task_suite.env_contract_version ||
    (payload.interaction_mode !== "autonomous" && payload.interaction_mode !== "bounded_choice") ||
    !isNonNegativeJsonInteger(payload.seed) ||
    !isPositiveInteger(payload.requested_max_steps_per_episode) ||
    (payload.planner_memory_mode !== "off" && payload.planner_memory_mode !== "llm_compaction") ||
    !Array.isArray(payload.episodes) ||
    payload.episodes.length === 0
  ) {
    fail("PlaytestTraceV1 root binding differs from the exact TaskSuite authority.");
  }
  requireProfile(payload.planner_policy, "Playtest trace planner profile");
  const payloadEpisodes = payload.episodes as unknown[];
  const episodeBindings = payloadEpisodes.map((episode) => {
    if (
      !isRecord(episode) ||
      !isNonEmptyString(episode.episode_id) ||
      !isNonEmptyString(episode.scenario_spec_artifact_id)
    ) {
      fail("Playtest trace episode binding is invalid.");
    }
    return {
      episode_id: episode.episode_id,
      scenario_spec_artifact_id: episode.scenario_spec_artifact_id,
    };
  });
  const selection = requireEpisodeSelection(suite, episodeBindings, payload.requested_max_steps_per_episode);
  const inputArtifactIds = expectedSelectionInputIds(suite, selection);
  if (!containsStringSet(result.artifact.parent_artifact_ids, inputArtifactIds)) {
    fail("Playtest trace lineage omits a selected frozen Run input.");
  }
  if (
    result.artifact.version_tuple.seed !== payload.seed ||
    result.artifact.version_tuple.ir_snapshot_id !== suite.artifact.version_tuple.ir_snapshot_id ||
    result.artifact.version_tuple.constraint_snapshot_id !==
      suite.artifact.version_tuple.constraint_snapshot_id ||
    result.artifact.version_tuple.env_contract_version !== suite.task_suite.env_contract_version ||
    !isNonEmptyString(result.artifact.version_tuple.tool_version)
  ) {
    fail("Playtest trace VersionTuple differs from TaskSuite and result authority.");
  }

  const suiteEpisodesById = new Map(
    selection.suiteEpisodes.map((episode) => [episode.episode_id, episode] as const),
  );
  const episodes = payloadEpisodes.map((episode, index) => {
    const suiteEpisode = suiteEpisodesById.get(episodeBindings[index].episode_id);
    if (suiteEpisode === undefined) fail("Playtest trace selected an unknown TaskSuite episode.");
    return requireTraceEpisode(episode, suiteEpisode, {
      environmentProfile: suite.task_suite.environment_profile,
      maxStepsPerEpisode: payload.requested_max_steps_per_episode as number,
      rootSeed: payload.seed as number,
      taskSuiteArtifactId: suite.artifact.artifact_id,
    });
  });
  if (!isRecord(payload.execution_envelope)) {
    fail("Playtest trace execution envelope is invalid.");
  }
  const execution = payload.execution_envelope;
  const totalActions = episodes.reduce((total, episode) => total + episode.actionCount, 0);
  if (
    !isSha256(execution.planner_profile_payload_hash) ||
    execution.selected_episode_count !== episodes.length ||
    execution.total_step_limit !== episodes.length * payload.requested_max_steps_per_episode ||
    execution.total_action_count !== totalActions ||
    !isNonNegativeInteger(execution.actual_model_calls) ||
    !isPositiveInteger(execution.model_call_upper_bound) ||
    !isPositiveInteger(execution.total_trace_byte_upper_bound) ||
    !isPositiveInteger(execution.actual_trace_bytes) ||
    !isNonNegativeInteger(execution.total_action_trace_bytes) ||
    execution.actual_model_calls > execution.model_call_upper_bound ||
    execution.total_action_count > execution.total_step_limit ||
    execution.total_action_trace_bytes > execution.total_trace_byte_upper_bound ||
    execution.actual_trace_bytes > execution.total_trace_byte_upper_bound
  ) {
    fail("Playtest trace execution envelope is not exact.");
  }
  return {
    inputArtifactIds,
    seed: payload.seed,
    selection,
    trace: { artifact: result, episodes, rawPayload: payload },
  };
}

function requireRunEnvelope(run: RunView, expectedRunId: string): void {
  if (
    run.view_schema_version !== "run-view@1" ||
    run.run_id !== expectedRunId ||
    !isPositiveInteger(run.revision) ||
    (run.attempt_no != null && !isPositiveInteger(run.attempt_no))
  ) {
    fail("Run route identity or view contract is invalid.");
  }
}

export function bindPlaytestTerminalAuthority({
  expectedRunId,
  manifest,
  requestCandidate = null,
  result,
  run,
  suite,
}: {
  expectedRunId: string;
  manifest: ArtifactPayloadView;
  /** Optional browser-local state; never a substitute for the verified result endpoint. */
  requestCandidate?: PlaytestRunRequest | null;
  result: ArtifactPayloadView | null;
  run: RunView;
  suite: TaskSuiteArtifactView;
}): PlaytestTerminalAuthority {
  requireRunEnvelope(run, expectedRunId);
  requireTaskSuiteAuthority(suite, suite.artifact.artifact_id);
  requireArtifactPayloadEnvelope(manifest);
  if (!isRecord(manifest.payload)) fail("Terminal manifest payload is invalid.");
  const payload = manifest.payload;
  if (payload.run_id !== expectedRunId) fail("Terminal manifest belongs to another Run.");
  requireRunKind(payload.run_kind);
  const attemptNo = parseAttemptNo(payload.attempt_no);
  if ((run.attempt_no ?? null) !== attemptNo) {
    fail("Run attempt differs from its terminal manifest.");
  }
  const projection = parseManifestProjection(payload.version_projection, manifest.artifact, attemptNo);
  const projectedInputIds = projection.parents
    .filter((parent) => parent.role === "input")
    .map((parent) => parent.artifactId);
  const suiteCoreInputIds = [
    suite.task_suite.config_export_artifact_id,
    suite.task_suite.constraint_snapshot_artifact_id,
    suite.artifact.artifact_id,
  ];
  if (!containsStringSet(projectedInputIds, suiteCoreInputIds)) {
    fail("Terminal manifest omits a frozen TaskSuite input.");
  }

  const projectedPublishedIds = projection.parents
    .filter((parent) => parent.publication === "run_published" && parent.role !== "input")
    .map((parent) => parent.artifactId);

  if (run.status === "succeeded") {
    if (
      !isNonEmptyString(run.result_artifact_id) ||
      run.result_artifact_id !== manifest.artifact.artifact_id ||
      run.failure_artifact_id != null ||
      manifest.artifact.kind !== "run_result" ||
      manifest.artifact.payload_schema_id !== "run-result@1" ||
      payload.result_schema_version !== "run-result@1" ||
      payload.outcome_code !== "playtest_completed" ||
      attemptNo === null ||
      !isNonEmptyString(payload.primary_artifact_id) ||
      !Array.isArray(payload.produced_artifact_ids) ||
      !payload.produced_artifact_ids.every(isNonEmptyString) ||
      !sameStringSet(payload.produced_artifact_ids, projectedPublishedIds) ||
      !isNonNegativeInteger(payload.finding_count)
    ) {
      fail("Succeeded Run does not close against a playtest RunResult manifest.");
    }
    const primaryParent = projection.parents.find(
      (parent) => parent.artifactId === payload.primary_artifact_id,
    );
    if (
      primaryParent?.role !== "output" ||
      primaryParent.publication !== "run_published" ||
      result === null
    ) {
      fail("RunResult primary Playtest trace binding is invalid.");
    }
    if (!isRecord(payload.summary)) fail("RunResult summary is invalid.");
    const summary = payload.summary;
    if (
      summary.summary_schema_version !== "run-result-summary@1" ||
      summary.outcome_code !== "playtest_completed" ||
      summary.primary_artifact_kind !== "playtest_trace" ||
      summary.produced_artifact_count !== payload.produced_artifact_ids.length ||
      summary.finding_count !== payload.finding_count
    ) {
      fail("RunResult summary differs from the terminal manifest.");
    }
    const boundTrace = requireTraceAuthority(
      result,
      payload.primary_artifact_id,
      projection.terminalVersionTuple,
      suite,
    );
    if (!containsStringSet(projectedInputIds, boundTrace.inputArtifactIds)) {
      fail("RunResult manifest omits an input proven by its Playtest trace.");
    }
    const requiredTraceParents = new Set(boundTrace.inputArtifactIds);
    for (const parentArtifactId of result.artifact.parent_artifact_ids) {
      if (requiredTraceParents.has(parentArtifactId)) continue;
      const manifestParent = projection.parents.find((parent) => parent.artifactId === parentArtifactId);
      if (manifestParent?.role !== "intermediate" || manifestParent.publication !== "run_published") {
        fail("Playtest trace has an extra parent outside Run-published intermediates.");
      }
    }
    requireFrozenInputTuple(payload.version_projection, suite, boundTrace.seed);
    const requestCandidateStatus =
      requestCandidate === null
        ? "not_provided"
        : assessRequestCandidateAgainstTrace(
            requestCandidate,
            suite,
            boundTrace.trace.rawPayload,
            boundTrace.selection,
          );
    const trace = boundTrace.trace;
    const completedEpisodeCount = trace.episodes.filter((episode) => episode.completed).length;
    return {
      allEpisodesCompleted: completedEpisodeCount === trace.episodes.length,
      attemptNo,
      completedEpisodeCount,
      findingCount: payload.finding_count,
      kind: "succeeded",
      manifest,
      manifestAuthority: projection,
      requestCandidateStatus,
      resultArtifact: result,
      run,
      runStatus: "succeeded",
      selection: boundTrace.selection,
      trace,
    };
  }

  if (run.status !== "failed" && run.status !== "cancelled" && run.status !== "timed_out") {
    fail("Run has not reached a terminal Playtest state.");
  }
  if (
    result !== null ||
    !isNonEmptyString(run.failure_artifact_id) ||
    run.failure_artifact_id !== manifest.artifact.artifact_id ||
    run.result_artifact_id != null ||
    manifest.artifact.kind !== "run_failure" ||
    manifest.artifact.payload_schema_id !== "run-failure@1" ||
    payload.failure_schema_version !== "run-failure@1" ||
    !isNonEmptyString(payload.cause_code) ||
    !isNonEmptyString(payload.redacted_message) ||
    !Array.isArray(payload.evidence_artifact_ids) ||
    !payload.evidence_artifact_ids.every(isNonEmptyString) ||
    !sameStringSet(payload.evidence_artifact_ids, projectedPublishedIds) ||
    projection.parents.some((parent) => parent.role === "output")
  ) {
    fail("Non-success Run does not close against its RunFailure manifest.");
  }
  requireFrozenInputTuple(payload.version_projection, suite);
  const knownScenarioIds = new Set(
    suite.task_suite.episodes.map((episode) => episode.scenario_spec_artifact_id),
  );
  if (!projectedInputIds.some((artifactId) => knownScenarioIds.has(artifactId))) {
    fail("RunFailure manifest has no selected TaskSuite scenario input.");
  }
  let requestCandidateStatus: PlaytestTerminalBase["requestCandidateStatus"] = "not_provided";
  let selectionCandidate: EpisodeSelectionAuthority | null = null;
  if (requestCandidate !== null) {
    try {
      const candidateSelection = requireRequestCandidate(requestCandidate, suite);
      const candidateInputIds = expectedSelectionInputIds(suite, candidateSelection);
      if (requestCandidate.cassette_artifact_id != null) {
        candidateInputIds.push(requestCandidate.cassette_artifact_id);
      }
      if (containsStringSet(projectedInputIds, candidateInputIds)) {
        requestCandidateStatus = "visible_bindings_match";
        selectionCandidate = candidateSelection;
      } else {
        requestCandidateStatus = "stale";
      }
    } catch (error) {
      if (!(error instanceof PlaytestAuthorityError)) throw error;
      requestCandidateStatus = "stale";
    }
  }
  return {
    attemptNo,
    causeCode: payload.cause_code,
    kind: "failed",
    manifest,
    manifestAuthority: projection,
    message: payload.redacted_message,
    requestCandidateStatus,
    resultArtifact: null,
    run,
    runStatus: run.status,
    selectionCandidate,
  };
}

export function bindPlaytestFindingLinks(
  terminal: SucceededPlaytestAuthority,
  links: readonly RunFindingLink[],
): readonly RunFindingLink[] {
  if (links.length !== terminal.findingCount) {
    fail("Playtest Finding link closure is partial.");
  }
  const sorted = [...links].sort((left, right) => left.ordinal - right.ordinal);
  const findingIds = new Set<string>();
  for (const [index, link] of sorted.entries()) {
    const finding = link.finding;
    if (
      link.view_schema_version !== "run-finding-link-view@1" ||
      link.run_id !== terminal.run.run_id ||
      link.attempt_no !== terminal.attemptNo ||
      link.ordinal !== index + 1 ||
      !isSha256(link.finding_digest) ||
      link.evidence_artifact_id !== terminal.trace.artifact.artifact.artifact_id ||
      finding.revision_schema_version !== "finding-revision@1" ||
      finding.payload.payload_schema_version !== "finding-payload@1" ||
      !isNonEmptyString(finding.finding_id) ||
      !isPositiveInteger(finding.revision) ||
      (finding.supersedes_revision != null &&
        (!isPositiveInteger(finding.supersedes_revision) ||
          finding.supersedes_revision >= finding.revision)) ||
      findingIds.has(finding.finding_id) ||
      finding.payload.source !== "playtest" ||
      (finding.payload.oracle_type !== "deterministic" && finding.payload.oracle_type !== "llm-assisted") ||
      finding.payload.producer_run_id !== terminal.run.run_id ||
      finding.payload.snapshot_id !== terminal.trace.artifact.artifact.version_tuple.ir_snapshot_id
    ) {
      fail("Run Finding link differs from the exact Playtest trace authority.");
    }
    findingIds.add(finding.finding_id);
  }
  return sorted;
}
