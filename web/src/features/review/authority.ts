import type { components } from "../../api/generated/openapi";

type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];
type ConstraintSnapshotView = components["schemas"]["ConstraintSnapshotViewV1"];
type Finding = components["schemas"]["Finding"];
type FindingRevision = components["schemas"]["FindingRevisionV1"];
type LineageEntry = components["schemas"]["LineageEntryV1"];
type ReviewArtifactView = components["schemas"]["ReviewArtifactViewV1"];
type ReviewProducerBinding = components["schemas"]["ReviewProducerBindingViewV1"];
type ReviewReport = components["schemas"]["ReviewReport"];
type RunFindingLinkView = components["schemas"]["RunFindingLinkViewV1"];
type SpecView = components["schemas"]["SpecViewV1"];

export class ReviewAuthorityError extends Error {
  override name = "ReviewAuthorityError";
}

export interface LegacyFindingBuckets {
  deterministic: Finding[];
  simulation: Finding[];
  suggestion: Finding[];
  unproven: Finding[];
}

export interface ReviewFindingBinding {
  embedded: Finding;
  exact: RunFindingLinkView | null;
}

export interface ReviewFindingBuckets {
  deterministic: ReviewFindingBinding[];
  simulation: ReviewFindingBinding[];
  suggestion: ReviewFindingBinding[];
  unproven: ReviewFindingBinding[];
}

export interface ReviewLineageAuthority {
  preview: ArtifactSummary;
  constraint: ArtifactSummary | null;
  snapshotContextMatches: boolean | null;
}

export interface BoundReviewAuthority extends ReviewLineageAuthority {
  review: ReviewArtifactView;
  producerBinding: ReviewProducerBinding | null;
  producerRunId: string | null;
  findingAuthority: "exact-run-links" | "embedded-only" | "not-applicable";
  buckets: ReviewFindingBuckets;
  sourceProducerBinding: ReviewProducerBinding | null;
  sourceRunId: string | null;
  sourceRunOccurrence: "verified" | "not-found" | null;
}

function fail(message: string): never {
  throw new ReviewAuthorityError(message);
}

function rawFindingBuckets(report: ReviewReport): LegacyFindingBuckets {
  return {
    deterministic: [...(report.deterministic_findings ?? [])],
    simulation: [...(report.simulation_findings ?? [])],
    suggestion: [...(report.llm_assisted_findings ?? [])],
    unproven: [...(report.unproven_findings ?? [])],
  };
}

function allFindings(buckets: LegacyFindingBuckets): Finding[] {
  return [...buckets.deterministic, ...buckets.simulation, ...buckets.suggestion, ...buckets.unproven];
}

function expectedBucket(finding: Finding): keyof LegacyFindingBuckets {
  if (finding.oracle_type === "llm-assisted") return "suggestion";
  if (finding.status === "unproven") return "unproven";
  if (finding.oracle_type === "simulation") return "simulation";
  if (finding.oracle_type === "deterministic") return "deterministic";
  fail("Review Finding has an unsupported oracle type.");
}

function requireReviewPartition(report: ReviewReport): LegacyFindingBuckets {
  const buckets = rawFindingBuckets(report);
  const entries: [keyof LegacyFindingBuckets, Finding[]][] = [
    ["deterministic", buckets.deterministic],
    ["simulation", buckets.simulation],
    ["suggestion", buckets.suggestion],
    ["unproven", buckets.unproven],
  ];
  for (const [bucket, findings] of entries) {
    if (findings.some((finding) => expectedBucket(finding) !== bucket)) {
      fail("Review Finding buckets differ from ReviewReport.partition.");
    }
  }

  const expectedCounts = new Map<string, number>();
  for (const finding of allFindings(buckets)) {
    const key = JSON.stringify([finding.defect_class, finding.severity]);
    expectedCounts.set(key, (expectedCounts.get(key) ?? 0) + 1);
  }
  const actualCounts = report.by_defect_class ?? [];
  if (actualCounts.length !== expectedCounts.size) {
    fail("Review by_defect_class differs from its embedded Findings.");
  }
  const seenKeys = new Set<string>();
  for (const item of actualCounts) {
    const key = JSON.stringify([item.defect_class, item.severity]);
    if (seenKeys.has(key) || expectedCounts.get(key) !== item.count) {
      fail("Review by_defect_class differs from its embedded Findings.");
    }
    seenKeys.add(key);
  }
  return buckets;
}

export function findingBuckets(report: ReviewReport): LegacyFindingBuckets {
  return requireReviewPartition(report);
}

function producerRunIdFor(findings: readonly Finding[]): string | null {
  const producerRunIds = [...new Set(findings.map((item) => item.producer_run_id))];
  if (producerRunIds.length > 1) {
    fail("Review Findings do not bind a single producer Run.");
  }
  return producerRunIds[0] ?? null;
}

export function reviewProducerRunCandidate(report: ReviewReport): string | null {
  return producerRunIdFor(allFindings(findingBuckets(report)));
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  return `{${Object.entries(value as Record<string, unknown>)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, item]) => `${JSON.stringify(key)}:${canonicalJson(item)}`)
    .join(",")}}`;
}

function isFrozenSourceRefWithOmittedColumn(value: Record<string, unknown>): boolean {
  const allowedFields = new Set(["adapter", "column", "file", "row", "sheet"]);
  const hasOwn = (field: string): boolean => Object.prototype.hasOwnProperty.call(value, field);
  return (
    Object.keys(value).every((field) => allowedFields.has(field)) &&
    typeof value.adapter === "string" &&
    typeof value.file === "string" &&
    !hasOwn("column") &&
    (!hasOwn("row") || value.row === null || Number.isInteger(value.row)) &&
    (!hasOwn("sheet") || value.sheet === null || typeof value.sheet === "string")
  );
}

function normalizeOptionalSourceRefColumn(minimalRepro: Record<string, unknown>): Record<string, unknown> {
  const sourceRef = minimalRepro.source_ref;
  if (
    sourceRef === null ||
    typeof sourceRef !== "object" ||
    Array.isArray(sourceRef) ||
    !isFrozenSourceRefWithOmittedColumn(sourceRef as Record<string, unknown>)
  ) {
    return minimalRepro;
  }
  return {
    ...minimalRepro,
    source_ref: { ...(sourceRef as Record<string, unknown>), column: null },
  };
}

function legacySemanticPayload(finding: Finding): Record<string, unknown> {
  return {
    confidence: finding.confidence ?? null,
    constraint_id: finding.constraint_id ?? null,
    defect_class: finding.defect_class,
    entities: finding.entities ?? [],
    evidence: finding.evidence ?? {},
    message: finding.message,
    minimal_repro: normalizeOptionalSourceRefColumn(finding.minimal_repro ?? {}),
    oracle_type: finding.oracle_type,
    producer_id: finding.producer_id,
    producer_run_id: finding.producer_run_id,
    relations: finding.relations ?? [],
    severity: finding.severity,
    snapshot_id: finding.snapshot_id,
    source: finding.source,
    status: finding.status,
  };
}

function exactSemanticPayload(finding: FindingRevision): Record<string, unknown> {
  const { payload_schema_version: _schema, ...payload } = finding.payload;
  return {
    ...payload,
    confidence: payload.confidence ?? null,
    constraint_id: payload.constraint_id ?? null,
    entities: payload.entities ?? [],
    evidence: payload.evidence ?? {},
    minimal_repro: normalizeOptionalSourceRefColumn(payload.minimal_repro ?? {}),
    relations: payload.relations ?? [],
  };
}

function sameSummary(left: ArtifactSummary, right: ArtifactSummary): boolean {
  return (
    left.artifact_id === right.artifact_id &&
    left.kind === right.kind &&
    left.payload_schema_id === right.payload_schema_id &&
    left.payload_hash === right.payload_hash &&
    canonicalJson(left.version_tuple) === canonicalJson(right.version_tuple)
  );
}

export function requireReviewEnvelope(review: ReviewArtifactView, requestedArtifactId: string): void {
  const artifact = review.artifact;
  if (
    artifact.artifact_id !== requestedArtifactId ||
    artifact.kind !== "review_report" ||
    artifact.payload_schema_id !== "review@1" ||
    artifact.lineage_schema_version !== "lineage@2"
  ) {
    fail("Review route identity or Artifact contract does not match the requested report.");
  }
  if (review.report.review_schema_version !== "review@1" || !artifact.version_tuple.tool_version) {
    fail("Review schema or frozen tool identity is unavailable.");
  }
  const buckets = requireReviewPartition(review.report);
  const findings = allFindings(buckets);
  if (
    buckets.suggestion.length > 0 &&
    (!artifact.version_tuple.model_snapshot ||
      !artifact.version_tuple.prompt_version ||
      !artifact.version_tuple.agent_graph_version)
  ) {
    fail("LLM-assisted Review findings lack their frozen model, prompt, or agent graph identity.");
  }
  if (review.report.snapshot_id !== artifact.version_tuple.ir_snapshot_id) {
    fail("Review report snapshot differs from its immutable VersionTuple.");
  }
  if (findings.some((finding) => finding.snapshot_id !== review.report.snapshot_id)) {
    fail("Review embedded Finding differs from the report snapshot.");
  }
  producerRunIdFor(findings);
}

export function resolveReviewLineage(
  review: ReviewArtifactView,
  lineage: readonly LineageEntry[],
  requestedArtifactId: string,
  snapshotContextArtifactId?: string,
): ReviewLineageAuthority {
  requireReviewEnvelope(review, requestedArtifactId);
  const artifact = review.artifact;

  const direct = lineage.filter((entry) => entry.depth === 1).map((entry) => entry.artifact);
  const directIds = direct.map((item) => item.artifact_id).sort();
  const declaredIds = [...artifact.parent_artifact_ids].sort();
  if (canonicalJson(directIds) !== canonicalJson(declaredIds)) {
    fail("Review direct lineage is incomplete or differs from its Artifact parents.");
  }

  const previews = direct.filter((item) => item.kind === "ir_snapshot");
  if (
    previews.length !== 1 ||
    previews[0].payload_schema_id !== "ir-core@1" ||
    previews[0].version_tuple.ir_snapshot_id !== review.report.snapshot_id
  ) {
    fail("Review must bind one exact preview snapshot parent.");
  }

  const constraints = direct.filter((item) => item.kind === "constraint_snapshot");
  const constraintSnapshotId = artifact.version_tuple.constraint_snapshot_id ?? null;
  if (constraintSnapshotId === null) {
    if (constraints.length !== 0) {
      fail("Review has a constraint parent without a VersionTuple constraint binding.");
    }
  } else if (
    constraints.length !== 1 ||
    constraints[0].payload_schema_id !== "constraint-snapshot@1" ||
    constraints[0].version_tuple.constraint_snapshot_id !== constraintSnapshotId
  ) {
    fail("Review constraint parent differs from its immutable VersionTuple.");
  }

  return {
    constraint: constraints[0] ?? null,
    preview: previews[0],
    snapshotContextMatches:
      snapshotContextArtifactId === undefined ? null : previews[0].artifact_id === snapshotContextArtifactId,
  };
}

function bindFindings(
  report: ReviewReport,
  exactFindingLinks: readonly RunFindingLinkView[],
  producerBinding: ReviewProducerBinding | null,
): {
  buckets: ReviewFindingBuckets;
  producerRunId: string | null;
  findingAuthority: "exact-run-links" | "embedded-only" | "not-applicable";
} {
  const legacyBuckets = findingBuckets(report);
  const embedded = allFindings(legacyBuckets);
  const producerRunCandidate = producerRunIdFor(embedded);

  if (embedded.length === 0) {
    if (exactFindingLinks.length !== 0) {
      fail("An empty Review unexpectedly resolved Run-scoped Finding links.");
    }
    if (producerBinding !== null && producerBinding.finding_authority !== "not-applicable") {
      fail("An empty Review occurrence claims a Finding authority.");
    }
    return {
      buckets: { deterministic: [], simulation: [], suggestion: [], unproven: [] },
      findingAuthority: "not-applicable",
      producerRunId: producerBinding?.run_id ?? null,
    };
  }

  if (producerBinding === null || producerRunCandidate !== producerBinding.run_id) {
    fail("Review Findings do not close against a verified producer occurrence.");
  }

  if (producerBinding.finding_authority === "embedded-only") {
    if (exactFindingLinks.length !== 0) {
      fail("Embedded-only Review evidence unexpectedly resolved Run Finding links.");
    }
    const embeddedOnly = (items: Finding[]): ReviewFindingBinding[] =>
      items.map((item) => ({ embedded: item, exact: null }));
    return {
      buckets: {
        deterministic: embeddedOnly(legacyBuckets.deterministic),
        simulation: embeddedOnly(legacyBuckets.simulation),
        suggestion: embeddedOnly(legacyBuckets.suggestion),
        unproven: embeddedOnly(legacyBuckets.unproven),
      },
      findingAuthority: "embedded-only",
      producerRunId: producerBinding.run_id,
    };
  }

  if (producerBinding.finding_authority !== "exact-run-links") {
    fail("A non-empty Review occurrence has no usable Finding authority.");
  }
  if (exactFindingLinks.length !== embedded.length) {
    fail("Review Run Finding link closure is partial.");
  }
  const exactById = new Map<string, RunFindingLinkView>();
  const ordinals = new Set<number>();
  for (const exact of exactFindingLinks) {
    if (
      exact.view_schema_version !== "run-finding-link-view@1" ||
      exact.run_id !== producerBinding.run_id ||
      exact.attempt_no !== producerBinding.attempt_no ||
      exact.finding.payload.producer_run_id !== producerBinding.run_id ||
      exact.finding.payload.snapshot_id !== report.snapshot_id
    ) {
      fail("Run Finding link differs from the verified Review occurrence.");
    }
    if (exactById.has(exact.finding.finding_id) || ordinals.has(exact.ordinal)) {
      fail("Review Run returned duplicate Finding series identities.");
    }
    exactById.set(exact.finding.finding_id, exact);
    ordinals.add(exact.ordinal);
  }

  const exactBinding = (item: Finding): ReviewFindingBinding => {
    const exact = exactById.get(item.id);
    if (
      !exact ||
      canonicalJson(legacySemanticPayload(item)) !== canonicalJson(exactSemanticPayload(exact.finding))
    ) {
      fail("Review embedded Finding differs from its exact Run-scoped revision.");
    }
    return { embedded: item, exact };
  };

  return {
    buckets: {
      deterministic: legacyBuckets.deterministic.map(exactBinding),
      simulation: legacyBuckets.simulation.map(exactBinding),
      suggestion: legacyBuckets.suggestion.map(exactBinding),
      unproven: legacyBuckets.unproven.map(exactBinding),
    },
    findingAuthority: "exact-run-links",
    producerRunId: producerBinding.run_id,
  };
}

export function bindReviewAuthority({
  constraintAuthority,
  exactFindingLinks,
  lineage,
  previewAuthority,
  producerBinding,
  requestedArtifactId,
  review,
  sourceProducerBinding = null,
  sourceRunId,
  sourceRunOccurrence = null,
  snapshotContextArtifactId,
}: {
  constraintAuthority: ConstraintSnapshotView | null;
  exactFindingLinks: readonly RunFindingLinkView[];
  lineage: readonly LineageEntry[];
  previewAuthority: SpecView;
  producerBinding: ReviewProducerBinding | null;
  requestedArtifactId: string;
  review: ReviewArtifactView;
  sourceProducerBinding?: ReviewProducerBinding | null;
  sourceRunId?: string;
  sourceRunOccurrence?: "verified" | "not-found" | null;
  snapshotContextArtifactId?: string;
}): BoundReviewAuthority {
  const lineageAuthority = resolveReviewLineage(
    review,
    lineage,
    requestedArtifactId,
    snapshotContextArtifactId,
  );
  if (
    previewAuthority.snapshot_id !== review.report.snapshot_id ||
    !sameSummary(previewAuthority.artifact, lineageAuthority.preview)
  ) {
    fail("Dedicated Spec authority differs from the Review preview lineage.");
  }
  if (lineageAuthority.constraint === null) {
    if (constraintAuthority !== null) {
      fail("Review has no constraint lineage but a constraint authority was supplied.");
    }
  } else if (
    constraintAuthority === null ||
    !sameSummary(constraintAuthority.artifact, lineageAuthority.constraint)
  ) {
    fail("Dedicated constraint authority differs from the Review constraint lineage.");
  }
  if (
    producerBinding !== null &&
    (producerBinding.view_schema_version !== "review-producer-binding-view@1" ||
      producerBinding.review_artifact_id !== review.artifact.artifact_id ||
      producerBinding.run_id !== reviewProducerRunCandidate(review.report))
  ) {
    fail("Producer occurrence differs from the requested Review Artifact.");
  }
  if (sourceRunId === undefined) {
    if (sourceProducerBinding !== null || sourceRunOccurrence !== null) {
      fail("Source producer occurrence was supplied without an explicit source Run.");
    }
  } else if (sourceRunOccurrence === "verified") {
    if (
      sourceProducerBinding === null ||
      sourceProducerBinding.view_schema_version !== "review-producer-binding-view@1" ||
      sourceProducerBinding.review_artifact_id !== review.artifact.artifact_id ||
      sourceProducerBinding.run_id !== sourceRunId
    ) {
      fail("Explicit source Run does not match its verified Review producer occurrence.");
    }
  } else if (sourceRunOccurrence !== "not-found" || sourceProducerBinding !== null) {
    fail("Explicit source Run occurrence resolution is incomplete.");
  }

  return {
    ...lineageAuthority,
    ...bindFindings(review.report, exactFindingLinks, producerBinding),
    producerBinding,
    review,
    sourceProducerBinding,
    sourceRunId: sourceRunId ?? null,
    sourceRunOccurrence,
  };
}

export function requireExactFindingRoute(
  finding: FindingRevision,
  requestedFindingId: string,
  requestedRevision: number,
): FindingRevision {
  if (finding.finding_id !== requestedFindingId || finding.revision !== requestedRevision) {
    fail("Finding route does not match the immutable revision returned by the server.");
  }
  return finding;
}
