import { describe, expect, it } from "vitest";

import type { components } from "../../api/generated/openapi";
import {
  applyIdentityConflictResolution,
  applyIdentityConflictValue,
  canonicalAttributeKeyPreview,
  snapshotArtifactToGraphDraft,
  toGraphItems,
} from "./model";

type ArtifactPayloadView = components["schemas"]["ArtifactPayloadViewV1"];

const preview = {
  artifact: {
    artifact_id: "artifact:preview",
    kind: "ir_snapshot",
    payload_schema_id: "ir-core@1",
  },
  payload: {
    entities: {
      "npc:sky-keeper": {
        attrs: { display_name: "守空人", "air.quality": "Clean" },
        schema_version: "ir-core@1",
        tags: ["主线"],
        type: "NPC",
      },
      "region:sky-harbor": {
        attrs: { display_name: "天空港" },
        schema_version: "ir-core@1",
        type: "REGION",
      },
    },
    meta_schema_version: "ir-meta@1",
    relations: {
      "rel:keeper-location": {
        attrs: {},
        dst_id: "region:sky-harbor",
        schema_version: "ir-core@1",
        src_id: "npc:sky-keeper",
        type: "LOCATED_IN",
      },
    },
  },
  resource_revision: 1,
  view_schema_version: "artifact-payload-view@1",
} as unknown as ArtifactPayloadView;

describe("project graph model", () => {
  it("reconstructs IDs omitted by canonical snapshot payloads and adapts the editable graph", () => {
    const draft = snapshotArtifactToGraphDraft(preview);

    expect(draft.entities.map((entity) => entity.id)).toEqual(["npc:sky-keeper", "region:sky-harbor"]);
    expect(draft.relations[0]).toMatchObject({
      dst_id: "region:sky-harbor",
      id: "rel:keeper-location",
      src_id: "npc:sky-keeper",
    });
    expect(toGraphItems(draft)).toHaveLength(3);
  });

  it("previews server canonical attribute keys and applies a visible conflict choice", () => {
    const draft = snapshotArtifactToGraphDraft(preview);
    const resolved = applyIdentityConflictResolution(
      draft,
      {
        candidates: [
          { op_id: "op:1", source_identity: "air.quality", value: "Clean" },
          { op_id: "op:2", source_identity: "air_quality", value: "Polluted" },
        ],
        canonical_identity: "npc:sky-keeper.air_quality",
        code: "attribute_value_conflict",
        conflict_id: "identity-conflict:air-quality",
        conflict_schema_version: "identity-conflict@1",
      },
      1,
    );

    expect(canonicalAttributeKeyPreview(" Air.Quality ")).toBe("air_quality");
    expect(resolved.entities[0]!.attrs?.air_quality).toBe("Polluted");
  });

  it("applies a planner-authored final value without pretending an absent graph target was resolved", () => {
    const draft = snapshotArtifactToGraphDraft(preview);
    const conflict = {
      candidates: [
        { op_id: "op:1", source_identity: "air.quality", value: "Clean" },
        { op_id: "op:2", source_identity: "air_quality", value: "Polluted" },
      ],
      canonical_identity: "npc:sky-keeper.air_quality",
      code: "attribute_value_conflict" as const,
      conflict_id: "identity-conflict:air-quality",
      conflict_schema_version: "identity-conflict@1" as const,
    };

    const resolved = applyIdentityConflictValue(draft, conflict, "Variable");

    expect(resolved.entities[0]!.attrs?.air_quality).toBe("Variable");
    expect(() =>
      applyIdentityConflictValue(
        draft,
        { ...conflict, canonical_identity: "rel:missing.src_id", code: "dangling_relation_endpoint" },
        "npc:sky-keeper",
      ),
    ).toThrow(/关系 .*不存在/);
  });

  it("fails closed for a payload that is not an IR snapshot", () => {
    expect(() =>
      snapshotArtifactToGraphDraft({
        ...preview,
        artifact: { ...preview.artifact, kind: "patch" },
      } as ArtifactPayloadView),
    ).toThrow(/IR snapshot/);
  });
});
