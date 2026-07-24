import type { components } from "../../api/generated/openapi";

type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
type RunPage = components["schemas"]["OpaquePageV1_RunViewV1_"];
type RunView = components["schemas"]["RunViewV1"];
type RunKindRef = components["schemas"]["RunKindRef"];

export interface ReplaySourceRun extends RunView {
  completedAt: string | null;
  outcomeCode: string;
  runKind: RunKindRef;
}

export type ReplaySourceRunPage = Omit<RunPage, "items"> & { items: ReplaySourceRun[] };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function sameRunKind(left: RunKindRef, right: RunKindRef): boolean {
  return left.kind === right.kind && left.version === right.version;
}

function terminalManifestId(run: RunView): string {
  const artifactId = run.status === "succeeded" ? run.result_artifact_id : run.failure_artifact_id;
  if (!artifactId) throw new Error("Cassette-backed Run has no exact terminal manifest.");
  return artifactId;
}

function parseTerminalManifest(
  run: RunView,
  artifact: ArtifactPayloadView,
): { completedAt: string | null; outcomeCode: string; runKind: RunKindRef } {
  const artifactId = terminalManifestId(run);
  const result = run.status === "succeeded";
  const expectedKind = result ? "run_result" : "run_failure";
  const expectedSchema = result ? "run-result@1" : "run-failure@1";
  const payload = artifact.payload;
  if (
    artifact.artifact.artifact_id !== artifactId ||
    artifact.artifact.kind !== expectedKind ||
    artifact.artifact.payload_schema_id !== expectedSchema ||
    !isRecord(payload) ||
    payload.run_id !== run.run_id ||
    !isRecord(payload.run_kind) ||
    typeof payload.run_kind.kind !== "string" ||
    typeof payload.run_kind.version !== "number"
  ) {
    throw new Error("Replay source terminal manifest does not close over the Run identity.");
  }
  const discriminator = result ? payload.result_schema_version : payload.failure_schema_version;
  const outcomeCode = result ? payload.outcome_code : payload.cause_code;
  if (discriminator !== expectedSchema || typeof outcomeCode !== "string" || !outcomeCode) {
    throw new Error("Replay source terminal manifest has an unsupported payload.");
  }
  return {
    completedAt: artifact.artifact.created_at ?? null,
    outcomeCode,
    runKind: { kind: payload.run_kind.kind, version: payload.run_kind.version },
  };
}

export async function projectReplaySourcePage(
  page: RunPage,
  expectedRunKind: RunKindRef,
  readArtifact: (artifactId: string) => Promise<ArtifactPayloadView>,
): Promise<ReplaySourceRunPage> {
  const cassetteBacked = page.items.filter((run) => run.terminal_cassette_artifact_id != null);
  const projected = await Promise.all(
    cassetteBacked.map(async (run) => {
      const manifest = await readArtifact(terminalManifestId(run));
      return { ...run, ...parseTerminalManifest(run, manifest) };
    }),
  );
  return {
    ...page,
    items: projected.filter((run) => sameRunKind(run.runKind, expectedRunKind)),
  };
}

const kindLabels: Readonly<Record<string, string>> = {
  "constraint_proposal.propose": "约束提案",
  "generation.propose": "内容生成",
  "patch.repair": "Patch 修复",
  "playtest.run": "自动试玩",
  "review.run": "内容审查",
};

const statusLabels: Readonly<Record<RunView["status"], string>> = {
  cancelled: "已取消",
  failed: "失败",
  leased: "已租约",
  queued: "排队中",
  retry_wait: "等待重试",
  running: "运行中",
  succeeded: "成功",
  timed_out: "已超时",
};

const outcomeLabels: Readonly<Record<string, string>> = {
  candidate_generated: "候选内容已生成",
  generation_gate_rejected: "生成门禁未通过",
  patch_repair_completed: "Patch 修复完成",
  playtest_completed: "试玩完成",
  repair_completed: "Patch 修复完成",
  repair_source_failed: "修复运行失败",
  review_completed: "审查完成",
  step_limit_exhausted: "达到试玩步数上限",
};

function compactIdentifier(value: string): string {
  return value.length <= 22 ? value : `${value.slice(0, 10)}…${value.slice(-8)}`;
}

export function replaySourceOptionLabel(run: ReplaySourceRun): string {
  const kind = kindLabels[run.runKind.kind] ?? run.runKind.kind;
  const outcome = outcomeLabels[run.outcomeCode] ?? run.outcomeCode;
  const attempt = run.attempt_no ? ` · 第 ${run.attempt_no} 次执行` : "";
  const completed = run.completedAt ? ` · ${run.completedAt.replace("T", " ").replace(/Z$/u, " UTC")}` : "";
  return `${kind} · ${statusLabels[run.status]}${attempt} · ${outcome}${completed} · ${compactIdentifier(run.run_id)}`;
}
