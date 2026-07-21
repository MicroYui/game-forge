import type { components } from "../../api/generated/openapi";
import { adaptPlaytestEpisodeTrace, type TracePlayback } from "../../components/playtest";
import playtestTraceFixture from "./playtest-trace.fixture.json";

type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];
type FindingRevision = components["schemas"]["FindingRevisionV1"];
type GraphItem = components["schemas"]["GraphItemV1"];
type LineagePage = components["schemas"]["OpaquePageV1_LineageEntryV1_"];
type LogRecordView = components["schemas"]["LogRecordViewV1"];
type MergeConflict = components["schemas"]["MergeConflict"];
type SnapshotDiff = components["schemas"]["SnapshotDiff"];
type SnapshotDiffEntry = components["schemas"]["SnapshotDiffEntry"];

const hash = (digit: string) => `sha256:${digit.repeat(64)}`;

function finding(
  id: string,
  oracleType: FindingRevision["payload"]["oracle_type"],
  severity: FindingRevision["payload"]["severity"],
  message: string,
  defectClass: string,
): FindingRevision {
  const source = oracleType === "deterministic" ? "checker" : oracleType === "simulation" ? "sim" : "llm";
  return {
    created_at: "2026-07-19T08:30:00Z",
    finding_id: id,
    payload: {
      defect_class: defectClass,
      entities: ["QUEST:outpost-signal"],
      evidence:
        oracleType === "simulation"
          ? { confidence_interval: [0.81, 0.88], seed: 731 }
          : { path: ["QUEST:outpost-signal", "QUEST_STEP:recover-beacon"] },
      message,
      minimal_repro: {
        source_ref: {
          adapter: "aureus-csv@1",
          column: "prerequisite",
          file: "content/quests/outpost.csv",
          row: 17,
          sheet: "Quests",
        },
        step_id: "QUEST_STEP:recover-beacon",
      },
      oracle_type: oracleType,
      payload_schema_version: "finding-payload@1",
      producer_id: source === "checker" ? "checker:quest-reachability" : `${source}:outpost-review`,
      producer_run_id: `run:visual-${source}`,
      relations: ["relation:quest-has-step"],
      severity,
      snapshot_id: "artifact:ir-snapshot-outpost-r18",
      source,
      status: oracleType === "llm-assisted" ? "unproven" : "confirmed",
    },
    revision: oracleType === "deterministic" ? 4 : 1,
    revision_schema_version: "finding-revision@1",
    supersedes_revision: oracleType === "deterministic" ? 3 : null,
  };
}

export const evidenceFindings = {
  deterministic: finding(
    "finding:quest-unreachable-step",
    "deterministic",
    "critical",
    "信标回收步骤在当前任务前置图中不可达",
    "quest_reachability",
  ),
  simulation: finding(
    "finding:economy-source-pressure",
    "simulation",
    "major",
    "固定种子仿真显示早期金币净流入持续高于回收口",
    "economy_collapse",
  ),
  suggestion: finding(
    "finding:narrative-identity-hint",
    "llm-assisted",
    "minor",
    "对白可能提前暗示白鸢身份，请由主叙事确认",
    "narrative_spoiler",
  ),
} satisfies Record<string, FindingRevision>;

export const snapshotDiff: SnapshotDiff = {
  base_snapshot_id: "artifact:ir-snapshot-outpost-r17",
  diff_schema_version: "snapshot-diff@1",
  entry_count: 3,
  target_snapshot_id: "artifact:ir-snapshot-outpost-r18",
};

export const snapshotDiffEntries: readonly SnapshotDiffEntry[] = [
  {
    after: { presence: "present", value: null },
    before: { presence: "missing" },
    path: "/entities/QUEST:outpost-signal/attrs/optional_note",
  },
  {
    after: { presence: "present", value: 120 },
    before: { presence: "present", value: 180 },
    path: "/entities/QUEST:outpost-signal/attrs/reward_gold",
  },
  {
    after: { presence: "present", value: ["ITEM:beacon-core", "CURRENCY:gold"] },
    before: { presence: "present", value: ["CURRENCY:gold"] },
    path: "/entities/QUEST:outpost-signal/attrs/rewards",
  },
];

export const mergeConflicts: readonly MergeConflict[] = [
  {
    allowed_resolutions: ["keep_current", "take_proposed", "custom"],
    base: { presence: "present", value: 180 },
    current: { presence: "present", value: 150 },
    id: "conflict:quest-reward-gold",
    kind: "both_modified",
    path: "/entities/QUEST:outpost-signal/attrs/reward_gold",
    proposed: { presence: "present", value: 120 },
  },
];

export const safeLogRecords: readonly LogRecordView[] = [
  {
    record: {
      event_name: "patch.validation.completed",
      fields: {
        apiKey: "never-render-this-api-key",
        outcome: "validation_failed",
        rawResponse: "never-render-this-provider-response",
        snapshot_id: "artifact:ir-snapshot-outpost-r18",
        finding_count: 2,
      },
      level: "warning",
      log_id: "log:validation-outpost-00018",
      log_schema_version: "log-record@1",
      message: "补丁验证完成；确定性门禁未通过。",
      producer_run_id: "run:visual-checker",
      request_id: "request:visual-0819",
      run_id: "run:visual-validation",
      service: "gameforge.worker",
      span_id: "span:checker-suite",
      trace_id: "trace:validation-outpost",
      ts_utc: "2026-07-19T08:31:42Z",
    },
    redacted_fields: ["apiKey", "rawResponse"],
  },
];

export const artifactSummary: ArtifactSummary = {
  artifact_id: "artifact:playtest-trace:outpost-signal:r18",
  created_at: "2026-07-19T08:33:10Z",
  domain_scope: { domain_ids: ["quest:outpost", "economy:newbie-zone"] },
  kind: "playtest_trace",
  lineage_schema_version: "lineage@2",
  parent_artifact_ids: ["artifact:config-export:outpost-r18", "artifact:task-suite:outpost-r18"],
  payload_hash: hash("a"),
  payload_schema_id: "playtest-trace@1",
  summary_schema_version: "artifact-summary@1",
  version_tuple: {
    agent_graph_version: "playtest-layered@4",
    cassette_id: "artifact:cassette-bundle:outpost-r18",
    constraint_snapshot_id: "artifact:constraints:production-r7",
    doc_version: "outpost-brief@12",
    env_contract_version: "env@1",
    ir_snapshot_id: "artifact:ir-snapshot-outpost-r18",
    model_snapshot: "anthropic/claude-opus-4-8/m2a@1",
    prompt_version: "playtest-planner@7",
    seed: 731,
    tool_version: "gameforge-playtest@4",
  },
};

function parentArtifact(
  artifactId: string,
  kind: ArtifactSummary["kind"],
  payloadSchemaId: string,
): ArtifactSummary {
  return {
    artifact_id: artifactId,
    created_at: "2026-07-19T08:28:00Z",
    domain_scope: { domain_ids: ["quest:outpost"] },
    kind,
    lineage_schema_version: "lineage@2",
    parent_artifact_ids: [],
    payload_hash: hash(kind === "config_export" ? "b" : "c"),
    payload_schema_id: payloadSchemaId,
    summary_schema_version: "artifact-summary@1",
    version_tuple: {
      constraint_snapshot_id: "artifact:constraints:production-r7",
      ir_snapshot_id: "artifact:ir-snapshot-outpost-r18",
      tool_version: "gameforge@4",
    },
  };
}

export const artifactLineagePage: LineagePage = {
  expires_at: "2026-07-19T09:03:10Z",
  items: [
    {
      artifact: parentArtifact(
        "artifact:config-export:outpost-r18",
        "config_export",
        "aureus-config-export@2",
      ),
      depth: 1,
      entry_schema_version: "lineage-entry@1",
    },
    {
      artifact: parentArtifact("artifact:task-suite:outpost-r18", "task_suite", "task-suite@1"),
      depth: 1,
      entry_schema_version: "lineage-entry@1",
    },
  ],
  next_cursor: "opaque:visual-lineage-next-page",
  page_schema_version: "page@1",
  read_snapshot_id: "read-snapshot:visual-lineage-01",
};

export const graphItems: readonly GraphItem[] = [
  {
    entity: {
      attrs: { display_name: "前哨信标", chapter: 2, status: "draft" },
      id: "QUEST:outpost-signal",
      schema_version: "ir-core@1",
      source_ref: {
        adapter: "aureus-csv@1",
        column: "quest_id",
        file: "content/quests/outpost.csv",
        row: 17,
        sheet: "Quests",
      },
      tags: ["支线", "前哨站"],
      type: "QUEST",
    },
    item_id: "QUEST:outpost-signal",
    item_kind: "entity",
    item_schema_version: "graph-item@1",
    relation: null,
  },
  {
    entity: {
      attrs: { display_name: "林澄", faction: "FACTION:wardens" },
      id: "NPC:lincheng",
      schema_version: "ir-core@1",
      source_ref: {
        adapter: "aureus-csv@1",
        column: "npc_id",
        file: "content/npcs/outpost.csv",
        row: 4,
        sheet: "NPCs",
      },
      tags: ["任务发布者"],
      type: "NPC",
    },
    item_id: "NPC:lincheng",
    item_kind: "entity",
    item_schema_version: "graph-item@1",
    relation: null,
  },
  {
    entity: null,
    item_id: "relation:outpost-starts-at",
    item_kind: "relation",
    item_schema_version: "graph-item@1",
    relation: {
      attrs: { required: true },
      dst_id: "SPAWN_POINT:outpost-gate",
      id: "relation:outpost-starts-at",
      schema_version: "ir-core@1",
      source_ref: {
        adapter: "aureus-csv@1",
        column: "start_spawn",
        file: "content/quests/outpost.csv",
        row: 17,
        sheet: "Quests",
      },
      src_id: "QUEST:outpost-signal",
      type: "STARTS_AT",
    },
  },
  {
    entity: null,
    item_id: "relation:outpost-talks-to",
    item_kind: "relation",
    item_schema_version: "graph-item@1",
    relation: {
      attrs: { step_order: 1 },
      dst_id: "NPC:lincheng",
      id: "relation:outpost-talks-to",
      schema_version: "ir-core@1",
      source_ref: {
        adapter: "aureus-csv@1",
        column: "target_npc",
        file: "content/quests/outpost.csv",
        row: 17,
        sheet: "Quests",
      },
      src_id: "QUEST:outpost-signal",
      type: "TALKS_TO",
    },
  },
];

export const playtestTracePayload = playtestTraceFixture;

const adaptedTrace = adaptPlaytestEpisodeTrace(playtestTracePayload, {
  episodeId: "episode:outpost-signal",
  traceId: "artifact:playtest-trace:outpost-signal:r18",
});

if (adaptedTrace === null) throw new Error("visual PlaytestTraceV1 fixture must adapt");

export const visualTrace: TracePlayback = {
  ...adaptedTrace,
  markers: adaptedTrace.markers.map((marker) =>
    marker.kind === "failure"
      ? {
          ...marker,
          findings: [
            {
              findingId: "finding:quest-unreachable-step",
              href: "/findings/finding%3Aquest-unreachable-step/revisions/4",
              revision: 4,
            },
          ],
        }
      : marker,
  ),
};

/** Independently validated visual fixture; it is not part of playtest-trace@1. */
export const aureusSpatialFixture = {
  renderer_payload_schema_id: "aureus-spatial-2d@1",
  map: {
    blocked: [
      { x: 4, y: 2 },
      { x: 4, y: 3 },
      { x: 4, y: 4 },
    ],
    height: 7,
    width: 10,
  },
  frames: [
    {
      entities: [
        { id: "NPC:lincheng", kind: "npc", label: "林澄", x: 3, y: 2 },
        { id: "MONSTER:relay-guardian", kind: "monster", label: "中继守卫", x: 8, y: 5 },
      ],
      frame_id: "episode:outpost-signal:step:0",
      player: { x: 1, y: 1 },
    },
    {
      entities: [
        { id: "NPC:lincheng", kind: "npc", label: "林澄", x: 3, y: 2 },
        { id: "MONSTER:relay-guardian", kind: "monster", label: "中继守卫", x: 8, y: 5 },
      ],
      frame_id: "episode:outpost-signal:step:1",
      player: { x: 3, y: 2 },
    },
    {
      entities: [
        { id: "NPC:lincheng", kind: "npc", label: "林澄", x: 3, y: 2 },
        { id: "MONSTER:relay-guardian", kind: "monster", label: "中继守卫", x: 8, y: 5 },
      ],
      frame_id: "episode:outpost-signal:step:2",
      player: { x: 3, y: 3 },
    },
    {
      entities: [
        { id: "NPC:lincheng", kind: "npc", label: "林澄", x: 3, y: 2 },
        { id: "MONSTER:relay-guardian", kind: "monster", label: "中继守卫", x: 8, y: 5 },
      ],
      frame_id: "episode:outpost-signal:step:3",
      player: { x: 3, y: 3 },
    },
  ],
} as const;
