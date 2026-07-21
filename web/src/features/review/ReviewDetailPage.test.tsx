import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { createQueryClient } from "../../api/query-client";
import { CursorExpiredError } from "../../api/pagination";
import { ApiProblemError } from "../../api/problem";
import type { components } from "../../api/generated/openapi";
import type {
  ConstraintSnapshotView,
  FindingRevision,
  LineagePage,
  ReviewApi,
  ReviewArtifactView,
  ReviewProducerBindingView,
  RunFindingLinkPage,
  RunFindingLinkView,
  SpecView,
} from "./api";
import { FindingDetailPage } from "./FindingDetailPage";
import { ReviewDetailPage } from "./ReviewDetailPage";

type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];
type Finding = components["schemas"]["Finding"];

const REVIEW_ID = "artifact:review:detail";
const PREVIEW_ID = "artifact:preview:detail";
const CONSTRAINT_ID = "artifact:constraint:detail";
const RUN_ID = "run:review:detail";
const SNAPSHOT_ID = "snapshot:detail";
const CONSTRAINT_SNAPSHOT_ID = "constraint:detail";

function summary(
  artifactId: string,
  kind: ArtifactSummary["kind"],
  versionTuple: ArtifactSummary["version_tuple"],
  parents: string[] = [],
): ArtifactSummary {
  return {
    artifact_id: artifactId,
    created_at: "2026-07-20T04:00:00Z",
    domain_scope: { domain_ids: ["domain:narrative"] },
    kind,
    lineage_schema_version: "lineage@2",
    parent_artifact_ids: [...parents].sort(),
    payload_hash:
      kind === "review_report" ? "a".repeat(64) : kind === "ir_snapshot" ? "b".repeat(64) : "c".repeat(64),
    payload_schema_id:
      kind === "review_report" ? "review@1" : kind === "ir_snapshot" ? "ir-core@1" : "constraint-snapshot@1",
    summary_schema_version: "artifact-summary@1",
    version_tuple: versionTuple,
  };
}

function finding(id: string, oracleType: Finding["oracle_type"], status: Finding["status"]): Finding {
  return {
    confidence: oracleType === "llm-assisted" ? 0.72 : null,
    constraint_id: "constraint:quest",
    defect_class: `class:${id}`,
    entities: ["quest:bridge"],
    evidence: { observed: "blocked", expected: "reachable" },
    finding_schema_version: "finding@1",
    id,
    message: `${id} 的审查消息`,
    minimal_repro: {
      source_ref: { adapter: "aureus-csv", file: "quests.csv", row: 19 },
      steps: ["navigate", "blocked"],
    },
    oracle_type: oracleType,
    producer_id: `producer:${oracleType}`,
    producer_run_id: RUN_ID,
    relations: [],
    severity: "major",
    snapshot_id: SNAPSHOT_ID,
    source: oracleType === "llm-assisted" ? "llm" : oracleType === "simulation" ? "sim" : "checker",
    status,
  };
}

function exact(embedded: Finding, revision: number): FindingRevision {
  return {
    created_at: "2026-07-20T04:00:02Z",
    finding_id: embedded.id,
    payload: {
      confidence: embedded.confidence ?? null,
      constraint_id: embedded.constraint_id ?? null,
      defect_class: embedded.defect_class,
      entities: embedded.entities ?? [],
      evidence: embedded.evidence ?? {},
      message: embedded.message,
      minimal_repro: embedded.minimal_repro ?? {},
      oracle_type: embedded.oracle_type,
      payload_schema_version: "finding-payload@1",
      producer_id: embedded.producer_id,
      producer_run_id: embedded.producer_run_id,
      relations: embedded.relations ?? [],
      severity: embedded.severity,
      snapshot_id: embedded.snapshot_id,
      source: embedded.source,
      status: embedded.status,
    },
    revision,
    revision_schema_version: "finding-revision@1",
    supersedes_revision: revision === 1 ? null : revision - 1,
  };
}

const embeddedFindings = {
  deterministic: finding("finding:det", "deterministic", "confirmed"),
  simulation: finding("finding:sim", "simulation", "dismissed"),
  suggestion: finding("finding:llm", "llm-assisted", "unproven"),
  unproven: finding("finding:unknown", "deterministic", "unproven"),
};
const exactFindings = [
  exact(embeddedFindings.deterministic, 3),
  exact(embeddedFindings.simulation, 4),
  exact(embeddedFindings.suggestion, 8),
  exact(embeddedFindings.unproven, 2),
];

const exactFindingLinks: RunFindingLinkView[] = exactFindings.map((finding, index) => ({
  attempt_no: 1,
  evidence_artifact_id: `artifact:evidence:${index + 1}`,
  finding,
  finding_digest: (index + 1).toString(16).padStart(64, "0"),
  ordinal: index + 1,
  run_id: RUN_ID,
  view_schema_version: "run-finding-link-view@1",
}));

function reviewView(artifactId = REVIEW_ID): ReviewArtifactView {
  return {
    artifact: summary(
      artifactId,
      "review_report",
      {
        agent_graph_version: "review-graph@3",
        cassette_id: "artifact:cassette:detail",
        constraint_snapshot_id: CONSTRAINT_SNAPSHOT_ID,
        doc_version: "doc:aureus:detail",
        env_contract_version: null,
        ir_snapshot_id: SNAPSHOT_ID,
        model_snapshot: "openai/gpt-5.6-sol/m4@1",
        prompt_version: "review-triage@2",
        seed: 77,
        tool_version: "review@1",
      },
      [PREVIEW_ID, CONSTRAINT_ID].sort(),
    ),
    report: {
      by_defect_class: [
        { count: 1, defect_class: "class:finding:det", severity: "major" },
        { count: 1, defect_class: "class:finding:llm", severity: "major" },
        { count: 1, defect_class: "class:finding:sim", severity: "major" },
        { count: 1, defect_class: "class:finding:unknown", severity: "major" },
      ],
      created_at: "2026-07-20T04:00:01Z",
      deterministic_findings: [embeddedFindings.deterministic],
      llm_assisted_findings: [embeddedFindings.suggestion],
      review_schema_version: "review@1",
      simulation_findings: [embeddedFindings.simulation],
      snapshot_id: SNAPSHOT_ID,
      unproven_findings: [embeddedFindings.unproven],
    },
    view_schema_version: "review-artifact-view@1",
  };
}

const previewSummary = summary(PREVIEW_ID, "ir_snapshot", { ir_snapshot_id: SNAPSHOT_ID });
const constraintSummary = summary(CONSTRAINT_ID, "constraint_snapshot", {
  constraint_snapshot_id: CONSTRAINT_SNAPSHOT_ID,
});

function lineagePage(): LineagePage {
  return {
    expires_at: "2026-07-20T05:00:00Z",
    items: [
      { artifact: previewSummary, depth: 1, entry_schema_version: "lineage-entry@1" },
      { artifact: constraintSummary, depth: 1, entry_schema_version: "lineage-entry@1" },
    ],
    next_cursor: null,
    page_schema_version: "page@1",
    read_snapshot_id: "read:lineage",
  };
}

function findingLinksPage(items: RunFindingLinkView[]): RunFindingLinkPage {
  return {
    expires_at: "2026-07-20T05:00:00Z",
    items,
    next_cursor: null,
    page_schema_version: "page@1",
    read_snapshot_id: "read:run-findings",
  };
}

function producerBinding(
  findingAuthority: ReviewProducerBindingView["finding_authority"] = "exact-run-links",
): ReviewProducerBindingView {
  return {
    attempt_no: 1,
    finding_authority: findingAuthority,
    manifest_role: findingAuthority === "embedded-only" ? "evidence" : "output",
    outcome_code: findingAuthority === "embedded-only" ? "generation_gate_passed" : "review_completed",
    outcome_policy_id: findingAuthority === "embedded-only" ? "generation-gate-pass" : "review-completed",
    outcome_policy_version: 1,
    outcome_rule_id: findingAuthority === "embedded-only" ? "review" : "primary",
    review_artifact_id: REVIEW_ID,
    run_id: RUN_ID,
    run_kind:
      findingAuthority === "embedded-only"
        ? { kind: "generation.propose", version: 1 }
        : { kind: "review.run", version: 1 },
    terminal_manifest_id: "artifact:manifest:review-detail",
    terminal_manifest_kind: "run_result",
    terminal_status: "succeeded",
    view_schema_version: "review-producer-binding-view@1",
  };
}

const spec: SpecView = {
  artifact: previewSummary,
  ref_name: null,
  ref_value: null,
  schema_registry_version: "ir-core@1",
  snapshot_id: SNAPSHOT_ID,
  view_schema_version: "spec-view@1",
};
const constraint: ConstraintSnapshotView = {
  artifact: constraintSummary,
  constraints: [],
  dsl_grammar_version: "constraint-dsl@1",
  view_schema_version: "constraint-snapshot-view@1",
};

function api(overrides: Partial<ReviewApi> = {}): ReviewApi {
  return {
    getConstraint: vi.fn().mockResolvedValue(constraint),
    getFinding: vi.fn().mockResolvedValue(exactFindings[0]),
    getReview: vi.fn().mockResolvedValue(reviewView()),
    getReviewProducerBinding: vi.fn().mockResolvedValue(producerBinding()),
    getSpec: vi.fn().mockResolvedValue(spec),
    listLineage: vi.fn().mockResolvedValue(lineagePage()),
    listReviews: vi.fn(),
    listRunFindingLinks: vi.fn().mockResolvedValue(findingLinksPage(exactFindingLinks)),
    ...overrides,
  } as ReviewApi;
}

function renderWithQuery(ui: React.ReactNode) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Review detail", () => {
  it("renders exact preview/tool identity and four separate Finding partitions", async () => {
    renderWithQuery(
      <ReviewDetailPage
        api={api()}
        artifactId={REVIEW_ID}
        snapshotContextArtifactId={PREVIEW_ID}
        sourceRunId={RUN_ID}
      />,
    );

    expect(await screen.findByRole("heading", { level: 1, name: "Review Report" })).toBeVisible();
    expect(screen.getByRole("link", { name: "打开 exact preview" })).toHaveAttribute(
      "href",
      `/specs/${encodeURIComponent(PREVIEW_ID)}`,
    );
    expect(screen.getByRole("link", { name: "打开 exact constraint" })).toHaveAttribute(
      "href",
      `/constraints/${encodeURIComponent(CONSTRAINT_ID)}`,
    );
    const tuple = screen.getByRole("region", { name: "Frozen VersionTuple" });
    expect(within(tuple).getByText("review@1")).toBeVisible();
    expect(within(tuple).getByText("openai/gpt-5.6-sol/m4@1")).toBeVisible();
    expect(within(tuple).getByText("review-triage@2")).toBeVisible();
    expect(screen.getByText("4 条 Finding；0 不代表通过")).toBeVisible();
    expect(screen.getByRole("region", { name: "确定性预言机" })).toBeVisible();
    expect(screen.getByRole("region", { name: "仿真证据（描述性）" })).toBeVisible();
    expect(screen.getByRole("region", { name: "LLM 建议（需人确认）" })).toBeVisible();
    expect(screen.getByRole("region", { name: "未证明（不可视为通过）" })).toBeVisible();
    expect(screen.getAllByRole("link", { name: "查看 exact Finding 修订" })).toHaveLength(4);
    expect(screen.getByRole("link", { name: "打开 Review producer Run" })).toHaveAttribute(
      "href",
      `/runs/${encodeURIComponent(RUN_ID)}`,
    );
    expect(screen.getByText(/与服务端验证的 Review producer occurrence 一致/)).toBeVisible();
    expect(screen.getByText(/direct preview 与请求上下文一致/)).toBeVisible();
    expect(screen.getByText("attempt 1 · ordinal 1")).toBeVisible();
    expect(screen.getByRole("link", { name: "artifact:evidence:1" })).toHaveAttribute(
      "href",
      "/artifacts/artifact%3Aevidence%3A1",
    );
    const counts = screen.getByRole("list", { name: "Finding 分区计数" });
    expect(within(counts).getAllByText("1")).toHaveLength(4);
  });

  it("states when navigation sourceRun matches the verified producer occurrence", async () => {
    const getReviewProducerBinding = vi.fn().mockResolvedValue(producerBinding());
    renderWithQuery(
      <ReviewDetailPage
        api={api({ getReviewProducerBinding })}
        artifactId={REVIEW_ID}
        sourceRunId={RUN_ID}
      />,
    );

    expect(await screen.findByText(/与服务端验证的 Review producer occurrence 一致/)).toBeVisible();
    expect(getReviewProducerBinding).toHaveBeenCalledTimes(1);
    expect(getReviewProducerBinding).toHaveBeenCalledWith(REVIEW_ID, RUN_ID);
  });

  it("verifies a distinct sourceRun as an independent producer occurrence", async () => {
    const sourceRunId = "run:generation:detail";
    const sourceBinding = {
      ...producerBinding("embedded-only"),
      run_id: sourceRunId,
    };
    const getReviewProducerBinding = vi.fn((_artifactId: string, runId: string) =>
      Promise.resolve(runId === sourceRunId ? sourceBinding : producerBinding()),
    );
    renderWithQuery(
      <ReviewDetailPage
        api={api({ getReviewProducerBinding })}
        artifactId={REVIEW_ID}
        sourceRunId={sourceRunId}
      />,
    );

    expect(await screen.findByText(/另一条已验证的 Review producer occurrence/)).toBeVisible();
    expect(getReviewProducerBinding).toHaveBeenCalledTimes(2);
    expect(getReviewProducerBinding).toHaveBeenCalledWith(REVIEW_ID, RUN_ID);
    expect(getReviewProducerBinding).toHaveBeenCalledWith(REVIEW_ID, sourceRunId);
    expect(screen.getByText(/generation-gate-pass@1/)).toBeVisible();
  });

  it("uses an explicit sourceRun to close an empty Review occurrence", async () => {
    const empty = reviewView();
    empty.report.by_defect_class = [];
    empty.report.deterministic_findings = [];
    empty.report.simulation_findings = [];
    empty.report.llm_assisted_findings = [];
    empty.report.unproven_findings = [];
    const emptyBinding = {
      ...producerBinding(),
      finding_authority: "not-applicable" as const,
    };
    const getReviewProducerBinding = vi.fn().mockResolvedValue(emptyBinding);
    renderWithQuery(
      <ReviewDetailPage
        api={api({ getReview: vi.fn().mockResolvedValue(empty), getReviewProducerBinding })}
        artifactId={REVIEW_ID}
        sourceRunId={RUN_ID}
      />,
    );

    expect(await screen.findByText("0 条 Finding；0 不代表通过")).toBeVisible();
    expect(getReviewProducerBinding).toHaveBeenCalledWith(REVIEW_ID, RUN_ID);
    expect(screen.getByText("run_result · succeeded")).toBeVisible();
    expect(screen.getByText(/review-completed@1/)).toBeVisible();
  });

  it("keeps a sourceRun that is not an occurrence as navigation context only", async () => {
    const sourceRunId = "run:generation:not-an-occurrence";
    const getReviewProducerBinding = vi.fn((_artifactId: string, runId: string) => {
      if (runId === sourceRunId) {
        return Promise.reject(
          new ApiProblemError({
            code: "not_found",
            conflict_set_id: null,
            detail: "The requested producer occurrence was not found.",
            earliest_cursor: null,
            instance: "/api/v1/reviews/review/producer-binding",
            request_id: "request:source-occurrence",
            retry_after_s: null,
            run_id: null,
            status: 404,
            title: "Not Found",
            trace_id: null,
            type: "about:blank",
          }),
        );
      }
      return Promise.resolve(producerBinding());
    });
    renderWithQuery(
      <ReviewDetailPage
        api={api({ getReviewProducerBinding })}
        artifactId={REVIEW_ID}
        sourceRunId={sourceRunId}
      />,
    );

    expect(await screen.findByText(/未验证为该 Review 的 producer occurrence/)).toBeVisible();
    expect(screen.getByRole("heading", { level: 1, name: "Review Report" })).toBeVisible();
  });

  it("binds sourceRun into the detail query identity", async () => {
    const secondSourceRunId = "run:generation:second-occurrence";
    const getReviewProducerBinding = vi.fn((_artifactId: string, runId: string) =>
      Promise.resolve(
        runId === secondSourceRunId
          ? { ...producerBinding("embedded-only"), run_id: secondSourceRunId }
          : producerBinding(),
      ),
    );
    const queryClient = createQueryClient();
    const page = (sourceRunId: string) => (
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ReviewDetailPage
            api={api({ getReviewProducerBinding })}
            artifactId={REVIEW_ID}
            sourceRunId={sourceRunId}
          />
        </MemoryRouter>
      </QueryClientProvider>
    );
    const rendered = render(page(RUN_ID));
    expect(await screen.findByText(/与服务端验证的 Review producer occurrence 一致/)).toBeVisible();

    rendered.rerender(page(secondSourceRunId));

    await waitFor(() => expect(getReviewProducerBinding).toHaveBeenCalledWith(REVIEW_ID, secondSourceRunId));
    expect(await screen.findByText(/另一条已验证的 Review producer occurrence/)).toBeVisible();
  });

  it("renders embedded-only generation evidence without fabricating revisions or latest links", async () => {
    renderWithQuery(
      <ReviewDetailPage
        api={api({
          getReviewProducerBinding: vi.fn().mockResolvedValue(producerBinding("embedded-only")),
          listRunFindingLinks: vi.fn(),
        })}
        artifactId={REVIEW_ID}
      />,
    );

    expect(await screen.findByText(/仅报告内嵌 Finding/)).toBeVisible();
    expect(screen.getAllByText("无 immutable revision；未回退 latest")).toHaveLength(4);
    expect(screen.queryByRole("link", { name: "查看 exact Finding 修订" })).not.toBeInTheDocument();
    expect(screen.getAllByText(/"observed": "blocked"/)).toHaveLength(4);
  });

  it("shows a failed generation producer occurrence without recasting Finding states", async () => {
    const failed = {
      ...producerBinding("embedded-only"),
      outcome_code: "generation_gate_rejected",
      outcome_policy_id: "generation-gate-rejected",
      terminal_manifest_id: "artifact:failure:generation-gate",
      terminal_manifest_kind: "run_failure" as const,
      terminal_status: "failed" as const,
    };
    renderWithQuery(
      <ReviewDetailPage
        api={api({
          getReviewProducerBinding: vi.fn().mockResolvedValue(failed),
          listRunFindingLinks: vi.fn(),
        })}
        artifactId={REVIEW_ID}
      />,
    );

    expect(await screen.findByText("run_failure · failed")).toBeVisible();
    expect(screen.getByText("已确认 · confirmed")).toBeVisible();
    expect(screen.getAllByText("未证明 · unproven")).toHaveLength(2);
  });

  it("shows explicit empty copy for every zero-Finding partition", async () => {
    const empty = reviewView();
    empty.report.by_defect_class = [];
    empty.report.deterministic_findings = [];
    empty.report.simulation_findings = [];
    empty.report.llm_assisted_findings = [];
    empty.report.unproven_findings = [];
    const getReviewProducerBinding = vi.fn();
    const listRunFindingLinks = vi.fn();
    renderWithQuery(
      <ReviewDetailPage
        api={api({
          getReview: vi.fn().mockResolvedValue(empty),
          getReviewProducerBinding,
          listRunFindingLinks,
        })}
        artifactId={REVIEW_ID}
      />,
    );

    expect(await screen.findByText("0 条 Finding；0 不代表通过")).toBeVisible();
    expect(screen.getAllByText("暂无此类证据")).toHaveLength(3);
    expect(screen.getByText("暂无未证明结果")).toBeVisible();
    expect(getReviewProducerBinding).not.toHaveBeenCalled();
    expect(listRunFindingLinks).not.toHaveBeenCalled();
  });

  it("fails closed when the requested Review identity differs from the response", async () => {
    const reviewApi = api({ getReview: vi.fn().mockResolvedValue(reviewView("artifact:review:wrong")) });
    renderWithQuery(<ReviewDetailPage api={reviewApi} artifactId={REVIEW_ID} />);

    expect(await screen.findByRole("heading", { name: "Review 权威闭合失败" })).toBeVisible();
    expect(screen.queryByText("finding:det 的审查消息")).not.toBeInTheDocument();
    expect(reviewApi.listLineage).not.toHaveBeenCalled();
    expect(reviewApi.listRunFindingLinks).not.toHaveBeenCalled();
    expect(reviewApi.getSpec).not.toHaveBeenCalled();
    expect(reviewApi.getConstraint).not.toHaveBeenCalled();
  });

  it.each([
    [
      "mispartitioned Finding",
      (view: ReviewArtifactView) => {
        view.report.deterministic_findings = [embeddedFindings.simulation];
        view.report.simulation_findings = [embeddedFindings.deterministic];
      },
    ],
    [
      "inexact defect-class aggregate",
      (view: ReviewArtifactView) => {
        view.report.by_defect_class![0].count = 2;
      },
    ],
  ])("rejects a %s before requesting downstream authority", async (_label, mutate) => {
    const invalid = reviewView();
    mutate(invalid);
    const reviewApi = api({ getReview: vi.fn().mockResolvedValue(invalid) });
    renderWithQuery(<ReviewDetailPage api={reviewApi} artifactId={REVIEW_ID} />);

    expect(await screen.findByRole("heading", { name: "Review 权威闭合失败" })).toBeVisible();
    expect(reviewApi.getReviewProducerBinding).not.toHaveBeenCalled();
    expect(reviewApi.listLineage).not.toHaveBeenCalled();
    expect(reviewApi.listRunFindingLinks).not.toHaveBeenCalled();
    expect(reviewApi.getSpec).not.toHaveBeenCalled();
    expect(reviewApi.getConstraint).not.toHaveBeenCalled();
  });

  it("fails closed on a repeated detail cursor instead of looping", async () => {
    const first = { ...lineagePage(), next_cursor: "opaque:cycle" };
    const repeated = {
      ...lineagePage(),
      items: [],
      next_cursor: "opaque:cycle",
    };
    const listLineage = vi.fn().mockResolvedValueOnce(first).mockResolvedValueOnce(repeated);
    renderWithQuery(<ReviewDetailPage api={api({ listLineage })} artifactId={REVIEW_ID} />);

    expect(await screen.findByRole("heading", { name: "Review 权威闭合失败" })).toBeVisible();
    expect(screen.getByText(/cursor cycle/)).toBeVisible();
    expect(listLineage).toHaveBeenCalledTimes(2);
  });

  it("requires a full detail restart when a continuation cursor expires", async () => {
    const first = { ...lineagePage(), next_cursor: "opaque:expired" };
    const listLineage = vi
      .fn()
      .mockResolvedValueOnce(first)
      .mockRejectedValueOnce(
        new CursorExpiredError(
          {
            code: "cursor_expired",
            conflict_set_id: null,
            detail: "Review lineage cursor expired.",
            earliest_cursor: null,
            instance: "/api/v1/artifacts/review/lineage",
            request_id: "request:review-detail-expired",
            retry_after_s: null,
            run_id: null,
            status: 410,
            title: "Cursor expired",
            trace_id: null,
            type: "about:blank",
          },
          "opaque:expired",
        ),
      );
    renderWithQuery(<ReviewDetailPage api={api({ listLineage })} artifactId={REVIEW_ID} />);

    expect(await screen.findByRole("heading", { name: "Review 详情快照已过期" })).toBeVisible();
    expect(screen.getByRole("button", { name: "从第一页重新读取全部权威" })).toBeVisible();
  });
});

describe("Finding exact revision detail", () => {
  it("loads only the requested immutable revision and preserves repro, source_ref, and evidence", async () => {
    const getFinding = vi.fn().mockResolvedValue(exactFindings[0]);
    renderWithQuery(<FindingDetailPage api={api({ getFinding })} findingId="finding:det" revision={3} />);

    expect(
      await screen.findByRole("heading", { level: 1, name: "Finding immutable revision" }),
    ).toBeVisible();
    expect(getFinding).toHaveBeenCalledWith("finding:det", 3);
    expect(screen.getByText("不可变修订 3")).toBeVisible();
    expect(screen.getByText("aureus-csv · quests.csv / 第 19 行")).toBeVisible();
    expect(screen.getByText(/"observed": "blocked"/)).toBeVisible();
    expect(screen.getByText(/"steps": \[/)).toBeVisible();
    expect(screen.getByRole("link", { name: "打开 producer Run" })).toHaveAttribute(
      "href",
      `/runs/${encodeURIComponent(RUN_ID)}`,
    );
  });

  it("fails closed when the exact endpoint returns another revision", async () => {
    renderWithQuery(
      <FindingDetailPage
        api={api({ getFinding: vi.fn().mockResolvedValue(exactFindings[1]) })}
        findingId="finding:det"
        revision={3}
      />,
    );

    expect(await screen.findByRole("heading", { name: "Finding 权威闭合失败" })).toBeVisible();
  });
});
