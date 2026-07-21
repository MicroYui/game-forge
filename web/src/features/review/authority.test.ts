import { describe, expect, it } from "vitest";

import type { components } from "../../api/generated/openapi";
import {
  bindReviewAuthority,
  findingBuckets,
  requireExactFindingRoute,
  ReviewAuthorityError,
} from "./authority";

type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];
type Finding = components["schemas"]["Finding"];
type FindingRevision = components["schemas"]["FindingRevisionV1"];
type LineageEntry = components["schemas"]["LineageEntryV1"];
type ReviewArtifactView = components["schemas"]["ReviewArtifactViewV1"];
type ReviewProducerBinding = components["schemas"]["ReviewProducerBindingViewV1"];
type RunFindingLinkView = components["schemas"]["RunFindingLinkViewV1"];
type SpecView = components["schemas"]["SpecViewV1"];
type ConstraintSnapshotView = components["schemas"]["ConstraintSnapshotViewV1"];

const REVIEW_ID = "artifact:review:7";
const PREVIEW_ID = "artifact:preview:7";
const CONSTRAINT_ID = "artifact:constraint:7";
const RUN_ID = "run:review:7";
const SNAPSHOT_ID = "snapshot:preview:7";
const CONSTRAINT_SNAPSHOT_ID = "constraint:live:7";

function summary(
  artifactId: string,
  kind: ArtifactSummary["kind"],
  versionTuple: ArtifactSummary["version_tuple"],
  parentArtifactIds: string[] = [],
): ArtifactSummary {
  return {
    artifact_id: artifactId,
    created_at: "2026-07-20T01:00:00Z",
    domain_scope: { domain_ids: ["domain:narrative"] },
    kind,
    lineage_schema_version: "lineage@2",
    parent_artifact_ids: parentArtifactIds,
    payload_hash: "a".repeat(64),
    payload_schema_id:
      kind === "review_report" ? "review@1" : kind === "ir_snapshot" ? "ir-core@1" : "constraint-snapshot@1",
    summary_schema_version: "artifact-summary@1",
    version_tuple: versionTuple,
  };
}

function legacyFinding(id: string, oracleType: Finding["oracle_type"], status: Finding["status"]): Finding {
  return {
    confidence: oracleType === "llm-assisted" ? 0.61 : null,
    constraint_id: "constraint:quest-path",
    created_at: null,
    defect_class: `class:${id}`,
    entities: ["quest:bridge"],
    evidence: { counterexample: ["start", "blocked"] },
    finding_schema_version: "finding@1",
    id,
    message: `${id} message`,
    minimal_repro: {
      source_ref: { adapter: "aureus-csv", file: "quests.csv", row: 7 },
      steps: ["start", "blocked"],
    },
    oracle_type: oracleType,
    producer_id: `producer:${oracleType}`,
    producer_run_id: RUN_ID,
    relations: ["relation:blocks"],
    severity: status === "confirmed" ? "critical" : "minor",
    snapshot_id: SNAPSHOT_ID,
    source: oracleType === "llm-assisted" ? "llm" : oracleType === "simulation" ? "sim" : "checker",
    status,
  };
}

function revision(finding: Finding, revisionNumber: number): FindingRevision {
  return {
    created_at: "2026-07-20T01:00:01Z",
    finding_id: finding.id,
    payload: {
      confidence: finding.confidence ?? null,
      constraint_id: finding.constraint_id ?? null,
      defect_class: finding.defect_class,
      entities: finding.entities ?? [],
      evidence: finding.evidence ?? {},
      message: finding.message,
      minimal_repro: finding.minimal_repro ?? {},
      oracle_type: finding.oracle_type,
      payload_schema_version: "finding-payload@1",
      producer_id: finding.producer_id,
      producer_run_id: finding.producer_run_id,
      relations: finding.relations ?? [],
      severity: finding.severity,
      snapshot_id: finding.snapshot_id,
      source: finding.source,
      status: finding.status,
    },
    revision: revisionNumber,
    revision_schema_version: "finding-revision@1",
    supersedes_revision: revisionNumber === 1 ? null : revisionNumber - 1,
  };
}

function findingLink(finding: Finding, revisionNumber: number, ordinal: number): RunFindingLinkView {
  return {
    attempt_no: 1,
    evidence_artifact_id: `artifact:evidence:${ordinal}`,
    finding: revision(finding, revisionNumber),
    finding_digest: ordinal.toString(16).padStart(64, "0"),
    ordinal,
    run_id: RUN_ID,
    view_schema_version: "run-finding-link-view@1",
  };
}

function producerBinding(
  findingAuthority: ReviewProducerBinding["finding_authority"] = "exact-run-links",
): ReviewProducerBinding {
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
    terminal_manifest_id: "artifact:manifest:review:7",
    terminal_manifest_kind: "run_result",
    terminal_status: "succeeded",
    view_schema_version: "review-producer-binding-view@1",
  };
}

const deterministic = legacyFinding("finding:deterministic", "deterministic", "confirmed");
const simulation = legacyFinding("finding:simulation", "simulation", "dismissed");
const suggestion = legacyFinding("finding:suggestion", "llm-assisted", "unproven");
const unproven = legacyFinding("finding:unproven", "deterministic", "unproven");

function reviewView(): ReviewArtifactView {
  return {
    artifact: summary(
      REVIEW_ID,
      "review_report",
      {
        agent_graph_version: "review-graph@3",
        cassette_id: "cassette:review:7",
        constraint_snapshot_id: CONSTRAINT_SNAPSHOT_ID,
        doc_version: "doc:aureus:7",
        env_contract_version: null,
        ir_snapshot_id: SNAPSHOT_ID,
        model_snapshot: "openai/gpt-5.6-sol/m4@1",
        prompt_version: "review-triage@2",
        seed: 41,
        tool_version: "review@1",
      },
      [CONSTRAINT_ID, PREVIEW_ID, "artifact:rendered:7"].sort(),
    ),
    report: {
      by_defect_class: [
        { count: 1, defect_class: deterministic.defect_class, severity: deterministic.severity },
        { count: 1, defect_class: simulation.defect_class, severity: simulation.severity },
        { count: 1, defect_class: suggestion.defect_class, severity: suggestion.severity },
        { count: 1, defect_class: unproven.defect_class, severity: unproven.severity },
      ],
      created_at: "2026-07-20T01:00:00Z",
      deterministic_findings: [deterministic],
      llm_assisted_findings: [suggestion],
      review_schema_version: "review@1",
      simulation_findings: [simulation],
      snapshot_id: SNAPSHOT_ID,
      unproven_findings: [unproven],
    },
    view_schema_version: "review-artifact-view@1",
  };
}

function lineage(): LineageEntry[] {
  return [
    {
      artifact: summary(PREVIEW_ID, "ir_snapshot", { ir_snapshot_id: SNAPSHOT_ID }),
      depth: 1,
      entry_schema_version: "lineage-entry@1",
    },
    {
      artifact: summary(CONSTRAINT_ID, "constraint_snapshot", {
        constraint_snapshot_id: CONSTRAINT_SNAPSHOT_ID,
      }),
      depth: 1,
      entry_schema_version: "lineage-entry@1",
    },
    {
      artifact: summary("artifact:rendered:7", "source_rendered", {
        ir_snapshot_id: SNAPSHOT_ID,
      }),
      depth: 1,
      entry_schema_version: "lineage-entry@1",
    },
  ];
}

function previewAuthority(): SpecView {
  return {
    artifact: summary(PREVIEW_ID, "ir_snapshot", { ir_snapshot_id: SNAPSHOT_ID }),
    ref_name: null,
    ref_value: null,
    schema_registry_version: "ir-core@1",
    snapshot_id: SNAPSHOT_ID,
    view_schema_version: "spec-view@1",
  };
}

function constraintAuthority(): ConstraintSnapshotView {
  return {
    artifact: summary(CONSTRAINT_ID, "constraint_snapshot", {
      constraint_snapshot_id: CONSTRAINT_SNAPSHOT_ID,
    }),
    constraints: [],
    dsl_grammar_version: "constraint-dsl@1",
    view_schema_version: "constraint-snapshot-view@1",
  };
}

const exactFindingLinks = [
  findingLink(deterministic, 5, 1),
  findingLink(simulation, 2, 2),
  findingLink(suggestion, 9, 3),
  findingLink(unproven, 1, 4),
];

interface AuthorityMismatchScenario {
  mutate?(view: ReviewArtifactView): void;
  mutateFindingLinks?(items: RunFindingLinkView[]): void;
  mutateLineage?(items: LineageEntry[]): void;
  requestedArtifactId?: string;
}

describe("Review authority", () => {
  it("binds one exact preview, constraint, immutable Finding revisions, and four honest buckets", () => {
    const bound = bindReviewAuthority({
      exactFindingLinks,
      lineage: lineage(),
      constraintAuthority: constraintAuthority(),
      previewAuthority: previewAuthority(),
      producerBinding: producerBinding(),
      requestedArtifactId: REVIEW_ID,
      review: reviewView(),
      snapshotContextArtifactId: PREVIEW_ID,
    });

    expect(bound.preview.artifact_id).toBe(PREVIEW_ID);
    expect(bound.constraint?.artifact_id).toBe(CONSTRAINT_ID);
    expect(bound.producerRunId).toBe(RUN_ID);
    expect(bound.snapshotContextMatches).toBe(true);
    expect(bound.findingAuthority).toBe("exact-run-links");
    expect(bound.buckets.deterministic.map((item) => item.exact?.finding.revision)).toEqual([5]);
    expect(bound.buckets.simulation.map((item) => item.exact?.finding.revision)).toEqual([2]);
    expect(bound.buckets.suggestion.map((item) => item.exact?.finding.revision)).toEqual([9]);
    expect(bound.buckets.unproven.map((item) => item.exact?.finding.revision)).toEqual([1]);
    expect(findingBuckets(reviewView().report)).toMatchObject({
      deterministic: [deterministic],
      simulation: [simulation],
      suggestion: [suggestion],
      unproven: [unproven],
    });
  });

  it("normalizes only the contract-optional source_ref column omitted/null projection", () => {
    const view = structuredClone(reviewView());
    const findingLinks = structuredClone(exactFindingLinks);
    const exactMinimalRepro = findingLinks[0].finding.payload.minimal_repro as Record<string, unknown>;
    exactMinimalRepro.source_ref = {
      ...(exactMinimalRepro.source_ref as Record<string, unknown>),
      column: null,
    };

    const bound = bindReviewAuthority({
      exactFindingLinks: findingLinks,
      lineage: lineage(),
      constraintAuthority: constraintAuthority(),
      previewAuthority: previewAuthority(),
      producerBinding: producerBinding(),
      requestedArtifactId: REVIEW_ID,
      review: view,
    });

    expect(bound.buckets.deterministic[0].exact?.finding.revision).toBe(5);

    (exactMinimalRepro.source_ref as Record<string, unknown>).column = "different-column";
    expect(() =>
      bindReviewAuthority({
        exactFindingLinks: findingLinks,
        lineage: lineage(),
        constraintAuthority: constraintAuthority(),
        previewAuthority: previewAuthority(),
        producerBinding: producerBinding(),
        requestedArtifactId: REVIEW_ID,
        review: view,
      }),
    ).toThrow(ReviewAuthorityError);
  });

  it("accepts the reverse source_ref column null/omitted projection", () => {
    const view = structuredClone(reviewView());
    const findingLinks = structuredClone(exactFindingLinks);
    const embeddedMinimalRepro = view.report.deterministic_findings![0].minimal_repro as Record<
      string,
      unknown
    >;
    embeddedMinimalRepro.source_ref = {
      ...(embeddedMinimalRepro.source_ref as Record<string, unknown>),
      column: null,
    };

    const bound = bindReviewAuthority({
      exactFindingLinks: findingLinks,
      lineage: lineage(),
      constraintAuthority: constraintAuthority(),
      previewAuthority: previewAuthority(),
      producerBinding: producerBinding(),
      requestedArtifactId: REVIEW_ID,
      review: view,
    });

    expect(bound.buckets.deterministic[0].exact?.finding.revision).toBe(5);
  });

  it.each([
    ["missing required adapter", { file: "quests.csv", row: 7 }],
    ["invalid row type", { adapter: "aureus-csv", file: "quests.csv", row: "7" }],
    ["extra field", { adapter: "aureus-csv", file: "quests.csv", row: 7, unsupported: "value" }],
  ])("does not normalize a malformed source_ref with %s", (_label, malformedSourceRef) => {
    const view = structuredClone(reviewView());
    const findingLinks = structuredClone(exactFindingLinks);
    const embeddedMinimalRepro = view.report.deterministic_findings![0].minimal_repro as Record<
      string,
      unknown
    >;
    const exactMinimalRepro = findingLinks[0].finding.payload.minimal_repro as Record<string, unknown>;
    embeddedMinimalRepro.source_ref = malformedSourceRef;
    exactMinimalRepro.source_ref = { ...malformedSourceRef, column: null };

    expect(() =>
      bindReviewAuthority({
        exactFindingLinks: findingLinks,
        lineage: lineage(),
        constraintAuthority: constraintAuthority(),
        previewAuthority: previewAuthority(),
        producerBinding: producerBinding(),
        requestedArtifactId: REVIEW_ID,
        review: view,
      }),
    ).toThrow(ReviewAuthorityError);
  });

  it.each(["sheet", "row", "unrelated"])(
    "keeps source_ref or minimal_repro %s missing distinct from null",
    (field) => {
      const view = structuredClone(reviewView());
      const findingLinks = structuredClone(exactFindingLinks);
      const exactMinimalRepro = findingLinks[0].finding.payload.minimal_repro as Record<string, unknown>;
      if (field === "unrelated") {
        exactMinimalRepro[field] = null;
      } else {
        const embeddedMinimalRepro = view.report.deterministic_findings![0].minimal_repro as Record<
          string,
          unknown
        >;
        delete (embeddedMinimalRepro.source_ref as Record<string, unknown>)[field];
        (exactMinimalRepro.source_ref as Record<string, unknown>)[field] = null;
      }

      expect(() =>
        bindReviewAuthority({
          exactFindingLinks: findingLinks,
          lineage: lineage(),
          constraintAuthority: constraintAuthority(),
          previewAuthority: previewAuthority(),
          producerBinding: producerBinding(),
          requestedArtifactId: REVIEW_ID,
          review: view,
        }),
      ).toThrow(ReviewAuthorityError);
    },
  );

  it("never exposes raw malformed buckets through the exported list helper", () => {
    const view = reviewView();
    view.report.deterministic_findings = [simulation];
    view.report.simulation_findings = [deterministic];

    expect(() => findingBuckets(view.report)).toThrow(ReviewAuthorityError);
  });

  it("does not need a fabricated producer Run when an exact report has no Findings", () => {
    const empty = reviewView();
    empty.report.by_defect_class = [];
    empty.report.deterministic_findings = [];
    empty.report.simulation_findings = [];
    empty.report.llm_assisted_findings = [];
    empty.report.unproven_findings = [];

    const bound = bindReviewAuthority({
      exactFindingLinks: [],
      lineage: lineage(),
      constraintAuthority: constraintAuthority(),
      previewAuthority: previewAuthority(),
      producerBinding: null,
      requestedArtifactId: REVIEW_ID,
      review: empty,
    });

    expect(bound.producerRunId).toBeNull();
    expect(bound.findingAuthority).toBe("not-applicable");
    expect(bound.buckets).toEqual({
      deterministic: [],
      simulation: [],
      suggestion: [],
      unproven: [],
    });
  });

  it("keeps a generation-gate Review honest when no exact Finding revisions were published", () => {
    const bound = bindReviewAuthority({
      exactFindingLinks: [],
      lineage: lineage(),
      constraintAuthority: constraintAuthority(),
      previewAuthority: previewAuthority(),
      producerBinding: producerBinding("embedded-only"),
      requestedArtifactId: REVIEW_ID,
      review: reviewView(),
    });

    expect(bound.findingAuthority).toBe("embedded-only");
    expect(bound.producerRunId).toBe(RUN_ID);
    expect(bound.buckets.deterministic[0]).toEqual({
      embedded: deterministic,
      exact: null,
    });
  });

  it.each([
    [
      "deterministic bucket",
      (view: ReviewArtifactView) => {
        view.report.deterministic_findings = [simulation];
        view.report.simulation_findings = [deterministic];
      },
    ],
    [
      "simulation bucket",
      (view: ReviewArtifactView) => {
        view.report.simulation_findings = [unproven];
        view.report.unproven_findings = [simulation];
      },
    ],
    [
      "LLM suggestion bucket",
      (view: ReviewArtifactView) => {
        view.report.llm_assisted_findings = [unproven];
        view.report.unproven_findings = [suggestion];
      },
    ],
    [
      "unproven bucket",
      (view: ReviewArtifactView) => {
        view.report.deterministic_findings = [unproven];
        view.report.unproven_findings = [deterministic];
      },
    ],
  ])("fails closed when the %s violates ReviewReport.partition", (_label, mutate) => {
    const view = reviewView();
    mutate(view);

    expect(() =>
      bindReviewAuthority({
        exactFindingLinks,
        lineage: lineage(),
        constraintAuthority: constraintAuthority(),
        previewAuthority: previewAuthority(),
        producerBinding: producerBinding(),
        requestedArtifactId: REVIEW_ID,
        review: view,
      }),
    ).toThrow(ReviewAuthorityError);
  });

  it.each([
    [
      "wrong count",
      (view: ReviewArtifactView) => {
        view.report.by_defect_class![0].count = 2;
      },
    ],
    [
      "missing key",
      (view: ReviewArtifactView) => {
        view.report.by_defect_class!.pop();
      },
    ],
    [
      "extra key",
      (view: ReviewArtifactView) => {
        view.report.by_defect_class!.push({
          count: 1,
          defect_class: "class:not-present",
          severity: "major",
        });
      },
    ],
    [
      "duplicate key",
      (view: ReviewArtifactView) => {
        view.report.by_defect_class!.push({ ...view.report.by_defect_class![0] });
      },
    ],
  ])("fails closed when by_defect_class has a %s", (_label, mutate) => {
    const view = reviewView();
    mutate(view);

    expect(() =>
      bindReviewAuthority({
        exactFindingLinks,
        lineage: lineage(),
        constraintAuthority: constraintAuthority(),
        previewAuthority: previewAuthority(),
        producerBinding: producerBinding(),
        requestedArtifactId: REVIEW_ID,
        review: view,
      }),
    ).toThrow(ReviewAuthorityError);
  });

  it.each<[string, AuthorityMismatchScenario]>([
    ["route identity", { requestedArtifactId: "artifact:review:other" }],
    [
      "report snapshot",
      {
        mutate: (view: ReviewArtifactView) => {
          view.report.snapshot_id = "snapshot:other";
        },
      },
    ],
    [
      "review schema",
      {
        mutate: (view: ReviewArtifactView) => {
          view.report.review_schema_version = "review@future";
        },
      },
    ],
    [
      "missing frozen tool identity",
      {
        mutate: (view: ReviewArtifactView) => {
          view.artifact.version_tuple.tool_version = null;
        },
      },
    ],
    [
      "missing LLM model identity",
      {
        mutate: (view: ReviewArtifactView) => {
          view.artifact.version_tuple.model_snapshot = null;
        },
      },
    ],
    [
      "preview lineage",
      {
        mutateLineage: (items: LineageEntry[]) => {
          items[0].artifact.version_tuple.ir_snapshot_id = "snapshot:other";
        },
      },
    ],
    [
      "Finding semantic payload",
      {
        mutateFindingLinks: (items: RunFindingLinkView[]) => {
          items[0].finding.payload.message = "drifted message";
        },
      },
    ],
    [
      "embedded Finding snapshot",
      {
        mutate: (view: ReviewArtifactView) => {
          view.report.deterministic_findings![0].snapshot_id = "snapshot:other";
        },
      },
    ],
    [
      "partial exact Finding revision closure",
      {
        mutateFindingLinks: (items: RunFindingLinkView[]) => {
          items.pop();
        },
      },
    ],
    [
      "Run Finding link snapshot",
      {
        mutateFindingLinks: (items: RunFindingLinkView[]) => {
          items[0].finding.payload.snapshot_id = "snapshot:other";
        },
      },
    ],
  ])("fails closed on %s mismatch", (_label, scenario) => {
    const view = structuredClone(reviewView());
    const lineageItems = structuredClone(lineage());
    const findingLinks = structuredClone(exactFindingLinks);
    scenario.mutate?.(view);
    scenario.mutateLineage?.(lineageItems);
    scenario.mutateFindingLinks?.(findingLinks);

    expect(() =>
      bindReviewAuthority({
        exactFindingLinks: findingLinks,
        lineage: lineageItems,
        constraintAuthority: constraintAuthority(),
        previewAuthority: previewAuthority(),
        producerBinding: producerBinding(),
        requestedArtifactId: scenario.requestedArtifactId ?? REVIEW_ID,
        review: view,
      }),
    ).toThrow(ReviewAuthorityError);
  });

  it("never accepts the Generation sourceRun as the Review producer Run", () => {
    const view = reviewView();
    view.report.llm_assisted_findings![0].producer_run_id = "run:generation:7";

    expect(() =>
      bindReviewAuthority({
        exactFindingLinks,
        lineage: lineage(),
        constraintAuthority: constraintAuthority(),
        previewAuthority: previewAuthority(),
        producerBinding: producerBinding(),
        requestedArtifactId: REVIEW_ID,
        review: view,
      }),
    ).toThrow(/single producer Run/);
  });

  it("requires an exact Finding route and never falls back to latest", () => {
    const exact = exactFindingLinks[0].finding;
    expect(requireExactFindingRoute(exact, exact.finding_id, exact.revision)).toBe(exact);
    expect(() => requireExactFindingRoute(exact, exact.finding_id, 6)).toThrow(ReviewAuthorityError);
    expect(() => requireExactFindingRoute(exact, "finding:other", exact.revision)).toThrow(
      ReviewAuthorityError,
    );
  });
});
