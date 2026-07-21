import { describe, expect, it } from "vitest";

import type { components } from "../../api/generated/openapi";
import { generationManifestArtifactIds, parseGenerationCandidateManifest } from "./candidate";

type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];
type ArtifactSummary = components["schemas"]["ArtifactSummaryV1"];

type ParentRole = "input" | "intermediate" | "output" | "evidence";

const hash = "a".repeat(64);

function summary(
  artifactId: string,
  kind: ArtifactSummary["kind"],
  payloadSchemaId: string,
  parentArtifactIds: string[] = [],
): ArtifactSummary {
  return {
    artifact_id: artifactId,
    created_at: "2026-07-20T00:00:00Z",
    domain_scope: { domain_ids: ["domain:economy"] },
    kind,
    lineage_schema_version: "lineage@2",
    parent_artifact_ids: parentArtifactIds,
    payload_hash: hash,
    payload_schema_id: payloadSchemaId,
    summary_schema_version: "artifact-summary@1",
    version_tuple: { tool_version: "generation@1" },
  };
}

function parent(artifactId: string, role: ParentRole) {
  return {
    artifact_id: artifactId,
    publication: role === "input" ? "existing" : "run_published",
    role,
  };
}

function projection(
  parents: ReturnType<typeof parent>[],
  runKind: "generation.propose" | "patch.repair" = "generation.propose",
  attemptNo = 1,
) {
  return {
    attempt_no: attemptNo,
    manifest_scope: "run",
    parents,
    projection_schema_version: "run-manifest-version-projection@1",
    run_kind: { kind: runKind, version: 1 },
  };
}

const patch = summary("artifact:patch:1", "patch", "patch@2");
const preview = summary("artifact:preview:1", "ir_snapshot", "ir-core@1");
const config = summary("artifact:config:1", "config_export", "config-export-package@1");
const checker = summary("artifact:checker:1", "checker_run", "checker-report@1");
const runtime = summary("artifact:cassette:1", "cassette_bundle", "cassette-bundle@2");
const runtimeRef = {
  artifactId: runtime.artifact_id,
  publication: "run_published",
  role: "intermediate",
} as const;

function successfulManifest(
  runKind: "generation.propose" | "patch.repair" = "generation.propose",
): ArtifactPayloadView {
  const parents = [
    parent(patch.artifact_id, "output"),
    parent(preview.artifact_id, "output"),
    parent(config.artifact_id, "output"),
    parent(checker.artifact_id, "evidence"),
    parent(runtime.artifact_id, "intermediate"),
  ];
  const producedArtifactIds = parents.map((item) => item.artifact_id).sort();
  return {
    artifact: summary("artifact:run-result:1", "run_result", "run-result@1", producedArtifactIds),
    payload: {
      attempt_no: 1,
      outcome_code: runKind === "generation.propose" ? "generation_gate_passed" : "repair_verified",
      primary_artifact_id: patch.artifact_id,
      produced_artifact_ids: producedArtifactIds,
      result_schema_version: "run-result@1",
      run_id: "run:generation:1",
      run_kind: { kind: runKind, version: 1 },
      version_projection: projection(parents, runKind),
    },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

function failureManifest(
  causeCode = "generation_gate_rejected",
  runKind: "generation.propose" | "patch.repair" = "generation.propose",
): ArtifactPayloadView {
  const parents =
    causeCode === "generation_gate_rejected"
      ? [
          parent(patch.artifact_id, "evidence"),
          parent(preview.artifact_id, "evidence"),
          parent(checker.artifact_id, "evidence"),
          parent(runtime.artifact_id, "intermediate"),
        ]
      : [parent(checker.artifact_id, "evidence"), parent(runtime.artifact_id, "intermediate")];
  const evidenceArtifactIds = parents.map((item) => item.artifact_id).sort();
  return {
    artifact: summary("artifact:run-failure:1", "run_failure", "run-failure@1", evidenceArtifactIds),
    payload: {
      attempt_no: 1,
      cause_code: causeCode,
      evidence_artifact_ids: evidenceArtifactIds,
      failure_schema_version: "run-failure@1",
      redacted_message: "The deterministic generation gate rejected this proposal.",
      run_id: "run:generation:1",
      run_kind: { kind: runKind, version: 1 },
      version_projection: projection(parents, runKind),
    },
    resource_revision: 1,
    view_schema_version: "artifact-payload-view@1",
  };
}

describe("parseGenerationCandidateManifest", () => {
  it("exposes only canonical public output/evidence IDs for the page fetch phase", () => {
    const success = successfulManifest();
    const failure = failureManifest();

    expect(generationManifestArtifactIds(success, "run:generation:1")).toEqual(
      ((success.payload as Record<string, unknown>).produced_artifact_ids as string[]).filter(
        (artifactId) => artifactId !== runtime.artifact_id,
      ),
    );
    expect(generationManifestArtifactIds(failure, "run:generation:1")).toEqual(
      ((failure.payload as Record<string, unknown>).evidence_artifact_ids as string[]).filter(
        (artifactId) => artifactId !== runtime.artifact_id,
      ),
    );

    const failureWithoutDetail = failureManifest();
    const fetchPayload = failureWithoutDetail.payload as Record<string, unknown>;
    delete fetchPayload.cause_code;
    delete fetchPayload.redacted_message;
    expect(generationManifestArtifactIds(failureWithoutDetail, "run:generation:1")).toEqual(
      (fetchPayload.evidence_artifact_ids as string[]).filter(
        (artifactId) => artifactId !== runtime.artifact_id,
      ),
    );
  });

  it("refuses malformed, non-canonical, or cross-Run fetch manifests", () => {
    const nonCanonical = successfulManifest();
    const payload = nonCanonical.payload as Record<string, unknown>;
    payload.produced_artifact_ids = [runtime.artifact_id, runtime.artifact_id];

    expect(generationManifestArtifactIds(nonCanonical, "run:generation:1")).toBeNull();
    const unsorted = successfulManifest();
    const unsortedPayload = unsorted.payload as Record<string, unknown>;
    unsortedPayload.produced_artifact_ids = [
      ...(unsortedPayload.produced_artifact_ids as string[]).slice(1),
      (unsortedPayload.produced_artifact_ids as string[])[0],
    ];
    expect(generationManifestArtifactIds(unsorted, "run:generation:1")).toBeNull();
    expect(generationManifestArtifactIds(successfulManifest(), "run:another")).toBeNull();
    expect(
      generationManifestArtifactIds(
        {
          ...successfulManifest(),
          artifact: { ...successfulManifest().artifact, payload_schema_id: "run-result@2" },
        },
        "run:generation:1",
      ),
    ).toBeNull();
  });

  it("closes a successful manifest without fetching sensitive intermediate payloads", () => {
    const result = parseGenerationCandidateManifest(successfulManifest(), "run:generation:1", [
      patch,
      preview,
      config,
      checker,
    ]);

    expect(result).toMatchObject({
      configExports: [config],
      evidence: [checker],
      intermediates: [runtimeRef],
      kind: "passed",
      manifestArtifactId: "artifact:run-result:1",
      patch,
      preview,
      primaryArtifactId: patch.artifact_id,
      runId: "run:generation:1",
      runKind: { kind: "generation.propose", version: 1 },
    });
    expect(result).not.toHaveProperty("workflowEligible");
    expect(result).not.toHaveProperty("canApply");
  });

  it("recognizes a verified repair successor without inheriting workflow eligibility", () => {
    const result = parseGenerationCandidateManifest(successfulManifest("patch.repair"), "run:generation:1", [
      patch,
      preview,
      config,
      checker,
    ]);

    expect(result).toMatchObject({
      kind: "passed",
      patch,
      preview,
      runKind: { kind: "patch.repair", version: 1 },
    });
    expect(result).not.toHaveProperty("workflowEligible");
  });

  it("does not require a config export in the generic successful manifest guard", () => {
    const manifest = successfulManifest();
    const payload = manifest.payload as Record<string, unknown>;
    payload.produced_artifact_ids = (payload.produced_artifact_ids as string[]).filter(
      (artifactId) => artifactId !== config.artifact_id,
    );
    const versionProjection = payload.version_projection as Record<string, unknown>;
    versionProjection.parents = (versionProjection.parents as ReturnType<typeof parent>[]).filter(
      (item) => item.artifact_id !== config.artifact_id,
    );
    manifest.artifact.parent_artifact_ids = manifest.artifact.parent_artifact_ids.filter(
      (artifactId) => artifactId !== config.artifact_id,
    );

    expect(
      parseGenerationCandidateManifest(manifest, "run:generation:1", [patch, preview, checker]),
    ).toMatchObject({ configExports: [], kind: "passed" });
  });

  it("returns an evidence-only gate rejection with one exact Patch and preview", () => {
    const result = parseGenerationCandidateManifest(failureManifest(), "run:generation:1", [
      patch,
      preview,
      checker,
    ]);

    expect(result).toMatchObject({
      causeCode: "generation_gate_rejected",
      evidence: [checker],
      intermediates: [runtimeRef],
      kind: "gate-rejected",
      message: "The deterministic generation gate rejected this proposal.",
      patch,
      preview,
      runId: "run:generation:1",
    });
    expect(result).not.toHaveProperty("configExports");
    expect(result).not.toHaveProperty("workflowEligible");
  });

  it("recognizes an ordinary failure without manufacturing a candidate or workflow state", () => {
    const result = parseGenerationCandidateManifest(
      failureManifest("provider_unavailable"),
      "run:generation:1",
      [checker],
    );

    expect(result).toMatchObject({
      causeCode: "provider_unavailable",
      evidence: [checker],
      intermediates: [runtimeRef],
      kind: "failure",
      runId: "run:generation:1",
    });
    expect(result).not.toHaveProperty("patch");
    expect(result).not.toHaveProperty("workflowEligible");
  });

  it.each([
    ["Patch schema", { ...patch, payload_schema_id: "patch@1" }],
    ["preview schema", { ...preview, payload_schema_id: "ir-core@2" }],
    ["config schema", { ...config, payload_schema_id: "config-export@1" }],
    ["evidence schema", { ...checker, payload_schema_id: "checker-report@2" }],
  ] satisfies [string, ArtifactSummary][])("fails closed for a lookalike %s", (_label, lookalike) => {
    const artifacts = [patch, preview, config, checker].map((item) =>
      item.artifact_id === lookalike.artifact_id ? lookalike : item,
    );

    expect(
      parseGenerationCandidateManifest(successfulManifest(), "run:generation:1", artifacts),
    ).toMatchObject({ kind: "unsafe", reason: "artifact_identity_mismatch" });
  });

  it("rejects a gate-rejected manifest that exposes output or config candidates", () => {
    const withOutput = failureManifest();
    const outputPayload = withOutput.payload as Record<string, unknown>;
    const outputProjection = outputPayload.version_projection as Record<string, unknown>;
    outputProjection.parents = [
      parent(patch.artifact_id, "output"),
      parent(preview.artifact_id, "evidence"),
      parent(checker.artifact_id, "evidence"),
      parent(runtime.artifact_id, "intermediate"),
    ];
    expect(
      parseGenerationCandidateManifest(withOutput, "run:generation:1", [patch, preview, checker]),
    ).toMatchObject({ kind: "unsafe", reason: "candidate_shape_mismatch" });

    const withConfig = failureManifest();
    const configPayload = withConfig.payload as Record<string, unknown>;
    const configProjection = configPayload.version_projection as Record<string, unknown>;
    const parents = configProjection.parents as ReturnType<typeof parent>[];
    parents.push(parent(config.artifact_id, "evidence"));
    configPayload.evidence_artifact_ids = [
      ...(configPayload.evidence_artifact_ids as string[]),
      config.artifact_id,
    ].sort();
    withConfig.artifact.parent_artifact_ids.push(config.artifact_id);
    withConfig.artifact.parent_artifact_ids.sort();
    expect(
      parseGenerationCandidateManifest(withConfig, "run:generation:1", [patch, preview, checker, config]),
    ).toMatchObject({ kind: "unsafe", reason: "candidate_shape_mismatch" });
  });

  it.each([
    [
      "outer manifest kind",
      () => ({
        ...successfulManifest(),
        artifact: { ...successfulManifest().artifact, kind: "run_failure" as const },
      }),
      "manifest_identity_mismatch",
      "run:generation:1",
    ],
    ["run id", () => successfulManifest(), "manifest_identity_mismatch", "run:another"],
    [
      "primary membership",
      () => {
        const manifest = successfulManifest();
        (manifest.payload as Record<string, unknown>).primary_artifact_id = "artifact:not-produced";
        return manifest;
      },
      "projection_identity_mismatch",
      "run:generation:1",
    ],
    [
      "projection collection",
      () => {
        const manifest = successfulManifest();
        const payload = manifest.payload as Record<string, unknown>;
        payload.produced_artifact_ids = (payload.produced_artifact_ids as string[]).slice(0, -1);
        return manifest;
      },
      "projection_identity_mismatch",
      "run:generation:1",
    ],
    [
      "manifest lineage",
      () => {
        const manifest = successfulManifest();
        manifest.artifact.parent_artifact_ids = manifest.artifact.parent_artifact_ids.slice(0, -1);
        return manifest;
      },
      "projection_identity_mismatch",
      "run:generation:1",
    ],
  ] as const)("fails closed for %s identity mismatch", (_label, makeManifest, reason, expectedRunId) => {
    expect(
      parseGenerationCandidateManifest(makeManifest(), expectedRunId, [patch, preview, config, checker]),
    ).toMatchObject({ kind: "unsafe", reason });
  });

  it("fails closed for malformed payloads and incomplete fetched Artifact identities", () => {
    const malformed = successfulManifest();
    malformed.payload = { result_schema_version: "run-result@1" };
    expect(parseGenerationCandidateManifest(malformed, "run:generation:1", [])).toMatchObject({
      kind: "unsafe",
      reason: "malformed_manifest",
    });

    expect(
      parseGenerationCandidateManifest(successfulManifest(), "run:generation:1", [patch, preview, config]),
    ).toMatchObject({ kind: "unsafe", reason: "artifact_identity_mismatch" });
  });
});
