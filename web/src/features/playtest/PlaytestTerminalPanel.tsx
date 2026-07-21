import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, CircleX, Link2, ScrollText } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import { cursorFromPage, CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import { FindingCard } from "../../components/evidence";
import { adaptPlaytestEpisodeTrace, TracePlayer } from "../../components/playtest";
import { ProblemPanel, StatePanel } from "../../components/ui";
import type { PlaytestApi, PlaytestRunRequest, RunFindingLinkPage, TaskSuiteArtifactView } from "./api";
import {
  bindPlaytestFindingLinks,
  bindPlaytestTerminalAuthority,
  PlaytestAuthorityError,
  type PlaytestTerminalAuthority,
  type SucceededPlaytestAuthority,
} from "./authority";

type RunFindingLink = RunFindingLinkPage["items"][number];

const TERMINAL_RUN_STATUSES = new Set(["succeeded", "failed", "cancelled", "timed_out"]);
const requestCandidateLabels = {
  not_provided: "未提供；终态由服务端结果闭合",
  stale: "可见字段已过期；不作为终态权威",
  visible_bindings_match: "可见字段一致；仅作本地提示",
} as const;

async function collectFindingLinks(api: PlaytestApi, runId: string): Promise<RunFindingLink[]> {
  const links: RunFindingLink[] = [];
  const seen = new Set<string>();
  let cursor: string | null = null;
  let readSnapshotId: string | null = null;

  for (let pageCount = 0; pageCount < 256; pageCount += 1) {
    const page = await api.listRunFindingLinks(runId, cursor);
    if (readSnapshotId !== null && page.read_snapshot_id !== readSnapshotId) {
      throw new PlaytestAuthorityError("Finding link pagination changed read snapshot.");
    }
    readSnapshotId = page.read_snapshot_id;
    links.push(...page.items);
    const next = cursorFromPage(page);
    if (next === null) return links;
    if (seen.has(next)) throw new PlaytestAuthorityError("Finding link pagination returned a cursor cycle.");
    seen.add(next);
    cursor = next;
  }
  throw new PlaytestAuthorityError("Finding link pagination exceeded its bounded page count.");
}

async function loadTerminalAuthority(
  api: PlaytestApi,
  runId: string,
  run: Awaited<ReturnType<PlaytestApi["getRun"]>>,
  requestCandidate: PlaytestRunRequest | null,
  suite: TaskSuiteArtifactView,
): Promise<{ links: readonly RunFindingLink[]; terminal: PlaytestTerminalAuthority }> {
  const manifestId = run.status === "succeeded" ? run.result_artifact_id : run.failure_artifact_id;
  if (manifestId == null) throw new PlaytestAuthorityError("Terminal Run has no exact manifest Artifact.");

  const [manifest, result] = await Promise.all([
    api.getArtifact(manifestId),
    run.status === "succeeded" ? api.getPlaytestResult(runId) : Promise.resolve(null),
  ]);
  const terminal = bindPlaytestTerminalAuthority({
    expectedRunId: runId,
    manifest,
    requestCandidate,
    result,
    run,
    suite,
  });
  if (terminal.kind === "failed") return { links: [], terminal };

  const links = bindPlaytestFindingLinks(terminal, await collectFindingLinks(api, runId));
  return { links, terminal };
}

function FindingLedger({
  links,
  terminal,
}: {
  links: readonly RunFindingLink[];
  terminal: SucceededPlaytestAuthority;
}) {
  if (links.length === 0) {
    return (
      <StatePanel
        description="RunResult 声明 finding_count=0；没有可展示的 exact Finding 修订。"
        headingLevel={3}
        state="empty"
        title="没有 Playtest Findings"
      />
    );
  }
  if (terminal.attemptNo === null) {
    return (
      <StatePanel
        description="成功结果缺少 attempt authority；Finding 链接未展示。"
        headingLevel={3}
        state="error"
        title="Finding authority 不完整"
      />
    );
  }
  return (
    <div className="gf-playtest-terminal__findings">
      {links.map((link) => (
        <FindingCard
          authorityBinding={{
            attemptNo: terminal.attemptNo!,
            evidenceArtifactId: link.evidence_artifact_id,
            findingDigest: link.finding_digest,
            ordinal: link.ordinal,
          }}
          detailHref={`/findings/${encodeURIComponent(link.finding.finding_id)}/revisions/${link.finding.revision}`}
          finding={link.finding}
          key={`${link.finding.finding_id}:${link.finding.revision}`}
        />
      ))}
    </div>
  );
}

function SucceededResult({
  links,
  terminal,
}: {
  links: readonly RunFindingLink[];
  terminal: SucceededPlaytestAuthority;
}) {
  const [episodeId, setEpisodeId] = useState(terminal.trace.episodes[0]?.episodeId ?? "");
  useEffect(() => {
    if (!terminal.trace.episodes.some((episode) => episode.episodeId === episodeId)) {
      setEpisodeId(terminal.trace.episodes[0]?.episodeId ?? "");
    }
  }, [episodeId, terminal.trace.episodes]);
  const selected = terminal.trace.episodes.find((episode) => episode.episodeId === episodeId) ?? null;
  const playback = useMemo(
    () =>
      selected === null
        ? null
        : adaptPlaytestEpisodeTrace(terminal.trace.rawPayload, {
            episodeId: selected.episodeId,
            traceId: `${terminal.trace.artifact.artifact.artifact_id}#${selected.episodeId}`,
          }),
    [selected, terminal.trace.artifact.artifact.artifact_id, terminal.trace.rawPayload],
  );

  return (
    <section className="gf-playtest-terminal" aria-labelledby="playtest-terminal-title">
      <header className="gf-playtest-terminal__header">
        <div
          className="gf-playtest-terminal__status"
          data-complete={terminal.allEpisodesCompleted || undefined}
        >
          {terminal.allEpisodesCompleted ? (
            <CheckCircle2 aria-hidden="true" />
          ) : (
            <CircleX aria-hidden="true" />
          )}
          <div>
            <p>Run status · succeeded</p>
            <h2 id="playtest-terminal-title">
              {terminal.allEpisodesCompleted ? "Run 已完成，全部任务通过" : "Run 已完成，任务未全部通过"}
            </h2>
          </div>
        </div>
        <dl>
          <div>
            <dt>Episode completion</dt>
            <dd>
              {terminal.completedEpisodeCount} / {terminal.trace.episodes.length} episodes completed
            </dd>
          </div>
          <div>
            <dt>Finding count</dt>
            <dd>{terminal.findingCount}</dd>
          </div>
          <div>
            <dt>Browser request candidate</dt>
            <dd>{requestCandidateLabels[terminal.requestCandidateStatus]}</dd>
          </div>
          <div>
            <dt>Trace Artifact</dt>
            <dd>
              <a href={`/artifacts/${encodeURIComponent(terminal.trace.artifact.artifact.artifact_id)}`}>
                {terminal.trace.artifact.artifact.artifact_id}
              </a>
            </dd>
          </div>
          <div>
            <dt>RunResult manifest</dt>
            <dd>
              <a href={`/artifacts/${encodeURIComponent(terminal.manifest.artifact.artifact_id)}`}>
                {terminal.manifest.artifact.artifact_id}
              </a>
            </dd>
          </div>
        </dl>
      </header>

      <section className="gf-playtest-terminal__episodes" aria-label="Playtest episode results">
        <div className="gf-playtest-terminal__section-title">
          <ScrollText aria-hidden="true" size={19} />
          <div>
            <p>Deterministic completion oracle</p>
            <h3>Episode 结果与轨迹</h3>
          </div>
        </div>
        <div className="gf-playtest-terminal__episode-tabs" role="group" aria-label="选择轨迹 episode">
          {terminal.trace.episodes.map((episode) => (
            <button
              aria-pressed={episode.episodeId === episodeId}
              data-completed={episode.completed || undefined}
              key={episode.episodeId}
              onClick={() => setEpisodeId(episode.episodeId)}
              type="button"
            >
              <span>{episode.episodeId}</span>
              <strong>{episode.completed ? "completed" : episode.terminalReason.replace(/_/g, " ")}</strong>
            </button>
          ))}
        </div>
        {playback === null ? (
          <StatePanel
            description="该 episode 的真实 PlaytestTraceV1 无法适配到静态通用播放器；没有猜测或合成帧。"
            headingLevel={3}
            state="error"
            title="轨迹载荷不可播放"
          />
        ) : (
          <TracePlayer trace={playback} />
        )}
      </section>

      <section
        className="gf-playtest-terminal__finding-ledger"
        aria-labelledby="playtest-finding-ledger-title"
      >
        <div className="gf-playtest-terminal__section-title">
          <Link2 aria-hidden="true" size={19} />
          <div>
            <p>Exact RunFindingLinkViewV1</p>
            <h3 id="playtest-finding-ledger-title">Playtest Findings</h3>
          </div>
        </div>
        <FindingLedger links={links} terminal={terminal} />
      </section>
    </section>
  );
}

export function PlaytestTerminalPanel({
  api,
  request,
  runId,
  suite,
}: {
  api: PlaytestApi;
  request: PlaytestRunRequest | null;
  runId: string;
  suite: TaskSuiteArtifactView | null;
}) {
  const suiteOwnerKey =
    suite === null ? null : `${suite.artifact.artifact_id}\u0000${suite.artifact.payload_hash}`;
  const run = useQuery({
    queryFn: () => api.getRun(runId),
    queryKey: ["playtest", "run", runId],
    retry: false,
  });
  const isTerminal = run.data !== undefined && TERMINAL_RUN_STATUSES.has(run.data.status);
  const terminal = useQuery({
    enabled: isTerminal && suite !== null,
    queryFn: async () => ({
      ownerKey: suiteOwnerKey,
      value: await loadTerminalAuthority(api, runId, run.data!, request, suite!),
    }),
    queryKey: ["playtest", "terminal", runId, run.data?.revision ?? null, suiteOwnerKey, request],
    retry: false,
  });

  if (!isTerminal || run.data === undefined) return null;
  if (suite === null) {
    return (
      <StatePanel
        description="当前 URL 没有可通过专用 read 重验的 TaskSuite；Run 状态可查看，但不会从浏览器状态猜测 suite。"
        state="error"
        title="缺少 exact TaskSuite authority"
      />
    );
  }
  if (terminal.isPending) {
    return (
      <StatePanel
        description="正在闭合 Run manifest、PlaytestTrace 与 Finding links。"
        state="loading"
        title="正在读取终态证据"
      />
    );
  }
  if (terminal.isError) {
    if (terminal.error instanceof CursorExpiredError) {
      return (
        <StatePanel
          action={
            <button className="gf-secondary-button" onClick={() => void terminal.refetch()} type="button">
              从第一页重新读取
            </button>
          }
          description="Finding link cursor 已过期；页面没有静默跳过缺失页。"
          state="error"
          title="终态 Finding 游标已过期"
        />
      );
    }
    if (terminal.error instanceof ApiProblemError) return <ProblemPanel problem={terminal.error.problem} />;
    return (
      <StatePanel
        description={terminal.error.message}
        state="error"
        title="Playtest 终态 authority 无法闭合"
      />
    );
  }
  if (terminal.data.ownerKey !== suiteOwnerKey) {
    return (
      <StatePanel
        description="终态查询结果不属于当前 TaskSuite owner；旧缓存未被展示。"
        state="error"
        title="Playtest 终态 owner 已变化"
      />
    );
  }
  if (terminal.data.value.terminal.kind === "failed") {
    return (
      <StatePanel
        description={`${terminal.data.value.terminal.causeCode}: ${terminal.data.value.terminal.message}`}
        state="error"
        title={`Playtest Run ${terminal.data.value.terminal.runStatus}`}
      />
    );
  }
  return <SucceededResult links={terminal.data.value.links} terminal={terminal.data.value.terminal} />;
}
