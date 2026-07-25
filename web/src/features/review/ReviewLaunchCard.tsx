import { useQuery } from "@tanstack/react-query";
import { Play, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { createMutationIntent, ReauthenticationRequiredError, type MutationIntent } from "../../api/csrf";
import { ApiProblemError } from "../../api/problem";
import { ReauthenticationLink } from "../../app/ReauthenticationLink";
import { TechnicalDetails } from "../../components/identity";
import { ProblemPanel, StatePanel } from "../../components/ui";
import { replaySourceOptionLabel, type ReplaySourceRun } from "../runs/replaySources";
import type {
  ExecutionOptionResolveRequest,
  ExecutionProfilePage,
  ProspectiveReviewRunRequest,
  ReviewApi,
  RunAccepted,
  RunSubmissionRequest,
} from "./api";
import { profileBusinessLabel, profileKey } from "../execution-profiles";

type ExecutionProfile = ExecutionProfilePage["items"][number];
type LlmExecutionMode = ProspectiveReviewRunRequest["llm_execution_mode"];

export interface ReviewCandidateContext {
  constraintArtifactId: string;
  snapshotArtifactId: string;
  sourceRunId: string | null;
}

interface ReviewLaunchAttempt {
  error: Error | null;
  intent: MutationIntent;
  pending: boolean;
  prospective: ProspectiveReviewRunRequest;
  request: ExecutionOptionResolveRequest;
  resolved: RunSubmissionRequest | null;
  result: RunAccepted | null;
}

class ReviewLaunchAuthorityError extends Error {
  override name = "ReviewLaunchAuthorityError";
}

function normalizedError(error: unknown): Error {
  return error instanceof Error ? error : new Error("Review 启动请求失败。");
}

function supportsReview(profile: ExecutionProfile): boolean {
  return profile.compatible_run_kinds.some(
    (runKind) => runKind.kind === "review.run" && runKind.version === 1,
  );
}

function sameSourceRun(left: string | null | undefined, right: string | null): boolean {
  return (left ?? null) === right;
}

function selectedProfiles(
  profiles: readonly ExecutionProfile[],
  keys: readonly string[],
): ExecutionProfile[] {
  return keys
    .map((key) => profiles.find((profile) => profileKey(profile) === key))
    .filter((profile): profile is ExecutionProfile => profile !== undefined)
    .sort((left, right) => profileKey(left).localeCompare(profileKey(right)));
}

function toggleSelection(keys: readonly string[], key: string): string[] {
  return keys.includes(key) ? keys.filter((item) => item !== key) : [...keys, key].sort();
}

async function collectReviewProfiles(api: ReviewApi): Promise<ExecutionProfile[]> {
  const profiles: ExecutionProfile[] = [];
  const seenCursors = new Set<string>();
  const seenProfiles = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;

  for (;;) {
    const page = await api.listReviewProfiles(cursor);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new ReviewLaunchAuthorityError("Execution profile 目录分页快照发生变化。");
    }
    readSnapshotId = page.read_snapshot_id;
    for (const profile of page.items) {
      const key = profileKey(profile);
      if (seenProfiles.has(key)) {
        throw new ReviewLaunchAuthorityError(`Execution profile 目录重复返回 ${key}。`);
      }
      seenProfiles.add(key);
      profiles.push(profile);
    }
    const next = page.next_cursor ?? null;
    if (next === null) return profiles;
    if (seenCursors.has(next)) {
      throw new ReviewLaunchAuthorityError("Execution profile 目录返回循环游标。");
    }
    seenCursors.add(next);
    cursor = next;
  }
}

async function collectReplaySourceRuns(api: ReviewApi): Promise<ReplaySourceRun[]> {
  const runs: ReplaySourceRun[] = [];
  const seenCursors = new Set<string>();
  const seenRuns = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;

  for (;;) {
    const page = await api.listReplaySourceRuns(cursor);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new ReviewLaunchAuthorityError("已完成 Run 目录分页快照发生变化。");
    }
    readSnapshotId = page.read_snapshot_id;
    for (const run of page.items) {
      if (seenRuns.has(run.run_id)) {
        throw new ReviewLaunchAuthorityError("已完成 Run 目录返回重复运行。");
      }
      seenRuns.add(run.run_id);
      runs.push(run);
    }
    const next = page.next_cursor ?? null;
    if (next === null) return runs;
    if (seenCursors.has(next)) throw new ReviewLaunchAuthorityError("已完成 Run 目录返回循环游标。");
    seenCursors.add(next);
    cursor = next;
  }
}

function replayRunLabel(run: ReplaySourceRun): string {
  return replaySourceOptionLabel(run);
}

function LaunchFailure({ attempt, onRetry }: { attempt: ReviewLaunchAttempt; onRetry(): void }) {
  if (attempt.error === null) return null;
  if (attempt.error instanceof ApiProblemError) return <ProblemPanel problem={attempt.error.problem} />;
  if (attempt.error instanceof ReauthenticationRequiredError) {
    return (
      <StatePanel
        action={<ReauthenticationLink />}
        description="当前浏览器标签页没有可用 CSRF 会话；Review mutation 尚未发送。"
        state="error"
        title="需要重新登录"
      />
    );
  }
  if (attempt.error instanceof ReviewLaunchAuthorityError) {
    return (
      <StatePanel
        description={`${attempt.error.message} 尚未提交 Run；可修改选择或原样重新启动。`}
        state="error"
        title="Review 启动 authority 不安全"
      />
    );
  }
  if (attempt.resolved === null) {
    return (
      <StatePanel
        description="Execution option 尚未解析完成，也没有提交 Run；可修改选择或原样重新启动。"
        state="error"
        title="Review 解析失败"
      />
    );
  }
  return (
    <StatePanel
      action={
        <button className="gf-secondary-button" onClick={onRetry} type="button">
          以同一 intent 重试
        </button>
      }
      description="网络结果未知；页面保留已解析的 exact request 与同一 Idempotency-Key，不会自动创建新 intent。"
      state="error"
      title="Review 结果未知"
    />
  );
}

function blocksNewIntent(attempt: ReviewLaunchAttempt | null): boolean {
  return attempt?.resolved != null && attempt.error != null && !(attempt.error instanceof ApiProblemError);
}

export function ReviewLaunchCard({ api, context }: { api: ReviewApi; context: ReviewCandidateContext }) {
  const catalog = useQuery({
    queryFn: () => collectReviewProfiles(api),
    queryKey: ["review", "launch-profiles"],
    retry: false,
  });
  const constraint = useQuery({
    queryFn: async () => {
      const view = await api.getConstraint(context.constraintArtifactId);
      if (view.artifact.artifact_id !== context.constraintArtifactId) {
        throw new ReviewLaunchAuthorityError("Constraint authority 与请求的 exact Artifact 不一致。");
      }
      return view;
    },
    queryKey: ["review", "launch-constraint", context.constraintArtifactId],
    retry: false,
  });
  const [reviewKey, setReviewKey] = useState("");
  const [checkerKeys, setCheckerKeys] = useState<string[]>([]);
  const [simulationKeys, setSimulationKeys] = useState<string[]>([]);
  const [triageKey, setTriageKey] = useState("");
  const [seed, setSeed] = useState("1");
  const [mode, setMode] = useState<"" | LlmExecutionMode>("");
  const [replaySourceRunId, setReplaySourceRunId] = useState("");
  const [attempt, setAttempt] = useState<ReviewLaunchAttempt | null>(null);
  const replayRuns = useQuery({
    enabled: mode === "replay",
    queryFn: () => collectReplaySourceRuns(api),
    queryKey: ["review", "replay-source-runs"],
    retry: false,
  });

  if (catalog.isPending || constraint.isPending) {
    return (
      <section className="gf-review__launch-card" aria-label="Review 启动卡">
        <StatePanel
          description="正在读取 active profile 目录与 exact constraint。"
          state="loading"
          title="正在准备 Review 启动卡"
        />
      </section>
    );
  }
  if (catalog.isError) {
    return (
      <section className="gf-review__launch-card" aria-label="Review 启动卡">
        {catalog.error instanceof ApiProblemError ? (
          <ProblemPanel problem={catalog.error.problem} />
        ) : (
          <StatePanel
            action={
              <button className="gf-secondary-button" onClick={() => void catalog.refetch()} type="button">
                重新读取 profile 目录
              </button>
            }
            description="未使用隐藏 profile 或 current alias；启动卡保持关闭。"
            state="error"
            title="无法读取 Review profiles"
          />
        )}
      </section>
    );
  }
  if (constraint.isError) {
    return (
      <section className="gf-review__launch-card" aria-label="Review 启动卡">
        {constraint.error instanceof ApiProblemError ? (
          <ProblemPanel problem={constraint.error.problem} />
        ) : constraint.error instanceof ReviewLaunchAuthorityError ? (
          <StatePanel
            description={`${constraint.error.message} 尚未提交 Run。`}
            state="error"
            title="Review 启动 authority 不安全"
          />
        ) : (
          <StatePanel
            description="无法确认 exact constraint 内容；启动卡保持关闭。"
            state="error"
            title="无法读取 Review constraint"
          />
        )}
      </section>
    );
  }

  const activeProfiles = catalog.data.filter(
    (profile) => profile.status === "active" && supportsReview(profile),
  );
  const reviewProfiles = activeProfiles.filter((profile) => profile.profile_kind === "review");
  const checkerProfiles = activeProfiles.filter((profile) => profile.profile_kind === "checker");
  const simulationProfiles = activeProfiles.filter((profile) => profile.profile_kind === "simulation");
  const triageProfiles = activeProfiles.filter((profile) => profile.profile_kind === "llm_triage");
  const selectedReview = reviewProfiles.find((profile) => profileKey(profile) === reviewKey);
  const selectedCheckers = selectedProfiles(checkerProfiles, checkerKeys);
  const selectedSimulations = selectedProfiles(simulationProfiles, simulationKeys);
  const selectedTriage = triageProfiles.find((profile) => profileKey(profile) === triageKey);
  const hasConstraintRules = constraint.data.constraints.length > 0;
  const profileSelectionValid =
    (selectedCheckers.length > 0 || selectedSimulations.length > 0) &&
    (!hasConstraintRules || selectedCheckers.length > 0);
  const seedRequired = [
    ...(selectedReview ? [selectedReview] : []),
    ...selectedCheckers,
    ...selectedSimulations,
    ...(selectedTriage ? [selectedTriage] : []),
  ].some((profile) => profile.stochastic);
  const parsedSeed = Number(seed);
  const validSeed = seed.trim().length > 0 && Number.isSafeInteger(parsedSeed) && parsedSeed >= 0;
  const expectedReplaySource = mode === "replay" ? replaySourceRunId.trim() : null;
  const controlsFrozen = Boolean(attempt?.pending || attempt?.result || blocksNewIntent(attempt));
  const canSubmit =
    !attempt?.pending &&
    !attempt?.result &&
    !blocksNewIntent(attempt) &&
    selectedReview !== undefined &&
    profileSelectionValid &&
    selectedTriage !== undefined &&
    (!seedRequired || validSeed) &&
    mode !== "" &&
    (mode !== "replay" || replayRuns.data?.some((run) => run.run_id === expectedReplaySource) === true);

  async function execute(frozen: ReviewLaunchAttempt) {
    setAttempt({ ...frozen, error: null, pending: true, result: null });
    let resolved = frozen.resolved;
    try {
      if (resolved === null) {
        const option = await api.resolveExecutionOption(frozen.request);
        const expectedSource = frozen.request.replay_source_run_id ?? null;
        if (
          option.resource_operation_id !== "submit_run_api_v1_runs_post" ||
          option.run_kind.kind !== "review.run" ||
          option.run_kind.version !== 1 ||
          option.llm_execution_mode !== frozen.request.llm_execution_mode ||
          !sameSourceRun(option.source_run_id, expectedSource) ||
          (frozen.request.llm_execution_mode === "replay" && !option.cassette_artifact_id)
        ) {
          throw new ReviewLaunchAuthorityError(
            "Execution option 与冻结的 Review operation、run kind、mode 或 replay source 不一致。",
          );
        }
        resolved = {
          ...frozen.prospective,
          cassette_artifact_id: option.cassette_artifact_id ?? null,
          execution_version_plan: option.execution_version_plan,
        };
        setAttempt({ ...frozen, error: null, pending: true, resolved, result: null });
      }
      const result = await api.submitRun(resolved, frozen.intent);
      setAttempt({ ...frozen, error: null, pending: false, resolved, result });
    } catch (error) {
      setAttempt({
        ...frozen,
        error: normalizedError(error),
        pending: false,
        resolved,
        result: null,
      });
    }
  }

  function submit() {
    if (!canSubmit || !selectedReview || !selectedTriage || (seedRequired && !validSeed)) return;
    const prospective: ProspectiveReviewRunRequest = {
      cassette_artifact_id: null,
      execution_version_plan: null,
      llm_execution_mode: mode,
      params: {
        checker_profiles: selectedCheckers.map((profile) => profile.profile),
        constraint_snapshot_artifact_id: context.constraintArtifactId,
        llm_triage_policy: selectedTriage.profile,
        review_profile: selectedReview.profile,
        schema_version: "review-run@1",
        selection: { entity_ids: [], mode: "full", relation_ids: [] },
        simulation_profiles: selectedSimulations.map((profile) => profile.profile),
        snapshot_artifact_id: context.snapshotArtifactId,
      },
      request_schema_version: "run-submission-request@1",
      seed: seedRequired ? parsedSeed : null,
    };
    const request: ExecutionOptionResolveRequest = {
      llm_execution_mode: mode,
      prospective_request: prospective,
      replay_source_run_id: expectedReplaySource,
      request_schema_version: "execution-option-resolve-request@1",
      resource_operation_id: "submit_run_api_v1_runs_post",
      run_kind: { kind: "review.run", version: 1 },
    };
    void execute({
      error: null,
      intent: createMutationIntent(),
      pending: false,
      prospective,
      request,
      resolved: null,
      result: null,
    });
  }

  return (
    <section className="gf-review__launch-card" aria-labelledby="review-launch-title">
      <header>
        <div className="gf-review__launch-title">
          <Play aria-hidden="true" size={21} />
          <div>
            <p className="gf-review__kicker">候选内容 · 有边界的自动检查</p>
            <h2 id="review-launch-title">启动内容检查</h2>
          </div>
        </div>
        <p>选择本次要用的固定规则检查、经济仿真和 AI 问题归纳方案，然后开始一次可追溯的内容检查。</p>
      </header>

      <div className="gf-review__launch-body">
        <form
          className="gf-form gf-review__launch-form"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <label>
            内容检查方案
            <select
              disabled={controlsFrozen}
              onChange={(event) => setReviewKey(event.target.value)}
              value={reviewKey}
            >
              <option value="">请选择内容检查方案</option>
              {reviewProfiles.map((profile) => (
                <option key={profileKey(profile)} value={profileKey(profile)}>
                  {profileBusinessLabel(profile)}
                </option>
              ))}
            </select>
          </label>

          <p className="gf-review__launch-note" role="note">
            {hasConstraintRules
              ? `当前规则含 ${constraint.data.constraints.length} 条约束；必须选择至少一项确定性检查，经济仿真可选。`
              : "当前没有额外规则；确定性检查或经济仿真至少选择一种。"}
          </p>

          <fieldset className="gf-review__profile-fieldset">
            <legend>确定性检查（{hasConstraintRules ? "当前规则下必选" : "没有额外规则时可选"}）</legend>
            {checkerProfiles.length === 0 ? (
              <p>
                当前没有兼容的确定性检查方案；
                {hasConstraintRules ? "由于存在规则，本次无法开始内容检查。" : "你仍可选择经济仿真开始检查。"}
              </p>
            ) : (
              checkerProfiles.map((profile) => {
                const key = profileKey(profile);
                return (
                  <label key={key}>
                    <input
                      checked={checkerKeys.includes(key)}
                      disabled={controlsFrozen}
                      onChange={() => setCheckerKeys((current) => toggleSelection(current, key))}
                      type="checkbox"
                    />
                    {profileBusinessLabel(profile)}
                  </label>
                );
              })
            )}
          </fieldset>

          <fieldset className="gf-review__profile-fieldset">
            <legend>经济仿真（没有额外规则时可单独使用）</legend>
            {simulationProfiles.length === 0 ? (
              <p>当前没有兼容的经济仿真方案。</p>
            ) : (
              simulationProfiles.map((profile) => {
                const key = profileKey(profile);
                return (
                  <label key={key}>
                    <input
                      checked={simulationKeys.includes(key)}
                      disabled={controlsFrozen}
                      onChange={() => setSimulationKeys((current) => toggleSelection(current, key))}
                      type="checkbox"
                    />
                    {profileBusinessLabel(profile)}
                  </label>
                );
              })
            )}
          </fieldset>

          <label>
            AI 问题归纳方案
            <select
              disabled={controlsFrozen}
              onChange={(event) => setTriageKey(event.target.value)}
              value={triageKey}
            >
              <option value="">不选择</option>
              {triageProfiles.map((profile) => (
                <option key={profileKey(profile)} value={profileKey(profile)}>
                  {profileBusinessLabel(profile)}
                </option>
              ))}
            </select>
          </label>
          {!selectedTriage && (
            <p className="gf-review__launch-note" role="note">
              当前自动检查需要选择一项 AI 问题归纳方案；AI 只负责整理建议，不负责判定对错。
            </p>
          )}

          <div className="gf-review__launch-row">
            {seedRequired ? (
              <label>
                随机种子
                <input
                  aria-label="随机种子"
                  aria-describedby="review-seed-help"
                  disabled={controlsFrozen}
                  min="0"
                  onChange={(event) => setSeed(event.target.value)}
                  required
                  step="1"
                  type="number"
                  value={seed}
                />
                <span className="u-small" id="review-seed-help">
                  所选方案包含随机执行；请填写一个非负整数，便于重复本次检查。
                </span>
              </label>
            ) : (
              <div className="gf-review__launch-note" role="note">
                <strong>不需要随机种子</strong>
                <span>当前所选方案均为确定性执行，重复运行会得到可复核的结果。</span>
              </div>
            )}
            <label>
              AI 运行方式
              <select
                disabled={controlsFrozen}
                onChange={(event) => {
                  const next = event.target.value as "" | LlmExecutionMode;
                  setMode(next);
                  if (next !== "replay") setReplaySourceRunId("");
                }}
                value={mode}
              >
                <option value="">请选择运行方式</option>
                <option value="live">在线检查（推荐）</option>
                <option value="record">在线检查并保存回放</option>
                <option value="replay">使用历史回放（测试用）</option>
              </select>
            </label>
          </div>
          {mode === "replay" && (
            <label>
              历史回放来源
              <select
                disabled={controlsFrozen}
                onChange={(event) => setReplaySourceRunId(event.target.value)}
                value={replaySourceRunId}
              >
                <option value="">
                  {replayRuns.isPending ? "正在读取已完成运行…" : "请选择一个已完成的回放来源"}
                </option>
                {replayRuns.data?.map((run) => (
                  <option key={run.run_id} value={run.run_id}>
                    {replayRunLabel(run)}
                  </option>
                ))}
              </select>
            </label>
          )}
          {mode === "replay" && replayRuns.isError && (
            <p className="gf-review__launch-note" role="alert">
              无法读取已完成的回放来源；页面不会要求你手工粘贴技术编号。
            </p>
          )}
          <button disabled={!canSubmit} type="submit">
            {attempt?.pending ? "正在准备检查…" : "开始内容检查"}
          </button>
        </form>

        <aside className="gf-review__launch-ledger" aria-label="本次内容检查范围">
          <ShieldCheck aria-hidden="true" size={20} />
          <h3>本次将检查</h3>
          <dl>
            <div>
              <dt>设计内容</dt>
              <dd>当前候选版本</dd>
            </div>
            <div>
              <dt>规则</dt>
              <dd>已发布的权威规则</dd>
            </div>
          </dl>
          <TechnicalDetails
            items={[
              ...(context.sourceRunId ? [{ label: "Source Run ID", value: context.sourceRunId }] : []),
              { label: "Candidate Artifact ID", value: context.snapshotArtifactId },
              { label: "Constraint Artifact ID", value: context.constraintArtifactId },
            ]}
            summary="查看检查输入技术信息"
          />
        </aside>
      </div>

      {attempt && <LaunchFailure attempt={attempt} onRetry={() => void execute(attempt)} />}
      {attempt?.result && (
        <div className="gf-review__launch-success" role="status">
          <strong>内容检查已开始</strong>
          <a href={`/runs/${encodeURIComponent(attempt.result.run_id)}`}>查看检查进度</a>
          <TechnicalDetails
            items={[{ label: "Run ID", value: attempt.result.run_id }]}
            summary="查看运行技术信息"
          />
        </div>
      )}
    </section>
  );
}
