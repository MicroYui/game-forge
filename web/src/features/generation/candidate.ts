import type { components } from "../../api/generated/openapi";

type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];

export type GenerationRunKind = {
  kind: "generation.propose" | "patch.repair";
  version: 1;
};

export type GenerationManifestParent = {
  artifactId: string;
  publication: "existing" | "run_published";
  role: "input" | "intermediate" | "output" | "evidence";
};

export type GenerationManifestVersionProjection = {
  attemptNo: number | null;
  manifestScope: "run";
  parents: GenerationManifestParent[];
  projectionSchemaVersion: "run-manifest-version-projection@1";
  runKind: GenerationRunKind;
};

type ParsedManifestBase = {
  manifestArtifactId: string;
  runId: string;
  runKind: GenerationRunKind;
  versionProjection: GenerationManifestVersionProjection;
};

export type PassedGenerationCandidate = ParsedManifestBase & {
  configExports: ArtifactSummary[];
  evidence: ArtifactSummary[];
  intermediates: GenerationManifestParent[];
  kind: "passed";
  patch: ArtifactSummary;
  preview: ArtifactSummary;
  primaryArtifactId: string;
};

export type RejectedGenerationCandidate = ParsedManifestBase & {
  causeCode: "generation_gate_rejected";
  evidence: ArtifactSummary[];
  intermediates: GenerationManifestParent[];
  kind: "gate-rejected";
  message: string;
  patch: ArtifactSummary;
  preview: ArtifactSummary;
};

export type FailedGenerationCandidate = ParsedManifestBase & {
  causeCode: string;
  evidence: ArtifactSummary[];
  intermediates: GenerationManifestParent[];
  kind: "failure";
  message: string;
};

export type UnsafeGenerationCandidate = {
  kind: "unsafe";
  manifestArtifactId: string;
  reason:
    | "artifact_identity_mismatch"
    | "candidate_shape_mismatch"
    | "malformed_manifest"
    | "manifest_identity_mismatch"
    | "projection_identity_mismatch";
};

export type GenerationCandidateManifest =
  | PassedGenerationCandidate
  | RejectedGenerationCandidate
  | FailedGenerationCandidate
  | UnsafeGenerationCandidate;

type ManifestKind = "failure" | "result";

type ParsedProjection = {
  attemptNo: number | null;
  parents: GenerationManifestParent[];
  runKind: GenerationRunKind;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function canonicalStringArray(value: unknown, allowEmpty: boolean): string[] | null {
  if (!Array.isArray(value) || (!allowEmpty && value.length === 0)) return null;
  if (!value.every(isNonEmptyString)) return null;
  const strings = value as string[];
  if (new Set(strings).size !== strings.length) return null;
  const sorted = [...strings].sort();
  if (!strings.every((item, index) => item === sorted[index])) return null;
  return [...strings];
}

function sameSet(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((item) => right.includes(item));
}

function manifestKind(manifest: ArtifactPayloadView): ManifestKind | null {
  if (manifest.artifact.kind === "run_result" && manifest.artifact.payload_schema_id === "run-result@1") {
    return "result";
  }
  if (manifest.artifact.kind === "run_failure" && manifest.artifact.payload_schema_id === "run-failure@1") {
    return "failure";
  }
  return null;
}

function manifestCollection(
  manifest: ArtifactPayloadView,
  expectedRunId: string,
): { ids: string[]; kind: ManifestKind; payload: Record<string, unknown> } | null {
  const kind = manifestKind(manifest);
  const payload = manifest.payload;
  if (
    kind === null ||
    !isRecord(payload) ||
    !isNonEmptyString(payload.run_id) ||
    payload.run_id !== expectedRunId
  ) {
    return null;
  }

  if (kind === "result") {
    if (payload.result_schema_version !== "run-result@1" || !isNonEmptyString(payload.primary_artifact_id)) {
      return null;
    }
    const ids = canonicalStringArray(payload.produced_artifact_ids, false);
    if (ids === null || !ids.includes(payload.primary_artifact_id)) return null;
    return { ids, kind, payload };
  }

  if (payload.failure_schema_version !== "run-failure@1") {
    return null;
  }
  const ids = canonicalStringArray(payload.evidence_artifact_ids, true);
  return ids === null ? null : { ids, kind, payload };
}

/** Returns only public output/evidence IDs; sensitive intermediates remain manifest-only refs. */
export function generationManifestArtifactIds(
  manifest: ArtifactPayloadView,
  expectedRunId: string,
): string[] | null {
  const collection = manifestCollection(manifest, expectedRunId);
  if (collection === null) return null;
  const projection = parseProjection(collection.payload.version_projection);
  if (projection === null) return null;
  const published = projection.parents.filter(
    (parent) => parent.publication === "run_published" && parent.role !== "input",
  );
  if (
    !sameSet(
      collection.ids,
      published.map((parent) => parent.artifactId),
    )
  )
    return null;
  const readableIds = new Set(
    published
      .filter((parent) => parent.role === "output" || parent.role === "evidence")
      .map((parent) => parent.artifactId),
  );
  return collection.ids.filter((artifactId) => readableIds.has(artifactId));
}

function parseRunKind(value: unknown): GenerationRunKind | null {
  if (!isRecord(value) || value.version !== 1) return null;
  if (value.kind !== "generation.propose" && value.kind !== "patch.repair") return null;
  return { kind: value.kind, version: 1 };
}

function parseAttemptNo(value: unknown): number | null | undefined {
  if (value === null) return null;
  if (typeof value === "number" && Number.isInteger(value) && value > 0) return value;
  return undefined;
}

function parseProjection(value: unknown): ParsedProjection | null {
  if (
    !isRecord(value) ||
    value.projection_schema_version !== "run-manifest-version-projection@1" ||
    value.manifest_scope !== "run" ||
    !Array.isArray(value.parents)
  ) {
    return null;
  }
  const attemptNo = parseAttemptNo(value.attempt_no);
  const runKind = parseRunKind(value.run_kind);
  if (attemptNo === undefined || runKind === null) return null;

  const parents: GenerationManifestParent[] = [];
  const ids = new Set<string>();
  for (const valueParent of value.parents) {
    if (
      !isRecord(valueParent) ||
      !isNonEmptyString(valueParent.artifact_id) ||
      (valueParent.role !== "input" &&
        valueParent.role !== "intermediate" &&
        valueParent.role !== "output" &&
        valueParent.role !== "evidence") ||
      (valueParent.publication !== "existing" && valueParent.publication !== "run_published") ||
      ids.has(valueParent.artifact_id)
    ) {
      return null;
    }
    ids.add(valueParent.artifact_id);
    parents.push({
      artifactId: valueParent.artifact_id,
      publication: valueParent.publication,
      role: valueParent.role,
    });
  }
  return { attemptNo, parents, runKind };
}

function sameRunKind(left: GenerationRunKind, right: GenerationRunKind): boolean {
  return left.kind === right.kind && left.version === right.version;
}

function unsafe(
  manifest: ArtifactPayloadView,
  reason: UnsafeGenerationCandidate["reason"],
): UnsafeGenerationCandidate {
  return { kind: "unsafe", manifestArtifactId: manifest.artifact.artifact_id, reason };
}

function fetchedArtifactMap(
  ids: readonly string[],
  fetchedArtifacts: readonly ArtifactSummary[],
): Map<string, ArtifactSummary> | null {
  if (fetchedArtifacts.length !== ids.length) return null;
  const fetched = new Map<string, ArtifactSummary>();
  for (const artifact of fetchedArtifacts) {
    if (
      !ids.includes(artifact.artifact_id) ||
      fetched.has(artifact.artifact_id) ||
      !isNonEmptyString(artifact.payload_schema_id)
    ) {
      return null;
    }
    fetched.set(artifact.artifact_id, artifact);
  }
  return fetched;
}

function classifyArtifact(artifact: ArtifactSummary): "config" | "evidence" | "patch" | "preview" | null {
  if (artifact.kind === "patch") return artifact.payload_schema_id === "patch@2" ? "patch" : null;
  if (artifact.kind === "ir_snapshot") {
    return artifact.payload_schema_id === "ir-core@1" ? "preview" : null;
  }
  if (artifact.kind === "config_export") {
    return artifact.payload_schema_id === "config-export-package@1" ? "config" : null;
  }
  if (artifact.kind === "checker_run") {
    return artifact.payload_schema_id === "checker-report@1" ? "evidence" : null;
  }
  if (artifact.kind === "simulation_run") {
    return artifact.payload_schema_id === "simulation-result@1" ? "evidence" : null;
  }
  if (artifact.kind === "review_report") {
    return artifact.payload_schema_id === "review@1" ? "evidence" : null;
  }
  if (artifact.kind === "regression_evidence") {
    return artifact.payload_schema_id === "regression-evidence@1" ? "evidence" : null;
  }
  return null;
}

function projectionView(projection: ParsedProjection): GenerationManifestVersionProjection {
  return {
    attemptNo: projection.attemptNo,
    manifestScope: "run",
    parents: projection.parents,
    projectionSchemaVersion: "run-manifest-version-projection@1",
    runKind: projection.runKind,
  };
}

export function parseGenerationCandidateManifest(
  manifest: ArtifactPayloadView,
  expectedRunId: string,
  fetchedArtifacts: readonly ArtifactSummary[],
): GenerationCandidateManifest {
  const kind = manifestKind(manifest);
  if (kind === null) return unsafe(manifest, "manifest_identity_mismatch");
  if (!isRecord(manifest.payload)) return unsafe(manifest, "malformed_manifest");
  const payload = manifest.payload;
  const expectedDiscriminator = kind === "result" ? "run-result@1" : "run-failure@1";
  const discriminator = kind === "result" ? payload.result_schema_version : payload.failure_schema_version;
  if (discriminator !== expectedDiscriminator) return unsafe(manifest, "malformed_manifest");
  if (!isNonEmptyString(payload.run_id)) return unsafe(manifest, "malformed_manifest");
  if (payload.run_id !== expectedRunId) return unsafe(manifest, "manifest_identity_mismatch");

  const collection = manifestCollection(manifest, expectedRunId);
  if (collection === null) {
    if (
      kind === "result" &&
      isNonEmptyString(payload.primary_artifact_id) &&
      canonicalStringArray(payload.produced_artifact_ids, false) !== null
    ) {
      return unsafe(manifest, "projection_identity_mismatch");
    }
    return unsafe(manifest, "malformed_manifest");
  }

  const runKind = parseRunKind(payload.run_kind);
  const attemptNo = parseAttemptNo(payload.attempt_no);
  const projection = parseProjection(payload.version_projection);
  if (runKind === null || attemptNo === undefined || projection === null) {
    return unsafe(manifest, "malformed_manifest");
  }
  if (attemptNo !== projection.attemptNo || !sameRunKind(runKind, projection.runKind)) {
    return unsafe(manifest, "projection_identity_mismatch");
  }

  const projectedPublishedIds = projection.parents
    .filter((item) => item.publication === "run_published" && item.role !== "input")
    .map((item) => item.artifactId);
  const projectedAllIds = projection.parents.map((item) => item.artifactId);
  const lineageIds = canonicalStringArray(manifest.artifact.parent_artifact_ids, true);
  if (
    !sameSet(collection.ids, projectedPublishedIds) ||
    lineageIds === null ||
    !sameSet(lineageIds, projectedAllIds)
  ) {
    return unsafe(manifest, "projection_identity_mismatch");
  }

  const readableIds = collection.ids.filter((artifactId) =>
    projection.parents.some(
      (parent) =>
        parent.artifactId === artifactId &&
        parent.publication === "run_published" &&
        (parent.role === "output" || parent.role === "evidence"),
    ),
  );
  const fetched = fetchedArtifactMap(readableIds, fetchedArtifacts);
  if (fetched === null) return unsafe(manifest, "artifact_identity_mismatch");
  const artifacts = readableIds.map((artifactId) => fetched.get(artifactId)!);
  const intermediates = projection.parents.filter(
    (parent) => parent.publication === "run_published" && parent.role === "intermediate",
  );

  const base = {
    manifestArtifactId: manifest.artifact.artifact_id,
    runId: expectedRunId,
    runKind,
    versionProjection: projectionView(projection),
  };

  if (kind === "failure") {
    const causeCode = payload.cause_code;
    const message = payload.redacted_message;
    if (!isNonEmptyString(causeCode) || !isNonEmptyString(message)) {
      return unsafe(manifest, "malformed_manifest");
    }
    if (causeCode !== "generation_gate_rejected" || runKind.kind !== "generation.propose") {
      if (projection.parents.some((item) => item.role === "output")) {
        return unsafe(manifest, "projection_identity_mismatch");
      }
      const parentById = new Map(projection.parents.map((item) => [item.artifactId, item]));
      const evidence: ArtifactSummary[] = [];
      for (const artifact of artifacts) {
        const role = parentById.get(artifact.artifact_id)?.role;
        if (role === "evidence" && classifyArtifact(artifact) === "evidence") {
          evidence.push(artifact);
        } else {
          return unsafe(manifest, "artifact_identity_mismatch");
        }
      }
      return { ...base, causeCode, evidence, intermediates, kind: "failure", message };
    }

    if (projection.parents.some((item) => item.role === "output")) {
      return unsafe(manifest, "candidate_shape_mismatch");
    }
    const patches: ArtifactSummary[] = [];
    const previews: ArtifactSummary[] = [];
    const evidence: ArtifactSummary[] = [];
    const parentById = new Map(projection.parents.map((item) => [item.artifactId, item]));
    for (const artifact of artifacts) {
      const role = parentById.get(artifact.artifact_id)?.role;
      if (role !== "evidence") return unsafe(manifest, "candidate_shape_mismatch");
      const category = classifyArtifact(artifact);
      if (category === null) return unsafe(manifest, "artifact_identity_mismatch");
      if (category === "config") return unsafe(manifest, "candidate_shape_mismatch");
      if (category === "patch") patches.push(artifact);
      else if (category === "preview") previews.push(artifact);
      else evidence.push(artifact);
    }
    if (
      patches.length !== 1 ||
      previews.length !== 1 ||
      parentById.get(patches[0].artifact_id)?.role !== "evidence" ||
      parentById.get(previews[0].artifact_id)?.role !== "evidence"
    ) {
      return unsafe(manifest, "candidate_shape_mismatch");
    }
    return {
      ...base,
      causeCode: "generation_gate_rejected",
      evidence,
      intermediates,
      kind: "gate-rejected",
      message,
      patch: patches[0],
      preview: previews[0],
    };
  }

  const primaryArtifactId = payload.primary_artifact_id;
  const outcomeCode = payload.outcome_code;
  if (!isNonEmptyString(primaryArtifactId)) {
    return unsafe(manifest, "projection_identity_mismatch");
  }
  if (
    (runKind.kind === "generation.propose" && outcomeCode !== "generation_gate_passed") ||
    (runKind.kind === "patch.repair" && outcomeCode !== "repair_verified")
  ) {
    return unsafe(manifest, "projection_identity_mismatch");
  }

  const patches: ArtifactSummary[] = [];
  const previews: ArtifactSummary[] = [];
  const configExports: ArtifactSummary[] = [];
  const evidence: ArtifactSummary[] = [];
  const parentById = new Map(projection.parents.map((item) => [item.artifactId, item]));
  for (const artifact of artifacts) {
    const parentRole = parentById.get(artifact.artifact_id)?.role;
    const category = classifyArtifact(artifact);
    if (category === null) return unsafe(manifest, "artifact_identity_mismatch");
    if (parentRole === "evidence" && category === "evidence") {
      evidence.push(artifact);
      continue;
    }
    if (parentRole !== "output" || category === "evidence") {
      return unsafe(manifest, "candidate_shape_mismatch");
    }
    if (category === "patch") patches.push(artifact);
    else if (category === "preview") previews.push(artifact);
    else configExports.push(artifact);
  }
  if (patches.length !== 1 || previews.length !== 1 || patches[0].artifact_id !== primaryArtifactId) {
    return unsafe(manifest, "candidate_shape_mismatch");
  }
  return {
    ...base,
    configExports,
    evidence,
    intermediates,
    kind: "passed",
    patch: patches[0],
    preview: previews[0],
    primaryArtifactId,
  };
}
